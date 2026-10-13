# Portefeuille paper IBKR — cahier des charges v1.1 (05/10/2026)

Fork de **guardrail-trader** (MIT). Ce fichier remplace le `CLAUDE.md` d'origine et vaut constitution du test. Les règles ci-dessous sont **pré-enregistrées** : tu (Claude Code) ne les modifies jamais toi-même. Toute modification passe par l'humain, datée dans `CHANGELOG.md`. Protocole d'évaluation sur compte de simulation, pas un conseil d'investissement.

## 0. Principes hérités du projet (inchangés)

- **Claude propose, le code décide.** Toute limite vit dans `config/risk.toml` et `guardrail_trader/risk/gate.py`, jamais seulement dans un prompt. Ne jamais assouplir une limite ni relever un plafond LLM sans instruction écrite de l'humain.
- Paper uniquement. `TRADING_MODE=paper`, port paper, compte `DU…`. Le code refuse toute incohérence (`broker/ibkr.py` → `check_endpoint`, `check_account`). Aucun identifiant réel ne doit exister sur cette machine.
- Secrets dans `.env`, `docker/gateway.env`, `docker/secrets/` (gitignorés). Jamais commités, jamais loggés.
- Chaque règle de la gate a un test ; `pytest -q` vert avant tout commit. Travail sur branche, PR, jamais de push direct sur `main`.
- Venv du projet (`.venv/bin/python`) pour tout.

## 1. Objectif et cadre

- Mesurer si des décisions fondées sur de l'information publique et un raisonnement systématique battent un ETF monde, sur 12 mois, avec assez de décisions pour être interprétable.
- **Capital virtuel** : 950 000 EUR (`[budget] capital`, `currency = "EUR"`) — amendé le 12/10/2026 puis le 13/10/2026 (compte paper en EUR, ~1 003 000 EUR, voir `CHANGELOG.md`). Le bot ne voit que son grand livre ; `journal_cli.py init` refuse de démarrer si la valeur liquidative du compte paper en EUR est inférieure au capital, et chaque run (après le kill switch) si elle est inférieure à la valeur virtuelle courante du bot (correction du 12/10/2026).
- **Durée** : 12 mois à partir du premier ordre réel (fin de la semaine à blanc). Points d'étape M+3 et M+6 (§9).
- **Benchmark** : IWDA (iShares Core MSCI World UCITS, AEB, EUR), 100 % investi au jour 1, calculé en parallèle dans la table `benchmark`.

## 2. Phases

- **Phase 1 (démarrage)** : poche A systématique seule + trésorerie. C'est ce que le harness sait faire une fois adapté.
- **Phase 2** : ajout de la poche B événementielle quand les outils `get_news` / `get_fundamentals` / `get_catalysts` existent et sont testés. Décision humaine, datée dans `CHANGELOG.md`. Pas avant M+1.

## 3. Univers (`config/universe/*.csv`)

- `us_core.csv` : constituants S&P 500 + Nasdaq-100, capi ≥ 2 Mds USD, volume moyen 20 j ≥ 5 M USD. Exchange `SMART`, devise `USD`.
- `eu_core.csv` : STOXX 600, capi ≥ 2 Mds EUR, volume ≥ 5 M EUR. Exchanges `SBF` (Paris), `AEB` (Amsterdam), `IBIS` (Xetra), `LSE` (GBP), `EBS` (SIX, CHF).
- `hedge.csv` : IWDA, CSPX, EXSA, un ETF obligataire court terme EUR. Réservés à la poche C.
- Colonnes : `symbol,exchange,currency,name,sector,country,source`.
- Rafraîchi mensuellement par `scripts/refresh_universe.py` (à étendre). Fichiers vides = rien n'est achetable (fail-closed, comportement d'origine).
- Exclus : options, futures, CFD, crypto, levier, short, small caps, titres cotés depuis moins de 6 mois.

## 4. Limites (`config/risk.toml`)

