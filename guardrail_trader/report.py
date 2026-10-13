"""Weekly report (CLAUDE.md §8.3), built from the journal (SQLite) and the universe files only.

Every figure comes from the journal; a figure without enough data is shown as "n/d", never estimated
(§8.4). Success, the win rate and calibration all use the single definition of T6: P&L net of commissions
and of the flat slippage allowance > 0 (closed_trades.reussite).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from guardrail_trader.metrics import BENCHMARK_KEY, from_journal

CONFIDENCE_BUCKETS = [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.0001)]
MIN_VERDICT_DECISIONS = 200          # CLAUDE.md §9: no verdict below this
MIN_RATIO_DAYS = 20                  # alpha, beta, Sharpe, Sortino on fewer daily returns would mislead: n/d


def nd(v, fmt: str = "{:.2f}") -> str:
    return "n/d" if v is None else fmt.format(v)


def week_bounds(day: date) -> tuple[date, date]:
    """Monday and Sunday of the ISO week of `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


@dataclass
class TradeStats:
    n: int
    wins: int
    win_rate: float | None
    avg_gain: float | None
    avg_loss: float | None
    profit_factor: float | None


def trade_stats(rows) -> TradeStats:
    """rows: closed_trades rows. P&L counted net of commissions and the slippage allowance."""
    pnl = [(r["pnl_net_base"] or 0.0) - (r["slippage_base"] or 0.0) for r in rows]
    gains, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    n = len(pnl)
    return TradeStats(
        n=n, wins=len(gains), win_rate=len(gains) / n * 100 if n else None,
        avg_gain=sum(gains) / len(gains) if gains else None,
        avg_loss=sum(losses) / len(losses) if losses else None,
        profit_factor=(sum(gains) / -sum(losses)) if gains and losses and sum(losses) < 0 else None)


def calibration(rows) -> tuple[list[dict], float | None]:
    """Per confidence bucket: n, mean confidence, success rate, gap (points). And the n-weighted mean |gap|."""
    out, weighted, total = [], 0.0, 0
    for lo, hi in CONFIDENCE_BUCKETS:
        b = [r for r in rows if r["confiance"] is not None and lo <= r["confiance"] < hi]
        if not b:
            continue
        conf = sum(r["confiance"] for r in b) / len(b) * 100
        rate = sum(1 for r in b if r["reussite"]) / len(b) * 100
        out.append({"bucket": f"{lo:.1f}–{min(hi, 1.0):.1f}", "n": len(b), "confidence": conf, "success": rate,
                    "gap": rate - conf})
        weighted += abs(rate - conf) * len(b)
        total += len(b)
    return out, (weighted / total if total else None)


def exposures(j, cfg) -> dict | None:
    """Exposure at the last valued run (snapshots table): equity, US, sectors, currencies, in % of NAV."""
    run = j.db.execute("SELECT id, value_base FROM runs WHERE value_base IS NOT NULL ORDER BY id DESC LIMIT 1"
                       ).fetchone()
    if run is None or not run["value_base"]:
        return None
    nav = run["value_base"]
    lines = {r["key"]: r["quantity"] * r["price_base"]
             for r in j.db.execute("SELECT key, quantity, price_base FROM snapshots WHERE run_id=?", (run["id"],))}
    sectors: dict[str, float] = {}
    ccys: dict[str, float] = {}
    us = 0.0
    for key, v in lines.items():
        inst = cfg.universe.get(key) or cfg.hedge.get(key)
        sector = inst.sector if inst else "?"
        sectors[sector] = sectors.get(sector, 0.0) + v
        ccy = key.split(":")[1]
        ccys[ccy] = ccys.get(ccy, 0.0) + v
        us += v if inst is not None and inst.country == "US" else 0.0
    pct = lambda v: v / nav * 100  # noqa: E731
    return {"run_id": run["id"], "nav": nav, "equity": pct(sum(lines.values())), "us": pct(us),
            "sectors": {k: pct(v) for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])},
            "currencies": {k: pct(v) for k, v in sorted(ccys.items(), key=lambda kv: -kv[1])}}


def _period_return(j, table: str, col: str, start: date, end: date) -> float | None:
    """Simple return over [start, end] from the last value before `start` to the last value <= `end`
    (the week's flows are not netted: a top-up week says so in the report)."""
    a = j.db.execute(f"SELECT {col} FROM {table} WHERE date < ? ORDER BY date DESC LIMIT 1",
                     (start.isoformat(),)).fetchone()
    b = j.db.execute(f"SELECT {col} FROM {table} WHERE date <= ? ORDER BY date DESC LIMIT 1",
                     (end.isoformat(),)).fetchone()
    return (b[0] / a[0] - 1) * 100 if a and b and a[0] else None


def build(j, cfg, today: date) -> str:
    monday, sunday = week_bounds(today)
    year, week, _ = today.isocalendar()
    base = j.base_currency() or ""
    perf = from_journal(j)
    nav = j.db.execute("SELECT date, value_base FROM nav_daily WHERE date <= ? ORDER BY date DESC LIMIT 1",
                       (sunday.isoformat(),)).fetchone()
    closed_all = j.db.execute("SELECT * FROM closed_trades").fetchall()
    closed_week = j.db.execute("SELECT * FROM closed_trades WHERE substr(exit_ts,1,10) BETWEEN ? AND ?",
                               (monday.isoformat(), sunday.isoformat())).fetchall()
    s_all, s_week = trade_stats(closed_all), trade_stats(closed_week)
    cal, cal_gap = calibration(closed_all)

    fills = j.db.execute("SELECT quantity, price, commission, fx_to_base FROM ledger WHERE kind IN ('BUY','SELL') "
                         "AND substr(ts,1,10) BETWEEN ? AND ?", (monday.isoformat(), sunday.isoformat())).fetchall()
    traded = sum(f["quantity"] * f["price"] * f["fx_to_base"] for f in fills)
    commissions = sum((f["commission"] or 0.0) * f["fx_to_base"] for f in fills)
    slippage = sum(r["slippage_base"] or 0.0 for r in closed_week)
    navs = [r[0] for r in j.db.execute("SELECT value_base FROM nav_daily WHERE date BETWEEN ? AND ?",
                                       (monday.isoformat(), sunday.isoformat()))]
    avg_nav = sum(navs) / len(navs) if navs else None
    week_ret = _period_return(j, "nav_daily", "value_base", monday, sunday)
    week_bench = _period_return(j, "benchmark", "nav_base", monday, sunday)
    costs_pct = (commissions + slippage) / avg_nav * 100 if avg_nav else None
    peak = max(j.peak(), nav["value_base"]) if nav else None
    current_dd = (1 - nav["value_base"] / peak) * 100 if nav and peak else None
    ok = perf.days >= MIN_RATIO_DAYS
    ratio = lambda v: v if ok else None  # noqa: E731
    expo = exposures(j, cfg)

    L = [f"# Rapport hebdomadaire {year}-W{week:02d} ({monday:%d/%m} – {sunday:%d/%m/%Y})", "",
         f"Généré le {datetime.now():%d/%m/%Y %H:%M} depuis le journal. « n/d » = pas assez de données.", "",
         "## Valeur et performance", "",
         "| | Semaine | Depuis le jour 1 |", "|---|---|---|",
         f"| NAV ({base}) | {nd(nav['value_base'] if nav else None, '{:,.0f}')} | |",
         f"| Rendement net | {nd(week_ret, '{:+.2f} %')} | {nd(perf.return_pct, '{:+.2f} %')} |",
         f"| Rendement brut (avant coûts) | {nd(None if week_ret is None or costs_pct is None else week_ret + costs_pct, '{:+.2f} %')} | |",
         f"| {BENCHMARK_KEY} | {nd(week_bench, '{:+.2f} %')} | {nd(perf.benchmark_return_pct, '{:+.2f} %')} |",
         f"| Écart vs {BENCHMARK_KEY} | {nd(None if week_ret is None or week_bench is None else week_ret - week_bench, '{:+.2f} pts')} | {nd(perf.excess_pct, '{:+.2f} pts')} |",
         "", f"Depuis le jour 1 ({perf.days} jours de bourse"
         + ("" if ok else f" ; ratios n/d sous {MIN_RATIO_DAYS} jours") + ") :", "",
         f"- alpha (Jensen, annualisé) : {nd(ratio(perf.alpha_pct), '{:+.2f} %')} ; beta : {nd(ratio(perf.beta))}",
         f"- Sharpe : {nd(ratio(perf.sharpe))} (IWDA {nd(ratio(perf.benchmark_sharpe))}) ;"
         f" Sortino : {nd(ratio(perf.sortino))}",
         f"- drawdown max : {nd(perf.max_drawdown_pct, '{:.2f} %')} (IWDA {nd(perf.benchmark_max_drawdown_pct, '{:.2f} %')}) ;"
         f" drawdown courant : {nd(current_dd, '{:.2f} %')}", "",
         "## Décisions fermées", "",
         "| | Semaine | Depuis le jour 1 |", "|---|---|---|",
         f"| Nombre | {s_week.n} | {s_all.n} |",
         f"| Taux de réussite | {nd(s_week.win_rate, '{:.0f} %')} | {nd(s_all.win_rate, '{:.0f} %')} |",
         f"| Gain moyen ({base}) | {nd(s_week.avg_gain, '{:,.0f}')} | {nd(s_all.avg_gain, '{:,.0f}')} |",
         f"| Perte moyenne ({base}) | {nd(s_week.avg_loss, '{:,.0f}')} | {nd(s_all.avg_loss, '{:,.0f}')} |",
         f"| Profit factor | {nd(s_week.profit_factor)} | {nd(s_all.profit_factor)} |", "",
         "Réussite = P&L net de commissions et du slippage forfaitaire de 0,05 % > 0 (définition unique, T6)."]
    if s_all.n < MIN_VERDICT_DECISIONS:
        L.append(f"Moins de {MIN_VERDICT_DECISIONS} décisions fermées : aucun verdict (CLAUDE.md §9).")
    L += ["", "## Calibration (depuis le jour 1)", ""]
    if cal:
        L += ["| Confiance | n | Confiance moyenne | Réussite | Écart |", "|---|---|---|---|---|"]
        L += [f"| {c['bucket']} | {c['n']} | {c['confidence']:.0f} % | {c['success']:.0f} % | {c['gap']:+.0f} pts |"
              for c in cal]
        L.append(f"\nÉcart moyen pondéré : {cal_gap:.1f} points.")
    else:
        L.append("n/d (aucune décision fermée avec une confiance).")
    L += ["", "## Rotation et coûts (semaine)", "",
          f"- montant échangé : {traded:,.0f} {base} ; rotation : {nd(traded / avg_nav * 100 if avg_nav else None, '{:.1f} %')} de la NAV moyenne",
          f"- commissions IBKR : {commissions:,.2f} {base} ; slippage forfaitaire (décisions fermées) : {slippage:,.2f} {base}",
          "", "## Expositions", ""]
    if expo:
        L += [f"Au run #{expo['run_id']} : actions {expo['equity']:.1f} %, US {expo['us']:.1f} %, "
              f"trésorerie {100 - expo['equity']:.1f} %.", "",
              "Secteurs : " + (", ".join(f"{k} {v:.1f} %" for k, v in expo["sectors"].items()) or "aucun"),
              "", "Devises : " + (", ".join(f"{k} {v:.1f} %" for k, v in expo["currencies"].items()) or "aucune")]
    else:
        L.append("n/d (aucun run valorisé).")
    L += ["", "## Meilleures et pires décisions de la semaine", ""]
    ranked = sorted(closed_week, key=lambda r: (r["pnl_net_base"] or 0.0) - (r["slippage_base"] or 0.0))
    if ranked:
        picks = ([("Meilleure", r) for r in ranked[::-1][:3]] + [("Pire", r) for r in ranked[:3]]
                 if len(ranked) > 6 else [("Décision", r) for r in ranked[::-1]])
        for label, r in picks:
            entry = (j.db.execute("SELECT these, invalidation, confiance, poche FROM proposals WHERE id=?",
                                  (r["entry_proposal_id"],)).fetchone() if r["entry_proposal_id"] else None)
            net = (r["pnl_net_base"] or 0.0) - (r["slippage_base"] or 0.0)
            L += [f"### {label} : {r['symbol']}:{r['currency']} {net:+,.0f} {base} ({r['motif']}, "
                  f"{nd(r['duree_jours'], '{:.0f}')} j, vs IWDA {nd(r['pnl_vs_benchmark_pct'], '{:+.1f} pts')})", ""]
            if entry:
                L += [f"- poche {entry['poche']}, confiance {nd(entry['confiance'], '{:.2f}')}",
                      f"- thèse : {entry['these']}", f"- invalidation prévue : {entry['invalidation']}", ""]
            else:
                L += ["- pas de fiche d'entrée (décision antérieure à T6 ou saisie manuelle)", ""]
    else:
        L.append("Aucune décision fermée cette semaine.")
    return "\n".join(L).rstrip() + "\n"


def summary(text: str) -> str:
    """The first lines worth a phone notification: NAV, returns and the decision count."""
    keep = [ln for ln in text.splitlines() if ln.startswith(("| NAV", "| Rendement net", "| Écart", "| Nombre",
                                                             "| Taux"))]
    return "\n".join(ln.strip("| ").replace(" | ", " · ") for ln in keep)
