from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from threading import Event, Lock
from typing import Any, Callable, Protocol, Sequence

from app.data.manager import DataProviderManager, ProviderUnavailable
from app.data.models import Candle
from app.strategy.engulfing import (
    is_bearish_engulfing,
    is_bullish_engulfing,
)
from app.strategy.signal import Signal, evaluate_signal, minutes_until_close


class MarketDataProvider(Protocol):
    def get_instruments(self) -> list[str]: ...
    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]: ...
    def get_current_price(self, symbol: str) -> float: ...
    def get_24h_volume_usd(self, symbol: str) -> float | None: ...


class Notifier(Protocol):
    def send_signal(self, signal: Signal) -> bool: ...


class SignalStore(Protocol):
    def reserve_signal(self, signal: Signal) -> bool: ...
    def mark_telegram_sent(self, signal_id: str) -> None: ...
    def recently_sent(self, symbol: str, provider: str, cooldown_minutes: int) -> bool: ...


@dataclass(frozen=True)
class ScanTarget:
    symbol: str
    provider: str = "oanda"

    def display_name(self) -> str:
        return f"{self.symbol} ({self.provider.upper()})"


class Scanner:
    def __init__(
        self,
        config: dict[str, Any],
        data_provider: MarketDataProvider | DataProviderManager,
        notifier: Notifier,
        store: SignalStore,
        logger: logging.Logger | None = None,
        status_notifier: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.data_provider = data_provider
        self.notifier = notifier
        self.store = store
        self.logger = logger or logging.getLogger(__name__)
        self.status_notifier = status_notifier
        self._last_processed: dict[tuple[str, str], str] = {}
        self._history: dict[tuple[str, str, str], dict[str, Candle]] = {}
        self._scan_lock = Lock()
        self._stop_event = Event()
        self._running = False
        self._started_at: datetime | None = None
        self._last_cycle_at: datetime | None = None
        self._active_targets: list[ScanTarget] = []
        self._scan_number = 0

    def resolve_symbols(self) -> list[str]:
        """Return symbols for compatibility with the original OANDA API."""

        return [target.symbol for target in self.resolve_targets()]

    def resolve_targets(self) -> list[ScanTarget]:
        scanner_config = self.config["scanner"]
        targets: list[ScanTarget] = []

        if scanner_config["symbol_mode"] == "manual":
            targets.extend(
                _target_from_config_entry(entry)
                for entry in scanner_config.get("symbols", [])
            )
        else:
            self._append_discovered(targets, "oanda")

        for provider in ("mt5", "capital"):
            provider_config = self.config.get(provider, {})
            if isinstance(provider_config, dict) and provider_config.get("enabled", False):
                self._append_configured_targets(targets, provider, provider_config)

        unique: list[ScanTarget] = []
        seen: set[tuple[str, str]] = set()
        for target in targets:
            key = (target.provider, target.symbol)
            if key in seen:
                continue
            seen.add(key)
            if self._passes_volume_filter(target):
                unique.append(target)
        return unique

    def scan_once(self, symbols: Sequence[str | ScanTarget] | None = None) -> None:
        if not self.config["scanner"]["enabled"]:
            self.logger.info("Scanner disabled in configuration")
            return
        with self._scan_lock:
            targets = self.resolve_targets() if symbols is None else _normalize_targets(symbols)
            self._active_targets = list(targets)
            self._scan_number += 1
            scan_number = self._scan_number
            self._notify_status(
                f"🔎 *Scan #{scan_number} started*\n"
                f"*Instruments:* {len(targets)}\n"
                f"*Interval:* {self.config['scanner']['scan_interval_seconds']} seconds"
            )
            failures = 0
            for target in targets:
                try:
                    self.scan_symbol(target.symbol, target.provider)
                except Exception:
                    failures += 1
                    self.logger.exception(
                        "Failed scanning %s via %s; continuing with other symbols",
                        target.symbol,
                        target.provider.upper(),
                    )
            self._last_cycle_at = datetime.now(timezone.utc)
            self._notify_status(
                f"✅ *Scan #{scan_number} completed*\n"
                f"*Instruments checked:* {len(targets)}\n"
                f"*Failures:* {failures}"
            )

    def scan_symbol(self, symbol: str, provider: str = "oanda") -> None:
        self.logger.info("Checking %s via %s", symbol, provider.upper())
        data_provider = self._provider(provider)
        history_config = self._history_config(provider)
        strategy_config = self.config["strategy"]
        pattern_config = self.config["pattern"]
        early_minutes = _early_minutes_before_close(pattern_config)
        one_hour = self._get_history(
            symbol,
            strategy_config["signal_timeframe"],
            history_config["1h"],
            refresh_count=3,
            provider=provider,
        )
        closed_1h = [candle for candle in one_hour if candle.is_closed]
        if not closed_1h:
            self.logger.warning("%s has no closed 1H candle", symbol)
            return

        reject_doji = pattern_config["reject_doji"]
        processed_key = (provider, symbol)
        latest_closed = closed_1h[-1]
        candle_key = latest_closed.timestamp.isoformat()
        already_processed_closed = self._last_processed.get(processed_key) == candle_key

        closed_setup = False
        if len(closed_1h) >= 2:
            previous, current = closed_1h[-2:]
            closed_setup = _has_engulfing_pattern(
                previous, current, pattern_config, require_closed=True
            )

        forming = one_hour[-1] if one_hour and not one_hour[-1].is_closed else None
        early_setup = False
        if (
            early_minutes > 0
            and forming is not None
            and _has_engulfing_pattern(
                closed_1h[-1], forming, pattern_config, require_closed=False
            )
        ):
            minutes_left = minutes_until_close(forming, strategy_config["signal_timeframe"])
            if minutes_left is not None and 0 < minutes_left <= early_minutes:
                early_setup = True

        if closed_setup or early_setup:
            self.logger.info("Setup detected for %s via %s", symbol, provider.upper())
        else:
            self.logger.info("Setup not detected for %s via %s", symbol, provider.upper())

        needs_confirmation = (closed_setup and not already_processed_closed) or early_setup
        if not needs_confirmation:
            if not already_processed_closed:
                self._last_processed[processed_key] = candle_key
            return

        four_hour = self._get_history(
            symbol,
            strategy_config["confirmation_4h"],
            history_config["4h"],
            refresh_count=2,
            provider=provider,
        )
        one_day = self._get_history(
            symbol,
            strategy_config["confirmation_1d"],
            history_config["1d"],
            refresh_count=2,
            provider=provider,
        )
        latest_4h = _latest_closed(four_hour)
        latest_1d = _latest_closed(one_day)
        if latest_4h is None or latest_1d is None:
            if not already_processed_closed:
                self._last_processed[processed_key] = candle_key
            return

        if not already_processed_closed:
            # Mark closed candle evaluated even if only early path produces a signal.
            self._last_processed[processed_key] = candle_key

        current_price = data_provider.get_current_price(symbol)
        signal = evaluate_signal(
            symbol,
            one_hour,
            latest_4h,
            latest_1d,
            current_price,
            signal_timeframe=strategy_config["signal_timeframe"],
            confirmation_4h=strategy_config["confirmation_4h"],
            confirmation_1d=strategy_config["confirmation_1d"],
            bullish_enabled=pattern_config["bullish_engulfing"],
            bearish_enabled=pattern_config["bearish_engulfing"],
            reject_doji=reject_doji,
            provider=provider,
            early_minutes_before_close=early_minutes,
            include_closed=not already_processed_closed,
        )
        if signal is None:
            return
        if not signal.candle_closed:
            self.logger.info(
                "Early %s signal for %s via %s (candle closes in ~%s min)",
                signal.direction,
                symbol,
                provider.upper(),
                signal.minutes_to_close,
            )
        elif provider == "capital":
            self.logger.info("Capital.com %s signal detected", signal.direction)
        else:
            self.logger.info(
                "%s signal created for %s via %s",
                signal.direction,
                symbol,
                provider.upper(),
            )
        self._emit_signal(signal, symbol, provider)

    def _emit_signal(self, signal: Signal, symbol: str, provider: str) -> None:
        cooldown_minutes = int(self.config.get("alerts", {}).get("cooldown_minutes", 0) or 0)
        if cooldown_minutes > 0 and self.store.recently_sent(symbol, provider, cooldown_minutes):
            self.logger.info(
                "Cooldown active for %s via %s; skipping alert for %s minutes",
                symbol,
                provider.upper(),
                cooldown_minutes,
            )
            return
        if not self.store.reserve_signal(signal):
            self.logger.info("Duplicate signal skipped: %s", signal.id)
            return
        try:
            sent = self.notifier.send_signal(signal)
            if sent:
                self.store.mark_telegram_sent(signal.id)
                self.logger.info("Telegram alert sent for %s", symbol)
            else:
                self.logger.info("Telegram alerts disabled; signal saved for %s", symbol)
        except Exception:
            self.logger.exception("Telegram alert failed for %s; signal remains saved", symbol)

    def run_forever(self, symbols: Sequence[str | ScanTarget] | None = None) -> None:
        scanner_config = self.config["scanner"]
        if not scanner_config["enabled"]:
            self.logger.info("Scanner disabled in configuration")
            return
        interval = int(scanner_config["scan_interval_seconds"])
        targets = self.prepare() if symbols is None else _normalize_targets(symbols)
        self._active_targets = list(targets)
        self._running = True
        self._started_at = datetime.now(timezone.utc)
        self.logger.info("Scanner started")
        while not self._stop_event.is_set():
            self.scan_once(targets)
            if self._stop_event.is_set():
                break
            self._wait_for_next_scan(interval)
        self._running = False
        self.logger.info("Scanner stopped")

    def _wait_for_next_scan(self, interval_seconds: int) -> None:
        remaining = max(0, int(interval_seconds))
        if remaining <= 0:
            return
        self.logger.info("Will scan again in %s seconds...", remaining)
        self._notify_status(f"⏳ Will scan again in *{remaining}* seconds...")
        self._stop_event.wait(remaining)

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    def prepare(self) -> list[ScanTarget]:
        targets = self.resolve_targets()
        self.logger.info(
            "Loaded %d symbols: %s",
            len(targets),
            ", ".join(target.display_name() for target in targets) or "none",
        )
        self._active_targets = list(targets)
        return targets

    def load_initial_history(self, symbols: Sequence[str | ScanTarget]) -> None:
        strategy_config = self.config["strategy"]
        for target in _normalize_targets(symbols):
            history_config = self._history_config(target.provider)
            timeframes = (
                (strategy_config["signal_timeframe"], history_config["1h"]),
                (strategy_config["confirmation_4h"], history_config["4h"]),
                (strategy_config["confirmation_1d"], history_config["1d"]),
            )
            for timeframe, count in timeframes:
                try:
                    self._get_history(
                        target.symbol,
                        timeframe,
                        count,
                        refresh_count=count,
                        force_full=True,
                        provider=target.provider,
                    )
                except Exception:
                    self.logger.exception(
                        "Failed loading %s history for %s via %s",
                        timeframe,
                        target.symbol,
                        target.provider.upper(),
                    )

    def _get_history(
        self,
        symbol: str,
        timeframe: str,
        configured_count: int,
        *,
        refresh_count: int,
        force_full: bool = False,
        provider: str = "oanda",
    ) -> list[Candle]:
        data_provider = self._provider(provider)
        cache_key = (provider, symbol, timeframe)
        cached = self._history.get(cache_key, {})
        request_count = configured_count if force_full or not cached else refresh_count
        fresh_candles = data_provider.get_candles(symbol, timeframe, request_count)
        merged = {candle.timestamp.isoformat(): candle for candle in cached.values()}
        merged.update({candle.timestamp.isoformat(): candle for candle in fresh_candles})
        ordered = sorted(merged.values(), key=lambda candle: candle.timestamp)
        if len(ordered) > configured_count:
            ordered = ordered[-configured_count:]
        self._history[cache_key] = {candle.timestamp.isoformat(): candle for candle in ordered}
        return ordered

    def search_symbols(self, provider: str, query: str = "") -> str:
        data_provider = self._provider(provider)
        search = getattr(data_provider, "search_symbols", None)
        if search is None:
            return f"{provider.upper()} does not support symbol search."
        matches = search(query)
        if not matches:
            return f"No {provider.upper()} symbols matched {query!r}."
        heading = f"{provider.upper()} symbols"
        if query:
            heading += f" matching {query!r}"
        return heading + ":\n\n" + "\n".join(f"• {symbol}" for symbol in matches)

    def health_message(self) -> str:
        status = "ACTIVE" if self._running else "READY"
        started = _format_time(self._started_at)
        last_cycle = _format_time(self._last_cycle_at)
        interval = self.config["scanner"]["scan_interval_seconds"]
        return (
            f"Scanner health: {status}\n"
            f"Last scan cycle: {last_cycle}\n"
            f"Started: {started}\n"
            f"Symbols: {len(self._active_targets)}\n"
            f"Interval: {interval} seconds"
        )

    def symbols_message(self) -> str:
        targets = self._active_targets
        if not targets:
            return "No eligible symbols are currently loaded."
        return "Symbols currently being scanned:\n\n" + "\n".join(
            f"• {target.display_name()}" for target in targets
        )

    def _provider(self, provider: str) -> MarketDataProvider:
        if isinstance(self.data_provider, DataProviderManager):
            return self.data_provider.get_provider(provider)
        if provider != "oanda":
            raise ProviderUnavailable(f"{provider.upper()} provider is not configured")
        return self.data_provider

    def _history_config(self, provider: str) -> dict[str, int]:
        history = dict(self.config["data"]["history"])
        provider_config = self.config.get(provider)
        if isinstance(provider_config, dict) and isinstance(provider_config.get("history"), dict):
            history.update(
                {
                    timeframe: value
                    for timeframe, value in provider_config["history"].items()
                    if isinstance(value, int)
                }
            )
        return history

    def _append_discovered(self, targets: list[ScanTarget], provider: str) -> None:
        try:
            symbols = self._provider(provider).get_instruments()
        except Exception:
            self.logger.exception("%s symbol discovery failed; continuing", provider.upper())
            return
        targets.extend(ScanTarget(symbol=symbol, provider=provider) for symbol in symbols)

    def _append_configured_targets(
        self,
        targets: list[ScanTarget],
        provider: str,
        provider_config: dict[str, Any],
    ) -> None:
        mode = provider_config.get("symbol_mode", "manual")
        if mode == "manual":
            targets.extend(
                ScanTarget(symbol=symbol, provider=provider)
                for symbol in provider_config.get("symbols", [])
                if isinstance(symbol, str) and symbol
            )
            return

        try:
            symbols = self._provider(provider).get_instruments()
        except Exception:
            self.logger.exception(
                "%s symbol discovery failed; other providers will continue",
                provider.upper(),
            )
            return
        filters = provider_config.get("all_mode_filter", {})
        include_patterns = filters.get("include_patterns", []) if isinstance(filters, dict) else []
        exclude_patterns = filters.get("exclude_patterns", []) if isinstance(filters, dict) else []
        if not include_patterns:
            self.logger.warning(
                "%s all-symbol mode is enabled without include_patterns; "
                "no symbols will be scanned",
                provider.upper(),
            )
            return
        for symbol in symbols:
            if not _matches_any(symbol, include_patterns):
                continue
            if _matches_any(symbol, exclude_patterns):
                continue
            targets.append(ScanTarget(symbol=symbol, provider=provider))

    def _passes_volume_filter(self, target: ScanTarget) -> bool:
        volume_filter = self.config["scanner"]["volume_filter"]
        if not volume_filter["enabled"]:
            return True
        try:
            volume = self._provider(target.provider).get_24h_volume_usd(target.symbol)
        except Exception:
            self.logger.exception("Could not evaluate volume filter for %s", target.display_name())
            return False
        if volume is None:
            self.logger.warning(
                "Skipping %s: %s does not provide reliable 24h USD volume",
                target.display_name(),
                target.provider.upper(),
            )
            return False
        if volume < volume_filter["minimum_24h_volume_usd"]:
            self.logger.info(
                "Skipping %s: 24h USD volume %.2f is below threshold",
                target.display_name(),
                volume,
            )
            return False
        return True

    def _notify_status(self, message: str) -> None:
        if self.status_notifier is None:
            return
        try:
            self.status_notifier(message)
        except Exception:
            self.logger.exception("Telegram scan status notification failed")


def _target_from_config_entry(entry: Any) -> ScanTarget:
    if isinstance(entry, str):
        return ScanTarget(symbol=entry)
    if isinstance(entry, dict):
        return ScanTarget(symbol=entry["symbol"], provider=entry.get("provider", "oanda"))
    raise ValueError("Each scanner symbol must be a string or {symbol, provider} mapping")


def _normalize_targets(symbols: Sequence[str | ScanTarget]) -> list[ScanTarget]:
    return [
        symbol if isinstance(symbol, ScanTarget) else ScanTarget(symbol=symbol)
        for symbol in symbols
    ]


def _matches_any(symbol: str, patterns: Sequence[Any]) -> bool:
    return any(
        isinstance(pattern, str) and fnmatchcase(symbol.casefold(), pattern.casefold())
        for pattern in patterns
    )


def _has_engulfing_pattern(
    previous: Candle,
    current: Candle,
    pattern_config: dict[str, Any],
    *,
    require_closed: bool,
) -> bool:
    reject_doji = pattern_config["reject_doji"]
    bullish = pattern_config["bullish_engulfing"] and is_bullish_engulfing(
        previous, current, reject_doji, require_closed=require_closed
    )
    bearish = pattern_config["bearish_engulfing"] and is_bearish_engulfing(
        previous, current, reject_doji, require_closed=require_closed
    )
    return bullish or bearish


def _early_minutes_before_close(pattern_config: dict[str, Any]) -> int:
    early = pattern_config.get("early_detection", {})
    if not isinstance(early, dict) or not early.get("enabled", False):
        return 0
    minutes = early.get("minutes_before_close", 5)
    try:
        return max(0, int(minutes))
    except (TypeError, ValueError):
        return 0


def _format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else "not yet"


def _latest_closed(candles: list[Candle]) -> Candle | None:
    closed = [candle for candle in candles if candle.is_closed]
    return closed[-1] if closed else None