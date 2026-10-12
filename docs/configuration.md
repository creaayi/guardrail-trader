# How to use

Once it's running, this is a set-and-forget setup. What you tune is **your rules**, **your ticker
universe**, **your spend caps** and, if you want, **the schedule**. Claude can read all of these but
can't change any of them.

| What | File | Takes effect |
|---|---|---|
| Budget and risk limits | [`config/risk.toml`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/config/risk.toml) | next run |
| Tradable tickers | `config/universe/*.csv` | next run |
| LLM provider, model, spend caps, broker port | `.env` | next run (Docker: `docker compose up -d` to reload env) |
| Run times | [`scripts/scheduled_run.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/scripts/scheduled_run.py) → `SLOTS` | next tick (Docker: rebuild the image) |

In Docker, `config/` is bind-mounted read-only, so edits on the host apply without a rebuild.

## Rules: `config/risk.toml`

```toml
[budget]
capital = 800000.0     # total virtual budget the bot may use
currency = "EUR"       # base currency for all accounting

[limits]
max_position_pct = 25.0         # no single holding above 25% of portfolio value
max_trades_per_month = 10       # orders submitted per calendar month
max_drawdown_pct = 25.0         # kill switch: sell all + halt at this drawdown from peak
max_price_deviation_pct = 2.0   # limit price must be within 2% of reference price
max_commission_pct = 10.0       # block orders whose commission exceeds 10% of order value
allow_fractional = false        # IBKR rejects fractional orders via the API
```

- The budget is deposited into the journal's ledger once, by `scripts/journal_cli.py init`, so set
  `capital` and `currency` **before** you run `init`. `init` connects to IB Gateway (read-only) and refuses
  if the account's net liquidation, converted to the base currency, is below `capital`; every run checks the
  same (status `capital_guard`), after the kill switch so a forced liquidation is never blocked.
- To raise the budget later, raise `capital` and run `scripts/journal_cli.py topup` (a dry run that prints
  the difference), then `topup --confirm` to deposit it. It deposits `capital` minus what is already
  deposited, so running it twice deposits once. Deposits are append-only: lowering `capital` below the
  total deposited is refused, there are no withdrawals. A top-up is not a drawdown (the peak only rises
  after two consecutive runs at the higher value) and the per-holding cap, which is a percentage of
  portfolio value, grows with it.
- Values are validated on load. A missing or out-of-range value stops the run (fail-closed).
- Tighter is always safe. To see which tickers fit your position limit at current prices, run
  `scripts/affordability.py`.

## Universe: which tickers may be traded

`[universe] files` lists CSV files. Only instruments in these files can be bought; no files or empty
files means nothing can be bought.

```toml
[universe]
files = [
  "config/universe/my_picks.csv",
  "config/universe/top_sp500.csv",
]
```

Each CSV has the columns `symbol,exchange,currency,name,source`:

```csv
symbol,exchange,currency,name,source
NVDA,SMART,USD,NVIDIA Corp,my-picks
CBA,ASX,AUD,Commonwealth Bank,my-picks
```

US stocks and ETFs use `SMART`/`USD`; ASX uses `ASX`/`AUD`. To use your own list, add a CSV and
point `files` at it. `scripts/refresh_universe.py` rebuilds `top_sp500.csv` from the SPY holdings
file (largest S&P 500 names by weight); run it monthly if you use that list.

!!! tip "Keep the universe small"
    Every ticker is priced on every run and summarised for Claude. About 50 keeps prompts small,
    pricing fast and cost low.

## LLM provider and spend caps

In `.env`:

```bash
LLM_PROVIDER=anthropic            # anthropic | openrouter
ANTHROPIC_API_KEY=...             # or OPENROUTER_API_KEY=...
CLAUDE_MODEL=                     # blank = default Sonnet; Haiku is cheaper
LLM_RUN_BUDGET_USD=0.10           # stop the loop (no trades) if one run costs more
LLM_MONTHLY_BUDGET_USD=2.50       # skip Claude for the rest of the month once spent
ENABLE_WEB_SEARCH=0               # 1 = allow news search (extra cost per search)
LLM_REQUEST_TIMEOUT_S=60          # per API call (SDK default is 10 min)
AGENT_MAX_SECONDS=300             # whole agent loop; over it = no trades
```

Cost is computed from actual token usage and a price table in
[`llm.py`](https://github.com/saifulislamamiyo/guardrail-trader/blob/main/guardrail_trader/llm.py). A model with no known price is refused, so caps can't be bypassed
by picking an unpriced model.

## Schedule

Two slots per US trading day, in New York time, so daylight-saving changes on either side are
handled automatically:

```python
SLOTS = {"open": (time(10, 30), time(11, 30)), "close": (time(15, 0), time(15, 40))}
MAX_ATTEMPTS = 3
```

The scheduler ticks every 15 minutes. Each slot runs at most once per day (up to 3 attempts if IB
Gateway is briefly unavailable). Weekends and US holidays are skipped using IBKR's own trading
hours, with no LLM call.

## Going live

Not recommended until you've watched paper trading for a while. It needs three deliberate changes:
`TRADING_MODE=live`, a live port (`4001`, or `4003` in Docker), and a live account. The code
refuses any mismatch between mode, port and account ID.
