"""
oauth_setup.py — one-time Google Drive OAuth setup for the scraper.

Run this LOCALLY (not in Docker), once, after creating an OAuth Client ID in
Google Cloud Console:

  1. https://console.cloud.google.com/apis/credentials
  2. Create credentials → OAuth client ID → Application type: Desktop app
  3. Download the JSON, save it as: credentials/oauth_client.json
  4. Run:   python oauth_setup.py
  5. A browser opens → sign in with the Google account whose Drive should
     receive uploads → "Continue" through the unverified-app warning.
  6. The script writes credentials/oauth_token.json containing your
     refresh token (no expiry under normal use).
  7. Copy both files into your deployment's credentials/ directory.

After that, the running app uses the refresh token to mint new access tokens
forever — no more browser prompts.
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES      = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_FILE = Path("credentials/oauth_client.json")
TOKEN_FILE  = Path("credentials/oauth_token.json")


def main() -> None:
    if not CLIENT_FILE.exists():
        raise SystemExit(
            f"Missing {CLIENT_FILE}.\n"
            "Download an OAuth Client ID (Desktop type) from\n"
            "  https://console.cloud.google.com/apis/credentials\n"
            f"and save it as {CLIENT_FILE}."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    # access_type=offline + prompt=consent guarantees we get a refresh_token,
    # not just a short-lived access token.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    print()
    print(f"✓ Wrote refresh token to {TOKEN_FILE}")
    print("  Copy both credentials/oauth_client.json and credentials/oauth_token.json")
    print("  into your deployment (Render disk: /app/data/credentials/).")


if __name__ == "__main__":
    main()
