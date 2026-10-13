"""Alerts to the owner's phone through ntfy (CLAUDE.md §8.4; decision of 13/10/2026, T9).

Three levels:
  (a) a short summary after every run (default priority);
  (b) an immediate high-priority alert on any anomaly: a stop that needs a human (exit code 3: reconciliation,
      stop sync, capital guard), the kill switch, a drawdown tier change, the daily loss halt, an error, the
      gateway unusable, a run that could not execute;
  (c) "scheduler silent": no heartbeat for more than 2 h during market hours (scripts/gateway_watchdog.py).

Configuration (.env only): NTFY_TOPIC (no topic = alerts off, said once in the run log), NTFY_URL (default
https://ntfy.sh). The topic is a secret - anyone who knows it reads the alerts - so it never appears in a log
line or an error message. Sending never blocks or fails a run: short timeout, every error swallowed and
reported without the URL.
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Callable

import guardrail_trader.config  # noqa: F401  (loads .env: the watchdog runs on the host, outside Docker)

DEFAULT_URL = "https://ntfy.sh"
TIMEOUT_S = 5.0

# Run statuses (journal `runs.status`) that call for an immediate high-priority alert.
ANOMALIES = {
    "reconcile_failed": "reconciliation failed: journal and IBKR disagree",
    "stop_sync_failed": "protective stops out of sync",
    "capital_guard": "paper account below the bot's value",
    "kill_switch": "KILL SWITCH: liquidating",
    "daily_loss_halt": "daily loss halt: no order today",
    "error": "run failed with an error",
    "broker_not_ready": "IB Gateway not ready",
    "market_data_unavailable": "IB Gateway serves no market data",
    "aborted": "a run died mid-way",
}


@dataclass(frozen=True)
class Alert:
    title: str
    body: str
    high: bool = False


class Notifier:
    """POSTs alerts to ntfy. `post(url, data, headers)` is injectable so tests never touch the network."""

    def __init__(self, topic: str | None, url: str = DEFAULT_URL, log: Callable[[str], None] = print,
                 post: Callable[[str, bytes, dict], None] | None = None):
        self.topic, self.url, self.log = (topic or "").strip(), url.rstrip("/"), log
        self._post = post or _post

    @classmethod
    def from_env(cls, log: Callable[[str], None] = print) -> "Notifier":
        return cls(os.getenv("NTFY_TOPIC"), os.getenv("NTFY_URL") or DEFAULT_URL, log)

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, alert: Alert) -> bool:
        if not self.enabled:
            self.log("alerts off (NTFY_TOPIC not set): " + alert.title)
            return False
        headers = {"Title": _ascii(alert.title), "Priority": "high" if alert.high else "default",
                   "Tags": "warning" if alert.high else "chart_with_upwards_trend"}
        try:
            self._post(f"{self.url}/{self.topic}", alert.body.encode("utf-8"), headers)
            return True
        except Exception as e:             # never let an alert break or delay a run; never print the topic
            self.log(f"alert not sent ({type(e).__name__}); title: {alert.title}")
            return False


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:   # noqa: S310 (https URL from .env)
        r.read()


def _ascii(s: str) -> str:
    """HTTP header values are latin-1: keep the title plain."""
    return s.encode("ascii", "replace").decode("ascii")


# ------------------------------------------------------------------ what to say
def run_alerts(j, run_ids: list[int], excess_pct: float | None = None) -> list[Alert]:
    """The alerts for runs just made (normally one): a summary, plus one high-priority alert per anomaly."""
    out: list[Alert] = []
    for run_id in run_ids:
        r = j.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            continue
        status, base = r["status"], j.base_currency() or ""
        props = j.db.execute("SELECT approved, COUNT(*) FROM proposals WHERE run_id=? GROUP BY approved",
                             (run_id,)).fetchall()
        approved = sum(n for a, n in props if a)
        blocked = sum(n for a, n in props if not a)
        filled = j.db.execute("SELECT COUNT(*) FROM orders o JOIN proposals p ON p.id=o.proposal_id "
                              "WHERE p.run_id=? AND o.filled > 0", (run_id,)).fetchone()[0]
        tiers = j.db.execute("SELECT from_tier, to_tier, drawdown_pct FROM tier_log WHERE ts >= ? "
                             "AND (? IS NULL OR ts <= ?)", (r["started_at"], r["finished_at"], r["finished_at"])
                             ).fetchall()
        lines = [
            f"run #{run_id} {r['mode']}: {status}",
            "NAV " + (f"{r['value_base']:,.0f} {base}" if r["value_base"] is not None else "n/a")
            + (f", drawdown {r['drawdown_pct']:.1f}%" if r["drawdown_pct"] is not None else ""),
            "vs IWDA " + ("n/a" if excess_pct is None else f"{excess_pct:+.2f} pts since day 1"),
            f"decisions: {approved} approved, {blocked} blocked, {filled} filled",
            "notes: " + ((r["notes"] or "-").splitlines()[0][:200]),
        ]
        out.append(Alert(f"guardrail {status}", "\n".join(lines)))
        if status in ANOMALIES:
            out.append(Alert(f"ALERT {ANOMALIES[status]}", "\n".join(lines), high=True))
        for frm, to, dd in tiers:
            out.append(Alert(f"ALERT drawdown tier {frm} -> {to}", f"drawdown {dd:.2f}%\n" + "\n".join(lines),
                             high=True))
    return out


def not_executed(reason: str) -> Alert:
    """A run that could not even start (no journal row): gateway refused the connection, journal missing..."""
    return Alert("ALERT run not executed", reason[:500], high=True)
