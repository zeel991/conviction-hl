import json
from types import SimpleNamespace

import pytest

from conviction_hl.compile import _schema, compile_idea
from conviction_hl.spec import Guardrails

GOOD = {
    "name": "BTC reclaim",
    "idea": "Buy BTC when it reclaims its 50-day average on rising volume. Exit on a 4% trailing stop.",
    "coin": "BTC",
    "interval": "1d",
    "side": "long",
    "entry": ["close crosses_above sma(50)", "volume > volume_sma(20)"],
    "exit": [],
    "stop_loss_pct": None,
    "take_profit_pct": None,
    "trailing_stop_pct": 4,
    "max_hold_bars": None,
    "position_pct": 100,
    "leverage": 1,
}


class FakeClient:
    """Stands in for anthropic.Anthropic: returns queued JSON replies and records requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        text = json.dumps(self.replies.pop(0))
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text=text)])


def test_schema_is_strict():
    s = _schema()
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])


def test_compiles_and_keeps_callers_guardrails():
    g = Guardrails(max_position_usd=123, max_leverage=2)
    client = FakeClient({**GOOD, "leverage": 1})
    s = compile_idea(GOOD["idea"], g, client)
    assert s.entry == GOOD["entry"] and s.trailing_stop_pct == 4
    assert s.guardrails.max_position_usd == 123  # never taken from the model
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["fallbacks"] == "default"


def test_retries_once_with_the_validation_error():
    bad = {**GOOD, "entry": ["close above sma(50)"]}
    client = FakeClient(bad, GOOD)
    s = compile_idea(GOOD["idea"], client=client)
    assert s.entry == GOOD["entry"]
    assert "failed validation" in client.requests[1]["messages"][-1]["content"]


def test_gives_up_after_second_invalid_spec():
    bad = {**GOOD, "entry": ["close > macd(12)"]}
    with pytest.raises(ValueError):
        compile_idea(GOOD["idea"], client=FakeClient(bad, bad))
