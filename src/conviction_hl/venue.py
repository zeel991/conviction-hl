"""A venue adapter: one small interface a trading app's broker layer can call,
and a Hyperliquid implementation of it on the official Python SDK.

The adapter does market data, account state and orders. It does not decide
what to trade or how much: strategy logic, sizing and risk limits stay in the
host app. Risk limits plug in through ``pre_trade``, a callable that sees every
order intent with the current mid and raises to block it.

Units, everywhere:
  * timestamps are Unix milliseconds, UTC
  * prices are USD; sizes are in coin units (0.01 = 0.01 BTC)
  * funding rates are fractions per hour (0.0000125 = 0.00125%/h)
  * a position's size is signed: positive long, negative short
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

MIN_ORDER_USD = 10.0  # Hyperliquid rejects orders below $10 notional
HOUR_MS = 3_600_000


class VenueError(Exception):
    """Raised for anything the caller should handle. ``code`` is stable; the message is for humans."""

    def __init__(self, code: str, message: str, detail: object = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Market:
    symbol: str
    size_decimals: int
    max_leverage: float
    min_notional_usd: float = MIN_ORDER_USD


@dataclass(frozen=True)
class Candle:
    ts_ms: int  # bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float  # in coin units


@dataclass(frozen=True)
class FundingRate:
    ts_ms: int  # settlement time, on the hour
    rate: float  # per hour; positive means longs pay shorts


@dataclass(frozen=True)
class FeeSchedule:
    taker: float
    maker: float


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Literal["buy", "sell"]
    size: float  # coin units, before rounding to the market's size decimals
    reduce_only: bool = False
    max_slippage: float = 0.01  # the IOC limit is placed this far through the mid
    client_id: str | None = None  # 0x + 32 hex chars; reusing one makes a retry idempotent


@dataclass(frozen=True)
class OrderResult:
    status: Literal["filled", "resting", "rejected", "dry_run"]
    symbol: str
    side: str
    requested_size: float
    filled_size: float = 0.0
    avg_price: float | None = None
    limit_price: float | None = None
    order_id: int | None = None
    client_id: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Position:
    symbol: str
    size: float
    entry_price: float
    unrealized_pnl: float
    liquidation_price: float | None
    leverage: float
    margin_used: float
    position_value: float


@dataclass(frozen=True)
class Account:
    equity: float
    margin_used: float
    withdrawable: float


PreTrade = Callable[[OrderIntent, float], None]  # (intent, current mid) -> raise VenueError to block


class Venue(Protocol):
    """What a host's broker layer needs from a venue."""

    name: str

    def markets(self) -> list[Market]: ...
    def market(self, symbol: str) -> Market: ...
    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]: ...
    def funding(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[FundingRate]: ...
    def mid(self, symbol: str) -> float: ...
    def fees(self) -> FeeSchedule: ...
    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def set_leverage(self, symbol: str, leverage: int, isolated: bool = True) -> None: ...
    def place_order(self, intent: OrderIntent) -> OrderResult: ...
    def close_position(self, symbol: str, max_slippage: float = 0.01, client_id: str | None = None) -> OrderResult | None: ...


def new_client_id() -> str:
    return "0x" + secrets.token_hex(16)


class HyperliquidVenue:
    """Hyperliquid perps through an API ("agent") wallet.

    * No ``agent_key``: read-only. Market data works; account calls need ``account_address``.
    * ``dry_run=True`` (the default): orders are validated, priced, built and signed
      locally, then returned with status ``"dry_run"`` instead of being posted.
    * ``info`` and ``exchange`` can be injected (tests, or a host's own SDK clients).
    """

    name = "hyperliquid"
    FEES = FeeSchedule(taker=0.00045, maker=0.00015)  # perps base tier; volume tiers and staking lower these

    def __init__(
        self,
        network: Literal["mainnet", "testnet"] = "mainnet",
        agent_key: str | None = None,
        account_address: str | None = None,
        dry_run: bool = True,
        pre_trade: PreTrade | None = None,
        info=None,
        exchange=None,
    ):
        from hyperliquid.utils import constants

        if network not in ("mainnet", "testnet"):
            raise VenueError("bad_config", "network must be 'mainnet' or 'testnet'")
        self.network = network
        self.base_url = constants.MAINNET_API_URL if network == "mainnet" else constants.TESTNET_API_URL
        self.account_address = account_address
        self.dry_run = dry_run
        self.pre_trade = pre_trade
        self._wallet = None
        if agent_key:
            from eth_account import Account as EthAccount

            self._wallet = EthAccount.from_key(agent_key)
        if info is None:
            from hyperliquid.info import Info

            info = Info(self.base_url, skip_ws=True)
        self.info = info
        self._exchange = exchange
        self._markets: dict[str, Market] = {}

    # ---------- market data ----------

    def markets(self, refresh: bool = False) -> list[Market]:
        if refresh or not self._markets:
            self._markets = {
                u["name"]: Market(u["name"], int(u["szDecimals"]), float(u["maxLeverage"]))
                for u in self.info.meta()["universe"]
                if not u.get("isDelisted")
            }
        return list(self._markets.values())

    def market(self, symbol: str) -> Market:
        self.markets()
        if symbol not in self._markets:
            raise VenueError("unknown_market", f"{symbol} is not a listed perp on Hyperliquid {self.network}")
        return self._markets[symbol]

    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        rows = self.info.candles_snapshot(symbol, interval, start_ms, end_ms)
        return [Candle(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])) for r in rows]

    def funding(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[FundingRate]:
        out: list[FundingRate] = []
        cursor = start_ms
        while True:
            page = self.info.funding_history(symbol, cursor, end_ms)
            if not page:
                break
            out += [FundingRate((int(p["time"]) // HOUR_MS) * HOUR_MS, float(p["fundingRate"])) for p in page]
            last = int(page[-1]["time"])
            if len(page) < 500 or last <= cursor:
                break
            cursor = last + 1
        return out

    def mid(self, symbol: str) -> float:
        mids = self.info.all_mids()
        if symbol not in mids:
            raise VenueError("unknown_market", f"no mid for {symbol}")
        return float(mids[symbol])

    def fees(self) -> FeeSchedule:
        return self.FEES

    # ---------- account ----------

    def _address(self) -> str:
        if not self.account_address:
            raise VenueError("no_account", "account_address is required for account state")
        return self.account_address

    def account(self) -> Account:
        st = self.info.user_state(self._address())
        ms = st["marginSummary"]
        return Account(float(ms["accountValue"]), float(ms["totalMarginUsed"]), float(st["withdrawable"]))

    def positions(self) -> list[Position]:
        out = []
        for ap in self.info.user_state(self._address())["assetPositions"]:
            p = ap["position"]
            liq = p.get("liquidationPx")
            out.append(
                Position(
                    symbol=p["coin"],
                    size=float(p["szi"]),
                    entry_price=float(p["entryPx"]),
                    unrealized_pnl=float(p["unrealizedPnl"]),
                    liquidation_price=float(liq) if liq not in (None, "") else None,
                    leverage=float(p["leverage"]["value"]),
                    margin_used=float(p["marginUsed"]),
                    position_value=float(p["positionValue"]),
                )
            )
        return out

    # ---------- orders ----------

    @property
    def exchange(self):
        if self._exchange is None:
            if self._wallet is None:
                raise VenueError("read_only", "an agent_key is required to trade")
            from hyperliquid.exchange import Exchange

            self._exchange = Exchange(self._wallet, self.base_url, account_address=self.account_address)
        return self._exchange

    def set_leverage(self, symbol: str, leverage: int, isolated: bool = True) -> None:
        m = self.market(symbol)
        if not 1 <= leverage <= m.max_leverage:
            raise VenueError("bad_leverage", f"{symbol} allows 1-{m.max_leverage:g}x, got {leverage}")
        if self.dry_run:
            return
        res = self.exchange.update_leverage(int(leverage), symbol, is_cross=not isolated)
        if not isinstance(res, dict) or res.get("status") != "ok":
            raise VenueError("rejected", f"leverage update rejected: {res}", res)

    def limit_price(self, symbol: str, is_buy: bool, max_slippage: float, mid: float | None = None) -> float:
        """IOC limit price: ``max_slippage`` through the mid, 5 significant figures, at most 6 - szDecimals decimals."""
        m = self.market(symbol)
        px = (mid if mid is not None else self.mid(symbol)) * (1 + max_slippage if is_buy else 1 - max_slippage)
        return round(float(f"{px:.5g}"), 6 - m.size_decimals)

    def place_order(self, intent: OrderIntent) -> OrderResult:
        if intent.side not in ("buy", "sell"):
            raise VenueError("bad_order", "side must be 'buy' or 'sell'")
        if not 0 < intent.max_slippage < 0.2:
            raise VenueError("bad_order", "max_slippage must be between 0 and 0.2")
        m = self.market(intent.symbol)
        size = round(intent.size, m.size_decimals)
        if size <= 0:
            raise VenueError("size_too_small", f"{intent.size} rounds to 0 at {m.size_decimals} decimals for {m.symbol}")
        mid = self.mid(intent.symbol)
        if not intent.reduce_only and size * mid < m.min_notional_usd:
            raise VenueError("min_notional", f"${size * mid:.2f} is below Hyperliquid's ${m.min_notional_usd:g} minimum")
        if self.pre_trade is not None:
            self.pre_trade(intent, mid)  # raises VenueError to block

        is_buy = intent.side == "buy"
        px = self.limit_price(intent.symbol, is_buy, intent.max_slippage, mid)
        base = dict(symbol=intent.symbol, side=intent.side, requested_size=size, limit_price=px, client_id=intent.client_id)

        if self.dry_run:
            return OrderResult("dry_run", raw=self._signed_payload(intent.symbol, is_buy, size, px, intent.reduce_only, intent.client_id), **base)

        from hyperliquid.utils.types import Cloid

        cloid = Cloid(intent.client_id) if intent.client_id else None
        res = self.exchange.order(intent.symbol, is_buy, size, px, {"limit": {"tif": "Ioc"}}, intent.reduce_only, cloid)
        return parse_order_response(res, base)

    def close_position(self, symbol: str, max_slippage: float = 0.01, client_id: str | None = None) -> OrderResult | None:
        pos = next((p for p in self.positions() if p.symbol == symbol and p.size != 0), None)
        if pos is None:
            return None
        side: Literal["buy", "sell"] = "sell" if pos.size > 0 else "buy"
        return self.place_order(OrderIntent(symbol, side, abs(pos.size), reduce_only=True, max_slippage=max_slippage, client_id=client_id))

    def _signed_payload(self, symbol: str, is_buy: bool, size: float, px: float, reduce_only: bool, client_id: str | None) -> dict:
        from hyperliquid.utils.signing import get_timestamp_ms, order_request_to_order_wire, order_wires_to_order_action, sign_l1_action
        from hyperliquid.utils.types import Cloid

        req = {"coin": symbol, "is_buy": is_buy, "sz": size, "limit_px": px, "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": reduce_only}
        if client_id:
            req["cloid"] = Cloid(client_id)
        action = order_wires_to_order_action([order_request_to_order_wire(req, self.info.name_to_asset(symbol))])
        nonce = get_timestamp_ms()
        payload = {"action": action, "nonce": nonce, "posted": False}
        if self._wallet is not None:
            payload["signature"] = sign_l1_action(self._wallet, action, None, nonce, None, self.network == "mainnet")
        return payload


def parse_order_response(res: object, base: dict) -> OrderResult:
    """Map Hyperliquid's /exchange order response onto OrderResult."""
    if not isinstance(res, dict) or res.get("status") != "ok":
        return OrderResult("rejected", error=str(res.get("response") if isinstance(res, dict) else res), raw=res if isinstance(res, dict) else {}, **base)
    statuses = res.get("response", {}).get("data", {}).get("statuses", [])
    st = statuses[0] if statuses else {}
    if isinstance(st, dict) and "filled" in st:
        f = st["filled"]
        return OrderResult("filled", filled_size=float(f["totalSz"]), avg_price=float(f["avgPx"]), order_id=int(f["oid"]), raw=res, **base)
    if isinstance(st, dict) and "resting" in st:
        return OrderResult("resting", order_id=int(st["resting"]["oid"]), raw=res, **base)
    if isinstance(st, dict) and "error" in st:
        return OrderResult("rejected", error=str(st["error"]), raw=res, **base)
    return OrderResult("rejected", error=f"unrecognised response: {st!r}", raw=res, **base)
