"""Voix ElevenLabs (choisie le 26/11), avec Gemini en secours.

Activée dès que ELEVENLABS_API_KEY et ELEVENLABS_VOICE_ID sont présents dans Railway.
Modèle par défaut : eleven_multilingual_v2 (ELEVENLABS_MODEL pour changer).
Si ElevenLabs échoue (quota, panne), la voix de secours prend le relais pour ne pas perdre la vidéo.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import httpx

from . import config

log = logging.getLogger(__name__)
API = "https://api.elevenlabs.io/v1/text-to-speech"


class ElevenLabsError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.get("ELEVENLABS_API_KEY") and config.get("ELEVENLABS_VOICE_ID"))


class ElevenLabsVoice:
    def __init__(self, api_key: str | None = None, voice_id: str | None = None,
                 http: httpx.Client | None = None, fallback=None):
        self._key = api_key or config.get("ELEVENLABS_API_KEY")
        self.voice_id = voice_id or config.get("ELEVENLABS_VOICE_ID")
        if not self._key or not self.voice_id:
            raise ElevenLabsError("ELEVENLABS_API_KEY ou ELEVENLABS_VOICE_ID manquante")
        self.model = config.get("ELEVENLABS_MODEL") or "eleven_multilingual_v2"
        self._http = http or httpx.Client(timeout=httpx.Timeout(20.0, read=180.0))
        self.fallback = fallback
        self.backoff = 5.0

    def _tts(self, text: str, out_mp3: Path) -> Path:
        body = {"text": text, "model_id": self.model,
                "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.35,
                                   "use_speaker_boost": True}}
        last = "inconnue"
        for attempt in range(3):
            try:
                r = self._http.post(f"{API}/{self.voice_id}", params={"output_format": "mp3_44100_128"},
                                    json=body, headers={"xi-api-key": self._key, "accept": "audio/mpeg"})
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            else:
                if r.status_code == 200 and r.content:
                    out_mp3.write_bytes(r.content)
                    return out_mp3
                last = f"HTTP {r.status_code}"
                if r.status_code in (400, 401, 402, 403, 404, 422):      # clé, quota ou voix : inutile d'insister
                    break
            time.sleep(self.backoff * (attempt + 1))
        raise ElevenLabsError(f"ElevenLabs indisponible ({last})")

    def speak(self, text: str, out_wav: Path, voice: str | None = None) -> Path:
        out_wav = Path(out_wav)
        try:
            mp3 = self._tts(text, out_wav.with_suffix(".mp3"))
        except ElevenLabsError as exc:
            if self.fallback is None:
                raise
            log.warning("%s : voix de secours utilisée", exc)
            return self.fallback.speak(text, out_wav)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1", "-ar", "48000",
                        "-c:a", "pcm_s16le", str(out_wav)], check=True, timeout=120)
        return out_wav
