import logging

from app.notifications.telegram_commands import TelegramCommandPoller


class FakeNotifier:
    enabled = True
    chat_id = "12345"
    chat_ids = ["12345"]

    def __init__(self):
        self.messages = []

    def send_message(self, text, parse_mode=None):
        self.messages.append((text, parse_mode))
        return True

    def get_updates(self, offset=0, timeout=25):
        del offset, timeout
        return []


class FakeScanner:
    def __init__(self):
        self.scan_calls = 0

    def health_message(self):
        return "Scanner health: ACTIVE"

    def symbols_message(self):
        return "Symbols currently being scanned:\n\n• XAU_USD"

    def scan_once(self):
        self.scan_calls += 1


def update(text, chat_id="12345"):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def make_poller():
    notifier = FakeNotifier()
    scanner = FakeScanner()
    poller = TelegramCommandPoller(notifier, scanner, logging.getLogger("test"))
    return poller, notifier, scanner


def test_help_aliases_reply_with_command_list():
    for command in ("/help", "/command", "/commands", "/start"):
        poller, notifier, _ = make_poller()
        poller._handle_update(update(command))
        assert "/health" in notifier.messages[-1][0]
        assert "/scan" in notifier.messages[-1][0]
        assert "/capitalsymbols" in notifier.messages[-1][0]
        assert "/binancesymbols" in notifier.messages[-1][0]


def test_health_and_symbols_commands_reply():
    poller, notifier, _ = make_poller()
    poller._handle_update(update("/health"))
    assert notifier.messages[-1][0] == "Scanner health: ACTIVE"
    poller._handle_update(update("/symbols"))
    assert "XAU_USD" in notifier.messages[-1][0]


def test_scan_command_triggers_one_scan_and_replies():
    poller, notifier, scanner = make_poller()
    poller._handle_update(update("/scan"))
    assert scanner.scan_calls == 1
    assert notifier.messages == [
        ("Manual scan started.", None),
        ("Manual scan completed. Any qualifying alert was sent separately.", None),
    ]


def test_unauthorized_chat_is_ignored():
    poller, notifier, scanner = make_poller()
    poller._handle_update(update("/scan", chat_id="99999"))
    assert scanner.scan_calls == 0
    assert notifier.messages == []


def test_capitalsymbols_command_searches_capital_provider():
    class CapitalAwareScanner(FakeScanner):
        def search_symbols(self, provider, query=""):
            return f"{provider}:{query}"

    notifier = FakeNotifier()
    scanner = CapitalAwareScanner()
    poller = TelegramCommandPoller(notifier, scanner, logging.getLogger("test"))
    poller._handle_update(update("/capitalsymbols US100"))
    assert notifier.messages[-1][0] == "capital:US100"
