"""Upload locally stored materials that do not yet have cloud copies.

Run from the repository root with ``python scripts/sync_to_cloud.py``.
Google Drive is the preferred archive. Telegram upload is an optional second
destination configured with CLOUD_SYNC_TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env", override=False)
sys.path.insert(0, str(ROOT_DIR))

from database import resolve_database_path  # noqa: E402
from drive_service import upload_pdf_to_drive  # noqa: E402


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_to_cloud")
DATABASE_PATH = Path(resolve_database_path())
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = ROOT_DIR / DATABASE_PATH
MATERIALS_DIR = Path(os.getenv("MATERIALS_DIR", str(ROOT_DIR / "static" / "materials"))).expanduser()
if not MATERIALS_DIR.is_absolute():
    MATERIALS_DIR = ROOT_DIR / MATERIALS_DIR
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("CLOUD_SYNC_TELEGRAM_CHAT_ID", "").strip()


def is_drive_url(value: str | None) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and parsed.hostname in {"drive.google.com", "docs.google.com"}


def local_file_for(row: sqlite3.Row) -> Path | None:
    candidates = [row["file_name"]]
    file_url = str(row["file_url"] or "")
    if file_url:
        candidates.append(Path(urlparse(file_url).path).name)
    for candidate_name in candidates:
        name = Path(str(candidate_name or "")).name
        if not name or name in {".", ".."}:
            continue
        candidate = (MATERIALS_DIR / name).resolve()
        if candidate.parent == MATERIALS_DIR.resolve() and candidate.is_file():
            return candidate
    return None


def upload_to_telegram(pdf_path: Path, caption: str) -> str:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and CLOUD_SYNC_TELEGRAM_CHAT_ID to enable Telegram archival.")
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    with pdf_path.open("rb") as source, httpx.Client(timeout=180) as client:
        response = client.post(
            endpoint,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
            files={"document": (pdf_path.name, source, "application/pdf")},
        )
    response.raise_for_status()
    body = response.json()
    document = body.get("result", {}).get("document", {}) if body.get("ok") else {}
    file_id = str(document.get("file_id") or "").strip()
    if not file_id:
        raise RuntimeError(f"Telegram did not return a document file_id: {body}")
    return file_id


def sync_materials() -> int:
    if not DATABASE_PATH.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {DATABASE_PATH}")
    if not MATERIALS_DIR.is_dir():
        raise FileNotFoundError(f"Materials directory does not exist: {MATERIALS_DIR}")

    synced = 0
    skipped = 0
    failed = 0
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='materials'"
        ).fetchone()
        if table is None:
            raise RuntimeError("The SQLite database has no materials table.")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(materials)")}
        required = {"id", "file_name", "file_url", "title", "subject", "department", "drive_url",
                    "drive_download_url", "drive_file_id", "telegram_file_id"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"The materials table is missing columns: {', '.join(sorted(missing))}")
        rows = connection.execute(
            """SELECT id,file_name,file_url,title,subject,department,drive_url,
                      drive_download_url,drive_file_id,telegram_file_id
               FROM materials ORDER BY id"""
        ).fetchall()

        for row in rows:
            has_drive = is_drive_url(row["drive_url"]) or is_drive_url(row["drive_download_url"])
            telegram_id = str(row["telegram_file_id"] or "").strip()
            # The ingest API historically stored a SHA-256 de-duplication key
            # when no retrievable Telegram file ID was supplied.
            has_telegram = bool(telegram_id) and not re.fullmatch(r"[a-fA-F0-9]{64}", telegram_id)
            if has_drive and (has_telegram or not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)):
                skipped += 1
                continue

            pdf_path = local_file_for(row)
            if pdf_path is None:
                if not has_drive and not has_telegram:
                    logger.error("Material %s has no local file or cloud copy", row["id"])
                    failed += 1
                else:
                    skipped += 1
                continue
            content = pdf_path.read_bytes()
            if not content.startswith(b"%PDF-"):
                logger.error("Material %s is not a valid PDF: %s", row["id"], pdf_path)
                failed += 1
                continue

            updates: dict[str, str] = {}
            department = str(row["department"] or "Cybersecurity")
            subject = str(row["subject"] or row["title"] or pdf_path.stem)
            try:
                if not has_drive:
                    drive_record = upload_pdf_to_drive(content, pdf_path.name, department, subject)
                    updates.update({
                        "drive_url": drive_record["web_view_link"],
                        "drive_download_url": drive_record["direct_download_link"],
                        "drive_file_id": drive_record["file_id"],
                    })
                    has_drive = True
                    logger.info("Material %s uploaded to Drive", row["id"])
            except Exception:
                logger.exception("Drive sync failed for material %s", row["id"])

            if not has_telegram and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                try:
                    uploaded_id = upload_to_telegram(pdf_path, str(row["title"] or pdf_path.stem))
                    updates["telegram_file_id"] = uploaded_id
                    has_telegram = True
                    logger.info("Material %s uploaded to Telegram archive", row["id"])
                except Exception:
                    logger.exception("Telegram sync failed for material %s", row["id"])

            if updates:
                assignments = ",".join(f'"{column}"=?' for column in updates)
                connection.execute(
                    f"UPDATE materials SET {assignments} WHERE id=?",
                    (*updates.values(), row["id"]),
                )
                connection.commit()
                synced += 1
            elif not has_drive and not has_telegram:
                logger.error("Material %s remains local-only; no cloud destination succeeded", row["id"])
                failed += 1
            else:
                skipped += 1

    logger.info("Cloud sync finished: updated=%s skipped=%s failed=%s", synced, skipped, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(sync_materials())
