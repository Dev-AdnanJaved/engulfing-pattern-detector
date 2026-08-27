# 1H Engulfing Scanner

A small Python signal scanner for OANDA with an optional, read-only MetaTrader 5 provider. It scans only closed candles and generates a signal when the exact V1 strategy passes:

- Closed 1H bullish or bearish engulfing pattern, using candle bodies only.
- Latest closed 4H candle confirms the same direction.
- Latest closed 1D candle confirms the same direction.

There are no extra indicators, scoring rules, orders, execution, dashboard, Redis, or PostgreSQL.

## Setup

1. Copy `.env.example` to `.env` for local use, or add the same names as Replit Secrets:

   - `OANDA_API_KEY`
   - `OANDA_ACCOUNT_ID`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `MT5_LOGIN` (only when MT5 is enabled)
   - `MT5_PASSWORD` (only when MT5 is enabled)
   - `MT5_SERVER` (only when MT5 is enabled)

   MT5 also requires a Windows-compatible machine or VPS with the official
   MetaTrader 5 terminal installed. On that host, install the optional provider
   dependency with `uv sync --extra mt5`. The package is loaded only when MT5
   is enabled, so OANDA-only operation remains available on Linux.

2. Keep `config/config.yaml` in manual mode to scan the listed OANDA instruments, or set `scanner.symbol_mode` to `all` to retrieve instruments from the account.

3. To add MT5 instruments, enable the `mt5` section. Manual mode scans the
   exact names listed there. Do not guess the broker's symbol suffix; use
   `/mt5symbols US100` in Telegram or:

   ```bash
   python main.py --mt5-search US100
   ```

   For `mt5.symbol_mode: all`, set `all_mode_filter.include_patterns` to
   configured prefixes or wildcard patterns. Empty include patterns scan no
   MT5 symbols intentionally.

4. Install dependencies:

   ```bash
   uv sync
   ```

5. Run one scan:

   ```bash
   python main.py --once
   ```

6. Run continuously every 15 minutes:

   ```bash
   python main.py
   ```

When all-instrument mode is enabled, the scanner loads every instrument
available to the OANDA account. It sends Telegram status messages when each
scan cycle starts and finishes, and sends qualifying signals immediately
without waiting for the rest of the instruments to finish.

## Volume filter behavior

The optional volume filter is disabled by default. OANDA's candle `volume` is tick count, not reliable USD trading volume, so this V1 never converts or substitutes it. When the filter is enabled and OANDA cannot provide a reliable 24-hour USD value, that instrument is skipped safely and a warning is logged.

## Storage and duplicate prevention

Signal history is stored in `data/scanner.db`. Each signal is reserved using:

`provider + symbol + timeframe + closed candle timestamp + direction`

The unique SQLite key survives restarts and prevents the same alert from being sent repeatedly.

## Tests

Run the focused strategy and persistence tests:

```bash
pytest -q
```