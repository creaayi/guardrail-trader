"""Rebuild config/universe/us_core.csv and eu_core.csv (CLAUDE.md §3). Run monthly, outside market hours.

Sources (public index constituents):
  US  S&P 500 and Nasdaq-100 constituent lists (Wikipedia).
  EU  STOXX Europe 600 holdings of the Xtrackers STOXX Europe 600 UCITS ETF (DWS, ISINs, no tickers).
  (iShares holdings files answer 403 from this VPS.)

IBKR (read-only connection, IB Gateway must be running):
  * contract resolution: US by symbol, EU by ISIN; only primary exchanges SBF, AEB, IBIS, LSE, EBS are kept;
  * sector = ContractDetails.industry: one classification for US and EU (max_sector_pct compares them);
  * daily bars, 6 months: >= MIN_BARS bars (listed for 6 months or more) and 20-day average traded value
    >= 5 M (USD for US, EUR for EU). Prices in pence (LSE) are converted with priceMagnifier.
  * market cap >= 2 bn: NO free source is reachable here. Index membership is used as the only proxy and
    every row says so in its `source` column ("cap unverified"). Missing data is flagged, never invented.

Pacing: IBKR allows ~60 historical-data requests per 10 minutes, so one request every --pace seconds
(default 10.5 s): about 2.5 hours for ~850 instruments. Run it at the weekend.

  .venv/bin/python scripts/refresh_universe.py [--only us|eu] [--pace 10.5] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.risk.config import UNIVERSE_COLUMNS

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
STOXX600_URL = "https://etf.dws.com/etfdata/export/DEU/DEU/excel/product/constituent/LU0328475792/"
HEADERS = {"User-Agent": "guardrail-trader universe refresh (paper-trading research)"}

US_OUT = PROJECT_ROOT / "config" / "universe" / "us_core.csv"
EU_OUT = PROJECT_ROOT / "config" / "universe" / "eu_core.csv"

EU_EXCHANGES = ("SBF", "AEB", "IBIS", "LSE", "EBS")
# DWS "Exchange" column -> IBKR primary exchange; "-" falls back to the ISIN country.
DWS_EXCHANGE = {"Euronext Paris": "SBF", "Euronext Amsterdam": "AEB", "XETRA": "IBIS",
                "London Stock Exchange": "LSE", "Scoach Switzerland": "EBS"}
ISIN_HOME = {"FR": "SBF", "NL": "AEB", "DE": "IBIS", "GB": "LSE", "CH": "EBS"}
SAME_COMPANY = {"GOOG": "GOOGL", "FOX": "FOXA", "NWS": "NWSA"}   # keep one share class per company

MIN_BARS = 120          # ~6 months of trading days
MIN_ADV = 5_000_000.0   # 20-day average traded value, USD (US) / EUR (EU)
MIN_ROWS = {"us": 400, "eu": 150}   # sanity: refuse to overwrite with a suspiciously short list
CAP_NOTE = "cap unverified (index membership only)"


@dataclass
class Candidate:
    symbol: str      # IBKR symbol (US) or "" until resolved (EU)
    name: str
    currency: str
    isin: str = ""
    hint: str = ""   # expected primary exchange (EU)
    index: str = ""


# ------------------------------------------------------------------ sources (pure parsing)
def ibkr_symbol(ticker: str) -> str:
    """Wikipedia/Bloomberg style -> IBKR: BRK.B -> 'BRK B'."""
    return ticker.strip().upper().replace(".", " ")


def parse_us(sp500_html: str, ndx_html: str) -> list[Candidate]:
    import pandas as pd
    sp = pd.read_html(io.StringIO(sp500_html), match="Symbol")[0]
    ndx = next(t for t in pd.read_html(io.StringIO(ndx_html), match="Ticker") if "Ticker" in t.columns)
    out: dict[str, Candidate] = {}
    for sym, name, idx in [*((s, n, "S&P 500") for s, n in zip(sp["Symbol"], sp["Security"])),
                           *((s, n, "Nasdaq-100") for s, n in zip(ndx["Ticker"], ndx["Company"]))]:
        s = ibkr_symbol(str(sym))
        if s in out:
            out[s].index += f" + {idx}"
            continue
        out[s] = Candidate(s, str(name).strip(), "USD", index=idx)
    for dup, keep in SAME_COMPANY.items():
        if keep in out:
            out.pop(dup, None)
    return list(out.values())


def parse_stoxx600(xlsx: bytes) -> list[Candidate]:
    import pandas as pd
    raw = pd.read_excel(io.BytesIO(xlsx), header=None)
    head = next(i for i in range(len(raw)) if "ISIN" in list(raw.iloc[i]))
    df = pd.read_excel(io.BytesIO(xlsx), header=head)
    df = df[(df["Type of Security"] == "Aktien") & df["ISIN"].astype(str).str.match(r"^[A-Z]{2}[A-Z0-9]{10}$")]
    out = []
    for _, r in df.iterrows():
        isin = str(r["ISIN"])
        hint = DWS_EXCHANGE.get(str(r["Exchange"]), ISIN_HOME.get(isin[:2], ""))
        out.append(Candidate("", str(r["Name"]).strip(), str(r["Currency"]).upper(), isin, hint, "STOXX 600"))
    return out


def choose_listing(details: list, hint: str):
    """Among IBKR listings of one ISIN, the one on the expected primary exchange of CLAUDE.md §3."""
    ok = [d for d in details if d.contract.primaryExchange in EU_EXCHANGES and d.contract.secType == "STK"]
    for d in ok:
        if d.contract.primaryExchange == hint:
            return d
    return None


def avg_traded_value(bars: list, magnifier: int, fx: float, days: int = 20) -> float:
    """Average close x volume over the last `days` bars, in the target currency."""
    last = bars[-days:]
    if not last:
        return 0.0
    return sum(float(b.close) * float(b.volume) for b in last) / len(last) / max(magnifier, 1) * fx


def keep_or_reason(bars: list, magnifier: int, fx: float, industry: str) -> str:
    """'' if the instrument passes CLAUDE.md §3 filters, else why not."""
    if not industry:
        return "no sector at IBKR"
    if len(bars) < MIN_BARS:
        return f"only {len(bars)} daily bars (< 6 months listed)"
    adv = avg_traded_value(bars, magnifier, fx)
    if adv < MIN_ADV:
        return f"20-day traded value {adv:,.0f} < {MIN_ADV:,.0f}"
    return ""


def write_csv(path: Path, rows: list[list[str]], min_rows: int) -> None:
    if len(rows) < min_rows:
        raise SystemExit(f"Only {len(rows)} rows for {path.name} (< {min_rows}); refusing to overwrite it.")
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(UNIVERSE_COLUMNS)
        w.writerows(sorted(rows))
    tmp.replace(path)


# ------------------------------------------------------------------ IBKR
def _bars(broker, contract, pace: float) -> list:
    time.sleep(pace)
    return broker.ib.reqHistoricalData(contract, endDateTime="", durationStr="6 M", barSizeSetting="1 day",
                                       whatToShow="TRADES", useRTH=True, formatDate=1, timeout=60) or []


def check_volume_units(broker, pace: float) -> None:
    """AAPL trades > 1 bn USD a day. Far less means volumes come in lots, not shares: stop, don't guess."""
    from guardrail_trader.broker.ibkr import stock_contract
    c = broker.ib.qualifyContracts(stock_contract("AAPL", "SMART", "USD"))[0]
    adv = avg_traded_value(_bars(broker, c, pace), 1, 1.0)
    if adv < 1e9:
        raise SystemExit(f"AAPL 20-day traded value {adv:,.0f} USD: IBKR volume units look wrong (lots?). "
                         "Check before trusting the 5 M filter.")
    print(f"volume units OK (AAPL 20-day traded value {adv:,.0f} USD)")


