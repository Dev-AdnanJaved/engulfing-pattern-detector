import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.data.manager import DataProviderManager, ProviderUnavailable
from app.data.mt5 import Mt5Client, Mt5Error, _load_mt5_module
from app.data.models import Candle
from app.scanner.scanner import ScanTarget, Scanner
from app.strategy.signal import evaluate_signal


class FakeMt5:
    TIMEFRAME_H1 = 101
    TIMEFRAME_H4 = 104
    TIMEFRAME_D1 = 124

    def __init__(self):
        self.initialized_with = None
        self.selected = []
        self.rate_calls = []
        self.shutdown_calls = 0
        self.symbols = [
            SimpleNamespace(name="US100.pro"),
            SimpleNamespace(name="EURUSD"),
            SimpleNamespace(name="NAS100"),
        ]
        self.rates = [
            {
                "time": 1_724_678_400,
                "open": 100,
                "high": 106,
                "low": 99,
                "close": 105,
                "tick_volume": 20,
                "real_volume": 0,
            },
            {
                "time": 1_724_682_000,
                "open": 106,
                "high": 108,
                "low": 103,
                "close": 104,
                "tick_volume": 25,
                "real_volume": 0,
            },
        ]

    def initialize(self, **kwargs):
        self.initialized_with = kwargs
        return True

    def account_info(self):
        return SimpleNamespace(login=12345, server="Skilling-Demo")

    def symbols_get(self):
        return self.symbols

    def symbol_select(self, symbol, visible):
        self.selected.append((symbol, visible))
        return symbol in {item.name for item in self.symbols}

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.rate_calls.append((symbol, timeframe, start, count))
        return self.rates[:count]

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=99.0, ask=101.0, last=100.0)

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        self.shutdown_calls += 1


class NoopNotifier:
    def send_signal(self, signal):
        del signal
        return False


class NoopStore:
    def reserve_signal(self, signal):
        del signal
        return True

    def mark_telegram_sent(self, signal_id):
        del signal_id


def connected_client(fake=None):
    fake = fake or FakeMt5()
    client = Mt5Client("12345", "not-used-in-logs", "Skilling-Demo", mt5_module=fake)
    client.connect()
    return client, fake


def test_mt5_connects_with_credentials_without_logging_password(caplog):
    client, fake = connected_client()

    assert fake.initialized_with == {
        "login": 12345,
        "password": "not-used-in-logs",
        "server": "Skilling-Demo",
    }
    assert "not-used-in-logs" not in caplog.text
    client.close()
    assert fake.shutdown_calls == 1


def test_symbol_discovery_and_search_do_not_assume_us100_name():
    client, _ = connected_client()

    assert client.get_instruments() == ["EURUSD", "NAS100", "US100.pro"]
    assert client.search_symbols("us100") == ["US100.pro"]


def test_all_mode_requires_include_patterns_and_applies_exclusions():
    client, _ = connected_client()
    config = {
        "scanner": {
            "enabled": True,
            "symbol_mode": "manual",
            "symbols": ["XAU_USD"],
            "volume_filter": {"enabled": False, "minimum_24h_volume_usd": 0},
        },
        "mt5": {
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
    providers = DataProviderManager({"oanda": object(), "mt5": client})
    scanner = Scanner(config, providers, NoopNotifier(), NoopStore())

    assert scanner.resolve_targets() == [
        ScanTarget("XAU_USD", "oanda"),
        ScanTarget("US100.pro", "mt5"),
    ]


def test_unknown_symbol_is_rejected_before_candle_request():
    client, fake = connected_client()

    with pytest.raises(Mt5Error, match="symbol not found"):
        client.get_candles("US100", "1h", 3)
    assert fake.rate_calls == []


def test_candles_use_mt5_timeframe_and_normalized_model():
    client, fake = connected_client()

    candles = client.get_candles("US100.pro", "4h", 2)

    assert fake.rate_calls == [("US100.pro", fake.TIMEFRAME_H4, 0, 2)]
    assert all(isinstance(candle, Candle) for candle in candles)
    assert candles[0].timeframe == "4h"
    assert candles[0].timestamp.tzinfo == timezone.utc
    assert candles[0].volume == 20
    assert candles[0].is_closed is True
    assert candles[1].is_closed is False


def test_current_price_uses_mt5_bid_ask():
    client, fake = connected_client()

    assert client.get_current_price("US100.pro") == 100.0
    assert fake.selected[-1] == ("US100.pro", True)


def test_mt5_initialization_failure_is_explicit():
    fake = FakeMt5()
    fake.initialize = lambda **kwargs: False
    client = Mt5Client("12345", "password", "server", mt5_module=fake)

    with pytest.raises(Mt5Error, match="initialization failed"):
        client.connect()


def test_mt5_account_verification_failure_is_explicit_and_shuts_down():
    fake = FakeMt5()
    fake.account_info = lambda: (_ for _ in ()).throw(OSError("terminal unavailable"))
    client = Mt5Client("12345", "password", "server", mt5_module=fake)

    with pytest.raises(Mt5Error, match="account verification failed"):
        client.connect()
    assert fake.shutdown_calls == 1


def test_mt5_import_failures_are_reported_as_provider_errors(monkeypatch):
    def fail_import(_name):
        raise OSError("MT5 DLL unavailable")

    monkeypatch.setattr("app.data.mt5.importlib.import_module", fail_import)

    with pytest.raises(Mt5Error, match="MetaTrader5 is unavailable"):
        _load_mt5_module()


def test_mt5_provider_failure_does_not_remove_oanda_provider():
    oanda = object()
    providers = DataProviderManager({"oanda": oanda})
    providers.mark_unavailable("mt5", "terminal offline")

    assert providers.get_provider("oanda") is oanda
    with pytest.raises(ProviderUnavailable, match="MT5 provider unavailable"):
        providers.get_provider("mt5")


def test_provider_is_part_of_mt5_signal_identity():
    def candle(open_price, close, offset, timeframe="1h"):
        return Candle(
            symbol="US100.pro",
            timeframe=timeframe,
            timestamp=datetime(2026, 8, 26, 20 + offset, tzinfo=timezone.utc),
            open=open_price,
            high=max(open_price, close),
            low=min(open_price, close),
            close=close,
            is_closed=True,
        )

    signal = evaluate_signal(
        "US100.pro",
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
        provider="mt5",
    )

    assert signal is not None
    assert signal.provider == "mt5"
    assert signal.id.startswith("mt5|US100.pro|")