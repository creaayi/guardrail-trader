"""The Claude agent loop (lesson 2's loop, now with a real harness around it).

Claude can only READ (portfolio, universe, price history, optional web search) and
PROPOSE. It never touches the broker. Its final proposals go through the risk gate
again in run_bot.py before anything is sent to IBKR.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from guardrail_trader.risk import (
    Decision, MarketInfo, PortfolioState, Proposal, RiskConfig, evaluate,
)

MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "20"))
MAX_HISTORY_CALLS = 15   # IBKR historical-data pacing + cost control
MAX_CHECK_CALLS = 5
MAX_SUBMIT_REVISIONS = 1   # a submit with blocked orders is bounced back once, with the gate's reasons
MAX_SECONDS = float(os.getenv("AGENT_MAX_SECONDS", "300"))   # wall-clock budget for the whole loop

SYSTEM_PROMPT = """You are the portfolio manager for a small, fully automated stock portfolio.

Objective: maximise long-term total return in {base} for this portfolio, while staying inside the
risk limits below. Those limits are enforced by code you cannot change: any order that breaks
them is rejected. Doing nothing is a valid and often correct decision.

Hard limits (code-enforced):
- Only instruments in the allowlist (see list_universe). Whole shares only. BUY or SELL only; no shorting, no margin.
- Limit orders only; limit price must be within {dev}% of the current reference price.
- No single holding above {pos}% of portfolio value.
- At most {trades} orders per calendar month ({used} already used this month).
- Orders whose commission exceeds {comm}% of order value are rejected.
- If the portfolio falls {dd}% below its peak, code sells everything and halts trading.

Things to weigh:
- Every order pays a broker commission; frequent trading erodes returns. Prefer fewer, deliberate trades.
- Prices you see may be delayed ~15 minutes.
- Diversify: a concentrated portfolio can hit the kill switch.
- Selling is a normal decision, not a failure. Each holding shows avg_cost and unrealized P/L. Consider a SELL when
  the reason you bought no longer holds (trend reversed, news changed), a holding is near the position cap, or a
  clearly better candidate needs the cash. Weigh the round trip (a sell plus a buy is 2 of the monthly orders,
  plus commission) against the expected gain. A gain alone is not a reason to sell, and a loss alone is not a
  reason to hold.
- Holdings the owner specifically picked are marked my_pick=true; treat them as candidates, not obligations.
- get_portfolio includes recent_activity: your last orders and what happened (blocked, filled, not filled).
  Don't repeat an order that was just blocked or didn't fill without a reason; unfilled orders still used up the monthly limit.

Process:
1. Call get_portfolio.
2. Explore candidates with list_universe and get_price_history (max {hist} history calls per run).
3. Optionally call check_orders to dry-run proposals through the risk gate (max {checks} calls).
4. Finish by calling submit_orders, with an empty list if you decide to hold. If any order breaks a
   limit, submit_orders returns the gate's reasons once so you can fix or drop it; after that,
   blocked orders are simply dropped.
