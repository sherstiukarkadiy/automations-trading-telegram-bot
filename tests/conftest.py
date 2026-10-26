from decimal import Decimal

import pytest

from tmbot.core.models import Instrument


@pytest.fixture
def btc() -> Instrument:
    return Instrument(
        asset="BTC",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_leverage=10,
        maintenance_margin_rate=Decimal("0.005"),
    )


@pytest.fixture
def coin() -> Instrument:
    """A made-up asset with a 0.01 step, for the worked examples from the spec."""
    return Instrument(
        asset="XYZ",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        max_leverage=20,
    )
