"""Contract tests for HyperliquidVenue, with fake SDK clients. Set HL_NETWORK_TESTS=1 to also read live testnet."""

import os

import pytest
from eth_account import Account as EthAccount

from conviction_hl import guardrails as gr
from conviction_hl.broker import HyperliquidBroker
from conviction_hl.spec import Guardrails
from conviction_hl.venue import HyperliquidVenue, OrderIntent, VenueError, new_client_id

ADDR = "0x" + "11" * 20


class FakeInfo:
    def __init__(self, mid=2000.0, positions=()):
        self.mids = {"ETH": str(mid), "BTC": "80000"}
        self.positions = list(positions)
        self.meta_calls = 0

    def meta(self):
        self.meta_calls += 1
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}, {"name": "OLD", "szDecimals": 2, "maxLeverage": 3, "isDelisted": True}]}

    def name_to_asset(self, name):
        return {"BTC": 0, "ETH": 1}[name]

    def all_mids(self):
        return self.mids

    def user_state(self, address):
        return {
            "assetPositions": [{"position": p, "type": "oneWay"} for p in self.positions],
            "marginSummary": {"accountValue": "1500.5", "totalMarginUsed": "120.25"},
            "withdrawable": "1380.25",
        }

    def candles_snapshot(self, name, interval, start, end):
        return [{"t": 0, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"}]

    def funding_history(self, name, start, end=None):
        if start > 0:
            return []
        return [{"time": 3_600_000 * i + 7, "fundingRate": "0.0000125"} for i in range(500)]


class FakeExchange:
    def __init__(self, response):
        self.response = response
        self.orders = []

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
        self.orders.append(dict(name=name, is_buy=is_buy, sz=sz, px=limit_px, type=order_type, reduce_only=reduce_only, cloid=cloid))
        return self.response

    def update_leverage(self, leverage, name, is_cross=True):
        return {"status": "ok"}


ETH_LONG = {"coin": "ETH", "szi": "0.0335", "entryPx": "2986.3", "unrealizedPnl": "-0.0134", "liquidationPx": "2866.26936529",
            "leverage": {"type": "isolated", "value": 20}, "marginUsed": "4.967826", "positionValue": "100.02765"}
FILLED = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.05", "avgPx": "2001.5", "oid": 7}}]}}}


def venue(**kw):
    kw.setdefault("info", FakeInfo())
    return HyperliquidVenue("testnet", **kw)


def test_markets_skip_delisted_and_are_cached():
    v = venue()
    assert [m.symbol for m in v.markets()] == ["BTC", "ETH"]
    v.market("ETH"), v.market("BTC")
    assert v.info.meta_calls == 1
    with pytest.raises(VenueError) as e:
        v.market("OLD")
    assert e.value.code == "unknown_market"


def test_market_data_units():
    v = venue()
    assert v.candles("ETH", "1h", 0, 1)[0].close == 1.5
    f = v.funding("ETH", 0)
    assert len(f) == 500 and f[0].ts_ms == 0 and f[0].rate == 0.0000125  # snapped to the hour


def test_account_and_positions_parse_docs_example():
    v = venue(account_address=ADDR, info=FakeInfo(positions=[ETH_LONG]))
    a = v.account()
    assert (a.equity, a.margin_used, a.withdrawable) == (1500.5, 120.25, 1380.25)
    p = v.positions()[0]
    assert (p.symbol, p.size, p.leverage, p.liquidation_price) == ("ETH", 0.0335, 20.0, 2866.26936529)


def test_account_needs_an_address():
    with pytest.raises(VenueError) as e:
        venue().account()
    assert e.value.code == "no_account"


def test_order_rounding_and_price():
    v = venue(agent_key=EthAccount.create().key.hex())
    r = v.place_order(OrderIntent("ETH", "buy", 0.123456, max_slippage=0.01))
    assert r.status == "dry_run" and r.requested_size == 0.1235
    assert r.limit_price == 2020.0
    wire = r.raw["action"]["orders"][0]
    assert wire == {"a": 1, "b": True, "p": "2020", "s": "0.1235", "r": False, "t": {"limit": {"tif": "Ioc"}}}
    assert set(r.raw["signature"]) == {"r", "s", "v"} and r.raw["posted"] is False


def test_client_id_goes_on_the_wire():
    cid = new_client_id()
    r = venue(agent_key=EthAccount.create().key.hex()).place_order(OrderIntent("ETH", "sell", 0.1, client_id=cid))
    assert r.raw["action"]["orders"][0]["c"] == cid


def test_rejects_below_minimum_and_zero_size():
    v = venue()
    with pytest.raises(VenueError) as e:
        v.place_order(OrderIntent("ETH", "buy", 0.004))  # $8
    assert e.value.code == "min_notional"
    with pytest.raises(VenueError) as e:
        v.place_order(OrderIntent("ETH", "buy", 0.00001))
    assert e.value.code == "size_too_small"
    # Closing a small position is allowed below the minimum.
    assert v.place_order(OrderIntent("ETH", "sell", 0.004, reduce_only=True)).status == "dry_run"


def test_read_only_cannot_trade_for_real():
    v = venue(dry_run=False)
    with pytest.raises(VenueError) as e:
        v.place_order(OrderIntent("ETH", "buy", 0.1))
    assert e.value.code == "read_only"


@pytest.mark.parametrize(
    "response,status,error",
    [
        (FILLED, "filled", None),
        ({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 9}}]}}}, "resting", None),
        ({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10."}]}}}, "rejected", "Order must have minimum value of $10."),
        ({"status": "err", "response": "User or API Wallet does not exist."}, "rejected", "User or API Wallet does not exist."),
    ],
)
def test_parses_exchange_responses(response, status, error):
    ex = FakeExchange(response)
    r = venue(dry_run=False, exchange=ex).place_order(OrderIntent("ETH", "buy", 0.05))
    assert r.status == status and r.error == error
    if status == "filled":
        assert (r.filled_size, r.avg_price, r.order_id) == (0.05, 2001.5, 7)
    assert ex.orders[0]["type"] == {"limit": {"tif": "Ioc"}}


