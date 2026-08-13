"""
s9_upgrades.py — S9 Pivot Confluence Blast ke enhancement modules
==================================================================

1. STAR LADDER (additive)  — base = daily levels, strictly sequential
                             ⭐⭐    close > Daily PP
                             ⭐⭐⭐   close > Daily R1
                             ⭐⭐⭐⭐  close > Daily R2
                             ⭐⭐⭐⭐⭐ close > Daily R3   → day blacklist
                             weekly PP cross = +1 star (har level pe, max 6)
                             🔥 JACKPOT = flat BB pre-breakout (alag tag, star nahi)

2. UPGRADE TRACKER         — blast ke baad har candle close pe star recheck.
                             Star badhe → upgraded alert. Base R3 pe alert band.

3. TF CONFIRMATION LADDER  — HAR HOURLY CANDLE ke opening pe NAYA ladder.
                             anchor+5M → anchor+15M → anchor+30M → anchor+60M
                             NSE: 9:15 → 9:20/9:30/9:45/10:15
                                  10:15 → 10:20/10:30/10:45/11:15
                                  11:15 → 11:20/11:30/11:45/12:15 ... poora din
                             Alert kam se kam 2 TF pe (5<15 se shuru).
                             Ladder alerts day-blacklist se EXEMPT.

4. ENTRY GATE              — opening window: candle green + high > previous day high
                             (STRICT — data missing ho to FAIL, koi bypass nahi)
                             mid-day: candle green + high > day's high so far
                             (yahan bypass allowed — naye/illiquid contracts)

5. PIVOTS + WEEKLY CACHE   — Kite convention (R3 = PP + 2×(H−L)),
                             weekly PP har Monday auto-refresh.

Integration (tps_unified_scanner.py mein):

    from s9_upgrades import S9Enhancer, entry_gate_ok, build_pivots, WeeklyPivotCache

    enh    = S9Enhancer()
    wcache = WeeklyPivotCache(fetch_weekly_ohlc)
    ...
    enh.maybe_reset_day(now)                 # har scan loop ke top pe (sasta hai)
    ...
    ok, why = entry_gate_ok(...)             # blast confirm karne se pehle
    if ok:
        msg = enh.on_blast(...)
        if msg: send_telegram(msg)
    ...
    msg = enh.on_candle_close(...)           # har 5M close pe
    if msg: send_telegram(msg)
    ...
    msg = enh.on_tf_confirm(sym, "15M", ts)  # us TF ka setup confirm hone pe
    if msg: send_telegram(msg)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

# MIN_VOLUME scanner ke config se aata hai — yahan dubara define karke drift
# nahi banane dena. config import na ho paye (standalone test) to fallback.
try:
    from config import MIN_VOLUME  # type: ignore
except Exception:  # pragma: no cover
    MIN_VOLUME = 100

# Segment-wise market open/close. MCX 9:00 pe khulta hai aur raat 23:30 tak chalta
# hai, isliye NSE ka 9:15 hardcode karna MCX ke liye galat hota.
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
DEFAULT_SEGMENT = "NSE"
OPENING_WINDOW_MINS = 15   # open ke baad itne minute tak "opening setup" maana jayega

# Ladder HOUR-ALIGNED hai aur HAR GHANTE naya banta hai. Jis hourly candle ke
# andar hain, usi ke start se +5 / +15 / +30 / +60 minute pe steps.
LADDER_OFFSETS_MINS: Dict[str, int] = {"5M": 5, "15M": 15, "30M": 30, "1H": 60}
TF_LADDER: List[str] = ["5M", "15M", "30M", "1H"]

# Akela "5M CONFIRMED" alert nahi jaayega — kam se kam 5<15.
MIN_LADDER_TFS = 2

# Weekly data mandatory hai ya nahi — segment ke hisaab se.
# NSE: OTM strike ka pichhle hafte ka data na ho to instrument BLOCK.
# Crypto/MCX: weekly na ho to daily pivots se kaam chalega.
WEEKLY_REQUIRED: Dict[str, bool] = {
    "NSE": True,
    "MCX": False,
    "CRYPTO": False,
}

STAR_SYMBOL = {n: "⭐" * n for n in range(0, 7)}
MAX_STARS = 6              # daily R3 (5) + weekly PP bonus (1)
BLACKLIST_AT_BASE = 5      # base score (R3 cross) — weekly bonus isme nahi ginta


# ----------------------------------------------------------------------------
# SESSION / HOUR HELPERS
# ----------------------------------------------------------------------------

def session_open_dt(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    o = SEGMENT_OPEN.get(segment, SEGMENT_OPEN[DEFAULT_SEGMENT])
    return ts.replace(hour=o.hour, minute=o.minute, second=0, microsecond=0)


def session_close_dt(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    c = SEGMENT_CLOSE.get(segment, SEGMENT_CLOSE[DEFAULT_SEGMENT])
    return ts.replace(hour=c.hour, minute=c.minute, second=0, microsecond=0)


def in_session(ts: datetime, segment: str = DEFAULT_SEGMENT) -> bool:
    return session_open_dt(ts, segment) <= ts <= session_close_dt(ts, segment)


def hour_anchor(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    """
    Jis hourly candle ke andar ts hai, uska start.
    Hourly candles session open se count hote hain — NSE: 9:15, 10:15, 11:15...
    MCX: 9:00, 10:00, 11:00...   Isliye clock ke :00 pe nahi, open pe align karte hain.
    """
    start = session_open_dt(ts, segment)
    if ts < start:
        return start
    hours = int((ts - start).total_seconds() // 3600)
    return start + timedelta(hours=hours)


def ladder_schedule(anchor: datetime) -> Dict[str, datetime]:
    """Us ghante ke 5M / 15M / 30M / 1H closes."""
    return {tf: anchor + timedelta(minutes=m) for tf, m in LADDER_OFFSETS_MINS.items()}


def ladder_anchor_for(ts: datetime, segment: str = DEFAULT_SEGMENT) -> datetime:
    """
    Ladder ka anchor.

    Har hourly candle ke opening pe naya ladder khulta hai. Agar hum us ghante ke
    PEHLE 5M close (anchor+5) tak pahunche hain, to usi ghante ka ladder pakad
    sakte hain — jaise 9:20 pe, anchor 9:15.

    Agar beech-e-ghanta hain (jaise 11:05, jo 10:15 wale ghante me padta hai), to
    us ghante ke 5M/15M/30M steps nikal chuke hote hain — to AGLE ghante ka
    ladder milega: 11:15, phir 11:20 / 11:30 / 11:45 / 12:15.
    """
    a = hour_anchor(ts, segment)
    if ts <= a + timedelta(minutes=LADDER_OFFSETS_MINS["5M"]):
        return a
    return a + timedelta(hours=1)


# ----------------------------------------------------------------------------
# 1. STAR LADDER
# ----------------------------------------------------------------------------

@dataclass
class Pivots:
    """Ek instrument ke pivot levels. Daily = previous day, Weekly = previous week."""
    daily_pp: Optional[float] = None
    daily_r1: Optional[float] = None
    daily_r2: Optional[float] = None
    daily_r3: Optional[float] = None
    weekly_pp: Optional[float] = None

    def base_ladder(self) -> List[tuple]:
        """
        Base score sequential hai: PP=2, R1=3, R2=4, R3=5.
        Weekly PP isme nahi — wo additive bonus hai (compute_stars dekhein).
        """
        raw = [
            (2, self.daily_pp),
            (3, self.daily_r1),
            (4, self.daily_r2),
            (5, self.daily_r3),
        ]
        return [(s, lvl) for s, lvl in raw if lvl is not None]


def compute_base_stars(close: float, pivots: Pivots) -> int:
    """Sirf daily levels ka score (0/2/3/4/5) — weekly bonus ke bina."""
    base = 0
    for s, level in pivots.base_ladder():
        if close > level:
            base = s
        else:
            break
    return base


def compute_stars(close: float, pivots: Pivots) -> int:
    """
    ADDITIVE scoring (0-6):

        base  — daily levels, STRICTLY SEQUENTIAL
                PP → ⭐⭐ | R1 → ⭐⭐⭐ | R2 → ⭐⭐⭐⭐ | R3 → ⭐⭐⭐⭐⭐
        bonus — weekly PP cross ho to +1, har level pe

    Blast hua par daily PP bhi cross nahi → 0 star. Alert phir bhi jaata hai
    (0-star practically aana nahi chahiye — aaye to pivot/data mismatch ka signal).

    Blacklist isse NAHI, compute_base_stars() se tay hota hai — yaani R3 cross pe.
    Warna weekly PP agar R2 aur R3 ke beech padi ho (ULTRACEMCO 11760 PE jaisa
    case), to R2+weekly milkar 5 ho jaate aur R3 ke baad ka poora move
    alert-less reh jaata.
    """
    bonus = 1 if (pivots.weekly_pp is not None and close > pivots.weekly_pp) else 0
    return min(compute_base_stars(close, pivots) + bonus, MAX_STARS)


# ----------------------------------------------------------------------------
# 4. ENTRY GATE
# ----------------------------------------------------------------------------

def is_opening_window(ts: datetime, segment: str = DEFAULT_SEGMENT) -> bool:
    """Segment ke open ke pehle OPENING_WINDOW_MINS minute = opening setup."""
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
) -> tuple:
    """
    Returns (ok: bool, reason: str)

    OPENING WINDOW — STRICT, koi bypass nahi:
        candle green ho AUR high > previous day high.
        prev-day high missing YA prev-day volume < MIN_VOLUME → FAIL.
        Maqsad: pehli 2-3 candle ke fake alerts kaatna. Move bada hoga to
        baad me mid-day gate se waise bhi alert aa jayega.

    MID-DAY / EVENING — bypass allowed:
        candle green ho AUR high > day's high so far.
        prev-day volume kam ya day-high unavailable → check skip (naye/illiquid
        contracts ke liye fallback).
    """
    if candle_close <= candle_open:
        return False, "candle red hai"

    if is_opening_window(ts, segment):
        if prev_day_high is None:
            return False, "opening gate FAIL (prev-day high missing — bypass nahi)"
        if prev_day_volume is None or prev_day_volume < MIN_VOLUME:
            return False, (f"opening gate FAIL (prev-day volume {prev_day_volume} "
                           f"< MIN_VOLUME {MIN_VOLUME} — bypass nahi)")
        if candle_high > prev_day_high:
            return True, f"opening gate OK (high {candle_high} > PDH {prev_day_high})"
        return False, f"high {candle_high} <= PDH {prev_day_high}"

    # Mid-day equivalent — yahan fallback zinda hai
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
    symbol: str
    blast_ts: datetime
    entry_price: float
    pivots: Pivots
    jackpot: bool = False              # flat BB pre-breakout
    stars_sent: int = 0                # abhi tak ka highest star bheja gaya
    blacklisted: bool = False          # base R3 hit → alert band
    tf_confirmed: List[str] = field(default_factory=list)
    ladder_complete: bool = False
    vix_confirmed: bool = False
    segment: str = DEFAULT_SEGMENT
    ladder_anchor: Optional[datetime] = None   # CURRENT hourly ladder ka start


# ----------------------------------------------------------------------------
# 2 + 3. ENHANCER
# ----------------------------------------------------------------------------

class S9Enhancer:
    """Per-symbol S9 state — blast, upgrades, aur hourly TF ladder ek jagah."""

    def __init__(self) -> None:
        self.states: Dict[str, S9State] = {}
        self._day: Optional[date] = None

    # -- lifecycle ---------------------------------------------------------

    def reset_day(self, now: Optional[datetime] = None) -> None:
        """Har trading din ke start pe. Blacklist + ladder state saaf."""
        self.states.clear()
        self._day = (now or datetime.now()).date()

    def maybe_reset_day(self, now: Optional[datetime] = None) -> bool:
        """
        Scan loop ke top pe har baar call karein — sasta hai.
        Date badalte hi state clear. Iske bina 24/7 Railway worker me kal ka
        blacklist aaj tak carry ho jaata hai aur subah se koi alert nahi aata.

        (Reset midnight IST pe hota hai. NSE 15:30 aur MCX 23:30 pe band ho jaate
        hain to theek hai; CRYPTO 24/7 hai, uske liye midnight cutoff maana gaya.)
        """
        now = now or datetime.now()
        if self._day != now.date():
            self.reset_day(now)
            return True
        return False

    def is_blacklisted(self, symbol: str) -> bool:
        st = self.states.get(symbol)
        return bool(st and st.blacklisted)

    # -- blast -------------------------------------------------------------

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
        """
        Naya S9 blast mila. Returns Telegram message, ya None agar
        instrument pehle se blacklisted hai.
        """
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
        # 5M blast hai to us ghante ke ladder ka pehla step already confirmed
        st.tf_confirmed.append("5M")
        if compute_base_stars(close, pivots) >= BLACKLIST_AT_BASE:
            st.blacklisted = True
        self.states[symbol] = st

        return format_alert(st, kind="NEW", price=close, ts=ts,
                            sent_at=sent_at or datetime.now())

    # -- upgrade -----------------------------------------------------------

    def on_candle_close(
        self,
        symbol: str,
        ts: datetime,
        close: float,
        sent_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Har candle close pe call karein. Star badha to upgraded alert,
        warna None. Base R3 cross pe instrument blacklist ho jaata hai.
        """
        st = self.states.get(symbol)
        if st is None or st.blacklisted:
            return None

        stars = compute_stars(close, st.pivots)
        if stars <= st.stars_sent:
            return None

        st.stars_sent = stars
        if compute_base_stars(close, st.pivots) >= BLACKLIST_AT_BASE:
            st.blacklisted = True

        return format_alert(st, kind="UPGRADE", price=close, ts=ts,
                            sent_at=sent_at or datetime.now())

    # -- TF ladder ---------------------------------------------------------

    def backfill_5m(
        self,
        symbol: str,
        met_first_5m: bool = False,
        met_second_5m: bool = False,
    ) -> None:
        """
        Ladder ke ghante ke pehle 5M close pe alag scan pass nahi chalta.
        Agle scan me retroactively mark karein — anchor+5 (jaise 9:15-9:20) YA
        anchor+10 (9:20-9:25) me se koi bhi 5M candle condition puri kare to
        "5M" confirmed. Uske baad anchor+15 wali 15M candle se ladder aage badhta hai.
        """
        st = self.states.get(symbol)
        if st is None:
            return
        met = met_first_5m or met_second_5m
        if met and "5M" not in st.tf_confirmed:
            st.tf_confirmed.insert(0, "5M")
        elif not met and "5M" in st.tf_confirmed:
            st.tf_confirmed.remove("5M")

    def ladder_times(self, symbol: str) -> Optional[Dict[str, datetime]]:
        """Us instrument ke CURRENT hourly ladder ke steps kab-kab close honge."""
        st = self.states.get(symbol)
        if st is None or st.ladder_anchor is None:
            return None
        return ladder_schedule(st.ladder_anchor)

    def _roll_ladder(self, st: S9State, ts: datetime) -> None:
        """
        Har hourly candle ke opening pe NAYA ladder. Jaise hi ts current ladder ke
        1H close se aage nikle, anchor current ghante pe aa jaata hai aur
        confirmations reset — nayi ladder phir 5M se shuru hogi.

        Dhyan: anchor+60 (1H close) aur AGLE ghante ka anchor ek hi timestamp hai
        (10:15 ladder ka 1H close = 11:15 = agla anchor). Isliye roll STRICTLY
        BAAD me hona chahiye, warna har ghante ka 1H step nigal jaata hai aur
        "ALL CONFIRMED" kabhi nahi aata.
        """
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
        """
        Kisi TF pe setup confirm hua (PP inside BB + us TF ki candle BB upper ke
        bahar close). Ladder alerts blacklist se EXEMPT hain.

        Ladder HAR GHANTE naya hai: jis hourly candle me ts hai, usi ke
        +5/+15/+30/+60 steps chalte hain. Ghanta badla → ladder reset, phir 5M se.
        Alert tabhi jaata hai jab kam se kam MIN_LADDER_TFS confirm ho chuke hon.
        """
        st = self.states.get(symbol)
        if st is None or tf not in TF_LADDER:
            return None

        # ghanta badla? naya ladder shuru
        self._roll_ladder(st, ts)

        if tf in st.tf_confirmed:
            return None
        if st.ladder_anchor is None:
            return None

        # ts is ghante ke ladder window ke andar hona chahiye
        sched = ladder_schedule(st.ladder_anchor)
        if ts < st.ladder_anchor or ts > sched["1H"]:
            return None

        # Ladder strictly sequential — pichhla TF confirm hona zaroori
        idx = TF_LADDER.index(tf)
        if idx > 0 and TF_LADDER[idx - 1] not in st.tf_confirmed:
            return None

        st.tf_confirmed.append(tf)
        st.ladder_complete = len(st.tf_confirmed) == len(TF_LADDER)

        # Akela "5M CONFIRMED" nahi bhejna — kam se kam 5<15
        if len(st.tf_confirmed) < MIN_LADDER_TFS:
            return None

        return format_ladder_alert(st, ts=ts)


