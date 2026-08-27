from __future__ import annotations

from app.data.models import Candle


def is_bullish(candle: Candle, reject_doji: bool = True) -> bool:
    return candle.close > candle.open or (not reject_doji and candle.close >= candle.open)


def is_bearish(candle: Candle, reject_doji: bool = True) -> bool:
    return candle.close < candle.open or (not reject_doji and candle.close <= candle.open)


def is_bullish_engulfing(
    previous: Candle,
    current: Candle,
    reject_doji: bool = True,
    *,
    require_closed: bool = True,
) -> bool:
    if not previous.is_closed:
        return False
    if require_closed and not current.is_closed:
        return False
    return (
        is_bearish(previous, reject_doji)
        and is_bullish(current, reject_doji)
        and current.open <= previous.close
        and current.close >= previous.open
    )


def is_bearish_engulfing(
    previous: Candle,
    current: Candle,
    reject_doji: bool = True,
    *,
    require_closed: bool = True,
) -> bool:
    if not previous.is_closed:
        return False
    if require_closed and not current.is_closed:
        return False
    return (
        is_bullish(previous, reject_doji)
        and is_bearish(current, reject_doji)
        and current.open >= previous.close
        and current.close <= previous.open
    )
