# Machine à contenu — Stickman Stories & clips gaming

Production automatisée de vidéos courtes pour deux comptes (TikTok + YouTube Shorts), avec validation humaine avant chaque publication. Voir `CLAUDE.md` pour l'architecture et les règles.

## État (v0.1)

| Brique | État |
| --- | --- |
| Orchestrateur FastAPI déployable sur Railway (`/health`, `/config`) | fait |
| Contrôle qualité vidéo (format, durée, son, noir, silence, image figée) | fait, testé |
| Liste blanche des sources (`config/sources_whitelist.yaml`) | fait, testé |
| Schéma du storyboard stickman (`packages/schemas/storyboard.json`) | fait |
| Clipper (fork OpenShorts) | à faire |
| Bot Telegram de validation | à faire |
| Publisher YouTube / TikTok | à faire |
| Rendu stickman (Remotion) + voix Gemini TTS | à faire |

## Déploiement Railway

1. Dans Railway, relier le service à ce repo (Settings → Source).
2. Variables du service : au minimum `GEMINI_API_KEY` (liste complète dans `.env.example`).
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
