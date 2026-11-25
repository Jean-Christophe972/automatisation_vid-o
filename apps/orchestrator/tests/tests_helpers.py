"""Faux serveur Telegram partagé par les tests."""
import httpx

from contentmachine.telegram import TelegramClient

TOKEN = "123456:ABC-test-token"
CHAT = "424242"


class FakeTelegram:
    """Faux serveur Telegram : enregistre chaque appel et répond ok."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = request.content.decode("utf-8", "replace")
        self.calls.append((method, body))
        if self.fail_with:
            return httpx.Response(400, json={"ok": False, "description": self.fail_with})
        result = {"message_id": 77} if method in ("sendVideo", "sendMessage") else True
        return httpx.Response(200, json={"ok": True, "result": result})

    def client(self):
        return TelegramClient(TOKEN, http=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def methods(self):
        return [m for m, _ in self.calls]