| Clé | Valeur | Note |
|---|---|---|
| `capital` / `currency` | 950 000 / EUR | amendé le 13/10/2026 (ex-800 000, ex-1 000 000) |
| `max_position_pct` | 2.0 | par ligne, à l'entrée |
| `min_position_pct` | 0.5 | nouvelle clé |
| `max_sector_pct` | 20.0 | nouvelle clé, secteur via colonne `sector` |
| `max_us_pct` | 70.0 | nouvelle clé |
| `max_equity_pct` | 90.0 | nouvelle clé ; trésorerie ≥ 10 % |
| `max_trades_per_month` | 60 | |
| `max_price_deviation_pct` | 0.5 | ordres limites |
| `max_commission_pct` | 1.0 | |
| `drawdown_pause_pct` | 8.0 | nouvelle clé : plus d'entrées 5 jours ouvrés |
| `drawdown_reduce_pct` | 12.0 | nouvelle clé : exposition actions ≤ 50 %, poche B gelée |
| `max_drawdown_pct` | 15.0 | kill switch d'origine : tout vendre, halt, reset manuel |
| `daily_loss_halt_pct` | 3.0 | nouvelle clé : aucun ordre le lendemain sauf stops |
| `stop_atr_mult` | 2.0 | nouvelle clé ; plancher −6 %, plafond −12 % |
| `trail_atr_mult` | 2.0 | nouvelle clé |
| `reentry_cooldown_days` | 10 | nouvelle clé, jours ouvrés après un stop |
| `allow_fractional` | false | |

Toutes ces clés sont validées fail-closed par `risk/config.py` → `load_risk_config()`.

## 5. Stops (nouveau, obligatoire avant tout ordre réel)

- À chaque achat exécuté, l'exécuteur place chez IBKR un ordre **STP** GTC à `entry − stop_atr_mult × ATR14`, borné à [−12 %, −6 %]. L'identifiant de l'ordre stop est stocké dans la table `stops`.
- À chaque run, pour chaque position : si le plus haut de clôture depuis l'entrée ≥ entrée + 1 × ATR14, le stop est remonté au prix d'entrée ; ensuite suivi à `trail_atr_mult × ATR14` sous le plus haut de clôture. Jamais abaissé.
- Un stop touché = décision fermée ; réentrée interdite sur le titre pendant `reentry_cooldown_days` (vérifié par la gate).
- La réconciliation d'origine (journal = positions IBKR, aucun ordre ouvert étranger) doit tolérer les ordres stop qu'elle connaît et refuser tout autre ordre ouvert.

## 6. Cycle de décision

Modèle du projet conservé (créneaux pendant la séance, prix frais, exécution immédiate), étendu à l'Europe :

| Créneau | Heure | Marchés | Contenu |
|---|---|---|---|
| `eu_open` | 10:00–11:00 Paris | Europe | stops, réconciliation, décisions poche A sur titres EU |
| `us_open` | 10:30–11:30 New York | US | idem titres US |
| `us_close` | 15:00–15:40 New York | US + EU (lecture) | stops, revue des positions, snapshot NAV, benchmark, résumé/alerte du jour |

- Lundi : rebalancement de la poche A (momentum 6 mois + qualité) aux créneaux `eu_open` et `us_open` ; les autres jours, ajustements et gestion des stops seulement.
- Ordres limites uniquement, DAY, ±0,5 % du prix de référence. Pas de market orders (données différées 15 min).
- « Aucune action » est une décision journalisée.
- Cible : 250 à 400 décisions fermées en 12 mois.

## 7. Ce que Claude voit (outils, `agent.py`)

Phase 1 : outils d'origine (`get_portfolio`, `list_universe`, `get_price_history`, `check_orders`, `submit_orders`) + `list_universe` enrichi (nom, secteur, pays). `ENABLE_WEB_SEARCH=1`, max 5 recherches/run, pour vérifier une actualité avant un ordre.
Phase 2 : `get_news(symbol)`, `get_fundamentals(symbol)`, `get_catalysts(days)`.

