"""Strategy and guardrail spec: the structured form a natural-language idea compiles to.

Conditions are small strings such as ``"close crosses_above sma(50)"`` or
``"funding_8h > 0.0003"``. Operands are a price field, an indicator call, or a
number; operators are comparisons or crosses. Everything here is plain data so
a spec can round-trip through JSON and an LLM can emit it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

FIELDS = {"open", "high", "low", "close", "volume", "funding", "funding_8h"}
# name -> number of integer arguments
INDICATORS = {"sma": 1, "ema": 1, "rsi": 1, "atr": 1, "highest": 1, "lowest": 1, "volume_sma": 1, "return": 1}
OPERATORS = (">=", "<=", ">", "<", "crosses_above", "crosses_below")
INTERVALS = {"5m": 5 / 60, "15m": 0.25, "1h": 1, "4h": 4, "1d": 24}  # hours per bar

_OPERAND = re.compile(r"^(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<field>[a-z_0-9]+)|(?P<fn>[a-z_]+)\((?P<arg>\d+)\))$")


@dataclass(frozen=True)
class Operand:
    kind: str  # "num" | "field" | "ind"
    name: str = ""
    arg: int = 0
    value: float = 0.0

    @staticmethod
    def parse(text: str) -> "Operand":
        m = _OPERAND.match(text.strip())
        if not m:
            raise ValueError(f"cannot parse operand {text!r}")
        if m["num"] is not None:
            return Operand("num", value=float(m["num"]))
        if m["fn"] is not None:
            if m["fn"] not in INDICATORS:
                raise ValueError(f"unknown indicator {m['fn']!r}; known: {sorted(INDICATORS)}")
            arg = int(m["arg"])
            if arg < 1:
                raise ValueError(f"{m['fn']} needs a period of at least 1")
            return Operand("ind", name=m["fn"], arg=arg)
        if m["field"] not in FIELDS:
            raise ValueError(f"unknown field {m['field']!r}; known: {sorted(FIELDS)}")
        return Operand("field", name=m["field"])

    def key(self) -> str:
        return {"num": f"{self.value:g}", "field": self.name, "ind": f"{self.name}({self.arg})"}[self.kind]


@dataclass(frozen=True)
class Condition:
    left: Operand
    op: str
    right: Operand

    @staticmethod
    def parse(text: str) -> "Condition":
        for op in sorted(OPERATORS, key=len, reverse=True):
            parts = re.split(rf"\s+{re.escape(op)}\s+", text.strip())
            if len(parts) == 2:
                return Condition(Operand.parse(parts[0]), op, Operand.parse(parts[1]))
        raise ValueError(f"cannot parse condition {text!r}; use '<a> <op> <b>' with op in {OPERATORS}")

    def __str__(self) -> str:
        return f"{self.left.key()} {self.op} {self.right.key()}"


@dataclass
class Guardrails:
    """Deterministic limits every order must pass before it reaches the venue."""

    allowed_coins: list[str] = field(default_factory=list)  # empty = only the strategy's coin
    max_position_usd: float = 1_000.0
    max_leverage: float = 3.0
    max_daily_loss_pct: float = 5.0  # of start-of-day equity; trading stops for the UTC day
    allow_short: bool = True
    cooldown_bars: int = 0  # bars to wait after a position closes
    max_funding_8h_against: float | None = None  # block entries paying more than this per 8h
    require_approval: bool = False  # live only: every order waits for a human


@dataclass
class Strategy:
    name: str
    idea: str
    coin: str
    interval: str = "1d"
    side: str = "long"  # "long" | "short"
    entry: list[str] = field(default_factory=list)  # all must hold
    exit: list[str] = field(default_factory=list)  # any closes the position
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_bars: int | None = None
    position_pct: float = 100.0  # of equity, before leverage
    leverage: float = 1.0
    guardrails: Guardrails = field(default_factory=Guardrails)

    def __post_init__(self) -> None:
        if isinstance(self.guardrails, dict):
            self.guardrails = Guardrails(**self.guardrails)
        self.validate()

    def validate(self) -> None:
        if self.interval not in INTERVALS:
            raise ValueError(f"interval must be one of {sorted(INTERVALS)}")
        if self.side not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'")
        if not self.entry:
            raise ValueError("a strategy needs at least one entry condition")
        for c in self.entry + self.exit:
            Condition.parse(c)
        for name in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct"):
            v = getattr(self, name)
            if v is not None and not 0 < v < 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if not 0 < self.position_pct <= 100:
            raise ValueError("position_pct must be in (0, 100]")
        if self.leverage < 1:
            raise ValueError("leverage must be at least 1")

    @property
    def entry_conditions(self) -> list[Condition]:
        return [Condition.parse(c) for c in self.entry]

    @property
    def exit_conditions(self) -> list[Condition]:
        return [Condition.parse(c) for c in self.exit]

    def operands(self) -> set[Operand]:
        out: set[Operand] = set()
        for c in self.entry_conditions + self.exit_conditions:
            out |= {c.left, c.right}
        return out

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_dict(d: dict) -> "Strategy":
        return Strategy(**d)

    @staticmethod
    def load(path: str | Path) -> "Strategy":
        return Strategy.from_dict(json.loads(Path(path).read_text()))


# JSON schema handed to the LLM that compiles natural language into a Strategy.
STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "idea": {"type": "string", "description": "The user's idea, verbatim"},
        "coin": {"type": "string", "description": "Hyperliquid perp symbol, e.g. BTC"},
        "interval": {"type": "string", "enum": sorted(INTERVALS)},
        "side": {"type": "string", "enum": ["long", "short"]},
        "entry": {
            "type": "array",
            "items": {"type": "string"},
            "description": "All must hold to enter. Each is '<a> <op> <b>'. Operands: numbers, fields "
            f"{sorted(FIELDS)} (funding is the hourly rate as a fraction; funding_8h is its 8h sum), or "
            f"indicators {sorted(INDICATORS)} written like sma(50). Ops: {list(OPERATORS)}.",
        },
        "exit": {"type": "array", "items": {"type": "string"}, "description": "Any one closes the position."},
        "stop_loss_pct": {"type": ["number", "null"]},
        "take_profit_pct": {"type": ["number", "null"]},
        "trailing_stop_pct": {"type": ["number", "null"]},
        "max_hold_bars": {"type": ["integer", "null"]},
        "position_pct": {"type": "number"},
        "leverage": {"type": "number"},
    },
    "required": ["name", "idea", "coin", "interval", "side", "entry"],
}
