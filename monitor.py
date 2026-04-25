#!/usr/bin/env python3
"""
Stock SMA Monitor
=================

Polls Alpaca for the current price of a configured list of tickers, compares
them against their 10/20/50/200-day simple moving averages (computed from
daily bars), and fires a Discord alert when a price crosses DOWN through an
SMA — a classic "pullback to support" signal for high-flying names.

Run modes:
    python monitor.py              # loop forever (default)
    python monitor.py --once       # do one check cycle and exit (for cron / scheduler)
    python monitor.py --test-alert # send a test Discord message and exit
    python monitor.py --dry-run    # compute + log alerts but don't send to Discord
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

# ---------------------------------------------------------------------------
# Paths & logging
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"
ENV_PATH = BASE_DIR / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sma-monitor")

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------
@dataclass
class Config:
    symbols: list[str]
    moving_averages: list[int]
    alert_rule: str
    proximity_percent: float
    check_interval_minutes: int
    cooldown_hours: int
    market_hours_only: bool
    discord_username: str
    discord_mention: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        discord = data.get("discord") or {}
        return cls(
            symbols=[s.upper().strip() for s in data["symbols"]],
            moving_averages=sorted(set(int(m) for m in data["moving_averages"])),
            alert_rule=data.get("alert_rule", "cross_below"),
            proximity_percent=float(data.get("proximity_percent", 0.3)),
            check_interval_minutes=int(data.get("check_interval_minutes", 5)),
            cooldown_hours=int(data.get("cooldown_hours", 24)),
            market_hours_only=bool(data.get("market_hours_only", True)),
            discord_username=discord.get("username", "SMA Monitor"),
            discord_mention=discord.get("mention", "") or "",
        )


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json corrupt - starting fresh")
    return {"last_prices": {}, "last_alerts": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------
def build_data_client() -> StockHistoricalDataClient:
    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_API_SECRET"]
    return StockHistoricalDataClient(key, secret)


def build_trading_client() -> TradingClient:
    """Used only for the market-clock endpoint."""
    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_API_SECRET"]
    return TradingClient(key, secret, paper=True)


def fetch_daily_bars(
    client: StockHistoricalDataClient,
    symbols: list[str],
    lookback_days: int = 400,
) -> dict[str, pd.DataFrame]:
    """Return {symbol: DataFrame(daily bars)} for `symbols`.

    400 calendar days ≈ ~275 trading days — enough for a stable 200-SMA.

    IMPORTANT: today's in-progress daily bar is *excluded*. We want SMAs to be
    a fixed reference line for today (built from closed days only) so that the
    live intraday price moves through it, instead of the SMA wiggling along
    with the live price.
    """
    now_et = datetime.now(ET)
    today_et = now_et.date()
    # 'end' = start of today in ET → Alpaca returns bars strictly before today
    end = datetime.combine(today_et, datetime.min.time(), tzinfo=ET)
    start = end - timedelta(days=lookback_days)

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment="split",  # split-adjusted; dividends left alone for clean SMA
        feed="iex",          # free feed; works on paper keys
    )
    resp = client.get_stock_bars(req)
    df = resp.df  # multi-indexed (symbol, timestamp)
    if df.empty:
        return {s: pd.DataFrame() for s in symbols}

    out = {}
    for sym in symbols:
        if sym in df.index.get_level_values(0):
            sym_df = df.xs(sym, level=0).copy()
            # Belt-and-suspenders: drop any bar whose date == today (ET) in
            # case Alpaca still returns it for this feed.
            if not sym_df.empty:
                bar_dates_et = sym_df.index.tz_convert(ET).date
                sym_df = sym_df[bar_dates_et < today_et]
            out[sym] = sym_df
        else:
            out[sym] = pd.DataFrame()
    return out


def fetch_latest_prices(
    client: StockHistoricalDataClient,
    symbols: list[str],
) -> dict[str, float]:
    """Return {symbol: latest trade price}."""
    req = StockLatestTradeRequest(symbol_or_symbols=symbols, feed="iex")
    trades = client.get_stock_latest_trade(req)
    return {sym: float(trade.price) for sym, trade in trades.items()}


def compute_smas(bars: pd.DataFrame, periods: list[int]) -> dict[int, float]:
    """Latest SMA value for each period. Returns {} if not enough data."""
    if bars.empty or "close" not in bars.columns:
        return {}
    closes = bars["close"].dropna()
    result: dict[int, float] = {}
    for p in periods:
        if len(closes) >= p:
            result[p] = float(closes.tail(p).mean())
    return result


def is_market_open(trading: TradingClient) -> bool:
    clock = trading.get_clock()
    return bool(clock.is_open)


def seconds_until_market_open(trading: TradingClient) -> int:
    clock = trading.get_clock()
    if clock.is_open:
        return 0
    next_open = clock.next_open
    if next_open.tzinfo is None:
        next_open = next_open.replace(tzinfo=timezone.utc)
    delta = (next_open - datetime.now(timezone.utc)).total_seconds()
    return max(int(delta), 30)


# ---------------------------------------------------------------------------
# Alert logic
# ---------------------------------------------------------------------------
def detect_alerts(
    symbol: str,
    current_price: float,
    prev_price: float | None,
    smas: dict[int, float],
    config: Config,
    last_alerts: dict[str, str],
    now: datetime,
) -> list[dict]:
    """Return a list of alert dicts for this symbol."""
    alerts: list[dict] = []
    cooldown = timedelta(hours=config.cooldown_hours)

    for period, sma in smas.items():
        key = f"{symbol}:{period}"
        last_alert_iso = last_alerts.get(key)
        if last_alert_iso:
            try:
                last_time = datetime.fromisoformat(last_alert_iso)
                if now - last_time < cooldown:
                    continue
            except ValueError:
                pass

        fired = False
        reason = ""

        # Primary rule: downward cross through the SMA
        if prev_price is not None and prev_price > sma and current_price <= sma:
            fired = True
            reason = "crossed_below"
        # Optional proximity rule
        elif (
            config.alert_rule == "touch"
            and current_price > sma
            and (current_price - sma) / sma * 100 <= config.proximity_percent
        ):
            fired = True
            reason = "near_sma"

        if fired:
            pct_from_sma = (current_price - sma) / sma * 100
            alerts.append(
                {
                    "symbol": symbol,
                    "period": period,
                    "sma": sma,
                    "price": current_price,
                    "prev_price": prev_price,
                    "pct_from_sma": pct_from_sma,
                    "reason": reason,
                    "key": key,
                }
            )
    return alerts


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def send_discord(webhook: str, username: str, mention: str, alerts: list[dict]) -> None:
    """Post one message containing all new alerts."""
    if not alerts:
        return

    lines: list[str] = []
    if mention:
        lines.append(mention)
    lines.append("**SMA pullback signal** — price touched/crossed below moving average")
    lines.append("")

    for a in alerts:
        arrow = "⬇" if a["reason"] == "crossed_below" else "↘"
        lines.append(
            f"{arrow} **{a['symbol']}** @ ${a['price']:.2f} "
            f"— crossed {a['period']}-day SMA (${a['sma']:.2f}) "
            f"[{a['pct_from_sma']:+.2f}%]"
        )

    payload = {
        "username": username,
        "content": "\n".join(lines),
        "allowed_mentions": {"parse": ["users", "roles", "everyone"]},
    }
    r = requests.post(webhook, json=payload, timeout=10)
    if r.status_code >= 300:
        log.error("Discord webhook failed: %s %s", r.status_code, r.text)
    else:
        log.info("Discord alert sent (%d item%s)", len(alerts), "" if len(alerts) == 1 else "s")


def send_test_discord(webhook: str, username: str) -> None:
    payload = {
        "username": username,
        "content": "✅ **SMA Monitor** — test message. Webhook works!",
    }
    r = requests.post(webhook, json=payload, timeout=10)
    r.raise_for_status()
    log.info("Test message sent.")


# ---------------------------------------------------------------------------
# Main check cycle
# ---------------------------------------------------------------------------
def run_cycle(
    config: Config,
    data_client: StockHistoricalDataClient,
    webhook: str,
    state: dict,
    dry_run: bool = False,
) -> None:
    log.info("Running check cycle on %d symbol(s)", len(config.symbols))

    # Daily bars change once per day (after market close). Cheap enough to
    # re-fetch every cycle; simplifies logic.
    bars_by_sym = fetch_daily_bars(data_client, config.symbols)
    prices = fetch_latest_prices(data_client, config.symbols)

    now = datetime.now(timezone.utc)
    new_alerts: list[dict] = []
    last_prices: dict[str, float] = state.setdefault("last_prices", {})
    last_alerts: dict[str, str] = state.setdefault("last_alerts", {})

    for sym in config.symbols:
        price = prices.get(sym)
        if price is None:
            log.warning("%s: no current price returned", sym)
            continue

        smas = compute_smas(bars_by_sym.get(sym, pd.DataFrame()), config.moving_averages)
        if not smas:
            log.warning("%s: insufficient history for SMAs", sym)
            last_prices[sym] = price
            continue

        prev_price = last_prices.get(sym)
        sma_snapshot = " | ".join(f"{p}d=${v:.2f}" for p, v in sorted(smas.items()))
        log.info("%-6s $%.2f  %s", sym, price, sma_snapshot)

        alerts = detect_alerts(
            sym, price, prev_price, smas, config, last_alerts, now
        )
        for a in alerts:
            log.info(
                "  ALERT %s crossed %dd SMA ($%.2f → $%.2f, SMA=$%.2f)",
                sym, a["period"], prev_price or 0.0, price, a["sma"],
            )
            last_alerts[a["key"]] = now.isoformat()
            new_alerts.append(a)

        last_prices[sym] = price

    if new_alerts and not dry_run:
        send_discord(
            webhook,
            config.discord_username,
            config.discord_mention,
            new_alerts,
        )
    elif new_alerts and dry_run:
        log.info("[dry-run] %d alert(s) would have been sent", len(new_alerts))

    save_state(state)


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stock SMA Monitor → Discord")
    p.add_argument("--once", action="store_true", help="Run one check cycle and exit")
    p.add_argument("--test-alert", action="store_true", help="Send a test Discord message and exit")
    p.add_argument("--dry-run", action="store_true", help="Log alerts but don't send to Discord")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    for required in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "DISCORD_WEBHOOK_URL"):
        if not os.environ.get(required):
            log.error("Missing required env var: %s (put it in .env)", required)
            return 2

    config = Config.load(CONFIG_PATH)
    webhook = os.environ["DISCORD_WEBHOOK_URL"]

    if args.test_alert:
        send_test_discord(webhook, config.discord_username)
        return 0

    data_client = build_data_client()
    trading_client = build_trading_client()
    state = load_state()

    if args.once:
        if config.market_hours_only and not is_market_open(trading_client):
            log.info("Market closed — skipping (run with market_hours_only=false to override).")
            return 0
        run_cycle(config, data_client, webhook, state, dry_run=args.dry_run)
        return 0

    # Loop mode
    log.info(
        "Starting loop: %d symbols, MAs=%s, interval=%dmin, rule=%s",
        len(config.symbols),
        config.moving_averages,
        config.check_interval_minutes,
        config.alert_rule,
    )
    while True:
        try:
            if config.market_hours_only and not is_market_open(trading_client):
                wait = seconds_until_market_open(trading_client)
                log.info("Market closed — sleeping %d min until next open", wait // 60)
                time.sleep(min(wait, 15 * 60))  # wake at least every 15 min
                continue

            run_cycle(config, data_client, webhook, state, dry_run=args.dry_run)
        except KeyboardInterrupt:
            log.info("Interrupted — exiting.")
            return 0
        except Exception as e:
            log.exception("Cycle failed: %s", e)

        time.sleep(config.check_interval_minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
