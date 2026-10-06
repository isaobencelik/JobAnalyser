---
title: Job Analyser
emoji: 🔍
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
short_description: Search LinkedIn jobs and analyse them with AI
---

# Job Analyser

Searches LinkedIn's public job listings and uses Gemini to summarise each posting:
what the role is for, must-have vs nice-to-have requirements, skills, red and green
flags, perks, and an estimated salary range.

Live demo: **https://jobanalyser.obencelik.com**

## Two ways to run it

| | Local (Windows) | Public demo |
|---|---|---|
| Start | `start.bat` or `python server.py` | Hugging Face Space (Dockerfile) |
| Gemini key | entered in Settings, saved in `config.json` | `GEMINI_API_KEY` Space secret |
| Favorites, auto-collect, reset | yes | switched off (shared server) |
| Rate limits | none | per visitor + daily AI cap |

Public mode is turned on with the `PUBLIC_MODE=1` environment variable (set in the Dockerfile).
Optional: `PUBLIC_GEMINI_DAILY_CAP` (default 300 AI calls per day across all visitors).

## Files

- `server.py` — Flask server: LinkedIn guest search, Gemini analysis, routes
- `db.py` — SQLite storage (job library, AI results, favorites)
- `paths.py` — file locations for dev, the Windows .exe, and hosted runs
- `job-analyser.html` — the whole front end
- `Dockerfile` — public demo container
- `.github/workflows/sync-to-hf.yml` — pushes `main` to the Hugging Face Space
- `cloudflare/worker.js` — serves the Space at jobanalyser.obencelik.com
