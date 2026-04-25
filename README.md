# RecordOwl Scraper

FastAPI + Selenium scraper that fans each scraped company out to **Postgres + a local JSON archive + Google Drive**, with a batch worker that resumes across redeploys.

## Data flow

```
POST /api/cookies/upload
        │
        ▼
  cookies saved to disk  ──► auto-starts batch (AUTO_START_SCRAPE=true)
                                     │
                                     ▼
                          for each row in CSV [start, end):
                                     │
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
       Postgres (DB)        Local volume (disk)      Google Drive
   singapore_contacts        json_data/*.json         day folders
   companies_completed
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | Health + batch status |
| GET  | `/api/batch/status` | Just the batch status |
| POST | `/api/cookies/upload` | Upload cookies; auto-kicks the batch |
| POST | `/api/scrape/manual` | Scrape one company by name |
| POST | `/api/batch/run?filename=&start_row=&end_row=` | Manually start a batch |
| GET  | `/api/export/excel` | Download `singapore_contacts` as `.xlsx` |
| GET  | `/api/export/json-archive` | Download every JSON file as a `.zip` |
| GET  | `/api/completed/count` | Scraped vs failed totals |

## Local dev (Docker Compose)

```bash
cp .env.example .env       # set GOOGLE_DRIVE_FOLDER_ID etc.
docker compose up --build
```

The compose file runs Postgres in a sibling container with a named volume; the app binds `./input_files`, `./json_data`, `./.cookies`, `./credentials` for local iteration.

Place your `entities.csv` in `input_files/` and your `service-account.json` in `credentials/`, then `POST /api/cookies/upload`.

## Deploy to Render

1. Push the repo to GitHub.
2. Render → **New → Blueprint** → pick the repo. The blueprint provisions:
   - Managed Postgres (`recordowl-db`)
   - Web service (Docker, starter plan)
   - 1 GB persistent disk at `/app/data`
3. In the Render dashboard, set `GOOGLE_DRIVE_FOLDER_ID`.
4. Open the Render shell and drop the input CSV + service-account.json onto the disk:
   ```
   /app/data/input_files/entities.csv
   /app/data/credentials/service-account.json
   ```
5. `POST /api/cookies/upload` with the cookies JSON. The batch starts immediately and uses `SCRAPE_START_ROW` / `SCRAPE_END_ROW` from env.

Redeploys: cookies persist on the disk, so the batch auto-resumes without you having to re-upload.

## Environment variables

See [`.env.example`](.env.example). The important ones:

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres URL (Render sets this from the managed DB) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the Drive service-account JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | Drive root folder ID (leave empty to disable Drive uploads) |
| `INPUT_DIR` / `JSON_DIR` / `COOKIE_FILE` | Persistent paths (point at `/app/data/...` on Render) |
| `INPUT_CSV_FILENAME` | CSV inside `INPUT_DIR` |
| `SCRAPE_START_ROW` / `SCRAPE_END_ROW` | Range to process |
| `AUTO_START_SCRAPE` | `true` to auto-kick on cookie upload + on restart-with-cookies |
| `RATE_LIMIT_MIN` / `RATE_LIMIT_MAX` | Per-company sleep range (seconds) |

## Project layout

```
backend.py                  FastAPI app + batch worker
db.py                       Postgres layer (companies_completed + singapore_contacts)
gdrive.py                   Google Drive uploads (no-op when folder id unset)
recordowl_scraper_selenium.py
requirements.txt
Dockerfile / start.sh       Container build + entrypoint
docker-compose.yml          Local dev: app + Postgres
render.yaml                 Render blueprint
.env.example
```
