"""Argument parsing and plan rendering shared by the CLI tools.

Output text is Ukrainian, since that is what the user reads.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from decimal import Decimal

from tmbot.core.models import AccountRisk, Side, Signal, TakeProfit
from tmbot.execution.executor import StepResult
from tmbot.execution.planner import TradePlan


def parse_take_profit(raw: str) -> TakeProfit:
    """'64200' or '64200:0.5' (price:share)."""
    price, _, share = raw.partition(":")
    return TakeProfit(Decimal(price), Decimal(share) if share else None)


def parse_weights(raw: str | None) -> tuple[Decimal, ...] | None:
    """'0.3,0.7' -> entry weights."""
    return tuple(Decimal(w) for w in raw.split(",")) if raw else None


def add_signal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("side", choices=[s.value for s in Side])
    parser.add_argument("--symbol", default="BTC", help="актив: BTC, ETH, SOL, ...")
    parser.add_argument(
        "--entry",
        type=Decimal,
        action="append",
        default=[],
        help="ціна входу; можна кілька; без --entry = вхід по ринку",
    )
    parser.add_argument("--weights", help="частки входів, напр. 0.3,0.7")
    parser.add_argument("--sl", type=Decimal, required=True)
    parser.add_argument(
        "--tp",
        type=parse_take_profit,
        action="append",
        default=[],
        help="тейк: ціна або ціна:частка (64200:0.5)",
    )
    parser.add_argument("--leverage", type=int, help="плече із сигналу (без нього = максимум)")
    risk = parser.add_mutually_exclusive_group(required=True)
    risk.add_argument("--risk-usd", type=Decimal)
    risk.add_argument("--risk-pct", type=Decimal, help="1 = 1%% балансу по закритих угодах")


def signal_from_args(args: argparse.Namespace) -> Signal:
    return Signal(
        asset=args.symbol.upper(),
        side=Side(args.side),
        stop_loss=args.sl,
        entries=tuple(args.entry),
        take_profits=tuple(args.tp),
        leverage=args.leverage,
        entry_weights=parse_weights(args.weights),
    )


def risk_from_args(args: argparse.Namespace) -> AccountRisk:
    if args.risk_usd is not None:
        return AccountRisk.fixed(args.risk_usd)
    return AccountRisk.percent(args.risk_pct / 100)


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def render_plan(plan: TradePlan) -> list[str]:
    inst = plan.instrument
    lev = plan.leverage
    lines = [
        f"{plan.side.value.upper()} {inst.exchange_symbol or inst.asset} · "
        f"ціна {plan.reference_price} · SL {plan.stop_loss} · дальній TP {plan.far_tp or '-'}",
        "Входи:",
    ]
    for e in plan.entries:
        kind = "ринок  " if e.is_market else "лімітка"
        lines.append(f"  {kind} {e.price:>12} × {e.qty}")
    lines += [
        f"  разом {plan.total_qty} · середня {plan.avg_entry:.4f} · номінал {money(plan.notional)}",
        f"Ризик: ціль {money(plan.risk_target)} · факт {money(plan.risk_actual)}"
        f" + комісії ~{money(plan.fees)}",
        f"Плече {lev.leverage}x · маржа {money(lev.margin_required)}"
        f" · ліквідація ≈ {lev.liquidation_price:.2f}",
    ]
    if plan.tp_full_fill:
        lines.append("Тейки після заповнення всіх входів:" if plan.has_limit_entries else "Тейки:")
        for t in plan.tp_full_fill:
            pnl = plan.side.sign * (t.price - plan.avg_entry) * t.qty
            lines.append(f"  TP{t.level + 1} {t.price:>12} × {t.qty:<10} → +{money(pnl)}")
    lines += [f"  ! {w}" for w in plan.warnings]
    return lines


def render_steps(steps: Iterable[StepResult]) -> list[str]:
    return [
        f"  [{'OK ' if s.ok else 'ERR'}] {s.step}{f' · {s.detail}' if s.detail else ''}"
        for s in steps
    ]
