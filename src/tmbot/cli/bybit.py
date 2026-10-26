"""Check the Bybit adapter against a live account (testnet by default).

Keys come from BYBIT_API_KEY / BYBIT_API_SECRET or are prompted for with getpass, so they
never land in the shell history.

    python -m tmbot.cli.bybit check
    python -m tmbot.cli.bybit positions
    python -m tmbot.cli.bybit open long --sl 60100 --tp 63000 --tp 65500 --risk-usd 20
    python -m tmbot.cli.bybit open long --entry 61000 --entry 60500 --sl 60100 \
        --tp 65500 --risk-usd 20
    python -m tmbot.cli.bybit close BTC
    python -m tmbot.cli.bybit close-all

A real-money account requires --mainnet and a typed confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from tmbot.cli.common import (
    add_signal_arguments,
    render_plan,
    render_steps,
    risk_from_args,
    signal_from_args,
)
from tmbot.core.errors import TradeSetupError
from tmbot.exchanges.base import ExchangeError
from tmbot.exchanges.bybit.adapter import BybitAdapter
from tmbot.exchanges.bybit.client import BybitHttp
from tmbot.exchanges.bybit.security import check_key
from tmbot.execution.executor import TradeExecutor
from tmbot.execution.planner import build_plan

CLOCK_DRIFT_WARN_MS = 1000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bybit", description="Перевірка Bybit-адаптера")
    parser.add_argument(
        "--mainnet", action="store_true", help="бойовий акаунт (за замовч. testnet)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="ключ, права, баланс, час сервера")
    sub.add_parser("positions", help="відкриті позиції")

    open_cmd = sub.add_parser("open", help="розрахувати і відкрити угоду")
    add_signal_arguments(open_cmd)
    open_cmd.add_argument("--dry-run", action="store_true", help="тільки показати план")
    open_cmd.add_argument(
        "--yes", action="store_true", help="не питати підтвердження (тільки testnet)"
    )

    close_cmd = sub.add_parser("close", help="скасувати ордери і закрити позицію по активу")
    close_cmd.add_argument("symbol")
    sub.add_parser("close-all", help="скасувати всі ордери і закрити всі позиції")
    return parser


def read_keys() -> tuple[str, str]:
    key = os.environ.get("BYBIT_API_KEY") or getpass.getpass("Bybit API key: ")
    secret = os.environ.get("BYBIT_API_SECRET") or getpass.getpass("Bybit API secret: ")
    if not key or not secret:
        sys.exit("ключ і секрет обов'язкові")
    return key.strip(), secret.strip()


def confirm(question: str) -> bool:
    return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes", "т", "так")


def show(lines: list[str]) -> None:
    print("\n".join(lines))


class BybitCli:
    """One CLI session: an authenticated adapter plus the command handlers."""

    def __init__(self, http: BybitHttp, *, mainnet: bool) -> None:
        self.http = http
        self.adapter = BybitAdapter(http)
        self.executor = TradeExecutor(self.adapter)
        self.mainnet = mainnet

    async def run(self, args: argparse.Namespace) -> int:
        handlers = {
            "check": self.check,
            "positions": self.positions,
            "open": self.open,
            "close": self.close,
            "close-all": self.close_all,
        }
        try:
            return await handlers[args.cmd](args)
        except ExchangeError as exc:
            print(f"Помилка біржі: {exc}")
            return 1
        finally:
            await self.adapter.aclose()

    async def check(self, _: argparse.Namespace) -> int:
        offset = await self.http.sync_time()
        drift = (
            "  (!) понад 1 с — синхронізуй годинник" if abs(offset) > CLOCK_DRIFT_WARN_MS else ""
        )
        print(f"Час сервера: зсув {offset} мс{drift}")

        report = await check_key(self.http)
        print("Ключ:", "OK" if report.ok else "НЕ ПРИДАТНИЙ")
        show([f"  ✗ {p}" for p in report.problems] + [f"  ! {w}" for w in report.warnings])
        if report.days_left is not None:
            print(f"  ключ діє ще {report.days_left} дн.")

        balance = await self.adapter.get_balance()
        print(
            f"Баланс {balance.coin}: закриті угоди {balance.realized:.2f} · "
            f"equity {balance.equity:.2f} · доступно {balance.available:.2f}"
        )
        instrument = await self.adapter.get_instrument("BTC")
        price = await self.adapter.get_price("BTC")
        print(
            f"BTC: {instrument.exchange_symbol} · крок ціни {instrument.tick_size} · "
            f"лот {instrument.qty_step} · макс. плече {instrument.max_leverage}x · ціна {price}"
        )
        return 0 if report.ok else 1

    async def positions(self, _: argparse.Namespace) -> int:
        positions = await self.adapter.get_positions()
        if not positions:
            print("Відкритих позицій немає")
        for p in positions:
            print(
                f"{p.asset} {p.side.value.upper()} {p.size} @ {p.avg_price} · "
                f"mark {p.mark_price} · uP&L {p.unrealized_pnl:.2f} · SL {p.stop_loss or '-'} · "
                f"TP {p.take_profit or '-'} · liq {p.liq_price or '-'} · {p.leverage}x"
            )
        return 0

    async def open(self, args: argparse.Namespace) -> int:
        signal = signal_from_args(args)
        instrument = await self.adapter.get_instrument(signal.asset)
        price = await self.adapter.get_price(signal.asset)
        balance = await self.adapter.get_balance(instrument.settle_coin)
        try:
            risk = risk_from_args(args).amount(balance.realized)
            plan = build_plan(signal, instrument, price, risk, available_margin=balance.available)
        except (TradeSetupError, ValueError) as exc:
            print(f"План не складено: {exc}")
            return 1

        print()
        show(render_plan(plan))
        if args.dry_run:
            return 0
        where = "БОЙОВОМУ" if self.mainnet else "testnet"
        if (self.mainnet or not args.yes) and not confirm(f"\nВідкрити на {where} акаунті?"):
            print("Скасовано")
            return 0

        report = await self.executor.open(plan)
        print(f"\nУгода {plan.trade_id}:")
        show(render_steps(report.steps))
        if report.market_filled_qty:
            print(f"  ринок заповнено {report.market_filled_qty} @ {report.market_avg_price}")
        if plan.has_limit_entries:
            print("  проміжні тейки для ліміток виставить воркер після заповнення (етап 5)")
        return 0 if report.ok else 1

    async def close(self, args: argparse.Namespace) -> int:
        steps = await self.executor.close_asset(args.symbol.upper())
        show(render_steps(steps))
        return 0 if all(s.ok for s in steps) else 1

    async def close_all(self, _: argparse.Namespace) -> int:
        if not confirm("Скасувати всі ордери і закрити всі позиції?"):
            return 0
        steps = await self.executor.close_all()
        show(render_steps(steps))
        return 0 if all(s.ok for s in steps) else 1


async def run(args: argparse.Namespace) -> int:
    if args.mainnet:
        print("УВАГА: бойовий акаунт Bybit.")
        if input("Введи MAINNET, щоб продовжити: ").strip() != "MAINNET":
            return 1
    key, secret = read_keys()
    cli = BybitCli(BybitHttp(key, secret, testnet=not args.mainnet), mainnet=args.mainnet)
    return await cli.run(args)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
