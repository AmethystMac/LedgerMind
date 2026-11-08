# LedgerMind

A personal, hands-off crypto trading bot. It finds profitable on-chain wallets from public DEX
trade data, buys on Binance spot when several of them agree, trades only from its own small pot
(paper mode first), and writes a plain-language daily report to Notion.

Personal experiment, not financial advice.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv installs the Python version pinned in
`.python-version`.

```bash
uv sync                                          # install from uv.lock
uv run pytest                                    # run the tests (offline)
uv run ruff check && uv run ruff format --check  # lint + format check
uv run ruff format                               # apply formatting
```
