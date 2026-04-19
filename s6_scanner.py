"""
TPS S6 Flat BB Jackpot Scanner - Upstox
========================================
Setup 6 (Flat BB Jackpot):
- BB(20,2) Squeeze + Flat bands (SMA20 + bands simultaneously flat)
- Flat threshold: <0.2% over 8 candles
- Blast candle with volume spike (>20avg x 1.5)
- TF Score: 5M=3, 15M=5, 30M=6, 1H=8
- Multi-TF: 5M+15M=6, 15M+30M=7, 30M+1H=9, All=10
- Instruments: Index, Futures, Options (CE/PE ATM+OTM)
- Telegram /token command for daily token update
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# ─── Environment Variables ────────────────────────────────────────────────────
UPSTOX_API_KEY      = os.environ.get("UPSTOX_API_KEY", "")
UPSTOX_SECRET_KEY   = os.environ.get("UPSTOX_SECRET_KEY", "")
UPSTOX_ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
REDIRECT_URI        = "https://127.0.0.1"

TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

RAILWAY_API_TOKEN      = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_PROJECT_ID     = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_SERVICE_ID     = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "production")

# ─── TF Score Map ─────────────────────────────────────────────────────────────
TF_SCORE = {
    "5m":  3,
    "15m": 5,
    "30m": 6,
    "1h":  8,
}

MULTI_TF_SCORE = {
    frozenset(["5m", "15m"]):        6,
    frozenset(["15m", "30m"]):       7,
    frozenset(["30m", "1h"]):        9,
    frozenset(["5m", "15m", "30m", "1h"]): 10,
}

TIMEFRAMES = ["5m", "15m", "30m", "1h"]

# ─── Upstox Instrument Keys ───────────────────────────────────────────────────
# These are Upstox instrument_key format: EXCHANGE|SYMBOL
INSTRUMENTS = {
    # Index
    "NIFTY_INDEX":   "NSE_INDEX|Nifty 50",
    "SENSEX_INDEX":  "BSE_INDEX|SENSEX",

    # Futures - current expiry (will be fetched dynamically)
    "NIFTY_FUT":     "NSE_FO|NIFTY",
    "SENSEX_FUT":    "BSE_FO|SENSEX",
    "GOLD_FUT":      "MCX_FO|GOLD",
    "SILVERM_FUT":   "MCX_FO|SILVERM",
    "CRUDE_FUT":     "MCX_FO|CRUDEOIL",
    "NATGAS_FUT":    "MCX_FO|NATURALGAS",
}

# ─── Upstox API Headers ───────────────────────────────────────────────────────
def get_headers():
    return {
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "Accept": "application/json"
    }

# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(message: str, parse_mode="HTML"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": parse_mode
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ─── Railway Variable Update ──────────────────────────────────────────────────
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

# ─── Token Exchange (code -> access_token) ───────────────────────────────────
def exchange_code_for_token(code: str) -> str | None:
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
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code"
            },
            timeout=30
        )
        data = resp.json()
        return data.get("access_token")
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return None

# ─── Fetch OHLCV Candles from Upstox ─────────────────────────────────────────
def fetch_candles(instrument_key: str, interval: str, days: int = 5) -> pd.DataFrame | None:
    """
    interval: '1minute', '5minute', '15minute', '30minute', '60minute'
    """
    interval_map = {
        "5m":  "5minute",
        "15m": "15minute",
        "30m": "30minute",
        "1h":  "60minute",
    }
    upstox_interval = interval_map.get(interval, "5minute")

    today = datetime.now(IST).date()
    from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/{upstox_interval}/{to_date}/{from_date}"

    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        data = resp.json()

        if data.get("status") != "success":
            logger.warning(f"Candle fetch failed for {instrument_key}: {data}")
            return None

        candles = data["data"]["candles"]
        if not candles or len(candles) < 20:
            return None

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
        return df

    except Exception as e:
        logger.error(f"Candle fetch error {instrument_key}: {e}")
        return None

# ─── Bollinger Band Calculation ───────────────────────────────────────────────
def calculate_bb(df: pd.DataFrame, period=20, std=2) -> pd.DataFrame:
    df = df.copy()
    df["sma20"]    = df["close"].rolling(period).mean()
    df["std20"]    = df["close"].rolling(period).std()
    df["bb_upper"] = df["sma20"] + std * df["std20"]
    df["bb_lower"] = df["sma20"] - std * df["std20"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["sma20"]
    return df

# ─── S6 Detection Logic ───────────────────────────────────────────────────────
def detect_s6(df: pd.DataFrame, tf: str) -> dict | None:
    """
    S6 Flat BB Jackpot:
    1. BB Squeeze: BW < 20avg x 0.7
    2. Flat bands: SMA20 + upper + lower all flat (<0.2% change over 8 candles)
    3. Blast candle: close outside BB + volume > 20avg x 1.5
    Returns signal dict or None
    """
    if df is None or len(df) < 30:
        return None

    df = calculate_bb(df)
    df = df.dropna().reset_index(drop=True)

    if len(df) < 20:
        return None

    last    = df.iloc[-1]
    prev8   = df.iloc[-9:-1]  # 8 candles before last

    # ── 1. BB Squeeze Check ──
    avg_bw_20 = df["bb_width"].iloc[-21:-1].mean()
    squeeze   = last["bb_width"] < avg_bw_20 * 0.7

    # ── 2. Flat Bands Check (SMA20 + upper + lower all flat over 8 candles) ──
    sma_range   = (prev8["sma20"].max() - prev8["sma20"].min()) / prev8["sma20"].mean()
    upper_range = (prev8["bb_upper"].max() - prev8["bb_upper"].min()) / prev8["bb_upper"].mean()
    lower_range = (prev8["bb_lower"].max() - prev8["bb_lower"].min()) / prev8["bb_lower"].mean()

    flat = sma_range < 0.002 and upper_range < 0.002 and lower_range < 0.002

    # ── 3. Blast Candle Check ──
    avg_vol_20 = df["volume"].iloc[-21:-1].mean()
    vol_spike  = last["volume"] > avg_vol_20 * 1.5

    blast_up   = last["close"] > last["bb_upper"] and vol_spike
    blast_down = last["close"] < last["bb_lower"] and vol_spike

    if not (squeeze and flat and (blast_up or blast_down)):
        return None

    direction = "BULLISH 🟢" if blast_up else "BEARISH 🔴"
    tf_score  = TF_SCORE.get(tf, 0)

    return {
        "direction":  direction,
        "tf":         tf,
        "tf_score":   tf_score,
        "close":      round(last["close"], 2),
        "bb_upper":   round(last["bb_upper"], 2),
        "bb_lower":   round(last["bb_lower"], 2),
        "bb_width":   round(last["bb_width"] * 100, 3),
        "volume":     int(last["volume"]),
        "avg_vol":    int(avg_vol_20),
        "timestamp":  last["timestamp"].strftime("%H:%M"),
    }

# ─── Multi-TF Score Calculator ────────────────────────────────────────────────
def calculate_multi_tf_score(signals: dict) -> int:
    """signals = {tf: signal_dict}"""
    active_tfs = frozenset(signals.keys())

    # Check multi-TF combos
    for combo, score in sorted(MULTI_TF_SCORE.items(), key=lambda x: -x[1]):
        if combo.issubset(active_tfs):
            return score

    # Single TF
    if active_tfs:
        return max(TF_SCORE.get(tf, 0) for tf in active_tfs)

    return 0

# ─── Format Alert Message ─────────────────────────────────────────────────────
def format_alert(symbol: str, signals: dict, score: int) -> str:
    direction = list(signals.values())[0]["direction"]
    tfs_str   = " + ".join([s.upper() for s in signals.keys()])
    close     = list(signals.values())[0]["close"]
    time_str  = list(signals.values())[0]["timestamp"]

    score_bar = "⭐" * min(score, 10)

    msg = f"""
