"""Fill races, settled fills, loop time budget, recent activity and the bounded submit revision."""
import json
from types import SimpleNamespace as NS

import pytest

from guardrail_trader.agent import TradingAgent
from guardrail_trader.broker import IBKRBroker
from guardrail_trader.broker.ibkr import OrderResult
from guardrail_trader.executor import execute
from guardrail_trader.fake_llm import FakeClaude
from guardrail_trader.journal import Journal
from guardrail_trader.risk import Decision, PortfolioState, Proposal
from tests.test_agent import CFG, StubMarket


# ---------------------------------------------------------------- fake ib_async objects
class FakeTrade:
    def __init__(self, oid, status="Submitted", filled=0.0, fills=()):
        self.order = NS(orderId=oid, action="BUY", totalQuantity=5, orderType="LMT", lmtPrice=110.0)
        self.orderStatus = NS(status=status, filled=filled, avgFillPrice=110.0)
        self.contract = NS(symbol="CSCO")
        self.fills, self.log = list(fills), []

    def isDone(self):
        return self.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")


def fill(shares, comm_arrived=True):
    return NS(execution=NS(shares=shares, price=110.0),
              commissionReport=NS(execId="e1" if comm_arrived else "", commission=1.0 if comm_arrived else 0.0,
                                  currency="USD"))


class FakeIB:
    def __init__(self, trades, on_sleep=None, cancel_works=True):
        self._trades, self.on_sleep, self.cancel_works, self.cancelled = trades, on_sleep, cancel_works, []

    def trades(self):
        return self._trades

    def openTrades(self):  # noqa: N802
        return [t for t in self._trades if not t.isDone()]

    def cancelOrder(self, order):  # noqa: N802
        self.cancelled.append(order.orderId)
        if self.cancel_works:
            next(t for t in self._trades if t.order is order).orderStatus.status = "Cancelled"

    def sleep(self, _s):
        if self.on_sleep:
            self.on_sleep()


def broker_with(ib):
    b = IBKRBroker.__new__(IBKRBroker)
    b.ib, b.readonly = ib, False
    return b


# ---------------------------------------------------------------- broker
def test_cancel_after_order_already_filled_does_not_raise():
    """Race: the order filled between the executor's status check and the cancel."""
    t = FakeTrade(7, "Filled", 5, [fill(5)])
    ib = FakeIB([t])
    r = broker_with(ib).cancel_order(7)
    assert r.status == "Filled" and ib.cancelled == []


def test_cancel_of_working_order_waits_for_final_state():
    ib = FakeIB([FakeTrade(7)])
    assert broker_with(ib).cancel_order(7).status == "Cancelled" and ib.cancelled == [7]


def test_cancel_that_never_confirms_returns_after_timeout():
    ib = FakeIB([FakeTrade(7)], cancel_works=False)
    assert broker_with(ib).cancel_order(7, wait_seconds=0.05).status == "Submitted"


def test_fill_details_waits_for_commission_report():
    t = FakeTrade(7, "Filled", 5, [fill(5, comm_arrived=False)])
    def deliver():
        t.fills[0].commissionReport.execId, t.fills[0].commissionReport.commission = "e1", 1.0
    qty, _avg, comm, ccy = broker_with(FakeIB([t], on_sleep=deliver)).fill_details(7)
    assert (qty, comm, ccy) == (5, 1.0, "USD")


def test_fill_details_waits_until_executions_add_up():
    t = FakeTrade(7, "Cancelled", 5, [fill(3)])          # status says 5 filled, only 3 executions so far
    qty, *_ = broker_with(FakeIB([t], on_sleep=lambda: t.fills.append(fill(2)) if len(t.fills) == 1 else None)).fill_details(7)
    assert qty == 5


# ---------------------------------------------------------------- executor
_IB = IBKRBroker.__new__(IBKRBroker)   # only for its OrderResult conversion
_IB._magnifiers = {}


