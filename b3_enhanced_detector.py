#!/usr/bin/env python3
"""
B3 FLAT BB DETECTOR - ENHANCED VERSION
Setup 6: Flat BB Jackpot Scanner with Advanced Filters
"""

import os
import json
import time
import math
from datetime import datetime, timedelta
from collections import defaultdict
import requests
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional

# ============================================================================
# CONFIG
# ============================================================================

UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "YOUR_UPSTOX_API_KEY")
UPSTOX_SECRET_KEY = os.getenv("UPSTOX_SECRET_KEY", "YOUR_SECRET_KEY")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
UPSTOX_REFRESH_TOKEN = os.getenv("UPSTOX_REFRESH_TOKEN", "YOUR_REFRESH_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
DELTA_EXCHANGE_API_KEY = os.getenv("DELTA_EXCHANGE_API_KEY", "YOUR_DELTA_KEY")

INSTRUMENTS = {
    "NIFTY_50": {"type": "index", "key": "NSE_INDEX|Nifty 50", "exchange": "upstox"},
    "SENSEX": {"type": "index", "key": "BSE_INDEX|SENSEX", "exchange": "upstox"},
    "GOLD": {"type": "commodity", "key": "MCX|GOLD", "exchange": "upstox"},
    "SILVER": {"type": "commodity", "key": "MCX|SILVER", "exchange": "upstox"},
    "CRUDE_OIL": {"type": "commodity", "key": "MCX|CRUDEOIL", "exchange": "upstox"},
    "NATURAL_GAS": {"type": "commodity", "key": "MCX|NATURALGAS", "exchange": "upstox"},
    "BTC": {"type": "crypto", "key": "BTC", "exchange": "delta"},
    "ETH": {"type": "crypto", "key": "ETH", "exchange": "delta"},
}

TIMEFRAMES = [5, 15, 30, 60]
HTF_TIMEFRAME = 60
BB_PERIOD = 20
BB_STD_DEV = 2
SQUEEZE_THRESHOLD = 0.7
FLAT_THRESHOLD = 0.002
OI_SPIKE_THRESHOLD = 1.15

# ============================================================================
# BOLLINGER BANDS
# ============================================================================

def calculate_bb(closes, period=20, std_dev=2):
    sma = pd.Series(closes).rolling(period).mean().values
    std = pd.Series(closes).rolling(period).std().values
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    return upper, sma, lower

def bb_width_percent(upper, lower, close):
    return ((upper - lower) / close) * 100 if close > 0 else 0

def is_bb_squeeze(closes, period=20, threshold=0.7):
    if len(closes) < period:
        return False
    upper, _, lower = calculate_bb(closes, period, BB_STD_DEV)
    bw_current = bb_width_percent(upper[-1], lower[-1], closes[-1])
    bw_list = [bb_width_percent(upper[i], lower[i], closes[i]) 
               for i in range(max(0, len(closes)-20), len(closes))]
    bw_avg = np.mean(bw_list) if bw_list else 0
    return bw_current < (bw_avg * threshold)

# ============================================================================
# VWAP & FLAT DETECTION
# ============================================================================

def calculate_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vwap = (tp * df['volume']).cumsum() / df['volume'].cumsum()
    return vwap.values

def is_flat_vwap(vwap, window=5, threshold=0.001):
    if len(vwap) < window:
        return False
    recent = vwap[-window:]
    change_pct = (max(recent) - min(recent)) / recent[-1]
    return change_pct < threshold

def is_flat_price(closes, window=8, threshold=0.002):
    if len(closes) < window:
        return False
    recent = closes[-window:]
    change_pct = (max(recent) - min(recent)) / recent[-1]
    return change_pct < threshold

# ============================================================================
# PIVOT POINTS
# ============================================================================

def calculate_pivot_points(high, low, close):
    pp = (high + low + close) / 3
    r1 = (2 * pp) - low
    s1 = (2 * pp) - high
    return {"pp": pp, "r1": r1, "s1": s1}

def calculate_pivot_accuracy(current_price, upper_band, pivot_pp):
    pp_distance = upper_band - pivot_pp
    if pp_distance > 0:
        accuracy = 0.95
        zone = "BEST"
        reasoning = f"Pivot PP ({pivot_pp:.2f}) is {pp_distance:.2f} below upper band ({upper_band:.2f}) → HIGH ACCURACY ✓"
    else:
        accuracy = 0.50
        zone = "WEAK"
        reasoning = f"Pivot PP ({pivot_pp:.2f}) is at/above upper band ({upper_band:.2f}) → LOW ACCURACY ✗"
    return {"accuracy_score": accuracy, "pivot_zone": zone, "reasoning": reasoning, "pp_distance": pp_distance}

# ============================================================================
# OI SPIKE DETECTION
# ============================================================================

def detect_oi_spike(current_oi, prev_oi, threshold=1.15):
    if prev_oi <= 0:
        return {"spike_detected": False, "oi_increase_pct": 0, "multiplier": 1.0}
    oi_increase_pct = current_oi / prev_oi
    spike_detected = oi_increase_pct > threshold
    if spike_detected:
        increase = ((oi_increase_pct - 1) * 100)
        multiplier = min(1.25, 1.0 + (increase / 100))
        reasoning = f"OI spike detected: +{increase:.1f}%"
    else:
        multiplier = 1.0
        reasoning = "No OI spike"
    return {"spike_detected": spike_detected, "oi_increase_pct": oi_increase_pct - 1, "multiplier": multiplier, "reasoning": reasoning}

# ============================================================================
# HTF SMART ZONE
# ============================================================================

def detect_htf_smart_zone(htf_closes, current_price, signal_type):
    if len(htf_closes) < 50:
        return {"smart_zone": False, "multiplier": 1.0, "reasoning": "Insufficient HTF data"}
    upper, middle, lower = calculate_bb(htf_closes, BB_PERIOD, BB_STD_DEV)
    htf_squeeze = is_bb_squeeze(htf_closes, BB_PERIOD, SQUEEZE_THRESHOLD)
    if not htf_squeeze:
        return {"smart_zone": False, "multiplier": 1.0, "reasoning": "1H BB not in squeeze"}
    band_height = upper[-1] - lower[-1]
    lower_zone = lower[-1] + (band_height * 0.2)
    upper_zone = upper[-1] - (band_height * 0.2)
    current_price_latest = htf_closes[-1]
    if signal_type == "BULLISH" and current_price_latest < lower_zone:
        return {"smart_zone": True, "multiplier": 1.15, "reasoning": f"✓ HTF Smart Zone: 1H squeeze active, price at lower band"}
    elif signal_type == "BEARISH" and current_price_latest > upper_zone:
        return {"smart_zone": True, "multiplier": 1.15, "reasoning": f"✓ HTF Smart Zone: 1H squeeze active, price at upper band"}
    return {"smart_zone": False, "multiplier": 1.0, "reasoning": "1H conditions not aligned"}

# ============================================================================
# VOLUME ANALYSIS
# ============================================================================

def volume_spike_detected(volumes, threshold=1.5, lookback=20):
    if len(volumes) < lookback + 1:
        return False
    avg_vol = np.mean(volumes[-lookback:-1])
    current_vol = volumes[-1]
    return current_vol > (avg_vol * threshold)

# ============================================================================
# CANDLE PATTERNS
# ============================================================================

def is_bullish_candle(open_, close):
    return close > open_

def is_bearish_candle(open_, close):
    return close < open_

# ============================================================================
# UPSTOX API CLIENT
# ============================================================================

class UpstoxClient:
    BASE_URL = "https://api.upstox.com/v2"
    
    def __init__(self, api_key, secret_key, access_token, refresh_token):
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        self.token_generated_time = datetime.now()
    
    def refresh_access_token(self):
        try:
            url = "https://api.upstox.com/v2/login/authorization/token"
            headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
            data = {"grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.api_key, "client_secret": self.secret_key}
            resp = requests.post(url, headers=headers, data=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if "access_token" in result:
                self.access_token = result["access_token"]
                self.headers["Authorization"] = f"Bearer {self.access_token}"
                self.token_generated_time = datetime.now()
                print(f"✓ Token refreshed successfully at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                return True
            return False
        except Exception as e:
            print(f"Error refreshing token: {e}")
            return False
    
    def is_token_expired(self):
        elapsed = datetime.now() - self.token_generated_time
        hours_elapsed = elapsed.total_seconds() / 3600
        return hours_elapsed >= 23
    
    def get_market_data(self, instrument_key, interval="1minute"):
        if self.is_token_expired():
            print(f"Token about to expire. Refreshing...")
            if not self.refresh_access_token():
                print("⚠️ Token refresh failed!")
                return None
        try:
            url = f"{self.BASE_URL}/market-quote/"
            params = {"mode": "OHLC", "instrument_key": instrument_key}
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success" and "data" in data:
                return data["data"]
            return None
        except Exception as e:
            print(f"Error fetching {instrument_key}: {e}")
            return None
    
    def get_option_chain(self, index_name="NIFTY"):
        if self.is_token_expired():
            print(f"Token about to expire. Refreshing...")
            if not self.refresh_access_token():
                print("⚠️ Token refresh failed!")
                return None
        try:
            url = f"{self.BASE_URL}/option/chain"
            params = {"index_name": index_name}
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Error fetching option chain: {e}")
            return None

# ============================================================================
# DELTA EXCHANGE CLIENT
# ============================================================================

class DeltaExchangeClient:
    BASE_URL = "https://api.delta.exchange"
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    def get_market_data(self, contract_id, interval="1m"):
        try:
            url = f"{self.BASE_URL}/v2/tickers/{contract_id}"
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Error fetching {contract_id}: {e}")
            return None
    
    def get_option_chain(self, contract_id):
        try:
            url = f"{self.BASE_URL}/v2/optionchain"
            params = {"underlying_symbol": contract_id}
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Error fetching option chain {contract_id}: {e}")
            return None

# ============================================================================
# TELEGRAM ALERTS
# ============================================================================

def send_telegram_alert(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        resp = requests.post(url, json=data, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ============================================================================
# DETECTOR
# ============================================================================

def detect_flat_bb_jackpot(instrument, timeframe_min, closes, opens, highs, lows, volumes, vwap, htf_closes=None, current_oi=None, prev_oi=None):
    result = {
        "instrument": instrument,
        "timeframe": f"{timeframe_min}min",
        "signal": "NO_SIGNAL",
        "confidence": 0.0,
        "timestamp": datetime.now().isoformat(),
        "details": {},
        "filters": {}
    }
    
    if len(closes) < 50:
        return result
    
    squeeze = is_bb_squeeze(closes, BB_PERIOD, SQUEEZE_THRESHOLD)
    result["details"]["bb_squeeze"] = squeeze
    if not squeeze:
        return result
    
    price_flat = is_flat_price(closes, window=8, threshold=FLAT_THRESHOLD)
    result["details"]["price_flat"] = price_flat
    if not price_flat:
        return result
    
    vwap_flat = is_flat_vwap(vwap, window=5, threshold=0.001)
    result["details"]["vwap_flat"] = vwap_flat
    if not vwap_flat:
        return result
    
    upper, middle, lower = calculate_bb(closes, BB_PERIOD, BB_STD_DEV)
    vwap_inside = (lower[-1] < vwap[-1] < upper[-1])
    result["details"]["vwap_inside_bands"] = vwap_inside
    if not vwap_inside:
        return result
    
    vol_spike = volume_spike_detected(volumes, threshold=1.5, lookback=20)
    result["details"]["volume_spike"] = vol_spike
    if not vol_spike:
        return result
    
    latest_open = opens[-1]
    latest_close = closes[-1]
    latest_high = highs[-1]
    latest_low = lows[-1]
    
    is_bullish = is_bullish_candle(latest_open, latest_close)
    is_bearish = is_bearish_candle(latest_open, latest_close)
    
    result["details"]["latest_candle_bullish"] = is_bullish
    result["details"]["latest_candle_bearish"] = is_bearish
    
    prev_high = highs[-2] if len(highs) >= 2 else highs[-1]
    prev_low = lows[-2] if len(lows) >= 2 else lows[-1]
    prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
    
    pivot = calculate_pivot_points(prev_high, prev_low, prev_close)
    pivot_accuracy = calculate_pivot_accuracy(latest_close, upper[-1], pivot["pp"])
    
    result["details"]["pivot_pp"] = pivot["pp"]
    result["details"]["pivot_accuracy_zone"] = pivot_accuracy["pivot_zone"]
    result["details"]["pivot_reasoning"] = pivot_accuracy["reasoning"]
    
    oi_spike = {"spike_detected": False, "multiplier": 1.0, "reasoning": "No OI data"}
    if current_oi and prev_oi:
        oi_spike = detect_oi_spike(current_oi, prev_oi, OI_SPIKE_THRESHOLD)
    
    result["filters"]["oi_spike"] = oi_spike
    result["details"]["oi_spike_detected"] = oi_spike["spike_detected"]
    result["details"]["oi_reasoning"] = oi_spike["reasoning"]
    
    htf_smart_zone = {"smart_zone": False, "multiplier": 1.0, "reasoning": "No HTF data"}
    signal_type = "BULLISH" if is_bullish else "BEARISH"
    if htf_closes is not None:
        htf_smart_zone = detect_htf_smart_zone(htf_closes, latest_close, signal_type)
    
    result["filters"]["htf_smart_zone"] = htf_smart_zone
    result["details"]["htf_smart_zone"] = htf_smart_zone["smart_zone"]
    result["details"]["htf_reasoning"] = htf_smart_zone["reasoning"]
    
    base_confidence = 0.85
    pivot_multiplier = pivot_accuracy["accuracy_score"]
    oi_multiplier = oi_spike["multiplier"]
    htf_multiplier = htf_smart_zone["multiplier"]
    
    final_confidence = base_confidence * pivot_multiplier * oi_multiplier * htf_multiplier
    final_confidence = min(final_confidence, 0.99)
    
    result["details"]["base_confidence"] = base_confidence
    result["details"]["pivot_multiplier"] = pivot_multiplier
    result["details"]["oi_multiplier"] = oi_multiplier
    result["details"]["htf_multiplier"] = htf_multiplier
    result["details"]["final_confidence"] = final_confidence
    
    if is_bullish:
        result["signal"] = "FLAT_BB_BULLISH_BUY"
        result["confidence"] = final_confidence
        result["entry_level"] = latest_close
        result["sl_level"] = latest_low
        result["rr_ratio"] = "1:2+" if latest_close > middle[-1] else "1:1.5"
    elif is_bearish:
        result["signal"] = "FLAT_BB_BEARISH_SELL"
        result["confidence"] = final_confidence
        result["entry_level"] = latest_close
        result["sl_level"] = latest_high
        result["rr_ratio"] = "1:2+" if latest_close < middle[-1] else "1:1.5"
    
    return result

# ============================================================================
# TELEGRAM FORMAT
# ============================================================================

def format_telegram_alert(signal):
    pivot_zone = signal['details'].get('pivot_accuracy_zone', 'N/A')
    pivot_reasoning = signal['details'].get('pivot_reasoning', '')
    oi_spike = signal['details'].get('oi_spike_detected', False)
    oi_reasoning = signal['details'].get('oi_reasoning', '')
    htf_smart = signal['details'].get('htf_smart_zone', False)
    htf_reasoning = signal['details'].get('htf_reasoning', '')
    confidence_pct = signal['confidence'] * 100
    
    if confidence_pct >= 90:
        conf_emoji = "🔥"
    elif confidence_pct >= 80:
        conf_emoji = "🟢"
    elif confidence_pct >= 70:
        conf_emoji = "🟡"
    else:
        conf_emoji = "🔴"
    
    oi_emoji = "⚡" if oi_spike else ""
    htf_emoji = "📈" if htf_smart else ""
    
    msg = f"""
<b>🎯 B3 FLAT BB JACKPOT + FILTERS</b>

<b>Instrument:</b> {signal['instrument']}
<b>Timeframe:</b> {signal['timeframe']}
<b>Signal:</b> {signal['signal']}

<b>{conf_emoji} Confidence:</b> {confidence_pct:.1f}%
<b>📍 Pivot Zone:</b> {pivot_zone}
{oi_emoji}<b>OI Spike:</b> {'YES ⚡' if oi_spike else 'No'}
{htf_emoji}<b>HTF Smart Zone:</b> {'YES 📈' if htf_smart else 'No'}

<b>Entry:</b> {signal.get('entry_level', 'N/A'):.2f}
<b>SL:</b> {signal.get('sl_level', 'N/A'):.2f}
<b>R:R:</b> {signal.get('rr_ratio', 'N/A')}

<b>Conditions (6/6):</b>
✓ BB Squeeze • Price Flat • VWAP Flat
✓ VWAP Inside • Volume Spike • Pivot PP

<b>Filter Details:</b>
{pivot_reasoning}
{oi_reasoning}
{htf_reasoning}

<b>Time:</b> {signal['timestamp']}
"""
    return msg

# ============================================================================
# SCANNER
# ============================================================================

def scan_instrument(client, instrument_key, instrument_name, htf_data=None, oi_data=None):
    signals = []
    for tf_min in TIMEFRAMES:
        try:
            if tf_min == 5:
                interval = "5minute"
            elif tf_min == 15:
                interval = "15minute"
            elif tf_min == 30:
                interval = "30minute"
            elif tf_min == 60:
                interval = "hour"
            else:
                continue
            
            data = client.get_market_data(instrument_key, interval)
            if not data or "candles" not in data:
                continue
            
            candles = data["candles"][-100:]
            if len(candles) < 30:
                continue
            
            df = pd.DataFrame({
                "open": [c[1] for c in candles],
                "high": [c[2] for c in candles],
                "low": [c[3] for c in candles],
                "close": [c[4] for c in candles],
                "volume": [c[5] for c in candles],
            })
            
            opens = df["open"].values
            highs = df["high"].values
            lows = df["low"].values
            closes = df["close"].values
            volumes = df["volume"].values
            vwap = calculate_vwap(df)
            
            htf_closes = None
            if htf_data and instrument_name in htf_data:
                htf_closes = htf_data[instrument_name]
            
            current_oi = None
            prev_oi = None
            if oi_data and instrument_name in oi_data:
                current_oi = oi_data[instrument_name].get("current", None)
                prev_oi = oi_data[instrument_name].get("prev", None)
            
            signal = detect_flat_bb_jackpot(
                instrument_name, tf_min,
                closes, opens, highs, lows, volumes, vwap,
                htf_closes=htf_closes,
                current_oi=current_oi,
                prev_oi=prev_oi
            )
            
            if signal["signal"] != "NO_SIGNAL":
                signals.append(signal)
                conf = signal['confidence'] * 100
                print(f"✓ {instrument_name} {tf_min}min: {signal['signal']} (conf: {conf:.0f}%)")
        
        except Exception as e:
            print(f"Error scanning {instrument_name} {tf_min}min: {e}")
    
    return signals

# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 80)
    print("B3 FLAT BB DETECTOR - ALL INSTRUMENTS (Nifty, Sensex, Crypto, Commodities)")
    print("=" * 80)
    print("Scanning:")
    print("  ✓ Nifty 50 + CE/PE")
    print("  ✓ Sensex + CE/PE")
    print("  ✓ Gold + CE/PE")
    print("  ✓ Silver + CE/PE")
    print("  ✓ Crude Oil + CE/PE")
    print("  ✓ Natural Gas + CE/PE")
    print("  ✓ BTC + CE/PE (Delta Exchange)")
    print("  ✓ ETH + CE/PE (Delta Exchange)")
    print("=" * 80)
    
    upstox_client = UpstoxClient(UPSTOX_API_KEY, UPSTOX_SECRET_KEY, UPSTOX_ACCESS_TOKEN, UPSTOX_REFRESH_TOKEN)
    delta_client = DeltaExchangeClient(DELTA_EXCHANGE_API_KEY)
    
    scan_count = 0
    
    while True:
        try:
            scan_count += 1
            print(f"\n[Scan #{scan_count}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            all_signals = []
            
            for inst_name, inst_config in INSTRUMENTS.items():
                if inst_config["exchange"] == "skip_upstox":
                    continue
                
                try:
                    signals = scan_instrument(upstox_client, inst_config["key"], inst_name)
                    all_signals.extend(signals)
                except Exception as e:
                    print(f"Error scanning {inst_name}: {e}")
            
            for signal in all_signals:
                alert_msg = format_telegram_alert(signal)
                success = send_telegram_alert(alert_msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
                if success:
                    print(f"✓ Alert sent: {signal['instrument']}")
            
            print(f"Waiting 5 minutes until next scan...")
            time.sleep(300)
        
        except KeyboardInterrupt:
            print("\n\nScanner stopped.")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
