"""Scénario v2 (26/11) : l'histoire d'abord, la mise en scène ensuite.

Leçon de la v0.4 : demander directement des scènes d'une phrase produisait des phrases sans lien.
1. `write_story` : le LLM écrit l'histoire complète comme un texte ORAL continu, en deux parties
   (partie 1 qui finit sur un suspense, partie 2 qui commence par un rappel puis enchaîne).
2. `plan_scenes` : un second appel découpe chaque partie en plans SANS changer un mot
   (vérifié), et décrit l'image et le mouvement de chaque plan pour le dessin animé.
"""
from __future__ import annotations

import re
import unicodedata

MIN_WORDS, MAX_WORDS = 125, 180     # par partie ; voix ~2-2,4 mots/s -> 60-85 s
MIN_SCENES, MAX_SCENES = 8, 16

# Mascotte du compte : identique dans toutes les vidéos (direction artistique « style C » validée le 26/11).
MASCOT = ("the main character is a stick figure with a big round white head, a bright orange cap, "
          "big expressive cartoon eyes, thin black stick limbs and a small black t-shirt")
STYLE = ("Vibrant 2D animated cartoon still, Saturday-morning cartoon style, stick figure characters with "
         "black line limbs and big round expressive heads, bold clean outlines, cel shading, warm lighting, "
         "colorful detailed background, fun and dreamy mood, dynamic camera angle, vertical composition, "
         "no text, no letters, no logos, no watermark. ")


