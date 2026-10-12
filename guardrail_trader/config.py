"""Runtime configuration, loaded from .env.

Safety-critical values live here (in code/config), never only in a prompt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# IBKR's standard API socket ports. Paper and live use different ports by default.
# ib-gateway-docker host mappings (docker-compose.yml, 127.0.0.1 only): 14002 -> paper 4004; 14001 is the
# live equivalent, refused in paper mode like any live port.
PAPER_PORTS = {7497, 4002, 4004, 14002}  # TWS, IB Gateway, ib-gateway-docker (socat), its host port
LIVE_PORTS = {7496, 4001, 4003, 14001}   # same, live

MARKET_DATA_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


@dataclass(frozen=True)
class IBKRConfig:
    mode: str              # "paper" | "live"
    host: str
    port: int
    client_id: int
    market_data_type: int  # 1 live, 2 frozen, 3 delayed, 4 delayed-frozen


def load_ibkr_config() -> IBKRConfig:
    mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")

    mdt = int(os.getenv("IBKR_MARKET_DATA_TYPE", "3"))
    if mdt not in MARKET_DATA_TYPES:
        raise ValueError(f"IBKR_MARKET_DATA_TYPE must be one of {list(MARKET_DATA_TYPES)}, got {mdt}")

    return IBKRConfig(
        mode=mode,
        host=os.getenv("IBKR_HOST", "127.0.0.1"),
        port=int(os.getenv("IBKR_PORT", "4002")),
        client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
        market_data_type=mdt,
    )
