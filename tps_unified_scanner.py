"""
TPS UNIFIED SCANNER v2.0
========================
B3 Flat BB Detector (BTC/ETH via Delta Exchange)
+
S6 Flat BB Jackpot (Indian Markets via Upstox)
+
OTM Options Scanner (2 strikes OTM CE+PE — current expiry)

Segments:
  - NIFTY 50 Index + Future + 2 OTM CE/PE
  - SENSEX Index + Future + 2 OTM CE/PE
  - GOLD Future + GOLDM 2 OTM CE/PE
  - SILVERM Future + 2 OTM CE/PE
  - CRUDEOIL Future + 2 OTM CE/PE
  - NATURALGAS Future + 2 OTM CE/PE
  - Top 20 Stocks + their 2 OTM CE/PE
  - BTC/ETH (Delta Exchange)

Telegram Commands:
  /token <code>  — Daily Upstox token update
  /status        — Scanner status
  /pause         — Pause scanner
  /resume        — Resume scanner
"""

import os
import time
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date
from threading import Thread
import pytz

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s - %(message)s'
)
logger = logging.getLogger("TPS")

IST = pytz.timezone('Asia/Kolkata')

# ═══════════════════════════════════════════════════════════════════
# UNIFIED PARAMETERS
# ═══════════════════════════════════════════════════════════════════
class PARAMS:
    BB_PERIOD         = 20
    BB_STD            = 2
    SQUEEZE_MULT      = 0.7
    FLAT_CANDLES      = 4        # min 4 candles flat
    FLAT_THRESHOLD    = 0.002    # 0.2%
    VOL_MULT          = 1.5
    VOL_PERIOD        = 20
    MARUBOZU_WICK     = 0.10
    RSI_PERIOD        = 9
    SCAN_INTERVAL_SEC = 300      # 5 min
    TIMEFRAMES        = ["5m", "15m", "30m", "1h"]
    OTM_STRIKES       = 2        # Upstox (Indian) options
    DELTA_OTM_STRIKES = 4        # BTC/ETH — 4 strikes away

    TF_SCORE = {
        "5m": 3, "15m": 5, "30m": 6, "1h": 8,
    }
    TF_SCORE_MULTI = {
        frozenset(["5m", "15m"]):               6,
        frozenset(["15m", "30m"]):              7,
        frozenset(["30m", "1h"]):               9,
        frozenset(["5m", "15m", "30m", "1h"]): 10,
    }
    MIN_SCORE = 3

    # Strike gaps per segment
    STRIKE_GAP = {
        "NIFTY":      50,
        "SENSEX":     100,
        "GOLD":       100,    # GOLDM
        "SILVERM":    100,
        "CRUDEOIL":   50,
        "NATURALGAS": 10,
        "STOCK":      5,      # default for stocks, overridden dynamically
    }

# ═══════════════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════════════════════════════
UPSTOX_API_KEY         = os.environ.get("UPSTOX_API_KEY", "")
UPSTOX_SECRET_KEY      = os.environ.get("UPSTOX_SECRET_KEY", "")
UPSTOX_ACCESS_TOKEN    = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_REDIRECT_URI    = "https://127.0.0.1"

DELTA_API_KEY          = os.environ.get("DELTA_EXCHANGE_API_KEY", "")

TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")

RAILWAY_API_TOKEN      = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_PROJECT_ID     = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_SERVICE_ID     = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "production")

