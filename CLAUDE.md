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
- Monorepo : apps/orchestrator (Python FastAPI : bot Telegram, stories, QA), apps/clipper (Python, ffmpeg),
  apps/publisher (Python).
- Postgres (jobs, vidéos, stats) + Redis (file de tâches) sur Railway ; stockage Cloudflare R2.
- LLM : modèles open source via OpenRouter/Groq (variable LLM_MODEL). Whisper : API Groq.
- TTS : Gemini 3.8 Flash TTS (API Gemini), validé à l'écoute. Kokoro écarté (trop robotique en FR).
- Pas de GPU sur Railway : tout ce qui demande un GPU passe par une API.

## Clipper (compte B)
Base : fork de mutonby/openshorts (hors dossier cloud/, licence commerciale).
Sources : campagnes gaming (Call of Duty FR d'abord, GTA 6 dès ouverture) — trailers/gameplay ET streams facecam.
Étapes : ingest (yt-dlp) -> transcribe -> highlights (volume + scènes + score LLM)
-> layout facecam/gameplay 9:16 -> sous-titres + hook -> review Telegram -> publish -> submit Whop.

## Stories (compte A) — format « Et si demain… » en 2 parties, dessin animé (v0.5, 26/11)
DA validée le 26/11 : « style C » (cartoon coloré, stickmen à grosse tête ronde, mascotte casquette orange).
Code : contentmachine/{story2,fal,cartoon}.py (l'ancien rendu Pillow render/pipeline reste pour mémoire).
Étapes : sujet -> histoire orale complète en 2 parties (Gemini, partie 1 finit sur un suspense, partie 2
commence par un rappel) -> découpage en 8-16 plans mot pour mot vérifié + description image/mouvement
-> voix ElevenLabs (si ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID, Gemini en secours) d'un seul bloc par partie (débit ajusté pour 61-90 s), coupes calées sur les pauses
-> image par plan FLUX.2 klein 4B (fal, ~0,003 $) -> animation LTX-2.3 distilled (fal, ~0,09 $ / 5 s,
plafond MAX_COST_PER_PART, repli zoom lent) -> sous-titres mot à mot + bandeaux PARTIE 1/2
-> contrôle qualité profil "story" -> validation Telegram des 2 parties (/histoire [sujet], /voix).
Jamais de consigne de style envoyée à la voix (elle la lisait à voix haute).

## Publisher
- YouTube Data API v3 (OAuth par chaîne).
- TikTok : phase 1 mode Upload (brouillon) ou service audité ; phase 2 Direct Post après audit.

## Boucle d'apprentissage
Cron nocturne : stats par vidéo -> table metrics -> top 10 injectés comme exemples dans les prompts.

## Ordre de travail
Priorité (24/11) : compte A stories d'abord, clips GTA ensuite. 1. Stories v1 (fait). 2. Postgres. 3. Publisher. 4. Clipper. 5. Métriques.
