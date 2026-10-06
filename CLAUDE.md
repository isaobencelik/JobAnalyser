Never execute anything before asking.

GitHub (isaobencelik/JobAnalyser, branch `main`) is the single source of truth. Nothing is developed on the laptop any more — all changes are made in the repo and pushed to GitHub.

Source files: server.py, db.py, paths.py, job-analyser.html.

Deployment: every push to `main` is built from the Dockerfile and deployed to Google Cloud Run (service `jobanalyser`, region europe-west1), served at https://jobanalyser.obencelik.com. The container runs with PUBLIC_MODE=1 (admin routes blocked, rate limits, daily Gemini cap). The Gemini key comes from Secret Manager (GEMINI_API_KEY) — never commit keys, config.json or jobs.db.

Bigger changes go on a branch first and are merged to `main` only after Oben reviews them, because merging to `main` deploys to the live site.

Local run (optional): `python server.py` still works without PUBLIC_MODE (full features, key saved in config.json). build.bat / PyInstaller (dist\, build\, *.spec) are legacy outputs, not source.
