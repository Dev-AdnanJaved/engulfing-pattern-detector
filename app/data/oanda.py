from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.data.models import Candle


class OandaError(RuntimeError):
    """Raised for OANDA API, network, or response errors."""


class OandaClient:
    """Small OANDA Practice API client that returns normalized candles."""

    BASE_URL = "https://api-fxpractice.oanda.com/v3"

    def __init__(
        self,
        api_key: str,
        account_id: str,
        timeout_seconds: float = 10,
        retries: int = 2,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not api_key or not account_id:
            raise ValueError("OANDA_API_KEY and OANDA_ACCOUNT_ID are required")
        self.api_key = api_key
        self.account_id = account_id
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.logger = logger or logging.getLogger(__name__)

    def _request_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.BASE_URL}{path}{query}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "one-hour-engulfing-scanner/1.0",
            },
            method="GET",
        )

        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise OandaError(f"Invalid response shape from OANDA: {path}")
                return payload
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    self.logger.warning("OANDA HTTP %s for %s; retrying", exc.code, path)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise OandaError(f"OANDA HTTP {exc.code} for {path}: {body[:300]}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.retries:
                    self.logger.warning("OANDA request failed for %s; retrying: %s", path, exc)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise OandaError(f"OANDA request failed for {path}: {exc}") from exc

        raise OandaError(f"OANDA request failed after retries: {path}")

    def get_instruments(self) -> list[str]:
        payload = self._request_json(f"/accounts/{self.account_id}/instruments")
        instruments = payload.get("instruments")
        if not isinstance(instruments, list):
            raise OandaError("OANDA instruments response did not contain an instruments list")
        names = [item.get("name") for item in instruments if isinstance(item, dict) and isinstance(item.get("name"), str)]
        if not names:
            raise OandaError("OANDA returned no usable instruments")
        return names

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        granularity = self._granularity(timeframe)
        payload = self._request_json(
            f"/instruments/{symbol}/candles",
            {"granularity": granularity, "count": count, "price": "M"},
        )
        candles = payload.get("candles")
        if not isinstance(candles, list):
            raise OandaError(f"OANDA candles response for {symbol} did not contain a candles list")

        normalized: list[Candle] = []
        for item in candles:
            if not isinstance(item, dict) or not isinstance(item.get("time"), str):
                continue
            mid = item.get("mid")
            if not isinstance(mid, dict):
                continue
            try:
                normalized.append(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=self._parse_timestamp(item["time"]),
                        open=float(mid["o"]),
                        high=float(mid["h"]),
                        low=float(mid["l"]),
                        close=float(mid["c"]),
                        # OANDA's candle volume is tick count, not USD trading volume.
                        volume=None,
                        is_closed=bool(item.get("complete", False)),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                self.logger.warning("Skipping invalid OANDA candle for %s: %s", symbol, exc)
        return normalized

    def get_current_price(self, symbol: str) -> float:
        payload = self._request_json(
            f"/accounts/{self.account_id}/pricing",
            {"instruments": symbol},
        )
        prices = payload.get("prices")
        if not isinstance(prices, list) or not prices:
            raise OandaError(f"OANDA returned no current price for {symbol}")
        price = prices[0]
        if not isinstance(price, dict):
            raise OandaError(f"Invalid current price response for {symbol}")
        try:
            bid = float(price["bids"][0]["price"])
            ask = float(price["asks"][0]["price"])
            return (bid + ask) / 2
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OandaError(f"Invalid bid/ask response for {symbol}") from exc

    def get_24h_volume_usd(self, symbol: str) -> Optional[float]:
        """Return reliable USD volume when a provider endpoint supplies it.

        OANDA's instruments and candle endpoints do not provide reliable
        24-hour USD trading volume. Returning None makes the optional filter
        fail closed rather than mislabeling tick volume as USD volume.
        """

        del symbol
        return None

    @staticmethod
    def _granularity(timeframe: str) -> str:
        mapping = {"1h": "H1", "4h": "H4", "1d": "D"}
        try:
            return mapping[timeframe.lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported OANDA timeframe: {timeframe}") from exc

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)