"""Offline trade calculator: shows what the bot would do with a signal, no exchange needed.

Examples:

    # LONG BTC, two entries, $200 risk, current price 61,800
    python -m tmbot.cli.plan long --price 61800 --entry 62000 --entry 61200 \
        --sl 60100 --tp 63000 --tp 64200 --tp 65500 --risk-usd 200

    # The same on a $25K prop account with 1% risk and a Propr limit check
    python -m tmbot.cli.plan long --price 61800 --entry 62000 --entry 61200 \
        --sl 60100 --tp 63000:0.5 --tp 64200 --tp 65500 \
        --risk-pct 1 --balance 25000 --prop --sod-balance 25000 --open-loss 300
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from tmbot.cli.common import (
    add_signal_arguments,
    money,
    render_plan,
    risk_from_args,
    signal_from_args,
)
from tmbot.core.errors import TradeSetupError
from tmbot.core.models import Instrument
from tmbot.core.prop_risk import (
    Exposure,
    PropCheckResult,
    PropRules,
    PropState,
    Verdict,
    check_new_trade,
)
from tmbot.execution.planner import TradePlan, build_plan

# Approximate contract parameters; the bot gets the real ones from the exchange API.
PRESETS: dict[str, Instrument] = {
    "BTC": Instrument("BTC", Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), 10),
    "ETH": Instrument("ETH", Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), 10),
    "SOL": Instrument("SOL", Decimal("0.01"), Decimal("0.1"), Decimal("0.1"), 10),
}
VERDICT_LABEL = {Verdict.OK: "OK", Verdict.WARN: "УВАГА", Verdict.BLOCK: "БЛОК"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plan", description="Розрахунок угоди ядром бота")
    add_signal_arguments(parser)
    parser.add_argument("--price", type=Decimal, required=True, help="поточна ціна")
    parser.add_argument(
        "--balance",
        type=Decimal,
        default=Decimal(0),
        help="баланс по закритих угодах (для --risk-pct і --prop)",
    )
    parser.add_argument("--fee", type=Decimal, default=Decimal("0.00075"), help="ставка taker")

    contract = parser.add_argument_group("контракт (для активів поза BTC/ETH/SOL)")
    contract.add_argument("--qty-step", type=Decimal)
    contract.add_argument("--min-qty", type=Decimal)
    contract.add_argument("--tick", type=Decimal)
    contract.add_argument("--max-lev", type=int)

    prop = parser.add_argument_group("prop (Propr)")
    prop.add_argument("--prop", action="store_true", help="перевірити ліміти Propr")
    prop.add_argument("--start", type=Decimal, default=Decimal(25000), help="стартовий баланс")
    prop.add_argument("--daily-pct", type=Decimal, default=Decimal(3))
    prop.add_argument("--dd-pct", type=Decimal, default=Decimal(5))
    prop.add_argument("--sod-balance", type=Decimal, help="баланс о 00:00 UTC")
    prop.add_argument(
        "--open-loss",
        type=Decimal,
        default=Decimal(0),
        help="сумарна втрата відкритих позицій по їхніх SL",
    )
    return parser


def instrument_from(args: argparse.Namespace) -> Instrument:
    asset = args.symbol.upper()
    preset = PRESETS.get(asset)
    if preset is None and args.qty_step is None:
        raise TradeSetupError(
            f"невідомий актив {asset}: вкажи --qty-step, --min-qty, --tick, --max-lev"
        )
    step = args.qty_step or (preset.qty_step if preset else Decimal("0.001"))
    return Instrument(
        asset=asset,
        tick_size=args.tick or (preset.tick_size if preset else Decimal("0.01")),
        qty_step=step,
        min_qty=args.min_qty or (preset.min_qty if preset else step),
        max_leverage=args.max_lev or (preset.max_leverage if preset else 10),
    )


def prop_check(args: argparse.Namespace, plan: TradePlan) -> PropCheckResult:
    rules = PropRules(args.start, args.daily_pct / 100, args.dd_pct / 100)
    balance = args.balance or args.start
    state = PropState(balance=balance, sod_balance=args.sod_balance or balance)
    exposure = [Exposure("open", args.open_loss)] if args.open_loss else []
    return check_new_trade(rules, state, exposure, plan.risk_actual, plan.fees)


def render_prop(result: PropCheckResult) -> list[str]:
    lines = [
        f"Propr: {VERDICT_LABEL[result.verdict]} · найгірша equity {money(result.equity_worst)}"
    ]
    for c in result.limits:
        lines.append(f"  {c.limit.label:<18} межа {money(c.floor)} · запас {money(c.headroom)}")
    lines.append(f"  макс. втрата нової угоди без попередження: {money(result.max_new_loss)}")
    lines += [f"  - {r}" for r in result.reasons]
    return lines


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        instrument = instrument_from(args)
        risk = risk_from_args(args).amount(args.balance)
        plan = build_plan(signal_from_args(args), instrument, args.price, risk, fee_rate=args.fee)
    except (TradeSetupError, ValueError) as exc:
        print(f"План не складено: {exc}")
        return 1

    print("\n" + "\n".join(render_plan(plan)))
    if args.prop:
        print("\n" + "\n".join(render_prop(prop_check(args, plan))))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
