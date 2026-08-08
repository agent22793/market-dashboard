"""
collect_data.py

Runs daily via GitHub Actions. Pulls index/VIX prices (yfinance, free, no key)
and the CNN Fear & Greed Index (public unofficial endpoint), computes a
composite 0-100 Market Score, classifies the overall regime, and writes:

    data/latest.json   -> current snapshot the dashboard reads
    data/history.csv    -> one row appended per run, powers the charts

If the regime changes since the last run (e.g. Bullish -> Neutral), it's
logged to the "Recent Alerts" list in latest.json, and — if email env vars
are set — an alert email is sent.

No API keys required for the core data. Email is optional (see README).
"""

import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.csv"

INDEX_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "russell": "^RUT",
}
VIX_TICKER = "^VIX"
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLY", "XLP", "XLV", "XLI", "XLB", "XLU", "XLRE", "XLC"]

FNG_API_URL = "https://feargreedchart.com/api/?action=all"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-dashboard-bot/1.0)"}

NASDAQ100_API_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

# Fallback only — used if the live api.nasdaq.com fetch below fails. Wikipedia
# no longer maintains a scrapable Nasdaq-100 table (removed from the article),
# and the Invesco QQQ holdings page loads via JS rather than a static file, so
# there's no other free, reliable source to fall back to. This list is not
# actively maintained; it exists purely so one bad day for Nasdaq's API doesn't
# take out the whole breadth panel. Last verified: Aug 2026.
NASDAQ100_FALLBACK_TICKERS = [
    "ADBE", "ADP", "AMD", "ABNB", "ALNY", "GOOGL", "GOOG", "AMZN", "AEP", "AMGN",
    "ADI", "AAPL", "AMAT", "APP", "ARM", "ASML", "TEAM", "ADSK", "AXON", "BKR",
    "BKNG", "AVGO", "CDNS", "CHTR", "CTAS", "CSCO", "CCEP", "CTSH", "CMCSA", "CEG",
    "CPRT", "CSGP", "COST", "CRWD", "CSX", "DDOG", "DXCM", "FANG", "DASH", "EA",
    "EXC", "FAST", "FER", "FTNT", "GEHC", "GILD", "HON", "IDXX", "INSM", "INTC",
    "INTU", "ISRG", "KDP", "KLAC", "KHC", "LRCX", "LIN", "MAR", "MRVL", "MELI",
    "META", "MCHP", "MU", "MSFT", "MSTR", "MDLZ", "MPWR", "MNST", "NFLX", "NVDA",
    "NXPI", "ODFL", "ORLY", "PCAR", "PLTR", "PANW", "PAYX", "PYPL", "PDD", "PEP",
    "QCOM", "REGN", "ROP", "ROST", "STX", "SHOP", "SBUX", "SNPS", "TTWO", "TSLA",
    "TXN", "TRI", "TMUS", "VRSK", "VRTX", "WMT", "WBD", "WDC", "WDAY", "XEL", "ZS",
    "ALAB",
]


