"""Markdown report and charts for one strategy."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import robustness as rb  # noqa: E402

INK, MUTED, HONEST_C, NAIVE_C, HOLD_C = "#1f2328", "#6e7781", "#0969da", "#bf8700", "#8c959f"


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")


def _equity_chart(rep: dict, df: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=150)
    start = rep["honest"].equity.iloc[0]
    hold = df["close"] / df["open"].iloc[0] * start
    if rep["strategy"].side == "short":
        hold = start * 2 - hold
    ax.plot(rep["naive"].equity.index, rep["naive"].equity, color=NAIVE_C, lw=1.4, label="Naive backtest")
    ax.plot(rep["honest"].equity.index, rep["honest"].equity, color=HONEST_C, lw=1.8, label="Honest (fees, slippage, funding, next-bar fills)")
    ax.plot(rep["honest"].equity.index, hold.to_numpy(), color=HOLD_C, lw=1, ls="--", label="Buy and hold" if rep["strategy"].side == "long" else "Short and hold")
    ax.set_ylabel("Equity ($)", color=MUTED)
    ax.set_title(rep["strategy"].name, loc="left", color=INK, fontsize=11)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color="#d0d7de", lw=0.5)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _row(name: str, m: dict) -> str:
    return (
        f"| {name} | {m['total_return_pct']:+.1f}% | {m['max_drawdown_pct']:.1f}% | {m['sharpe']:.2f} | {m['trades']} | "
        f"{m['win_rate_pct']:.0f}% | ${m['fees_usd']:,.0f} | ${m['funding_usd']:,.0f} | {m['liquidations']} | {m['blocked_entries']} |"
    )


def write(df: pd.DataFrame, rep: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    s = rep["strategy"]
    slug = _slug(s.name)
    chart = out_dir / f"{slug}.png"
    _equity_chart(rep, df, chart)

    nb = rep["neighbours"]
    nb_lines = ["| Setting | Changed to | Return |", "|---|---|---|"] + [
        f"| {r.knob} = {r['from']:g} | {r.to:g} | {r.return_pct:+.1f}% |" for _, r in nb.iterrows()
    ]
    reg = rep["regimes"]
    ts = rep["time_split"]
    lines = [
        f"# {s.name}",
        "",
        f"> {s.idea}",
        "",
        f"**{s.coin}-PERP on Hyperliquid**, {s.interval} bars, {rep['period'][0]} to {rep['period'][1]}, $10,000 start, "
        f"{s.side}, {s.leverage:g}x leverage. Data: Hyperliquid candles and hourly funding.",
        "",
        "**Compiled rules**",
        "",
        f"- Enter {s.side} when all hold: " + "; ".join(f"`{c}`" for c in s.entry),
        *([f"- Exit when any holds: " + "; ".join(f"`{c}`" for c in s.exit)] if s.exit else []),
        *([f"- Stop loss {s.stop_loss_pct:g}%"] if s.stop_loss_pct else []),
        *([f"- Take profit {s.take_profit_pct:g}%"] if s.take_profit_pct else []),
        *([f"- Trailing stop {s.trailing_stop_pct:g}%"] if s.trailing_stop_pct else []),
        *([f"- Exit after {s.max_hold_bars} bars"] if s.max_hold_bars else []),
        f"- Guardrails: max position ${s.guardrails.max_position_usd:,.0f}, max leverage {s.guardrails.max_leverage:g}x, "
        f"daily loss limit {s.guardrails.max_daily_loss_pct:g}%"
        + (f", cooldown {s.guardrails.cooldown_bars} bars" if s.guardrails.cooldown_bars else "")
        + (f", no entry paying over {s.guardrails.max_funding_8h_against * 100:.3f}%/8h funding" if s.guardrails.max_funding_8h_against is not None else ""),
        "",
        "## Where it breaks",
        "",
        *([f"- {f}" for f in rep["flags"]] or ["- No red flags from these checks."]),
        "",
        f"![Equity]({chart.name})",
        "",
        "## Results",
        "",
        "| Run | Return | Max drawdown | Sharpe | Trades | Win rate | Fees | Funding paid | Liquidations | Entries blocked |",
        "|---|---|---|---|---|---|---|---|---|---|",
        _row("Naive", rep["naive"].metrics()),
        _row("Honest", rep["honest"].metrics()),
        _row("Honest, fees ×2 and slippage ×3", rep["stressed"].metrics()),
        "",
        f"{'Buy' if s.side == 'long' else 'Short'} and hold over the same period: {rep['buy_hold_pct']:+.1f}%.",
        "",
        "## Time split and regimes",
        "",
        f"- Before {ts["split_date"]} (first 70% of bars): {ts["first_70pct_return"]:+.1f}% over {ts["first_70pct_trades"]} trades. After: {ts["last_30pct_return"]:+.1f}% over {ts["last_30pct_trades"]} trades.",
        f"- P&L by {s.coin}'s trailing 30-day trend: uptrend ${reg['uptrend']:,.0f}, sideways ${reg['sideways']:,.0f}, "
        f"downtrend ${reg['downtrend']:,.0f}. The coin trended up {reg['bars_uptrend_pct']:.0f}% of the time and down {reg['bars_downtrend_pct']:.0f}%.",
        "",
        "## Nearby settings",
        "",
        "Each setting nudged on its own, everything else unchanged (honest costs).",
        "",
        *nb_lines,
        "",
    ]
    path = out_dir / f"{slug}.md"
    path.write_text("\n".join(lines))
    return path


def run_all(strategy_paths: list[Path], out_dir: Path) -> list[Path]:
    from . import data
    from .spec import Strategy

    out = []
    for p in strategy_paths:
        s = Strategy.load(p)
        df = data.load(s.coin, s.interval)
        rep = rb.report(df, s, data.contract(s.coin).max_leverage)
        out.append(write(df, rep, out_dir))
    return out
