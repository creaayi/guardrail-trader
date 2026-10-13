# Protective stops

Every holding has one SELL **STP** order at IBKR, placed and moved by code (CLAUDE.md §5,
[`guardrail_trader/stops.py`](https://github.com/creaayi/guardrail-trader/blob/main/guardrail_trader/stops.py)).

| Rule | Value |
|---|---|
| Initial stop | entry − 2 × ATR14, bounded to [−12 %, −6 %] of entry |
| Breakeven | highest close since entry ≥ entry + 1 × ATR14 → stop raised to entry |
| Trailing | then 2 × ATR14 below the highest close; never lowered |
| Order | SELL STP, GTC, **regular trading hours only** (`outsideRth = false`) |
| Before any sale | the stop is cancelled (full sale) or shrunk (partial sale) first |

## What a stop does not protect against

!!! warning "A stop is a trigger, not a guaranteed price"
    - **Regular hours only.** The stop is not active in pre-market, after-hours or overnight. A
      move outside the session does nothing until the next regular session opens.
    - **Gaps.** If a stock opens below the stop (overnight news, results, a market-wide gap), the
      stop triggers at the open and becomes a market sell: it fills **at the opening price, below the
      stop**, possibly well below. A −12 % stop can realise −20 % on a bad gap.
    - **Fast markets.** Even intraday, a STP fills at the next available price, not at the stop.

The realised exit price is booked as reported by IBKR (`closed_trades`, motif `stop`); nothing is
adjusted to the stop level. The weekly report should show the slippage between stop price and fill.

## Executions between runs

IBKR only reports the current day's executions. Each run books the stops' executions before
reconciliation, and an evening slot (16:15–16:45 New York, before midnight in Paris) runs
`run_bot.py --stops-only` to catch the day's last ones. An execution that can no longer be found
stops the bot (`stop_sync_failed`, exit code 3) until it is booked by hand:

```
.venv/bin/python scripts/journal_cli.py stops
.venv/bin/python scripts/journal_cli.py book-stop-fill --perm-id P --shares N --price X --commission C --fx F --confirm
```