def fetch_nasdaq100_tickers() -> list:
    """Pulls the current Nasdaq-100 list from Nasdaq's own (unofficial, but
    functional) JSON API — the same endpoint their own website's list pages
    use internally. No key required, but undocumented and sometimes slow to
    respond from cloud/datacenter IPs (which is what GitHub Actions runs on),
    so this retries once with a longer timeout before giving up. Falls back
    to the static list if both attempts fail.
    """
    for attempt, timeout in enumerate((20, 40), start=1):
        try:
            resp = requests.get(NASDAQ100_API_URL, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            rows = resp.json()["data"]["data"]["rows"]
            tickers = [row["symbol"].strip() for row in rows if row.get("symbol")]
            if len(tickers) < 90:
                raise ValueError(f"Unexpectedly few tickers returned ({len(tickers)})")
            return tickers
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Live Nasdaq-100 fetch attempt {attempt} failed: {exc}")
    print("[warn] Both Nasdaq-100 fetch attempts failed; using the built-in fallback list (may be slightly stale).")
    return NASDAQ100_FALLBACK_TICKERS


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_index_data(ticker: str) -> dict:
    hist = None
    last_exc = None
    for attempt, delay in enumerate((0, 15, 45), start=1):
        if delay:
            print(f"[warn] {ticker}: retrying after rate limit (attempt {attempt}, waited {delay}s)")
            time.sleep(delay)
        try:
            hist = yf.Ticker(ticker).history(period="400d", interval="1d")
            if not hist.empty and len(hist) >= 20:
                break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            hist = None
    if hist is None or hist.empty or len(hist) < 20:
        raise ValueError(f"No data for {ticker} after 3 attempts") from last_exc
    close = hist["Close"]
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    change_pct = (last / prev - 1) * 100
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    high_252 = float(close.rolling(252).max().iloc[-1]) if len(close) >= 20 else last
    off_high = (last / max(high_252, last) - 1) * 100
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close) >= 20 else None
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else None
    ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1]) if len(close) >= 200 else None
    return {
        "price": last,
        "change_pct": change_pct,
        "sma50": sma50,
        "sma200": sma200,
        "off_52w_high_pct": off_high,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
    }


def compute_trend_strength(spx: dict) -> dict | None:
    """Classifies trend direction/strength from EMA20/50/200 alignment on the
    S&P 500, with a confidence score derived from how cleanly separated the
    EMAs are (as a % of price) — tightly bunched EMAs mean a weaker, less
    reliable read even when the ordering technically qualifies as a trend.
    """
    ema20, ema50, ema200 = spx.get("ema20"), spx.get("ema50"), spx.get("ema200")
    price = spx.get("price")
    if not all([ema20, ema50, ema200, price]):
        return None

    if ema20 > ema50 > ema200:
        label, arrow, base = "Strong Uptrend", "▲", 70
        relation = "EMA20 > EMA50 > EMA200"
    elif ema20 > ema50:
        label, arrow, base = "Uptrend", "▲", 50
        relation = "EMA20 > EMA50, EMA50 ≤ EMA200"
    elif ema20 < ema50 < ema200:
        label, arrow, base = "Strong Downtrend", "▼", 70
        relation = "EMA20 < EMA50 < EMA200"
    elif ema20 < ema50:
        label, arrow, base = "Downtrend", "▼", 50
        relation = "EMA20 < EMA50, EMA50 ≥ EMA200"
    else:
        label, arrow, base = "Sideways", "─", 25
        relation = "EMA20 ≈ EMA50"

    # Spread between EMA20 and EMA200, as a % of price, is the separation
    # signal — a 10% spread is treated as a "fully confident" reading; smaller
    # spreads (EMAs bunched close together) scale confidence down from there,
    # since a technically-qualifying trend on barely-separated EMAs is a much
    # weaker read than one with wide separation.
    spread_pct = abs(ema20 - ema200) / price * 100
    confidence = round(min(base + min(spread_pct / 10 * 25, 25), 97))

    return {"label": label, "arrow": arrow, "confidence": confidence, "relation": relation}


def vix_level_label(vix: float) -> str:
    if vix < 15:
        return "very calm"
    if vix < 20:
        return "calm"
    if vix < 25:
        return "moderate"
    if vix < 30:
        return "elevated"
    return "high stress"


