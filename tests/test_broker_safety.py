"""Safety checks must hold without a broker connection."""
import pytest

from guardrail_trader.broker import (
    BrokerNotReadyError, BrokerSafetyError, IBKRBroker, check_account, check_endpoint,
)
from guardrail_trader.config import IBKRConfig


@pytest.mark.parametrize("port", [4001, 7496, 4003, 14001])
def test_paper_mode_refuses_live_ports(port):
    with pytest.raises(BrokerSafetyError):
        check_endpoint("paper", port)


@pytest.mark.parametrize("port", [4002, 7497, 4004, 14002])
def test_live_mode_refuses_paper_ports(port):
    with pytest.raises(BrokerSafetyError):
        check_endpoint("live", port)


def test_matching_ports_pass():
    check_endpoint("paper", 4002)
    check_endpoint("live", 4001)
    check_endpoint("paper", 14002)     # docker host port used by scripts run on the VPS


def test_paper_mode_refuses_live_account():
    with pytest.raises(BrokerSafetyError):
        check_account("paper", "U1234567")


def test_live_mode_refuses_paper_account():
    with pytest.raises(BrokerSafetyError):
        check_account("live", "DU1234567")


def test_matching_accounts_pass():
    check_account("paper", "DU1234567")
    check_account("live", "U1234567")


def test_connect_refuses_live_port_before_any_network_call():
    cfg = IBKRConfig(mode="paper", host="127.0.0.1", port=4001, client_id=99, market_data_type=3)
    with pytest.raises(BrokerSafetyError):
        IBKRBroker(cfg).connect()


def test_readonly_broker_cannot_place_orders():
    cfg = IBKRConfig(mode="paper", host="127.0.0.1", port=4002, client_id=99, market_data_type=3)
    b = IBKRBroker(cfg, readonly=True)
    b.account_id = "DU1234567"
    with pytest.raises(BrokerSafetyError):
        b.place_order(contract=None, action="BUY", quantity=1, limit_price=1.0)


@pytest.mark.parametrize("action,qty,lmt", [("HOLD", 1, None), ("BUY", 0, None), ("BUY", -1, None), ("BUY", 1, -5.0)])
def test_invalid_orders_rejected(action, qty, lmt):
    cfg = IBKRConfig(mode="paper", host="127.0.0.1", port=4002, client_id=99, market_data_type=3)
    with pytest.raises(ValueError):
        IBKRBroker(cfg)._build_order(action, qty, lmt)


def test_context_manager_does_not_reconnect_when_already_connected(monkeypatch):
    """Regression: `with broker.connect():` opened a 2nd socket with the same clientId."""
    cfg = IBKRConfig(mode="paper", host="127.0.0.1", port=4002, client_id=99, market_data_type=3)
    b = IBKRBroker(cfg)
    monkeypatch.setattr(b.ib, "isConnected", lambda: True)
    monkeypatch.setattr(b.ib, "disconnect", lambda: None)
    monkeypatch.setattr(b, "connect", lambda: pytest.fail("connect() called twice"))
    with b as same:
        assert same is b


# -- gateway connected but not serving data (IBKR's nightly re-login) -------------------------
class _Summary:
    def __init__(self, tag, value, currency="AUD"):
        self.tag, self.value, self.currency = tag, value, currency


def _broker(summary_rows):
    from types import SimpleNamespace as NS
    b = IBKRBroker.__new__(IBKRBroker)
    b.ib, b.account_id = NS(accountSummary=lambda _a: summary_rows), "DU1234567"
    return b


def test_ensure_ready_passes_when_the_account_summary_has_data():
    snap = _broker([_Summary("NetLiquidation", "5000.0"), _Summary("TotalCashValue", "100.0"),
                    _Summary("AvailableFunds", "100.0")]).ensure_ready()
    assert snap.net_liquidation == 5000.0 and snap.currency == "AUD"


@pytest.mark.parametrize("rows", [[], [_Summary("NetLiquidation", "0.0")], [_Summary("TotalCashValue", "10.0")]])
def test_ensure_ready_raises_when_requests_time_out(rows):
    """Empty/zero account summary = requests timed out; positions would come back empty too."""
    with pytest.raises(BrokerNotReadyError):
        _broker(rows).ensure_ready()


# -- capital guard and FX to the EUR base (CHANGELOG 12/10/2026) ------------------------------
from guardrail_trader.broker import check_capital  # noqa: E402


def test_capital_guard_passes_when_the_account_covers_the_capital():
    # 1 M USD paper account at 0.86 EUR per USD covers an 800k EUR budget
    assert check_capital(1_000_000.0, 0.86, 800_000.0, "EUR") == pytest.approx(860_000.0)


@pytest.mark.parametrize("net_liq,fx", [(900_000.0, 0.86), (float("nan"), 0.86), (1_000_000.0, float("nan"))])
def test_capital_guard_refuses_a_smaller_or_unknown_account(net_liq, fx):
    with pytest.raises(BrokerSafetyError):
        check_capital(net_liq, fx, 800_000.0, "EUR")


def _fx_broker(quotes: dict[str, float]):
    """Broker whose IDEALPRO only knows `quotes` (pair -> price)."""
    from types import SimpleNamespace as NS
    b = IBKRBroker.__new__(IBKRBroker)

    def fx_contract(pair):
        if pair not in quotes:
            raise ValueError(f"Unknown FX pair {pair}")
        return pair
    b.fx_contract = fx_contract
    b.quote = lambda pair: NS(price=quotes[pair])
    return b


@pytest.mark.parametrize("ccy,pair,price,expected", [
    ("USD", "EURUSD", 1.16, 1 / 1.16),
    ("GBP", "EURGBP", 0.87, 1 / 0.87),
    ("CHF", "EURCHF", 0.94, 1 / 0.94),
])
def test_fx_to_eur_inverts_the_eur_quoted_pair(ccy, pair, price, expected):
    b = _fx_broker({"EURUSD": 1.16, "EURGBP": 0.87, "EURCHF": 0.94})
    assert b.fx_to_base(ccy, "EUR") == pytest.approx(expected)


def test_fx_to_eur_uses_the_direct_pair_when_only_it_exists():
    assert _fx_broker({"USDEUR": 0.86}).fx_to_base("USD", "EUR") == pytest.approx(0.86)


def test_fx_to_base_is_one_for_the_base_currency():
    assert _fx_broker({}).fx_to_base("eur", "EUR") == 1.0


def test_fx_without_any_quote_is_market_data_unavailable():
    from guardrail_trader.broker import MarketDataUnavailableError
    with pytest.raises(MarketDataUnavailableError):
        _fx_broker({}).fx_to_base("CHF", "EUR")
