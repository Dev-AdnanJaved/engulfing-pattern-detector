from __future__ import annotations

import json
import logging
import time
from datetime import timezone
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.strategy.signal import Signal


class TelegramError(RuntimeError):
    """Raised when Telegram cannot accept an alert."""


class TelegramNotifier:
    def __init__(
        self,
        bot_token: Optional[str],
        chat_id: Optional[str],
        enabled: bool,
        timeout_seconds: float = 10,
        retries: int = 2,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.logger = logger or logging.getLogger(__name__)
        if self.enabled and (not self.bot_token or not self.chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when Telegram is enabled")

    def send_signal(self, signal: Signal) -> bool:
        if not self.enabled:
            return False
        return self.send_message(format_signal(signal), parse_mode="Markdown")

    def send_message(self, text: str, parse_mode: Optional[str] = None) -> bool:
        if not self.enabled:
            return False
        payload = urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                **({"parse_mode": parse_mode} if parse_mode else {}),
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        result = self._api_request("sendMessage", payload, timeout=self.timeout_seconds)
        if result.get("ok") is not True:
            raise TelegramError(f"Telegram rejected message: {result}")
        return True

    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        query = urlencode(
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message"]),
            }
        ).encode("utf-8")
        result = self._api_request("getUpdates", query, timeout=max(self.timeout_seconds, timeout + 5), method="GET")
        updates = result.get("result")
        if not isinstance(updates, list):
            raise TelegramError(f"Telegram returned an invalid updates response: {result}")
        return [update for update in updates if isinstance(update, dict)]

    def _api_request(
        self,
        method_name: str,
        payload: bytes,
        *,
        timeout: float,
        method: str = "POST",
    ) -> dict[str, Any]:
        request_url = f"https://api.telegram.org/bot{self.bot_token}/{method_name}"
        if method == "GET":
            request_url = f"{request_url}?{payload.decode('utf-8')}"
            request = Request(request_url, method="GET")
        else:
            request = Request(
                request_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method=method,
            )
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise TelegramError(f"Telegram returned an invalid response for {method_name}")
                return result
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    self.logger.warning("Telegram HTTP %s; retrying %s", exc.code, method_name)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise TelegramError(f"Telegram {method_name} failed: {body[:300]}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.retries:
                    self.logger.warning("Telegram request failed; retrying %s: %s", method_name, exc)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise TelegramError(f"Telegram {method_name} failed: {exc}") from exc
        raise TelegramError(f"Telegram {method_name} failed after retries")


def format_signal(signal: Signal) -> str:
    display_pair = signal.symbol.replace("_", "/")
    direction_icon = "🟢" if signal.direction == "BUY" else "🔴"
    pattern_icon = "🟢" if signal.direction == "BUY" else "🔴"
    direction_word = "Bullish" if signal.direction == "BUY" else "Bearish"
    candle_time = signal.candle_time.astimezone(timezone.utc).strftime("%H:%M UTC")
    current_price = _format_price(signal.current_price)
    return (
        f"🚨 *{signal.direction} SIGNAL — {display_pair}*\n\n"
        f"*Pair:* {display_pair}\n"
        f"*Signal:* {direction_icon} {signal.direction}\n"
        f"*Timeframe:* 1H\n"
        f"*Current Price:* {current_price}\n\n"
        f"📊 *SETUP*\n\n"
        f"• 1H: {pattern_icon} {direction_word} Engulfing\n"
        f"• 4H: {pattern_icon} {signal.h4_direction.title()}\n"
        f"• 1D: {pattern_icon} {signal.d1_direction.title()}\n\n"
        f"💡 *REASON*\n\n"
        f"{direction_word} engulfing pattern formed on the 1H closed candle with "
        f"{signal.h4_direction} 4H and {signal.d1_direction} 1D confirmation.\n\n"
        f"🕐 *CANDLE CLOSED:* {candle_time}"
    )


def _format_price(value: float) -> str:
    return f"{value:.5f}".rstrip("0").rstrip(".")