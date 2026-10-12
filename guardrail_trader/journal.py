"""SQLite journal: the bot's audit log AND its virtual sub-account (budget from config/risk.toml).

The paper account holds far more than the budget, so the bot never trusts the
broker's cash figure. Its cash and holdings come from this ledger:
    cash     = sum(ledger.amount_base)
    holdings = sum(BUY qty) - sum(SELL qty) per instrument

Tables
  runs       one row per bot run (value, drawdown, status)
  proposals  every proposal Claude made + the gate's verdict and reasons
  orders     every order sent to the broker and its outcome
  ledger     DEPOSIT / BUY / SELL cash movements in base currency (fills only)
  state      key/value: peak value, halted flag
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.risk.gate import Decision, Holding, PortfolioState

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


STALE_RUN_MINUTES = 30  # well past AGENT_MAX_SECONDS + the fill wait


def add_business_days(day: date, n: int) -> date:
    """`n` Monday-Friday days after `day`. Exchange holidays are not skipped: the cooldown can end up to a
    day or two early around a holiday, never late enough to matter for a 10-day rule."""
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path | str = DEFAULT_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

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
        self._set("peak_base", repr(peak_value_base))
        self._set("last_value_base", repr(peak_value_base))

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
                    commission: float, fx_to_base: float, broker_order_id: int | None = None) -> None:
        """Book a FILL (not an order). Price and commission in the instrument currency."""
        action = action.upper()
        gross = quantity * price
        amount = -(gross + commission) * fx_to_base if action == "BUY" else (gross - commission) * fx_to_base
        with self.db:
            self.db.execute(
                "INSERT INTO ledger(ts,kind,symbol,currency,quantity,price,commission,fx_to_base,amount_base,broker_order_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (_now(), action, symbol.upper(), currency.upper(), quantity, price, commission,
                 fx_to_base, amount, broker_order_id))

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
        p = d.proposal
        with self.db:
            return self.db.execute(
                "INSERT INTO proposals(run_id,ts,symbol,exchange,currency,action,quantity,limit_price,reason,"
                "approved,block_reasons,notional_base,est_commission_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, _now(), p.symbol, p.exchange, p.currency, p.action.upper(), p.quantity, p.limit_price,
                 p.reason, int(d.approved), json.dumps(d.reasons), d.notional_base, d.commission_base)).lastrowid

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
