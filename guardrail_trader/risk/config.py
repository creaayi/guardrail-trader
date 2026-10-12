"""Load and validate config/risk.toml. Invalid limits refuse to load (fail-closed)."""
from __future__ import annotations

import csv
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from guardrail_trader.config import PROJECT_ROOT

DEFAULT_RISK_PATH = PROJECT_ROOT / "config" / "risk.toml"

# CLAUDE.md §3: US via SMART; Europe via SMART routing with these primary exchanges.
EXCHANGES = {"SMART", "SBF", "AEB", "IBIS", "LSE", "EBS"}
CURRENCIES = {"USD", "EUR", "GBP", "CHF"}
UNIVERSE_COLUMNS = ("symbol", "exchange", "currency", "name", "sector", "country", "source")


def instrument_key(symbol: str, currency: str) -> str:
    """One canonical key per instrument, used by the gate, journal and broker glue."""
    return f"{symbol.upper()}:{currency.upper()}"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str   # SMART (US) or the primary exchange (SBF, AEB, IBIS, LSE, EBS); orders route via SMART
    currency: str
    name: str = ""
    sector: str = ""
    country: str = ""   # ISO 3166 alpha-2; "US" = US market (for max_us_pct)

    @property
    def key(self) -> str:
        return instrument_key(self.symbol, self.currency)


@dataclass(frozen=True)
class RiskConfig:
    capital: float
    base_currency: str
    max_position_pct: float
    max_trades_per_month: int
    max_drawdown_pct: float
    max_price_deviation_pct: float
    max_commission_pct: float
    allow_fractional: bool
    universe: dict[str, Instrument]  # key -> Instrument (what may be traded)
    # Pocket C instruments (hedge.csv). Never in `universe` and always blocked by the gate unless enabled.
    hedge: dict[str, Instrument] = field(default_factory=dict)
    enable_hedge: bool = False


@dataclass(frozen=True)
class MomentumConfig:
    """Pocket A ranking ([momentum] in risk.toml). A selection rule, validated like the limits."""
    top_n: int = 40
    lookback_days: int = 126     # ~6 months of trading days
    skip_days: int = 21          # ~1 month excluded (short-term reversal)
    max_atr_pct: float = 6.0     # ATR14/price cap: above it a 2xATR stop is always clipped by the -12% floor
    max_age_days: int = 10       # cached history older than this (calendar days) is not used


def load_momentum_config(path: Path | str = DEFAULT_RISK_PATH) -> MomentumConfig:
    with open(path, "rb") as f:
        m = tomllib.load(f).get("momentum")
    if m is None:
        raise ValueError("[momentum] section missing in risk.toml")
    cfg = MomentumConfig(top_n=int(m["top_n"]), lookback_days=int(m["lookback_days"]), skip_days=int(m["skip_days"]),
                         max_atr_pct=_pct("max_atr_pct", m["max_atr_pct"], hi=20.0),
                         max_age_days=int(m["max_age_days"]))
    if not 1 <= cfg.top_n <= 100:
        raise ValueError(f"momentum.top_n must be in [1, 100], got {cfg.top_n}")
    if not 0 <= cfg.skip_days < cfg.lookback_days - 20 or not 60 <= cfg.lookback_days <= 260:
        raise ValueError(f"momentum lookback_days {cfg.lookback_days} / skip_days {cfg.skip_days} out of range")
    if not 1 <= cfg.max_age_days <= 31:
        raise ValueError(f"momentum.max_age_days must be in [1, 31], got {cfg.max_age_days}")
    return cfg


def _pct(name: str, v, lo: float = 0.0, hi: float = 100.0) -> float:
    v = float(v)
    if not (lo < v <= hi):
        raise ValueError(f"{name} must be in ({lo}, {hi}], got {v}")
    return v


def _read_universe_csv(path: Path) -> list[Instrument]:
    """Fail-closed: a missing column or field, or an unknown exchange/currency, refuses the whole file."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in UNIVERSE_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing column(s) {missing}; expected {','.join(UNIVERSE_COLUMNS)}")
        rows = list(reader)
    out = []
    for i, r in enumerate(rows, start=2):
        v = {c: (r.get(c) or "").strip() for c in UNIVERSE_COLUMNS}
        empty = [c for c in ("symbol", "exchange", "currency", "name", "sector", "country") if not v[c]]
        if empty:
            raise ValueError(f"{path.name}:{i} has no {', '.join(empty)}")
        ex, ccy, country = v["exchange"].upper(), v["currency"].upper(), v["country"].upper()
        if ex not in EXCHANGES:
            raise ValueError(f"{path.name}:{i} exchange {ex} not in {sorted(EXCHANGES)}")
        if ccy not in CURRENCIES:
            raise ValueError(f"{path.name}:{i} currency {ccy} not in {sorted(CURRENCIES)}")
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError(f"{path.name}:{i} country must be an ISO alpha-2 code, got {v['country']!r}")
        out.append(Instrument(v["symbol"].upper(), ex, ccy, v["name"], v["sector"], country))
    return out


def _read_instruments(files: list[str]) -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    for rel in files:
        for inst in _read_universe_csv(PROJECT_ROOT / rel):
            prev = out.get(inst.key)
            if prev and prev.exchange != inst.exchange:
                raise ValueError(f"{inst.key} listed with conflicting exchanges {prev.exchange} / {inst.exchange}")
            out[inst.key] = inst  # same ticker in several files is fine
    return out


def load_risk_config(path: Path | str = DEFAULT_RISK_PATH) -> RiskConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    b, lim = raw["budget"], raw["limits"]
    capital = float(b["capital"])
    if capital <= 0:
        raise ValueError(f"budget.capital must be > 0, got {capital}")

    trades = int(lim["max_trades_per_month"])
    if trades < 0:
        raise ValueError("max_trades_per_month must be >= 0")

    u = raw.get("universe", {})
    universe = _read_instruments(u.get("files", []))
    hedge = _read_instruments(u.get("hedge_files", []))
    enable_hedge = u.get("enable_hedge", False)
    if not isinstance(enable_hedge, bool):
        raise ValueError(f"universe.enable_hedge must be true or false, got {enable_hedge!r}")
    both = sorted(set(universe) & set(hedge))
    if both:
        raise ValueError(f"{both} listed both in the universe and in the hedge files")
    if enable_hedge:
        universe = {**universe, **hedge}

    return RiskConfig(
        capital=capital,
        base_currency=b["currency"].upper(),
        max_position_pct=_pct("max_position_pct", lim["max_position_pct"]),
        max_trades_per_month=trades,
        max_drawdown_pct=_pct("max_drawdown_pct", lim["max_drawdown_pct"]),
        max_price_deviation_pct=_pct("max_price_deviation_pct", lim["max_price_deviation_pct"], hi=20.0),
        max_commission_pct=_pct("max_commission_pct", lim["max_commission_pct"]),
        allow_fractional=bool(lim.get("allow_fractional", False)),
        universe=universe,
        hedge=hedge,
        enable_hedge=enable_hedge,
    )