Le prompt système (`SYSTEM_PROMPT`) impose pour chaque ordre les champs du §8.1 ; `submit_orders` les valide (schéma JSON strict, fail-closed).

## 8. Journal et métriques

### 8.1 Proposals enrichies (table `proposals`)
Colonnes ajoutées : `poche` (A|B|C), `these` (2–4 phrases), `catalyseur`, `fiabilite` (mecanique|signal|signal_sectoriel|a_confirmer), `horizon_jours`, `stop_pct`, `objectif_pct`, `invalidation`, `confiance` (0–1), `sources` (JSON).
Le champ `confiance` sert à mesurer la **calibration** : les décisions notées 0,6 doivent réussir ~60 % du temps.

### 8.2 Tables nouvelles
- `stops` : ordre stop par position, historique des relèvements.
- `benchmark` : NAV quotidienne d'IWDA 100 % depuis le jour 1.
- `closed_trades` : une ligne par décision fermée (entrée, sortie, motif : stop|objectif|horizon|rebalancement|kill, P&L net, durée, poche, confiance).

### 8.3 Rapport hebdomadaire (`reports/YYYY-WW.md`, vendredi après `us_close`)
NAV, rendement brut/net vs IWDA, alpha, beta, Sharpe, Sortino, drawdown max et courant, nombre de décisions fermées, taux de réussite, gain moyen/perte moyenne, profit factor, calibration par tranche de confiance, turnover et coûts (commissions IBKR + slippage forfaitaire 0,05 % si fill au prix limite exact), expositions (actions, US, secteurs, devises), 3 meilleures / 3 pires décisions avec relecture de thèse.

### 8.4 Hygiène
- Pas de modification de règle hors `CHANGELOG.md`.
- Donnée manquante = signalée, jamais inventée ; run impossible = journalisé « non exécuté » avec raison.
- Alerte (ntfy ou Telegram) à chaque run : NAV, écart vs benchmark, décisions, anomalies. Alerte immédiate sur kill switch, échec de réconciliation, scheduler silencieux > 2 h en séance.

## 9. Critères de verdict (pré-enregistrés)

Toujours relatif à IWDA sur la même période.

- **M+3** (~60 décisions fermées) : arrêt si drawdown > 12 %, ou alpha < −6 % avec taux de réussite < 45 %.
- **M+6** (~150) : arrêt si alpha < −4 % et Sharpe < benchmark. Fermeture d'une poche si elle sous-performe de > 5 % avec profit factor < 1.
- **M+12** (~300) : *Négatif* si alpha ≤ 0 ou Sharpe ≤ benchmark. *Indistinguable* si alpha entre 0 et +3 % (résultat le plus probable). *Prometteur, à confirmer* si alpha > +3 %, Sharpe > benchmark + 0,3, profit factor > 1,3, calibration < 10 points d'écart, drawdown max < 12 % → extension à 24 mois.
- Sous 200 décisions fermées : aucun verdict, quel que soit le rendement.

## 10. Exploitation

- Hébergement : VPS Linux, mode Docker du projet (`ib-gateway` + `bot` + `dashboard`), fuseau du gateway `Europe/Paris`, redémarrage quotidien à 04:00 Paris (hors séances).
- Budget LLM : `LLM_RUN_BUDGET_USD=1.50`, `LLM_MONTHLY_BUDGET_USD=120`. Modèle par défaut `claude-sonnet-5` ; changement de modèle = amendement daté.
- Semaine à blanc (`--dry-run`) obligatoire avant le premier ordre réel ; pendant cette semaine, deux runs de décision le même créneau pour mesurer le taux de désaccord (cohérence).
- Audit : tous les 15 jours, l'humain apporte `reports/` et un export de `proposals`/`closed_trades` à Claude (chat). Trader et auditeur sont séparés par construction.
