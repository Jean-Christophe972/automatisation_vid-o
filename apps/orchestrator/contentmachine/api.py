"""Service orchestrateur (déployé sur Railway).

v0.2 : état du service + circuit de validation Telegram.
Commandes du bot (uniquement depuis TELEGRAM_CHAT_ID) :
  /start  vérifie la configuration
  /test   envoie une vidéo d'essai passée au contrôle qualité, avec les boutons
  /histoire [sujet]  histoire « Et si demain… » en 2 parties animées (style dessin animé, v0.5)
  /voix   le même extrait lu par plusieurs voix, pour choisir
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
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


_recent_subjects: list[str] = []   # titres déjà produits (en mémoire ; Postgres à venir)


def send_story_review(chat_id: int, subject: str | None) -> None:
    from .gemini import GeminiClient, GeminiError
    from .images import DecorError, FalDecor
    from .pipeline import StoryTooLong, make_story
    from .scriptgen import caption as story_caption
    from .storyboard import StoryboardError
    from .render import RenderError

    client = telegram()
    if client is None:
        return
    try:
        gem = GeminiClient()
        decor = None
        if config.get("FAL_KEY"):
            try:
                decor = FalDecor()
            except DecorError as exc:
                log.warning("décors IA désactivés : %s", exc)
        result = None
        for _ in range(2):                       # une réécriture si la voix reste trop longue
            try:
                result = make_story(gem, gem, subject=subject, avoid=_recent_subjects, decor=decor)
                break
            except StoryTooLong:
                continue
        if result is None:
            raise StoryTooLong("histoire trop longue deux fois de suite")
        sb = result.storyboard
        _recent_subjects.append(sb["title"])
        if not result.qa.passed:
            client.send_message(chat_id, f"⚠️ Vidéo « {sb['title']} » écartée par le contrôle qualité : "
                                         f"{result.qa.summary()}. Renvoie /histoire pour réessayer.")
            return
        review = store.create("stories", str(result.video), story_caption(sb))
        decors = (f"🎨 Décors IA : {result.decors}/{len(sb['scenes']) + 1} scènes, ~{result.cost_usd:.3f} $"
                  if decor else "🎨 Décors dessinés en code (ajoute FAL_KEY dans Railway pour les décors IA)")
        tempo = f"\n🎙️ Débit de la voix ajusté x{result.tempo:.2f}" if abs(result.tempo - 1) > 0.005 else ""
        text = (f"📖 {sb['title']}\n\n{story_caption(sb)}\n\n✔️ Contrôle qualité : {result.qa.summary()}\n"
                f"{decors}{tempo}")
        client.send_for_review(chat_id, result.video, review.id, text)
    except (GeminiError, StoryboardError, StoryTooLong, RenderError, QAError, TelegramError, OSError) as exc:
        log.error("histoire impossible : %s", exc)
        try:
            client.send_message(chat_id, f"⚠️ Histoire impossible : {str(exc)[:500]}")
        except TelegramError:
            pass


VOICES = ["Charon", "Puck", "Fenrir", "Sadachbia"]
VOICE_SAMPLE = ("Et si demain, chaque mensonge te rapportait cent euros ? Le premier jour, tu testes. "
                "Tu dis à ton patron que tu adores ses blagues… et ton téléphone vibre. Cent euros. "
                "Tu te figes. Et là, tu comprends que ta vie vient de changer.")
_busy = threading.Lock()


def send_voice_samples(chat_id: int) -> None:
    """/voix : le même texte lu par plusieurs voix Gemini, pour choisir à l'oreille."""
    from .gemini import GeminiClient, GeminiError

    client = telegram()
    if client is None:
        return
    try:
        gem = GeminiClient()
        with tempfile.TemporaryDirectory() as tmp:
            for name in VOICES:
                wav = gem.speak(VOICE_SAMPLE, Path(tmp) / f"{name}.wav", voice=name)
                ogg = Path(tmp) / f"{name}.ogg"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus",
                                "-b:a", "48k", str(ogg)], check=True, timeout=120)
                client.send_voice(chat_id, ogg, f"🎙️ Voix {name}")
        client.send_message(chat_id, "Pour choisir une voix : dans Railway, mets son nom dans la variable "
                                     f"GEMINI_TTS_VOICE (actuelle : {config.get('GEMINI_TTS_VOICE') or 'Charon'}).")
    except (GeminiError, TelegramError, OSError, subprocess.SubprocessError) as exc:
        log.error("voix impossibles : %s", exc)
        try:
            client.send_message(chat_id, f"⚠️ Extraits de voix impossibles : {str(exc)[:400]}")
        except TelegramError:
            pass


