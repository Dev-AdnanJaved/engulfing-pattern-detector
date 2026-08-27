from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    """Raised when the scanner configuration is missing or invalid."""


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigurationError("Configuration root must be a YAML mapping")

    _validate_config(config)
    return config


def _validate_config(config: dict[str, Any]) -> None:
    required_sections = ("app", "scanner", "strategy", "pattern", "data", "alerts", "storage", "logging")
    missing = [section for section in required_sections if not isinstance(config.get(section), dict)]
    if missing:
        raise ConfigurationError(f"Missing configuration sections: {', '.join(missing)}")

    scanner = config["scanner"]
    if scanner.get("symbol_mode") not in {"manual", "all"}:
        raise ConfigurationError("scanner.symbol_mode must be 'manual' or 'all'")
    if scanner["symbol_mode"] == "manual":
        symbols = scanner.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ConfigurationError("scanner.symbols must be a non-empty list in manual mode")
        for entry in symbols:
            if isinstance(entry, str) and entry:
                continue
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("symbol"), str)
                and entry["symbol"]
                and entry.get("provider", "oanda") in {"oanda", "mt5", "capital"}
            ):
                continue
            raise ConfigurationError(
                "scanner.symbols entries must be symbols or {symbol, provider} mappings"
            )
    if not isinstance(scanner.get("scan_interval_seconds"), (int, float)) or scanner["scan_interval_seconds"] <= 0:
        raise ConfigurationError("scanner.scan_interval_seconds must be greater than zero")

    volume_filter = scanner.get("volume_filter")
    if not isinstance(volume_filter, dict) or not isinstance(volume_filter.get("enabled"), bool):
        raise ConfigurationError("scanner.volume_filter.enabled must be true or false")
    if not isinstance(volume_filter.get("minimum_24h_volume_usd"), (int, float)) or volume_filter["minimum_24h_volume_usd"] < 0:
        raise ConfigurationError("scanner.volume_filter.minimum_24h_volume_usd must be non-negative")

    strategy = config["strategy"]
    for key in ("signal_timeframe", "confirmation_4h", "confirmation_1d"):
        if not isinstance(strategy.get(key), str) or not strategy[key]:
            raise ConfigurationError(f"strategy.{key} must be a non-empty string")

    pattern = config["pattern"]
    for key in ("bullish_engulfing", "bearish_engulfing", "body_only", "reject_doji"):
        if not isinstance(pattern.get(key), bool):
            raise ConfigurationError(f"pattern.{key} must be true or false")
    if pattern["body_only"] is not True:
        raise ConfigurationError("pattern.body_only must remain true for this strategy")
    early = pattern.get("early_detection", {})
    if early is None:
        early = {}
    if not isinstance(early, dict):
        raise ConfigurationError("pattern.early_detection must be a mapping")
    if early:
        if not isinstance(early.get("enabled"), bool):
            raise ConfigurationError("pattern.early_detection.enabled must be true or false")
        minutes = early.get("minutes_before_close", 5)
        if not isinstance(minutes, (int, float)) or minutes < 0:
            raise ConfigurationError(
                "pattern.early_detection.minutes_before_close must be a non-negative number"
            )

    data = config["data"]
    if data.get("provider") != "oanda":
        raise ConfigurationError("data.provider must be 'oanda'")
    history = data.get("history")
    if not isinstance(history, dict):
        raise ConfigurationError("data.history must be a mapping")
    for timeframe in ("1h", "4h", "1d"):
        if not isinstance(history.get(timeframe), int) or history[timeframe] < 3:
            raise ConfigurationError(f"data.history.{timeframe} must be an integer of at least 3")
    if not isinstance(data.get("timeout_seconds"), (int, float)) or data["timeout_seconds"] <= 0:
        raise ConfigurationError("data.timeout_seconds must be greater than zero")

    alerts = config["alerts"]
    telegram = alerts.get("telegram")
    if not isinstance(telegram, dict) or not isinstance(telegram.get("enabled"), bool):
        raise ConfigurationError("alerts.telegram.enabled must be true or false")
    if "status_updates" in telegram and not isinstance(telegram.get("status_updates"), bool):
        raise ConfigurationError("alerts.telegram.status_updates must be true or false")
    telegram.setdefault("status_updates", False)
    cooldown = alerts.get("cooldown_minutes", 0)
    if not isinstance(cooldown, (int, float)) or cooldown < 0:
        raise ConfigurationError("alerts.cooldown_minutes must be a non-negative number")
    if not isinstance(config["storage"].get("database"), str) or not config["storage"]["database"]:
        raise ConfigurationError("storage.database must be a non-empty path")
    logging_config = config["logging"]
    if logging_config.get("level") not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("logging.level must be a standard Python logging level")
    if not isinstance(logging_config.get("file_enabled"), bool):
        raise ConfigurationError("logging.file_enabled must be true or false")
    if logging_config["file_enabled"] and (
        not isinstance(logging_config.get("file_path"), str) or not logging_config["file_path"]
    ):
        raise ConfigurationError("logging.file_path must be set when file logging is enabled")

    for provider in ("oanda", "mt5", "capital"):
        provider_config = config.get(provider)
        if provider_config is not None:
            _validate_optional_provider(provider, provider_config)


def _validate_optional_provider(provider: str, provider_config: Any) -> None:
    if not isinstance(provider_config, dict):
        raise ConfigurationError(f"{provider} must be a mapping")
    if not isinstance(provider_config.get("enabled"), bool):
        raise ConfigurationError(f"{provider}.enabled must be true or false")
    if provider_config.get("symbol_mode", "manual") not in {"manual", "all"}:
        raise ConfigurationError(f"{provider}.symbol_mode must be 'manual' or 'all'")
    symbols = provider_config.get("symbols", [])
    if not isinstance(symbols, list) or not all(
        isinstance(symbol, str) and symbol for symbol in symbols
    ):
        raise ConfigurationError(f"{provider}.symbols must be a list of non-empty strings")
    all_mode_filter = provider_config.get("all_mode_filter", {})
    if not isinstance(all_mode_filter, dict):
        raise ConfigurationError(f"{provider}.all_mode_filter must be a mapping")
    for key in ("include_patterns", "exclude_patterns"):
        patterns = all_mode_filter.get(key, [])
        if not isinstance(patterns, list) or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ConfigurationError(f"{provider}.all_mode_filter.{key} must be a list of strings")
    if provider == "capital":
        symbol_map = provider_config.get("capital_symbols", {})
        if not isinstance(symbol_map, dict):
            raise ConfigurationError("capital.capital_symbols must be a mapping")
        for symbol, mapping in symbol_map.items():
            if not isinstance(symbol, str) or not symbol:
                raise ConfigurationError("capital.capital_symbols keys must be non-empty strings")
            epic = mapping.get("epic") if isinstance(mapping, dict) else mapping
            if not isinstance(epic, str) or not epic:
                raise ConfigurationError(
                    "capital.capital_symbols values must contain a non-empty epic"
                )
        history = provider_config.get("history")
        if history is not None:
            if not isinstance(history, dict):
                raise ConfigurationError("capital.history must be a mapping")
            for timeframe in ("1h", "4h", "1d"):
                if not isinstance(history.get(timeframe), int) or history[timeframe] < 3:
                    raise ConfigurationError(
                        f"capital.history.{timeframe} must be an integer of at least 3"
                    )
        timeout = provider_config.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise ConfigurationError("capital.timeout_seconds must be greater than zero")
        retries = provider_config.get("retries")
        if retries is not None and (not isinstance(retries, int) or retries < 0):
            raise ConfigurationError("capital.retries must be a non-negative integer")