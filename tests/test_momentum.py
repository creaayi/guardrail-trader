"""T2b: momentum ranking computed by code from the bar cache; Claude only receives it."""
import importlib.util
import json
import sys
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

from guardrail_trader.bars import Bar, BarCache
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.momentum import atr, momentum_return, rank
from guardrail_trader.risk import Instrument, MomentumConfig, load_momentum_config

TODAY = date(2026, 10, 12)
CFG = MomentumConfig(top_n=2, lookback_days=126, skip_days=21, max_atr_pct=6.0, max_age_days=10,
                     min_traded_value=5_000_000.0)
FX = {"USD": 0.86, "EUR": 1.0, "GBP": 1.15}


def series(closes, last=TODAY - timedelta(days=3), spread=0.01):
    """Daily bars ending on `last`, high/low +-spread around the close."""
    n = len(closes)
    return [Bar((last - timedelta(days=n - 1 - i)).isoformat(), c, c * (1 + spread), c * (1 - spread), c, 1e6)
            for i, c in enumerate(closes)]


def linear(start, end, n=140):
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def inst(sym):
    return Instrument(sym, "SMART", "USD", f"{sym} Inc", "Technology", "US")


# ------------------------------------------------------------------ the score
def test_momentum_excludes_the_last_month():
    closes = [100.0] * 105 + [150.0] * 21                   # flat, then a jump inside the last month only
    closes = [100.0] + closes                               # 127 closes
    assert momentum_return(closes, 126, 21) == pytest.approx(0.0)
    closes[-22] = 120.0                                     # the bar 21 days ago counts
    assert momentum_return(closes, 126, 21) == pytest.approx(0.2)


def test_momentum_needs_lookback_plus_one_closes():
    assert momentum_return([100.0] * 126, 126, 21) is None


def test_wilder_atr_on_constant_true_range():
    b = series([100.0] * 30, spread=0.01)                   # high-low = 2.0 every day
    assert atr(b) == pytest.approx(2.0)


def test_atr_uses_gaps_from_the_previous_close():
    b = [Bar("2026-01-01", 100, 100, 100, 100, 1)] + [Bar(f"2026-01-{d:02d}", 110, 110, 110, 110, 1) for d in range(2, 17)]
    # true ranges: 10 (gap up from the previous close), then 14 x 0
    assert atr(b) == pytest.approx((10 / 14 * 13 + 0) / 14)   # seed = mean of the first 14, then Wilder step
    assert atr(b[:14]) is None                                 # 14 bars < n+1


# ------------------------------------------------------------------ the ranking
def test_ranking_orders_by_score_and_keeps_top_n():
    data = {"AAA:USD": series(linear(100, 130)), "BBB:USD": series(linear(100, 150)),
            "CCC:USD": series(linear(100, 110))}
    r = rank({k: inst(k[:3]) for k in data}, data.get, CFG, TODAY, FX)
    assert [x["symbol"] for x in r.top] == ["BBB", "AAA"] and [x["rank"] for x in r.top] == [1, 2]
    top = r.top[0]
    assert top["sector"] == "Technology" and top["country"] == "US" and top["momentum_pct"] > 0
    assert r.as_of == (TODAY - timedelta(days=3)).isoformat()


def test_volatile_names_are_filtered_out():
    data = {"CALM:USD": series(linear(100, 120)), "WILD:USD": series(linear(100, 200), spread=0.05)}  # ATR ~10%
    r = rank({k: inst(k.split(":")[0]) for k in data}, data.get, CFG, TODAY, FX)
    assert [x["symbol"] for x in r.top] == ["CALM"] and r.excluded == {"ATR14/price above 6.0%": 1}


def test_missing_short_or_stale_history_is_counted_never_filled_in():
    data = {"OK:USD": series(linear(100, 120)),
            "SHORT:USD": series(linear(100, 120, n=60)),
            "STALE:USD": series(linear(100, 120), last=TODAY - timedelta(days=30))}
    uni = {k: inst(k.split(":")[0]) for k in [*data, "NONE:USD"]}
    r = rank(uni, lambda k: data.get(k, []), CFG, TODAY, FX)
    assert [x["symbol"] for x in r.top] == ["OK"]
    assert r.excluded == {"no cached history": 1, "history too short": 1, "history older than 10 days": 1}


def test_empty_cache_gives_an_empty_ranking_that_says_so():
    r = rank({"AAA:USD": inst("AAA")}, lambda k: [], CFG, TODAY, FX)
    out = r.for_claude()
    assert out["top"] == [] and out["as_of"] is None and out["excluded_counts"] == {"no cached history": 1}


