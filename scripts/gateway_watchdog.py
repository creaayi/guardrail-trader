"""Restart the IB Gateway container when runs keep failing for gateway reasons; alert when the scheduler
falls silent during market hours (T9).

Why this exists: a network outage can leave IB Gateway connected but useless - it serves account
data while IBKR has taken its market-data line away (error 10197, "competing live session"), or it
serves nothing at all. Runs then fail every slot until someone restarts the container. Seen live on
23-24 Sep 2026: six runs skipped over two slots.

Design:
  * Runs on the host (launchd), not in a container: Docker control stays out of the bot, which
    would otherwise need the Docker socket - root-equivalent access to this Mac.
  * Decides from the journal's own run statuses, so there is no extra state to keep in sync.
  * Never acts while a run holds the scheduler lock, so it can't restart mid-execution.
  * Cooldown + daily cap, so a broken gateway can't cause a restart loop.
  * Silent scheduler: no heartbeat (data/scheduler_heartbeat.json, written every 15-min tick) for more than
    SILENT_HOURS while a market is open (Paris 09:00-17:30 or New York 09:30-16:00, weekdays) -> one
    high-priority ntfy alert, repeated at most every SILENT_HOURS while it lasts. A gateway restart and a
    daily cap reached are alerted too.

Runs on the host every 15 minutes (cron or a systemd timer; see docs/alerts.md), e.g.
  */15 * * * * cd ~/guardrail-trader && .venv/bin/python scripts/gateway_watchdog.py >> logs/watchdog.cron.log 2>&1

  scripts/gateway_watchdog.py [--dry-run] [--min-bad N] [--cooldown-min M] [--max-per-day K]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from guardrail_trader.alerts import Alert, Notifier

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "data" / "journal.sqlite"
LOCK = ROOT / "data" / "scheduler.lock"
STATE = ROOT / "data" / "gateway_watchdog_state.json"
LOG = ROOT / "logs" / "gateway_watchdog.log"
TZ = ZoneInfo("Europe/Paris")
NY = ZoneInfo("America/New_York")
HEARTBEAT = ROOT / "data" / "scheduler_heartbeat.json"
SILENT_HOURS = 2.0
SERVICE = "ib-gateway"
# Statuses that mean "the gateway is the problem" (see guardrail_trader/pipeline.py)
GATEWAY_BAD = ("broker_not_ready", "market_data_unavailable")


def recent_statuses(db: Path, limit: int = 5) -> list[str]:
    """Statuses of the last real (non dry-run, non fake) runs, newest first."""
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT status FROM runs WHERE mode NOT LIKE '%dry%' AND mode NOT LIKE '%fake%' "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [r[0] or "" for r in rows]


def should_restart(statuses: list[str], state: dict, now: datetime,
                   min_bad: int, cooldown_min: int, max_per_day: int) -> tuple[bool, str]:
    """Decide from the recent run statuses, the last restart and today's restart count."""
    bad = [s for s in statuses[:min_bad]]
    if len(bad) < min_bad or not all(s in GATEWAY_BAD for s in bad):
        return False, f"last {min_bad} run(s) {statuses[:min_bad] or ['(none)']} - nothing to do"
    last = state.get("last_restart")
    if last:
        since = now - datetime.fromisoformat(last)
        if since < timedelta(minutes=cooldown_min):
            return False, f"in cooldown ({int(since.total_seconds() // 60)}/{cooldown_min} min since last restart)"
    if state.get("restarts", {}).get(now.strftime("%Y-%m-%d"), 0) >= max_per_day:
        return False, f"daily cap reached ({max_per_day}); the gateway needs a human"
    return True, f"last {min_bad} run(s) {bad} are gateway failures"


def in_session(now: datetime) -> bool:
    """A market the bot trades is open (weekdays; holidays are not modelled here: an alert on a holiday
    with a dead scheduler is still worth having)."""
    paris, ny = now.astimezone(TZ), now.astimezone(NY)
    eu = paris.weekday() < 5 and time(9, 0) <= paris.time() < time(17, 30)
    us = ny.weekday() < 5 and time(9, 30) <= ny.time() < time(16, 0)
    return eu or us


def heartbeat_age_hours(path: Path, now: datetime) -> float | None:
    """Hours since the scheduler's last tick; None if it never wrote one (or wrote garbage)."""
    try:
        last = datetime.fromisoformat(json.loads(path.read_text())["last_tick_ny"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None
    return (now - last).total_seconds() / 3600


def silent_scheduler_alert(age_hours: float | None, state: dict, now: datetime) -> str | None:
    """The alert text if the scheduler is silent in session and we haven't said so in the last SILENT_HOURS."""
    if not in_session(now) or (age_hours is not None and age_hours <= SILENT_HOURS):
        return None
    last = state.get("last_silent_alert")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=SILENT_HOURS):
        return None
    return ("scheduler never wrote a heartbeat" if age_hours is None
            else f"no scheduler heartbeat for {age_hours:.1f} h during market hours")


def a_run_is_in_progress() -> bool:
    """The scheduler holds this lock for the duration of a run."""
    if not LOCK.exists():
        return False
    with open(LOCK) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    line = f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-bad", type=int, default=2, help="consecutive gateway failures before acting")
    ap.add_argument("--cooldown-min", type=int, default=30)
    ap.add_argument("--max-per-day", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="decide and log, restart nothing")
    args = ap.parse_args(argv)

    now = datetime.now(TZ)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    notifier = Notifier.from_env(log)
    silent = silent_scheduler_alert(heartbeat_age_hours(HEARTBEAT, now), state, now)
    if silent:
        log(f"SCHEDULER SILENT: {silent}")
        if not args.dry_run:
            notifier.send(Alert("ALERT scheduler silent", silent + "; check the bot container", high=True))
            state["last_silent_alert"] = now.isoformat(timespec="seconds")
            STATE.write_text(json.dumps(state, indent=1))
    go, why = should_restart(recent_statuses(JOURNAL), state, now,
                             args.min_bad, args.cooldown_min, args.max_per_day)
    if not go:
        if "daily cap" in why and state.get("cap_alerted") != now.strftime("%Y-%m-%d") and not args.dry_run:
            notifier.send(Alert("ALERT IB Gateway down", why, high=True))
            state["cap_alerted"] = now.strftime("%Y-%m-%d")
            STATE.write_text(json.dumps(state, indent=1))
        return 0
    if a_run_is_in_progress():
        log(f"WOULD RESTART ({why}) but a run is in progress; waiting for the next check")
        return 0
    if args.dry_run:
        log(f"DRY RUN: would restart {SERVICE} ({why})")
        return 0

    log(f"restarting {SERVICE}: {why}")
    r = subprocess.run(["docker", "compose", "restart", SERVICE], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0
    log(f"docker compose restart {SERVICE} -> rc={r.returncode} {(r.stderr or r.stdout).strip()[:200]}")
    notifier.send(Alert("ALERT IB Gateway restarted" if ok else "ALERT IB Gateway restart failed", why, high=True))
    if ok:
        state["last_restart"] = now.isoformat(timespec="seconds")
        state.setdefault("restarts", {})
        state["restarts"][now.strftime("%Y-%m-%d")] = state["restarts"].get(now.strftime("%Y-%m-%d"), 0) + 1
        STATE.write_text(json.dumps(state, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