def resolve_us(broker, cands: list[Candidate], pace: float, log) -> list[list[str]]:
    from guardrail_trader.broker.ibkr import stock_contract
    rows = []
    for n, c in enumerate(cands, 1):
        details = broker.ib.reqContractDetails(stock_contract(c.symbol, "SMART", "USD"))
        if len(details) != 1:
            log(f"US {c.symbol}: {len(details)} IBKR contracts, skipped")
            continue
        d = details[0]
        why = keep_or_reason(_bars(broker, d.contract, pace), 1, 1.0, d.industry)
        if why:
            log(f"US {c.symbol}: excluded, {why}")
            continue
        rows.append([c.symbol, "SMART", "USD", c.name, d.industry, "US", f"{c.index}; {CAP_NOTE}; {date.today()}"])
        if n % 50 == 0:
            log(f"US {n}/{len(cands)} checked, {len(rows)} kept")
    return rows


def resolve_eu(broker, cands: list[Candidate], pace: float, log) -> list[list[str]]:
    from ib_async import Stock
    fx = {}
    rows, seen = [], set()
    for n, c in enumerate(cands, 1):
        if c.hint not in EU_EXCHANGES:
            log(f"EU {c.isin} {c.name}: primary exchange outside {EU_EXCHANGES}, skipped")
            continue
        details = broker.ib.reqContractDetails(Stock(secIdType="ISIN", secId=c.isin, exchange="SMART"))
        d = choose_listing(details, c.hint)
        if d is None:
            log(f"EU {c.isin} {c.name}: no IBKR listing on {c.hint}, skipped")
            continue
        k = (d.contract.symbol, d.contract.currency)
        if k in seen:
            log(f"EU {c.isin} {c.name}: {k} already listed (key collision), skipped")
            continue
        ccy = d.contract.currency
        if ccy not in fx:
            fx[ccy] = broker.fx_to_base(ccy, "EUR")
        why = keep_or_reason(_bars(broker, d.contract, pace), int(d.priceMagnifier or 1), fx[ccy], d.industry)
        if why:
            log(f"EU {d.contract.symbol} {c.name}: excluded, {why}")
            continue
        seen.add(k)
        rows.append([d.contract.symbol, d.contract.primaryExchange, ccy, c.name, d.industry, c.isin[:2],
                     f"{c.index} {c.isin}; {CAP_NOTE}; {date.today()}"])
        if n % 50 == 0:
            log(f"EU {n}/{len(cands)} checked, {len(rows)} kept")
    return rows


