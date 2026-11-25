"""Rendu d'une vidéo stickman à partir d'un storyboard et des voix off par scène.

Choix techniques :
- Dessin en Pillow à 2x puis réduction (traits lisses), envoi des images brutes à ffmpeg.
- Le décor de chaque scène est dessiné une seule fois ; seuls les personnages,
  les accessoires animés et les sous-titres sont redessinés à chaque image.
- La durée de chaque scène vient de sa voix off : les sous-titres sont calés mot à mot.
- Zones sûres TikTok : rien d'important dans les 140 px du haut ni sous 1060 px.
"""
from __future__ import annotations

import math
import os
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS, SS = 720, 1280, 24, 2           # sortie 720x1280 à 24 i/s, dessin à 2x
GROUND = 1010
GAP_S = 0.15                               # silence entre deux scènes
END_HOLD_S = 0.8                           # image tenue à la fin

LIGHT = {"bg": (246, 241, 231), "deco": (214, 205, 189), "ink": (30, 30, 32), "soft": (120, 112, 100)}
DARK = {"bg": (27, 35, 64), "deco": (70, 82, 122), "ink": (240, 240, 245), "soft": (170, 176, 200)}
YELLOW, WHITE, BLACK = (255, 200, 0), (255, 255, 255), (0, 0, 0)

FONT_PATHS = [os.environ.get("FONT_PATH", ""), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if p and Path(p).is_file():
            return ImageFont.truetype(p, size * SS)
    return ImageFont.load_default(size * SS)


class RenderError(RuntimeError):
    pass


# --- audio ------------------------------------------------------------------

def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def concat_wavs(parts: list[tuple[Path, float]], out: Path, tail_s: float) -> Path:
    """Assemble les voix (WAV 48 kHz mono 16 bits) avec un silence après chacune."""
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(48000)
        for path, pause in parts:
            with wave.open(str(path), "rb") as src:
                if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1, 2, 48000):
                    raise RenderError(f"format audio inattendu : {path.name}")
                dst.writeframes(src.readframes(src.getnframes()))
            dst.writeframes(b"\x00\x00" * int(48000 * pause))
        dst.writeframes(b"\x00\x00" * int(48000 * tail_s))
    return out


# --- sous-titres --------------------------------------------------------------

@dataclass
class Word:
    text: str
    start: float
    end: float


def time_words(text: str, duration: float) -> list[Word]:
    """Répartit la durée de la voix sur les mots, au prorata de leur longueur (+ pauses de ponctuation)."""
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 2 + (4 if w[-1] in ".,;:!?…" else 0) for w in words]
    total, t, out = sum(weights), 0.05, []
    usable = max(0.1, duration - 0.1)
    for w, wt in zip(words, weights):
        d = usable * wt / total
        out.append(Word(w, t, t + d))
        t += d
    return out


