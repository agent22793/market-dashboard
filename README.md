# Market Dashboard

A self-updating market dashboard: a GitHub Actions job pulls index, volatility,
breadth, trend, and sentiment data roughly every 15 minutes during US market
hours, computes a composite Market Score, commits it to the repo, and GitHub
Pages serves a static dashboard that reads that data. No server, no paid API
keys required.

```
market-dashboard/
├── .github/workflows/update-data.yml   # automation schedule (GitHub Actions)
├── scripts/collect_data.py              # fetch + score + write data
├── data/
│   ├── latest.json                      # current snapshot (dashboard reads this)
│   └── history.csv                      # one row per calendar day (powers the charts)
├── assets/chart.umd.js                  # vendored Chart.js (no CDN dependency)
├── index.html                           # the dashboard page (GitHub Pages)
└── requirements.txt
```

---

## Features

### Overall Market score & regime badge
A 0-100 composite score with a Bullish (>=60) / Neutral (40-59) / Bearish
(<40) label, shown with a labeled threshold scale so the score is
self-explanatory at a glance.

### Market Trend
EMA20/50/200 alignment on the S&P 500, classified into Strong Uptrend /
Uptrend / Sideways / Downtrend / Strong Downtrend, with a confidence
percentage that scales with how widely separated the EMAs actually are
(not just whether they're technically ordered correctly).

### Market Summary
Five plain-language bullets recapping Overall status, Trend, Volatility,
Breadth, and Sentiment -- template-generated from the same numbers computed
elsewhere on the dashboard (not an AI-written summary; deterministic, no
extra API call, can't invent a number).

### Major indices
S&P 500, Nasdaq, Dow, Russell 2000 -- current price and daily % change.

### VIX
Current level.

### Fear & Greed Index
Composite score (0-100) with the standard 5-band label (Extreme Fear ->
Extreme Greed).

### Market Breadth
- **Advancers / Decliners** and **New 52-week Highs / Lows** across the
  Nasdaq-100
- **% of Nasdaq-100 stocks above their 20-day / 50-day / 200-day average**,
  each shown in its own color-coded row

### How the Score Works
A methodology panel showing all 4 score components (Trend 35%, Volatility
25%, Breadth 20%, Sentiment 20%) as bars, each labeled with the actual
points it's contributing to the final score (not just its own 0-100
reading) and a plain-language explanation built from that run's live data.

### Charts
- Market Score, last 60 sessions
- Cumulative index performance (S&P 500 / Nasdaq / Dow / Russell), last 60
  sessions

Both read directly from `history.csv`; no backend needed.

### Recent Alerts
Capped at 5 entries. Logs immediately on a regime change (e.g. Bullish ->
Neutral); an unchanged status logs at most once per calendar day rather
than on every refresh, so frequent polling doesn't flood the list.

### Email alerts (optional)
Fires only when the Overall Market label changes, via Gmail SMTP. Fully
optional -- leave the repo secrets unset and it's silently skipped.

### Automated refresh
GitHub Actions runs every 15 minutes during US market hours (weekdays,
13:00-20:45 UTC) via a schedule, plus manual `workflow_dispatch` triggering.
`history.csv` updates *today's* row in place on each run rather than
appending a new row every 15 minutes, so the daily chart stays clean while
the live cards stay current.

### Hosting
Static site on GitHub Pages -- free, no server to maintain.

---

## Data Sources

| Data | Source | Notes |
|---|---|---|
| Index prices (S&P 500, Nasdaq, Dow, Russell 2000), VIX | [Yahoo Finance](https://finance.yahoo.com) via the `yfinance` Python library | Free, no key. Unofficial/undocumented wrapper around Yahoo's endpoints -- can occasionally break if Yahoo changes something. |
| EMA20/50/200, SMA50/200, 52-week high/off-high | Computed locally from the Yahoo Finance price history above | No separate source. |
| Nasdaq-100 constituent list | Nasdaq's own internal JSON API (`api.nasdaq.com/api/quote/list-type/nasdaq100`) | Free, no key, but undocumented and can be slow/unreliable from cloud IPs (retried twice with increasing timeout before giving up). |
| Nasdaq-100 fallback list | A hand-maintained static list baked into `collect_data.py` | Used only if the live API fetch above fails twice. Not actively refreshed -- may drift stale over time; check the Actions log for `[warn] Both Nasdaq-100 fetch attempts failed` to know when it's in use. |
| Market breadth internals (advancers/decliners, new highs/lows, % above SMA20/50/200) | Computed locally: one batched Yahoo Finance download across the ~100 Nasdaq-100 tickers above | No separate source. |
| Sector breadth (11 SPDR sector ETFs) | Yahoo Finance | Used as a secondary/fallback input to the Market Score's breadth component if the Nasdaq-100 internals aren't available that run. |
| Fear & Greed Index (score + rating) | [FearGreedChart.com](https://feargreedchart.com/api-docs) public JSON API | Free, no key, documented, CORS-enabled, 15-minute server-side cache. Replaced an earlier undocumented CNN scrape. |
| Historical daily snapshots | `data/history.csv`, written by this project's own collector | Not fetched from anywhere external -- it's the project's own accumulated data. |

**Nothing here requires a paid subscription or API key.** TradingView was
considered but explicitly ruled out -- their Terms of Service prohibit any
"machine-driven, non-display" use of their data, which is exactly what an
automated collector does, regardless of subscription tier.

---

## Setup

### 1. Create the repo
Public GitHub repo (Pages requires public on the free tier). Push everything
in this folder to it.

### 2. Enable GitHub Pages
Repo -> **Settings -> Pages** -> Source: `Deploy from a branch` -> Branch: `main`
/ root.

### 3. Let Actions write to the repo
Repo -> **Settings -> Actions -> General -> Workflow permissions** -> select
**"Read and write permissions"**.

### 4. Run it once manually
Repo -> **Actions** tab -> **Update Market Data** -> **Run workflow**.

### 5. (Optional) Email alerts
Set three repo secrets -- **Settings -> Secrets and variables -> Actions**:

| Secret | Value |
|---|---|
| `EMAIL_ADDRESS` | The Gmail address to send from |
| `EMAIL_PASSWORD` | A [Gmail App Password](https://myaccount.google.com/apppasswords) -- not your regular password |
| `EMAIL_TO` | Where alerts should be sent |

Leave unset and email is silently skipped.

---

## How the Market Score works

A 0-100 composite, recomputed on every run:

- **35% Trend** -- S&P 500 vs its 50-day and 200-day moving averages, and
  proximity to its 52-week high.
- **25% Volatility** -- VIX level (lower VIX -> higher score).
- **20% Breadth** -- % of Nasdaq-100 stocks above their 200-day average
  (falls back to the 11-sector-ETF version if Nasdaq-100 data is
  unavailable that run).
- **20% Sentiment** -- FearGreedChart.com's Fear & Greed Index, used as-is.

>=60 -> Bullish, 40-59 -> Neutral, <40 -> Bearish. This is a transparent
heuristic, not a validated trading signal -- tune the weights in
`compute_market_score()` if you want to match your own view of what
matters most.

## Known fragility points

Everything free and undocumented carries some risk of silently breaking.
Each of these is wrapped to degrade gracefully rather than take down the
whole run -- but worth knowing where the soft spots are:

- **`yfinance`** -- unofficial Yahoo Finance wrapper. If Yahoo changes their
  API, this breaks and there's no fallback.
- **Nasdaq's list API** -- undocumented, retried twice, falls back to a
  static list that isn't actively maintained.
- **FearGreedChart.com** -- documented but stated as "no uptime guarantee"
  in their own docs; falls back to sentiment being unavailable that run,
  not a hard failure.

Check the Actions log occasionally for `[warn]` lines -- they're the
earliest signal something upstream has changed.
