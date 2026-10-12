# Tâches Claude Code — adaptation de guardrail-trader, phase 1

Contexte : fork de guardrail-trader sur un VPS Linux, mode Docker. Le `CLAUDE.md` v1.1 à la racine est la référence ; il prime sur le README d'origine en cas de conflit. Une branche et une PR par tâche, tests verts à chaque étape, `uv lock` après toute modification de dépendances. Ne pas commencer une tâche avant que la précédente soit mergée. Amendement du 12/10/2026 : Claude Code merge lui-même ses PR (`gh pr merge --squash --delete-branch`), uniquement si `pytest -q` est vert, puis enchaîne sur la tâche suivante. Exceptions : T3, T4 et T5 — PR ouverte, résumé donné à l'humain, merge seulement après sa validation. Ne jamais assouplir une limite existante.

Mise à jour du 12/10/2026 : décisions de validation de T0 intégrées (voir `CHANGELOG.md` et `docs/adaptation-plan.md` §5).

## T0 — État des lieux (sans modification)
- Lire `CLAUDE.md`, `docs/architecture.md`, `docs/harness-and-loop.md`, `guardrail_trader/risk/gate.py`, `journal.py`, `executor.py`, `broker/ibkr.py`, `scripts/run_bot.py`, `scripts/scheduled_run.py`.
- `pytest -q` doit passer tel quel.
- Produire `docs/adaptation-plan.md` : pour chaque tâche T1–T10, fichiers touchés et risques. Attendre validation humaine.
- Environnement : `uv` 0.10.12 (même version que la CI) et `gh` installés ; `.venv` créé par `uv sync --frozen --extra dev`.

## T1 — Devise EUR et capital
- `config/risk.toml` : `capital = 800000.0`, `currency = "EUR"` (compte paper plafonné à 1 M USD). Répercuter dans `CLAUDE.md` §1 et §4, amendement daté du 12/10/2026 dans `CHANGELOG.md`.
- Garde-fou : `journal_cli.py init` et chaque run refusent de démarrer si la valeur liquidative du compte paper convertie en EUR est inférieure à `capital`.
- `fx` contient toujours la devise de base (`EUR: 1.0`) pour convertir les commissions.
- `agent.py` : enum `currency` du schéma `_ORDER` → `USD, EUR, GBP, CHF`.
- `market.py` / `broker/ibkr.py` : conversion FX vers EUR pour USD, GBP, CHF (vérifier que `fx_to_base` gère un panier, pas seulement AUD/USD).
- `journal.py` : `init_budget` en EUR ; `scripts/journal_cli.py init`.
- `scripts/scheduled_run.py` : le fuseau de comptage mensuel passe de Sydney à Europe/Paris.
- Tests : FX multi-devises, budget EUR, garde-fou valeur liquidative < capital.

