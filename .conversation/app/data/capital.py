from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.data.models import Candle


class CapitalError(RuntimeError):
    """Raised for Capital.com API, network, or response errors."""


class CapitalClient:
    """Read-only Capital.com demo API client.

    The client intentionally exposes only market-data operations. It never
    calls Capital.com order or position endpoints.
    """

    BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"
    TIMEFRAME_MAP = {
        "1h": "HOUR",
        "4h": "HOUR_4",
        "1d": "DAY",
    }

    def __init__(
        self,
        identifier: str,
        api_key: str,
        password: str,
        *,
        base_url: str = BASE_URL,
        timeout_seconds: float = 10,
        retries: int = 2,
        logger: logging.Logger | None = None,
    ) -> None:
        if not identifier:
            raise ValueError("CAPITAL_IDENTIFIER is required when Capital.com is enabled")
        if not api_key:
            raise ValueError("CAPITAL_API_KEY is required when Capital.com is enabled")
        if not password:
            raise ValueError("CAPITAL_PASSWORD is required when Capital.com is enabled")
        self.identifier = identifier
        self.api_key = api_key
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.logger = logger or logging.getLogger(__name__)
        self._cst: str | None = None
        self._security_token: str | None = None

    def connect(self) -> None:
        """Start a demo session and verify that both session headers exist."""

        _, headers = self._request_json(
            "/session",
            method="POST",
            payload={
                "identifier": self.identifier,
                "password": self.password,
                "encryptedPassword": False,
            },
            include_session=False,
        )
        self._cst = _header(headers, "CST")
        self._security_token = _header(headers, "X-SECURITY-TOKEN")
        if not self._cst or not self._security_token:
            raise CapitalError("Capital.com session response did not contain authorization headers")
        self.logger.info("Capital.com demo API connected for %s", self.identifier)

    def close(self) -> None:
        self._cst = None
        self._security_token = None

    def get_instruments(self) -> list[str]:
        markets = self._markets()
        epics = sorted(
            {
                str(item["epic"])
                for item in markets
                if isinstance(item, dict) and isinstance(item.get("epic"), str) and item["epic"]
            }
        )
        if not epics:
            raise CapitalError("Capital.com returned no usable market epics")
        return epics

    def search_symbols(self, query: str) -> list[str]:
        query = query.strip()
        markets = self._markets(query)
        matches = sorted(
            {
                str(item["epic"])
                for item in markets
                if isinstance(item, dict) and isinstance(item.get("epic"), str) and item["epic"]
            }
        )
        self.logger.info("Capital.com symbols matching %r: %d", query, len(matches))
        for item in markets:
            if isinstance(item, dict) and item.get("epic") in matches:
                name = item.get("instrumentName") or item.get("name")
                if name:
                    self.logger.info("Capital.com symbol match: %s (%s)", item["epic"], name)
        return matches

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        resolution = self._resolution(timeframe)
        payload, _ = self._request_json(
            f"/prices/{symbol}",
            params={"resolution": resolution, "max": count},
        )
        prices = payload.get("prices")
        if not isinstance(prices, list):
            raise CapitalError(f"Capital.com prices response for {symbol} did not contain prices")

        normalized: list[Candle] = []
        for index, item in enumerate(prices):
            if not isinstance(item, dict):
                continue
            try:
                timestamp = _parse_timestamp(item.get("snapshotTimeUTC") or item["snapshotTime"])
                normalized.append(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=timestamp,
                        open=_price(item["openPrice"]),
                        high=_price(item["highPrice"]),
                        low=_price(item["lowPrice"]),
                        close=_price(item["closePrice"]),
                        volume=None,
                        is_closed=_closed_status(item, index, len(prices)),
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.logger.warning("Skipping invalid Capital.com candle for %s: %s", symbol, exc)
        return normalized

    def get_current_price(self, symbol: str) -> float:
        payload, _ = self._request_json(f"/markets/{symbol}")
        snapshot = payload.get("snapshot", payload)
        if not isinstance(snapshot, dict):
            raise CapitalError(f"Capital.com returned no current price for {symbol}")
        bid = _positive_float(snapshot.get("bid"))
        ask = _positive_float(snapshot.get("offer") or snapshot.get("ask"))
        last = _positive_float(
            snapshot.get("lastTradedPrice")
            or snapshot.get("lastTraded")
            or snapshot.get("mid")
        )
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        if last is not None:
            return last
        raise CapitalError(f"Capital.com returned no usable current price for {symbol}")

    def get_24h_volume_usd(self, symbol: str) -> float | None:
        del symbol
        return None

    def _markets(self, query: str = "") -> list[dict[str, Any]]:
        payload, _ = self._request_json("/markets", params={"searchTerm": query})
        markets = payload.get("markets")
        if not isinstance(markets, list):
            raise CapitalError("Capital.com markets response did not contain markets")
        return [item for item in markets if isinstance(item, dict)]

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        include_session: bool = True,
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        self._ensure_session(include_session)
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{query}",
            headers=self._headers(include_session),
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
        )
        if payload is not None:
            request.add_header("Content-Type", "application/json")

        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw_body = response.read().decode("utf-8")
                    body = json.loads(raw_body) if raw_body else {}
                    headers = response.headers
                if not isinstance(body, dict):
                    raise CapitalError(f"Invalid Capital.com response shape from {path}")
                return body, headers
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    self.logger.warning("Capital.com HTTP %s for %s; retrying", exc.code, path)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise CapitalError(f"Capital.com HTTP {exc.code} for {path}: {body[:300]}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.retries:
                    self.logger.warning("Capital.com request failed for %s; retrying: %s", path, exc)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise CapitalError(f"Capital.com request failed for {path}: {exc}") from exc
        raise CapitalError(f"Capital.com request failed after retries: {path}")

    def _headers(self, include_session: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-CAP-API-KEY": self.api_key,
            "User-Agent": "one-hour-engulfing-scanner/1.0",
        }
        if include_session:
            headers["CST"] = self._cst or ""
            headers["X-SECURITY-TOKEN"] = self._security_token or ""
        return headers

    def _ensure_session(self, include_session: bool) -> None:
        if include_session and (not self._cst or not self._security_token):
            raise CapitalError("Capital.com API is not connected")

    @classmethod
    def _resolution(cls, timeframe: str) -> str:
        try:
            return cls.TIMEFRAME_MAP[timeframe.lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported Capital.com timeframe: {timeframe}") from exc


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    value = headers.get(name) or headers.get(name.lower())
    return str(value) if value else None


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Capital.com timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _price(value: Any) -> float:
    if isinstance(value, Mapping):
        bid = _positive_float(value.get("bid"))
        ask = _positive_float(value.get("ask"))
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        value = value.get("value") or value.get("lastTraded")
    parsed = _positive_float(value)
    if parsed is None:
        raise ValueError("Capital.com returned an invalid candle price")
    return parsed


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _closed_status(item: Mapping[str, Any], index: int, total: int) -> bool:
    for key in ("complete", "isComplete", "closed", "isClosed"):
        if key in item:
            return bool(item[key])
    # The API may include the currently forming bar as the newest price.
    return index < total - 1