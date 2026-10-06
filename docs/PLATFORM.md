# obencelik.com — App Hosting Platform

How Oben's portfolio site and apps are built, hosted and deployed, and how to add the next app.

_Last updated: 6 October 2026_

---

## 1. The big picture

```
                      you / Claude edit code
                               │
                               ▼
   GitHub  (source of truth — one repo per app)
       │  push to `main`
       ▼
   Google Cloud Build  (builds the Dockerfile automatically)
       │
       ▼
   Google Cloud Run  (runs the app; scales to zero when idle)
       │  domain mapping + Google-managed HTTPS certificate
       ▼
   Cloudflare DNS  (CNAME  <app>.obencelik.com → ghs.googlehosted.com)
       │
       ▼
   Visitors:  https://<app>.obencelik.com
```

- **www.obencelik.com** is the portfolio website. Its domain and DNS are managed in **Cloudflare**.
- **Each app lives on its own subdomain**, e.g. `jobanalyser.obencelik.com`, and runs on **Google Cloud Run**.
- **GitHub is the single source of truth.** Nothing is developed on the laptop. If the laptop is lost, nothing is lost.
- **Deploying is automatic.** Merge or push to `main` and the live site updates in about 3–5 minutes.

---

## 2. What's set up today

### Accounts and services

| Piece | What it is | Details |
|---|---|---|
| **GitHub** | Code for every app | Account `isaobencelik`. The Claude GitHub App has access to the JobAnalyser repo |
| **Google Cloud** | Hosting and future agent tools | Project `obencelik-app` (number `130780102369`), billing enabled |
| **Cloud Build** | Builds and deploys from GitHub | One trigger per app, watching branch `^main$`, build type Dockerfile |
| **Artifact Registry** | Stores the built app images | Created automatically by Cloud Build |
| **Cloud Run** | Runs the apps | Region `europe-west1` (Belgium) |
| **Secret Manager** | Stores API keys safely | `GEMINI_API_KEY` |
| **Cloudflare** | Domain and DNS for obencelik.com | Hosts the portfolio site; one CNAME record per app |
| **Budget alert** | Early warning on spend | €5/month, alerts at 50%, 90% and 100% _(confirm it exists: Billing → Budgets & alerts)_ |

### Apps

| App | Live URL | Repo | Status |
|---|---|---|---|
| **Job Analyser** | https://jobanalyser.obencelik.com | `isaobencelik/JobAnalyser` | Live on Cloud Run. Custom domain certificate may take up to 24h after setup |

Direct Cloud Run address (always works, handy for testing):
https://jobanalyser-130780102369.europe-west1.run.app

---

## 3. Job Analyser — what it does

A Flask (Python) app with a single-page front end.

**Features**
- Searches LinkedIn's **public guest job listings** (no login), streaming results live
- Expands searches automatically: related titles (AI synonyms + Senior/Junior) and nearby towns (e.g. Lisbon → Amadora, Oeiras, Cascais)
- Filters by time window (24h / week / month) and work type (remote / hybrid / on-site)
- **AI analysis with Gemini** (`gemini-3.1-flash-lite`): what the role exists for, must-have vs nice-to-have requirements, skills, red and green flags, perks, seniority, culture
- **Salary estimates** calibrated to the location (Portuguese market by default)
- **Job Library**: every fetched job, kept in SQLite
- Export a job or the list as HTML

**Two modes, same code**

| | Public demo (Cloud Run) | Local (optional) |
|---|---|---|
| Turned on by | `PUBLIC_MODE=1` (set in the Dockerfile) | default |
| Gemini key | Secret Manager → env var `GEMINI_API_KEY` | entered in Settings → `config.json` |
| Settings, Favorites, Auto-collect, Reset | hidden and blocked (403) | available |
| Rate limits per visitor (per 10 min) | 12 searches, 120 job details, 20 AI analyses, 30 salary estimates | none |
| Daily AI cap (all visitors) | 300 Gemini calls (`PUBLIC_GEMINI_DAILY_CAP`) | none |
| LinkedIn sweep depth | reduced, to avoid being blocked | full |
| Banner | "Public demo of Job Analyser — a side project by Oben Celik" | none |

**Cloud Run settings**
- Service `jobanalyser`, region `europe-west1`, public access
- Request-based billing, scaling **min 0 / max 1** (cost guard, and keeps rate limits accurate)
- 1 CPU, 512 MiB memory, port 8080, request timeout 3600 s recommended (long streamed searches)
- Secret `GEMINI_API_KEY` (version `latest`) exposed as an environment variable
- The runtime service account `130780102369-compute@developer.gserviceaccount.com` has the **Secret Manager Secret Accessor** role on the secret

**Repo layout**

| File | Purpose |
|---|---|
| `server.py` | Flask server: LinkedIn search, Gemini analysis, routes, public-mode guards |
| `db.py` | SQLite storage (library, AI results, favorites) |
| `paths.py` | File locations (local, old Windows .exe, cloud via `JOBANALYSER_DATA_DIR`) |
| `job-analyser.html` | Entire front end (calls the server with same-origin paths) |
| `Dockerfile` | Cloud container: Python 3.12 + gunicorn, `PUBLIC_MODE=1`, listens on `$PORT` |
| `CLAUDE.md` | Working rules for Claude in this repo |
| `docs/PLATFORM.md` | This document |

---

## 4. Everyday workflow

### Changing an app
1. Tell Claude what to change (or edit on github.com).
2. Small fixes go straight to `main`. **Bigger changes go on a branch**, and you review them before merging.
3. Merging to `main` builds and deploys automatically in about 3–5 minutes.
4. Check: Cloud Run → service → **Revision History** shows a new revision with a green tick and 100% traffic.

