"""Hyperliquid public market data: candles, hourly funding and contract metadata.

Everything comes from the public info endpoint; no key or account is needed.
Hyperliquid serves at most the 5,000 most recent candles per interval.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

MAINNET_INFO = "https://api.hyperliquid.xyz/info"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data"
HOUR_MS = 3_600_000


def _post(body: dict, url: str = MAINNET_INFO, retries: int = 6) -> object:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if attempt == retries - 1 or e.code not in (429, 500, 502, 503):
                raise
            time.sleep(2 ** attempt)  # 1, 2, 4, 8, 16 s: the info endpoint rate-limits by weight
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Contract:
    coin: str
    sz_decimals: int
    max_leverage: float


def contract(coin: str) -> Contract:
    meta = _post({"type": "meta"})
    for u in meta["universe"]:  # type: ignore[index]
        if u["name"] == coin:
            return Contract(coin, int(u["szDecimals"]), float(u["maxLeverage"]))
    raise KeyError(f"{coin} is not a Hyperliquid perp")


def candles(coin: str, interval: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = start_ms if start_ms is not None else 0
    rows = _post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}})
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"no {interval} candles for {coin}")
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df[["ts", "open", "high", "low", "close", "volume"]].astype({"ts": "int64"})
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def funding_history(coin: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    """Hourly funding rates (fraction per hour), paginated 500 at a time."""
    end_ms = end_ms or int(time.time() * 1000)
    out: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        page = _post({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms})
        if not page:
            break
        out.extend(page)  # type: ignore[arg-type]
        last = int(page[-1]["time"])  # type: ignore[index]
        if last <= cursor:
            break
        cursor = last + 1
        if len(page) < 500:  # type: ignore[arg-type]
            break
        time.sleep(0.25)
    df = pd.DataFrame(out)
    if df.empty:
        return pd.DataFrame(columns=["ts", "funding"])
    df = pd.DataFrame({"ts": df["time"].astype("int64"), "funding": df["fundingRate"].astype(float)})
    # Funding settles on the hour; snap the few-ms jitter to the hour boundary.
    df["ts"] = (df["ts"] // HOUR_MS) * HOUR_MS
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def load(coin: str, interval: str, refresh: bool = False) -> pd.DataFrame:
    """Candles with per-bar funding columns, cached under data/.

    ``funding`` is the sum of hourly rates that settle inside the bar (what a
    position held through the whole bar pays per unit notional); ``funding_8h``
    is the trailing 8-hour sum at the bar close, the number traders quote.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{coin}_{interval}.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path)
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    bars = _with_funding(candles(coin, interval), coin, interval)
    # Keep only bars where funding history exists, so no backtest silently assumes zero funding.
    bars = bars.loc[bars["funding_covered"].idxmax():].reset_index(drop=True)
    bars.drop(columns=["time"]).to_csv(path, index=False)
    return bars


def _with_funding(bars: pd.DataFrame, coin: str, interval: str) -> pd.DataFrame:
    from .spec import INTERVALS

    bar_len = int(INTERVALS[interval] * HOUR_MS)
    fund = funding_history(coin, int(bars["ts"].iloc[0]) - 8 * HOUR_MS)
    hourly = fund.set_index("ts")["funding"]
    # Funding settled at hour h belongs to the bar covering (bar_open, bar_close].
    bar_ts = ((hourly.index.to_numpy() - 1) // bar_len) * bar_len
    per_bar = hourly.groupby(bar_ts).sum()
    bars["funding"] = bars["ts"].map(per_bar).fillna(0.0)
    trailing8 = hourly.rolling(8, min_periods=1).sum()
    close_ts = bars["ts"] + bar_len
    bars["funding_8h"] = close_ts.map(lambda t: trailing8.asof(t) if len(trailing8) else 0.0).fillna(0.0)
    bars["funding_covered"] = bars["ts"].isin(per_bar.index) | (bar_len < HOUR_MS)
    return bars


def recent(coin: str, interval: str, n_bars: int = 300) -> pd.DataFrame:
    """The last ``n_bars`` closed bars with funding, for live and paper trading."""
    from .spec import INTERVALS

    now = int(time.time() * 1000)
    bar_len = int(INTERVALS[interval] * HOUR_MS)
    bars = candles(coin, interval, now - (n_bars + 2) * bar_len, now)
    bars = bars[bars["ts"] + bar_len <= now].reset_index(drop=True)  # drop the bar still forming
    return _with_funding(bars, coin, interval)


def mid(coin: str, url: str = MAINNET_INFO) -> float:
    return float(_post({"type": "allMids"}, url)[coin])  # type: ignore[index]
