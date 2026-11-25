"""Chaîne complète d'une vidéo « Et si demain… » : scénario -> voix -> rendu -> contrôle qualité."""
from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .qa import QAReport, run_qa
from .render import SceneAudio, render_video, scenes_with_outro, wav_duration
from .scriptgen import write_story

log = logging.getLogger(__name__)

MAX_VOICE_S = 88.0   # au-delà, la vidéo dépasserait 90 s : on réécrit plus court


class StoryTooLong(RuntimeError):
    pass


@dataclass
class StoryResult:
    storyboard: dict
    video: Path
    qa: QAReport


class FakeVoice:
    """Voix factice pour les tests (pas d'appel réseau) : un son continu, durée ~ 2,6 mots/s."""

    def speak(self, text: str, out_wav: Path) -> Path:
        seconds = max(1.0, len(text.split()) / 2.6)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={seconds:.2f}:amplitude=0.2",
                        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out_wav)], check=True, timeout=60)
        return out_wav


def voice_scenes(sb: dict, voice, workdir: Path) -> list[SceneAudio]:
    audios = []
    for i, scene in enumerate(scenes_with_outro(sb), start=1):
        wav = voice.speak(scene["narration"], workdir / f"scene_{i:02d}.wav")
        audios.append(SceneAudio(wav=Path(wav), duration=wav_duration(Path(wav))))
    total = sum(a.duration for a in audios)
    if total > MAX_VOICE_S:
        raise StoryTooLong(f"voix de {total:.0f} s, trop longue")
    return audios


def make_story(llm, voice, subject: str | None = None, avoid: list[str] | None = None,
               storyboard: dict | None = None, out_dir: Path | None = None) -> StoryResult:
    """Produit une vidéo prête à valider. `storyboard` permet de sauter l'étape LLM (tests, reprise)."""
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="cm-story-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    sb = storyboard or write_story(llm, subject, avoid)
    log.info("scénario prêt : %s", sb["title"])
    audios = voice_scenes(sb, voice, out_dir)
    video = render_video(sb, audios, out_dir / f"{sb['story_id']}.mp4", out_dir)
    report = run_qa(video, "story")
    log.info("contrôle qualité : %s", report.summary())
    return StoryResult(storyboard=sb, video=video, qa=report)
