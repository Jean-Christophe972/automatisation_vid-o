"""Lecture de la configuration depuis les variables d'environnement (Railway).

Les secrets ne sont jamais écrits dans le repo ni renvoyés par l'API :
`status()` indique seulement si chaque variable est définie.
"""
from __future__ import annotations

import os

# Variables attendues, par étape du projet. "required" = le service refuse de traiter des jobs sans elles.
VARIABLES = {
    "GEMINI_API_KEY": {"required": True, "usage": "choix des moments (clipper) + voix (stories)"},
    "TELEGRAM_BOT_TOKEN": {"required": False, "usage": "validation des vidéos"},
    "TELEGRAM_CHAT_ID": {"required": False, "usage": "destinataire des aperçus"},
    "FAL_KEY": {"required": False, "usage": "décors dessinés par IA (stories) ; sans elle, décors dessinés en code"},
    "ELEVENLABS_API_KEY": {"required": False, "usage": "voix ElevenLabs (sinon voix Gemini)"},
    "ELEVENLABS_VOICE_ID": {"required": False, "usage": "identifiant de la voix ElevenLabs choisie"},
    "GROQ_API_KEY": {"required": False, "usage": "transcription Whisper"},
    "OPENROUTER_API_KEY": {"required": False, "usage": "LLM open source (scénarios)"},
    "DATABASE_URL": {"required": False, "usage": "Postgres Railway"},
    "REDIS_URL": {"required": False, "usage": "file de tâches"},
}


def get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def status() -> dict:
    return {name: {"set": bool(get(name)), **meta} for name, meta in VARIABLES.items()}


def missing_required() -> list[str]:
    return [n for n, m in VARIABLES.items() if m["required"] and not get(n)]
