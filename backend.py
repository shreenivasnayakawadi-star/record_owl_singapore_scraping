"""
backend.py — RecordOwl Scraper API + batch worker.

Batch lifecycle:
  • On startup: ensure DB tables, then auto-start the batch *only if* a cookies
    file is already present (so redeploys resume seamlessly).
  • First deploy: no cookies on disk → POST /api/cookies/upload kicks off the batch
    using SCRAPE_START_ROW / SCRAPE_END_ROW from env.
  • Each scraped company is fanned out to: Postgres + local JSON archive + Google Drive.
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import io
import json
import os
import random
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

import pandas as pd

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from recordowl_scraper_selenium import scrape_company
from db import (
    ensure_tables,
    get_connection,
    fetch_completed_companies,
    fetch_completed_slugs,
    mark_completed,
    save_to_contacts,
    slugify,
)
from gdrive import upload_json_to_drive, safe_filename

# ─────────────────────────── App init ────────────────────────────────────────

app = FastAPI(title="RecordOwl Scraper", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── Config from env ─────────────────────────────────

COOKIE_FILE    = os.environ.get("COOKIE_FILE", ".cookies/recordowl-cookies.json")
INPUT_DIR      = os.environ.get("INPUT_DIR", "input_files")
JSON_DIR       = os.environ.get("JSON_DIR", "json_data")

os.makedirs(os.path.dirname(COOKIE_FILE) or ".", exist_ok=True)
os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(JSON_DIR, exist_ok=True)

# Single-worker executor — Selenium + cookies aren't safe under concurrency.
executor = ThreadPoolExecutor(max_workers=1)


# ─────────────────────────── Pydantic models ─────────────────────────────────

class CookieData(BaseModel):
    cookies: List[Dict[str, Any]]

class ManualScrapeRequest(BaseModel):
    company_name: str

class BatchStatus(BaseModel):
    running: bool = False
    current_company: str = ""
    processed: int = 0
    skipped: int = 0
    total: int = 0

# Global batch status tracker
batch_status = BatchStatus()


# ─────────────────────────── JSON archive + Drive ────────────────────────────

def archive_and_upload(
    company_name: str,
    slug: str,
    data: dict,
    row_number: int | None = None,
) -> str:
    """
    Save JSON locally, then upload to Google Drive.
    Filename: "{row_number}_{company_name}.json" when row_number is provided,
    else "{company_name}.json" (e.g. for /api/scrape/manual).
    """
    safe_name = safe_filename(company_name) or safe_filename(slug) or "unknown"
    if row_number is not None:
        filename = f"{row_number}_{safe_name}.json"
    else:
        filename = f"{safe_name}.json"
    path = os.path.join(JSON_DIR, filename)

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[JSON OK] archived -> {path}")
    except Exception as e:
        print(f"[JSON ERROR] {path}: {e}")
        return path

    # Upload to Google Drive (non-blocking — errors are logged, not raised)
    try:
        upload_json_to_drive(company_name, slug, data, path)
    except Exception as e:
        print(f"[GDRIVE UPLOAD ERROR] {company_name}: {e}")

    return path


# ─────────────────────────── Full save pipeline ──────────────────────────────

def full_save(data: dict, row_number: int | None = None) -> bool:
    """
    Save to DB (contacts + completed) and archive JSON to disk + Drive.
    `row_number` is the 0-based CSV row index; included in the JSON filename.
    """
    saved = save_to_contacts(data)

    slug         = data.get("slug", "")
    company_name = (data.get("overview") or {}).get("company_name", slug)

    archive_and_upload(company_name, slug, data, row_number=row_number)
    return saved


# ─────────────────────────── Batch logic ─────────────────────────────────────

async def run_batch_logic(filepath: str, start_row: int, end_row: int) -> None:
    """
    Process rows [start_row, end_row) from the CSV.
    Skips any company already in companies_completed.
    """
    global batch_status

    df = pd.read_csv(filepath, header=0, dtype=str)
    total = len(df)
    start = max(0, start_row)
    end   = min(total, end_row)

    if start >= end:
        print(f"[BATCH] Nothing to process: start={start} end={end} total={total}")
        return

    subset = df.iloc[start:end]

    # Fetch already-completed companies for skip logic
    print("[BATCH] Loading completed companies from DB...")
    completed_numbers = fetch_completed_companies()
    completed_slugs   = fetch_completed_slugs()
    print(f"[BATCH] {len(completed_numbers)} companies already completed")

    batch_status.running = True
    batch_status.total   = len(subset)
    batch_status.processed = 0
    batch_status.skipped   = 0

    loop = asyncio.get_event_loop()
    rate_min = float(os.environ.get("RATE_LIMIT_MIN", "3"))
    rate_max = float(os.environ.get("RATE_LIMIT_MAX", "10"))

    for idx, row in subset.iterrows():
        entity_name     = str(row.get("entity_name", "")).strip()
        uen_status_desc = str(row.get("uen_status_desc", "")).strip().lower()

        if not entity_name or entity_name.lower() == "nan" or uen_status_desc == "deregistered":
            print(f"[BATCH] Row {idx}: empty/deregistered, skipping")
            batch_status.skipped += 1
            batch_status.processed += 1
            continue

        # ── Skip if already completed ────────────────────────────────────
        entity_slug = slugify(entity_name)
        uen = str(row.get("uen", "")).strip()

        if uen and uen in completed_numbers:
            print(f"[SKIP] {entity_name} (UEN {uen}) — already completed")
            batch_status.skipped += 1
            batch_status.processed += 1
            continue

        if entity_slug in completed_slugs:
            print(f"[SKIP] {entity_name} — slug already completed")
            batch_status.skipped += 1
            batch_status.processed += 1
            continue

        batch_status.current_company = entity_name

        # ── Scrape with retries ──────────────────────────────────────────
        result = None
        for attempt in range(1, 4):
            try:
                result = await loop.run_in_executor(
                    executor, scrape_company, entity_name, True,
                )
                # 404 from RecordOwl is permanent — don't retry.
                if result and result.get("not_found"):
                    print(f"[404] {entity_name}: page does not exist, no retries")
                    break
                has_name = bool(result and result.get("overview", {}).get("company_name"))
                if has_name or attempt == 3:
                    break
                print(f"[RETRY] {entity_name}: no company_name attempt {attempt}")
            except Exception as e:
                if attempt < 3:
                    backoff = 5 * attempt
                    print(f"[RETRY] {entity_name}: {e} (attempt {attempt}/3); sleeping {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                print(f"[ERROR] {entity_name}: {e} (final attempt)")
                result = None

        if result is not None:
            people_with_email = [p for p in result.get("people", []) if p.get("email")]
            print(
                f"[BATCH] {entity_name}: "
                f"people={len(result.get('people', []))} "
                f"(with email={len(people_with_email)}), "
                f"request_required={bool(result.get('request_required'))}"
            )
            scraped_name = (result.get("overview") or {}).get("company_name")
            try:
                full_save(result, row_number=int(idx))

                # After saving, refresh local caches so we don't re-scrape
                # if the same company appears again in the CSV
                reg_no = (result.get("overview") or {}).get("registration_number")
                if reg_no:
                    completed_numbers.add(reg_no)

                # If the page never rendered a company_name, save_to_contacts
                # skipped the DB write. Still mark the CSV's UEN as failed so
                # we don't retry this row on every subsequent batch.
                if not scraped_name and uen:
                    print(f"[BATCH] {entity_name}: no company_name, marking UEN {uen} as failed")
                    mark_completed(uen, entity_name, status="failed")
                    completed_numbers.add(uen)

                completed_slugs.add(entity_slug)
            except Exception as e:
                print(f"[ERROR] {entity_name} save: {e}")
        else:
            # Mark as attempted even if scrape failed, to avoid infinite retries
            # Use entity_name as both number and name if we have nothing else
            if uen:
                mark_completed(uen, entity_name, status="failed")
                completed_numbers.add(uen)
            completed_slugs.add(entity_slug)

        batch_status.processed += 1

        delay = random.uniform(rate_min, rate_max)
        print(f"[RATE] sleeping {delay:.1f}s before next company")
        await asyncio.sleep(delay)

    batch_status.running = False
    batch_status.current_company = ""
    print(f"[BATCH DONE] processed={batch_status.processed} skipped={batch_status.skipped}")


# ─────────────────────────── Batch trigger ───────────────────────────────────

def _kickoff_batch_from_env() -> Dict[str, Any]:
    """
    Start the batch from env-configured CSV + range, unless one is already running.
    Returns a status dict the caller can include in its HTTP response.
    """
    if batch_status.running:
        return {"started": False, "reason": "batch already running"}

    csv_filename = os.environ.get("INPUT_CSV_FILENAME", "entities.csv")
    start_row    = int(os.environ.get("SCRAPE_START_ROW", "0"))
    end_row      = int(os.environ.get("SCRAPE_END_ROW", "500"))
    filepath     = os.path.join(INPUT_DIR, csv_filename)

    if not os.path.exists(filepath):
        return {"started": False, "reason": f"CSV not found at {filepath}"}

    print(f"[KICKOFF] {csv_filename} rows [{start_row}, {end_row})")
    asyncio.create_task(run_batch_logic(filepath, start_row, end_row))
    return {
        "started":   True,
        "file":      csv_filename,
        "start_row": start_row,
        "end_row":   end_row,
    }


# ─────────────────────────── Startup event ───────────────────────────────────

@app.on_event("startup")
async def on_startup():
    """
    On server start: just ensure DB tables exist. The batch is *only* started
    when POST /api/cookies/upload arrives — never on startup, even if cookies
    are already on disk from a previous deploy.
    """
    print("[STARTUP] Initializing...")
    try:
        ensure_tables()
    except Exception as e:
        print(f"[STARTUP] DB init failed: {e} — will retry on first request")
    print("[STARTUP] Ready. Waiting for POST /api/cookies/upload to start batch.")


# ─────────────────────────── Endpoints ───────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "batch": batch_status.dict(),
    }


@app.get("/api/batch/status")
async def get_batch_status():
    return batch_status.dict()


@app.post("/api/cookies/upload")
async def upload_cookies(body: CookieData):
    """
    Save cookies and, if AUTO_START_SCRAPE=true, immediately kick off the batch
    using SCRAPE_START_ROW / SCRAPE_END_ROW from env.
    """
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(body.cookies, f, indent=2)

    response: Dict[str, Any] = {"status": "success", "count": len(body.cookies)}

    if os.environ.get("AUTO_START_SCRAPE", "false").lower() == "true":
        response["batch"] = _kickoff_batch_from_env()
    else:
        response["batch"] = {"started": False, "reason": "AUTO_START_SCRAPE=false"}

    return response


@app.post("/api/scrape/manual")
async def manual_scrape(req: ManualScrapeRequest):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, scrape_company, req.company_name, True)
    saved = full_save(result)
    people_with_email = [p for p in result.get("people", []) if p.get("email")]

    return {
        "status":            "completed",
        "saved":             saved,
        "request_required":  bool(result.get("request_required")),
        "people_count":      len(result.get("people", [])),
        "people_with_email": len(people_with_email),
        "data":              result,
    }


@app.post("/api/batch/run")
async def start_batch(
    filename: str,
    start_row: int,
    end_row: int,
    background_tasks: BackgroundTasks,
):
    if batch_status.running:
        raise HTTPException(status_code=409, detail="A batch is already running")

    file_path = os.path.join(INPUT_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"CSV not found: {filename}")
    if start_row < 0 or end_row <= start_row:
        raise HTTPException(status_code=400, detail=f"Invalid range: {start_row}-{end_row}")

    background_tasks.add_task(run_batch_logic, file_path, start_row, end_row)
    return {"status": "batch_started", "file": filename, "start_row": start_row, "end_row": end_row}


@app.get("/api/export/excel")
async def export_to_excel():
    try:
        query = """
            SELECT registration_number, company_name, contact_no, email
            FROM singapore_contacts
            WHERE COALESCE(contact_no, '') <> '' OR COALESCE(email, '') <> ''
            ORDER BY company_name ASC
        """
        with get_connection() as conn:
            df = pd.read_sql_query(query, conn)

        if df.empty:
            raise HTTPException(status_code=404, detail="No leads found.")

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="singapore_contacts")
        output.seek(0)

        return StreamingResponse(
            output,
            headers={"Content-Disposition": 'attachment; filename="singapore_contacts.xlsx"'},
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[EXPORT ERROR] {e}")
        raise HTTPException(status_code=500, detail="Export failed.")


@app.get("/api/export/json-archive")
async def export_json_archive():
    """Stream every archived JSON file as a single ZIP."""
    if not os.path.isdir(JSON_DIR):
        raise HTTPException(status_code=404, detail="JSON archive directory does not exist")

    files = sorted(f for f in os.listdir(JSON_DIR) if f.endswith(".json"))
    if not files:
        raise HTTPException(status_code=404, detail="No JSON files to export")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            zf.write(os.path.join(JSON_DIR, fname), arcname=fname)
    buf.seek(0)

    return StreamingResponse(
        buf,
        headers={"Content-Disposition": 'attachment; filename="recordowl_json_archive.zip"'},
        media_type="application/zip",
    )


@app.get("/api/completed/count")
async def completed_count():
    """How many companies have been processed so far."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM companies_completed;")
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM companies_completed WHERE status = 'scraped';")
                scraped = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM companies_completed WHERE status = 'failed';")
                failed = cur.fetchone()[0]
        return {"total": total, "scraped": scraped, "failed": failed}
    except Exception as e:
        return {"error": str(e)}