def build_market_summary(spx: dict, market: dict, trend: dict | None, vix_price: float,
                          internals: dict | None, fng: dict) -> list:
    """Five plain-language bullets recapping the same data already computed
    elsewhere on the dashboard — Overall status plus one bullet per score
    component (Trend, Volatility, Breadth, Sentiment), so it mirrors "How the
    Score Works" as a quick "catch me up" read. Deterministic and
    template-based (no external AI call), so it's free and can't hallucinate
    a number.
    """
    bullets = [
        f"{market['emoji']} Overall market is {market['label']} with a Market Score of {market['score']}/100."
    ]

    if trend:
        bullets.append(
            f"{trend['arrow']} Trend: {trend['label']} ({trend['relation']}), "
            f"{trend['confidence']}% confidence."
        )
    else:
        bullets.append("Trend data unavailable this run.")

    bullets.append(f"Volatility: VIX at {vix_price:.1f} — {vix_level_label(vix_price)} conditions.")

    if internals:
        lean = "more advancers than decliners" if internals["advancers"] > internals["decliners"] else \
               "more decliners than advancers" if internals["decliners"] > internals["advancers"] else \
               "advancers and decliners roughly even"
        bullets.append(
            f"Breadth: {internals['pct_above_sma200']}% of Nasdaq-100 stocks above their 200-day average, "
            f"{lean} today ({internals['advancers']} vs {internals['decliners']})."
        )
    else:
        bullets.append("Breadth data unavailable this run.")

    if fng.get("score") is not None:
        bullets.append(f"Sentiment: Fear & Greed at {fng['score']} ({fng['rating']}).")
    else:
        bullets.append("Sentiment data unavailable this run.")

    return bullets[:5]


def fetch_sector_breadth() -> dict:
    above = 0
    total = 0
    for symbol in SECTOR_ETFS:
        try:
            hist = yf.Ticker(symbol).history(period="250d", interval="1d")
            if len(hist) < 200:
                continue
            close = hist["Close"]
            last = float(close.iloc[-1])
            sma200 = float(close.rolling(200).mean().iloc[-1])
            total += 1
            if last > sma200:
                above += 1
        except Exception:
            continue
    return {"sectors_above_200sma": above, "sectors_total": total}


