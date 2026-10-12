"""T4: drawdown tiers, half-size recovery, daily loss halt. The kill switch itself is unchanged."""
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal
from guardrail_trader.risk import (
    Holding, Instrument, MarketInfo, PortfolioState, Proposal, RiskConfig, evaluate, kill_switch_triggered,
    load_risk_config,
)
from guardrail_trader.risk.gate import NORMAL, PAUSED, RECOVERY, REDUCED, next_tier

UNI = {i.key: i for i in [Instrument("AAA", "SBF", "EUR", "Aaa", "Energy", "FR"),
                          Instrument("BBB", "IBIS", "EUR", "Bbb", "Technology", "DE")]}
MKT = {k: MarketInfo(10.0, 1.0, 0.0) for k in UNI}
CFG = RiskConfig(1000.0, "EUR", 2.0, 60, 15.0, 0.5, 1.0, False, UNI, min_position_pct=0.5, max_sector_pct=100.0,
                 max_us_pct=100.0, max_equity_pct=90.0, drawdown_pause_pct=8.0, drawdown_pause_days=5,
                 drawdown_reduce_pct=12.0, reduce_max_equity_pct=50.0, drawdown_resume_pct=4.0,
                 daily_loss_halt_pct=3.0)
MON = date(2026, 10, 12)


# ------------------------------------------------------------------ transitions (pure)
@pytest.mark.parametrize("tier,until,dd,today,expected", [
    (NORMAL, None, 7.9, MON, (NORMAL, None)),
    (NORMAL, None, 8.0, MON, (PAUSED, "2026-10-19")),                 # 5 business days after Monday
    (PAUSED, "2026-10-19", 9.0, date(2026, 10, 19), (PAUSED, "2026-10-19")),   # last day of the pause
    (PAUSED, "2026-10-19", 9.0, date(2026, 10, 20), (RECOVERY, None)),  # pause over, still -9%: half size
    (PAUSED, "2026-10-19", 3.0, date(2026, 10, 20), (NORMAL, None)),    # pause over and recovered
    (RECOVERY, None, 9.5, MON, (RECOVERY, None)),                       # never re-pauses
    (RECOVERY, None, 4.0, MON, (RECOVERY, None)),
    (RECOVERY, None, 3.99, MON, (NORMAL, None)),
    (NORMAL, None, 12.0, MON, (REDUCED, None)),
    (PAUSED, "2026-10-19", 12.5, MON, (REDUCED, None)),
    (RECOVERY, None, 13.0, MON, (REDUCED, None)),
    (REDUCED, None, 11.9, MON, (RECOVERY, None)),                       # left the -12% tier: half size
    (REDUCED, None, 3.0, MON, (NORMAL, None)),
])
def test_tier_transitions(tier, until, dd, today, expected):
    assert next_tier(tier, until, dd, today, CFG) == expected


def test_a_full_episode_pause_then_half_size_then_normal():
    tier, until, seen = NORMAL, None, []
    for day, dd in [(date(2026, 10, 12), 8.5), (date(2026, 10, 16), 9.0), (date(2026, 10, 19), 7.0),
                    (date(2026, 10, 20), 6.0), (date(2026, 10, 27), 4.5), (date(2026, 10, 28), 3.5)]:
        tier, until = next_tier(tier, until, dd, day, CFG)
        seen.append(tier)
    assert seen == [PAUSED, PAUSED, PAUSED, RECOVERY, RECOVERY, NORMAL]


# ------------------------------------------------------------------ the gate
def buy(sym, qty):
    i = next(i for i in UNI.values() if i.symbol == sym)
    return Proposal(sym, i.exchange, i.currency, "BUY", qty, 10.0)


def sell(sym, qty):
    i = next(i for i in UNI.values() if i.symbol == sym)
    return Proposal(sym, i.exchange, i.currency, "SELL", qty, 10.0)


def state(**kw):
    base = dict(cash_base=900.0, holdings={"AAA:EUR": Holding(10, 10.0)}, peak_value_base=1000.0)
    base.update(kw)
    return PortfolioState(**base)


def test_paused_blocks_buys_not_sells():
    s = state(tier=PAUSED, pause_until="2026-10-19")
    (d,) = evaluate([buy("BBB", 1)], s, MKT, CFG)[0]
    assert not d.approved and any("2026-10-19" in r for r in d.reasons)
    assert evaluate([sell("AAA", 10)], s, MKT, CFG)[0][0].approved


def test_recovery_halves_the_max_position():
    assert evaluate([buy("BBB", 2)], state(), MKT, CFG)[0][0].approved                     # 2% normal
    (d,) = evaluate([buy("BBB", 2)], state(tier=RECOVERY), MKT, CFG)[0]                    # 2% > 1%
    assert not d.approved and any("half size" in r for r in d.reasons)
    assert evaluate([buy("BBB", 1)], state(tier=RECOVERY), MKT, CFG)[0][0].approved        # 1%