def chunk_words(words: list[Word], max_chars: int = 26) -> list[list[Word]]:
    chunks, cur = [], []
    for w in words:
        if cur and (len(" ".join(x.text for x in cur + [w])) > max_chars or cur[-1].text[-1] in ".!?…"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


# --- géométrie du stickman ------------------------------------------------------

HEAD_R, TORSO, THIGH, SHIN, UPPER, FORE = 30, 112, 72, 72, 60, 56


def pose_angles(pose: str, t: float) -> dict:
    """Angles en degrés depuis la verticale basse, positifs vers l'avant (personnage tourné à droite)."""
    s = math.sin(2 * math.pi * 1.6 * t)
    bob = 3 * math.sin(2 * math.pi * 0.8 * t)
    a = {"fa": (12, 8), "ba": (-12, 8), "fl": (6, 0), "bl": (-6, 0), "lean": 0, "lift": 0, "bob": bob,
         "dx": 0.0, "mood": "neutral"}
    if pose == "walk":
        a.update(fl=(25 * s, 10), bl=(-25 * s, 10), fa=(-20 * s, 15), ba=(20 * s, 15), lift=-abs(s) * 4)
    elif pose == "run":
        s2 = math.sin(2 * math.pi * 2.6 * t)
        a.update(fl=(40 * s2, 35), bl=(-40 * s2, 35), fa=(-45 * s2, 80), ba=(45 * s2, 80), lean=12,
                 lift=-abs(s2) * 10)
    elif pose == "point":
        a.update(fa=(88, 0), ba=(-10, 10))
    elif pose == "sit":
        a.update(fl=(88, -88), bl=(80, -80), fa=(25, 35), ba=(15, 40))
        a["sit"] = True
    elif pose == "jump":
        j = abs(math.sin(2 * math.pi * 1.1 * t))
        a.update(lift=55 * j, fa=(165, -10), ba=(-165, 10), fl=(20, -40 * j), bl=(-20, -40 * j), mood="happy")
    elif pose == "scared":
        a.update(fa=(130, 45), ba=(-130, -45), lean=-8, dx=2.5 * math.sin(2 * math.pi * 7 * t), mood="scared")
    elif pose == "think":
        a.update(fa=(25, 140), ba=(10, 25), mood="neutral")
        a["tilt"] = 8
    elif pose == "wave":
        a.update(fa=(140, 25 + 30 * math.sin(2 * math.pi * 2.2 * t)), ba=(-8, 10), mood="happy")
    elif pose == "celebrate":
        j = abs(math.sin(2 * math.pi * 1.4 * t))
        a.update(fa=(160, -15), ba=(-160, 15), lift=25 * j, mood="happy")
    elif pose == "sad":
        a.update(fa=(4, 0), ba=(-4, 0), lean=10, bob=bob / 2, mood="sad")
    elif pose == "shrug":
        a.update(fa=(45, -105), ba=(-45, 105), mood="neutral")
    return a


def _seg(p, ang, length):
    r = math.radians(ang)
    return (p[0] + length * math.sin(r), p[1] + length * math.cos(r))


def draw_stickman(d: ImageDraw.ImageDraw, cx: float, pose: str, t: float, facing: str, color: tuple,
                  accessory: str, theme: dict, scale: float = 1.32) -> tuple[float, float]:
    """Dessine un personnage ; renvoie la position du haut de la tête (pour la bulle)."""
    a = pose_angles(pose, t)
    k = SS * scale
    sign = 1 if facing != "left" else -1
    lw = max(2, int(10 * k))
    ink = theme["ink"]

    def P(x, y):  # coordonnées locales (personnage tourné à droite) -> image
        return ((cx + a["dx"]) * SS + sign * x * k, y)

    leg_len = THIGH + SHIN
    hip_y = (GROUND - leg_len * 0.98 * scale - a["lift"] * scale + a["bob"] * scale) * SS
    if a.get("sit"):
        hip_y = (GROUND - SHIN * scale - 6 * scale) * SS
        seat_x0, seat_x1 = sorted([P(-30, 0)[0], P(40, 0)[0]])
        d.rectangle([seat_x0, hip_y + 4 * k, seat_x1, GROUND * SS], outline=theme["soft"], width=max(2, int(5 * k)))
    hip = (0, 0)
    lean = math.radians(a["lean"])
    neck = (TORSO * math.sin(lean), -TORSO * math.cos(lean))

    def local_to_img(p):
        x, y = P(p[0], 0)
        return (x, hip_y + p[1] * k)

    # jambes
    for hipa, knee in (a["bl"], a["fl"]):
        kn = _seg(hip, hipa, THIGH)
        ft = _seg(kn, hipa + knee, SHIN)
        d.line([local_to_img(hip), local_to_img(kn), local_to_img(ft)], fill=ink, width=lw, joint="curve")
    # torse
    d.line([local_to_img(hip), local_to_img(neck)], fill=ink, width=lw)
    if accessory == "tie":
        tx, ty = local_to_img((neck[0] * 0.9, neck[1] * 0.85))
        d.polygon([(tx - 7 * k, ty), (tx + 7 * k, ty), (tx, ty + 40 * k)], fill=(200, 40, 50))
    # bras
    shoulder = (neck[0] * 0.92, neck[1] * 0.92)
    for sh, el in (a["ba"], a["fa"]):
        elb = _seg(shoulder, sh, UPPER)
        hand = _seg(elb, sh + el, FORE)
        d.line([local_to_img(shoulder), local_to_img(elb), local_to_img(hand)], fill=ink, width=lw, joint="curve")
    # tête
    tilt = math.radians(a.get("tilt", 0) + (12 if a["mood"] == "sad" else 0))
    hc_local = (neck[0] + (HEAD_R + 4) * math.sin(lean + tilt), neck[1] - (HEAD_R + 4) * math.cos(lean + tilt))
    hx, hy = local_to_img(hc_local)
    r = HEAD_R * k
    d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=color, outline=ink, width=lw)
    # visage
    ex = hx + sign * 9 * k
    eye_r = 3.2 * k
    for dx in (-8, 8):
        d.ellipse([ex + dx * k - eye_r, hy - 7 * k - eye_r, ex + dx * k + eye_r, hy - 7 * k + eye_r], fill=ink)
    mood = a["mood"]
    mx, my = ex, hy + 9 * k
    if mood == "happy":
        d.arc([mx - 11 * k, my - 9 * k, mx + 11 * k, my + 7 * k], 20, 160, fill=ink, width=max(2, int(4 * k)))
    elif mood == "sad":
        d.arc([mx - 10 * k, my, mx + 10 * k, my + 14 * k], 200, 340, fill=ink, width=max(2, int(4 * k)))
    elif mood == "scared":
        d.ellipse([mx - 6 * k, my - 4 * k, mx + 6 * k, my + 9 * k], outline=ink, width=max(2, int(4 * k)))
    else:
        d.line([mx - 8 * k, my + 3 * k, mx + 8 * k, my + 3 * k], fill=ink, width=max(2, int(4 * k)))
    # accessoires de tête
    if accessory == "cap":
        d.chord([hx - r, hy - r - 2 * k, hx + r, hy + r * 0.6], 180, 360, fill=color, outline=ink, width=lw)
        bx = hx + sign * r * 0.9
        d.line([hx, hy - r * 0.2, bx + sign * 20 * k, hy - r * 0.2], fill=ink, width=lw)
    elif accessory == "hat":
        d.rectangle([hx - r * 0.7, hy - r * 2.0, hx + r * 0.7, hy - r * 0.8], fill=ink)
        d.line([hx - r * 1.2, hy - r * 0.8, hx + r * 1.2, hy - r * 0.8], fill=ink, width=lw)
    elif accessory == "glasses":
        for dx in (-8, 8):
            gx = ex + dx * k
            d.ellipse([gx - 8 * k, hy - 15 * k, gx + 8 * k, hy + 1 * k], outline=ink, width=max(2, int(3 * k)))
    elif accessory == "crown":
        y0 = hy - r - 4 * k
        pts = [(hx - r * 0.8, y0), (hx - r * 0.8, y0 - 22 * k), (hx - r * 0.4, y0 - 10 * k), (hx, y0 - 26 * k),
               (hx + r * 0.4, y0 - 10 * k), (hx + r * 0.8, y0 - 22 * k), (hx + r * 0.8, y0)]
        d.polygon(pts, fill=YELLOW, outline=ink)
    return hx / SS, (hy - r) / SS


