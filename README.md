# Job Analyser

Searches LinkedIn's public job listings and uses Gemini to summarise each posting:
what the role is for, must-have vs nice-to-have requirements, skills, red and green
flags, perks, and an estimated salary range.

Live demo: **https://jobanalyser.obencelik.com**

## Two ways to run it

| | Local (Windows) | Public demo |
|---|---|---|
| Start | `start.bat` or `python server.py` | Google Cloud Run (Dockerfile) |
| Gemini key | entered in Settings, saved in `config.json` | `GEMINI_API_KEY` from Secret Manager |
| Favorites, auto-collect, reset | yes | switched off (shared server) |
| Rate limits | none | per visitor + daily AI cap |

Public mode is turned on with the `PUBLIC_MODE=1` environment variable (set in the Dockerfile).
Optional: `PUBLIC_GEMINI_DAILY_CAP` (default 300 AI calls per day across all visitors).

## Deploying

Cloud Run builds the `Dockerfile` and redeploys automatically on every push to `main`
(continuous deployment via Cloud Build). Service settings: region `europe-west1`,
public access, max instances 1, request timeout 3600 s.

## Files

- `server.py` — Flask server: LinkedIn guest search, Gemini analysis, routes
- `db.py` — SQLite storage (job library, AI results, favorites)
- `paths.py` — file locations for dev, the Windows .exe, and hosted runs
- `job-analyser.html` — the whole front end
- `Dockerfile` — public demo container
