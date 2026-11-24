"""Validation humaine des vidéos via un bot Telegram.

Règle du projet : rien n'est publié sans un clic « Publier » de l'utilisateur.

- `TelegramClient.send_for_review` envoie l'aperçu vidéo avec deux boutons.
- `parse_update` transforme une mise à jour Telegram en action, et ignore tout
  ce qui ne vient pas du chat autorisé (TELEGRAM_CHAT_ID).
- Le secret du webhook est dérivé du token du bot : aucune variable de plus à gérer.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
APPROVE, REJECT = "approve", "reject"
_REVIEW_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def webhook_secret(bot_token: str) -> str:
    """Secret envoyé par Telegram dans l'en-tête X-Telegram-Bot-Api-Secret-Token."""
    return hmac.new(b"contentmachine-webhook", bot_token.encode(), hashlib.sha256).hexdigest()


def secret_matches(bot_token: str, received: str | None) -> bool:
    return bool(received) and hmac.compare_digest(webhook_secret(bot_token), received)


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class Decision:
    review_id: str
    action: str            # APPROVE | REJECT
    callback_id: str
    message_id: int


@dataclass(frozen=True)
class Command:
    name: str              # "start", "test"...
    chat_id: int
    authorized: bool


class TelegramClient:
    def __init__(self, token: str, http: httpx.Client | None = None):
        if not token:
            raise ValueError("token Telegram manquant")
        self._base = f"{API}/bot{token}"
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))

    def _call(self, method: str, **kwargs) -> dict:
        try:
            r = self._http.post(f"{self._base}/{method}", **kwargs)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} : erreur réseau ({type(exc).__name__})") from exc
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            # on ne journalise jamais l'URL : elle contient le token
            raise TelegramError(f"{method} : {data.get('description', r.status_code)}")
        return data["result"]

    def send_message(self, chat_id: int | str, text: str) -> dict:
        return self._call("sendMessage", json={"chat_id": chat_id, "text": text})

    def send_for_review(self, chat_id: int | str, video: Path, review_id: str, caption: str) -> int:
        if not _REVIEW_ID.match(review_id):
            raise ValueError(f"identifiant de revue invalide : {review_id!r}")
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Publier", "callback_data": f"rv:{review_id}:{APPROVE}"},
            {"text": "❌ Jeter", "callback_data": f"rv:{review_id}:{REJECT}"},
        ]]}
        with Path(video).open("rb") as fh:
            result = self._call("sendVideo", data={
                "chat_id": str(chat_id),
                "caption": caption[:1000],
                "supports_streaming": "true",
                "reply_markup": json.dumps(keyboard),
            }, files={"video": (Path(video).name, fh, "video/mp4")})
        return int(result["message_id"])

    def confirm(self, chat_id: int | str, decision: Decision, label: str) -> None:
        """Répond au clic et remplace les boutons par le verdict (évite un double clic)."""
        self._call("answerCallbackQuery", json={"callback_query_id": decision.callback_id, "text": label})
        self._call("editMessageReplyMarkup", json={
            "chat_id": chat_id, "message_id": decision.message_id,
            "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]},
        })

    def set_webhook(self, url: str, secret: str) -> None:
        self._call("setWebhook", json={
            "url": url, "secret_token": secret,
            "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True,
        })


def parse_update(update: dict, allowed_chat_id: str | None) -> Decision | Command | None:
    """Renvoie l'action correspondant à la mise à jour, ou None s'il faut l'ignorer."""
    allowed = str(allowed_chat_id).strip() if allowed_chat_id else None

    cq = update.get("callback_query")
    if cq:
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not allowed or chat_id != allowed:
            log.warning("clic ignoré : chat non autorisé")
            return None
        m = re.fullmatch(r"rv:([A-Za-z0-9_-]{1,40}):(approve|reject)", cq.get("data") or "")
        if not m:
            return None
        return Decision(review_id=m.group(1), action=m.group(2),
                        callback_id=str(cq["id"]), message_id=int(msg["message_id"]))

    msg = update.get("message")
    if msg and isinstance(msg.get("text"), str) and msg["text"].startswith("/"):
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return None
        name = msg["text"][1:].split()[0].split("@")[0].lower()
        return Command(name=name, chat_id=int(chat_id), authorized=bool(allowed) and str(chat_id) == allowed)
    return None
