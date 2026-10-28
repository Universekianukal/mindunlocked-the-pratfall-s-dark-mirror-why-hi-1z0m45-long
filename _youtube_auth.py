"""One-time local script: mint a YouTube refresh token via OAuth consent.

Run this ONCE on your machine (not in CI) after downloading the OAuth client
JSON from Google Cloud Console. It opens a browser for you to approve access
to the Google account that owns the MindUnlocked channel, then prints the
refresh token to store as the YOUTUBE_REFRESH_TOKEN GitHub secret.

Usage:
    pip install google-auth-oauthlib
    python _youtube_auth.py path/to/client_secret.json
"""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # duplicate-topic detection against real channel history
]

def main():
    if len(sys.argv) != 2:
        print("Usage: python _youtube_auth.py path/to/client_secret.json")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n--- Save these as GitHub repo secrets ---")
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")

if __name__ == "__main__":
    main()
