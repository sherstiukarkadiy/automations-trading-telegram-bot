from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tmbot.core.models import Instrument, Side, TakeProfit
from tmbot.core.takeprofits import far_take_profit, plan_take_profits, resolve_shares

BELOW_ALL = D(1)  # a reference price no LONG take-profit has been reached at


def test_equal_shares_when_none_given() -> None:
    shares = resolve_shares([TakeProfit(D(1)), TakeProfit(D(2)), TakeProfit(D(3))])
    assert sum(shares) == 1
    assert shares[0] == shares[1]


def test_signal_shares_and_rest_split() -> None:
    tps = [TakeProfit(D(1), D("0.5")), TakeProfit(D(2)), TakeProfit(D(3))]
    assert resolve_shares(tps) == [D("0.5"), D("0.25"), D("0.25")]


def test_partial_explicit_shares_normalized() -> None:
    tps = [TakeProfit(D(1), D("0.3")), TakeProfit(D(2), D("0.3"))]
    assert resolve_shares(tps) == [D("0.5"), D("0.5")]


def test_shares_over_one_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_shares([TakeProfit(D(1), D("0.7")), TakeProfit(D(2), D("0.5"))])


def test_split_sums_to_total(coin: Instrument) -> None:
    tps = [TakeProfit(D(110)), TakeProfit(D(120)), TakeProfit(D(130))]
    orders = plan_take_profits(Side.LONG, tps, D(10), coin, BELOW_ALL)
    assert [o.qty for o in orders] == [D("3.33"), D("3.33"), D("3.34")]
    assert [o.level for o in orders] == [0, 1, 2]


def test_passed_level_share_moves_to_next(coin: Instrument) -> None:
    tps = (TakeProfit(D(110)), TakeProfit(D(120)), TakeProfit(D(130)))
    # price is already 115: TP1 is passed, its share moves to TP2
    orders = plan_take_profits(Side.LONG, tps, D(9), coin, reference_price=D(115))
    assert [o.level for o in orders] == [1, 2]
    assert [o.qty for o in orders] == [D(6), D(3)]


def test_all_levels_passed_gives_no_orders(coin: Instrument) -> None:
    tps = (TakeProfit(D(110)),)
    assert plan_take_profits(Side.LONG, tps, D(9), coin, reference_price=D(111)) == []


def test_far_take_profit() -> None:
    near, far = TakeProfit(D(110)), TakeProfit(D(130))
    assert far_take_profit(Side.LONG, (near, far)) == far
    assert far_take_profit(Side.SHORT, (TakeProfit(D(90)), TakeProfit(D(80)))) == TakeProfit(D(80))
    assert far_take_profit(Side.LONG, ()) is None


def test_tiny_parts_merge_and_respect_min_lot(btc: Instrument) -> None:
    tps = [TakeProfit(D(1)), TakeProfit(D(2)), TakeProfit(D(3))]
    orders = plan_take_profits(Side.LONG, tps, D("0.002"), btc, D("0.5"))
    assert sum(o.qty for o in orders) == D("0.002")
    assert all(o.qty >= btc.min_qty for o in orders)


@given(
    qty=st.decimals(min_value=D("0.01"), max_value=D(1000), places=2),
    n=st.integers(min_value=1, max_value=6),
)
def test_parts_always_sum_exactly(qty: D, n: int) -> None:
    instrument = Instrument("T", D("0.01"), D("0.01"), D("0.01"), 10)
    tps = [TakeProfit(D(100 + i)) for i in range(n)]
    orders = plan_take_profits(Side.LONG, tps, qty, instrument, BELOW_ALL)
    assert sum(o.qty for o in orders) == qty
