from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    is_closed: bool = True

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc))
        else:
            object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))