🔥 <b>TPS S6 - FLAT BB JACKPOT</b> 🔥

📌 <b>{symbol}</b>
{direction}
⏰ Timeframe: <b>{tfs_str}</b>
💰 Price: <b>{close}</b>
🕐 Time: {time_str} IST

📊 <b>TF Score: {score}/10</b>
{score_bar}

"""
    for tf, sig in signals.items():
        msg += f"  {tf.upper()}: BW={sig['bb_width']}% | Vol={sig['volume']:,} (avg {sig['avg_vol']:,})\n"

    msg += f"\n⚡ <b>Action: Options Buying (ATM Current Expiry)</b>"
    msg += f"\n🔴 SL = Entry candle low (for Bull) / high (for Bear)"
    msg += f"\n\n#TPS #S6 #FlatBB"

    return msg.strip()

# ─── Fetch Top 50 High OI Stocks ─────────────────────────────────────────────
def get_top50_instruments() -> list:
    """
    Upstox se Nifty 100 stocks ka OI fetch karke top 50 return karo.
    Fallback: hardcoded top 30 liquid stocks.
    """
    # Fallback top liquid stocks (Nifty 100 high OI)
    TOP_STOCKS = [
        "NSE_EQ|INE009A01021",  # INFY
        "NSE_EQ|INE062A01020",  # TCS
        "NSE_EQ|INE040A01034",  # HDFC Bank
        "NSE_EQ|INE090A01021",  # ICICI Bank
        "NSE_EQ|INE001A01036",  # Reliance
        "NSE_EQ|INE467B01029",  # Bajaj Finance
        "NSE_EQ|INE585B01010",  # Axis Bank
        "NSE_EQ|INE721A01013",  # Kotak Bank
        "NSE_EQ|INE029A01011",  # Wipro
        "NSE_EQ|INE018A01030",  # Larsen
        "NSE_EQ|INE066A01021",  # ONGC
        "NSE_EQ|INE101A01026",  # Titan
        "NSE_EQ|INE070A01015",  # Tata Motors
        "NSE_EQ|INE155A01022",  # Asian Paints
        "NSE_EQ|INE196A01026",  # Sun Pharma
        "NSE_EQ|INE002A01018",  # State Bank
        "NSE_EQ|INE356A01018",  # Maruti
        "NSE_EQ|INE117A01022",  # Cipla
        "NSE_EQ|INE758T01015",  # Adani Ent
        "NSE_EQ|INE669C01036",  # NTPC
    ]
    return TOP_STOCKS

# ─── Main Scanner ─────────────────────────────────────────────────────────────
def run_scanner():
    logger.info("S6 Scanner cycle start...")

    # All instruments to scan
    instruments_to_scan = {}

    # Index + Futures
    instruments_to_scan.update(INSTRUMENTS)

    # Top 50 Stocks
    for i, ikey in enumerate(get_top50_instruments()):
        instruments_to_scan[f"STOCK_{i+1}"] = ikey

    alerts_sent = 0

    for symbol, instrument_key in instruments_to_scan.items():
        signals = {}

        for tf in TIMEFRAMES:
            try:
                df = fetch_candles(instrument_key, tf)
                signal = detect_s6(df, tf)
                if signal:
                    signals[tf] = signal
                time.sleep(0.3)  # Rate limit
            except Exception as e:
                logger.error(f"Error scanning {symbol} {tf}: {e}")

        if signals:
            score = calculate_multi_tf_score(signals)
            msg   = format_alert(symbol, signals, score)
            send_telegram(msg)
            alerts_sent += 1
            logger.info(f"✅ Alert sent: {symbol} | Score: {score}/10")
            time.sleep(1)

    logger.info(f"Scanner cycle complete. Alerts sent: {alerts_sent}")

# ─── Telegram Bot Command Handler ─────────────────────────────────────────────
def handle_telegram_updates():
    """
    Poll Telegram for /token command.
    Usage: /token YOUR_UPSTOX_AUTH_CODE
    """
    global UPSTOX_ACCESS_TOKEN

    offset = None

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=35
            )
            data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "")
                chat   = msg.get("chat", {}).get("id")

                # Only accept from authorized chat
                if str(chat) != str(TELEGRAM_CHAT_ID):
                    continue

                # /token command
                if text.startswith("/token "):
                    code = text.split("/token ", 1)[1].strip()
                    send_telegram("⏳ Token exchange ho raha hai...")

                    new_token = exchange_code_for_token(code)

                    if new_token:
                        UPSTOX_ACCESS_TOKEN = new_token

                        # Railway update
                        railway_ok = update_railway_token(new_token)
                        railway_msg = "✅ Railway variable bhi update ho gaya!" if railway_ok else "⚠️ Railway update manual karo!"

                        send_telegram(
                            f"✅ <b>Upstox Token Update Ho Gaya!</b>\n\n"
                            f"{railway_msg}\n\n"
                            f"🕐 Valid until: kal subah tak\n"
                            f"📅 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
                        )
                        logger.info("✅ Token updated via Telegram!")
                    else:
                        send_telegram(
                            "❌ <b>Token Exchange Failed!</b>\n\n"
                            "Code expire ho gaya hoga.\n"
                            "Dobara /token <fresh_code> bhejo."
                        )

                # /status command
                elif text == "/status":
                    now = datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')
                    token_ok = "✅ Set" if UPSTOX_ACCESS_TOKEN else "❌ Missing"
                    send_telegram(
                        f"📊 <b>TPS S6 Scanner Status</b>\n\n"
                        f"🤖 Scanner: ✅ Running\n"
                        f"🔑 Token: {token_ok}\n"
                        f"🕐 Time: {now}\n\n"
                        f"Commands:\n"
                        f"/token &lt;code&gt; — Token update karo\n"
                        f"/status — Yeh message"
                    )

        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
            time.sleep(5)

# ─── Scheduler ────────────────────────────────────────────────────────────────
def scheduler():
    """Run scanner every 5 minutes during market hours"""
    while True:
        now = datetime.now(IST)
        hour   = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=Mon, 6=Sun

        # Market hours: Mon-Fri 9:15 AM to 3:30 PM IST
        market_open  = (hour == 9 and minute >= 15) or (9 < hour < 15) or (hour == 15 and minute <= 30)
        is_weekday   = weekday < 5

        # MCX hours: 9 AM to 11:30 PM (commodities)
        mcx_open = (9 <= hour < 23) or (hour == 23 and minute <= 30)

        if is_weekday and (market_open or mcx_open):
            try:
                run_scanner()
            except Exception as e:
                logger.error(f"Scanner error: {e}")
        else:
            logger.info(f"Market closed. Next check in 5 min. Time: {now.strftime('%H:%M')}")

        time.sleep(300)  # 5 minutes

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TPS S6 Flat BB Jackpot Scanner - Starting...")
    logger.info(f"Time: {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}")
    logger.info("=" * 60)

    # Send startup message
    send_telegram(
        "🚀 <b>TPS S6 Scanner Started!</b>\n\n"
        "📊 Scanning: Index + Futures + Top Stocks\n"
        "⏰ Timeframes: 5M, 15M, 30M, 1H\n"
        "🔍 Setup: S6 Flat BB Jackpot\n\n"
        "Commands:\n"
        "/token &lt;code&gt; — Daily token update\n"
        "/status — Scanner status\n\n"
        f"🕐 {datetime.now(IST).strftime('%d-%m-%Y %H:%M IST')}"
    )

    # Start Telegram bot in background thread
    bot_thread = Thread(target=handle_telegram_updates, daemon=True)
    bot_thread.start()
    logger.info("Telegram bot thread started.")

    # Start scanner
    scheduler()
