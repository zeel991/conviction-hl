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
    def __init__(self, agent_key: str, account_address: str, network: str = "testnet", dry_run: bool = True, slippage: float = 0.01):
        from eth_account import Account
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        if network not in ("testnet", "mainnet"):
            raise ValueError("network must be 'testnet' or 'mainnet'")
        self.base_url = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
        self.is_mainnet = network == "mainnet"
        self.wallet = Account.from_key(agent_key)
        self.account_address = account_address
        self.dry_run = dry_run
        self.slippage = slippage
        self.info = Info(self.base_url, skip_ws=True)
        self._exchange = None
        self.position = 0.0
        self.fills: list[Fill] = []

    @property
    def exchange(self):
        if self._exchange is None:
            from hyperliquid.exchange import Exchange

            self._exchange = Exchange(self.wallet, self.base_url, account_address=self.account_address)
        return self._exchange

    def mid(self, coin: str) -> float:
        return float(self.info.all_mids()[coin])

    def equity(self, coin: str, dry_run_equity: float = 1_000.0) -> float:
        if self.dry_run:
            return dry_run_equity
        return float(self.info.user_state(self.account_address)["marginSummary"]["accountValue"])

    def sz_decimals(self, coin: str) -> int:
        return self.info.asset_to_sz_decimals[self.info.name_to_asset(coin)]

    def limit_price(self, coin: str, is_buy: bool) -> float:
        """Same rounding as the SDK: 5 significant figures, at most 6 - szDecimals decimals."""
        px = self.mid(coin) * (1 + self.slippage if is_buy else 1 - self.slippage)
        return round(float(f"{px:.5g}"), 6 - self.sz_decimals(coin))

    def build_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool) -> tuple[dict, dict, int]:
        from hyperliquid.utils.signing import get_timestamp_ms, order_request_to_order_wire, order_wires_to_order_action, sign_l1_action

        sz = round(size, self.sz_decimals(coin))
        if sz <= 0:
            raise ValueError(f"order size rounds to zero at {self.sz_decimals(coin)} decimals")
        request = {"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": self.limit_price(coin, is_buy), "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": reduce_only}
        action = order_wires_to_order_action([order_request_to_order_wire(request, self.info.name_to_asset(coin))])
        nonce = get_timestamp_ms()
        signature = sign_l1_action(self.wallet, action, None, nonce, None, self.is_mainnet)
        return action, signature, nonce

    def set_leverage(self, coin: str, leverage: int) -> dict | None:
        if self.dry_run:
            return None
        return self.exchange.update_leverage(leverage, coin, is_cross=False)

    def market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False) -> Fill:
        action, signature, nonce = self.build_order(coin, is_buy, size, reduce_only)
        wire = action["orders"][0]
        if self.dry_run:
            raw = {"posted": False, "action": action, "nonce": nonce, "signature": signature}
        else:
            raw = self.exchange.post("/exchange", {"action": action, "nonce": nonce, "signature": signature, "vaultAddress": None, "expiresAfter": None})
            status = raw.get("response", {}).get("data", {}).get("statuses", [{}])[0] if isinstance(raw, dict) else {}
            if raw.get("status") != "ok" or "error" in status:
                raise RuntimeError(f"Hyperliquid rejected the order: {raw}")
        px = float(wire["p"])
        size = float(wire["s"])
        self.position = 0.0 if reduce_only else (size if is_buy else -size)
        fill = Fill(coin, is_buy, size, px, 0.0, reduce_only, raw)
        self.fills.append(fill)
        return fill