# --- décors et accessoires ------------------------------------------------------

def draw_background(img: Image.Image, kind: str, theme: dict) -> None:
    d = ImageDraw.Draw(img)
    S = SS
    c, w = theme["deco"], 4 * S
    g = GROUND * S
    d.line([(0, g), (W * S, g)], fill=theme["soft"], width=5 * S)
    if kind == "room":
        d.rectangle([70 * S, 520 * S, 250 * S, 720 * S], outline=c, width=w)
        d.line([160 * S, 520 * S, 160 * S, 720 * S], fill=c, width=w)
        d.line([70 * S, 620 * S, 250 * S, 620 * S], fill=c, width=w)
        d.rectangle([500 * S, 560 * S, 640 * S, 660 * S], outline=c, width=w)
    elif kind in ("street", "city"):
        xs = [20, 140, 250, 370, 480, 600] if kind == "city" else [30, 200, 470, 590]
        hs = [260, 340, 220, 380, 300, 250] if kind == "city" else [240, 300, 260, 220]
        bw = 110 if kind == "city" else 150
        for x, h in zip(xs, hs):
            d.rectangle([x * S, (GROUND - h) * S, (x + bw) * S, g], outline=c, width=w)
            for wy in range(GROUND - h + 30, GROUND - 40, 55):
                for wx in (x + 20, x + bw - 45):
                    d.rectangle([wx * S, wy * S, (wx + 25) * S, (wy + 28) * S], outline=c, width=2 * S)
    elif kind == "office":
        d.rectangle([430 * S, 860 * S, 680 * S, 880 * S], outline=c, width=w)
        d.line([450 * S, 880 * S, 450 * S, g], fill=c, width=w)
        d.line([660 * S, 880 * S, 660 * S, g], fill=c, width=w)
        d.rectangle([500 * S, 760 * S, 610 * S, 840 * S], outline=c, width=w)
        d.line([555 * S, 840 * S, 555 * S, 860 * S], fill=c, width=w)
        for y in range(520, 700, 30):
            d.line([60 * S, y * S, 230 * S, y * S], fill=c, width=2 * S)
    elif kind == "night_sky":
        for i in range(40):
            x, y = (i * 137) % W, 160 + (i * 211) % 520
            r = 2 + (i % 3)
            d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=theme["soft"])
        d.ellipse([560 * S, 470 * S, 650 * S, 560 * S], fill=(240, 232, 200))
    elif kind == "house_exterior":
        d.rectangle([420 * S, 760 * S, 660 * S, g], outline=c, width=w)
        d.polygon([(400 * S, 760 * S), (540 * S, 640 * S), (680 * S, 760 * S)], outline=c, width=w)
        d.rectangle([515 * S, 880 * S, 565 * S, g], outline=c, width=w)
        _tree(d, 120, c, w)
    elif kind == "nature":
        d.arc([-200 * S, 820 * S, 400 * S, 1200 * S], 180, 360, fill=c, width=w)
        d.arc([300 * S, 860 * S, 950 * S, 1200 * S], 180, 360, fill=c, width=w)
        d.ellipse([560 * S, 480 * S, 650 * S, 570 * S], outline=c, width=w)
        _tree(d, 110, c, w)
        _tree(d, 640, c, w)


