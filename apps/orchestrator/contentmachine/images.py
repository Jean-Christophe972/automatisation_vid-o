"""Décors dessinés par IA (fal.ai), derrière les stickmen animés par notre moteur.

- Une image par scène, générée en parallèle, en 720x1280 (facturé 1 mégapixel).
- Style verrouillé par un préfixe de prompt fixe : dessin animé plat, contours noirs,
  couleurs vives, sol dégagé en bas (les personnages y sont posés), haut calme (textes).
- Aucun échec ne bloque la vidéo : une scène sans décor IA garde le décor dessiné en code.
- La clé FAL_KEY vient des variables Railway ; elle n'apparaît jamais dans les erreurs.
"""
from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from PIL import Image

from . import config

log = logging.getLogger(__name__)

# Prix public fal (nov. 2026) par image ~1 MP, pour afficher le coût dans Telegram.
PRICES = {"fal-ai/flux/schnell": 0.003, "fal-ai/z-image/turbo": 0.005}

STYLE = ("Flat 2D cartoon background for a funny animated short, simple rounded shapes, "
         "thick clean black outlines, bright cheerful colors, soft flat shading, kid-friendly. "
         "Vertical composition, eye-level view: a wide empty flat floor or ground filling the bottom quarter, "
         "calm uncluttered sky or wall in the top third. Absolutely no people, no characters, no faces, "
         "no animals, no text, no letters, no logos. Scene: ")
NIGHT = " Night time, but keep colors bright and readable (medium blue, warm lights)."


class DecorError(RuntimeError):
    pass


def model() -> str:
    return config.get("FAL_IMAGE_MODEL") or "fal-ai/flux/schnell"


def decor_prompt(scene: dict) -> str | None:
    text = (scene.get("decor") or "").strip()
    if not text:
        return None
    return STYLE + text + (NIGHT if scene.get("background") == "night_sky" else "")


class FalDecor:
    """Générateur de décors via l'API synchrone de fal (https://fal.run/<modèle>)."""

    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None,
                 model_id: str | None = None, cache_dir: Path | None = None):
        self._key = api_key or config.get("FAL_KEY")
        if not self._key:
            raise DecorError("FAL_KEY manquante")
        self._http = http or httpx.Client(timeout=httpx.Timeout(20.0, read=90.0))
        self.model = model_id or model()
        self.cache_dir = Path(cache_dir or "/tmp/cm-decors")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.generated = 0

    @property
    def unit_price(self) -> float:
        return PRICES.get(self.model, 0.005)

    def generate(self, prompt: str, out_png: Path) -> Path:
        key = hashlib.sha256(f"{self.model}|{prompt}".encode()).hexdigest()[:24]
        cached = self.cache_dir / f"{key}.png"
        if cached.is_file():
            out_png.write_bytes(cached.read_bytes())
            return out_png
        body = {"prompt": prompt, "image_size": {"width": 720, "height": 1280}, "num_images": 1,
                "enable_safety_checker": True, "output_format": "png"}
        last = None
        for _ in range(2):
            try:
                r = self._http.post(f"https://fal.run/{self.model}", json=body,
                                    headers={"Authorization": f"Key {self._key}"})
                if r.status_code != 200:
                    last = f"fal HTTP {r.status_code}"
                    if r.status_code in (401, 403, 422):
                        break
                    continue
                url = r.json()["images"][0]["url"]
                img = self._http.get(url)
                img.raise_for_status()
                tmp = out_png.with_suffix(".dl")
                tmp.write_bytes(img.content)
                with Image.open(tmp) as im:          # vérifie que c'est bien une image
                    im.convert("RGB").save(out_png)
                tmp.unlink(missing_ok=True)
                cached.write_bytes(out_png.read_bytes())
                self.generated += 1
                return out_png
            except (httpx.HTTPError, KeyError, IndexError, ValueError, OSError) as exc:
                last = f"{type(exc).__name__}"
        raise DecorError(f"décor impossible ({last})")


def make_decors(scenes: list[dict], provider, workdir: Path, workers: int = 4) -> dict[int, Path]:
    """Génère les décors des scènes qui en décrivent un. Renvoie {index de scène: image}."""
    if provider is None:
        return {}
    jobs = {i: p for i, s in enumerate(scenes) if (p := decor_prompt(s))}

    def one(item):
        i, prompt = item
        try:
            return i, provider.generate(prompt, workdir / f"decor_{i + 1:02d}.png")
        except Exception as exc:                     # un décor raté ne doit jamais bloquer la vidéo
            log.warning("scène %d : décor IA indisponible (%s), décor dessiné utilisé", i + 1, exc)
            return i, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(pool.map(one, jobs.items()))
    return {i: p for i, p in results.items() if p}
