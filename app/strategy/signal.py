from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    candle_closed: bool = True
    minutes_to_close: int | None = None


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
    early_minutes_before_close: int = 0,
    now: datetime | None = None,
    include_closed: bool = True,
    strict_engulfing: bool = True,
) -> Optional[Signal]:
    if four_hour_candle is None or one_day_candle is None:
        return None

    h4_direction = _direction(four_hour_candle, reject_doji)
    d1_direction = _direction(one_day_candle, reject_doji)
    created_at = datetime.now(timezone.utc)

    closed_1h = [candle for candle in one_hour_candles if candle.is_closed]
    if include_closed and len(closed_1h) >= 2:
        previous, current = closed_1h[-2:]
        closed = _match_engulfing_signal(
            symbol,
            previous,
            current,
            current_price,
            h4_direction=h4_direction,
            d1_direction=d1_direction,
            created_at=created_at,
            signal_timeframe=signal_timeframe,
            bullish_enabled=bullish_enabled,
            bearish_enabled=bearish_enabled,
            reject_doji=reject_doji,
            provider=provider,
            require_closed=True,
            candle_closed=True,
            minutes_to_close=None,
            strict_engulfing=strict_engulfing,
        )
        if closed is not None:
            return closed

    if early_minutes_before_close <= 0 or not one_hour_candles or not closed_1h:
        return None

    forming = one_hour_candles[-1]
    if forming.is_closed:
        return None

    previous = closed_1h[-1]
    minutes_left = minutes_until_close(forming, signal_timeframe, now=now)
    if minutes_left is None or minutes_left <= 0 or minutes_left > early_minutes_before_close:
        return None

    return _match_engulfing_signal(
        symbol,
        previous,
        forming,
        current_price,
        h4_direction=h4_direction,
        d1_direction=d1_direction,
        created_at=created_at,
        signal_timeframe=signal_timeframe,
        bullish_enabled=bullish_enabled,
        bearish_enabled=bearish_enabled,
        reject_doji=reject_doji,
        provider=provider,
        require_closed=False,
        candle_closed=False,
        minutes_to_close=max(1, int(math.ceil(minutes_left))),
        strict_engulfing=strict_engulfing,
    )


def minutes_until_close(
    candle: Candle,
    timeframe: str,
    *,
    now: datetime | None = None,
) -> float | None:
    duration = timeframe_duration(timeframe)
    if duration is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    remaining = (candle.timestamp + duration) - current
    return remaining.total_seconds() / 60.0


def timeframe_duration(timeframe: str) -> timedelta | None:
    normalized = timeframe.strip().lower()
    match = re.fullmatch(r"(?:(\d+)([mhd])|h1|d1)", normalized)
    if match is None:
        aliases = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
        return aliases.get(normalized)
    if match.group(1) is None:
        return timedelta(hours=1) if normalized == "h1" else timedelta(days=1)
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _match_engulfing_signal(
    symbol: str,
    previous: Candle,
    current: Candle,
    current_price: float,
    *,
    h4_direction: str,
    d1_direction: str,
    created_at: datetime,
    signal_timeframe: str,
    bullish_enabled: bool,
    bearish_enabled: bool,
    reject_doji: bool,
    provider: str,
    require_closed: bool,
    candle_closed: bool,
    minutes_to_close: int | None,
    strict_engulfing: bool = True,
) -> Optional[Signal]:
    if bullish_enabled and is_bullish_engulfing(
        previous,
        current,
        reject_doji,
        require_closed=require_closed,
        strict=strict_engulfing,
    ):
        if h4_direction == "bullish" and d1_direction == "bullish":
            return _build_signal(
                symbol,
                "BUY",
                "bullish_engulfing",
                signal_timeframe,
                current,
                current_price,
                h4_direction,
                d1_direction,
                created_at,
                provider,
                candle_closed=candle_closed,
                minutes_to_close=minutes_to_close,
            )

    if bearish_enabled and is_bearish_engulfing(
        previous,
        current,
        reject_doji,
        require_closed=require_closed,
        strict=strict_engulfing,
    ):
        if h4_direction == "bearish" and d1_direction == "bearish":
            return _build_signal(
                symbol,
                "SELL",
                "bearish_engulfing",
                signal_timeframe,
                current,
                current_price,
                h4_direction,
                d1_direction,
                created_at,
                provider,
                candle_closed=candle_closed,
                minutes_to_close=minutes_to_close,
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
    candle: Candle,
    current_price: float,
    h4_direction: str,
    d1_direction: str,
    created_at: datetime,
    provider: str,
    *,
    candle_closed: bool = True,
    minutes_to_close: int | None = None,
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
        candle_closed=candle_closed,
        minutes_to_close=minutes_to_close,
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
    return f"{provider}|{base_id}"
