# Tâches Claude Code — adaptation de guardrail-trader, phase 1

Contexte : fork de guardrail-trader sur un VPS Linux, mode Docker. Le `CLAUDE.md` v1.1 à la racine est la référence ; il prime sur le README d'origine en cas de conflit. Une branche et une PR par tâche, tests verts à chaque étape, `uv lock` après toute modification de dépendances. Ne pas commencer une tâche avant que la précédente soit mergée. Ne jamais assouplir une limite existante.

## T0 — État des lieux (sans modification)
- Lire `CLAUDE.md`, `docs/architecture.md`, `docs/harness-and-loop.md`, `guardrail_trader/risk/gate.py`, `journal.py`, `executor.py`, `broker/ibkr.py`, `scripts/run_bot.py`, `scripts/scheduled_run.py`.
- `pytest -q` doit passer tel quel.
- Produire `docs/adaptation-plan.md` : pour chaque tâche T1–T10, fichiers touchés et risques. Attendre validation humaine.

## T1 — Devise EUR et capital
- `config/risk.toml` : `capital = 1000000.0`, `currency = "EUR"`.
- `agent.py` : enum `currency` du schéma `_ORDER` → `USD, EUR, GBP, CHF`.
- `market.py` / `broker/ibkr.py` : conversion FX vers EUR pour USD, GBP, CHF (vérifier que `fx_to_base` gère un panier, pas seulement AUD/USD).
- `journal.py` : `init_budget` en EUR ; `scripts/journal_cli.py init`.
- `scripts/scheduled_run.py` : le fuseau de comptage mensuel passe de Sydney à Europe/Paris.
- Tests : FX multi-devises, budget EUR.

