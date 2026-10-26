from decimal import Decimal as D

import pytest

from tmbot.core.errors import TradeSetupError
from tmbot.core.models import Instrument, Side, Signal, TakeProfit
from tmbot.execution.planner import TradePlan, build_plan

BTC = Instrument(
    "BTC", D("0.1"), D("0.001"), D("0.001"), 100, exchange_symbol="BTCUSDT", min_notional=D(5)
)
THREE_TPS = (TakeProfit(D(63000)), TakeProfit(D(64200)), TakeProfit(D(65500)))


def signal(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "asset": "BTC",
        "side": Side.LONG,
        "stop_loss": D(60100),
        "entries": (D(62000), D(61200)),
        "take_profits": THREE_TPS,
        "leverage": 10,
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


def plan(price: D = D(61800), risk: D = D(200), **overrides: object) -> TradePlan:
    return build_plan(signal(**overrides), BTC, price, risk, trade_id="t1")


def test_mixed_entry_plan() -> None:
    p = plan()
    assert [e.is_market for e in p.entries] == [True, False]
    assert p.has_limit_entries
    assert p.far_tp == D(65500)
    assert p.risk_actual <= D(200)
    assert p.leverage.leverage == 10
    assert sum(t.qty for t in p.tp_full_fill) == p.total_qty
    assert p.link_id("e", 0) == "tmt1-e0"


def test_prices_rounded_to_tick() -> None:
    assert plan(stop_loss=D("60100.07")).stop_loss == D("60100.1")


def test_invalid_signal_raises() -> None:
    with pytest.raises(TradeSetupError, match="SL"):
        plan(stop_loss=D(63000))


def test_price_beyond_stop_raises() -> None:
    with pytest.raises(TradeSetupError, match="за стопом"):
        plan(price=D(60000), entries=())


def test_min_notional_raises() -> None:
    instrument = Instrument("BTC", D("0.1"), D("0.001"), D("0.001"), 100, min_notional=D(100))
    with pytest.raises(TradeSetupError, match="мінімум"):
        build_plan(signal(entries=()), instrument, D(61800), D(2))


def test_all_take_profits_passed_raises() -> None:
    with pytest.raises(TradeSetupError, match="всі тейки"):
        plan(price=D(66000), entries=())


def test_passed_take_profit_warns() -> None:
    p = plan(price=D(63500), entries=(), stop_loss=D(62000))
    assert any("пройдені" in w for w in p.warnings)
    assert [t.level for t in p.tp_full_fill] == [1, 2]


def test_margin_warning() -> None:
    p = build_plan(signal(), BTC, D(61800), D(200), available_margin=D(10))
    assert any("маржа" in w for w in p.warnings)


def test_intermediate_take_profits_exclude_far_level() -> None:
    p = plan(entries=())
    tps = p.intermediate_take_profits(p.total_qty, D(61800))
    assert [t.level for t in tps] == [0, 1]


def test_single_take_profit_has_no_intermediate() -> None:
    p = plan(take_profits=(TakeProfit(D(65000)),))
    assert p.intermediate_take_profits(p.total_qty, D(61800)) == []
