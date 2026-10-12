"""Daily OHLCV cache (SQLite, data/bars.sqlite), filled by scripts/refresh_bars.py.

Prices are stored in currency units (LSE pence already converted with priceMagnifier), so every
consumer (momentum ranking, ATR stops) reads comparable numbers. Kept out of the journal: it is a
rebuildable cache, not an audit record.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from guardrail_trader.broker.ibkr import to_major
from guardrail_trader.config import PROJECT_ROOT

DEFAULT_BARS_DB = PROJECT_ROOT / "data" / "bars.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  key TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL,
  PRIMARY KEY (key, date)
);
CREATE TABLE IF NOT EXISTS refresh (
  key TEXT PRIMARY KEY, refreshed_at TEXT NOT NULL, status TEXT NOT NULL, bars INTEGER
);
"""


@dataclass(frozen=True)
class Bar:
    date: str      # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float


def from_ibkr(ib_bars, magnifier: int) -> list[Bar]:
    """IBKR BarData -> Bar in currency units (LSE pence / priceMagnifier)."""
    return [Bar(str(b.date)[:10], to_major(float(b.open), magnifier), to_major(float(b.high), magnifier),
                to_major(float(b.low), magnifier), to_major(float(b.close), magnifier), float(b.volume))
            for b in ib_bars]


def avg_traded_value(bars: list[Bar], days: int = 20) -> float:
    """Average close x volume over the last `days` bars, in the instrument's currency."""
    last = bars[-days:]
    return sum(b.close * b.volume for b in last) / len(last) if last else 0.0


class BarCache:
    def __init__(self, path: Path | str = DEFAULT_BARS_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def upsert(self, key: str, bars: list[Bar]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO bars(key,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(key,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume",
                [(key, b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars])

    def mark(self, key: str, status: str, now: datetime | None = None) -> None:
        """Record a refresh attempt ('ok' or the error), so the next batch starts with the oldest."""
        ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        n = self.db.execute("SELECT COUNT(*) FROM bars WHERE key=?", (key,)).fetchone()[0]
        with self.db:
            self.db.execute("INSERT INTO refresh(key,refreshed_at,status,bars) VALUES(?,?,?,?) "
                            "ON CONFLICT(key) DO UPDATE SET refreshed_at=excluded.refreshed_at, "
                            "status=excluded.status, bars=excluded.bars", (key, ts, status, n))

    def bars(self, key: str, limit: int | None = None) -> list[Bar]:
        """Oldest first; `limit` = only the most recent N."""
        sql = "SELECT date,open,high,low,close,volume FROM bars WHERE key=? ORDER BY date DESC"
        rows = self.db.execute(sql + (" LIMIT ?" if limit else ""), (key, limit) if limit else (key,)).fetchall()
        return [Bar(*r) for r in reversed(rows)]

    def latest_date(self, key: str) -> str | None:
        return self.db.execute("SELECT MAX(date) FROM bars WHERE key=?", (key,)).fetchone()[0]

    def refresh_order(self, keys: list[str]) -> list[str]:
        """Never-refreshed keys first, then the least recently refreshed."""
        seen = dict(self.db.execute("SELECT key, refreshed_at FROM refresh").fetchall())
        return sorted(keys, key=lambda k: (k in seen, seen.get(k, ""), k))