def main() -> int:
    import requests

    from guardrail_trader.broker import IBKRBroker
    from guardrail_trader.config import load_ibkr_config

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["us", "eu"])
    ap.add_argument("--pace", type=float, default=10.5, help="seconds between historical-data requests")
    ap.add_argument("--dry-run", action="store_true", help="print the counts, write nothing")
    args = ap.parse_args()
    log = lambda m: print(m, flush=True)  # noqa: E731

    get = lambda u: requests.get(u, headers=HEADERS, timeout=60)  # noqa: E731
    todo = {}
    if args.only in (None, "us"):
        todo["us"] = parse_us(get(SP500_URL).text, get(NDX_URL).text)
    if args.only in (None, "eu"):
        r = get(STOXX600_URL)
        r.raise_for_status()
        todo["eu"] = parse_stoxx600(r.content)
    log("candidates: " + ", ".join(f"{k} {len(v)}" for k, v in todo.items()))

    ib_cfg = load_ibkr_config()   # client id + 2: never collides with a bot run or refresh_bars.py
    with IBKRBroker(replace(ib_cfg, client_id=ib_cfg.client_id + 2), readonly=True) as broker:
        broker.ensure_ready()
        check_volume_units(broker, args.pace)
        for market, cands in todo.items():
            rows = (resolve_us if market == "us" else resolve_eu)(broker, cands, args.pace, log)
            out = US_OUT if market == "us" else EU_OUT
            log(f"{market}: {len(rows)} of {len(cands)} kept")
            if not args.dry_run:
                write_csv(out, rows, MIN_ROWS[market])
                log(f"wrote {out.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
