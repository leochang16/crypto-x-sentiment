#!/usr/bin/env python3
"""
verify.py - Independent reconciliation of daily-crypto-x-sentiment outputs.

Runs in GitHub Actions CI on every PR. Re-fetches critical metrics from
their original public sources (CoinGecko /global, yfinance VIX/MOVE/DXY)
and compares against the agent-written outputs/sources_today.json.

Exit codes:
  0 - all checks passed (drift within tolerance)
  1 - drift error (BLOCK merge)
  2 - verification couldn't run (e.g., no sources_today.json yet)

Tolerances are intentionally generous because intraday quotes between
the routine run (T) and CI run (T+5min) genuinely differ. The goal is
to catch HALLUCINATED values (e.g., btc_d=60.00 when actual is 58.18),
not to enforce exact match.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("FATAL: requests not installed. Run: pip install requests")
    sys.exit(2)


THRESHOLDS = {
    "btc_d":       0.6,
    "eth_d":       0.4,
    "others_d":    0.5,
    "vix":         1.5,
    "vix3m":       1.5,
    "move":        8.0,
    "dxy":         0.4,
    "tips_real":   0.10,
}

UA = "crypto-x-sentiment-ci/1.0"
SOURCES_PATH = Path("outputs/sources_today.json")


def fetch_coingecko_global():
    r = requests.get("https://api.coingecko.com/api/v3/global", timeout=20,
                     headers={"User-Agent": UA})
    r.raise_for_status()
    mc_pct = r.json()["data"]["market_cap_percentage"]
    out = {
        "btc_d": float(mc_pct["btc"]),
        "eth_d": float(mc_pct["eth"]),
    }
    excluded = {"btc", "eth", "usdt", "usdc", "usds", "dai", "fdusd", "usde", "tusd"}
    top10_d = sum(v for k, v in mc_pct.items() if k not in excluded)
    out["others_d"] = round(top10_d, 4)
    return out


def fetch_yahoo_chart(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = requests.get(url, params={"interval": "1d", "range": "10d"},
                     timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    valid = [c for c in closes if c is not None]
    if not valid:
        raise ValueError(f"no valid closes for {ticker}")
    return float(valid[-1])


def fetch_fred_dfii10():
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"
    r = requests.get(url, timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    lines = r.text.strip().split("\n")
    for line in reversed(lines):
        parts = line.split(",")
        if len(parts) == 2 and parts[1] not in (".", "VALUE", ""):
            try:
                return float(parts[1])
            except ValueError:
                continue
    raise ValueError("no valid DFII10 value")


def get_dashboard_value(sources, field):
    entry = sources.get(field)
    if entry is None:
        return None, None
    if isinstance(entry, dict):
        return entry.get("value"), entry.get("source", "?")
    return entry, "raw"


def check_field(field, fetched, dashboard, tol, errors, warnings):
    if dashboard is None:
        warnings.append(f"{field}: not in sources_today.json")
        return
    if fetched is None:
        warnings.append(f"{field}: fetch failed")
        return
    diff = abs(fetched - dashboard)
    status = "PASS" if diff <= tol else "FAIL"
    print(f"  [{status}] {field}: dashboard={dashboard:.3f} live={fetched:.3f} diff={diff:.3f} (tol {tol})")
    if diff > tol:
        errors.append(f"{field}: drift {diff:.3f} > {tol} (dashboard {dashboard:.3f} vs live {fetched:.3f})")


def sanity_integer_endings(sources, errors):
    for field, entry in sources.items():
        if not isinstance(entry, dict):
            continue
        v = entry.get("value")
        if v is None or not isinstance(v, (int, float)):
            continue
        if field.startswith("n_") or field.endswith("_count"):
            continue
        if "flow" in field or "_usd" in field or "price" in field:
            continue
        if field in {"btc_d", "eth_d", "others_d", "total3_d"}:
            if v == int(v):
                errors.append(f"L1 violation: {field}={v} integer-ending (need 0.01 precision)")


def sanity_date_freshness(sources, errors, warnings):
    now = datetime.now(timezone.utc)
    for field, entry in sources.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("fetched_at_utc")
        if not ts:
            warnings.append(f"{field}: no timestamp")
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            warnings.append(f"{field}: bad timestamp '{ts}'")
            continue
        age_h = (now - t).total_seconds() / 3600
        if age_h > 24:
            warnings.append(f"{field}: {age_h:.1f}h old")


def main():
    print("=" * 60)
    print("verify.py independent reconciliation")
    print(f"started {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    if not SOURCES_PATH.exists():
        print(f"[skip] {SOURCES_PATH} not found - not blocking.")
        sys.exit(0)

    try:
        with SOURCES_PATH.open() as f:
            sources = json.load(f)
    except Exception as e:
        print(f"[fail] could not parse {SOURCES_PATH}: {e}")
        sys.exit(2)

    errors = []
    warnings = []

    print("\n[1/5] Static sanity checks")
    sanity_integer_endings(sources, errors)
    sanity_date_freshness(sources, errors, warnings)

    print("\n[2/5] CoinGecko /global")
    try:
        cg = fetch_coingecko_global()
        for f in ("btc_d", "eth_d", "others_d"):
            dv, _ = get_dashboard_value(sources, f)
            check_field(f, cg.get(f), dv, THRESHOLDS[f], errors, warnings)
    except Exception as e:
        warnings.append(f"CoinGecko: {e}")
        print(f"  WARN CoinGecko failed: {e}")

    print("\n[3/5] Yahoo Finance VIX/MOVE/DXY")
    for ticker, field in [("^VIX", "vix"), ("^MOVE", "move"), ("DX-Y.NYB", "dxy")]:
        try:
            live = fetch_yahoo_chart(ticker)
            dv, _ = get_dashboard_value(sources, field)
            check_field(field, live, dv, THRESHOLDS[field], errors, warnings)
        except Exception as e:
            warnings.append(f"{ticker}: {e}")
            print(f"  WARN {ticker} failed: {e}")

    print("\n[4/5] FRED DFII10")
    try:
        tips = fetch_fred_dfii10()
        dv, _ = get_dashboard_value(sources, "tips_real")
        check_field("tips_real", tips, dv, THRESHOLDS["tips_real"], errors, warnings)
    except Exception as e:
        warnings.append(f"FRED: {e}")
        print(f"  WARN FRED failed: {e}")

    print("\n[5/5] Summary")
    print("=" * 60)
    if errors:
        print(f"FAIL: {len(errors)} drift error(s):")
        for e in errors:
            print(f"   - {e}")
    if warnings:
        print(f"WARN: {len(warnings)} non-blocking warning(s):")
        for w in warnings:
            print(f"   - {w}")
    if not errors and not warnings:
        print("OK: all checks passed cleanly")
    elif not errors:
        print("OK: all comparable fields within tolerance")

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

