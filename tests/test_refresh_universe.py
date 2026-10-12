"""scripts/refresh_universe.py: parsing and CLAUDE.md §3 filters, without network or IBKR."""
import importlib.util
import sys
from types import SimpleNamespace as NS

import pytest

from guardrail_trader.bars import Bar, avg_traded_value, from_ibkr
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.risk.config import _read_universe_csv

spec = importlib.util.spec_from_file_location("refresh_universe", PROJECT_ROOT / "scripts" / "refresh_universe.py")
ru = importlib.util.module_from_spec(spec)
sys.modules["refresh_universe"] = ru      # dataclasses need the module registered
spec.loader.exec_module(ru)


def bars(n, close=100.0, volume=100_000):
    return [Bar("2026-10-09", close, close, close, close, volume) for _ in range(n)]


def test_ibkr_symbol_for_share_classes():
    assert ru.ibkr_symbol("BRK.B") == "BRK B" and ru.ibkr_symbol(" aapl ") == "AAPL"


def test_listing_filters():
    ok = bars(126)                                          # 100 x 100k = 10 M a day
    assert ru.keep_or_reason(ok, 1.0, "Technology", 5e6) == ""
    assert "sector" in ru.keep_or_reason(ok, 1.0, "", 5e6)
    assert "6 months" in ru.keep_or_reason(bars(60), 1.0, "Technology", 5e6)
    assert "traded value" in ru.keep_or_reason(bars(126, volume=40_000), 1.0, "Technology", 5e6)   # 4 M


def test_traded_value_from_ibkr_bars_converts_pence_and_currency():
    # LSE: 7,250 pence x 100k shares = 7.25 M GBP; x 1.15 EUR per GBP = 8.34 M EUR >= 5 M
    raw = [NS(date="20261009", open=7250.0, high=7250.0, low=7250.0, close=7250.0, volume=100_000)] * 126
    gbp = from_ibkr(raw, 100)
    assert avg_traded_value(gbp) * 1.15 == pytest.approx(8_337_500.0)
    assert ru.keep_or_reason(gbp, 1.15, "Communications", 5e6) == ""
    # 40k shares: 2.9 M GBP = 3.3 M EUR -> too illiquid
    thin = from_ibkr([NS(date="20261009", open=7250.0, high=7250.0, low=7250.0, close=7250.0, volume=40_000)] * 126, 100)
    assert "traded value" in ru.keep_or_reason(thin, 1.15, "Communications", 5e6)


def test_traded_value_uses_the_last_20_bars():
    b = bars(100, volume=1) + bars(20, volume=100_000)
    assert avg_traded_value(b) == pytest.approx(10_000_000.0)


def _detail(primary, sec="STK"):
    return NS(contract=NS(primaryExchange=primary, secType=sec))


def test_choose_listing_takes_the_expected_primary_exchange_only():
    lse, aeb, mil = _detail("LSE"), _detail("AEB"), _detail("BVME")
    assert ru.choose_listing([aeb, lse], "LSE") is lse
    assert ru.choose_listing([mil], "BVME") is None          # Milan is not in CLAUDE.md §3
    assert ru.choose_listing([aeb], "LSE") is None            # never guess another listing


def test_written_csv_is_a_valid_universe_file(tmp_path):
    out = tmp_path / "eu_core.csv"
    rows = [["SAP", "IBIS", "EUR", "SAP SE, Walldorf", "Technology", "DE", "STOXX 600 DE0007164600; cap unverified"]]
    ru.write_csv(out, rows, min_rows=1)
    (sap,) = _read_universe_csv(out)
    assert (sap.symbol, sap.exchange, sap.name, sap.country) == ("SAP", "IBIS", "SAP SE, Walldorf", "DE")


def test_short_list_never_overwrites_the_file(tmp_path):
    out = tmp_path / "us_core.csv"
    out.write_text("keep me")
    with pytest.raises(SystemExit):
        ru.write_csv(out, [["AAPL"]], min_rows=400)
    assert out.read_text() == "keep me"


def test_parse_us_merges_indices_and_keeps_one_share_class():
    pytest.importorskip("pandas")
    pytest.importorskip("lxml")
    sp = ("<table><tr><th>Symbol</th><th>Security</th></tr><tr><td>AAPL</td><td>Apple Inc.</td></tr>"
          "<tr><td>GOOGL</td><td>Alphabet (A)</td></tr><tr><td>GOOG</td><td>Alphabet (C)</td></tr>"
          "<tr><td>BRK.B</td><td>Berkshire Hathaway</td></tr></table>")
    ndx = ("<table><tr><th>Ticker</th><th>Company</th></tr><tr><td>AAPL</td><td>Apple Inc.</td></tr>"
           "<tr><td>ASML</td><td>ASML Holding</td></tr></table>")
    c = {x.symbol: x for x in ru.parse_us(sp, ndx)}
    assert set(c) == {"AAPL", "GOOGL", "BRK B", "ASML"}
    assert c["AAPL"].index == "S&P 500 + Nasdaq-100" and c["ASML"].index == "Nasdaq-100"


def test_parse_stoxx600_keeps_equities_and_maps_exchanges(tmp_path):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    rows = [["disclaimer"] + [None] * 6,
            ["Name", "ISIN", "Country", "Currency", "Exchange", "Type of Security", "Weighting"],
            ["ASML HOLDING", "NL0010273215", "Niederlande", "EUR", "Euronext Amsterdam", "Aktien", 0.05],
            ["L AIR LIQUIDE", "FR0000120073", "Frankreich", "EUR", "-", "Aktien", 0.01],
            ["ENI", "IT0003132476", "Italien", "EUR", "Milan Stock Exchange", "Aktien", 0.01],
            ["EUR CASH", "-", "-", "EUR", "-", "Cash", 0.001]]
    path = tmp_path / "sxxp.xlsx"
    pd.DataFrame(rows).to_excel(path, header=False, index=False)
    c = {x.isin: x for x in ru.parse_stoxx600(path.read_bytes())}
    assert set(c) == {"NL0010273215", "FR0000120073", "IT0003132476"}
    assert c["NL0010273215"].hint == "AEB" and c["FR0000120073"].hint == "SBF"   # "-" -> ISIN country
    assert c["IT0003132476"].hint == ""                                          # Milan: skipped later
