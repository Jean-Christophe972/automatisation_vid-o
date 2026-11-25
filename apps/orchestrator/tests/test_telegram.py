import json

import httpx
import pytest
from fastapi.testclient import TestClient

from contentmachine import api
from contentmachine.reviews import ReviewStore
from contentmachine.telegram import (Command, Decision, TelegramClient, TelegramError,
                                     parse_update, secret_matches, webhook_secret)

from tests_helpers import CHAT, TOKEN, FakeTelegram


def cb(data, chat=CHAT, msg_id=77):
    return {"callback_query": {"id": "cq1", "data": data,
                               "message": {"message_id": msg_id, "chat": {"id": int(chat)}}}}


def text(t, chat=CHAT):
    return {"message": {"text": t, "chat": {"id": int(chat)}}}


# --- parse_update -------------------------------------------------------------

def test_decision_from_authorized_chat():
    d = parse_update(cb("rv:abc_123:approve"), CHAT)
    assert d == Decision("abc_123", "approve", "cq1", 77)


@pytest.mark.parametrize("update,allowed", [
    (cb("rv:abc:approve", chat="999"), CHAT),       # autre personne
    (cb("rv:abc:approve"), None),                    # aucun chat configuré
    (cb("rv:abc:publish_everything"), CHAT),         # action inconnue
    (cb("rv:../../x:approve"), CHAT),                # identifiant forgé
    ({"edited_message": {}}, CHAT),
])
def test_ignored_updates(update, allowed):
    assert parse_update(update, allowed) is None


def test_commands():
    assert parse_update(text("/test"), CHAT) == Command("test", int(CHAT), True)
    assert parse_update(text("/start@MonBot"), CHAT).name == "start"
    assert parse_update(text("/test", chat="999"), CHAT).authorized is False
    assert parse_update(text("bonjour"), CHAT) is None


def test_webhook_secret():
    s = webhook_secret(TOKEN)
    assert secret_matches(TOKEN, s)
    assert not secret_matches(TOKEN, "faux")
    assert not secret_matches(TOKEN, None)
    assert TOKEN not in s


# --- client -------------------------------------------------------------------

def test_send_for_review_has_two_buttons(tmp_path):
    fake = FakeTelegram()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 10)
    assert fake.client().send_for_review(CHAT, video, "rid1", "légende") == 77
    method, body = fake.calls[0]
    assert method == "sendVideo"
    assert "rv:rid1:approve" in body and "rv:rid1:reject" in body


def test_error_never_leaks_token():
    fake = FakeTelegram(fail_with="Unauthorized")
    with pytest.raises(TelegramError) as exc:
        fake.client().send_message(CHAT, "x")
    assert TOKEN not in str(exc.value)


def test_invalid_review_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        FakeTelegram().client().send_for_review(CHAT, tmp_path / "v.mp4", "../x", "c")


# --- webhook de bout en bout --------------------------------------------------

@pytest.fixture
def bot(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setattr(api, "_client", fake.client())
    monkeypatch.setattr(api, "store", ReviewStore())
    return fake


def post(update, secret=None):
    with TestClient(api.app) as c:
        return c.post("/telegram/webhook", json=update,
                      headers={"X-Telegram-Bot-Api-Secret-Token": secret or webhook_secret(TOKEN)})


def test_webhook_refuses_wrong_secret(bot):
    assert post(text("/start"), secret="mauvais").status_code == 403
    assert bot.calls == []


def test_start_authorized(bot):
    assert post(text("/start")).status_code == 200
    assert bot.methods() == ["sendMessage"]


def test_stranger_gets_nothing(bot):
    post(text("/test", chat="999"))
    post(cb("rv:x:approve", chat="999"))
    assert bot.calls == []


def test_start_helps_setup_when_chat_not_configured(bot, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    post(text("/start", chat="555"))
    assert "555" in bot.calls[0][1]


def test_approve_then_double_click(bot):
    review = api.store.create("test", "/tmp/v.mp4", "c")
    post(cb(f"rv:{review.id}:approve"))
    assert api.store.get(review.id).status == "approved"
    assert bot.methods() == ["answerCallbackQuery", "editMessageReplyMarkup"]
    post(cb(f"rv:{review.id}:reject"))              # second clic : refusé, statut inchangé
    assert api.store.get(review.id).status == "approved"
    assert "D\\u00e9j\\u00e0 trait\\u00e9e" in bot.calls[-2][1] or "Déjà traitée" in bot.calls[-2][1]


def test_unknown_review(bot):
    post(cb("rv:inconnu:approve"))
    assert "inconnue" in bot.calls[0][1]


def test_test_command_sends_qa_checked_video(bot):
    post(text("/test"))
    assert bot.methods() == ["sendMessage", "sendVideo"]
    assert "OK (" in bot.calls[1][1]                 # le résumé du contrôle qualité est dans la légende
