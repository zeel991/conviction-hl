"""Bar-by-bar perp backtester with an honest mode and a naive mode.

Honest mode is what would have happened on Hyperliquid:
  * a signal seen at a bar's close fills at the next bar's open, plus slippage
  * every fill pays the taker fee
  * every bar held pays (or earns) that bar's hourly funding
  * stops, take-profits and trailing stops trigger intrabar on the high/low,
    filling at the open when price gaps through them; when a stop and a
    take-profit both fall inside one bar, the stop is assumed to hit first
  * leveraged positions are liquidated at the isolated-margin liquidation price
  * a position closed intrabar does not pay that bar's funding (a small optimism)

Naive mode is how most quick backtests are written: fill at the signal bar's
close, no fees, no slippage, no funding, stops checked on closes, no liquidation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from . import guardrails as gr
from .indicators import all_of, any_of
from .spec import INTERVALS, Strategy


@dataclass(frozen=True)
class Costs:
    taker_fee: float = 0.00045  # Hyperliquid perps, base tier
    slippage_bps: float = 2.0
    funding: bool = True
    next_bar_fills: bool = True
    intrabar_stops: bool = True
    liquidation: bool = True


HONEST = Costs()
NAIVE = Costs(taker_fee=0.0, slippage_bps=0.0, funding=False, next_bar_fills=False, intrabar_stops=False, liquidation=False)


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str
    entry_px: float
    exit_px: float
    notional: float
    gross_pnl: float
    fees: float
    funding: float
    reason: str

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding


@dataclass
class Result:
    strategy: Strategy
    costs: Costs
    equity: pd.Series
    trades: list[Trade]
    blocked: list[tuple[pd.Timestamp, tuple[str, ...]]] = field(default_factory=list)
    liquidations: int = 0

    def metrics(self) -> dict[str, float]:
        return metrics(self)


def run(
    df: pd.DataFrame,
    s: Strategy,
    costs: Costs = HONEST,
    start_equity: float = 10_000.0,
    coin_max_leverage: float = 40.0,
) -> Result:
    entry_sig = all_of(df, s.entry_conditions).to_numpy()
    exit_sig = (any_of(df, s.exit_conditions) if s.exit else pd.Series(False, index=df.index)).to_numpy()
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    funding = df["funding"].to_numpy(float) if "funding" in df else np.zeros(len(df))
    funding_8h = df["funding_8h"].to_numpy(float) if "funding_8h" in df else np.zeros(len(df))
    times = pd.to_datetime(df["ts"], unit="ms", utc=True)
    days = times.dt.floor("D").to_numpy()

    sign = 1 if s.side == "long" else -1
    slip = costs.slippage_bps / 10_000
    mm = 1 / (2 * coin_max_leverage)  # maintenance margin fraction on Hyperliquid
    g = s.guardrails

    cash = start_equity
    pos: dict | None = None
    pending: str | None = None  # "enter" or an exit reason, executed at the next open
    trades: list[Trade] = []
    blocked: list[tuple[pd.Timestamp, tuple[str, ...]]] = []
    liquidations = 0
    bars_since_exit = 1_000_000
    day_start_equity = start_equity
    equity = np.empty(len(df))

    def open_pos(i: int, px_raw: float) -> None:
        nonlocal cash, pos
        px = px_raw * (1 + sign * slip)
        notional = min(cash * s.position_pct / 100 * s.leverage, g.max_position_usd)
        fee = notional * costs.taker_fee
        cash -= fee
        pos = {"i": i, "px": px, "size": notional / px, "notional": notional, "peak": px, "fees": fee, "funding": 0.0}

    def close_pos(i: int, px_raw: float, reason: str, slipped: bool = True) -> None:
        nonlocal cash, pos, bars_since_exit, liquidations
        assert pos is not None
        px = px_raw * (1 - sign * slip) if slipped else px_raw
        gross = sign * pos["size"] * (px - pos["px"])
        fee = pos["size"] * px * costs.taker_fee
        if reason == "liquidation":
            gross = -pos["notional"] / s.leverage  # isolated margin is lost
            fee = 0.0
            liquidations += 1
        cash += gross - fee
        trades.append(
            Trade(times.iloc[pos["i"]], times.iloc[i], s.side, pos["px"], px, pos["notional"], gross, pos["fees"] + fee, pos["funding"], reason)
        )
        pos = None
        bars_since_exit = 0

    for i in range(len(df)):
        if i > 0 and days[i] != days[i - 1]:
            day_start_equity = equity[i - 1]

        # 1. Orders queued at the previous close fill at this open.
        if pending is not None:
            if pending == "enter" and pos is None:
                open_pos(i, o[i])
            elif pending != "enter" and pos is not None:
                close_pos(i, o[i], pending)
            pending = None

        # 2. Stops, take-profit, trailing stop, liquidation.
        if pos is not None:
            e, peak = pos["px"], pos["peak"]
            stops = []
            if s.stop_loss_pct is not None:
                stops.append(e * (1 - sign * s.stop_loss_pct / 100))
            if s.trailing_stop_pct is not None:
                stops.append(peak * (1 - sign * s.trailing_stop_pct / 100))
            stop = (max(stops) if sign > 0 else min(stops)) if stops else None
            tp = e * (1 + sign * s.take_profit_pct / 100) if s.take_profit_pct is not None else None
            liq = e * (1 - sign * (1 / s.leverage - mm)) if costs.liquidation and s.leverage > 1 else None

            if costs.intrabar_stops:
                adverse, favorable = (l[i], h[i]) if sign > 0 else (h[i], l[i])
                beyond = (lambda px, lvl: px <= lvl) if sign > 0 else (lambda px, lvl: px >= lvl)
                stop_first = stop is not None and (liq is None or not beyond(stop, liq))
                if stop is not None and stop_first and beyond(o[i], stop):
                    close_pos(i, o[i], "stop (gap)")
                elif stop is not None and stop_first and beyond(adverse, stop):
                    close_pos(i, stop, "stop")
                elif liq is not None and beyond(adverse, liq):
                    close_pos(i, liq, "liquidation", slipped=False)
                elif tp is not None and (o[i] >= tp if sign > 0 else o[i] <= tp):
                    close_pos(i, o[i], "take profit (gap)", slipped=False)
                elif tp is not None and (favorable >= tp if sign > 0 else favorable <= tp):
                    close_pos(i, tp, "take profit", slipped=False)
            else:
                hit_stop = stop is not None and (c[i] <= stop if sign > 0 else c[i] >= stop)
                hit_tp = tp is not None and (c[i] >= tp if sign > 0 else c[i] <= tp)
                if hit_stop:
                    close_pos(i, c[i], "stop")
                elif hit_tp:
                    close_pos(i, c[i], "take profit", slipped=False)

        # 3. Funding for the bar, trailing peak, exit signals.
        if pos is not None:
            if costs.funding:
                paid = sign * pos["size"] * c[i] * funding[i]
                cash -= paid
                pos["funding"] += paid
            pos["peak"] = max(pos["peak"], h[i]) if sign > 0 else min(pos["peak"], l[i])
            held = i - pos["i"] + 1
            if s.max_hold_bars is not None and held >= s.max_hold_bars:
                pending = "max hold"
            elif exit_sig[i]:
                pending = "exit signal"
        else:
            bars_since_exit += 1

        # 4. Entry decision at the close, through the guardrails.
        if pos is None and pending is None and entry_sig[i] and i < len(df) - 1 and cash > 0:
            notional = min(cash * s.position_pct / 100 * s.leverage, g.max_position_usd)
            order = gr.Order(s.coin, sign > 0, notional, s.leverage)
            state = gr.AccountState(cash, day_start_equity, bars_since_exit, funding_8h[i])
            decision = gr.check(order, state, g, s.coin)
            if decision.allowed:
                pending = "enter"
            else:
                blocked.append((times.iloc[i], decision.reasons))

        # 5. Naive mode fills at the signal bar's close.
        if not costs.next_bar_fills and pending is not None:
            if pending == "enter":
                open_pos(i, c[i])
            elif pos is not None:
                close_pos(i, c[i], pending)
            pending = None

        unrealized = sign * pos["size"] * (c[i] - pos["px"]) if pos is not None else 0.0
        equity[i] = cash + unrealized

    if pos is not None:
        close_pos(len(df) - 1, c[-1], "end of data")
        equity[-1] = cash

    return Result(s, costs, pd.Series(equity, index=times), trades, blocked, liquidations)


def metrics(r: Result) -> dict[str, float]:
    eq = r.equity
    hours = INTERVALS[r.strategy.interval]
    bars_per_year = 8760 / hours
    rets = eq.pct_change().fillna(0.0)
    years = len(eq) / bars_per_year
    total = eq.iloc[-1] / eq.iloc[0] - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = rets.mean() / rets.std() * np.sqrt(bars_per_year) if rets.std() > 0 else 0.0
    wins = [t for t in r.trades if t.net_pnl > 0]
    held_bars = sum(max(1, int((t.exit_time - t.entry_time) / pd.Timedelta(hours=hours))) for t in r.trades)
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in r.trades if t.net_pnl <= 0)
    return {
        "total_return_pct": total * 100,
        "cagr_pct": ((1 + total) ** (1 / years) - 1) * 100 if years > 0 and total > -1 else -100.0,
        "max_drawdown_pct": dd * 100,
        "sharpe": float(sharpe),
        "trades": len(r.trades),
        "win_rate_pct": len(wins) / len(r.trades) * 100 if r.trades else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "exposure_pct": held_bars / len(eq) * 100,
        "fees_usd": sum(t.fees for t in r.trades),
        "funding_usd": sum(t.funding for t in r.trades),
        "liquidations": r.liquidations,
        "blocked_entries": len(r.blocked),
    }


def with_costs(c: Costs, fee_mult: float = 1.0, slip_mult: float = 1.0) -> Costs:
    return replace(c, taker_fee=c.taker_fee * fee_mult, slippage_bps=c.slippage_bps * slip_mult)
