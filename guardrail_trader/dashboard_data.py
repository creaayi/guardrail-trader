"""Read-only views over the journal for the local dashboard. No broker, no network."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from guardrail_trader import llm
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal
from guardrail_trader.risk import RiskConfig

RUNS_DIR = PROJECT_ROOT / "data" / "runs"


def _rows(j: Journal, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in j.db.execute(sql, args).fetchall()]


def _avg_costs(j: Journal) -> dict[str, float]:
    return j.avg_costs()


DEFAULT_DAYS = 2        # trading days the Decisions / Runs / Fills tables start with
MAX_DAYS = 3650
ROW_CAP = 5000          # safety bound for "all"; the tables are small (a few rows per run)
NY = ZoneInfo("America/New_York")


def parse_days(value: str | None) -> int | None:
    """`?days=` query value -> number of trading days, or None for all. Bad input is an error, not a default."""
    if value is None or value == "":
        return DEFAULT_DAYS
    if value == "all":
        return None
    if not value.isdigit() or not 1 <= int(value) <= MAX_DAYS:
        raise ValueError(f"days must be 1-{MAX_DAYS} or 'all', got {value!r}")
    return int(value)


def _trading_day(ts: str | None) -> str | None:
    """US/Eastern calendar date of an ISO timestamp: the bot's own 'session day' (open + close slots share one)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone(NY).date().isoformat()
    except ValueError:
        return None


def _window(runs: list[dict], days: int | None) -> tuple[set[str] | None, list[str]]:
    """The most recent `days` trading days that have runs (gaps like weekends don't use up the budget)."""
    all_days = sorted({d for r in runs if (d := _trading_day(r["started_at"]))}, reverse=True)
    return (None if days is None else set(all_days[:days])), all_days


def build(j: Journal, cfg: RiskConfig, days: int | None = DEFAULT_DAYS) -> dict:
    runs = _rows(j, "SELECT * FROM runs ORDER BY id")
    deposits = j.deposits()
    deposited = sum(a for _, a in deposits)
    valued = [r for r in runs if r["value_base"] is not None]
    last_valued = valued[-1] if valued else None
    cash = j.cash_base()
    value = last_valued["value_base"] if last_valued else cash
    if last_valued:
        # The last run's value predates any deposit made since, so add that cash now; otherwise a top-up
        # reads as a loss until the next run. (Holdings can't change between runs: fills happen inside one.)
        as_of = datetime.fromisoformat(last_valued["finished_at"] or last_valued["started_at"])
        value += sum(a for ts, a in deposits if datetime.fromisoformat(ts) > as_of)
    holdings_now = j.holdings_qty()
    if not holdings_now:
        value = cash  # all cash: value is exact without prices
    peak = max(j.peak(), value)
    drawdown = (1 - value / peak) * 100 if peak else 0.0

    # Holdings: quantities from the ledger, prices from the latest snapshot that has them.
    snap = {}
    if holdings_now:
        snap_run = j.db.execute("SELECT MAX(run_id) FROM snapshots").fetchone()[0]
        if snap_run is not None:
            snap = {r["key"]: r for r in _rows(j, "SELECT * FROM snapshots WHERE run_id=?", (snap_run,))}
    avg = _avg_costs(j)
    holdings = []
    for k, q in sorted(holdings_now.items()):
        px = snap.get(k, {}).get("price_base")
        val = q * px if px is not None else None
        cost = avg.get(k)
        holdings.append({
            "symbol": k.split(":")[0], "currency": k.split(":")[1], "quantity": q,
            "avg_cost_base": cost, "price_base": px, "value_base": val,
            "pnl_base": (px - cost) * q if px is not None and cost is not None else None,
            "weight_pct": val / value * 100 if val is not None and value else None,
        })

    llm_by_run = {r["run_id"]: r for r in _rows(
        j, "SELECT run_id, SUM(cost_usd) cost, SUM(input_tokens) tin, SUM(output_tokens) tout, "
           "SUM(cache_read_tokens) tcache, MAX(model) model FROM llm_usage GROUP BY run_id")}
    counts = {r["run_id"]: r for r in _rows(
        j, "SELECT run_id, SUM(approved) approved, COUNT(*) - SUM(approved) blocked FROM proposals GROUP BY run_id")}

    keep, all_days = _window(runs, days)
    run_day = {r["id"]: _trading_day(r["started_at"]) for r in runs}
    shown_runs = [r for r in runs if keep is None or run_day[r["id"]] in keep]

    run_list = []
    for r in reversed(shown_runs[-ROW_CAP:]):
        u, c = llm_by_run.get(r["id"], {}), counts.get(r["id"], {})
        run_list.append({**r, "llm_cost_usd": u.get("cost"), "model": u.get("model"),
                         "tokens_in": u.get("tin"), "tokens_out": u.get("tout"), "tokens_cached": u.get("tcache"),
                         "approved": c.get("approved") or 0, "blocked": c.get("blocked") or 0,
                         "has_transcript": (RUNS_DIR / f"run_{r['id']}.json").exists()})

    decisions = _rows(j, """
        SELECT p.id, p.run_id, p.ts, p.action, p.quantity, p.symbol, p.currency, p.limit_price, p.reason,
               p.approved, p.block_reasons, p.notional_base, p.est_commission_base, r.mode,
               p.poche, p.confiance, p.these, p.fiabilite, p.motif_sortie,
               o.status AS order_status, o.filled, o.avg_fill_price
        FROM proposals p JOIN runs r ON r.id = p.run_id LEFT JOIN orders o ON o.proposal_id = p.id
        ORDER BY p.id DESC LIMIT ?""", (ROW_CAP,))
    n_decisions = j.db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
    decisions = [d for d in decisions if keep is None or run_day.get(d["run_id"]) in keep]
    for d in decisions:
        d["block_reasons"] = json.loads(d["block_reasons"] or "[]")

    fills = _rows(j, "SELECT ts, kind, symbol, currency, quantity, price, commission, fx_to_base, amount_base, "
                     "broker_order_id FROM ledger WHERE kind IN ('BUY','SELL') ORDER BY id DESC LIMIT ?", (ROW_CAP,))
    n_fills = j.db.execute("SELECT COUNT(*) FROM ledger WHERE kind IN ('BUY','SELL')").fetchone()[0]
    fills = [f for f in fills if keep is None or _trading_day(f["ts"]) in keep]

    llm_months = _rows(j, "SELECT substr(ts,1,7) month, SUM(cost_usd) cost, COUNT(DISTINCT run_id) runs, "
                          "SUM(input_tokens) tin, SUM(output_tokens) tout FROM llm_usage GROUP BY month ORDER BY month")

    # Brokerage actually paid: commission on booked fills, converted to the base currency.
    brk = _rows(j, "SELECT COUNT(*) n, COALESCE(SUM(commission * fx_to_base), 0) paid, "
                   "COALESCE(SUM(ABS(quantity * price) * fx_to_base), 0) traded "
                   "FROM ledger WHERE kind IN ('BUY', 'SELL')")[0]

    halted_reason = j._get("halted_reason", "") or ""
    scheduler = _scheduler_status()
    return {
        "base_currency": cfg.base_currency,
        "kpis": {
            # P/L is against what was actually deposited (the ledger), not the config: a top-up moves
            # the baseline from that day on instead of rewriting history.
            "capital": deposited, "deposits": [{"ts": ts, "amount": a} for ts, a in deposits],
            "value": value, "cash": cash, "peak": peak,
            "pnl": value - deposited, "pnl_pct": (value / deposited - 1) * 100 if deposited else 0.0,
            "drawdown_pct": drawdown, "kill_switch_pct": cfg.max_drawdown_pct,
            "orders_this_month": j.orders_this_month(), "orders_limit": cfg.max_trades_per_month,
            "llm_month_usd": j.llm_spend_usd(), "llm_month_cap_usd": llm.monthly_budget_usd(),
            "llm_lifetime_usd": j.llm_spend_usd("all"),
            "brokerage_base": brk["paid"], "brokerage_fills": brk["n"], "traded_base": brk["traded"],
            "halted": j.is_halted(), "halted_reason": halted_reason,
            "value_as_of": last_valued["finished_at"] if last_valued else None,
            "last_run": runs[-1] if runs else None,
            "universe_size": len(cfg.universe), "max_position_pct": cfg.max_position_pct,
        },
        "value_series": [{"ts": r["finished_at"] or r["started_at"], "value": r["value_base"],
                          "mode": r["mode"], "status": r["status"], "run_id": r["id"]} for r in valued],
        "holdings": holdings,
        # What the Decisions / Runs / Fills tables cover: the latest `days` trading days that have runs
        # (US/Eastern dates). days=None means everything. The value chart and KPIs are never windowed.
        "window": {
            "days": days, "available_days": len(all_days),
            "since": min(keep) if keep else (all_days[-1] if all_days else None),
            "until": all_days[0] if all_days else None,
            "shown": {"runs": len(run_list), "decisions": len(decisions), "fills": len(fills)},
            "totals": {"runs": len(runs), "decisions": n_decisions, "fills": n_fills},
        },
        "runs": run_list,
        "decisions": decisions,
        "fills": fills,
        "llm_months": llm_months,
        "scheduler": scheduler,
    }


