"""Unattended scheduler entry point. cron (supercronic) calls this every 15 minutes; it decides.

Slots live in guardrail_trader/slots.py, each in its own market's zone (CLAUDE.md §6), so
daylight-saving changes need no edits:
  eu_open  10:00-11:00 Paris  Europe         us_open 10:30-11:30 New York  US
  us_close 15:00-15:40 New York  US (+ trailing stops; Europe read only)
  stops    16:15-16:45 New York  books the day's stop executions while IBKR still reports them, no Claude

Per slot per day: at most MAX_ATTEMPTS tries (e.g. if IB Gateway is briefly down),
and never two runs at once (file lock). Weekends are skipped here; holidays are caught per
exchange by run_bot's own market-hours check (no Claude call, no cost).

Dry-run week (CLAUDE.md §10, T10): SCHEDULER_DRY_RUN=1 in .env adds --dry-run to every run, and each
decision slot runs a second time right after the first (same lock, so never two IBKR connections at once);
scripts/consistency.py compares the two sets of proposals.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from guardrail_trader.slots import current_slot

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "scheduler_state.json"
LOCK = ROOT / "data" / "scheduler.lock"
LOGS = ROOT / "logs"
NY = ZoneInfo("America/New_York")
MAX_ATTEMPTS = 3
DECISION_SLOTS = ("eu_open", "us_open", "us_close")     # slots where Claude decides (second dry run)
DONE_CODES = (0, 3)   # 0 = ran / market closed / halted; 3 = reconcile failed (needs a human)


def slot_done(rc: int) -> bool:
    """Is this slot finished, or worth another attempt within its window?

    Retry transient failures - rc 1 (error) and rc 2 (gateway connected but serving no data,
    e.g. during IBKR's nightly re-login) - up to MAX_ATTEMPTS.
    """
    return rc in DONE_CODES


def dry_run_week() -> bool:
    """SCHEDULER_DRY_RUN=1 (from .env through docker compose's env_file) = the dry-run week."""
    return os.getenv("SCHEDULER_DRY_RUN", "0") == "1"


def write_heartbeat(heartbeat: dict) -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    tmp = ROOT / "data" / "scheduler_heartbeat.tmp"
    tmp.write_text(json.dumps(heartbeat))
    tmp.replace(ROOT / "data" / "scheduler_heartbeat.json")


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
    # Every tick proves the scheduler is alive (docker healthcheck, silent-scheduler watchdog), including the
    # ticks of a slot already done; a run result is added after the run.
    write_heartbeat(heartbeat)
    slot = current_slot(now_ny)
    if slot is None and "--force" not in argv:
        return 0
    name = slot.name if slot else "manual"
    day = slot.day(now_ny) if slot else now_ny.date()
    run_args = ["--slot", slot.name, *slot.args] if slot else []
    dry_week = dry_run_week()
    if dry_week:
        run_args.append("--dry-run")

    LOCK.parent.mkdir(exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a run is already in progress

        key = f"{day:%Y-%m-%d}:{name}"
        state = load_state()
        entry = state.get(key, {"attempts": 0, "done": False})
        if entry["done"] or entry["attempts"] >= MAX_ATTEMPTS:
            return 0

        entry["attempts"] += 1
        LOGS.mkdir(exist_ok=True)
        log_path = LOGS / f"run_{day:%Y%m%d}_{name}_{entry['attempts']}.log"
        with open(log_path, "w") as log:
            rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_bot.py"), *run_args],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
        if dry_week and rc == 0 and name in DECISION_SLOTS:
            with open(log_path.with_name(log_path.stem + "_second.log"), "w") as log:
                entry["second_rc"] = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "run_bot.py"), *run_args],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
        entry["done"] = slot_done(rc)
        entry["last_rc"] = rc
        entry["last_log"] = str(log_path.relative_to(ROOT))
        entry["at_ny"] = now_ny.isoformat(timespec="seconds")
        state[key] = entry
        save_state(state)
        heartbeat.update(last_run_key=key, last_rc=rc)
        write_heartbeat(heartbeat)
        if name == "stops" and day.weekday() == 4:     # Friday, after us_close and the day's stop fills
            with open(LOGS / f"weekly_report_{day:%Y%m%d}.log", "w") as log:
                subprocess.run([sys.executable, str(ROOT / "scripts" / "weekly_report.py"), "--notify"],
                               cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
