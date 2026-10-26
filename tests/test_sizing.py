from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tmbot.core.errors import TradeSetupError
from tmbot.core.models import Instrument, Side, Signal
from tmbot.core.sizing import EntryLeg, resolve_entries, size_position


def test_spec_example_two_entries(coin: Instrument) -> None:
    """Worked example from the spec: LONG, entries 100 and 95, SL 90, R=$200, equal split."""
    legs = [EntryLeg(D(100), D("0.5"), is_market=False), EntryLeg(D(95), D("0.5"), is_market=False)]
    size = size_position(Side.LONG, legs, D(90), D(200), coin)
    assert [e.qty for e in size.entries] == [D("13.33"), D("13.33")]
    assert size.risk_actual == D("199.95")
    assert size.avg_entry == D("97.5")


def test_short_single_entry(btc: Instrument) -> None:
    size = size_position(Side.SHORT, [EntryLeg(D(60000), D(1), True)], D(61000), D(100), btc)
    assert size.total_qty == D("0.1")
    assert size.risk_actual == D(100)


def test_fees_are_entry_plus_exit_at_stop(btc: Instrument) -> None:
    size = size_position(Side.LONG, [EntryLeg(D(100), D(1), True)], D(90), D(10), btc)
    assert size.estimated_fees(D(90), D("0.001")) == (D(100) + D(90)) * size.total_qty * D("0.001")


def test_below_min_lot_raises(btc: Instrument) -> None:
    with pytest.raises(TradeSetupError, match="мінімальний лот"):
        size_position(Side.LONG, [EntryLeg(D(60000), D(1), True)], D(50000), D(5), btc)


def test_entry_beyond_stop_raises(btc: Instrument) -> None:
    with pytest.raises(TradeSetupError):
        size_position(Side.LONG, [EntryLeg(D(59000), D(1), True)], D(60000), D(100), btc)


def test_resolve_entries_market_when_no_prices() -> None:
    signal = Signal(asset="BTC", side=Side.LONG, stop_loss=D(90))
    assert resolve_entries(signal, D(100)) == [EntryLeg(D(100), D(1), is_market=True)]


def test_resolve_entries_long_reached_goes_market() -> None:
    signal = Signal(asset="BTC", side=Side.LONG, stop_loss=D(90), entries=(D(100), D(95)))
    # price 98: the first entry (100) is already reached -> market; the second (95) rests
    assert resolve_entries(signal, D(98)) == [
        EntryLeg(D(98), D("0.5"), is_market=True),
        EntryLeg(D(95), D("0.5"), is_market=False),
    ]


def test_resolve_entries_both_reached_merge() -> None:
    signal = Signal(asset="BTC", side=Side.LONG, stop_loss=D(90), entries=(D(100), D(95)))
    assert resolve_entries(signal, D(94)) == [EntryLeg(D(94), D(1), is_market=True)]


def test_resolve_entries_short_mirror() -> None:
    signal = Signal(asset="BTC", side=Side.SHORT, stop_loss=D(110), entries=(D(100), D(105)))
    legs = resolve_entries(signal, D(102))
    assert legs[0].is_market and legs[0].weight == D("0.5")
    assert legs[1] == EntryLeg(D(105), D("0.5"), is_market=False)


prices = st.decimals(min_value=D(1), max_value=D(100000), places=2)


@settings(max_examples=300)
@given(
    entry=prices,
    second=prices,
    stop_gap=st.decimals(min_value=D("0.5"), max_value=D(5000), places=2),
    risk=st.decimals(min_value=D(10), max_value=D(5000), places=2),
    side=st.sampled_from([Side.LONG, Side.SHORT]),
)
def test_rounded_risk_never_exceeds_target(
    entry: D, second: D, stop_gap: D, risk: D, side: Side
) -> None:
    instrument = Instrument("T", D("0.01"), D("0.001"), D("0.001"), 10)
    long_stop = min(entry, second) - stop_gap
    stop = long_stop if side is Side.LONG else max(entry, second) + stop_gap
    if stop <= 0:
        return
    legs = [EntryLeg(entry, D("0.5"), False), EntryLeg(second, D("0.5"), False)]
    try:
        size = size_position(side, legs, stop, risk, instrument)
    except TradeSetupError:
        return  # below the minimum lot: refusing is the correct outcome
    assert size.risk_actual <= risk
    assert all(e.qty >= instrument.min_qty for e in size.entries)