# ----------------------------------------------------------------------------
# 5. PIVOT CALCULATION + WEEKLY AUTO-REFRESH
# ----------------------------------------------------------------------------

def daily_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Previous DAY ke H/L/C se classic pivots — 5M/15M/30M ke liye.

    NOTE: R3/S3 ke do prachalit convention hain —
        (a) R3 = H + 2(PP−L),  S3 = L − 2(H−PP)
        (b) R3 = PP + 2(H−L),  S3 = PP − 2(H−L)
    Kite (b) plot karta hai, isliye hum bhi (b) use karte hain — warna scanner
    aur chart ke levels alag aayenge aur star count match nahi karega.
    """
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
    """Previous WEEK ke H/L/C se pivots — scanner sirf PP use karta hai."""
    pp = (high + low + close) / 3.0
    return {
        "PP": pp,
        "R1": (2 * pp) - low,
        "R2": pp + (high - low),
        "S1": (2 * pp) - high,
    }


def week_key(dt: datetime) -> tuple:
    """ISO (year, week). Har Monday ko apne aap badal jaati hai."""
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def prev_week_range(dt: datetime) -> tuple:
    """Pichhle hafte ka Monday aur Friday (date objects) — data fetch ke liye."""
    this_monday = (dt - timedelta(days=dt.weekday())).date()
    prev_monday = this_monday - timedelta(days=7)
    prev_friday = prev_monday + timedelta(days=4)
    return prev_monday, prev_friday


class WeeklyPivotCache:
    """
    Weekly PP ka cache. Har Monday apne aap refresh hota hai — ISO week badalte hi
    purani entry invalid ho jaati hai aur agle call pe dobara calculate hoti hai.

    Monday chhutti ho to koi dikkat nahi: refresh lazy hai, jis din pehla call
    aayega usi din nayi value ban jayegi (data to pichhle hafte ka hi hai).

    fetch_fn(symbol, start_date, end_date) -> (high, low, close) ya None
    Naye contract me pichhle hafte ka data nahi hota — tab None return karein.
    """

    def __init__(self, fetch_fn) -> None:
        self.fetch_fn = fetch_fn
        self._cache: Dict[str, tuple] = {}   # symbol -> (week_key, pivots|None)

    def get(self, symbol: str, now: Optional[datetime] = None) -> Optional[Dict[str, float]]:
        now = now or datetime.now()
        wk = week_key(now)

        cached = self._cache.get(symbol)
        if cached is not None and cached[0] == wk:
            return cached[1]

        start, end = prev_week_range(now)
        try:
            ohlc = self.fetch_fn(symbol, start, end)
        except Exception:
            ohlc = None

        pivots = weekly_pivots(*ohlc) if ohlc else None
        self._cache[symbol] = (wk, pivots)
        return pivots

    def weekly_pp(self, symbol: str, now: Optional[datetime] = None) -> Optional[float]:
        p = self.get(symbol, now)
        return p["PP"] if p else None

    def force_refresh(self, symbol: Optional[str] = None) -> None:
        """Manual refresh — symbol None ho to poora cache clear."""
        if symbol is None:
            self._cache.clear()
        else:
            self._cache.pop(symbol, None)


def build_pivots(
    symbol: str,
    prev_day_hlc: tuple,
    weekly_cache: WeeklyPivotCache,
    now: Optional[datetime] = None,
    segment: str = DEFAULT_SEGMENT,
) -> Optional[Pivots]:
    """
    Daily PP/R1/R2/R3 + weekly PP ek saath.

    Returns None ka matlab: is instrument ko SKIP karo.
    NSE me weekly data mandatory hai — jis OTM strike ka pichhle hafte ka data
    nahi hai, wo setup chahiye hi nahi. Crypto/MCX me weekly optional hai;
    weekly PP None rahegi aur daily levels se kaam chalta rahega (us case me
    weekly bonus star reachable nahi hoga).
    """
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
# TELEGRAM FORMATTERS
# ----------------------------------------------------------------------------

def _ladder_string(confirmed: List[str]) -> str:
    """['5M','15M','30M'] → '5<15<30'"""
    short = {"5M": "5", "15M": "15", "30M": "30", "1H": "60"}
    ordered = [t for t in TF_LADDER if t in confirmed]
    return "<".join(short[t] for t in ordered)


def format_alert(st: S9State, kind: str, price: float, ts: datetime,
                 sent_at: Optional[datetime] = None) -> str:
    """
    Chhota alert — 3 line, bas kaam ki baat.

        🔥 JACKPOT S9 BLAST ⭐⭐⭐⭐⭐⭐
        📊 ULTRACEMCO AUG 11760 PE
        Buy@135.00  time-11:05

    time = CANDLE CLOSE ka waqt (5M / 15M / 30M / 1H — jis candle pe signal bana).
    Message kab bheja gaya, wo Telegram khud dikha deta hai; yahan wo dohrana
    bekaar hai. sent_at param sirf backward-compat ke liye hai, use nahi hota.
    """
    header = "🔥 JACKPOT " if st.jackpot else ""
    stars = STAR_SYMBOL[st.stars_sent]
    title = "S9 BLAST" if kind == "NEW" else "S9 UPGRADE"

    return "\n".join([
        f"{header}{title} {stars}".strip(),
        f"📊 {st.symbol}",
        f"Buy@{price:.2f}  time-{ts:%H:%M}",
    ])


def format_ladder_alert(st: S9State, ts: datetime) -> str:
    chain = _ladder_string(st.tf_confirmed)
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
# SELF TEST
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    D = datetime(2026, 8, 10)          # Monday
    SYM = "RECLTD AUG 350 PE"
    piv = Pivots(daily_pp=2.10, daily_r1=2.90, daily_r2=3.70, daily_r3=4.60,
                 weekly_pp=7.50)

    enh = S9Enhancer()
    enh.maybe_reset_day(D)

    # ---- ENTRY GATE ----
    print("=" * 52)
    print("ENTRY GATE")
    print("=" * 52)
    cases = [
        ("opening, PDH cross", D.replace(hour=9, minute=20), 2.30, 2.95, 3.05, 2.60, 45_000),
        ("opening, PDH missing (ab FAIL)", D.replace(hour=9, minute=20), 2.30, 2.95, 3.05, None, 45_000),
        ("opening, illiquid (ab FAIL)", D.replace(hour=9, minute=20), 2.30, 2.95, 3.05, 2.60, 40),
        ("mid-day, illiquid (skip OK)", D.replace(hour=11, minute=5), 2.30, 2.95, 3.05, 2.60, 40),
    ]
    for label, ts, o, c, h, pdh, vol in cases:
        ok, why = entry_gate_ok(ts=ts, candle_open=o, candle_close=c, candle_high=h,
                                prev_day_high=pdh, prev_day_volume=vol,
                                day_high_so_far=2.90)
        print(f"  {label:34s} → {str(ok):5s} | {why}")

    # ---- BLAST + UPGRADES ----
    print("\n" + "=" * 52)
    print("BLAST + STAR UPGRADES")
    print("=" * 52)
    print(enh.on_blast(SYM, D.replace(hour=9, minute=30), close=2.95,
                       pivots=piv, flat_bb=True))
    for hh, mm, px in [(9, 45, 3.90), (10, 0, 5.20), (10, 15, 4.80),
                       (11, 0, 7.90), (13, 45, 11.55)]:
        msg = enh.on_candle_close(SYM, D.replace(hour=hh, minute=mm), px)
        if msg:
            print("-" * 52)
            print(msg)
    print("-" * 52)
    print("blacklisted:", enh.is_blacklisted(SYM))

    # ---- HOURLY LADDERS (din bhar, blacklist ke baad bhi) ----
    print("\n" + "=" * 52)
    print("HOURLY LADDERS — har ghante naya")
    print("=" * 52)
    for anchor_h, anchor_m in [(9, 15), (10, 15), (11, 15), (14, 15)]:
        anchor = D.replace(hour=anchor_h, minute=anchor_m)
        print(f"\n>>> anchor {anchor:%H:%M} → "
              + "  ".join(f"{tf} {t:%H:%M}" for tf, t in
                          sorted(ladder_schedule(anchor).items(),
                                 key=lambda kv: kv[1])))
        for tf in TF_LADDER:
            ts = anchor + timedelta(minutes=LADDER_OFFSETS_MINS[tf])
            msg = enh.on_tf_confirm(SYM, tf, ts)
            if msg:
                print("-" * 52)
                print(msg)

    # ---- WEEKLY CACHE ----
    print("\n" + "=" * 52)
    print("WEEKLY PIVOT CACHE — Monday auto-refresh")
    print("=" * 52)
    fetch_log = []

    def fake_fetch(symbol, start, end):
        fetch_log.append((symbol, start, end))
        return (9.80, 4.20, 8.50)      # pichhle hafte ka H/L/C

    wc = WeeklyPivotCache(fake_fetch)
    mon = datetime(2026, 8, 10, 9, 30)
    thu = datetime(2026, 8, 13, 11, 0)
    nxt = datetime(2026, 8, 17, 9, 20)

    print("  Mon      WPP:", round(wc.weekly_pp(SYM, mon), 2))
    print("  Thu      WPP:", round(wc.weekly_pp(SYM, thu), 2), " (cache se)")
    print("  Next Mon WPP:", round(wc.weekly_pp(SYM, nxt), 2), " (auto refresh)")
    print("  Fetch calls:", len(fetch_log), "| weeks:", [str(f[1]) for f in fetch_log])

    # ---- DAY ROLLOVER ----
    print("\n" + "=" * 52)
    print("DAY ROLLOVER")
    print("=" * 52)
    print("  same day  reset?", enh.maybe_reset_day(D.replace(hour=15, minute=0)))
    print("  next day  reset?", enh.maybe_reset_day(D + timedelta(days=1)))
    print("  blacklist saaf?", not enh.is_blacklisted(SYM))
