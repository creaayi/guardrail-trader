"""Dry-run week (CLAUDE.md §10, T10): how consistent are two decision runs of the same slot, and what did the week
cost. Writes reports/dry-run-week.md from the journal only.

Two runs of one slot on one day form a pair (the scheduler's second dry run, SCHEDULER_DRY_RUN=1). Each run's
proposals are reduced to (symbol, action); for a pair:
  disagreement on orders  = 1 - |A ∩ B| / |A ∪ B|  over (symbol, action)   (two empty sets agree: 0)
  disagreement on symbols = the same over symbols only
  opposite directions     = symbols one run buys and the other sells
The week's rate is the mean over pairs. Quantities and prices are not compared: same name and direction counts
as agreement.

  .venv/bin/python scripts/consistency.py [--since 2026-10-19] [--until 2026-10-23]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal


@dataclass(frozen=True)
class Pair:
    day: str
    slot: str
    runs: tuple[int, int]
    a: frozenset
    b: frozenset

    @staticmethod
    def _distance(x: set, y: set) -> float:
        return 0.0 if not (x | y) else 1 - len(x & y) / len(x | y)

    @property
    def orders(self) -> float:
        return self._distance(set(self.a), set(self.b))

    @property
    def symbols(self) -> float:
        return self._distance({s for s, _ in self.a}, {s for s, _ in self.b})

    @property
    def opposite(self) -> set[str]:
        da, db = dict(self.a), dict(self.b)
        return {s for s in da.keys() & db.keys() if da[s] != db[s]}


def dry_runs(j: Journal, since: str, until: str) -> list:
    return j.db.execute("SELECT id, started_at, slot, status FROM runs WHERE mode LIKE '%dry%' AND "
                        "substr(started_at,1,10) BETWEEN ? AND ? ORDER BY id", (since, until)).fetchall()


def pairs(j: Journal, runs: list) -> list[Pair]:
    """The first two dry runs of each (day, slot) that reached Claude (status dry_run)."""
    by_slot = defaultdict(list)
    for r in runs:
        if r["status"] == "dry_run" and r["slot"]:
            by_slot[(r["started_at"][:10], r["slot"])].append(r["id"])
    out = []
    for (day, slot), ids in sorted(by_slot.items()):
        if len(ids) < 2:
            continue
        sets = [frozenset((s, a) for s, a in j.db.execute(
            "SELECT symbol || ':' || currency, action FROM proposals WHERE run_id=?", (rid,))) for rid in ids[:2]]
        out.append(Pair(day, slot, (ids[0], ids[1]), sets[0], sets[1]))
    return out


def build(j: Journal, since: str, until: str) -> str:
    runs = dry_runs(j, since, until)
    ps = pairs(j, runs)
    cost = {r[0]: r[1] for r in j.db.execute("SELECT run_id, SUM(cost_usd) FROM llm_usage GROUP BY run_id")}
    statuses = Counter(r["status"] for r in runs)
    costs = [cost.get(r["id"], 0.0) for r in runs if r["status"] == "dry_run"]
    L = [f"# Semaine à blanc ({since} – {until})", "",
         "Généré depuis le journal (runs `--dry-run` uniquement). Aucun ordre n'a été envoyé.", "",
         "## Runs", "",
         f"- {len(runs)} runs à blanc : " + ", ".join(f"{s} {n}" for s, n in statuses.most_common()),
         f"- coût LLM : {sum(cost.get(r['id'], 0.0) for r in runs):.2f} USD au total ; par run ayant appelé Claude : "
         + (f"moyenne {sum(costs) / len(costs):.3f}, max {max(costs):.3f} USD" if costs else "n/d"), "",
         "| Run | Début | Créneau | Statut | Coût LLM (USD) | Propositions | Bloquées |", "|---|---|---|---|---|---|---|"]
    for r in runs:
        n, blocked = j.db.execute("SELECT COUNT(*), COALESCE(SUM(1 - approved), 0) FROM proposals WHERE run_id=?",
                                  (r["id"],)).fetchone()
        L.append(f"| #{r['id']} | {r['started_at'][:16]} | {r['slot'] or '-'} | {r['status']} | "
                 f"{cost.get(r['id'], 0.0):.3f} | {n} | {blocked} |")
    L += ["", "## Cohérence (deux runs du même créneau)", ""]
    if ps:
        L += ["| Jour | Créneau | Runs | Ordres run 1 | Ordres run 2 | Désaccord ordres | Désaccord titres | Sens opposés |",
              "|---|---|---|---|---|---|---|---|"]
        for p in ps:
            fmt = lambda s: ", ".join(f"{a} {k}" for k, a in sorted(s)) or "aucun"  # noqa: E731
            L.append(f"| {p.day} | {p.slot} | #{p.runs[0]}/#{p.runs[1]} | {fmt(p.a)} | {fmt(p.b)} | "
                     f"{p.orders:.0%} | {p.symbols:.0%} | {', '.join(sorted(p.opposite)) or '-'} |")
        L += ["", f"**Taux de désaccord moyen** sur {len(ps)} paires : ordres {sum(p.orders for p in ps) / len(ps):.0%}, "
                  f"titres {sum(p.symbols for p in ps) / len(ps):.0%} ; paires avec des sens opposés : "
                  f"{sum(1 for p in ps if p.opposite)}."]
    else:
        L.append("n/d : aucune paire de runs à blanc du même créneau.")
    L += ["", "## Propositions bloquées par la gate", ""]
    reasons = Counter()
    ids = [r["id"] for r in runs] or [-1]
    for (raw,) in j.db.execute(f"SELECT block_reasons FROM proposals WHERE approved=0 AND run_id IN "
                               f"({','.join('?' * len(ids))})", ids):
        for reason in json.loads(raw or "[]"):
            reasons[_kind(reason)] += 1
    L += [f"- {n} × {k}" for k, n in reasons.most_common()] or ["Aucune."]
    L += ["", "## Décision", "",
          "Le passage en réel est une décision humaine, datée dans `CHANGELOG.md` ; elle fixe le jour 0."]
    return "\n".join(L) + "\n"


def _kind(reason: str) -> str:
    """Group reasons that differ only by numbers or symbols (e.g. 'position would be 2.4%...')."""
    return re.sub(r"[A-Z0-9.]+:[A-Z]{3}", "<titre>", re.sub(r"\d[\d,.]*", "#", reason))[:160]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2000-01-01")
    ap.add_argument("--until", default=date.today().isoformat())
    args = ap.parse_args(argv)
    j = Journal()
    try:
        text = build(j, args.since, args.until)
    finally:
        j.close()
    out = PROJECT_ROOT / "reports" / "dry-run-week.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text)
    print(f"wrote {out.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