def send_cartoon_review(chat_id: int, subject: str | None) -> None:
    """/histoire : histoire en 2 parties, style dessin animé, chaque partie envoyée pour validation."""
    from .cartoon import make_two_parts
    from .fal import FalClient, FalError
    from .gemini import GeminiClient, GeminiError
    from .pipeline import StoryTooLong
    from .story2 import StoryError, caption as part_caption

    client = telegram()
    if client is None:
        return
    if not _busy.acquire(blocking=False):
        client.send_message(chat_id, "⏳ Une histoire est déjà en cours de fabrication, attends qu'elle arrive.")
        return
    try:
        gem, fal = GeminiClient(), FalClient()
        result = None
        for _ in range(2):                       # une réécriture si une voix reste trop longue
            try:
                # dossier conservé : les vidéos validées serviront à la publication
                tmp = tempfile.mkdtemp(prefix="cm-story-")
                result = make_two_parts(gem, gem, fal, subject, avoid=_recent_subjects, out_dir=Path(tmp))
                st = result.story
                _recent_subjects.append(st["title"])
                client.send_message(chat_id, f"📖 {st['title']}\n\n— PARTIE 1 —\n{st['part1']}\n\n"
                                             f"— PARTIE 2 —\n{st['part2']}"[:4000])
                for pr in result.parts:
                    info = (f"✔️ Contrôle qualité : {pr.qa.summary()}\n🎬 {pr.animated}/{pr.shots} plans animés"
                            f" · voix {pr.voice_s:.0f} s" + (f" (débit x{pr.tempo:.2f})" if abs(pr.tempo - 1) > 0.005
                                                             else ""))
                    if not pr.qa.passed:
                        client.send_message(chat_id, f"⚠️ Partie {pr.part} écartée par le contrôle qualité : "
                                                     f"{pr.qa.summary()}")
                        continue
                    cap = part_caption(st, pr.part)
                    review = store.create("stories", str(pr.video), cap)
                    client.send_for_review(chat_id, pr.video, review.id, f"{cap}\n\n{info}")
                client.send_message(chat_id, f"💶 Coût fal de cette histoire : {result.cost_usd:.2f} $")
                break
            except StoryTooLong:
                continue
        if result is None:
            raise StoryTooLong("voix trop longue deux fois de suite")
    except (GeminiError, StoryError, StoryTooLong, FalError, QAError, TelegramError, OSError,
            RuntimeError, subprocess.SubprocessError) as exc:
        log.error("histoire impossible : %s", exc)
        try:
            client.send_message(chat_id, f"⚠️ Histoire impossible : {str(exc)[:500]}")
        except TelegramError:
            pass
    finally:
        _busy.release()


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
        client.send_message(cmd.chat_id, "✅ Bot configuré. Tu recevras ici chaque vidéo à valider.\n"
                                         "/histoire : une histoire « Et si demain… » en 2 parties animées\n"
                                         "/histoire et si tu gagnais au loto : sur ton sujet\n"
                                         "/voix : écouter les voix disponibles\n"
                                         "/test : vidéo d'essai technique")
    elif cmd.name == "test":
        client.send_message(cmd.chat_id, "Je génère une vidéo d'essai, environ 20 secondes…")
        background.add_task(send_test_review, cmd.chat_id)
    elif cmd.name == "histoire":
        subject = cmd.args.strip() or None
        if not config.get("FAL_KEY"):
            client.send_message(cmd.chat_id, "⚠️ FAL_KEY manquante dans Railway : impossible de dessiner et "
                                             "d'animer les scènes.")
            return
        client.send_message(cmd.chat_id, "✍️ J'écris l'histoire" + (f" « {subject} »" if subject else "")
                            + " en 2 parties, puis voix, dessins et animation : compte 15 à 30 minutes…")
        background.add_task(send_cartoon_review, cmd.chat_id, subject)
    elif cmd.name == "voix":
        client.send_message(cmd.chat_id, "🎙️ J'enregistre le même texte avec 4 voix, une minute…")
        background.add_task(send_voice_samples, cmd.chat_id)


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
