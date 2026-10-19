import numpy as np
import pandas as pd
import pytest

from conviction_hl import backtest as bt
from conviction_hl import guardrails as gr
from conviction_hl.spec import Condition, Guardrails, Strategy

H = 3_600_000


def bars(closes, opens=None, highs=None, lows=None, funding=0.0, hours=24):
    n = len(closes)
    closes = np.asarray(closes, float)
    opens = np.asarray(opens if opens is not None else closes, float)
    return pd.DataFrame(
        {
            "ts": np.arange(n, dtype="int64") * hours * H,
            "open": opens,
            "high": np.asarray(highs if highs is not None else np.maximum(opens, closes), float),
            "low": np.asarray(lows if lows is not None else np.minimum(opens, closes), float),
            "close": closes,
            "volume": np.ones(n),
            "funding": np.full(n, funding) if np.isscalar(funding) else np.asarray(funding, float),
            "funding_8h": np.zeros(n),
        }
    )


def strat(**kw):
    base = dict(name="t", idea="t", coin="BTC", interval="1d", entry=["close > 100"], guardrails=Guardrails(max_position_usd=1e9, max_leverage=50))
    base.update(kw)
    return Strategy(**base)


def test_condition_parsing():
    c = Condition.parse("close crosses_above sma(50)")
    assert (c.left.key(), c.op, c.right.key()) == ("close", "crosses_above", "sma(50)")
    assert str(Condition.parse("funding_8h >= 0.0003")) == "funding_8h >= 0.0003"
    with pytest.raises(ValueError):
        Condition.parse("close above sma(50)")
    with pytest.raises(ValueError):
        Condition.parse("close > macd(12)")


def test_honest_fills_next_open_naive_fills_signal_close():
    # Signal at bar 1 close (101); next open is 110.
    df = bars([99, 101, 120, 120], opens=[99, 99, 110, 120])
    s = strat(max_hold_bars=1)
    naive = bt.run(df, s, bt.NAIVE)
    honest = bt.run(df, s, bt.Costs(taker_fee=0, slippage_bps=0, funding=False))
    assert naive.trades[0].entry_px == pytest.approx(101)
    assert honest.trades[0].entry_px == pytest.approx(110)


def test_fees_and_slippage_are_charged_both_ways():
    df = bars([99, 101, 101, 101, 101])
    s = strat(max_hold_bars=1)
    r = bt.run(df, s, bt.Costs(taker_fee=0.001, slippage_bps=10, funding=False))
    t = r.trades[0]
    assert t.entry_px == pytest.approx(101 * 1.001)
    assert t.exit_px == pytest.approx(101 * 0.999)
    assert t.fees == pytest.approx(10_000 * 0.001 + t.notional / t.entry_px * t.exit_px * 0.001)
    assert r.equity.iloc[-1] < 10_000


def test_funding_sign_long_pays_short_earns():
    df = bars([99, 101, 101, 101, 101], funding=0.001)
    long_r = bt.run(df, strat(max_hold_bars=2), bt.Costs(taker_fee=0, slippage_bps=0))
    short_r = bt.run(df, strat(side="short", max_hold_bars=2), bt.Costs(taker_fee=0, slippage_bps=0))
    assert long_r.trades[0].funding > 0
    assert short_r.trades[0].funding < 0
    assert long_r.equity.iloc[-1] < 10_000 < short_r.equity.iloc[-1]


def test_gap_through_stop_fills_at_open_not_stop():
    # Enter at bar 2 open = 101; stop 5% = 95.95. Bar 3 opens at 80.
    df = bars([99, 101, 101, 85, 85], opens=[99, 99, 101, 80, 85])
    r = bt.run(df, strat(stop_loss_pct=5), bt.Costs(taker_fee=0, slippage_bps=0, funding=False))
    assert r.trades[0].reason == "stop (gap)"
    assert r.trades[0].exit_px == pytest.approx(80)


def test_stop_checked_before_take_profit_in_same_bar():
    df = bars([99, 101, 101, 101], opens=[99, 99, 101, 101], highs=[99, 101, 101, 120], lows=[99, 99, 101, 90])
    r = bt.run(df, strat(stop_loss_pct=5, take_profit_pct=10), bt.Costs(taker_fee=0, slippage_bps=0, funding=False))
    assert r.trades[0].reason == "stop"


def test_liquidation_loses_margin():
    # 10x long from 101; liquidation near 101 * (1 - 0.1 + 1/80) = 92.17. Bar 3 low 90.
    df = bars([99, 101, 101, 95, 95], opens=[99, 99, 101, 100, 95], lows=[99, 99, 101, 90, 95])
    r = bt.run(df, strat(leverage=10), bt.Costs(taker_fee=0, slippage_bps=0, funding=False), coin_max_leverage=40)
    assert r.liquidations == 1
    assert r.trades[0].gross_pnl == pytest.approx(-10_000)


def test_guardrails_block_and_never_block_exits():
    g = Guardrails(max_position_usd=500, max_leverage=2, allow_short=False, max_funding_8h_against=0.0003)
    st = gr.AccountState(equity=1000, day_start_equity=1000, funding_8h=0.001)
    d = gr.check(gr.Order("ETH", False, 900, 5), st, g, "BTC")
    assert not d.allowed and len(d.reasons) == 4  # coin, short, size, leverage (shorts earn positive funding)
    assert gr.check(gr.Order("ETH", False, 900, 5, reduce_only=True), st, g, "BTC").allowed
    d2 = gr.check(gr.Order("BTC", True, 100, 1), st, g, "BTC")
    assert not d2.allowed and "funding" in d2.reasons[0]


def test_daily_loss_limit_blocks_entries():
    g = Guardrails(max_daily_loss_pct=5)
    d = gr.check(gr.Order("BTC", True, 100, 1), gr.AccountState(940, 1000), g, "BTC")
    assert not d.allowed and "daily loss" in d.reasons[0]