def _tree(d, x, c, w):
    S = SS
    d.line([x * S, (GROUND - 150) * S, x * S, GROUND * S], fill=c, width=w)
    d.ellipse([(x - 60) * S, (GROUND - 270) * S, (x + 60) * S, (GROUND - 140) * S], outline=c, width=w)


PROP_SLOTS = [(0.17, 560), (0.83, 560), (0.5, 470)]


def draw_prop(d: ImageDraw.ImageDraw, kind: str, cx: float, cy: float, scale: float, theme: dict) -> None:
    k = SS * scale
    x, y = cx * SS, cy * SS
    ink = theme["ink"]
    lw = max(2, int(5 * k))
    f = _font(int(44 * scale) or 1)
    if kind == "money":
        for i in range(3):
            o = i * 10 * k
            d.rounded_rectangle([x - 55 * k + o, y - 30 * k - o, x + 55 * k + o, y + 30 * k - o], 8 * k,
                                fill=(120, 190, 110), outline=ink, width=lw)
        d.text((x + 20 * k, y - 20 * k), "€", font=f, fill=ink, anchor="mm")
    elif kind == "house":
        d.rectangle([x - 45 * k, y - 10 * k, x + 45 * k, y + 50 * k], fill=(240, 200, 150), outline=ink, width=lw)
        d.polygon([(x - 60 * k, y - 10 * k), (x, y - 55 * k), (x + 60 * k, y - 10 * k)], fill=(200, 80, 60),
                  outline=ink)
    elif kind == "car":
        d.rounded_rectangle([x - 70 * k, y - 10 * k, x + 70 * k, y + 25 * k], 10 * k, fill=(80, 140, 220),
                            outline=ink, width=lw)
        d.polygon([(x - 40 * k, y - 10 * k), (x - 20 * k, y - 40 * k), (x + 30 * k, y - 40 * k),
                   (x + 45 * k, y - 10 * k)], fill=(150, 190, 240), outline=ink)
        for wx in (-40, 40):
            d.ellipse([x + (wx - 15) * k, y + 10 * k, x + (wx + 15) * k, y + 40 * k], fill=ink)
    elif kind == "phone":
        d.rounded_rectangle([x - 28 * k, y - 50 * k, x + 28 * k, y + 50 * k], 10 * k, fill=(60, 60, 70),
                            outline=ink, width=lw)
        d.rectangle([x - 20 * k, y - 38 * k, x + 20 * k, y + 32 * k], fill=(140, 200, 255))
    elif kind == "clock":
        d.ellipse([x - 50 * k, y - 50 * k, x + 50 * k, y + 50 * k], fill=WHITE, outline=ink, width=lw)
        d.line([x, y, x, y - 32 * k], fill=ink, width=lw)
        d.line([x, y, x + 24 * k, y + 8 * k], fill=ink, width=lw)
    elif kind == "globe":
        d.ellipse([x - 50 * k, y - 50 * k, x + 50 * k, y + 50 * k], fill=(120, 180, 230), outline=ink, width=lw)
        d.ellipse([x - 22 * k, y - 50 * k, x + 22 * k, y + 50 * k], outline=ink, width=max(2, int(3 * k)))
        d.line([x - 50 * k, y, x + 50 * k, y], fill=ink, width=max(2, int(3 * k)))
    elif kind == "heart":
        d.ellipse([x - 44 * k, y - 36 * k, x + 2 * k, y + 8 * k], fill=(230, 60, 80))
        d.ellipse([x - 2 * k, y - 36 * k, x + 44 * k, y + 8 * k], fill=(230, 60, 80))
        d.polygon([(x - 42 * k, y - 4 * k), (x + 42 * k, y - 4 * k), (x, y + 46 * k)], fill=(230, 60, 80))
    elif kind == "trophy":
        d.chord([x - 40 * k, y - 50 * k, x + 40 * k, y + 20 * k], 0, 180, fill=YELLOW, outline=ink, width=lw)
        d.rectangle([x - 40 * k, y - 50 * k, x + 40 * k, y - 15 * k], fill=YELLOW, outline=ink, width=lw)
        d.rectangle([x - 8 * k, y + 20 * k, x + 8 * k, y + 40 * k], fill=YELLOW, outline=ink, width=lw)
        d.rectangle([x - 30 * k, y + 40 * k, x + 30 * k, y + 52 * k], fill=YELLOW, outline=ink, width=lw)
    elif kind == "question":
        d.text((x, y), "?", font=_font(int(110 * scale) or 1), fill=YELLOW, anchor="mm",
               stroke_width=int(6 * k), stroke_fill=ink)
    elif kind == "lightning":
        pts = [(x + 10, y - 60), (x - 30, y + 5), (x, y + 5), (x - 15, y + 60), (x + 35, y - 10), (x + 5, y - 10)]
        d.polygon([(x + (px - x) * k, y + (py - y) * k) for px, py in pts], fill=YELLOW, outline=ink)
    elif kind == "tree":
        d.rectangle([x - 8 * k, y, x + 8 * k, y + 55 * k], fill=(140, 90, 50))
        d.ellipse([x - 45 * k, y - 70 * k, x + 45 * k, y + 15 * k], fill=(90, 170, 90), outline=ink, width=lw)
    elif kind == "door":
        d.rectangle([x - 35 * k, y - 60 * k, x + 35 * k, y + 60 * k], fill=(170, 110, 70), outline=ink, width=lw)
        d.ellipse([x + 15 * k, y - 5 * k, x + 25 * k, y + 5 * k], fill=YELLOW)


