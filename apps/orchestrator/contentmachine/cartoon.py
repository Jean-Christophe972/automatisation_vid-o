"""Pipeline « dessin animé » v0.5 (26/11) : histoire en 2 parties, style C, scènes animées par IA.

Pour chaque partie :
1. voix lue d'un seul bloc (intonation naturelle), débit ajusté pour tenir 61-90 s ;
2. coupes entre plans calées sur les pauses de la voix ;
3. une image par plan (FLUX.2 klein, mascotte et style fixes), animée par LTX-2.3 ;
   si une animation échoue ou dépasse le budget, le plan garde son image avec un zoom lent ;
4. assemblage, sous-titres mot à mot, bandeaux « PARTIE 1/2 », contrôle qualité.
"""
from __future__ import annotations

import logging
import re
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config
from .fal import FalError, frames_for, video_cost
from .pipeline import StoryTooLong, change_tempo, tempo_for
from .qa import QAReport, run_qa
from .render import FONT_PATHS, Word, chunk_words, time_words, wav_duration
from .story2 import image_prompt, motion_prompt, plan_scenes, write_story

log = logging.getLogger(__name__)

W, H, FPS = 720, 1280, 24
END_HOLD_S = 1.0
YELLOW, WHITE, BLACK = (255, 200, 0), (255, 255, 255), (0, 0, 0)


def max_cost_per_part() -> float:
    try:
        return float(config.get("MAX_COST_PER_PART") or 1.5)
    except ValueError:
        return 1.5


def animation_enabled() -> bool:
    return (config.get("ANIMATION") or "on").lower() not in ("off", "0", "non", "false")


@dataclass
class Shot:
    text: str
    image_desc: str
    motion: str
    start: float = 0.0
    end: float = 0.0
    image: Path | None = None
    image_url: str | None = None
    clip: Path | None = None

    @property
    def dur(self) -> float:
        return self.end - self.start


@dataclass
class PartResult:
    part: int
    video: Path
    qa: QAReport
    tempo: float
    shots: int
    animated: int
    voice_s: float


@dataclass
class StoryParts:
    story: dict
    parts: list[PartResult] = field(default_factory=list)
    cost_usd: float = 0.0


# --- voix et calage ------------------------------------------------------------------------

def silences(wav: Path, noise_db: int = -32, min_s: float = 0.12) -> list[tuple[float, float]]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav), "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends))


def time_shots(texts: list[str], total: float, sil: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Répartit la durée de la voix entre les plans (au prorata du texte), puis aligne chaque coupe
    sur la pause la plus proche (à 0,7 s près) pour couper entre deux phrases, jamais au milieu d'un mot."""
    lead = sil[0][1] if sil and sil[0][0] <= 0.05 else 0.0
    tail = sil[-1][0] if sil and sil[-1][1] >= total - 0.05 else total
    speech = max(0.1, tail - lead)
    weights = [max(1, len(t)) for t in texts]
    cuts, acc = [], 0
    for w in weights[:-1]:
        acc += w
        guess = lead + speech * acc / sum(weights)
        mids = [(a + b) / 2 for a, b in sil if lead < (a + b) / 2 < tail]
        near = min(mids, key=lambda m: abs(m - guess), default=None)
        cut = near if near is not None and abs(near - guess) <= 0.7 else guess
        if cuts and cut <= cuts[-1] + 0.8:          # un plan dure au moins 0,8 s
            cut = max(guess, cuts[-1] + 0.8)
        cuts.append(cut)
    bounds = [0.0, *cuts, total]
    return list(zip(bounds[:-1], bounds[1:]))


def voice_part(text: str, voice, workdir: Path, part: int) -> tuple[Path, float]:
    wav = Path(voice.speak(text, workdir / f"voix_p{part}.wav"))
    factor = tempo_for(wav_duration(wav))
    if abs(factor - 1.0) > 0.005:
        wav = change_tempo(wav, factor)
    return wav, factor


# --- images et animation ---------------------------------------------------------------------

def make_images(shots: list[Shot], story: dict, fal, workdir: Path, tag: str) -> None:
    seed = zlib.crc32(story["title"].encode()) % 1_000_000     # même graine : style plus homogène

    def one(i_shot):
        i, shot = i_shot
        try:
            shot.image, shot.image_url = fal.image(image_prompt({"image": shot.image_desc}, story),
                                                   workdir / f"img_{tag}_{i:02d}.png", seed=seed)
        except FalError as exc:
            log.warning("image %s-%d impossible : %s", tag, i, exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, enumerate(shots)))
    ok = [s for s in shots if s.image]
    if not ok:
        raise FalError("aucune image n'a pu être générée")
    for i, s in enumerate(shots):                   # un plan sans image reprend celle du plan voisin
        if not s.image:
            src = next((shots[j] for j in list(range(i - 1, -1, -1)) + list(range(i + 1, len(shots)))
                        if shots[j].image), ok[0])
            s.image, s.image_url = src.image, src.image_url