scanner_paused = False

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ═══════════════════════════════════════════════════════════════════
# RAILWAY TOKEN UPDATE
# ═══════════════════════════════════════════════════════════════════
def update_railway_token(new_token: str) -> bool:
    if not RAILWAY_API_TOKEN:
        return False
    query = """
    mutation variableUpsert($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    try:
        resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"},
            json={"query": query, "variables": {"input": {
                "projectId": RAILWAY_PROJECT_ID,
                "serviceId": RAILWAY_SERVICE_ID,
                "environmentId": RAILWAY_ENVIRONMENT_ID,
                "name": "UPSTOX_ACCESS_TOKEN",
                "value": new_token
            }}},
            timeout=30
        )
        return "errors" not in resp.json()
    except Exception as e:
        logger.error(f"Railway update error: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════
# UPSTOX TOKEN EXCHANGE
# ═══════════════════════════════════════════════════════════════════
def exchange_upstox_token(code: str) -> str | None:
    global UPSTOX_ACCESS_TOKEN
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "client_id": UPSTOX_API_KEY, "client_secret": UPSTOX_SECRET_KEY,
                  "redirect_uri": UPSTOX_REDIRECT_URI, "grant_type": "authorization_code"},
            timeout=30
        )
        token = resp.json().get("access_token")
        if token:
            UPSTOX_ACCESS_TOKEN = token
        return token
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════
# INDICATORS — BB + VWAP + EMA5 + PIVOT
# ═══════════════════════════════════════════════════════════════════
def calculate_indicators(df: pd.DataFrame, tf: str = "5m") -> pd.DataFrame:
    """
    tf: '5m', '15m', '30m', '1h' etc.
    Pivot period:
    - Below 30m (5m, 15m) → Previous DAY pivot
    - 30m and above (30m, 1h, 2h, 4h) → Previous WEEK pivot
    """
    df = df.copy()

    # Bollinger Band
    df["sma20"]    = df["close"].rolling(PARAMS.BB_PERIOD).mean()
    df["std20"]    = df["close"].rolling(PARAMS.BB_PERIOD).std()
    df["bb_upper"] = df["sma20"] + PARAMS.BB_STD * df["std20"]
    df["bb_lower"] = df["sma20"] - PARAMS.BB_STD * df["std20"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["sma20"]

    # EMA 5
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()

    # HM — Hilega Milega Indicator
    # RSI(9) = black line
    # EMA(3) of RSI(9) = blue line
    # WMA(21) of RSI(9) = red line
    delta     = df["close"].diff()
    gain      = delta.clip(lower=0)
    loss      = -delta.clip(upper=0)
    avg_gain  = gain.ewm(com=8, adjust=False).mean()   # RSI(9) ewm
    avg_loss  = loss.ewm(com=8, adjust=False).mean()
    rs        = avg_gain / avg_loss.replace(0, 1e-10)
    df["rsi9"]      = 100 - (100 / (1 + rs))                          # Black
    df["hm_blue"]   = df["rsi9"].ewm(span=3, adjust=False).mean()     # Blue = EMA(3) of RSI
    weights         = range(1, 22)
    df["hm_red"]    = df["rsi9"].rolling(21).apply(
        lambda x: sum(w * v for w, v in zip(weights, x)) / sum(weights), raw=True
    )                                                                   # Red = WMA(21) of RSI

    # VWAP (intraday reset — cumulative from first candle)
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"]        = df["typical_price"] * df["volume"]
    df["cum_tp_vol"]    = df["tp_vol"].cumsum()
    df["cum_vol"]       = df["volume"].cumsum()
    df["vwap"]          = df["cum_tp_vol"] / df["cum_vol"]

    # Pivot Point — TF ke hisaab se period decide karo
    # 5m/15m  → Previous DAY pivot (H+L+C/3)
    # 30m/1h+ → Previous WEEK pivot (H+L+C/3)
    try:
        if "timestamp" not in df.columns:
            pivot_val = float(df.iloc[-1]["close"])
        else:
            df_temp = df.copy()
            df_temp["ts"]   = pd.to_datetime(df_temp["timestamp"])
            df_temp["date"] = df_temp["ts"].dt.date

            # Decide pivot period based on TF
            use_weekly = tf in ["30m", "1h", "2h", "4h", "1d"]

            if use_weekly:
                # Previous WEEK pivot
                df_temp["year"] = df_temp["ts"].dt.isocalendar().year
                df_temp["week"] = df_temp["ts"].dt.isocalendar().week
                current_year = df_temp["year"].iloc[-1]
                current_week = df_temp["week"].iloc[-1]

                # Previous week candles
                prev_week = df_temp[
                    (df_temp["year"] < current_year) |
                    ((df_temp["year"] == current_year) & (df_temp["week"] < current_week))
                ]

                if len(prev_week) > 0:
                    last_year = prev_week["year"].iloc[-1]
                    last_week = prev_week["week"].iloc[-1]
                    last_prev_week = prev_week[
                        (prev_week["year"] == last_year) &
                        (prev_week["week"] == last_week)
                    ]
                    prev_H = float(last_prev_week["high"].max())
                    prev_L = float(last_prev_week["low"].min())
                    prev_C = float(last_prev_week.iloc[-1]["close"])
                    pivot_val = (prev_H + prev_L + prev_C) / 3
                else:
                    pivot_val = float(df.iloc[-1]["close"])
            else:
                # Previous DAY pivot (for 5m, 15m)
                today = df_temp["date"].iloc[-1]
                prev_day_candles = df_temp[df_temp["date"] < today]

                if len(prev_day_candles) > 0:
                    last_prev_date = prev_day_candles["date"].max()
                    prev_day = prev_day_candles[prev_day_candles["date"] == last_prev_date]
                    prev_H = float(prev_day["high"].max())
                    prev_L = float(prev_day["low"].min())
                    prev_C = float(prev_day.iloc[-1]["close"])
                    pivot_val = (prev_H + prev_L + prev_C) / 3
                else:
                    pivot_val = float(df.iloc[-1]["close"])

    except Exception as e:
        logger.error(f"[PIVOT] Error: {e}")
        pivot_val = float(df.iloc[-1]["close"])

    df["pivot"] = pivot_val
    return df

# Keep backward compat alias
def calculate_bb(df: pd.DataFrame, tf: str = "5m") -> pd.DataFrame:
    return calculate_indicators(df, tf)


# ═══════════════════════════════════════════════════════════════════
# HM — HILEGA MILEGA SIGNAL
# ═══════════════════════════════════════════════════════════════════
def get_hm_signal(df: pd.DataFrame) -> dict:
    """
    HM Indicator:
    Bullish: Blue cross above Red + all three above 50
    Bearish: Blue cross below Red + all three below 50
    Returns: {signal, rsi9, hm_blue, hm_red, confirmed}
    """
    if "hm_blue" not in df.columns or "hm_red" not in df.columns:
        return {"signal": "NEUTRAL", "confirmed": False, "rsi9": None, "hm_blue": None, "hm_red": None}

    df = df.dropna(subset=["rsi9", "hm_blue", "hm_red"])
    if len(df) < 3:
        return {"signal": "NEUTRAL", "confirmed": False, "rsi9": None, "hm_blue": None, "hm_red": None}

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    rsi9     = round(float(last["rsi9"]), 2)
    hm_blue  = round(float(last["hm_blue"]), 2)
    hm_red   = round(float(last["hm_red"]), 2)

    # Blue cross above Red (bullish)
    bull_cross = (float(prev["hm_blue"]) <= float(prev["hm_red"])) and (hm_blue > hm_red)
    # Blue cross below Red (bearish)
    bear_cross = (float(prev["hm_blue"]) >= float(prev["hm_red"])) and (hm_blue < hm_red)

    # All above 50 = bull confirmed, all below 50 = bear confirmed
    all_above_50 = rsi9 > 50 and hm_blue > 50 and hm_red > 50
    all_below_50 = rsi9 < 50 and hm_blue < 50 and hm_red < 50

    if bull_cross and all_above_50:
        signal    = "BULLISH 🔵"
        confirmed = True
    elif bear_cross and all_below_50:
        signal    = "BEARISH 🔴"
        confirmed = True
    elif bull_cross:
        signal    = "BULLISH 🔵"
        confirmed = False  # Cross hua par 50 ke upar nahi
    elif bear_cross:
        signal    = "BEARISH 🔴"
        confirmed = False
    elif hm_blue > hm_red:
        signal    = "BULLISH 🔵"
        confirmed = all_above_50
    else:
        signal    = "BEARISH 🔴"
        confirmed = all_below_50

    return {
        "signal":    signal,
        "confirmed": confirmed,
        "rsi9":      rsi9,
        "hm_blue":   hm_blue,
        "hm_red":    hm_red,
    }

# ═══════════════════════════════════════════════════════════════════
# S6 DETECTION (UNIFIED)
# ═══════════════════════════════════════════════════════════════════
def detect_s6(df: pd.DataFrame, tf: str) -> dict | None:
    if df is None or len(df) < PARAMS.BB_PERIOD + PARAMS.FLAT_CANDLES + 5:
        return None

    df = calculate_indicators(df, tf).dropna().reset_index(drop=True)
    if len(df) < PARAMS.BB_PERIOD:
        return None

    # Candle references — teen alag jagah, teen alag check
    last      = df.iloc[-1]   # Candle 0  — BLAST candle
    pre       = df.iloc[-2]   # Candle -1 — blast se ek pehle
    prev_flat = df.iloc[-(PARAMS.FLAT_CANDLES + 1):-1]  # candles -5 to -1

    def pct_range(s):
        m = s.mean()
        return 0 if m == 0 else (s.max() - s.min()) / m

    # ── 1. Squeeze — Candle -1 (pre-blast) ───────────────────────
    # avg BW: candles -22 to -2 (blast exclude)
    avg_bw  = df["bb_width"].iloc[-(PARAMS.VOL_PERIOD + 2):-1].mean()
    # Blast se pehle wala candle squeeze mein tha?
    squeeze = float(pre["bb_width"]) < avg_bw * PARAMS.SQUEEZE_MULT

    # ── 2. Flat Bands — Candles -5 to -1 ─────────────────────────
    # Blast candle exclude — woh toh bahar ja raha hai
    flat_bb = (pct_range(prev_flat["sma20"])    < PARAMS.FLAT_THRESHOLD and
               pct_range(prev_flat["bb_upper"]) < PARAMS.FLAT_THRESHOLD and
               pct_range(prev_flat["bb_lower"]) < PARAMS.FLAT_THRESHOLD)

    # ── 3. Blast — Last Candle (0) ────────────────────────────────
    # Volume — optional (bonus), not mandatory
    avg_vol   = df["volume"].iloc[-(PARAMS.VOL_PERIOD + 2):-1].mean()
    vol_spike = last["volume"] > avg_vol * PARAMS.VOL_MULT
    blast_up  = last["close"] > last["bb_upper"]
    blast_dn  = last["close"] < last["bb_lower"]

    # Squeeze(-1) + Flat(-5to-1) + Blast(0) — volume bonus mein
    if not (squeeze and flat_bb and (blast_up or blast_dn)):
        return None

    # ── 4. Pivot Check (PP should be below upper band) ────────────
    pivot      = float(last["pivot"])
    # Pre-blast candle ka BB use karo pivot check ke liye
    # (blast candle mein BB already break ho chuka hai)
    bb_upper   = float(pre["bb_upper"])
    bb_lower   = float(pre["bb_lower"])
    # PP pre-blast UBB ke neeche hona chahiye — 2% buffer
    pivot_ok   = pivot < bb_upper * 1.02

    # ── 5. VWAP + SMA20 + EMA5 Flat Check (optional — SUPER DUPER) ─
    vwap_flat  = pct_range(prev_flat["vwap"])  < PARAMS.FLAT_THRESHOLD if "vwap" in prev_flat.columns else False
    ema5_flat  = pct_range(prev_flat["ema5"])  < PARAMS.FLAT_THRESHOLD if "ema5" in prev_flat.columns else False
    sma_flat   = pct_range(prev_flat["sma20"]) < PARAMS.FLAT_THRESHOLD
    all_flat   = vwap_flat and ema5_flat and sma_flat  # SUPER DUPER condition

    # ── 6. Liquidity Sweep Check (1-4 candles before blast) ───────
    # Blast se pehle 1-4 candles mein price ne bb_lower touch kiya ho
    pre_blast  = df.iloc[-(PARAMS.FLAT_CANDLES + 2):-1]
    liq_sweep  = (pre_blast["low"] <= bb_lower * 1.001).any() if blast_up else                  (pre_blast["high"] >= bb_upper * 0.999).any()

    # SUPER DUPER = all_flat + liq_sweep + pivot_ok
    super_duper = all_flat and liq_sweep and pivot_ok

    direction = "BULLISH 🟢" if blast_up else "BEARISH 🔴"

    # HM signal
    hm = get_hm_signal(df)

    return {
        "direction":   direction,
        "tf":          tf,
        "tf_score":    PARAMS.TF_SCORE.get(tf, 0),
        "close":       round(float(last["close"]), 4),
        "bb_upper":    round(bb_upper, 4),
        "bb_lower":    round(bb_lower, 4),
        "bb_width":    round(float(last["bb_width"]) * 100, 3),
        "pivot":       round(pivot, 4),
        "pivot_ok":    pivot_ok,
        "vwap":        round(float(last["vwap"]), 4) if "vwap" in last else None,
        "ema5":        round(float(last["ema5"]), 4) if "ema5" in last else None,
        "all_flat":    all_flat,
        "liq_sweep":   liq_sweep,
        "super_duper": super_duper,
        "volume":      int(last["volume"]),
        "avg_vol":     int(avg_vol),
        "vol_spike":   vol_spike,
        "hm_signal":   hm["signal"],
        "hm_confirm":  hm["confirmed"],
        "hm_rsi9":     hm["rsi9"],
        "hm_blue":     hm["hm_blue"],
        "hm_red":      hm["hm_red"],
        "timestamp":   last["timestamp"].strftime("%H:%M") if hasattr(last["timestamp"], "strftime") else str(last["timestamp"]),
    }

# ═══════════════════════════════════════════════════════════════════
# MULTI-TF SCORE
# ═══════════════════════════════════════════════════════════════════
def get_multi_tf_score(signals: dict) -> int:
    active = frozenset(signals.keys())
    for combo, score in sorted(PARAMS.TF_SCORE_MULTI.items(), key=lambda x: -x[1]):
        if combo.issubset(active):
            return score
    return max((PARAMS.TF_SCORE.get(tf, 0) for tf in active), default=0)

# ═══════════════════════════════════════════════════════════════════
# ALERT FORMAT
# ═══════════════════════════════════════════════════════════════════
def format_alert(symbol: str, signals: dict, score: int, source: str, option_type: str = "") -> str:
    sig        = list(signals.values())[0]
    direction  = sig["direction"]
    icon       = "🌐" if source == "DELTA" else "🇮🇳"
    opt_label  = f" [{option_type}]" if option_type else ""
    score_bar  = "⭐" * min(score, 10)

    # TF string — e.g. "5M+15M" or "1H"
    tfs_str = "+".join([s.upper() for s in signals.keys()])

    # Super Duper check — any signal mein true ho
    super_duper = any(s.get("super_duper", False) for s in signals.values())
    all_flat    = any(s.get("all_flat", False) for s in signals.values())
    liq_sweep   = any(s.get("liq_sweep", False) for s in signals.values())
    pivot_ok    = sig.get("pivot_ok", True)

    # Header
    if super_duper:
        header = f"🚀💥 <b>TPS S6 — SUPER DUPER JACKPOT!</b> {icon}"
    else:
        header = f"🔥 <b>TPS S6 — FLAT BB JACKPOT</b> {icon}"

    msg = (
        f"{header}\n\n"
        f"📌 <b>{symbol}{opt_label}</b>\n"
        f"{direction}\n"
        f"⏰ Setup: <b>{tfs_str}</b>\n"
        f"💰 Price: <b>{sig['close']}</b>\n"
        f"📍 Pivot (PP): <b>{sig.get('pivot', 'N/A')}</b> {'✅' if pivot_ok else '⚠️ PP high!'}\n"
    )

    if sig.get("vwap"):
        msg += f"〽️ VWAP: {sig['vwap']} | EMA5: {sig.get('ema5', 'N/A')}\n"

    msg += f"🕐 {sig['timestamp']} IST\n\n"
    msg += f"📊 <b>Score: {score}/10</b>  {score_bar}\n"

    # Per-TF details
    for tf, s in signals.items():
        msg += f"  {tf.upper()}: BW={s['bb_width']}% | Vol={s['volume']:,} (avg {s['avg_vol']:,})\n"

    # Super Duper badges
    # Volume spike info (optional — bonus)
    vol_spike_any = any(s.get("vol_spike", False) for s in signals.values())

    if super_duper:
        msg += "\n🏆 <b>SUPER DUPER CONDITIONS:</b>\n"
        msg += f"  {'✅' if all_flat else '❌'} VWAP + SMA20 + EMA5 — Sab Flat\n"
        msg += f"  {'✅' if liq_sweep else '❌'} Liquidity Sweep (blast se pehle)\n"
        msg += f"  {'✅' if pivot_ok else '❌'} Pivot PP — Band ke neeche\n"
        if vol_spike_any:
            msg += "  ⭐ <b>BONUS</b> — Volume Spike confirmed!\n"
    elif all_flat or liq_sweep or vol_spike_any:
        msg += "\n✨ <b>Extra Conditions:</b>\n"
        if all_flat:
            msg += "  ✅ VWAP + SMA20 + EMA5 Flat\n"
        if liq_sweep:
            msg += "  ✅ Liquidity Sweep detected\n"
        if pivot_ok:
            msg += "  ✅ Pivot PP — Sahi position\n"
        if vol_spike_any:
            msg += "  ⭐ <b>BONUS</b> — Volume Spike confirmed!\n"

    # HM info — first signal se lo
    first_sig  = list(signals.values())[0]
    hm_signal  = first_sig.get("hm_signal", "")
    hm_confirm = first_sig.get("hm_confirm", False)
    hm_rsi9    = first_sig.get("hm_rsi9")
    hm_blue    = first_sig.get("hm_blue")
    hm_red     = first_sig.get("hm_red")

    if hm_rsi9 is not None:
        hm_status = "✅ Confirmed" if hm_confirm else "⚠️ Weak"
        msg += f"\n\n🎯 <b>HM (Hilega Milega):</b> {hm_signal} {hm_status}"
        msg += f"\n  RSI9={hm_rsi9} | Blue={hm_blue} | Red={hm_red}"

    msg += (
        f"\n\n⚡ <b>Options Buying — ATM Current Expiry</b>"
        f"\n🛑 SL = Blast candle low (Bull) / high (Bear)"
        f"\n\n#TPS #S6 #FlatBB #{symbol.replace(' ','').replace('-','')}"
    )
    if super_duper:
        msg += " #SuperDuper"

    return msg.strip()

# ═══════════════════════════════════════════════════════════════════
# UPSTOX HELPERS
# ═══════════════════════════════════════════════════════════════════
def upstox_headers():
    return {"Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}", "Accept": "application/json"}

UPSTOX_TF_MAP = {"5m": "5minute", "15m": "15minute", "30m": "30minute", "1h": "60minute"}

def fetch_upstox_candles(instrument_key: str, tf: str) -> pd.DataFrame | None:
    if not UPSTOX_ACCESS_TOKEN:
        return None
    resolution = UPSTOX_TF_MAP.get(tf, "5minute")
    today      = datetime.now(IST).date()
    from_date  = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date    = today.strftime("%Y-%m-%d")
    encoded    = requests.utils.quote(instrument_key, safe='')
    url        = f"https://api.upstox.com/v2/historical-candle/{encoded}/{resolution}/{to_date}/{from_date}"
    try:
        resp = requests.get(url, headers=upstox_headers(), timeout=15)
        data = resp.json()
        if data.get("status") != "success":
            return None
        candles = data["data"]["candles"]
        if not candles or len(candles) < PARAMS.BB_PERIOD:
            return None
        df = pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume","oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        logger.error(f"[UPSTOX] Candle error {instrument_key} {tf}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════
# OTM OPTIONS — DYNAMIC INSTRUMENT KEY FETCH
# ═══════════════════════════════════════════════════════════════════

# Cache to avoid repeated API calls
_option_cache = {}
_option_cache_time = {}
OPTION_CACHE_SECONDS = 3600  # 1 hour

def get_current_price(instrument_key: str) -> float | None:
    """LTP fetch karo Upstox se"""
    try:
        encoded = requests.utils.quote(instrument_key, safe='')
        url     = f"https://api.upstox.com/v2/market-quote/ltp?instrument_key={encoded}"
        resp    = requests.get(url, headers=upstox_headers(), timeout=10)
        data    = resp.json()
        if data.get("status") == "success":
            d = data["data"]
            key = list(d.keys())[0]
            return float(d[key]["last_price"])
    except Exception as e:
        logger.error(f"LTP fetch error {instrument_key}: {e}")
    return None

def round_to_strike(price: float, gap: int) -> int:
    """Price ko nearest strike pe round karo"""
    return int(round(price / gap) * gap)

def get_nearest_expiry_options(symbol_base: str, exchange: str, ltp: float, strike_gap: int, option_sym: str = None) -> list:
    """
    Current expiry ke 2 OTM CE aur 2 OTM PE instrument keys return karo.
    Upstox option chain API use karo.

    symbol_base: NIFTY, BANKNIFTY, GOLD, SILVERM, CRUDEOIL, NATURALGAS etc.
    exchange: NSE_FO ya MCX_FO
    option_sym: override symbol name for options (e.g. GOLDM for GOLD options)
    """
    cache_key = f"{symbol_base}_{exchange}"
    now_ts    = time.time()

    # Cache check
    if cache_key in _option_cache and (now_ts - _option_cache_time.get(cache_key, 0)) < OPTION_CACHE_SECONDS:
        cached = _option_cache[cache_key]
        # Recalculate strikes based on current ltp
        return _build_otm_keys(cached, ltp, strike_gap, PARAMS.OTM_STRIKES)

    try:
        # Fetch option contracts from Upstox
        url    = "https://api.upstox.com/v2/option/chain"
        params = {"instrument_key": f"{exchange}|{symbol_base}", "expiry_date": ""}
        resp   = requests.get(url, headers=upstox_headers(), params=params, timeout=15)
        data   = resp.json()

        if data.get("status") != "success":
            logger.warning(f"Option chain fail {symbol_base}: {data.get('errors','')}")
            return []

        contracts = data["data"]
        _option_cache[cache_key]      = contracts
        _option_cache_time[cache_key] = now_ts

        return _build_otm_keys(contracts, ltp, strike_gap, PARAMS.OTM_STRIKES)

    except Exception as e:
        logger.error(f"Option chain error {symbol_base}: {e}")
        return []

def _build_otm_keys(contracts: list, ltp: float, strike_gap: int, otm_count: int) -> list:
    """
    Contracts list se nearest expiry + OTM strikes find karo.
    Returns list of (label, instrument_key, option_type) tuples
    """
    if not contracts:
        return []

    # Sabse nearest expiry find karo
    today = date.today()
    expiries = sorted(set(c.get("expiry") for c in contracts if c.get("expiry")))
    nearest_expiry = None
    for exp in expiries:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            if exp_date >= today:
                nearest_expiry = exp
                break
        except:
            continue

    if not nearest_expiry:
        return []

    # Us expiry ke contracts filter karo
    exp_contracts = [c for c in contracts if c.get("expiry") == nearest_expiry]

    atm_strike = round_to_strike(ltp, strike_gap)
    result     = []

    for i in range(1, otm_count + 1):
        # CE — upar wala OTM
        ce_strike = atm_strike + (strike_gap * i)
        ce_contract = next(
            (c for c in exp_contracts if c.get("strike_price") == ce_strike and c.get("option_type") == "CE"),
            None
        )
        if ce_contract and ce_contract.get("instrument_key"):
            result.append((f"CE {ce_strike}", ce_contract["instrument_key"], "CE"))

        # PE — neeche wala OTM
        pe_strike = atm_strike - (strike_gap * i)
        pe_contract = next(
            (c for c in exp_contracts if c.get("strike_price") == pe_strike and c.get("option_type") == "PE"),
            None
        )
        if pe_contract and pe_contract.get("instrument_key"):
            result.append((f"PE {pe_strike}", pe_contract["instrument_key"], "PE"))

    return result

# ═══════════════════════════════════════════════════════════════════
# SCAN ONE INSTRUMENT + ITS OPTIONS
# ═══════════════════════════════════════════════════════════════════
def scan_instrument_with_options(
    symbol: str,
    instrument_key: str,
    option_base: str = None,       # e.g. "NIFTY", "GOLD"
    option_exchange: str = None,   # e.g. "NSE_FO", "MCX_FO"
    strike_gap: int = 50,
    alerts_list: list = None
):
    """
    1. Instrument scan karo (future/index)
    2. LTP se OTM strikes nikalo
    3. CE/PE options bhi scan karo
    """
    if alerts_list is None:
        alerts_list = []

    # ── Scan main instrument ──────────────────────────────────────
    signals = {}
    ltp     = None

    for tf in PARAMS.TIMEFRAMES:
        try:
            df = fetch_upstox_candles(instrument_key, tf)
            if df is not None and len(df) > 0:
                if ltp is None:
                    ltp = float(df.iloc[-1]["close"])
            signal = detect_s6(df, tf)
            if signal:
                signals[tf] = signal
            time.sleep(0.25)
        except Exception as e:
            logger.error(f"[UPSTOX] Error {symbol} {tf}: {e}")

    if signals:
        score = get_multi_tf_score(signals)
        if score >= PARAMS.MIN_SCORE:
            msg = format_alert(symbol, signals, score, "UPSTOX")
            send_telegram(msg)
            alerts_list.append(symbol)
            logger.info(f"[UPSTOX] ✅ {symbol} Score={score}/10")
            time.sleep(0.5)
    else:
        logger.info(f"[UPSTOX] {symbol}: No signal")

    # ── Scan OTM Options ──────────────────────────────────────────
    if option_base and option_exchange and ltp:
        try:
            otm_list = get_nearest_expiry_options(
                symbol_base=option_base,
                exchange=option_exchange,
                ltp=ltp,
                strike_gap=strike_gap
            )

            for label, opt_key, opt_type in otm_list:
                opt_signals = {}
                for tf in PARAMS.TIMEFRAMES:
                    try:
                        df     = fetch_upstox_candles(opt_key, tf)
                        signal = detect_s6(df, tf)
                        if signal:
                            opt_signals[tf] = signal
                        time.sleep(0.25)
                    except Exception as e:
                        logger.error(f"[OPT] Error {symbol} {label} {tf}: {e}")

                if opt_signals:
                    score = get_multi_tf_score(opt_signals)
                    if score >= PARAMS.MIN_SCORE:
                        msg = format_alert(symbol, opt_signals, score, "UPSTOX", option_type=f"{label} OTM")
                        send_telegram(msg)
                        alerts_list.append(f"{symbol} {label}")
                        logger.info(f"[OPT] ✅ {symbol} {label} Score={score}/10")
                        time.sleep(0.5)
                else:
                    logger.info(f"[OPT] {symbol} {label}: No signal")

        except Exception as e:
            logger.error(f"[OPT] Option scan error {symbol}: {e}")

# ═══════════════════════════════════════════════════════════════════
# UPSTOX INSTRUMENTS CONFIG
# ═══════════════════════════════════════════════════════════════════
UPSTOX_SCAN_LIST = [
    # (display_name, instrument_key, option_base, option_exchange, strike_gap)
    ("NIFTY 50",       "NSE_INDEX|Nifty 50",   "NIFTY",      "NSE_FO",  50),
    ("SENSEX",         "BSE_INDEX|SENSEX",      "SENSEX",     "BSE_FO",  100),
    ("NIFTY FUT",      "NSE_FO|NIFTY",          "NIFTY",      "NSE_FO",  50),
    ("GOLD FUT",       "MCX_FO|GOLD",           "GOLDM",      "MCX_FO",  100),
    ("SILVERM FUT",    "MCX_FO|SILVERM",        "SILVERM",    "MCX_FO",  100),
    ("CRUDE OIL FUT",  "MCX_FO|CRUDEOIL",      "CRUDEOIL",   "MCX_FO",  50),
    ("NAT GAS FUT",    "MCX_FO|NATURALGAS",     "NATURALGAS", "MCX_FO",  10),
]

# ═══════════════════════════════════════════════════════════════════
# DYNAMIC TOP 20 — Nifty 100 High OI + Volume Active Stocks
# ═══════════════════════════════════════════════════════════════════

# Nifty 100 complete list — symbol + strike gap
NIFTY100_UNIVERSE = [
    ("RELIANCE",    50),  ("TCS",         50),  ("HDFCBANK",    50),
    ("ICICIBANK",   10),  ("INFY",        50),  ("BAJFINANCE",  50),
    ("AXISBANK",    10),  ("KOTAKBANK",   20),  ("LT",          50),
    ("HINDUNILVR",  20),  ("SBIN",         5),  ("BHARTIARTL",  10),
    ("WIPRO",        5),  ("ONGC",         5),  ("SUNPHARMA",   20),
    ("TATAMOTORS",   5),  ("TITAN",       50),  ("MARUTI",     100),
    ("NTPC",         5),  ("ADANIENT",    50),  ("ADANIPORTS",  10),
    ("BAJAJ-AUTO",  50),  ("BAJAJFINSV",  10),  ("BPCL",         5),
    ("BRITANNIA",   50),  ("CIPLA",       10),  ("COALINDIA",    5),
    ("DRREDDY",     50),  ("EICHERMOT",   50),  ("GRASIM",      20),
    ("HCLTECH",     10),  ("HDFCLIFE",     5),  ("HEROMOTOCO",  50),
    ("HINDALCO",     5),  ("ICICIPRULI",  10),  ("INDUSINDBK",  10),
    ("ITC",          5),  ("JSWSTEEL",     5),  ("M&M",         20),
    ("NESTLEIND",  100),  ("POWERGRID",    5),  ("SHRIRAMFIN",  50),
    ("TATACONSUM",  10),  ("TATASTEEL",    2),  ("TECHM",       10),
    ("ULTRACEMCO",  50),  ("UPL",          5),  ("VEDL",         5),
    ("ZOMATO",       2),  ("PIDILITIND",  50),  ("DMART",       50),
    ("SBILIFE",     10),  ("ICICIGI",     50),  ("GODREJCP",    10),
    ("SIEMENS",     50),  ("HAVELLS",     10),  ("DABUR",        5),
    ("MARICO",       5),  ("MUTHOOTFIN",  10),  ("NAUKRI",      50),
    ("LTIM",        50),  ("PERSISTENT",  50),  ("COFORGE",     50),
    ("INDIGO",      50),  ("TRENT",       50),  ("DIXON",       50),
    ("ZYDUSLIFE",    5),  ("MANKIND",     50),  ("MOTHERSON",    2),
    ("ASHOKLEY",     2),  ("BALKRISIND",  20),  ("CUMMINSIND",  20),
    ("MPHASIS",     50),  ("PNB",          2),  ("BANKBARODA",   2),
    ("CANBK",        2),  ("FEDERALBNK",   2),  ("IDFCFIRSTB",   2),
    ("IDEA",         1),  ("SUZLON",       2),  ("IRFC",         2),
    ("NHPC",         2),  ("RECLTD",       5),  ("PFC",          5),
    ("SAIL",         2),  ("NATIONALUM",   2),  ("HFCL",         1),
    ("IRCTC",       10),  ("JUBLFOOD",    20),  ("PIIND",        50),
    ("TORNTPHARM",  10),  ("ALKEM",       50),  ("AUROPHARMA",   5),
    ("BIOCON",       2),  ("GLENMARK",     5),  ("LUPIN",       10),
    ("ABFRL",        2),  ("PAGEIND",    100),  ("VOLTAS",      10),
    ("WHIRLPOOL",   20),  ("TATAPOWER",    2),  ("CGPOWER",      5),
    ("ABB",         50),  ("BEL",          2),  ("HAL",         50),
]

# Cache for dynamic stock list
_active_stocks_cache = []
_active_stocks_time  = 0
ACTIVE_STOCKS_REFRESH = 3600 * 4  # 4 ghante mein refresh (market ke doran)

def fetch_active_nifty100_stocks(top_n: int = 100) -> list:
    """
    Nifty 100 se top N stocks fetch karo jo:
    - OI + Volume combined score pe highest hain
    - Pichhle 1-2 hafte mein active hain
    Returns: [(symbol, instrument_key, option_base, exchange, strike_gap), ...]
    """
    global _active_stocks_cache, _active_stocks_time

    now_ts = time.time()
    if _active_stocks_cache and (now_ts - _active_stocks_time) < ACTIVE_STOCKS_REFRESH:
        logger.info(f"[STOCKS] Cache use kar raha hoon — {len(_active_stocks_cache)} stocks")
        return _active_stocks_cache

    logger.info("[STOCKS] Nifty 100 active stocks fetch kar raha hoon (OI+Volume)...")

    if not UPSTOX_ACCESS_TOKEN:
        logger.warning("[STOCKS] Token missing — fallback to default list")
        return _get_fallback_stocks(top_n)

    scored = []

    for symbol, gap in NIFTY100_UNIVERSE:
        try:
            # NSE FO option chain se OI + Volume fetch karo
            url    = "https://api.upstox.com/v2/option/chain"
            params = {"instrument_key": f"NSE_FO|{symbol}", "expiry_date": ""}
            resp   = requests.get(url, headers=upstox_headers(), params=params, timeout=10)
            data   = resp.json()

            if data.get("status") != "success":
                time.sleep(0.2)
                continue

            contracts = data.get("data", [])
            if not contracts:
                time.sleep(0.2)
                continue

            # Total OI + Volume across all strikes + expiries
            total_oi  = sum(float(c.get("call_options", {}).get("market_data", {}).get("oi", 0) or 0) +
                            float(c.get("put_options",  {}).get("market_data", {}).get("oi", 0) or 0)
                            for c in contracts)

            total_vol = sum(float(c.get("call_options", {}).get("market_data", {}).get("volume", 0) or 0) +
                            float(c.get("put_options",  {}).get("market_data", {}).get("volume", 0) or 0)
                            for c in contracts)

            # Combined score — normalize karo
            combined_score = total_oi + (total_vol * 2)  # volume ko double weight

            if combined_score > 0:
                scored.append({
                    "symbol":    symbol,
                    "gap":       gap,
                    "oi":        total_oi,
                    "volume":    total_vol,
                    "score":     combined_score,
                })
                logger.info(f"[STOCKS] {symbol}: OI={int(total_oi):,} Vol={int(total_vol):,} Score={int(combined_score):,}")

            time.sleep(0.3)  # Rate limit

        except Exception as e:
            logger.error(f"[STOCKS] Error fetching {symbol}: {e}")
            time.sleep(0.2)
            continue

    if not scored:
        logger.warning("[STOCKS] Koi data nahi mila — fallback use kar raha hoon")
        return _get_fallback_stocks(top_n)

    # Sort by combined score descending — top N lo
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_stocks = scored[:top_n]

    logger.info(f"[STOCKS] Top {top_n} active stocks selected:")
    for i, s in enumerate(top_stocks, 1):
        logger.info(f"  {i}. {s['symbol']} — Score={int(s['score']):,} OI={int(s['oi']):,} Vol={int(s['volume']):,}")

    # Format for scanner
    result = [
        (s["symbol"], f"NSE_FO|{s['symbol']}", s["symbol"], "NSE_FO", s["gap"])
        for s in top_stocks
    ]

    _active_stocks_cache = result  # Top N stocks (default 100)
    _active_stocks_time  = now_ts
    return result

def _get_fallback_stocks(top_n: int) -> list:
    """Token missing ya API fail — hardcoded fallback"""
    fallback = [
        ("RELIANCE",   "NSE_FO|RELIANCE",   "RELIANCE",   "NSE_FO", 50),
        ("TCS",        "NSE_FO|TCS",        "TCS",        "NSE_FO", 50),
        ("HDFCBANK",   "NSE_FO|HDFCBANK",   "HDFCBANK",   "NSE_FO", 50),
        ("ICICIBANK",  "NSE_FO|ICICIBANK",  "ICICIBANK",  "NSE_FO", 10),
        ("INFY",       "NSE_FO|INFY",       "INFY",       "NSE_FO", 50),
        ("SBIN",       "NSE_FO|SBIN",       "SBIN",       "NSE_FO",  5),
        ("AXISBANK",   "NSE_FO|AXISBANK",   "AXISBANK",   "NSE_FO", 10),
        ("BAJFINANCE", "NSE_FO|BAJFINANCE", "BAJFINANCE", "NSE_FO", 50),
        ("TATAMOTORS", "NSE_FO|TATAMOTORS", "TATAMOTORS", "NSE_FO",  5),
        ("ZOMATO",     "NSE_FO|ZOMATO",     "ZOMATO",     "NSE_FO",  2),
    ]
    return fallback[:top_n]

# ═══════════════════════════════════════════════════════════════════
# UPSTOX SCANNER — MAIN
# ═══════════════════════════════════════════════════════════════════
def run_upstox_scanner():
    if not UPSTOX_ACCESS_TOKEN:
        logger.warning("[UPSTOX] Token missing! /token command bhejo Telegram pe.")
        send_telegram("⚠️ <b>Upstox Token Missing!</b>\n\nTelegram mein bhejo:\n/token &lt;aapka_code&gt;")
        return

    logger.info("[UPSTOX] Scan cycle start...")
    alerts = []

    # Index + Futures + Commodities
    for (symbol, ikey, opt_base, opt_exch, gap) in UPSTOX_SCAN_LIST:
        scan_instrument_with_options(symbol, ikey, opt_base, opt_exch, gap, alerts)
        time.sleep(0.5)

    # Top 20 Active Nifty 100 Stocks for S6 multi-TF scan (speed ke liye limited)
    # Money Printer + Super Flat mein saare 100 scan hote hain alag se
    # Cache se lo (100 tak stored hai), top 20 S6 ke liye
    if _active_stocks_cache:
        active_stocks = _active_stocks_cache[:20]
    else:
        active_stocks = fetch_active_nifty100_stocks(top_n=100)[:20]
    logger.info(f"[UPSTOX] Scanning {len(active_stocks)} active stocks...")
    for (symbol, ikey, opt_base, opt_exch, gap) in active_stocks:
        scan_instrument_with_options(symbol, ikey, opt_base, opt_exch, gap, alerts)
        time.sleep(0.5)

    logger.info(f"[UPSTOX] Cycle done. Total alerts: {len(alerts)}")

# ═══════════════════════════════════════════════════════════════════
# DELTA EXCHANGE (BTC/ETH + OPTIONS)
# ═══════════════════════════════════════════════════════════════════
DELTA_SYMBOLS = ["BTCUSD", "ETHUSD"]
DELTA_TF_MAP  = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}

# Strike gaps: BTC=$500, ETH=$50
DELTA_STRIKE_GAP = {"BTCUSD": 500, "ETHUSD": 50}

def fetch_delta_candles(symbol: str, tf: str) -> pd.DataFrame | None:
    resolution = DELTA_TF_MAP.get(tf, "5m")
    end_time   = int(time.time())
    start_time = end_time - (300 * PARAMS.BB_PERIOD * 3)
    try:
        resp = requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={"resolution": resolution, "symbol": symbol, "start": start_time, "end": end_time},
            timeout=15
        )
        data    = resp.json()
        candles = data.get("result", [])
        if not candles or len(candles) < PARAMS.BB_PERIOD:
            return None
        df = pd.DataFrame(candles).rename(columns={
            "time":"timestamp","open":"open","high":"high",
            "low":"low","close":"close","volume":"volume"
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        logger.error(f"Delta candle error {symbol} {tf}: {e}")
        return None

def get_delta_otm_options(base_symbol: str, ltp: float) -> list:
    """
    Delta Exchange se nearest expiry OTM options fetch karo.
    BTC: C-BTC-95000-DDMMYY / P-BTC-94000-DDMMYY
    ETH: C-ETH-3200-DDMMYY  / P-ETH-3000-DDMMYY
    Returns: [(label, symbol, option_type), ...]
    """
    try:
        underlying = "BTC" if "BTC" in base_symbol else "ETH"
        gap        = DELTA_STRIKE_GAP.get(base_symbol, 500)

        resp = requests.get(
            "https://api.india.delta.exchange/v2/products",
            params={"contract_types": "call_options,put_options", "states": "live"},
            timeout=15
        )
        products = resp.json().get("result", [])

        # Sirf is underlying ke options
        opts = [
            p for p in products
            if p.get("underlying_asset", {}).get("symbol", "") == underlying
        ]
        if not opts:
            logger.warning(f"[DELTA OPT] No options for {underlying}")
            return []

        # Nearest expiry
        today = date.today()
        expiry_dates = []
        for p in opts:
            exp_str = p.get("settlement_time", "")[:10]
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                if exp_date >= today:
                    expiry_dates.append(exp_date)
            except:
                continue

        if not expiry_dates:
            return []

        nearest_expiry = min(expiry_dates).strftime("%Y-%m-%d")
        exp_opts = [p for p in opts if p.get("settlement_time", "")[:10] == nearest_expiry]

        atm    = round_to_strike(ltp, gap)
        result = []

        # Sirf 4th strike OTM (na ki 1,2,3,4 saare)
        i = PARAMS.DELTA_OTM_STRIKES  # = 4

        # CE — 4 strikes upar
        ce_strike = atm + (gap * i)
        ce = next((p for p in exp_opts
                   if p.get("contract_type") == "call_options"
                   and int(float(p.get("strike_price", 0))) == ce_strike), None)
        if ce:
            result.append((f"CE {ce_strike} ({i} OTM)", ce["symbol"], "CE"))

        # PE — 4 strikes neeche
        pe_strike = atm - (gap * i)
        pe = next((p for p in exp_opts
                   if p.get("contract_type") == "put_options"
                   and int(float(p.get("strike_price", 0))) == pe_strike), None)
        if pe:
            result.append((f"PE {pe_strike} ({i} OTM)", pe["symbol"], "PE"))

        logger.info(f"[DELTA OPT] {underlying} expiry={nearest_expiry} | {[r[0] for r in result]}")
        return result

    except Exception as e:
        logger.error(f"[DELTA OPT] Error {base_symbol}: {e}")
        return []

def run_delta_scanner():
    logger.info("[DELTA] Scan cycle start...")
    alerts = 0

    for symbol in DELTA_SYMBOLS:
        signals = {}
        ltp     = None

        # ── Futures scan ──
        for tf in PARAMS.TIMEFRAMES:
            try:
                df = fetch_delta_candles(symbol, tf)
                if df is not None and len(df) > 0 and ltp is None:
                    ltp = float(df.iloc[-1]["close"])
                signal = detect_s6(df, tf)
                if signal:
                    signals[tf] = signal
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"[DELTA] Error {symbol} {tf}: {e}")

        if signals:
            score = get_multi_tf_score(signals)
            if score >= PARAMS.MIN_SCORE:
                send_telegram(format_alert(symbol, signals, score, "DELTA"))
                alerts += 1
                logger.info(f"[DELTA] ✅ {symbol} Score={score}/10")
                time.sleep(1)
        else:
            logger.info(f"[DELTA] {symbol}: No signal")

        # ── OTM Options scan ──
        if ltp:
            otm_list = get_delta_otm_options(symbol, ltp)
            for label, opt_sym, opt_type in otm_list:
                opt_signals = {}
                for tf in PARAMS.TIMEFRAMES:
                    try:
                        df     = fetch_delta_candles(opt_sym, tf)
                        signal = detect_s6(df, tf)
                        if signal:
                            opt_signals[tf] = signal
                        time.sleep(0.3)
                    except Exception as e:
                        logger.error(f"[DELTA OPT] {symbol} {label} {tf}: {e}")

                if opt_signals:
                    score = get_multi_tf_score(opt_signals)
                    if score >= PARAMS.MIN_SCORE:
                        msg = format_alert(symbol, opt_signals, score, "DELTA", option_type=f"{label} OTM")
                        send_telegram(msg)
                        alerts += 1
                        logger.info(f"[DELTA OPT] ✅ {symbol} {label} Score={score}/10")
                        time.sleep(1)
                else:
                    logger.info(f"[DELTA OPT] {symbol} {label}: No signal")

    logger.info(f"[DELTA] Cycle done. Total alerts: {alerts}")

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# 📋 POST-MARKET WATCHLIST SCAN (5:00 PM Daily)
# ═══════════════════════════════════════════════════════════════════
# Market close ke baad chalega — saare Nifty 100 ke OTM options check karke
# Super Flat + Near-breakout setups ki watchlist banega agle din ke liye
# ═══════════════════════════════════════════════════════════════════

_post_market_done_date = None  # Track karo aaj scan hua ya nahi

def run_post_market_watchlist():
    """
    5:00 PM ke baad chalega — saare Nifty 100 options scan karke watchlist bheje.
    1H timeframe use hota hai — agle din manual monitor ke liye.
    """
    if not UPSTOX_ACCESS_TOKEN:
        send_telegram("⚠️ <b>Post-Market Scan Skip</b>\nToken missing!")
        return

    logger.info("=" * 60)
    logger.info("📋 POST-MARKET WATCHLIST SCAN — Starting...")
    logger.info("=" * 60)

    send_telegram(
        "📋 <b>POST-MARKET SCAN STARTED</b>\n\n"
        "🔍 Nifty 100 ke active stocks ke OTM options check ho rahe hain...\n"
        "⏰ Kal ke liye watchlist ban rahi hai\n\n"
        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
    )

    start_time = time.time()
    super_flat_found   = []
    near_breakout_found = []

    # Full N100 scan — top 30 by OI+Vol
    active_stocks = fetch_active_nifty100_stocks(top_n=100)
    logger.info(f"[📋 Post-Market] Scanning {len(active_stocks)} liquid stocks...")

    for (symbol, ikey, opt_base, opt_exch, gap) in active_stocks:
        try:
            spot_key = ikey.replace("NSE_FO", "NSE_EQ")
            df_ltp   = fetch_upstox_candles(spot_key, "1h")
            if df_ltp is None or len(df_ltp) < 5:
                time.sleep(0.3)
                continue

            ltp      = float(df_ltp.iloc[-1]["close"])
            otm_list = get_nearest_expiry_options(opt_base, opt_exch, ltp, gap)

            if not otm_list:
                time.sleep(0.3)
                continue

            for label, opt_key, opt_type in otm_list:
                try:
                    df_opt = fetch_upstox_candles(opt_key, "1h")
                    if df_opt is None or len(df_opt) < 25:
                        time.sleep(0.2)
                        continue

                    # 1. Super Flat check
                    sf_result = detect_super_flat(df_opt)
                    if sf_result:
                        super_flat_found.append({
                            "symbol":       symbol,
                            "label":        label,
                            "opt_type":     opt_type,
                            "price":        sf_result["price"],
                            "ubb":          sf_result["UBB"],
                            "lbb":          sf_result["LBB"],
                            "pp":           sf_result["SMA20"],
                            "bw":           sf_result["bb_width_pct"],
                        })
                        logger.info(f"[📋 SF] ⭐ {symbol} {label}")
                        time.sleep(0.3)
                        continue

                    # 2. Near-breakout check — BB squeeze + price UBB ke paas
                    df_ind = calculate_indicators(df_opt, "1h").dropna().reset_index(drop=True)
                    if len(df_ind) < 5:
                        time.sleep(0.2)
                        continue

                    last     = df_ind.iloc[-1]
                    bb_upper = float(last["bb_upper"])
                    bb_lower = float(last["bb_lower"])
                    close    = float(last["close"])

                    # Price upper band ke 10% ke andar?
                    if bb_upper > 0:
                        gap_pct = (bb_upper - close) / bb_upper * 100
                        near_ubb = -3 < gap_pct < 10  # 10% neeche se 3% upar tak
                    else:
                        near_ubb = False

                    # Squeeze bhi hona chahiye
                    avg_bw  = df_ind["bb_width"].iloc[-21:-1].mean() if len(df_ind) >= 21 else 0
                    squeeze = float(last["bb_width"]) < avg_bw * PARAMS.SQUEEZE_MULT if avg_bw > 0 else False

                    if squeeze and near_ubb:
                        near_breakout_found.append({
                            "symbol":   symbol,
                            "label":    label,
                            "opt_type": opt_type,
                            "price":    round(close, 4),
                            "ubb":      round(bb_upper, 4),
                            "lbb":      round(bb_lower, 4),
                            "gap_pct":  round(gap_pct, 2),
                        })
                        logger.info(f"[📋 NB] 🎯 {symbol} {label} gap={gap_pct:.1f}%")

                    time.sleep(0.2)
                except Exception as e:
                    logger.error(f"[📋 PM] {symbol} {label}: {e}")

            time.sleep(0.3)
        except Exception as e:
            logger.error(f"[📋 PM] {symbol}: {e}")

    elapsed = round(time.time() - start_time, 1)

    # ── Send Watchlist Report ─────────────────────────────────────
    report = f"📋 <b>TOMORROW'S WATCHLIST</b> 🎯\n"
    report += f"<i>Scan complete — {elapsed}s</i>\n\n"

    if super_flat_found:
        report += f"🌟 <b>SUPER FLAT Setups ({len(super_flat_found)})</b>\n"
        report += "<i>Breakout ready — top priority!</i>\n\n"
        for i, s in enumerate(super_flat_found[:10], 1):  # max 10
            report += (
                f"{i}. <b>{s['symbol']} {s['label']}</b> ({s['opt_type']})\n"
                f"   Price=₹{s['price']} | UBB=₹{s['ubb']} | BW={s['bw']}%\n"
            )
        if len(super_flat_found) > 10:
            report += f"   ...aur {len(super_flat_found)-10} more\n"
        report += "\n"
    else:
        report += "🌟 Super Flat: <i>Koi setup nahi mila</i>\n\n"

    if near_breakout_found:
        # Sort by gap — closest to UBB first
        near_breakout_found.sort(key=lambda x: abs(x["gap_pct"]))
        report += f"🎯 <b>NEAR BREAKOUT Setups ({len(near_breakout_found)})</b>\n"
        report += "<i>UBB ke paas — closely monitor karo</i>\n\n"
        for i, s in enumerate(near_breakout_found[:10], 1):
            report += (
                f"{i}. <b>{s['symbol']} {s['label']}</b> ({s['opt_type']})\n"
                f"   Price=₹{s['price']} | UBB=₹{s['ubb']} | Gap={s['gap_pct']}%\n"
            )
        if len(near_breakout_found) > 10:
            report += f"   ...aur {len(near_breakout_found)-10} more\n"
        report += "\n"
    else:
        report += "🎯 Near Breakout: <i>Koi nahi mila</i>\n\n"

    report += f"<b>Action:</b> Kal in stocks ko manually monitor karo\n"
    report += f"Breakout ke baad Money Printer alert automatically aayega!\n\n"
    report += f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
    report += "\n#TPS #Watchlist #PostMarket"

    send_telegram(report)

    logger.info(f"[📋 Post-Market] Done! SF={len(super_flat_found)} NB={len(near_breakout_found)} ({elapsed}s)")

# ═══════════════════════════════════════════════════════════════════
# ON-DEMAND SCAN FUNCTIONS (Telegram Commands)
# ═══════════════════════════════════════════════════════════════════
def _run_s6_on_demand():
    """Manual /scan_s6 command handler"""
    try:
        start = time.time()
        run_delta_scanner()
        if UPSTOX_ACCESS_TOKEN:
            run_upstox_scanner()
        elapsed = round(time.time() - start, 1)
        send_telegram(
            f"✅ <b>S6 Scan Complete!</b>\n"
            f"⏱ Time: {elapsed}s\n"
            f"🕐 {datetime.now(IST).strftime('%H:%M IST')}"
        )
    except Exception as e:
        send_telegram(f"❌ S6 Scan Error: {e}")

def _run_mp_on_demand():
    """Manual /scan_mp command handler"""
    try:
        if not UPSTOX_ACCESS_TOKEN:
            send_telegram("❌ Upstox token missing! /token bhejo pehle.")
            return
        start = time.time()
        # Use cached active stocks, ya fresh lo
        active = _active_stocks_cache if _active_stocks_cache else fetch_active_nifty100_stocks(top_n=100)
        run_money_printer_scanner(active)
        elapsed = round(time.time() - start, 1)
        send_telegram(
            f"✅ <b>Money Printer Scan Complete!</b>\n"
            f"📊 Scanned: {len(active)} stocks × 4 OTM options\n"
            f"⏱ Time: {elapsed}s\n"
            f"🕐 {datetime.now(IST).strftime('%H:%M IST')}"
        )
    except Exception as e:
        send_telegram(f"❌ MP Scan Error: {e}")

def _run_superflat_on_demand():
    """Manual /scan_superflat — sirf Super Flat detect kare (Money Printer skip)"""
    try:
        if not UPSTOX_ACCESS_TOKEN:
            send_telegram("❌ Upstox token missing! /token bhejo pehle.")
            return

        start = time.time()
        active = _active_stocks_cache if _active_stocks_cache else fetch_active_nifty100_stocks(top_n=100)
        found = 0

        for (symbol, ikey, opt_base, opt_exch, gap) in active:
            try:
                spot_key = ikey.replace("NSE_FO", "NSE_EQ")
                df_ltp   = fetch_upstox_candles(spot_key, "1h")
                if df_ltp is None or len(df_ltp) < 5:
                    time.sleep(0.3)
                    continue

                ltp      = float(df_ltp.iloc[-1]["close"])
                otm_list = get_nearest_expiry_options(opt_base, opt_exch, ltp, gap)
                if not otm_list:
                    time.sleep(0.3)
                    continue

                for label, opt_key, opt_type in otm_list:
                    try:
                        df_opt    = fetch_upstox_candles(opt_key, "1h")
                        sf_result = detect_super_flat(df_opt)
                        if sf_result:
                            msg = format_super_flat_alert(symbol, label, opt_type, sf_result)
                            send_telegram(msg)
                            found += 1
                            time.sleep(1)
                        time.sleep(0.3)
                    except Exception as e:
                        logger.error(f"[/scan_superflat] {symbol} {label}: {e}")
                time.sleep(0.4)
            except Exception as e:
                logger.error(f"[/scan_superflat] {symbol}: {e}")

        elapsed = round(time.time() - start, 1)
        send_telegram(
            f"✅ <b>Super Flat Scan Complete!</b>\n"
            f"🌟 Setups Found: {found}\n"
            f"📊 Scanned: {len(active)} stocks × 4 OTM options\n"
            f"⏱ Time: {elapsed}s\n"
            f"🕐 {datetime.now(IST).strftime('%H:%M IST')}"
        )
    except Exception as e:
        send_telegram(f"❌ SuperFlat Scan Error: {e}")

def telegram_bot():
    global scanner_paused, UPSTOX_ACCESS_TOKEN
    offset = None
    logger.info("[BOT] Telegram bot started.")

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            resp   = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params, timeout=35)
            data   = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip()
                chat   = str(msg.get("chat", {}).get("id", ""))

                if chat != str(TELEGRAM_CHAT_ID):
                    continue

                if text.startswith("/token "):
                    code = text.split("/token ", 1)[1].strip()
                    send_telegram("⏳ Token exchange ho raha hai...")
                    new_token = exchange_upstox_token(code)
                    if new_token:
                        railway_ok = update_railway_token(new_token)
                        send_telegram(
                            f"✅ <b>Upstox Token Updated!</b>\n\n"
                            f"{'✅ Railway bhi update!' if railway_ok else '⚠️ Railway manual karo!'}\n"
                            f"📅 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}\n"
                            f"🕐 Kal subah expire hoga"
                        )
                    else:
                        send_telegram("❌ Token fail! Code expire hua hoga.\nDobara /token &lt;fresh_code&gt; bhejo.")

                elif text == "/status":
                    tok = "✅ Set" if UPSTOX_ACCESS_TOKEN else "❌ Missing!"
                    psd = "⏸ Paused" if scanner_paused else "▶️ Running"
                    send_telegram(
                        f"📊 <b>TPS Unified Scanner v2.0</b>\n\n"
                        f"🤖 Status: {psd}\n"
                        f"🌐 BTC/ETH (Delta): ✅ Active\n"
                        f"🇮🇳 Indian Markets: {tok}\n"
                        f"📈 Options: 2 OTM CE+PE (current expiry)\n"
                        f"⏱ Scan: har 5 min\n"
                        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}\n\n"
                        f"<b>Commands:</b>\n"
                        f"/token &lt;code&gt; — Token update\n"
                        f"/status — Yeh message\n"
                        f"/pause · /resume\n"
                        f"/scan_s6 — S6 manual scan\n"
                        f"/scan_mp — Money Printer scan\n"
                        f"/scan_superflat — Super Flat scan\n"
                        f"/help — All commands"
                    )

                elif text == "/pause":
                    scanner_paused = True
                    send_telegram("⏸ <b>Scanner Paused!</b>\n/resume se chalu karo.")

                elif text == "/resume":
                    scanner_paused = False
                    send_telegram("▶️ <b>Scanner Resumed!</b>")

                # ── CUSTOM SCAN COMMANDS ─────────────────────────────
                elif text == "/scan_s6":
                    send_telegram("⏳ <b>S6 Scan Start</b> — saare instruments scan kar raha hoon...")
                    try:
                        Thread(target=_run_s6_on_demand, daemon=True).start()
                    except Exception as e:
                        send_telegram(f"❌ Error: {e}")

                elif text == "/scan_mp":
                    send_telegram("⏳ <b>Money Printer Scan Start</b> — active stocks ke options scan ho rahe hain...")
                    try:
                        Thread(target=_run_mp_on_demand, daemon=True).start()
                    except Exception as e:
                        send_telegram(f"❌ Error: {e}")

                elif text == "/scan_superflat":
                    send_telegram("⏳ <b>Super Flat Scan Start</b> — pre-breakout setups dhundh raha hoon...")
                    try:
                        Thread(target=_run_superflat_on_demand, daemon=True).start()
                    except Exception as e:
                        send_telegram(f"❌ Error: {e}")

                elif text == "/watchlist":
                    send_telegram("⏳ <b>Watchlist Scan Start</b> — kal ke liye setups check kar raha hoon...")
                    try:
                        Thread(target=run_post_market_watchlist, daemon=True).start()
                    except Exception as e:
                        send_telegram(f"❌ Error: {e}")

                elif text == "/help" or text == "/commands":
                    send_telegram(
                        "📖 <b>TPS Scanner Commands:</b>\n\n"
                        "<b>Token:</b>\n"
                        "/token &lt;code&gt; — Daily Upstox token update\n\n"
                        "<b>Scanner Control:</b>\n"
                        "/status — Scanner status\n"
                        "/pause · /resume — Pause/resume\n\n"
                        "<b>On-Demand Scans:</b>\n"
                        "/scan_s6 — S6 setup turant check\n"
                        "/scan_mp — Money Printer check\n"
                        "/scan_superflat — Super Flat warnings\n"
                        "/watchlist — Kal ke liye watchlist\n\n"
                        "<b>Auto Schedule:</b>\n"
                        "📋 Daily 5:00 PM — Post-market watchlist\n\n"
                        "<b>Info:</b>\n"
                        "/help — Yeh message"
                    )

        except Exception as e:
            logger.error(f"[BOT] Error: {e}")
            time.sleep(5)



# ═══════════════════════════════════════════════════════════════════
# 🌟 SUPER FLAT DETECTOR — Pre-Breakout Setup
# ═══════════════════════════════════════════════════════════════════
# DIVISLAB jaisa setup — jab bands tight + sab lines chipki ho
# Yeh breakout se PEHLE ka warning alert hai (1H option chart only)
# ═══════════════════════════════════════════════════════════════════

def detect_super_flat(df_opt: pd.DataFrame) -> dict | None:
    """
    Super Flat detect karo — Breakout se pehle ka setup
    Conditions (1H option chart):
    1. UBB, LBB, SMA20, VWAP — sab super flat (<0.1%)
    2. BB bahut tight — (UBB-LBB)/price < 3%
    3. Lines paas paas — UBB-SMA aur SMA-LBB < 1.5%
    4. VWAP chipka hua SMA20 ke — <0.5% difference
    """
    if df_opt is None or len(df_opt) < PARAMS.BB_PERIOD + PARAMS.FLAT_CANDLES + 5:
        return None

    df = calculate_indicators(df_opt, "1h").dropna().reset_index(drop=True)
    if len(df) < PARAMS.BB_PERIOD:
        return None

    last = df.iloc[-1]
    # Flat check pichhle 5-6 candles pe
    check_candles = df.iloc[-(PARAMS.FLAT_CANDLES + 2):]

    def pct_range(s):
        m = s.mean()
        return 0 if m == 0 else (s.max() - s.min()) / m

    # ── 1. Sab lines super flat ───────────────────────────────────
    ubb_flat  = pct_range(check_candles["bb_upper"]) < PARAMS.SUPER_FLAT_TH
    lbb_flat  = pct_range(check_candles["bb_lower"]) < PARAMS.SUPER_FLAT_TH
    sma_flat  = pct_range(check_candles["sma20"])    < PARAMS.SUPER_FLAT_TH
    vwap_flat = pct_range(check_candles["vwap"])     < PARAMS.SUPER_FLAT_TH if "vwap" in check_candles.columns else False

    all_super_flat = ubb_flat and lbb_flat and sma_flat and vwap_flat

    # ── 2. BB tight — bandwidth small ─────────────────────────────
    ubb   = float(last["bb_upper"])
    lbb   = float(last["bb_lower"])
    sma   = float(last["sma20"])
    vwap  = float(last["vwap"]) if "vwap" in last.index else sma
    price = float(last["close"])

    bb_tight = (ubb - lbb) / price < PARAMS.BB_TIGHT_TH if price > 0 else False

    # ── 3. Lines paas paas ────────────────────────────────────────
    ubb_sma_close = abs(ubb - sma) / sma < PARAMS.LINES_CLOSE_TH if sma > 0 else False
    sma_lbb_close = abs(sma - lbb) / sma < PARAMS.LINES_CLOSE_TH if sma > 0 else False
    lines_close   = ubb_sma_close and sma_lbb_close

    # ── 4. VWAP chipka hua SMA ke ─────────────────────────────────
    vwap_sma_close = abs(vwap - sma) / sma < PARAMS.VWAP_CLOSE_TH if sma > 0 else False

    # Sab conditions pass hone chahiye
    if not (all_super_flat and bb_tight and lines_close and vwap_sma_close):
        return None

    # HM status bhi lo (informational)
    hm = get_hm_signal(df)

    return {
        "UBB":           round(ubb, 4),
        "LBB":           round(lbb, 4),
        "SMA20":         round(sma, 4),
        "VWAP":          round(vwap, 4),
        "price":         round(price, 4),
        "bb_width_pct":  round((ubb - lbb) / price * 100, 3),
        "ubb_lbb_gap":   round(abs(ubb - sma) / sma * 100, 3),
        "vwap_sma_gap":  round(abs(vwap - sma) / sma * 100, 3),
        "candles":       PARAMS.FLAT_CANDLES + 2,
        "hm_signal":     hm.get("signal"),
        "hm_rsi9":       hm.get("rsi9"),
        "hm_blue":       hm.get("hm_blue"),
        "hm_red":        hm.get("hm_red"),
        "timestamp":     last["timestamp"].strftime("%H:%M") if hasattr(last["timestamp"], "strftime") else str(last["timestamp"]),
    }

def format_super_flat_alert(symbol: str, strike_label: str, opt_type: str, sig: dict) -> str:
    """Super Flat alert — breakout se pehle ka warning"""
    msg  = f"\U0001f31f\U0001f31f <b>SUPER FLAT DETECTED!</b> \U0001f3af\n"
    msg += f"<i>Breakout se pehle ka setup — ready karo!</i>\n\n"
    msg += f"\U0001f4cc <b>{symbol} {strike_label}</b> ({opt_type})\n"
    msg += f"\u23f0 Timeframe: <b>1H Option Chart</b>\n\n"

    msg += f"\U0001f4ca <b>Chipki Hui Lines:</b>\n"
    msg += f"   UBB   = \u20b9{sig['UBB']}\n"
    msg += f"   SMA20 = \u20b9{sig['SMA20']}\n"
    msg += f"   VWAP  = \u20b9{sig['VWAP']}\n"
    msg += f"   LBB   = \u20b9{sig['LBB']}\n"
    msg += f"   Price = \u20b9{sig['price']}\n\n"

    msg += f"\U0001f50d <b>Tightness Metrics:</b>\n"
    msg += f"   BB Width = {sig['bb_width_pct']}% (tight!)\n"
    msg += f"   UBB\u2194SMA Gap = {sig['ubb_lbb_gap']}%\n"
    msg += f"   VWAP\u2194SMA Gap = {sig['vwap_sma_gap']}% (chipka!)\n"
    msg += f"   Flat Candles = {sig['candles']}\n\n"

    if sig.get("hm_rsi9"):
        msg += f"\U0001f3af <b>HM Status:</b> {sig['hm_signal']}\n"
        msg += f"   RSI9={sig['hm_rsi9']} | Blue={sig['hm_blue']} | Red={sig['hm_red']}\n\n"

    msg += f"\u26a1 <b>Kya Karo:</b>\n"
    msg += f"   \u2022 Watchlist mein daalo\n"
    msg += f"   \u2022 Breakout ka wait karo (UBB + R2 cross)\n"
    msg += f"   \u2022 Money Printer alert ayega blast pe\n"
    msg += f"   \u2022 Jaldi entry na lo \u2014 false breakout possible\n\n"

    msg += f"\U0001f552 {sig['timestamp']} IST\n"
    msg += f"\n#TPS #SuperFlat #{symbol} #{opt_type}"
    return msg.strip()

# ═══════════════════════════════════════════════════════════════════
# 💰 MONEY PRINTER DETECTOR
# ═══════════════════════════════════════════════════════════════════
# Setup conditions (Hourly only):
# 1. Option BB Squeeze + Flat (OTM premium sasta)
# 2. PP (Pivot Point) last 1-2 candles se BB bands ke ANDAR
# 3. Weekly R3 breakout — bull mein close > R3, bear mein close < S3
# 4. Volume spike confirm kare
# ═══════════════════════════════════════════════════════════════════

def calculate_pivot_levels(df: pd.DataFrame, period: str = "daily") -> dict:
    """
    Pivot levels calculate karo.
    period: 'daily' (prev daily H+L+C) ya 'weekly' (prev weekly H+L+C)
    Returns: PP, R1, R2, R3, S1, S2, S3
    """
    if df is None or len(df) < 2:
        return {"PP":0,"R1":0,"R2":0,"R3":0,"S1":0,"S2":0,"S3":0}

    if period == "weekly":
        # Previous week ka H+L+C nikalo
        # Assume df has hourly candles — group by week
        df_copy = df.copy()
        if "timestamp" in df_copy.columns:
            df_copy["timestamp"] = pd.to_datetime(df_copy["timestamp"])
            df_copy["week"] = df_copy["timestamp"].dt.isocalendar().week
            df_copy["year"] = df_copy["timestamp"].dt.isocalendar().year
            grouped = df_copy.groupby(["year", "week"]).agg(
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last")
            ).reset_index()
            # Previous completed week (second last)
            if len(grouped) >= 2:
                prev = grouped.iloc[-2]
                H, L, C = float(prev["high"]), float(prev["low"]), float(prev["close"])
            else:
                return {"PP":0,"R1":0,"R2":0,"R3":0,"S1":0,"S2":0,"S3":0}
        else:
            # Fallback — last 5 candles
            recent = df.iloc[-5:]
            H = float(recent["high"].max())
            L = float(recent["low"].min())
            C = float(recent.iloc[-1]["close"])
    else:
        # Daily — previous candle
        prev = df.iloc[-2]
        H = float(prev["high"])
        L = float(prev["low"])
        C = float(prev["close"])

    PP = (H + L + C) / 3
    R1 = (2 * PP) - L
    R2 = PP + (H - L)
    R3 = H + 2 * (PP - L)
    S1 = (2 * PP) - H
    S2 = PP - (H - L)
    S3 = L - 2 * (H - PP)

    return {"PP": PP, "R1": R1, "R2": R2, "R3": R3,
            "S1": S1, "S2": S2, "S3": S3}

def detect_money_printer(
    symbol: str,
    option_key: str,
    option_type: str,
    strike_label: str = ""
) -> dict | None:
    """
    Money Printer — Pure Option Chart Detection (1H only)

    MANDATORY:
    1. BB Squeeze + Flat (4+ candles)
    2. PP inside bands
    3. Blast candle: close > UBB AND close > R2
    4. Volume spike
    5. HM confirmation

    BONUS: R3 break = POWERFUL (super signal)
    NO SPOT DATA — sirf option chart!
    """
    df_opt = fetch_upstox_candles(option_key, "1h")
    if df_opt is None or len(df_opt) < 25:
        return None

    df = calculate_indicators(df_opt, "1h").dropna().reset_index(drop=True)
    if len(df) < 10:
        return None

    last      = df.iloc[-1]   # BLAST candle
    pre       = df.iloc[-2]   # Pre-blast
    prev_flat = df.iloc[-(PARAMS.FLAT_CANDLES + 1):-1]

    def pct_range(s):
        m = s.mean()
        return 0 if m == 0 else (s.max() - s.min()) / m

    close    = float(last["close"])
    bb_upper = float(pre["bb_upper"])
    bb_lower = float(pre["bb_lower"])
    bb_mid   = float(pre["sma20"])

    # ── 1. Squeeze (pre-blast) ────────────────────────────────────
    avg_bw  = df["bb_width"].iloc[-(PARAMS.VOL_PERIOD + 2):-1].mean()
    squeeze = float(pre["bb_width"]) < avg_bw * PARAMS.SQUEEZE_MULT

    # ── 2. Flat Bands (candles -5 to -1) ──────────────────────────
    flat_bb = (pct_range(prev_flat["sma20"])    < PARAMS.FLAT_THRESHOLD and
               pct_range(prev_flat["bb_upper"]) < PARAMS.FLAT_THRESHOLD and
               pct_range(prev_flat["bb_lower"]) < PARAMS.FLAT_THRESHOLD)

    # ── 3. Weekly Pivots on OPTION chart ──────────────────────────
    opt_pivots = calculate_pivot_levels(df_opt, period="weekly")
    PP = opt_pivots["PP"]
    R1 = opt_pivots["R1"]
    R2 = opt_pivots["R2"]
    R3 = opt_pivots["R3"]

    # ── PP Check — Last 2-3 din PP inside bands hona chahiye ──
    # Har din kam se kam 2 candles inside = valid
    # Agar sab candles inside = perfect (bonus +5)
    pp_inside_last = bb_lower <= PP <= bb_upper  # Current candle check

    # Last 3 din ka data — 1H candles, so ~18 candles (6/day × 3)
    lookback = min(18, len(df) - 1)
    recent   = df.iloc[-(lookback+1):-1]  # blast candle exclude

    pp_inside_count = ((recent["bb_lower"] <= PP) & (PP <= recent["bb_upper"])).sum()
    pp_total        = len(recent)

    # Valid: har day kam se kam 2 candles inside (avg ~6/day)
    # Simple check: >60% candles inside
    pp_valid   = pp_inside_count >= (pp_total * 0.6) if pp_total > 0 else False
    # Perfect: >95% candles inside = bonus
    pp_perfect = pp_inside_count >= (pp_total * 0.95) if pp_total > 0 else False

    pp_inside = pp_inside_last and pp_valid

    # ── 4. Blast Candle ───────────────────────────────────────────
    avg_vol   = df["volume"].iloc[-(PARAMS.VOL_PERIOD + 2):-1].mean()
    vol_spike = float(last["volume"]) > avg_vol * PARAMS.VOL_MULT

    # UBB break — MANDATORY
    ubb_break = close > bb_upper

    # R2 break — MANDATORY (minimum)
    r2_break = close > R2 if R2 > 0 else False

    # R3 break — BONUS (super powerful)
    r3_break = close > R3 if R3 > 0 else False

    # ── Bald Candle Check (optional — bonus marks ke liye) ────────
    candle_open  = float(last["open"])
    candle_high  = float(last["high"])
    candle_low   = float(last["low"])
    candle_range = candle_high - candle_low
    candle_body  = abs(float(last["close"]) - candle_open)
    bald_ratio   = candle_body / candle_range if candle_range > 0 else 0
    is_bald      = bald_ratio >= PARAMS.BALD_MIN_RATIO

    # Final blast = UBB break + R2 break (MANDATORY)
    # Bald candle aur volume ab bonus mein — signal trigger ke liye nahi
    blast = ubb_break and r2_break

    if not (squeeze and flat_bb and pp_inside and blast):
        return None

    # ── 5. HM Signal ──────────────────────────────────────────────
    hm = get_hm_signal(df)

    # Super signal agar R3 cross + HM confirmed
    super_signal = r3_break and hm["confirmed"]

    # VWAP for SL
    vwap_val = round(float(last["vwap"]), 4) if "vwap" in last.index else None

    return {
        "symbol":        symbol,
        "option_type":   option_type,
        "strike_label":  strike_label,
        "direction":     "BULLISH \U0001f7e2",  # Option buying is always directional
        "close":         round(close, 4),
        "opt_UBB":       round(bb_upper, 4),
        "opt_LBB":       round(bb_lower, 4),
        "opt_PP":        round(PP, 4),
        "opt_R2":        round(R2, 4),
        "opt_R3":        round(R3, 4),
        "opt_bw":        round(float(pre["bb_width"]) * 100, 3),
        "squeeze":       squeeze,
        "flat_bb":       flat_bb,
        "pp_inside":     pp_inside,
        "ubb_break":     ubb_break,
        "r2_break":      r2_break,
        "r3_break":      r3_break,
        "super_signal":  super_signal,
        "pp_perfect":    pp_perfect,
        "pp_inside_pct": round(pp_inside_count / pp_total * 100, 1) if pp_total > 0 else 0,
        "bald_ratio":    round(bald_ratio * 100, 1),
        "is_bald":       is_bald,
        "vol_spike":     vol_spike,
        "volume":        int(last["volume"]),
        "avg_vol":       int(avg_vol),
        "vwap":          vwap_val,
        "hm_signal":     hm["signal"],
        "hm_confirm":    hm["confirmed"],
        "hm_rsi9":       hm["rsi9"],
        "hm_blue":       hm["hm_blue"],
        "hm_red":        hm["hm_red"],
        "timestamp":     last["timestamp"].strftime("%H:%M") if hasattr(last["timestamp"], "strftime") else str(last["timestamp"]),
    }

def format_money_printer_alert(sig: dict) -> str:
    """Money Printer — Pure option chart alert"""
    opt_type      = sig["option_type"]
    opt_price     = sig["close"]
    strike_label  = sig.get("strike_label", "")
    vwap_val      = sig.get("vwap")
    sl_vwap       = f"\u20b9{vwap_val}" if vwap_val else "VWAP"
    target_2x     = round(opt_price * 2, 2)
    super_signal  = sig.get("super_signal", False)
    r3_break      = sig.get("r3_break", False)

    # Tick marks only — no numeric score
    checks = []
    bonus_items = []

    # Core conditions
    checks.append(("BB Squeeze", sig["squeeze"]))
    checks.append(("Flat Bands (4+ candles)", sig["flat_bb"]))
    checks.append((f"PP Inside Bands ({sig.get('pp_inside_pct',0)}% candles)", sig["pp_inside"]))
    checks.append((f"R2 Crossed (\u20b9{sig['opt_R2']})", sig.get("r2_break", False)))
    checks.append((f"HM {sig.get('hm_signal','')} Confirmed", sig["hm_confirm"]))

    # Bonus items (strong signals)
    if sig.get("r3_break"):
        bonus_items.append(f"\U0001f680 <b>R3 CROSSED!</b> (\u20b9{sig['opt_R3']}) \u2014 SUPER signal!")
    if sig.get("is_bald"):
        bonus_items.append(f"\u26a1 Bald Candle ({sig.get('bald_ratio',0)}% body)")
    if sig.get("vol_spike"):
        bonus_items.append(f"\U0001f4c8 Volume Spike ({sig['volume']:,})")
    if sig.get("pp_perfect"):
        bonus_items.append(f"\u2b50 PP Perfect \u2014 all candles inside!")

    # Bonus: R3 break adds super flag
    r3_badge = " \U0001f680 R3 CROSSED!" if r3_break else ""



    # Header — super signal ya normal
    if super_signal:
        header = "\U0001f4b0\U0001f4b0 <b>SUPER MONEY PRINTER!</b> \U0001f680"
    else:
        header = "\U0001f4b0\U0001f4b0 <b>MONEY PRINTER DETECTED!</b>"

    msg  = f"{header}\n\n"
    msg += f"\U0001f4cc <b>{sig['symbol']} {strike_label}</b>\n\n"

    # Buy line
    msg += f"\u26a1 Buy <b>{sig['symbol']} {strike_label or opt_type}</b>\n"
    msg += f"   @ CMP <b>\u20b9{opt_price}</b>\n\n"

    # Option chart data
    msg += f"\U0001f4ca <b>Option Chart (1H):</b>\n"
    msg += f"   UBB = \u20b9{sig['opt_UBB']} \u2705 Crossed\n"
    msg += f"   R2  = \u20b9{sig['opt_R2']} \u2705 Crossed\n"
    if r3_break:
        msg += f"   R3  = \u20b9{sig['opt_R3']} \U0001f680 Crossed! (BONUS)\n"
    else:
        msg += f"   R3  = \u20b9{sig['opt_R3']} (not yet)\n"
    msg += f"   PP  = \u20b9{sig['opt_PP']}\n"
    msg += f"   LBB = \u20b9{sig['opt_LBB']}\n\n"

    # HM
    if sig.get("hm_rsi9") is not None:
        hm_status = "\u2705 Confirmed" if sig['hm_confirm'] else "\u26a0\ufe0f Weak"
        msg += f"\U0001f3af <b>HM:</b> {sig['hm_signal']} {hm_status}\n"
        msg += f"   RSI9={sig['hm_rsi9']} | Blue={sig['hm_blue']} | Red={sig['hm_red']}\n\n"

    # SL
    msg += f"\U0001f6d1 <b>SL</b> \u2014 Close below VWAP ({sl_vwap})\n\n"

    # Target
    msg += f"\U0001f3af <b>Target:</b>\n"
    msg += f"   \u2022 50% book @ <b>\u20b9{target_2x}</b> (2x of \u20b9{opt_price})\n"
    msg += f"   \u2022 Baki 50% SL shift \u2192 entry price\n"
    msg += f"   \u2022 Trail karo \u2014 VWAP ke neeche close = exit\n\n"

    # Checklist — tick marks only
    msg += f"\U0001f4cb <b>Setup Checklist:</b>\n"
    for label, passed in checks:
        icon = "\u2705" if passed else "\u274c"
        msg += f"   {icon} {label}\n"

    # Bonus strong signals
    if bonus_items:
        msg += f"\n\u2b50 <b>Bonus Strong Signals:</b>\n"
        for item in bonus_items:
            msg += f"   {item}\n"

    tags = f"#TPS #MoneyPrinter #{sig['symbol']} #{opt_type}"
    if sig.get("super_signal") or sig.get("r3_break"):
        tags += " #Super"
    msg += f"\n{tags}"
    return msg.strip()

def run_money_printer_scanner(active_stocks: list):
    """
    Money Printer — 1H only — Pure option chart scan
    Har stock ke 2 OTM CE + 2 OTM PE pe detect karo
    """
    logger.info("[\U0001f4b0 MONEY PRINTER] Scan start \u2014 Option charts only (1H)...")
    alerts = 0

    for (symbol, ikey, opt_base, opt_exch, gap) in active_stocks:
        try:
            # Spot se LTP lo (sirf OTM strikes nikalne ke liye)
            spot_key = ikey.replace("NSE_FO", "NSE_EQ")
            df_ltp   = fetch_upstox_candles(spot_key, "1h")
            if df_ltp is None or len(df_ltp) < 5:
                time.sleep(0.3)
                continue

            ltp      = float(df_ltp.iloc[-1]["close"])
            otm_list = get_nearest_expiry_options(opt_base, opt_exch, ltp, gap)

            if not otm_list:
                time.sleep(0.3)
                continue

            # Har CE aur PE chart pe detect karo
            for label, opt_key, opt_type in otm_list:
                try:
                    strike_label = f"{label}"

                    # 1. Money Printer detect (breakout)
                    mp_result = detect_money_printer(
                        symbol=symbol,
                        option_key=opt_key,
                        option_type=opt_type,
                        strike_label=strike_label
                    )

                    if mp_result:
                        msg = format_money_printer_alert(mp_result)
                        send_telegram(msg)
                        alerts += 1
                        super_tag = " (SUPER)" if mp_result.get("super_signal") else ""
                        logger.info(f"[\U0001f4b0 MP] \u2705 {symbol} {label}{super_tag}")
                        time.sleep(1)
                    else:
                        # 2. Super Flat detect (pre-breakout warning)
                        df_opt = fetch_upstox_candles(opt_key, "1h")
                        sf_result = detect_super_flat(df_opt)
                        if sf_result:
                            msg = format_super_flat_alert(symbol, strike_label, opt_type, sf_result)
                            send_telegram(msg)
                            alerts += 1
                            logger.info(f"[\U0001f31f SUPER FLAT] \u2705 {symbol} {label}")
                            time.sleep(1)
                        else:
                            logger.info(f"[\U0001f4b0 MP] {symbol} {label}: No signal")

                    time.sleep(0.3)
                except Exception as e:
                    logger.error(f"[\U0001f4b0 MP] Error {symbol} {label}: {e}")

            time.sleep(0.4)

        except Exception as e:
            logger.error(f"[\U0001f4b0 MONEY PRINTER] Error {symbol}: {e}")
            time.sleep(0.3)

    logger.info(f"[\U0001f4b0 MONEY PRINTER] Cycle done. Alerts: {alerts}")

def scheduler():
    while True:
        if scanner_paused:
            time.sleep(60)
            continue

        now     = datetime.now(IST)
        hour    = now.hour
        minute  = now.minute
        weekday = now.weekday()

        is_weekday  = weekday < 5
        market_open = (hour == 9 and minute >= 15) or (9 < hour < 15) or (hour == 15 and minute <= 30)
        mcx_open    = (9 <= hour < 23) or (hour == 23 and minute <= 30)

        try:
            run_delta_scanner()

            if is_weekday and (market_open or mcx_open):
                run_upstox_scanner()

                # Money Printer — 1H only — same active stocks use karo
                if _active_stocks_cache:
                    run_money_printer_scanner(_active_stocks_cache)
            else:
                logger.info(f"[UPSTOX] Market closed. {now.strftime('%H:%M IST')}")

            # ── Post-Market Watchlist Scan (5:00 PM Weekday) ──
            global _post_market_done_date
            today = now.date()
            is_post_market_time = (hour == PARAMS.POST_MARKET_HOUR and
                                    minute < 10)  # 5:00-5:09 window
            if (is_weekday and is_post_market_time and
                _post_market_done_date != today):
                logger.info("[SCHEDULER] 5 PM — Post-Market scan trigger")
                run_post_market_watchlist()
                _post_market_done_date = today

        except Exception as e:
            logger.error(f"[SCHEDULER] Error: {e}")

        time.sleep(PARAMS.SCAN_INTERVAL_SEC)

# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TPS UNIFIED SCANNER v2.0 — Starting...")
    logger.info(f"OTM Strikes: {PARAMS.OTM_STRIKES} | Flat Candles: {PARAMS.FLAT_CANDLES}")
    logger.info(f"Time: {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}")
    logger.info("=" * 60)

    send_telegram(
        "🚀 <b>TPS Unified Scanner v2.0 Started!</b>\n\n"
        "🌐 BTC + ETH (Delta Exchange)\n"
        "🇮🇳 Nifty · Sensex · Gold · SilverM · Crude · NatGas\n"
        "📈 + 2 OTM CE/PE (current expiry) — sabhi segments\n"
        "📊 Top 20 Active N100 Stocks (OI+Vol) + options\n"
        "⏰ TF: 5M · 15M · 30M · 1H\n\n"
        f"💰 Money Printer: 1H | Active N100 Stocks\n"
        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
    )

    Thread(target=telegram_bot, daemon=True).start()
    scheduler()