# --- textes -------------------------------------------------------------------

def _wrap(d, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=font) <= max_w * SS or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_label(img, text: str, top_y: int, big: bool) -> None:
    d = ImageDraw.Draw(img)
    f = _font(46 if big else 34)
    lines = _wrap(d, text, f, 600) if big else [text.upper()]
    lh = (58 if big else 44) * SS
    pad = 18 * SS
    widths = [d.textlength(l, font=f) for l in lines]
    bw = max(widths) + 2 * pad
    x0 = (W * SS - bw) / 2
    y0 = top_y * SS
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + lh * len(lines) + pad], 18 * SS,
                        fill=YELLOW if big else BLACK)
    for i, l in enumerate(lines):
        d.text((W * SS / 2, y0 + pad / 2 + lh * i + lh / 2), l, font=f, anchor="mm",
               fill=BLACK if big else WHITE)


def draw_caption(d, chunk: list[Word], t: float, y: int) -> None:
    f = _font(56)
    text = " ".join(w.text for w in chunk)
    lines = _wrap(d, text, f, 640)
    lh = 70 * SS
    idx = 0
    for li, line in enumerate(lines):
        n = len(line.split())
        words = chunk[idx: idx + n]
        idx += n
        total_w = d.textlength(line, font=f)
        x = (W * SS - total_w) / 2
        yy = y * SS + li * lh
        for w in words:
            active = w.start <= t < w.end
            d.text((x, yy), w.text, font=f, fill=YELLOW if active else WHITE,
                   stroke_width=7 * SS, stroke_fill=BLACK, anchor="ls")
            x += d.textlength(w.text + " ", font=f)


