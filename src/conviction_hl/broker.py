"""Where orders go: a paper broker on live prices, or Hyperliquid through an API wallet.

Both take the same call, ``market_order(coin, is_buy, size, reduce_only)``, so
the trading loop cannot tell them apart. The live loop calls the guardrails
before either broker sees an order.

Hyperliquid notes:
  * An API ("agent") wallet signs orders for a master account. It is passed as
    ``agent_key``; ``account_address`` is the master account it trades for.
    Orders are L1 actions an API wallet can sign; withdrawals and transfers are
    user-signed actions that need the master key.
  * A market order is an IOC limit order priced ``slippage`` through the mid,
    the same way the official SDK's ``market_open`` builds it.
  * ``dry_run`` builds and signs the exact action locally and returns it
    without posting anything.
  * The Hyperliquid specifics live in ``venue.py``; this file only adapts it
    to the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import data


@dataclass
class Fill:
    coin: str
    is_buy: bool
    size: float
    price: float
    fee: float
    reduce_only: bool
    raw: dict = field(default_factory=dict)


class PaperBroker:
    def __init__(self, equity: float = 10_000.0, taker_fee: float = 0.00045, slippage_bps: float = 2.0):
        self.cash = equity
        self.position = 0.0  # signed size
        self.entry_px = 0.0
        self.taker_fee = taker_fee
        self.slip = slippage_bps / 10_000
        self.fills: list[Fill] = []

    def mid(self, coin: str) -> float:
        return data.mid(coin)

    def equity(self, coin: str) -> float:
        return self.cash + (self.position * (self.mid(coin) - self.entry_px) if self.position else 0.0)

    def apply_funding(self, coin: str, rate: float) -> float:
        paid = self.position * self.mid(coin) * rate
        self.cash -= paid
        return paid

    def market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False) -> Fill:
        px = self.mid(coin) * (1 + self.slip if is_buy else 1 - self.slip)
        fee = size * px * self.taker_fee
        signed = size if is_buy else -size
        if reduce_only:
            self.cash += self.position * (px - self.entry_px)
            self.position = 0.0
        else:
            self.position, self.entry_px = signed, px
        self.cash -= fee
        fill = Fill(coin, is_buy, size, px, fee, reduce_only)
        self.fills.append(fill)
        return fill


class HyperliquidBroker:
    """The live loop's broker interface on top of :class:`conviction_hl.venue.HyperliquidVenue`."""

    def __init__(self, agent_key: str, account_address: str, network: str = "testnet", dry_run: bool = True, slippage: float = 0.01, venue=None):
        from .venue import HyperliquidVenue

        self.venue = venue or HyperliquidVenue(network, agent_key, account_address, dry_run=dry_run)  # type: ignore[arg-type]
        self.dry_run = self.venue.dry_run
        self.slippage = slippage
        self.position = 0.0
        self.fills: list[Fill] = []

    def mid(self, coin: str) -> float:
        return self.venue.mid(coin)

    def equity(self, coin: str, dry_run_equity: float = 1_000.0) -> float:
        return dry_run_equity if self.dry_run else self.venue.account().equity

    def set_leverage(self, coin: str, leverage: int) -> None:
        self.venue.set_leverage(coin, leverage)

    def market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False) -> Fill:
        from .venue import OrderIntent, VenueError, new_client_id

        res = self.venue.place_order(OrderIntent(coin, "buy" if is_buy else "sell", size, reduce_only, self.slippage, new_client_id()))
        if res.status in ("rejected", "resting"):
            raise VenueError("rejected", f"Hyperliquid did not fill the order: {res.error or res.status}", res)
        filled = res.filled_size if res.status == "filled" else res.requested_size
        px = res.avg_price if res.avg_price is not None else res.limit_price
        self.position = 0.0 if reduce_only else (filled if is_buy else -filled)
        fill = Fill(coin, is_buy, filled, float(px), filled * float(px) * self.venue.fees().taker, reduce_only, dict(res.raw))
        self.fills.append(fill)
        return fill