class StoryError(RuntimeError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


WRITE_PROMPT = """Tu es le meilleur scénariste francophone de TikTok. Tu écris des histoires « Et si demain… » \
racontées par une voix off, illustrées en dessin animé avec des stickmen. Elles cumulent des millions de vues \
parce que la VOIX accroche : on a l'impression qu'un pote te raconte un truc incroyable.

SUJET : {subject}
{avoid}
FORMAT : une histoire en DEUX parties, publiées à 24 h d'écart.
- Partie 1 : commence EXACTEMENT par le hook (une question « Et si demain… ? »), installe la règle, fais \
monter les enjeux avec des étapes datées (« Le premier jour… », « Une semaine plus tard… »), et termine sur \
un suspense fort, au pire ou au meilleur moment, par une phrase du type « Mais ce que tu ignorais, c'est \
que… » ou « Et c'est là que tout a dérapé. ». La partie 1 ne révèle PAS la suite.
- Partie 2 : commence par un rappel d'une ou deux phrases (« Dans la partie 1, … »), reprend exactement où \
le suspense s'est arrêté, enchaîne les rebondissements jusqu'à une chute drôle ou surprenante, et finit \
par une question au spectateur pour les commentaires.

ÉCRITURE ORALE (le plus important) :
- Tutoiement, phrases courtes, rythme vif, comme à l'oral. Une idée par phrase.
- Chaque phrase découle de la précédente : cause, conséquence, réaction. Pas de phrases isolées.
- Un rebondissement ou une image marquante toutes les 2 ou 3 phrases. Des détails concrets et visuels \
(chiffres, objets, lieux, réactions des gens). De l'humour, de l'absurde crédible, du rêve.
- Ponctuation naturelle pour la voix : points, virgules, points d'interrogation, points de suspension.
- INTERDIT : didascalies, parenthèses, astérisques, crochets, émojis, titres, « Partie 1 : », listes, \
mots en anglais inutiles, noms de vraies personnes ou de marques, violence, contenu sexuel ou dangereux.
- Chaque partie fait entre {min_words} et {max_words} mots (c'est impératif : chaque vidéo doit durer \
entre 61 et 90 secondes).

PERSONNAGES : le héros est « toi ». Décris en anglais l'apparence fixe de chaque personnage secondaire \
(2 au maximum), pour que le dessinateur le garde identique d'une image à l'autre.

Réponds UNIQUEMENT avec ce JSON :
{{"title": "titre court accrocheur",
  "hook": "Et si demain… ?",
  "part1": "texte oral complet de la partie 1",
  "part2": "texte oral complet de la partie 2",
  "characters": [{{"name": "le patron", "look": "a tall stick figure with a blue tie and a grey mustache"}}],
  "hashtags": ["#etsidemain", "#histoire"]}}"""


PLAN_PROMPT = """Tu es réalisateur de dessins animés. Découpe ce texte de voix off en plans pour une vidéo \
verticale animée (style cartoon, personnages stickmen).

TEXTE (partie {part}) :
\"\"\"{text}\"\"\"

PERSONNAGES : toi = {mascot}.
{characters}

RÈGLES :
- Découpe le texte en {min_scenes} à {max_scenes} plans consécutifs. Chaque plan = une ou deux phrases \
(6 à 16 mots). Le champ "text" de chaque plan reprend le texte MOT POUR MOT : mis bout à bout, les plans \
doivent redonner exactement le texte, sans rien ajouter, retirer ni reformuler.
- "image" : description EN ANGLAIS de l'image du plan pour un générateur d'images : qui est là (nomme \
« the main character » et les personnages par leur apparence), ce qu'ils font, leur expression, le lieu, \
les objets, le cadrage. L'image montre littéralement ce que dit le texte. Varie les lieux et les cadrages.
- "motion" : description EN ANGLAIS du mouvement pendant le plan (action des personnages, mouvement de \
caméra simple), une phrase. Mouvements vifs et exagérés de cartoon.
- Jamais de texte écrit dans les images, jamais de vraie personne ni de marque.

Réponds UNIQUEMENT avec ce JSON :
{{"scenes": [{{"text": "…", "image": "…", "motion": "…"}}]}}"""


def norm(text: str) -> str:
    """Forme comparable d'un texte : sans accents typographiques, ponctuation ni espaces multiples."""
    t = unicodedata.normalize("NFKC", text).lower().replace("’", "'").replace("…", "...")
    t = re.sub(r"[^\w']+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def words(text: str) -> int:
    return len(norm(text).split())


def clean_oral(text: str) -> str:
    """Retire ce que la voix lirait à tort : didascalies, astérisques, émojis, espaces superflus."""
    text = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", text)
    text = re.sub(r"[*_#>`~|]", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "S" or ch in "€$%°+=")
    text = re.sub(r"(?i)^\s*partie\s*[12]\s*[:.\-]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def check_story(st: dict) -> list[str]:
    errs = []
    for key in ("title", "hook", "part1", "part2"):
        if not isinstance(st.get(key), str) or not st[key].strip():
            errs.append(f"champ « {key} » manquant")
    if errs:
        return errs
    for key in ("part1", "part2"):
        n = words(st[key])
        if not MIN_WORDS <= n <= MAX_WORDS:
            errs.append(f"« {key} » fait {n} mots, il en faut entre {MIN_WORDS} et {MAX_WORDS}")
    if not norm(st["part1"]).startswith(norm(st["hook"])[:25]):
        errs.append("la partie 1 doit commencer exactement par le hook")
    if "?" not in st["part2"][-200:]:
        errs.append("la partie 2 doit finir par une question au spectateur")
    return errs


def write_story(llm, subject: str | None, avoid: list[str] | None = None, attempts: int = 3) -> dict:
    avoid_txt = ("Évite ces sujets déjà traités : " + "; ".join(avoid[-15:]) + "\n") if avoid else ""
    prompt = WRITE_PROMPT.format(subject=subject or "choisis toi-même un sujet original qui fait rêver",
                                 avoid=avoid_txt, min_words=MIN_WORDS, max_words=MAX_WORDS)
    errors: list[str] = []
    for _ in range(attempts):
        p = prompt if not errors else prompt + "\n\nTA RÉPONSE PRÉCÉDENTE ÉTAIT REFUSÉE : " + "; ".join(errors) \
            + ". Corrige ces points."
        st = llm.generate_json(p)
        if not isinstance(st, dict):
            errors = ["la réponse n'est pas un objet JSON"]
            continue
        for key in ("part1", "part2", "hook", "title"):
            if isinstance(st.get(key), str):
                st[key] = clean_oral(st[key])
        errors = check_story(st)
        if not errors:
            st["characters"] = [c for c in st.get("characters") or []
                                if isinstance(c, dict) and c.get("name") and c.get("look")][:2]
            tags = [t if str(t).startswith("#") else f"#{t}" for t in st.get("hashtags") or []]
            st["hashtags"] = [re.sub(r"\s+", "", t) for t in tags][:6] or ["#etsidemain", "#histoire"]
            return st
    raise StoryError(errors)


def check_scenes(scenes, text: str) -> list[str]:
    if not isinstance(scenes, list) or not scenes:
        return ["aucun plan"]
    errs = []
    for i, s in enumerate(scenes, 1):
        if not isinstance(s, dict) or not all(isinstance(s.get(k), str) and s[k].strip()
                                              for k in ("text", "image", "motion")):
            errs.append(f"plan {i} incomplet (text, image, motion)")
    if errs:
        return errs
    if not MIN_SCENES <= len(scenes) <= MAX_SCENES:
        errs.append(f"{len(scenes)} plans, il en faut entre {MIN_SCENES} et {MAX_SCENES}")
    joined = norm(" ".join(s["text"] for s in scenes))
    if joined != norm(text):
        a, b = joined.split(), norm(text).split()
        k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        errs.append("les plans ne redonnent pas le texte mot pour mot (écart vers : « "
                    + " ".join(b[max(0, k - 3):k + 5]) + " »)")
    return errs


def plan_scenes(llm, story: dict, part: int, attempts: int = 3) -> list[dict]:
    text = story[f"part{part}"]
    chars = "\n".join(f"{c['name']} = {c['look']}." for c in story.get("characters", []))
    prompt = PLAN_PROMPT.format(part=part, text=text, mascot=MASCOT, characters=chars,
                                min_scenes=MIN_SCENES, max_scenes=MAX_SCENES)
    errors: list[str] = []
    for _ in range(attempts):
        p = prompt if not errors else prompt + "\n\nTA RÉPONSE PRÉCÉDENTE ÉTAIT REFUSÉE : " + "; ".join(errors)
        data = llm.generate_json(p)
        scenes = data.get("scenes") if isinstance(data, dict) else None
        if isinstance(scenes, list):
            for s in scenes:
                if isinstance(s, dict) and isinstance(s.get("text"), str):
                    s["text"] = clean_oral(s["text"])
        errors = check_scenes(scenes, text)
        if not errors:
            return scenes
    raise StoryError(errors)


def image_prompt(scene: dict, story: dict) -> str:
    chars = " ".join(f"{c['name']}: {c['look']}." for c in story.get("characters", []))
    return f"{STYLE}{MASCOT[0].upper() + MASCOT[1:]}. {chars} Scene: {scene['image']}"


def motion_prompt(scene: dict) -> str:
    return (f"2D cartoon animation. {scene['motion']} Lively, bouncy, exaggerated cartoon motion, smooth "
            "frame-by-frame animation. Keep the flat cartoon style, the same characters, bold outlines and colors.")


def caption(story: dict, part: int) -> str:
    tags = " ".join(story.get("hashtags", []))
    if part == 1:
        return f"{story['hook']} (Partie 1) La suite demain 👀\n\n{tags}"
    return f"{story['title']} (Partie 2) Tu as vu la partie 1 ? 👇\n\n{tags}"
