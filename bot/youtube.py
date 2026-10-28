import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# readonly lets /youtube status show which channel is connected.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
# Desktop-app OAuth clients may redirect to localhost; the admin copies that URL back into Discord.
REDIRECT_URI = "http://localhost"


def _credentials(token_path: Path) -> Credentials:
    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return creds


def upload_video(path: Path, title: str, description: str, privacy: str, token_path: Path, on_progress=None) -> str:
    """Upload a video and return its YouTube ID. Blocking; run it in a thread.

    on_progress, if given, is called with the fraction uploaded (0.0-1.0) after each chunk.
    """
    youtube = build("youtube", "v3", credentials=_credentials(token_path), cache_discovery=False)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title, "description": description, "categoryId": "10"},  # 10 = Music
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        },
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if on_progress and status:
            on_progress(status.progress())
    return response["id"]


def save_client(client_path: Path, client_id: str, client_secret: str):
    client_path.write_text(json.dumps({
        "installed": {
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }))


def start_login(client_path: Path) -> tuple[Flow, str]:
    """Return the flow (keep it for finish_login) and the Google sign-in link."""
    flow = Flow.from_client_secrets_file(str(client_path), scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return flow, url


def finish_login(flow: Flow, pasted: str, token_path: Path):
    """Exchange what the admin pasted (the localhost URL, or just the code) for a saved token. Blocking."""
    pasted = pasted.strip()
    code = parse_qs(urlparse(pasted).query).get("code", [None])[0] if "code=" in pasted else pasted
    if not code:
        raise ValueError("No code found in what you pasted.")
    flow.fetch_token(code=code)
    token_path.write_text(flow.credentials.to_json())


def channel_name(token_path: Path) -> str:
    """Name of the connected YouTube channel. Blocking."""
    youtube = build("youtube", "v3", credentials=_credentials(token_path), cache_discovery=False)
    items = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    return items[0]["snippet"]["title"] if items else "(this Google account has no YouTube channel)"
