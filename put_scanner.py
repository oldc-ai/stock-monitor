#!/usr/bin/env python3
"""
Put Selling Opportunity Scanner

Ranks a list of tickers by attractiveness for selling cash-secured puts
with 1-2 week expiration (7-14 DTE).

Scoring factors:
  - IV Rank (current IV vs 52-week range) — want elevated IV
  - Annualised premium yield at ~20-30 delta strike
  - Distance of strike from current price (downside buffer %)
  - Trend health (above key MAs, recent momentum)

Ticker source priority:
  1. Inline args:        python put_scanner.py NVDA TSLA
  2. --file/-f flag:     python put_scanner.py -f my_list.txt
  3. config.yaml:        reads `symbols:` from the same config as monitor.py (default)
"""

import argparse
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml
import yfinance as yf
from scipy.stats import norm

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

TARGET_DTE_MIN = 7
TARGET_DTE_MAX = 21
TARGET_DELTA = 0.25


# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------

def bs_put_price(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_put_delta(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return 1.0 if K > S else 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return norm.cdf(d1) - 1


def find_strike_for_delta(S, T, r, sigma, target_delta=0.25, steps=200):
    best_K = S * 0.85
    best_diff = 999
    for pct in np.linspace(0.60, 1.00, steps):
        K = S * pct
        d = -bs_put_delta(S, K, T, r, sigma)
        diff = abs(d - target_delta)
        if diff < best_diff:
            best_diff = diff
            best_K = K
    return round(best_K, 2)


# ---------------------------------------------------------------------------
# Historical volatility & IV rank
# ---------------------------------------------------------------------------

def calc_hv(closes, window=20):
    log_returns = np.log(closes / closes.shift(1)).dropna()
    if len(log_returns) < window:
        return None
    return log_returns.rolling(window).std().iloc[-1] * np.sqrt(252)


def calc_iv_rank(current_iv, iv_series):
    if iv_series is None or len(iv_series) < 10:
        return None, None
    iv_min = iv_series.min()
    iv_max = iv_series.max()
    iv_rank = (current_iv - iv_min) / (iv_max - iv_min) * 100 if iv_max > iv_min else 50
    iv_pct = (iv_series < current_iv).mean() * 100
    return round(iv_rank, 1), round(iv_pct, 1)


# ---------------------------------------------------------------------------
# Trend health score (0-100)
# ---------------------------------------------------------------------------

def trend_score(closes, price=None):
    if len(closes) < 200:
        return 50
    price = price if price else closes.iloc[-1]
    sma20  = closes.rolling(20).mean().iloc[-1]
    sma50  = closes.rolling(50).mean().iloc[-1]
    sma200 = closes.rolling(200).mean().iloc[-1]

    score = 0
    if price > sma20:   score += 25
    if price > sma50:   score += 25
    if price > sma200:  score += 25
    mom = (closes.iloc[-1] / closes.iloc[-21] - 1) if len(closes) >= 22 else 0
    if mom > 0:    score += 15
    if mom > 0.05: score += 10
    return min(score, 100)


# ---------------------------------------------------------------------------
# Main scanner per ticker
# ---------------------------------------------------------------------------

def scan_ticker(ticker: str, risk_free_rate: float = 0.05) -> dict | None:
    try:
        tk = yf.Ticker(ticker)

        hist = tk.history(period="1y", auto_adjust=True)
        if hist.empty or len(hist) < 30:
            return None
        closes = hist["Close"]

        # Real-time (15-min delayed) price via fast_info; fall back to last close
        price_source = "RT"
        try:
            rt_price = tk.fast_info.last_price
            if rt_price and rt_price > 0:
                price = rt_price
            else:
                price = closes.iloc[-1]
                price_source = "close"
        except Exception:
            price = closes.iloc[-1]
            price_source = "close"

        hv20 = calc_hv(closes, 20)
        if hv20 is None:
            return None

        current_iv = hv20

        # Try to get implied vol from nearest expiration in target window
        try:
            exps = tk.options
            if exps:
                now = datetime.now()
                target_exps = []
                for e in exps:
                    exp_dt = datetime.strptime(e, "%Y-%m-%d")
                    dte = (exp_dt - now).days
                    if TARGET_DTE_MIN <= dte <= TARGET_DTE_MAX:
                        target_exps.append((dte, e))
                    elif dte > TARGET_DTE_MAX:
                        break
                if not target_exps:
                    for e in exps:
                        exp_dt = datetime.strptime(e, "%Y-%m-%d")
                        dte = (exp_dt - now).days
                        if dte >= TARGET_DTE_MIN:
                            target_exps.append((dte, e))
                            break
                if target_exps:
                    target_exps.sort()
                    dte_days_iv, exp_str_iv = target_exps[0]
                    T_iv = dte_days_iv / 365
                    chain = tk.option_chain(exp_str_iv)
                    puts = chain.puts
                    if not puts.empty and "impliedVolatility" in puts.columns:
                        target_strike = find_strike_for_delta(price, T_iv, risk_free_rate, hv20, TARGET_DELTA)
                        atm_puts = puts[puts["strike"].between(target_strike * 0.90, target_strike * 1.10)]
                        if not atm_puts.empty:
                            iv_vals = atm_puts["impliedVolatility"].replace(0, np.nan).dropna()
                            if not iv_vals.empty:
                                current_iv = float(iv_vals.mean())
        except Exception:
            pass

        log_ret = np.log(closes / closes.shift(1)).dropna()
        iv_series = log_ret.rolling(20).std().dropna() * np.sqrt(252)
        iv_rank, iv_pct = calc_iv_rank(current_iv, iv_series)

        # Pick best expiration in target window
        dte_days = 10
        exp_str = "n/a"
        try:
            exps = tk.options
            if exps:
                now = datetime.now()
                for e in exps:
                    exp_dt = datetime.strptime(e, "%Y-%m-%d")
                    d = (exp_dt - now).days
                    if d >= TARGET_DTE_MIN:
                        dte_days = d
                        exp_str = e
                        if d <= TARGET_DTE_MAX:
                            break
        except Exception:
            pass

        T = dte_days / 365
        strike = find_strike_for_delta(price, T, risk_free_rate, current_iv, TARGET_DELTA)
        put_premium = bs_put_price(price, strike, T, risk_free_rate, current_iv)
        actual_delta = round(-bs_put_delta(price, strike, T, risk_free_rate, current_iv), 3)

        # Try to fetch actual market bid
        market_bid = None
        try:
            chain = tk.option_chain(exp_str)
            puts = chain.puts
            if not puts.empty:
                row = puts.iloc[(puts["strike"] - strike).abs().argsort()[:1]]
                market_bid = float(row["bid"].values[0])
                if market_bid > 0:
                    put_premium = market_bid
                    strike = float(row["strike"].values[0])
        except Exception:
            pass

        collateral = strike * 100
        raw_yield = put_premium * 100 / collateral * 100
        annualised_yield = raw_yield / dte_days * 365
        otm_pct = (price - strike) / price * 100
        ts = trend_score(closes, price=price)

        iv_rank_score = min(iv_rank or 50, 100)
        yield_score   = min(annualised_yield / 2, 100)
        buffer_score  = max(0, 100 - abs(otm_pct - 10) * 5)
        composite = (
            0.30 * iv_rank_score +
            0.30 * yield_score +
            0.25 * ts +
            0.15 * buffer_score
        )

        return {
            "ticker":          ticker,
            "price":           round(price, 2),
            "expiration":      exp_str,
            "dte":             dte_days,
            "strike":          round(strike, 2),
            "delta":           actual_delta,
            "premium":         round(put_premium, 2),
            "otm_pct":         round(otm_pct, 1),
            "iv_current":      round(current_iv * 100, 1),
            "hv20":            round(hv20 * 100, 1),
            "iv_rank":         iv_rank,
            "iv_pct":          iv_pct,
            "raw_yield_pct":   round(raw_yield, 2),
            "ann_yield_pct":   round(annualised_yield, 1),
            "trend_score":     ts,
            "composite_score": round(composite, 1),
            "prem_source":     "market bid" if market_bid else "BS estimate",
            "price_source":    price_source,
        }

    except Exception as e:
        print(f"  [!] {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_results(results: list[dict]) -> None:
    results = sorted(results, key=lambda x: x["composite_score"], reverse=True)

    header = (
        f"{'#':<3} {'Ticker':<7} {'Price':>7}{'':5} {'Exp':>12} {'DTE':>4} "
        f"{'Strike':>7} {'Delta':>6} {'Prem':>6} {'OTM%':>6} "
        f"{'IV%':>5} {'IVR':>5} {'Ann%':>6} {'Trend':>5} {'Score':>6} {'Note'}"
    )
    sep = "-" * len(header)

    print("\n" + "=" * len(header))
    print("  PUT SELLING OPPORTUNITY SCANNER")
    print("=" * len(header))
    print(header)
    print(sep)

    for i, r in enumerate(results, 1):
        flags = []
        if r["iv_rank"] and r["iv_rank"] >= 50:
            flags.append("HIGH-IVR")
        if r["ann_yield_pct"] >= 50:
            flags.append("FAT-PREM")
        if r["trend_score"] >= 75:
            flags.append("STRONG-TREND")
        if r["otm_pct"] < 3:
            flags.append("CLOSE-TO-MONEY")

        price_tag = f"({r.get('price_source', '?')})"
        print(
            f"{i:<3} {r['ticker']:<7} {r['price']:>7.2f}{price_tag:<5} {r['expiration']:>12} {r['dte']:>4} "
            f"{r['strike']:>7.2f} {r['delta']:>6.2f} {r['premium']:>6.2f} {r['otm_pct']:>5.1f}% "
            f"{r['iv_current']:>4.0f}% {r['iv_rank'] or 0:>4.0f}% {r['ann_yield_pct']:>5.1f}% "
            f"{r['trend_score']:>5} {r['composite_score']:>6.1f}  {', '.join(flags)}"
        )

    print(sep)
    print("\nColumn guide:")
    print("  DTE      = days to expiration")
    print("  Delta    = put delta (magnitude, ~0.25 target)")
    print("  Prem     = option premium per share ($)")
    print("  OTM%     = how far strike is below current price")
    print("  IV%      = implied/realised volatility annualised")
    print("  IVR      = IV Rank (0-100, higher = more elevated)")
    print("  Ann%     = annualised premium yield on collateral")
    print("  Trend    = trend health score (0-100)")
    print("  Score    = composite opportunity score (higher = better)")
    print()


# ---------------------------------------------------------------------------
# Ticker source helpers
# ---------------------------------------------------------------------------

def load_from_config() -> list[str]:
    if not CONFIG_PATH.exists():
        return []
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return [s.upper().strip() for s in data.get("symbols", [])]


def load_from_file(path: str) -> list[str]:
    try:
        with open(path) as f:
            tickers = [
                line.strip().upper()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        if not tickers:
            print(f"No tickers found in {path}")
            sys.exit(1)
        print(f"Loaded {len(tickers)} tickers from {path}")
        return tickers
    except FileNotFoundError:
        print(f"File not found: {path}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rank put-selling opportunities. Defaults to symbols in config.yaml."
    )
    parser.add_argument(
        "tickers", nargs="*",
        help="Ticker symbols (space-separated). Overrides config.yaml."
    )
    parser.add_argument(
        "--file", "-f", type=str,
        help="Path to a .txt file with one ticker per line (# lines ignored). Overrides config.yaml."
    )
    parser.add_argument(
        "--delta", type=float, default=0.25,
        help="Target put delta magnitude (default: 0.25)"
    )
    parser.add_argument(
        "--dte-min", type=int, default=7,
        help="Minimum days to expiration (default: 7)"
    )
    parser.add_argument(
        "--dte-max", type=int, default=21,
        help="Maximum days to expiration (default: 21)"
    )
    parser.add_argument(
        "--rate", type=float, default=0.05,
        help="Risk-free rate (default: 0.05)"
    )
    args = parser.parse_args()

    global TARGET_DTE_MIN, TARGET_DTE_MAX, TARGET_DELTA
    TARGET_DTE_MIN = args.dte_min
    TARGET_DTE_MAX = args.dte_max
    TARGET_DELTA   = args.delta

    if args.file:
        tickers = load_from_file(args.file)
    elif args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        tickers = load_from_config()
        if tickers:
            print(f"Using {len(tickers)} symbols from config.yaml")
        else:
            print("No tickers found in config.yaml and none provided. Pass tickers as args or use --file.")
            sys.exit(1)

    print(f"\nScanning {len(tickers)} tickers for put-selling opportunities...")
    print(f"Target: DTE {TARGET_DTE_MIN}-{TARGET_DTE_MAX}, ~{TARGET_DELTA:.0%} delta\n")

    results = []
    for t in tickers:
        print(f"  Fetching {t}...", end=" ", flush=True)
        r = scan_ticker(t, risk_free_rate=args.rate)
        if r:
            print(f"score={r['composite_score']}")
            results.append(r)
        else:
            print("skipped")

    if not results:
        print("No results. Check your tickers or network connection.")
        sys.exit(1)

    print_results(results)


if __name__ == "__main__":
    main()
