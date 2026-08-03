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

CNN_FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-dashboard-bot/1.0)"}


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_index_data(ticker: str) -> dict:
    hist = yf.Ticker(ticker).history(period="400d", interval="1d")
    if hist.empty or len(hist) < 20:
        raise ValueError(f"No data for {ticker}")
    close = hist["Close"]
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    change_pct = (last / prev - 1) * 100
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    high_252 = float(close.rolling(252).max().iloc[-1]) if len(close) >= 20 else last
    off_high = (last / max(high_252, last) - 1) * 100
    return {
        "price": last,
        "change_pct": change_pct,
        "sma50": sma50,
        "sma200": sma200,
        "off_52w_high_pct": off_high,
    }


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


def fetch_fear_greed() -> dict:
    """CNN's Fear & Greed Index via its public (unofficial) data endpoint.
    Falls back gracefully to None if the endpoint changes or is unreachable —
    the dashboard should not break just because this one field is missing.
    """
    try:
        start_date = (datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        resp = requests.get(f"{CNN_FNG_URL}/{start_date}", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        score = float(payload["fear_and_greed"]["score"])
        rating = str(payload["fear_and_greed"]["rating"]).title()
        return {"score": round(score), "rating": rating}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Fear & Greed fetch failed: {exc}")
        return {"score": None, "rating": None}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def compute_market_score(spx: dict, vix_price: float, breadth: dict, fng: dict) -> dict:
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

    # Breadth: % of sectors above their 200-day average
    if breadth["sectors_total"]:
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df_row = pd.DataFrame([row])
    if HISTORY_PATH.exists():
        df_row.to_csv(HISTORY_PATH, mode="a", header=False, index=False)
    else:
        df_row.to_csv(HISTORY_PATH, mode="w", header=True, index=False)


def build_alerts(prev: dict | None, market: dict, now_iso: str) -> list:
    alerts = []
    if prev:
        alerts = prev.get("recent_alerts", [])[:9]  # keep most recent 9, we add 1 more below

    prev_label = prev["market"]["label"] if prev else None
    if prev_label != market["label"]:
        text = f"Market shifted to {market['label']}" if prev_label else f"Market opened as {market['label']}"
        icon = "⚠" if market["label"] != "Bullish" else "✔"
    else:
        text = f"Market remains {market['label']}"
        icon = "✔"

    alerts.insert(0, {"time": now_iso, "text": f"{icon} {text}"})
    return alerts[:10]


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

    market = compute_market_score(spx, vix["price"], breadth, fng)

    prev = load_previous()

    snapshot = {
        "updated_at": now_iso,
        "market": market,
        "sp500": spx,
        "nasdaq": nasdaq,
        "dow": dow,
        "russell": russell,
        "vix": vix["price"],
        "breadth": breadth,
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
    })

    maybe_send_email(prev, market, snapshot)

    print(f"[done] {market['emoji']} {market['label']} ({market['score']}/100) written at {now_iso}")


if __name__ == "__main__":
    main()
