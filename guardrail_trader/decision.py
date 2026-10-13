"""Decision fields every order from Claude must carry (CLAUDE.md §8.1, T6), and their strict validation.

Claude proposes; this code checks the record is complete before the gate even looks at the order.
An order with a missing or malformed field is rejected with the reasons (fail-closed): the first
time it bounces back to Claude like a gate rejection (the run's single revision), after that it is
dropped and journaled as blocked.

BUY  : opens or adds to a decision; the full thesis is required.
SELL : closes a decision; the exit motif is required (motif_sortie). `stop` and `kill` are set by code
       (protective stop fill, kill switch), never by Claude.
"""
from __future__ import annotations

import math

POCHES = ("A", "B", "C")
FIABILITE = ("mecanique", "signal", "signal_sectoriel", "a_confirmer")
EXIT_MOTIFS = ("objectif", "horizon", "rebalancement", "these_invalidee")   # Claude's SELLs
CODE_MOTIFS = ("stop", "kill")                                              # set by code only
MAX_HORIZON_DAYS = 365
MAX_TEXT = 1500

# JSON schema properties, shared by check_orders and submit_orders.
PROPERTIES = {
    "poche": {"type": "string", "enum": list(POCHES),
              "description": "Pocket: A = systematic momentum (the only one enabled in phase 1)."},
    "these": {"type": "string", "description": "Thesis, 2-4 sentences: why this order, now."},
    "catalyseur": {"type": "string", "description": "BUY: what should move the price (event, trend, rebalancing)."},
    "fiabilite": {"type": "string", "enum": list(FIABILITE),
                  "description": "mecanique = rule-based (ranking, rebalancing); signal = instrument-specific "
                                 "evidence; signal_sectoriel = sector-level evidence; a_confirmer = unverified."},
    "horizon_jours": {"type": "integer", "minimum": 1, "maximum": MAX_HORIZON_DAYS,
                      "description": "BUY: holding period you expect, in calendar days."},
    "stop_pct": {"type": "number", "description": "BUY: loss in % below entry at which the thesis is wrong "
                                                  "(positive number; code places its own ATR stop)."},
    "objectif_pct": {"type": "number", "description": "BUY: expected gain in % at which you would take profit."},
    "invalidation": {"type": "string", "description": "BUY: the observable fact that would prove the thesis wrong."},
    "confiance": {"type": "number", "minimum": 0, "maximum": 1,
                  "description": "Probability (0-1) that this decision ends with a net profit. Calibrated: "
                                 "decisions rated 0.6 should succeed about 60% of the time."},
    "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                "description": "What you relied on: tool names (momentum_ranking, get_price_history) or URLs."},
    "motif_sortie": {"type": "string", "enum": list(EXIT_MOTIFS),
                     "description": "SELL only: objectif (target reached), horizon (holding period over), "
                                    "rebalancement (pocket A rebalance), these_invalidee (thesis proven wrong)."},
}
COMMON = ("poche", "these", "fiabilite", "confiance", "sources")
BUY_ONLY = ("catalyseur", "horizon_jours", "stop_pct", "objectif_pct", "invalidation")
SELL_ONLY = ("motif_sortie",)


def _text(v) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= MAX_TEXT


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def field_errors(order: dict, pockets: tuple[str, ...] = POCHES) -> list[str]:
    """Every problem with the decision fields of one order; [] when the record is complete."""
    action = str(order.get("action", "")).upper()
    required = COMMON + (BUY_ONLY if action == "BUY" else SELL_ONLY if action == "SELL" else ())
    errs = [f"missing field {k}" for k in required if order.get(k) is None or order.get(k) == ""]
    present = lambda k: k in required and order.get(k) not in (None, "")  # noqa: E731

    if present("poche"):
        if order["poche"] not in POCHES:
            errs.append(f"poche must be one of {'|'.join(POCHES)}, got {order['poche']!r}")
        elif order["poche"] not in pockets:
            errs.append(f"poche {order['poche']} is not enabled (enabled: {', '.join(pockets)})")
    for k in ("these", "catalyseur", "invalidation"):
        if present(k) and not _text(order[k]):
            errs.append(f"{k} must be a non-empty text of at most {MAX_TEXT} characters")
    if present("these") and _text(order["these"]) and len(order["these"].strip()) < 40:
        errs.append("these must explain the decision in 2-4 sentences (at least 40 characters)")
    if present("fiabilite") and order["fiabilite"] not in FIABILITE:
        errs.append(f"fiabilite must be one of {'|'.join(FIABILITE)}, got {order['fiabilite']!r}")
    if present("confiance") and not (_number(order["confiance"]) and 0 <= order["confiance"] <= 1):
        errs.append(f"confiance must be a number between 0 and 1, got {order['confiance']!r}")
    if present("sources"):
        s = order["sources"]
        if not (isinstance(s, list) and s and all(_text(x) for x in s)):
            errs.append("sources must be a non-empty list of non-empty strings")
    if present("horizon_jours"):
        h = order["horizon_jours"]
        if not (_number(h) and h == int(h) and 1 <= h <= MAX_HORIZON_DAYS):
            errs.append(f"horizon_jours must be a whole number of days in 1..{MAX_HORIZON_DAYS}, got {h!r}")
    if present("stop_pct") and not (_number(order["stop_pct"]) and 0 < order["stop_pct"] < 100):
        errs.append(f"stop_pct must be a positive % below 100 (loss below entry), got {order['stop_pct']!r}")
    if present("objectif_pct") and not (_number(order["objectif_pct"]) and 0 < order["objectif_pct"] <= 1000):
        errs.append(f"objectif_pct must be a positive % (expected gain), got {order['objectif_pct']!r}")
    if present("motif_sortie") and order["motif_sortie"] not in EXIT_MOTIFS:
        errs.append(f"motif_sortie must be one of {'|'.join(EXIT_MOTIFS)}, got {order['motif_sortie']!r}")
    return errs


def details(order: dict) -> dict:
    """The decision fields of a validated order, as journaled (only the keys that apply to its action)."""
    action = str(order.get("action", "")).upper()
    keys = COMMON + (BUY_ONLY if action == "BUY" else SELL_ONLY if action == "SELL" else ())
    return {k: order[k] for k in keys if k in order and k != "poche"}


def example(action: str, poche: str = "A", motif_sortie: str = "rebalancement") -> dict:
    """A complete, valid decision record for `action` (fake Claude, evals and tests); no meaning of its own."""
    rec = {"poche": poche, "fiabilite": "mecanique", "confiance": 0.55, "sources": ["momentum_ranking"],
           "these": "Scripted decision used to exercise the harness. It carries every field a real "
                    "decision must have, and nothing in it is market analysis."}
    if str(action).upper() == "BUY":
        rec |= {"catalyseur": "momentum ranking", "horizon_jours": 60, "stop_pct": 8.0, "objectif_pct": 12.0,
                "invalidation": "drops out of the momentum top 40"}
    else:
        rec["motif_sortie"] = motif_sortie
    return rec
