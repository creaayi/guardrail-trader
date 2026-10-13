"""T2: universe CSV format (fail-closed), hedge instruments, SMART routing, GBX prices."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from guardrail_trader.broker.ibkr import IBKRBroker, stock_contract, to_major, to_quote_units
from guardrail_trader.risk import Instrument, PortfolioState, Proposal, evaluate, load_risk_config
from guardrail_trader.risk.config import _read_universe_csv
from guardrail_trader.risk.gate import MarketInfo

HEADER = "symbol,exchange,currency,name,sector,country,source\n"


def _csv(tmp_path: Path, body: str, header: str = HEADER, name: str = "u.csv") -> Path:
    p = tmp_path / name
    p.write_text(header + body)
    return p


# ------------------------------------------------------------------ CSV parsing
def test_csv_parses_name_sector_country(tmp_path):
    p = _csv(tmp_path, 'SAP,ibis,eur,"SAP SE, Walldorf",Technology,de,STOXX 600\n'
                       "AAPL,SMART,USD,Apple Inc,Technology,US,S&P 500\n")
    sap, aapl = _read_universe_csv(p)
    assert sap == Instrument("SAP", "IBIS", "EUR", "SAP SE, Walldorf", "Technology", "DE")
    assert aapl.key == "AAPL:USD" and aapl.country == "US"


def test_csv_row_without_sector_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="sector"):
        _read_universe_csv(_csv(tmp_path, "SAP,IBIS,EUR,SAP SE,,DE,STOXX 600\n"))


@pytest.mark.parametrize("row,match", [
    ("SAP,IBIS,EUR,SAP SE,Technology,,x\n", "country"),
    ("SAP,IBIS,EUR,,Technology,DE,x\n", "name"),
    ("SAP,MIL,EUR,SAP SE,Technology,DE,x\n", "exchange"),        # not one of CLAUDE.md §3 exchanges
    ("SAP,IBIS,SEK,SAP SE,Technology,DE,x\n", "currency"),
    ("SAP,IBIS,EUR,SAP SE,Technology,Germany,x\n", "ISO"),
])
def test_csv_bad_rows_are_rejected(tmp_path, row, match):
    with pytest.raises(ValueError, match=match):
        _read_universe_csv(_csv(tmp_path, row))


def test_csv_in_the_old_format_is_rejected(tmp_path):
    p = _csv(tmp_path, "AAPL,SMART,USD,Apple,x\n", header="symbol,exchange,currency,name,source\n")
    with pytest.raises(ValueError, match="missing column"):
        _read_universe_csv(p)


def test_header_only_csv_is_an_empty_universe(tmp_path):
    assert _read_universe_csv(_csv(tmp_path, "")) == []


# ------------------------------------------------------------------ hedge instruments (pocket C)
def _cfg(tmp_path, enable_hedge=False, universe_rows="AAPL,SMART,USD,Apple Inc,Technology,US,x\n"):
    _csv(tmp_path, universe_rows, name="core.csv")
    _csv(tmp_path, "IWDA,AEB,EUR,iShares Core MSCI World,Funds,IE,x\n", name="hedge.csv")
    toml = tmp_path / "risk.toml"
    toml.write_text(f'''[budget]
capital = 100000.0
currency = "EUR"
[limits]
max_position_pct = 25.0
max_trades_per_month = 10
max_drawdown_pct = 25.0
max_price_deviation_pct = 2.0
max_commission_pct = 10.0
min_position_pct = 0.5
max_sector_pct = 100.0
max_us_pct = 100.0
max_equity_pct = 100.0
reentry_cooldown_days = 10
drawdown_pause_pct = 8.0
drawdown_pause_days = 5
drawdown_reduce_pct = 12.0
reduce_max_equity_pct = 50.0
drawdown_resume_pct = 4.0
daily_loss_halt_pct = 3.0
stop_atr_mult = 2.0
stop_min_pct = 6.0
stop_max_pct = 12.0
breakeven_atr_mult = 1.0
trail_atr_mult = 2.0
[universe]
files = ["{tmp_path / 'core.csv'}"]
hedge_files = ["{tmp_path / 'hedge.csv'}"]
enable_hedge = {str(enable_hedge).lower()}
pockets = ["A"]
''')
    return load_risk_config(toml)       # absolute file paths: PROJECT_ROOT / "/abs" == "/abs"


def test_hedge_instruments_are_loaded_but_not_tradeable_by_default(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.enable_hedge is False and set(cfg.hedge) == {"IWDA:EUR"} and set(cfg.universe) == {"AAPL:USD"}


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_gate_rejects_any_order_on_a_hedge_ticker(tmp_path, action):
    cfg = _cfg(tmp_path)
    state = PortfolioState(cash_base=100000.0, peak_value_base=100000.0)
    p = Proposal("IWDA", "AEB", "EUR", action, 10, 100.0)
    (d,), _ = evaluate([p], state, {"IWDA:EUR": MarketInfo(100.0, 1.0, 3.0)}, cfg)
    assert not d.approved and "hedge" in d.reasons[0]


def test_enable_hedge_makes_them_ordinary_universe_instruments(tmp_path):
    cfg = _cfg(tmp_path, enable_hedge=True)
    assert "IWDA:EUR" in cfg.universe


def test_a_ticker_both_in_the_universe_and_hedge_files_refuses_to_load(tmp_path):
    with pytest.raises(ValueError, match="both"):
        _cfg(tmp_path, universe_rows="IWDA,AEB,EUR,iShares Core MSCI World,Funds,IE,x\n")


def test_enable_hedge_must_be_a_boolean(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(ValueError, match="enable_hedge"):
        _cfg(tmp_path / "x", enable_hedge='"no"')


def test_list_universe_shows_name_sector_country_and_never_hedge(tmp_path):
    from guardrail_trader.agent import TradingAgent
    cfg = _cfg(tmp_path, universe_rows='AAPL,SMART,USD,"Apple, Inc.",Technology,US,x\n')
    market = NS(prices={"AAPL:USD": 200.0, "IWDA:EUR": 100.0}, fx={"USD": 0.86, "EUR": 1.0})
    a = TradingAgent(None, cfg, PortfolioState(100000.0, peak_value_base=100000.0), market, log=lambda *_: None)
    out = a._universe({}).splitlines()
    assert out[0] == "symbol,currency,name,sector,country,price,price_base,my_pick"
    assert out[1] == 'AAPL,USD,"Apple, Inc.",Technology,US,200.00,172.00,false'
    assert len(out) == 2                                   # IWDA is priced but never listed


def test_shipped_universe_files_load():
    cfg = load_risk_config()
    assert cfg.hedge["CSPX:USD"].exchange == "LSEETF"          # London ETFs: IBKR primary exchange LSEETF
    assert {"IWDA:EUR", "CSPX:USD", "EXSA:EUR"} <= set(cfg.hedge) and not cfg.enable_hedge
    assert all(i.sector and i.country for i in [*cfg.universe.values(), *cfg.hedge.values()])


# ------------------------------------------------------------------ routing
def test_us_stock_routes_smart_without_primary_exchange():
    c = stock_contract("aapl", "SMART", "usd")
    assert (c.symbol, c.exchange, c.currency, c.primaryExchange) == ("AAPL", "SMART", "USD", "")


@pytest.mark.parametrize("ex", ["SBF", "AEB", "IBIS", "LSE", "LSEETF", "EBS"])
def test_european_stock_routes_smart_with_its_primary_exchange(ex):
    c = stock_contract("XYZ", ex, "EUR")
    assert c.exchange == "SMART" and c.primaryExchange == ex


# ------------------------------------------------------------------ GBX (LSE quotes in pence)
def test_magnifier_conversion_helpers():
    assert to_major(7250.0, 100) == 72.5 and to_major(72.5, 1) == 72.5
    assert to_quote_units(72.5, 100) == 7250.0 and to_quote_units(72.5, 1) == 72.5


class _Ticker:
    def __init__(self, px):
        self.px, self.close, self.bid, self.ask, self.marketDataType = px, px, px - 1, px + 1, 3

    def marketPrice(self):
        return self.px


def _gbx_broker(magnifier=100):
    """A broker whose IBKR quotes VOD (LSE, GBP) in pence and records what it is sent."""
    sent = {}
    trade = NS(contract=NS(symbol="VOD", currency="GBP", conId=1, secType="STK"),
               order=NS(orderId=5, action="BUY", totalQuantity=100, orderType="LMT", lmtPrice=7250.0),
               orderStatus=NS(status="Filled", filled=100, avgFillPrice=7249.0), log=[],
               fills=[NS(execution=NS(shares=100, price=7249.0), commissionReport=NS(execId="e1", commission=3.0,
                                                                                    currency="GBP"))],
               isDone=lambda: True)

    def place(contract, order):
        sent["lmt"] = order.lmtPrice
        return trade
    ib = NS(reqContractDetails=lambda c: [NS(priceMagnifier=magnifier)], reqMktData=lambda c: _Ticker(7250.0),
            cancelMktData=lambda c: None, sleep=lambda s: None, placeOrder=place, trades=lambda: [trade],
            whatIfOrder=lambda c, o: sent.setdefault("whatif", o.lmtPrice))
    b = IBKRBroker.__new__(IBKRBroker)
    b.ib, b.account_id, b.readonly, b._magnifiers, b._details, b._rules = ib, "DU1234567", False, {}, {}, {}
    b.cfg = NS(mode="paper")
    return b, trade.contract, sent


def test_gbx_quote_is_converted_from_pence_to_pounds():
    b, vod, _ = _gbx_broker()
    q = b.quote(vod, wait_seconds=0)
    assert (q.price, q.close, q.bid, q.ask) == (72.5, 72.5, 72.49, 72.51)


def test_gbx_limit_order_is_sent_in_pence_and_reported_in_pounds():
    b, vod, sent = _gbx_broker()
    r = b.place_order(vod, "BUY", 100, limit_price=72.5, wait_seconds=0)
    assert sent["lmt"] == 7250.0                           # IBKR receives pence
    assert r.limit_price == 72.5 and r.avg_fill_price == 72.49
    qty, avg, comm, ccy = b.fill_details(5)
    assert (qty, avg, comm, ccy) == (100.0, 72.49, 3.0, "GBP")   # commission is in GBP already


def test_gbp_stock_with_magnifier_1_is_left_alone():
    b, vod, sent = _gbx_broker(magnifier=1)
    assert b.quote(vod, wait_seconds=0).price == 7250.0
    b.place_order(vod, "BUY", 1, limit_price=7250.0, wait_seconds=0)
    assert sent["lmt"] == 7250.0


def test_fx_pairs_and_non_gbp_stocks_never_look_up_a_magnifier():
    b, _, _ = _gbx_broker()
    b.ib.reqContractDetails = lambda c: pytest.fail("no contract details needed")
    assert b.magnifier(NS(secType="CASH", currency="GBP", conId=9)) == 1     # EURGBP
    assert b.magnifier(NS(secType="STK", currency="EUR", conId=9)) == 1
