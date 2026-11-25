"""Chaîne complète d'une vidéo « Et si demain… » : scénario -> voix -> rendu -> contrôle qualité."""
from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .images import make_decors
from .qa import QAReport, run_qa
from .render import SceneAudio, render_video, scenes_with_outro, wav_duration
from .scriptgen import write_story

log = logging.getLogger(__name__)

TARGET_MAX_S = 85.0  # voix totale visée au maximum (la vidéo finale doit rester sous 90 s)
TARGET_MIN_S = 60.0  # en dessous, on ralentit légèrement (la vidéo doit dépasser 61 s)
MAX_SPEEDUP = 1.22   # au-delà, la voix sonnerait pressée : on réécrit plus court
MAX_SLOWDOWN = 0.92


class StoryTooLong(RuntimeError):
    pass


@dataclass
class StoryResult:
    storyboard: dict
    video: Path
    qa: QAReport
    tempo: float = 1.0
    decors: int = 0
    cost_usd: float = 0.0


class FakeVoice:
    """Voix factice pour les tests (pas d'appel réseau) : un son continu, durée ~ 2,6 mots/s."""

    def speak(self, text: str, out_wav: Path) -> Path:
        seconds = max(1.0, len(text.split()) / 2.6)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={seconds:.2f}:amplitude=0.2",
                        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out_wav)], check=True, timeout=60)
        return out_wav


def tempo_for(total: float) -> float:
    """Facteur de débit pour ramener la voix dans [60 s, 85 s] ; StoryTooLong si l'écart est trop grand."""
    if total > TARGET_MAX_S:
        factor = total / TARGET_MAX_S
        if factor > MAX_SPEEDUP:
            raise StoryTooLong(f"voix de {total:.0f} s, trop longue même accélérée")
        return factor
    if total < TARGET_MIN_S:
        return max(MAX_SLOWDOWN, total / TARGET_MIN_S)
    return 1.0


def change_tempo(wav: Path, factor: float) -> Path:
    """Accélère (>1) ou ralentit (<1) une voix sans changer sa hauteur."""
    out = wav.with_name(wav.stem + "_t.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-af", f"atempo={factor:.4f}",
                    "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out)], check=True, timeout=120)
    return out


def voice_scenes(sb: dict, voice, workdir: Path) -> tuple[list[SceneAudio], float]:
    wavs = []
    for i, scene in enumerate(scenes_with_outro(sb), start=1):
        wavs.append(Path(voice.speak(scene["narration"], workdir / f"scene_{i:02d}.wav")))
    total = sum(wav_duration(w) for w in wavs)
    factor = tempo_for(total)
    if abs(factor - 1.0) > 0.005:
        log.info("voix de %.1f s : débit ajusté x%.2f", total, factor)
        wavs = [change_tempo(w, factor) for w in wavs]
    return [SceneAudio(wav=w, duration=wav_duration(w)) for w in wavs], factor


def make_story(llm, voice, subject: str | None = None, avoid: list[str] | None = None,
               storyboard: dict | None = None, out_dir: Path | None = None, decor=None) -> StoryResult:
    """Produit une vidéo prête à valider. `storyboard` permet de sauter l'étape LLM (tests, reprise).
    `decor` : générateur de décors IA (images.FalDecor) ou None pour les décors dessinés en code."""
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="cm-story-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    sb = storyboard or write_story(llm, subject, avoid)
    log.info("scénario prêt : %s", sb["title"])
    audios, tempo = voice_scenes(sb, voice, out_dir)
    decors = make_decors(scenes_with_outro(sb), decor, out_dir)
    video = render_video(sb, audios, out_dir / f"{sb['story_id']}.mp4", out_dir, decors=decors)
    report = run_qa(video, "story")
    log.info("contrôle qualité : %s", report.summary())
    cost = getattr(decor, "generated", 0) * getattr(decor, "unit_price", 0.0)
    return StoryResult(storyboard=sb, video=video, qa=report, tempo=tempo, decors=len(decors), cost_usd=cost)