def fetch_market_internals(tickers: list) -> dict | None:
    """Real market breadth across the given ticker universe (currently the
    Nasdaq-100): advancers/decliners, new 52-week highs/lows, and % of
    stocks above their 20/50/200-day averages. One batched download rather
    than one call per ticker, to stay well within Yahoo's informal rate limits.
    """
    if not tickers:
        return None
    try:
        data = yf.download(
            tickers, period="400d", interval="1d",
            group_by="ticker", threads=True, progress=False, auto_adjust=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Batched internals download failed: {exc}")
        return None

    advancers = decliners = unchanged = 0
    new_highs = new_lows = 0
    above20 = above50 = above200 = 0
    counted = 0
    constituents = []

    for t in tickers:
        try:
            closes = data[t]["Close"].dropna()
            if len(closes) < 25:
                continue
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            counted += 1

            if last > prev:
                advancers += 1
            elif last < prev:
                decliners += 1
            else:
                unchanged += 1

            window = closes.tail(252)
            is_new_high = last >= float(window.max())
            is_new_low = last <= float(window.min())
            if is_new_high:
                new_highs += 1
            if is_new_low:
                new_lows += 1

            above_sma5 = len(closes) >= 5 and last > float(closes.rolling(5).mean().iloc[-1])
            above_sma10 = len(closes) >= 10 and last > float(closes.rolling(10).mean().iloc[-1])
            above_sma20_ticker = len(closes) >= 20 and last > float(closes.rolling(20).mean().iloc[-1])
            above_sma50_ticker = len(closes) >= 50 and last > float(closes.rolling(50).mean().iloc[-1])
            above_sma200 = len(closes) >= 200 and last > float(closes.rolling(200).mean().iloc[-1])
            if above_sma20_ticker:
                above20 += 1
            if above_sma50_ticker:
                above50 += 1
            if above_sma200:
                above200 += 1

            # Weighted trailing-quarter return, used below to rank all tickers into a
            # 1-99 relative-strength percentile — our own independent approximation of
            # the concept IBD's RS Rating measures (40% weight on the most recent
            # quarter, 20% each on the prior three), not their proprietary number.
            rs_raw = None
            if len(closes) >= 253:
                p_now = float(closes.iloc[-1])
                p_q1 = float(closes.iloc[-64])
                p_q2 = float(closes.iloc[-127])
                p_q3 = float(closes.iloc[-190])
                p_q4 = float(closes.iloc[-253])
                q1r = p_now / p_q1 - 1
                q2r = p_q1 / p_q2 - 1
                q3r = p_q2 / p_q3 - 1
                q4r = p_q3 / p_q4 - 1
                rs_raw = 0.4 * q1r + 0.2 * q2r + 0.2 * q3r + 0.2 * q4r

            constituents.append({
                "symbol": t,
                "price": round(last, 2),
                "change_pct": round((last / prev - 1) * 100, 2),
                "above_sma5": above_sma5,
                "above_sma10": above_sma10,
                "above_sma20": above_sma20_ticker,
                "above_sma50": above_sma50_ticker,
                "above_sma200": above_sma200,
                "new_high": is_new_high,
                "new_low": is_new_low,
                "_rs_raw": rs_raw,
            })
        except Exception:
            continue  # skip any single ticker that failed to download cleanly

    if counted == 0:
        return None

    # Convert raw weighted-return scores into a 1-99 percentile rank across
    # the whole universe (IBD's RS Rating scale convention) -- ranked only
    # among tickers with enough history; the rest get rs_rating: null.
    ranked = sorted(
        (c for c in constituents if c["_rs_raw"] is not None),
        key=lambda c: c["_rs_raw"],
    )
    n = len(ranked)
    for i, c in enumerate(ranked):
        c["rs_rating"] = round(1 + i / (n - 1) * 98) if n > 1 else 50
    for c in constituents:
        c.setdefault("rs_rating", None)
        c.pop("_rs_raw", None)

    constituents.sort(key=lambda c: c["change_pct"], reverse=True)

    return {
        "universe_size": counted,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "new_highs": new_highs,
        "new_lows": new_lows,
        "pct_above_sma20": round(above20 / counted * 100, 1),
        "pct_above_sma50": round(above50 / counted * 100, 1),
        "pct_above_sma200": round(above200 / counted * 100, 1),
        "constituents": constituents,
    }


def fetch_fear_greed() -> dict:
    """Fear & Greed Index via FearGreedChart.com's free public JSON API —
    documented, no key required, CORS-enabled, 15-minute server-side cache.
    Also captures the 5-component breakdown (Momentum, Volatility, Safe
    Haven, Breadth, Junk Bonds) the composite score is built from. Falls
    back gracefully to None if unreachable — the dashboard should not break
    just because this one field is missing.
    """
    try:
        resp = requests.get(FNG_API_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        score = int(payload["score"]["score"])
        rating = (
            "Extreme Fear" if score <= 20 else
            "Fear" if score <= 40 else
            "Neutral" if score <= 60 else
            "Greed" if score <= 80 else
            "Extreme Greed"
        )
        components = [
            {"name": c.get("name"), "val": c.get("val")}
            for c in payload.get("score", {}).get("components", [])
        ]
        return {"score": score, "rating": rating, "components": components}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Fear & Greed fetch failed: {exc}")
        return {"score": None, "rating": None, "components": []}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def compute_market_score(spx: dict, vix_price: float, breadth: dict, fng: dict, internals: dict | None = None) -> dict:
    # Trend: reward being above SMA50/SMA200 and close to highs
    trend = 50.0
    if spx["sma50"]:
        trend += 15 if spx["price"] > spx["sma50"] else -15
    if spx["sma200"]:
        trend += 20 if spx["price"] > spx["sma200"] else -20
    trend += clamp(spx["off_52w_high_pct"] * 1.5, -15, 0)  # closer to high = better
    trend = clamp(trend)

    # Volatility: lower VIX = higher score. ~12 VIX -> ~100, ~32 VIX -> ~0
    vol = clamp(100 - (vix_price - 12) * 5)

    # Breadth: prefer real breadth (% above 200-day avg) across the tracked
    # ticker universe when available, since it's a larger, more accurate
    # sample than the 11-sector proxy.
    if internals and internals.get("pct_above_sma200") is not None:
        breadth_score = internals["pct_above_sma200"]
    elif breadth["sectors_total"]:
        breadth_score = (breadth["sectors_above_200sma"] / breadth["sectors_total"]) * 100
    else:
        breadth_score = 50.0

    # Sentiment: CNN Fear & Greed is already 0-100; default to neutral if unavailable
    sentiment = fng["score"] if fng["score"] is not None else 50.0

    composite = 0.35 * trend + 0.25 * vol + 0.20 * breadth_score + 0.20 * sentiment
    composite = round(clamp(composite))

    if composite >= 60:
        label = "Bullish"
        emoji = "🟢"
    elif composite >= 40:
        label = "Neutral"
        emoji = "🟡"
    else:
        label = "Bearish"
        emoji = "🔴"

    return {
        "score": composite,
        "label": label,
        "emoji": emoji,
        "components": {
            "trend": round(trend),
            "volatility": round(vol),
            "breadth": round(breadth_score),
            "sentiment": round(sentiment),
        },
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_previous() -> dict | None:
    if LATEST_PATH.exists():
        try:
            return json.loads(LATEST_PATH.read_text())
        except Exception:
            return None
    return None


def append_history(row: dict) -> None:
    """Writes one row per calendar date. If today's date already has a row
    (from an earlier run today, now that this runs every 15 minutes rather
    than once daily), that row is replaced in place rather than duplicated —
    keeps the historical chart at one point per day while still reflecting
    the latest intraday value for today.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df_row = pd.DataFrame([row])
    if HISTORY_PATH.exists():
        existing = pd.read_csv(HISTORY_PATH, dtype={"date": str})
        existing = existing[existing["date"] != row["date"]]
        combined = pd.concat([existing, df_row], ignore_index=True)
    else:
        combined = df_row
    combined.to_csv(HISTORY_PATH, mode="w", header=True, index=False)


def build_alerts(prev: dict | None, market: dict, now_iso: str) -> list:
    """Keeps at most 5 alerts. Regime changes are logged immediately; an
    unchanged status is logged at most once per calendar day rather than
    every run, now that this runs every 15 minutes instead of once daily.
    """
    alerts = prev.get("recent_alerts", [])[:4] if prev else []
    prev_label = prev["market"]["label"] if prev else None
    changed = prev_label != market["label"]

    if changed:
        text = f"Market shifted to {market['label']}" if prev_label else f"Market opened as {market['label']}"
        icon = "⚠" if market["label"] != "Bullish" else "✔"
        alerts.insert(0, {"time": now_iso, "text": f"{icon} {text}"})
    else:
        today = now_iso[:10]
        already_logged_today = alerts and str(alerts[0].get("time", ""))[:10] == today
        if not already_logged_today:
            alerts.insert(0, {"time": now_iso, "text": f"✔ Market remains {market['label']}"})

    return alerts[:5]


# ---------------------------------------------------------------------------
# Email (optional)
# ---------------------------------------------------------------------------

def maybe_send_email(prev: dict | None, market: dict, snapshot: dict) -> None:
    email_from = os.environ.get("EMAIL_ADDRESS")
    email_password = os.environ.get("EMAIL_PASSWORD")
    email_to = os.environ.get("EMAIL_TO")
    only_on_change = os.environ.get("EMAIL_ONLY_ON_CHANGE", "true").lower() == "true"

    if not (email_from and email_password and email_to):
        return  # email not configured, skip silently

    prev_label = prev["market"]["label"] if prev else None
    changed = prev_label != market["label"]
    if only_on_change and not changed:
        return

    subject = f"Market Dashboard: {market['emoji']} {market['label']} ({market['score']}/100)"
    lines = [
        f"Overall market: {market['emoji']} {market['label']}  —  Score {market['score']}/100",
        "",
        f"S&P 500:  {snapshot['sp500']['change_pct']:+.2f}%",
        f"Nasdaq:   {snapshot['nasdaq']['change_pct']:+.2f}%",
        f"Dow:      {snapshot['dow']['change_pct']:+.2f}%",
        f"Russell:  {snapshot['russell']['change_pct']:+.2f}%",
        f"VIX:      {snapshot['vix']:.1f}",
        f"Fear & Greed: {snapshot['fear_greed']['score']} ({snapshot['fear_greed']['rating']})",
    ]
    internals = snapshot.get("internals")
    if internals:
        lines += [
            f"Breadth:  {internals['advancers']} advancers / {internals['decliners']} decliners "
            f"({internals['new_highs']} new highs, {internals['new_lows']} new lows)",
            f"% > SMA200: {internals['pct_above_sma200']}%   % > SMA50: {internals['pct_above_sma50']}%   % > SMA20: {internals['pct_above_sma20']}%",
        ]
    lines += [
        "",
        "Full dashboard: (add your GitHub Pages URL here)",
    ]
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(email_from, email_password)
        server.sendmail(email_from, [email_to], msg.as_string())
    print("[info] alert email sent")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    spx = fetch_index_data(INDEX_TICKERS["sp500"])
    nasdaq = fetch_index_data(INDEX_TICKERS["nasdaq"])
    dow = fetch_index_data(INDEX_TICKERS["dow"])
    russell = fetch_index_data(INDEX_TICKERS["russell"])
    vix = fetch_index_data(VIX_TICKER)
    breadth = fetch_sector_breadth()
    fng = fetch_fear_greed()
    ndx100_tickers = fetch_nasdaq100_tickers()
    internals = fetch_market_internals(ndx100_tickers)

    market = compute_market_score(spx, vix["price"], breadth, fng, internals)
    trend_strength = compute_trend_strength(spx)
    market_summary = build_market_summary(spx, market, trend_strength, vix["price"], internals, fng)

    prev = load_previous()

    snapshot = {
        "updated_at": now_iso,
        "market": market,
        "trend_strength": trend_strength,
        "market_summary": market_summary,
        "sp500": spx,
        "nasdaq": nasdaq,
        "dow": dow,
        "russell": russell,
        "vix": vix["price"],
        "breadth": breadth,
        "internals": internals,
        "fear_greed": fng,
    }
    snapshot["recent_alerts"] = build_alerts(prev, market, now_iso)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(snapshot, indent=2))

    append_history({
        "date": now.strftime("%Y-%m-%d"),
        "market_score": market["score"],
        "market_label": market["label"],
        "sp500_price": spx["price"],
        "sp500_change_pct": round(spx["change_pct"], 3),
        "nasdaq_change_pct": round(nasdaq["change_pct"], 3),
        "dow_change_pct": round(dow["change_pct"], 3),
        "russell_change_pct": round(russell["change_pct"], 3),
        "vix": round(vix["price"], 2),
        "fear_greed": fng["score"],
        "sectors_above_200sma": breadth["sectors_above_200sma"],
        "sectors_total": breadth["sectors_total"],
        "advancers": internals["advancers"] if internals else "",
        "decliners": internals["decliners"] if internals else "",
        "new_highs": internals["new_highs"] if internals else "",
        "new_lows": internals["new_lows"] if internals else "",
        "pct_above_sma20": internals["pct_above_sma20"] if internals else "",
        "pct_above_sma50": internals["pct_above_sma50"] if internals else "",
        "pct_above_sma200": internals["pct_above_sma200"] if internals else "",
    })

    maybe_send_email(prev, market, snapshot)

    print(f"[done] {market['emoji']} {market['label']} ({market['score']}/100) written at {now_iso}")


if __name__ == "__main__":
    main()
