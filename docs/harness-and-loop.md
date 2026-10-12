# Harness and agent loop

## Two ideas

- **Agent loop:** call the model → if it asks for a tool, run the tool → send the result back →
  repeat until it stops asking or a limit is hit. The API is stateless, so the growing message
  list *is* the agent's memory.
- **Harness:** everything around that loop: which tools exist, what they're allowed to do, when the
  loop must stop, what it may spend, and what gets recorded. In this project the harness, not the
  prompt, is what makes an LLM safe to let near a brokerage account.

## The loop in this project

```mermaid
sequenceDiagram
    participant H as Harness (agent.py)
    participant C as Claude
    participant T as Tools
    participant G as Risk gate
    H->>C: system prompt (limits) + task
    loop until submit_orders, stop, or a limit is hit
        C->>H: tool_use (get_portfolio / list_universe / get_price_history / check_orders)
        H->>T: run tool (read-only)
        T-->>H: result
        H-->>C: tool_result
    end
    C->>H: submit_orders(orders, summary)
    H->>G: final authoritative check (run_bot.py)
    G-->>H: approved / blocked + reasons
```

Implemented in [`agent.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/agent.py) → `TradingAgent.run()`. The loop stops when:

- Claude calls `submit_orders` (an empty list means "hold"),
- Claude ends its turn without submitting, which means **no trades**,
- `MAX_TURNS` (20) is reached,
- the per-run cost budget is exceeded, or
- the loop runs longer than `AGENT_MAX_SECONDS` (300 s). Each API call also has a timeout
  (`LLM_REQUEST_TIMEOUT_S`, 60 s, capped by the time left) and at most 2 retries.

Every one of these ends the run with **no trades**.

## Tools Claude gets

| Tool | Kind | Limit per run |
|---|---|---|
| `get_portfolio` | read | — |
| `list_universe` | read | — |
| `get_price_history` | read | `MAX_HISTORY_CALLS = 15` |
| `check_orders` | dry-run the gate | `MAX_CHECK_CALLS = 5` |
| `submit_orders` | final answer (a proposal, not an order) | ends the loop; bounced **once** if an order breaks a limit |

Claude never talks to the broker. `submit_orders` only hands proposals back to the harness.

For what Claude looks at when picking stocks, and how an ML model could plug in, see
[How stocks are chosen](stock-selection.md).

## Feedback inside the loop and across runs

- **Same run:** `check_orders` dry-runs proposals through the real gate. If `submit_orders` still
  contains a blocked order, the harness returns the gate's reasons as a tool error and lets Claude
  revise **once** (`MAX_SUBMIT_REVISIONS = 1`). After that, blocked orders are dropped and the rest
  go ahead. The bound stops a model from negotiating with the gate indefinitely.
- **Across runs:** `get_portfolio` includes `recent_activity`, the last 10 real orders and what
  happened to each (blocked with reasons, filled, partially filled, not filled). Unfilled orders
  still count toward the monthly limit, so Claude is told not to repeat them blindly.
  Source: [`journal.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/journal.py) → `recent_activity()`.

## Orders and fills

Orders are DAY limit orders. After placing them, the executor waits up to 120 s, cancels anything
still working, and books only real fills:

- **Fill/cancel race:** an order can fill between the status check and the cancel. `cancel_order()`
  then returns the final state instead of raising.
- **Settled fills:** executions and commission reports arrive separately. `fill_details()` waits
  until the executions add up to the filled quantity and each has its commission.
- **Still working after cancel:** the known fills are booked and a warning logged. If more fill later,
  the next run's reconciliation stops trading until a human looks.

## Harness concerns and where they live

