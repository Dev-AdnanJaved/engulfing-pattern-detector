from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from app.strategy.signal import Signal


class SignalDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'oanda',
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                pattern TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                candle_time TEXT NOT NULL,
                pattern_candle_close REAL NOT NULL,
                current_price REAL NOT NULL,
                h4_direction TEXT NOT NULL,
                d1_direction TEXT NOT NULL,
                telegram_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        self._migrate_provider_column()
        self.connection.commit()

    def reserve_signal(self, signal: Signal) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO signals (
                id, provider, symbol, direction, pattern, timeframe, candle_time,
                pattern_candle_close, current_price, h4_direction,
                d1_direction, telegram_sent, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                signal.id,
                signal.provider,
                signal.symbol,
                signal.direction,
                signal.pattern,
                signal.timeframe,
                signal.candle_time.isoformat(),
                signal.pattern_candle_close,
                signal.current_price,
                signal.h4_direction,
                signal.d1_direction,
                signal.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def _migrate_provider_column(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(signals)").fetchall()
        }
        if "provider" not in columns:
            self.connection.execute(
                "ALTER TABLE signals ADD COLUMN provider TEXT NOT NULL DEFAULT 'oanda'"
            )

    def mark_telegram_sent(self, signal_id: str) -> None:
        self.connection.execute("UPDATE signals SET telegram_sent = 1 WHERE id = ?", (signal_id,))
        self.connection.commit()

    def get_signal(self, signal_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()

    def close(self) -> None:
        self.connection.close()