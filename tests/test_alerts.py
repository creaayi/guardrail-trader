"""T9: ntfy alerts (never a network call in tests), run summaries and anomalies, the silent-scheduler watchdog."""
import importlib.util
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from guardrail_trader.alerts import Alert, Notifier, not_executed, run_alerts
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal
from guardrail_trader.risk import PortfolioState
from guardrail_trader.risk.gate import NORMAL, PAUSED

SECRET = "topic-s3cret-xyz"


def notifier(fail=False):
    sent, logs = [], []

    def post(url, data, headers):
        if fail:
            raise ConnectionError(f"cannot reach {url}")       # the URL holds the secret topic
        sent.append((url, data.decode(), headers))
    return Notifier(SECRET, "https://ntfy.example/", logs.append, post=post), sent, logs


def test_send_posts_to_the_topic_with_priority():
    n, sent, _ = notifier()
    assert n.send(Alert("ALERT kill switch", "body é", high=True))
    (url, body, headers), = sent
    assert url == f"https://ntfy.example/{SECRET}" and body == "body é"
    assert headers["Priority"] == "high" and headers["Title"] == "ALERT kill switch"


def test_a_failed_send_never_raises_and_never_logs_the_topic():
    n, _, logs = notifier(fail=True)
    assert n.send(Alert("guardrail ok", "x")) is False
    assert logs and all(SECRET not in line for line in logs)


def test_no_topic_means_alerts_off():
    logs = []
    n = Notifier("", log=logs.append, post=lambda *a: pytest.fail("no network without a topic"))
    assert not n.enabled and n.send(Alert("t", "b")) is False and "NTFY_TOPIC" in logs[0]


def test_non_ascii_titles_are_made_header_safe():
    n, sent, _ = notifier()
    n.send(Alert("rapport W42 – été", "b"))
    sent[0][2]["Title"].encode("latin-1")


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(1000.0, "EUR")
    yield jr
    jr.close()


def _run(j, status, notes=""):
    rid = j.start_run("paper")
    j.finish_run(rid, status, PortfolioState(cash_base=1000.0, peak_value_base=1000.0), notes)
    return rid


def test_a_normal_run_gets_one_summary(j):
    (a,) = run_alerts(j, [_run(j, "ok", "bought SAP")], excess_pct=1.25)
    assert not a.high and "NAV 1,000 EUR" in a.body and "+1.25 pts" in a.body and "bought SAP" in a.body


@pytest.mark.parametrize("status", ["reconcile_failed", "stop_sync_failed", "capital_guard", "kill_switch",
                                    "daily_loss_halt", "error", "broker_not_ready", "market_data_unavailable"])
def test_anomalies_add_a_high_priority_alert(j, status):
    summary, urgent = run_alerts(j, [_run(j, status, "details")])
    assert not summary.high and urgent.high and "details" in urgent.body


def test_a_tier_change_during_the_run_is_urgent(j):
    rid = j.start_run("paper")
    j.set_tier(PAUSED, "2026-10-19", 8.3)
    j.finish_run(rid, "ok", PortfolioState(cash_base=1000.0, peak_value_base=1000.0))
    alerts = run_alerts(j, [rid])
    assert any(a.high and f"{NORMAL} -> {PAUSED}" in a.title for a in alerts)


def test_run_not_executed_is_urgent():
    assert not_executed("ConnectionRefusedError").high


def test_run_bot_alerts_are_sent_after_the_run(j, monkeypatch):
    spec = importlib.util.spec_from_file_location("run_bot", PROJECT_ROOT / "scripts" / "run_bot.py")
    rb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rb)
    n, sent, _ = notifier()
    _run(j, "reconcile_failed")
    rb.send_run_alerts(j, 0, n)
    assert [h["Priority"] for *_, h in sent] == ["default", "high"]


# ------------------------------------------------------------------ silent scheduler (host watchdog)
spec = importlib.util.spec_from_file_location("gateway_watchdog", PROJECT_ROOT / "scripts" / "gateway_watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)
PARIS, NY = ZoneInfo("Europe/Paris"), ZoneInfo("America/New_York")


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 13, 9, 30, tzinfo=PARIS), True),       # Paris open
    (datetime(2026, 10, 13, 18, 0, tzinfo=PARIS), True),       # 12:00 New York
    (datetime(2026, 10, 13, 23, 0, tzinfo=PARIS), False),      # 17:00 New York, after the close
    (datetime(2026, 10, 13, 7, 0, tzinfo=PARIS), False),
    (datetime(2026, 10, 17, 11, 0, tzinfo=PARIS), False),      # Saturday
])
def test_in_session_covers_both_markets(now, expected):
    assert wd.in_session(now) is expected


def test_silent_scheduler_alert_after_two_hours_in_session_once_per_two_hours(tmp_path):
    now = datetime(2026, 10, 13, 15, 0, tzinfo=PARIS)
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"last_tick_ny": datetime(2026, 10, 13, 12, 30, tzinfo=PARIS).isoformat()}))
    age = wd.heartbeat_age_hours(hb, now)
    assert age == pytest.approx(2.5) and "2.5 h" in wd.silent_scheduler_alert(age, {}, now)
    assert wd.silent_scheduler_alert(1.9, {}, now) is None                      # a slot run can take a while
    said = {"last_silent_alert": datetime(2026, 10, 13, 14, 0, tzinfo=PARIS).isoformat()}
    assert wd.silent_scheduler_alert(age, said, now) is None                   # said an hour ago
    assert wd.silent_scheduler_alert(age, {}, datetime(2026, 10, 13, 23, 30, tzinfo=PARIS)) is None   # closed
    assert "never" in wd.silent_scheduler_alert(wd.heartbeat_age_hours(tmp_path / "none.json", now), {}, now)
