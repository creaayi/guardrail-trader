# Plan d'adaptation — T0 (état des lieux)

Date : 12/10/2026. Base : `main` @ `2acefed`. Référence : `CLAUDE.md` v1.1, `TASKS.md`.
Aucun fichier de code n'a été modifié pour produire ce document.

## 0. Constat T0

> **Mise à jour 12/10/2026** : plan validé avec décisions (§5). Environnement installé (`uv` 0.10.12,
> `gh` 2.46.0) ; `uv lock --check` OK, `uv sync --frozen --extra dev`, `uv pip check` OK ;
> `pytest -q` → **179 passés, 1 xfail** (`test_one_off_downward_bad_tick_does_not_liquidate`, lacune
> connue marquée `strict=True` par le projet d'origine). Les « 110 tests » cités plus bas sont les
> fonctions `test_*` de `tests/` ; pytest en collecte 180 avec la paramétrisation et `evals/harness/`.

### 0.1 `pytest -q` : NON EXÉCUTÉ (bloquant environnement) — résolu, voir ci-dessus

Le VPS n'a ni `.venv`, ni `uv`, ni `pip`, ni le module `venv` (`ensurepip` absent :
paquet Debian `python3.14-venv` manquant). Python système : 3.14.4. La CI (`.github/workflows/tests.yml`)
utilise `uv 0.10.12` + Python 3.13 ; je n'ai pas vérifié son statut sur `main`, et **les tests
n'ont pas tourné localement**. Il faut choisir une des deux voies (décision humaine, modifie la machine) :

- `sudo apt install python3.14-venv` puis `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` ;
- ou installer `uv` (binaire officiel astral) puis `uv sync --frozen --extra dev` — cohérent avec
  la CI et avec la consigne `uv lock` de `TASKS.md`. **Recommandé.**

Inventaire : 110 fonctions `test_*` dans `tests/`, plus `evals/harness/` (scénarios sur `evals/sim.py`).

### 0.2 Autres constats d'exploitation

- `gh` absent : les PR devront être ouvertes à la main sur `github.com/creaayi/guardrail-trader`
  (ou installer `gh`).
- `CHANGELOG.md` et `reports/` n'existent pas ; à créer en T1 (le premier amendement daté est le
  passage AUD → EUR et le changement des limites).
- Le projet d'origine tourne sur **macOS** : `scripts/launchd.sh`, `scripts/keep_awake.py`
  (caffeinate) et le lancement de `gateway_watchdog.py` par launchd n'ont pas d'équivalent Linux.
  Sur le VPS il faudra un timer systemd ou une crontab hôte pour le watchdog (T9).
- Fuseau Sydney codé en dur à 7 endroits (voir annexe A).

## 1. Architecture actuelle (résumé utile pour le plan)

```
scheduled_run.py (toutes les 15 min, créneaux en heure NY "open"/"close")
  └─ run_bot.py → pipeline.run_once()
       0 ensure_ready → 1 réconciliation (positions IBKR == ledger ; AUCUN ordre ouvert)
       2 prix + FX (tout l'univers) → 3 kill switch → contrôle marché ouvert (1er titre de l'univers)
       → plafond mensuel d'ordres → 4 Claude (agent.py) → 5 gate (evaluate) → 6 executor
```

