# Changelog des règles

Toute modification de `CLAUDE.md`, des limites (`config/risk.toml`), du modèle ou des créneaux est
décidée par l'humain et datée ici. Claude Code n'écrit une entrée que sur instruction écrite.

## 12/10/2026 — Validation de T0 (décisions humaines)

Source : validation du plan `docs/adaptation-plan.md` (§5). `TASKS.md` mis à jour en conséquence.

1. **Environnement** : `uv` 0.10.12 et `gh` installés sur le VPS ; `.venv` créé par `uv`. T1 ne
   commence qu'avec une suite de tests verte (constat T0 : 179 passés, 1 xfail connu).
2. **Couverture (`hedge.csv`)** : hors univers achetable en phase 1. Nouvelle clé
   `enable_hedge = false` ; `list_universe` ne l'expose pas ; la gate rejette tout ordre sur un
   ticker de `hedge.csv` tant que la poche C n'est pas activée.
3. **Routage EU** : contrats `SMART` avec `primaryExchange` (`SBF`, `AEB`, `IBIS`, `LSE`, `EBS`).
   LSE : conversion pence → livres via `priceMagnifier`, avec test unitaire obligatoire sur un titre GBX.
4. **Palier −12 %** : passif. Achats bloqués tant que l'exposition actions dépasse 50 % ; aucune
   vente forcée en dehors des stops. Seul le kill switch −15 % vend activement.
5. **Reprise après palier** : après les 5 jours de pause (−8 %) ou une sortie du palier −12 %,
   entrées à demi-taille (`max_position_pct / 2`) tant que le drawdown reste au-delà de −4 % ;
   taille normale sous −4 %.
6. **Réussite d'une décision fermée** : P&L net > 0 après commissions et slippage forfaitaire.
   Définition du taux de réussite et de la calibration. P&L relatif à IWDA enregistré à titre secondaire.
7. **Momentum** : nouvelle tâche T2b. Classement calculé par le code (rendement 6 mois hors
   dernier mois, filtre de volatilité ATR14/prix), cache SQLite rafraîchi le week-end ; Claude reçoit
   les 40 premiers avec leurs scores. Critère « qualité » reporté à la phase 2 (fondamentaux).
8. **Capital** : le compte paper est plafonné à 1 M USD → `capital = 800000.0` EUR (au lieu de
   1 000 000). Appliqué dans `config/risk.toml` et `CLAUDE.md` §1 et §4 lors de T1. Garde-fou :
   `journal_cli.py init` et chaque run refusent de démarrer si la valeur liquidative du compte paper
   en EUR est inférieure à `capital`.
9. **Stops (T5)** : avant toute vente volontaire ou kill switch, annuler ou réduire le stop de la
   ligne, puis vendre ; identifiant `permId` ; ordres ouverts lus par `reqAllOpenOrders` ; ordres stop
   dans une table séparée, hors `max_trades_per_month`.
10. **Réordonnancements** : snapshot NAV quotidien dès T4 ; table `closed_trades` minimale dès T5 ;
    stops exécutés traités avant la réconciliation des positions.

## 12/10/2026 — Amendement : merge des PR

Claude Code merge lui-même ses PR avec `gh pr merge --squash --delete-branch`, uniquement si
`pytest -q` est vert, et enchaîne sur la tâche suivante. Exceptions : T3, T4 et T5 — la PR est
ouverte, son résumé transmis à l'humain, et le merge attend sa validation. `TASKS.md` mis à jour.

## 12/10/2026 — T1 appliquée : devise EUR et capital

