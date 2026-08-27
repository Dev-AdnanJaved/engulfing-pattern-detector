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


class KrakenFuturesError(RuntimeError):
    """Raised for Kraken Futures API, network, or response errors."""


class KrakenFuturesClient:
    """Read-only Kraken Futures client using public market-data endpoints."""

    DEFAULT_BASE_URL = "https://futures.kraken.com"
    TIMEFRAME_MAP = {
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }
    RESOLUTION_MS = {
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        tick_type: str = "trade",
        timeout_seconds: float = 10,
        retries: int = 2,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tick_type = tick_type.strip() or "trade"
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.logger = logger or logging.getLogger(__name__)
        self._instruments_cache: list[str] | None = None
        self._tickers_cache: dict[str, dict[str, Any]] | None = None

    def connect(self) -> None:
        """Verify public API access by loading tradable symbols."""

        symbols = self.get_instruments()
        self.logger.info("Kraken Futures connected")
        self.logger.info("Kraken Futures available symbols: %d", len(symbols))

    def close(self) -> None:
        self._instruments_cache = None
        self._tickers_cache = None
        self.logger.info("Kraken Futures connection closed")

    def get_instruments(self) -> list[str]:
        if self._instruments_cache is not None:
            return list(self._instruments_cache)
        payload = self._request_json("/derivatives/api/v3/instruments")
        instruments = payload.get("instruments")
        if not isinstance(instruments, list):
            raise KrakenFuturesError("Kraken Futures instruments response was invalid")
        names = sorted(
            {
                str(item["symbol"])
                for item in instruments
                if isinstance(item, dict)
                and item.get("tradeable") is True
                and isinstance(item.get("symbol"), str)
                and item["symbol"]
            }
        )
        if not names:
            raise KrakenFuturesError("Kraken Futures returned no tradable symbols")
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
            self.logger.info("Kraken Futures symbols matching %r: %d", query, len(matches))
        else:
            self.logger.info("Kraken Futures symbols available: %d", len(matches))
        for symbol in matches[:50]:
            self.logger.info("%s", symbol)
        if len(matches) > 50:
            self.logger.info("... and %d more", len(matches) - 50)
        return matches

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        resolution = self._resolution(timeframe)
        limit = max(3, min(int(count), 200))
        normalized_symbol = symbol.upper()
        self.logger.info("Fetching %s candles for %s", timeframe.upper(), normalized_symbol)
        payload = self._request_json(
            f"/api/charts/v1/{self.tick_type}/{normalized_symbol}/{resolution}",
            params={"count": limit},
        )
        rows = payload.get("candles")
        if not isinstance(rows, list):
            raise KrakenFuturesError(
                f"Kraken Futures candles response for {normalized_symbol} was invalid"
            )

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        period_ms = self.RESOLUTION_MS[timeframe.lower()]
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                open_time_ms = int(row["time"])
                candles.append(
                    Candle(
                        symbol=normalized_symbol,
                        timeframe=timeframe,
                        timestamp=datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        is_closed=(open_time_ms + period_ms) <= now_ms,
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.logger.warning(
                    "Skipping invalid Kraken Futures candle for %s: %s",
                    normalized_symbol,
                    exc,
                )
        if not candles:
            raise KrakenFuturesError(
                f"Kraken Futures returned no usable candles for {normalized_symbol}"
            )
        self.logger.info("Kraken Futures candle data normalized")
        return candles

    def get_current_price(self, symbol: str) -> float:
        ticker = self._ticker(symbol.upper())
        for key in ("last", "markPrice", "indexPrice"):
            value = ticker.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        raise KrakenFuturesError(
            f"Kraken Futures returned no current price for {symbol.upper()}"
        )

    def get_24h_volume_usd(self, symbol: str) -> float | None:
        ticker = self._ticker(symbol.upper())
        quote_volume = ticker.get("volumeQuote")
        try:
            return float(quote_volume)
        except (TypeError, ValueError):
            return None

    def _ticker(self, symbol: str) -> dict[str, Any]:
        tickers = self._load_tickers()
        ticker = tickers.get(symbol)
        if ticker is None:
            raise KrakenFuturesError(f"Kraken Futures returned no ticker for {symbol}")
        return ticker

    def _load_tickers(self) -> dict[str, dict[str, Any]]:
        if self._tickers_cache is not None:
            return self._tickers_cache
        payload = self._request_json("/derivatives/api/v3/tickers")
        rows = payload.get("tickers")
        if not isinstance(rows, list):
            raise KrakenFuturesError("Kraken Futures tickers response was invalid")
        tickers = {
            str(item["symbol"]): item
            for item in rows
            if isinstance(item, dict)
            and isinstance(item.get("symbol"), str)
            and item["symbol"]
        }
        if not tickers:
            raise KrakenFuturesError("Kraken Futures returned no tickers")
        self._tickers_cache = tickers
        return tickers

    def _resolution(self, timeframe: str) -> str:
        try:
            return self.TIMEFRAME_MAP[timeframe.lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported Kraken Futures timeframe: {timeframe}") from exc

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
                if isinstance(payload, dict) and payload.get("result") == "error":
                    message = payload.get("error", payload)
                    raise KrakenFuturesError(f"Kraken Futures API error for {path}: {message}")
                return payload
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    self.logger.warning("Kraken Futures HTTP %s for %s; retrying", exc.code, path)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise KrakenFuturesError(
                    f"Kraken Futures HTTP {exc.code} for {path}: {body[:300]}"
                ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.retries:
                    self.logger.warning(
                        "Kraken Futures request failed for %s; retrying: %s",
                        path,
                        exc,
                    )
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise KrakenFuturesError(f"Kraken Futures request failed for {path}: {exc}") from exc
        raise KrakenFuturesError(f"Kraken Futures request failed after retries: {path}")
