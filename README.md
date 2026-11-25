# Machine à contenu — Stickman Stories & clips gaming

Production automatisée de vidéos courtes pour deux comptes (TikTok + YouTube Shorts), avec validation humaine avant chaque publication. Voir `CLAUDE.md` pour l'architecture et les règles.

## État (v0.4)

| Brique | État |
| --- | --- |
| Orchestrateur FastAPI déployable sur Railway (`/health`, `/config`) | fait |
| Contrôle qualité vidéo (format, durée, son, noir, silence, image figée) | fait, testé |
| Liste blanche des sources (`config/sources_whitelist.yaml`) | fait, testé |
| Schéma du storyboard stickman (`packages/schemas/storyboard.json`) | fait |
| Clipper (fork OpenShorts) | à faire |
| Bot Telegram de validation (`/start`, `/test`, boutons Publier / Jeter) | fait, testé (stockage en mémoire, Postgres à venir) |
| Publisher YouTube / TikTok | à faire |
| Générateur « Et si demain… » : scénario Gemini validé, voix Gemini TTS, rendu stickman animé, sous-titres mot à mot (`/histoire`) | fait, testé avec voix factice ; voix réelle à valider sur Railway |
| v0.4 : une scène par phrase, décors dessinés par IA (fal, `FAL_KEY`, ~0,04 $ par vidéo), déplacements et 16 poses, débit de voix ajusté au lieu de rejeter | fait, testé avec décors factices ; décors réels à valider sur Railway |

## Déploiement Railway

1. Dans Railway, relier le service à ce repo (Settings → Source).
2. Variables du service : au minimum `GEMINI_API_KEY`, puis `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` pour la validation (liste complète dans `.env.example`). Le webhook Telegram s'enregistre tout seul au démarrage grâce au domaine public Railway.
3. Chaque push sur `main` redéploie. Railway vérifie `/health` avant de basculer.

`/health` renvoie `ready_for_jobs: false` tant qu'une variable obligatoire manque. Aucune valeur secrète n'est jamais renvoyée.

## Tests en local

```bash
cd apps/orchestrator
pip install -e ".[dev]"   # ffmpeg doit être installé
pytest -q
```

Contrôle qualité d'une vidéo :

```bash
python -m contentmachine.qa ma_video.mp4 clip    # ou story
```
