from __future__ import annotations

import logging
import threading
from typing import Any

from app.notifications.telegram import TelegramNotifier
from app.scanner.scanner import Scanner


class TelegramCommandPoller:
    """Long-poll Telegram commands from the configured chat in a background thread."""

    def __init__(
        self,
        notifier: TelegramNotifier,
        scanner: Scanner,
        logger: logging.Logger | None = None,
    ) -> None:
        self.notifier = notifier
        self.scanner = scanner
        self.logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        if not self.notifier.enabled:
            self.logger.info("Telegram command polling disabled")
            return
        self._prime_update_offset()
        self._thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)
        self._thread.start()
        self.logger.info("Telegram command polling started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _prime_update_offset(self) -> None:
        try:
            updates = self.notifier.get_updates(offset=-1, timeout=0)
            if updates:
                latest_update_id = updates[-1].get("update_id")
                if isinstance(latest_update_id, int):
                    self._offset = latest_update_id + 1
        except Exception:
            self.logger.exception("Could not clear old Telegram updates; continuing")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = self.notifier.get_updates(offset=self._offset, timeout=25)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = max(self._offset, update_id + 1)
                    self._handle_update(update)
            except Exception:
                self.logger.exception("Telegram command polling failed; retrying")
                self._stop_event.wait(3)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(text, str):
            return
        if str(chat.get("id")) not in {str(item) for item in self.notifier.chat_ids}:
            self.logger.warning("Ignoring Telegram command from unauthorized chat")
            return
        command = text.strip().split()[0].lower() if text.strip() else ""
        command = command.split("@", 1)[0]
        if command in {"/start", "/help", "/command", "/commands"}:
            self._reply(self._help_text())
        elif command in {"/health", "/status"}:
            self._reply(self.scanner.health_message())
        elif command == "/symbols":
            self._reply(self.scanner.symbols_message())
        elif command == "/mt5symbols":
            query = " ".join(text.strip().split()[1:])
            try:
                self._reply(self.scanner.search_symbols("mt5", query))
            except Exception:
                self.logger.exception("MT5 symbol search failed")
                self._reply("MT5 symbol search is currently unavailable.")
        elif command == "/capitalsymbols":
            query = " ".join(text.strip().split()[1:])
            try:
                self._reply(self.scanner.search_symbols("capital", query))
            except Exception:
                self.logger.exception("Capital.com symbol search failed")
                self._reply("Capital.com symbol search is currently unavailable.")
        elif command == "/binancesymbols":
            query = " ".join(text.strip().split()[1:])
            try:
                self._reply(self.scanner.search_symbols("binance_futures", query))
            except Exception:
                self.logger.exception("Binance Futures symbol search failed")
                self._reply("Binance Futures symbol search is currently unavailable.")
        elif command == "/scan":
            self._reply("Manual scan started.")
            self.scanner.scan_once()
            self._reply("Manual scan completed. Any qualifying alert was sent separately.")
        elif command:
            self._reply(f"Unknown command: {command}\n\n{self._help_text()}")

    def _reply(self, text: str) -> None:
        try:
            self.notifier.send_message(text)
        except Exception:
            self.logger.exception("Could not send Telegram command response")

    @staticmethod
    def _help_text() -> str:
        return (
            "1H Engulfing Scanner commands:\n\n"
            "/help — show this help\n"
            "/health — check whether the scanner is active\n"
            "/status — show scanner status and last cycle\n"
            "/symbols — show symbols currently being scanned\n"
            "/mt5symbols [query] — search symbols available from MT5\n"
            "/capitalsymbols [query] — search symbols available from Capital.com\n"
            "/binancesymbols [query] — search symbols available from Binance Futures\n"
            "/scan — run a manual scan now"
        )