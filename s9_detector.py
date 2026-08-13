"""
s9_detector.py — S9 "Pivot Confluence Blast" detector engine
=============================================================

Setup:
    Daily PP (previous day ka H+L+C/3) BB upper aur lower ke ANDAR ho —
    5M / 15M / 30M / 1H / DAILY — SAB PE, ek saath.
    Phir 5M candle BB upper ke BAHAR close kare = BLAST.

    Buying-only setup. Sirf upside blast. Koi bearish logic nahi.

Ye module fetch functions ko INJECT karke chalta hai (fetch_fn(key, tf) -> DataFrame),
isliye tps_unified_scanner.py se circular import nahi hota.

Scoring / ladder / gate / blacklist ka pura logic s9_upgrades.py me hai —
ye file sirf DETECTION aur DATA CACHING karti hai.

PERFORMANCE — kyun caching zaroori hai:
    Har instrument pe 5 TF ka data chahiye. Universe ~150 option charts ka hai,
    yaani 750 API calls per cycle — 5 minute ka cycle isme hi nikal jaata.
    Isliye:
        5m   → har cycle fresh (yahi blast detect karta hai)
        15m  → 5 min cache
        30m  → 10 min cache
        1h   → 15 min cache
        1d   → poore din ek baar (daily pivot aur daily BB din me ek baar hi badalte)
    Isse ~750 calls ghat kar ~200 reh jaate hain.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from s9_upgrades import (
    LADDER_OFFSETS_MINS,
    TF_LADDER,
    MIN_VOLUME,
    Pivots,
    S9Enhancer,
    daily_pivots,
    entry_gate_ok,
    hour_anchor,
    weekly_pivots,
    week_key,
)

# ----------------------------------------------------------------------------
# PARAMS (scanner ke PARAMS se align)
# ----------------------------------------------------------------------------

BB_PERIOD      = 20
BB_STD         = 2
FLAT_CANDLES   = 4
FLAT_THRESHOLD = 0.002      # 0.2% — S6 wali flat definition

S9_TFS = ["5m", "15m", "30m", "1h", "1d"]

# Daily TF ka confluence check kis segment pe chalega.
# CRYPTO=False kyunki Delta ke daily-expiry options 1-2 din purane hote hain —
# unke paas daily BB(20) ke liye 22 candles kabhi nahi hongi, aur check hamesha
# chupchaap fail karta rehta. NSE/MCX ke option contracts me history hoti hai,
# wahan daily confluence bana rehta hai.
DAILY_TF_REQUIRED = {"NSE": True, "MCX": True, "CRYPTO": False}

# TF ka cache TTL (seconds). 1d ka cache din bhar ka hota hai (alag handle).
TF_CACHE_TTL = {"5m": 0, "15m": 300, "30m": 600, "1h": 900, "1d": 86400}

# Ladder step ka TF naam s9_upgrades ke naam se map
LADDER_TF_TO_DATA_TF = {"5M": "5m", "15M": "15m", "30M": "30m", "1H": "1h"}

# Ladder step exact close pe hi chalti hai — scan cycle thoda late ho sakta hai,
# isliye itne minute ka tolerance.
LADDER_TOLERANCE_MIN = 4


# ----------------------------------------------------------------------------
# INDICATOR HELPERS
# ----------------------------------------------------------------------------

def add_bb(df: pd.DataFrame) -> pd.DataFrame:
    """BB(20,2) columns add karo. Scanner ke calculate_indicators se same formula."""
    df = df.copy()
    df["sma20"]    = df["close"].rolling(BB_PERIOD).mean()
    df["std20"]    = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = df["sma20"] + BB_STD * df["std20"]
    df["bb_lower"] = df["sma20"] - BB_STD * df["std20"]
    return df


def _pct_range(s: pd.Series) -> float:
    m = s.mean()
    if not m:
        return 0.0
    return float((s.max() - s.min()) / m)


def pp_inside_bb(df: pd.DataFrame, pp: float, use_prev_candle: bool = False) -> Optional[bool]:
    """
    Daily PP is TF ke BB ke andar hai ya nahi.

    use_prev_candle=True → blast candle ke pehle wala candle dekho. 5M pe yahi
    chahiye, kyunki blast candle me band already toot chuka hota hai aur uska
    BB check bekaar ho jaata.

    None = data nahi/insufficient (check fail nahi, "pata nahi").
    """
    if df is None or len(df) < BB_PERIOD + 2:
        return None
    d = add_bb(df).dropna()
    if len(d) < 2:
        return None
    row = d.iloc[-2] if use_prev_candle else d.iloc[-1]
    up, lo = float(row["bb_upper"]), float(row["bb_lower"])
    if pd.isna(up) or pd.isna(lo):
        return None
    return bool(lo < pp < up)


def is_bb_flat(df: pd.DataFrame) -> bool:
    """S6 wali flat definition — teeno bands last FLAT_CANDLES me <0.2% hile."""
    if df is None or len(df) < BB_PERIOD + FLAT_CANDLES + 2:
        return False
    d = add_bb(df).dropna()
    if len(d) < FLAT_CANDLES + 1:
        return False
    win = d.iloc[-(FLAT_CANDLES + 1):-1]      # blast candle exclude
    return (_pct_range(win["sma20"])    < FLAT_THRESHOLD and
            _pct_range(win["bb_upper"]) < FLAT_THRESHOLD and
            _pct_range(win["bb_lower"]) < FLAT_THRESHOLD)


def blast_up(df: pd.DataFrame) -> Optional[dict]:
    """Last 5M candle BB upper ke bahar close hua? Blast ka data return karo."""
    if df is None or len(df) < BB_PERIOD + 2:
        return None
    d = add_bb(df).dropna().reset_index(drop=True)
    if len(d) < 2:
        return None
    last = d.iloc[-1]
    if not (float(last["close"]) > float(last["bb_upper"])):
        return None
    return {
        "open":   float(last["open"]),
        "high":   float(last["high"]),
        "low":    float(last["low"]),
        "close":  float(last["close"]),
        "volume": float(last.get("volume", 0) or 0),
        "ts":     last["timestamp"],
        "bb_upper": float(last["bb_upper"]),
    }


# ----------------------------------------------------------------------------
# PREVIOUS DAY / WEEK DATA
# ----------------------------------------------------------------------------

def prev_day_hlcv(df_intraday: pd.DataFrame) -> Optional[Tuple[float, float, float, float]]:
    """
    Intraday candles se PICHHLE DIN ka (High, Low, Close, Volume).
    Volume gate ke liye chahiye — naya/illiquid contract pakadne ko.
    """
    if df_intraday is None or "timestamp" not in df_intraday.columns or len(df_intraday) == 0:
        return None
    d = df_intraday.copy()
    d["_d"] = pd.to_datetime(d["timestamp"]).dt.date
    today = d["_d"].iloc[-1]
    prev = d[d["_d"] < today]
    if len(prev) == 0:
        return None
    last_date = prev["_d"].max()
    day = prev[prev["_d"] == last_date]
    return (float(day["high"].max()), float(day["low"].min()),
            float(day.iloc[-1]["close"]), float(day["volume"].sum()))


def day_high_so_far(df_intraday: pd.DataFrame) -> Optional[float]:
    if df_intraday is None or "timestamp" not in df_intraday.columns or len(df_intraday) == 0:
        return None
    d = df_intraday.copy()
    d["_d"] = pd.to_datetime(d["timestamp"]).dt.date
    today = d["_d"].iloc[-1]
    tdf = d[d["_d"] == today]
    if len(tdf) <= 1:
        return None
    return float(tdf.iloc[:-1]["high"].max())   # current candle exclude


def prev_week_hlc_from_daily(df_daily: pd.DataFrame) -> Optional[Tuple[float, float, float]]:
    """Daily candles se PICHHLE ISO HAFTE ka (H, L, C) — weekly PP ke liye."""
    if df_daily is None or "timestamp" not in df_daily.columns or len(df_daily) == 0:
        return None
    d = df_daily.copy()
    ts = pd.to_datetime(d["timestamp"])
    iso = ts.dt.isocalendar()
    d["_y"], d["_w"] = iso.year.values, iso.week.values
    cy, cw = d["_y"].iloc[-1], d["_w"].iloc[-1]
    prev = d[(d["_y"] < cy) | ((d["_y"] == cy) & (d["_w"] < cw))]
    if len(prev) == 0:
        return None
    ly, lw = prev["_y"].iloc[-1], prev["_w"].iloc[-1]
    wk = prev[(prev["_y"] == ly) & (prev["_w"] == lw)]
    return (float(wk["high"].max()), float(wk["low"].min()), float(wk.iloc[-1]["close"]))


# ----------------------------------------------------------------------------
# ENGINE
# ----------------------------------------------------------------------------

class S9Engine:
    """
    Ek hi jagah: data cache + detection + s9_upgrades ka state.

    send_fn(message) — Telegram sender
    log             — logger (optional)
    weekly_required — segment-wise; NSE me weekly data na ho to instrument BLOCK
    """

    def __init__(self, send_fn: Callable[[str], None], log=None,
                 weekly_required: Optional[Dict[str, bool]] = None):
        self.send = send_fn
        self.log = log
        self.enh = S9Enhancer()
        self.weekly_required = weekly_required or {"NSE": True, "MCX": False, "CRYPTO": False}

        self._tf_cache: Dict[Tuple[str, str], Tuple[float, pd.DataFrame]] = {}
        self._daily_cache: Dict[str, Tuple[object, pd.DataFrame]] = {}      # sym -> (date, df)
        self._weekly_pp: Dict[str, Tuple[tuple, Optional[float]]] = {}      # sym -> (week_key, pp)
        self.stats = {"scanned": 0, "blasts": 0, "upgrades": 0, "ladder": 0, "gated": 0}

    # -- logging -----------------------------------------------------------

    def _log(self, level: str, msg: str) -> None:
        if self.log:
            getattr(self.log, level, self.log.info)(msg)

    # -- data --------------------------------------------------------------

    def get_df(self, sym: str, tf: str, fetch_fn: Callable, now: datetime) -> Optional[pd.DataFrame]:
        """TTL cache ke saath candles."""
        if tf == "1d":
            cached = self._daily_cache.get(sym)
            if cached and cached[0] == now.date():
                return cached[1]
            df = fetch_fn(sym, "1d")
            if df is not None:
                self._daily_cache[sym] = (now.date(), df)
            return df

        key = (sym, tf)
        ttl = TF_CACHE_TTL.get(tf, 300)
        hit = self._tf_cache.get(key)
        if hit and ttl and (_time.time() - hit[0]) < ttl:
            return hit[1]
        df = fetch_fn(sym, tf)
        if df is not None:
            self._tf_cache[key] = (_time.time(), df)
        return df

    def get_weekly_pp(self, sym: str, df_daily: Optional[pd.DataFrame],
                      now: datetime) -> Optional[float]:
        """Weekly PP — har Monday apne aap refresh (ISO week key badalte hi)."""
        wk = week_key(now)
        hit = self._weekly_pp.get(sym)
        if hit and hit[0] == wk:
            return hit[1]
        hlc = prev_week_hlc_from_daily(df_daily) if df_daily is not None else None
        pp = weekly_pivots(*hlc)["PP"] if hlc else None
        self._weekly_pp[sym] = (wk, pp)
        return pp

    # -- lifecycle ---------------------------------------------------------

    def new_day_check(self, now: datetime) -> None:
        """Din badla to blacklist + ladder + intraday cache saaf."""
        if self.enh.maybe_reset_day(now):
            self._tf_cache.clear()
            self._daily_cache.clear()
            self.stats = {k: 0 for k in self.stats}
            self._log("info", "[S9] Naya din — state reset")

    # -- main check --------------------------------------------------------

    def check(self, sym: str, fetch_fn: Callable, segment: str,
              now: Optional[datetime] = None) -> List[str]:
        """
        Ek instrument (option chart) pe pura S9 pass.
        Returns bheje gaye Telegram messages ki list.
        """
        now = now or datetime.now()
        sent: List[str] = []
        self.stats["scanned"] += 1

        df5 = self.get_df(sym, "5m", fetch_fn, now)
        if df5 is None or len(df5) < BB_PERIOD + 5:
            return sent

        # ── Pivots ────────────────────────────────────────────────────
        pdhlcv = prev_day_hlcv(df5)
        if pdhlcv is None:
            self._log("info", f"[S9] {sym}: prev-day data nahi — skip")
            return sent
        pd_high, pd_low, pd_close, pd_vol = pdhlcv

        df1d = self.get_df(sym, "1d", fetch_fn, now)
        wpp = self.get_weekly_pp(sym, df1d, now)
        if wpp is None and self.weekly_required.get(segment, False):
            self._log("info", f"[S9] {sym}: weekly data nahi ({segment}) — BLOCK")
            return sent

        d = daily_pivots(pd_high, pd_low, pd_close)
        pivots = Pivots(daily_pp=d["PP"], daily_r1=d["R1"], daily_r2=d["R2"],
                        daily_r3=d["R3"], weekly_pp=wpp)
        pp = d["PP"]

        st_existing = self.enh.states.get(sym)

        # ── Ladder (blast ke baad, blacklist se EXEMPT) ───────────────
        if st_existing is not None:
            msg = self._ladder_step(sym, fetch_fn, pp, now)
            if msg:
                self.send(msg)
                sent.append(msg)
                self.stats["ladder"] += 1

        # ── Star upgrade (blast ho chuka ho to) ───────────────────────
        if st_existing is not None and not st_existing.blacklisted:
            last_close = float(add_bb(df5).dropna().iloc[-1]["close"])
            ts5 = pd.to_datetime(df5.iloc[-1]["timestamp"]).to_pydatetime().replace(tzinfo=None)
            msg = self.enh.on_candle_close(sym, ts5, last_close)
            if msg:
                self.send(msg)
                sent.append(msg)
                self.stats["upgrades"] += 1

        if self.enh.is_blacklisted(sym) or st_existing is not None:
            return sent      # ek din me ek hi blast — dobara blast nahi dhundhna

        # ── BLAST detection ───────────────────────────────────────────
        b = blast_up(df5)
        if b is None:
            return sent

        # PP sab TF pe BB ke andar? (5M pe pre-blast candle dekho)
        if pp_inside_bb(df5, pp, use_prev_candle=True) is not True:
            return sent
        higher_tfs = ["15m", "30m", "1h"]
        if DAILY_TF_REQUIRED.get(segment, True):
            higher_tfs.append("1d")
        for tf in higher_tfs:
            dft = self.get_df(sym, tf, fetch_fn, now)
            if pp_inside_bb(dft, pp) is not True:
                return sent

        # ── ENTRY GATE ────────────────────────────────────────────────
        ts = pd.to_datetime(b["ts"]).to_pydatetime().replace(tzinfo=None)
        ok, why = entry_gate_ok(
            ts=ts,
            candle_open=b["open"], candle_close=b["close"], candle_high=b["high"],
            prev_day_high=pd_high, prev_day_volume=pd_vol,
            day_high_so_far=day_high_so_far(df5),
            segment=segment,
        )
        if not ok:
            self.stats["gated"] += 1
            self._log("info", f"[S9] {sym}: gate FAIL — {why}")
            return sent

        # ── JACKPOT — flat BB (15M ya 1H) ─────────────────────────────
        flat = is_bb_flat(self.get_df(sym, "15m", fetch_fn, now)) or \
               is_bb_flat(self.get_df(sym, "1h", fetch_fn, now))

        msg = self.enh.on_blast(sym, ts, b["close"], pivots,
                                flat_bb=flat, segment=segment)
        if msg:
            self.send(msg)
            sent.append(msg)
            self.stats["blasts"] += 1
            self._log("info", f"[S9] ✅ BLAST {sym} @ {b['close']} | {why}")
        return sent

    # -- ladder ------------------------------------------------------------

    def _due_ladder_tf(self, now: datetime, segment: str) -> Optional[str]:
        """
        Abhi kaunsi ladder step due hai? Har hourly candle ke open se
        +5 / +15 / +30 / +60 minute.
        """
        anchor = hour_anchor(now, segment)
        for tf in TF_LADDER:
            step = anchor + timedelta(minutes=LADDER_OFFSETS_MINS[tf])
            if 0 <= (now - step).total_seconds() / 60.0 <= LADDER_TOLERANCE_MIN:
                return tf
        return None

    def _ladder_step(self, sym: str, fetch_fn: Callable, pp: float,
                     now: datetime) -> Optional[str]:
        st = self.enh.states.get(sym)
        if st is None:
            return None
        tf = self._due_ladder_tf(now, st.segment)
        if tf is None:
            return None

        data_tf = LADDER_TF_TO_DATA_TF[tf]
        df = self.get_df(sym, data_tf, fetch_fn, now)
        if df is None:
            return None
        # Us TF pe confirmation = PP band ke andar + candle BB upper ke bahar close
        if pp_inside_bb(df, pp, use_prev_candle=True) is not True:
            return None
        if blast_up(df) is None:
            return None

        anchor = hour_anchor(now, st.segment)
        step_ts = anchor + timedelta(minutes=LADDER_OFFSETS_MINS[tf])
        return self.enh.on_tf_confirm(sym, tf, step_ts)

    # -- summary -----------------------------------------------------------

    def summary(self) -> str:
        s = self.stats
        return (f"scanned={s['scanned']} blasts={s['blasts']} "
                f"upgrades={s['upgrades']} ladder={s['ladder']} gated={s['gated']}")
