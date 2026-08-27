from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.data.models import Candle


class BinanceFuturesError(RuntimeError):
    """Raised for Binance Futures API, network, or response errors."""


class BinanceFuturesClient:
    """Read-only Binance USD-M Futures client using public market-data endpoints."""

    DEFAULT_BASE_URL = "https://fapi.binance.com"
    TIMEFRAME_MAP = {
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10,
        retries: int = 2,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.logger = logger or logging.getLogger(__name__)
        self._instruments_cache: list[str] | None = None

    def connect(self) -> None:
        """Verify public API access by loading tradable symbols."""

        symbols = self.get_instruments()
        self.logger.info("Binance Futures connected")
        self.logger.info("Binance Futures available symbols: %d", len(symbols))

    def close(self) -> None:
        self._instruments_cache = None
        self.logger.info("Binance Futures connection closed")

    def get_instruments(self) -> list[str]:
        if self._instruments_cache is not None:
            return list(self._instruments_cache)
        payload = self._request_json("/fapi/v1/exchangeInfo")
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise BinanceFuturesError("Binance Futures exchangeInfo did not contain symbols")
        names = sorted(
            {
                str(item["symbol"])
                for item in symbols
                if isinstance(item, dict)
                and item.get("status") == "TRADING"
                and isinstance(item.get("symbol"), str)
                and item["symbol"]
            }
        )
        if not names:
            raise BinanceFuturesError("Binance Futures returned no tradable symbols")
        self._instruments_cache = names
        return list(names)

    def search_symbols(self, query: str) -> list[str]:
        query = query.strip()
        matches = [
            symbol
            for symbol in self.get_instruments()
            if not query or query.casefold() in symbol.casefold()
        ]
        if query:
            self.logger.info("Binance Futures symbols matching %r: %d", query, len(matches))
        else:
            self.logger.info("Binance Futures symbols available: %d", len(matches))
        for symbol in matches[:50]:
            self.logger.info("%s", symbol)
        if len(matches) > 50:
            self.logger.info("... and %d more", len(matches) - 50)
        return matches

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        interval = self._interval(timeframe)
        limit = max(3, min(int(count), 1500))
        self.logger.info("Fetching %s candles for %s", timeframe.upper(), symbol.upper())
        payload = self._request_json(
            "/fapi/v1/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        )
        if not isinstance(payload, list):
            raise BinanceFuturesError(f"Binance Futures klines response for {symbol} was invalid")

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        candles: list[Candle] = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                continue
            try:
                open_time_ms = int(row[0])
                close_time_ms = int(row[6])
                candles.append(
                    Candle(
                        symbol=symbol.upper(),
                        timeframe=timeframe,
                        timestamp=datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        is_closed=close_time_ms < now_ms,
                    )
                )
            except (TypeError, ValueError, OverflowError) as exc:
                self.logger.warning("Skipping invalid Binance Futures candle for %s: %s", symbol, exc)
        if not candles:
            raise BinanceFuturesError(f"Binance Futures returned no usable candles for {symbol}")
        self.logger.info("Binance Futures candle data normalized")
        return candles

    def get_current_price(self, symbol: str) -> float:
        payload = self._request_json(
            "/fapi/v1/ticker/price",
            params={"symbol": symbol.upper()},
        )
        price = payload.get("price") if isinstance(payload, dict) else None
        try:
            return float(price)
        except (TypeError, ValueError) as exc:
            raise BinanceFuturesError(
                f"Binance Futures returned no current price for {symbol}"
            ) from exc

    def get_24h_volume_usd(self, symbol: str) -> float | None:
        payload = self._request_json(
            "/fapi/v1/ticker/24hr",
            params={"symbol": symbol.upper()},
        )
        if not isinstance(payload, dict):
            return None
        quote_volume = payload.get("quoteVolume")
        try:
            return float(quote_volume)
        except (TypeError, ValueError):
            return None

    def _interval(self, timeframe: str) -> str:
        try:
            return self.TIMEFRAME_MAP[timeframe.lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported Binance Futures timeframe: {timeframe}") from exc

    def _request_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "one-hour-engulfing-scanner/1.0",
            },
            method="GET",
        )
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("code") is not None:
                    message = payload.get("msg", payload)
                    raise BinanceFuturesError(f"Binance Futures API error for {path}: {message}")
                return payload
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    self.logger.warning("Binance Futures HTTP %s for %s; retrying", exc.code, path)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise BinanceFuturesError(
                    f"Binance Futures HTTP {exc.code} for {path}: {body[:300]}"
                ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.retries:
                    self.logger.warning("Binance Futures request failed for %s; retrying: %s", path, exc)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise BinanceFuturesError(f"Binance Futures request failed for {path}: {exc}") from exc
        raise BinanceFuturesError(f"Binance Futures request failed after retries: {path}")
