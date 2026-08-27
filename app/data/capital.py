from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
        symbol_epics: Mapping[str, Any] | None = None,
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
        self.symbol_epics = {
            str(symbol): _epic_value(epic)
            for symbol, epic in (symbol_epics or {}).items()
            if _epic_value(epic)
        }
        self.logger = logger or logging.getLogger(__name__)
        self._cst: str | None = None
        self._security_token: str | None = None

    def connect(self) -> None:
        """Start a demo session and verify that both session headers exist."""

        self.logger.info("Connecting to Capital.com")
        self._cst = None
        self._security_token = None
        try:
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
        except CapitalError as exc:
            raise CapitalError(f"Capital.com authentication failed: {exc}") from exc
        self._cst = _header(headers, "CST")
        self._security_token = _header(headers, "X-SECURITY-TOKEN")
        if not self._cst or not self._security_token:
            raise CapitalError("Capital.com authentication failed: missing session headers")
        self.logger.info("Capital.com authentication successful")
        self.logger.info("Capital.com connected")
        try:
            self.logger.info("Capital.com available instruments: %d", len(self.get_instruments()))
        except CapitalError as exc:
            # Session authentication is still valid if optional discovery is
            # temporarily unavailable. The scanner can use configured epics.
            self.logger.warning("Capital.com instrument count unavailable: %s", exc)

    def close(self) -> None:
        if self._cst and self._security_token:
            request = Request(
                f"{self.base_url}/session",
                headers=self._headers(True),
                method="DELETE",
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds):
                    pass
            except (HTTPError, URLError, TimeoutError, OSError):
                self.logger.warning("Capital.com session close request failed")
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
        self.logger.info("Searching Capital.com instruments for: %s", query or "(all)")
        markets = self._markets(query)
        matches = sorted(
            {
                str(item["epic"])
                for item in markets
                if isinstance(item, dict) and isinstance(item.get("epic"), str) and item["epic"]
            }
        )
        self.logger.info("Matching instruments:")
        if not matches:
            self.logger.info("(none)")
            return matches
        for item in markets:
            if not isinstance(item, dict) or item.get("epic") not in matches:
                continue
            name = item.get("instrumentName") or item.get("name")
            if name:
                self.logger.info("%s / %s", item["epic"], name)
            else:
                self.logger.info("%s", item["epic"])
        return matches

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        resolution = self._resolution(timeframe)
        epic = self._epic_for(symbol)
        self.logger.info("Fetching %s candles for %s", timeframe.upper(), epic)
        try:
            payload, _ = self._request_json(
                f"/prices/{quote(epic, safe='')}",
                params={"resolution": resolution, "max": count},
            )
        except CapitalError as exc:
            message = str(exc)
            if "HTTP 404" in message:
                raise CapitalError(f"Capital.com symbol not found: {symbol}") from exc
            if "rate limit" in message.casefold():
                raise
            raise CapitalError(f"Capital.com candle request failed: {exc}") from exc
        prices = payload.get("prices")
        if not isinstance(prices, list):
            raise CapitalError(f"Capital.com prices response for {symbol} did not contain prices")

        parsed_items: list[tuple[datetime, Mapping[str, Any]]] = []
        for item in prices:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = _parse_timestamp(item.get("snapshotTimeUTC") or item["snapshotTime"])
                parsed_items.append((timestamp, item))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.logger.warning("Skipping invalid Capital.com candle for %s: %s", symbol, exc)
        parsed_items.sort(key=lambda item: item[0])
        newest_timestamp = parsed_items[-1][0] if parsed_items else None
        normalized: list[Candle] = []
        for timestamp, item in parsed_items:
            try:
                normalized.append(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=timestamp,
                        open=_price(item["openPrice"]),
                        high=_price(item["highPrice"]),
                        low=_price(item["lowPrice"]),
                        close=_price(item["closePrice"]),
                        volume=_volume(item),
                        is_closed=_closed_status(item, timestamp, newest_timestamp),
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.logger.warning("Skipping invalid Capital.com candle for %s: %s", symbol, exc)
        self.logger.info("Capital.com candle data normalized")
        return normalized

    def get_current_price(self, symbol: str) -> float:
        epic = self._epic_for(symbol)
        try:
            payload, _ = self._request_json(f"/markets/{quote(epic, safe='')}")
        except CapitalError as exc:
            if "HTTP 404" in str(exc):
                raise CapitalError(f"Capital.com symbol not found: {symbol}") from exc
            raise
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

    def _epic_for(self, symbol: str) -> str:
        return self.symbol_epics.get(symbol, symbol)

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        include_session: bool = True,
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        reauthenticated = False
        for attempt in range(self.retries + 1):
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
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw_body = response.read().decode("utf-8")
                    body = json.loads(raw_body) if raw_body else {}
                    headers = response.headers
                if not isinstance(body, dict):
                    raise CapitalError(f"Invalid Capital.com response shape from {path}")
                return body, headers
            except HTTPError as exc:
                exc.read()
                if include_session and exc.code in {401, 403} and not reauthenticated:
                    self.logger.warning("Capital.com session expired; re-authenticating")
                    reauthenticated = True
                    self.connect()
                    continue
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    if exc.code == 429:
                        self.logger.error("Capital.com rate limit reached; retrying")
                    else:
                        self.logger.warning("Capital.com HTTP %s for %s; retrying", exc.code, path)
                    time.sleep(_retry_delay(exc, attempt))
                    continue
                if exc.code == 429:
                    raise CapitalError(f"Capital.com rate limit reached for {path}") from exc
                raise CapitalError(f"Capital.com HTTP {exc.code} for {path}") from exc
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


def _volume(item: Mapping[str, Any]) -> float | None:
    for key in ("volume", "lastTradedVolume"):
        value = _non_negative_float(item.get(key))
        if value is not None:
            return value
    return None


def _non_negative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _closed_status(
    item: Mapping[str, Any],
    timestamp: datetime,
    newest_timestamp: datetime | None,
) -> bool:
    for key in ("complete", "isComplete", "closed", "isClosed"):
        if key in item:
            value = item[key]
            if isinstance(value, str):
                return value.strip().casefold() not in {"false", "0", "no"}
            return bool(value)
    # The API may include the currently forming bar as the newest price.
    return newest_timestamp is None or timestamp < newest_timestamp


def _epic_value(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("epic")
    return str(value).strip() if value else None


def _retry_delay(error: HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(0.0, min(float(retry_after), 30.0))
    except (TypeError, ValueError):
        return 0.5 * (attempt + 1)