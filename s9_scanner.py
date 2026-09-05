from __future__ import annotations

"""
S9 Scanner — Pivot Confluence Blast (Multi-Exchange)
=====================================================
Scans:
- BTC/ETH 4-far-OTM options (Delta Exchange) — CE + PE
- Nifty 100 stocks — 2-far-OTM options (Upstox) — monthly expiry
- NIFTY / BANKNIFTY / SENSEX options (Upstox) — weekly expiry
- MCX commodity options (Upstox) — monthly expiry
- After 4 PM IST: crypto expiry shifts to next day

Telegram: S9-only alerts with stars + ladder + pivots.
No stock scanning — options only.
"""

import os
import time
import json
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Optional, Union
from threading import Thread
import pytz
import http.server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s - %(message)s")
logger = logging.getLogger("S9")

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

BB_PERIOD = 20
BB_STD = 2
FLAT_CANDLES = 4
FLAT_THRESHOLD = 0.002
VOL_MULT = 1.5
VOL_PERIOD = 20
MIN_STARS = 2
MAX_STARS = 6
BLACKLIST_AT_BASE = 5
SCAN_INTERVAL = 300

SEGMENT_OPEN = {"NSE": (9, 15), "MCX": (9, 0), "CRYPTO": (0, 0)}
SEGMENT_CLOSE = {"NSE": (15, 30), "MCX": (23, 30), "CRYPTO": (23, 59)}

# Crypto 24/7 me daily TF nahi chahiye (option candles 1-2 din ki hoti)
DAILY_TF_FOR = {"NSE": True, "MCX": True, "CRYPTO": False}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
UPSTOX_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

# ═══════════════════════════════════════════════════════════════════
# UNIVERSE
# ═══════════════════════════════════════════════════════════════════

NIFTY100: list[tuple[str, int]] = [
    ("RELIANCE", 10), ("TCS", 20), ("HDFCBANK", 5), ("ICICIBANK", 10),
    ("INFY", 10), ("HINDUNILVR", 20), ("ITC", 5), ("SBIN", 5),
    ("BHARTIARTL", 10), ("KOTAKBANK", 10), ("LT", 20), ("AXISBANK", 5),
    ("TITAN", 50), ("SUNPHARMA", 20), ("NESTLEIND", 50), ("BAJFINANCE", 10),
    ("POWERGRID", 5), ("NTPC", 5), ("TATAMOTORS", 5), ("M&M", 20),
    ("HCLTECH", 10), ("MARUTI", 100), ("TATASTEEL", 2), ("ADANIENT", 20),
    ("COALINDIA", 5), ("ONGC", 5), ("JSWSTEEL", 5), ("HINDALCO", 5),
    ("BAJAJ-AUTO", 50), ("TATACONSUM", 5), ("EICHERMOT", 50), ("BPCL", 2),
    ("CIPLA", 10), ("DRREDDY", 50), ("TECHM", 20), ("GRASIM", 20),
    ("ULTRACEMCO", 50), ("WIPRO", 5), ("INDUSINDBANK", 10), ("ADANIPORTS", 10),
    ("HDFCLIFE", 10), ("SBILIFE", 10), ("BAJAJFINSV", 20), ("ICICIGI", 20),
    ("APOLLOHOSP", 50), ("DIVISLAB", 100), ("BEL", 5), ("HAL", 50),
    ("ZOMATO", 2), ("TRENT", 50), ("ASIANPAINT", 20), ("SHRIRAMFIN", 20),
    ("HEROMOTOCO", 20), ("VEDL", 2), ("GAIL", 2), ("PIDILITIND", 50),
    ("MOTHERSON", 2), ("SIEMENS", 50), ("LTIM", 50), ("MEDANTA", 50),
    ("JSWENERGY", 10), ("TORNTPHARM", 100), ("LICI", 10), ("TVSMOTOR", 20),
    ("PNB", 2), ("BRITANNIA", 50), ("ASHOKLEY", 2), ("ICICIPRULI", 10),
    ("JIOFIN", 2), ("IDFCFIRSTB", 2), ("CANBK", 2), ("UNIONBANK", 2),
    ("BANKBARODA", 2), ("IOC", 2), ("SAIL", 2), ("NMDC", 2),
    ("PFC", 2), ("RECLTD", 2), ("POLYCAB", 20), ("INDUSTOWER", 5),
    ("GODREJCP", 10), ("MCDOWELL-N", 10), ("DMART", 50), ("TATAELXSI", 50),
    ("COLPAL", 50), ("PERSISTENT", 50), ("NUVAMA", 50), ("INFRA", 5),
    ("BALKRISIND", 20), ("BHARATFORG", 20), ("BHEL", 2), ("CROMPTON", 10),
    ("DABUR", 10), ("ESCORTS", 50), ("EXIDEIND", 2), ("FINCABLES", 10),
    ("FLUOROCHEM", 50), ("GSPL", 10), ("HAPPSTMND", 10), ("HONAUT", 50),
]

INDEX_ROOTS = [("NIFTY", 50), ("BANKNIFTY", 100), ("SENSEX", 100)]
MCX_ROOTS = [("GOLD", 100), ("GOLDM", 100), ("SILVERM", 100), ("CRUDEOIL", 50), ("NATGAS", 1)]


