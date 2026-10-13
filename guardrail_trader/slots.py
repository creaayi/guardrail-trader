"""Decision slots (CLAUDE.md §6), each in its own market's time zone, and the markets each one may trade.

Defining every slot in its own zone absorbs the 2-3 weeks a year when the US and Europe are on different
daylight-saving schedules. The scheduler (scripts/scheduled_run.py) picks the slot; run_bot.py --slot
passes it to the pipeline, which trades only on the slot's exchanges that are open right now (holidays per
exchange come from IBKR's liquidHours). The gate enforces it: an order on any other exchange is blocked.

  eu_open   10:00-11:00 Europe/Paris      trades Europe (SBF, AEB, IBIS, LSE, EBS)
  us_open   10:30-11:30 America/New_York  trades the US
  us_close  15:00-15:40 America/New_York  trades the US; Europe is closed (read only); trailing stops
  stops     16:15-16:45 America/New_York  books the day's stop fills while IBKR still reports them; no Claude

Monday is the weekly pocket A rebalance in eu_open and us_open (the prompt is told); other days and
us_close: adjustments and stop management.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
NY = ZoneInfo("America/New_York")

US_EXCHANGES = frozenset({"SMART"})                                 # US stocks route SMART, no primary
EU_EXCHANGES = frozenset({"SBF", "AEB", "IBIS", "LSE", "EBS"})


@dataclass(frozen=True)
class Slot:
    name: str
    tz: ZoneInfo
    start: time
    end: time
    exchanges: frozenset[str]           # where this slot may trade (when the exchange is open)
    rebalance_monday: bool = False      # Monday = weekly pocket A rebalance
    args: tuple[str, ...] = ()          # extra run_bot.py flags

    def day(self, now: datetime) -> date:
        """The slot's calendar day (its own zone): the scheduler's once-per-day key."""
        return now.astimezone(self.tz).date()

    def contains(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return local.weekday() < 5 and self.start <= local.time() < self.end

    def rebalance(self, now: datetime) -> bool:
        return self.rebalance_monday and now.astimezone(self.tz).weekday() == 0


SLOTS: dict[str, Slot] = {s.name: s for s in [
    Slot("eu_open", PARIS, time(10, 0), time(11, 0), EU_EXCHANGES, rebalance_monday=True),
    Slot("us_open", NY, time(10, 30), time(11, 30), US_EXCHANGES, rebalance_monday=True),
    Slot("us_close", NY, time(15, 0), time(15, 40), US_EXCHANGES, args=("--trail-stops",)),
    Slot("stops", NY, time(16, 15), time(16, 45), frozenset(), args=("--stops-only",)),
]}
ALL_EXCHANGES = US_EXCHANGES | EU_EXCHANGES


def current_slot(now: datetime) -> Slot | None:
    """The slot `now` (timezone-aware) falls in, None outside every slot and at weekends."""
    return next((s for s in SLOTS.values() if s.contains(now)), None)


def next_slot(now: datetime) -> tuple[Slot, datetime] | None:
    """The next slot start after `now` within a week: (slot, start as an aware datetime)."""
    from datetime import timedelta
    best = None
    for s in SLOTS.values():
        local = now.astimezone(s.tz)
        for d in range(8):
            day = local.date() + timedelta(days=d)
            start = datetime.combine(day, s.start, tzinfo=s.tz)
            if start > now and day.weekday() < 5:
                if best is None or start < best[1]:
                    best = (s, start)
                break
    return best
