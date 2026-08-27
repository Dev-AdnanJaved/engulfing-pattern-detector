from datetime import datetime, timezone

from app.data.models import Candle
from app.strategy.engulfing import is_bearish_engulfing, is_bullish_engulfing


def candle(open_price: float, close: float, *, high: float | None = None, low: float | None = None, closed: bool = True) -> Candle:
    return Candle(
        symbol="EUR_USD",
        timeframe="1h",
        timestamp=datetime.now(timezone.utc),
        open=open_price,
        high=high if high is not None else max(open_price, close),
        low=low if low is not None else min(open_price, close),
        close=close,
        is_closed=closed,
    )


def test_valid_bullish_engulfing():
    assert is_bullish_engulfing(candle(105, 100), candle(99, 106))


def test_invalid_bullish_engulfing_when_body_does_not_cover():
    assert not is_bullish_engulfing(candle(105, 100), candle(101, 104))


def test_bullish_same_color_fails():
    assert not is_bullish_engulfing(candle(100, 105), candle(104, 108))


def test_bullish_doji_fails():
    assert not is_bullish_engulfing(candle(100, 100), candle(99, 101))
    assert not is_bullish_engulfing(candle(105, 100), candle(102, 102))


def test_bullish_wick_only_engulfing_fails():
    assert not is_bullish_engulfing(candle(105, 100, high=110, low=95), candle(101, 104, high=120, low=90))


def test_valid_bearish_engulfing():
    assert is_bearish_engulfing(candle(100, 105), candle(106, 99))


def test_invalid_bearish_engulfing_when_body_does_not_cover():
    assert not is_bearish_engulfing(candle(100, 105), candle(104, 101))


def test_bearish_same_color_fails():
    assert not is_bearish_engulfing(candle(105, 100), candle(101, 95))


def test_bearish_doji_fails():
    assert not is_bearish_engulfing(candle(100, 100), candle(101, 99))
    assert not is_bearish_engulfing(candle(100, 105), candle(102, 102))


def test_bearish_wick_only_engulfing_fails():
    assert not is_bearish_engulfing(candle(100, 105, high=110, low=95), candle(104, 101, high=120, low=90))


def test_open_candle_is_rejected():
    assert not is_bullish_engulfing(candle(105, 100), candle(99, 106, closed=False))