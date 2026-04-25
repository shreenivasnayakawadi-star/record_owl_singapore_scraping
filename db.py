"""
db.py — Neon Postgres layer for the RecordOwl scraper.

Two tables:
  1. companies_completed  — every company we've attempted (skip-set)
  2. singapore_contacts   — leads with phone/email

Single source of truth: DATABASE_URL points at Neon.
"""

import os
import re
import psycopg2
from typing import Any, Dict, List, Set


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return url


def get_connection():
    return psycopg2.connect(_url())


_COMPLETED_DDL = """
CREATE TABLE IF NOT EXISTS companies_completed (
    company_number  TEXT PRIMARY KEY,
    company_name    TEXT NOT NULL,
    status          TEXT DEFAULT 'scraped',
    completed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CONTACTS_DDL = """
CREATE TABLE IF NOT EXISTS singapore_contacts (
    registration_number  TEXT PRIMARY KEY,
    company_name         TEXT NOT NULL,
    contact_no           TEXT,
    email                TEXT,
    scraped_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_TABLES_READY = False


def ensure_tables() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_COMPLETED_DDL)
                cur.execute(_CONTACTS_DDL)
        _TABLES_READY = True
        print("[DB] tables ready")
    except Exception as e:
        print(f"[DB ERROR] ensure_tables: {e}")
        raise


def fetch_completed_companies() -> Set[str]:
    ensure_tables()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT company_number FROM companies_completed;")
                return {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"[DB ERROR] fetch_completed_companies: {e}")
        return set()


def fetch_completed_slugs() -> Set[str]:
    ensure_tables()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT company_name FROM companies_completed;")
                return {slugify(row[0]) for row in cur.fetchall()}
    except Exception as e:
        print(f"[DB ERROR] fetch_completed_slugs: {e}")
        return set()


def mark_completed(company_number: str, company_name: str, status: str = "scraped") -> None:
    ensure_tables()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO companies_completed (company_number, company_name, status)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (company_number) DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        status       = EXCLUDED.status,
                        completed_at = CURRENT_TIMESTAMP;
                    """,
                    (company_number, company_name, status),
                )
        print(f"[COMPLETED] marked {company_number} ({company_name})")
    except Exception as e:
        print(f"[DB ERROR] mark_completed: {e}")


_PLACEHOLDER_PHONES = {"", "-", "—", "n/a", "na", "none", "null", "not available"}
_PLACEHOLDER_EMAILS = {"", "-", "n/a", "none", "null"}
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.com.sg", "hotmail.com", "hotmail.sg",
    "outlook.com", "live.com", "icloud.com", "me.com", "protonmail.com",
    "aol.com", "ymail.com",
}


def _clean_phone(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.lower() in _PLACEHOLDER_PHONES:
        return ""
    if sum(c.isdigit() for c in s) < 6:
        return ""
    return s


def _clean_email(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if s in _PLACEHOLDER_EMAILS:
        return ""
    if "@" not in s:
        return ""
    local, _, domain = s.partition("@")
    if not local or "." not in domain:
        return ""
    return s


def _is_personal_domain(email: str) -> bool:
    if not email or "@" not in email:
        return False
    return email.split("@", 1)[1].strip().lower() in _PERSONAL_EMAIL_DOMAINS


def _join_unique(values: List[str]) -> str:
    seen = set()
    out: List[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return ", ".join(out)


def slugify(name: str) -> str:
    if not name:
        return ""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9\-\s]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def save_to_contacts(data: Dict[str, Any]) -> bool:
    """Upsert a scraped company into singapore_contacts + mark_completed."""
    ensure_tables()

    slug     = data.get("slug")
    overview = data.get("overview", {}) or {}
    people   = data.get("people", []) or []

    company_name = overview.get("company_name")
    reg_no       = overview.get("registration_number")

    if not company_name:
        print(f"[SKIP] {slug}: no company_name")
        return False
    if not reg_no:
        print(f"[SKIP] {slug}: no registration_number")
        return False

    mark_completed(reg_no, company_name)

    raw_phones: List[str] = []
    for candidate in (overview.get("contact_number"), overview.get("phone"), overview.get("telephone")):
        cleaned = _clean_phone(candidate)
        if cleaned:
            raw_phones.append(cleaned)
    for p in people:
        cleaned = _clean_phone(p.get("contact_number"))
        if cleaned:
            raw_phones.append(cleaned)
    contact_no = _join_unique(raw_phones)

    raw_emails: List[str] = []
    for p in people:
        cleaned = _clean_email(p.get("email"))
        if cleaned:
            raw_emails.append(cleaned)
    if not raw_emails:
        for candidate in (data.get("page_emails") or []):
            cleaned = _clean_email(candidate)
            if cleaned and not _is_personal_domain(cleaned):
                raw_emails.append(cleaned)
    email = _join_unique(raw_emails)

    if not contact_no and not email:
        reason = "RFI-locked + no email" if data.get("request_required") else "no phone/email"
        print(f"[SKIP CONTACT] {slug}: {reason} — still marked completed")
        return False

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO singapore_contacts
                        (registration_number, company_name, contact_no, email)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (registration_number) DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        contact_no   = EXCLUDED.contact_no,
                        email        = EXCLUDED.email,
                        last_updated = CURRENT_TIMESTAMP;
                    """,
                    (reg_no, company_name, contact_no or None, email or None),
                )
        print(f"[DB OK] {reg_no} | {company_name}")
        return True
    except Exception as e:
        print(f"[DB ERROR] {slug}: {e}")
        return False