def draw_bubble(d, text: str, hx: float, hy: float) -> None:
    f = _font(28)
    lines = _wrap(d, text, f, 420)
    lh = 36 * SS
    pad = 14 * SS
    bw = max(d.textlength(l, font=f) for l in lines) + 2 * pad
    bh = lh * len(lines) + pad
    x0 = min(max(hx * SS - bw / 2, 20 * SS), W * SS - bw - 20 * SS)
    y1 = (hy - 26) * SS
    y0 = y1 - bh
    d.rounded_rectangle([x0, y0, x0 + bw, y1], 16 * SS, fill=WHITE, outline=BLACK, width=4 * SS)
    tx = min(max(hx * SS, x0 + 30 * SS), x0 + bw - 30 * SS)
    d.polygon([(tx - 12 * SS, y1 - 2 * SS), (tx + 12 * SS, y1 - 2 * SS), (tx, y1 + 18 * SS)], fill=WHITE,
              outline=BLACK)
    for i, l in enumerate(lines):
        d.text((x0 + bw / 2, y0 + pad / 2 + lh * i + lh / 2), l, font=f, fill=BLACK, anchor="mm")


# --- assemblage ---------------------------------------------------------------

@dataclass
class SceneAudio:
    wav: Path
    duration: float


def _hex(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def scenes_with_outro(sb: dict) -> list[dict]:
    """Ajoute la scène finale (question aux spectateurs) à la liste des scènes."""
    main = sb["characters"][0]["id"]
    outro = {"id": len(sb["scenes"]) + 1, "label": "ET TOI ?", "background": "plain",
             "narration": sb["outro"], "actors": [{"character": main, "pose": "shrug", "x": 0.5}],
             "props": ["question"]}
    return list(sb["scenes"]) + [outro]


def render_video(sb: dict, audios: list[SceneAudio], out: Path, workdir: Path,
                 min_total: float = 61.5) -> Path:
    scenes = scenes_with_outro(sb)
    if len(scenes) != len(audios):
        raise RenderError(f"{len(scenes)} scènes mais {len(audios)} pistes audio")
    gaps = [GAP_S] * len(scenes)
    total = sum(a.duration for a in audios) + sum(gaps) + END_HOLD_S
    if total < min_total:              # un peu court : on aère les transitions (max +0,8 s chacune)
        extra = min(0.8, (min_total - total) / len(scenes) + 0.01)
        gaps = [g + extra for g in gaps]
        total = sum(a.duration for a in audios) + sum(gaps) + END_HOLD_S

    narration = concat_wavs([(a.wav, g) for a, g in zip(audios, gaps)], workdir / "voix.wav", END_HOLD_S)
    chars = {c["id"]: c for c in sb["characters"]}

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(narration),
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for si, (scene, audio, gap) in enumerate(zip(scenes, audios, gaps)):
            dur = audio.duration + gap + (END_HOLD_S if si == len(scenes) - 1 else 0)
            theme = DARK if scene["background"] == "night_sky" else LIGHT
            static = Image.new("RGB", (W * SS, H * SS), theme["bg"])
            draw_background(static, scene["background"], theme)
            if si == 0:
                draw_label(static, sb["hook"], 150, big=True)
                cap_y = 385
            else:
                draw_label(static, scene.get("label") or f"ÉTAPE {si + 1}", 150, big=False)
                cap_y = 300
            if si == len(scenes) - 1:
                cap_y = 300
            words = time_words(scene["narration"], audio.duration)
            chunks = chunk_words(words)
            n_frames = max(1, round(dur * FPS))
            for fi in range(n_frames):
                t = fi / FPS
                frame = static.copy()
                d = ImageDraw.Draw(frame)
                pop = min(1.0, t / 0.35)
                for pi, prop in enumerate(scene.get("props") or []):
                    px, py = PROP_SLOTS[pi % len(PROP_SLOTS)]
                    float_y = py + 6 * math.sin(2 * math.pi * 0.5 * t + pi)
                    draw_prop(d, prop, px * W, float_y, 0.4 + 0.6 * _ease(pop), theme)
                heads = []
                for ai, actor in enumerate(scene["actors"]):
                    ch = chars.get(actor["character"], sb["characters"][0])
                    x = actor["x"]
                    if actor["pose"] in ("walk", "run"):
                        speed = 0.04 if actor["pose"] == "walk" else 0.09
                        direction = -1 if actor.get("facing") == "left" else 1
                        x = min(0.9, max(0.1, x + direction * speed * (t / max(dur, 0.1) - 0.5) * 2))
                    hx, hy = draw_stickman(d, x * W, actor["pose"], t + ai * 0.37, actor.get("facing", "right"),
                                           _hex(ch["color"]), ch.get("accessory", "none"), theme)
                    if actor.get("bubble") and t > 0.4:
                        heads.append((actor["bubble"], hx, hy))
                for text, hx, hy in heads:
                    draw_bubble(d, text, hx, hy)
                active = next((c for c in chunks if c[0].start <= t < c[-1].end + 0.15), None)
                if active:
                    draw_caption(d, active, t, cap_y)
                # léger « punch » de zoom au début de chaque scène
                z = 1 + 0.05 * (1 - _ease(min(1.0, t / 0.3)))
                cw, ch_ = W * SS / z, H * SS / z
                box = ((W * SS - cw) / 2, (H * SS - ch_) / 2, (W * SS + cw) / 2, (H * SS + ch_) / 2)
                small = frame.resize((W, H), Image.LANCZOS, box=box)
                proc.stdin.write(small.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait(timeout=600) != 0:
            raise RenderError(f"ffmpeg : {err[-400:]}")
    except BrokenPipeError as exc:
        err = proc.stderr.read().decode(errors="replace")
        raise RenderError(f"ffmpeg s'est arrêté : {err[-400:]}") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
    return out


def _ease(x: float) -> float:
    return 1 - (1 - x) ** 3
