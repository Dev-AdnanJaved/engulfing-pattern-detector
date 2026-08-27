from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from app.data.models import Candle


class Mt5Error(RuntimeError):
    """Raised when the MetaTrader 5 terminal cannot provide market data."""


class Mt5Client:
    """Read-only MetaTrader 5 market-data client.

    The MetaTrader5 package is imported lazily so OANDA-only deployments can
    continue to run on Linux or on machines without an MT5 terminal.
    """

    TIMEFRAME_MAP = {
        "1h": "TIMEFRAME_H1",
        "4h": "TIMEFRAME_H4",
        "1d": "TIMEFRAME_D1",
    }

    def __init__(
        self,
        login: str | int,
        password: str,
        server: str,
        *,
        mt5_module: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        try:
            self.login = int(login)
        except (TypeError, ValueError) as exc:
            raise ValueError("MT5_LOGIN must be an integer") from exc
        if not password:
            raise ValueError("MT5_PASSWORD is required when MT5 is enabled")
        if not server:
            raise ValueError("MT5_SERVER is required when MT5 is enabled")

        self.password = password
        self.server = server
        self.logger = logger or logging.getLogger(__name__)
        self._mt5 = mt5_module if mt5_module is not None else _load_mt5_module()
        self._connected = False

    def connect(self) -> None:
        """Initialize the terminal, log in, and verify account access."""

        try:
            initialized = self._mt5.initialize(
                login=self.login,
                password=self.password,
                server=self.server,
            )
        except Exception as exc:
            raise Mt5Error(f"MT5 initialization failed: {exc}") from exc
        if not initialized:
            raise Mt5Error(f"MT5 initialization failed: {self._last_error()}")

        try:
            account = self._mt5.account_info()
        except Exception as exc:
            self._shutdown()
            raise Mt5Error(f"MT5 account verification failed: {exc}") from exc
        if account is None:
            self._shutdown()
            raise Mt5Error(f"MT5 account verification failed: {self._last_error()}")

        self._connected = True
        account_number = _field(account, "login", self.login)
        account_server = _field(account, "server", self.server)
        self.logger.info("MT5 connected: account=%s server=%s", account_number, account_server)

        symbols = self.get_instruments()
        self.logger.info("MT5 available symbols: %d", len(symbols))

    def close(self) -> None:
        if self._connected:
            self._shutdown()
            self._connected = False
            self.logger.info("MT5 connection closed")

    def get_instruments(self) -> list[str]:
        self._ensure_connected()
        try:
            raw_symbols = self._mt5.symbols_get()
        except Exception as exc:
            raise Mt5Error(f"MT5 symbol discovery failed: {exc}") from exc
        if raw_symbols is None:
            raise Mt5Error(f"MT5 symbol discovery failed: {self._last_error()}")

        names = sorted(
            {
                name
                for item in raw_symbols
                for name in [_field(item, "name", None)]
                if isinstance(name, str) and name
            }
        )
        if not names:
            raise Mt5Error("MT5 returned no usable symbols")
        return names

    def search_symbols(self, query: str) -> list[str]:
        """Return and log symbols containing the supplied text."""

        query = query.strip()
        matches = [
            symbol
            for symbol in self.get_instruments()
            if not query or query.casefold() in symbol.casefold()
        ]
        if query:
            self.logger.info("MT5 symbols matching %r: %d", query, len(matches))
            for symbol in matches:
                self.logger.info("MT5 symbol match: %s", symbol)
        return matches

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        self._ensure_connected()
        timeframe_constant = self._timeframe_constant(timeframe)
        self._select_symbol(symbol)
        try:
            rates = self._mt5.copy_rates_from_pos(symbol, timeframe_constant, 0, count)
        except Exception as exc:
            raise Mt5Error(f"MT5 candle retrieval failed for {symbol} {timeframe}: {exc}") from exc
        if rates is None:
            raise Mt5Error(
                f"MT5 candle retrieval failed for {symbol} {timeframe}: {self._last_error()}"
            )

        rows = list(rates)
        candles: list[Candle] = []
        for index, row in enumerate(rows):
            try:
                timestamp = datetime.fromtimestamp(
                    float(_required_field(row, "time")),
                    tz=timezone.utc,
                )
                candles.append(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=timestamp,
                        open=float(_required_field(row, "open")),
                        high=float(_required_field(row, "high")),
                        low=float(_required_field(row, "low")),
                        close=float(_required_field(row, "close")),
                        volume=_volume_from_rate(row),
                        # MT5 returns the current bar at the end of the array.
                        is_closed=index < len(rows) - 1,
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.logger.warning("Skipping invalid MT5 candle for %s: %s", symbol, exc)
        return candles

    def get_current_price(self, symbol: str) -> float:
        self._ensure_connected()
        self._select_symbol(symbol)
        try:
            tick = self._mt5.symbol_info_tick(symbol)
        except Exception as exc:
            raise Mt5Error(f"MT5 price retrieval failed for {symbol}: {exc}") from exc
        if tick is None:
            raise Mt5Error(f"MT5 price retrieval failed for {symbol}: {self._last_error()}")

        bid = _positive_float(_field(tick, "bid", None))
        ask = _positive_float(_field(tick, "ask", None))
        last = _positive_float(_field(tick, "last", None))
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        if last is not None:
            return last
        raise Mt5Error(f"MT5 returned no usable current price for {symbol}")

    def get_24h_volume_usd(self, symbol: str) -> float | None:
        """MT5 tick/real volume is not a reliable USD volume value."""

        del symbol
        return None

    def _select_symbol(self, symbol: str) -> None:
        try:
            selected = self._mt5.symbol_select(symbol, True)
        except Exception as exc:
            raise Mt5Error(f"MT5 symbol selection failed for {symbol}: {exc}") from exc
        if not selected:
            raise Mt5Error(f"MT5 symbol not found or unavailable: {symbol}")

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise Mt5Error("MT5 is not connected")

    def _timeframe_constant(self, timeframe: str) -> Any:
        constant_name = self.TIMEFRAME_MAP.get(timeframe.lower())
        if constant_name is None:
            raise ValueError(f"Unsupported MT5 timeframe: {timeframe}")
        try:
            return getattr(self._mt5, constant_name)
        except AttributeError as exc:
            raise Mt5Error(f"MT5 module does not expose {constant_name}") from exc

    def _last_error(self) -> str:
        try:
            return str(self._mt5.last_error())
        except Exception:
            return "unknown terminal error"

    def _shutdown(self) -> None:
        try:
            self._mt5.shutdown()
        except Exception:
            self.logger.exception("MT5 terminal shutdown failed")


def _load_mt5_module() -> Any:
    try:
        return importlib.import_module("MetaTrader5")
    except (ImportError, OSError) as exc:
        raise Mt5Error(
            "MetaTrader5 is unavailable in this runtime. Install the official "
            "package on a Windows machine or VPS with the MT5 terminal installed."
        ) from exc


def _field(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _required_field(value: Any, name: str) -> Any:
    result = _field(value, name, None)
    if result is None:
        raise KeyError(name)
    return result


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _volume_from_rate(rate: Any) -> float | None:
    real_volume = _positive_float(_field(rate, "real_volume", None))
    if real_volume is not None:
        return real_volume
    return _positive_float(_field(rate, "tick_volume", None))