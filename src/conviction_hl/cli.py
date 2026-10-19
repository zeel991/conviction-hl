"""conviction-hl: idea -> rules -> honest backtest -> paper -> Hyperliquid."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="conviction-hl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile", help="turn a plain-English idea into a strategy JSON (needs Anthropic credentials)")
    c.add_argument("idea")
    c.add_argument("--out", type=Path)

    r = sub.add_parser("report", help="naive vs honest backtest plus robustness checks, as markdown and a chart")
    r.add_argument("strategies", nargs="+", type=Path)
    r.add_argument("--out", type=Path, default=Path("reports"))
    r.add_argument("--refresh", action="store_true", help="re-download market data")

    pp = sub.add_parser("paper", help="trade the strategy on live Hyperliquid prices with simulated fills")
    pp.add_argument("strategy", type=Path)
    pp.add_argument("--minutes", type=float, default=60)
    pp.add_argument("--poll", type=float, default=15)
    pp.add_argument("--log", type=Path, default=Path("logs/paper.jsonl"))

    lv = sub.add_parser("live", help="trade through a Hyperliquid API wallet (dry run unless --send)")
    lv.add_argument("strategy", type=Path)
    lv.add_argument("--network", choices=["testnet", "mainnet"], default="testnet")
    lv.add_argument("--send", action="store_true", help="actually post orders; without it orders are built and signed but not sent")
    lv.add_argument("--minutes", type=float, default=60)
    lv.add_argument("--poll", type=float, default=15)
    lv.add_argument("--log", type=Path, default=Path("logs/live.jsonl"))

    a = p.parse_args(argv)

    if a.cmd == "compile":
        from .compile import compile_idea

        s = compile_idea(a.idea)
        out = s.to_json()
        if a.out:
            a.out.write_text(out)
        print(out)
        return 0

    if a.cmd == "report":
        from . import data, report
        from .spec import Strategy

        if a.refresh:
            for path in a.strategies:
                s = Strategy.load(path)
                data.load(s.coin, s.interval, refresh=True)
        for path in report.run_all(a.strategies, a.out):
            print(path)
        return 0

    from . import live
    from .spec import Strategy

    s = Strategy.load(a.strategy)
    a.log.parent.mkdir(parents=True, exist_ok=True)

    if a.cmd == "paper":
        from .broker import PaperBroker

        live.run(s, PaperBroker(), a.minutes, a.poll, a.log)
        return 0

    key, account = os.environ.get("HL_AGENT_KEY"), os.environ.get("HL_ACCOUNT_ADDRESS")
    if not key or not account:
        print("Set HL_AGENT_KEY (the API wallet's private key) and HL_ACCOUNT_ADDRESS (the master account it trades for).", file=sys.stderr)
        return 2
    if a.network == "mainnet" and a.send and os.environ.get("HL_CONFIRM_MAINNET") != "yes":
        print("Refusing to send mainnet orders without HL_CONFIRM_MAINNET=yes.", file=sys.stderr)
        return 2
    from .broker import HyperliquidBroker

    broker = HyperliquidBroker(key, account, a.network, dry_run=not a.send)
    events = live.run(s, broker, a.minutes, a.poll, a.log)
    print(json.dumps({"orders": sum(e["event"] in ("entry", "exit") for e in events), "sent": a.send, "network": a.network}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
