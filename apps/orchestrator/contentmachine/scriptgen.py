"""Écriture du scénario « Et si demain… » par le LLM, avec validation et relance."""
from __future__ import annotations

import json
import logging
import re
import unicodedata

from .storyboard import MAX_WORDS, MIN_WORDS, StoryboardError, validate

log = logging.getLogger(__name__)

PROMPT = """Tu écris le scénario d'une vidéo TikTok verticale en français, format « Et si demain… », \
animée avec des personnages stickman (bonhommes bâtons). Public : 16-35 ans, francophone.

SUJET : {subject}

CE QUI FAIT LE SUCCÈS DU FORMAT
- Le hook est une question choc qui donne immédiatement envie de connaître la suite.
- L'histoire monte en intensité par étapes datées (JOUR 1, 1 SEMAINE APRÈS, 1 MOIS APRÈS, 1 AN APRÈS…) \
  avec des conséquences de plus en plus surprenantes, concrètes et crédibles.
- Chaque scène apporte un rebondissement ou un détail inattendu. Zéro remplissage, zéro morale.
- Tutoie le spectateur (« tu »). Phrases courtes, rythme parlé, vocabulaire simple.
- Fin : une question qui pousse à commenter (« Et toi, tu ferais quoi ? » ou plus original).

- Ton léger, drôle et qui fait rêver : situations absurdes mais crédibles, petites chutes comiques.

MISE EN SCÈNE : UNE PHRASE = UNE SCÈNE
- Chaque scène est UNE phrase courte (6 à 18 mots) avec SA propre image : un nouveau lieu, une nouvelle \
action ou un nouvel objet. Le spectateur doit voir littéralement ce que dit la voix.
- Les personnages bougent : varie les poses, fais-les traverser l'écran avec "to_x" (marcher, courir), \
réagir (surprised, celebrate, scared, angry, dance…). Évite deux scènes de suite avec la même pose.
- "decor" décrit en ANGLAIS le décor dessiné derrière les personnages : un lieu précis et des objets \
concrets liés à la phrase (ex. "a tiny apartment kitchen with a fridge full of gold coins, morning light"). \
Jamais de personne, d'animal humanoïde, de texte, de logo ni de marque dans "decor".

CONTRAINTES STRICTES
- Narration totale (toutes les scènes + outro) : entre {min_words} et {max_words} mots. C'est impératif : \
la vidéo doit durer moins de 90 secondes.
- 10 à 14 scènes. La 1re scène commence par lire le hook.
- 1 ou 2 personnages, dont le personnage principal « toi » (id "toi").
- Rien de violent, sexuel, haineux, ni de conseil dangereux. Pas de vraie personne ni de marque.
- Réponds UNIQUEMENT avec un objet JSON valide respectant ce format :

{{
  "story_id": "slug-en-minuscules-avec-tirets",
  "title": "Titre court (5-80 caractères)",
  "language": "fr",
  "hook": "Et si demain… ? (10-90 caractères)",
  "characters": [{{"id": "toi", "name": "Toi", "color": "#E4572E", "accessory": "none"}}],
  "scenes": [
    {{"id": 1, "label": "JOUR 1", "background": "room",
      "decor": "a cozy bedroom with an unmade bed and a window showing a sunny city",
      "narration": "texte lu par la voix off",
      "actors": [{{"character": "toi", "pose": "walk", "x": 0.2, "to_x": 0.6, "facing": "right", "bubble": "court (optionnel, max 40)"}}],
      "props": ["money"]}}
  ],
  "outro": "Question finale pour les commentaires",
  "hashtags": ["#etsi", "#histoire"]
}}

Règles du format : "background" ∈ {{{backgrounds}}} (style de secours et ambiance jour/nuit) ; \
"pose" ∈ {{{poses}}} ; "props" ⊂ {{{props}}} (0 à 2, objets dessinés au premier plan) ; \
"x" et "to_x" entre 0.1 et 0.9 ; "accessory" ∈ {{none, cap, glasses, hat, tie, crown}} ; couleurs en #RRGGBB ; \
"label" court (max 24 caractères, peut se répéter d'une scène à l'autre) ; \
"bubble", "to_x" et "props" sont optionnels ; les ids de scènes vont de 1 à N sans trou.
{avoid}{feedback}"""