def test_reduced_blocks_buys_while_equity_is_above_50pct():
    heavy = state(cash_base=400.0, holdings={"AAA:EUR": Holding(60, 10.0)})               # 60% equity
    (d,) = evaluate([buy("BBB", 1)], replace(heavy, tier=REDUCED), MKT, CFG)[0]
    assert not d.approved and any("REDUCED" in r for r in d.reasons)
    assert evaluate([buy("BBB", 1)], heavy, MKT, CFG)[0][0].approved                     # same, NORMAL tier
    light = state(cash_base=600.0, holdings={"AAA:EUR": Holding(40, 10.0)}, tier=REDUCED)  # 40% equity
    assert evaluate([buy("BBB", 1)], light, MKT, CFG)[0][0].approved


def test_reduced_never_forces_a_sale():
    """Passive tier: the gate only judges what Claude proposes; nothing generates sells."""
    heavy = state(cash_base=400.0, holdings={"AAA:EUR": Holding(60, 10.0)}, tier=REDUCED)
    decisions, after = evaluate([], heavy, MKT, CFG)
    assert decisions == [] and after.holdings["AAA:EUR"].quantity == 60


def test_daily_loss_halt_blocks_every_order_including_sells():
    s = state(daily_halt="daily loss halt: the previous day lost 3.50%")
    for p in (buy("BBB", 1), sell("AAA", 10)):
        (d,) = evaluate([p], s, MKT, CFG)[0]
        assert not d.approved and "daily loss halt" in d.reasons[0]


def test_kill_switch_unchanged_at_15pct():
    assert not kill_switch_triggered(PortfolioState(cash_base=851.0, peak_value_base=1000.0), CFG)
    assert kill_switch_triggered(PortfolioState(cash_base=850.0, peak_value_base=1000.0), CFG)
    assert load_risk_config().max_drawdown_pct == 15.0


# ------------------------------------------------------------------ config
def _toml(tmp_path: Path, **sub) -> Path:
    text = (PROJECT_ROOT / "config" / "risk.toml").read_text()
    for k, v in sub.items():
        text = re.sub(rf"(?m)^{k} = [^#\n]+", f"{k} = {v} ", text) if v is not None else \
            re.sub(rf"(?m)^{k} = .*\n", "", text)
    p = tmp_path / "risk.toml"
    p.write_text(text.replace('"config/universe/', f'"{PROJECT_ROOT}/config/universe/'))
    return p


@pytest.mark.parametrize("sub", [
    {"drawdown_pause_pct": "13.0"},            # pause above reduce
    {"drawdown_reduce_pct": "15.0"},           # reduce not below the kill switch
    {"drawdown_resume_pct": "8.0"},            # resume not below pause
    {"drawdown_pause_days": "0"},
    {"drawdown_pause_days": "2.5"},
    {"reduce_max_equity_pct": "95.0"},         # above max_equity_pct
    {"daily_loss_halt_pct": "0"},
    {"min_position_pct": "1.0"},               # not below half of max_position_pct (2.0)
])
def test_tier_keys_are_validated(tmp_path, sub):
    with pytest.raises(ValueError):
        load_risk_config(_toml(tmp_path, **sub))


@pytest.mark.parametrize("key", ["drawdown_pause_pct", "drawdown_pause_days", "drawdown_reduce_pct",
                                 "reduce_max_equity_pct", "drawdown_resume_pct", "daily_loss_halt_pct"])
def test_tier_keys_are_required(tmp_path, key):
    with pytest.raises(KeyError):
        load_risk_config(_toml(tmp_path, **{key: None}))


# ------------------------------------------------------------------ journal
@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(1000.0, "EUR")
    yield jr
    jr.close()


def test_daily_nav_keeps_the_last_run_of_the_day(j):
    j.record_nav(1000.0, date(2026, 10, 12))
    j.record_nav(990.0, date(2026, 10, 12))
    assert [tuple(r) for r in j.db.execute("SELECT value_base FROM nav_daily")] == [(990.0,)]


def test_previous_day_loss_is_close_to_close(j):
    assert j.previous_day_loss_pct(date(2026, 10, 14)) is None             # no data: not a loss
    j.record_nav(1000.0, date(2026, 10, 12))
    j.record_nav(965.0, date(2026, 10, 13))                                 # -3.5%
    j.record_nav(900.0, date(2026, 10, 14))                                 # today: not used yet
    assert j.previous_day_loss_pct(date(2026, 10, 14)) == pytest.approx(3.5)
    assert j.previous_day_loss_pct(date(2026, 10, 15)) == pytest.approx(100 - 900 / 965 * 100)


def test_tier_is_stored_logged_and_reset_with_the_kill_switch(j):
    assert j.tier() == (NORMAL, None)
    assert j.set_tier(PAUSED, "2026-10-19", 8.2) is True
    assert j.set_tier(PAUSED, "2026-10-19", 8.4) is False                  # unchanged: no log row
    assert j.tier() == (PAUSED, "2026-10-19")
    assert [tuple(r) for r in j.db.execute("SELECT from_tier, to_tier FROM tier_log")] == [(NORMAL, PAUSED)]
    j.reset_halt(1000.0)
    assert j.tier() == (NORMAL, None)


def test_unknown_stored_tier_fails_closed(j):
    j._set("tier", "WHATEVER")
    with pytest.raises(ValueError):
        j.tier()
