"""One bot run: reconcile -> price -> kill switch -> Claude -> risk gate -> execute -> journal.

A plain function with its dependencies passed in (broker, market data, journal, LLM, clock), so
the CLI (scripts/run_bot.py) and the scenario evals (evals/) drive exactly the same code.

Return codes: 0 ok/skipped, 1 error, 2 gateway unusable (not ready / no market data - retry),
3 needs a human (reconciliation failed, or the account holds less than the virtual capital).
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from guardrail_trader.agent import TradingAgent
from guardrail_trader.broker import BrokerNotReadyError, BrokerSafetyError, MarketDataUnavailableError, check_capital
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.executor import execute
from guardrail_trader.journal import Journal
from guardrail_trader.market import daily_history, is_open, market_sessions, price_instruments, summarize_history
from guardrail_trader.risk import (
    Decision, MarketInfo, Proposal, RiskConfig, evaluate, instrument_key, kill_switch_triggered,
    liquidation_proposals,
)

RUNS_DIR = PROJECT_ROOT / "data" / "runs"
NY = ZoneInfo("America/New_York")


class IBKRMarketData:
    """Market data from IBKR: prices, trading hours, history, commission previews.

    Also the agent's MarketView once `prices` and `fx` are set by the pipeline.
    """

    def __init__(self, broker, cfg: RiskConfig):
        self.broker, self.cfg = broker, cfg
        self.prices: dict[str, float] = {}
        self.fx: dict[str, float] = {}
        self._comm_cache: dict[tuple, float] = {}

    def fx_rates(self) -> dict[str, float]:
        return {c: self.broker.fx_to_base(c, self.cfg.base_currency) for c in {i.currency for i in self.cfg.universe.values()}}

    def fx_to_base(self, currency: str) -> float:
        return self.broker.fx_to_base(currency, self.cfg.base_currency)

    def price_all(self) -> dict[str, float]:
        return price_instruments(self.broker, list(self.cfg.universe.values()))

    def market_open(self, now: datetime) -> bool:
        return is_open(market_sessions(self.broker, next(iter(self.cfg.universe.values()))), now)

    def history_summary(self, key: str) -> dict:
        return summarize_history(daily_history(self.broker, self.cfg.universe[key]))

    def commission_base(self, p: Proposal) -> float:
        ck = (p.key, p.action, p.quantity, p.limit_price)
        if ck not in self._comm_cache:
            try:
                c = self.broker.stock(p.symbol, p.exchange, p.currency)
                pv = self.broker.preview_order(c, p.action, p.quantity, p.limit_price)
                comm_ccy = pv["commission_currency"] or p.currency
                self._comm_cache[ck] = pv["commission"] * self.fx.get(comm_ccy, self.fx[p.currency])
            except Exception:
                self._comm_cache[ck] = float("inf")  # unknown cost -> gate blocks it
        return self._comm_cache[ck]


@dataclass
class LLMSetup:
    """How to get a Claude client for this run. make_client(market) is only called once the
    market is open and (for metered clients) the monthly budget has room."""
    make_client: Callable[[Any], Any]
    provider: str
    model: str
    cost_fn: Callable | None = None          # None = unmetered (scripted fake)
    run_budget_usd: float = 0.10
    monthly_budget_usd: float = 2.50
    request_timeout_s: float = 60.0
    max_seconds: float | None = None         # agent loop budget; None = agent default


def _jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items()}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def run_once(broker, market, j: Journal, cfg: RiskConfig, llm: LLMSetup, *, mode: str, dry_run: bool = False,
             fill_timeout: float = 120.0, my_picks: set[str] | None = None, web_search: bool = False,
             log: Callable[[str], None] = print, now: datetime | None = None,
             runs_dir: Path = RUNS_DIR) -> int:
    for stale_id in j.abort_stale_runs():
        log(f"run {stale_id} was left 'running' by a process that died; marked aborted")
    run_id = j.start_run(mode)
    log(f"run {run_id} mode={mode} universe={len(cfg.universe)} budget={cfg.capital:g} {cfg.base_currency}")
    try:
        # 0. Is the gateway actually serving data? A connected socket is not enough: after IBKR's
        # nightly reset every request times out and positions come back empty, which would look
        # like an emptied account in the reconciliation below.
        try:
            snap = broker.ensure_ready()
        except BrokerNotReadyError as e:
            j.finish_run(run_id, "broker_not_ready", notes=str(e))
            log(f"BROKER NOT READY: {e} Skipping; no Claude call, no orders.")
            return 2
        log(f"broker ready: {snap.account_id} net liquidation {snap.net_liquidation:,.2f} {snap.currency}")

        # 1. Reconcile: the ledger must match what IBKR actually holds.
        ibkr = {instrument_key(p.symbol, p.currency): p.quantity for p in broker.positions() if abs(p.quantity) > 1e-9}
        mine = j.holdings_qty()
        if any(abs(ibkr.get(k, 0) - mine.get(k, 0)) > 1e-6 for k in set(ibkr) | set(mine)):
            j.finish_run(run_id, "reconcile_failed", notes=f"ibkr={ibkr} journal={mine}")
            log(f"RECONCILE FAILED: IBKR {ibkr} vs journal {mine}. Not trading; needs a human.")
            return 3
        if broker.open_orders():
            j.finish_run(run_id, "reconcile_failed", notes="unexpected open orders at IBKR")
            log("Unexpected open orders at IBKR. Not trading; needs a human.")
            return 3

        # 2. Prices + FX
        try:
            # The base currency is always present: commissions can be reported in it.
            fx = {cfg.base_currency: 1.0, **market.fx_rates()}
            log(f"fx {fx}; pricing {len(cfg.universe)} instruments...")
            prices = market.price_all()
            if not prices:
                raise MarketDataUnavailableError("no prices returned for any instrument")
        except MarketDataUnavailableError as e:
            j.finish_run(run_id, "market_data_unavailable", notes=str(e))
            log(f"MARKET DATA UNAVAILABLE: {e} Skipping; no Claude call, no orders.")
            return 2
        market.prices, market.fx = prices, fx
        missing = [k for k in mine if k not in prices]
        if missing:
            j.finish_run(run_id, "error", notes=f"no price for holdings {missing}")
            log(f"No price for held {missing}; refusing to value portfolio.")
            return 1
        prices_base = {k: px * fx[cfg.universe[k].currency] for k, px in prices.items()}
        state = j.portfolio_state({k: prices_base[k] for k in mine})
        log(f"portfolio value {state.value_base:,.2f} cash {state.cash_base:,.2f} "
            f"drawdown {state.drawdown_pct:.2f}% orders this month {state.trades_this_month}")

        usd_open = market.market_open(now or datetime.now(NY))

        def liquidate() -> int:
            """Sell every holding (limit slightly below reference). Only when the market is open."""
            info = {k: MarketInfo(prices[k], fx[cfg.universe[k].currency], 0.0) for k in mine}
            liq = liquidation_proposals(state, info, cfg)
            if not dry_run and usd_open:
                rows = [(j.record_decision(run_id, Decision(p, True, [])), Decision(p, True, [])) for p in liq]
                execute(rows, broker, j, fx, fill_timeout, log)
            return len(liq)

        if state.halted:
            if mine and usd_open and not dry_run:
                # The kill switch fired while the market was closed, or some sells didn't fill:
                # keep liquidating at every open-market run until flat. Code decides, not Claude.
                n = liquidate()
                j.finish_run(run_id, "kill_switch", state, f"halted: completing liquidation ({n} sell orders)")
                log(f"Halted with {len(mine)} holding(s) left: placed {n} liquidation order(s).")
                return 0
            j.finish_run(run_id, "halted", state, "kill switch active; reset with journal_cli reset-halt")
            log("Trading halted (kill switch). Nothing to do.")
            return 0

        # 3. Kill switch: code decides, not Claude.
        if kill_switch_triggered(state, cfg):
            log(f"KILL SWITCH: drawdown {state.drawdown_pct:.2f}% >= {cfg.max_drawdown_pct}%")
            n = liquidate()
            j.halt(f"drawdown {state.drawdown_pct:.2f}%")
            j.finish_run(run_id, "kill_switch", state, f"liquidation orders: {n}; market open={usd_open}"
                         + ("" if usd_open else "; will sell at the next open-market run"))
            return 0

        if not usd_open and not dry_run:
            j.finish_run(run_id, "market_closed", state)
            log("US market closed (or <15 min left). Skipping (no Claude call, no cost).")
            return 0

        # Capital guard: the broker account must cover the virtual capital. Checked after the kill
        # switch on purpose, so a forced liquidation is never blocked by it.
        try:
            acct_fx = fx[snap.currency] if snap.currency in fx else market.fx_to_base(snap.currency)
            acct_base = check_capital(snap.net_liquidation, acct_fx, cfg.capital, cfg.base_currency)
        except (BrokerSafetyError, MarketDataUnavailableError) as e:
            j.finish_run(run_id, "capital_guard", state, str(e))
            log(f"CAPITAL GUARD: {e} Not trading; needs a human.")
            return 3
        log(f"account {acct_base:,.2f} {cfg.base_currency} covers capital {cfg.capital:,.2f}")

        # Monthly order limit used up: every order Claude proposed would be blocked, so don't pay
        # for the call. The kill switch above is unaffected (liquidation bypasses this limit).
        if state.trades_this_month >= cfg.max_trades_per_month:
            j.finish_run(run_id, "order_limit_reached", state,
                         f"{state.trades_this_month}/{cfg.max_trades_per_month} orders used this month")
            log(f"Monthly order limit reached ({state.trades_this_month}/{cfg.max_trades_per_month}). "
                "Skipping (no Claude call, no cost).")
            return 0

        # 4. Claude
        run_budget = llm.run_budget_usd
        if llm.cost_fn is not None:
            # LLM spend guardrail: never start a run once the monthly budget is used up.
            spent, cap = j.llm_spend_usd(), llm.monthly_budget_usd
            if spent >= cap:
                j.finish_run(run_id, "llm_budget_exhausted", state, f"spent US${spent:.2f} of US${cap:.2f} this month")
                log(f"LLM monthly budget used (US${spent:.2f}/{cap:.2f}). Skipping; no trades.")
                return 0
            run_budget = min(llm.run_budget_usd, cap - spent)
            log(f"LLM {llm.provider}/{llm.model}; spent this month US${spent:.2f} of US${cap:.2f}")
        client = llm.make_client(market)
        agent = TradingAgent(
            client, cfg, state, market, my_picks, recent_activity=j.recent_activity(10), avg_costs=j.avg_costs(),
            request_timeout_s=llm.request_timeout_s,
            **({} if llm.max_seconds is None else dict(max_seconds=llm.max_seconds)),
            web_search=web_search, log=log, model=llm.model,
            **({} if llm.cost_fn is None else dict(
                cost_fn=llm.cost_fn, run_budget_usd=run_budget,
                on_usage=lambda usage, cost: j.record_llm_usage(run_id, llm.provider, llm.model, usage, cost))),
        )
        result = agent.run()
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"run_{run_id}.json").write_text(json.dumps(_jsonable(result.transcript), indent=1, default=str))
        log(f"Claude: {result.turns} turns, tokens in/out {result.input_tokens}/{result.output_tokens}, "
            f"cost US${result.cost_usd:.4f}; summary: {result.summary or '(none)'}")

        # 5. Final gate check - authoritative, independent of what Claude saw.
        info = {}
        for p in result.proposals:
            if p.key in prices:
                info[p.key] = MarketInfo(prices[p.key], fx[p.currency], market.commission_base(p))
        decisions, _ = evaluate(result.proposals, state, info, cfg)
        rows = [(j.record_decision(run_id, d), d) for d in decisions]
        for _, d in rows:
            p = d.proposal
            log(f"[gate] {p.action} {p.quantity:g} {p.symbol} @ {p.limit_price}: "
                f"{'APPROVED' if d.approved else 'BLOCKED ' + '; '.join(d.reasons)}")

        # 6. Execute
        approved = [(pid, d) for pid, d in rows if d.approved]
        if dry_run:
            j.finish_run(run_id, "dry_run", state, result.summary)
            log(f"Dry run: {len(approved)} order(s) would be placed. Nothing sent.")
            return 0
        execute(approved, broker, j, fx, fill_timeout, log)
        final = j.portfolio_state({k: prices_base[k] for k in j.holdings_qty()}, observe=False)
        j.finish_run(run_id, "ok", final, result.summary)
        log(f"Done. value {final.value_base:,.2f} cash {final.cash_base:,.2f}")
        return 0
    except Exception as e:
        j.finish_run(run_id, "error", notes=f"{e!r}\n{traceback.format_exc()[-2000:]}")
        log(f"ERROR: {e!r}")
        raise
