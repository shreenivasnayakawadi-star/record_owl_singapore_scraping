"""
gdrive.py — upload scraped JSON to Google Drive, no local disk involved.

Auth: OAuth user credentials, **read entirely from environment variables**:
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  GOOGLE_OAUTH_REFRESH_TOKEN
  GOOGLE_DRIVE_FOLDER_ID   (or full Drive URL — extracted automatically)

These values come from Google Cloud Console + a one-time `oauth_setup.py`
local run that produces a refresh token. Paste them into Render's
Environment dashboard once; they're encrypted at rest and survive redeploys.
"""

import io
import json
import os
import re
from datetime import date
from typing import Optional

SCOPES   = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

_AVAILABLE = False
_SERVICE   = None

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    _AVAILABLE = True
except ImportError:
    print("[GDRIVE] google libs not installed — uploads disabled")


def _extract_folder_id(value: str) -> str:
    """Accept a raw Drive folder ID or a full URL and return just the ID."""
    value = (value or "").strip()
    if not value:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


def safe_filename(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    cleaned = re.sub(r"[\s_]+", "_", cleaned)
    return cleaned.strip("._ -")


def _get_service():
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE

    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    client_id     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()

    if not (refresh_token and client_id and client_secret):
        print("[GDRIVE] OAuth env vars not set — uploads disabled")
        return None

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except Exception as e:
        print(f"[GDRIVE] token refresh failed: {e}")
        return None

    _SERVICE = build("drive", "v3", credentials=creds, cache_discovery=False)
    print("[GDRIVE] OAuth service initialised")
    return _SERVICE


def _find_or_create_folder(service, name: str, parent_id: str) -> str:
    query = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}' and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(
        q=query, spaces="drive", fields="files(id, name)", pageSize=1,
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    print(f"[GDRIVE] created folder: {name} ({folder['id']})")
    return folder["id"]


def upload_json(filename: str, data: dict) -> Optional[str]:
    """
    Serialise `data` and upload it to Drive as `filename` under today's date folder.
    No local file is written. Returns the Drive file ID on success.
    """
    if not _AVAILABLE:
        print("[GDRIVE] skipping — libraries missing")
        return None

    raw = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    root = _extract_folder_id(raw)
    if not root:
        print("[GDRIVE] GOOGLE_DRIVE_FOLDER_ID not set — skipping upload")
        return None

    service = _get_service()
    if service is None:
        return None

    try:
        day_folder = _find_or_create_folder(service, date.today().isoformat(), root)

        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(
            io.BytesIO(body),
            mimetype="application/json",
            resumable=False,
        )
        uploaded = service.files().create(
            body={"name": filename, "parents": [day_folder]},
            media_body=media,
            fields="id",
        ).execute()
        print(f"[GDRIVE OK] {filename} -> {date.today().isoformat()}/ ({uploaded['id']})")
        return uploaded["id"]
    except Exception as e:
        print(f"[GDRIVE ERROR] {filename}: {e}")
        return None
