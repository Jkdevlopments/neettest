
import io
import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload


BASE_DIR = Path(__file__).resolve().parent
IS_RENDER = bool(os.environ.get("RENDER"))

DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_FILE = DATA_DIR / "token.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def save_token(credentials):
    TOKEN_FILE.write_text(
        credentials.to_json(),
        encoding="utf-8",
    )


def get_drive_service():
    credentials = None

    # Prefer the persistent token saved after a refresh.
    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE), SCOPES
        )

    # Initial token supplied through Render Environment.
    elif os.environ.get("GOOGLE_DRIVE_TOKEN_JSON"):
        token_data = json.loads(
            os.environ["GOOGLE_DRIVE_TOKEN_JSON"]
        )
        credentials = Credentials.from_authorized_user_info(
            token_data, SCOPES
        )
        save_token(credentials)

    # Local development fallback.
    elif not IS_RENDER:
        oauth_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON")

        if oauth_json:
            client_config = json.loads(oauth_json)
            flow = InstalledAppFlow.from_client_config(
                client_config, SCOPES
            )
        elif CREDENTIALS_FILE.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
        else:
            raise RuntimeError(
                "Google Drive OAuth credentials are missing."
            )

        credentials = flow.run_local_server(port=0)
        save_token(credentials)

    else:
        raise RuntimeError(
            "Google Drive token missing. Configure "
            "GOOGLE_DRIVE_TOKEN_JSON in Render."
        )

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            save_token(credentials)
        else:
            raise RuntimeError(
                "Google Drive token is invalid or cannot be refreshed. "
                "Re-authorize Google Drive locally and update "
                "GOOGLE_DRIVE_TOKEN_JSON."
            )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def upload_question_image(image_bytes, filename, mime_type):
    service = get_drive_service()

    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes),
        mimetype=mime_type,
        resumable=True,
    )

    result = service.files().create(
        body={"name": filename, "mimeType": mime_type},
        media_body=media,
        fields="id,name,mimeType",
    ).execute()

    return result["id"]


def download_question_image(file_id):
    service = get_drive_service()

    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return buffer


def delete_question_image(file_id):
    service = get_drive_service()
    service.files().delete(fileId=file_id).execute()