- La **gate** (`risk/gate.py`) est pure (pas d'horloge, pas de réseau) : tout état temporel doit
  lui arriver via `PortfolioState`. À préserver.
- Le **journal** est le seul grand livre (`ledger`) ; `state` est une table clé/valeur
  (pic, halt) — elle existe déjà, T4 peut s'y appuyer sans nouvelle table.
- `orders_this_month()` compte **toutes** les lignes de `orders` : tout ce qui y est écrit consomme
  le quota mensuel.
- Clé d'instrument = `SYMBOL:DEVISE`. Deux titres de même symbole et même devise sur deux places
  différentes font échouer le chargement (fail-closed, voulu).

## 2. Plan par tâche

Légende risque : **[C]** critique (perte d'argent virtuel, ordre non voulu, réconciliation cassée),
**[M]** moyen (run bloqué, données fausses), **[F]** faible.

### T1 — Devise EUR et capital

Fichiers : `config/risk.toml`, `guardrail_trader/agent.py` (`_ORDER.currency`, texte « US$1 » du
prompt), `guardrail_trader/journal.py` (`TZ`, docstring « A$100 »), `scripts/journal_cli.py`,
`scripts/run_bot.py` (horodatage des logs), `scripts/scheduled_run.py`, `guardrail_trader/pipeline.py`
(`fx_rates`), `guardrail_trader/dashboard_data.py` (`next_slot_sydney`), `scripts/gateway_watchdog.py`
(`TZ`), `CHANGELOG.md` (nouveau). Tests : `tests/test_journal.py`, `test_agent.py`, `test_risk_gate.py`,
`test_dashboard.py`, `test_gateway_watchdog.py`, `evals/sim.py` (tous en AUD/Sydney aujourd'hui).

Constat : `IBKRBroker.fx_to_base()` gère déjà n'importe quelle paire (essaie `EURUSD` inversé puis
`USDEUR`) ; `EURUSD`, `EURGBP`, `EURCHF` existent sur IDEALPRO. Le travail est surtout du test.

Risques :
- **[C] Capital réel du compte paper.** Un compte paper IBKR est souvent crédité de 1 M USD
  (≈ 0,86 M EUR) : si la NetLiquidation est < 1 M EUR, le bot peut vouloir acheter plus que ce que
  le compte détient. À vérifier avec `check_connection.py` **avant** `journal_cli.py init` ;
  proposer un garde-fou : refus du run si `net_liquidation (en EUR) < capital`.
- **[M] Pence LSE.** IBKR cote beaucoup de titres LSE en GBp (pence) avec `priceMagnifier = 100`
  dans `ContractDetails`. Sans correction, la valeur en EUR d'une ligne LSE est fausse d'un facteur
  100. À traiter en T1 (conversion) ou T2 (vérification), avec un test.
- **[M]** `executor.py` et `pipeline.commission_base` utilisent `fx.get(comm_ccy, …)` avec un
  repli implicite ; `fx` doit toujours contenir la devise de base (`EUR: 1.0`), sinon une commission
  en EUR sur un titre USD serait mal convertie.
- **[M] Changement de fuseau du journal.** Le comptage mensuel repose sur `substr(ts,1,7)` des
  horodatages ISO locaux. Passer à Europe/Paris est sûr **uniquement sur un journal neuf** ; le
  `data/journal.sqlite` éventuel d'origine ne doit pas être réutilisé. `journal_cli.py init` refuse
  déjà un second dépôt — à documenter.
- **[F]** Le « relèvement » `capital` 10 000 → 1 000 000 passe par `init` sur journal neuf, pas par
  `topup` (sinon AUD/EUR incohérents : `topup_to` lève déjà une erreur, bien).

### T2 — Univers US + Europe

Fichiers : `guardrail_trader/risk/config.py` (`Instrument` + `name, sector, country`,
`_read_universe_csv`), `config/universe/{us_core,eu_core,hedge}.csv` (nouveaux),
`config/risk.toml` (`[universe] files`), `scripts/refresh_universe.py` (réécriture), `agent.py`
(`_universe` : nom/secteur/pays), `scripts/run_bot.py` (lit `my_picks.csv` en dur),
`scripts/check_connection.py` (mode échantillon multi-places), `pyproject.toml`/`uv.lock` si
dépendances. Tests : `test_risk_gate.py` (parsing), nouveau `tests/test_universe.py`.

Risques :
- **[C] `hedge.csv` en phase 1.** `CLAUDE.md` le réserve à la poche C, qui n'existe pas en phase 1
  (« poche A + trésorerie »). Le charger dans `[universe] files` le rendrait achetable par Claude.
  Proposition : ne pas le lister dans `files` en phase 1 ; l'utiliser seulement pour le prix d'IWDA
  (T7). **À valider.**
- **[M] Sources et filtres.** Constituants publics exploitables : fichiers de positions iShares
  (IVV/CSPX pour le S&P 500, EXSA pour le STOXX 600 — ils donnent secteur, pays, place), Nasdaq-100
  (QQQ). Le volume moyen 20 j n'y figure pas : il faut une autre source ou les barres IBKR (pacing,
  voir T5). Les titres « cotés depuis < 6 mois » demandent une date d'introduction : source à choisir.
- **[M] Correspondance des symboles.** Les tickers iShares/Bloomberg ≠ symboles IBKR (`BRK.B` →
  `BRK B`, Nestlé `NESN`, suffixes LSE `.A`, doubles cotations). Chaque ligne doit être qualifiée
  chez IBKR ; prévoir un fichier de correspondance manuel et rejeter les non-qualifiés.
- **[M] Routage.** `CLAUDE.md` impose `SBF/AEB/IBIS/LSE/EBS` comme `exchange`. Utilisé tel quel dans
  `Stock(sym, "IBIS", "EUR")`, cela donne des ordres **dirigés** (commissions plus élevées, possibles
  avertissements API). Alternative : `exchange=SMART` + `primaryExchange=IBIS`. Le CSV peut garder la
  place primaire ; c'est `broker.stock()` qui choisit. **Décision humaine (ne change pas le cahier).**
- **[M] Collisions de clé** `SYMBOL:EUR` entre Paris/Amsterdam/Xetra : le chargement échoue
  (fail-closed). Mieux vaut le détecter au rafraîchissement qu'au run.
- **[M] Taille de l'univers** (~900 titres) : `price_all()` interroge tout l'univers à chaque run
  (lots de 50, 8 s d'attente) → plusieurs minutes et proches des limites de lignes de données.
  T8 doit restreindre aux marchés du créneau + positions.
- **[F]** `my_picks.csv` contient TMC (small cap, exclue par §3) : retirer le fichier de l'univers
  et rendre la lecture dans `run_bot.py` facultative.

### T3 — Nouvelles limites dans la gate

Fichiers : `risk/config.py` (`RiskConfig` + 5 clés validées), `risk/gate.py` (`PortfolioState` :
`sector_weights`, `us_weight`, `recently_stopped` ; `_check`, `_apply`), `journal.py`
(`portfolio_state` alimente ces champs), `config/risk.toml`, `agent.py` (prompt : nouvelles limites),
`evals/sim.py`. Tests : `test_risk_gate.py` (un cas par règle + rafale sectorielle).

Risques :
- **[C] Sens des changements de limites.** Resserrements : `max_position_pct` 25→2,
  `max_price_deviation_pct` 2→0,5, `max_commission_pct` 10→1, `max_drawdown_pct` 25→15.
  **Assouplissement : `max_trades_per_month` 10→60** — autorisé par écrit (§4 + T3), à dater
  explicitement dans `CHANGELOG.md`.
- **[M] Signature de `RiskConfig`.** `tests/test_agent.py` et `evals/sim.py` la construisent
  **positionnellement** ; ajouter des champs casse ces appels. Mettre les nouveaux champs en fin avec
  valeurs par défaut… sauf que les défauts ne doivent jamais être plus laxistes que `risk.toml` —
  préférer mettre à jour les appels en mots-clés.
- **[M] Simulation de lot.** Secteur, part US et part actions doivent être recalculés dans `_apply`
  (le simulé), sinon la « rafale qui dépasse ensemble » passe. `us_weight` : par la colonne `country`,
  pas par la devise (CSPX est en USD sur LSE).
- **[M] Interaction `min_position_pct` × SELL.** Une vente partielle laissant une ligne < 0,5 % :
  bloquer, ou n'appliquer le minimum qu'aux BUY ? `CLAUDE.md` ne tranche pas. **Proposition :
  minimum appliqué au BUY (taille d'entrée) et à la ligne résiduelle après SELL partiel ; SELL total
  toujours permis.** À valider.
- **[F]** `recently_stopped` n'aura de données qu'après T5 : en T3, requête journal vide + test sur
  état synthétique. La gate reste pure : le journal calcule la fenêtre de 10 jours ouvrés.

### T4 — Paliers de drawdown et perte journalière

Fichiers : `risk/config.py` (3 clés + contrainte `pause < reduce < max_drawdown`), `risk/gate.py`
(états, champs `PortfolioState`), `journal.py` (palier dans la table `state` existante, NAV de
clôture veille), `pipeline.py` (étape 3), `agent.py` (prompt : état courant), `evals/sim.py`.
Tests : `test_risk_gate.py`, `test_journal.py`, scénarios `evals/harness`.

Risques :
- **[M] Ambiguïté REDUCED.** `CLAUDE.md` : « exposition actions ≤ 50 % » ; `TASKS.md` : « BUY
  bloqués tant que > 50 % ». La seconde ne réduit rien activement. **Question : la réduction
  active (ventes forcées par le code, comme le kill switch) est-elle voulue ?** Par défaut
  j'implémente la version `TASKS.md` (plus restrictive sur les entrées, sans vente forcée).
- **[M] Sortie de palier.** Non spécifiée : PAUSED sort après 5 jours ouvrés, mais si le drawdown
  est toujours ≥ 8 % ? REDUCED sort quand ? Proposition : PAUSED se ré-arme tant que DD ≥ 8 % ;
  REDUCED tient tant que DD ≥ 12 %. **À valider.**
- **[M] « Jours ouvrés »** : calendrier US ou EU ? Proposer : jours où au moins un des marchés de
  l'univers est ouvert (lundi–vendredi hors jours fériés communs).
- **[M] Perte journalière** : référence = NAV du snapshot `us_close` de la veille (T7). Avant T7,
  il manque cette donnée → T4 doit soit créer le snapshot quotidien, soit dépendre de T7.
  **Suggestion d'ordre : faire le snapshot NAV de clôture dès T4.**
- **[F]** Le pic confirmé sur deux runs (`observe_value`) s'applique aussi aux paliers : bien.

### T5 — Stops de protection chez IBKR (tâche la plus risquée)

Fichiers : `broker/ibkr.py` (`place_stop`, `modify_stop`, `cancel_stop`, `open_stop_orders`,
exécutions depuis une date), `market.py` (`daily_history` → OHLC, `atr14`), `executor.py`,
`journal.py` (tables `stops`, `closed_trades` minimale), `pipeline.py` (étape stops + réconciliation),
`risk/gate.py` (`liquidation_proposals`), `evals/sim.py` (broker simulé : ordres STP persistants).
Tests : `test_broker_safety.py`, `test_journal.py`, scénarios `evals/harness`.

Risques :
- **[C] Vente à découvert involontaire.** Si un SELL (Claude, rebalancement ou kill switch) est
  exécuté alors que le STP GTC de la même ligne est toujours actif, le stop peut ensuite vendre des
  titres qu'on n'a plus → position courte. Règle : **annuler (ou réduire) le stop avant d'envoyer
  le SELL**, puis replacer un stop sur le reliquat si le SELL n'est que partiellement exécuté.
  Idem dans `liquidate()` du kill switch. Test obligatoire.
- **[C] Fills de stop entre deux runs.** Un stop exécuté la nuit fait diverger positions IBKR et
  ledger → la réconciliation actuelle (étape 1) s'arrête « needs a human ». L'étape « stops » doit
  donc **précéder** la comparaison des positions : récupérer les exécutions (`reqExecutions`) des
  stops connus, les booker (`record_fill`, commission comprise), puis réconcilier. `TASKS.md` place
  l'étape « avant l'appel LLM » : je propose « avant la réconciliation des positions », la vérification
  des ordres ouverts restant stricte.
- **[C] Identité des ordres.** `orderId` dépend du `clientId` et de la session ; `permId` est
  l'identifiant stable chez IBKR. Stocker `permId` (et `orderId`) dans `stops` ; réconcilier par
  `permId`. De plus `ib.openTrades()` ne voit que les ordres du `clientId` courant : utiliser
  `reqAllOpenOrders()` pour que « aucun ordre étranger » soit vraiment vérifié.
- **[C] Quota mensuel.** Les ordres stop ne doivent pas être écrits dans `orders` (sinon ils
  consomment les 60 ordres/mois) et ne doivent jamais être bloqués par la gate.
- **[M] Pas de cotation.** IBKR rejette un `auxPrice` hors pas de cotation (MiFID II par tranche de
  prix en Europe, 0,01 USD aux US). Arrondir via `ContractDetails.minTick`/`marketRuleIds`. Même
  problème latent pour les prix limites (`round(…, 2)`) et la liquidation.
- **[M] Fill partiel du BUY** : le stop porte sur la quantité réellement exécutée, et un fill
  tardif après annulation est déjà signalé par la réconciliation suivante.
- **[M] Pacing IBKR.** ATR14 + plus haut de clôture pour ~45 positions (90 % / 2 %) = ~45 requêtes
  historiques par run ; IBKR limite à ~60 requêtes / 10 min. Proposition : table de barres
  quotidiennes en cache SQLite, mise à jour une fois par jour.
- **[M] Opérations sur titres** (split, regroupement) : quantités IBKR changent → réconciliation
  échoue (comportement fail-closed acceptable, mais à documenter) et le stop porte sur une mauvaise
  quantité.
- **[F]** Dépendance inversée avec T6 : `closed_trades` est « créée en T6, remplie par T5 ».
  Proposition : créer la table en T5 (colonnes minimales), l'enrichir en T6.

### T6 — Journal enrichi et `closed_trades`

Fichiers : `agent.py` (`_ORDER`, `SYSTEM_PROMPT`, `_to_proposals`, validation dans `_dispatch`),
`risk/gate.py` (`Proposal` porte les nouveaux champs ou un `meta` dict), `journal.py` (migration
`ALTER TABLE proposals ADD COLUMN …` idempotente via `PRAGMA table_info`, `closed_trades`),
`dashboard_data.py`, `web/dashboard.html`, `fake_llm.py` (doit produire des ordres complets),
`evals/model/run_model_evals.py`. Tests : `test_agent.py`, `test_journal.py`, `test_dashboard.py`.

Risques :
- **[M] Validation stricte vs boucle bornée.** Un ordre incomplet rejeté doit consommer la révision
  unique (`MAX_SUBMIT_REVISIONS = 1`) comme un blocage de gate, sinon Claude peut boucler ;
  l'outil d'API ne valide pas le JSON Schema côté serveur : la validation en Python est la seule
  barrière.
- **[M] `poche` ≠ A en phase 1** : à rejeter dans la validation (B et C non actives).
- **[M] Coût LLM.** Thèse + invalidation + sources pour chaque ordre augmentent nettement les
  tokens de sortie ; avec 1,50 USD/run et 120 USD/mois (≈ 3 créneaux × 21 j = 63 runs → 1,90 USD
  moyen max), la marge est faible. À mesurer en T10.
- **[M] Calibration** : `confiance` doit être liée à une définition de « réussite » (P&L net > 0 ?
  objectif atteint ?). Non définie dans `CLAUDE.md` §8.1. **À préciser avant T9.**
- **[F]** Motif `closed_trades` pour un SELL volontaire : rebalancement vs horizon vs objectif —
  fourni par Claude ou déduit ? Proposition : champ `motif_sortie` obligatoire sur les SELL.

### T7 — Benchmark IWDA

Fichiers : `journal.py` (table `benchmark`, parts virtuelles), `pipeline.py` / `run_bot.py`
(snapshot `us_close`), `scripts/journal_cli.py init` (prix IWDA requis), nouveau module de métriques
(alpha/beta) partagé avec T9. Tests : série synthétique.

Risques :
- **[M] Ordre d'initialisation** : `init` exige un prix IWDA, donc une connexion IBKR, alors
  qu'aujourd'hui `init` est hors ligne. Alternative : initialiser le benchmark au premier snapshot
  du jour 0 (fin de la semaine à blanc) — conforme à « 100 % investi au jour 1 ». **Préférable.**
- **[F]** IWDA (AEB) se clôture à 17:30 Paris ; au créneau `us_close` (21:00–21:40 Paris) on lit
  la clôture du jour : OK. Jours fériés AEB vs NYSE : reprendre la dernière valeur connue.
- **[F]** Alpha/beta sur 12 mois de données quotidiennes : convention (rendements quotidiens,
  taux sans risque €STR ou 0 ?) à fixer une fois — **à valider**, sinon 0.

### T8 — Créneaux Europe

Fichiers : `scripts/scheduled_run.py` (`SLOTS` avec fuseau par créneau, clé d'état), `run_bot.py`
(`--slot`), `pipeline.py` (`market_open` par exchange, filtrage de l'univers, `usd_open` →
ouverture par place, flag `rebalance`), `agent.py` (prompt + tâche), `market.py`,
`docker-compose.yml` (`TIME_ZONE`, `AUTO_RESTART_TIME`, commentaire healthcheck),
`docker/crontab` (commentaire), `dashboard_data.py` (prochains créneaux), `scripts/keep_awake.py`
(obsolète sur Linux). Tests : `test_scheduled_run.py`, `test_dashboard.py`.

Risques :
- **[C] Kill switch / stops hors créneau.** Aujourd'hui la liquidation n'a lieu que si « le »
  marché est ouvert. Avec deux continents, une liquidation doit vendre chaque ligne quand **sa**
  place est ouverte, et les lignes EU ne sont vendables qu'au créneau `eu_open` (le créneau
  `us_close` est 21:00 Paris, places EU fermées).
- **[M] Créneau `us_close` en « lecture » pour l'EU** : les prix EU y sont des clôtures, pas
  d'ordres EU — à faire respecter par code (filtrage), pas par le prompt.
- **[M] Décalage des heures d'été** : US et UE changent d'heure à des dates différentes (2 à 3
  semaines par an). Les créneaux définis chacun dans leur fuseau absorbent cela ; le heartbeat
  `last_tick_ny` reste valable.
- **[M] Redémarrage gateway 04:00 Paris** : hors séances, mais la réinitialisation serveur IBKR
  Europe a lieu tôt le matin ; le `ensure_ready` + 3 tentatives couvrent ce cas.
- **[F]** Jours fériés par place : `liquidHours` IBKR (`CLOSED`) les fournit déjà par contrat ;
  évaluer sur un titre représentatif **par exchange**, pas sur le premier de l'univers.

### T9 — Rapport hebdomadaire et alertes

Fichiers : `scripts/weekly_report.py` (nouveau), `guardrail_trader/alerts.py` (nouveau),
`pipeline.py` (appels d'alerte), `scheduled_run.py` (déclenchement vendredi), 
`scripts/gateway_watchdog.py` (heartbeat > 2 h, `TZ`), `.env.example` (topic ntfy / Telegram),
`docker-compose.yml` (egress), systemd/cron hôte pour le watchdog. Tests : rapport sur base
synthétique, alerte simulée (pas d'appel réseau en test).

Risques :
- **[M] Fuite de secrets** : topic ntfy et token Telegram dans `.env` seulement, jamais dans les
  logs ni dans le message d'erreur d'une requête échouée.
- **[M] Alerte qui bloque le run** : un échec d'envoi ne doit jamais interrompre ou retarder un
  run (timeout court, exception avalée et journalisée).
- **[M] Watchdog** : sur le VPS il tourne sur l'hôte (accès au socket Docker) ; « en séance »
  doit couvrir les deux plages EU et US.
- **[F]** Métriques (Sharpe, Sortino, profit factor, calibration) : peu de points au début →
  afficher « n/d » plutôt qu'une valeur trompeuse (§8.4 : donnée manquante signalée).

### T10 — Semaine à blanc

Fichiers : `.env` (hors git), `scripts/consistency.py` (nouveau), `reports/dry-run-week.md`,
`CHANGELOG.md`. Aucun changement de code de décision.

Risques :
- **[C] Deux runs simultanés.** Deux connexions avec le même `IBKR_CLIENT_ID` : IBKR coupe la
  première. Le second run doit être séquentiel (même créneau, après le premier, sous le verrou du
  scheduler) et/ou utiliser un autre `clientId` en lecture seule.
- **[M] Journal des runs à blanc** : `recent_activity` exclut déjà les modes `dry` ; vérifier que
  `closed_trades`, `benchmark` et le compteur mensuel les excluent aussi, et repartir d'un journal
  propre au jour 0.
- **[M] Stops non testables en `--dry-run`** (broker en lecture seule) : prévoir un test
  `check_connection.py --test-order` étendu à un STP sur le compte paper avant le jour 0.
- **[F]** `IBKR_MARKET_DATA_TYPE=3` : vérifier que les données différées EU sont bien servies
  gratuitement sur ce compte paper.

## 3. Points structurels hors T1–T10 (décision humaine)

1. **Stratégie de la poche A non outillée.** « Momentum 6 mois + qualité » sur ~900 titres ne peut
   pas reposer sur l'agent tel qu'il est : `MAX_HISTORY_CALLS = 15` par run et le pacing IBKR
   empêchent Claude de classer l'univers. Il faudrait un classement momentum **calculé par le code**
   (cache de barres quotidiennes) et fourni à Claude. Le critère « qualité » exige des
   fondamentaux, prévus seulement en phase 2 (`get_fundamentals`). Aucune tâche T1–T10 ne couvre
   cela. **Proposition : ajouter une tâche « T5b — cache de barres et classement momentum » ;
   qualité différée en phase 2 ou définie autrement.**
2. **Volume de décisions.** Cible 250–400 décisions fermées / 12 mois ≈ 500–800 ordres, soit
   40–67 ordres/mois, avec une construction initiale de ≥ 45 lignes (90 % / 2 %). Le premier mois
   épuise presque le quota de 60 : la montée en charge prendra ~1 mois. À garder en tête pour M+3.
3. **Ordre des tâches.** Suggestions ci-dessus : snapshot NAV quotidien dans T4 ; `closed_trades`
   minimale dans T5 ; étape stops avant la réconciliation des positions.

## 4. Questions à trancher avant T1

1. Environnement : installer `uv` (recommandé) ou `python3.14-venv` ? Installer `gh` ?
2. `hedge.csv` hors de `[universe] files` en phase 1 ?
3. Routage EU : `exchange` direct (texte du cahier) ou `SMART` + `primaryExchange` ?
4. REDUCED : blocage des achats seul, ou réduction active jusqu'à 50 % ?
5. Sortie des paliers PAUSED/REDUCED si le drawdown persiste ?
6. Définition de « réussite » pour la calibration et le taux de réussite.
7. Ajout d'une tâche classement momentum (point 3.1) ?
8. Garde-fou « NetLiquidation du compte paper ≥ capital en EUR » ?

## Annexe A — occurrences Sydney/AUD dans le code

| Fichier | Ligne | Objet |
|---|---|---|
| `guardrail_trader/journal.py` | 27 | `TZ = Australia/Sydney` (horodatages, mois des ordres, mois LLM) |
| `guardrail_trader/dashboard_data.py` | 193–199 | prochain créneau affiché en heure de Sydney, créneaux « open/close » dupliqués |
| `guardrail_trader/agent.py` | 84 | `currency` enum `USD, AUD` |
| `scripts/run_bot.py` | 28 | horodatage des logs |
| `scripts/gateway_watchdog.py` | 34 | `TZ` |
| `config/risk.toml` | 6, 10, 18 | devise, commentaire mensuel |
| `docker-compose.yml` | 35–36 | `TIME_ZONE`, `AUTO_RESTART_TIME` |
| `scripts/keep_awake.py` | — | spécifique macOS, à retirer du chemin VPS |
| tests / `evals/sim.py` | multiples | fixtures AUD/Sydney à migrer avec chaque tâche |

La documentation MkDocs (`docs/*.md`, `README.md`) décrit le setup Sydney/macOS ; mise à jour au fil
des tâches.

## 5. Décisions humaines du 12/10/2026 (réponses au §4)

| # | Question | Décision |
|---|---|---|
| 1 | Environnement | `uv` + `gh` installés ; `.venv` via `uv` ; tests verts avant T1. |
| 2 | `hedge.csv` | Hors univers achetable en phase 1 ; clé `enable_hedge = false` ; non exposé par `list_universe` ; rejeté par la gate. |
| 3 | Routage EU | `SMART` + `primaryExchange` ; LSE : pence → livres via `priceMagnifier`, test GBX obligatoire. |
| 4 | Palier −12 % | Passif : achats bloqués tant que actions > 50 % ; aucune vente forcée hors stops ; seul le kill switch vend. |
| 5 | Sortie des paliers | Demi-taille (`max_position_pct / 2`) tant que DD au-delà de −4 % ; taille normale sous −4 %. |
| 6 | Réussite | P&L net > 0 après commissions et slippage forfaitaire ; P&L relatif à IWDA en secondaire. |
| 7 | Momentum | Nouvelle tâche T2b : classement calculé par le code, cache SQLite rafraîchi le week-end, top 40 transmis à Claude ; qualité en phase 2. |
| 8 | Capital | `capital = 800000.0` EUR (compte paper plafonné à 1 M USD) ; garde-fou valeur liquidative ≥ capital à `init` et à chaque run. |

Stops (T5) : annuler ou réduire le stop avant toute vente, `permId`, `reqAllOpenOrders`, table séparée
hors quota. Réordonnancements §3.3 acceptés. Détail : `TASKS.md`, `CHANGELOG.md`.
