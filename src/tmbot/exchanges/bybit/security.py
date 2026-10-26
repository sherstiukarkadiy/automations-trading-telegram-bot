"""Bybit API key permission check, run before a key is stored (a spec requirement).

A key that can withdraw or transfer funds is rejected: the bot must only be able to trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tmbot.exchanges.bybit.client import BybitHttp, JsonDict

FORBIDDEN_WALLET = {"Withdraw", "AccountTransfer", "SubMemberTransfer"}
REQUIRED_CONTRACT = {"Order", "Position"}


@dataclass(slots=True)
class KeyReport:
    problems: list[str] = field(default_factory=list)  # block storing the key; user-facing
    warnings: list[str] = field(default_factory=list)  # user-facing
    ips: list[str] = field(default_factory=list)
    days_left: int | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def evaluate_key(info: JsonDict, *, testnet: bool) -> KeyReport:
    """Assess a ``/v5/user/query-api`` response."""
    report = KeyReport()
    perms: dict[str, list[str]] = info.get("permissions", {}) or {}

    if int(info.get("readOnly", 0)) == 1:
        report.problems.append("ключ тільки для читання — торгувати ним не можна")

    wallet = set(perms.get("Wallet", []))
    if wallet & FORBIDDEN_WALLET:
        bad = ", ".join(sorted(wallet & FORBIDDEN_WALLET))
        report.problems.append(f"ключ має небезпечні права Wallet: {bad} — створи ключ без них")

    contract = set(perms.get("ContractTrade", []))
    missing = REQUIRED_CONTRACT - contract
    if missing:
        report.problems.append(f"бракує прав ContractTrade: {', '.join(sorted(missing))}")

    if int(info.get("uta", 0)) != 1:
        report.problems.append("акаунт не Unified Trading Account")

    ips = [ip for ip in info.get("ips", []) or [] if ip]
    report.ips = ips
    if not ips or ips == ["*"]:
        msg = "ключ не прив'язаний до IP"
        if testnet:
            report.warnings.append(msg + " (для testnet допустимо, для бойового — обов'язково)")
        else:
            report.problems.append(msg + " — прив'яжи до IP сервера")

    allowed = {"ContractTrade", "Derivatives", "Wallet"}  # Wallet is checked above
    extra = sorted(k for k, v in perms.items() if v and k not in allowed)
    if extra:
        report.warnings.append("зайві права, які боту не потрібні: " + ", ".join(extra))

    if info.get("deadlineDay") not in (None, ""):
        report.days_left = int(info["deadlineDay"])
    return report


async def check_key(http: BybitHttp) -> KeyReport:
    info = await http.get("/v5/user/query-api")
    return evaluate_key(info, testnet=http.testnet)
