"""SQLite journal: the bot's audit log AND its virtual sub-account (budget from config/risk.toml).

The paper account holds far more than the budget, so the bot never trusts the
broker's cash figure. Its cash and holdings come from this ledger:
    cash     = sum(ledger.amount_base)
    holdings = sum(BUY qty) - sum(SELL qty) per instrument

Tables
  runs       one row per bot run (value, drawdown, status)
  proposals  every proposal Claude made, its decision record (pocket, thesis, confidence...; T6) + the gate's verdict
  orders     every order sent to the broker and its outcome
  ledger     DEPOSIT / BUY / SELL cash movements in base currency (fills only)
  state      key/value: peak value, halted flag, drawdown tier (+ pause end, since)
  nav_daily  one value per Paris day (the day's last first-valuation), for the daily-loss halt and reports
  stops          protective STP orders at IBKR, one active per held line, identified by permId (T5)
  stop_history   every placement / raise / resize / cancel of a stop
  stop_fills     stop executions already booked (by IBKR execId, so a fill is never booked twice)
  benchmark      IWDA held 100% from day 1 (CLAUDE.md §1): virtual units bought with every deposit, NAV per day (T7)
  closed_trades  one row per exit (stop, kill, voluntary sale): entry, exit, P&L net, motif, pocket, confidence,
                 duration, slippage allowance and success (T5, enriched by T6)

Columns added after a table was first shipped live in MIGRATIONS and are added to an existing journal on open
(idempotent ALTER TABLE ... ADD COLUMN); a fresh journal goes through the same path.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.risk.gate import (  # noqa: F401  (add_business_days re-exported)
    NORMAL, PAUSED, RECOVERY, REDUCED, Decision, Holding, PortfolioState, add_business_days,
)

DEFAULT_DB = PROJECT_ROOT / "data" / "journal.sqlite"
TZ = ZoneInfo("Europe/Paris")   # timestamps; calendar months for the order and LLM limits

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
  value_base REAL, cash_base REAL, peak_base REAL, drawdown_pct REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), ts TEXT NOT NULL,
  symbol TEXT, exchange TEXT, currency TEXT, action TEXT, quantity REAL, limit_price REAL,
  reason TEXT, approved INTEGER NOT NULL, block_reasons TEXT,
  notional_base REAL, est_commission_base REAL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, proposal_id INTEGER REFERENCES proposals(id), ts TEXT NOT NULL,
  broker_order_id INTEGER, symbol TEXT, action TEXT, quantity REAL, limit_price REAL,
  status TEXT, filled REAL, avg_fill_price REAL, message TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('DEPOSIT','BUY','SELL')),
  symbol TEXT, currency TEXT, quantity REAL, price REAL, commission REAL,
  fx_to_base REAL, amount_base REAL NOT NULL, broker_order_id INTEGER
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nav_daily (date TEXT PRIMARY KEY, value_base REAL NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stops (
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, currency TEXT NOT NULL, perm_id INTEGER NOT NULL UNIQUE,
  order_id INTEGER, quantity REAL NOT NULL, entry_price REAL, entry_date TEXT, atr_entry REAL,
  stop_price REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ('active','cancelled','filled','missing')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS stop_history (
  id INTEGER PRIMARY KEY, perm_id INTEGER NOT NULL, ts TEXT NOT NULL, stop_price REAL, quantity REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS stop_fills (
  exec_id TEXT PRIMARY KEY, perm_id INTEGER NOT NULL, ts TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL,
  commission REAL, commission_currency TEXT
);
CREATE TABLE IF NOT EXISTS closed_trades (
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, currency TEXT NOT NULL, entry_ts TEXT, exit_ts TEXT NOT NULL,
  quantity REAL NOT NULL, entry_price REAL, exit_price REAL NOT NULL, pnl_net_base REAL, motif TEXT NOT NULL,
  perm_id INTEGER, broker_order_id INTEGER
);
CREATE TABLE IF NOT EXISTS benchmark (
  date TEXT PRIMARY KEY, price_base REAL NOT NULL, units REAL NOT NULL, nav_base REAL NOT NULL, ts TEXT NOT NULL,
  invested_base REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tier_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, from_tier TEXT, to_tier TEXT NOT NULL, drawdown_pct REAL, pause_until TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  run_id INTEGER NOT NULL REFERENCES runs(id), key TEXT NOT NULL, quantity REAL, price_base REAL,
  PRIMARY KEY (run_id, key)
);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), ts TEXT NOT NULL, provider TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  web_searches INTEGER, cost_usd REAL NOT NULL
);
"""


