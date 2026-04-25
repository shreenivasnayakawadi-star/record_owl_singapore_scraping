"""
db.py — Database layer for the RecordOwl scraper.

Two tables:
  1. companies_completed  — tracks every company we've attempted (for skip logic)
  2. singapore_contacts   — outreach leads with phone/email

Dual-write strategy:
  • DATABASE_URL           → primary (e.g. Neon). Writes here MUST succeed.
  • SECONDARY_DATABASE_URL → optional secondary (e.g. local Postgres container).
    Writes here are best-effort — failures are logged, never fatal.
  • Reads (skip-set + exports) hit the primary only — single source of truth.
"""

import os
import re
import psycopg2
from typing import Any, Dict, List, Set


# ─────────────────────────── Connection ──────────────────────────────────────

def _primary_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return url


def _secondary_url() -> str:
    """Return the secondary URL, or empty string if dual-write is disabled."""
    return os.environ.get("SECONDARY_DATABASE_URL", "").strip()


def get_connection():
    """Connect to the primary DB. Used by reads and the excel export."""
    return psycopg2.connect(_primary_url())


def _exec_on(url: str, statements) -> None:
    """Run [(sql, params|None), ...] on `url` in a single transaction."""
    with psycopg2.connect(url) as conn:
        with conn.cursor() as cur:
            for sql, params in statements:
                if params is None:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)


def _write_both(statements, *, label: str) -> None:
    """
    Run statements against primary (raises) and secondary (logs only).
    The two transactions are independent — secondary divergence won't poison primary.
    """
    _exec_on(_primary_url(), statements)
    sec = _secondary_url()
    if sec:
        try:
            _exec_on(sec, statements)
        except Exception as e:
            print(f"[DB SECONDARY ERROR] {label}: {e}")


# ─────────────────────────── DDL ─────────────────────────────────────────────

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

# Per-URL DDL cache. Lets the secondary catch up if it became reachable later.
_TABLES_READY: Set[str] = set()


def ensure_tables() -> None:
    """
    Create both tables on primary (must succeed) and secondary (best-effort).
    Idempotent: caches per URL so re-runs are cheap.
    """
    primary = _primary_url()
    if primary not in _TABLES_READY:
        try:
            _exec_on(primary, [(_COMPLETED_DDL, None), (_CONTACTS_DDL, None)])
            _TABLES_READY.add(primary)
            print("[DB] tables ready on primary")
        except Exception as e:
            print(f"[DB ERROR] ensure_tables primary: {e}")
            raise

    secondary = _secondary_url()
    if secondary and secondary not in _TABLES_READY:
        try:
            _exec_on(secondary, [(_COMPLETED_DDL, None), (_CONTACTS_DDL, None)])
            _TABLES_READY.add(secondary)
            print("[DB] tables ready on secondary")
        except Exception as e:
            # Don't raise — secondary readiness is best-effort.
            print(f"[DB SECONDARY ERROR] ensure_tables: {e}")


# ─────────────────────────── companies_completed ─────────────────────────────

def fetch_completed_companies() -> Set[str]:
    """Set of company_number values already scraped (read from primary)."""
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
    """Set of slugified company names (read from primary)."""
    ensure_tables()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT company_name FROM companies_completed;")
                return {slugify(row[0]) for row in cur.fetchall()}
    except Exception as e:
        print(f"[DB ERROR] fetch_completed_slugs: {e}")
        return set()


_MARK_COMPLETED_SQL = """
INSERT INTO companies_completed (company_number, company_name, status)
VALUES (%s, %s, %s)
ON CONFLICT (company_number) DO UPDATE SET
    company_name = EXCLUDED.company_name,
    status       = EXCLUDED.status,
    completed_at = CURRENT_TIMESTAMP;
"""


def mark_completed(company_number: str, company_name: str, status: str = "scraped") -> None:
    """Upsert into companies_completed on primary + secondary."""
    ensure_tables()
    try:
        _write_both(
            [(_MARK_COMPLETED_SQL, (company_number, company_name, status))],
            label=f"mark_completed {company_number}",
        )
        print(f"[COMPLETED] marked {company_number} ({company_name})")
    except Exception as e:
        print(f"[DB ERROR] mark_completed: {e}")


# ─────────────────────────── singapore_contacts ─────────────────────────────

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
    """URL-style slug used for cross-module name matching."""
    if not name:
        return ""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9\-\s]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


_CONTACTS_UPSERT_SQL = """
INSERT INTO singapore_contacts
    (registration_number, company_name, contact_no, email)
VALUES (%s, %s, %s, %s)
ON CONFLICT (registration_number) DO UPDATE SET
    company_name = EXCLUDED.company_name,
    contact_no   = EXCLUDED.contact_no,
    email        = EXCLUDED.email,
    last_updated = CURRENT_TIMESTAMP;
"""


def save_to_contacts(data: Dict[str, Any]) -> bool:
    """
    Full save pipeline for one scraped company.
    Returns True if a row was upserted into singapore_contacts.
    Also marks the company as completed in companies_completed.
    """
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

    # Always mark as completed regardless of whether we have contact info
    mark_completed(reg_no, company_name)

    # ── Phones ────────────────────────────────────────────────────────────
    raw_phones: List[str] = []
    for candidate in (
        overview.get("contact_number"),
        overview.get("phone"),
        overview.get("telephone"),
    ):
        cleaned = _clean_phone(candidate)
        if cleaned:
            raw_phones.append(cleaned)
    for p in people:
        cleaned = _clean_phone(p.get("contact_number"))
        if cleaned:
            raw_phones.append(cleaned)
    contact_no = _join_unique(raw_phones)

    # ── Emails ────────────────────────────────────────────────────────────
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
        if raw_emails:
            print(f"[FALLBACK EMAIL] {slug}: using page-level emails ({len(raw_emails)})")

    email = _join_unique(raw_emails)

    if not contact_no and not email:
        reason = "RFI-locked + no email" if data.get("request_required") else "no phone/email"
        print(f"[SKIP CONTACT] {slug}: {reason} — still marked completed")
        return False

    # ── Upsert singapore_contacts on both primary and secondary ───────────
    try:
        _write_both(
            [(_CONTACTS_UPSERT_SQL, (reg_no, company_name, contact_no or None, email or None))],
            label=f"save_to_contacts {reg_no}",
        )
        print(f"[DB OK] {reg_no} | {company_name}")
        return True
    except Exception as e:
        print(f"[DB ERROR] {slug}: {e}")
        return False
