from datetime import datetime, timedelta, timezone

from app.data.models import Candle
from app.strategy.signal import evaluate_signal


BASE = datetime(2026, 8, 26, 20, tzinfo=timezone.utc)


def c(open_price: float, close: float, offset: int, timeframe: str = "1h", closed: bool = True) -> Candle:
    return Candle(
        symbol="XAU_USD",
        timeframe=timeframe,
        timestamp=BASE + timedelta(hours=offset),
        open=open_price,
        high=max(open_price, close),
        low=min(open_price, close),
        close=close,
        is_closed=closed,
    )


def evaluate(one_hour, four_hour, one_day, **kwargs):
    options = {
        "signal_timeframe": "1h",
        "confirmation_4h": "4h",
        "confirmation_1d": "1d",
        "bullish_enabled": True,
        "bearish_enabled": True,
        "reject_doji": True,
    }
    options.update(kwargs)
    return evaluate_signal(
        "XAU_USD",
        one_hour,
        four_hour,
        one_day,
        4639.18,
        **options,
    )


def test_buy_requires_bullish_engulfing_and_both_confirmations():
    signal = evaluate([c(105, 100, 0), c(99, 106, 1)], c(4600, 4650, 2, "4h"), c(4500, 4700, 3, "1d"))
    assert signal is not None
    assert signal.direction == "BUY"
    assert signal.pattern == "bullish_engulfing"
    assert signal.candle_closed is True


def test_buy_missing_confirmation_is_no_signal():
    assert evaluate([c(105, 100, 0), c(99, 106, 1)], c(4600, 4550, 2, "4h"), c(4500, 4700, 3, "1d")) is None
    assert evaluate([c(105, 100, 0), c(99, 106, 1)], c(4600, 4650, 2, "4h"), c(4700, 4500, 3, "1d")) is None


def test_sell_requires_bearish_engulfing_and_both_confirmations():
    signal = evaluate([c(100, 105, 0), c(106, 99, 1)], c(4600, 4550, 2, "4h"), c(4700, 4500, 3, "1d"))
    assert signal is not None
    assert signal.direction == "SELL"
    assert signal.pattern == "bearish_engulfing"


def test_sell_missing_confirmation_is_no_signal():
    assert evaluate([c(100, 105, 0), c(106, 99, 1)], c(4600, 4650, 2, "4h"), c(4700, 4500, 3, "1d")) is None


def test_open_confirmation_candle_is_rejected():
    assert evaluate([c(105, 100, 0), c(99, 106, 1)], c(4600, 4650, 2, "4h", closed=False), c(4500, 4700, 3, "1d")) is None


def test_forming_1h_candle_is_ignored_without_early_detection():
    assert evaluate([c(105, 100, 0), c(99, 106, 1, closed=False)], c(4600, 4650, 2, "4h"), c(4500, 4700, 3, "1d")) is None


def test_early_forming_1h_signal_within_window():
    forming_open = BASE + timedelta(hours=1)
    now = forming_open + timedelta(minutes=56)
    signal = evaluate(
        [c(105, 100, 0), c(99, 106, 1, closed=False)],
        c(4600, 4650, 2, "4h"),
        c(4500, 4700, 3, "1d"),
        early_minutes_before_close=5,
        now=now,
    )
    assert signal is not None
    assert signal.direction == "BUY"
    assert signal.candle_closed is False
    assert signal.minutes_to_close == 4


def test_early_forming_1h_ignored_outside_window():
    forming_open = BASE + timedelta(hours=1)
    now = forming_open + timedelta(minutes=30)
    assert (
        evaluate(
            [c(105, 100, 0), c(99, 106, 1, closed=False)],
            c(4600, 4650, 2, "4h"),
            c(4500, 4700, 3, "1d"),
            early_minutes_before_close=5,
            now=now,
        )
        is None
    )
