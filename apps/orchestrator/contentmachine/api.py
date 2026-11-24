"""Service orchestrateur (déployé sur Railway).

v0.2 : état du service + circuit de validation Telegram.
Commandes du bot (uniquement depuis TELEGRAM_CHAT_ID) :
  /start  vérifie la configuration
  /test   envoie une vidéo d'essai passée au contrôle qualité, avec les boutons
"""
from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import __version__, config
from .qa import QAError, run_qa
from .reviews import AlreadyDecided, ReviewStore
from .telegram import (APPROVE, Command, Decision, TelegramClient, TelegramError,
                       parse_update, secret_matches, webhook_secret)

log = logging.getLogger("contentmachine")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

store = ReviewStore()
_client: TelegramClient | None = None


def telegram() -> TelegramClient | None:
    """Client Telegram, ou None si le token n'est pas configuré."""
    global _client
    token = config.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    if _client is None:
        _client = TelegramClient(token)
    return _client


def _register_webhook() -> None:
    domain = config.get("RAILWAY_PUBLIC_DOMAIN")
    token = config.get("TELEGRAM_BOT_TOKEN")
    client = telegram()
    if not (domain and token and client):
        log.info("webhook Telegram non enregistré (token ou domaine public absent)")
        return
    try:
        client.set_webhook(f"https://{domain}/telegram/webhook", webhook_secret(token))
        log.info("webhook Telegram enregistré")
    except TelegramError as exc:
        log.error("échec d'enregistrement du webhook : %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _register_webhook()
    yield


app = FastAPI(title="Content Machine", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    """Utilisé par Railway pour savoir si le déploiement est sain. Ne renvoie aucun secret."""
    missing = config.missing_required()
    return {
        "status": "ok",
        "version": __version__,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ready_for_jobs": not missing,
        "missing_required": missing,
        "telegram": bool(config.get("TELEGRAM_BOT_TOKEN")) and bool(config.get("TELEGRAM_CHAT_ID")),
    }


@app.get("/config")
def config_status() -> dict:
    return config.status()


# --- Telegram ---------------------------------------------------------------

def send_test_review(chat_id: int) -> None:
    from .testvideo import make_test_video

    client = telegram()
    if client is None:
        return
    try:
        video = make_test_video()
        report = run_qa(video, "clip")
        review = store.create("test", str(video), "Vidéo d'essai")
        caption = (f"🧪 Vidéo d'essai\nContrôle qualité : {report.summary()}\n"
                   "Clique sur un bouton pour vérifier le circuit.")
        client.send_for_review(chat_id, video, review.id, caption)
    except (QAError, TelegramError, OSError) as exc:
        log.error("envoi de la vidéo d'essai impossible : %s", exc)
        try:
            client.send_message(chat_id, f"⚠️ Envoi de la vidéo d'essai impossible : {exc}")
        except TelegramError:
            pass


def handle_command(cmd: Command, background: BackgroundTasks) -> None:
    client = telegram()
    if client is None:
        return
    if not cmd.authorized:
        # Configuration initiale : on n'aide que si aucun chat n'est encore autorisé.
        if cmd.name == "start" and not config.get("TELEGRAM_CHAT_ID"):
            client.send_message(cmd.chat_id, f"Ton identifiant Telegram est {cmd.chat_id}. "
                                             "Mets-le dans la variable Railway TELEGRAM_CHAT_ID.")
        return
    if cmd.name == "start":
        client.send_message(cmd.chat_id, "✅ Bot configuré. Tu recevras ici chaque vidéo à valider. "
                                         "Envoie /test pour recevoir une vidéo d'essai.")
    elif cmd.name == "test":
        client.send_message(cmd.chat_id, "Je génère une vidéo d'essai, environ 20 secondes…")
        background.add_task(send_test_review, cmd.chat_id)


def handle_decision(d: Decision) -> None:
    client = telegram()
    chat_id = config.get("TELEGRAM_CHAT_ID")
    if client is None or not chat_id:
        return
    try:
        review = store.decide(d.review_id, approved=(d.action == APPROVE))
        label = "✅ Validée" if review.status == "approved" else "❌ Jetée"
        log.info("revue %s : %s", review.id, review.status)
        # Étape suivante (à venir) : une vidéo validée part dans la file de publication.
    except AlreadyDecided as exc:
        label = f"Déjà traitée ({exc})"
    except KeyError:
        label = "Vidéo inconnue (serveur redémarré ?)"
    client.confirm(chat_id, d, label)


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    token = config.get("TELEGRAM_BOT_TOKEN")
    if not token or not secret_matches(token, x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403)
    update = await request.json()
    action = parse_update(update, config.get("TELEGRAM_CHAT_ID"))
    try:
        if isinstance(action, Command):
            handle_command(action, background)
        elif isinstance(action, Decision):
            handle_decision(action)
    except TelegramError as exc:
        log.error("réponse Telegram impossible : %s", exc)
    return {"ok": True}   # toujours 200, sinon Telegram renvoie la même mise à jour en boucle
