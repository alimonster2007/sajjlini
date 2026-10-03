from fastapi import FastAPI, Form, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
import sqlite3
import hashlib
import base64
import binascii
import random
import secrets
import os
import sys
import ipaddress
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import Request, Body, HTTPException, Header
import httpx
import re
import json
import logging
import unicodedata
import traceback
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
import asyncio
from database import hash_password, verify_password, PASSWORD_HASH_PREFIX, seed_doctor_accounts, seed_master_schedule, resolve_database_path

load_dotenv(Path(__file__).with_name(".env"), override=False)
DATABASE_PATH = Path(resolve_database_path())
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
MATERIALS_DIR = Path(os.getenv("MATERIALS_DIR", str(Path(__file__).with_name("static") / "materials"))).expanduser()
ATTENDANCE_SNAPSHOT_DIR = Path(os.getenv("ATTENDANCE_SNAPSHOT_DIR", str(Path(__file__).with_name("attendance_snapshots")))).expanduser()
MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
ATTENDANCE_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ظ†ط¸ط§ظ… ط§ظ„ط­ط¶ظˆط± ط§ظ„ط°ظƒظٹ ط§ظ„ظ…ظˆط­ط¯")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ATTENDANCE_BOT_TOKEN = os.getenv("ATTENDANCE_BOT_TOKEN", "").strip()
ATTENDANCE_CHAT_ID = os.getenv("ATTENDANCE_CHAT_ID", "").strip()
TELEGRAM_GROUP_B_CHAT_ID = os.getenv("TELEGRAM_CYBERSECURITY_GROUP_B_CHAT_ID", "").strip()
TELEGRAM_GROUP_B_TOPIC_ID = os.getenv("TELEGRAM_CYBERSECURITY_GROUP_B_TOPIC_ID", "").strip()
LECTURE_INGEST_API_KEY = os.getenv("LECTURE_INGEST_API_KEY", "").strip()
TELEGRAM_CYBERSECURITY_CHAT_ID = int(os.getenv("TELEGRAM_CYBERSECURITY_CHAT_ID", "-100439103"))
CYBERSECURITY_DEPARTMENT_NAME = "\u0642\u0633\u0645 \u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0634\u0628\u0643\u0627\u062a \u0648\u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a"
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Baghdad"))
ATTENDANCE_OTP_SCHEDULER_TASK = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static/materials", StaticFiles(directory=str(MATERIALS_DIR)), name="materials")
app.mount("/static", StaticFiles(directory="static"), name="static")

def ensure_materials_table():
    with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        file_name TEXT NOT NULL UNIQUE,
        uploaded_by TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        subject TEXT,
        department TEXT,
        department_id TEXT,
        group_name TEXT,
        file_url TEXT,
        upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
        telegram_file_id TEXT UNIQUE
        )""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(materials)").fetchall()}
        for column, declaration in (
            ("subject", "TEXT"), ("department", "TEXT"), ("department_id", "TEXT"), ("group_name", "TEXT"),
            ("created_at", "DATETIME"),
            ("file_url", "TEXT"), ("upload_date", "DATETIME"), ("telegram_file_id", "TEXT"),
            ("drive_url", "TEXT"), ("drive_download_url", "TEXT"), ("telegram_backup_url", "TEXT"),
            ("topic_id", "INTEGER"), ("file_size", "INTEGER"), ("drive_file_id", "TEXT"),
            ("subject_id", "INTEGER"), ("department_id", "TEXT"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE materials ADD COLUMN {column} {declaration}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_telegram_file_id ON materials(telegram_file_id)")
        conn.execute("UPDATE materials SET upload_date=COALESCE(upload_date,created_at)")
        conn.execute("UPDATE materials SET created_at=COALESCE(created_at,upload_date,CURRENT_TIMESTAMP)")
        conn.execute("UPDATE materials SET file_url='/static/materials/' || file_name WHERE file_url IS NULL")
        conn.execute("UPDATE materials SET subject=COALESCE(subject,title) WHERE subject IS NULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS lectures (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            department TEXT NOT NULL,
            drive_url TEXT NOT NULL,
            telegram_url TEXT,
            topic_id INTEGER,
            created_at DATETIME NOT NULL,
            telegram_file_id TEXT UNIQUE,
            drive_file_id TEXT,
            subject_id INTEGER,
            department_id TEXT
        )""")
        lecture_columns = {row[1] for row in conn.execute("PRAGMA table_info(lectures)").fetchall()}
        if "drive_file_id" not in lecture_columns:
            conn.execute("ALTER TABLE lectures ADD COLUMN drive_file_id TEXT")
        if "subject_id" not in lecture_columns:
            conn.execute("ALTER TABLE lectures ADD COLUMN subject_id INTEGER")
        if "department_id" not in lecture_columns:
            conn.execute("ALTER TABLE lectures ADD COLUMN department_id TEXT")
        # Rows present before department isolation was introduced belong to Cybersecurity.
        conn.execute("""INSERT OR IGNORE INTO lectures
            (id,title,subject,department,department_id,drive_url,telegram_url,topic_id,created_at,telegram_file_id,drive_file_id)
            SELECT id,title,COALESCE(subject,title),COALESCE(NULLIF(department,''),''),
                   NULLIF(trim(department_id),''),
                   COALESCE(drive_url,file_url,''),telegram_backup_url,topic_id,
                   COALESCE(created_at,upload_date,CURRENT_TIMESTAMP),telegram_file_id,drive_file_id
            FROM materials""")
        conn.execute("""CREATE TABLE IF NOT EXISTS lecture_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_name TEXT NOT NULL,
            department TEXT NOT NULL,
            lookup_key TEXT NOT NULL,
            UNIQUE(department, lookup_key)
        )""")
        _enforce_cybersecurity_subject_assignment(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            department_id TEXT NOT NULL,
            department TEXT NOT NULL,
            lookup_key TEXT NOT NULL,
            topic_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(department_id, lookup_key)
        )""")
        subject_columns = {row[1] for row in conn.execute("PRAGMA table_info(subjects)").fetchall()}
        if "topic_id" not in subject_columns:
            conn.execute("ALTER TABLE subjects ADD COLUMN topic_id INTEGER")
        if "doctor_name" not in subject_columns:
            conn.execute("ALTER TABLE subjects ADD COLUMN doctor_name TEXT")
        if "doctor_email" not in subject_columns:
            conn.execute("ALTER TABLE subjects ADD COLUMN doctor_email TEXT")
        _clean_department_records(conn)
        _sync_subject_catalog(conn)
        _seed_department_subjects(conn)


def is_google_drive_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {"drive.google.com", "docs.google.com"}


def lecture_api_key_matches(supplied_key: str | None) -> bool:
    supplied_bytes = (supplied_key or "").encode("utf-8")
    expected_bytes = (LECTURE_INGEST_API_KEY or "").encode("utf-8")
    return secrets.compare_digest(supplied_bytes, expected_bytes)


def _subject_lookup_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.translate(str.maketrans({"\u0649": "\u064a"}))
    return " ".join(normalized.casefold().split())


def _enforce_cybersecurity_subject_assignment(conn: sqlite3.Connection) -> None:
    subject = conn.execute(
        "SELECT subject_name,department,lookup_key FROM lecture_subjects WHERE id=7"
    ).fetchone()
    if not subject:
        return
    expected_key = _subject_lookup_key("Electrical Circuit Analysis II")
    if _subject_lookup_key(str(subject[0])) != expected_key:
        logger.warning(
            "Subject ID 7 is %r, not 'Electrical Circuit Analysis II'; leaving its department unchanged",
            subject[0],
        )
        return
    if str(subject[1]) == CYBERSECURITY_DEPARTMENT_NAME:
        return
    duplicate = conn.execute(
        "SELECT id FROM lecture_subjects WHERE id<>7 AND department=? AND lookup_key=?",
        (CYBERSECURITY_DEPARTMENT_NAME, subject[2]),
    ).fetchone()
    if duplicate:
        logger.error(
            "Cannot move subject ID 7 to the Cybersecurity department: duplicate subject ID %s already exists",
            duplicate[0],
        )
        return
    cursor = conn.execute(
        "UPDATE lecture_subjects SET department=? WHERE id=7",
        (CYBERSECURITY_DEPARTMENT_NAME,),
    )
    if cursor.rowcount:
        logger.info("Updated subject ID 7 (%s) to department %s", subject[0], CYBERSECURITY_DEPARTMENT_NAME)


def _resolve_subject_id(conn: sqlite3.Connection, subject_name: str, department: str,
                        proposed_subject_id=None, allow_department_mismatch: bool = False) -> tuple[int, str]:
    """Resolve a stable subject ID by configured topic subject, independent of file name."""
    conn.execute("""CREATE TABLE IF NOT EXISTS lecture_subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_name TEXT NOT NULL,
        department TEXT NOT NULL,
        lookup_key TEXT NOT NULL,
        UNIQUE(department, lookup_key)
    )""")
    if proposed_subject_id is not None:
        try:
            proposed_subject_id = int(proposed_subject_id)
        except (TypeError, ValueError):
            proposed_subject_id = None
        if proposed_subject_id is not None and proposed_subject_id <= 0:
            proposed_subject_id = None
        if proposed_subject_id is not None:
            subject_by_id = conn.execute(
                "SELECT id,subject_name,department FROM lecture_subjects WHERE id=?",
                (proposed_subject_id,),
            ).fetchone()
            if subject_by_id:
                if str(subject_by_id[2]) != department and not allow_department_mismatch:
                    raise ValueError("The subject ID is not in the selected department.")
                return int(subject_by_id[0]), str(subject_by_id[1])

    key = _subject_lookup_key(subject_name)
    if not key:
        raise ValueError("The subject_id is not registered; provide a subject name to register it.")

    canonical_name = subject_name.strip()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "lecture_schedule" in tables:
        schedule_subjects = conn.execute(
            "SELECT DISTINCT subject FROM lecture_schedule WHERE department=? AND subject IS NOT NULL",
            (department,),
        ).fetchall()
        for (scheduled_name,) in schedule_subjects:
            scheduled_name = str(scheduled_name or "").strip()
            scheduled_key = _subject_lookup_key(scheduled_name)
            if scheduled_key == key:
                canonical_name = scheduled_name
                break

    subject_row = conn.execute(
        "SELECT id,subject_name FROM lecture_subjects WHERE department=? AND lookup_key=?",
        (department, key),
    ).fetchone()
    if subject_row is None:
        catalog_row = conn.execute(
            "SELECT id,name FROM subjects WHERE department_id=? AND lookup_key=?",
            (department_id_for(department), key),
        ).fetchone()
        if catalog_row is not None:
            conn.execute("UPDATE subjects SET is_active=1 WHERE id=?", (int(catalog_row[0]),))
            legacy_id_row = conn.execute(
                "SELECT subject_name,department,lookup_key FROM lecture_subjects WHERE id=?",
                (int(catalog_row[0]),),
            ).fetchone()
            if legacy_id_row is None:
                conn.execute(
                    "INSERT INTO lecture_subjects(id,subject_name,department,lookup_key) VALUES(?,?,?,?)",
                    (int(catalog_row[0]), str(catalog_row[1]), department, key),
                )
            elif (str(legacy_id_row[1]) != department or str(legacy_id_row[2]) != key):
                raise ValueError("Subject ID conflict between catalog tables; refusing to allocate a new ID.")
            return int(catalog_row[0]), str(catalog_row[1])
        if proposed_subject_id is not None:
            occupied_id = conn.execute(
                "SELECT id FROM lecture_subjects WHERE id=?", (proposed_subject_id,)
            ).fetchone()
            if occupied_id is None:
                conn.execute(
                    "INSERT INTO lecture_subjects(id,subject_name,department,lookup_key) VALUES(?,?,?,?)",
                    (proposed_subject_id, canonical_name, department, key),
                )
                return int(proposed_subject_id), canonical_name
        cursor = conn.execute(
            "INSERT INTO lecture_subjects(subject_name,department,lookup_key) VALUES(?,?,?)",
            (canonical_name, department, key),
        )
        return int(cursor.lastrowid), canonical_name
    return int(subject_row[0]), str(subject_row[1])


DEFAULT_CYBER_SUBJECTS = (
    "Fundamentals of Cybersecurity",
    "Electrical Circuit Analysis II",
    "Communication I",
    "Applied Numerical Methods and Statistical Analysis",
    "Chemistry",
    "Crimes of the Baath Regime in Iraq",
    "Mathematics III",
)

DEFAULT_AI_SUBJECTS = (
    "Mathematics III",
    "Introduction to AI",
    "Electronics I",
    "Communication Systems",
    "Object-Oriented Programming (OOP)",
    "English Language II",
    "Crimes of the Baath Regime",
)


