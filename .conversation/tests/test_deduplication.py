from datetime import datetime, timezone

from app.data.models import Candle
from app.storage.database import SignalDatabase
from app.strategy.signal import Signal


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