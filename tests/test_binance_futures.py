import io
import json
import logging
from datetime import datetime, timezone
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from app.data.binance_futures import BinanceFuturesClient, BinanceFuturesError
from app.data.manager import DataProviderManager
from app.scanner.scanner import ScanTarget, Scanner


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False


class FakeHTTPError(HTTPError):
    def __init__(self, code, body=b""):
        header_map = Message()
        super().__init__(
            url="https://fapi.binance.com/fapi/v1/test",
            code=code,
            msg="error",
            hdrs=header_map,
            fp=io.BytesIO(body),
        )


def connected_client(monkeypatch, handler, **kwargs):
    monkeypatch.setattr("app.data.binance_futures.urlopen", handler)
    client = BinanceFuturesClient(
        retries=kwargs.pop("retries", 1),
        logger=logging.getLogger("test.binance"),
        **kwargs,
    )
    client.connect()
    return client


def test_connect_loads_tradable_symbols(monkeypatch, caplog):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/exchangeInfo"):
            return FakeResponse(
                {
                    "symbols": [
                        {"symbol": "BTCUSDT", "status": "TRADING"},
                        {"symbol": "ETHUSDT", "status": "TRADING"},
                        {"symbol": "OLDUSDT", "status": "BREAK"},
                    ]
                }
            )
        raise AssertionError(request.full_url)

    with caplog.at_level(logging.INFO):
        client = connected_client(monkeypatch, urlopen)
    assert client.get_instruments() == ["BTCUSDT", "ETHUSDT"]
    assert "Binance Futures connected" in caplog.text


def test_search_symbols_filters_by_query(monkeypatch, caplog):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/exchangeInfo"):
            return FakeResponse(
                {
                    "symbols": [
                        {"symbol": "BTCUSDT", "status": "TRADING"},
                        {"symbol": "ETHUSDT", "status": "TRADING"},
                    ]
                }
            )
        raise AssertionError(request.full_url)

    with caplog.at_level(logging.INFO):
        client = connected_client(monkeypatch, urlopen)
        matches = client.search_symbols("BTC")
    assert matches == ["BTCUSDT"]
    assert "Binance Futures symbols matching 'BTC': 1" in caplog.text


def test_candles_map_timeframes_and_closed_status(monkeypatch):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/exchangeInfo"):
            return FakeResponse({"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]})
        if "/klines?" in request.full_url:
            assert "interval=1h" in request.full_url
            assert "symbol=BTCUSDT" in request.full_url
            return FakeResponse(
                [
                    [
                        now_ms - 7200000,
                        "100",
                        "101",
                        "99",
                        "100.5",
                        "10",
                        now_ms - 3600001,
                    ],
                    [
                        now_ms - 3600000,
                        "100.5",
                        "102",
                        "100",
                        "101.5",
                        "12",
                        now_ms + 3600000,
                    ],
                ]
            )
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    candles = client.get_candles("btcusdt", "1h", 2)
    assert candles[0].symbol == "BTCUSDT"
    assert candles[0].is_closed is True
    assert candles[1].is_closed is False


def test_current_price_and_24h_volume(monkeypatch):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/exchangeInfo"):
            return FakeResponse({"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]})
        if request.full_url.endswith("/ticker/price?symbol=BTCUSDT"):
            return FakeResponse({"symbol": "BTCUSDT", "price": "65000.5"})
        if request.full_url.endswith("/ticker/24hr?symbol=BTCUSDT"):
            return FakeResponse({"symbol": "BTCUSDT", "quoteVolume": "1234567890"})
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    assert client.get_current_price("BTCUSDT") == 65000.5
    assert client.get_24h_volume_usd("BTCUSDT") == 1234567890.0


def test_scanner_loads_manual_binance_targets(monkeypatch):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/exchangeInfo"):
            return FakeResponse(
                {
                    "symbols": [
                        {"symbol": "BTCUSDT", "status": "TRADING"},
                        {"symbol": "ETHUSDT", "status": "TRADING"},
                    ]
                }
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr("app.data.binance_futures.urlopen", urlopen)
    client = BinanceFuturesClient(logger=logging.getLogger("test.binance"))
    client.connect()
    providers = DataProviderManager()
    providers.add_provider("binance_futures", client)
    config = {
        "scanner": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["XAU_USD"],
            "scan_interval_seconds": 900,
            "volume_filter": {"enabled": False, "minimum_24h_volume_usd": 0},
        },
        "binance_futures": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "all_mode_filter": {"include_patterns": ["*USDT"], "exclude_patterns": []},
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
            "strict_engulfing": True,
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
    targets = scanner.resolve_targets()
    assert ScanTarget("BTCUSDT", "binance_futures") in targets
    assert ScanTarget("ETHUSDT", "binance_futures") in targets


def test_api_error_is_explicit(monkeypatch):
    def urlopen(request, timeout=0):
        del request, timeout
        raise FakeHTTPError(418, b'{"code":-1000,"msg":"bad"}')

    client = BinanceFuturesClient(retries=0, logger=logging.getLogger("test.binance"))
    monkeypatch.setattr("app.data.binance_futures.urlopen", urlopen)
    with pytest.raises(BinanceFuturesError):
        client.get_instruments()
