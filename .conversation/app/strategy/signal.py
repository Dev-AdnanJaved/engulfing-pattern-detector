from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from app.data.models import Candle
from app.strategy.engulfing import (
    is_bearish,
    is_bearish_engulfing,
    is_bullish,
    is_bullish_engulfing,
)


@dataclass(frozen=True)
class Signal:
    id: str
    symbol: str
    direction: str
    pattern: str
    timeframe: str
    candle_time: datetime
    pattern_candle_close: float
    current_price: float
    h4_direction: str
    d1_direction: str
    created_at: datetime
    provider: str = "oanda"


def evaluate_signal(
    symbol: str,
    one_hour_candles: Sequence[Candle],
    four_hour_candle: Optional[Candle],
    one_day_candle: Optional[Candle],
    current_price: float,
    *,
    signal_timeframe: str,
    confirmation_4h: str,
    confirmation_1d: str,
    bullish_enabled: bool,
    bearish_enabled: bool,
    reject_doji: bool,
    provider: str = "oanda",
) -> Optional[Signal]:
    closed_1h = [candle for candle in one_hour_candles if candle.is_closed]
    if len(closed_1h) < 2 or four_hour_candle is None or one_day_candle is None:
        return None
    if not four_hour_candle.is_closed or not one_day_candle.is_closed:
        return None

    previous, current = closed_1h[-2:]
    h4_direction = _direction(four_hour_candle, reject_doji)
    d1_direction = _direction(one_day_candle, reject_doji)
    created_at = datetime.now(timezone.utc)

    if bullish_enabled and is_bullish_engulfing(previous, current, reject_doji):
        if h4_direction == "bullish" and d1_direction == "bullish":
            return _build_signal(
                symbol,
                "BUY",
                "bullish_engulfing",
                signal_timeframe,
                confirmation_4h,
                confirmation_1d,
                current,
                current_price,
                h4_direction,
                d1_direction,
                created_at,
                provider,
            )

    if bearish_enabled and is_bearish_engulfing(previous, current, reject_doji):
        if h4_direction == "bearish" and d1_direction == "bearish":
            return _build_signal(
                symbol,
                "SELL",
                "bearish_engulfing",
                signal_timeframe,
                confirmation_4h,
                confirmation_1d,
                current,
                current_price,
                h4_direction,
                d1_direction,
                created_at,
                provider,
            )
    return None


def _direction(candle: Candle, reject_doji: bool) -> str:
    if is_bullish(candle, reject_doji):
        return "bullish"
    if is_bearish(candle, reject_doji):
        return "bearish"
    return "doji"


def _build_signal(
    symbol: str,
    direction: str,
    pattern: str,
    timeframe: str,
    confirmation_4h: str,
    confirmation_1d: str,
    candle: Candle,
    current_price: float,
    h4_direction: str,
    d1_direction: str,
    created_at: datetime,
    provider: str,
) -> Signal:
    signal_id = make_signal_id(symbol, timeframe, candle.timestamp, direction, provider)
    return Signal(
        id=signal_id,
        symbol=symbol,
        direction=direction,
        pattern=pattern,
        timeframe=timeframe,
        candle_time=candle.timestamp,
        pattern_candle_close=candle.close,
        current_price=current_price,
        h4_direction=h4_direction,
        d1_direction=d1_direction,
        created_at=created_at,
        provider=provider,
    )


def make_signal_id(
    symbol: str,
    timeframe: str,
    candle_time: datetime,
    direction: str,
    provider: str = "oanda",
) -> str:
    timestamp = candle_time.astimezone(timezone.utc).isoformat()
    base_id = f"{symbol}|{timeframe}|{timestamp}|{direction}"
    # Preserve existing OANDA IDs while separating same-named MT5 symbols.
    return base_id if provider == "oanda" else f"{provider}|{base_id}"