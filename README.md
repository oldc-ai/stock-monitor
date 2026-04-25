# Stock SMA Monitor

A small Python service that watches a list of stocks against their 10/20/50/200-day simple moving averages and pings a Discord channel the moment the live intraday price crosses **down** through one of those lines — i.e. classic "high-flyer pulled back to the 50-day" buy-the-dip signals.

## How it works

1. Each cycle, the script fetches ~320 days of *closed* daily bars from Alpaca (today's in-progress bar is excluded).
2. SMA-10/20/50/200 are computed from those closed bars — these are your **fixed reference levels for today**, exactly the lines you'd see plotted on TradingView.
3. The script also fetches the latest live trade price from Alpaca's IEX feed.
4. If the live price crossed *down* through any SMA since the last cycle (previous price was above the line, current is at/below), it posts an embed to your Discord webhook.
5. A 24-hour cooldown per (symbol, MA) prevents spam if price oscillates around a level.

You don't wait for today's close — alerts fire intraday the moment the live price touches the line.

## Setup

```bash
cd stock-monitor
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in:
#   ALPACA_API_KEY, ALPACA_API_SECRET, DISCORD_WEBHOOK_URL
```

Edit `config.yaml` to set your watchlist, MAs, check interval, and cooldown.

## Run

**Continuous mode** (recommended for a small VPS, a `tmux` session, or `systemd`):

```bash
python monitor.py
```

It loops forever, sleeping `check_interval_minutes` between cycles, and skips cycles when the US market is closed.

**Single-shot mode** (for cron / Cowork scheduler / GitHub Actions):

```bash
python monitor.py --once
```

Example crontab to run every 5 minutes during market hours (Mon-Fri, 9:30 AM – 4:00 PM ET — adjust for your timezone):

```cron
*/5 9-16 * * 1-5 cd /path/to/stock-monitor && /path/to/.venv/bin/python monitor.py --once >> monitor.log 2>&1
```

## Files

| File | Purpose |
| --- | --- |
| `monitor.py` | Main script (continuous loop or `--once`). |
| `config.yaml` | Watchlist, MAs, interval, cooldown, Discord settings. |
| `.env` | Alpaca + Discord credentials (you create from `.env.example`). |
| `state.json` | Auto-created. Stores last-seen prices and last-alert timestamps. **Don't commit.** |
| `requirements.txt` | Python deps. |

## Cost

Alpaca's free tier (IEX feed) covers everything this script does — historical daily bars plus latest trade quotes — with no per-call cost. Polling 10 symbols every 5 minutes is well within free-tier limits. If you upgrade to the paid SIP feed for tighter quotes, change `feed="iex"` to `feed="sip"` in `monitor.py`.

## Tuning notes

- **Want pre-warning before the touch?** Set `alert_rule: "touch"` and `proximity_percent: 0.5` in `config.yaml`. You'll get an orange "approaching" alert when the price comes within 0.5% above the SMA, plus the standard red "crossed below" alert when it actually breaks through.
- **Too many alerts?** Increase `cooldown_hours` (24 → 72) or remove the shortest MAs (10, 20) which trigger more often.
- **Want to test the Discord wiring?** Run once outside market hours with `market_hours_only: false` and one symbol. The first cycle records prices but won't fire (no `prev_price` yet); the second cycle is when alerts can trigger.

## Holiday accuracy

The `is_market_open()` check uses a hardcoded list of 2026 US holidays. If you'll run this past 2026 or want NYSE half-days handled, swap that block for [`pandas_market_calendars`](https://pypi.org/project/pandas-market-calendars/) — it's a one-line change.
