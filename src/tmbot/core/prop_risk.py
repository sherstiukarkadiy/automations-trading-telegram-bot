"""Prop account risk control (Propr rules).

Propr breaches an account on equity (balance + floating P&L), with no grace period:
- daily loss: equity ≥ SOD balance − pct·SOD balance (resets at 00:00 UTC);
- max drawdown: static — equity ≥ start·(1 − pct);
  trailing — equity ≥ min(HWM − pct·start, start).

Before a trade we compute the worst case: every open position and resting entry order,
plus the new trade, closes at its stop.

    Equity_worst = Balance_now − Σ loss_at_SL(open) − loss_at_SL(new) − fees
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tmbot.core.models import Side

DEFAULT_BUFFER = Decimal("0.10")  # warn when less than 10% of a limit is left


class Limit(StrEnum):
    DAILY = "daily"
    DRAWDOWN = "drawdown"

    @property
    def label(self) -> str:
        return "денний ліміт" if self is Limit.DAILY else "загальний drawdown"


class Verdict(StrEnum):
    OK = "ok"
    WARN = "warn"  # within limits but inside the safety buffer
    BLOCK = "block"  # the worst case touches a limit, or some exposure has no stop


@dataclass(frozen=True, slots=True)
class PropState:
    balance: Decimal  # balance from closed trades, now
    sod_balance: Decimal  # balance at 00:00 UTC
    high_water_mark: Decimal | None = None  # trailing drawdown only


@dataclass(frozen=True, slots=True)
class PropRules:
    starting_balance: Decimal
    daily_loss_pct: Decimal  # fraction: 0.03 = 3%
    max_dd_pct: Decimal  # fraction: 0.05 = 5%
    trailing_dd: bool = False

    def daily_floor(self, state: PropState) -> tuple[Decimal, Decimal]:
        """(equity floor, limit amount) for the current day."""
        amount = state.sod_balance * self.daily_loss_pct
        return state.sod_balance - amount, amount

    def drawdown_floor(self, state: PropState) -> tuple[Decimal, Decimal]:
        """(equity floor, limit amount) for the overall drawdown."""
        amount = self.starting_balance * self.max_dd_pct
        if not self.trailing_dd:
            return self.starting_balance - amount, amount
        hwm = state.high_water_mark or self.starting_balance
        return min(hwm - amount, self.starting_balance), amount

    def floors(self, state: PropState) -> dict[Limit, tuple[Decimal, Decimal]]:
        return {Limit.DAILY: self.daily_floor(state), Limit.DRAWDOWN: self.drawdown_floor(state)}


@dataclass(frozen=True, slots=True)
class Exposure:
    """An open position or resting entry order on the prop account."""

    asset: str
    loss_at_stop: Decimal | None  # positive; None means it has no stop


@dataclass(frozen=True, slots=True)
class LimitCheck:
    limit: Limit
    floor: Decimal
    limit_amount: Decimal
    headroom: Decimal  # equity_worst − floor


@dataclass(frozen=True, slots=True)
class PropCheckResult:
    verdict: Verdict
    equity_worst: Decimal
    limits: tuple[LimitCheck, ...]
    max_new_loss: Decimal  # how much the new trade may lose without entering the buffer
    reasons: tuple[str, ...] = ()  # user-facing


def check_new_trade(
    rules: PropRules,
    state: PropState,
    open_exposure: list[Exposure],
    new_loss_at_stop: Decimal,
    fees: Decimal = Decimal(0),
    buffer_pct: Decimal = DEFAULT_BUFFER,
) -> PropCheckResult:
    reasons: list[str] = []
    verdict = Verdict.OK

    unprotected = sorted({e.asset for e in open_exposure if e.loss_at_stop is None})
    if unprotected:
        verdict = Verdict.BLOCK
        reasons.append(f"позиції без SL: {', '.join(unprotected)} — ризик невизначений")

    open_loss = sum((e.loss_at_stop or Decimal(0) for e in open_exposure), Decimal(0))
    equity_without_new = state.balance - open_loss
    equity_worst = equity_without_new - new_loss_at_stop - fees

    checks: list[LimitCheck] = []
    allowances: list[Decimal] = []
    for limit, (floor, amount) in rules.floors(state).items():
        check = LimitCheck(limit, floor, amount, equity_worst - floor)
        checks.append(check)
        allowances.append(equity_without_new - fees - floor - buffer_pct * amount)

        if check.headroom <= 0:
            verdict = Verdict.BLOCK
            reasons.append(f"{limit.label}: найгірша equity {equity_worst:.2f} ≤ межі {floor:.2f}")
        elif check.headroom < buffer_pct * amount:
            if verdict is Verdict.OK:
                verdict = Verdict.WARN
            reasons.append(
                f"{limit.label}: до межі лишається {check.headroom:.2f} "
                f"(< {buffer_pct:.0%} від {amount:.2f})"
            )

    return PropCheckResult(
        verdict=verdict,
        equity_worst=equity_worst,
        limits=tuple(checks),
        max_new_loss=max(min(allowances), Decimal(0)),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class OpenPosition:
    account_id: str
    asset: str
    side: Side


def hedge_conflicts(
    asset: str,
    side: Side,
    target_accounts: set[str],
    prop_accounts: set[str],
    positions: list[OpenPosition],
) -> list[str]:
    """Hedging against a prop account is forbidden; returns user-facing conflicts.

    A conflict is a new trade in the direction opposite to an open position on the same
    asset, where at least one side (that position or a target account) is a prop account.
    """
    conflicts: list[str] = []
    targets_include_prop = bool(target_accounts & prop_accounts)
    for p in positions:
        if p.asset != asset or p.side is not side.opposite:
            continue
        if p.account_id in prop_accounts:
            conflicts.append(f"на prop-акаунті {p.account_id} відкрито {p.side.value} {asset}")
        elif targets_include_prop:
            conflicts.append(
                f"на {p.account_id} відкрито {p.side.value} {asset}, а угода йде на prop"
            )
    return conflicts
