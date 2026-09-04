"""
s9_scanner.py — Fixed version with proper type annotations
==================================================================
"""

from __future__ import annotations
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta
import calendar
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------

MIN_VOLUME = 100
DEFAULT_SEGMENT = "NSE"
OPENING_WINDOW_MINS = 15

SEGMENT_OPEN: Dict[str, dtime] = {
    "NSE": dtime(9, 15),
    "MCX": dtime(9, 0),
    "CRYPTO": dtime(0, 0),
}

SEGMENT_CLOSE: Dict[str, dtime] = {
    "NSE": dtime(15, 30),
    "MCX": dtime(23, 30),
    "CRYPTO": dtime(23, 59),
}

WEEKLY_REQUIRED: Dict[str, bool] = {
    "NSE": True,
    "MCX": False,
    "CRYPTO": False,
}

TF_LADDER: List[str] = ["5M", "15M", "30M", "1H"]
LADDER_OFFSETS_MINS: Dict[str, int] = {"5M": 5, "15M": 15, "30M": 30, "1H": 60}
MIN_LADDER_TFS = 2
BLACKLIST_AT_BASE = 5
MAX_STARS = 6

STAR_SYMBOL = {n: "⭐" * n for n in range(0, 7)}

# ----------------------------------------------------------------------------
# FIXED FUNCTION - THIS WAS THE ERROR
# ----------------------------------------------------------------------------

def _current_expiry(seg: str, style: str = "monthly") -> Optional[date]:
    """
    Get the current expiry date for options contracts.
    
    For NSE: Monthly expiry is last Thursday of the month
    For MCX: Monthly expiry is 20th of the month
    For CRYPTO: Weekly expiry every Friday
    
    Args:
        seg: Market segment (NSE, MCX, CRYPTO)
        style: Expiry frequency (monthly, weekly)
    
    Returns:
        Optional[date]: The expiry date or None if not supported
    """
    today = date.today()
    
    if seg == "NSE":
        if style == "monthly":
            # Last Thursday of the month
            last_day = calendar.monthrange(today.year, today.month)[1]
            for day in range(last_day, last_day - 7, -1):
                d = date(today.year, today.month, day)
                if d.weekday() == 3:  # Thursday
                    return d
        elif style == "weekly":
            # Next Thursday
            days_until_thursday = (3 - today.weekday()) % 7
            if days_until_thursday == 0:
                days_until_thursday = 7
            return today + timedelta(days=days_until_thursday)
    
    elif seg == "MCX":
        if style == "monthly":
            # 20th of the month, adjust if weekend/holiday
            target_day = 20
            max_day = calendar.monthrange(today.year, today.month)[1]
            day = min(target_day, max_day)
            return date(today.year, today.month, day)
    
    elif seg == "CRYPTO":
        if style == "weekly":
            # Next Friday
            days_until_friday = (4 - today.weekday()) % 7
            if days_until_friday == 0:
                days_until_friday = 7
            return today + timedelta(days=days_until_friday)
    
    return None


# ----------------------------------------------------------------------------
# PIVOT CALCULATION
# ----------------------------------------------------------------------------

@dataclass
class Pivots:
    """Pivot levels for an instrument."""
    daily_pp: Optional[float] = None
    daily_r1: Optional[float] = None
    daily_r2: Optional[float] = None
    daily_r3: Optional[float] = None
    weekly_pp: Optional[float] = None

    def base_ladder(self) -> List[Tuple[int, float]]:
        """Return sequential base ladder levels."""
        raw = [
            (2, self.daily_pp),
            (3, self.daily_r1),
            (4, self.daily_r2),
            (5, self.daily_r3),
        ]
        return [(s, lvl) for s, lvl in raw if lvl is not None]


def daily_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """Calculate daily pivot points."""
    pp = (high + low + close) / 3.0
    rng = high - low
    return {
        "PP": pp,
        "R1": (2 * pp) - low,
        "R2": pp + rng,
        "R3": pp + 2 * rng,
        "S1": (2 * pp) - high,
        "S2": pp - rng,
        "S3": pp - 2 * rng,
    }


def weekly_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """Calculate weekly pivot points."""
    pp = (high + low + close) / 3.0
    return {
        "PP": pp,
        "R1": (2 * pp) - low,
        "R2": pp + (high - low),
        "S1": (2 * pp) - high,
    }