def plan_animation(parts: list[list[Shot]]) -> list[tuple[int, int]]:
    """Choisit les plans à animer sans dépasser le budget par partie (les premiers plans d'abord)."""
    chosen = []
    if not animation_enabled():
        return chosen
    for p, shots in enumerate(parts):
        budget = max_cost_per_part()
        for i, s in enumerate(shots):
            c = video_cost(frames_for(s.dur))
            if s.image_url and c <= budget:
                chosen.append((p, i))
                budget -= c
    return chosen


def animate(parts: list[list[Shot]], fal, workdir: Path) -> int:
    jobs = {}
    for p, i in plan_animation(parts):
        s = parts[p][i]
        try:
            jobs[f"{p + 1}_{i:02d}"] = fal.submit_video(s.image_url, motion_prompt({"motion": s.motion}),
                                                        frames_for(s.dur))
        except FalError as exc:
            log.warning("animation %d-%d non soumise : %s", p + 1, i, exc)
    if not jobs:
        return 0
    done = fal.poll_videos(jobs, workdir)
    for key, path in done.items():
        p, i = key.split("_")
        parts[int(p) - 1][int(i)].clip = path
    return len(done)


# --- montage ----------------------------------------------------------------------------------

def _probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


FIT = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"


def shot_clip(shot: Shot, dur: float, out: Path, index: int) -> Path:
    """Vidéo muette du plan, exactement `dur` secondes, en 720x1280 à 24 i/s."""
    enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS)]
    if shot.clip and (length := _probe_duration(shot.clip)) > 0.5:
        if dur > length * 1.3:                      # plan long : aller-retour de l'animation
            chain = (f"[0:v]{FIT},fps={FPS},split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[c];"
                     f"[c]setpts=PTS*{max(1.0, dur / (2 * length)):.4f}")
        else:
            chain = f"[0:v]{FIT},fps={FPS},setpts=PTS*{max(1.0, dur / length):.4f}"
        vf = f"{chain},tpad=stop_mode=clone:stop_duration={dur:.3f},trim=duration={dur:.3f},setpts=PTS-STARTPTS[v]"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(shot.clip), "-filter_complex", vf, "-map", "[v]"]
    else:                                           # repli : image fixe avec zoom lent alterné
        n = max(1, round(dur * FPS))
        z = "min(zoom+0.0009,1.15)" if index % 2 == 0 else "if(eq(on,0),1.15,max(zoom-0.0009,1.0))"
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='{z}':d={n}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},"
              f"trim=duration={dur:.3f},setpts=PTS-STARTPTS")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(shot.image), "-vf", vf]
    subprocess.run([*cmd, "-t", f"{dur:.3f}", *enc, str(out)], check=True, timeout=600)
    return out


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if p and Path(p).is_file():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _wrap(d, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if d.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


class Overlay:
    """Textes dessinés par-dessus l'animation : hook, bandeau de partie, sous-titres mot à mot."""

    def __init__(self, part: int, hook: str, words: list[Word], total: float):
        self.part, self.hook, self.total = part, hook, total
        self.chunks = chunk_words(words, max_chars=24)
        self.f_cap, self.f_hook, self.f_tag = _font(54), _font(44), _font(30)

    def _box(self, d, lines, font, y, fill, ink, lh):
        pad = 18
        bw = max(d.textlength(l, font=font) for l in lines) + 2 * pad
        x0 = (W - bw) / 2
        d.rounded_rectangle([x0, y, x0 + bw, y + lh * len(lines) + pad], 18, fill=fill)
        for i, l in enumerate(lines):
            d.text((W / 2, y + pad / 2 + lh * i + lh / 2), l, font=font, anchor="mm", fill=ink)

    def draw(self, img: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(img)
        self._box(d, [f"PARTIE {self.part}"], self.f_tag, 70, BLACK, WHITE, 40)
        if self.part == 1 and t < 3.0:
            self._box(d, _wrap(d, self.hook, self.f_hook, 600), self.f_hook, 140, YELLOW, BLACK, 56)
        if self.part == 1 and t > self.total - 3.2:
            self._box(d, ["LA SUITE DANS", "LA PARTIE 2"], self.f_hook, 140, YELLOW, BLACK, 56)
        chunk = next((c for c in self.chunks if c[0].start <= t < c[-1].end + 0.25), None)
        if not chunk:
            return
        lines = _wrap(d, " ".join(w.text for w in chunk), self.f_cap, 640)
        y, idx = 1000, 0
        for line in lines:
            n = len(line.split())
            ws = chunk[idx: idx + n]
            idx += n
            x = (W - d.textlength(line, font=self.f_cap)) / 2
            for w in ws:
                active = w.start <= t < w.end
                d.text((x, y), w.text, font=self.f_cap, fill=YELLOW if active else WHITE,
                       stroke_width=6, stroke_fill=BLACK, anchor="ls")
                x += d.textlength(w.text + " ", font=self.f_cap)
            y += 66


def assemble(shots: list[Shot], wav: Path, overlay: Overlay, out: Path, workdir: Path, part: int) -> Path:
    clips = []
    for i, s in enumerate(shots):
        dur = s.dur + (END_HOLD_S if i == len(shots) - 1 else 0.0)
        clips.append(shot_clip(s, dur, workdir / f"plan_p{part}_{i:02d}.mp4", i))
    lst = workdir / f"plans_p{part}.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    silent = workdir / f"muet_p{part}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(silent)], check=True, timeout=300)
    total = shots[-1].end + END_HOLD_S
    n_frames = round(total * FPS)
    dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(silent), "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(wav),
                            "-af", "apad,loudnorm=I=-14:TP=-1.5:LRA=11", "-shortest",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)],
                           stdin=subprocess.PIPE)
    size = W * H * 3
    last = None
    try:
        for k in range(n_frames):
            raw = dec.stdout.read(size)
            if len(raw) == size:
                last = raw
            elif last is None:
                break
            img = Image.frombytes("RGB", (W, H), last)
            overlay.draw(img, k / FPS)
            enc.stdin.write(img.tobytes())
    finally:
        enc.stdin.close()
        dec.stdout.close()
        dec.wait(timeout=60)
        enc.wait(timeout=600)
    if enc.returncode != 0 or not out.is_file():
        raise RuntimeError("montage final impossible")
    return out


