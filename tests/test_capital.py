import io
import json
import logging
from datetime import datetime, timezone
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from app.data.capital import CapitalClient, CapitalError
from app.data.manager import DataProviderManager, ProviderUnavailable
from app.data.models import Candle
from app.scanner.scanner import ScanTarget, Scanner
from app.strategy.signal import evaluate_signal, make_signal_id


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self._payload = payload
        self.status = status
        self.headers = headers or {}

    def read(self):
        if isinstance(self._payload, (bytes, bytearray)):
            return bytes(self._payload)
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False


class FakeHTTPError(HTTPError):
    def __init__(self, code, body=b"", headers=None):
        header_map = Message()
        for key, value in (headers or {}).items():
            header_map[key] = str(value)
        super().__init__(
            url="https://demo-api-capital.backend-capital.com/api/v1/test",
            code=code,
            msg="error",
            hdrs=header_map,
            fp=io.BytesIO(body),
        )


def _price(bid, ask=None):
    ask = bid if ask is None else ask
    return {"bid": bid, "ask": ask}


def session_headers():
    return {"CST": "cst-token", "X-SECURITY-TOKEN": "sec-token"}


def connected_client(monkeypatch, handler, **kwargs):
    monkeypatch.setattr("app.data.capital.urlopen", handler)
    client = CapitalClient(
        "demo-user",
        "api-key",
        "secret-password",
        retries=kwargs.pop("retries", 1),
        symbol_epics=kwargs.pop("symbol_epics", {"US100": {"epic": "US100"}}),
        logger=logging.getLogger("test.capital"),
        **kwargs,
    )
    client.connect()
    return client


def test_authentication_stores_session_headers_without_logging_password(monkeypatch, caplog):
    calls = []

    def urlopen(request, timeout=0):
        calls.append((request.get_method(), request.full_url, dict(request.header_items())))
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({"accountId": "demo"}, session_headers())
        if "/markets" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100", "instrumentName": "US Tech 100"}]})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    with caplog.at_level(logging.INFO):
        client = connected_client(monkeypatch, urlopen)

    assert client._cst == "cst-token"
    assert client._security_token == "sec-token"
    assert any(method == "POST" and url.endswith("/session") for method, url, _ in calls)
    assert "secret-password" not in caplog.text
    assert "api-key" not in caplog.text
    assert "Capital.com authentication successful" in caplog.text
    assert "Capital.com connected" in caplog.text


def test_authentication_failure_is_explicit(monkeypatch):
    def urlopen(request, timeout=0):
        del request, timeout
        raise FakeHTTPError(401, b'{"errorCode":"error.invalid.details"}')

    monkeypatch.setattr("app.data.capital.urlopen", urlopen)
    client = CapitalClient("demo-user", "api-key", "secret-password", retries=0)

    with pytest.raises(CapitalError, match="authentication failed"):
        client.connect()


def test_symbol_discovery_and_search_do_not_assume_us100_name(monkeypatch, caplog):
    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "searchTerm=US100" in request.full_url:
            return FakeResponse(
                {
                    "markets": [
                        {"epic": "US100", "instrumentName": "US Tech 100"},
                        {"epic": "US500", "instrumentName": "US 500"},
                    ]
                }
            )
        if "/markets" in request.full_url:
            return FakeResponse(
                {
                    "markets": [
                        {"epic": "US100", "instrumentName": "US Tech 100"},
                        {"epic": "NAS100", "instrumentName": "Nasdaq 100"},
                    ]
                }
            )
        raise AssertionError(request.full_url)

    with caplog.at_level(logging.INFO):
        client = connected_client(monkeypatch, urlopen, symbol_epics={})
        assert client.get_instruments() == ["NAS100", "US100"]
        assert client.search_symbols("US100") == ["US100", "US500"]
        assert "Searching Capital.com instruments for: US100" in caplog.text
        assert "US100 / US Tech 100" in caplog.text


def test_symbol_mapping_uses_configured_epic(monkeypatch):
    seen = []

    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "/markets?" in request.full_url:
            return FakeResponse({"markets": []})
        if "/prices/" in request.full_url:
            seen.append(request.full_url)
            return FakeResponse(
                {
                    "prices": [
                        {
                            "snapshotTimeUTC": "2026-08-27T09:00:00",
                            "openPrice": _price(100),
                            "highPrice": _price(101),
                            "lowPrice": _price(99),
                            "closePrice": _price(100.5),
                            "lastTradedVolume": 10,
                        }
                    ]
                }
            )
        raise AssertionError(request.full_url)

    client = connected_client(
        monkeypatch,
        urlopen,
        symbol_epics={"US100": {"epic": "INDEX:US100"}},
    )
    candles = client.get_candles("US100", "1h", 1)
    assert "INDEX%3AUS100" in seen[0] or "INDEX:US100" in seen[0]
    assert candles[0].symbol == "US100"