def _build_universe() -> list[dict]:
    u = []
    # BTC/ETH — 4 far OTM options via Delta (resolved dynamically)
    u += [{"sym": "BTC_OTM_CE", "kind": "option", "seg": "CRYPTO", "src": "delta",
           "underlying": "BTCUSD", "opt": "CE", "otm": 4, "gap": 500},
          {"sym": "BTC_OTM_PE", "kind": "option", "seg": "CRYPTO", "src": "delta",
           "underlying": "BTCUSD", "opt": "PE", "otm": 4, "gap": 500},
          {"sym": "ETH_OTM_CE", "kind": "option", "seg": "CRYPTO", "src": "delta",
           "underlying": "ETHUSD", "opt": "CE", "otm": 4, "gap": 50},
          {"sym": "ETH_OTM_PE", "kind": "option", "seg": "CRYPTO", "src": "delta",
           "underlying": "ETHUSD", "opt": "PE", "otm": 4, "gap": 50}]
    # Nifty 100 — 2 far OTM options each CE/PE (monthly expiry)
    for name, gap in NIFTY100:
        u.append({"sym": f"{name}_CE", "kind": "option", "seg": "NSE", "src": "upstox",
                   "fo_root": name, "opt": "CE", "otm": 2, "gap": gap, "exp_style": "monthly"})
        u.append({"sym": f"{name}_PE", "kind": "option", "seg": "NSE", "src": "upstox",
                   "fo_root": name, "opt": "PE", "otm": 2, "gap": gap, "exp_style": "monthly"})
    # Index options (weekly expiry)
    for root, gap in INDEX_ROOTS:
        u.append({"sym": f"{root}CE", "kind": "option", "seg": "NSE", "src": "upstox",
                   "fo_root": root, "opt": "CE", "gap": gap, "exp_style": "weekly"})
        u.append({"sym": f"{root}PE", "kind": "option", "seg": "NSE", "src": "upstox",
                   "fo_root": root, "opt": "PE", "gap": gap, "exp_style": "weekly"})
    # MCX commodity options (monthly expiry)
    for root, gap in MCX_ROOTS:
        u.append({"sym": f"{root}_CE", "kind": "option", "seg": "MCX", "src": "upstox",
                   "fo_root": root, "opt": "CE", "gap": gap, "exp_style": "monthly"})
        u.append({"sym": f"{root}_PE", "kind": "option", "seg": "MCX", "src": "upstox",
                   "fo_root": root, "opt": "PE", "gap": gap, "exp_style": "monthly"})
    return u


UNIVERSE = _build_universe()

# ═══════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════

_scanner_paused = False
_states: dict[str, dict] = {}
_day: str = ""
_crypto_expiry_date: str = ""   # current crypto expiry string YYYY-MM-DD
_nse_expiry_date: str = ""      # current NSE weekly/monthly expiry
_mcx_expiry_date: str = ""      # current MCX monthly expiry

# TF cache: (sym, tf) -> (timestamp, DataFrame)
_tf_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_1d_cache: dict[str, tuple[str, pd.DataFrame]] = {}   # sym -> (date_str, df)
_weekly_pp_cache: dict[str, tuple[str, Optional[float]]] = {}

UPSTOX_MASTER: list[dict] = []
_upstox_master_ts: float = 0
UPSTOX_MASTER_TTL = 6 * 3600

# ═══════════════════════════════════════════════════════════════════
# DELTA EXCHANGE (BTC/ETH)
# ═══════════════════════════════════════════════════════════════════

_DELTA_API = "https://api.india.delta.exchange/v2"
_DELTA_CDN = "https://cdn.india.deltaex.org/v2"


def _delta_get(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=15, headers={"Accept": "application/json"})
        return r.json() if r.ok else None
    except Exception as e:
        logger.error(f"[DELTA] GET {url}: {e}")
        return None


def _delta_candles(symbol: str, tf: str, lookback: int = 120) -> Optional[pd.DataFrame]:
    res_map = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "1d": "1d"}
    secs = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
    end = int(time.time())
    start = end - (secs.get(tf, 300) * lookback)
    for base in [_DELTA_API, _DELTA_CDN]:
        data = _delta_get(f"{base}/history/candles?symbol={symbol}&resolution={res_map.get(tf, '5m')}&start={start}&end={end}")
        if not data:
            continue
        candles = data.get("result", [])
        if not candles or len(candles) < BB_PERIOD:
            continue
        df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(IST)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.sort_values("timestamp").reset_index(drop=True)
    return None


