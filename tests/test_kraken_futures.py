import io
import json
import logging
from datetime import datetime, timezone
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from app.data.kraken_futures import KrakenFuturesClient, KrakenFuturesError
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
            url="https://futures.kraken.com/derivatives/api/v3/test",
            code=code,
            msg="error",
            hdrs=header_map,
            fp=io.BytesIO(body),
        )


def connected_client(monkeypatch, handler, **kwargs):
    monkeypatch.setattr("app.data.kraken_futures.urlopen", handler)
    client = KrakenFuturesClient(
        retries=kwargs.pop("retries", 1),
        logger=logging.getLogger("test.kraken"),
        **kwargs,
    )
    client.connect()
    return client


def test_connect_loads_tradable_symbols(monkeypatch, caplog):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/instruments"):
            return FakeResponse(
                {
                    "result": "success",
                    "instruments": [
                        {"symbol": "PF_XBTUSD", "tradeable": True},
                        {"symbol": "PF_ETHUSD", "tradeable": True},
                        {"symbol": "PI_XBTUSD", "tradeable": False},
                    ],
                }
            )
        raise AssertionError(request.full_url)

    with caplog.at_level(logging.INFO):
        client = connected_client(monkeypatch, urlopen)
    assert client.get_instruments() == ["PF_ETHUSD", "PF_XBTUSD"]
    assert "Kraken Futures connected" in caplog.text


def test_search_symbols_filters_by_query(monkeypatch, caplog):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/instruments"):
            return FakeResponse(
                {
                    "result": "success",
                    "instruments": [
                        {"symbol": "PF_XBTUSD", "tradeable": True},
                        {"symbol": "PF_ETHUSD", "tradeable": True},
                    ],
                }
            )
        raise AssertionError(request.full_url)

    with caplog.at_level(logging.INFO):
        client = connected_client(monkeypatch, urlopen)
        matches = client.search_symbols("XBT")
    assert matches == ["PF_XBTUSD"]
    assert "Kraken Futures symbols matching 'XBT': 1" in caplog.text


def test_candles_map_timeframes_and_closed_status(monkeypatch):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    hour_ms = 3_600_000
    current_hour_open = now_ms - (now_ms % hour_ms)

    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/instruments"):
            return FakeResponse(
                {
                    "result": "success",
                    "instruments": [{"symbol": "PF_XBTUSD", "tradeable": True}],
                }
            )
        if "/api/charts/v1/trade/PF_XBTUSD/1h" in request.full_url:
            return FakeResponse(
                {
                    "candles": [
                        {
                            "time": current_hour_open - hour_ms,
                            "open": "100",
                            "high": "101",
                            "low": "99",
                            "close": "100.5",
                            "volume": "10",
                        },
                        {
                            "time": current_hour_open,
                            "open": "100.5",
                            "high": "102",
                            "low": "100",
                            "close": "101.5",
                            "volume": "12",
                        },
                    ]
                }
            )
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    candles = client.get_candles("pf_xbtusd", "1h", 2)
    assert candles[0].symbol == "PF_XBTUSD"
    assert candles[0].is_closed is True
    assert candles[1].is_closed is False


def test_current_price_and_24h_volume(monkeypatch):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/instruments"):
            return FakeResponse(
                {
                    "result": "success",
                    "instruments": [{"symbol": "PF_XBTUSD", "tradeable": True}],
                }
            )
        if request.full_url.endswith("/tickers"):
            return FakeResponse(
                {
                    "result": "success",
                    "tickers": [
                        {
                            "symbol": "PF_XBTUSD",
                            "last": 65000.5,
                            "volumeQuote": 1234567890.0,
                        }
                    ],
                }
            )
        raise AssertionError(request.full_url)

    client = connected_client(monkeypatch, urlopen)
    assert client.get_current_price("PF_XBTUSD") == 65000.5
    assert client.get_24h_volume_usd("PF_XBTUSD") == 1234567890.0


def test_scanner_loads_manual_kraken_targets(monkeypatch):
    def urlopen(request, timeout=0):
        del timeout
        if request.full_url.endswith("/instruments"):
            return FakeResponse(
                {
                    "result": "success",
                    "instruments": [
                        {"symbol": "PF_XBTUSD", "tradeable": True},
                        {"symbol": "PF_ETHUSD", "tradeable": True},
                    ],
                }
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr("app.data.kraken_futures.urlopen", urlopen)
    client = KrakenFuturesClient(logger=logging.getLogger("test.kraken"))
    client.connect()
    providers = DataProviderManager()
    providers.add_provider("kraken_futures", client)
    config = {
        "scanner": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["XAU_USD"],
            "scan_interval_seconds": 900,
            "volume_filter": {"enabled": False, "minimum_24h_volume_usd": 0},
        },
        "kraken_futures": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["PF_XBTUSD", "PF_ETHUSD"],
            "all_mode_filter": {"include_patterns": ["PF_*"], "exclude_patterns": []},
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
    assert ScanTarget("PF_XBTUSD", "kraken_futures") in targets
    assert ScanTarget("PF_ETHUSD", "kraken_futures") in targets


def test_unavailable_kraken_targets_are_skipped():
    providers = DataProviderManager()
    providers.mark_unavailable("kraken_futures", "connection failed")
    config = {
        "scanner": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": [],
            "scan_interval_seconds": 900,
            "volume_filter": {"enabled": False, "minimum_24h_volume_usd": 0},
        },
        "kraken_futures": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["PF_XBTUSD"],
            "all_mode_filter": {"include_patterns": ["PF_*"], "exclude_patterns": []},
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
    assert scanner.resolve_targets() == []


def test_api_error_is_explicit(monkeypatch):
    def urlopen(request, timeout=0):
        del request, timeout
        raise FakeHTTPError(503, b'{"result":"error","error":"down"}')

    client = KrakenFuturesClient(retries=0, logger=logging.getLogger("test.kraken"))
    monkeypatch.setattr("app.data.kraken_futures.urlopen", urlopen)
    with pytest.raises(KrakenFuturesError):
        client.get_instruments()
