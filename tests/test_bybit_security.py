from tmbot.exchanges.bybit.security import evaluate_key

GOOD = {
    "readOnly": 0,
    "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": [], "Spot": []},
    "ips": ["203.0.113.10"],
    "uta": 1,
}


def test_good_key_passes() -> None:
    report = evaluate_key(GOOD, testnet=False)
    assert report.ok and not report.warnings


def test_withdraw_rejected() -> None:
    info = {**GOOD, "permissions": {**GOOD["permissions"], "Wallet": ["Withdraw"]}}
    report = evaluate_key(info, testnet=True)
    assert not report.ok and "Withdraw" in report.problems[0]


def test_transfer_rejected() -> None:
    info = {**GOOD, "permissions": {**GOOD["permissions"], "Wallet": ["AccountTransfer"]}}
    assert not evaluate_key(info, testnet=True).ok


def test_read_only_rejected() -> None:
    assert not evaluate_key({**GOOD, "readOnly": 1}, testnet=True).ok


def test_no_ip_warn_on_testnet_block_on_mainnet() -> None:
    info = {**GOOD, "ips": ["*"]}
    assert evaluate_key(info, testnet=True).ok
    assert evaluate_key(info, testnet=True).warnings
    assert not evaluate_key(info, testnet=False).ok


def test_non_uta_rejected() -> None:
    assert not evaluate_key({**GOOD, "uta": 0}, testnet=True).ok


def test_extra_permissions_warned() -> None:
    info = {**GOOD, "permissions": {**GOOD["permissions"], "Spot": ["SpotTrade"]}}
    report = evaluate_key(info, testnet=False)
    assert report.ok and "Spot" in report.warnings[0]
