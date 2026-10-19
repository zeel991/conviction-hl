"""Operand series. Every value at bar i uses data up to and including bar i only."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .spec import Condition, Operand


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).where(loss != 0, 100.0)


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def series(df: pd.DataFrame, op: Operand) -> pd.Series:
    if op.kind == "num":
        return pd.Series(op.value, index=df.index)
    if op.kind == "field":
        return df[op.name].astype(float)
    n, c = op.arg, df["close"]
    match op.name:
        case "sma":
            return c.rolling(n).mean()
        case "ema":
            return c.ewm(span=n, adjust=False, min_periods=n).mean()
        case "rsi":
            return _rsi(c, n)
        case "atr":
            return _atr(df, n)
        case "highest":
            return df["high"].rolling(n).max()
        case "lowest":
            return df["low"].rolling(n).min()
        case "volume_sma":
            return df["volume"].rolling(n).mean()
        case "return":
            return c.pct_change(n) * 100
    raise ValueError(op.name)


def evaluate(df: pd.DataFrame, cond: Condition, cache: dict[str, pd.Series] | None = None) -> pd.Series:
    """Boolean series: the condition holds at the close of each bar. NaN warm-up bars are False."""
    cache = {} if cache is None else cache
    a = cache.setdefault(cond.left.key(), series(df, cond.left))
    b = cache.setdefault(cond.right.key(), series(df, cond.right))
    match cond.op:
        case ">":
            out = a > b
        case "<":
            out = a < b
        case ">=":
            out = a >= b
        case "<=":
            out = a <= b
        case "crosses_above":
            out = (a > b) & (a.shift() <= b.shift())
        case "crosses_below":
            out = (a < b) & (a.shift() >= b.shift())
        case _:
            raise ValueError(cond.op)
    return (out & a.notna() & b.notna()).fillna(False).astype(bool)


def all_of(df: pd.DataFrame, conds: list[Condition]) -> pd.Series:
    cache: dict[str, pd.Series] = {}
    out = pd.Series(True, index=df.index)
    for c in conds:
        out &= evaluate(df, c, cache)
    return out


def any_of(df: pd.DataFrame, conds: list[Condition]) -> pd.Series:
    cache: dict[str, pd.Series] = {}
    out = pd.Series(False, index=df.index)
    for c in conds:
        out |= evaluate(df, c, cache)
    return out