def _delta_options(symbol: str, ltp: float, expiry: str) -> list[dict]:
    """Get 4 OTM CE + PE for crypto."""
    try:
        data = _delta_get(f"{_DELTA_API}/products?contract_types=call_options,put_options&states=live")
        if not data:
            return []
        products = data.get("result", [])
        underlying = "BTC" if "BTC" in symbol else "ETH"
        gap = 500 if underlying == "BTC" else 50
        opts = [p for p in products if p.get("underlying_asset", {}).get("symbol") == underlying]
        if not opts:
            return []
        exp_opts = [p for p in opts if (p.get("settlement_time", "")[:10] == expiry)]
        if not exp_opts:
            return []
        atm = int(round(ltp / gap) * gap)
        result = []
        for i in range(1, 5):
            ce = next((p for p in exp_opts if p.get("contract_type") == "call_options"
                       and int(float(p.get("strike_price", 0))) == atm + gap * i), None)
            pe = next((p for p in exp_opts if p.get("contract_type") == "put_options"
                       and int(float(p.get("strike_price", 0))) == atm - gap * i), None)
            if ce:
                result.append({"symbol": ce["symbol"], "strike": ce.get("strike_price"), "type": "CE", "otm": i})
            if pe:
                result.append({"symbol": pe["symbol"], "strike": pe.get("strike_price"), "type": "PE", "otm": i})
        return result
    except Exception as e:
        logger.error(f"[DELTA_OPT] {symbol}: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# DELTA OPTION RESOLVER (BTC/ETH)
# ═══════════════════════════════════════════════════════════════════

def _get_crypto_ltp(symbol: str) -> Optional[float]:
    """Get last traded price for a crypto underlying from Delta."""
    data = _delta_get(f"{_DELTA_API}/tickers?symbol={symbol}")
    if not data:
        return None
    tickers = data.get("result", [])
    hit = next((t for t in tickers if t.get("symbol") == symbol), None)
    if hit:
        return float(hit.get("last_price") or hit.get("mark_price") or 0)
    return None


_delta_option_cache: dict[str, str] = {}  # sym_def key -> resolved option symbol


def _resolve_delta_option(sym_def: dict) -> Optional[str]:
    """Resolve BTC_OTM_CE/PE or ETH_OTM_CE/PE to actual Delta option symbol."""
    global _delta_option_cache

    underlying = sym_def.get("underlying", "BTCUSD")
    opt_type = sym_def.get("opt", "CE")
    otm = sym_def.get("otm", 4)
    gap = sym_def.get("gap", 500)

    cache_key = f"{underlying}|{opt_type}|{otm}"
    if cache_key in _delta_option_cache:
        return _delta_option_cache[cache_key]

    ltp = _get_crypto_ltp(underlying)
    if not ltp:
        return None

    today = datetime.now(IST).date()
    expiry = _maybe_roll_crypto_expiry()
    options = _delta_options(underlying, ltp, expiry)
    if not options:
        return None

    target = next((o for o in options if o["type"] == opt_type and o["otm"] == otm), None)
    if target:
        _delta_option_cache[cache_key] = target["symbol"]
        return target["symbol"]

    return None


# ═══════════════════════════════════════════════════════════════════
# UPSTOX API (NSE / MCX)
# ═══════════════════════════════════════════════════════════════════

_UPSTOX_HDRS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# Master instrument cache
_upstox_master_cache: list[dict] = []
_upstox_master_ts: float = 0
_UPSTOX_MASTER_TTL = 6 * 3600


def _upstox_get(url: str) -> Optional[dict]:
    hdrs = {**_UPSTOX_HDRS}
    if UPSTOX_TOKEN:
        hdrs["Authorization"] = f"Bearer {UPSTOX_TOKEN}"
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        return r.json() if r.ok else None
    except Exception as e:
        logger.error(f"[UPSTOX] GET {url}: {e}")
        return None


def _upstox_fetch_candles(instrument_key: str, tf: str, lookback: int = 120) -> Optional[pd.DataFrame]:
    """Fetch 5m candles from Upstox historical API."""
    tf_map = {"5m": "minutes/5", "15m": "minutes/15", "30m": "minutes/30", "1h": "hours/1", "1d": "days/1"}
    interval = tf_map.get(tf, "minutes/5")
    now = datetime.now(IST)
    to_date = now.strftime("%Y-%m-%d")
    from_date = (now - timedelta(minutes=5 * lookback if tf == "5m" else
                                  15 * lookback if tf == "15m" else
                                  30 * lookback if tf == "30m" else
                                  60 * lookback if tf == "1h" else
                                  86400 * lookback)).strftime("%Y-%m-%d")
    url = (f"https://api.upstox.com/v3/historical-candle/"
           f"{instrument_key}/{interval}/{to_date}/{from_date}")
    data = _upstox_get(url)
    if not data:
        return None
    candles = data.get("data", {}).get("candles", [])
    if not candles or len(candles) < BB_PERIOD:
        return None
    # candles: [timestamp, open, high, low, close, volume, oi]
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    return df.sort_values("timestamp").reset_index(drop=True)


def _load_upstox_master() -> list[dict]:
    """Load Upstox instrument master (cached)."""
    global _upstox_master_cache, _upstox_master_ts
    now = time.time()
    if _upstox_master_cache and (now - _upstox_master_ts) < _UPSTOX_MASTER_TTL:
        return _upstox_master_cache
    instruments = []
    for url in [
        "https://assets.upstox.com/market-quote/instruments/exchange_NSE.csv",
        "https://assets.upstox.com/market-quote/instruments/exchange_NSE_FO.csv",
        "https://assets.upstox.com/market-quote/instruments/exchange_MCX.csv",
        "https://assets.upstox.com/market-quote/instruments/exchange_MCX_FO.csv",
    ]:
        try:
            r = requests.get(url, timeout=30, headers=_UPSTOX_HDRS)
            if not r.ok:
                continue
            text = r.text
            lines = text.splitlines()
            if not lines:
                continue
            header = [h.strip().strip('"') for h in lines[0].split(",")]
            idx = {h: i for i, h in enumerate(header)}
            for line in lines[1:]:
                parts = line.split(",")
                if len(parts) < len(header):
                    continue
                def _col(name):
                    pos = idx.get(name, -1)
                    return parts[pos].strip().strip('"') if pos >= 0 else ""
                instruments.append({
                    "key": _col("instrument_key"),
                    "symbol": _col("trading_symbol"),
                    "exchange": _col("exchange"),
                    "instrument_type": _col("instrument_type"),
                    "option_type": _col("option_type"),
                    "strike": float(_col("strike_price") or 0),
                    "expiry": _col("expiry"),
                    "lot_size": int(_col("lot_size") or 0),
                    "tick_size": float(_col("tick_size") or 0),
                    "prev_close": float(_col("last_price") or 0),
                })
        except Exception as e:
            logger.warning(f"[UPSTOX] Master load failed for {url}: {e}")
    _upstox_master_cache = instruments
    _upstox_master_ts = now
    logger.info(f"[UPSTOX] Master loaded: {len(instruments)} instruments")
    return instruments


def _resolve_upstox_key(sym_def: dict) -> Optional[str]:
    """Find the instrument key for a given symbol definition."""
    master = _load_upstox_master()
    if not master:
        return None

    # Direct key mapping
    if sym_def.get("upstox_key"):
        key = sym_def["upstox_key"]
        if any(m["key"] == key for m in master):
            return key

    kind = sym_def.get("kind")
    seg = sym_def.get("seg")
    fo_root = sym_def.get("fo_root")
    opt_type = sym_def.get("opt")
    gap = sym_def.get("gap", 50)
    otm = sym_def.get("otm", 2)

    if kind == "stock":
        pattern = f"NSE_EQ|{sym_def['sym']}"
        hit = next((m for m in master if m["key"] == pattern), None)
        return hit["key"] if hit else None

    if kind == "option" and fo_root:
        today = datetime.now(IST).date()
        # Get current expiry
        exp_date = _current_expiry(seg, sym_def.get("exp_style", "monthly"))
        exp_str = exp_date.strftime("%Y-%m-%d") if exp_date else ""

        # Find matching options
        candidates = [
            m for m in master
            if m.get("exchange") == ("MCX_FO" if seg == "MCX" else "NSE_FO")
            and m.get("instrument_type") in ("OPTIDX", "OPTSTK", "OPTFUT")
            and m.get("option_type") == opt_type
            and m.get("expiry", "") == exp_str
        ]

        if not candidates:
            return None

        # Get spot price
        spot_key = f"NSE_INDEX|{fo_root}" if seg == "NSE" else f"MCX_FO|{fo_root}"
        spot_row = next((m for m in master if m["key"] == spot_key), None)
        if not spot_row:
            # Try MCX futures
            spot_key = f"MCX_FO|{fo_root}FUT"
            spot_row = next((m for m in master if m["key"] == spot_key), None)

        if not spot_row:
            return None

        spot = spot_row.get("prev_close", 0)
        if spot <= 0:
            return None

        atm = int(round(spot / gap) * gap)
        target = atm + (otm * gap) if opt_type == "CE" else atm - (otm * gap)

        # Find closest OTM
        hit = min(candidates, key=lambda m: abs(m.get("strike", 0) - target))
        return hit["key"] if hit else None

    return None


def _upstox_candles(sym_def: dict, tf: str = "5m", lookback: int = 120) -> Optional[pd.DataFrame]:
    """Fetch candles for any Upstox instrument."""
    key = _resolve_upstox_key(sym_def)
    if not key:
        return None
    return _upstox_fetch_candles(key, tf, lookback)


def _upstox_prev_day_vol(sym_def: dict) -> Optional[float]:
    """Get previous day volume for entry gate."""
    key = _resolve_upstox_key(sym_def)
    if not key:
        return None
    df = _upstox_fetch_candles(key, "1d", 5)
    if df is None or len(df) < 2:
        return None
    return float(df.iloc[-2]["volume"])


# ═══════════════════════════════════════════════════════════════════
# EXPIRY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

def _next_weekly_expiry(base: datetime, day_of_week: int = 3) -> datetime.date:
    """Next Thursday (or specified weekday) expiry."""
    days_ahead = day_of_week - base.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (base + timedelta(days=days_ahead)).date()


def _next_delta_expiry() -> str:
    """Get next Delta crypto option expiry (daily)."""
    today = datetime.now(UTC).date()
    for i in range(7):
        d = today + timedelta(days=i)
        if d.weekday() < 5:  # Mon-Fri
            return d.strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def _maybe_roll_crypto_expiry() -> str:
    """Roll to next expiry if current is expiring today."""
    now_utc = datetime.now(UTC)
    now_ist = datetime.now(IST)
    # After 4 PM IST = after 10:30 AM UTC, roll to next expiry
    if now_ist.hour >= 16:
        return _next_delta_expiry()
    # Check if today's expiry has already happened (use UTC time)
    today_exp = now_utc.date().strftime("%Y-%m-%d")
    data = _delta_get(f"{_DELTA_API}/products?contract_types=call_options,put_options&states=live&underlying_asset_symbols=BTC,ETH")
    if data:
        products = data.get("result", [])
        today_expiries = [p for p in products if p.get("settlement_time", "")[:10] == today_exp]
        if today_expiries:
            # Check if first expiry time has passed
            first = min(today_expiries, key=lambda p: p.get("settlement_time", ""))
            settl = first.get("settlement_time", "")
            if settl:
                settl_dt = datetime.fromisoformat(settl.replace("Z", "+00:00"))
                if now_utc >= settl_dt:
                    return _next_delta_expiry()
    return today_exp


def _next_monthly_expiry(base: datetime, roll_days_before: int = 5) -> datetime.date:
    """Last Thursday of the month, or earlier if within roll_days_before."""
    # Find last Thursday of current month
    year, month = base.year, base.month
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).date()
    # Last Thursday
    days_since_thu = (last_day.weekday() - 3) % 7
    last_thu = last_day - timedelta(days=days_since_thu)

    today = base.date()
    days_to_expiry = (last_thu - today).days

    # If within roll_days_before or already passed, go to next month
    if days_to_expiry <= roll_days_before:
        next_month_date = next_month.date()
        next_last_day = (datetime(next_month.year, next_month.month + 1 if next_month.month < 12 else 1, 1) - timedelta(days=1)).date()
        days_since_thu = (next_last_day.weekday() - 3) % 7
        return next_last_day - timedelta(days=days_since_thu)

    return last_thu