def test_candles_map_timeframes_and_normalize_closed_status(monkeypatch):
    seen = []

    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "/markets?" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100"}]})
        if "/prices/" in request.full_url:
            seen.append(request.full_url)
            return FakeResponse(
                {
                    "prices": [
                        {
                            "snapshotTimeUTC": "2026-08-27T08:00:00Z",
                            "openPrice": _price(100, 100.2),
                            "highPrice": _price(106, 106.2),
                            "lowPrice": _price(99, 99.2),
                            "closePrice": _price(105, 105.2),
                            "volume": 20,
                        },
                        {
                            "snapshotTimeUTC": "2026-08-27T09:00:00Z",
                            "openPrice": _price(105, 105.2),
                            "highPrice": _price(108, 108.2),
                            "lowPrice": _price(103, 103.2),
                            "closePrice": _price(104, 104.2),
                            "volume": 25,
                        },
                    ]
                }
            )
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    candles = client.get_candles("US100", "4h", 2)

    assert "resolution=HOUR_4" in seen[0]
    assert all(isinstance(candle, Candle) for candle in candles)
    assert candles[0].timeframe == "4h"
    assert candles[0].timestamp == datetime(2026, 8, 27, 8, tzinfo=timezone.utc)
    assert candles[0].open == pytest.approx(100.1)
    assert candles[0].volume == 20
    assert candles[0].is_closed is True
    assert candles[1].is_closed is False


def test_explicit_complete_flag_marks_closed_candle(monkeypatch):
    seen = []

    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "/markets?" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100"}]})
        seen.append(request.full_url)
        return FakeResponse(
            {
                "prices": [
                    {
                        "snapshotTimeUTC": "2026-08-27T10:00:00Z",
                        "openPrice": _price(1),
                        "highPrice": _price(2),
                        "lowPrice": _price(0.5),
                        "closePrice": _price(1.5),
                        "complete": True,
                    }
                ]
            }
        )

    client = connected_client(monkeypatch, urlopen)
    candles = client.get_candles("US100", "1d", 1)
    assert "resolution=DAY" in seen[0]
    assert candles[0].is_closed is True
    assert CapitalClient._resolution("1h") == "HOUR"
    assert CapitalClient._resolution("4h") == "HOUR_4"


def test_symbol_not_found_on_candle_request(monkeypatch):
    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "/markets?" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100"}]})
        if "/prices/" in request.full_url:
            raise FakeHTTPError(404, b'{"errorCode":"error.not.found"}')
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen, retries=0)
    with pytest.raises(CapitalError, match="symbol not found"):
        client.get_candles("MISSING", "1h", 3)


def test_current_price_uses_capital_bid_offer(monkeypatch):
    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "/markets?" in request.full_url and "searchTerm" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100"}]})
        if request.full_url.endswith("/markets/US100"):
            return FakeResponse({"snapshot": {"bid": 99.0, "offer": 101.0}})
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    assert client.get_current_price("US100") == 100.0


def test_rate_limit_is_reported_after_retries(monkeypatch):
    attempts = {"count": 0}

    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            return FakeResponse({}, session_headers())
        if "/markets?" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100"}]})
        attempts["count"] += 1
        raise FakeHTTPError(429, b"rate limit", headers={"Retry-After": "0"})

    client = connected_client(monkeypatch, urlopen, retries=1)
    with pytest.raises(CapitalError, match="rate limit"):
        client.get_candles("US100", "1h", 2)
    assert attempts["count"] == 2


def test_expired_session_reauthenticates(monkeypatch):
    state = {"authed": 0, "price_calls": 0}

    def urlopen(request, timeout=0):
        if request.full_url.endswith("/session") and request.get_method() == "POST":
            state["authed"] += 1
            return FakeResponse({}, {"CST": f"cst-{state['authed']}", "X-SECURITY-TOKEN": "sec"})
        if "/markets?" in request.full_url:
            return FakeResponse({"markets": [{"epic": "US100"}]})
        if "/prices/" in request.full_url:
            state["price_calls"] += 1
            if state["price_calls"] == 1:
                raise FakeHTTPError(401, b"expired")
            return FakeResponse(
                {
                    "prices": [
                        {
                            "snapshotTimeUTC": "2026-08-27T10:00:00Z",
                            "openPrice": _price(1),
                            "highPrice": _price(2),
                            "lowPrice": _price(0.5),
                            "closePrice": _price(1.5),
                        }
                    ]
                }
            )
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    candles = client.get_candles("US100", "1h", 1)
    assert state["authed"] == 2
    assert len(candles) == 1


def test_capital_provider_failure_does_not_remove_other_providers():
    oanda = object()
    mt5 = object()
    providers = DataProviderManager({"oanda": oanda, "mt5": mt5})
    providers.mark_unavailable("capital", "auth failed")

    assert providers.get_provider("oanda") is oanda
    assert providers.get_provider("mt5") is mt5
    with pytest.raises(ProviderUnavailable, match="CAPITAL provider unavailable"):
        providers.get_provider("capital")


