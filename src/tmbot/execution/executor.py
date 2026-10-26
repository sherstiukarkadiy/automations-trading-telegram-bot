"""Placing a trade plan on one account, and closing positions.

Opening order:
1. Leverage.
2. Entries: the market one first, then limits. Every entry carries the stop and the far
   take-profit for the whole position.
3. After a market entry: wait for the fill and place the intermediate take-profits as
   reduce-only limits for the filled quantity.
Intermediate take-profits for limit entries are placed by the worker once they fill (stage 5).

Network retries live in the HTTP client. Here every failure is recorded in the report and
the remaining steps still run, so that one failure cannot leave a position unprotected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TypeVar

from tmbot.exchanges.base import (
    EntryOrder,
    ExchangeAdapter,
    ExchangeError,
    OrderAck,
    ReduceOrder,
)
from tmbot.execution.planner import TradePlan

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StepResult:
    step: str  # user-facing label
    ok: bool
    detail: str = ""
    link_id: str = ""
    order_id: str = ""


@dataclass(slots=True)
class ExecutionReport:
    account: str
    trade_id: str
    steps: list[StepResult] = field(default_factory=list)
    market_filled_qty: Decimal = Decimal(0)
    market_avg_price: Decimal | None = None

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def failures(self) -> list[StepResult]:
        return [s for s in self.steps if not s.ok]


class TradeExecutor:
    """Executes plans and close requests on one exchange account."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        *,
        sleep: Sleep = asyncio.sleep,
        fill_poll_attempts: int = 10,
        fill_poll_delay: float = 0.3,
    ) -> None:
        self.adapter = adapter
        self._sleep = sleep
        self._fill_poll_attempts = fill_poll_attempts
        self._fill_poll_delay = fill_poll_delay

    # --- opening ------------------------------------------------------------------

    async def open(self, plan: TradePlan) -> ExecutionReport:
        report = ExecutionReport(account=self.adapter.name, trade_id=plan.trade_id)
        leverage = plan.leverage.leverage

        step, _ = await self._attempt(
            "плече", self.adapter.set_leverage(plan.asset, leverage), f"{leverage}x"
        )
        report.steps.append(step)
        if not step.ok:
            return report  # with the wrong leverage the liquidation check is invalid

        market_link = await self._place_entries(plan, report)
        if market_link is None:
            return report

        filled, avg_price = await self._wait_for_fill(plan.asset, market_link)
        report.market_filled_qty, report.market_avg_price = filled, avg_price
        if filled <= 0:
            report.steps.append(
                StepResult("заповнення ринку", False, "ринковий ордер не заповнився")
            )
            return report

        await self._place_take_profits(plan, filled, avg_price or plan.reference_price, report)
        return report

    async def _place_entries(self, plan: TradePlan, report: ExecutionReport) -> str | None:
        """Place every entry, market first. Returns the market entry's link id, if any."""
        market_link: str | None = None
        ordered = sorted(enumerate(plan.entries), key=lambda item: not item[1].is_market)
        for index, entry in ordered:
            link = plan.link_id("e", index)
            label = "вхід ринок" if entry.is_market else f"вхід лімітка {entry.price}"
            order = EntryOrder(
                asset=plan.asset,
                side=plan.side,
                qty=entry.qty,
                price=None if entry.is_market else entry.price,
                stop_loss=plan.stop_loss,
                take_profit=plan.far_tp,
                link_id=link,
            )
            step, ack = await self._attempt(label, self.adapter.place_entry(order), f"{entry.qty}")
            report.steps.append(_with_order(step, link, ack))
            if ack is not None and entry.is_market:
                market_link = link
        return market_link

    async def _place_take_profits(
        self, plan: TradePlan, filled: Decimal, price: Decimal, report: ExecutionReport
    ) -> None:
        for tp in plan.intermediate_take_profits(filled, price):
            link = plan.link_id("t", tp.level)
            order = ReduceOrder(plan.asset, plan.side, tp.qty, tp.price, link)
            step, ack = await self._attempt(
                f"TP{tp.level + 1} {tp.price}", self.adapter.place_reduce_limit(order), f"{tp.qty}"
            )
            report.steps.append(_with_order(step, link, ack))

    async def _wait_for_fill(self, asset: str, link_id: str) -> tuple[Decimal, Decimal | None]:
        """Poll a market order until it reaches a final status; returns (filled, avg price)."""
        filled, avg_price = Decimal(0), None
        for _ in range(self._fill_poll_attempts):
            try:
                state = await self.adapter.get_order(asset, link_id)
            except ExchangeError:
                await self._sleep(self._fill_poll_delay)
                continue
            filled, avg_price = state.filled_qty, state.avg_price
            if state.status.is_final:
                break
            await self._sleep(self._fill_poll_delay)
        return filled, avg_price

    # --- closing ------------------------------------------------------------------

    async def close_asset(self, asset: str) -> list[StepResult]:
        """Cancel every order on the asset and close its position at market (reduce-only)."""
        cancel, _ = await self._attempt(
            f"{asset}: скасування ордерів", self.adapter.cancel_all(asset)
        )
        close, ack = await self._attempt(f"{asset}: закриття", self.adapter.close_position(asset))
        if close.ok:
            detail = "закрито ринком" if ack else "позиції немає"
            close = replace(close, detail=detail, order_id=ack.order_id if ack else "")
        return [cancel, close]

    async def close_all(self) -> list[StepResult]:
        """ "Close everything" on the account: cancel all orders, close all positions."""
        cancel, _ = await self._attempt("скасування всіх ордерів", self.adapter.cancel_everything())
        steps = [cancel]
        listed, positions = await self._attempt("список позицій", self.adapter.get_positions())
        if positions is None:
            return [*steps, listed]
        for pos in positions:
            step, ack = await self._attempt(
                f"{pos.asset}: закриття", self.adapter.close_position(pos.asset)
            )
            steps.append(_with_order(step, "", ack))
        return steps

    # --- helpers ------------------------------------------------------------------

    @staticmethod
    async def _attempt(
        label: str, call: Awaitable[T], detail: str = ""
    ) -> tuple[StepResult, T | None]:
        """Await an exchange call, turning an ``ExchangeError`` into a failed step."""
        try:
            value = await call
        except ExchangeError as exc:
            return StepResult(label, False, str(exc)), None
        return StepResult(label, True, detail), value


def _with_order(step: StepResult, link_id: str, ack: OrderAck | None) -> StepResult:
    return replace(step, link_id=link_id, order_id=ack.order_id if ack else "")