Every order needs a concise, specific reason; it is logged for the owner to audit."""

TOOLS = [
    {"name": "get_portfolio",
     "description": "Current virtual portfolio: cash, holdings with prices and weights, total value, peak, drawdown, orders used this month.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_universe",
     "description": "Allowlisted instruments with latest price. Filter to keep output small.",
     "input_schema": {"type": "object", "properties": {
         "only_affordable": {"type": "boolean", "description": "Only instruments where 1 share fits the max position size. Default true."},
         "my_picks_only": {"type": "boolean", "description": "Only the owner's picks."},
         "symbols": {"type": "array", "items": {"type": "string"}, "description": "Only these tickers."},
         "limit": {"type": "integer", "description": "Max rows (default 600)."}}}},
    {"name": "get_price_history",
     "description": "6-month daily price summary for one ticker: returns (1w/1m/3m/6m), SMA20/50, 6m high/low, volatility, last 10 closes.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "check_orders",
     "description": "Dry-run proposed orders through the risk gate. Returns approve/block with reasons. Places nothing.",
     "input_schema": {}},  # filled below
    {"name": "submit_orders",
     "description": "FINAL. Submit orders (possibly empty) and a short summary of your reasoning. Ends your turn.",
     "input_schema": {}},
]

_ORDER = {"type": "object", "properties": {
    "symbol": {"type": "string"}, "currency": {"type": "string", "enum": ["USD", "EUR", "GBP", "CHF"]},
    "action": {"type": "string", "enum": ["BUY", "SELL"]},
    "quantity": {"type": "integer", "minimum": 1},
    "limit_price": {"type": "number", "description": "In the instrument currency."},
    "reason": {"type": "string"}},
    "required": ["symbol", "currency", "action", "quantity", "limit_price", "reason"]}
TOOLS[3]["input_schema"] = {"type": "object", "properties": {"orders": {"type": "array", "items": _ORDER}},
                            "required": ["orders"]}
TOOLS[4]["input_schema"] = {"type": "object", "properties": {
    "orders": {"type": "array", "items": _ORDER},
    "summary": {"type": "string", "description": "2-5 sentences: market view and why these orders (or none)."}},
    "required": ["orders", "summary"]}

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


class MarketView(Protocol):
    """What the agent may know about the market. Real impl wraps IBKR; tests use stubs."""
    prices: dict[str, float]        # key -> price in instrument currency
    fx: dict[str, float]            # currency -> base units per 1
    def history_summary(self, key: str) -> dict: ...
    def commission_base(self, p: Proposal) -> float: ...


@dataclass
class AgentResult:
    proposals: list[Proposal]
    summary: str
    submitted: bool
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""
    transcript: list = field(default_factory=list)


def _free(_model, _usage) -> float:
    return 0.0


class TradingAgent:
    def __init__(self, client, cfg: RiskConfig, state: PortfolioState, market: MarketView,
                 my_picks: set[str] | None = None, web_search: bool = False,
                 log: Callable[[str], None] = print, model: str = "claude-sonnet-5",
                 cost_fn: Callable = _free, run_budget_usd: float = 0.50,
                 on_usage: Callable | None = None, recent_activity: list[dict] | None = None,
                 avg_costs: dict[str, float] | None = None,
                 max_seconds: float = MAX_SECONDS, request_timeout_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.client, self.cfg, self.state, self.market = client, cfg, state, market
        self.model, self.cost_fn, self.run_budget_usd, self.on_usage = model, cost_fn, run_budget_usd, on_usage
        self.picks = my_picks or set()
        self.web_search = web_search
        self.log = log
        self.history_calls = 0
        self.check_calls = 0
        self.submit_revisions = 0
        self.recent = recent_activity or []
        self.avg_costs = avg_costs or {}
        self.max_seconds, self.request_timeout_s, self.clock = max_seconds, request_timeout_s, clock
        self.max_pos_base = state.value_base * cfg.max_position_pct / 100

    # ---------------------------------------------------------------- loop
    def run(self, task: str = "Run today's portfolio review and decide what to do.") -> AgentResult:
        c = self.cfg
        system = SYSTEM_PROMPT.format(base=c.base_currency, dev=c.max_price_deviation_pct, pos=c.max_position_pct,
                                      trades=c.max_trades_per_month, used=self.state.trades_this_month,
                                      comm=c.max_commission_pct, dd=c.max_drawdown_pct,
                                      hist=MAX_HISTORY_CALLS, checks=MAX_CHECK_CALLS)
        # Prompt caching: system + tools are identical every turn; the conversation prefix grows.
        # Cached input is billed at 10% of the normal price.
        tools = [dict(t) for t in TOOLS]
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        tools += [WEB_SEARCH_TOOL] if self.web_search else []
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        messages: list = [{"role": "user", "content": [{"type": "text", "text": task}]}]
        res = AgentResult([], "", False, 0, transcript=messages)
        deadline = self.clock() + self.max_seconds

        for turn in range(1, MAX_TURNS + 1):
            res.turns = turn
            remaining = deadline - self.clock()
            if remaining <= 0:
                self.log(f"[harness] loop exceeded {self.max_seconds:.0f}s -> stop, no trades")
                res.stop_reason = "time_budget_exceeded"
                return res
            _move_cache_breakpoint(messages)
            resp = self.client.messages.create(model=self.model, max_tokens=4096, system=system_blocks,
                                               tools=tools, messages=messages,
                                               timeout=max(5.0, min(self.request_timeout_s, remaining)))
            res.input_tokens += int(getattr(resp.usage, "input_tokens", 0) or 0)
            res.output_tokens += int(getattr(resp.usage, "output_tokens", 0) or 0)
            cost = self.cost_fn(self.model, resp.usage)
            res.cost_usd += cost
            if self.on_usage:
                self.on_usage(resp.usage, cost)
            messages.append({"role": "assistant", "content": resp.content})
            if res.cost_usd > self.run_budget_usd:
                self.log(f"[harness] run LLM cost US${res.cost_usd:.3f} > budget US${self.run_budget_usd:.2f} -> stop, no trades")
                res.stop_reason = "run_budget_exceeded"
                return res
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    self.log(f"[claude] {b.text.strip()[:500]}")

            if resp.stop_reason == "pause_turn":   # server tool (web search) still working
                continue
            if resp.stop_reason != "tool_use":
                self.log("[harness] Claude stopped without submit_orders -> no trades")
                return res

            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                out, is_err = self._dispatch(b.name, b.input or {})
                self.log(f"[tool] {b.name}({json.dumps(b.input)[:200]}) -> {'ERROR ' if is_err else ''}{out[:200]}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": is_err})
                if b.name == "submit_orders" and not is_err:
                    res.proposals, res.summary, res.submitted = self._to_proposals(b.input["orders"]), b.input.get("summary", ""), True
            messages.append({"role": "user", "content": results})
            if res.submitted:
                return res

        self.log(f"[harness] hit MAX_TURNS={MAX_TURNS} -> no trades")
        return res

    # ---------------------------------------------------------------- tools
    def _dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            handler = {"get_portfolio": self._portfolio, "list_universe": self._universe,
                       "get_price_history": self._history, "check_orders": self._check,
                       "submit_orders": self._submit}[name]
        except KeyError:
            return f"unknown tool {name}", True
        try:
            return handler(args), False
        except _ToolError as e:
            return str(e), True
        except Exception as e:  # never let a tool crash the loop; Claude sees the error
            return f"tool error: {e!r}", True

    def _portfolio(self, _args) -> str:
        s, v = self.state, self.state.value_base
        return json.dumps({
            "base_currency": self.cfg.base_currency, "cash": round(s.cash_base, 2), "total_value": round(v, 2),
            "peak_value": round(s.peak_value_base, 2), "drawdown_pct": round(s.drawdown_pct, 2),
            "orders_used_this_month": s.trades_this_month, "orders_limit_per_month": self.cfg.max_trades_per_month,
            "max_value_per_holding": round(self.max_pos_base, 2),
            "holdings": [self._holding_row(k, h, v) for k, h in s.holdings.items()],
            "recent_activity": self.recent,
        })

    def _holding_row(self, key: str, h, total: float) -> dict:
        """One holding as Claude sees it. avg_cost/P&L are in base currency, from the journal's ledger
        (commission included), so a sell decision can weigh what the position has actually earned."""
        value = h.quantity * h.price_base
        row = {"symbol": key.split(":")[0], "currency": key.split(":")[1], "quantity": h.quantity,
               "price": round(self.market.prices.get(key, float("nan")), 2),
               "value": round(value, 2),
               "weight_pct": round(value / total * 100, 2) if total else 0}
        cost = self.avg_costs.get(key)
        if cost:
            row["avg_cost"] = round(cost, 2)
            row["unrealized_pnl"] = round((h.price_base - cost) * h.quantity, 2)
            row["unrealized_pnl_pct"] = round((h.price_base / cost - 1) * 100, 2)
        return row

    def _universe(self, a) -> str:
        only_aff = a.get("only_affordable", True)
        syms = {s.upper() for s in a.get("symbols") or []}
        rows = ["symbol,currency,price,price_base,my_pick"]
        for key, inst in sorted(self.cfg.universe.items()):
            px = self.market.prices.get(key)
            if px is None or (syms and inst.symbol not in syms):
                continue
            if a.get("my_picks_only") and inst.symbol not in self.picks:
                continue
            pb = px * self.market.fx[inst.currency]
            if only_aff and pb > self.max_pos_base:
                continue
            rows.append(f"{inst.symbol},{inst.currency},{px:.2f},{pb:.2f},{str(inst.symbol in self.picks).lower()}")
            if len(rows) > int(a.get("limit", 600)):
                break
        return "\n".join(rows)

    def _history(self, a) -> str:
        if self.history_calls >= MAX_HISTORY_CALLS:
            raise _ToolError(f"history call limit ({MAX_HISTORY_CALLS}) reached for this run")
        key = self._key(a["symbol"], None)
        self.history_calls += 1
        return json.dumps(self.market.history_summary(key))

    def _check(self, a) -> str:
        if self.check_calls >= MAX_CHECK_CALLS:
            raise _ToolError(f"check_orders limit ({MAX_CHECK_CALLS}) reached; call submit_orders")
        self.check_calls += 1
        return json.dumps([_verdict(d) for d in self.evaluate(self._to_proposals(a["orders"]))])

    def _submit(self, a) -> str:
        proposals = self._to_proposals(a["orders"])
        decisions = self.evaluate(proposals)
        if any(not d.approved for d in decisions) and self.submit_revisions < MAX_SUBMIT_REVISIONS:
            self.submit_revisions += 1
            raise _ToolError(json.dumps({
                "rejected": "Some orders break a limit. Nothing was submitted. Fix or drop the blocked "
                            "orders and call submit_orders again. This is your only revision: next time, "
                            "blocked orders are dropped and the rest go ahead.",
                "gate": [_verdict(d) for d in decisions]}))
        return json.dumps({"received": len(proposals), "gate_preview": [_verdict(d) for d in decisions],
                           "note": "Final gate check and execution happen in code after this."})

    # ---------------------------------------------------------------- helpers
    def evaluate(self, proposals: list[Proposal]) -> list[Decision]:
        info = {}
        for p in proposals:
            px = self.market.prices.get(p.key)
            if px is not None and p.key not in info:
                info[p.key] = MarketInfo(px, self.market.fx[p.currency], self.market.commission_base(p))
        decisions, _ = evaluate(proposals, self.state, info, self.cfg)
        return decisions

    def _key(self, symbol: str, currency: str | None) -> str:
        symbol = symbol.upper().replace(".", " ")
        matches = [k for k, i in self.cfg.universe.items() if i.symbol == symbol and (currency is None or i.currency == currency.upper())]
        if not matches:
            raise _ToolError(f"{symbol} is not on the allowlist")
        return matches[0]

    def _to_proposals(self, orders: list[dict]) -> list[Proposal]:
        out = []
        for o in orders:
            sym, ccy = o["symbol"].upper().replace(".", " "), o["currency"].upper()
            inst = self.cfg.universe.get(f"{sym}:{ccy}")
            out.append(Proposal(sym, inst.exchange if inst else "?", ccy, o["action"].upper(),
                                float(o["quantity"]), float(o["limit_price"]), o.get("reason", "")))
        return out


class _ToolError(Exception):
    pass


def _move_cache_breakpoint(messages: list) -> None:
    """Keep exactly one cache breakpoint on the newest user message (API allows max 4 in total)."""
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]
    if last["role"] == "user" and isinstance(last["content"], list) and last["content"]:
        last["content"][-1]["cache_control"] = {"type": "ephemeral"}


def _verdict(d: Decision) -> dict:
    p = d.proposal
    return {"order": f"{p.action} {p.quantity:g} {p.symbol}:{p.currency} @ {p.limit_price}",
            "approved": d.approved, "block_reasons": d.reasons,
            "est_value_base": round(d.notional_base, 2), "est_commission_base": round(d.commission_base, 2)}