def _current_expiry(seg: str, style: str = "monthly") -> Optional[datetime.date]:
    """Get the current active expiry date for a segment."""
    now = datetime.now(IST)
    if style == "weekly":
        return _next_weekly_expiry(now)
    return _next_monthly_expiry(now)




def _maybe_roll_nse_expiry() -> str:
    """Roll NSE expiry after monthly/weekly expiry day."""
    global _nse_expiry_date
    now = datetime.now(IST)
    today = now.date().isoformat()

    if not _nse_expiry_date:
        _nse_expiry_date = _next_monthly_expiry(now).isoformat()
        return _nse_expiry_date

    # If today is expiry day, roll after market close
    if today == _nse_expiry_date and now.hour >= 16:
        _nse_expiry_date = _next_monthly_expiry(now + timedelta(days=1)).isoformat()
        logger.info(f"[EXPIRY] NSE rolled to {_nse_expiry_date}")

    return _nse_expiry_date


def _maybe_roll_mcx_expiry() -> str:
    """Roll MCX expiry after monthly expiry day."""
    global _mcx_expiry_date
    now = datetime.now(IST)
    today = now.date().isoformat()

    if not _mcx_expiry_date:
        _mcx_expiry_date = _next_monthly_expiry(now).isoformat()
        return _mcx_expiry_date

    if today == _mcx_expiry_date and now.hour >= 23:
        _mcx_expiry_date = _next_monthly_expiry(now + timedelta(days=1)).isoformat()
        logger.info(f"[EXPIRY] MCX rolled to {_mcx_expiry_date}")

    return _mcx_expiry_date


# ═══════════════════════════════════════════════════════════════════
# INDICATORS (shared by all segments)
# ═══════════════════════════════════════════════════════════════════

