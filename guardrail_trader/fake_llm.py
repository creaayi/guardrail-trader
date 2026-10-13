"""A scripted stand-in for the Anthropic client (for tests and --fake-claude dry runs).

It follows the same tool protocol as real Claude, so the whole harness can be
exercised end to end without an API key or token spend.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace as NS

from guardrail_trader.decision import example


def _block_text(text):
    return NS(type="text", text=text)


_ids = itertools.count(1)


def _block_tool(name, args):
    return NS(type="tool_use", id=f"toolu_fake_{next(_ids)}", name=name, input=args)


class FakeClaude:
    """Plays a fixed script: portfolio -> universe -> history -> check -> submit."""

    def __init__(self, symbol: str = "CSCO", quantity: int = 2, price_fn=None):
        self.symbol, self.quantity = symbol, quantity
        self.price_fn = price_fn  # symbol -> reference price, to build a valid limit
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        usage = NS(input_tokens=0, output_tokens=0)
        order = {"symbol": self.symbol, "currency": "USD", "action": "BUY", "quantity": self.quantity,
                 "limit_price": round(self.price_fn(self.symbol), 2) if self.price_fn else 1.0,
                 "reason": "fake-claude: harness test order", **example("BUY")}
        script = [
            [_block_text("Checking the portfolio."), _block_tool("get_portfolio", {})],
            [_block_tool("list_universe", {"my_picks_only": True})],
            [_block_tool("get_price_history", {"symbol": self.symbol})],
            [_block_tool("check_orders", {"orders": [order]})],
            [_block_tool("submit_orders", {"orders": [order], "summary": "Fake run to exercise the harness."})],
        ]
        step = script[min(self.calls - 1, len(script) - 1)]
        return NS(content=step, stop_reason="tool_use", usage=usage)
