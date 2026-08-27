"""Investigate US100 engulfing signal at 2026-08-27 16:00 UTC."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data.models import Candle
from app.main import load_local_env
from app.strategy.engulfing import is_bullish_engulfing


def candle(
    symbol: str,
    ts: str,
    o: float,
    c: float,
    *,
    h: float | None = None,
    l: float | None = None,
    closed: bool = True,
) -> Candle:
    timestamp = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    return Candle(
        symbol=symbol,
        timeframe="1h",
        timestamp=timestamp,
        open=o,
        high=h if h is not None else max(o, c) + 5,
        low=l if l is not None else min(o, c) - 5,
        close=c,
        is_closed=closed,
    )


def check_pair(label: str, previous: Candle, current: Candle) -> None:
    result = is_bullish_engulfing(previous, current, reject_doji=True)
    print(f"\n=== {label} ===")
    print(
        f"Previous: O={previous.open} C={previous.close} "
        f"({'bearish' if previous.close < previous.open else 'not bearish'})"
    )
    print(
        f"Current:  O={current.open} C={current.close} "
        f"({'bullish' if current.close > current.open else 'not bullish'})"
    )
    print(f"  current.open <= previous.close ? {current.open <= previous.close}")
    print(f"  current.close >= previous.open ? {current.close >= previous.open}")
    print(f"  is_bullish_engulfing -> {result}")


def fetch_capital_candles() -> list[Candle] | None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return None
    load_local_env(env_path)
    api_key = os.environ.get("CAPITAL_API_KEY")
    identifier = os.environ.get("CAPITAL_IDENTIFIER")
    password = os.environ.get("CAPITAL_PASSWORD")
    if not all([api_key, identifier, password]):
        return None

    import logging

    from app.data.capital import CapitalClient

    client = CapitalClient(
        identifier,
        api_key,
        password,
        symbol_epics={"US100": {"epic": "US100"}},
        logger=logging.getLogger("investigate"),
    )
    client.connect()
    candles = client.get_candles("US100", "1h", 10)
    client.close()
    return candles


def main() -> None:
    print("US100 engulfing investigation")
    print("Signal reference: BUY @ ~29597.5, candle timestamp 16:00 UTC\n")

    # Scenario A: chart-like gap (should FAIL)
    check_pair(
        "Chart-like gap (green opens above red close)",
        candle("US100", "2026-08-27T15:00:00+00:00", 29592.0, 29590.0),
        candle("US100", "2026-08-27T16:00:00+00:00", 29591.0, 29597.5),
    )

    # Scenario B: touching bodies (passes loose rules, fails strict)
    check_pair(
        "Touching bodies (open equals red close, tiny 2pt red body)",
        candle("US100", "2026-08-27T15:00:00+00:00", 29592.0, 29590.0),
        candle("US100", "2026-08-27T16:00:00+00:00", 29590.0, 29597.5),
    )
    print(
        "  strict=True ->",
        is_bullish_engulfing(
            candle("US100", "2026-08-27T15:00:00+00:00", 29592.0, 29590.0),
            candle("US100", "2026-08-27T16:00:00+00:00", 29590.0, 29597.5),
            strict=True,
        ),
    )

    # Scenario C: mid-price shifts open down by spread (PASSES API, chart shows gap)
    check_pair(
        "API mid-price (display open 29591 but mid open 29590)",
        candle("US100", "2026-08-27T15:00:00+00:00", 29592.0, 29590.0),
        candle("US100", "2026-08-27T16:00:00+00:00", 29590.0, 29597.5),
    )

    candles = fetch_capital_candles()
    if candles is None:
        print("\nNo .env / Capital credentials — skipped live API fetch.")
        return

    print("\n=== Live Capital.com candles (last 10) ===")
    for item in candles:
        state = "closed" if item.is_closed else "OPEN"
        print(
            f"{item.timestamp.isoformat()} {state} "
            f"O={item.open} H={item.high} L={item.low} C={item.close}"
        )

    closed = [c for c in candles if c.is_closed]
    if len(closed) >= 2:
        target = None
        for index, item in enumerate(closed):
            if item.timestamp.hour == 16 and item.timestamp.date().day == 27:
                if index >= 1:
                    target = (closed[index - 1], item)
                    break
        pair = target or (closed[-2], closed[-1])
        check_pair("Live API last matching 16:00 pair (or last closed pair)", pair[0], pair[1])


if __name__ == "__main__":
    main()