def test_provider_is_part_of_capital_signal_identity():
    def candle(open_price, close, offset, timeframe="1h"):
        return Candle(
            symbol="US100",
            timeframe=timeframe,
            timestamp=datetime(2026, 8, 27, 10 + offset, tzinfo=timezone.utc),
            open=open_price,
            high=max(open_price, close),
            low=min(open_price, close),
            close=close,
            is_closed=True,
        )

    signal = evaluate_signal(
        "US100",
        [candle(105, 100, 0), candle(99, 106, 1)],
        candle(4600, 4650, 0, "4h"),
        candle(4500, 4700, 0, "1d"),
        4639.18,
        signal_timeframe="1h",
        confirmation_4h="4h",
        confirmation_1d="1d",
        bullish_enabled=True,
        bearish_enabled=True,
        reject_doji=True,
        provider="capital",
    )

    assert signal is not None
    assert signal.provider == "capital"
    assert signal.id.startswith("capital|US100|")
    mt5_id = make_signal_id(
        "US100",
        "1h",
        signal.candle_time,
        "BUY",
        "mt5",
    )
    assert signal.id != mt5_id


def test_all_mode_requires_include_patterns_for_capital():
    class FakeCapital:
        def get_instruments(self):
            return ["US100", "NAS100", "EURUSD"]

        def get_24h_volume_usd(self, symbol):
            del symbol
            return None

    config = {
        "scanner": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["XAU_USD"],
            "volume_filter": {"enabled": False, "minimum_24h_volume_usd": 0},
        },
        "capital": {
            "enabled": True,
            "symbol_mode": "all",
            "all_mode_filter": {
                "include_patterns": ["US*", "NAS*"],
                "exclude_patterns": ["NAS100"],
            },
        },
        "data": {"history": {"1h": 3, "4h": 3, "1d": 3}},
        "strategy": {
            "signal_timeframe": "1h",
            "confirmation_4h": "4h",
            "confirmation_1d": "1d",
        },
        "pattern": {
            "bullish_engulfing": True,
            "bearish_engulfing": True,
            "body_only": True,
            "reject_doji": True,
        },
    }
    providers = DataProviderManager({"oanda": object(), "capital": FakeCapital()})
    scanner = Scanner(config, providers, SimpleNamespace(send_signal=lambda s: False), SimpleNamespace())

    assert scanner.resolve_targets() == [
        ScanTarget("XAU_USD", "oanda"),
        ScanTarget("US100", "capital"),
    ]


def test_oanda_and_mt5_continue_when_capital_scan_fails():
    class TrackingProvider:
        def __init__(self, name):
            self.name = name
            self.calls = []

        def get_candles(self, symbol, timeframe, count):
            self.calls.append((symbol, timeframe, count))
            return [
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    is_closed=True,
                )
            ]

        def get_current_price(self, symbol):
            del symbol
            return 100.5

        def get_24h_volume_usd(self, symbol):
            del symbol
            return None

    class FailingCapital(TrackingProvider):
        def get_candles(self, symbol, timeframe, count):
            self.calls.append((symbol, timeframe, count))
            raise CapitalError("Capital.com candle request failed")

    oanda = TrackingProvider("oanda")
    mt5 = TrackingProvider("mt5")
    capital = FailingCapital("capital")
    providers = DataProviderManager({"oanda": oanda, "mt5": mt5, "capital": capital})
    config = {
        "scanner": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["XAU_USD"],
            "scan_interval_seconds": 30,
            "volume_filter": {"enabled": False, "minimum_24h_volume_usd": 0},
        },
        "strategy": {
            "signal_timeframe": "1h",
            "confirmation_4h": "4h",
            "confirmation_1d": "1d",
        },
        "pattern": {
            "bullish_engulfing": True,
            "bearish_engulfing": True,
            "body_only": True,
            "reject_doji": True,
        },
        "data": {"history": {"1h": 4, "4h": 3, "1d": 3}},
    }
    scanner = Scanner(
        config,
        providers,
        SimpleNamespace(send_signal=lambda s: False),
        SimpleNamespace(
            reserve_signal=lambda s: True,
            mark_telegram_sent=lambda s: None,
            recently_sent=lambda symbol, provider, cooldown_minutes: False,
        ),
        logging.getLogger("test"),
    )
    scanner.scan_once(
        [
            ScanTarget("XAU_USD", "oanda"),
            ScanTarget("US100", "mt5"),
            ScanTarget("US100", "capital"),
        ]
    )

    assert oanda.calls == [("XAU_USD", "1h", 4)]
    assert mt5.calls == [("US100", "1h", 4)]
    assert capital.calls == [("US100", "1h", 4)]
