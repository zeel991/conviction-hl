"""Deterministic order checks. The same function gates backtest, paper and live orders.

Orders that reduce risk (closing a position) are never blocked: a guardrail
that traps a user in a losing position is worse than no guardrail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .spec import Guardrails


@dataclass(frozen=True)
class Order:
    coin: str
    is_buy: bool
    notional_usd: float
    leverage: float
    reduce_only: bool = False


@dataclass
class AccountState:
    equity: float
    day_start_equity: float
    bars_since_exit: int = 1_000_000
    funding_8h: float = 0.0  # current trailing 8h funding rate of the coin, as a fraction


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    needs_approval: bool = False


def check(order: Order, state: AccountState, g: Guardrails, strategy_coin: str) -> Decision:
    if order.reduce_only:
        return Decision(True)

    reasons: list[str] = []
    allowed = set(g.allowed_coins) or {strategy_coin}
    if order.coin not in allowed:
        reasons.append(f"{order.coin} is not in allowed coins {sorted(allowed)}")
    if not order.is_buy and not g.allow_short:
        reasons.append("shorting is disabled")
    if order.notional_usd > g.max_position_usd + 1e-9:
        reasons.append(f"position ${order.notional_usd:,.0f} exceeds max ${g.max_position_usd:,.0f}")
    if order.leverage > g.max_leverage + 1e-9:
        reasons.append(f"leverage {order.leverage:g}x exceeds max {g.max_leverage:g}x")
    if state.day_start_equity > 0:
        day_loss = (state.day_start_equity - state.equity) / state.day_start_equity * 100
        if day_loss >= g.max_daily_loss_pct:
            reasons.append(f"daily loss {day_loss:.1f}% hit the {g.max_daily_loss_pct:g}% limit")
    if state.bars_since_exit < g.cooldown_bars:
        reasons.append(f"cooldown: {state.bars_since_exit}/{g.cooldown_bars} bars since last exit")
    if g.max_funding_8h_against is not None:
        # Longs pay positive funding, shorts pay negative funding.
        paying = state.funding_8h if order.is_buy else -state.funding_8h
        if paying > g.max_funding_8h_against:
            reasons.append(f"entry would pay {paying * 100:.4f}%/8h funding, above {g.max_funding_8h_against * 100:.4f}%")

    if reasons:
        return Decision(False, tuple(reasons))
    return Decision(True, needs_approval=g.require_approval)


def pre_trade_hook(g: Guardrails, strategy_coin: str, leverage: float, state: Callable[[], AccountState]):
    """Adapt these guardrails to ``HyperliquidVenue(pre_trade=...)``: raises VenueError("guardrail") to block."""
    from .venue import OrderIntent, VenueError

    def hook(intent: OrderIntent, mid: float) -> None:
        order = Order(intent.symbol, intent.side == "buy", intent.size * mid, leverage, intent.reduce_only)
        decision = check(order, state(), g, strategy_coin)
        if not decision.allowed:
            raise VenueError("guardrail", "; ".join(decision.reasons), decision.reasons)

    return hook