BACKGROUNDS = ["room", "street", "office", "night_sky", "house_exterior", "city", "nature", "plain"]
POSES = ["stand", "walk", "run", "point", "sit", "jump", "scared", "think", "wave", "celebrate", "sad", "shrug",
         "dance", "talk", "surprised", "angry"]
PROPS = ["money", "house", "car", "phone", "clock", "globe", "heart", "trophy", "question", "lightning", "tree", "door"]

DEFAULT_SUBJECT = ("Choisis toi-même un sujet « Et si demain… » original, universel et très intrigant "
                   "(argent, pouvoirs, société, technologie, nature, quotidien).")


def build_prompt(subject: str | None, avoid: list[str] | None = None, feedback: list[str] | None = None) -> str:
    avoid_txt = ""
    if avoid:
        avoid_txt = "\nSujets DÉJÀ traités, à ne pas refaire : " + " | ".join(avoid[-30:]) + "\n"
    fb = ""
    if feedback:
        fb = ("\nTA RÉPONSE PRÉCÉDENTE ÉTAIT INVALIDE. Corrige exactement ces erreurs :\n- "
              + "\n- ".join(feedback[:15]) + "\n")
    return PROMPT.format(subject=(subject or DEFAULT_SUBJECT).strip()[:200], min_words=MIN_WORDS,
                         max_words=MAX_WORDS, backgrounds=", ".join(BACKGROUNDS), poses=", ".join(POSES),
                         props=", ".join(PROPS), avoid=avoid_txt, feedback=fb)


def slugify(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:64] or "histoire"


def _repair(sb: dict) -> dict:
    """Corrections sans risque avant validation (le LLM se trompe souvent sur ces détails)."""
    if isinstance(sb.get("story_id"), str):
        sb["story_id"] = slugify(sb["story_id"])[:64]
        if len(sb["story_id"]) < 3:
            sb["story_id"] = "histoire-" + sb["story_id"]
    for i, scene in enumerate(sb.get("scenes") or [], start=1):
        if isinstance(scene, dict):
            scene["id"] = i
            for a in scene.get("actors") or []:
                for key in ("x", "to_x"):
                    if isinstance(a, dict) and isinstance(a.get(key), (int, float)):
                        a[key] = min(0.9, max(0.1, float(a[key])))
                if isinstance(a, dict) and isinstance(a.get("bubble"), str):
                    a["bubble"] = a["bubble"][:40]
                    if not a["bubble"].strip():
                        del a["bubble"]
            if isinstance(scene.get("decor"), str):
                scene["decor"] = scene["decor"].strip()[:300]
                if len(scene["decor"]) < 10:
                    del scene["decor"]
            if isinstance(scene.get("props"), list):
                scene["props"] = [p for p in scene["props"] if p in PROPS][:3]
    return sb


def write_story(llm, subject: str | None = None, avoid: list[str] | None = None, attempts: int = 3) -> dict:
    """Demande un scénario au LLM, le valide, et relance avec les erreurs si besoin."""
    feedback: list[str] = []
    for attempt in range(1, attempts + 1):
        raw = llm.generate_json(build_prompt(subject, avoid, feedback))
        if not isinstance(raw, dict):
            feedback = ["la réponse doit être un objet JSON"]
            continue
        try:
            return validate(_repair(raw))
        except StoryboardError as exc:
            log.warning("scénario invalide (essai %d) : %s", attempt, exc)
            feedback = exc.errors
    raise StoryboardError(["scénario toujours invalide après relances"] + feedback)


def caption(sb: dict) -> str:
    tags = " ".join(sb.get("hashtags") or ["#etsi", "#etsidemain", "#histoire"])
    return f"{sb['hook']}\n\n{sb['outro']}\n\n{tags}"


def dumps(sb: dict) -> str:
    return json.dumps(sb, ensure_ascii=False, indent=2)
