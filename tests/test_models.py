from decimal import Decimal as D

import pytest

from tmbot.core.models import AccountRisk, Instrument, Side, Signal, TakeProfit


def long_signal(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "asset": "BTC",
        "side": Side.LONG,
        "stop_loss": D(90),
        "entries": (D(100), D(95)),
        "take_profits": (TakeProfit(D(110)), TakeProfit(D(120))),
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


def fields_of(signal: Signal) -> list[str]:
    return [issue.field for issue in signal.validate()]


def test_valid_signal_has_no_issues() -> None:
    assert long_signal().validate() == []


def test_stop_on_wrong_side_of_one_entry() -> None:
    assert fields_of(long_signal(stop_loss=D(97))) == ["entries[1]"]


def test_take_profits_must_be_ordered() -> None:
    signal = long_signal(take_profits=(TakeProfit(D(120)), TakeProfit(D(110))))
    assert "take_profits" in fields_of(signal)


def test_take_profit_not_beyond_entry() -> None:
    assert "take_profits[0]" in fields_of(long_signal(take_profits=(TakeProfit(D(98)),)))


def test_short_take_profits_descending_ok() -> None:
    signal = Signal(
        asset="BTC",
        side=Side.SHORT,
        stop_loss=D(110),
        entries=(D(100),),
        take_profits=(TakeProfit(D(90)), TakeProfit(D(80))),
    )
    assert signal.validate() == []


def test_take_profit_shares_over_100_percent() -> None:
    signal = long_signal(take_profits=(TakeProfit(D(110), D("0.7")), TakeProfit(D(120), D("0.5"))))
    assert any("100%" in issue.message for issue in signal.validate())


def test_entry_weights_must_match_entries() -> None:
    assert fields_of(long_signal(entry_weights=(D(1),))) == ["entry_weights"]
    assert long_signal(entry_weights=(D("0.3"), D("0.7"))).validate() == []


def test_resolved_weights_default_to_equal_split() -> None:
    assert long_signal().resolved_weights() == (D("0.5"), D("0.5"))
    assert long_signal(entries=()).resolved_weights() == (D(1),)


def test_side_is_beyond() -> None:
    assert Side.LONG.is_beyond(D(101), D(100))
    assert Side.LONG.is_beyond(D(100), D(100))
    assert not Side.LONG.is_beyond(D(99), D(100))
    assert Side.SHORT.is_beyond(D(99), D(100))


def test_account_risk() -> None:
    assert AccountRisk.fixed(D(200)).amount(D(10_000)) == D(200)
    assert AccountRisk.percent(D("0.01")).amount(D(25_000)) == D(250)
    with pytest.raises(ValueError):
        AccountRisk()
    with pytest.raises(ValueError):
        AccountRisk(usd=D(1), pct=D("0.01"))
    with pytest.raises(ValueError):
        AccountRisk.fixed(D(0))


def test_instrument_rejects_zero_steps() -> None:
    with pytest.raises(ValueError):
        Instrument("BTC", D(0), D("0.001"), D("0.001"), 10)


def test_instrument_rounding(btc: Instrument) -> None:
    assert btc.round_qty_down(D("0.0719")) == D("0.071")
    assert btc.round_qty_down(D(-1)) == 0
    assert btc.round_price(D("60100.07")) == D("60100.1")
