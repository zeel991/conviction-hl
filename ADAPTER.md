# Hyperliquid venue adapter

`conviction_hl/venue.py` is a self-contained Hyperliquid perps adapter: one small interface (`Venue`) plus a Hyperliquid implementation (`HyperliquidVenue`) on the official Python SDK. It is meant to be wired into an existing broker layer as one more venue next to Robinhood. It has no dependency on the rest of this repo; only `hyperliquid-python-sdk` and `eth-account`, which comes with that SDK.

**What it does:** market data, account state and order execution.

**What it leaves to the host app:** what to trade, how much, and when. Strategy logic, sizing and risk limits stay where they are. Risk limits plug in through one hook.

## Interface

```python
class Venue(Protocol):
    name: str
    def markets(self) -> list[Market]                     # symbol, size decimals, max leverage, $ minimum
    def market(self, symbol) -> Market
    def candles(self, symbol, interval, start_ms, end_ms) -> list[Candle]
    def funding(self, symbol, start_ms, end_ms=None) -> list[FundingRate]   # hourly, paginated
    def mid(self, symbol) -> float
    def fees(self) -> FeeSchedule                         # taker / maker fractions
    def account(self) -> Account                          # equity, margin used, withdrawable
    def positions(self) -> list[Position]                 # signed size, entry, uPnL, liquidation price, leverage
    def set_leverage(self, symbol, leverage, isolated=True) -> None
    def place_order(self, intent: OrderIntent) -> OrderResult
    def close_position(self, symbol, max_slippage=0.01, client_id=None) -> OrderResult | None
```

**Units:**
- Timestamps are Unix milliseconds, UTC.
- Prices are in USD. Sizes are in coin units.
- Funding is a fraction per hour; positive means longs pay.
- Position size is signed: positive is long, negative is short.

## Using it

```python
from conviction_hl.venue import HyperliquidVenue, OrderIntent, VenueError, new_client_id

# Read-only: market data for backtests and signals. No key needed.
hl = HyperliquidVenue("mainnet")
bars = hl.candles("BTC", "4h", start_ms, end_ms)
funding = hl.funding("BTC", start_ms)

# Trading for a user. agent_key is an API wallet the user approved on Hyperliquid;
# account_address is the user's account it trades for.
hl = HyperliquidVenue(
    "testnet",
    agent_key=user.hl_agent_key,
    account_address=user.hl_address,
    dry_run=False,               # default True: build and sign, don't send
    pre_trade=your_risk_check,   # (intent, mid) -> raise VenueError to block
)
hl.set_leverage("ETH", 3)
result = hl.place_order(OrderIntent("ETH", "buy", size=0.05, client_id=new_client_id()))
if result.status == "filled":
    record_fill(result.filled_size, result.avg_price, result.order_id)
```

**Orders:**
- Every order is an IOC limit priced `max_slippage` through the mid, which is how the official SDK places market orders.
- Sizes are rounded to the market's size decimals.
- Prices are rounded to Hyperliquid's tick rules: 5 significant figures, and at most 6 − szDecimals decimals.

**Idempotency.** `client_id` (0x plus 32 hex characters) is sent as Hyperliquid's `cloid`. Keep the same id when retrying after a timeout, so the retry can't double the order.

## Risk limits

`pre_trade` sees each `OrderIntent` and the current mid before anything is built or signed. To block an order, raise `VenueError`.

This repo's guardrails plug in like this (`conviction_hl/guardrails.py`):

```python
from conviction_hl import guardrails as gr

hook = gr.pre_trade_hook(strategy.guardrails, "ETH", leverage=3,
                         state=lambda: gr.AccountState(equity, day_start_equity, bars_since_exit, funding_8h))
hl = HyperliquidVenue("mainnet", agent_key=..., account_address=..., dry_run=False, pre_trade=hook)
```

**Exits are never blocked.** The hook passes `reduce_only` orders through, because a limit that traps a user in a losing position is worse than no limit.

**Moving funds needs the master key.** Hyperliquid API wallets sign orders ("L1 actions"). Withdrawals and USDC transfers are user-signed actions that need the master key. So the key the host stores can trade, but this adapter has no path to move funds.

## Errors

Every failure the caller should handle raises `VenueError` with a stable `code`:

| code | When |
|---|---|
| `unknown_market` | The symbol isn't a listed (non-delisted) perp |
| `min_notional` | Opening order under Hyperliquid's $10 minimum (reduce-only orders are exempt) |
| `size_too_small` | The size rounds to zero at the market's decimals |
| `bad_order` | Invalid side or slippage |
| `bad_leverage` | Leverage outside 1 to the market's maximum |
| `guardrail` | The `pre_trade` hook blocked the order; `detail` holds the reasons |
| `read_only` | A real order was attempted without an `agent_key` |
| `no_account` | Account state requested without an `account_address` |
| `rejected` | A leverage update was refused |

A rejected **order** doesn't raise. It comes back as `OrderResult(status="rejected", error=...)`, carrying Hyperliquid's message, for example "Order must have minimum value of $10." or "User or API Wallet does not exist."

## Testing

`tests/test_venue.py` checks the adapter against fake SDK clients. The response shapes are copied from Hyperliquid's API docs. The tests cover:
- Rounding, price ticks, the minimum notional, and reduce-only exemptions.
- Every order response type: filled, resting, per-order error, and request-level error.
- Position and account parsing, and close-position direction and size.
- Read-only protection, `cloid` placement, and the guardrail hook blocking before an order is built.

`HL_NETWORK_TESTS=1 uv run pytest -k live` also reads live testnet: market specs, the mid, 24h of candles and funding, and a dry-run order.

**Not yet verified:** a real order posted to testnet. That needs a funded testnet account and an approved API wallet. The response parsing for that path is tested against the documented shapes only.

## Out of scope for now

- Spot markets, HIP-3 builder-deployed perps, cross-margin portfolio views.
- Resting limit orders, and cancel/modify.
- WebSocket streaming. The adapter polls; the SDK's WebSocket manager is the natural next step.
- Per-user fee tiers. `fees()` returns the base tier.
