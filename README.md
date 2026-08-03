# Market Dashboard

A self-updating market dashboard: a GitHub Actions job pulls index/VIX/sentiment
data once a day, computes a composite Market Score, commits it to the repo, and
GitHub Pages serves a static dashboard that reads that data. No server, no
paid API keys required for the core system.

```
market-dashboard/
├── .github/workflows/update-data.yml   # daily automation (GitHub Actions)
├── scripts/collect_data.py              # fetch + score + write data
├── data/
│   ├── latest.json                      # current snapshot (dashboard reads this)
│   └── history.csv                      # one row per day (powers the charts)
├── index.html                           # the dashboard page (GitHub Pages)
└── requirements.txt
```

## 1. Create the repo

1. Create a new **public** GitHub repo (Pages requires public on the free tier,
   or Pro/Team for a private repo).
2. Push everything in this folder to it.

## 2. Enable GitHub Pages

Repo → **Settings → Pages** → Source: `Deploy from a branch` → Branch: `main` /
root. Your dashboard will be live at `https://<username>.github.io/<repo>/`
within a minute or two.

## 3. Let Actions write to the repo

Repo → **Settings → Actions → General → Workflow permissions** → select
**"Read and write permissions"**. This lets the scheduled job commit updated
`data/latest.json` and `data/history.csv` back to the repo.

## 4. Run it once manually

Repo → **Actions** tab → **Update Market Data** → **Run workflow**. This
populates real data immediately instead of waiting for tomorrow's schedule,
and is a good way to confirm everything works before you walk away.

After that, it runs automatically on the schedule in
`update-data.yml` (weekdays, shortly after the US close — adjust the cron
expression if you're not on US market hours or want a different time).

## 5. (Optional) Email alerts

The collector will email you only when the overall market label changes
(e.g. Bullish → Neutral) if you set these three repo secrets — **Settings →
Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `EMAIL_ADDRESS` | The Gmail address to send from |
| `EMAIL_PASSWORD` | A [Gmail App Password](https://myaccount.google.com/apppasswords) — not your regular password |
| `EMAIL_TO` | Where alerts should be sent (can be the same address) |

Leave these unset and email is simply skipped — everything else still works.
Want alerts every day rather than only on change? Set `EMAIL_ONLY_ON_CHANGE:
"false"` in the workflow's `env:` block.

Using a provider other than Gmail: swap the `smtplib.SMTP_SSL("smtp.gmail.com", 465)`
line in `scripts/collect_data.py` for your provider's SMTP host/port.

## How the Market Score works

A 0–100 composite, recomputed daily:

- **35% Trend** — S&P 500 vs its 50-day and 200-day moving averages, and
  proximity to its 52-week high.
- **25% Volatility** — VIX level (lower VIX → higher score).
- **20% Breadth** — how many of the 11 S&P sector ETFs are trading above
  their own 200-day average.
- **20% Sentiment** — CNN's Fear & Greed Index (0–100 scale, used as-is).

≥60 → 🟢 Bullish · 40–59 → 🟡 Neutral · <40 → 🔴 Bearish. This is a simple,
transparent heuristic, not a validated trading signal — treat it as a
quick-glance regime read, and tune the weights in
`compute_market_score()` in `scripts/collect_data.py` to match your own
view of what matters.

## Data sources

- **Prices** (S&P 500, Nasdaq, Dow, Russell 2000, VIX, 11 sector ETFs):
  [yfinance](https://github.com/ranaroussi/yfinance) — free, unofficial
  wrapper around Yahoo Finance. No key needed. It can occasionally break if
  Yahoo changes its endpoints; if a run fails, check the Actions log first.
- **Fear & Greed Index**: CNN's own public (but unofficial/undocumented)
  data endpoint, widely used by the open-source community for this exact
  purpose. If CNN changes or removes it, `fetch_fear_greed()` fails
  gracefully and the dashboard just shows sentiment as neutral (50) rather
  than breaking.

## Extending it

- **More tickers**: add entries to `INDEX_TICKERS` or `SECTOR_ETFS` in
  `collect_data.py`.
- **Intraday updates**: add a second `schedule:` cron entry in the workflow
  (e.g. midday) — the collector is idempotent and safe to run more than
  once a day.
- **Multiple watchlists/portfolios**: extend `latest.json`'s schema with a
  `portfolios` section and a corresponding card on the dashboard.
- **Slack/Teams instead of email**: swap `maybe_send_email()` for a webhook
  POST — same "only fire on change" logic applies.
