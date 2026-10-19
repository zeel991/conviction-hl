# conviction-hl: Hyperliquid for Conviction-style trading agents

Conviction lets anyone describe a trading idea in plain English, backtest it, paper trade it and deploy it as an agent with guardrails. Crypto is "coming soon" on its site. This is a working sketch of crypto perps on **Hyperliquid**, built around the same flow:

```
idea  ->  rules  ->  honest backtest + "where it breaks"  ->  paper trading  ->  live, through guardrails
```

Why Hyperliquid:
- **Fully public market data.** Candles, hourly funding and contract specs come from one open endpoint, with no keys and no brokerage onboarding.
- **API wallets.** An API ("agent") wallet signs orders for a user's account. Withdrawals and USDC transfers are a different, user-signed action that needs the master key. That separation is a natural fit for "the agent can trade, inside limits, and nothing else".

## What's in it

| Piece | File | What it does |
|---|---|---|
| Rules format | `spec.py` | Strategies as plain data: entry/exit conditions such as `close crosses_above sma(50)` or `funding_8h > 0.0003`, plus stops, take-profit, trailing stop, max hold, sizing, leverage. |
| Idea compiler | `compile.py` | Plain English to rules with Claude (Opus 5.5, schema-constrained JSON, one retry on a validation error). **Guardrails never come from the model.** |
| Market data | `data.py` | Hyperliquid candles plus hourly funding per bar, cached. Bars without funding history are dropped rather than assumed to be zero. |
| Backtester | `backtest.py` | Runs every strategy twice: **naive** (fill at the signal close, no costs) and **honest** (next-bar fills, 0.045% taker fee, slippage, hourly funding, intrabar and gap-through stops, isolated-margin liquidation). |
| Where it breaks | `robustness.py`, `report.py` | Cost stress, a 70/30 time split, P&L by market regime, every setting nudged ±10–25%, and plain-language flags. Markdown report and chart per strategy. |
| Guardrails | `guardrails.py` | One deterministic check that gates backtest, paper and live orders: allowed coins, max position, max leverage, daily loss limit, shorting, cooldown, funding ceiling and human approval. Orders that close a position are never blocked. |
| Paper and live | `live.py`, `broker.py` | The same loop for both. A paper broker on live mids, or Hyperliquid through an API wallet. **Live is dry-run by default**: orders are built and signed but not sent. |
| **Venue adapter** | `venue.py`, [ADAPTER.md](ADAPTER.md) | **The piece meant to be lifted into a host app.** A small `Venue` interface and a Hyperliquid implementation of it: markets, candles, funding, mid, fees, account, positions, leverage, orders and closes, with stable error codes, idempotent client order ids and a `pre_trade` hook for risk limits. No dependency on the rest of this repo. |

The guardrails follow the list in Conviction's own FAQ: max position size, max exposure, max daily loss, allowed assets, stop-loss and take-profit rules, leverage and shorting restrictions, cooldowns and human approval.

## Results on three ideas

All three use Hyperliquid data and start with $10,000. Full reports with charts are in `reports/`.

| Idea | Period | Naive | Honest | Buy & hold | The main finding |
|---|---|---|---|---|---|
| **Conviction's own homepage example, on BTC.** "Buy when it reclaims its 50-day average on rising volume. Exit on a 4% trailing stop." | May 2023 – Oct 2026 | +26.3% | **+16.1%** | +196.5% | Naive overstates the return by 10 points. All of the profit comes in uptrends, and it misses most of the bull run. |
| **Fade crowded ETH longs.** "Short when funding is above 0.03% per 8h and price is below its 20-period EMA." | Jul 2024 – Oct 2026 | −9.9% | **+4.0%** | — | Only 14 trades; half of the nearby settings flip the result's sign; no trades after Feb 2026. Funding paid the strategy $190. |
| **SOL RSI dip at 3×.** "Buy when 4h RSI drops below 30. Take profit 6%, stop 4%." | Jul 2024 – Oct 2026 | −96.0% | **−97.5%** | −26.2% | 48 of 64 trades hit the stop. At 3× leverage, buying RSI dips catches falling knives (5 stops in a row in the August 2024 crash). |

The ETH row shows why these reports flag small samples. With fees doubled and slippage tripled, the return *rose* to +11.1%. That's because a stop hit one bar earlier, which freed the strategy to take a winning trade the base run skipped. One trade moved the result by 7 points.

## What has been checked

- **31 tests pass**, plus a live read check against Hyperliquid testnet for the venue adapter. They include:
  - Next-bar fills, fees and slippage in both directions.
  - Funding sign: longs pay positive funding, shorts receive it.
  - Gap-through stops fill at the open; a stop is assumed to hit before a take-profit in the same bar.
  - Liquidation loses the margin.
  - Every guardrail, including "never block an exit".
  - The live loop's entry, funding block and declined approval.
  - The compiler's request shape and retry, using a stand-in client.
  - The venue adapter against Hyperliquid's documented responses: rounding, the $10 minimum, filled, resting and rejected orders, positions, closes, and guardrails blocking before an order is built.
- **Live paper session on real BTC prices** (11 minutes, 2026-10-19 20:05–20:16 UTC). Three round trips of about $1,000 each: every entry fired on a closed 5-minute bar and passed the guardrails, and every position exited one bar later on "max hold". Net P&L was −$3.03, which matches about $1.30 per round trip in slippage and taker fees. The log is in `logs/paper_smoke.jsonl`.
- **Dry run against Hyperliquid testnet.** Metadata and prices came from testnet, and the guardrails blocked an oversized order. A $400 ETH buy became an IOC limit order 1% through the mid, with size rounded to ETH's 4 decimals, signed with a throwaway key and not sent.

## Not checked yet

- **Sending orders.** No order has been posted to testnet or mainnet; that needs a funded testnet account and an approved API wallet. `conviction-hl live … --send` is the path.
- **Compiling with Claude.** The compiler hasn't run against the real Claude API, because no Anthropic credentials were available where this was built.

## Simplifications (all erring toward caution, or noted)

- **Funding:** charged on the bar's close price, not Hyperliquid's oracle price.
- **Exit bars:** a position closed intrabar pays no funding for that bar.
- **Liquidation:** uses isolated margin, with maintenance margin at half of the coin's max-leverage initial margin.
- **History length:** Hyperliquid serves the 5,000 most recent candles per interval, so 4h history starts in July 2024.
- **Fills:** no partial fills or queue position; every fill is taker.

## Run it

```bash
uv sync
uv run conviction-hl report strategies/*.json                 # naive vs honest + where it breaks
uv run conviction-hl paper strategies/btc_reclaim_50d.json --minutes 60
uv run conviction-hl compile "Short ETH when funding is extreme and price loses its 20 EMA"   # needs Anthropic credentials

# Live, dry run by default. The agent key is an API wallet approved by HL_ACCOUNT_ADDRESS.
HL_AGENT_KEY=0x... HL_ACCOUNT_ADDRESS=0x... uv run conviction-hl live strategies/demo/btc_5m_smoke.json --network testnet
# add --send to post orders; mainnet additionally needs HL_CONFIRM_MAINNET=yes
uv run pytest
```
