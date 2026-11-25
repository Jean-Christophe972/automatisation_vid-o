"""Client minimal de l'API Gemini : texte JSON (scénarios) et voix (TTS).

Les noms de modèles et la voix se règlent par variables d'environnement, pour
changer de modèle sans toucher au code. En cas d'échec du modèle de voix
principal, on essaie le modèle de secours.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import httpx

from . import config

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(RuntimeError):
    pass


def text_model() -> str:
    return config.get("GEMINI_TEXT_MODEL") or "gemini-3.5-flash"


def tts_models() -> list[str]:
    primary = config.get("GEMINI_TTS_MODEL") or "gemini-3.8-flash-tts"
    fallback = "gemini-2.5-flash-preview-tts"
    return [primary] if primary == fallback else [primary, fallback]


def tts_voice() -> str:
    return config.get("GEMINI_TTS_VOICE") or "Charon"


# Pas de consigne de style ajoutée au texte : le modèle de voix la lisait parfois à voix haute
# (constaté le 26/11, « des parties du prompt » entendues dans la vidéo). On n'envoie que le texte à dire.


class GeminiClient:
    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self._key = api_key or config.get("GEMINI_API_KEY")
        if not self._key:
            raise GeminiError("GEMINI_API_KEY manquante")
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, read=180.0))
        self.backoff = 20.0

    def _post(self, model: str, body: dict, retries: int = 3) -> dict:
        """Appel avec nouvelles tentatives espacées si Gemini répond « trop de requêtes » (offre gratuite)."""
        for attempt in range(retries + 1):
            try:
                r = self._http.post(f"{API}/{model}:generateContent", json=body,
                                    headers={"x-goog-api-key": self._key})
            except httpx.HTTPError as exc:
                if attempt < retries:
                    time.sleep(self.backoff * (attempt + 1))
                    continue
                raise GeminiError(f"{model} : erreur réseau ({type(exc).__name__})") from exc
            if r.status_code in (429, 500, 503) and attempt < retries:
                log.warning("%s : HTTP %s, nouvelle tentative", model, r.status_code)
                time.sleep(self.backoff * (attempt + 1))
                continue
            break
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            msg = (data.get("error") or {}).get("message", f"HTTP {r.status_code}")
            raise GeminiError(f"{model} : {msg[:300]}")
        return data

    @staticmethod
    def _parts(data: dict) -> list[dict]:
        try:
            return data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or "réponse vide"
            raise GeminiError(f"réponse inexploitable ({reason})") from exc

    def generate_json(self, prompt: str, temperature: float = 1.0) -> dict:
        data = self._post(text_model(), {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        })
        text = "".join(p.get("text", "") for p in self._parts(data)).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiError(f"JSON invalide renvoyé par le modèle : {exc}") from exc

    def speak(self, text: str, out_wav: Path) -> Path:
        """Synthèse vocale d'un passage ; écrit un WAV 48 kHz mono."""
        last: Exception | None = None
        for model in tts_models():
            try:
                data = self._post(model, {
                    "contents": [{"role": "user", "parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": tts_voice()}}},
                    },
                })
                inline = next((p["inlineData"] for p in self._parts(data) if "inlineData" in p), None)
                if not inline:
                    raise GeminiError(f"{model} : aucune piste audio dans la réponse")
                return _write_audio(base64.b64decode(inline["data"]), inline.get("mimeType", ""), out_wav)
            except GeminiError as exc:
                log.warning("voix : %s", exc)
                last = exc
        raise GeminiError(f"aucun modèle de voix n'a répondu ({last})")


def _write_audio(raw: bytes, mime: str, out_wav: Path) -> Path:
    out_wav = Path(out_wav)
    m = re.search(r"rate=(\d+)", mime)
    with tempfile.TemporaryDirectory() as tmp:
        if "L16" in mime or "pcm" in mime.lower():
            src = Path(tmp) / "raw.wav"
            with wave.open(str(src), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(int(m.group(1)) if m else 24000)
                w.writeframes(raw)
        else:
            src = Path(tmp) / "raw.audio"
            src.write_bytes(raw)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-ac", "1", "-ar", "48000", str(out_wav)], check=True, timeout=120)
    return out_wav
