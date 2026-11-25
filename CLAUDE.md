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

## Stories (compte A) — format « Et si demain… »
Décision (25/11) : rendu en Python/Pillow dans l'orchestrateur plutôt que Remotion (une seule image Docker,
rendu testable en CI, pas de navigateur headless). Code : contentmachine/{scriptgen,gemini,render,pipeline}.py.
Étapes : sujet -> scénario Gemini (storyboard JSON validé par packages/schemas/storyboard.json, relance avec
les erreurs) -> voix Gemini TTS par scène (débit ajusté x0,92-1,22 pour tenir 61-90 s) -> décors IA fal FLUX schnell
(une scène = une phrase, champ "decor", repli sur décor dessiné en code) -> stickmen animés par notre moteur
par-dessus (déplacements to_x, 16 poses), sous-titres calés sur la durée de chaque voix
-> contrôle qualité profil "story" (61-90 s) -> validation Telegram (/histoire [sujet]).

## Publisher
- YouTube Data API v3 (OAuth par chaîne).
- TikTok : phase 1 mode Upload (brouillon) ou service audité ; phase 2 Direct Post après audit.

## Boucle d'apprentissage
Cron nocturne : stats par vidéo -> table metrics -> top 10 injectés comme exemples dans les prompts.

## Ordre de travail
Priorité (24/11) : compte A stories d'abord, clips GTA ensuite. 1. Stories v1 (fait). 2. Postgres. 3. Publisher. 4. Clipper. 5. Métriques.
