"""Contrôle qualité automatique d'une vidéo avant qu'elle soit proposée à la validation.

Chaque vidéo produite (clip ou histoire) passe par `run_qa`. Une vidéo qui échoue
n'est jamais envoyée sur Telegram : elle est refaite ou jetée.

Vérifications (toutes via ffprobe / ffmpeg, aucune dépendance Python lourde) :
- format vertical 9:16, résolution minimale
- durée dans les bornes du profil (clip 15-60 s, histoire 61-90 s)
- piste audio présente
- volume perçu (LUFS intégrés) et pic maximal, aux normes TikTok / Shorts
- pas d'écran noir au début, peu de noir au total
- pas de silence long
- pas d'image figée longue
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    name: str
    min_duration: float
    max_duration: float
    min_height: int = 1280
    lufs_min: float = -18.0
    lufs_max: float = -10.0
    true_peak_max: float = -0.5
    max_black_start: float = 0.3      # secondes de noir tolérées au tout début
    max_black_ratio: float = 0.05     # part de la vidéo en noir
    max_silence: float = 2.0          # plus long silence toléré (s)
    max_freeze: float = 2.0           # plus longue image figée tolérée (s)


PROFILES = {
    "clip": Profile("clip", min_duration=15, max_duration=60),
    "story": Profile("story", min_duration=61, max_duration=90),
}


@dataclass
class Check:
    name: str
    ok: bool
    value: object
    expected: str


@dataclass
class QAReport:
    path: str
    profile: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict:
        return {"path": self.path, "profile": self.profile, "passed": self.passed,
                "checks": [asdict(c) for c in self.checks]}

    def summary(self) -> str:
        if self.passed:
            return f"OK ({len(self.checks)} contrôles)"
        return "; ".join(f"{c.name}: {c.value} (attendu {c.expected})" for c in self.failures)


class QAError(RuntimeError):
    """La vidéo n'a pas pu être analysée (fichier illisible, ffmpeg absent...)."""


def _require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise QAError(f"{tool} introuvable dans le PATH")


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise QAError(f"délai dépassé : {' '.join(cmd[:3])}…") from exc


def probe(path: Path) -> dict:
    res = _run(["ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)])
    if res.returncode != 0:
        raise QAError(f"ffprobe a échoué : {res.stderr.strip()[:300]}")
    data = json.loads(res.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise QAError("aucune piste vidéo")
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": float(data.get("format", {}).get("duration") or video.get("duration") or 0),
        "has_audio": audio is not None,
    }


_FLOAT = r"(-?\d+(?:\.\d+)?|-?inf)"


def _ffmpeg_filters(path: Path, has_audio: bool) -> str:
    """Un seul passage ffmpeg pour tous les filtres d'analyse ; renvoie le journal."""
    vf = "blackdetect=d=0.1:pix_th=0.10,freezedetect=n=-60dB:d=0.5"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", vf]
    if has_audio:
        cmd += ["-af", "silencedetect=n=-50dB:d=0.5,ebur128=peak=true"]
    cmd += ["-f", "null", "-"]
    res = _run(cmd)
    if res.returncode != 0:
        raise QAError(f"analyse ffmpeg en échec : {res.stderr.strip()[-300:]}")
    return res.stderr


def _black_segments(log: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in
            re.findall(rf"black_start:{_FLOAT} black_end:{_FLOAT}", log)]


def _durations(log: str, key: str) -> list[float]:
    return [float(x) for x in re.findall(rf"{key}_duration: ?{_FLOAT}", log)]


def _open_ended(log: str, start_key: str, end_key: str, duration: float) -> list[float]:
    """Segments commencés mais jamais terminés (ils courent jusqu'à la fin de la vidéo)."""
    starts = [float(x) for x in re.findall(rf"{start_key}: ?{_FLOAT}", log)]
    ends = re.findall(rf"{end_key}: ?{_FLOAT}", log)
    if len(starts) > len(ends):
        return [max(0.0, duration - starts[-1])]
    return []


def _loudness(log: str) -> tuple[float | None, float | None]:
    summary = log.split("Summary:")[-1] if "Summary:" in log else ""
    i = re.search(rf"I:\s+{_FLOAT} LUFS", summary)
    tp = re.search(rf"Peak:\s+{_FLOAT} dBFS", summary)
    return (float(i.group(1)) if i else None, float(tp.group(1)) if tp else None)


def run_qa(path: str | Path, profile: str = "clip") -> QAReport:
    _require_tools()
    if profile not in PROFILES:
        raise ValueError(f"profil inconnu : {profile}")
    p = PROFILES[profile]
    path = Path(path)
    if not path.is_file():
        raise QAError(f"fichier introuvable : {path}")

    info = probe(path)
    report = QAReport(path=str(path), profile=profile)
    add = report.checks.append

    w, h, d = info["width"], info["height"], info["duration"]
    add(Check("format_vertical", abs(w / h - 9 / 16) < 0.01, f"{w}x{h}", "9:16"))
    add(Check("resolution", h >= p.min_height, h, f">= {p.min_height}px de haut"))
    add(Check("duree", p.min_duration <= d <= p.max_duration, round(d, 2),
              f"{p.min_duration}-{p.max_duration} s"))
    add(Check("piste_audio", info["has_audio"], info["has_audio"], "présente"))

    log = _ffmpeg_filters(path, info["has_audio"])

    blacks = _black_segments(log)
    black_start = next((b - a for a, b in blacks if a <= 0.05), 0.0)
    black_total = sum(b - a for a, b in blacks)
    add(Check("noir_au_debut", black_start <= p.max_black_start, round(black_start, 2),
              f"<= {p.max_black_start} s"))
    add(Check("noir_total", d > 0 and black_total / d <= p.max_black_ratio,
              f"{round(100 * black_total / d, 1) if d else 0}%", f"<= {int(p.max_black_ratio * 100)}%"))

    freezes = _durations(log, "lavfi.freezedetect.freeze") + _open_ended(
        log, "lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_end", d)
    longest_freeze = max(freezes, default=0.0)
    add(Check("image_figee", longest_freeze <= p.max_freeze, round(longest_freeze, 2),
              f"<= {p.max_freeze} s"))

    if info["has_audio"]:
        silences = _durations(log, "silence") + _open_ended(log, "silence_start", "silence_end", d)
        longest_silence = max(silences, default=0.0)
        add(Check("silence", longest_silence <= p.max_silence, round(longest_silence, 2),
                  f"<= {p.max_silence} s"))
        lufs, peak = _loudness(log)
        add(Check("volume_lufs", lufs is not None and p.lufs_min <= lufs <= p.lufs_max, lufs,
                  f"{p.lufs_min} à {p.lufs_max} LUFS"))
        add(Check("pic_audio", peak is not None and peak <= p.true_peak_max, peak,
                  f"<= {p.true_peak_max} dBFS"))
    return report


if __name__ == "__main__":  # usage : python -m contentmachine.qa video.mp4 [clip|story]
    import sys

    r = run_qa(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "clip")
    print(json.dumps(r.to_dict(), indent=2, ensure_ascii=False))
    sys.exit(0 if r.passed else 1)
