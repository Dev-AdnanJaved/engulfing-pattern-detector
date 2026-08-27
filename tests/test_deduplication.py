from datetime import datetime, timedelta, timezone

from app.storage.database import SignalDatabase
from app.strategy.signal import Signal, make_signal_id


def _make_signal(
    *,
    symbol: str = "XAU_USD",
    provider: str = "oanda",
    candle_time: datetime | None = None,
    created_at: datetime | None = None,
    direction: str = "BUY",
) -> Signal:
    candle_time = candle_time or datetime(2026, 8, 26, 20, tzinfo=timezone.utc)
    created_at = created_at or datetime.now(timezone.utc)
    return Signal(
        id=make_signal_id(symbol, "1h", candle_time, direction, provider),
        symbol=symbol,
        direction=direction,
        pattern="bullish_engulfing",
        timeframe="1h",
        candle_time=candle_time,
        pattern_candle_close=4639.18,
        current_price=4640.10,
        h4_direction="bullish",
        d1_direction="bullish",
        created_at=created_at,
        provider=provider,
    )


def test_signal_reservation_survives_reopen(tmp_path):
    signal = Signal(
        id="XAU_USD|1h|2026-08-26T20:00:00+00:00|BUY",
        symbol="XAU_USD",
        direction="BUY",
        pattern="bullish_engulfing",
        timeframe="1h",
        candle_time=datetime(2026, 8, 26, 20, tzinfo=timezone.utc),
        pattern_candle_close=4639.18,
        current_price=4640.10,
        h4_direction="bullish",
        d1_direction="bullish",
        created_at=datetime.now(timezone.utc),
    )
    path = tmp_path / "scanner.db"
    first = SignalDatabase(path)
    assert first.reserve_signal(signal) is True
    first.close()

    second = SignalDatabase(path)
    assert second.reserve_signal(signal) is False
    assert second.get_signal(signal.id)["telegram_sent"] == 0
    second.mark_telegram_sent(signal.id)
    assert second.get_signal(signal.id)["telegram_sent"] == 1
    second.close()


def test_provider_aware_ids_keep_capital_and_mt5_signals_separate(tmp_path):
    candle_time = datetime(2026, 8, 27, 10, tzinfo=timezone.utc)
    capital = Signal(
        id=make_signal_id("US100", "1h", candle_time, "BUY", "capital"),
        symbol="US100",
        direction="BUY",
        pattern="bullish_engulfing",
        timeframe="1h",
        candle_time=candle_time,
        pattern_candle_close=19000.0,
        current_price=19010.0,
        h4_direction="bullish",
        d1_direction="bullish",
        created_at=datetime.now(timezone.utc),
        provider="capital",
    )
    mt5 = Signal(
        id=make_signal_id("US100", "1h", candle_time, "BUY", "mt5"),
        symbol="US100",
        direction="BUY",
        pattern="bullish_engulfing",
        timeframe="1h",
        candle_time=candle_time,
        pattern_candle_close=19000.0,
        current_price=19005.0,
        h4_direction="bullish",
        d1_direction="bullish",
        created_at=datetime.now(timezone.utc),
        provider="mt5",
    )
    database = SignalDatabase(tmp_path / "scanner.db")
    assert capital.id != mt5.id
    assert database.reserve_signal(capital) is True
    assert database.reserve_signal(mt5) is True
    assert database.get_signal(capital.id)["provider"] == "capital"
    assert database.get_signal(mt5.id)["provider"] == "mt5"
    database.close()


def test_recently_sent_blocks_same_pair_within_cooldown(tmp_path):
    database = SignalDatabase(tmp_path / "scanner.db")
    signal = _make_signal()
    assert database.reserve_signal(signal) is True
    assert database.recently_sent("XAU_USD", "oanda", 60) is False
    database.mark_telegram_sent(signal.id)
    assert database.recently_sent("XAU_USD", "oanda", 60) is True
    assert database.recently_sent("EUR_USD", "oanda", 60) is False
    assert database.recently_sent("XAU_USD", "capital", 60) is False
    database.close()


def test_recently_sent_allows_pair_after_cooldown_expires(tmp_path):
    database = SignalDatabase(tmp_path / "scanner.db")
    old_time = datetime.now(timezone.utc) - timedelta(minutes=61)
    signal = _make_signal(created_at=old_time)
    assert database.reserve_signal(signal) is True
    database.mark_telegram_sent(signal.id)
    assert database.recently_sent("XAU_USD", "oanda", 60) is False
    database.close()