| Concern | What the harness does | Where |
|---|---|---|
| Permissions | The gate decides; the executor only places gate-approved orders. | [`run_bot.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/scripts/run_bot.py) step 5 · [`executor.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/executor.py) |
| Paper vs live safety | Refuses live ports in paper mode; account ID must match the mode. | [`broker/ibkr.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/broker/ibkr.py) → `check_endpoint()`, `check_account()` |
| Reconciliation | Journal holdings must equal IBKR positions before any run. | `run_bot.py` step 1 |
| Virtual budget | The bot only sees its own ledger's cash, not the whole paper account. | [`journal.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/journal.py) → `cash_base()` |
| Stop conditions | Max turns, per-run cost, per-tool call limits. | `agent.py` |
| Spend control | Cost from real token usage; monthly cap checked before calling Claude. | [`llm.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/llm.py) → `cost_usd()` |
| Context management | Prompt caching; price history summarised before it reaches Claude. | `agent.py` → `_move_cache_breakpoint()` · [`market.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/market.py) → `summarize_history()` |
| Tool errors | Returned to Claude as `is_error` results instead of crashing. | `agent.py` → `_dispatch()` |
| Observability | Proposals, reasons, verdicts, orders, fills, cost and full transcripts recorded. | journal · `data/runs/run_<id>.json` · [Dashboard](dashboard.md) |

## The peak (for the kill switch)

Drawdown is measured from the portfolio's peak value. The peak only rises to a value **seen on two
consecutive runs** (the lower of this run's and the previous run's value), so one bad price tick
can't inflate it and cause a false kill switch later. Real gains count one run later. Only the
run's first valuation counts; the re-valuation after trading doesn't.
Source: [`journal.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/journal.py) → `observe_value()`.

## The risk gate

Every proposal passes the gate **twice**: when Claude calls `check_orders`/`submit_orders` (so it
can see why something was blocked), and again, authoritatively, in `run_bot.py` before anything is
sent. Orders are evaluated in sequence on a *simulated* portfolio, sells first, so a batch can't
break a limit that each order passes alone.

| Rule | Default | Configurable in |
|---|---|---|
| Only allowlisted instruments | universe CSVs | `[universe] files` |
| Max single holding | 2% of portfolio | `max_position_pct` |
| Min line after a BUY / left after a partial SELL (full SELL always allowed) | 0.5% | `min_position_pct` |
| Max per sector (`sector` column) | 20% | `max_sector_pct` |
| Max US holdings (`country` US) | 70% | `max_us_pct` |
| Max equity (cash ≥ the rest) | 90% | `max_equity_pct` |
| No re-entry after a stop exit | 10 business days | `reentry_cooldown_days` |
| Hedge instruments (pocket C) blocked | while `enable_hedge = false` | `[universe]` |
| Protective stop on every holding: SELL STP GTC at entry − 2×ATR14, bounded [−12%, −6%]; to entry at +1×ATR, then 2×ATR under the highest close; never lowered; cancelled/shrunk before any SELL | always | `stop_*`, `breakeven_atr_mult`, `trail_atr_mult` |
| Max orders per calendar month (Paris); once used up, runs skip Claude until the 1st | 60 | `max_trades_per_month` |
| Drawdown −8%: PAUSED, no new BUY | 5 business days, then RECOVERY | `drawdown_pause_pct`, `drawdown_pause_days` |
| RECOVERY: BUYs at half the max size | until the drawdown is back under 4% | `drawdown_resume_pct` |
| Drawdown −12%: REDUCED, no BUY while equity > 50% (no forced sale) | then RECOVERY | `drawdown_reduce_pct`, `reduce_max_equity_pct` |
| A day's loss > 3% (close to close): no order the next day, except stops | 3% | `daily_loss_halt_pct` |
| Kill switch: drawdown from peak | 15% → sell all, halt until manual reset; holdings left unsold (market closed) are sold at the next open run | `max_drawdown_pct` |
| Limit price within ± of reference | 0.5% | `max_price_deviation_pct` |
| Commission ≤ share of order value | 1% | `max_commission_pct` |
| Whole shares only | on | `allow_fractional` |
| No shorting, no margin | always | hard-coded |
| Invalid config refuses to load | always | hard-coded |

Code: [`risk/gate.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/risk/gate.py) → `evaluate()`, `_check()`, `kill_switch_triggered()`. Tests: [`tests/test_risk_gate.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/tests/test_risk_gate.py).

!!! tip "Try it without spending"
    `run_bot.py --dry-run --fake-claude` drives the whole harness with a scripted stand-in for
    Claude ([`fake_llm.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/fake_llm.py)): no API cost, no orders.