# ------------------------------------------------------------------ cache
def test_bar_cache_upserts_and_orders_refreshes(tmp_path):
    c = BarCache(tmp_path / "bars.sqlite")
    c.upsert("AAA:USD", series([1.0, 2.0, 3.0]))
    c.upsert("AAA:USD", [replace(series([1.0, 2.0, 3.0])[-1], close=9.0)])     # same date: replaced
    assert [b.close for b in c.bars("AAA:USD")] == [1.0, 2.0, 9.0]
    assert [b.close for b in c.bars("AAA:USD", limit=2)] == [2.0, 9.0]
    c.mark("AAA:USD", "ok")
    assert c.refresh_order(["AAA:USD", "BBB:USD"]) == ["BBB:USD", "AAA:USD"]      # never refreshed first
    assert c.latest_date("AAA:USD") == (TODAY - timedelta(days=3)).isoformat() and c.latest_date("X") is None
    c.close()


def test_refresh_bars_converts_pence_and_picks_the_duration():
    from guardrail_trader.bars import from_ibkr
    spec = importlib.util.spec_from_file_location("refresh_bars", PROJECT_ROOT / "scripts" / "refresh_bars.py")
    rb = importlib.util.module_from_spec(spec); sys.modules["refresh_bars"] = rb; spec.loader.exec_module(rb)
    (b,) = from_ibkr([NS(date=date(2026, 10, 9), open=7200.0, high=7300.0, low=7100.0, close=7250.0, volume=5e5)], 100)
    assert (b.date, b.open, b.high, b.low, b.close, b.volume) == ("2026-10-09", 72.0, 73.0, 71.0, 72.5, 5e5)
    assert rb.duration(None, TODAY) == "1 Y" and rb.duration("2026-10-09", TODAY) == "2 M"
    assert rb.duration("2026-07-01", TODAY) == "1 Y"


# ------------------------------------------------------------------ config + what Claude sees
def test_shipped_momentum_config():
    assert load_momentum_config() == MomentumConfig(40, 126, 21, 6.0, 10, 5_000_000.0)


@pytest.mark.parametrize("bad", ["top_n = 0", "top_n = 500", "skip_days = 120", "max_atr_pct = 0", "max_age_days = 90",
                                 "min_traded_value = 0"])
def test_invalid_momentum_config_refuses_to_load(tmp_path, bad):
    good = {"top_n": "40", "lookback_days": "126", "skip_days": "21", "max_atr_pct": "6.0", "max_age_days": "10",
            "min_traded_value": "5000000.0"}
    k, v = bad.split(" = ")
    good[k] = v
    p = tmp_path / "risk.toml"
    p.write_text("[momentum]\n" + "\n".join(f"{a} = {b}" for a, b in good.items()) + "\n")
    with pytest.raises(ValueError):
        load_momentum_config(p)


def test_claude_receives_the_ranking_in_its_task():
    from guardrail_trader.agent import TradingAgent
    from guardrail_trader.risk import PortfolioState, RiskConfig

    seen = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                seen["task"] = kw["messages"][0]["content"][0]["text"]
                return NS(content=[], stop_reason="end_turn", usage=NS(input_tokens=0, output_tokens=0))
    cfg = RiskConfig(1000.0, "EUR", 25.0, 10, 25.0, 2.0, 10.0, False, {})
    ranking = {"as_of": "2026-10-09", "top": [{"rank": 1, "symbol": "AAA", "momentum_pct": 30.0}]}
    TradingAgent(Client, cfg, PortfolioState(1000.0), NS(prices={}, fx={}), momentum=ranking,
                 log=lambda *_: None).run()
    assert "do not recompute" in seen["task"]
    assert json.loads(seen["task"].split("\n")[-1]) == ranking


def test_illiquid_names_are_excluded_from_the_ranking():
    def vol(bars, v):
        return [replace(b, volume=v) for b in bars]
    gbp = Instrument("VOD", "LSE", "GBP", "Vodafone", "Communications", "GB")
    data = {"LIQ:USD": vol(series(linear(100, 120)), 60_000),      # ~7 M USD a day
            "THIN:USD": vol(series(linear(100, 120)), 30_000),     # ~3.6 M USD
            "VOD:GBP": vol(series(linear(1.0, 1.2)), 3_000_000)}   # ~3.6 M GBP x 1.15 = 4.1 M EUR
    uni = {"LIQ:USD": inst("LIQ"), "THIN:USD": inst("THIN"), "VOD:GBP": gbp}
    r = rank(uni, data.get, CFG, TODAY, FX)
    assert [x["symbol"] for x in r.top] == ["LIQ"]
    assert r.excluded == {"20-day traded value below 5,000,000": 2}
    r = rank({"VOD:GBP": gbp}, data.get, CFG, TODAY, {})          # no FX: excluded, not guessed
    assert r.top == [] and r.excluded == {"no FX rate": 1}
