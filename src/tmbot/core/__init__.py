"""Pure trading core: models, position sizing, take-profits, leverage and prop risk checks.

The core knows nothing about exchanges, Telegram or the database. All money and prices
are ``Decimal``.
"""
