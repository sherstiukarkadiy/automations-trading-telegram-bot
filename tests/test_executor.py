from dataclasses import dataclass, field
from decimal import Decimal as D

from tmbot.core.models import Instrument, Side, Signal, TakeProfit
from tmbot.exchanges.base import (
    Balance,
    EntryOrder,
    OrderAck,
    OrderState,
    OrderStatus,
    PositionInfo,
    ReduceOrder,
    RejectedError,
)
from tmbot.execution.executor import TradeExecutor
from tmbot.execution.planner import TradePlan, build_plan

BTC = Instrument("BTC", D("0.1"), D("0.001"), D("0.001"), 100, exchange_symbol="BTCUSDT")


async def no_sleep(_: float) -> None:
    return None


@dataclass
class FakeAdapter:
    """In-memory exchange that records every call."""

    name: str = "fake"
    fail_entries: set[str] = field(default_factory=set)
    fail_leverage: bool = False
    fill_ratio: D = D(1)
    calls: list[tuple[str, object]] = field(default_factory=list)
    entries: dict[str, EntryOrder] = field(default_factory=dict)
    positions: list[PositionInfo] = field(default_factory=list)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def of_kind(self, kind: str) -> list[object]:
        return [payload for k, payload in self.calls if k == kind]

    async def get_instrument(self, asset: str) -> Instrument:
        return BTC

    async def get_price(self, asset: str) -> D:
        return D(61800)

    async def get_balance(self, coin: str = "USDT") -> Balance:
        return Balance(coin, D(1000), D(1000), D(1000))

    async def get_positions(self) -> list[PositionInfo]:
        return self.positions

    async def set_leverage(self, asset: str, leverage: int) -> None:
        self.calls.append(("leverage", leverage))
        if self.fail_leverage:
            raise RejectedError("no")

    async def place_entry(self, order: EntryOrder) -> OrderAck:
        self.calls.append(("entry", order))
        if order.link_id in self.fail_entries:
            raise RejectedError("insufficient balance", 110007)
        self.entries[order.link_id] = order
        return OrderAck("id-" + order.link_id, order.link_id)

    async def place_reduce_limit(self, order: ReduceOrder) -> OrderAck:
        self.calls.append(("reduce", order))
        return OrderAck("id-" + order.link_id, order.link_id)

    async def get_order(self, asset: str, link_id: str) -> OrderState:
        order = self.entries[link_id]
        filled = (order.qty * self.fill_ratio).quantize(D("0.001"))
        status = OrderStatus.FILLED if filled == order.qty else OrderStatus.CANCELLED
        return OrderState("id", link_id, status, order.qty, filled, D(61805))

    async def amend_stop(self, asset: str, stop_loss: D) -> None:
        self.calls.append(("amend", stop_loss))

    async def cancel_all(self, asset: str) -> None:
        self.calls.append(("cancel_all", asset))

    async def cancel_everything(self) -> None:
        self.calls.append(("cancel_everything", None))

    async def close_position(self, asset: str) -> OrderAck | None:
        self.calls.append(("close", asset))
        return OrderAck("c", "c")

    async def aclose(self) -> None:
        return None


def make_plan(**overrides: object) -> TradePlan:
    fields: dict[str, object] = {
        "asset": "BTC",
        "side": Side.LONG,
        "stop_loss": D(60100),
        "entries": (D(62000), D(61200)),
        "take_profits": (TakeProfit(D(63000)), TakeProfit(D(64200)), TakeProfit(D(65500))),
        "leverage": 10,
    }
    fields.update(overrides)
    return build_plan(Signal(**fields), BTC, D(61800), D(200), trade_id="T")  # type: ignore[arg-type]


def executor(fake: FakeAdapter) -> TradeExecutor:
    return TradeExecutor(fake, sleep=no_sleep)


async def test_mixed_entry_places_market_then_limit_then_take_profits() -> None:
    fake = FakeAdapter()
    report = await executor(fake).open(make_plan())
    assert report.ok, report.failures
    assert fake.kinds() == ["leverage", "entry", "entry", "reduce", "reduce"]

    market, limit = fake.of_kind("entry")
    assert isinstance(market, EntryOrder) and isinstance(limit, EntryOrder)
    assert market.price is None and limit.price == D(61200)
    # both entries carry the stop and the far take-profit
    assert market.stop_loss == limit.stop_loss == D(60100)
    assert market.take_profit == limit.take_profit == D(65500)

    # intermediate take-profits cover the market fill only, and skip the far level
    reduces = [r for r in fake.of_kind("reduce") if isinstance(r, ReduceOrder)]
    assert [r.price for r in reduces] == [D(63000), D(64200)]
    assert sum(r.qty for r in reduces) < market.qty
    assert all(s.order_id for s in report.steps if s.link_id)


async def test_limit_only_places_no_intermediate_take_profits() -> None:
    fake = FakeAdapter()
    report = await executor(fake).open(make_plan(entries=(D(61500), D(61000))))
    assert report.ok
    assert fake.kinds() == ["leverage", "entry", "entry"]


async def test_leverage_failure_aborts_before_entries() -> None:
    fake = FakeAdapter(fail_leverage=True)
    report = await executor(fake).open(make_plan())
    assert not report.ok
    assert fake.kinds() == ["leverage"]


async def test_failed_limit_is_reported_but_take_profits_still_placed() -> None:
    fake = FakeAdapter(fail_entries={"tmT-e1"})
    report = await executor(fake).open(make_plan())
    assert [f.link_id for f in report.failures] == ["tmT-e1"]
    assert "reduce" in fake.kinds()


async def test_failed_market_entry_skips_take_profits() -> None:
    fake = FakeAdapter(fail_entries={"tmT-e0"})
    report = await executor(fake).open(make_plan())
    assert not report.ok
    assert "reduce" not in fake.kinds()


async def test_partial_market_fill_sizes_take_profits_from_filled_qty() -> None:
    fake = FakeAdapter(fill_ratio=D("0.5"))
    report = await executor(fake).open(make_plan(entries=()))
    reduces = [r for r in fake.of_kind("reduce") if isinstance(r, ReduceOrder)]
    assert report.market_filled_qty > 0
    assert sum(r.qty for r in reduces) < report.market_filled_qty


async def test_close_asset_cancels_then_closes() -> None:
    fake = FakeAdapter()
    steps = await executor(fake).close_asset("BTC")
    assert all(s.ok for s in steps)
    assert fake.kinds() == ["cancel_all", "close"]
    assert steps[1].detail == "закрито ринком"


async def test_close_all_closes_each_position() -> None:
    position = PositionInfo("ETH", Side.SHORT, D(1), D(3000), D(3000), D(5), D(0), None, None, None)
    fake = FakeAdapter(positions=[position])
    steps = await executor(fake).close_all()
    assert all(s.ok for s in steps)
    assert ("close", "ETH") in fake.calls