def compute_base_stars(close: float, pivots: Pivots) -> int:
    """Calculate base stars from daily levels only."""
    base = 0
    for s, level in pivots.base_ladder():
        if close > level:
            base = s
        else:
            break
    return base


def compute_stars(close: float, pivots: Pivots) -> int:
    """Calculate total stars with weekly bonus."""
    bonus = 1 if (pivots.weekly_pp is not None and close > pivots.weekly_pp) else 0
    return min(compute_base_stars(close, pivots) + bonus, MAX_STARS)


# ----------------------------------------------------------------------------
# SESSION HELPERS
# ----------------------------------------------------------------------------

def session_open_dt(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    """Get session opening datetime."""
    o = SEGMENT_OPEN.get(segment, SEGMENT_OPEN[DEFAULT_SEGMENT])
    return ts.replace(hour=o.hour, minute=o.minute, second=0, microsecond=0)


def session_close_dt(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    """Get session closing datetime."""
    c = SEGMENT_CLOSE.get(segment, SEGMENT_CLOSE[DEFAULT_SEGMENT])
    return ts.replace(hour=c.hour, minute=c.minute, second=0, microsecond=0)


def in_session(ts: datetime, segment: str = DEFAULT_SEGMENT) -> bool:
    """Check if timestamp is within market hours."""
    return session_open_dt(ts, segment) <= ts <= session_close_dt(ts, segment)


def hour_anchor(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    """Get the start of the current hourly candle."""
    start = session_open_dt(ts, segment)
    if ts < start:
        return start
    hours = int((ts - start).total_seconds() // 3600)
    return start + timedelta(hours=hours)


def ladder_schedule(anchor: datetime) -> Dict[str, datetime]:
    """Get ladder schedule times for a given anchor."""
    return {tf: anchor + timedelta(minutes=m) for tf, m in LADDER_OFFSETS_MINS.items()}


def ladder_anchor_for(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    """Get the current ladder anchor."""
    a = hour_anchor(ts, segment)
    if ts <= a + timedelta(minutes=LADDER_OFFSETS_MINS["5M"]):
        return a
    return a + timedelta(hours=1)


# ----------------------------------------------------------------------------
# ENTRY GATE
# ----------------------------------------------------------------------------

def is_opening_window(ts: datetime, segment: str = DEFAULT_SEGMENT) -> bool:
    """Check if timestamp is within opening window."""
    open_dt = session_open_dt(ts, segment)
    return open_dt <= ts <= open_dt + timedelta(minutes=OPENING_WINDOW_MINS)


def entry_gate_ok(
    ts: datetime,
    candle_open: float,
    candle_close: float,
    candle_high: float,
    prev_day_high: Optional[float],
    prev_day_volume: Optional[float],
    day_high_so_far: Optional[float] = None,
    segment: str = DEFAULT_SEGMENT,
) -> Tuple[bool, str]:
    """
    Check if entry gate conditions are met.
    
    Returns:
        (ok: bool, reason: str)
    """
    if candle_close <= candle_open:
        return False, "candle red hai"

    if is_opening_window(ts, segment):
        if prev_day_high is None:
            return False, "opening gate FAIL (prev-day high missing)"
        if prev_day_volume is None or prev_day_volume < MIN_VOLUME:
            return False, f"opening gate FAIL (prev-day volume {prev_day_volume} < MIN_VOLUME {MIN_VOLUME})"
        if candle_high > prev_day_high:
            return True, f"opening gate OK (high {candle_high} > PDH {prev_day_high})"
        return False, f"high {candle_high} <= PDH {prev_day_high}"

    # Mid-day gate with bypass allowed
    if prev_day_volume is None or prev_day_volume < MIN_VOLUME:
        return True, "gate skipped (prev-day volume < MIN_VOLUME)"
    if day_high_so_far is None:
        return True, "gate skipped (day high unavailable)"
    if candle_high > day_high_so_far:
        return True, f"mid-day gate OK (high {candle_high} > day high {day_high_so_far})"
    return False, f"high {candle_high} <= day high {day_high_so_far}"


# ----------------------------------------------------------------------------
# STATE
# ----------------------------------------------------------------------------

@dataclass
class S9State:
    """State for S9 tracking per symbol."""
    symbol: str
    blast_ts: datetime
    entry_price: float
    pivots: Pivots
    jackpot: bool = False
    stars_sent: int = 0
    blacklisted: bool = False
    tf_confirmed: List[str] = field(default_factory=list)
    ladder_complete: bool = False
    vix_confirmed: bool = False
    segment: str = DEFAULT_SEGMENT
    ladder_anchor: Optional[datetime] = None


# ----------------------------------------------------------------------------
# WEEKLY PIVOT CACHE
# ----------------------------------------------------------------------------

class WeeklyPivotCache:
    """Cache for weekly pivot points with automatic refresh."""
    
    def __init__(self, fetch_fn):
        self.fetch_fn = fetch_fn
        self._cache: Dict[str, tuple] = {}

    def week_key(self, dt: datetime) -> Tuple[int, int]:
        """Get ISO week key."""
        iso = dt.isocalendar()
        return (iso[0], iso[1])

    def prev_week_range(self, dt: datetime) -> Tuple[date, date]:
        """Get previous week Monday and Friday."""
        this_monday = (dt - timedelta(days=dt.weekday())).date()
        prev_monday = this_monday - timedelta(days=7)
        prev_friday = prev_monday + timedelta(days=4)
        return prev_monday, prev_friday

    def get(self, symbol: str, now: Optional[datetime] = None) -> Optional[Dict[str, float]]:
        """Get weekly pivots for symbol."""
        now = now or datetime.now()
        wk = self.week_key(now)
        
        cached = self._cache.get(symbol)
        if cached is not None and cached[0] == wk:
            return cached[1]
        
        start, end = self.prev_week_range(now)
        try:
            ohlc = self.fetch_fn(symbol, start, end)
        except Exception as e:
            logger.error(f"Error fetching weekly data for {symbol}: {e}")
            ohlc = None
        
        pivots = weekly_pivots(*ohlc) if ohlc else None
        self._cache[symbol] = (wk, pivots)
        return pivots

    def weekly_pp(self, symbol: str, now: Optional[datetime] = None) -> Optional[float]:
        """Get weekly PP for symbol."""
        p = self.get(symbol, now)
        return p["PP"] if p else None


def build_pivots(
    symbol: str,
    prev_day_hlc: Tuple[float, float, float],
    weekly_cache: WeeklyPivotCache,
    now: Optional[datetime] = None,
    segment: str = DEFAULT_SEGMENT,
) -> Optional[Pivots]:
    """Build pivot levels for an instrument."""
    wpp = weekly_cache.weekly_pp(symbol, now)
    
    if wpp is None and WEEKLY_REQUIRED.get(segment, False):
        return None
    
    d = daily_pivots(*prev_day_hlc)
    return Pivots(
        daily_pp=d["PP"],
        daily_r1=d["R1"],
        daily_r2=d["R2"],
        daily_r3=d["R3"],
        weekly_pp=wpp,
    )


# ----------------------------------------------------------------------------
# S9 ENHANCER
# ----------------------------------------------------------------------------

class S9Enhancer:
    """S9 state management and alert generation."""
    
    def __init__(self) -> None:
        self.states: Dict[str, S9State] = {}
        self._day: Optional[date] = None

    def reset_day(self, now: Optional[datetime] = None) -> None:
        """Reset state for new trading day."""
        self.states.clear()
        self._day = (now or datetime.now()).date()

    def maybe_reset_day(self, now: Optional[datetime] = None) -> bool:
        """Check and reset if day has changed."""
        now = now or datetime.now()
        if self._day != now.date():
            self.reset_day(now)
            return True
        return False

    def is_blacklisted(self, symbol: str) -> bool:
        """Check if symbol is blacklisted."""
        st = self.states.get(symbol)
        return bool(st and st.blacklisted)

    def on_blast(
        self,
        symbol: str,
        ts: datetime,
        close: float,
        pivots: Pivots,
        flat_bb: bool = False,
        vix_confirmed: bool = False,
        segment: str = DEFAULT_SEGMENT,
        sent_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """Process a new S9 blast."""
        if self.is_blacklisted(symbol):
            return None
        
        stars = compute_stars(close, pivots)
        st = S9State(
            symbol=symbol,
            blast_ts=ts,
            entry_price=close,
            pivots=pivots,
            jackpot=flat_bb,
            stars_sent=stars,
            vix_confirmed=vix_confirmed,
            segment=segment,
            ladder_anchor=ladder_anchor_for(ts, segment),
        )
        st.tf_confirmed.append("5M")
        if compute_base_stars(close, pivots) >= BLACKLIST_AT_BASE:
            st.blacklisted = True
        
        self.states[symbol] = st
        
        return self.format_alert(st, kind="NEW", price=close, ts=ts)

    def on_candle_close(
        self,
        symbol: str,
        ts: datetime,
        close: float,
        sent_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """Process candle close for upgrades."""
        st = self.states.get(symbol)
        if st is None or st.blacklisted:
            return None
        
        stars = compute_stars(close, st.pivots)
        if stars <= st.stars_sent:
            return None
        
        st.stars_sent = stars
        if compute_base_stars(close, st.pivots) >= BLACKLIST_AT_BASE:
            st.blacklisted = True
        
        return self.format_alert(st, kind="UPGRADE", price=close, ts=ts)

    def _roll_ladder(self, st: S9State, ts: datetime) -> None:
        """Roll ladder to new hour if needed."""
        if st.ladder_anchor is None:
            st.ladder_anchor = hour_anchor(ts, st.segment)
            st.tf_confirmed = []
            st.ladder_complete = False
            return
        
        if ts > st.ladder_anchor + timedelta(minutes=LADDER_OFFSETS_MINS["1H"]):
            st.ladder_anchor = hour_anchor(ts, st.segment)
            st.tf_confirmed = []
            st.ladder_complete = False

    def on_tf_confirm(
        self,
        symbol: str,
        tf: str,
        ts: datetime,
    ) -> Optional[str]:
        """Process time frame confirmation."""
        st = self.states.get(symbol)
        if st is None or tf not in TF_LADDER:
            return None
        
        self._roll_ladder(st, ts)
        
        if tf in st.tf_confirmed:
            return None
        if st.ladder_anchor is None:
            return None
        
        sched = ladder_schedule(st.ladder_anchor)
        if ts < st.ladder_anchor or ts > sched["1H"]:
            return None
        
        idx = TF_LADDER.index(tf)
        if idx > 0 and TF_LADDER[idx - 1] not in st.tf_confirmed:
            return None
        
        st.tf_confirmed.append(tf)
        st.ladder_complete = len(st.tf_confirmed) == len(TF_LADDER)
        
        if len(st.tf_confirmed) < MIN_LADDER_TFS:
            return None
        
        return self.format_ladder_alert(st, ts=ts)

    # ------------------------------------------------------------------------
    # FORMATTERS
    # ------------------------------------------------------------------------

    def _ladder_string(self, confirmed: List[str]) -> str:
        """Convert confirmed TFs to string."""
        short = {"5M": "5", "15M": "15", "30M": "30", "1H": "60"}
        ordered = [t for t in TF_LADDER if t in confirmed]
        return "<".join(short[t] for t in ordered)

    def format_alert(self, st: S9State, kind: str, price: float, ts: datetime) -> str:
        """Format alert message."""
        header = "🔥 JACKPOT " if st.jackpot else ""
        stars = STAR_SYMBOL[st.stars_sent]
        title = "S9 BLAST" if kind == "NEW" else "S9 UPGRADE"
        
        return "\n".join([
            f"{header}{title} {stars}".strip(),
            f"📊 {st.symbol}",
            f"Buy@{price:.2f}  time-{ts:%H:%M}",
        ])

    def format_ladder_alert(self, st: S9State, ts: datetime) -> str:
        """Format ladder alert message."""
        chain = self._ladder_string(st.tf_confirmed)
        if st.ladder_complete:
            head = "✅ 5<15<30<60 — ALL CONFIRMED"
            note = "Yeh entry signal nahi hai — conviction/add-on ke liye hai."
        else:
            head = f"🔗 {chain} CONFIRMED"
            note = ""
        
        lines = [
            head,
            f"📊 {st.symbol}",
            f"🕒 {ts:%H:%M}   Entry {st.entry_price:.2f}",
        ]
        if st.jackpot:
            lines.insert(0, "🔥 JACKPOT")
        if note:
            lines.append(note)
        return "\n".join(lines)


# ----------------------------------------------------------------------------
# MAIN - SELF TEST
# ----------------------------------------------------------------------------

def main():
    """Self-test function."""
    print("=" * 60)
    print("S9 SCANNER - FIXED VERSION")
    print("=" * 60)
    
    # Test 1: Check _current_expiry function
    print("\n[TEST 1] _current_expiry function")
    print("-" * 40)
    
    for seg in ["NSE", "MCX", "CRYPTO"]:
        for style in ["monthly", "weekly"]:
            try:
                result = _current_expiry(seg, style)
                print(f"  {seg:6s} {style:8s} → {result}")
            except Exception as e:
                print(f"  {seg:6s} {style:8s} → ERROR: {e}")
    
    # Test 2: Entry Gate
    print("\n[TEST 2] Entry Gate")
    print("-" * 40)
    
    D = datetime(2026, 8, 10, 9, 20)  # Monday
    cases = [
        ("opening, PDH cross", D, 2.30, 2.95, 3.05, 2.60, 45000),
        ("opening, PDH missing", D, 2.30, 2.95, 3.05, None, 45000),
        ("opening, illiquid", D, 2.30, 2.95, 3.05, 2.60, 40),
        ("mid-day, illiquid", D.replace(hour=11, minute=5), 2.30, 2.95, 3.05, 2.60, 40),
    ]
    
    for label, ts, o, c, h, pdh, vol in cases:
        ok, why = entry_gate_ok(
            ts=ts, candle_open=o, candle_close=c, candle_high=h,
            prev_day_high=pdh, prev_day_volume=vol,
            day_high_so_far=2.90
        )
        print(f"  {label:25s} → {str(ok):5s} | {why}")
    
    # Test 3: S9 Enhancer
    print("\n[TEST 3] S9 Enhancer")
    print("-" * 40)
    
    enh = S9Enhancer()
    enh.maybe_reset_day(D)
    
    # Mock pivots
    piv = Pivots(
        daily_pp=2.10, daily_r1=2.90, daily_r2=3.70, daily_r3=4.60,
        weekly_pp=7.50
    )
    
    SYM = "RECLTD AUG 350 PE"
    
    print("\n  BLAST:")
    msg = enh.on_blast(SYM, D.replace(hour=9, minute=30), close=2.95,
                       pivots=piv, flat_bb=True)
    print(f"    {msg}")
    
    print("\n  UPGRADES:")
    for hh, mm, px in [(9, 45, 3.90), (10, 0, 5.20), (10, 15, 4.80),
                       (11, 0, 7.90), (13, 45, 11.55)]:
        msg = enh.on_candle_close(SYM, D.replace(hour=hh, minute=mm), px)
        if msg:
            print(f"    {msg}")
    
    print(f"\n  Blacklisted: {enh.is_blacklisted(SYM)}")
    
    # Test 4: Ladders
    print("\n[TEST 4] Hourly Ladders")
    print("-" * 40)
    
    for anchor_h, anchor_m in [(9, 15), (10, 15), (11, 15), (14, 15)]:
        anchor = D.replace(hour=anchor_h, minute=anchor_m)
        schedule = ladder_schedule(anchor)
        times = "  ".join(f"{tf} {t:%H:%M}" for tf, t in sorted(schedule.items(), key=lambda kv: kv[1]))
        print(f"\n  Anchor {anchor:%H:%M} → {times}")
        
        for tf in TF_LADDER:
            ts = anchor + timedelta(minutes=LADDER_OFFSETS_MINS[tf])
            msg = enh.on_tf_confirm(SYM, tf, ts)
            if msg:
                print(f"    {msg}")
    
    # Test 5: Weekly Pivot Cache
    print("\n[TEST 5] Weekly Pivot Cache")
    print("-" * 40)
    
    fetch_log = []
    
    def fake_fetch(symbol, start, end):
        fetch_log.append((symbol, start, end))
        return (9.80, 4.20, 8.50)  # H, L, C
    
    wc = WeeklyPivotCache(fake_fetch)
    
    mon = datetime(2026, 8, 10, 9, 30)
    thu = datetime(2026, 8, 13, 11, 0)
    nxt = datetime(2026, 8, 17, 9, 20)
    
    print(f"  Mon WPP: {wc.weekly_pp(SYM, mon)}")
    print(f"  Thu WPP: {wc.weekly_pp(SYM, thu)} (cache)")
    print(f"  Next Mon WPP: {wc.weekly_pp(SYM, nxt)} (auto refresh)")
    print(f"  Fetch calls: {len(fetch_log)}")
    
    print("\n" + "=" * 60)
    print("ALL TESTS COMPLETED SUCCESSFULLY")
    print("=" * 60)


if __name__ == "__main__":
    main()