## T2 — Univers US + Europe
- Nouveau format CSV (`symbol,exchange,currency,name,sector,country,source`), validation fail-closed dans `risk/config.py` → `_read_universe_csv`.
- `config/universe/us_core.csv`, `eu_core.csv`, `hedge.csv` selon `CLAUDE.md` §3. Étendre `scripts/refresh_universe.py` (filtres capi et volume ; sources : constituants d'indices publics).
- `list_universe` renvoie nom, secteur, pays.
- Vérifier avec `scripts/check_connection.py` que chaque exchange IBKR européen résout correctement (contrat qualifié) sur un échantillon.
- Tests : parsing CSV, rejet d'une ligne sans secteur.

## T3 — Nouvelles limites dans la gate
- `risk/config.py` : clés `min_position_pct`, `max_sector_pct`, `max_us_pct`, `max_equity_pct`, `reentry_cooldown_days`, validées.
- `risk/gate.py` → `_check()` : taille minimale, secteur, part US, part actions, cooldown après stop (lecture via `PortfolioState` enrichi : `sector_weights`, `us_weight`, `recently_stopped`).
- Mettre `max_position_pct = 2.0`, `max_trades_per_month = 60`, `max_price_deviation_pct = 0.5`, `max_commission_pct = 1.0`.
- Tests : un cas par règle, plus un cas « rafale d'ordres qui dépasse un secteur ensemble mais pas séparément ».

## T4 — Paliers de drawdown et perte journalière
- `risk/config.py` : `drawdown_pause_pct`, `drawdown_reduce_pct`, `daily_loss_halt_pct`, avec contrainte `pause < reduce < max_drawdown`.
- `risk/gate.py` : états `PAUSED` (aucun BUY pendant N jours ouvrés), `REDUCED` (exposition actions cible ≤ 50 % : les BUY sont bloqués tant que l'exposition dépasse 50 %), halt journalier (aucun ordre le lendemain d'une perte > 3 %). Le kill switch −15 % d'origine reste tel quel.
- `journal.py` : table `state` pour mémoriser le palier courant et sa date ; `run_bot.py` step 3 étendu.
- Tests : transitions entre paliers, sortie de pause après N jours, kill switch inchangé.

## T5 — Stops de protection chez IBKR
- `broker/ibkr.py` : `place_stop(contract, qty, stop_price)` (ordre STP GTC), `modify_stop(order_id, new_price)`, `cancel_stop`, `open_stop_orders()`.
- `market.py` : ATR14 à partir de `daily_history` (nécessite high/low : passer de closes seuls à OHLC).
- `executor.py` : après chaque fill BUY, placer le stop ; après chaque fill SELL, annuler le stop correspondant. Table `stops` dans `journal.py` (position, order_id, prix, historique des relèvements).
- `run_bot.py` nouveau step « stops » avant l'appel LLM : relever les stops suiveurs (jamais abaisser), détecter les stops exécutés depuis le dernier run → `closed_trades` + cooldown.
- Réconciliation (step 1) : les ordres ouverts connus dans `stops` sont tolérés ; tout autre ordre ouvert = arrêt.
- Tests : calcul de stop borné [−12 %, −6 %], relèvement monotone, réconciliation avec stops.

## T6 — Journal enrichi et closed_trades
- `agent.py` : `_ORDER` exige `poche`, `these`, `catalyseur`, `fiabilite`, `horizon_jours`, `stop_pct`, `objectif_pct`, `invalidation`, `confiance`, `sources`. `SYSTEM_PROMPT` réécrit pour l'exiger et expliquer la calibration. Validation stricte dans `_dispatch` (ordre sans champ = rejeté avec raison).
- `journal.py` : colonnes ajoutées à `proposals` (migration SQLite idempotente), table `closed_trades`, remplie par T5 et par les SELL volontaires (motif).
- `dashboard_data.py` / `web/dashboard.html` : afficher poche, confiance, thèse.
- Tests : rejet d'une proposition incomplète, migration sur une base existante.

## T7 — Benchmark IWDA
- `journal.py` : table `benchmark` (date, nav_iwda_equivalent). Au `init`, nombre de parts virtuelles = capital / prix IWDA du jour.
- `run_bot.py` créneau `us_close` : snapshot NAV portefeuille + NAV benchmark.
- Tests : calcul de l'alpha sur une série synthétique.

## T8 — Créneaux Europe
- `scripts/scheduled_run.py` : `SLOTS` → `eu_open` (10:00–11:00 Europe/Paris, marchés EU), `us_open` (10:30–11:30 America/New_York), `us_close` (15:00–15:40 America/New_York). Chaque créneau porte la liste des exchanges concernés ; `market.is_open` évalué par exchange.
- `run_bot.py` accepte `--slot` et ne propose que les titres des marchés ouverts dans ce créneau.
- Rebalancement du lundi : flag `rebalance=True` passé au prompt dans les créneaux `eu_open` et `us_open`.
- Docker : `TIME_ZONE: Europe/Paris`, `AUTO_RESTART_TIME: "04:00 AM"`.
- Tests : détection de créneau aux deux fuseaux, jours fériés par exchange.

## T9 — Rapport hebdomadaire et alertes
- `scripts/weekly_report.py` : génère `reports/YYYY-WW.md` selon `CLAUDE.md` §8.3, depuis SQLite uniquement. Lancé par le scheduler le vendredi après `us_close`.
- `guardrail_trader/alerts.py` : envoi ntfy (par défaut, topic dans `.env`) ou Telegram. Appelé en fin de run (résumé 5 lignes) et immédiatement sur kill switch, palier, échec de réconciliation, run non exécuté. Watchdog : alerte si aucun heartbeat depuis > 2 h en séance (étendre `scripts/gateway_watchdog.py`).
- Tests : rapport sur base synthétique, alerte simulée.

## T10 — Semaine à blanc
- `.env` : `LLM_RUN_BUDGET_USD=1.50`, `LLM_MONTHLY_BUDGET_USD=120`, `ENABLE_WEB_SEARCH=1`, `IBKR_MARKET_DATA_TYPE=3`.
- `docker compose up -d`, vérifier `check_connection.py` (compte `DU`), puis 5 jours ouvrés de `scheduled_run.py` en `--dry-run` : propositions journalisées, verdicts de gate, aucun ordre.
- Chaque jour, un second run `--dry-run` au même créneau ; script `scripts/consistency.py` qui compare les deux jeux de propositions (taux de désaccord sur les titres et les sens).
- Livrable : `reports/dry-run-week.md` (coût LLM réel par run, taux de désaccord, propositions bloquées et pourquoi). L'humain décide du passage en réel ; cette décision est datée dans `CHANGELOG.md` et fixe le jour 0.

## Hors périmètre phase 1 (ne pas commencer)
- Outils `get_news`, `get_fundamentals`, `get_catalysts` et poche B.
- Tout changement de modèle, de limite ou de créneau non décrit ci-dessus.
