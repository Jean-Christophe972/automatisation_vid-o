"""Service orchestrateur (déployé sur Railway).

v0 : expose l'état du service et le contrôle qualité. Les workers (clipper,
stories, publisher, bot) s'y brancheront ensuite.
"""
from __future__ import annotations

import shutil

from fastapi import FastAPI

from . import __version__, config

app = FastAPI(title="Content Machine", version=__version__)


@app.get("/health")
def health() -> dict:
    """Utilisé par Railway pour savoir si le déploiement est sain. Ne renvoie aucun secret."""
    missing = config.missing_required()
    return {
        "status": "ok",
        "version": __version__,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ready_for_jobs": not missing,
        "missing_required": missing,
    }


@app.get("/config")
def config_status() -> dict:
    return config.status()
