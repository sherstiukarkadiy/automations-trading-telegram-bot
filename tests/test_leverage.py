from decimal import Decimal as D

import pytest

from tmbot.core.errors import TradeSetupError
from tmbot.core.leverage import choose_leverage, liquidation_price
from tmbot.core.models import Instrument, Side


def test_no_requested_uses_exchange_max(btc: Instrument) -> None:
    decision = choose_leverage(Side.LONG, D(60000), D(59400), D(6000), btc, requested=None)
    assert decision.leverage == 10
    assert not decision.clamped_to_exchange
    assert decision.margin_required == D(600)


def test_requested_above_max_is_clamped(btc: Instrument) -> None:
    decision = choose_leverage(Side.LONG, D(60000), D(59400), D(6000), btc, requested=50)
    assert decision.leverage == 10
    assert decision.clamped_to_exchange
    assert decision.notes


def test_lowered_when_liquidation_before_stop(btc: Instrument) -> None:
    # stop 8% below: at 10x liquidation is ~9.5% below, but the 1.2 buffer needs >= 9.6%
    decision = choose_leverage(Side.LONG, D(100), D(92), D(1000), btc, requested=10)
    assert decision.lowered_for_liquidation
    assert decision.leverage < 10
    liq = liquidation_price(Side.LONG, D(100), decision.leverage, btc.maintenance_margin_rate)
    assert D(100) - liq >= (D(100) - D(92)) * D("1.2")


def test_short_liquidation_is_above_stop(btc: Instrument) -> None:
    decision = choose_leverage(Side.SHORT, D(100), D(103), D(1000), btc, requested=10)
    assert decision.liquidation_price > D(103)


def test_impossible_stop_raises(btc: Instrument) -> None:
    with pytest.raises(TradeSetupError):
        choose_leverage(Side.LONG, D(100), D(10), D(1000), btc, requested=5)
