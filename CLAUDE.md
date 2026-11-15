# Content Machine — Stickman Stories & GTA 6 Clips

## Objectif
Deux comptes (TikTok + YouTube Shorts chacun), production automatisée, validation humaine en 1 clic.
- Compte A "stories" : histoires originales stickman en 2-3 parties de 61-90 s.
- Compte B "clips" : clips gaming (GTA 6, Call of Duty…) de campagnes Whop/Vyro AUTORISÉES uniquement.

## Règles non négociables
- Ne jamais télécharger une source hors de config/sources_whitelist.yaml.
- Rien n'est publié sans validation Telegram (bouton approve).
- Pas d'automatisation de navigateur pour publier : APIs officielles ou service audité.
- Secrets uniquement en variables d'environnement Railway, jamais dans le repo.

## Stack
- Monorepo : apps/orchestrator (Python FastAPI + APScheduler), apps/clipper (Python, ffmpeg),
  apps/stories (Node + Remotion), apps/publisher (Python), apps/bot (Telegram).
- Postgres (jobs, vidéos, stats) + Redis (file de tâches) sur Railway ; stockage Cloudflare R2.
- LLM : modèles open source via OpenRouter/Groq (variable LLM_MODEL). Whisper : API Groq.
- TTS : Gemini 3.8 Flash TTS (API Gemini), validé à l'écoute. Kokoro écarté (trop robotique en FR).
- Pas de GPU sur Railway : tout ce qui demande un GPU passe par une API.

## Clipper (compte B)
Base : fork de mutonby/openshorts (hors dossier cloud/, licence commerciale).
Sources : campagnes gaming (Call of Duty FR d'abord, GTA 6 dès ouverture) — trailers/gameplay ET streams facecam.
Étapes : ingest (yt-dlp) -> transcribe -> highlights (volume + scènes + score LLM)
-> layout facecam/gameplay 9:16 -> sous-titres + hook -> review Telegram -> publish -> submit Whop.

## Stories (compte A)
Base : remotion-dev/template-prompt-to-video + morceaux de gyoridavid/short-video-maker.
Étapes : pitchs -> scénario en parties -> storyboard JSON (scènes, poses, bulles, durées)
-> voix Gemini TTS -> rendu Remotion avec bibliothèque de poses stickman SVG -> sous-titres Whisper -> review.
Le schéma du storyboard est dans packages/schemas/storyboard.json (à créer en premier).

## Publisher
- YouTube Data API v3 (OAuth par chaîne).
- TikTok : phase 1 mode Upload (brouillon) ou service audité ; phase 2 Direct Post après audit.

## Boucle d'apprentissage
Cron nocturne : stats par vidéo -> table metrics -> top 10 injectés comme exemples dans les prompts.

## Ordre de travail
1. Clipper v1 en prod avant le 19/11. 2. Publisher + bot. 3. Stories v1. 4. Métriques.
