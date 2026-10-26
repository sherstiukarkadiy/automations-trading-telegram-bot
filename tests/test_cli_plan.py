import pytest

from tmbot.cli.plan import main

TWO_ENTRY_LONG = [
    "long", "--price", "61800", "--entry", "62000", "--entry", "61200",
    "--sl", "60100", "--tp", "63000", "--tp", "65500",
]  # fmt: skip


def test_plan_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*TWO_ENTRY_LONG, "--risk-usd", "200"]) == 0
    out = capsys.readouterr().out
    assert "Ризик" in out
    assert "TP2" in out


def test_plan_prop_block(capsys: pytest.CaptureFixture[str]) -> None:
    args = ["long", "--price", "61800", "--sl", "60100", "--risk-usd", "900"]
    main([*args, "--prop", "--balance", "25000"])
    assert "БЛОК" in capsys.readouterr().out


def test_plan_invalid_signal(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["long", "--price", "100", "--entry", "100", "--sl", "105", "--risk-usd", "10"])
    assert code == 1
    assert "План не складено" in capsys.readouterr().out


def test_plan_unknown_asset_needs_contract_params(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["long", "--symbol", "PEPE", "--price", "1", "--sl", "0.9", "--risk-usd", "10"])
    assert code == 1
    assert "--qty-step" in capsys.readouterr().out
