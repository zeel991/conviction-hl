"""Where does a strategy break? Cost stress, time split, regimes and parameter neighbours."""

from __future__ import annotations

import re
from dataclasses import replace

import numpy as np
import pandas as pd

from . import backtest as bt
from .spec import INTERVALS, Strategy


def _ret(r: bt.Result) -> float:
    return r.equity.iloc[-1] / r.equity.iloc[0] * 100 - 100


def time_split(df: pd.DataFrame, s: Strategy, frac: float = 0.7) -> dict[str, float]:
    cut = int(len(df) * frac)
    first = bt.run(df.iloc[:cut].reset_index(drop=True), s)
    last = bt.run(df.iloc[cut:].reset_index(drop=True), s)
    return {
        "first_70pct_return": _ret(first),
        "last_30pct_return": _ret(last),
        "first_70pct_trades": len(first.trades),
        "last_30pct_trades": len(last.trades),
        "split_date": str(pd.to_datetime(df["ts"].iloc[cut], unit="ms").date()),
    }


def regimes(df: pd.DataFrame, r: bt.Result, lookback_days: int = 30, band_pct: float = 10.0) -> dict[str, float]:
    """Split the strategy's P&L by the coin's trailing 30-day trend at each bar."""
    hours = INTERVALS[r.strategy.interval]
    n = max(1, int(lookback_days * 24 / hours))
    trend = df["close"].pct_change(n).to_numpy() * 100
    label = np.where(trend > band_pct, "uptrend", np.where(trend < -band_pct, "downtrend", "sideways"))
    pnl = r.equity.diff().fillna(0.0).to_numpy()
    out = {k: float(pnl[label == k].sum()) for k in ("uptrend", "sideways", "downtrend")}
    out["bars_uptrend_pct"] = float((label == "uptrend").mean() * 100)
    out["bars_downtrend_pct"] = float((label == "downtrend").mean() * 100)
    return out


def _numbers_in(s: Strategy) -> list[tuple[str, float]]:
    """Tunable knobs: indicator periods and percentage exits."""
    knobs: list[tuple[str, float]] = []
    for c in s.entry + s.exit:
        for m in re.finditer(r"([a-z_]+)\((\d+)\)", c):
            knobs.append((f"{m[1]}({m[2]})", float(m[2])))
    for name in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct"):
        v = getattr(s, name)
        if v is not None:
            knobs.append((name, v))
    return list(dict.fromkeys(knobs))


def _with_knob(s: Strategy, knob: str, value: float) -> Strategy:
    if knob.endswith("_pct"):
        return replace(s, **{knob: round(value, 2)})
    name, old = re.match(r"([a-z_]+)\((\d+)\)", knob).groups()  # type: ignore[union-attr]
    new = max(2, int(round(value)))
    sub = lambda conds: [c.replace(f"{name}({old})", f"{name}({new})") for c in conds]  # noqa: E731
    return replace(s, entry=sub(s.entry), exit=sub(s.exit))


def neighbours(df: pd.DataFrame, s: Strategy, factors=(0.75, 0.9, 1.1, 1.25)) -> pd.DataFrame:
    rows = []
    for knob, base in _numbers_in(s):
        for f in factors:
            variant = _with_knob(s, knob, base * f)
            rows.append({"knob": knob, "from": base, "to": base * f, "return_pct": _ret(bt.run(df, variant))})
    return pd.DataFrame(rows)


def report(df: pd.DataFrame, s: Strategy, coin_max_leverage: float = 40.0) -> dict:
    naive = bt.run(df, s, bt.NAIVE, coin_max_leverage=coin_max_leverage)
    honest = bt.run(df, s, bt.HONEST, coin_max_leverage=coin_max_leverage)
    stressed = bt.run(df, s, bt.with_costs(bt.HONEST, 2.0, 3.0), coin_max_leverage=coin_max_leverage)
    hm = honest.metrics()
    gross = sum(t.gross_pnl for t in honest.trades)
    nb = neighbours(df, s)
    buy_hold = (df["close"].iloc[-1] / df["open"].iloc[0] - 1) * 100 * (1 if s.side == "long" else -1)

    flags: list[str] = []
    if hm["total_return_pct"] < 0:
        stops = sum(t.reason.startswith("stop") for t in honest.trades)
        flags.append(f"Loses {-hm['total_return_pct']:.1f}% after costs; {stops} of {hm['trades']} trades ended at the stop.")
    if hm["trades"] < 30:
        flags.append(f"Only {hm['trades']} trades: too few to tell skill from luck; one trade can flip the result.")
    gap = naive.metrics()["total_return_pct"] - hm["total_return_pct"]
    if gap > 5:
        flags.append(f"A naive backtest overstates the return by {gap:.1f} points.")
    if gross > 0 and hm["funding_usd"] + hm["fees_usd"] > 0.5 * gross:
        flags.append(f"Fees and funding eat {(hm['funding_usd'] + hm['fees_usd']) / gross * 100:.0f}% of gross profit.")
    if hm["funding_usd"] < 0:
        flags.append(f"Funding paid the strategy ${-hm['funding_usd']:,.0f}; part of the edge is carry, not price.")
    if hm["liquidations"]:
        flags.append(f"{hm['liquidations']} liquidation(s) at {s.leverage:g}x leverage.")
    if len(nb):
        sign_flips = (np.sign(nb["return_pct"]) != np.sign(hm["total_return_pct"])).mean()
        if sign_flips > 0.25:
            flags.append(f"Fragile: {sign_flips * 100:.0f}% of nearby parameter settings flip the result's sign.")
    ts = time_split(df, s)
    if ts["last_30pct_trades"] == 0:
        flags.append(f"No trades after {ts['split_date']}: the setup stopped occurring.")
    elif ts["first_70pct_trades"] and np.sign(ts["first_70pct_return"]) != np.sign(ts["last_30pct_return"]):
        flags.append(f"The result flips sign after {ts['split_date']}.")
    reg = regimes(df, honest)
    total_pnl = reg["uptrend"] + reg["sideways"] + reg["downtrend"]
    if total_pnl > 0 and max(reg["uptrend"], reg["sideways"], reg["downtrend"]) > 1.5 * total_pnl:
        flags.append("Profit comes from one market regime and is given back in the others.")
    if s.side == "long" and hm["total_return_pct"] < buy_hold:
        flags.append(f"Buying and holding returned {buy_hold:.1f}% over the same period.")

    return {
        "strategy": s,
        "period": (str(honest.equity.index[0].date()), str(honest.equity.index[-1].date())),
        "naive": naive,
        "honest": honest,
        "stressed": stressed,
        "buy_hold_pct": buy_hold,
        "time_split": ts,
        "regimes": reg,
        "neighbours": nb,
        "flags": flags,
    }
