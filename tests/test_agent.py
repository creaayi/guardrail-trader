"""Agent loop with a fake Claude and a stub market: no broker, no API."""
import json
from types import SimpleNamespace as NS

from guardrail_trader.agent import MAX_HISTORY_CALLS, TradingAgent
from guardrail_trader.fake_llm import FakeClaude
from guardrail_trader.risk import Holding, Instrument, PortfolioState, RiskConfig

UNI = {i.key: i for i in [Instrument("CSCO", "SMART", "USD"), Instrument("META", "SMART", "USD")]}
CFG = RiskConfig(5000.0, "AUD", 25.0, 10, 25.0, 2.0, 10.0, False, UNI)


class StubMarket:
    prices = {"CSCO:USD": 110.0, "META:USD": 680.0}
    fx = {"USD": 1.4}
    history_calls = 0

    def history_summary(self, key):
        self.history_calls += 1
        return {"last_close": self.prices[key]}

    def commission_base(self, p):
        return 1.4


def agent(client, state=None):
    return TradingAgent(client, CFG, state or PortfolioState(5000.0, peak_value_base=5000.0), StubMarket(),
                        {"CSCO"}, log=lambda *_: None)


def test_full_script_submits_gate_checked_order():
    res = agent(FakeClaude("CSCO", 2, price_fn=lambda s: 110.0)).run()
    assert res.submitted and res.turns == 5
    (p,) = res.proposals
    assert (p.symbol, p.action, p.quantity, p.exchange) == ("CSCO", "BUY", 2, "SMART")


def test_universe_filters_unaffordable():
    a = agent(FakeClaude())
    out = a._universe({})  # max per holding A$1250; META = A$952 fits, CSCO fits
    assert "CSCO" in out and "META" in out
    a.max_pos_base = 200
    out = a._universe({})
    assert "CSCO" in out and "META" not in out


def test_history_call_limit_enforced():
    a = agent(FakeClaude())
    for _ in range(MAX_HISTORY_CALLS):
        _, err = a._dispatch("get_price_history", {"symbol": "CSCO"})
        assert not err
    out, err = a._dispatch("get_price_history", {"symbol": "CSCO"})
    assert err and "limit" in out


def test_non_allowlisted_symbol_is_a_tool_error_not_a_crash():
    out, err = agent(FakeClaude())._dispatch("get_price_history", {"symbol": "TSLA"})
    assert err and "allowlist" in out


def test_check_orders_reports_gate_blocks():
    out, err = agent(FakeClaude())._dispatch("check_orders", {"orders": [
        {"symbol": "META", "currency": "USD", "action": "BUY", "quantity": 3, "limit_price": 680.0, "reason": "x"}]})
    v = json.loads(out)[0]
    assert not err and not v["approved"] and any("position would be" in r for r in v["block_reasons"])


def test_claude_ending_without_submit_means_no_trades():
    class Quitter:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            return NS(content=[NS(type="text", text="I'll hold.")], stop_reason="end_turn",
                      usage=NS(input_tokens=1, output_tokens=1))

    res = agent(Quitter()).run()
    assert not res.submitted and res.proposals == []


def test_get_portfolio_shows_cost_basis_and_unrealized_pnl():
    state = PortfolioState(1000.0, {"CSCO:USD": Holding(4, 154.0), "META:USD": Holding(1, 900.0)}, peak_value_base=5000.0)
    a = TradingAgent(FakeClaude(), CFG, state, StubMarket(), set(), log=lambda *_: None,
                     avg_costs={"CSCO:USD": 140.0})
    rows = {h["symbol"]: h for h in json.loads(a._portfolio({}))["holdings"]}
    assert rows["CSCO"]["avg_cost"] == 140.0
    assert rows["CSCO"]["unrealized_pnl"] == 56.0          # (154 - 140) * 4
    assert rows["CSCO"]["unrealized_pnl_pct"] == 10.0
    assert "avg_cost" not in rows["META"]                   # no ledger cost known: omit, never invent


def test_order_schema_accepts_the_eur_universe_currencies():
    from guardrail_trader.agent import TOOLS
    submit = next(t for t in TOOLS if t["name"] == "submit_orders")
    assert submit["input_schema"]["properties"]["orders"]["items"]["properties"]["currency"]["enum"] == \
        ["USD", "EUR", "GBP", "CHF"]


def test_prompt_and_portfolio_show_the_t3_limits_and_exposures():
    import json as _json
    from dataclasses import replace

    uni = {i.key: i for i in [Instrument("CSCO", "SMART", "USD", "Cisco", "Technology", "US"),
                              Instrument("META", "SMART", "USD", "Meta", "Communications", "US")]}
    c = replace(CFG, universe=uni, min_position_pct=0.5, max_sector_pct=20.0, max_us_pct=70.0, max_equity_pct=90.0,
                reentry_cooldown_days=10)
    state = PortfolioState(3600.0, holdings={"CSCO:USD": Holding(10, 140.0)}, peak_value_base=5000.0,
                           recently_stopped={"META:USD": "2026-10-26"})
    a = TradingAgent(FakeClaude(), c, state, StubMarket(), log=lambda *_: None)
    p = _json.loads(a._portfolio({}))
    assert p["exposure_pct"] == {"equity": 28.0, "us": 28.0, "sectors": {"Technology": 28.0}}
    assert p["reentry_blocked_until"] == {"META:USD": "2026-10-26"}
