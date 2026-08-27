import logging
from datetime import datetime, timezone

from app.data.models import Candle
from app.data.manager import DataProviderManager
from app.scanner.scanner import ScanTarget, Scanner


def scanner_config(*, symbol_mode: str = "manual", volume_enabled: bool = False) -> dict:
    return {
        "scanner": {
            "enabled": True,
            "symbol_mode": symbol_mode,
            "symbols": ["XAU_USD"],
            "scan_interval_seconds": 30,
            "volume_filter": {
                "enabled": volume_enabled,
                "minimum_24h_volume_usd": 5_000_000,
            },
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
        "data": {
            "history": {"1h": 4, "4h": 3, "1d": 3},
        },
    }


class FakeProvider:
    def __init__(self):
        self.calls = []

    def get_instruments(self):
        return ["EUR_USD", "GBP_USD"]

    def get_24h_volume_usd(self, symbol):
        del symbol
        return None

    def get_candles(self, symbol, timeframe, count):
        self.calls.append((symbol, timeframe, count))
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=datetime(2026, 8, 26, tzinfo=timezone.utc),
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

    def recently_sent(self, symbol, provider, cooldown_minutes):
        del symbol, provider, cooldown_minutes
        return False


def make_scanner(config, provider):
    return Scanner(config, provider, NoopNotifier(), NoopStore(), logging.getLogger("test"))


def test_all_mode_loads_instruments_from_provider():
    provider = FakeProvider()
    scanner = make_scanner(scanner_config(symbol_mode="all"), provider)
    assert scanner.resolve_symbols() == ["EUR_USD", "GBP_USD"]


def test_enabled_volume_filter_skips_missing_usd_volume():
    provider = FakeProvider()
    scanner = make_scanner(scanner_config(volume_enabled=True), provider)
    assert scanner.resolve_symbols() == []


def test_startup_history_uses_configured_windows_then_recent_refreshes():
    provider = FakeProvider()
    scanner = make_scanner(scanner_config(), provider)
    scanner.load_initial_history(["XAU_USD"])
    assert provider.calls == [
        ("XAU_USD", "1h", 4),
        ("XAU_USD", "4h", 3),
        ("XAU_USD", "1d", 3),
    ]

    scanner._get_history("XAU_USD", "1h", 4, refresh_count=3)
    assert provider.calls[-1] == ("XAU_USD", "1h", 3)


def test_oanda_scan_continues_when_mt5_provider_is_unavailable():
    oanda = FakeProvider()
    providers = DataProviderManager({"oanda": oanda})
    providers.mark_unavailable("mt5", "terminal offline")
    scanner = make_scanner(scanner_config(), providers)

    scanner.scan_once(
        [
            ScanTarget("XAU_USD", "oanda"),
            ScanTarget("US100.pro", "mt5"),
        ]
    )

    assert oanda.calls == [
        ("XAU_USD", "1h", 4),
    ]


def test_oanda_scan_continues_when_capital_provider_is_unavailable():
    oanda = FakeProvider()
    providers = DataProviderManager({"oanda": oanda})
    providers.mark_unavailable("capital", "auth failed")
    scanner = make_scanner(scanner_config(), providers)

    scanner.scan_once(
        [
            ScanTarget("XAU_USD", "oanda"),
            ScanTarget("US100", "capital"),
        ]
    )

    assert oanda.calls == [
        ("XAU_USD", "1h", 4),
    ]
