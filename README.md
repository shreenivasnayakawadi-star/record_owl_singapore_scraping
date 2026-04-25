# RecordOwl Scraper

FastAPI + Selenium scraper. Each scraped company is written to **Neon Postgres** and uploaded as JSON to **Google Drive**. No local volumes, no Docker complexity, no managed databases on Render — everything persistent lives off the platform.

## Data flow

```
POST /api/cookies/upload   →   batch starts
                                   │
                                   ▼
                       for each row in CSV [start..end):
                                   │
                ┌──────────────────┴─────────────────┐
                ▼                                    ▼
         Neon Postgres                        Google Drive
   singapore_contacts (leads)            day-folder / row_company.json
   companies_completed (skip-set)
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | Health + batch status |
| GET  | `/api/batch/status` | Batch progress |
| POST | `/api/cookies/upload` | Upload cookies; auto-kicks the batch |
| POST | `/api/scrape/manual` | Scrape one company by name |
| POST | `/api/batch/run?start_row=&end_row=` | Manual batch range |
| GET  | `/api/export/excel` | Download `singapore_contacts` as `.xlsx` |
| GET  | `/api/completed/count` | Scraped vs failed totals |

## One-time setup (do once locally)

### Neon
1. Sign up at https://neon.tech, create a project + database.
2. Copy the connection string (with `?sslmode=require`). This is `DATABASE_URL`.

### Google OAuth (so Drive uploads bill against your 15 GB user quota — service accounts can't do this)

1. https://console.cloud.google.com/apis/credentials → enable Drive API.
2. **OAuth consent screen** → Internal (Workspace) or External + add yourself as test user.
3. **Credentials → + Create Credentials → OAuth client ID → Desktop app** → download JSON, save as `credentials/oauth_client.json`.
4. Run the local helper to mint a refresh token:
   ```bash
   python oauth_setup.py
   ```
   Browser opens → sign in → allow. It writes `credentials/oauth_token.json`.

### Extract OAuth values for Render env vars

Open both files; the values you'll need are:

| Env var | Where it's from |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | `oauth_client.json` → `installed.client_id` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `oauth_client.json` → `installed.client_secret` |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | `oauth_token.json` → `refresh_token` |
| `GOOGLE_DRIVE_FOLDER_ID` | The `…/folders/<id>` part of your Drive folder URL |

Quick PowerShell extractor:
```powershell
python -c "import json; c=json.load(open('credentials/oauth_client.json'))['installed']; t=json.load(open('credentials/oauth_token.json')); print('CLIENT_ID:    ',c['client_id']); print('CLIENT_SECRET:',c['client_secret']); print('REFRESH_TOKEN:',t['refresh_token'])"
```

## Deploy to Render (free tier)

1. Push the repo to GitHub.
2. Render → **+ New → Blueprint** → pick the repo. It reads `render.yaml` and proposes one web service.
3. **Apply / Create Resources**.
4. Once the service is created, open it → **Environment** tab. Set the five `sync: false` vars:
   - `DATABASE_URL` (Neon)
   - `GOOGLE_OAUTH_CLIENT_ID`
   - `GOOGLE_OAUTH_CLIENT_SECRET`
   - `GOOGLE_OAUTH_REFRESH_TOKEN`
   - `GOOGLE_DRIVE_FOLDER_ID`
5. Render redeploys. After it boots, hit `/api/health` → should return `{"status":"ok"}`.
6. `POST /api/cookies/upload` with your RecordOwl cookies → batch fires using `SCRAPE_START_ROW..END_ROW`.

## Free-tier caveats

- **15 min idle spin-down** — first request after idle takes ~30s to wake up.
- **512 MB RAM** — tight for Selenium+Chrome. If you hit OOM, upgrade to Starter ($7/mo).
- **Ephemeral filesystem** — cookies are lost on every redeploy. Re-upload via `/api/cookies/upload`. (Drive uploads aren't affected; they're remote.)

## Project layout

```
backend.py                  FastAPI app + batch worker
db.py                       Neon Postgres layer
gdrive.py                   Drive upload (OAuth via env vars, in-memory)
recordowl_scraper_selenium.py
oauth_setup.py              One-time refresh-token mint
requirements.txt
Dockerfile                  Used by Render to install Chrome
render.yaml                 Render Blueprint
.env.example
input_files/entities.csv.gz Input CSV (gzipped, ~57 MB)
postman_collection.json     API client
```