### Rolling back
Cloud Run → service → **Revision History** → pick the previous green revision → **Manage traffic** → send 100% to it. Instant, no rebuild.

### Watching it
- **Logs:** Cloud Run → service → **Observability → Logs** (every request plus the app's own messages)
- **Builds:** Cloud Build → **History**
- **Spend:** Billing → Reports

---

## 5. Playbook — adding a new app at `<name>.obencelik.com`

Same recipe every time. About 20 minutes once the code is ready.

**In the repo (Claude does this)**
1. Create a GitHub repo `isaobencelik/<AppName>` and give the Claude GitHub App access.
2. Add a `Dockerfile` that listens on `$PORT` (Cloud Run uses 8080).
3. Read every secret from environment variables. **Never commit keys**, `.env`, databases or personal data. Add a `.gitignore`.
4. If the app is public and calls a paid API (AI), add per-visitor rate limits and a daily cap, like Job Analyser's `PUBLIC_MODE`.

**In Google Cloud (you click, Claude guides)**
5. **Secret Manager** → create a secret for each key. Grant **Secret Manager Secret Accessor** to `130780102369-compute@developer.gserviceaccount.com` (or grant it once at project level so every app inherits it).
6. **Cloud Run → Connect repository → Cloud Build** → GitHub → pick the repo → branch `^main$` → Dockerfile.
7. Service settings: name `<name>`, region **europe-west1**, **Allow public access**, request-based billing, **min 0 / max 1**, 512 MiB, 1 CPU.
8. **Variables & Secrets** → reference each secret as an environment variable.
9. Create, wait for the green tick, then test the `….run.app` URL.

**Domain (you click)**
10. Cloud Run → **Domain mappings → Add mapping** → service `<name>` → domain `<name>.obencelik.com` (obencelik.com is already verified).
11. Cloudflare → obencelik.com → **DNS → Add record**: `CNAME`, name `<name>`, target `ghs.googlehosted.com`, **Proxy status: DNS only (grey cloud)**.
12. Wait 15 minutes to 24 hours for the certificate, then open `https://<name>.obencelik.com`.

**Portfolio**
13. Add a project card on www.obencelik.com linking to the new subdomain: problem → what you built → key decision → link.

---

## 6. Costs and limits

- **Cloud Run free tier** (per month, shared by all apps): about 2 million requests, 180,000 vCPU-seconds, 360,000 GiB-seconds. Small portfolio apps normally stay at **€0**.
- **Cloud Build, Artifact Registry, Secret Manager:** small free allowances. Builds and stored images for a few apps typically cost cents or nothing.
- **AI API calls are the real cost driver**, not hosting. Keep daily caps on every public AI feature.
- **There is no hard spending stop** on Google Cloud by default. The budget alert only emails. Guards in place: max 1 instance per app, app-level rate limits, the daily Gemini cap, and a separate Gemini key for the website that you can revoke on its own.

---

## 7. Known caveats

| Caveat | Impact | Fix if it matters |
|---|---|---|
| **Cold starts** (scale to zero) | First visit after idle takes a few seconds | Min instances 1 (costs money), or accept it |
| **No persistent disk** | Job Library resets when the instance shuts down | Move storage to Firestore or Cloud Storage (free tiers) |
| **Domain mapping is "Preview"** | Works, but Google doesn't call it production-grade | Later: Global Load Balancer (paid) or Firebase Hosting |
| **LinkedIn may block cloud IPs** | Searches could start failing | Add a "paste a job description" mode (AI part unaffected) |
| **One instance (max 1)** | Fine for a demo, not for heavy traffic | Raise the max and move rate limits to shared storage |

---

## 8. Ready for AI agents next

Capabilities available in the same Google Cloud project (not set up yet):

| Need | Google Cloud service |
|---|---|
| Run an agent on a schedule ("every morning…") | **Cloud Scheduler** → **Cloud Run Jobs** |
| Long or batch work (minutes to hours) | **Cloud Run Jobs** |
| Queues and multi-step workflows | **Cloud Tasks**, **Pub/Sub**, **Workflows** |
| Agent memory and state | **Firestore** (free tier), Cloud SQL |
| Keys and credentials | **Secret Manager** (already in use) |
| Models (Gemini, also Claude) | **Vertex AI**, or the Gemini API key already in use |
| Managed agent runtime and framework | **Vertex AI Agent Engine**, Agent Development Kit (ADK) |

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Revision fails: *Permission denied on secret …* | Runtime service account can't read the secret | Secret Manager → secret → Permissions → grant **Secret Manager Secret Accessor** to `130780102369-compute@developer.gserviceaccount.com`, then redeploy |
| "Build failed" | Error in the build or in the deploy step | Cloud Build → History → red build → read the last lines of the log |
| Need to redeploy without code changes | — | Push an empty commit to `main` (Claude can do this), or Cloud Build → Triggers → Run |
| Custom domain shows a certificate error | Certificate still provisioning, or Cloudflare proxy is on | Wait up to 24h; make sure the Cloudflare record is **DNS only (grey cloud)** |
| App says "going a bit fast" | Per-visitor rate limit hit | Wait 10 minutes (by design) |
| AI says daily limit reached | Daily Gemini cap hit | Resets at midnight; raise `PUBLIC_GEMINI_DAILY_CAP` if needed |

---

## 10. Working rules with Claude

- **GitHub is the source of truth.** Claude edits, commits and pushes. Nothing lives only on the laptop.
- **Claude asks before executing anything** (from `CLAUDE.md`).
- **Bigger changes go on a branch**; merging to `main` means going live.
- **Secrets never pass through the chat.** You paste them straight into Secret Manager.