def test_close_position_sends_opposite_reduce_only():
    ex = FakeExchange(FILLED)
    v = venue(dry_run=False, exchange=ex, account_address=ADDR, info=FakeInfo(positions=[ETH_LONG]))
    v.close_position("ETH")
    o = ex.orders[0]
    assert (o["is_buy"], o["sz"], o["reduce_only"]) == (False, 0.0335, True)
    assert venue(account_address=ADDR).close_position("ETH") is None


def test_guardrail_hook_blocks_before_anything_is_built():
    g = Guardrails(max_position_usd=150, max_leverage=3)
    hook = gr.pre_trade_hook(g, "ETH", leverage=2, state=lambda: gr.AccountState(1000, 1000))
    ex = FakeExchange(FILLED)
    v = venue(dry_run=False, exchange=ex, pre_trade=hook)
    with pytest.raises(VenueError) as e:
        v.place_order(OrderIntent("ETH", "buy", 0.1))  # $200
    assert e.value.code == "guardrail" and not ex.orders
    assert v.place_order(OrderIntent("ETH", "buy", 0.05)).status == "filled"  # $100


def test_broker_wraps_the_venue():
    b = HyperliquidBroker("", ADDR, venue=venue(dry_run=False, exchange=FakeExchange(FILLED)))
    f = b.market_order("ETH", True, 0.05)
    assert (f.size, f.price, b.position) == (0.05, 2001.5, 0.05)


@pytest.mark.skipif(os.environ.get("HL_NETWORK_TESTS") != "1", reason="set HL_NETWORK_TESTS=1 to read live testnet")
def test_live_testnet_reads():
    v = HyperliquidVenue("testnet", agent_key=EthAccount.create().key.hex())
    eth = v.market("ETH")
    assert eth.size_decimals >= 0 and eth.max_leverage > 1
    assert v.mid("ETH") > 0
    import time

    now = int(time.time() * 1000)
    assert len(v.candles("ETH", "1h", now - 24 * 3_600_000, now)) >= 20
    assert len(v.funding("ETH", now - 24 * 3_600_000)) >= 20
    r = v.place_order(OrderIntent("ETH", "buy", 20 / v.mid("ETH")))
    assert r.status == "dry_run" and r.raw["posted"] is False
