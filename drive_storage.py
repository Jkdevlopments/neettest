
import io
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE), SCOPES
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not CREDENTIALS_FILE.exists():
            raise RuntimeError(
                "credentials.json not found beside app.py"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        credentials = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def upload_question_image(image_bytes, filename, mime_type):
    """Upload validated image bytes to Google Drive and return file ID."""
    service = get_drive_service()

    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes),
        mimetype=mime_type,
        resumable=True,
    )

    result = service.files().create(
        body={
            "name": filename,
            "mimeType": mime_type,
        },
        media_body=media,
        fields="id,name,mimeType",
    ).execute()

    return result["id"]


def download_question_image(file_id):
    """Download a private Drive image into a BytesIO buffer."""
    service = get_drive_service()

    drive_request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, drive_request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return buffer


def delete_question_image(file_id):
    """Delete an image from Google Drive."""
    service = get_drive_service()
    service.files().delete(fileId=file_id).execute()
