"""Client fal.ai : images (FLUX.2 klein 4B) et animation image -> vidéo (LTX-2.3 distilled).

- Images : appel synchrone (moins d'une seconde), 576x1024, ~0,003 $ l'image.
- Animation : file d'attente fal (soumission, suivi, résultat), facturée au mégapixel de vidéo générée.
- Tout est remplaçable par variables Railway (FAL_IMAGE_MODEL, FAL_VIDEO_MODEL).
- La clé FAL_KEY ne figure jamais dans les erreurs ni les journaux.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from . import config

log = logging.getLogger(__name__)

IMG_W, IMG_H = 576, 1024
FPS = 24
MAX_FRAMES = 121                       # 5 s à 24 i/s, limite LTX
IMAGE_PRICE = 0.0029                   # relevé sur fal le 26/11 (0,005 $/MP, 576x1024)
VIDEO_PRICE_PER_MP = 0.001205          # LTX-2.3 22B distilled, relevé le 26/11


class FalError(RuntimeError):
    pass


def image_model() -> str:
    return config.get("FAL_IMAGE_MODEL") or "fal-ai/flux-2/klein/4b"


def video_model() -> str:
    return config.get("FAL_VIDEO_MODEL") or "fal-ai/ltx-2.3-22b/distilled/image-to-video"


def frames_for(duration: float) -> int:
    """Nombre d'images à demander (forme 8n+1 attendue par LTX), plafonné à 5 s."""
    n = max(3, min((MAX_FRAMES - 1) // 8, round(duration * FPS / 8)))
    return 8 * n + 1


def video_cost(frames: int) -> float:
    return IMG_W * IMG_H * frames / 1e6 * VIDEO_PRICE_PER_MP


def _find_url(obj, exts: tuple[str, ...]) -> str | None:
    """Cherche la première URL de média dans une réponse fal (le format varie selon les modèles)."""
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str) and url.startswith("http") and (not exts or url.split("?")[0].endswith(exts)):
            return url
        for v in obj.values():
            if (found := _find_url(v, exts)):
                return found
    elif isinstance(obj, list):
        for v in obj:
            if (found := _find_url(v, exts)):
                return found
    return None


class FalClient:
    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self._key = api_key or config.get("FAL_KEY")
        if not self._key:
            raise FalError("FAL_KEY manquante")
        self._http = http or httpx.Client(timeout=httpx.Timeout(20.0, read=120.0))
        self.poll_s = 5.0
        self.spent = 0.0

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Key {self._key}"}

    def _json(self, r: httpx.Response, what: str) -> dict:
        if r.status_code not in (200, 202):
            raise FalError(f"{what} : HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise FalError(f"{what} : réponse illisible") from exc

    def download(self, url: str, out: Path) -> Path:
        try:
            r = self._http.get(url)
        except httpx.HTTPError as exc:
            raise FalError(f"téléchargement : {type(exc).__name__}") from exc
        if r.status_code != 200 or not r.content:
            raise FalError(f"téléchargement : HTTP {r.status_code}")
        out.write_bytes(r.content)
        return out

    # --- images -------------------------------------------------------------------------
    def image(self, prompt: str, out: Path, seed: int | None = None) -> tuple[Path, str]:
        """Génère une image ; renvoie (fichier local, URL fal réutilisable pour l'animation)."""
        body = {"prompt": prompt, "image_size": {"width": IMG_W, "height": IMG_H}, "num_images": 1,
                "num_inference_steps": 4, "output_format": "png", "enable_safety_checker": True}
        if seed is not None:
            body["seed"] = seed
        last = "inconnue"
        for _ in range(2):
            try:
                r = self._http.post(f"https://fal.run/{image_model()}", json=body, headers=self._auth)
                data = self._json(r, "image")
                url = _find_url(data.get("images") or data, ())
                if not url:
                    raise FalError("image : pas d'URL dans la réponse")
                self.spent += IMAGE_PRICE
                return self.download(url, out), url
            except (FalError, httpx.HTTPError) as exc:
                last = str(exc) if isinstance(exc, FalError) else type(exc).__name__
                if "HTTP 401" in last or "HTTP 403" in last or "HTTP 422" in last:
                    break
        raise FalError(f"image impossible ({last})")

    # --- animation (file d'attente) --------------------------------------------------------
    def submit_video(self, image_url: str, prompt: str, frames: int) -> dict:
        body = {"image_url": image_url, "prompt": prompt, "num_frames": frames, "generate_audio": False}
        try:
            r = self._http.post(f"https://queue.fal.run/{video_model()}", json=body, headers=self._auth)
        except httpx.HTTPError as exc:
            raise FalError(f"animation : {type(exc).__name__}") from exc
        data = self._json(r, "animation")
        if not data.get("status_url") or not data.get("response_url"):
            raise FalError("animation : soumission sans suivi")
        return {"status_url": data["status_url"], "response_url": data["response_url"], "frames": frames}

    def poll_videos(self, jobs: dict, out_dir: Path, timeout_s: float = 1800) -> dict:
        """Attend les animations soumises ; renvoie {clé: fichier mp4} pour celles qui ont réussi."""
        done: dict = {}
        pending = dict(jobs)
        deadline = time.monotonic() + timeout_s
        while pending and time.monotonic() < deadline:
            for key, job in list(pending.items()):
                try:
                    st = self._json(self._http.get(job["status_url"], headers=self._auth), "suivi")
                except (FalError, httpx.HTTPError) as exc:
                    log.warning("animation %s : suivi impossible (%s)", key, exc)
                    continue
                status = st.get("status")
                if status == "COMPLETED":
                    pending.pop(key)
                    try:
                        res = self._json(self._http.get(job["response_url"], headers=self._auth), "résultat")
                        url = _find_url(res, (".mp4", ".webm", ".mov")) or _find_url(res.get("video") or {}, ())
                        if not url:
                            raise FalError("pas de vidéo dans le résultat")
                        done[key] = self.download(url, out_dir / f"anim_{key}.mp4")
                        self.spent += video_cost(job["frames"])
                    except (FalError, httpx.HTTPError) as exc:
                        log.warning("animation %s ratée : %s", key, exc)
                elif status not in ("IN_QUEUE", "IN_PROGRESS"):
                    pending.pop(key)
                    log.warning("animation %s : statut %s", key, status)
            if pending:
                time.sleep(self.poll_s)
        for key in pending:
            log.warning("animation %s : délai dépassé", key)
        return done
