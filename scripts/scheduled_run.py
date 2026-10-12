"""Unattended scheduler entry point. launchd calls this every 15 minutes; it decides.

Slots are defined in New York time, so daylight-saving changes need no edits:
  slot "open"  10:30-11:30 ET   (1h after the open, prices settled)
  slot "close" 15:00-15:40 ET   (before the 16:00 close; run_bot needs >=15 min left) + trailing stops
  slot "stops" 16:15-16:45 ET   (after the US close, before midnight in Paris): books the day's stop
                                 executions while IBKR still reports them (current day only), no Claude

Per slot per day: at most MAX_ATTEMPTS tries (e.g. if IB Gateway is briefly down),
and never two runs at once (file lock). Weekends are skipped here; holidays are caught
by run_bot's own market-hours check (no Claude call, no cost).
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "scheduler_state.json"
LOCK = ROOT / "data" / "scheduler.lock"
LOGS = ROOT / "logs"
NY = ZoneInfo("America/New_York")
SLOTS = {"open": (time(10, 30), time(11, 30)), "close": (time(15, 0), time(15, 40)),
         "stops": (time(16, 15), time(16, 45))}
SLOT_ARGS = {"close": ["--trail-stops"], "stops": ["--stops-only"]}
MAX_ATTEMPTS = 3
DONE_CODES = (0, 3)   # 0 = ran / market closed / halted; 3 = reconcile failed (needs a human)


def slot_done(rc: int) -> bool:
    """Is this slot finished, or worth another attempt within its window?

    Retry transient failures - rc 1 (error) and rc 2 (gateway connected but serving no data,
    e.g. during IBKR's nightly re-login) - up to MAX_ATTEMPTS.
    """
    return rc in DONE_CODES


def current_slot(now_ny: datetime) -> str | None:
    if now_ny.weekday() >= 5:
        return None
    for name, (start, end) in SLOTS.items():
        if start <= now_ny.time() < end:
            return name
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    # keep 30 days of history
    keep = dict(sorted(state.items())[-60:])
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=1))
    tmp.replace(STATE)


def main(argv: list[str]) -> int:
    now_ny = datetime.now(NY)
    heartbeat = {"last_tick_ny": now_ny.isoformat(timespec="seconds")}
    slot = current_slot(now_ny)
    if slot is None and "--force" not in argv:
        (ROOT / "data").mkdir(exist_ok=True)
        (ROOT / "data" / "scheduler_heartbeat.json").write_text(json.dumps(heartbeat))
        return 0
    slot = slot or "manual"

    LOCK.parent.mkdir(exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a run is already in progress

        key = f"{now_ny:%Y-%m-%d}:{slot}"
        state = load_state()
        entry = state.get(key, {"attempts": 0, "done": False})
        if entry["done"] or entry["attempts"] >= MAX_ATTEMPTS:
            return 0

        entry["attempts"] += 1
        LOGS.mkdir(exist_ok=True)
        log_path = LOGS / f"run_{now_ny:%Y%m%d}_{slot}_{entry['attempts']}.log"
        with open(log_path, "w") as log:
            rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_bot.py"), *SLOT_ARGS.get(slot, [])],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
        entry["done"] = slot_done(rc)
        entry["last_rc"] = rc
        entry["last_log"] = str(log_path.relative_to(ROOT))
        entry["at_ny"] = now_ny.isoformat(timespec="seconds")
        state[key] = entry
        save_state(state)
        heartbeat.update(last_run_key=key, last_rc=rc)
        (ROOT / "data" / "scheduler_heartbeat.json").write_text(json.dumps(heartbeat))
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
