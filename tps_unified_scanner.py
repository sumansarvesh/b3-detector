"""
TPS UNIFIED SCANNER v1.0
========================
B3 Flat BB Detector (BTC/ETH via Delta Exchange)
+
S6 Flat BB Jackpot (Indian Markets via Upstox)

Dono ek hi file mein — same parameters — ek jagah change karo sab pe apply hoga.

Telegram Commands:
  /token <code>  — Daily Upstox token update (phone se)
  /status        — Scanner status check
  /pause         — Scanner pause karo
  /resume        — Scanner resume karo
"""

import os
import time
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from threading import Thread
import pytz

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s - %(message)s'
)
logger = logging.getLogger("TPS")

IST = pytz.timezone('Asia/Kolkata')

# ═══════════════════════════════════════════════════════════════════
# UNIFIED PARAMETERS — YAHAN CHANGE KARO, DONO SCANNER PE APPLY HOGA
# ═══════════════════════════════════════════════════════════════════
class PARAMS:
    # Bollinger Band
    BB_PERIOD        = 20
    BB_STD           = 2

    # Squeeze: BW < avg_bw * SQUEEZE_MULT
    SQUEEZE_MULT     = 0.7

    # Flat: SMA20 + upper + lower teeno flat over N candles
    FLAT_CANDLES     = 4       # kitne candles flat hone chahiye (min 4)
    FLAT_THRESHOLD   = 0.002   # 0.2% change allowed

    # Blast candle volume spike
    VOL_MULT         = 1.5     # volume > 20avg * VOL_MULT
    VOL_PERIOD       = 20      # volume average period

    # Marubozu (S3/S4/S5 ke liye future use)
    MARUBOZU_WICK    = 0.10    # wick < 10% of body

    # RSI
    RSI_PERIOD       = 9

    # Scanner interval
    SCAN_INTERVAL_SEC = 300    # 5 minutes

    # Timeframes to scan
    TIMEFRAMES       = ["5m", "15m", "30m", "1h"]

    # TF Score (single)
    TF_SCORE = {
        "5m":  3,
        "15m": 5,
        "30m": 6,
        "1h":  8,
    }

    # TF Score (multi)
    TF_SCORE_MULTI = {
        frozenset(["5m", "15m"]):              6,
        frozenset(["15m", "30m"]):             7,
        frozenset(["30m", "1h"]):              9,
        frozenset(["5m", "15m", "30m", "1h"]): 10,
    }

    # Minimum TF score to send alert
    MIN_SCORE        = 3

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