class FakeBroker:
    """Order placed -> 2 of 5 fill -> cancel doesn't confirm in time."""
    def __init__(self):
        self.trade = FakeTrade(9, "Submitted", 2, [fill(2)])

    def stock(self, *a):
        return NS()

    def place_order(self, *a, **k):
        return OrderResult(9, "CSCO", "BUY", 5, "LMT", 110.0, "Submitted", 0, float("nan"), "")

    def wait_for_orders(self, *a):
        pass

    def cancel_order(self, oid):
        return _IB._to_result(self.trade)

    def fill_details(self, oid):
        return 2.0, 110.0, 1.0, "USD"

    def order_result(self, oid):
        return _IB._to_result(self.trade)


def test_partial_fill_books_only_filled_and_warns(tmp_path):
    j = Journal(tmp_path / "j.sqlite"); j.init_budget(5000.0, "AUD")
    run = j.start_run("paper")
    d = Decision(Proposal("CSCO", "SMART", "USD", "BUY", 5, 110.0, "x"), True, [], 770.0, 1.4)
    logs = []
    (rep,) = execute([(j.record_decision(run, d), d)], FakeBroker(), j, {"USD": 1.4}, log=logs.append)
    assert rep.filled == 2 and j.holdings_qty() == {"CSCO:USD": 2}
    assert any("WARNING" in m for m in logs)
    (act,) = j.recent_activity()
    assert act["outcome"].startswith("partially filled 2/5")


# ---------------------------------------------------------------- journal recent activity
def test_recent_activity_outcomes_and_excludes_dry_runs(tmp_path):
    j = Journal(tmp_path / "j.sqlite"); j.init_budget(5000.0, "AUD")
    real, dry = j.start_run("paper"), j.start_run("paper-dry-fake")
    j.record_decision(real, Decision(Proposal("META", "SMART", "USD", "BUY", 3, 680.0, "x"), False, ["position too big"]))
    j.record_decision(dry, Decision(Proposal("CSCO", "SMART", "USD", "BUY", 1, 110.0, "x"), True, []))
    (a,) = j.recent_activity()
    assert a["order"].startswith("BUY 3 META") and "position too big" in a["outcome"]


# ---------------------------------------------------------------- agent loop
def agent(client, **kw):
    return TradingAgent(client, CFG, PortfolioState(5000.0, peak_value_base=5000.0), StubMarket(), {"CSCO"},
                        log=lambda *_: None, **kw)


def test_blocked_submit_is_bounced_once_then_accepted():
    a = agent(FakeClaude("META", 3, price_fn=lambda s: 680.0))   # 3 x 680 x 1.4 > 25% position limit
    res = a.run()
    results = [b for m in res.transcript if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    bounced = [r for r in results if r["is_error"] and "rejected" in r["content"]]
    assert len(bounced) == 1 and "position" in json.loads(bounced[0]["content"])["gate"][0]["block_reasons"][0]
    assert res.submitted and a.submit_revisions == 1          # second submit accepted; final gate drops it


def test_valid_submit_is_not_bounced():
    a = agent(FakeClaude("CSCO", 2, price_fn=lambda s: 110.0))
    assert a.run().submitted and a.submit_revisions == 0


def test_loop_time_budget_stops_with_no_trades():
    t = iter([0.0, 0.0, 400.0, 400.0, 400.0])
    res = agent(FakeClaude("CSCO", 2, price_fn=lambda s: 110.0), max_seconds=300, clock=lambda: next(t)).run()
    assert res.stop_reason == "time_budget_exceeded" and not res.submitted and res.proposals == []


def test_request_timeout_passed_and_capped_by_remaining_time():
    seen = []
    class Client(FakeClaude):
        def create(self, **kw):
            seen.append(kw["timeout"]); return super().create(**kw)
    t = iter([0.0, 0.0] + [280.0] * 20)
    agent(Client("CSCO", 2, price_fn=lambda s: 110.0), max_seconds=300, request_timeout_s=60, clock=lambda: next(t)).run()
    assert seen[0] == 60 and seen[1] == 20


def test_portfolio_tool_includes_recent_activity():
    recent = [{"date": "2026-09-22", "order": "BUY 3 META:USD @ 680", "outcome": "blocked by gate: position too big"}]
    out = json.loads(agent(FakeClaude(), recent_activity=recent)._dispatch("get_portfolio", {})[0])
    assert out["recent_activity"] == recent
