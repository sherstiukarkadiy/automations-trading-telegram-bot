from decimal import Decimal as D

from tmbot.core.models import Side
from tmbot.core.prop_risk import (
    Exposure,
    Limit,
    OpenPosition,
    PropRules,
    PropState,
    Verdict,
    check_new_trade,
    hedge_conflicts,
)

PRO_25K = PropRules(starting_balance=D(25_000), daily_loss_pct=D("0.03"), max_dd_pct=D("0.05"))


def test_fresh_account_small_trade_ok() -> None:
    state = PropState(balance=D(25_000), sod_balance=D(25_000))
    result = check_new_trade(PRO_25K, state, [], new_loss_at_stop=D(250))
    assert result.verdict is Verdict.OK
    daily, drawdown = result.limits
    assert daily.limit is Limit.DAILY and daily.floor == D(24_250)  # 25,000 − 3%
    assert drawdown.limit is Limit.DRAWDOWN and drawdown.floor == D(23_750)  # 25,000 − 5%


def test_daily_limit_breach_blocks() -> None:
    # already −$500 closed today, an open position risks $200 more, the new trade $100
    state = PropState(balance=D(24_500), sod_balance=D(25_000))
    result = check_new_trade(PRO_25K, state, [Exposure("ETH", D(200))], new_loss_at_stop=D(100))
    assert result.verdict is Verdict.BLOCK
    assert result.equity_worst == D(24_200)


def test_buffer_zone_warns() -> None:
    state = PropState(balance=D(25_000), sod_balance=D(25_000))
    # daily limit $750, 10% buffer = $75; a $700 loss leaves $50 < $75
    result = check_new_trade(PRO_25K, state, [], new_loss_at_stop=D(700))
    assert result.verdict is Verdict.WARN
    assert result.max_new_loss == D(675)


def test_position_without_stop_blocks() -> None:
    state = PropState(balance=D(25_000), sod_balance=D(25_000))
    result = check_new_trade(PRO_25K, state, [Exposure("SOL", None)], D(10))
    assert result.verdict is Verdict.BLOCK
    assert any("без SL" in r for r in result.reasons)


def test_drawdown_floor_binds_after_losses() -> None:
    # balance 24,000, SOD 24,000: daily floor 23,280, but the drawdown floor 23,750 is higher
    state = PropState(balance=D(24_000), sod_balance=D(24_000))
    result = check_new_trade(PRO_25K, state, [], new_loss_at_stop=D(260))
    assert result.verdict is Verdict.BLOCK
    assert result.limits[1].headroom == D(-10)  # 23,740 − 23,750


def test_max_new_loss_respects_tightest_limit() -> None:
    state = PropState(balance=D(24_000), sod_balance=D(24_000))
    result = check_new_trade(PRO_25K, state, [], new_loss_at_stop=D(0))
    # drawdown: 24,000 − 23,750 − 0.10·1,250 = 125; daily: 24,000 − 23,280 − 72 = 648
    assert result.max_new_loss == D(125)


def test_trailing_drawdown_capped_at_start() -> None:
    rules = PropRules(D(100_000), D("0.05"), D("0.08"), trailing_dd=True)
    state = PropState(D(110_000), D(110_000), high_water_mark=D(115_000))
    floor, _ = rules.drawdown_floor(state)
    assert floor == D(100_000)


def test_hedge_against_prop_detected() -> None:
    positions = [OpenPosition("propr-25k", "BTC", Side.LONG)]
    assert hedge_conflicts("BTC", Side.SHORT, {"bybit-main"}, {"propr-25k"}, positions)


def test_hedge_from_external_into_prop_detected() -> None:
    positions = [OpenPosition("bybit-main", "BTC", Side.SHORT)]
    assert hedge_conflicts("BTC", Side.LONG, {"propr-25k"}, {"propr-25k"}, positions)


def test_same_direction_mirror_is_fine() -> None:
    positions = [OpenPosition("propr-25k", "BTC", Side.LONG)]
    assert hedge_conflicts("BTC", Side.LONG, {"bybit-main"}, {"propr-25k"}, positions) == []