# Columns added after the first schema (T6): (table, column, type). Applied on every open; existing ones skipped.
MIGRATIONS = [
    # proposals: Claude's decision record (CLAUDE.md §8.1, guardrail_trader/decision.py)
    ("proposals", "poche", "TEXT"), ("proposals", "these", "TEXT"), ("proposals", "catalyseur", "TEXT"),
    ("proposals", "fiabilite", "TEXT"), ("proposals", "horizon_jours", "INTEGER"), ("proposals", "stop_pct", "REAL"),
    ("proposals", "objectif_pct", "REAL"), ("proposals", "invalidation", "TEXT"), ("proposals", "confiance", "REAL"),
    ("proposals", "sources", "TEXT"), ("proposals", "motif_sortie", "TEXT"),
    # ledger: which decision a fill belongs to, and its limit (slippage allowance)
    ("ledger", "proposal_id", "INTEGER"), ("ledger", "limit_price", "REAL"),
    # closed_trades: the decision it closes, and how it is scored
    ("closed_trades", "poche", "TEXT"), ("closed_trades", "confiance", "REAL"), ("closed_trades", "duree_jours", "INTEGER"),
    ("closed_trades", "entry_proposal_id", "INTEGER"), ("closed_trades", "exit_proposal_id", "INTEGER"),
    ("closed_trades", "slippage_base", "REAL"), ("closed_trades", "reussite", "INTEGER"),
    ("closed_trades", "pnl_vs_benchmark_pct", "REAL"),        # secondary: trade return - IWDA return, in points (T7)
]
DECISION_COLUMNS = ("these", "catalyseur", "fiabilite", "horizon_jours", "stop_pct", "objectif_pct", "invalidation",
                    "confiance", "sources", "motif_sortie")

# Flat slippage allowance on a leg filled exactly at its limit price (CLAUDE.md §8.3; CHANGELOG 12/10/2026 #6).
# A decision succeeds when its P&L, net of commissions and of this allowance, is > 0.
SLIPPAGE_PCT = 0.05

STALE_RUN_MINUTES = 30  # well past AGENT_MAX_SECONDS + the fill wait



