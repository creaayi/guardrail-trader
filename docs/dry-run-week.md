# Dry-run week and day 0

Mandatory before the first real order (CLAUDE.md §10, T10). Five trading days of scheduled runs in
`--dry-run`: Claude decides, the gate judges, nothing is sent to IBKR. Each decision slot runs twice to
measure consistency.

## Start (operator)

1. `.env` (mode 600): `SCHEDULER_DRY_RUN=1`, `NTFY_TOPIC=...`, and the T10 values
   `LLM_RUN_BUDGET_USD=1.50`, `LLM_MONTHLY_BUDGET_USD=120`, `ENABLE_WEB_SEARCH=1`, `IBKR_MARKET_DATA_TYPE=3`.
2. Universe and bars: `config/universe/*.csv` filled, `data/bars.sqlite` refreshed (`scripts/refresh_bars.py`).
3. `docker compose up -d` (recreates the gateway with the Paris time zone and the 04:00 restart), then
   `.venv/bin/python scripts/check_connection.py`: paper account `DU…`, paper port.
4. `.venv/bin/python scripts/journal_cli.py init`: deposits the capital and buys the IWDA benchmark units.
   This journal is the dry-run journal; it is archived at day 0.
5. Host crontab: the watchdog line from [Alerts](alerts.md).

## During the week

- Slots `eu_open`, `us_open`, `us_close` run twice (second run right after the first, same lock); `stops`
  runs once. Every run sends its ntfy summary.
- Cost bound: 3 slots × 2 runs × 1.50 USD = 9 USD a day at most.

## End of the week

`.venv/bin/python scripts/consistency.py --since YYYY-MM-DD --until YYYY-MM-DD` writes
`reports/dry-run-week.md`: LLM cost per run, disagreement between the two runs of each slot (orders, symbols,
opposite directions), blocked proposals grouped by reason.

## Day 0 (human decision, dated in `CHANGELOG.md`)

1. `docker compose stop bot`.
2. Archive the dry-run journal: `mkdir -p data/dry-run-week && mv data/journal.sqlite data/dry-run-week/`
   (its NAV, benchmark and tier rows belong to the dry week, not to the 12-month test).
3. Remove `SCHEDULER_DRY_RUN` from `.env`.
4. `.venv/bin/python scripts/journal_cli.py init` again: the 12 months start from this capital and this
   IWDA price.
5. `docker compose up -d bot`.
