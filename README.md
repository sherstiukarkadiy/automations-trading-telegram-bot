# Trading Mirror Bot

A Telegram bot that turns a trading signal into matching positions on multiple Bybit
and Propr accounts, with a separate risk calculation for each account.

## Status

Stage 1 of the spec — **core**:

| Module | What it does |
| --- | --- |
| `core/models.py` | `Signal`, `Instrument`, `Side`, signal validation, risk in $ or % |
| `core/sizing.py` | Entry type (market/limit), size with averaging, round down, fees |
| `core/takeprofits.py` | TP shares from the signal or equal split, skip already-hit TPs, far TP |
| `core/leverage.py` | Leverage from the signal or the exchange max, clipping, liquidation vs SL check |
| `core/prop_risk.py` | Worst-case SL scenario, Propr daily limit and drawdown, 10% buffer, no hedging |

The core has no external dependencies. All money and prices use `Decimal`.

## Install

```bash
uv venv && uv pip install -e ".[dev]"
```

## Automated tests

```bash
uv run pytest -v
uv run ruff check . && uv run mypy
```

## Manual check: trade calculator

Shows what the bot will do with a signal: entries, sizes, risk, leverage, take-profits, and the Propr check.
No exchanges or Telegram required.

```bash
# LONG BTC, two entries, $200 risk
uv run python -m tmbot.cli.plan long --price 61800 --entry 62000 --entry 61200 \
    --sl 60100 --tp 63000 --tp 64200 --tp 65500 --risk-usd 200

# Prop $25K, 1% risk, 50% at TP1, already −$500 today, open position risking $300
uv run python -m tmbot.cli.plan long --price 61800 --entry 62000 --entry 61200 \
    --sl 60100 --tp 63000:0.5 --tp 64200 --tp 65500 \
    --risk-pct 1 --balance 24500 --prop --sod-balance 25000 --open-loss 300
```

All options: `uv run python -m tmbot.cli.plan --help`.

Stage 2 — **Bybit and trade execution**:

| Module | What it does |
| --- | --- |
| `exchanges/base.py` | Shared `ExchangeAdapter` interface, order/position types, errors |
| `exchanges/bybit/client.py` | Async V5 client: HMAC signing, retries (network, 429/5xx, 10006), time sync |
| `exchanges/bybit/adapter.py` | Instruments, price, UTA balance, positions, orders, leverage, close |
| `exchanges/bybit/security.py` | Rejects keys with Withdraw/Transfer, read-only, non-UTA; IP binding |
| `execution/planner.py` | Signal + account data → a concrete order plan |
| `execution/executor.py` | Place the plan, intermediate TPs after a market entry, close |

## Check on Bybit testnet

1. Sign up at [testnet.bybit.com](https://testnet.bybit.com) and fund a test USDT balance.
2. API → Create New Key → System-generated: permissions **Contracts → Orders, Positions**, no Wallet.
3. In PowerShell (you can skip the keys — the CLI will prompt for them hidden):

```powershell
$env:BYBIT_API_KEY = "..."
$env:BYBIT_API_SECRET = "..."

uv run python -m tmbot.cli.bybit check
uv run python -m tmbot.cli.bybit open long --sl 60100 --tp 63000 --tp 65500 --risk-usd 20 --dry-run
uv run python -m tmbot.cli.bybit open long --sl 60100 --tp 63000 --tp 65500 --risk-usd 20
uv run python -m tmbot.cli.bybit positions
uv run python -m tmbot.cli.bybit close BTC
```

`--dry-run` only prints the plan. A live account requires `--mainnet` and a manual confirmation.

## Next

3. Propr adapter (beta) + risk control on live data
4. Signal parser via Claude API and the Telegram flow
