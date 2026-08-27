from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from app.config.loader import ConfigurationError, load_config
from app.data.capital import CapitalClient
from app.data.manager import DataProviderManager
from app.data.mt5 import Mt5Client, Mt5Error
from app.data.oanda import OandaClient
from app.notifications.telegram import TelegramNotifier
from app.notifications.telegram_commands import TelegramCommandPoller
from app.scanner.scanner import ScanTarget, Scanner
from app.storage.database import SignalDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_local_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding managed environment variables."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def configure_logging(config: dict[str, object], root: Path) -> logging.Logger:
    logging_config = config["logging"]
    level = getattr(logging, logging_config["level"])
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logging_config["file_enabled"]:
        log_path = root / logging_config["file_path"]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("scanner")


def build_scanner(
    config: dict[str, object],
    root: Path,
    logger: logging.Logger,
) -> tuple[Scanner, SignalDatabase, TelegramNotifier, DataProviderManager]:
    providers = DataProviderManager(logger=logger)
    oanda_config = config.get("oanda", {})
    if not isinstance(oanda_config, dict) or oanda_config.get("enabled", True):
        oanda = OandaClient(
            api_key=os.environ.get("OANDA_API_KEY", ""),
            account_id=os.environ.get("OANDA_ACCOUNT_ID", ""),
            timeout_seconds=config["data"]["timeout_seconds"],
            logger=logger,
        )
        providers.add_provider("oanda", oanda)
    else:
        providers.mark_unavailable("oanda", "disabled in configuration")
    mt5_config = config.get("mt5", {})
    if isinstance(mt5_config, dict) and mt5_config.get("enabled", False):
        mt5: Mt5Client | None = None
        try:
            mt5 = Mt5Client(
                os.environ.get("MT5_LOGIN", ""),
                os.environ.get("MT5_PASSWORD", ""),
                os.environ.get("MT5_SERVER", ""),
                logger=logger,
            )
            mt5.connect()
            providers.add_provider("mt5", mt5)
        except Exception as exc:
            # MT5 is optional: a terminal or package problem must not stop OANDA.
            logger.error("MT5 provider unavailable: %s", exc)
            if mt5 is not None:
                mt5.close()
            providers.mark_unavailable("mt5", str(exc))
    capital_config = config.get("capital", {})
    if isinstance(capital_config, dict) and capital_config.get("enabled", False):
        logger.info("Capital.com provider enabled")
        capital: CapitalClient | None = None
        try:
            capital = CapitalClient(
                os.environ.get("CAPITAL_IDENTIFIER", ""),
                os.environ.get("CAPITAL_API_KEY", ""),
                os.environ.get("CAPITAL_PASSWORD", ""),
                timeout_seconds=capital_config.get(
                    "timeout_seconds", config["data"]["timeout_seconds"]
                ),
                retries=capital_config.get("retries", 2),
                symbol_epics=capital_config.get("capital_symbols", {}),
                logger=logger,
            )
            capital.connect()
            providers.add_provider("capital", capital)
        except Exception as exc:
            logger.error("Capital.com provider unavailable: %s", exc)
            if capital is not None:
                capital.close()
            providers.mark_unavailable("capital", str(exc))
    telegram_config = config["alerts"]["telegram"]
    notifier = TelegramNotifier(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        enabled=telegram_config["enabled"],
        timeout_seconds=config["data"]["timeout_seconds"],
        logger=logger,
    )
    database = SignalDatabase(root / config["storage"]["database"])
    scanner = Scanner(config, providers, notifier, database, logger)
    return scanner, database, notifier, providers


def startup_message(scanner: Scanner, symbols: list[ScanTarget]) -> str:
    return (
        "🤖 *1H Engulfing Scanner started*\n\n"
        "*Status:* 🟢 Active\n"
        f"*Mode:* {scanner.config['scanner']['symbol_mode'].upper()}\n"
        f"*Symbols:* {len(symbols)}\n"
        "*Strategy:* 1H engulfing + closed 4H and 1D confirmation\n\n"
        "Send /help for available commands."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan OANDA, MT5, and Capital.com confirmed 1H engulfing signals"
    )
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "config.yaml"))
    parser.add_argument("--once", action="store_true", help="scan once instead of running continuously")
    parser.add_argument("--mt5-search", metavar="QUERY", help="list MT5 symbols matching QUERY and exit")
    parser.add_argument(
        "--capital-search",
        metavar="QUERY",
        help="list Capital.com symbols matching QUERY and exit",
    )
    args = parser.parse_args()

    try:
        load_local_env(PROJECT_ROOT / ".env")
        config = load_config(args.config)
        logger = configure_logging(config, PROJECT_ROOT)
        scanner, database, notifier, providers = build_scanner(config, PROJECT_ROOT, logger)
        scanner.status_notifier = lambda message: notifier.send_message(message, parse_mode="Markdown")
        command_poller: TelegramCommandPoller | None = None
        startup_sent = False

        def request_shutdown(signum: int, _frame: object) -> None:
            logger.info("Shutdown signal received: %s", signum)
            scanner.stop()

        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)
        try:
            symbols = scanner.prepare()
            if args.mt5_search is not None:
                print(scanner.search_symbols("mt5", args.mt5_search))
                return
            if args.capital_search is not None:
                print(scanner.search_symbols("capital", args.capital_search))
                return
            if notifier.enabled:
                try:
                    notifier.send_message(startup_message(scanner, symbols), parse_mode="Markdown")
                    startup_sent = True
                    logger.info("Telegram startup notification sent")
                except Exception:
                    logger.exception("Telegram startup notification failed")
                command_poller = TelegramCommandPoller(notifier, scanner, logger)
                command_poller.start()
            if args.once:
                scanner.scan_once(symbols)
            else:
                scanner.run_forever(symbols)
        finally:
            if command_poller is not None:
                command_poller.stop()
            if startup_sent and notifier.enabled:
                try:
                    notifier.send_message(
                        "⏹️ *1H Engulfing Scanner stopped*\n\n"
                        "*Status:* ⚪ Inactive\n"
                        "No further scans will run until the scanner is started again.",
                        parse_mode="Markdown",
                    )
                    logger.info("Telegram stop notification sent")
                except Exception:
                    logger.exception("Telegram stop notification failed")
            database.close()
            providers.close()
    except (ConfigurationError, ValueError) as exc:
        logging.getLogger("scanner").error("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        logging.getLogger("scanner").info("Scanner stopped")


if __name__ == "__main__":
    main()