def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path | str = DEFAULT_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            for table, col, typ in MIGRATIONS:
                if col not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.db.close()

    # -- state ----------------------------------------------------------------
    def _get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO state(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def base_currency(self) -> str | None:
        return self._get("base_currency")

    def is_halted(self) -> bool:
        return self._get("halted", "0") == "1"

    def halt(self, reason: str) -> None:
        self._set("halted", "1")
        self._set("halted_reason", f"{_now()} {reason}")

    def reset_halt(self, peak_value_base: float) -> None:
        """Manual re-arm by the operator. Peak resets to current value so drawdown starts at 0."""
        self._set("halted", "0")
        self._set("halted_reason", "")
        self.set_tier(NORMAL, None, 0.0)
        self._set("peak_base", repr(peak_value_base))
        self._set("last_value_base", repr(peak_value_base))

    # -- drawdown tiers and daily NAV (T4) ---------------------------------------
    def tier(self) -> tuple[str, str | None]:
        """(tier, pause_until). An unknown stored tier refuses (fail-closed) rather than guessing."""
        t = self._get("tier", NORMAL)
        if t not in (NORMAL, PAUSED, RECOVERY, REDUCED):
            raise ValueError(f"unknown drawdown tier {t!r} in the journal state")
        return t, (self._get("pause_until", "") or None)

    def set_tier(self, tier: str, pause_until: str | None, drawdown_pct: float) -> bool:
        """Store the tier; log the transition. Returns True if it changed."""
        prev, prev_until = self.tier()
        if (prev, prev_until) == (tier, pause_until):
            return False
        self._set("tier", tier)
        self._set("pause_until", pause_until or "")
        self._set("tier_since", _now())
        with self.db:
            self.db.execute("INSERT INTO tier_log(ts,from_tier,to_tier,drawdown_pct,pause_until) VALUES(?,?,?,?,?)",
                            (_now(), prev, tier, drawdown_pct, pause_until))
        return True

    def record_nav(self, value_base: float, day: date | None = None) -> None:
        """The day's NAV: every run's first valuation overwrites it, so the last run of the day wins."""
        day = day or datetime.now(TZ).date()
        with self.db:
            self.db.execute("INSERT INTO nav_daily(date,value_base,ts) VALUES(?,?,?) ON CONFLICT(date) DO UPDATE "
                            "SET value_base=excluded.value_base, ts=excluded.ts", (day.isoformat(), value_base, _now()))

    def previous_day_loss_pct(self, today: date | None = None) -> float | None:
        """Loss of the last completed day before `today`, close to close, in % (positive = loss).
        None without two days of NAV (missing data is not a loss)."""
        today = today or datetime.now(TZ).date()
        rows = self.db.execute("SELECT value_base FROM nav_daily WHERE date < ? ORDER BY date DESC LIMIT 2",
                               (today.isoformat(),)).fetchall()
        if len(rows) < 2 or rows[1][0] <= 0:
            return None
        return (1 - rows[0][0] / rows[1][0]) * 100

    def peak(self) -> float:
        return float(self._get("peak_base", "0"))

    def observe_value(self, value_base: float) -> float:
        """Record this run's portfolio value and return the (possibly raised) peak.

        The peak only rises to a value seen on two consecutive runs: min(this run, previous run).
        A one-off bad price tick can't inflate the peak, so it can't cause a false kill switch
        on the next normal run. Real rises count one run later. Call once per run.
        """
        prev = self._get("last_value_base", None)
        peak = self.peak()
        if prev is not None:
            peak = max(peak, min(value_base, float(prev)))
        elif peak == 0.0:                       # brand-new journal: nothing to confirm against yet
            peak = value_base
        self._set("peak_base", repr(peak))
        self._set("last_value_base", repr(value_base))
        return peak

    # -- benchmark (T7) -------------------------------------------------------------
    def benchmark_units(self) -> float:
        return float(self._get("benchmark_units", "0"))

    def invest_benchmark(self, price_base: float) -> float:
        """Buy virtual benchmark units with every deposit not yet invested, at `price_base` (base currency).
        At init this is the whole capital; a later top-up is invested at the next snapshot. Returns the amount."""
        if not (price_base > 0):
            raise ValueError(f"benchmark price must be > 0, got {price_base!r}")
        pending = self.total_deposits() - float(self._get("benchmark_invested", "0"))
        if pending <= 0.005:
            return 0.0
        self._set("benchmark_units", repr(self.benchmark_units() + pending / price_base))
        self._set("benchmark_invested", repr(self.total_deposits()))
        return pending

    def record_benchmark(self, price_base: float, day: date | None = None) -> float:
        """The day's benchmark NAV (the last snapshot of the day wins, like nav_daily); a top-up not yet
        invested is invested first, and counted as that day's flow (invested_base). Returns the NAV."""
        day = day or datetime.now(TZ).date()
        invested = self.invest_benchmark(price_base) if self.benchmark_units() > 0 else 0.0
        units = self.benchmark_units()
        if units <= 0:
            raise RuntimeError("benchmark not started; journal_cli.py init buys its units")
        nav = units * price_base
        with self.db:
            self.db.execute("INSERT INTO benchmark(date,price_base,units,nav_base,ts,invested_base) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(date) DO UPDATE SET price_base=excluded.price_base, units=excluded.units, "
                            "nav_base=excluded.nav_base, ts=excluded.ts, "
                            "invested_base=benchmark.invested_base + excluded.invested_base",
                            (day.isoformat(), price_base, units, nav, _now(), invested))
        return nav

    def benchmark_price_on(self, day: date) -> float | None:
        """The last benchmark price known on or before `day` (holidays carry the previous close)."""
        row = self.db.execute("SELECT price_base FROM benchmark WHERE date <= ? ORDER BY date DESC LIMIT 1",
                              (day.isoformat(),)).fetchone()
        return row[0] if row else None

    # -- ledger -----------------------------------------------------------------
    def init_budget(self, amount: float, currency: str) -> None:
        if self.db.execute("SELECT 1 FROM ledger WHERE kind='DEPOSIT'").fetchone():
            raise RuntimeError("Budget already initialised; refusing to deposit twice.")
        if amount <= 0:
            raise ValueError("amount must be > 0")
        with self.db:
            self.db.execute("INSERT INTO ledger(ts,kind,currency,amount_base,fx_to_base) "
                            "VALUES(?,?,?,?,1.0)", (_now(), "DEPOSIT", currency.upper(), amount))
        self._set("base_currency", currency.upper())
        self._set("peak_base", repr(amount))

    def deposits(self) -> list[tuple[str, float]]:
        """Every virtual deposit, oldest first: (timestamp, amount in base currency)."""
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT ts, amount_base FROM ledger WHERE kind='DEPOSIT' ORDER BY id").fetchall()]

    def total_deposits(self) -> float:
        return sum(a for _, a in self.deposits())

    def topup_to(self, target_total: float, currency: str) -> float:
        """Deposit whatever brings total deposits up to target_total. Returns the amount deposited.

        Idempotent: running it twice with the same target deposits once. Deposits are append-only, so
        there is no withdrawal: a target below what is already deposited is an error, not a no-op.
        The peak is left alone: it rises on its own after two consecutive runs at the higher value,
        so a deposit can never read as a drawdown and one odd tick can't inflate the peak.
        """
        base = self.base_currency()
        if not base:
            raise RuntimeError("Budget not initialised; run `journal_cli.py init` first.")
        if currency.upper() != base:
            raise ValueError(f"currency {currency.upper()} does not match the journal's base currency {base}")
        total = self.total_deposits()
        if target_total < total - 0.005:
            raise ValueError(f"target {target_total:,.2f} is below the {total:,.2f} already deposited; "
                             "deposits are append-only (no withdrawals)")
        delta = round(target_total - total, 2)
        if delta < 0.01:
            return 0.0
        with self.db:
            self.db.execute("INSERT INTO ledger(ts,kind,currency,amount_base,fx_to_base) "
                            "VALUES(?,?,?,?,1.0)", (_now(), "DEPOSIT", base, delta))
        return delta

    def cash_base(self) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(amount_base),0) FROM ledger").fetchone()[0])

    def holdings_qty(self) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT symbol || ':' || currency AS k, "
            "SUM(CASE kind WHEN 'BUY' THEN quantity WHEN 'SELL' THEN -quantity END) AS q "
            "FROM ledger WHERE kind IN ('BUY','SELL') GROUP BY k").fetchall()
        return {r["k"]: r["q"] for r in rows if r["q"] and abs(r["q"]) > 1e-9}

    def record_fill(self, symbol: str, currency: str, action: str, quantity: float, price: float,
                    commission: float, fx_to_base: float, broker_order_id: int | None = None,
                    proposal_id: int | None = None, limit_price: float | None = None) -> None:
        """Book a FILL (not an order). Price, commission and limit in the instrument currency.
        proposal_id: the decision the fill belongs to (None for stop fills and fills booked by hand)."""
        action = action.upper()
        gross = quantity * price
        amount = -(gross + commission) * fx_to_base if action == "BUY" else (gross - commission) * fx_to_base
        with self.db:
            self.db.execute(
                "INSERT INTO ledger(ts,kind,symbol,currency,quantity,price,commission,fx_to_base,amount_base,"
                "broker_order_id,proposal_id,limit_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), action, symbol.upper(), currency.upper(), quantity, price, commission,
                 fx_to_base, amount, broker_order_id, proposal_id, limit_price))

    def position_entry(self, key: str) -> tuple[str | None, float | None]:
        """(timestamp the current position was opened, average BUY price in the instrument currency,
        commission excluded) - the entry the stop rules refer to. (None, None) when flat."""
        pos = self._open_position(key)
        return (pos["opened"], pos["cost"] / pos["qty"]) if pos else (None, None)

    def _open_position(self, key: str) -> dict | None:
        """The current position of `key`, None when flat: opened (ts), qty, cost (instrument currency,
        commission excluded), slip (slippage allowance of its BUY legs, base currency) and proposal_id (the BUY
        decision that opened it). Partial sells scale cost and slip down pro rata."""
        sym, ccy = key.split(":")
        pos = None
        for kind, q, px, ts, fx, lmt, pid in self.db.execute(
                "SELECT kind, quantity, price, ts, fx_to_base, limit_price, proposal_id FROM ledger "
                "WHERE symbol=? AND currency=? AND kind IN ('BUY','SELL') ORDER BY id", (sym, ccy)).fetchall():
            if kind == "BUY":
                if pos is None:
                    pos = dict(opened=ts, qty=0.0, cost=0.0, slip=0.0, proposal_id=pid)
                pos["qty"] += q
                pos["cost"] += q * px
                pos["slip"] += _slippage(q, px, lmt, fx)
            elif pos is not None:
                left = max(pos["qty"] - q, 0.0)
                if left <= 1e-9:
                    pos = None
                    continue
                for k in ("cost", "slip"):
                    pos[k] *= left / pos["qty"]
                pos["qty"] = left
        return pos

    def book_sell(self, symbol: str, currency: str, quantity: float, price: float, commission: float,
                  fx_to_base: float, motif: str, broker_order_id: int | None = None,
                  perm_id: int | None = None, proposal_id: int | None = None,
                  limit_price: float | None = None) -> int:
        """Book a SELL fill AND its closed-trade row, linked to the BUY decision that opened the position.

        pnl_net_base: net of both commissions (base currency). slippage_base: the flat SLIPPAGE_PCT allowance
        on each leg filled exactly at its limit (stop fills have no limit: their real slippage is in the price).
        reussite = pnl_net_base - slippage_base > 0, the single success definition (rate and calibration).
        """
        key = f"{symbol.upper()}:{currency.upper()}"
        pos = self._open_position(key)
        cost_per_share = self.avg_costs().get(key, 0.0)          # base, BUY commission included
        self.record_fill(symbol, currency, "SELL", quantity, price, commission, fx_to_base, broker_order_id,
                         proposal_id, limit_price)
        exit_ts = _now()
        pnl = (quantity * price - commission) * fx_to_base - cost_per_share * quantity
        share = min(quantity / pos["qty"], 1.0) if pos else 0.0
        slip = (pos["slip"] * share if pos else 0.0) + _slippage(quantity, price, limit_price, fx_to_base)
        entry = self._proposal(pos["proposal_id"]) if pos else None
        days = ((datetime.fromisoformat(exit_ts).date() - datetime.fromisoformat(pos["opened"]).date()).days
                if pos else None)
        vs_bench = None                     # trade return minus the benchmark's over the same days, in points
        if pos and cost_per_share > 0:
            b0 = self.benchmark_price_on(datetime.fromisoformat(pos["opened"]).date())
            b1 = self.benchmark_price_on(datetime.fromisoformat(exit_ts).date())
            if b0 and b1:
                vs_bench = (pnl / (cost_per_share * quantity) - (b1 / b0 - 1)) * 100
        with self.db:
            return self.db.execute(
                "INSERT INTO closed_trades(symbol,currency,entry_ts,exit_ts,quantity,entry_price,exit_price,"
                "pnl_net_base,motif,perm_id,broker_order_id,poche,confiance,duree_jours,entry_proposal_id,"
                "exit_proposal_id,slippage_base,reussite,pnl_vs_benchmark_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol.upper(), currency.upper(), pos["opened"] if pos else None, exit_ts, quantity,
                 pos["cost"] / pos["qty"] if pos else None, price, pnl, motif, perm_id, broker_order_id,
                 entry["poche"] if entry else None, entry["confiance"] if entry else None, days,
                 pos["proposal_id"] if pos else None, proposal_id, slip, int(pnl - slip > 0), vs_bench)).lastrowid

    def _proposal(self, proposal_id: int | None) -> sqlite3.Row | None:
        if proposal_id is None:
            return None
        return self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()

    # -- protective stops (T5) ----------------------------------------------------
    def add_stop(self, symbol: str, currency: str, perm_id: int, order_id: int, quantity: float, stop_price: float,
                 entry_price: float | None, entry_date: str | None, atr: float | None, note: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO stops(symbol,currency,perm_id,order_id,quantity,entry_price,entry_date,atr_entry,"
                "stop_price,status,created_at,updated_at,note) VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                (symbol.upper(), currency.upper(), perm_id, order_id, quantity, entry_price, entry_date, atr,
                 stop_price, _now(), _now(), note))
            self.db.execute("INSERT INTO stop_history(perm_id,ts,stop_price,quantity,reason) VALUES(?,?,?,?,?)",
                            (perm_id, _now(), stop_price, quantity, f"placed {note}".strip()))

    def update_stop(self, perm_id: int, reason: str, stop_price: float | None = None, quantity: float | None = None,
                    status: str | None = None) -> None:
        sets, args = ["updated_at=?"], [_now()]
        for col, v in (("stop_price", stop_price), ("quantity", quantity), ("status", status)):
            if v is not None:
                sets.append(f"{col}=?")
                args.append(v)
        with self.db:
            self.db.execute(f"UPDATE stops SET {', '.join(sets)} WHERE perm_id=?", (*args, perm_id))
            self.db.execute("INSERT INTO stop_history(perm_id,ts,stop_price,quantity,reason) VALUES(?,?,?,?,?)",
                            (perm_id, _now(), stop_price, quantity, reason + (f" -> {status}" if status else "")))

    def active_stops(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM stops WHERE status='active' ORDER BY id").fetchall()

    def active_stop(self, key: str) -> sqlite3.Row | None:
        sym, ccy = key.split(":")
        return self.db.execute("SELECT * FROM stops WHERE status='active' AND symbol=? AND currency=? "
                               "ORDER BY id DESC LIMIT 1", (sym, ccy)).fetchone()

    def stop_filled_qty(self, perm_id: int) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(shares),0) FROM stop_fills WHERE perm_id=?",
                                     (perm_id,)).fetchone()[0])

    def book_stop_fill(self, exec_id: str, perm_id: int, symbol: str, currency: str, shares: float, price: float,
                       commission: float, commission_currency: str, commission_instr: float,
                       fx_to_base: float) -> bool:
        """Book one stop execution once (keyed by IBKR execId): ledger SELL + closed trade 'stop'.
        commission_instr = the commission converted to the instrument currency. False if already booked."""
        if self.db.execute("SELECT 1 FROM stop_fills WHERE exec_id=?", (exec_id,)).fetchone():
            return False
        with self.db:
            self.db.execute("INSERT INTO stop_fills(exec_id,perm_id,ts,shares,price,commission,commission_currency) "
                            "VALUES(?,?,?,?,?,?,?)", (exec_id, perm_id, _now(), shares, price, commission,
                                                      commission_currency))
        self.book_sell(symbol, currency, shares, price, commission_instr, fx_to_base, "stop", perm_id=perm_id)
        return True

    # -- runs / proposals / orders -------------------------------------------
    def start_run(self, mode: str) -> int:
        with self.db:
            return self.db.execute("INSERT INTO runs(started_at,mode) VALUES(?,?)", (_now(), mode)).lastrowid

    def abort_stale_runs(self, older_than_min: int = STALE_RUN_MINUTES) -> list[int]:
        """Close runs left as 'running' by a process that died mid-run (container restart, OOM, power loss).

        A run is bounded by the agent and fill timeouts, so one still 'running' well past that is not
        running. Left alone it shows as in progress on the dashboard forever. Orders it may have sent are
        not lost: the next run reconciles the broker against the journal before it does anything else.
        """
        cutoff = datetime.now(TZ) - timedelta(minutes=older_than_min)
        rows = self.db.execute("SELECT id, started_at FROM runs WHERE status='running'").fetchall()
        stale = [r[0] for r in rows if datetime.fromisoformat(r[1]) < cutoff]
        with self.db:
            for run_id in stale:
                self.db.execute(
                    "UPDATE runs SET finished_at=?, status='aborted', notes=? WHERE id=? AND status='running'",
                    (_now(), f"Process died mid-run (no finish after {older_than_min} min); closed by the next run.",
                     run_id))
        return stale

    def finish_run(self, run_id: int, status: str, state: PortfolioState | None = None, notes: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at=?, status=?, value_base=?, cash_base=?, peak_base=?, drawdown_pct=?, notes=? WHERE id=?",
                (_now(), status,
                 state.value_base if state else None, state.cash_base if state else None,
                 state.peak_value_base if state else None, state.drawdown_pct if state else None,
                 notes, run_id))
            if state:  # holdings + prices at the end of the run, for the dashboard
                self.db.executemany(
                    "INSERT OR REPLACE INTO snapshots(run_id,key,quantity,price_base) VALUES(?,?,?,?)",
                    [(run_id, k, h.quantity, h.price_base) for k, h in state.holdings.items()])

    def record_decision(self, run_id: int, d: Decision) -> int:
        """One proposal, the gate's verdict, and Claude's decision record (poche + proposal.details)."""
        p = d.proposal
        rec = [p.details.get(c) for c in DECISION_COLUMNS]
        i = DECISION_COLUMNS.index("sources")
        rec[i] = json.dumps(rec[i]) if rec[i] is not None else None
        cols = ",".join(DECISION_COLUMNS)
        with self.db:
            return self.db.execute(
                "INSERT INTO proposals(run_id,ts,symbol,exchange,currency,action,quantity,limit_price,reason,"
                f"approved,block_reasons,notional_base,est_commission_base,poche,{cols}) "
                f"VALUES({','.join('?' * (14 + len(DECISION_COLUMNS)))})",
                (run_id, _now(), p.symbol, p.exchange, p.currency, p.action.upper(), p.quantity, p.limit_price,
                 p.reason, int(d.approved), json.dumps(d.reasons), d.notional_base, d.commission_base,
                 p.poche or None, *rec)).lastrowid

    def record_order(self, proposal_id: int | None, r) -> int:
        """r: broker OrderResult."""
        with self.db:
            return self.db.execute(
                "INSERT INTO orders(proposal_id,ts,broker_order_id,symbol,action,quantity,limit_price,status,filled,avg_fill_price,message) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, _now(), r.order_id, r.symbol, r.action, r.quantity, r.limit_price,
                 r.status, r.filled, r.avg_fill_price, r.message)).lastrowid

    def recent_activity(self, limit: int = 10) -> list[dict]:
        """The bot's last proposals (real runs only) and what happened to each, newest first.

        Given to Claude so it doesn't blindly re-propose an order that was blocked or didn't fill;
        every submitted order counts toward the monthly limit, filled or not.
        """
        rows = self.db.execute("""
            SELECT p.ts, p.action, p.quantity, p.symbol, p.currency, p.limit_price, p.approved,
                   p.block_reasons, o.status, o.filled
            FROM proposals p JOIN runs r ON r.id = p.run_id LEFT JOIN orders o ON o.proposal_id = p.id
            WHERE r.mode NOT LIKE '%dry%' AND r.mode NOT LIKE '%fake%'
            ORDER BY p.id DESC LIMIT ?""", (limit,)).fetchall()
        out = []
        for ts, action, qty, sym, ccy, px, approved, reasons, status, filled in rows:
            if not approved:
                outcome = "blocked by gate: " + "; ".join(json.loads(reasons or "[]"))
            elif status is None:
                outcome = "approved, but no order was placed"
            elif status == "Filled":
                outcome = "filled"
            elif filled and filled > 0:
                outcome = f"partially filled {filled:g}/{qty:g}, rest cancelled"
            else:
                outcome = f"not filled ({status}); counted toward the monthly limit"
            out.append({"date": ts[:10], "order": f"{action} {qty:g} {sym}:{ccy} @ {px}", "outcome": outcome})
        return out

    def orders_this_month(self, now: datetime | None = None) -> int:
        """Counts orders SUBMITTED this calendar month (Paris) - filled or not."""
        now = now or datetime.now(TZ)
        month = now.astimezone(TZ).strftime("%Y-%m")
        return int(self.db.execute("SELECT COUNT(*) FROM orders WHERE substr(ts,1,7)=?", (month,)).fetchone()[0])

    # -- LLM spend --------------------------------------------------------------
    def record_llm_usage(self, run_id: int | None, provider: str, model: str, usage, cost_usd: float) -> None:
        g = lambda n: int(getattr(usage, n, 0) or 0)  # noqa: E731
        stu = getattr(usage, "server_tool_use", None)
        searches = int(getattr(stu, "web_search_requests", 0) or 0) if stu is not None else 0
        with self.db:
            self.db.execute(
                "INSERT INTO llm_usage(run_id,ts,provider,model,input_tokens,output_tokens,cache_read_tokens,"
                "cache_write_tokens,web_searches,cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, _now(), provider, model, g("input_tokens"), g("output_tokens"),
                 g("cache_read_input_tokens"), g("cache_creation_input_tokens"), searches, cost_usd))

    def llm_spend_usd(self, month: str | None = None) -> float:
        """USD spent this calendar month (Paris), or for `month`='YYYY-MM'; month='all' for lifetime."""
        if month == "all":
            return float(self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage").fetchone()[0])
        month = month or datetime.now(TZ).strftime("%Y-%m")
        return float(self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE substr(ts,1,7)=?",
                                     (month,)).fetchone()[0])

    def recently_stopped(self, cooldown_days: int, today: date | None = None) -> dict[str, str]:
        """Instrument key -> last day of its re-entry cooldown, for stop exits still inside it.

        Reads closed trades with motif 'stop' (table created with the stops, T5); none yet = {}.
        """
        if cooldown_days <= 0 or not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='closed_trades'").fetchone():
            return {}
        today = today or datetime.now(TZ).date()
        out = {}
        for sym, ccy, exit_ts in self.db.execute(
                "SELECT symbol, currency, MAX(exit_ts) FROM closed_trades WHERE motif='stop' GROUP BY symbol, currency"):
            until = add_business_days(datetime.fromisoformat(exit_ts).date(), cooldown_days)
            if today <= until:
                out[f"{sym}:{ccy}"] = until.isoformat()
        return out

    # -- the view the gate needs ----------------------------------------------
    def avg_costs(self) -> dict[str, float]:
        """Average cost per share in base currency (incl. commission) for open positions."""
        pos: dict[str, list[float]] = {}  # key -> [qty, cost_base]
        for kind, sym, ccy, qty, amt in self.db.execute(
                "SELECT kind, symbol, currency, quantity, amount_base FROM ledger "
                "WHERE kind IN ('BUY','SELL') ORDER BY id").fetchall():
            k = f"{sym}:{ccy}"
            q, c = pos.setdefault(k, [0.0, 0.0])
            if kind == "BUY":
                pos[k] = [q + qty, c - amt]
            elif q > 0:
                sold = min(qty, q)
                pos[k] = [q - sold, c * (q - sold) / q]
        return {k: c / q for k, (q, c) in pos.items() if q > 1e-9}

    def portfolio_state(self, prices_base: dict[str, float], observe: bool = True,
                        cooldown_days: int = 0) -> PortfolioState:
        """prices_base: instrument key -> current price in base currency (for every holding).

        observe=True: this is the run's valuation and feeds the confirmed peak (once per run).
        observe=False: a re-valuation in the same run (e.g. after trading); peak untouched."""
        holdings = {}
        for key, qty in self.holdings_qty().items():
            if key not in prices_base:
                raise KeyError(f"no price for held instrument {key}; refusing to value portfolio")
            holdings[key] = Holding(qty, prices_base[key])
        state = PortfolioState(cash_base=self.cash_base(), holdings=holdings,
                               peak_value_base=self.peak(), trades_this_month=self.orders_this_month(),
                               halted=self.is_halted(), recently_stopped=self.recently_stopped(cooldown_days))
        if observe:
            state.peak_value_base = self.observe_value(state.value_base)
        return state


def _slippage(quantity: float, price: float, limit_price: float | None, fx_to_base: float) -> float:
    """SLIPPAGE_PCT of the leg's value (base currency) when it filled exactly at its limit, else 0."""
    if limit_price is None or abs(price - limit_price) > 1e-9 * max(abs(limit_price), 1.0):
        return 0.0
    return quantity * price * fx_to_base * SLIPPAGE_PCT / 100
