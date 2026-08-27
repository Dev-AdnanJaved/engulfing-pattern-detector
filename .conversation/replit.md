# 1H Engulfing Scanner

OANDA-first trading signal scanner with an optional read-only MetaTrader 5 market-data provider.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pytest -q` — run strategy, provider, and SQLite tests
- `python main.py --once` — run one scanner cycle
- `python main.py --mt5-search US100` — search symbols exposed by the MT5 terminal
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required OANDA/Telegram env: `OANDA_API_KEY`, `OANDA_ACCOUNT_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional MT5 env: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `app/data/oanda.py` — existing OANDA client
- `app/data/mt5.py` — optional read-only MT5 terminal client
- `app/data/manager.py` — provider registry and isolated availability
- `app/scanner/scanner.py` — provider-aware polling and shared strategy evaluation
- `app/strategy/` — existing body-only engulfing rules and signal evaluator
- `app/storage/database.py` — SQLite history and provider-aware deduplication
- `config/config.yaml` — OANDA/MT5 runtime configuration

## Architecture decisions

- MT5 imports lazily and remains optional so OANDA-only operation works where an MT5 terminal cannot run.
- Both providers return the same normalized `Candle` model and share the existing strategy and Telegram notifier.
- MT5 all-symbol mode fails closed unless configured include patterns prevent scanning irrelevant terminal symbols.
- Provider failures are logged per target and do not stop another provider's scan.

## Product

Scans closed 1H candles for body-only engulfing setups confirmed by closed 4H
and 1D candles, using OANDA and optionally a Skilling MT5 terminal as market
data sources. Sends qualifying alerts through the existing Telegram bot.

## User preferences

- Keep the OANDA implementation, strategy, Telegram behavior, and data-only boundary intact.
- Do not add trade execution or extra indicators without explicit approval.

## Gotchas

- MetaTrader5 Python integration requires a compatible Windows MT5 terminal/VPS; it is not assumed to work in the Linux runtime.
- Never place MT5 credentials in `config.yaml`; use `.env` or Replit Secrets.
- Use `/mt5symbols QUERY` or `--mt5-search QUERY` to discover broker-specific symbol names before adding them to config.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
