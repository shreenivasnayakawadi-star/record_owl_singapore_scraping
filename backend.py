"""
backend.py — RecordOwl Scraper API + batch worker.

Free-tier flow:
  • POST /api/cookies/upload → save cookies (ephemeral) + kick off batch
  • Each row: scrape → write to Neon (DB) + upload JSON to Google Drive
  • No local disk, no JSON archive, no dual-write
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import io
import json
import os
import random
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
from gdrive import upload_json, safe_filename

# ─────────────────────────── App init ────────────────────────────────────────

app = FastAPI(title="RecordOwl Scraper", version="6.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── Config from env ─────────────────────────────────

# Cookies are written to a writable path inside the container. Free-tier
# filesystem is ephemeral, but cookies are re-uploaded per deploy anyway.
COOKIE_FILE = os.environ.get("COOKIE_FILE", "/tmp/recordowl-cookies.json")
INPUT_DIR   = os.environ.get("INPUT_DIR", "input_files")

os.makedirs(os.path.dirname(COOKIE_FILE) or ".", exist_ok=True)
os.makedirs(INPUT_DIR, exist_ok=True)

# Single-worker executor — Selenium isn't safe under concurrency.
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

batch_status = BatchStatus()


# ─────────────────────────── Save pipeline ───────────────────────────────────

def full_save(data: dict, row_number: int | None = None) -> bool:
    """
    Persist a scraped company:
      1. Upsert into Neon (singapore_contacts + companies_completed)
      2. Upload JSON to Google Drive (in-memory, no local file)
    """
    saved = save_to_contacts(data)

    slug         = data.get("slug", "")
    company_name = (data.get("overview") or {}).get("company_name", slug)

    safe_name = safe_filename(company_name) or safe_filename(slug) or "unknown"
    if row_number is not None:
        filename = f"{row_number}_{safe_name}.json"
    else:
        filename = f"{safe_name}.json"

    try:
        upload_json(filename, data)
    except Exception as e:
        print(f"[GDRIVE UPLOAD ERROR] {filename}: {e}")

    return saved


# ─────────────────────────── Batch logic ─────────────────────────────────────

def _read_csv_slice(filepath: str, start_row: int, end_row: int) -> pd.DataFrame:
    """
    Read only rows [start_row, end_row) from the CSV (gz-aware).
    Free-tier 512MB RAM can't fit a 220MB pandas frame, so we slice with
    skiprows + nrows to keep memory usage small.
    """
    nrows = max(0, end_row - start_row)
    if nrows == 0:
        return pd.DataFrame()
    return pd.read_csv(
        filepath,
        header=0,
        dtype=str,
        skiprows=range(1, start_row + 1) if start_row > 0 else None,
        nrows=nrows,
    )


def _resolve_csv_path() -> str | None:
    """Find the configured CSV (or its .gz) in INPUT_DIR."""
    name = os.environ.get("INPUT_CSV_FILENAME", "entities.csv")
    for candidate in (
        os.path.join(INPUT_DIR, name),
        os.path.join(INPUT_DIR, name + ".gz"),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


async def run_batch_logic(filepath: str, start_row: int, end_row: int) -> None:
    """Process rows [start_row, end_row). Skips rows already in companies_completed."""
    global batch_status

    df = _read_csv_slice(filepath, start_row, end_row)
    if df.empty:
        print(f"[BATCH] Nothing to process: start={start_row} end={end_row}")
        return

    print("[BATCH] Loading completed companies from Neon...")
    completed_numbers = fetch_completed_companies()
    completed_slugs   = fetch_completed_slugs()
    print(f"[BATCH] {len(completed_numbers)} companies already completed")

    batch_status.running = True
    batch_status.total   = len(df)
    batch_status.processed = 0
    batch_status.skipped   = 0

    loop = asyncio.get_event_loop()
    rate_min = float(os.environ.get("RATE_LIMIT_MIN", "3"))
    rate_max = float(os.environ.get("RATE_LIMIT_MAX", "10"))

    for offset, row in df.reset_index(drop=True).iterrows():
        actual_row = start_row + offset
        entity_name     = str(row.get("entity_name", "")).strip()
        uen_status_desc = str(row.get("uen_status_desc", "")).strip().lower()

        if not entity_name or entity_name.lower() == "nan" or uen_status_desc == "deregistered":
            print(f"[BATCH] Row {actual_row}: empty/deregistered, skipping")
            batch_status.skipped += 1
            batch_status.processed += 1
            continue

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
                full_save(result, row_number=actual_row)

                reg_no = (result.get("overview") or {}).get("registration_number")
                if reg_no:
                    completed_numbers.add(reg_no)

                if not scraped_name and uen:
                    print(f"[BATCH] {entity_name}: no company_name, marking UEN {uen} as failed")
                    mark_completed(uen, entity_name, status="failed")
                    completed_numbers.add(uen)

                completed_slugs.add(entity_slug)
            except Exception as e:
                print(f"[ERROR] {entity_name} save: {e}")
        else:
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
    if batch_status.running:
        return {"started": False, "reason": "batch already running"}

    start_row = int(os.environ.get("SCRAPE_START_ROW", "0"))
    end_row   = int(os.environ.get("SCRAPE_END_ROW", "500"))
    filepath  = _resolve_csv_path()

    if not filepath:
        return {
            "started": False,
            "reason": f"CSV not found at {INPUT_DIR}/{os.environ.get('INPUT_CSV_FILENAME', 'entities.csv')}",
        }

    print(f"[KICKOFF] {filepath} rows [{start_row}, {end_row})")
    asyncio.create_task(run_batch_logic(filepath, start_row, end_row))
    return {"started": True, "file": filepath, "start_row": start_row, "end_row": end_row}


# ─────────────────────────── Startup ─────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    print("[STARTUP] Initializing...")
    try:
        ensure_tables()
    except Exception as e:
        print(f"[STARTUP] DB init failed: {e} — will retry on first request")
    print("[STARTUP] Ready. Waiting for POST /api/cookies/upload to start batch.")


# ─────────────────────────── Endpoints ───────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "batch": batch_status.dict()}


@app.get("/api/batch/status")
async def get_batch_status():
    return batch_status.dict()


@app.post("/api/cookies/upload")
async def upload_cookies(body: CookieData):
    """Save cookies (ephemeral). If AUTO_START_SCRAPE=true, kick off the batch."""
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
async def start_batch(start_row: int, end_row: int, background_tasks: BackgroundTasks):
    if batch_status.running:
        raise HTTPException(status_code=409, detail="A batch is already running")
    if start_row < 0 or end_row <= start_row:
        raise HTTPException(status_code=400, detail=f"Invalid range: {start_row}-{end_row}")
    filepath = _resolve_csv_path()
    if not filepath:
        raise HTTPException(status_code=404, detail="CSV not found in INPUT_DIR")
    background_tasks.add_task(run_batch_logic, filepath, start_row, end_row)
    return {"status": "batch_started", "file": filepath, "start_row": start_row, "end_row": end_row}


@app.get("/api/export/excel")
async def export_to_excel():
    """Stream singapore_contacts as .xlsx (Neon → openpyxl)."""
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


@app.get("/api/completed/count")
async def completed_count():
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