- `config/risk.toml` : `capital` 10 000 → **800 000**, `currency` AUD → **EUR** (décision 8 ci-dessus).
- `CLAUDE.md` §1 et §4 : capital 1 000 000 → 800 000 EUR, garde-fou décrit.
- Calendrier mensuel (quota d'ordres, budget LLM) et horodatages du journal : Sydney → **Europe/Paris**.
- Garde-fou valeur liquidative : à `journal_cli.py init` et à chaque run (statut `capital_guard`,
  code 3). Placé après le kill switch dans le run, pour qu'une liquidation forcée ne soit jamais bloquée.
- Aucune autre limite modifiée.

## 12/10/2026 — T2b appliquée : classement momentum

Nouvelle section `[momentum]` dans `config/risk.toml`, validée fail-closed (`load_momentum_config`).
Valeurs fixées par la décision 7 : `top_n = 40`, `lookback_days = 126`, `skip_days = 21`.
**Valeurs proposées par Claude Code, à confirmer par l'humain** (le cahier ne les fixe pas) :
- `max_atr_pct = 6.0` : au-delà, un stop à 2 × ATR14 serait toujours tronqué par le plancher −12 % (§5) ;
- `max_age_days = 10` : historique en cache plus ancien = titre exclu et compté (cache hebdomadaire + jours fériés).

## 12/10/2026 — T3 (PR en attente de validation humaine) : nouvelles limites de la gate

Valeurs de `CLAUDE.md` §4 appliquées dans `config/risk.toml` :
- resserrements : `max_position_pct` 25 → 2, `max_price_deviation_pct` 2 → 0,5, `max_commission_pct` 10 → 1 ;
- **assouplissement autorisé par écrit** (§4, `TASKS.md` T3) : `max_trades_per_month` 10 → 60 ;
- nouvelles clés : `min_position_pct = 0.5`, `max_sector_pct = 20`, `max_us_pct = 70`, `max_equity_pct = 90`,
  `reentry_cooldown_days = 10` (obligatoires, validées fail-closed).
- `max_drawdown_pct` reste à 25 jusqu'à T4 (passage à 15 avec les paliers).
Interprétation à valider : le minimum de 0,5 % s'applique à la ligne après un BUY et à la ligne restante
après une vente partielle ; une vente totale est toujours permise.

## 12/10/2026 — Décisions humaines (suite de T3)

T3 validée et mergée (PR #5), y compris l'interprétation de la taille minimale.


1. **Garde-fou de valeur liquidative — correction de la décision 8** : « valeur liquidative ≥ capital »
   uniquement à `journal_cli.py init`. À chaque run, la valeur liquidative (en EUR) est comparée à la
   **valeur virtuelle courante du bot** (trésorerie du grand livre + positions), après le kill switch.
   Motif : avec 1 M USD ≈ 855 k EUR, la comparaison au capital aurait arrêté le bot vers −7 %, avant les
   paliers −8 / −12 %. `CLAUDE.md` §1 mis à jour.
2. **Capitalisation** : en phase 1, l'appartenance à l'indice suffit ; la mention `cap unverified` reste
   dans la colonne `source`.
3. **Liquidité** : filtre ≥ 5 M obligatoire (`[momentum] min_traded_value = 5000000.0`), moyenne 20 jours
   close × volume en USD (US) / EUR (EU), calculée depuis le cache de barres IBKR : au rafraîchissement de
   l'univers (`refresh_universe.py` remplit le cache) et à chaque classement (titre exclu et compté).
4. `max_atr_pct = 6.0` et `max_age_days = 10` confirmés.

## 12/10/2026 — T4 (PR en attente de validation humaine) : paliers de drawdown et perte journalière

- `max_drawdown_pct` 25 → **15** (resserrement, CLAUDE.md §4) ; kill switch inchangé par ailleurs.
- Nouvelles clés obligatoires, contrainte `resume < pause < reduce < max_drawdown` :
  `drawdown_pause_pct = 8`, `drawdown_pause_days = 5`, `drawdown_reduce_pct = 12`,
  `reduce_max_equity_pct = 50`, `drawdown_resume_pct = 4`, `daily_loss_halt_pct = 3`.
  Les valeurs 5 jours et 50 % viennent du texte de §4 ; 4 % de la décision 5.
- `min_position_pct` doit rester sous la moitié de `max_position_pct` (achats à demi-taille).
- Interprétations à valider : un palier RECOVERY ne repasse jamais en PAUSED (une aggravation est
  traitée par REDUCED puis par le kill switch) ; la perte journalière compare les deux dernières NAV
  quotidiennes avant aujourd'hui (dernière valorisation de chaque jour, heure de Paris) et bloque
  aussi les ventes de Claude ce jour-là ; le kill switch reste prioritaire.

## 12/10/2026 — T4 validée (PR #7) et ports de l'hôte Docker

- T4 validée et mergée ; interprétations acceptées : pas de retour en PAUSED depuis RECOVERY (relais par
  REDUCED puis par le kill switch) ; perte journalière mesurée sur les deux dernières valorisations de fin
  de journée, heure de Paris.
- `config.py` : le port hôte 14002 (gateway Docker paper) est reconnu comme port paper, 14001 comme son
  équivalent réel, refusé en mode paper.

## 13/10/2026 — Amendement : capital 800 000 → 950 000 EUR

Décision humaine. Le compte paper est libellé en EUR et vaut 1 003 038 EUR (constaté au premier
`check_connection.py`), et non 1 M USD comme supposé le 12/10/2026 (décision 8).
- `config/risk.toml` : `capital` 800 000 → **950 000** EUR ; `CLAUDE.md` §1 et §4 mis à jour.
- Motif : utiliser l'essentiel du compte tout en gardant une marge (~53 000 EUR) sous le garde-fou de
  `journal_cli.py init` (valeur liquidative ≥ capital). Le garde-fou de chaque run reste comparé à la
  valeur virtuelle courante du bot.
- Le journal n'est pas encore initialisé : `init` déposera directement 950 000 EUR.

## 13/10/2026 — CSPX : place primaire LSEETF

Constaté au premier `check_connection.py --sample` : IBKR range les ETF cotés à Londres sur la place
primaire `LSEETF`, pas `LSE`. `hedge.csv` (CSPX) et la liste des places autorisées (`risk/config.py`)
corrigés, avec accord humain. Sans effet en phase 1 (`enable_hedge = false`).
