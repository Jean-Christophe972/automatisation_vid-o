"""Validation des storyboards produits par le LLM.

Deux niveaux : le schéma JSON (forme), puis des règles de cohérence que le
schéma ne peut pas exprimer (personnages connus, longueur de narration...).
Un storyboard invalide n'est jamais rendu : on redemande au LLM en lui
renvoyant la liste des erreurs.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(os.environ.get(
    "STORYBOARD_SCHEMA_PATH",
    Path(__file__).resolve().parents[3] / "packages" / "schemas" / "storyboard.json",
))

# La voix Gemini « conteur » lit ~2 à 2,4 mots/s (mesuré le 25/11 : 165+ mots dépassaient 88 s).
# Cible 130-185 mots ; pipeline.fit_tempo rattrape les petits écarts en ajustant le débit.
MIN_WORDS, MAX_WORDS = 130, 185


class StoryboardError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def word_count(sb: dict) -> int:
    texts = [s.get("narration", "") for s in sb.get("scenes", [])] + [sb.get("outro", "")]
    return sum(len(t.split()) for t in texts)


def validate(sb: dict) -> dict:
    errors = [f"{'/'.join(map(str, e.path)) or 'racine'} : {e.message}"
              for e in sorted(_validator().iter_errors(sb), key=lambda e: list(e.path))]
    if errors:
        raise StoryboardError(errors)

    chars = {c["id"] for c in sb["characters"]}
    for i, scene in enumerate(sb["scenes"], start=1):
        if scene["id"] != i:
            errors.append(f"scenes/{i - 1}/id : attendu {i}, reçu {scene['id']}")
        for a in scene["actors"]:
            if a["character"] not in chars:
                errors.append(f"scène {i} : personnage inconnu « {a['character']} »")
    n = word_count(sb)
    if not MIN_WORDS <= n <= MAX_WORDS:
        errors.append(f"narration de {n} mots : il en faut entre {MIN_WORDS} et {MAX_WORDS}")
    if errors:
        raise StoryboardError(errors)
    return sb
