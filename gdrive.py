"""
gdrive.py — Upload JSON files to Google Drive, organised by day.

Folder structure:
    <ROOT_FOLDER>/
        2026-04-24/
            PSA_International_Pte_Ltd.json
            DBS_Group_Holdings_Ltd.json
        2026-04-25/
            ...

Auth: OAuth user credentials (NOT a service account).
Service accounts have no Drive storage quota; user OAuth uploads count
against the signed-in user's 15 GB allowance, which works on free accounts.

Two files are needed under credentials/:
  - oauth_client.json  → OAuth 2.0 Client ID (Desktop type), downloaded from GCP
  - oauth_token.json   → user's refresh token, written by `python oauth_setup.py`

Run oauth_setup.py once locally to produce oauth_token.json.
"""

import os
import re
from datetime import date
from typing import Optional

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_DRIVE_AVAILABLE = False
_SERVICE = None

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    _DRIVE_AVAILABLE = True
except ImportError:
    print("[GDRIVE] google-api-python-client / google-auth not installed — uploads disabled")


def _get_service():
    """Lazy-init the Drive API service using OAuth user credentials."""
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE

    token_file = os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "credentials/oauth_token.json")
    if not os.path.exists(token_file):
        print(f"[GDRIVE] OAuth token not found at {token_file} — run `python oauth_setup.py` once")
        return None

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    # Refresh expired access tokens automatically (refresh token never expires
    # unless the user revokes it or it sits unused for 6+ months).
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_file, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print("[GDRIVE] refreshed access token")
            except Exception as e:
                print(f"[GDRIVE] token refresh failed: {e} — re-run oauth_setup.py")
                return None
        else:
            print("[GDRIVE] OAuth token invalid and not refreshable — re-run oauth_setup.py")
            return None

    _SERVICE = build("drive", "v3", credentials=creds, cache_discovery=False)
    print("[GDRIVE] OAuth service initialised")
    return _SERVICE


def _find_or_create_folder(service, name: str, parent_id: str) -> str:
    """Find a subfolder by name under parent_id, or create it."""
    query = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}' "
        f"and '{parent_id}' in parents "
        f"and trashed=false"
    )
    results = service.files().list(
        q=query, spaces="drive", fields="files(id, name)", pageSize=1,
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    folder_id = folder["id"]
    print(f"[GDRIVE] created folder: {name} ({folder_id})")
    return folder_id


def safe_filename(name: str) -> str:
    """Filesystem-safe filename derived from a company name."""
    if not name:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    cleaned = re.sub(r"[\s_]+", "_", cleaned)
    return cleaned.strip("._ -")


def _extract_folder_id(value: str) -> str:
    """Accept either a raw Drive folder ID or a full URL and return just the ID."""
    value = (value or "").strip()
    if not value:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


def upload_json_to_drive(company_name: str, slug: str, data: dict, local_path: str) -> Optional[str]:
    """
    Upload a JSON file to Google Drive under a day-based folder.
    Returns Drive file ID on success, None otherwise.
    """
    if not _DRIVE_AVAILABLE:
        print("[GDRIVE] skipping upload — library not installed")
        return None

    raw = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    root_folder = _extract_folder_id(raw)
    if not root_folder:
        print("[GDRIVE] GOOGLE_DRIVE_FOLDER_ID not set — skipping upload")
        return None
    if root_folder != raw.strip():
        print(f"[GDRIVE] extracted folder ID '{root_folder}' from URL")

    service = _get_service()
    if service is None:
        return None

    try:
        today_str = date.today().isoformat()
        day_folder_id = _find_or_create_folder(service, today_str, root_folder)

        filename = os.path.basename(local_path) or (
            (safe_filename(company_name) or safe_filename(slug) or "unknown") + ".json"
        )

        file_metadata = {"name": filename, "parents": [day_folder_id]}
        media = MediaFileUpload(local_path, mimetype="application/json", resumable=True)

        uploaded = service.files().create(
            body=file_metadata, media_body=media, fields="id",
        ).execute()

        file_id = uploaded["id"]
        print(f"[GDRIVE OK] {filename} -> {today_str}/ ({file_id})")
        return file_id

    except Exception as e:
        print(f"[GDRIVE ERROR] {company_name}: {e}")
        return None