## T2 — Univers US + Europe
- Nouveau format CSV (`symbol,exchange,currency,name,sector,country,source`), validation fail-closed dans `risk/config.py` → `_read_universe_csv`.
- `config/universe/us_core.csv`, `eu_core.csv`, `hedge.csv` selon `CLAUDE.md` §3. Étendre `scripts/refresh_universe.py` (filtres capi et volume ; sources : constituants d'indices publics).
- `list_universe` renvoie nom, secteur, pays.
- Routage : contrats `SMART` avec `primaryExchange` renseigné (`SBF`, `AEB`, `IBIS`, `LSE`, `EBS`) ; la colonne `exchange` du CSV porte la place primaire.
- LSE : conversion pence → livres via le `priceMagnifier` du contrat (prix, valorisation, prix limites et stops).
- `hedge.csv` hors univers achetable en phase 1 : nouvelle clé `enable_hedge = false` dans `config/risk.toml` ; `list_universe` ne l'expose pas ; la gate rejette tout ordre sur un ticker de `hedge.csv` tant que `enable_hedge` est faux. Le fichier reste lu pour le prix d'IWDA (T7).
- Vérifier avec `scripts/check_connection.py` que chaque exchange IBKR européen résout correctement (contrat qualifié) sur un échantillon.
- Tests : parsing CSV, rejet d'une ligne sans secteur, conversion d'un titre GBX (obligatoire), rejet d'un ordre sur un ticker de `hedge.csv`.
- État au 12/10/2026 : code et tests mergés ; `us_core.csv` / `eu_core.csv` vides (fail-closed) jusqu'au premier `refresh_universe.py` et `check_connection.py --sample`, qui exigent IB Gateway (configuré en T10). Filtre capitalisation ≥ 2 Mds : aucune source gratuite accessible, signalé `cap unverified` dans `source` — décision humaine attendue.

## T2b — Classement momentum (calculé par le code)
- Cache SQLite des barres quotidiennes (OHLCV) issues de l'historique IBKR, rafraîchi le week-end par lots pour respecter les limites de requêtes IBKR.
- Score : rendement 6 mois hors dernier mois, filtre de volatilité par ATR14/prix.
- Claude reçoit les 40 premiers avec leurs scores ; il ne recalcule rien. Le critère « qualité » attend les fondamentaux de la phase 2.
- Le même cache sert à l'ATR14 et au plus haut de clôture des stops (T5).
- Tests : score sur série synthétique, exclusion par le filtre de volatilité, cache incomplet signalé (jamais inventé).
- État au 12/10/2026 : `guardrail_trader/bars.py` (cache `data/bars.sqlite`), `guardrail_trader/momentum.py`, `scripts/refresh_bars.py` (crontab samedi + dimanche 05:00 UTC), section `[momentum]` de `risk.toml`. Le classement est transmis dans le message de tâche de Claude. Cache vide tant que le gateway n'est pas configuré (T10).

## T3 — Nouvelles limites dans la gate
- `risk/config.py` : clés `min_position_pct`, `max_sector_pct`, `max_us_pct`, `max_equity_pct`, `reentry_cooldown_days`, validées.
- `risk/gate.py` → `_check()` : taille minimale, secteur, part US, part actions, cooldown après stop (lecture via `PortfolioState` enrichi : `sector_weights`, `us_weight`, `recently_stopped`).
- Mettre `max_position_pct = 2.0`, `max_trades_per_month = 60`, `max_price_deviation_pct = 0.5`, `max_commission_pct = 1.0`.
- Tests : un cas par règle, plus un cas « rafale d'ordres qui dépasse un secteur ensemble mais pas séparément ».
- État au 12/10/2026 : mergée (PR #5) après validation humaine. `max_drawdown_pct` reste à 25 jusqu'à T4. Le cooldown lit `closed_trades` (motif `stop`), table créée en T5 ; d'ici là, aucun titre n'est en cooldown.

## T4 — Paliers de drawdown et perte journalière
- `risk/config.py` : `drawdown_pause_pct`, `drawdown_reduce_pct`, `daily_loss_halt_pct`, avec contrainte `pause < reduce < max_drawdown`.
- `risk/gate.py` : états `PAUSED` (aucun BUY pendant N jours ouvrés), `REDUCED` (passif : les BUY sont bloqués tant que l'exposition actions dépasse 50 % ; aucune vente forcée en dehors des stops), halt journalier (aucun ordre le lendemain d'une perte > 3 %). Le kill switch −15 % d'origine reste tel quel et reste le seul à vendre activement.
- Reprise : après les 5 jours de pause (−8 %) ou une sortie du palier −12 %, les entrées reprennent à demi-taille (`max_position_pct / 2`) tant que le drawdown reste au-delà de −4 % ; taille normale quand il repasse sous −4 % (seuil dans `config/risk.toml`, validé `< drawdown_pause_pct`).
- Snapshot NAV quotidien (référence de la perte journalière) créé dès T4 ; T7 y ajoute le benchmark.
- `journal.py` : table `state` pour mémoriser le palier courant et sa date ; `run_bot.py` step 3 étendu.
- Tests : transitions entre paliers, sortie de pause après N jours, demi-taille puis taille normale, kill switch inchangé.
- État au 12/10/2026 : mergée (PR #7) après validation humaine. Tables `nav_daily` et `tier_log`, palier dans `state` ; transitions dans `risk/gate.py` → `next_tier()`.

## T5 — Stops de protection chez IBKR
- `broker/ibkr.py` : `place_stop(contract, qty, stop_price)` (ordre STP GTC), `modify_stop(order_id, new_price)`, `cancel_stop`, `open_stop_orders()`. Identifiant stocké : `permId`. Ordres ouverts lus par `reqAllOpenOrders`. Prix de stop arrondis au pas de cotation du contrat.
- `market.py` : ATR14 à partir de `daily_history` (nécessite high/low : passer de closes seuls à OHLC).
- `executor.py` : après chaque fill BUY, placer le stop sur la quantité exécutée. Avant toute vente volontaire ou liquidation du kill switch : annuler ou réduire le stop de la ligne, puis vendre ; replacer un stop sur le reliquat si la vente est partielle. Table `stops` dans `journal.py` (position, permId, order_id, prix, historique des relèvements), séparée de `orders` : les ordres stop ne comptent pas dans `max_trades_per_month`.
- Table `closed_trades` créée ici en version minimale (entrée, sortie, motif, quantité, P&L net) ; enrichie en T6.
- `run_bot.py` nouveau step « stops » **avant la réconciliation des positions** : détecter et booker les stops exécutés depuis le dernier run (exécutions IBKR, commission comprise) → ledger + `closed_trades` + cooldown ; puis réconcilier ; puis relever les stops suiveurs (jamais abaisser) avant l'appel LLM.
- Réconciliation (step 1) : les ordres ouverts connus dans `stops` sont tolérés ; tout autre ordre ouvert = arrêt.
- Tests : calcul de stop borné [−12 %, −6 %], relèvement monotone, réconciliation avec stops, stop exécuté entre deux runs, aucune vente sans annulation/réduction préalable du stop (pas de position courte), stops hors quota mensuel.

## T6 — Journal enrichi et closed_trades
- `agent.py` : `_ORDER` exige `poche`, `these`, `catalyseur`, `fiabilite`, `horizon_jours`, `stop_pct`, `objectif_pct`, `invalidation`, `confiance`, `sources`. `SYSTEM_PROMPT` réécrit pour l'exiger et expliquer la calibration. Validation stricte dans `_dispatch` (ordre sans champ = rejeté avec raison).
- `journal.py` : colonnes ajoutées à `proposals` (migration SQLite idempotente), table `closed_trades` enrichie (poche, confiance, durée), remplie par T5 et par les SELL volontaires (motif).
- Réussite d'une décision fermée = P&L net > 0 après commissions et slippage forfaitaire (0,05 %). Définition unique pour le taux de réussite et la calibration. À titre secondaire, P&L relatif à IWDA sur la même période (renseigné dès que T7 fournit le benchmark).
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