def part_words(shots: list[Shot]) -> list[Word]:
    words = []
    for s in shots:
        for w in time_words(s.text, max(0.3, s.dur - 0.1)):
            words.append(Word(w.text, w.start + s.start, w.end + s.start))
    return words


# --- orchestration ------------------------------------------------------------------------------

def make_two_parts(llm, voice, fal, subject: str | None = None, avoid: list[str] | None = None,
                   out_dir: Path | None = None, story: dict | None = None,
                   scenes: dict[int, list[dict]] | None = None) -> StoryParts:
    out_dir = Path(out_dir or "/tmp/cm-cartoon")
    out_dir.mkdir(parents=True, exist_ok=True)
    story = story or write_story(llm, subject, avoid)
    log.info("histoire : %s", story["title"])
    parts_shots, voices = [], []
    for part in (1, 2):
        plan = (scenes or {}).get(part) or plan_scenes(llm, story, part)
        wav, tempo = voice_part(story[f"part{part}"], voice, out_dir, part)
        total = wav_duration(wav)
        bounds = time_shots([s["text"] for s in plan], total, silences(wav))
        shots = [Shot(s["text"], s["image"], s["motion"], a, b) for s, (a, b) in zip(plan, bounds)]
        parts_shots.append(shots)
        voices.append((wav, tempo, total))
    for part, shots in enumerate(parts_shots, 1):
        make_images(shots, story, fal, out_dir, f"p{part}")
    animated = animate(parts_shots, fal, out_dir) if parts_shots else 0
    result = StoryParts(story=story)
    for part, (shots, (wav, tempo, total)) in enumerate(zip(parts_shots, voices), 1):
        overlay = Overlay(part, story["hook"], part_words(shots), total + END_HOLD_S)
        video = assemble(shots, wav, overlay, out_dir / f"{_slug(story['title'])}_p{part}.mp4", out_dir, part)
        report = run_qa(video, "story")
        log.info("partie %d : %s", part, report.summary())
        result.parts.append(PartResult(part, video, report, tempo, len(shots),
                                       sum(1 for s in shots if s.clip), total))
    result.cost_usd = getattr(fal, "spent", 0.0)
    log.info("coût fal : %.3f $ (%d plans animés)", result.cost_usd, animated)
    return result


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:50] or "histoire"


__all__ = ["make_two_parts", "StoryParts", "PartResult", "StoryTooLong", "time_shots", "Overlay"]
