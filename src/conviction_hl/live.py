"""The trading loop for paper and live: same signals as the backtest, same guardrails.

Signals are evaluated once per closed bar. Stops, take-profits and trailing
stops are checked against the mid on every poll. Every decision is appended to
a JSONL log.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import data
from . import guardrails as gr
from .indicators import all_of, any_of
from .spec import Strategy


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ask_terminal(prompt: str) -> bool:
    return input(f"{prompt} Approve? [y/N] ").strip().lower() == "y"


def run(
    s: Strategy,
    broker,
    minutes: float,
    poll_seconds: float = 15,
    log_path: Path | None = None,
    approve: Callable[[str], bool] = ask_terminal,
    equity_fn: Callable[[], float] | None = None,
) -> list[dict]:
    sign = 1 if s.side == "long" else -1
    g = s.guardrails
    events: list[dict] = []
    log = open(log_path, "a") if log_path else None

    def emit(kind: str, **kw) -> None:
        ev = {"t": _now(), "event": kind, **kw}
        events.append(ev)
        print(json.dumps(ev))
        if log:
            log.write(json.dumps(ev) + "\n")
            log.flush()

    equity_fn = equity_fn or (lambda: broker.equity(s.coin))
    start_equity = equity_fn()
    day, day_start = datetime.now(timezone.utc).date(), start_equity
    last_bar_ts = None
    entry_px = peak = 0.0
    held_bars = 0
    bars_since_exit = 1_000_000
    in_pos = False
    emit("start", strategy=s.name, coin=s.coin, interval=s.interval, side=s.side, equity=round(start_equity, 2), broker=type(broker).__name__)
    if hasattr(broker, "set_leverage"):
        broker.set_leverage(s.coin, int(s.leverage))

    def close(reason: str) -> None:
        nonlocal in_pos, bars_since_exit
        fill = broker.market_order(s.coin, sign < 0, abs(broker.position), reduce_only=True)
        in_pos, bars_since_exit = False, 0
        emit("exit", reason=reason, price=fill.price, size=fill.size, equity=round(equity_fn(), 2))

    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        mid = broker.mid(s.coin)
        today = datetime.now(timezone.utc).date()
        if today != day:
            day, day_start = today, equity_fn()

        # Intrabar exits on the live mid.
        if in_pos:
            peak = max(peak, mid) if sign > 0 else min(peak, mid)
            levels = []
            if s.stop_loss_pct is not None:
                levels.append(("stop", entry_px * (1 - sign * s.stop_loss_pct / 100)))
            if s.trailing_stop_pct is not None:
                levels.append(("trailing stop", peak * (1 - sign * s.trailing_stop_pct / 100)))
            hit = next((name for name, lvl in levels if (mid <= lvl if sign > 0 else mid >= lvl)), None)
            if hit is None and s.take_profit_pct is not None:
                tp = entry_px * (1 + sign * s.take_profit_pct / 100)
                hit = "take profit" if (mid >= tp if sign > 0 else mid <= tp) else None
            if hit:
                close(hit)

        # Bar-close signals.
        bars = data.recent(s.coin, s.interval, n_bars=300)
        bar_ts = int(bars["ts"].iloc[-1])
        if bar_ts != last_bar_ts:
            first_bar = last_bar_ts is None
            last_bar_ts = bar_ts
            entry = bool(all_of(bars, s.entry_conditions).iloc[-1])
            exit_ = bool(any_of(bars, s.exit_conditions).iloc[-1]) if s.exit else False
            emit("bar", bar=str(bars["time"].iloc[-1]), close=float(bars["close"].iloc[-1]), entry_signal=entry, exit_signal=exit_, in_position=in_pos)
            if in_pos:
                if hasattr(broker, "apply_funding"):
                    paid = broker.apply_funding(s.coin, float(bars["funding"].iloc[-1]))
                    if paid:
                        emit("funding", paid=round(paid, 4))
                held_bars += 1
                if s.max_hold_bars is not None and held_bars >= s.max_hold_bars:
                    close("max hold")
                elif exit_:
                    close("exit signal")
            else:
                bars_since_exit += 0 if first_bar else 1

            if not in_pos and entry:
                equity = equity_fn()
                notional = min(equity * s.position_pct / 100 * s.leverage, g.max_position_usd)
                order = gr.Order(s.coin, sign > 0, notional, s.leverage)
                state = gr.AccountState(equity, day_start, bars_since_exit, float(bars["funding_8h"].iloc[-1]))
                decision = gr.check(order, state, g, s.coin)
                if not decision.allowed:
                    emit("blocked", reasons=list(decision.reasons))
                elif decision.needs_approval and not approve(f"{s.side} {s.coin} ${notional:,.0f} at ~{mid:,.2f}."):
                    emit("declined", notional=round(notional, 2))
                else:
                    fill = broker.market_order(s.coin, sign > 0, notional / mid)
                    in_pos, entry_px, peak, held_bars = True, fill.price, fill.price, 0
                    emit("entry", price=fill.price, size=fill.size, notional=round(fill.size * fill.price, 2), raw=fill.raw or None)

        time.sleep(poll_seconds)

    if in_pos:
        close("session end")
    emit("stop", equity=round(equity_fn(), 2), pnl=round(equity_fn() - start_equity, 2))
    if log:
        log.close()
    return events