def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """BB(20,2) + EMA5 + VWAP + RSI(9) + HM."""
    df = df.copy()
    df["sma20"] = df["close"].rolling(BB_PERIOD).mean()
    df["std20"] = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = df["sma20"] + BB_STD * df["std20"]
    df["bb_lower"] = df["sma20"] - BB_STD * df["std20"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["sma20"]
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()

    # VWAP
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = df["tp"] * df["volume"]
    df["cum_tp_vol"] = df["tp_vol"].cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"] = df["cum_tp_vol"] / df["cum_vol"]

    # RSI(9)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=8, adjust=False).mean()
    avg_loss = loss.ewm(com=8, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    df["rsi9"] = 100 - (100 / (1 + rs))

    # HM: EMA3(RSI) vs WMA21(RSI)
    df["hm_blue"] = df["rsi9"].ewm(span=3, adjust=False).mean()
    w = list(range(1, 22))
    df["hm_red"] = df["rsi9"].rolling(21).apply(
        lambda x: sum(ww * vv for ww, vv in zip(w, x)) / sum(w), raw=True
    )
    return df


def _hm_signal(df: pd.DataFrame) -> dict:
    if "hm_blue" not in df.columns:
        return {"signal": "NEUTRAL", "confirmed": False, "rsi9": 0, "hm_blue": 0, "hm_red": 0}
    d = df.dropna(subset=["rsi9", "hm_blue", "hm_red"])
    if len(d) < 3:
        return {"signal": "NEUTRAL", "confirmed": False, "rsi9": 0, "hm_blue": 0, "hm_red": 0}
    last = d.iloc[-1]
    prev = d.iloc[-2]
    rsi9 = round(float(last["rsi9"]), 2)
    blue = round(float(last["hm_blue"]), 2)
    red = round(float(last["hm_red"]), 2)
    bull_cross = float(prev["hm_blue"]) <= float(prev["hm_red"]) and blue > red
    bear_cross = float(prev["hm_blue"]) >= float(prev["hm_red"]) and blue < red

    if bull_cross and rsi9 > 50:
        return {"signal": "BULLISH", "confirmed": rsi9 > 50 and blue > 50, "rsi9": rsi9, "hm_blue": blue, "hm_red": red}
    if bear_cross and rsi9 < 50:
        return {"signal": "BEARISH", "confirmed": rsi9 < 50 and red > 50, "rsi9": rsi9, "hm_blue": blue, "hm_red": red}
    return {"signal": "BULLISH" if blue > red else "BEARISH", "confirmed": False, "rsi9": rsi9, "hm_blue": blue, "hm_red": red}


def _pct_range(s: pd.Series) -> float:
    m = s.mean()
    return 0.0 if not m else float((s.max() - s.min()) / m)


def _prev_day_hlcv(df: pd.DataFrame) -> Optional[tuple]:
    try:
        d = df.copy()
        d["_d"] = pd.to_datetime(d["timestamp"]).dt.date
        today = d["_d"].iloc[-1]
        prev = d[d["_d"] < today]
        if len(prev) == 0:
            return None
        last_date = prev["_d"].max()
        day = prev[prev["_d"] == last_date]
        return (float(day["high"].max()), float(day["low"].min()),
                float(day.iloc[-1]["close"]), float(day["volume"].sum()))
    except Exception:
        return None


def _calc_pivots(df: pd.DataFrame) -> dict:
    try:
        d = df.copy()
        d["_date"] = pd.to_datetime(d["timestamp"]).dt.date
        today = d["_date"].iloc[-1]
        prev = d[d["_date"] < today]
        if len(prev) == 0:
            return {}
        last_date = prev["_date"].max()
        day = prev[prev["_date"] == last_date]
        H = float(day["high"].max())
        L = float(day["low"].min())
        C = float(day.iloc[-1]["close"])
        pp = (H + L + C) / 3
        rng = H - L
        return {"PP": pp, "R1": 2*pp - L, "R2": pp + rng, "R3": pp + 2*rng,
                "S1": 2*pp - H, "S2": pp - rng}
    except Exception:
        return {}


def _calc_weekly_pp(df_1d: Optional[pd.DataFrame]) -> Optional[float]:
    if df_1d is None or len(df_1d) < 7:
        return None
    try:
        ts = pd.to_datetime(df_1d["timestamp"])
        iso = ts.dt.isocalendar()
        df_1d = df_1d.copy()
        df_1d["_y"], df_1d["_w"] = iso.year.values, iso.week.values
        cy, cw = df_1d["_y"].iloc[-1], df_1d["_w"].iloc[-1]
        prev = df_1d[(df_1d["_y"] < cy) | ((df_1d["_y"] == cy) & (df_1d["_w"] < cw))]
        if len(prev) == 0:
            return None
        ly, lw = prev["_y"].iloc[-1], prev["_w"].iloc[-1]
        wk = prev[(prev["_y"] == ly) & (prev["_w"] == lw)]
        return (float(wk["high"].max()) + float(wk["low"].min()) + float(wk.iloc[-1]["close"])) / 3
    except Exception:
        return None


def _compute_stars(close: float, pivots: dict, weekly_pp: Optional[float]) -> int:
    stars = 0
    for level, pts in [(pivots.get("PP"), 2), (pivots.get("R1"), 3),
                       (pivots.get("R2"), 4), (pivots.get("R3"), 5)]:
        if level is not None and close > level:
            stars = pts
        else:
            break
    if weekly_pp is not None and close > weekly_pp:
        stars = min(stars + 1, MAX_STARS)
    return stars


# ═══════════════════════════════════════════════════════════════════
# CANDLE FETCH (unified interface)
# ═══════════════════════════════════════════════════════════════════

_CANDLE_TTL = {"5m": 0, "15m": 300, "30m": 600, "1h": 900, "1d": 86400}


def fetch_candles(sym_def: dict, tf: str, lookback: int = 120) -> Optional[pd.DataFrame]:
    """Unified candle fetch — Delta for crypto, Upstox for NSE/MCX."""
    src = sym_def.get("src", "delta")

    if src == "delta" and sym_def.get("kind") == "option":
        symbol = sym_def.get("underlying", sym_def["sym"])
        resolved = sym_def.get("_resolved_symbol")
        if not resolved:
            resolved = _resolve_delta_option(sym_def)
            sym_def["_resolved_symbol"] = resolved
        if resolved:
            symbol = resolved
        df = _delta_candles(symbol, tf, lookback)
        # Resolve the option once and cache the actual symbol
        if df is None and not sym_def.get("_resolved_symbol"):
            actual = _resolve_delta_option(sym_def)
            if actual:
                sym_def["_resolved_symbol"] = actual
                df = _delta_candles(actual, tf, lookback)
        return df

    if src == "delta":
        return _delta_candles(sym_def["sym"], tf, lookback)

    # Upstox with cache
    key = (sym_def["sym"], tf)
    ttl = _CANDLE_TTL.get(tf, 300)
    if tf != "1d":
        hit = _tf_cache.get(key)
        if hit and ttl and (time.time() - hit[0]) < ttl:
            return hit[1]

    df = _upstox_candles(sym_def, tf, lookback)
    if df is not None:
        if tf != "1d":
            _tf_cache[key] = (time.time(), df)
        else:
            _1d_cache[sym_def["sym"]] = (datetime.now(IST).date().isoformat(), df)
    return df


def _get_1d_cached(sym_def: dict) -> Optional[pd.DataFrame]:
    """Get cached 1d data or fetch fresh."""
    sym = sym_def["sym"]
    hit = _1d_cache.get(sym)
    if hit and hit[0] == datetime.now(IST).date().isoformat():
        return hit[1]
    df = fetch_candles(sym_def, "1d", 30)
    if df is not None:
        _1d_cache[sym] = (datetime.now(IST).date().isoformat(), df)
    return df


# ═══════════════════════════════════════════════════════════════════
# S9 DETECTION
# ═══════════════════════════════════════════════════════════════════

def _blast_up(df: pd.DataFrame) -> Optional[dict]:
    d = _add_indicators(df).dropna().reset_index(drop=True)
    if len(d) < 2:
        return None
    last = d.iloc[-1]
    if not (float(last["close"]) > float(last["bb_upper"])):
        return None
    return {
        "open": float(last["open"]), "high": float(last["high"]),
        "low": float(last["low"]), "close": float(last["close"]),
        "volume": float(last.get("volume", 0) or 0),
        "ts": last["timestamp"], "bb_upper": float(last["bb_upper"]),
    }


def _pp_inside_bb(df: Optional[pd.DataFrame], pp: float, prev_candle: bool = False) -> Optional[bool]:
    if df is None or len(df) < BB_PERIOD + 2:
        return None
    d = _add_indicators(df).dropna()
    if len(d) < 2:
        return None
    row = d.iloc[-2] if prev_candle else d.iloc[-1]
    up, lo = float(row["bb_upper"]), float(row["bb_lower"])
    if pd.isna(up) or pd.isna(lo):
        return None
    return bool(lo < pp < up)


def _is_bb_flat(df: pd.DataFrame) -> bool:
    if df is None or len(df) < BB_PERIOD + FLAT_CANDLES + 2:
        return False
    d = _add_indicators(df).dropna()
    if len(d) < FLAT_CANDLES + 1:
        return False
    win = d.iloc[-(FLAT_CANDLES + 1):-1]
    return (_pct_range(win["sma20"]) < FLAT_THRESHOLD and
            _pct_range(win["bb_upper"]) < FLAT_THRESHOLD and
            _pct_range(win["bb_lower"]) < FLAT_THRESHOLD)


def _entry_gate(ts: datetime, candle_open: float, candle_close: float, candle_high: float,
                pd_high: Optional[float], pd_vol: Optional[float], day_high: Optional[float],
                seg: str) -> tuple[bool, str]:
    open_hr, open_mn = SEGMENT_OPEN.get(seg, (0, 0))
    open_dt = ts.replace(hour=open_hr, minute=open_mn, second=0, microsecond=0)

    # Opening window: first 15 min — strict
    if open_dt <= ts <= open_dt + timedelta(minutes=15):
        if pd_high is None:
            return False, "PDH missing (opening)"
        if pd_vol is not None and pd_vol < 1000:
            return False, "Illiquid (opening)"
        if candle_high > pd_high:
            return True, f"Opening gate OK"
        return False, f"Opening gate FAIL"

    # Mid-day/evening — bypass if illiquid
    if pd_vol is not None and pd_vol < 1000:
        return True, "Gate bypassed (illiquid)"
    if day_high is None:
        return True, "Gate bypassed (day high unavailable)"
    if candle_high > day_high:
        return True, "Mid-day gate OK"
    return False, "Mid-day gate FAIL"


def detect_s9(sym_def: dict, tf: str = "5m") -> Optional[dict]:
    """Run S9 detection on one symbol + timeframe."""
    lookback = 200 if tf == "1d" else 120
    df = fetch_candles(sym_def, tf, lookback)
    if df is None or len(df) < BB_PERIOD + 5:
        return None

    df_ind = _add_indicators(df).dropna().reset_index(drop=True)
    if len(df_ind) < BB_PERIOD + 2:
        return None

    last = df_ind.iloc[-1]

    # Blast
    if not (float(last["close"]) > float(last["bb_upper"])):
        return None

    # Pivots from 1d (or current df for crypto)
    df_for_pivots = _get_1d_cached(sym_def) if DAILY_TF_FOR.get(sym_def["seg"], True) else df
    pivots = _calc_pivots(df_for_pivots)
    if not pivots:
        return None
    pp = pivots["PP"]

    # PP inside BB on all TFs
    tfs_to_check = ["5m", "15m", "30m", "1h"]
    if DAILY_TF_FOR.get(sym_def["seg"], True):
        tfs_to_check.append("1d")

    for ctf in tfs_to_check:
        cdf = fetch_candles(sym_def, ctf, 200 if ctf == "1d" else 120)
        if ctf == "5m":
            ok = _pp_inside_bb(cdf, pp, prev_candle=True)
        else:
            ok = _pp_inside_bb(cdf, pp)
        if ok is not True:
            return None

    # Volume spike
    avg_vol = df_ind["volume"].iloc[-(VOL_PERIOD + 2):-1].mean()
    vol_spike = float(last["volume"]) > avg_vol * VOL_MULT if avg_vol > 0 else False

    # HM
    hm = _hm_signal(df_ind)

    # Jackpot (flat BB on 15m/1h)
    jackpot = False
    for flat_tf in ["15m", "1h"]:
        fdf = fetch_candles(sym_def, flat_tf, 120)
        if _is_bb_flat(fdf):
            jackpot = True
            break

    # Entry gate
    seg = sym_def["seg"]
    pd_hlcv = _prev_day_hlcv(df_for_pivots)
    pd_high = pd_hlcv[0] if pd_hlcv else None
    pd_vol = pd_hlcv[3] if pd_hlcv else None
    today_date = pd.to_datetime(df["timestamp"].iloc[-1]).date()
    day_rows = df[pd.to_datetime(df["timestamp"]).dt.date == today_date]
    day_high = float(day_rows["high"].iloc[:-1].max()) if len(day_rows) > 1 else None

    ok, _ = _entry_gate(
        ts=pd.to_datetime(last["timestamp"]).to_pydatetime(),
        candle_open=float(last["open"]), candle_close=float(last["close"]),
        candle_high=float(last["high"]),
        pd_high=pd_high, pd_vol=pd_vol, day_high=day_high, seg=seg,
    )
    if not ok:
        return None

    # Weekly PP
    df_1d = _get_1d_cached(sym_def) if DAILY_TF_FOR.get(seg, True) else None
    weekly_pp = _calc_weekly_pp(df_1d)

    # Stars
    stars = _compute_stars(float(last["close"]), pivots, weekly_pp)
    if stars < MIN_STARS:
        return None

    return {
        "symbol": sym_def["sym"],
        "tf": tf,
        "direction": "BULL",
        "close": round(float(last["close"]), 2),
        "stars": stars,
        "jackpot": jackpot,
        "vol_spike": vol_spike,
        "hm": hm,
        "pivots": {k: round(v, 2) for k, v in pivots.items()},
        "weekly_pp": round(weekly_pp, 2) if weekly_pp else None,
        "bb_upper": round(float(last["bb_upper"]), 2),
        "bb_lower": round(float(last["bb_lower"]), 2),
        "timestamp": pd.to_datetime(last["timestamp"]).strftime("%H:%M"),
        "seg": seg,
        "kind": sym_def["kind"],
    }


# ═══════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

def _get_state(sym: str) -> dict:
    if sym not in _states:
        _states[sym] = {
            "blast_ts": None, "stars_sent": 0, "blacklisted": False,
            "ladder_tfs": [],
        }
    return _states[sym]


def _maybe_reset_day() -> bool:
    global _day, _states
    today = datetime.now(IST).date().isoformat()
    if _day != today:
        _day = today
        _states.clear()
        _tf_cache.clear()
        _1d_cache.clear()
        _weekly_pp_cache.clear()
        return True
    return False


def _should_blacklist(sym_def: dict) -> bool:
    """Only stocks get blacklisted at R3."""
    return sym_def.get("kind") == "stock"


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════

def _send_tg(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=12,
        )
        return r.json().get("ok", False)
    except Exception as e:
        logger.error(f"[TG] Send fail: {e}")
        return False


def _tg_alert_compact(sig: dict, prefix: str = "") -> str:
    """Compact alert for NSE/MCX options + stocks."""
    tag = sig["symbol"].replace("_", "").replace("USD", "")
    return (
        f"🔥 S9 BLAST {'⭐' * sig['stars']}\n"
        f"{prefix}{sig['symbol']}  {sig['tf'].upper()}  ₹{sig['close']}\n"
        f"PP={sig['pivots'].get('PP', '—')} R1={sig['pivots'].get('R1', '—')} "
        f"R2={sig['pivots'].get('R2', '—')} R3={sig['pivots'].get('R3', '—')}\n"
        f"#TPS #S9 #{tag}"
    )


def _tg_alert_full(sig: dict, kind: str = "BLAST") -> str:
    """Full alert for crypto."""
    stars = "⭐" * sig["stars"]
    header = f"🔥 JACKPOT S9 {kind} {stars}" if sig["jackpot"] else f"🔥 S9 {kind} {stars}"
    p = sig["pivots"]
    lines = [header, f"📊 {sig['symbol']}  {sig['tf'].upper()}",
             f"💰 ${sig['close']}  🕐 {sig['timestamp']} IST", "",
             f"📈 PP={p.get('PP', '—')} R1={p.get('R1', '—')} R2={p.get('R2', '—')} R3={p.get('R3', '—')}"]
    if sig.get("weekly_pp"):
        lines.append(f"📅 Weekly PP={sig['weekly_pp']}")
    lines += ["", f"🎯 HM: {sig['hm']['signal']} {'✅' if sig['hm']['confirmed'] else '⚠️'}"]
    if sig["vol_spike"]:
        lines.append("⚡ Volume Spike!")
    lines.append(f"#TPS #S9 #{sig['symbol'].replace('USD', '')}")
    return "\n".join(lines)


def _tg_upgrade(sym: str, stars: int, close: float, pivots: dict, seg: str) -> str:
    tag = sym.replace("_", "").replace("USD", "")
    if seg == "CRYPTO":
        return (
            f"⬆️ S9 UPGRADE {'⭐' * stars}\n"
            f"📊 {sym}\n💰 ${close}\n"
            f"📈 PP={pivots.get('PP', '—')} R1={pivots.get('R1', '—')} R2={pivots.get('R2', '—')} R3={pivots.get('R3', '—')}\n"
            f"#TPS #S9 #{sym.replace('USD', '')}"
        )
    return (
        f"⬆️ S9 UPGRADE {'⭐' * stars}\n"
        f"{sym}  ₹{close}\n"
        f"PP={pivots.get('PP', '—')} R1={pivots.get('R1', '—')} "
        f"R2={pivots.get('R2', '—')} R3={pivots.get('R3', '—')}\n"
        f"#TPS #S9 #{tag}"
    )


# ═══════════════════════════════════════════════════════════════════
# SCANNER CORE
# ═══════════════════════════════════════════════════════════════════

def scan_symbol(sym_def: dict) -> int:
    """Scan one instrument across TFs. Returns alerts sent."""
    sym = sym_def["sym"]
    seg = sym_def["seg"]
    alerts = 0
    st = _get_state(sym)

    _maybe_reset_day()
    if st.get("blacklisted") and _should_blacklist(sym_def):
        return 0

    # Roll expiries
    if seg == "CRYPTO":
        _maybe_roll_crypto_expiry()
    elif seg == "NSE":
        _maybe_roll_nse_expiry()
    elif seg == "MCX":
        _maybe_roll_mcx_expiry()

    for tf in ["5m", "15m", "30m", "1h"]:
        try:
            sig = detect_s9(sym_def, tf)
            if not sig:
                continue

            new_stars = sig["stars"]

            if st.get("blast_ts") is None:
                # New blast
                st["blast_ts"] = sig["timestamp"]
                st["stars_sent"] = new_stars
                msg = _tg_alert_compact(sig) if seg != "CRYPTO" else _tg_alert_full(sig)
                if _send_tg(msg):
                    alerts += 1
                    logger.info(f"[S9] ✅ BLAST {sym} {tf} stars={new_stars}")

                # Blacklist at R3 (stocks only)
                if new_stars >= BLACKLIST_AT_BASE and _should_blacklist(sym_def):
                    st["blacklisted"] = True
                    logger.info(f"[S9] {sym} blacklisted (R3)")

            elif new_stars > st["stars_sent"]:
                # Star upgrade
                st["stars_sent"] = new_stars
                msg = _tg_upgrade(sym, new_stars, sig["close"], sig["pivots"], seg)
                if _send_tg(msg):
                    alerts += 1
                    logger.info(f"[S9] ⬆️ UPGRADE {sym} stars={new_stars}")

                if new_stars >= BLACKLIST_AT_BASE and _should_blacklist(sym_def):
                    st["blacklisted"] = True

            time.sleep(0.05)
        except Exception as e:
            logger.error(f"[S9] {sym} {tf}: {e}")

    return alerts


def run_scan_cycle() -> None:
    """One full scan cycle across all instruments."""
    now = datetime.now(IST)
    _maybe_reset_day()

    if _scanner_paused:
        logger.info("[S9] Paused")
        return

    logger.info(f"[S9] Cycle — {now.strftime('%H:%M IST')} | universe={len(UNIVERSE)}")

    total = 0
    for item in UNIVERSE:
        try:
            # Skip instruments outside their market hours
            seg = item["seg"]
            hh = now.hour
            # Simple market hours gate
            if seg == "NSE" and not (9 <= hh <= 15):
                continue
            if seg == "MCX" and not (9 <= hh <= 23):
                continue

            total += scan_symbol(item)
            time.sleep(0.02)  # rate limit
        except Exception as e:
            logger.error(f"[S9] {item['sym']}: {e}")

    logger.info(f"[S9] Cycle done — alerts={total}")


def scheduler() -> None:
    """5-minute aligned scan loop."""
    logger.info("[S9] Scheduler started")
    while True:
        try:
            now = datetime.now(IST)
            nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            while nxt.minute % 5 != 0:
                nxt += timedelta(minutes=1)
            wait = (nxt - now).total_seconds()
            if wait > 0:
                time.sleep(wait)
            run_scan_cycle()
        except Exception as e:
            logger.error(f"[S9] Scheduler error: {e}")
            time.sleep(30)


# ═══════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ═══════════════════════════════════════════════════════════════════

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        state = {
            "scanner": "S9",
            "paused": _scanner_paused,
            "universe": len(UNIVERSE),
            "symbols": [i["sym"] for i in UNIVERSE[:10]],
            "states": {k: {"stars_sent": v.get("stars_sent", 0),
                           "blacklisted": v.get("blacklisted", False)}
                       for k, v in _states.items()},
            "time": datetime.now(IST).isoformat(),
        }
        self.wfile.write(json.dumps(state).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        logger.info(f"[API] POST {self.path}: {body.decode()[:200]}")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════════

def telegram_bot():
    global _scanner_paused
    logger.info("[BOT] Started")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params, timeout=35,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat = str(msg.get("chat", {}).get("id", ""))
                if chat != str(TELEGRAM_CHAT_ID):
                    continue

                if text == "/status":
                    active = sum(1 for v in _states.values() if v.get("blast_ts"))
                    bl = sum(1 for k, v in _states.items() if v.get("blacklisted"))
                    _send_tg(
                        f"📊 S9 Scanner\n\n"
                        f"▶️ {'Paused' if _scanner_paused else 'Running'}\n"
                        f"📡 Delta: BTC/ETH 4-OTM options\n"
                        f"📡 Upstox: NSE 100 (2-OTM) + NSE/MCX index options\n"
                        f"🔍 Universe: {len(UNIVERSE)} instruments\n"
                        f"🔥 Active blasts: {active}\n"
                        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}\n\n"
                        f"Commands: /status /pause /resume /scan /help"
                    )
                elif text == "/pause":
                    _scanner_paused = True
                    _send_tg("⏸ Scanner Paused. /resume to start.")
                elif text == "/resume":
                    _scanner_paused = False
                    _send_tg("▶️ Scanner Resumed!")
                elif text == "/scan":
                    _send_tg("⏳ Running manual scan...")
                    run_scan_cycle()
                    _send_tg(f"✅ Scan complete! {len(_states)} instruments tracked.")
                elif text == "/help":
                    _send_tg(
                        "📖 S9 Scanner Commands:\n\n"
                        "/status — Full status\n"
                        "/pause · /resume\n"
                        "/scan — Manual scan\n"
                        "/help — This message\n\n"
                        "S9 Setup: Daily PP inside BB on all TFs + 5m blast above UBB\n"
                        "Stars: ⭐⭐PP ⭐⭐⭐R1 ⭐⭐⭐⭐R2 ⭐⭐⭐⭐⭐R3 +1 weekly\n"
                        "Blacklist at R3 (stocks only)"
                    )
        except Exception as e:
            logger.error(f"[BOT] Error: {e}")
            time.sleep(5)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("S9 Scanner — Multi-Exchange (Delta + Upstox)")
    logger.info(f"Universe: {len(UNIVERSE)} instruments")
    logger.info(f"  CRYPTO: 4 (BTC/ETH 4-OTM options)")
    nse_count = sum(1 for u in UNIVERSE if u["seg"] == "NSE")
    mcx_count = sum(1 for u in UNIVERSE if u["seg"] == "MCX")
    logger.info(f"  NSE: {nse_count} options (Nifty100 2-OTM + indices)")
    logger.info(f"  MCX: {mcx_count} options")
    logger.info(f"Time: {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}")
    logger.info("=" * 60)

    # Pre-load Upstox master
    if UPSTOX_TOKEN:
        logger.info("[UPSTOX] Loading instrument master...")
        _load_upstox_master()
        _maybe_roll_nse_expiry()
        _maybe_roll_mcx_expiry()
    else:
        logger.warning("[UPSTOX] No token — NSE/MCX scan disabled")

    _maybe_roll_crypto_expiry()
    _maybe_reset_day()

    # Startup Telegram ping
    _send_tg(
        f"🚀 S9 Scanner LIVE\n\n"
        f"📊 Nifty 100 options (2-OTM) + NSE/MCX index options\n"
        f"₿ BTC/ETH 4-OTM options\n"
        f"📡 Delta + Upstox\n"
        f"⏱ Har 5 min\n"
        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
    )

    # Start threads
    Thread(target=telegram_bot, daemon=True).start()
    Thread(target=lambda: http.server.HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(), daemon=True).start()

    # First scan
    run_scan_cycle()

    # Main loop
    scheduler()
