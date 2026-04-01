#!/usr/bin/env python3
import os, time, requests, numpy as np, pandas as pd
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
SYMBOLS    = ["BTCUSD", "ETHUSD"]
TIMEFRAMES = [5, 15, 30, 60]
BB_PERIOD  = 20
RES_MAP    = {5: "5m", 15: "15m", 30: "30m", 60: "1h"}

def get_candles(symbol, resolution, limit=100):
    res_str = RES_MAP.get(resolution, "5m")
    end     = int(datetime.now().timestamp())
    start   = end - limit * resolution * 60
    try:
        r = requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={"resolution": res_str, "symbol": symbol, "start": start, "end": end},
            timeout=15
        )
        data = r.json()
        if data.get("success") and data.get("result"):
            return data["result"]
    except Exception as e:
        print(f"  Error {symbol} {res_str}: {e}")
    return []

def calc_bb(closes):
    s   = pd.Series(closes)
    sma = s.rolling(BB_PERIOD).mean().values
    std = s.rolling(BB_PERIOD).std().values
    return sma + 2*std, sma, sma - 2*std

def is_flat(series, window=8, threshold=0.002):
    r = series[-window:]
    r = r[~np.isnan(r)]
    if len(r) < 3:
        return False
    return (np.max(r) - np.min(r)) / abs(r[-1]) < threshold

def vol_spike(volumes):
    if len(volumes) < 21:
        return False
    return volumes[-1] > np.mean(volumes[-21:-1]) * 1.5

def detect(symbol, tf, candles):
    if len(candles) < 40:
        return None
    op = np.array([c["open"]   for c in candles], dtype=float)
    hi = np.array([c["high"]   for c in candles], dtype=float)
    lo = np.array([c["low"]    for c in candles], dtype=float)
    cl = np.array([c["close"]  for c in candles], dtype=float)
    vo = np.array([c["volume"] for c in candles], dtype=float)
    upper, sma, lower = calc_bb(cl)
    if not (is_flat(sma) and is_flat(upper) and is_flat(lower)):
        return None
    if not vol_spike(vo):
        return None
    pp   = (hi[-2] + lo[-2] + cl[-2]) / 3
    dist = (pp - upper[-1]) / upper[-1] * 100
    if dist > 0.5:
        return None
    pp_zone = "PERFECT" if dist <= 0 else "OK"
    if cl[-1] > op[-1] and cl[-1] >= upper[-1]:
        direction, entry, sl = "BULLISH", cl[-1], lo[-1]
    elif cl[-1] < op[-1] and cl[-1] <= lower[-1]:
        direction, entry, sl = "BEARISH", cl[-1], hi[-1]
    else:
        return None
    return {
        "symbol": symbol, "tf": tf, "direction": direction,
        "entry": round(entry, 2), "sl": round(sl, 2), "pp_zone": pp_zone,
        "upper": round(float(upper[-1]), 2), "sma": round(float(sma[-1]), 2),
        "lower": round(float(lower[-1]), 2),
        "time": datetime.now().strftime("%H:%M:%S")
    }

def send_alert(s):
    e   = "📈" if s["direction"] == "BULLISH" else "📉"
    opt = "BUY CE ATM" if s["direction"] == "BULLISH" else "BUY PE ATM"
    msg = (
        f"{e} <b>B3 FLAT BB JACKPOT</b>\n\n"
        f"<b>Symbol:</b> {s['symbol']}\n"
        f"<b>TF:</b> {s['tf']}min\n"
        f"<b>Direction:</b> {s['direction']}\n"
        f"<b>Option:</b> {opt}\n"
        f"<b>Entry:</b> {s['entry']}\n"
        f"<b>SL:</b> {s['sl']}\n"
        f"<b>PP Zone:</b> {s['pp_zone']}\n"
        f"<b>Upper:</b> {s['upper']} Flat\n"
        f"<b>SMA20:</b> {s['sma']} Flat\n"
        f"<b>Lower:</b> {s['lower']} Flat\n"
        f"<b>Time:</b> {s['time']}"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

def main():
    print("=" * 50)
    print("B3 FLAT BB DETECTOR - DELTA (BTC+ETH)")
    print("=" * 50)
    scan = 0
    while True:
        try:
            scan += 1
            print(f"\n[Scan #{scan}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            for symbol in SYMBOLS:
                print(f"  {symbol}:")
                for tf in TIMEFRAMES:
                    candles = get_candles(symbol, tf)
                    if not candles:
                        print(f"    {tf}min: No data")
                        continue
                    sig = detect(symbol, tf, candles)
                    if sig:
                        print(f"    {tf}min: SIGNAL {sig['direction']}!")
                        send_alert(sig)
                    else:
                        print(f"    {tf}min: No signal")
            print("  Waiting 5min...")
            time.sleep(300)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
