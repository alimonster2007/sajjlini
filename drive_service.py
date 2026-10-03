"""Google Drive upload helpers for Telegram lecture PDFs."""

import io
import logging
import os
import traceback
import unicodedata
from pathlib import Path
from urllib.parse import quote


SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]
TOKEN_FILE = Path(os.getenv(
    "GOOGLE_OAUTH_TOKEN_FILE", str(Path(__file__).resolve().parent / "token.json")
)).expanduser()
if not TOKEN_FILE.is_absolute():
    TOKEN_FILE = Path(__file__).resolve().parent / TOKEN_FILE
ROOT_ID = "12Dz6_w7C3OAZmyOHx5kq1L8okOYla_7A"
DEFAULT_PARENT_ID = ROOT_ID
ROOT_FOLDER_ID = ROOT_ID
DEFAULT_ROOT_FOLDER_ID = ROOT_ID
logger = logging.getLogger(__name__)


def get_drive_service():
    """Build an authenticated Drive client from a local OAuth user token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                "token.json is invalid. Please run authenticate.py to generate token.json."
            ) from exc
    if not creds:
        raise RuntimeError(
            "token.json is missing or invalid. Please run authenticate.py to generate token.json."
        )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise RuntimeError(
                    "Could not refresh the Google OAuth token. Please run authenticate.py to generate token.json."
                ) from exc
        else:
            raise RuntimeError(
                "token.json is missing or invalid. Please run authenticate.py to generate token.json."
            )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_or_create_department_folder(department_name: str, root_folder_id: str = DEFAULT_ROOT_FOLDER_ID) -> str:
    """Find or create a link-viewable department folder under the Drive root."""
    fallback_id = str(root_folder_id or "").strip() or DEFAULT_ROOT_FOLDER_ID
    try:
        service = get_drive_service()
        folder_id = _named_folder(service, department_name, fallback_id)
        return str(folder_id or fallback_id)
    except Exception:
        logger.exception("Could not resolve department folder %r; using root folder", department_name)
        traceback.print_exc()
        return fallback_id


def get_or_create_subfolder(subject_name: str, department_name: str,
                            root_folder_id: str = DEFAULT_ROOT_FOLDER_ID) -> str:
    """Find or create a subject folder under its required department folder."""
    fallback_root_id = str(root_folder_id or "").strip() or DEFAULT_ROOT_FOLDER_ID
    try:
        service = get_drive_service()
    except Exception:
        logger.exception("Could not connect to Drive; using root folder as subject-folder fallback")
        traceback.print_exc()
        return fallback_root_id

    try:
        dept_folder_id = str(_named_folder(service, department_name, fallback_root_id) or fallback_root_id)
    except Exception:
        logger.exception("Could not resolve department folder %r; using root folder", department_name)
        traceback.print_exc()
        dept_folder_id = fallback_root_id
    try:
        subject_folder_id = _named_folder(service, subject_name, dept_folder_id)
        return str(subject_folder_id or dept_folder_id)
    except Exception:
        logger.exception("Could not resolve subject folder %r; using department folder", subject_name)
        traceback.print_exc()
        return dept_folder_id


def _named_folder(service, name: str, parent_folder_id: str) -> str:
    name = unicodedata.normalize("NFC", str(name or "").strip())
    if not name:
        raise ValueError("Folder name cannot be empty.")
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{escaped}' and '{parent_folder_id}' in parents and trashed=false"
    )
    matches = service.files().list(
        q=query, spaces="drive", pageSize=100, fields="nextPageToken,files(id,name,mimeType,parents)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    for match in matches:
        if match.get("id") and parent_folder_id in match.get("parents", []):
            return str(match["id"])

    created = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_folder_id]},
        fields="id,name", supportsAllDrives=True,
    ).execute()
    service.permissions().create(
        fileId=created["id"], body={"type": "anyone", "role": "reader"}, fields="id", supportsAllDrives=True
    ).execute()
    folder_id = created.get("id")
    if not folder_id:
        raise RuntimeError(f"Drive did not return an ID for folder {name!r}.")
    return str(folder_id)


def _upload_media(service, media, file_name: str, department_name: str, subject_name: str) -> dict[str, str]:
    root_id = DEFAULT_ROOT_FOLDER_ID
    try:
        service.files().get(fileId=root_id, fields="id", supportsAllDrives=True).execute()
    except Exception:
        logger.exception("Could not verify the Drive root; keeping configured root ID as fallback")
        traceback.print_exc()
    try:
        dept_folder_id = str(_named_folder(service, department_name, root_id) or root_id)
    except Exception:
        logger.exception("Could not resolve department folder %r; using root folder", department_name)
        traceback.print_exc()
        dept_folder_id = root_id
    try:
        subject_folder_id = str(_named_folder(service, subject_name, dept_folder_id) or dept_folder_id)
    except Exception:
        logger.exception("Could not resolve subject folder %r; using department folder", subject_name)
        traceback.print_exc()
        subject_folder_id = dept_folder_id
    target_id = str(subject_folder_id).strip() if subject_folder_id else ""
    if not target_id or target_id.lower() == "none":
        target_id = ROOT_ID
    try:
        target_info = service.files().get(
            fileId=target_id, fields="id,mimeType", supportsAllDrives=True
        ).execute()
        if target_info.get("mimeType") != "application/vnd.google-apps.folder":
            raise ValueError(f"Drive parent {target_id!r} is not a folder.")
    except Exception:
        if target_id == ROOT_ID:
            raise
        logger.exception("Resolved upload parent %r is invalid; falling back to Drive root", target_id)
        target_id = ROOT_ID

    file_metadata = {"name": file_name, "parents": [target_id]}
    print(f"--> UPLOADING FILE '{file_name}' WITH METADATA: {file_metadata}")
    created = service.files().create(
        body=file_metadata,
        media_body=media, fields="id,name,webViewLink,webContentLink", supportsAllDrives=True,
    ).execute()
    file_id = created["id"]
    service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}, fields="id", supportsAllDrives=True
    ).execute()
    links = service.files().get(
        fileId=file_id, fields="id,name,webViewLink,webContentLink", supportsAllDrives=True
    ).execute()
    view_url = links.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    download_url = links.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={quote(file_id)}"
    return {"file_id": file_id, "file_name": links.get("name", file_name),
            "web_view_link": view_url, "direct_download_link": download_url}


def upload_pdf_to_drive(file_bytes: bytes, filename: str, department_name: str,
                        subject_name: str) -> dict[str, str]:
    """Resolve the department/subject hierarchy and upload a PDF into its subject folder."""
    from googleapiclient.http import MediaIoBaseUpload

    filename = unicodedata.normalize("NFC", str(filename))
    if not filename.lower().endswith(".pdf") or not file_bytes.startswith(b"%PDF-"):
        raise ValueError("Only a valid PDF can be uploaded to Google Drive.")
    try:
        service = get_drive_service()
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/pdf", resumable=True)
        return _upload_media(service, media, filename, department_name, subject_name)
    except Exception as exc:
        logger.exception("Google Drive upload failed for %s in %s / %s", filename, department_name, subject_name)
        traceback.print_exc()
        raise RuntimeError("Google Drive PDF upload failed.") from exc


def upload_pdf_stream_to_drive(file_bytes: bytes, file_name: str, department_name: str,
                               subject_name: str) -> dict[str, str]:
    """Upload an in-memory PDF to Drive without creating a local copy."""
    file_name = unicodedata.normalize("NFC", str(file_name))
    subject_name = unicodedata.normalize("NFC", str(subject_name).strip())
    department_name = unicodedata.normalize("NFC", str(department_name).strip())
    if not file_name.lower().endswith(".pdf") or not file_bytes.startswith(b"%PDF-"):
        raise ValueError("Only a valid PDF can be uploaded to Google Drive.")

    from googleapiclient.http import MediaIoBaseUpload

    try:
        service = get_drive_service()
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/pdf", resumable=True)
        return _upload_media(service, media, file_name, department_name, subject_name)
    except Exception as exc:
        logger.exception("Google Drive upload failed for %s in %s / %s", file_name, department_name, subject_name)
        traceback.print_exc()
        raise RuntimeError("Google Drive PDF upload failed.") from exc
