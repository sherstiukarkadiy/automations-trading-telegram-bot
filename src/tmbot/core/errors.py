"""Core exceptions."""

from __future__ import annotations


class TradeSetupError(ValueError):
    """A signal cannot be turned into a trade (invalid prices, size below the lot, ...).

    The message is user-facing and is shown in the bot/CLI as is.
    """