def _sync_subject_catalog(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT id,subject_name,department,lookup_key FROM lecture_subjects").fetchall():
        department_name = str(row[2])
        department_id = department_id_for(department_name)
        conn.execute(
            """INSERT INTO subjects(id,name,department_id,department,lookup_key,is_active)
               VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,department_id=excluded.department_id,
                 department=excluded.department,lookup_key=excluded.lookup_key,is_active=1""",
            (int(row[0]), str(row[1]), department_id, department_name, str(row[3])),
        )


def _seed_department_subjects(conn: sqlite3.Connection) -> None:
    for department, subject_names in (
        (DEPARTMENTS[0], DEFAULT_CYBER_SUBJECTS),
        (DEPARTMENTS[1], DEFAULT_AI_SUBJECTS),
    ):
        for subject_name in subject_names:
            _resolve_subject_id(conn, subject_name, department)
    _sync_subject_catalog(conn)
    _sync_schedule_contract(conn)


def _configured_subject_name(department_id: str, value: str | None) -> str | None:
    key = _subject_lookup_key(value or "")
    if not key:
        return None
    configured = DEFAULT_AI_SUBJECTS if department_id == "AI" else DEFAULT_CYBER_SUBJECTS
    for subject_name in configured:
        if _subject_lookup_key(subject_name) == key:
            return subject_name
    aliases = {
        ("Cybersecurity", _subject_lookup_key("Applied Numerical Methods & Statistical Analysis")):
            "Applied Numerical Methods and Statistical Analysis",
        ("AI", _subject_lookup_key("Artificial Intelligence Fundamentals")): "Introduction to AI",
        ("AI", _subject_lookup_key("Crimes of the Baath Regime in Iraq")): "Crimes of the Baath Regime",
    }
    return aliases.get((department_id, key))


def _purge_cross_department_lecture_links(conn: sqlite3.Connection) -> None:
    """Remove lecture records whose explicit subject link belongs to another department."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    contaminated_ids: set[int] = set()
    for table in ("materials", "lectures"):
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not {"id", "subject_id", "department_id"}.issubset(columns):
            continue
        subject_column = "subject" if "subject" in columns else None
        if not subject_column:
            continue
        for row in conn.execute(
            f"""SELECT records.id,records.subject_id,records.department_id,records.subject,
                       subject.department_id AS subject_department_id
                FROM {table} AS records
                LEFT JOIN subjects AS subject ON subject.id=records.subject_id
                WHERE 1=1"""
        ).fetchall():
            record_id, subject_id, department_id, subject_name, subject_department_id = row
            if subject_id is not None:
                if subject_department_id != department_id:
                    contaminated_ids.add(int(record_id))
                continue
            subject_key = _subject_lookup_key(subject_name)
            if not subject_key:
                continue
            own_subject = conn.execute(
                "SELECT id FROM subjects WHERE department_id=? AND lookup_key=? LIMIT 1",
                (department_id, subject_key),
            ).fetchone()
            if own_subject:
                conn.execute(f"UPDATE {table} SET subject_id=? WHERE id=?", (int(own_subject[0]), record_id))
                continue
            foreign_subject = conn.execute(
                "SELECT id FROM subjects WHERE department_id<>? AND lookup_key=? LIMIT 1",
                (department_id, subject_key),
            ).fetchone()
            if foreign_subject:
                contaminated_ids.add(int(record_id))
    for lecture_id in contaminated_ids:
        conn.execute("DELETE FROM materials WHERE id=?", (lecture_id,))
        conn.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))


@app.post("/api/subjects/resolve")
def resolve_lecture_subject(payload: dict = Body(...), api_key: str | None = Header(None, alias="X-API-Key")):
    if not LECTURE_INGEST_API_KEY:
        raise HTTPException(status_code=503, detail="Lecture ingestion is not configured.")
    if not lecture_api_key_matches((api_key or "").strip()):
        raise HTTPException(status_code=401, detail="Invalid API Key")
    department = str(payload.get("department") or payload.get("department_name") or os.getenv(
        "DEFAULT_LECTURE_DEPARTMENT", "\u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a"
    )).strip()[:120]
    subject_name = str(payload.get("subject") or payload.get("subject_name") or "").strip()[:200]
    subject_id_input = payload.get("subject_id")
    if not department or (not subject_name and subject_id_input is None):
        raise HTTPException(status_code=422, detail="A department and subject name or subject_id are required.")
    try:
        ensure_materials_table()
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
            subject_id, subject_name = _resolve_subject_id(
                conn, subject_name, department, proposed_subject_id=subject_id_input
            )
            _sync_subject_catalog(conn)
        return {"status": "success", "subject_id": subject_id, "subject": subject_name,
                "department": department}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.error("Could not resolve lecture subject: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not resolve lecture subject.") from exc


@app.get("/api/subjects")
def list_lecture_subjects(department_id: str | None = None, api_key: str | None = Header(None, alias="X-API-Key")):
    if not LECTURE_INGEST_API_KEY:
        raise HTTPException(status_code=503, detail="Lecture ingestion is not configured.")
    if not lecture_api_key_matches((api_key or "").strip()):
        raise HTTPException(status_code=401, detail="Invalid API Key")
    try:
        ensure_academic_schema()
        canonical_id = _canonical_department_id(department_id) if department_id else None
        if department_id and canonical_id is None:
            raise HTTPException(status_code=400, detail="Unknown department_id.")
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            if canonical_id:
                subjects = conn.execute(
                    """SELECT subject.id,subject.name,subject.department,subject.department_id,subject.topic_id,
                              COALESCE(NULLIF(subject.doctor_name,''),(SELECT COALESCE(NULLIF(trim(ls.doctor_name),''),NULLIF(trim(ls.instructor),''),ls.professor_name)
                                FROM lecture_schedule ls WHERE ls.department_id=subject.department_id AND lower(trim(ls.subject))=lower(trim(subject.name))
                                ORDER BY ls.is_active DESC,ls.id LIMIT 1)) AS doctor_name,
                              COALESCE(NULLIF(subject.doctor_email,''),(SELECT ls.doctor_email FROM lecture_schedule ls
                                WHERE ls.department_id=subject.department_id AND lower(trim(ls.subject))=lower(trim(subject.name))
                                ORDER BY ls.is_active DESC,ls.id LIMIT 1)) AS doctor_email
                       FROM subjects AS subject WHERE subject.is_active=1 AND subject.department_id=? """
                    "ORDER BY name COLLATE NOCASE,id", (canonical_id,)
                ).fetchall()
            else:
                subjects = conn.execute(
                    """SELECT subject.id,subject.name,subject.department,subject.department_id,subject.topic_id,
                              subject.doctor_name,subject.doctor_email FROM subjects AS subject WHERE subject.is_active=1
                       ORDER BY subject.department COLLATE NOCASE,subject.name COLLATE NOCASE,subject.id"""
                ).fetchall()
        return {"status": "success", "subjects": [dict(row) for row in subjects]}
    except sqlite3.Error as exc:
        logger.error("Could not list lecture subjects: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not list lecture subjects.") from exc


@app.put("/api/subjects/{subject_id}/topic")
def link_subject_topic(
    subject_id: int,
    payload: dict = Body(...),
    api_key: str | None = Header(None, alias="X-API-Key"),
):
    if not LECTURE_INGEST_API_KEY:
        raise HTTPException(status_code=503, detail="Lecture ingestion is not configured.")
    if not lecture_api_key_matches((api_key or "").strip()):
        raise HTTPException(status_code=401, detail="Invalid API Key")
    try:
        topic_id = int(payload.get("topic_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="A numeric topic_id is required.")
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        cursor = conn.execute("UPDATE subjects SET topic_id=? WHERE id=?", (topic_id, subject_id))
        conn.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Subject not found.")
    return {"status": "success", "subject_id": subject_id, "topic_id": topic_id}


@app.get("/api/catalog/subjects")
@app.get("/subjects")
def list_subject_catalog(request: Request, department_id: str | None = None):
    actor = require_authenticated_user(request)
    ensure_academic_schema()
    is_admin = str(actor.get("role") or "").casefold() in {"admin", "super_admin"}
    own_department_id = department_id_for(actor.get("department_id") or actor.get("department") or actor.get("class_id"))
    if not is_admin:
        if not actor.get("department_id") and not actor.get("department") and not actor.get("class_id"):
            raise HTTPException(status_code=403, detail="This account has no department assigned.")
        if department_id and department_id_for(department_id) != own_department_id:
            raise HTTPException(status_code=403, detail="This account cannot view another department's subjects.")
        department_id = own_department_id
    elif department_id:
        if _canonical_department_id(department_id) is None:
            raise HTTPException(status_code=400, detail="Unknown department_id.")
        department_id = department_id_for(department_id)

    ensure_materials_table()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        if department_id:
            rows = conn.execute(
                """SELECT subject.id,subject.name,subject.department,subject.department_id,
                          COALESCE(NULLIF(subject.doctor_name,''),(SELECT COALESCE(NULLIF(trim(ls.doctor_name),''),NULLIF(trim(ls.instructor),''),ls.professor_name)
                            FROM lecture_schedule ls WHERE ls.department_id=subject.department_id AND lower(trim(ls.subject))=lower(trim(subject.name))
                            ORDER BY ls.is_active DESC,ls.id LIMIT 1)) AS doctor_name,
                          COALESCE(NULLIF(subject.doctor_email,''),(SELECT ls.doctor_email FROM lecture_schedule ls
                            WHERE ls.department_id=subject.department_id AND lower(trim(ls.subject))=lower(trim(subject.name))
                            ORDER BY ls.is_active DESC,ls.id LIMIT 1)) AS doctor_email
                   FROM subjects AS subject WHERE subject.is_active=1 AND subject.department_id=? ORDER BY subject.name COLLATE NOCASE,subject.id""",
                (department_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,name,department,department_id,doctor_name,doctor_email FROM subjects WHERE is_active=1 ORDER BY department COLLATE NOCASE,name COLLATE NOCASE,id"
            ).fetchall()
    return {"status": "success", "subjects": [dict(row) for row in rows]}


@app.post("/api/lectures/add")
def add_lecture_from_bot(payload: dict = Body(...), api_key: str | None = Header(None, alias="X-API-Key")):
    if not LECTURE_INGEST_API_KEY:
        raise HTTPException(status_code=503, detail="Lecture ingestion is not configured.")
    supplied_key = (api_key or "").strip()
    if not lecture_api_key_matches(supplied_key):
        raise HTTPException(status_code=401, detail="Invalid API Key")

    original_file_name = Path(str(payload.get("file_name") or payload.get("title") or "")).name.strip()
    title = str(payload.get("title") or payload.get("lecture_title") or original_file_name).strip()[:300]
    subject = str(payload.get("subject") or payload.get("subject_name") or "").strip()[:200]
    proposed_subject_id = payload.get("subject_id")
    drive_url = str(payload.get("drive_url") or payload.get("google_drive_url") or "").strip()
    drive_download_url = str(payload.get("google_drive_download_url") or "").strip()
    drive_file_id = str(payload.get("drive_file_id") or "").strip()
    telegram_backup_url = str(payload.get("telegram_backup_url") or "").strip() or None
    telegram_file_id = str(payload.get("telegram_file_id") or "").strip()
    department = str(payload.get("department") or payload.get("department_name") or os.getenv(
        "DEFAULT_LECTURE_DEPARTMENT", "\u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a"
    )).strip()[:120]
    try:
        request_chat_id = int(payload.get("chat_id"))
    except (TypeError, ValueError):
        request_chat_id = None
    bot_department = str(payload.get("bot_department") or "").strip().casefold()
    is_ai_telegram_bot = bot_department in {"ai", "artificial intelligence", "\u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a"}
    is_cybersecurity_telegram_group = request_chat_id == TELEGRAM_CYBERSECURITY_CHAT_ID and not is_ai_telegram_bot
    if is_cybersecurity_telegram_group:
        department = CYBERSECURITY_DEPARTMENT_NAME
    if is_ai_telegram_bot and proposed_subject_id is None:
        raise HTTPException(status_code=422, detail="The AI bot must provide a subject ID.")
    upload_date = str(payload.get("created_at") or payload.get("upload_date") or datetime.now(timezone.utc).isoformat()).strip()

    if not original_file_name or Path(original_file_name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=422, detail="A PDF file name is required.")
    if not subject or not department or not is_google_drive_url(drive_url):
        raise HTTPException(status_code=422, detail="A subject, department, and valid Google Drive URL are required.")
    if not drive_file_id:
        parsed_drive_url = urlparse(drive_url)
        drive_file_id = (parse_qs(parsed_drive_url.query).get("id") or [""])[0]
        if not drive_file_id:
            match = re.search(r"/file/d/([^/]+)", parsed_drive_url.path)
            drive_file_id = match.group(1) if match else ""
    if not drive_file_id:
        raise HTTPException(status_code=422, detail="A Google Drive file ID is required.")
    if drive_download_url and not is_google_drive_url(drive_download_url):
        raise HTTPException(status_code=422, detail="Invalid Google Drive download URL.")
    if telegram_backup_url and urlparse(telegram_backup_url).scheme != "https":
        raise HTTPException(status_code=422, detail="Invalid Telegram backup URL.")
    if not telegram_file_id:
        telegram_file_id = hashlib.sha256(f"{original_file_name}|{drive_url}".encode("utf-8")).hexdigest()
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(original_file_name).stem).strip("_") or "lecture"
    unique_suffix = hashlib.sha256(telegram_file_id.encode("utf-8")).hexdigest()[:12]
    file_name = f"{safe_stem}_{unique_suffix}.pdf"

    subject_id = None
    try:
        ensure_materials_table()
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            subject_tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='lecture_subjects'"
            )}
            if proposed_subject_id is not None and "lecture_subjects" in subject_tables:
                try:
                    requested_subject_id = int(proposed_subject_id)
                except (TypeError, ValueError):
                    requested_subject_id = None
                if is_ai_telegram_bot and requested_subject_id is None:
                    raise HTTPException(status_code=422, detail="The AI bot must provide a valid subject ID.")
                if requested_subject_id is not None:
                    catalog_subject = conn.execute(
                        "SELECT subject_name,department FROM lecture_subjects WHERE id=?",
                        (requested_subject_id,),
                    ).fetchone()
                    if catalog_subject:
                        if is_ai_telegram_bot and normalize_department_name(str(catalog_subject["department"])) != DEPARTMENTS[1]:
                            raise HTTPException(status_code=422, detail="The subject ID does not belong to the AI department.")
                        # The configured Telegram group always records lectures under its required department.
                        subject = str(catalog_subject["subject_name"])
                        if not is_cybersecurity_telegram_group:
                            department = str(catalog_subject["department"])
                    elif is_ai_telegram_bot:
                        raise HTTPException(status_code=422, detail="The subject ID does not belong to the AI department.")
            subject_id, subject = _resolve_subject_id(
                conn, subject, department, proposed_subject_id=proposed_subject_id,
                allow_department_mismatch=is_cybersecurity_telegram_group,
            )
            _sync_subject_catalog(conn)
            existing = conn.execute(
                "SELECT id,drive_url,drive_file_id,subject_id,department,department_id FROM materials WHERE telegram_file_id=?", (telegram_file_id,)
            ).fetchone()
            if existing:
                return {"status": "success", "duplicate": True, "database_saved": True,
                        "id": existing["id"], "drive_file_id": existing["drive_file_id"] or drive_file_id,
                        "subject_id": existing["subject_id"] or subject_id, "subject": subject,
                        "department": existing["department"] or department,
                        "department_id": existing["department_id"] or department_id_for(department),
                        "google_drive_url": existing["drive_url"]}
            cursor = conn.execute(
                """INSERT INTO materials
                   (title,file_name,uploaded_by,created_at,subject,department,department_id,group_name,file_url,upload_date,
                    telegram_file_id,drive_url,drive_download_url,telegram_backup_url,topic_id,file_size,drive_file_id,subject_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    title, file_name, "telegram-lecture-bot", upload_date, subject,
                    department, department_id_for(department),
                    str(payload.get("group_name") or "")[:40],
                    drive_url, upload_date, telegram_file_id, drive_url,
                    drive_download_url or None, telegram_backup_url,
                    int(payload["topic_id"]) if payload.get("topic_id") is not None else None,
                    int(payload["file_size"]) if payload.get("file_size") is not None else None,
                    drive_file_id,
                    subject_id,
                ),
            )
            lecture_id = cursor.lastrowid
            conn.execute(
                """INSERT INTO lectures
                   (id,title,subject,department,department_id,drive_url,telegram_url,topic_id,created_at,telegram_file_id,drive_file_id,subject_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lecture_id, title, subject, department, department_id_for(department), drive_url, telegram_backup_url,
                 int(payload["topic_id"]) if payload.get("topic_id") is not None else None,
                 upload_date, telegram_file_id, drive_file_id, subject_id),
            )
            return {"status": "success", "duplicate": False, "database_saved": True,
                    "id": cursor.lastrowid, "drive_file_id": drive_file_id, "subject_id": subject_id,
                    "subject": subject, "department": department, "department_id": department_id_for(department), "created_at": upload_date,
                    "google_drive_url": drive_url}
    except sqlite3.Error as exc:
        logger.error("Drive upload succeeded, but SQLite lecture indexing failed: %s", exc, exc_info=True)
        return {"status": "success", "database_saved": False, "database_warning": "Lecture metadata could not be indexed.",
                "database_error": str(exc),
                "drive_file_id": drive_file_id, "subject_id": subject_id,
                "created_at": upload_date, "google_drive_url": drive_url}
    except Exception as exc:
        logger.error("Drive upload succeeded, but lecture indexing failed unexpectedly: %s", exc, exc_info=True)
        return {"status": "success", "database_saved": False, "database_warning": "Lecture metadata could not be indexed.",
                "database_error": str(exc),
                "drive_file_id": drive_file_id, "subject_id": subject_id,
                "created_at": upload_date, "google_drive_url": drive_url}


@app.post("/api/lectures/exists")
def lecture_record_exists(payload: dict = Body(...), api_key: str | None = Header(None, alias="X-API-Key")):
    if not LECTURE_INGEST_API_KEY:
        raise HTTPException(status_code=503, detail="Lecture ingestion is not configured.")
    supplied_key = (api_key or "").strip()
    if not lecture_api_key_matches(supplied_key):
        raise HTTPException(status_code=401, detail="Invalid API Key")

    telegram_file_id = str(payload.get("telegram_file_id") or "").strip()
    if not telegram_file_id:
        raise HTTPException(status_code=422, detail="Telegram file ID is required.")
    ensure_materials_table()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        row = conn.execute(
            "SELECT id FROM materials WHERE telegram_file_id=?", (telegram_file_id,)
        ).fetchone()
    return {"status": "success", "exists": row is not None}

def telegram_configured():
    if not TELEGRAM_BOT_TOKEN:
        return False
    if TELEGRAM_CHAT_ID or TELEGRAM_GROUP_B_CHAT_ID:
        return True
    try:
        mapping = json.loads(os.getenv("TELEGRAM_GROUP_CHAT_IDS", "{}"))
    except json.JSONDecodeError:
        mapping = {}
    configured_mapping = any(
        (value.get("chat_id") or value.get("group_chat_id")) if isinstance(value, dict) else value
        for value in mapping.values()
    )
    if configured_mapping:
        return True
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("groups", "departments"):
                if table not in tables:
                    continue
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                chat_columns = columns & {"chat_id", "telegram_chat_id", "group_chat_id"}
                if not chat_columns:
                    continue
                checks = " OR ".join(f'"{column}" IS NOT NULL AND "{column}" != ?' for column in chat_columns)
                if conn.execute(f'SELECT 1 FROM "{table}" WHERE {checks}', ("",) * len(chat_columns)).fetchone():
                    return True
    except sqlite3.Error:
        pass
    return False


def cybersecurity_attendance_chat_id() -> str:
    """Return the first configured cybersecurity attendance destination."""
    for name in ("CYBER_ATTENDANCE_CHAT_ID", "CYBER_CHAT_ID", "TELEGRAM_CYBERSECURITY_CHAT_ID"):
        value = os.getenv(name, "").strip()
        if value:
            if value.upper().startswith("-100XXXXXXXXXX") or "X" in value.upper():
                logger.warning("%s appears to be a placeholder Telegram chat ID: %r", name, value)
            return value
    logger.warning(
        "No cybersecurity attendance chat ID configured; set CYBER_ATTENDANCE_CHAT_ID, "
        "CYBER_CHAT_ID, or TELEGRAM_CYBERSECURITY_CHAT_ID."
    )
    return ""


def masked_bot_token(token: str) -> str:
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:5]}…{token[-3:]}"


async def send_telegram_message(text: str, chat_id: str | None = None, message_thread_id: int | None = None):
    if not telegram_configured():
        return False
    async with httpx.AsyncClient(timeout=12) as client:
        payload = {"chat_id": chat_id or TELEGRAM_CHAT_ID or TELEGRAM_GROUP_B_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        response = await client.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)
        response.raise_for_status()
        return bool(response.json().get("ok"))


async def send_attendance_telegram_message(
    text: str, department_name: str,
) -> bool:
    department = normalize_department_name(department_name)
    if department == DEPARTMENTS[0] or "cyber" in str(department_name).casefold() or "سيبراني" in str(department_name):
        token = os.getenv("CYBER_ATTENDANCE_BOT_TOKEN", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        target_chat_id = (
            os.getenv("CYBER_ATTENDANCE_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CYBERSECURITY_CHAT_ID", "").strip()
        )
        route_name = "Cybersecurity"
    elif department == DEPARTMENTS[1] or "artificial intelligence" in str(department_name).casefold() or "الذكاء الاصطناعي" in str(department_name):
        token = os.getenv("AI_ATTENDANCE_BOT_TOKEN", "").strip()
        target_chat_id = os.getenv("AI_ATTENDANCE_CHAT_ID", "").strip()
        route_name = "AI"
    else:
        logger.error("Attendance Telegram send skipped: unrecognized department %r", department_name)
        return False
    if not token or not target_chat_id:
        logger.error(
            "Attendance Telegram send skipped for %s: bot token or chat ID is not configured (chat_id=%r)",
            route_name, target_chat_id,
        )
        return False
    logger.info(
        "Sending attendance Telegram message: bot_token=%s target_chat_id=%s",
        masked_bot_token(token), target_chat_id,
    )
    payload = {"chat_id": target_chat_id, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        if response.is_error:
            logger.error(
                "Telegram attendance sendMessage failed: HTTP %s %s, body=%s",
                response.status_code, response.reason_phrase, response.text,
            )
            response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            logger.error(
                "Telegram attendance sendMessage rejected: HTTP %s, body=%s",
                response.status_code, response.text,
            )
            return False
        logger.info(
            "Attendance Telegram response: HTTP %s %s, body=%s",
            response.status_code, response.reason_phrase, response.text,
        )
        logger.info("Attendance notification sent to %s Telegram chat %s", route_name, target_chat_id)
        return True
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Telegram attendance HTTP error for chat %s: status=%s, body=%s",
            target_chat_id, exc.response.status_code, exc.response.text,
        )
        return False
    except Exception:
        logger.exception("Telegram attendance message delivery failed for chat %s", target_chat_id)
        return False

def group_chat_id(department_name: str, group_name: str):
    """Resolve attendance chat/topic from saved group data, then configuration."""
    if normalize_department_name(department_name) == DEPARTMENTS[0] and normalize_group_name(group_name) == "B":
        if TELEGRAM_GROUP_B_CHAT_ID:
            topic_id = TELEGRAM_GROUP_B_TOPIC_ID or os.getenv("TELEGRAM_TOPIC_ID", "")
            return TELEGRAM_GROUP_B_CHAT_ID, int(topic_id) if topic_id else None
    db_target = None
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            department_keys = {department_name.casefold(), "cybersecurity"} if department_name == DEPARTMENTS[0] else {department_name.casefold()}
            for table in ("groups", "departments"):
                if table not in tables:
                    continue
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                for row in rows:
                    values = dict(row)
                    normalized = {str(key).casefold(): value for key, value in values.items()}
                    db_department = next((normalized[key] for key in ("department", "department_name", "name", "title") if normalized.get(key) is not None), None)
                    db_group = next((normalized[key] for key in ("group_name", "group", "code") if normalized.get(key) is not None), None)
                    if db_department is not None and str(db_department).casefold() not in department_keys:
                        continue
                    if table == "groups" and db_group is not None and str(db_group).casefold() != group_name.casefold():
                        continue
                    if table == "groups" and db_group is None:
                        continue
                    chat = next((normalized[key] for key in ("chat_id", "telegram_chat_id", "group_chat_id") if normalized.get(key) not in (None, "")), None)
                    topic = next((normalized[key] for key in ("topic_id", "message_thread_id", "telegram_topic_id") if normalized.get(key) not in (None, "")), None)
                    if chat is not None:
                        db_target = (str(chat), int(topic) if topic is not None else None)
                        break
                if db_target:
                    break
    except (sqlite3.Error, ValueError, TypeError):
        db_target = None
    if db_target:
        return db_target[0], db_target[1] or (int(os.environ["TELEGRAM_TOPIC_ID"]) if os.getenv("TELEGRAM_TOPIC_ID") else None)

    try:
        mapping = json.loads(os.getenv("TELEGRAM_GROUP_CHAT_IDS", "{}"))
    except json.JSONDecodeError:
        mapping = {}
    target = mapping.get(f"{department_name}|{group_name}")
    if target is None and department_name == DEPARTMENTS[0]:
        target = (
            mapping.get(f"CyberSecurity|{group_name}")
            or mapping.get(f"هندسة الأمن السيبراني|{group_name}")
            or mapping.get("CyberSecurity")
            or mapping.get("هندسة الأمن السيبراني")
        )
    if isinstance(target, dict):
        chat_id = target.get("chat_id") or target.get("group_chat_id") or TELEGRAM_CHAT_ID
        topic_id = target.get("topic_id") or target.get("message_thread_id")
        return str(chat_id), int(topic_id) if topic_id is not None else None
    fallback_topic = os.getenv("TELEGRAM_TOPIC_ID")
    if department_name == DEPARTMENTS[0]:
        cyber_chat_id = cybersecurity_attendance_chat_id()
        return str(target or cyber_chat_id or ATTENDANCE_CHAT_ID or TELEGRAM_CHAT_ID), int(fallback_topic) if fallback_topic else None
    return str(target or ATTENDANCE_CHAT_ID or TELEGRAM_CHAT_ID), int(fallback_topic) if fallback_topic else None

async def send_telegram_otp(otp_code: str, department_name: str, group_name: str, subject_name: str):
    current_slot = today_schedule(department_name, group_name)
    lecture_time = format_time_12h(current_slot[2]) if current_slot else ""
    message = (
        f"Lecture time: {lecture_time}\n"
        "----------------------------------------------\n"
        "⚠️ <b>رمز تأكيد الحضور (OTP)</b>\n"
        f"🏛️ <b>القسم:</b> {department_name}\n"
        f"👥 <b>الكروب:</b> {group_name}\n"
        f"📚 <b>المادة:</b> {subject_name}\n"
        f"🔑 <b>الرمز:</b> <code>{otp_code}</code>\n\n"
        "⏱️ صلاحية الرمز: 3 دقائق من تاريخ الإرسال.\n"
        "----------------------------------------------"
    )
    return await send_attendance_telegram_message(message, department_name)

async def create_and_send_otp(department_name: str, group_name: str, subject_name: str):
    code = f"{secrets.randbelow(10000):04d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    session_id = secrets.token_urlsafe(12)
    telegram_sent = False
    try:
        telegram_sent = await send_telegram_otp(code, department_name, group_name, subject_name)
    except Exception:
        logger.exception("Telegram OTP delivery failed; keeping generated OTP active for UI fallback")
    key = f"{department_name}|{group_name}"
    ACTIVE_OTPS[key] = {"code": code, "expires_at": expires_at, "session_id": session_id, "attempts": 0, "subject": subject_name}
    SESSION_DEVICE_BINDINGS[session_id] = {}
    SESSION_ATTENDANCE[session_id] = set()
    return {"session_id": session_id, "expires_at": expires_at, "otp": code, "telegram_sent": telegram_sent}


async def attendance_otp_scheduler_loop() -> None:
    """Dispatch each scheduled lecture's OTP at its start time in Baghdad time."""
    poll_seconds = max(10, int(os.getenv("ATTENDANCE_OTP_POLL_SECONDS", "15")))
    while True:
        try:
            now = datetime.now(APP_TIMEZONE)
            today = now.date().isoformat()
            with closing(sqlite3.connect(DATABASE_PATH)) as conn:
                conn.row_factory = sqlite3.Row
                due_rows = conn.execute(
                    """SELECT s.id,s.department_id,s.group_name,s.subject_name,
                              COALESCE(o.new_time, s.start_time) AS start_time
                       FROM schedules AS s
                       LEFT JOIN schedule_overrides AS o
                         ON o.schedule_id=s.id AND o.lecture_date=?
                       LEFT JOIN scheduled_otp_log AS sent
                         ON sent.schedule_id=s.id AND sent.lecture_date=?
                       WHERE s.day_of_week=? AND sent.schedule_id IS NULL
                         AND COALESCE(o.status, 'active')='active'
                       ORDER BY s.department_id,s.group_name,s.id""",
                    (today, today, now.weekday()),
                ).fetchall()
            for row in due_rows:
                try:
                    slot_time = datetime.strptime(str(row["start_time"]), "%H:%M").time()
                except ValueError:
                    logger.error("Ignoring invalid schedule start time %r for schedule_id=%s", row["start_time"], row["id"])
                    continue
                scheduled_at = datetime.combine(now.date(), slot_time).replace(tzinfo=APP_TIMEZONE)
                if scheduled_at > now or now - scheduled_at > timedelta(minutes=2):
                    continue
                department_name = DEPARTMENTS[1] if row["department_id"] == "AI" else DEPARTMENTS[0]
                group_code = normalize_group_name(row["group_name"])
                try:
                    session = await create_and_send_otp(
                        department_name, group_code, str(row["subject_name"] or "")
                    )
                    if not session.get("telegram_sent"):
                        logger.error(
                            "Scheduled OTP delivery failed; will retry while this start minute is active "
                            "(schedule_id=%s, department=%s, group=%s)",
                            row["id"], department_name, group_code,
                        )
                        continue
                    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO scheduled_otp_log(schedule_id,lecture_date,sent_at) VALUES(?,?,?)",
                            (row["id"], today, now.isoformat()),
                        )
                        conn.commit()
                except Exception:
                    logger.exception("Could not dispatch scheduled OTP (schedule_id=%s)", row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Attendance OTP scheduler iteration failed")
        await asyncio.sleep(poll_seconds)


@app.on_event("startup")
async def start_attendance_otp_scheduler() -> None:
    global ATTENDANCE_OTP_SCHEDULER_TASK
    if ATTENDANCE_OTP_SCHEDULER_TASK is None or ATTENDANCE_OTP_SCHEDULER_TASK.done():
        ATTENDANCE_OTP_SCHEDULER_TASK = asyncio.create_task(attendance_otp_scheduler_loop())
        logger.info("Attendance OTP scheduler started (timezone=%s)", APP_TIMEZONE.key)


@app.on_event("shutdown")
async def stop_attendance_otp_scheduler() -> None:
    global ATTENDANCE_OTP_SCHEDULER_TASK
    if ATTENDANCE_OTP_SCHEDULER_TASK is not None:
        ATTENDANCE_OTP_SCHEDULER_TASK.cancel()
        try:
            await ATTENDANCE_OTP_SCHEDULER_TASK
        except asyncio.CancelledError:
            pass
        ATTENDANCE_OTP_SCHEDULER_TASK = None

@app.get("/api/lectures")
@app.get("/lectures")
@app.get("/materials")
@app.get("/materials/")
def list_materials(
    request: Request,
    department: str | None = None,
    department_name: str | None = None,
    group_name: str | None = None,
):
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        actor = require_authenticated_user(request)
        role = str(actor["role"]).casefold()
        is_admin = role in {"admin", "super_admin"}
        own_department_id = department_id_for(actor.get("department_id") or actor.get("department") or actor.get("class_id"))
        if not is_admin:
            if not actor.get("department_id") and not actor.get("department") and not actor.get("class_id"):
                raise HTTPException(status_code=403, detail="This account has no department assigned.")
            if department_name or department:
                requested = department_id_for(department_name or department)
                if requested != own_department_id:
                    raise HTTPException(status_code=403, detail="This account cannot view another department's lectures.")
            department_id = own_department_id
        else:
            requested_filter = department_name or department
            department_id = department_id_for(requested_filter) if requested_filter else None
        sql = "SELECT * FROM materials WHERE 1=1"
        params = []
        if department_id:
            sql += " AND department_id=?"
            params.append(department_id)
        if group_name:
            sql += " AND group_name=?"
            params.append(normalize_group_name(group_name))
        sql += " ORDER BY id DESC"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        materials_list = []
        for row in rows:
            row_dict = dict(row)
            materials_list.append({
                "id": row_dict.get("id"),
                "department": row_dict.get("department") or "",
                "department_id": row_dict.get("department_id") or "",
                "group_name": row_dict.get("group_name") or "",
                "subject": row_dict.get("subject") or row_dict.get("subject_name") or "الأمن السيبراني",
                "title": row_dict.get("title") or row_dict.get("file_name") or "ملزمة دراسية",
                "file_name": row_dict.get("file_name") or "",
                "file_url": row_dict.get("file_url") or "#",
                "drive_url": row_dict.get("drive_url") or "",
                "drive_download_url": row_dict.get("drive_download_url") or "",
                "download_url": f"/materials/{row_dict.get('id')}/download" if row_dict.get("id") is not None else (row_dict.get("drive_download_url") or row_dict.get("file_url") or "#"),
                "upload_date": str(row_dict.get("upload_date") or row_dict.get("created_at") or ""),
            })
        return {"status": "success", "materials": materials_list}
    except HTTPException:
        raise
    except Exception as exc:
        traceback.print_exc()
        print(f"[MATERIALS API] Could not load materials: {exc}")
        return {"status": "success", "materials": []}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post("/lectures/upload")
async def upload_lecture_from_main_view(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    subject: str = Form(...),
    department: str = Form(...),
    group_name: str = Form(...),
):
    actor = require_authenticated_user(request)
    if str(actor.get("role") or "").casefold() not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Only admins can upload lectures from the web interface.")
    department_name = normalize_department_name(department)
    group_name = normalize_group_name(group_name)
    require_department_manager(request, department_name)
    if department_name not in DEPARTMENTS or group_name not in GROUPS:
        raise HTTPException(status_code=400, detail="Choose a valid department and group.")
    if not str(file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Only PDF files can be uploaded.")
    contents = await file.read(25 * 1024 * 1024 + 1)
    if len(contents) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF files must be 25 MB or smaller.")
    if not contents.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF.")

    ensure_materials_table()
    stored_name = f"{secrets.token_hex(16)}.pdf"
    stored_path = MATERIALS_DIR / stored_name
    await run_in_threadpool(stored_path.write_bytes, contents)
    file_url = f"/static/materials/{stored_name}"
    created_at = datetime.now(APP_TIMEZONE).isoformat()
    clean_title = str(title).strip()[:250] or Path(file.filename or stored_name).stem
    clean_subject = str(subject).strip()[:200]
    if not clean_subject:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Subject is required.")
    department_id = department_id_for(department_name)
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            subject_id, clean_subject = _resolve_subject_id(conn, clean_subject, department_name)
            _sync_subject_catalog(conn)
            cursor = conn.execute(
                """INSERT INTO materials
                   (title,file_name,uploaded_by,created_at,subject,department,department_id,group_name,
                    file_url,upload_date,drive_url,drive_download_url,file_size,subject_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (clean_title, stored_name, actor.get("email"), created_at, clean_subject, department_name,
                 department_id, group_name, file_url, created_at, file_url, file_url, len(contents), subject_id),
            )
            material_id = cursor.lastrowid
            conn.execute(
                """INSERT OR REPLACE INTO lectures
                   (id,title,subject,department,department_id,drive_url,created_at,subject_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (material_id, clean_title, clean_subject, department_name, department_id, file_url, created_at, subject_id),
            )
            conn.commit()
    except Exception:
        stored_path.unlink(missing_ok=True)
        logger.exception("Could not save uploaded lecture metadata")
        raise HTTPException(status_code=500, detail="Could not save the uploaded lecture.")
    return {"status": "success", "id": material_id, "title": clean_title, "file_url": file_url}


@app.get("/materials/{material_id}/download")
async def download_material_pdf(material_id: int, request: Request):
    """Serve a lecture PDF from this origin so the frontend can cache it offline."""
    actor = require_authenticated_user(request)
    is_admin = str(actor["role"]).casefold() in {"admin", "super_admin"}
    actor_department_id = actor.get("department_id") or department_id_for(actor.get("department") or actor.get("class_id"))
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT file_name,title,file_url,drive_url,drive_download_url,department_id FROM materials WHERE id=?",
            (material_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Lecture file not found.")
    if not is_admin and row["department_id"] != actor_department_id:
        raise HTTPException(status_code=403, detail="This lecture belongs to another department.")

    filename = str(row["file_name"] or row["title"] or f"lecture-{material_id}.pdf")
    local_url = str(row["file_url"] or "")
    local_path = urlparse(local_url).path
    if local_path.startswith("/static/materials/"):
        candidate = (MATERIALS_DIR / Path(local_path).name).resolve()
        if candidate.is_file() and candidate.parent == MATERIALS_DIR.resolve():
            return FileResponse(candidate, media_type="application/pdf", filename=filename)

    download_url = str(row["drive_download_url"] or "").strip()
    drive_url = str(row["drive_url"] or local_url or "").strip()
    if not download_url:
        parsed = urlparse(drive_url)
        file_id = (parse_qs(parsed.query).get("id") or [""])[0]
        if not file_id:
            match = re.search(r"/file/d/([^/]+)", parsed.path)
            file_id = match.group(1) if match else ""
        if file_id:
            download_url = f"https://drive.google.com/uc?export=download&id={file_id}"

    parsed_download = urlparse(download_url)
    if parsed_download.scheme != "https" or parsed_download.hostname not in {"drive.google.com", "docs.google.com"}:
        raise HTTPException(status_code=404, detail="A downloadable PDF URL is not available.")

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            upstream = await client.get(download_url)
        upstream.raise_for_status()
        content_type = upstream.headers.get("content-type", "").lower()
        if "pdf" not in content_type and not upstream.content.startswith(b"%PDF-"):
            raise HTTPException(status_code=502, detail="Google Drive did not return a PDF file.")
        return Response(
            content=upstream.content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=lecture.pdf; filename*=UTF-8''{quote(Path(filename).name)}"},
        )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Could not proxy PDF download for lecture %s: %s", material_id, exc)
        raise HTTPException(status_code=502, detail="Could not download this PDF from Google Drive.") from exc

# ط°ط§ظƒط±ط© ظ…ط¤ظ‚طھط© ظ„ط­ظپط¸ ط§ظ„ظ€ OTP ط§ظ„ط®ط§طµ ط¨ظƒظ„ ظ‚ط§ط¹ط©
ACTIVE_OTPS = {}
ACTIVE_SESSIONS = {}
SESSION_DEVICE_BINDINGS = {}
SESSION_ATTENDANCE = {}
DEPARTMENTS = ("هندسة الأمن السيبراني", "هندسة الذكاء الاصطناعي")
GROUPS = ("A", "B")
REPRESENTATIVE_ROLES = ("rep", "assistant", "deputy_rep", "representative", "group_rep")
DEPARTMENT_MANAGER_ROLES = {"rep", "assistant"}
MAIN_REPRESENTATIVE_ROLES = ("rep", "representative", "group_rep")
logger = logging.getLogger(__name__)
ADMIN_SESSION_COOKIE = "sajjilni_session"
ADMIN_SESSION_TTL_SECONDS = 12 * 60 * 60

def normalize_department_name(value: str | None):
    normalized = (value or "").strip() or "CyberSecurity"
    if normalized.casefold() in {"cybersecurity", "أمن سيبراني".casefold(), "هندسة الشبكات والأمن السيبراني".casefold()}:
        return DEPARTMENTS[0]
    if normalized.casefold() in {
        "ai", "ai_department", "ai_robotics", "ai/robotics", "artificial intelligence", "artificial_intelligence", "robotics",
        "هندسة الذكاء الاصطناعي والروبوتات".casefold(),
    }:
        return DEPARTMENTS[1]
    return normalized


def department_id_for(value: str | None) -> str:
    return "AI" if normalize_department_name(value) == DEPARTMENTS[1] else "Cybersecurity"


def _canonical_department_id(value: str | None, department_name: str | None = None) -> str | None:
    """Return a canonical department ID only when the row can be attributed safely."""
    resolved = []
    for candidate in (value, department_name):
        raw = str(candidate or "").strip()
        if not raw:
            continue
        key = raw.casefold().replace(" ", "_")
        if key in {"ai", "ai_department", "ai_robotics", "ai/robotics", "artificial_intelligence", "robotics"}:
            resolved.append("AI")
            continue
        normalized = normalize_department_name(raw)
        if normalized == DEPARTMENTS[1]:
            resolved.append("AI")
            continue
        if key in {"cybersecurity", "cyber_security", "cybersecurity_department"} or normalized == DEPARTMENTS[0]:
            resolved.append("Cybersecurity")
    if not resolved or len(set(resolved)) > 1:
        return None
    return resolved[0]


def _clean_department_records(conn: sqlite3.Connection) -> None:
    """Canonicalize isolated rows and remove records whose department cannot be resolved."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("subjects", "lectures", "materials", "lecture_schedule", "schedules"):
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "department_id" not in columns:
            if table == "schedules":
                conn.execute("DELETE FROM schedules")
            continue
        label_column = "department" if "department" in columns else None
        id_column = "id" if "id" in columns else "rowid"
        group_column = "group_name" if table in {"lecture_schedule", "schedules"} and "group_name" in columns else None
        selected_columns = [id_column, "department_id"]
        if label_column:
            selected_columns.append(label_column)
        if group_column:
            selected_columns.append(group_column)
        selected = ",".join(selected_columns)
        for row in conn.execute(f"SELECT {selected} FROM {table}").fetchall():
            values = dict(zip(selected_columns, row))
            row_id, department_id = values[id_column], values["department_id"]
            department_name = values.get(label_column) if label_column else None
            canonical_id = _canonical_department_id(department_id, department_name)
            group_code = None
            if group_column:
                raw_group = str(values.get(group_column) or "").strip()
                group_code = normalize_group_name(raw_group) if raw_group else None
                if group_code not in GROUPS:
                    canonical_id = None
            if canonical_id is None:
                if table == "subjects":
                    conn.execute("UPDATE subjects SET is_active=0 WHERE id=?", (row_id,))
                    continue
                if table == "lecture_schedule":
                    if "schedule_overrides" in tables:
                        conn.execute("DELETE FROM schedule_overrides WHERE schedule_id=?", (row_id,))
                    if "scheduled_otp_log" in tables:
                        conn.execute("DELETE FROM scheduled_otp_log WHERE schedule_id=?", (row_id,))
                conn.execute(f"DELETE FROM {table} WHERE {id_column}=?", (row_id,))
                continue
            if str(department_id or "") != canonical_id:
                conn.execute(f"UPDATE {table} SET department_id=? WHERE {id_column}=?", (canonical_id, row_id))
            if table == "subjects" and label_column:
                canonical_name = DEPARTMENTS[1] if canonical_id == "AI" else DEPARTMENTS[0]
                if str(department_name or "") != canonical_name:
                    conn.execute(f"UPDATE {table} SET department=? WHERE {id_column}=?", (canonical_name, row_id))
            if group_column:
                stored_group = f"Group {group_code}" if table == "schedules" else group_code
                if str(values[group_column]) != stored_group:
                    conn.execute(f"UPDATE {table} SET group_name=? WHERE {id_column}=?", (stored_group, row_id))

    if "lecture_subjects" in tables:
        for row in conn.execute("SELECT id,department FROM lecture_subjects").fetchall():
            canonical_id = _canonical_department_id(None, row[1])
            if canonical_id is None:
                continue
            canonical_name = DEPARTMENTS[1] if canonical_id == "AI" else DEPARTMENTS[0]
            if str(row[1]) != canonical_name:
                duplicate = conn.execute(
                    "SELECT id FROM lecture_subjects WHERE id<>? AND department=? AND lookup_key=? ORDER BY id LIMIT 1",
                    (row[0], canonical_name,
                     conn.execute("SELECT lookup_key FROM lecture_subjects WHERE id=?", (row[0],)).fetchone()[0]),
                ).fetchone()
                if duplicate is None:
                    conn.execute("UPDATE lecture_subjects SET department=? WHERE id=?", (canonical_name, row[0]))


def _upsert_schedule_contract(conn: sqlite3.Connection, schedule_id: int, values: dict) -> None:
    department_id = department_id_for(values["department"])
    group_name = f"Group {normalize_group_name(values['group_name'])}"
    conn.execute(
        """INSERT INTO schedules(id,department_id,group_name,day_of_week,start_time,end_time,
           subject_name,doctor_name,room_number) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET department_id=excluded.department_id,group_name=excluded.group_name,
           day_of_week=excluded.day_of_week,start_time=excluded.start_time,end_time=excluded.end_time,
           subject_name=excluded.subject_name,doctor_name=excluded.doctor_name,room_number=excluded.room_number""",
        (schedule_id, department_id, group_name, values["weekday"], values["start_time"],
         values["end_time"], values["subject"], values["doctor_name"], values["room"]),
    )


def _sync_schedule_contract(conn: sqlite3.Connection) -> None:
    """Mirror active legacy schedule slots into the normalized schedules table."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "lecture_schedule" not in tables or "schedules" not in tables:
        return
    rows = conn.execute(
        "SELECT id,weekday,start_time,end_time,department,department_id,group_name,subject,"
        "COALESCE(NULLIF(trim(doctor_name),''),instructor),room FROM lecture_schedule WHERE is_active=1"
    ).fetchall()
    active_ids = set()
    for row in rows:
        canonical_id = _canonical_department_id(row[5], row[4])
        if canonical_id is None:
            continue
        active_ids.add(int(row[0]))
        _upsert_schedule_contract(conn, int(row[0]), {
            "department": DEPARTMENTS[1] if canonical_id == "AI" else DEPARTMENTS[0],
            "group_name": row[6], "weekday": row[1], "start_time": row[2], "end_time": row[3],
            "subject": row[7], "doctor_name": row[8], "room": row[9],
        })
    for row in conn.execute("SELECT id FROM schedules").fetchall():
        if int(row[0]) not in active_ids:
            conn.execute("DELETE FROM schedules WHERE id=?", (row[0],))

def normalize_group_name(value: str | None):
    normalized = (value or "B").strip().upper()
    for group, arabic_letter in (("A", "ا"), ("B", "ب")):
        if normalized in {
            group, f"GROUP {group}", f"كروب {group}", f"كروب {arabic_letter}",
            f"المجموعة {group}", f"المجموعة {arabic_letter}", f"مجموعة {group}", f"مجموعة {arabic_letter}",
        }:
            return group
    if not normalized:
        return "B"
    return normalized

TOPIC_SUBJECT_MAP = {
    7: {"subject": "أساسيات الأمن السيبراني", "department": "هندسة الأمن السيبراني", "group": "B"},
    8: {"subject": "جرائم حزب البعث", "department": "هندسة الأمن السيبراني", "group": "B"},
    9: {"subject": "كيمياء", "department": "هندسة الأمن السيبراني", "group": "B"},
    10: {"subject": "الإحصاء الهندسي", "department": "هندسة الأمن السيبراني", "group": "B"},
    11: {"subject": "تحليل الدوائر الكهربائية", "department": "هندسة الأمن السيبراني", "group": "B"},
    12: {"subject": "كيمياء", "department": "هندسة الأمن السيبراني", "group": "A"},
    13: {"subject": "الإحصاء الهندسي", "department": "هندسة الأمن السيبراني", "group": "A"},
    14: {"subject": "تحليل الدوائر الكهربائية", "department": "هندسة الأمن السيبراني", "group": "A"},
}

def ensure_academic_schema():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    if "department" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN department TEXT")
    if "group_name" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN group_name TEXT")
    if "department_id" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN department_id TEXT")
    if "created_at" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN created_at DATETIME")
    if "display_role" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN display_role TEXT")
    if "college" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN college TEXT")
    if "stage" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN stage TEXT")
    if "is_first_login" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_first_login INTEGER NOT NULL DEFAULT 1")
    attendance_columns = {row[1] for row in cursor.execute("PRAGMA table_info(attendance)").fetchall()}
    if "department" not in attendance_columns:
        cursor.execute("ALTER TABLE attendance ADD COLUMN department TEXT")
    if "group_name" not in attendance_columns:
        cursor.execute("ALTER TABLE attendance ADD COLUMN group_name TEXT")
    if "snapshot_url" not in attendance_columns:
        cursor.execute("ALTER TABLE attendance ADD COLUMN snapshot_url TEXT")
    cursor.execute("UPDATE users SET department = ? WHERE department IS NULL AND class_id IN ('Cybersecurity','CyberSecurity')", (DEPARTMENTS[0],))
    cursor.execute("UPDATE users SET department = ? WHERE department IS NULL AND lower(role) NOT IN ('admin','super_admin')", (DEPARTMENTS[0],))
    cursor.execute("UPDATE users SET department_id = COALESCE(department_id, class_id)")
    cursor.execute("UPDATE users SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)")
    cursor.execute("UPDATE users SET group_name = 'A' WHERE group_name IS NULL AND lower(role) NOT IN ('admin','super_admin')")
    cursor.execute("UPDATE attendance SET department = ? WHERE department IS NULL", (DEPARTMENTS[0],))
    cursor.execute("UPDATE attendance SET group_name = 'A' WHERE group_name IS NULL")
    cursor.execute("""CREATE TABLE IF NOT EXISTS lecture_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT, weekday INTEGER NOT NULL, start_time TEXT NOT NULL,
        department TEXT NOT NULL, department_id TEXT, group_name TEXT NOT NULL, subject TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
        end_time TEXT, lecture_type TEXT, instructor TEXT, doctor_name TEXT, professor_name TEXT, room TEXT,
        UNIQUE(weekday, start_time, department, group_name)
    )""")
    schedule_columns = {row[1] for row in cursor.execute("PRAGMA table_info(lecture_schedule)").fetchall()}
    for column in ("end_time", "lecture_type", "instructor", "doctor_name", "professor_name", "room", "stage", "doctor_email", "department_code", "department_id"):
        if column not in schedule_columns:
            cursor.execute(f"ALTER TABLE lecture_schedule ADD COLUMN {column} TEXT")
    cursor.execute("UPDATE lecture_schedule SET doctor_name=COALESCE(NULLIF(trim(doctor_name),''),NULLIF(trim(instructor),''),professor_name)")
    cursor.execute("UPDATE lecture_schedule SET instructor=COALESCE(NULLIF(trim(instructor),''),doctor_name,professor_name)")
    cursor.execute("UPDATE lecture_schedule SET professor_name=COALESCE(NULLIF(trim(professor_name),''),doctor_name,instructor)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS schedule_overrides (
        schedule_id INTEGER NOT NULL, lecture_date TEXT NOT NULL, new_time TEXT,
        status TEXT NOT NULL DEFAULT 'active', PRIMARY KEY(schedule_id, lecture_date),
        FOREIGN KEY(schedule_id) REFERENCES lecture_schedule(id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS scheduled_otp_log (
        schedule_id INTEGER NOT NULL, lecture_date TEXT NOT NULL, sent_at TEXT NOT NULL,
        PRIMARY KEY(schedule_id, lecture_date)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY,
        department_id TEXT NOT NULL CHECK(department_id IN ('Cybersecurity','AI')),
        group_name TEXT NOT NULL CHECK(group_name IN ('Group A','Group B')),
        day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
        start_time TEXT NOT NULL,
        end_time TEXT,
        subject_name TEXT NOT NULL,
        doctor_name TEXT,
        room_number TEXT
    )""")
    _clean_department_records(conn)
    _sync_schedule_contract(conn)
    for user_row in cursor.execute("SELECT id,department_id,class_id,department FROM users").fetchall():
        canonical_id = (_canonical_department_id(user_row[1], user_row[3])
                        or _canonical_department_id(user_row[2], user_row[3]))
        if canonical_id:
            canonical_name = DEPARTMENTS[1] if canonical_id == "AI" else DEPARTMENTS[0]
            class_id = user_row[2]
            if _canonical_department_id(class_id) is not None:
                class_id = canonical_id
            cursor.execute(
                "UPDATE users SET department_id=?,department=?,class_id=? WHERE id=?",
                (canonical_id, canonical_name, class_id, user_row[0]),
            )
    cursor.execute("""INSERT INTO users (name,email,password,role,class_id,device_uuid,department,group_name)
        VALUES (?,?,'student123','student','Cybersecurity',NULL,?,?)
        ON CONFLICT(email) DO UPDATE SET name=excluded.name, password=excluded.password, role='student',
        class_id='Cybersecurity', department=excluded.department, group_name=excluded.group_name""",
        ("علي علي", "student@uob.edu.iq", DEPARTMENTS[0], "B"))
    conn.commit()
    conn.close()
    ensure_materials_table()

def _delete_material_rows(conn: sqlite3.Connection, material_ids: list[int]) -> int:
    if not material_ids:
        return 0
    placeholders = ",".join("?" for _ in material_ids)
    rows = conn.execute(f"SELECT file_name FROM materials WHERE id IN ({placeholders})", material_ids).fetchall()
    conn.execute(f"DELETE FROM lectures WHERE id IN ({placeholders})", material_ids)
    cursor = conn.execute(f"DELETE FROM materials WHERE id IN ({placeholders})", material_ids)
    for (file_name,) in rows:
        if file_name:
            path = (MATERIALS_DIR / Path(str(file_name)).name).resolve()
            if path.parent == MATERIALS_DIR.resolve() and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    logger.warning("Could not remove local material file %s", path, exc_info=True)
    return cursor.rowcount


def delete_lecture(lecture_id: int, request: Request):
    actor = require_authenticated_user(request)
    ensure_materials_table()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        material = conn.execute("SELECT id,department,department_id FROM materials WHERE id=?", (lecture_id,)).fetchone()
        lecture = conn.execute("SELECT id,department FROM lectures WHERE id=?", (lecture_id,)).fetchone()
        record = material or lecture
        if record is None:
            raise HTTPException(status_code=404, detail="Lecture not found.")
        department = str((record["department_id"] if "department_id" in record.keys() else None) or record["department"] or "").strip()
        if str(actor["role"]).casefold() not in {"admin", "super_admin"} and not department:
            raise HTTPException(status_code=403, detail="This lecture has no department assigned.")
        require_department_manager(request, department)
        _delete_material_rows(conn, [lecture_id])
        conn.commit()
    return {"status": "success", "deleted_lecture_id": lecture_id}


@app.put("/lectures/{lecture_id}")
def update_lecture_metadata(lecture_id: int, request: Request, payload: dict = Body(...)):
    ensure_materials_table()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        material = conn.execute("SELECT id,department,department_id FROM materials WHERE id=?", (lecture_id,)).fetchone()
        if material is None:
            raise HTTPException(status_code=404, detail="Lecture not found.")
        require_department_manager(request, material["department_id"] or material["department"])
        title = str(payload.get("title") or "").strip()[:250]
        subject = str(payload.get("subject") or "").strip()[:200]
        group_name = normalize_group_name(str(payload.get("group_name") or ""))
        if not title or not subject or group_name not in GROUPS:
            raise HTTPException(status_code=400, detail="Title, subject, and a valid group are required.")
        subject_id, subject = _resolve_subject_id(conn, subject, str(material["department"] or material["department_id"]))
        _sync_subject_catalog(conn)
        conn.execute("UPDATE materials SET title=?,subject=?,group_name=?,subject_id=? WHERE id=?", (title, subject, group_name, subject_id, lecture_id))
        conn.execute("UPDATE lectures SET title=?,subject=?,subject_id=? WHERE id=?", (title, subject, subject_id, lecture_id))
        conn.commit()
    return {"status": "success", "id": lecture_id, "title": title, "subject": subject, "group_name": group_name}


@app.delete("/lectures/department/{dept_id}")
def clear_department_lectures(dept_id: str, request: Request, group_name: str | None = None):
    department = normalize_department_name(dept_id)
    if department not in DEPARTMENTS:
        raise HTTPException(status_code=400, detail="Unknown department.")
    require_department_manager(request, department)
    ensure_materials_table()
    department_id = department_id_for(department)
    group_filter = normalize_group_name(group_name) if group_name else None
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        ids: set[int] = set()
        material_rows = conn.execute("SELECT id,group_name FROM materials WHERE department_id=?", (department_id,)).fetchall()
        ids.update(int(row[0]) for row in material_rows if not group_filter or normalize_group_name(row[1]) == group_filter)
        if not group_filter:
            ids.update(int(row[0]) for row in conn.execute("SELECT id FROM lectures WHERE department_id=?", (department_id,)).fetchall())
        material_ids = sorted(ids)
        deleted = _delete_material_rows(conn, material_ids)
        conn.commit()
    return {"status": "success", "department": department, "group_name": group_filter, "deleted_count": max(deleted, len(material_ids))}


# Register the variable-ID route after the fixed department route so "department" is not parsed as an ID.
app.delete("/lectures/{lecture_id}")(delete_lecture)


def _subject_and_doctor(conn: sqlite3.Connection, department: str, subject: str, subject_id=None):
    department_id = department_id_for(department)
    row = None
    if subject_id not in (None, ""):
        try:
            row = conn.execute(
                "SELECT id,name,doctor_name,doctor_email FROM subjects WHERE id=? AND department_id=? AND is_active=1",
                (int(subject_id), department_id),
            ).fetchone()
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid subject_id.")
        if row is None:
            raise HTTPException(status_code=400, detail="Selected subject does not belong to this department.")
        subject = row[1]
    else:
        row = conn.execute(
            "SELECT id,name,doctor_name,doctor_email FROM subjects WHERE department_id=? AND lookup_key=? AND is_active=1 LIMIT 1",
            (department_id, _subject_lookup_key(subject)),
        ).fetchone()
    doctor_name = (row[2] or "").strip() if row else ""
    doctor_email = (row[3] or "").strip() if row else ""
    if not doctor_name or not doctor_email:
        mapped = conn.execute(
            """SELECT COALESCE(NULLIF(trim(doctor_name),''),NULLIF(trim(instructor),''),professor_name),doctor_email
               FROM lecture_schedule WHERE department_id=? AND lower(trim(subject))=lower(trim(?))
                 AND (COALESCE(NULLIF(trim(doctor_name),''),NULLIF(trim(instructor),''),professor_name) IS NOT NULL
                      OR doctor_email IS NOT NULL)
               ORDER BY is_active DESC,id LIMIT 1""",
            (department_id, subject),
        ).fetchone()
        if mapped:
            doctor_name = doctor_name or str(mapped[0] or "").strip()
            doctor_email = doctor_email or str(mapped[1] or "").strip()
            if row and (doctor_name or doctor_email):
                conn.execute(
                    "UPDATE subjects SET doctor_name=COALESCE(NULLIF(doctor_name,''),?),doctor_email=COALESCE(NULLIF(doctor_email,''),?) WHERE id=?",
                    (doctor_name or None, doctor_email or None, row[0]),
                )
    return subject, doctor_name or None, doctor_email or None


def _validated_schedule_values(payload: dict, existing: sqlite3.Row | None = None, conn: sqlite3.Connection | None = None) -> dict:
    def value(name, default=""):
        old_value = existing[name] if existing is not None and name in existing.keys() else default
        return str(payload.get(name, old_value) or "").strip()
    submitted_department = payload.get("department") or payload.get("department_id")
    if submitted_department is None and existing is not None:
        submitted_department = existing["department"]
    department = normalize_department_name(str(submitted_department or DEPARTMENTS[0]).strip())
    group_name = normalize_group_name(value("group_name", "B"))
    subject = str(payload.get("subject") or payload.get("subject_name") or
                  (existing["subject"] if existing is not None else "") or "").strip()
    try:
        weekday = int(payload.get("weekday", payload.get("day_of_week", existing["weekday"] if existing is not None else -1)))
        if weekday < 0 or weekday > 6:
            raise ValueError
        start_time = datetime.strptime(value("start_time"), "%H:%M").strftime("%H:%M")
        end_time = value("end_time")
        if end_time:
            end_time = datetime.strptime(end_time, "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Provide weekday 0–6 and times in HH:MM format.")
    if department not in DEPARTMENTS or group_name not in GROUPS or not subject:
        raise HTTPException(status_code=400, detail="Department, group, and subject are required.")
    doctor_name = doctor_email = None
    if conn is not None:
        subject, doctor_name, doctor_email = _subject_and_doctor(
            conn, department, subject, payload.get("subject_id")
        )
    if not doctor_name and existing is not None and subject == str(existing["subject"] or "").strip():
        doctor_name = (existing["doctor_name"] or existing["instructor"] or "").strip() or None
        doctor_email = existing["doctor_email"]
    return {"weekday": weekday, "start_time": start_time, "end_time": end_time or None,
            "department": department, "group_name": group_name, "subject": subject[:200],
            "lecture_type": value("lecture_type")[:100] or None, "instructor": doctor_name, "doctor_name": doctor_name,
            "doctor_email": doctor_email,
            "room": str(payload.get("room") or payload.get("room_number") or
                        (existing["room"] if existing is not None else "") or "").strip()[:100] or None,
            "stage": value("stage", "Second Stage")[:100] or "Second Stage"}


@app.get("/api/schedules")
@app.get("/schedules")
def get_schedules(
    request: Request,
    department: str | None = None,
    department_id: str | None = None,
    group_name: str | None = None,
):
    actor = require_authenticated_user(request)
    ensure_academic_schema()
    role = str(actor["role"]).casefold()
    is_admin = role in {"admin", "super_admin"}
    current_department_id = department_id_for(actor.get("department_id") or actor.get("department") or actor.get("class_id"))
    requested_department = department_id or department
    if requested_department and _canonical_department_id(requested_department) is None:
        raise HTTPException(status_code=400, detail="Unknown department_id.")
    if not is_admin:
        if not actor.get("department_id") and not actor.get("department") and not actor.get("class_id"):
            raise HTTPException(status_code=403, detail="This account has no department assigned.")
        if requested_department and department_id_for(requested_department) != current_department_id:
            raise HTTPException(status_code=403, detail="This account cannot view another department's schedule.")
        department_id = current_department_id
    else:
        department_id = department_id_for(requested_department) if requested_department else None
    sql = "SELECT id,department_id,group_name,day_of_week,start_time,end_time,subject_name,doctor_name,room_number FROM schedules WHERE 1=1"
    params = []
    if department_id:
        sql += " AND department_id=?"
        params.append(department_id)
    if group_name:
        sql += " AND group_name=?"
        params.append(f"Group {normalize_group_name(group_name)}")
    sql += " ORDER BY department_id,group_name,day_of_week,start_time,id"
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            item["department"] = DEPARTMENTS[1] if item["department_id"] == "AI" else DEPARTMENTS[0]
            item["weekday"] = item.get("day_of_week")
            item["subject"] = item.get("subject_name")
            item["room"] = item.get("room_number")
            item["instructor"] = item.get("doctor_name")
            item["group_code"] = normalize_group_name(item.get("group_name"))
            item["group_label"] = item["group_name"]
            item["start_time"] = format_time_12h(item.get("start_time"))
            item["end_time"] = format_time_12h(item.get("end_time"))
            rows.append(item)
        return {"schedules": rows}


@app.post("/schedules")
def create_schedule(request: Request, payload: dict = Body(...)):
    ensure_academic_schema()
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            values = _validated_schedule_values(payload, conn=conn)
            manager = require_department_manager(request, values["department"])
            if str(manager.get("role") or "").casefold() not in {"admin", "super_admin"} and (not manager.get("group_name") or normalize_group_name(values["group_name"]) != normalize_group_name(manager.get("group_name"))):
                raise HTTPException(status_code=403, detail="You can only manage schedule slots for your assigned group.")
            cursor = conn.execute("""INSERT INTO lecture_schedule
                (weekday,start_time,end_time,department,department_id,group_name,subject,lecture_type,instructor,doctor_name,professor_name,room,stage,department_code,doctor_email,is_active)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (values["weekday"], values["start_time"], values["end_time"], values["department"], department_id_for(values["department"]), values["group_name"],
                 values["subject"], values["lecture_type"], values["instructor"], values["doctor_name"], values["doctor_name"], values["room"], values["stage"],
                 SCHEDULE_DEPARTMENT_CODES.get(values["department"], values["department"]), values["doctor_email"]))
            _upsert_schedule_contract(conn, int(cursor.lastrowid), values)
            conn.commit()
            return {"status": "success", "schedule_id": cursor.lastrowid}
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="A schedule slot already exists at that time.") from exc


@app.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, request: Request, payload: dict = Body(...)):
    require_authenticated_user(request)
    ensure_academic_schema()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute("SELECT * FROM lecture_schedule WHERE id=?", (schedule_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Schedule slot not found.")
        manager = require_department_manager(request, existing["department"])
        if str(manager.get("role") or "").casefold() not in {"admin", "super_admin"} and (not manager.get("group_name") or normalize_group_name(existing["group_name"]) != normalize_group_name(manager.get("group_name"))):
            raise HTTPException(status_code=403, detail="You cannot edit a schedule slot outside your assigned group.")
        values = _validated_schedule_values(payload, existing, conn)
        manager = require_department_manager(request, values["department"])
        if str(manager.get("role") or "").casefold() not in {"admin", "super_admin"} and (not manager.get("group_name") or normalize_group_name(values["group_name"]) != normalize_group_name(manager.get("group_name"))):
            raise HTTPException(status_code=403, detail="You cannot move a schedule slot outside your assigned group.")
        try:
            conn.execute("""UPDATE lecture_schedule SET weekday=?,start_time=?,end_time=?,department=?,group_name=?,subject=?,
                lecture_type=?,instructor=?,doctor_name=?,professor_name=?,room=?,stage=?,department_code=?,department_id=?,doctor_email=? WHERE id=?""",
                (values["weekday"], values["start_time"], values["end_time"], values["department"], values["group_name"],
                 values["subject"], values["lecture_type"], values["instructor"], values["doctor_name"], values["doctor_name"], values["room"], values["stage"],
                 SCHEDULE_DEPARTMENT_CODES.get(values["department"], values["department"]), department_id_for(values["department"]), values["doctor_email"], schedule_id))
            _upsert_schedule_contract(conn, schedule_id, values)
            conn.commit()
            return {"status": "success", "schedule_id": schedule_id}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A schedule slot already exists at that time.") from exc


@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, request: Request):
    require_authenticated_user(request)
    ensure_academic_schema()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        row = conn.execute("SELECT department FROM lecture_schedule WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Schedule slot not found.")
        manager = require_department_manager(request, row[0])
        if str(manager.get("role") or "").casefold() not in {"admin", "super_admin"}:
            schedule_group = conn.execute("SELECT group_name FROM lecture_schedule WHERE id=?", (schedule_id,)).fetchone()[0]
            if not manager.get("group_name") or normalize_group_name(schedule_group) != normalize_group_name(manager.get("group_name")):
                raise HTTPException(status_code=403, detail="You cannot delete a schedule slot outside your assigned group.")
        conn.execute("DELETE FROM schedule_overrides WHERE schedule_id=?", (schedule_id,))
        conn.execute("DELETE FROM scheduled_otp_log WHERE schedule_id=?", (schedule_id,))
        conn.execute("DELETE FROM lecture_schedule WHERE id=?", (schedule_id,))
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        conn.commit()
    return {"status": "success", "deleted_schedule_id": schedule_id}


def _read_department_for_user(request: Request, requested_department: str | None) -> str:
    actor = require_authenticated_user(request)
    role = str(actor.get("role") or "").casefold()
    if role in {"admin", "super_admin"}:
        department = normalize_department_name(requested_department) if requested_department else DEPARTMENTS[0]
    else:
        department_id = actor.get("department_id") or actor.get("department") or actor.get("class_id")
        if not department_id:
            raise HTTPException(status_code=403, detail="This account has no department assigned.")
        own_department = normalize_department_name(department_id)
        if requested_department and normalize_department_name(requested_department) != own_department:
            raise HTTPException(status_code=403, detail="This account cannot view another department's schedule.")
        department = own_department
    if department not in DEPARTMENTS:
        raise HTTPException(status_code=400, detail="Unknown department.")
    return department


def today_schedule(department_name: str, group_name: str, now=None):
    local_now = now or datetime.now(APP_TIMEZONE)
    ensure_academic_schema()
    conn = sqlite3.connect(DATABASE_PATH)
    row = conn.execute("""SELECT s.id, s.subject, COALESCE(o.new_time, s.start_time), COALESCE(o.status, 'active'),
        COALESCE(NULLIF(trim(s.doctor_name),''),s.instructor)
        FROM lecture_schedule s LEFT JOIN schedule_overrides o
        ON o.schedule_id=s.id AND o.lecture_date=?
        WHERE s.weekday=? AND s.department_id=? AND s.group_name=? AND s.is_active=1
        ORDER BY s.start_time LIMIT 1""",
        (local_now.date().isoformat(), local_now.weekday(), department_id_for(department_name), group_name)).fetchone()
    conn.close()
    return row

def get_subject_for_today(department_name: str, group_name: str):
    slot = today_schedule(department_name, group_name)
    return slot[1] if slot else None

def representative_authorized(email: str, department_name: str | None = None, group_name: str | None = None):
    conn = sqlite3.connect(DATABASE_PATH)
    row = conn.execute("SELECT role,department,group_name,class_id FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not row or row[0] not in REPRESENTATIVE_ROLES:
        return False
    owned_department = normalize_department_name(row[1] or row[3])
    owned_group = normalize_group_name(row[2])
    return (department_name is None or normalize_department_name(department_name) == owned_department) and \
        (group_name is None or normalize_group_name(group_name) == owned_group)

@app.get("/schedule/today")
def schedule_today(request: Request, department_name: str | None = None, group_name: str = "B"):
    department_name = _read_department_for_user(request, department_name)
    group_name = normalize_group_name(group_name)
    if department_name not in DEPARTMENTS or group_name not in GROUPS:
        return {"status": "error", "message": "اختر قسماً وكروباً صحيحين."}
    slot = today_schedule(department_name, group_name)
    if not slot:
        return {"status": "empty", "message": "لا توجد محاضرة مجدولة لهذا القسم والكروب اليوم."}
    return {"status": slot[3], "subject": slot[1], "time": format_time_12h(slot[2]), "doctor_name": slot[4], "instructor": slot[4]}

@app.get("/today_lectures")
def list_today_lectures(request: Request, department_name: str | None = None, group_name: str = "B"):
    """Return today's class list, falling back to the department's available subjects."""
    department_name = _read_department_for_user(request, department_name)
    group_name = normalize_group_name(group_name)
    if department_name not in DEPARTMENTS:
        department_name = DEPARTMENTS[0]
    if group_name not in GROUPS:
        group_name = "B"
    ensure_academic_schema()
    today = datetime.now(APP_TIMEZONE)
    date_key = today.date().isoformat()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        rows = conn.execute(
            """SELECT s.subject, COALESCE(o.new_time, s.start_time), COALESCE(o.status, 'active'),
                      s.end_time, s.lecture_type, COALESCE(NULLIF(trim(s.doctor_name),''),s.instructor), s.room
               FROM lecture_schedule s LEFT JOIN schedule_overrides o
               ON o.schedule_id=s.id AND o.lecture_date=?
               WHERE s.weekday=? AND s.department_id=? AND s.group_name=? AND s.is_active=1
               ORDER BY COALESCE(o.new_time, s.start_time)""",
            (date_key, today.weekday(), department_id_for(department_name), group_name),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                """SELECT subject, start_time, 'active', end_time, lecture_type, COALESCE(NULLIF(trim(doctor_name),''),instructor), room FROM lecture_schedule
                   WHERE department_id=? AND group_name=? AND is_active=1
                   GROUP BY subject ORDER BY start_time""",
                (department_id_for(department_name), group_name),
            ).fetchall()
    if not rows and today.weekday() == 2 and department_name == DEPARTMENTS[0] and group_name == "B":
        rows = [
            ("Communication I", "08:30", "active", "10:30", "مختبر عملي", "د. محمد طلال", "Lab"),
            ("Object-Oriented Programming (OOP)", "10:30", "active", "12:30", "مختبر عملي", "د. علي", "Lab"),
            ("Mathematics III", "12:30", "active", "14:30", "محاضرة نظري", "د. قبس", "C-04C123"),
        ]
    if not rows and department_name == DEPARTMENTS[0]:
        rows = [
            ("Fundamentals of Cybersecurity", "08:30", "active", "10:30", "محاضرة نظري", "م.م. نيرمين مجيد", "C-04C123"),
            ("Object-Oriented Programming (OOP)", "10:30", "active", "12:30", "محاضرة نظري", "د. علي", "C-04C123"),
        ]
    lectures = [{"subject": row[0], "time": format_time_12h(row[1]), "status": row[2], "end_time": format_time_12h(row[3]),
                 "type": row[4], "instructor": row[5], "doctor_name": row[5], "room": row[6]} for row in rows]
    return {"status": "success", "lectures": lectures}


@app.get("/schedule/next")
def next_scheduled_lecture(request: Request, department_name: str | None = None, group_name: str = "B"):
    """Return the next active lecture after the current local time, if any."""
    department_name = _read_department_for_user(request, department_name)
    group_name = normalize_group_name(group_name)
    if department_name not in DEPARTMENTS or group_name not in GROUPS:
        return {"status": "success", "next_lecture": None, "message": "لا توجد محاضرات قادمة حالياً"}

    now = datetime.now(APP_TIMEZONE)
    today = now.date()
    ensure_academic_schema()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT s.subject, COALESCE(o.new_time, s.start_time) AS start_time,
                      s.end_time, s.lecture_type, COALESCE(NULLIF(trim(s.doctor_name),''),s.instructor) AS doctor_name, s.room
               FROM lecture_schedule s LEFT JOIN schedule_overrides o
                 ON o.schedule_id=s.id AND o.lecture_date=?
               WHERE s.weekday=? AND s.department_id=? AND s.group_name=? AND s.is_active=1
                 AND COALESCE(o.status, 'active')='active'
                 AND time(COALESCE(o.new_time, s.start_time)) > time(?)
               ORDER BY time(COALESCE(o.new_time, s.start_time)), s.id LIMIT 1""",
            (today.isoformat(), now.weekday(), department_id_for(department_name), group_name, now.strftime("%H:%M:%S")),
        ).fetchone()
        day_offset = 0
        lecture_date = today
        if row is None:
            for offset in range(1, 8):
                lecture_date = today + timedelta(days=offset)
                row = conn.execute(
                    """SELECT s.subject, COALESCE(o.new_time, s.start_time) AS start_time,
                              s.end_time, s.lecture_type, COALESCE(NULLIF(trim(s.doctor_name),''),s.instructor) AS doctor_name, s.room
                       FROM lecture_schedule s LEFT JOIN schedule_overrides o
                         ON o.schedule_id=s.id AND o.lecture_date=?
                       WHERE s.weekday=? AND s.department_id=? AND s.group_name=? AND s.is_active=1
                         AND COALESCE(o.status, 'active')='active'
                       ORDER BY time(COALESCE(o.new_time, s.start_time)), s.id LIMIT 1""",
                    (lecture_date.isoformat(), lecture_date.weekday(), department_id_for(department_name), group_name),
                ).fetchone()
                if row is not None:
                    day_offset = offset
                    break

    if row is None:
        return {"status": "success", "next_lecture": None, "message": "لا توجد محاضرات قادمة حالياً"}
    return {
        "status": "success",
        "next_lecture": {
            "subject": row["subject"],
            "start_time": format_time_12h(row["start_time"]),
            "end_time": format_time_12h(row["end_time"]),
            "type": row["lecture_type"],
            "instructor": row["instructor"],
            "doctor_name": row["doctor_name"],
            "room": row["room"],
            "date": lecture_date.isoformat(),
            "weekday": lecture_date.weekday(),
            "day_offset": day_offset,
        },
    }

@app.post("/representative/postpone-lecture/")
async def postpone_lecture(email: str = Form(...), new_time: str = Form(...), department_name: str = Form("CyberSecurity"), group_name: str = Form("B")):
    department_name = normalize_department_name(department_name)
    group_name = normalize_group_name(group_name)
    if not representative_authorized(email, department_name, group_name):
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب ممثل مخولاً."}
    if department_name not in DEPARTMENTS or group_name not in GROUPS:
        return {"status": "error", "message": "اختر قسماً وكروباً صحيحين."}
    try:
        parsed_time = datetime.strptime(new_time.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        try:
            parsed_time = datetime.strptime(new_time.strip(), "%I:%M %p").strftime("%H:%M")
        except ValueError:
            return {"status": "error", "message": "أدخل الوقت بصيغة مثل 09:00 أو 09:00 AM."}
    slot = today_schedule(department_name, group_name)
    if not slot:
        return {"status": "error", "message": "لا توجد محاضرة مجدولة لهذا القسم والكروب اليوم."}
    today = datetime.now(APP_TIMEZONE).date().isoformat()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("INSERT INTO schedule_overrides(schedule_id,lecture_date,new_time,status) VALUES (?,?,?,'active') ON CONFLICT(schedule_id,lecture_date) DO UPDATE SET new_time=excluded.new_time,status='active'", (slot[0], today, parsed_time))
    conn.execute("DELETE FROM scheduled_otp_log WHERE schedule_id=? AND lecture_date=?", (slot[0], today))
    conn.commit()
    conn.close()
    otp_key = f"{department_name}|{group_name}"
    ACTIVE_OTPS.pop(otp_key, None)
    rescheduled_at = datetime.combine(datetime.fromisoformat(today).date(), datetime.strptime(parsed_time, "%H:%M").time()).replace(tzinfo=APP_TIMEZONE)
    logger.info("Rescheduled attendance OTP for schedule_id=%s to %s (%s)", slot[0], rescheduled_at.isoformat(), format_time_12h(parsed_time))
    notified = await send_attendance_telegram_message(
        f"📢 تنبيه: تم تأجيل محاضرة {slot[1]} إلى الساعة {format_time_12h(parsed_time)}", department_name
    )
    return {"status": "success", "time": format_time_12h(parsed_time), "message": f"تم تحديث وقت المحاضرة إلى {format_time_12h(parsed_time)}." if notified else "تم تحديث الوقت، لكن تعذر إرسال إشعار تيليجرام."}

@app.post("/representative/cancel-lecture/")
async def cancel_lecture(email: str = Form(...), department_name: str = Form("CyberSecurity"), group_name: str = Form("B")):
    department_name = normalize_department_name(department_name)
    group_name = normalize_group_name(group_name)
    if not representative_authorized(email, department_name, group_name):
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب ممثل مخولاً."}
    if department_name not in DEPARTMENTS or group_name not in GROUPS:
        return {"status": "error", "message": "اختر قسماً وكروباً صحيحين."}
    slot = today_schedule(department_name, group_name)
    if not slot:
        return {"status": "error", "message": "لا توجد محاضرة مجدولة لهذا القسم والكروب اليوم."}
    today = datetime.now(APP_TIMEZONE).date().isoformat()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("INSERT INTO schedule_overrides(schedule_id,lecture_date,new_time,status) VALUES (?,?,NULL,'cancelled') ON CONFLICT(schedule_id,lecture_date) DO UPDATE SET new_time=NULL,status='cancelled'", (slot[0], today))
    conn.commit()
    conn.close()
    otp_key = f"{department_name}|{group_name}"
    ACTIVE_OTPS.pop(otp_key, None)
    notified = await send_attendance_telegram_message(
        f"📢 تنبيه: تم إلغاء محاضرة {slot[1]} لهذا اليوم", department_name
    )
    return {"status": "success", "message": "تم إلغاء محاضرة اليوم." if notified else "تم إلغاء المحاضرة، لكن تعذر إرسال إشعار تيليجرام."}

@app.get("/")
@app.get("/app/")
async def serve_app():
    return FileResponse(Path(__file__).with_name("login.html"))


@app.get("/health")
def read_health():
    return {"status": "online", "message": "Sajjilni API Server is Running"}


@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.get("/manifest.json")
def serve_manifest():
    return FileResponse('manifest.json', media_type="application/manifest+json")

@app.get("/sw.js")
def serve_service_worker():
    return FileResponse(Path(__file__).with_name("sw.js"), media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

@app.get("/logo.png")
def serve_app_logo():
    return FileResponse('logo.png', media_type="image/png")


def ensure_admin_schema() -> None:
    with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for column, declaration in (
            ("department_id", "TEXT"),
            ("created_at", "DATETIME"),
        ):
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        conn.execute("UPDATE users SET department_id=COALESCE(department_id,class_id)")
        conn.execute("UPDATE users SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP)")
        conn.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_sessions_user_id ON admin_sessions(user_id)")
        conn.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (datetime.now(timezone.utc).isoformat(),))


def create_user_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expiry = datetime.now(timezone.utc) + timedelta(seconds=ADMIN_SESSION_TTL_SECONDS)
    conn.execute(
        "INSERT INTO admin_sessions(user_id,token_hash,expires_at) VALUES(?,?,?)",
        (user_id, token_hash, expiry.isoformat()),
    )
    return token


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        token,
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def require_authenticated_user(request: Request) -> dict:
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if not token:
        authorization = request.headers.get("Authorization", "")
        scheme, _, bearer_token = authorization.partition(" ")
        if scheme.casefold() == "bearer":
            token = bearer_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT u.id,u.email,u.role,u.department_id,u.department,u.class_id,u.group_name
               FROM admin_sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>?""",
            (token_hash, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return dict(row)


def require_admin(request: Request) -> dict:
    row = require_authenticated_user(request)
    if str(row["role"]).casefold() not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Admin role required.")
    return row


def require_department_manager(request: Request, department_value: str) -> dict:
    user = require_authenticated_user(request)
    role = str(user.get("role") or "").casefold()
    if role in {"admin", "super_admin"}:
        return user
    if role not in DEPARTMENT_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Representative or admin role required.")
    requested_department = normalize_department_name(department_value)
    owned_department = normalize_department_name(user.get("department_id") or user.get("department") or user.get("class_id"))
    if requested_department not in DEPARTMENTS or requested_department != owned_department:
        raise HTTPException(status_code=403, detail="This account cannot manage the selected department.")
    return user


@app.post("/login/")
def login(request: Request, response: Response, email: str = Form(...), password: str = Form(...)):
    ensure_academic_schema()
    ensure_admin_schema()
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    doctor_exists = cursor.execute("SELECT 1 FROM users WHERE email=?", ("dr.ahmed@uob.edu.iq",)).fetchone()
    if not doctor_exists:
        cursor.execute("INSERT INTO users (name, email, password, role, class_id, device_uuid, is_first_login) VALUES (?, ?, ?, 'doctor', 'CyberSecurity', NULL, 1)", ("د. أحمد المحمداوي", "dr.ahmed@uob.edu.iq", hash_password("doc12345")))
    cursor.execute("SELECT name, role, class_id, department, group_name, display_role, college, stage, password, is_first_login, id FROM users WHERE lower(email) = lower(?)", (email.strip(),))
    user = cursor.fetchone()
    valid_credentials = bool(user and verify_password(password, user[8]))
    if valid_credentials and not user[8].startswith(PASSWORD_HASH_PREFIX):
        cursor.execute("UPDATE users SET password=? WHERE lower(email)=lower(?)", (hash_password(password), email.strip()))
        conn.commit()
    session_token = None
    if valid_credentials:
        user_id = cursor.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email.strip(),)).fetchone()[0]
        session_token = create_user_session(conn, user_id)
        conn.commit()
    conn.close()
    
    accepts_html = "text/html" in request.headers.get("accept", "").lower()
    if valid_credentials:
        if accepts_html:
            redirect = RedirectResponse(url="/", status_code=303)
            set_session_cookie(redirect, request, session_token)
            return redirect
        user_payload = {"id": user[10], "name": user[0], "role": user[1], "class_id": user[2], "department": user[3], "department_id": user[2], "group_name": user[4], "display_role": user[5], "college": user[6], "stage": user[7], "email": email.strip(), "is_first_login": bool(user[9]), "access_token": session_token, "token_type": "bearer"}
        user_payload["profile"] = get_localized_profile(user_payload)
        set_session_cookie(response, request, session_token)
        return {"status": "success", "require_password_change": bool(user[9]), "user": user_payload, **user_payload}
    if accepts_html:
        return RedirectResponse(url="/?login_error=1", status_code=303)
    return {"status": "error", "message": "البريد الإلكتروني أو كلمة المرور غير صحيحة"}

def get_localized_profile(user: dict) -> dict:
    role = str(user.get("role") or "student").strip().casefold()
    group_value = user.get("group_name") or user.get("group")
    group = normalize_group_name(str(group_value)) if group_value else ""
    department_id = _canonical_department_id(
        str(user.get("department_id") or user.get("class_id") or ""),
        str(user.get("department") or ""),
    )
    is_representative = role in {"rep", "assistant", "deputy_rep", "representative", "group_rep"}
    is_student = role == "student"
    role_label = "ممثل" if is_representative else "طالب" if is_student else str(user.get("display_role") or user.get("role") or "")
    if group and is_representative:
        role_label = f"ممثل كروب {group}"
    elif group and is_student:
        role_label = f"طالب - كروب {group}"
    return {
        "college": "كلية الهندسة",
        "stage_group": f"المرحلة الثانية - كروب {group}" if group else "المرحلة الثانية",
        "display_role": role_label,
        "department": "هندسة الذكاء الاصطناعي" if department_id == "AI" else "هندسة الشبكات والأمن السيبراني",
    }


@app.get("/profile")
def get_profile(request: Request):
    return {"status": "success", "profile": get_localized_profile(require_authenticated_user(request))}


ADMIN_MANAGEABLE_ROLES = {"student", "rep", "assistant", "deputy_rep", "representative", "group_rep", "doctor", "professor", "admin", "super_admin"}


def _admin_conn():
    ensure_admin_schema()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/admin/users")
def admin_list_users(request: Request, department: str | None = None, role: str | None = None):
    require_admin(request)
    conn = _admin_conn()
    try:
        sql = "SELECT id,email,role,department_id,created_at,name,department,group_name FROM users WHERE 1=1"
        params = []
        if department:
            if department.casefold() in {"all", "null", "none"}:
                sql += " AND department_id IS NULL"
            else:
                sql += " AND (department_id=? OR department=?)"
                params.extend((department, department))
        if role:
            sql += " AND lower(role)=lower(?)"
            params.append(role.strip())
        sql += " ORDER BY lower(email),id"
        return {"users": [dict(row) for row in conn.execute(sql, params).fetchall()]}
    finally:
        conn.close()


@app.get("/admin/lectures")
def admin_list_lectures(request: Request, department_id: str | None = None, group: str | None = None):
    require_admin(request)
    ensure_materials_table()
    department_filter = None if (department_id or "").casefold() in {"", "all", "none"} else department_id
    group_filter = None if (group or "").casefold() in {"", "all", "none"} else group
    return list_materials(request, department=department_filter, group_name=group_filter)


@app.delete("/admin/lectures/{lecture_id}")
def admin_delete_lecture(lecture_id: int, request: Request):
    require_admin(request)
    return delete_lecture(lecture_id, request)


@app.get("/admin/schedules")
def admin_list_schedules(request: Request, department_id: str | None = None, group: str | None = None):
    require_admin(request)
    department_filter = None if (department_id or "").casefold() in {"", "all", "none"} else department_id
    group_filter = None if (group or "").casefold() in {"", "all", "none"} else group
    return get_schedules(request, department=department_filter, group_name=group_filter)


@app.post("/admin/users")
def admin_create_user(request: Request, payload: dict = Body(...)):
    actor = require_admin(request)
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "student")).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must contain at least 6 characters.")
    if role not in ADMIN_MANAGEABLE_ROLES:
        raise HTTPException(status_code=400, detail="Unsupported role.")
    if role in {"admin", "super_admin"} and actor["role"].casefold() != "super_admin":
        raise HTTPException(status_code=403, detail="Only a super admin may create admin accounts.")
    department_id = str(payload.get("department_id") or "").strip()
    if department_id.casefold() in {"", "all", "null", "none"}:
        department_id, department, class_id = "", None, None
    else:
        class_id = department_id
        department = DEPARTMENTS[0] if department_id.casefold() in {"cybersecurity", "cyber security"} else DEPARTMENTS[1] if department_id.casefold() in {"ai_robotics", "ai", "artificial intelligence"} else str(payload.get("department") or department_id)
    name = str(payload.get("name") or email.split("@", 1)[0]).strip()
    conn = _admin_conn()
    try:
        if conn.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        cursor = conn.execute("""INSERT INTO users(name,email,password,role,class_id,department,department_id,group_name,display_role,is_first_login,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (name, email, hash_password(password), role, class_id, department, department_id or None,
             payload.get("group_name") if department_id else None, payload.get("display_role"), 1))
        conn.commit()
        user = conn.execute("SELECT id,email,role,department_id,created_at,name,department,group_name FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
        return {"status": "success", "user": dict(user)}
    finally:
        conn.close()


@app.put("/admin/users/{user_id}/role")
def admin_update_user_role(user_id: int, request: Request, payload: dict = Body(...)):
    actor = require_admin(request)
    role = str(payload.get("role", "")).strip().lower()
    if role not in ADMIN_MANAGEABLE_ROLES:
        raise HTTPException(status_code=400, detail="Unsupported role.")
    if role in {"admin", "super_admin"} and actor["role"].casefold() != "super_admin":
        raise HTTPException(status_code=403, detail="Only a super admin may assign admin roles.")
    conn = _admin_conn()
    try:
        target = conn.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        old_admin = str(target["role"]).casefold() in {"admin", "super_admin"}
        if old_admin and actor["role"].casefold() != "super_admin":
            raise HTTPException(status_code=403, detail="Only a super admin may change admin accounts.")
        if user_id == actor["id"] and role not in {"admin", "super_admin"}:
            raise HTTPException(status_code=400, detail="You cannot remove your own admin access.")
        if old_admin and role not in {"admin", "super_admin"}:
            if target["role"].casefold() == "super_admin" and actor["role"].casefold() != "super_admin":
                raise HTTPException(status_code=403, detail="Only a super admin may change this account.")
            if conn.execute("SELECT COUNT(*) FROM users WHERE lower(role) IN ('admin','super_admin')").fetchone()[0] <= 1:
                raise HTTPException(status_code=400, detail="The last admin account cannot be demoted.")
        conn.execute("UPDATE users SET role=?,display_role=? WHERE id=?", (role, "ممثل" if role in REPRESENTATIVE_ROLES else None, user_id))
        if role not in {"admin", "super_admin"}:
            conn.execute("DELETE FROM admin_sessions WHERE user_id=?", (user_id,))
        conn.commit()
        return {"status": "success", "user_id": user_id, "role": role}
    finally:
        conn.close()


@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    actor = require_admin(request)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    conn = _admin_conn()
    try:
        target = conn.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        is_admin = str(target["role"]).casefold() in {"admin", "super_admin"}
        if is_admin and actor["role"].casefold() != "super_admin":
            raise HTTPException(status_code=403, detail="Only a super admin may delete admin accounts.")
        if is_admin and conn.execute("SELECT COUNT(*) FROM users WHERE lower(role) IN ('admin','super_admin')").fetchone()[0] <= 1:
            raise HTTPException(status_code=400, detail="The last admin account cannot be deleted.")
        conn.execute("DELETE FROM admin_sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        return {"status": "success", "deleted_user_id": user_id}
    finally:
        conn.close()


@app.post("/logout/")
def logout(request: Request, response: Response):
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if token:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (hashlib.sha256(token.encode("utf-8")).hexdigest(),))
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    return {"status": "success"}


@app.post("/change_password")
def change_password(payload: dict = Body(...)):
    email = str(payload.get("email", "")).strip().lower()
    old_password = str(payload.get("old_password", ""))
    new_password = str(payload.get("new_password", ""))
    if len(new_password) < 6:
        return {"status": "error", "message": "يجب أن تتكون كلمة المرور الجديدة من 6 أحرف على الأقل."}
    if new_password.casefold() == "b2026":
        return {"status": "error", "message": "اختر كلمة مرور مختلفة عن كلمة المرور الافتراضية."}
    ensure_academic_schema()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        row = conn.execute("SELECT password FROM users WHERE lower(email)=?", (email,)).fetchone()
        if not row or not verify_password(old_password, row[0]):
            return {"status": "error", "message": "كلمة المرور الحالية غير صحيحة."}
        conn.execute("UPDATE users SET password=?, is_first_login=0 WHERE lower(email)=?", (hash_password(new_password), email))
        conn.commit()
        return {"status": "success"}
    finally:
        conn.close()

def doctor_user(email: str):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, class_id FROM users WHERE lower(email) = lower(?) AND lower(role) IN ('doctor', 'professor')", (email,))
    result = cursor.fetchone()
    conn.close()
    return result


SCHEDULE_DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
SCHEDULE_DEPARTMENT_CODES = {
    DEPARTMENTS[0]: "Cybersecurity",
    DEPARTMENTS[1]: "AI",
}


def format_time_12h(time_val) -> str:
    if time_val is None or time_val == "":
        return ""
    if isinstance(time_val, datetime):
        parsed = time_val
    elif hasattr(time_val, "hour") and hasattr(time_val, "minute"):
        parsed = time_val
    else:
        value = str(time_val).strip()
        parsed = None
        for pattern in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
            try:
                parsed = datetime.strptime(value, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"Unsupported time value: {time_val!r}")
    hour = parsed.hour % 12 or 12
    marker = "ص" if parsed.hour < 12 else "م"
    return f"{hour:02d}:{parsed.minute:02d} {marker}"


def display_schedule_time(value: str) -> str:
    return format_time_12h(value)


def doctor_schedule_records(email: str, weekday: int | None = None):
    doctor = doctor_user(email)
    if not doctor:
        return None
    ensure_academic_schema()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.row_factory = sqlite3.Row
        query = """SELECT id, weekday, start_time, end_time, department, department_code,
                          group_name, subject, stage, doctor_email, room, lecture_type
                   FROM lecture_schedule WHERE lower(doctor_email)=lower(?) AND is_active=1"""
        params = [email.strip()]
        if weekday is not None:
            query += " AND weekday=?"
            params.append(weekday)
        query += " ORDER BY weekday, start_time, department, group_name"
        rows = conn.execute(query, params).fetchall()
        lectures = []
        local_date = datetime.now(APP_TIMEZONE).date().isoformat()
        for row in rows:
            status = "active"
            start_time = row["start_time"]
            if weekday is not None:
                override = conn.execute(
                    "SELECT new_time,status FROM schedule_overrides WHERE schedule_id=? AND lecture_date=?",
                    (row["id"], local_date),
                ).fetchone()
                if override:
                    status = override["status"]
                    start_time = override["new_time"] or start_time
            if status == "cancelled":
                continue
            end_time = row["end_time"] or ""
            start_time = display_schedule_time(start_time)
            end_time = display_schedule_time(end_time)
            department_code = row["department_code"] or SCHEDULE_DEPARTMENT_CODES.get(row["department"], row["department"])
            department_label = "هندسة الأمن السيبراني" if department_code == "CyberSecurity" else "هندسة الذكاء الاصطناعي والروبوتات"
            lectures.append({
                "id": row["id"],
                "department": department_code,
                "department_name": department_label,
                "stage": row["stage"] or "Second Stage",
                "group": f"Group {row['group_name']}",
                "day": SCHEDULE_DAY_NAMES.get(row["weekday"], ""),
                "time": f"{start_time} - {end_time}".strip(" -"),
                "subject": row["subject"],
                "doctor_email": row["doctor_email"],
                "classroom": row["room"] or "",
                "type": row["lecture_type"] or "Lecture",
                "status": status,
            })
        return lectures
    finally:
        conn.close()


@app.get("/doctor/schedule/today")
def doctor_today_schedule(request: Request, email: str):
    actor = require_authenticated_user(request)
    if str(actor.get("email") or "").casefold() != email.strip().casefold():
        raise HTTPException(status_code=403, detail="You can only view your own schedule.")
    weekday = datetime.now(APP_TIMEZONE).weekday()
    lectures = doctor_schedule_records(email, weekday)
    if lectures is None:
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب طبيب."}
    return {"status": "success", "day": SCHEDULE_DAY_NAMES[weekday], "lectures": lectures}


@app.get("/doctor/schedule/weekly")
def doctor_weekly_schedule(request: Request, email: str):
    actor = require_authenticated_user(request)
    if str(actor.get("email") or "").casefold() != email.strip().casefold():
        raise HTTPException(status_code=403, detail="You can only view your own schedule.")
    lectures = doctor_schedule_records(email)
    if lectures is None:
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب طبيب."}
    return {"status": "success", "lectures": lectures}

@app.get("/doctor/active-session")
def doctor_active_session(email: str, subject_name: str | None = None, schedule_id: int | None = None):
    doctor = doctor_user(email)
    if not doctor:
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب طبيب."}
    schedule_scope = None
    if schedule_id is not None:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            schedule_scope = conn.execute(
                "SELECT department_code,group_name,subject FROM lecture_schedule WHERE id=? AND lower(doctor_email)=lower(?) AND is_active=1",
                (schedule_id, email.strip()),
            ).fetchone()
        if not schedule_scope:
            return {"status": "denied", "message": "المحاضرة المحددة غير مسندة إلى حسابك."}
    department_code = schedule_scope["department_code"] if schedule_scope else (doctor[1] or "CyberSecurity")
    group_name = schedule_scope["group_name"] if schedule_scope else None
    class_id = department_code
    session_key = f"{class_id}:{group_name or 'ALL'}"
    session = ACTIVE_SESSIONS.get(session_key)
    if not session:
        session = {"session_code": secrets.token_urlsafe(8), "class_id": class_id, "group_name": group_name, "subject": (schedule_scope["subject"] if schedule_scope else None) or subject_name or "", "hall": "قاعة 302"}
        ACTIVE_SESSIONS[session_key] = session
    elif subject_name and subject_name.strip():
        session["subject"] = subject_name.strip()
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    role_values = "'student','rep','deputy_rep','representative','group_rep'"
    params = [class_id]
    if group_name:
        arabic_letter = "ا" if group_name == "A" else "ب"
        group_values = [
            group_name, f"GROUP {group_name}", f"كروب {group_name}", f"كروب {arabic_letter}",
            f"المجموعة {group_name}", f"المجموعة {arabic_letter}", f"مجموعة {group_name}", f"مجموعة {arabic_letter}",
        ]
        group_filter = f" AND upper(trim(group_name)) IN ({','.join('?' for _ in group_values)})"
        params.extend(value.upper() for value in group_values)
    else:
        group_filter = ""
    cursor.execute(f"SELECT COUNT(*) FROM users WHERE class_id = ? AND role IN ({role_values}){group_filter}", params)
    expected = cursor.fetchone()[0]
    attendance_group_filter = group_filter.replace("group_name", "u.group_name")
    cursor.execute(f"""SELECT COUNT(DISTINCT a.student_email) FROM attendance a
        JOIN users u ON lower(u.email)=lower(a.student_email)
        WHERE a.class_id=? AND a.status IN ('present','confirmed'){attendance_group_filter}""", params)
    checked_in = cursor.fetchone()[0]
    conn.close()
    return {"status": "success", **session, "doctor_name": doctor[0], "department": class_id, "expected_total": expected, "checked_in_count": checked_in}

@app.get("/doctor/roster")
def doctor_roster(email: str, schedule_id: int | None = None):
    doctor = doctor_user(email)
    if not doctor:
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب طبيب."}
    schedule_scope = None
    if schedule_id is not None:
        with closing(sqlite3.connect(DATABASE_PATH)) as scope_conn:
            scope_conn.row_factory = sqlite3.Row
            schedule_scope = scope_conn.execute(
                "SELECT department_code,group_name FROM lecture_schedule WHERE id=? AND lower(doctor_email)=lower(?) AND is_active=1",
                (schedule_id, email.strip()),
            ).fetchone()
    if schedule_id is not None and not schedule_scope:
        return {"status": "denied", "message": "المحاضرة المحددة غير مسندة إلى حسابك."}
    class_id = (schedule_scope["department_code"] if schedule_scope else None) or doctor[1] or "CyberSecurity"
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""SELECT u.name, u.email, COALESCE((SELECT a.status FROM attendance a WHERE a.student_email=u.email AND a.class_id=u.class_id ORDER BY a.id DESC LIMIT 1), 'absent') FROM users u WHERE u.class_id=? AND u.role IN ('student','rep','deputy_rep','representative','group_rep') ORDER BY u.name""", (class_id,))
    roster = [{"name": row[0], "email": row[1], "status": row[2]} for row in cursor.fetchall()]
    if schedule_scope:
        target_group = normalize_group_name(schedule_scope["group_name"])
        conn.close()
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        roster = []
        students = conn.execute("SELECT name,email,group_name FROM users WHERE class_id=? AND role IN ('student','rep','deputy_rep','representative','group_rep') ORDER BY name", (class_id,)).fetchall()
        for student in students:
            if normalize_group_name(student["group_name"]) != target_group:
                continue
            status_row = conn.execute("SELECT status FROM attendance WHERE lower(student_email)=lower(?) AND class_id=? ORDER BY id DESC LIMIT 1", (student["email"], class_id)).fetchone()
            roster.append({"name": student["name"], "email": student["email"], "status": status_row[0] if status_row else "absent"})
    conn.close()
    return {"status": "success", "roster": roster}

@app.post("/doctor/attendance-toggle")
def doctor_attendance_toggle(email: str = Form(...), student_email: str = Form(...), schedule_id: int | None = Form(None)):
    doctor = doctor_user(email)
    if not doctor:
        return {"status": "denied", "message": "يتطلب هذا الإجراء حساب طبيب."}
    schedule_scope = None
    if schedule_id is not None:
        with closing(sqlite3.connect(DATABASE_PATH)) as scope_conn:
            scope_conn.row_factory = sqlite3.Row
            schedule_scope = scope_conn.execute("SELECT department_code,group_name FROM lecture_schedule WHERE id=? AND lower(doctor_email)=lower(?) AND is_active=1", (schedule_id, email.strip())).fetchone()
    if schedule_id is not None and not schedule_scope:
        return {"status": "denied", "message": "المحاضرة المحددة غير مسندة إلى حسابك."}
    class_id = (schedule_scope["department_code"] if schedule_scope else None) or doctor[1] or "CyberSecurity"
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE email=? AND class_id=?", (student_email, class_id))
    student = cursor.fetchone()
    if student and schedule_scope:
        group_row = cursor.execute("SELECT group_name FROM users WHERE email=?", (student_email,)).fetchone()
        if not group_row or normalize_group_name(group_row[0]) != normalize_group_name(schedule_scope["group_name"]):
            student = None
    if not student:
        conn.close()
        return {"status": "error", "message": "الطالب غير موجود في هذه الشعبة."}
    cursor.execute("SELECT id FROM attendance WHERE student_email=? AND class_id=? ORDER BY id DESC LIMIT 1", (student_email, class_id))
    record = cursor.fetchone()
    if record:
        cursor.execute("UPDATE attendance SET status=CASE WHEN status IN ('present','confirmed') THEN 'absent' ELSE 'present' END WHERE id=?", (record[0],))
    else:
        cursor.execute("INSERT INTO attendance (student_email,class_id,status) VALUES (?,?, 'present')", (student_email,class_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/generate-otp/")
@app.post("/generate_otp")
async def generate_attendance_otp(
    email: str = Form(...),
    department_name: str = Form("CyberSecurity"),
    group_name: str = Form("B"),
    subject_name: str = Form(""),
    schedule_id: int | None = Form(None),
):
    department_name = normalize_department_name(department_name)
    group_name = normalize_group_name(group_name)
    ensure_academic_schema()
    doctor = doctor_user(email)
    authorized_representative = representative_authorized(email, department_name, group_name)
    if not authorized_representative and not doctor:
        return {"status": "denied", "message": "يتطلب إنشاء رمز الحضور حساب طبيب أو ممثل مخولاً."}
    if doctor:
        if schedule_id is None:
            return {"status": "denied", "message": "اختر محاضرة اليوم المسندة إلى حسابك."}
        local_today = datetime.now(APP_TIMEZONE)
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            scheduled_lecture = conn.execute(
                """SELECT s.department,s.group_name,s.subject,COALESCE(o.status,'active') AS status
                   FROM lecture_schedule s LEFT JOIN schedule_overrides o
                   ON o.schedule_id=s.id AND o.lecture_date=?
                   WHERE s.id=? AND lower(s.doctor_email)=lower(?) AND s.weekday=? AND s.is_active=1""",
                (local_today.date().isoformat(), schedule_id, email.strip(), local_today.weekday()),
            ).fetchone()
        if not scheduled_lecture:
            return {"status": "denied", "message": "المحاضرة المحددة غير مسندة إلى حسابك اليوم."}
        if scheduled_lecture["status"] == "cancelled":
            return {"status": "error", "message": "تم إلغاء هذه المحاضرة اليوم."}
        department_name = normalize_department_name(scheduled_lecture["department"])
        group_name = normalize_group_name(scheduled_lecture["group_name"])
        subject_name = scheduled_lecture["subject"]
    if department_name not in DEPARTMENTS or group_name not in GROUPS:
        return {"status": "error", "message": "اختر قسماً وكروباً صحيحين."}
    try:
        schedule = None if doctor else today_schedule(department_name, group_name)
        if not doctor and schedule and schedule[3] == "cancelled":
            return {"status": "error", "message": "تم إلغاء محاضرة هذا القسم والكروب اليوم."}
        subject = subject_name.strip() or (schedule[1] if schedule else "محاضرة اليوم")
        session = await create_and_send_otp(department_name, group_name, subject)
    except (httpx.HTTPError, RuntimeError, ValueError):
        logger.exception("OTP generation failed after request validation")
        return {"status": "error", "message": "تعذر توليد رمز التحقق."}
    if not session:
        return {"status": "error", "message": "تعذر توليد رمز التحقق."}
    message = "تم إرسال رمز التحقق بنجاح إلى جروب تليجرام!" if session["telegram_sent"] else "تم توليد الرمز بنجاح!"
    return {"status": "success", "otp": session["otp"], "subject": subject, "message": message, "telegram_sent": session["telegram_sent"], "expires_in": 180}

def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * earth_radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def attendance_room_constraints(department_name: str, group_name: str, subject_name: str):
    """Read optional room network/location settings; missing settings mean no restriction."""
    settings = {"cidrs": [], "latitude": None, "longitude": None, "radius": None}
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schedule = conn.execute(
                "SELECT room FROM lecture_schedule WHERE department=? AND group_name=? AND subject=? ORDER BY weekday LIMIT 1",
                (department_name, group_name, subject_name),
            ).fetchone() if "lecture_schedule" in tables else None
            room_name = str(schedule[0]).strip() if schedule and schedule[0] else ""
            for table in ("classrooms", "rooms", "departments", "groups"):
                if table not in tables:
                    continue
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                for row in rows:
                    values = dict(row)
                    department_keys = ("department", "department_name", "department_id", "name") if table == "departments" else ("department", "department_name", "department_id")
                    department_value = next((values[key] for key in department_keys if key in columns and values[key] not in (None, "")), None)
                    group_value = next((values[key] for key in ("group_name", "group", "group_id") if key in columns and values[key] not in (None, "")), None)
                    room_keys = ("room", "room_name", "hall", "hall_name", "name") if table in ("classrooms", "rooms") else ("room", "room_name", "hall", "hall_name")
                    room_value = next((values[key] for key in room_keys if key in columns and values[key] not in (None, "")), None)
                    if department_value is not None and normalize_department_name(str(department_value)) != department_name:
                        continue
                    if group_value is not None and normalize_group_name(str(group_value)) != group_name:
                        continue
                    if room_name and room_value is not None and str(room_value).strip().casefold() != room_name.casefold():
                        continue
                    cidr_value = next((values[key] for key in ("allowed_cidrs", "allowed_cidr", "network_cidr", "allowed_network", "allowed_networks", "network_range") if key in columns and values[key]), None)
                    if cidr_value:
                        settings["cidrs"] = [value.strip() for value in re.split(r"[,;]", str(cidr_value)) if value.strip()]
                    settings["latitude"] = next((values[key] for key in ("classroom_latitude", "latitude", "gps_latitude") if key in columns and values[key] not in (None, "")), settings["latitude"])
                    settings["longitude"] = next((values[key] for key in ("classroom_longitude", "longitude", "gps_longitude") if key in columns and values[key] not in (None, "")), settings["longitude"])
                    settings["radius"] = next((values[key] for key in ("radius_meters", "radius", "allowed_radius") if key in columns and values[key] not in (None, "")), settings["radius"])
                    if settings["cidrs"] or settings["latitude"] is not None or settings["longitude"] is not None:
                        return settings
    except (sqlite3.Error, ValueError, TypeError):
        logger.exception("Could not read optional classroom validation settings")
    env_cidrs = [value.strip() for value in os.getenv("CLASSROOM_ALLOWED_CIDRS", "").split(",") if value.strip()]
    settings["cidrs"] = settings["cidrs"] or env_cidrs
    settings["latitude"] = settings["latitude"] if settings["latitude"] is not None else os.getenv("CLASSROOM_LATITUDE")
    settings["longitude"] = settings["longitude"] if settings["longitude"] is not None else os.getenv("CLASSROOM_LONGITUDE")
    settings["radius"] = settings["radius"] if settings["radius"] is not None else os.getenv("CLASSROOM_RADIUS_METERS", "50")
    return settings

@app.post("/student-checkin/")
def student_checkin(
    request: Request,
    email: str = Form(...),
    otp_code: str = Form(...),
    device_uuid: str = Form(...),
    latitude: str | None = Form(None),
    longitude: str | None = Form(None),
    snapshot_base64: str | None = Form(None),
):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    ensure_academic_schema()
    cursor.execute("SELECT role, department, group_name, class_id, device_uuid FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    if not user or user[0] not in ('student', *REPRESENTATIVE_ROLES):
        conn.close()
        return {"status": "error", "message": "حساب الطالب غير موجود أو غير مخول لتسجيل الحضور."}
    department_name, group_name = normalize_department_name(user[1]), normalize_group_name(user[2] or "A")
    class_id = user[3] or "CyberSecurity"

    otp_key = f"{department_name}|{group_name}"
    active_otp = ACTIVE_OTPS.get(otp_key)
    now = datetime.now(timezone.utc)
    if not active_otp or not isinstance(active_otp, dict) or now >= active_otp["expires_at"]:
        conn.close()
        return {"status": "error", "message": "لا يوجد رمز حضور فعال أو انتهت صلاحيته. اطلب من الممثل توليد رمز جديد."}
    if active_otp.get("attempts", 0) >= 5:
        conn.close()
        return {"status": "error", "message": "تم تجاوز محاولات الرمز. اطلب توليد رمز حضور جديد."}
    if not secrets.compare_digest(str(otp_code), active_otp["code"]):
        active_otp["attempts"] = active_otp.get("attempts", 0) + 1
        conn.close()
        return {"status": "error", "message": "رمز التحقق غير صحيح."}

    constraints = attendance_room_constraints(department_name, group_name, active_otp.get("subject", ""))
    remote_ip = request.client.host if request.client else ""
    try:
        client_ip = ipaddress.ip_address(remote_ip)
        local_dev_networks = tuple(ipaddress.ip_network(value) for value in ("127.0.0.0/8", "192.168.0.0/16", "10.0.0.0/8", "::1/128"))
        is_local_development_ip = any(client_ip in network for network in local_dev_networks)
        if constraints["cidrs"] and not is_local_development_ip:
            allowed_networks = [ipaddress.ip_network(value, strict=False) for value in constraints["cidrs"]]
            if not any(client_ip in network for network in allowed_networks):
                conn.close()
                return {"status": "error", "message": "اتصل بشبكة الجامعة أو شبكة القاعة لتسجيل الحضور."}
    except ValueError:
        if constraints["cidrs"]:
            conn.close()
            return {"status": "error", "message": "تعذر التحقق من عنوان شبكة القاعة."}

    # GPS proximity is enforced only when the room has configured coordinates.
    if constraints["latitude"] not in (None, "") and constraints["longitude"] not in (None, ""):
        try:
            latitude_value = float(latitude)
            longitude_value = float(longitude)
            classroom_lat = float(constraints["latitude"])
            classroom_lon = float(constraints["longitude"])
            radius = float(constraints["radius"] or 50)
        except (TypeError, ValueError):
            conn.close()
            return {"status": "error", "message": "إحداثيات موقع القاعة أو الجهاز غير صالحة أو غير مكتملة."}
        if not all(math.isfinite(value) for value in (latitude_value, longitude_value, classroom_lat, classroom_lon, radius)) or not (-90 <= latitude_value <= 90 and -180 <= longitude_value <= 180 and -90 <= classroom_lat <= 90 and -180 <= classroom_lon <= 180 and radius > 0):
            conn.close()
            return {"status": "error", "message": "إحداثيات موقع القاعة أو الجهاز غير صالحة."}
        if distance_meters(latitude_value, longitude_value, classroom_lat, classroom_lon) > radius:
            conn.close()
            return {"status": "error", "message": "أنت خارج نطاق القاعة الدراسية."}

    session_id = active_otp["session_id"]
    bindings = SESSION_DEVICE_BINDINGS.setdefault(session_id, {})
    attendance = SESSION_ATTENDANCE.setdefault(session_id, set())
    if device_uuid in bindings and bindings[device_uuid] != email:
        conn.close()
        return {"status": "error", "message": "عفواً، الهاتف مستخدم لتسجيل طالب آخر."}
    if email in attendance:
        conn.close()
        return {"status": "error", "message": "تم تسجيل حضورك مسبقاً في هذه الجلسة."}
    saved_uuid = user[4]
    if saved_uuid and saved_uuid != device_uuid:
        conn.close()
        return {"status": "error", "message": "هذا الحساب مرتبط بجهاز آخر."}

    snapshot_url = None
    if snapshot_base64:
        try:
            header, encoded_image = snapshot_base64.split(",", 1) if "," in snapshot_base64 else ("", snapshot_base64)
            if header and header not in ("data:image/jpeg;base64", "data:image/png;base64"):
                raise ValueError("Unsupported image type")
            if len(encoded_image) > 6_000_000:
                raise ValueError("Image is too large")
            image_bytes = base64.b64decode(encoded_image, validate=True)
            if not image_bytes or len(image_bytes) > 4_500_000:
                raise ValueError("Image is empty or too large")
            extension = ".png" if header == "data:image/png;base64" else ".jpg"
            snapshot_dir = ATTENDANCE_SNAPSHOT_DIR
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_name = f"{secrets.token_hex(16)}{extension}"
            (snapshot_dir / snapshot_name).write_bytes(image_bytes)
            snapshot_url = f"/attendance-snapshots/{snapshot_name}"
        except (ValueError, binascii.Error, OSError):
            conn.close()
            return {"status": "error", "message": "تعذر حفظ صورة التحقق. أعد المحاولة."}

    cursor.execute("UPDATE users SET device_uuid = ? WHERE email = ?", (device_uuid, email))
    cursor.execute("INSERT INTO attendance (student_email, class_id, status, department, group_name, snapshot_url) VALUES (?, ?, 'present', ?, ?, ?)", (email, class_id, department_name, group_name, snapshot_url))
    conn.commit()
    conn.close()
    bindings[device_uuid] = email
    attendance.add(email)
    return {"status": "success", "message": "تم تسجيل حضورك بنجاح!"}

@app.post("/verify_attendance_otp")
def verify_attendance_otp(email: str = Form(...), otp_code: str = Form(...)):
    ensure_academic_schema()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        user = conn.execute(
            "SELECT role, department, group_name FROM users WHERE lower(email)=lower(?)", (email.strip(),)
        ).fetchone()
        if not user or user[0] not in ("student", *REPRESENTATIVE_ROLES):
            return {"status": "error", "message": "رمز الحضور غير صحيح، يرجى التأكد من الرمز"}
        otp_key = f"{normalize_department_name(user[1])}|{normalize_group_name(user[2] or 'A')}"
        active_otp = ACTIVE_OTPS.get(otp_key)
        if not isinstance(active_otp, dict) or datetime.now(timezone.utc) >= active_otp.get("expires_at", datetime.min.replace(tzinfo=timezone.utc)):
            return {"status": "error", "message": "رمز الحضور غير صحيح، يرجى التأكد من الرمز"}
        if active_otp.get("attempts", 0) >= 5:
            return {"status": "error", "message": "تم تجاوز محاولات الرمز. اطلب توليد رمز حضور جديد."}
        if not secrets.compare_digest(str(otp_code).strip(), str(active_otp.get("code", ""))):
            active_otp["attempts"] = active_otp.get("attempts", 0) + 1
            return {"status": "error", "message": "رمز الحضور غير صحيح، يرجى التأكد من الرمز"}
        return {"status": "success", "message": "رمز الحضور صحيح."}

@app.post("/verify_face_and_attendance")
def verify_face_and_attendance(
    request: Request,
    email: str = Form(...),
    otp_code: str = Form(...),
    device_uuid: str = Form(...),
    image_base64: str = Form(...),
    latitude: str | None = Form(None),
    longitude: str | None = Form(None),
):
    if not image_base64:
        return {"status": "error", "message": "يرجى التقاط صورة للتحقق من الحضور."}
    # Reuse the attendance pipeline so OTP, network, GPS and device binding
    # validations run before the optional audit snapshot is stored.
    return student_checkin(request, email, otp_code, device_uuid, latitude, longitude, image_base64)

@app.get("/attendance-snapshots/{snapshot_name}")
def attendance_snapshot(snapshot_name: str, email: str):
    """Serve attendance photos only to a representative of the same group."""
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png)", snapshot_name):
        return {"status": "error", "message": "الصورة المطلوبة غير موجودة."}
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            representative = conn.execute(
                "SELECT role, department, group_name FROM users WHERE email=?", (email,)
            ).fetchone()
            if not representative or representative["role"] not in REPRESENTATIVE_ROLES:
                return {"status": "error", "message": "لا تملك صلاحية عرض صورة الحضور."}
            record = conn.execute(
                "SELECT department, group_name FROM attendance WHERE snapshot_url=? LIMIT 1",
                (f"/attendance-snapshots/{snapshot_name}",),
            ).fetchone()
            if not record or normalize_department_name(record["department"]) != normalize_department_name(representative["department"]) or normalize_group_name(record["group_name"]) != normalize_group_name(representative["group_name"]):
                return {"status": "error", "message": "لا تملك صلاحية عرض صورة الحضور."}
        photo_path = ATTENDANCE_SNAPSHOT_DIR / snapshot_name
        if not photo_path.is_file():
            return {"status": "error", "message": "الصورة المطلوبة غير موجودة."}
        media_type = "image/png" if photo_path.suffix == ".png" else "image/jpeg"
        return FileResponse(photo_path, media_type=media_type, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        logger.exception("Could not serve attendance snapshot")
        return {"status": "error", "message": str(exc)}

@app.get("/attendance/active-session/")
def active_session_attendance(email: str):
    """Return the active OTP session's live attendance for the caller's group."""
    try:
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            caller = conn.execute(
                "SELECT department, group_name, class_id, role FROM users WHERE email = ?", (email,)
            ).fetchone()
            if not caller:
                return {"status": "error", "present_count": 0, "expected_count": 0, "students": []}

            department_name = normalize_department_name(caller["department"])
            group_name = normalize_group_name(caller["group_name"] or "B")
            otp_key = f"{department_name}|{group_name}"
            active_otp = ACTIVE_OTPS.get(otp_key)
            present_emails = set()
            if isinstance(active_otp, dict) and datetime.now(timezone.utc) < active_otp.get("expires_at", datetime.min.replace(tzinfo=timezone.utc)):
                present_emails = SESSION_ATTENDANCE.get(active_otp.get("session_id"), set())

            rows = conn.execute(
                "SELECT email, name, department, group_name, class_id FROM users WHERE lower(role) IN ('student','rep','representative','group_rep','deputy_rep')"
            ).fetchall()
            group_students = []
            for row in rows:
                row_department = normalize_department_name(row["department"])
                row_group = normalize_group_name(row["group_name"] or "B")
                if row_group != group_name:
                    continue
                # Cyber Security accounts have legacy department labels, so compare
                # their normalized department or shared class identifier.
                row_class_id = str(row["class_id"] or "").strip().casefold()
                caller_class_id = str(caller["class_id"] or "").strip().casefold()
                same_department = row_department == department_name or (caller_class_id and row_class_id == caller_class_id)
                if same_department:
                    group_students.append(row)

            students = []
            if caller["role"] in REPRESENTATIVE_ROLES:
                for row in group_students:
                    if row["email"] not in present_emails:
                        continue
                    snapshot = conn.execute(
                        "SELECT snapshot_url FROM attendance WHERE student_email=? AND snapshot_url IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (row["email"],),
                    ).fetchone()
                    students.append({
                        "email": row["email"],
                        "name": row["name"] or row["email"],
                        "status": "present",
                        "snapshot_url": snapshot["snapshot_url"] if snapshot else None,
                    })
            return {
                "status": "success",
                "present_count": len(present_emails),
                "expected_count": len(group_students),
                "students": students,
            }
    except Exception as exc:
        logger.exception("Could not load active session attendance")
        return {"status": "error", "present_count": 0, "expected_count": 0, "students": [], "message": str(exc)}

@app.get("/my_absences")
def my_absences(email: str):
    """Return absence records with the scheduled lectures for each recorded date."""
    try:
        ensure_academic_schema()
        with closing(sqlite3.connect(DATABASE_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            user = conn.execute("SELECT department, group_name FROM users WHERE email=?", (email,)).fetchone()
            if not user:
                return {"status": "error", "absences": [], "total": 0, "message": "المستخدم غير موجود."}
            department_name = normalize_department_name(user["department"])
            group_name = normalize_group_name(user["group_name"])
            rows = conn.execute(
                """SELECT a.id,
                          COALESCE(strftime('%Y-%m-%d', a.timestamp), substr(a.timestamp,1,10), '') AS absence_date,
                          COALESCE(s.subject, 'Unspecified Lecture') AS subject,
                          COALESCE(s.start_time, '') AS start_time,
                          COALESCE(s.end_time, '') AS end_time,
                          COALESCE(s.lecture_type, '') AS lecture_type
                   FROM attendance a
                   LEFT JOIN lecture_schedule s ON s.id=(
                     SELECT s2.id FROM lecture_schedule s2
                     WHERE s2.department=? AND s2.group_name=?
                       AND s2.weekday=(CAST(strftime('%w', a.timestamp) AS INTEGER) + 6) % 7
                     ORDER BY ABS((CAST(substr(s2.start_time,1,2) AS INTEGER)*60 + CAST(substr(s2.start_time,4,2) AS INTEGER)) - (CAST(strftime('%H',a.timestamp) AS INTEGER)*60 + CAST(strftime('%M',a.timestamp) AS INTEGER))), s2.start_time
                     LIMIT 1
                   )
                   WHERE a.student_email=? AND lower(trim(a.status)) IN ('absent','missed')
                   ORDER BY a.timestamp DESC, s.start_time""",
                (department_name, group_name, email),
            ).fetchall()
        absences = [{
            "id": row["id"],
            "subject": row["subject"],
            "date": row["absence_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "type": row["lecture_type"],
        } for row in rows]
        return {"status": "success", "absences": absences, "total": len(absences)}
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "absences": [], "total": 0, "message": str(exc)}

@app.get("/rep-dashboard-data/")
def rep_dashboard_data(email: str, class_id: str):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT role FROM users WHERE email = ?", (email,))
    role_res = cursor.fetchone()
    if not role_res:
        conn.close()
        return {"status": "denied", "message": "المستخدم غير موجود."}
    role = role_res[0]
    
    cursor.execute("SELECT email FROM users WHERE role IN ('rep','representative','group_rep') AND class_id = ?", (class_id,))
    main_rep_res = cursor.fetchone()
    main_rep_email = main_rep_res[0] if main_rep_res else ""
    
    cursor.execute("SELECT id FROM attendance WHERE student_email = ? AND status = 'present'", (main_rep_email,))
    is_main_rep_present = cursor.fetchone() is not None

    if role in MAIN_REPRESENTATIVE_ROLES and not is_main_rep_present:
        conn.close()
        return {"status": "denied", "message": "عذراً، يجب تسجيل حضورك كطالب أولاً لتتمكن من إدارة القاعة."}
        
    if role == 'deputy_rep' and is_main_rep_present:
        conn.close()
        return {"status": "denied", "message": "الممثل الرئيسي حاضر ويتولى إدارة الحضور حالياً."}

    cursor.execute("SELECT COUNT(DISTINCT student_email) FROM attendance WHERE class_id = ?", (class_id,))
    app_count = cursor.fetchone()[0]
    
    conn.close()
    return {"status": "success", "app_count": app_count}

# --- ط®ظˆط§ط±ط²ظ…ظٹط© ط§ظ„ظ…ط·ط§ط¨ظ‚ط© ظˆط§ظ„ظ€ OTP ---

@app.post("/submit-audit/")
def submit_audit(class_id: str = Form(...), actual_count: int = Form(...)):
    return {"status": "error", "message": "استخدم إجراء توليد OTP وإرساله إلى جروب تيليجرام."}

@app.post("/verify-otp/")
def verify_otp(email: str = Form(...), class_id: str = Form(...), otp: str = Form(...)):
    return {"status": "error", "message": "تحقق الحضور يتم عبر مسار تسجيل الحضور الآمن."}

@app.get("/doctor-report/")
def doctor_report(class_id: str):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.name, u.email, a.status, a.timestamp 
        FROM attendance a 
        JOIN users u ON a.student_email = u.email 
        WHERE a.class_id = ?
    """, (class_id,))
    records = cursor.fetchall()
    conn.close()
    
    report = [{"name": r[0], "email": r[1], "status": r[2], "time": r[3]} for r in records]
    return {"status": "success", "report": report}


def initialize_api_database():
    """Initialize API-owned SQLite schemas and seed data without starting workers."""
    ensure_academic_schema()
    with closing(sqlite3.connect(DATABASE_PATH)) as conn, conn:
        seed_doctor_accounts(conn)
        seed_master_schedule(conn)
    ensure_materials_table()


initialize_api_database()