# Global scanner state
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
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
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
            headers={
                "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "query": query,
                "variables": {
                    "input": {
                        "projectId": RAILWAY_PROJECT_ID,
                        "serviceId": RAILWAY_SERVICE_ID,
                        "environmentId": RAILWAY_ENVIRONMENT_ID,
                        "name": "UPSTOX_ACCESS_TOKEN",
                        "value": new_token
                    }
                }
            },
            timeout=30
        )
        result = resp.json()
        return "errors" not in result
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
            headers={
                "accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "code": code,
                "client_id": UPSTOX_API_KEY,
                "client_secret": UPSTOX_SECRET_KEY,
                "redirect_uri": UPSTOX_REDIRECT_URI,
                "grant_type": "authorization_code"
            },
            timeout=30
        )
        data = resp.json()
        token = data.get("access_token")
        if token:
            UPSTOX_ACCESS_TOKEN = token
        return token
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════
# BOLLINGER BAND CALCULATION (UNIFIED)
# ═══════════════════════════════════════════════════════════════════
def calculate_bb(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma20"]    = df["close"].rolling(PARAMS.BB_PERIOD).mean()
    df["std20"]    = df["close"].rolling(PARAMS.BB_PERIOD).std()
    df["bb_upper"] = df["sma20"] + PARAMS.BB_STD * df["std20"]
    df["bb_lower"] = df["sma20"] - PARAMS.BB_STD * df["std20"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["sma20"]
    return df

# ═══════════════════════════════════════════════════════════════════
# S6 DETECTION LOGIC (UNIFIED — SAME FOR B3 AND UPSTOX)
# ═══════════════════════════════════════════════════════════════════
def detect_s6(df: pd.DataFrame, tf: str) -> dict | None:
    """
    S6 Flat BB Jackpot Detection:
    1. BB Squeeze: BW < 20avg * 0.7
    2. Flat: SMA20 + upper + lower all flat (<0.2%) over 8 candles
    3. Blast: close outside BB + volume > 20avg * 1.5
    """
    if df is None or len(df) < PARAMS.BB_PERIOD + PARAMS.FLAT_CANDLES + 5:
        return None

    df = calculate_bb(df)
    df = df.dropna().reset_index(drop=True)

    if len(df) < PARAMS.BB_PERIOD:
        return None

    last  = df.iloc[-1]
    prev8 = df.iloc[-(PARAMS.FLAT_CANDLES + 1):-1]

    # ── 1. Squeeze Check ──────────────────────────────────────────
    avg_bw = df["bb_width"].iloc[-(PARAMS.VOL_PERIOD + 1):-1].mean()
    squeeze = last["bb_width"] < avg_bw * PARAMS.SQUEEZE_MULT

    # ── 2. Flat Bands Check ───────────────────────────────────────
    def pct_range(series):
        m = series.mean()
        if m == 0:
            return 0
        return (series.max() - series.min()) / m

    sma_flat   = pct_range(prev8["sma20"])   < PARAMS.FLAT_THRESHOLD
    upper_flat = pct_range(prev8["bb_upper"]) < PARAMS.FLAT_THRESHOLD
    lower_flat = pct_range(prev8["bb_lower"]) < PARAMS.FLAT_THRESHOLD
    flat = sma_flat and upper_flat and lower_flat

    # ── 3. Blast Candle Check ─────────────────────────────────────
    avg_vol   = df["volume"].iloc[-(PARAMS.VOL_PERIOD + 1):-1].mean()
    vol_spike = last["volume"] > avg_vol * PARAMS.VOL_MULT

    blast_up   = last["close"] > last["bb_upper"] and vol_spike
    blast_down = last["close"] < last["bb_lower"] and vol_spike

    if not (squeeze and flat and (blast_up or blast_down)):
        return None

    direction = "BULLISH 🟢" if blast_up else "BEARISH 🔴"

    return {
        "direction":  direction,
        "tf":         tf,
        "tf_score":   PARAMS.TF_SCORE.get(tf, 0),
        "close":      round(float(last["close"]), 4),
        "bb_upper":   round(float(last["bb_upper"]), 4),
        "bb_lower":   round(float(last["bb_lower"]), 4),
        "bb_width":   round(float(last["bb_width"]) * 100, 3),
        "volume":     int(last["volume"]),
        "avg_vol":    int(avg_vol),
        "timestamp":  last["timestamp"].strftime("%H:%M") if hasattr(last["timestamp"], "strftime") else str(last["timestamp"]),
    }

# ═══════════════════════════════════════════════════════════════════
# MULTI-TF SCORE
# ═══════════════════════════════════════════════════════════════════
def get_multi_tf_score(signals: dict) -> int:
    active = frozenset(signals.keys())
    for combo, score in sorted(PARAMS.TF_SCORE_MULTI.items(), key=lambda x: -x[1]):
        if combo.issubset(active):
            return score
    if active:
        return max(PARAMS.TF_SCORE.get(tf, 0) for tf in active)
    return 0

# ═══════════════════════════════════════════════════════════════════
# ALERT FORMAT
# ═══════════════════════════════════════════════════════════════════
def format_alert(symbol: str, signals: dict, score: int, source: str) -> str:
    direction = list(signals.values())[0]["direction"]
    tfs_str   = " + ".join([s.upper() for s in signals.keys()])
    close     = list(signals.values())[0]["close"]
    time_str  = list(signals.values())[0]["timestamp"]
    score_bar = "⭐" * min(score, 10)
    source_icon = "🌐" if source == "DELTA" else "🇮🇳"

    msg = f"""🔥 <b>TPS S6 — FLAT BB JACKPOT</b> {source_icon}

📌 <b>{symbol}</b>
{direction}
⏰ TF: <b>{tfs_str}</b>
💰 Price: <b>{close}</b>
🕐 {time_str} IST

📊 <b>Score: {score}/10</b>  {score_bar}"""

    for tf, sig in signals.items():
        msg += f"\n  {tf.upper()}: BW={sig['bb_width']}% | Vol={sig['volume']:,} (avg {sig['avg_vol']:,})"

    msg += f"\n\n⚡ <b>Options Buying — ATM Current Expiry</b>"
    msg += f"\n🛑 SL = Blast candle low (Bull) / high (Bear)"
    msg += f"\n\n#TPS #S6 #FlatBB #{symbol.replace(' ','')}"

    return msg.strip()

# ═══════════════════════════════════════════════════════════════════
# ─── DELTA EXCHANGE (B3 — BTC/ETH) ───────────────────────────────
# ═══════════════════════════════════════════════════════════════════
DELTA_SYMBOLS = ["BTCUSD", "ETHUSD"]

DELTA_TF_MAP = {
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
}

def fetch_delta_candles(symbol: str, tf: str) -> pd.DataFrame | None:
    resolution = DELTA_TF_MAP.get(tf, "5m")
    end_time   = int(time.time())
    start_time = end_time - (300 * PARAMS.BB_PERIOD * 3)  # enough candles

    try:
        url = f"https://api.india.delta.exchange/v2/history/candles"
        params = {
            "resolution": resolution,
            "symbol":     symbol,
            "start":      start_time,
            "end":        end_time
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        candles = data.get("result", [])
        if not candles or len(candles) < PARAMS.BB_PERIOD:
            return None

        df = pd.DataFrame(candles)
        df = df.rename(columns={
            "time": "timestamp",
            "open": "open",
            "high": "high",
            "low":  "low",
            "close":"close",
            "volume":"volume"
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    except Exception as e:
        logger.error(f"Delta candle error {symbol} {tf}: {e}")
        return None

def run_delta_scanner():
    """B3 Scanner — BTC/ETH via Delta Exchange"""
    logger.info("[DELTA] Scan cycle start...")
    alerts = 0

    for symbol in DELTA_SYMBOLS:
        signals = {}
        for tf in PARAMS.TIMEFRAMES:
            try:
                df     = fetch_delta_candles(symbol, tf)
                signal = detect_s6(df, tf)
                if signal:
                    signals[tf] = signal
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"[DELTA] Error {symbol} {tf}: {e}")

        if signals:
            score = get_multi_tf_score(signals)
            if score >= PARAMS.MIN_SCORE:
                msg = format_alert(symbol, signals, score, "DELTA")
                send_telegram(msg)
                alerts += 1
                logger.info(f"[DELTA] ✅ Alert: {symbol} Score={score}/10")
                time.sleep(1)
        else:
            logger.info(f"[DELTA] {symbol}: No signal")

    logger.info(f"[DELTA] Cycle done. Alerts: {alerts}")

# ═══════════════════════════════════════════════════════════════════
# ─── UPSTOX (S6 — Indian Markets) ────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

# Upstox instrument keys
UPSTOX_INSTRUMENTS = {
    "NIFTY 50":       "NSE_INDEX|Nifty 50",
    "SENSEX":         "BSE_INDEX|SENSEX",
    "NIFTY FUT":      "NSE_FO|NIFTY",
    "GOLD FUT":       "MCX_FO|GOLD",
    "SILVERM FUT":    "MCX_FO|SILVERM",
    "CRUDE OIL FUT":  "MCX_FO|CRUDEOIL",
    "NATURAL GAS FUT":"MCX_FO|NATURALGAS",
}

# Top 20 liquid Nifty 100 stocks (high OI)
TOP_STOCKS = {
    "RELIANCE":   "NSE_EQ|INE002A01018",
    "TCS":        "NSE_EQ|INE467B01029",
    "HDFCBANK":   "NSE_EQ|INE040A01034",
    "ICICIBANK":  "NSE_EQ|INE090A01021",
    "INFY":       "NSE_EQ|INE009A01021",
    "BAJFINANCE": "NSE_EQ|INE296A01024",
    "AXISBANK":   "NSE_EQ|INE238A01034",
    "KOTAKBANK":  "NSE_EQ|INE237A01028",
    "WIPRO":      "NSE_EQ|INE075A01022",
    "LT":         "NSE_EQ|INE018A01030",
    "ONGC":       "NSE_EQ|INE213A01029",
    "TITAN":      "NSE_EQ|INE280A01028",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "SUNPHARMA":  "NSE_EQ|INE044A01036",
    "SBIN":       "NSE_EQ|INE062A01020",
    "MARUTI":     "NSE_EQ|INE585B01010",
    "CIPLA":      "NSE_EQ|INE059A01026",
    "NTPC":       "NSE_EQ|INE733E01010",
    "ADANIENT":   "NSE_EQ|INE423A01024",
    "HINDUNILVR": "NSE_EQ|INE030A01027",
}

UPSTOX_TF_MAP = {
    "5m":  "5minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h":  "60minute",
}

def upstox_headers():
    return {
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "Accept": "application/json"
    }

def fetch_upstox_candles(instrument_key: str, tf: str) -> pd.DataFrame | None:
    if not UPSTOX_ACCESS_TOKEN:
        return None

    resolution = UPSTOX_TF_MAP.get(tf, "5minute")
    today      = datetime.now(IST).date()
    from_date  = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date    = today.strftime("%Y-%m-%d")

    # URL encode the instrument key
    encoded_key = requests.utils.quote(instrument_key, safe='')
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/{resolution}/{to_date}/{from_date}"

    try:
        resp = requests.get(url, headers=upstox_headers(), timeout=15)
        data = resp.json()

        if data.get("status") != "success":
            logger.warning(f"[UPSTOX] Candle fail {instrument_key}: {data.get('errors', '')}")
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

def run_upstox_scanner():
    """S6 Scanner — Indian Markets via Upstox"""
    if not UPSTOX_ACCESS_TOKEN:
        logger.warning("[UPSTOX] Token missing — /token command se update karo!")
        return

    logger.info("[UPSTOX] Scan cycle start...")
    alerts = 0

    # Merge all instruments
    all_instruments = {}
    all_instruments.update(UPSTOX_INSTRUMENTS)
    all_instruments.update(TOP_STOCKS)

    for symbol, ikey in all_instruments.items():
        signals = {}
        for tf in PARAMS.TIMEFRAMES:
            try:
                df     = fetch_upstox_candles(ikey, tf)
                signal = detect_s6(df, tf)
                if signal:
                    signals[tf] = signal
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"[UPSTOX] Error {symbol} {tf}: {e}")

        if signals:
            score = get_multi_tf_score(signals)
            if score >= PARAMS.MIN_SCORE:
                msg = format_alert(symbol, signals, score, "UPSTOX")
                send_telegram(msg)
                alerts += 1
                logger.info(f"[UPSTOX] ✅ Alert: {symbol} Score={score}/10")
                time.sleep(1)
        else:
            logger.info(f"[UPSTOX] {symbol}: No signal")

    logger.info(f"[UPSTOX] Cycle done. Alerts: {alerts}")

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM BOT — COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════════
def telegram_bot():
    global scanner_paused, UPSTOX_ACCESS_TOKEN
    offset = None
    logger.info("[BOT] Telegram bot started.")

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params, timeout=35
            )
            data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip()
                chat   = str(msg.get("chat", {}).get("id", ""))

                if chat != str(TELEGRAM_CHAT_ID):
                    continue

                # /token command
                if text.startswith("/token "):
                    code = text.split("/token ", 1)[1].strip()
                    send_telegram("⏳ Token exchange ho raha hai...")
                    new_token = exchange_upstox_token(code)

                    if new_token:
                        railway_ok = update_railway_token(new_token)
                        send_telegram(
                            f"✅ <b>Upstox Token Update!</b>\n\n"
                            f"{'✅ Railway bhi update!' if railway_ok else '⚠️ Railway manual karo!'}\n"
                            f"📅 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}\n"
                            f"🕐 Kal subah expire hoga"
                        )
                    else:
                        send_telegram("❌ Token fail! Code expire hua hoga.\nDobara /token &lt;fresh_code&gt; bhejo.")

                # /status command
                elif text == "/status":
                    now       = datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')
                    tok_status = "✅ Set" if UPSTOX_ACCESS_TOKEN else "❌ Missing — /token bhejo!"
                    paused_st  = "⏸ Paused" if scanner_paused else "▶️ Running"
                    send_telegram(
                        f"📊 <b>TPS Unified Scanner</b>\n\n"
                        f"🤖 Status: {paused_st}\n"
                        f"🌐 B3 (BTC/ETH): ✅ Active\n"
                        f"🇮🇳 S6 Indian: {tok_status}\n"
                        f"⏱ Scan: every 5 min\n"
                        f"🕐 {now}\n\n"
                        f"<b>Commands:</b>\n"
                        f"/token &lt;code&gt; — Token update\n"
                        f"/status — Yeh message\n"
                        f"/pause — Scanner rok do\n"
                        f"/resume — Scanner chalu karo"
                    )

                # /pause
                elif text == "/pause":
                    scanner_paused = True
                    send_telegram("⏸ <b>Scanner Paused!</b>\n/resume se chalu karo.")

                # /resume
                elif text == "/resume":
                    scanner_paused = False
                    send_telegram("▶️ <b>Scanner Resumed!</b>")

        except Exception as e:
            logger.error(f"[BOT] Error: {e}")
            time.sleep(5)

# ═══════════════════════════════════════════════════════════════════
# MAIN SCHEDULER
# ═══════════════════════════════════════════════════════════════════
def scheduler():
    while True:
        if scanner_paused:
            time.sleep(60)
            continue

        now     = datetime.now(IST)
        hour    = now.hour
        minute  = now.minute
        weekday = now.weekday()

        is_weekday   = weekday < 5
        market_open  = (hour == 9 and minute >= 15) or (9 < hour < 15) or (hour == 15 and minute <= 30)
        mcx_open     = (9 <= hour < 23) or (hour == 23 and minute <= 30)
        crypto_always = True  # BTC/ETH 24x7

        try:
            # BTC/ETH — always
            if crypto_always:
                run_delta_scanner()

            # Indian markets — weekday market/MCX hours
            if is_weekday and (market_open or mcx_open):
                run_upstox_scanner()
            else:
                logger.info(f"[UPSTOX] Market closed. Time: {now.strftime('%H:%M IST')}")

        except Exception as e:
            logger.error(f"[SCHEDULER] Error: {e}")

        time.sleep(PARAMS.SCAN_INTERVAL_SEC)

# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TPS UNIFIED SCANNER v1.0 — Starting...")
    logger.info(f"Time: {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}")
    logger.info(f"BB Period={PARAMS.BB_PERIOD} | Squeeze={PARAMS.SQUEEZE_MULT} | Flat={PARAMS.FLAT_THRESHOLD} | Vol={PARAMS.VOL_MULT}x")
    logger.info("=" * 60)

    send_telegram(
        "🚀 <b>TPS Unified Scanner v1.0 Started!</b>\n\n"
        "🌐 B3: BTC + ETH (Delta Exchange)\n"
        "🇮🇳 S6: Nifty/Sensex + Futures + Top 20 Stocks\n"
        "⏰ TF: 5M · 15M · 30M · 1H\n"
        f"📊 Min Score: {PARAMS.MIN_SCORE}/10\n\n"
        "<b>Commands:</b>\n"
        "/token &lt;code&gt; — Daily Upstox token\n"
        "/status — Scanner status\n"
        "/pause · /resume\n\n"
        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
    )

    # Telegram bot background thread
    bot_thread = Thread(target=telegram_bot, daemon=True)
    bot_thread.start()

    # Main scanner
    scheduler()
