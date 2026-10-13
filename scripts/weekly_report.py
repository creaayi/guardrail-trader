"""Write reports/YYYY-WW.md (CLAUDE.md §8.3) from the journal. The scheduler runs it on Fridays after the
evening stops slot; it can be run by hand for any week.

  .venv/bin/python scripts/weekly_report.py [--date 2026-10-16] [--notify]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from guardrail_trader.alerts import Alert, Notifier
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal
from guardrail_trader.report import build, summary
from guardrail_trader.risk import load_risk_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=date.fromisoformat, default=date.today(), help="any day of the week to report")
    ap.add_argument("--notify", action="store_true", help="send the headline figures through ntfy")
    args = ap.parse_args(argv)
    j = Journal()
    try:
        text = build(j, load_risk_config(), args.date)
    finally:
        j.close()
    year, week, _ = args.date.isocalendar()
    out = PROJECT_ROOT / "reports" / f"{year}-{week:02d}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text)
    print(f"wrote {out.relative_to(PROJECT_ROOT)}")
    if args.notify:
        Notifier.from_env().send(Alert(f"weekly report {year}-W{week:02d}", summary(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
