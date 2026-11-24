"""Vidéo d'essai générée sur le serveur, pour tester le circuit de validation de bout en bout."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def make_test_video(seconds: int = 16) -> Path:
    out = Path(tempfile.mkdtemp(prefix="cm-test-")) / "video_essai.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=720x1280:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={seconds}:amplitude=0.3",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "96k",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-t", str(seconds), str(out),
    ], check=True, timeout=120)
    return out
