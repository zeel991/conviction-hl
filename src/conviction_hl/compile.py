"""Natural language -> Strategy, with Claude.

The model writes the trading rules only. Guardrails are never taken from the
model: they come from the caller, so a prompt cannot raise its own limits.
The output is schema-constrained JSON, then validated by Strategy itself; one
retry feeds the validation error back.
"""

from __future__ import annotations

import json

import anthropic

from .spec import FIELDS, INDICATORS, INTERVALS, OPERATORS, Guardrails, Strategy

MODEL = "claude-opus-5-5"

SYSTEM = f"""You compile a trader's idea into rules for a Hyperliquid perpetual-futures backtester.

Conditions are strings of the form "<a> <op> <b>".
- Operands: a number; a field from {sorted(FIELDS)}; or an indicator from {sorted(INDICATORS)} written with one integer period, like sma(50).
- funding is the hourly funding rate settled in the bar, as a fraction (0.0001 = 0.01%). funding_8h is the trailing 8-hour sum, the figure traders usually quote. Positive funding means longs pay shorts.
- return(n) is the percent change over n bars. volume_sma(n) is the average volume.
- Operators: {list(OPERATORS)}. "crosses_above" fires only on the bar where a moves from at-or-below b to above it.
- entry conditions must all hold; exit conditions close the position if any holds. Use the dedicated fields for stop loss, take profit, trailing stop and max hold rather than exit conditions.
- interval is one of {sorted(INTERVALS)}. Pick the one the idea implies; default to 4h for intraday swing ideas and 1d for trend ideas.
- coin is the Hyperliquid symbol (BTC, ETH, SOL, HYPE, ...).
- position_pct is the share of equity committed before leverage. leverage defaults to 1 unless the idea states one.
- Use null for anything the idea does not specify. Do not invent exits the trader did not ask for.
- Copy the trader's words into "idea" verbatim."""


def _schema() -> dict:
    nullable_num = {"type": ["number", "null"]}
    props = {
        "name": {"type": "string"},
        "idea": {"type": "string"},
        "coin": {"type": "string"},
        "interval": {"type": "string", "enum": sorted(INTERVALS)},
        "side": {"type": "string", "enum": ["long", "short"]},
        "entry": {"type": "array", "items": {"type": "string"}},
        "exit": {"type": "array", "items": {"type": "string"}},
        "stop_loss_pct": nullable_num,
        "take_profit_pct": nullable_num,
        "trailing_stop_pct": nullable_num,
        "max_hold_bars": {"type": ["integer", "null"]},
        "position_pct": {"type": "number"},
        "leverage": {"type": "number"},
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def compile_idea(idea: str, guardrails: Guardrails | None = None, client: anthropic.Anthropic | None = None) -> Strategy:
    client = client or anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": idea}]
    for attempt in range(2):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=messages,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _schema()}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined to compile this idea: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("compile ran out of tokens")
        text = next(b.text for b in response.content if b.type == "text")
        spec = json.loads(text)
        try:
            return Strategy.from_dict({**spec, "guardrails": guardrails or Guardrails()})
        except ValueError as e:
            if attempt == 1:
                raise
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"That spec failed validation: {e}. Return a corrected spec."},
            ]
    raise AssertionError("unreachable")
