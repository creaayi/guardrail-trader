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
