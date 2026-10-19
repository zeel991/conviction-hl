import numpy as np
import pandas as pd

from conviction_hl import live
from conviction_hl.broker import PaperBroker
from conviction_hl.spec import Guardrails, Strategy


def _bars(n=60, close=100.0, funding_8h=0.0):
    ts = np.arange(n, dtype="int64") * 300_000
    df = pd.DataFrame({"ts": ts, "open": close, "high": close, "low": close, "close": close, "volume": 1.0, "funding": 0.0, "funding_8h": funding_8h})
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


class FakePaper(PaperBroker):
    def __init__(self, px=100.0):
        super().__init__()
        self.px = px

    def mid(self, coin):
        return self.px


def _strategy(**g):
    return Strategy(name="t", idea="t", coin="BTC", interval="5m", entry=["close > 0"], max_hold_bars=1, position_pct=10,
                    guardrails=Guardrails(max_position_usd=5000, max_leverage=3, **g))


def test_live_loop_enters_through_guardrails(monkeypatch):
    monkeypatch.setattr(live.data, "recent", lambda *a, **k: _bars())
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    b = FakePaper()
    events = live.run(_strategy(), b, minutes=0.0005, poll_seconds=0, approve=lambda _: True)
    kinds = [e["event"] for e in events]
    assert "entry" in kinds and kinds[-1] == "stop"
    assert b.fills[0].size * b.fills[0].price == __import__("pytest").approx(1000, rel=1e-3)


def test_live_loop_blocks_on_funding_guardrail(monkeypatch):
    monkeypatch.setattr(live.data, "recent", lambda *a, **k: _bars(funding_8h=0.01))
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    b = FakePaper()
    events = live.run(_strategy(max_funding_8h_against=0.0005), b, minutes=0.0005, poll_seconds=0)
    assert any(e["event"] == "blocked" and "funding" in e["reasons"][0] for e in events)
    assert not b.fills


def test_live_loop_respects_declined_approval(monkeypatch):
    monkeypatch.setattr(live.data, "recent", lambda *a, **k: _bars())
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    b = FakePaper()
    events = live.run(_strategy(require_approval=True), b, minutes=0.0005, poll_seconds=0, approve=lambda _: False)
    assert any(e["event"] == "declined" for e in events) and not b.fills
