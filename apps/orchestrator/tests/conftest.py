"""Fabrique de vidéos de test (générées par ffmpeg, aucune vidéo réelle dans le repo)."""
import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("ffmpeg") is None:  # pragma: no cover
    pytest.skip("ffmpeg requis pour les tests vidéo", allow_module_level=True)


def make_video(out: Path, *, w=1080, h=1920, seconds=20, audio=True, black_start=0.0,
               silence_from=None, freeze_from=None, loud=True) -> Path:
    """Vidéo synthétique : mire animée + son normalisé, avec défauts injectables."""
    vf = []
    if freeze_from is not None:   # image figée à partir de freeze_from
        vf.append(f"trim=end={freeze_from},tpad=stop_mode=clone:stop_duration={seconds - freeze_from}")
    if black_start:
        vf.append(f"drawbox=t=fill:c=black:enable='lt(t\\,{black_start})'")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={seconds}:amplitude=0.3"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if audio:
        af = ["loudnorm=I=-14:TP=-1.5:LRA=11"] if loud else ["volume=-40dB"]
        if silence_from is not None:
            af.append(f"volume=enable='gte(t\\,{silence_from})':volume=0")
        cmd += ["-af", ",".join(af), "-ar", "48000", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-t", str(seconds), str(out)]
    subprocess.run(cmd, check=True)
    return out


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("videos")
    return {
        "good": make_video(d / "good.mp4"),
        "horizontal": make_video(d / "horizontal.mp4", w=1920, h=1080),
        "no_audio": make_video(d / "no_audio.mp4", audio=False),
        "black_start": make_video(d / "black_start.mp4", black_start=1.5),
        "silence": make_video(d / "silence.mp4", silence_from=15),
        "quiet": make_video(d / "quiet.mp4", loud=False),
        "too_short": make_video(d / "short.mp4", seconds=8),
        "freeze": make_video(d / "freeze.mp4", freeze_from=10),
    }