def _scheduler_status() -> dict:
    """Heartbeat + last slots from scripts/scheduled_run.py, and the next scheduled slot."""
    from datetime import datetime

    from guardrail_trader.slots import PARIS, next_slot
    data = PROJECT_ROOT / "data"
    out: dict = {"last_tick_ny": None, "next_slot_paris": None, "recent": []}
    try:
        out["last_tick_ny"] = json.loads((data / "scheduler_heartbeat.json").read_text()).get("last_tick_ny")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        state = json.loads((data / "scheduler_state.json").read_text())
        out["recent"] = [{"slot": k, **v} for k, v in sorted(state.items())[-6:]][::-1]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    nxt = next_slot(datetime.now(PARIS))
    if nxt:
        out["next_slot_paris"] = nxt[1].astimezone(PARIS).isoformat(timespec="minutes")
        out["next_slot_name"] = nxt[0].name
    return out


def transcript(run_id: int) -> list[dict]:
    """Simplified view of Claude's run: its text, tool calls and (truncated) tool results."""
    path = RUNS_DIR / f"run_{int(run_id)}.json"
    if not path.exists():
        return []
    steps = []
    for m in json.loads(path.read_text()):
        content = m.get("content")
        if isinstance(content, str):
            steps.append({"role": m["role"], "kind": "text", "text": content})
            continue
        for b in content or []:
            t = b.get("type")
            if t == "text" and b.get("text", "").strip():
                steps.append({"role": m["role"], "kind": "text", "text": b["text"]})
            elif t == "tool_use":
                steps.append({"role": "assistant", "kind": "tool_call", "name": b.get("name"),
                              "input": b.get("input")})
            elif t == "tool_result":
                c = b.get("content")
                c = c if isinstance(c, str) else json.dumps(c)
                steps.append({"role": "tool", "kind": "tool_result", "is_error": b.get("is_error", False),
                              "text": c[:3000] + ("…" if len(c) > 3000 else "")})
            elif t in ("server_tool_use", "web_search_tool_result"):
                steps.append({"role": "assistant", "kind": "web_search",
                              "text": json.dumps(b.get("input") or "")[:300]})
    return steps
