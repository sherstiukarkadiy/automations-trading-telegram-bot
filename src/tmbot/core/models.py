"""Core domain models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for LONG, -1 for SHORT, so that PnL = sign * (exit - entry) * qty."""
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG

    def is_beyond(self, price: Decimal, level: Decimal) -> bool:
        """True if ``price`` has reached or passed ``level`` in the profit direction.

        For LONG that means price >= level, for SHORT price <= level.
        """
        return self.sign * (price - level) >= 0


@dataclass(frozen=True, slots=True)
class Instrument:
    """Contract parameters on a specific exchange."""

    asset: str  # normalized asset name: "BTC"
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_leverage: int
    maintenance_margin_rate: Decimal = Decimal("0.005")
    exchange_symbol: str = ""  # contract name on the exchange: "BTCUSDT", "BTC", ...
    settle_coin: str = "USDT"
    min_notional: Decimal = Decimal(0)  # minimum order value in settle_coin

    def __post_init__(self) -> None:
        if self.tick_size <= 0 or self.qty_step <= 0:
            raise ValueError(f"{self.asset}: tick_size and qty_step must be positive")
        if self.max_leverage < 1:
            raise ValueError(f"{self.asset}: max_leverage must be >= 1")

    def round_qty_down(self, qty: Decimal) -> Decimal:
        """Round a quantity down to the lot step, so rounding can never increase risk."""
        if qty <= 0:
            return Decimal(0)
        steps = (qty / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        return steps * self.qty_step

    def round_price(self, price: Decimal) -> Decimal:
        """Round a price to the nearest tick."""
        steps = (price / self.tick_size).to_integral_value()
        return steps * self.tick_size


@dataclass(frozen=True, slots=True)
class TakeProfit:
    price: Decimal
    share: Decimal | None = None  # fraction of the position from the signal; None = split equally


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: str
    message: str  # user-facing

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


@dataclass(frozen=True, slots=True)
class Signal:
    """A normalized trade signal, produced by the parser from text or a screenshot."""

    asset: str
    side: Side
    stop_loss: Decimal
    entries: tuple[Decimal, ...] = ()  # empty = enter at market
    take_profits: tuple[TakeProfit, ...] = ()  # ordered from nearest to farthest
    leverage: int | None = None
    entry_weights: tuple[Decimal, ...] | None = None  # None = split equally between entries

    @property
    def is_market(self) -> bool:
        return not self.entries

    def resolved_weights(self) -> tuple[Decimal, ...]:
        """Entry weights summing to 1 (equal split unless the signal specifies them)."""
        if self.entry_weights is not None:
            return self.entry_weights
        n = max(len(self.entries), 1)
        return tuple(Decimal(1) / n for _ in range(n))

    def validate(self) -> list[ValidationIssue]:
        """Check the signal for internal consistency. An empty list means it is valid."""
        issues: list[ValidationIssue] = []
        issues += self._validate_stop_and_entries()
        issues += self._validate_weights()
        issues += self._validate_take_profits()
        if self.leverage is not None and self.leverage < 1:
            issues.append(ValidationIssue("leverage", "плече має бути ≥ 1"))
        return issues

    def _validate_stop_and_entries(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if self.stop_loss <= 0:
            issues.append(ValidationIssue("stop_loss", "SL має бути додатним"))
        for i, entry in enumerate(self.entries):
            if entry <= 0:
                issues.append(ValidationIssue(f"entries[{i}]", "ціна входу має бути додатною"))
            elif self.side.is_beyond(self.stop_loss, entry):
                issues.append(ValidationIssue(f"entries[{i}]", "SL з неправильного боку від входу"))
        return issues

    def _validate_weights(self) -> list[ValidationIssue]:
        weights = self.entry_weights
        if weights is None:
            return []
        if len(weights) != len(self.entries):
            return [ValidationIssue("entry_weights", "кількість часток ≠ кількості входів")]
        if any(w <= 0 for w in weights):
            return [ValidationIssue("entry_weights", "частки мають бути додатними")]
        if sum(weights) != 1:
            return [ValidationIssue("entry_weights", "сума часток входів ≠ 1")]
        return []

    def _validate_take_profits(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        prices = [tp.price for tp in self.take_profits]
        if prices != sorted(prices, reverse=self.side is Side.SHORT):
            issues.append(ValidationIssue("take_profits", "тейки не впорядковані в напрямку угоди"))

        for i, tp in enumerate(self.take_profits):
            name = f"take_profits[{i}]"
            if self.side.is_beyond(self.stop_loss, tp.price):
                issues.append(ValidationIssue(name, "тейк з боку стопа"))
            blocking = next(
                (j for j, e in enumerate(self.entries) if self.side.is_beyond(e, tp.price)), None
            )
            if blocking is not None:
                issues.append(ValidationIssue(name, f"тейк не далі за вхід entries[{blocking}]"))

        shares = [tp.share for tp in self.take_profits if tp.share is not None]
        if any(share <= 0 or share > 1 for share in shares):
            issues.append(ValidationIssue("take_profits", "частка тейку має бути в (0, 1]"))
        elif sum(shares, Decimal(0)) > 1:
            issues.append(ValidationIssue("take_profits", "сума часток тейків > 100%"))
        return issues


@dataclass(frozen=True, slots=True)
class AccountRisk:
    """How much an account may lose on a trade: a fixed amount or a percentage of balance."""

    usd: Decimal | None = None
    pct: Decimal | None = None  # fraction: 0.01 = 1%

    def __post_init__(self) -> None:
        if (self.usd is None) == (self.pct is None):
            raise ValueError("exactly one of usd or pct must be set")
        value = self.usd if self.usd is not None else self.pct
        if value is not None and value <= 0:
            raise ValueError("risk must be positive")

    @classmethod
    def fixed(cls, usd: Decimal) -> AccountRisk:
        return cls(usd=usd)

    @classmethod
    def percent(cls, pct: Decimal) -> AccountRisk:
        """``pct`` is a fraction: ``Decimal("0.01")`` means 1%."""
        return cls(pct=pct)

    def amount(self, realized_balance: Decimal) -> Decimal:
        """Risk in money for an account whose balance from closed trades is given."""
        if self.usd is not None:
            return self.usd
        return realized_balance * (self.pct or Decimal(0))
