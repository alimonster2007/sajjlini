"""Standalone Telegram PDF worker for lecture uploads to Google Drive."""

import asyncio
import html
import json
import logging
import os
import sys
import unicodedata
from datetime import timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
from drive_service import upload_pdf_stream_to_drive

for _console_stream in (sys.stdout, sys.stderr):
    try:
        _console_stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        pass
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sajjilni.lecture_bot")

MAX_PDF_BYTES = 20 * 1024 * 1024
API_URL = os.getenv("SAJJILNI_API_URL", "http://127.0.0.1:8000/api/lectures/add").strip()
_api_url_root = API_URL.rstrip("/")
_lecture_route_suffix = "/api/lectures/add"
if _api_url_root.endswith(_lecture_route_suffix):
    _derived_backend_url = _api_url_root[:-len(_lecture_route_suffix)]
else:
    _derived_backend_url = "http://127.0.0.1:8000"
BACKEND_URL = os.getenv("BACKEND_URL", _derived_backend_url).strip().rstrip("/")
INGEST_API_KEY = os.getenv("LECTURE_INGEST_API_KEY", "").strip()
CYBERSECURITY_CHAT_ID = int(os.getenv("TELEGRAM_CYBERSECURITY_CHAT_ID", "-100439103"))
CYBERSECURITY_DEPARTMENT = "\u0642\u0633\u0645 \u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0634\u0628\u0643\u0627\u062a \u0648\u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a"
AI_DEPARTMENT = "\u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a \u0648\u0627\u0644\u0631\u0648\u0628\u0648\u062a\u0627\u062a"
TOPIC_MAP_PATH = Path(os.getenv("TOPIC_MAP_FILE", str(ROOT / "topic_map.json"))).expanduser()
if not TOPIC_MAP_PATH.is_absolute():
    TOPIC_MAP_PATH = ROOT / TOPIC_MAP_PATH


def api_key_headers() -> dict[str, str]:
    if not INGEST_API_KEY:
        raise RuntimeError("LECTURE_INGEST_API_KEY is required by the ingestion worker.")
    try:
        INGEST_API_KEY.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("LECTURE_INGEST_API_KEY must contain ASCII characters only.") from exc
    return {"X-API-Key": INGEST_API_KEY}


def json_setting(name: str) -> dict:
    raw = os.getenv(name, "{}").strip()
    try:
        result = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be valid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{name} must be a JSON object.")
    return result


def topic_map_key(chat_id: int, thread_id: int) -> str:
    return f"{chat_id}:{thread_id}"


def load_topic_map() -> dict:
    if not TOPIC_MAP_PATH.exists():
        return {}
    with TOPIC_MAP_PATH.open("r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("topic_map.json must contain a JSON object.")
    return data


def subject_matches_bot_department(subject: dict, department_kind: str) -> bool:
    department_id = str(subject.get("department_id") or "").strip().casefold()
    if department_id:
        if department_kind == "ai":
            return department_id in {"ai", "ai_robotics"}
        return department_id in {"cybersecurity", "cyber_security"}
    department = unicodedata.normalize("NFC", str(subject.get("department") or "")).strip().casefold()
    if department_kind == "ai":
        return any(term in department for term in ("artificial intelligence", "robotics", "\u0630\u0643\u0627\u0621", "\u0627\u0644\u0631\u0648\u0628\u0648\u062a"))
    return any(term in department for term in ("cybersecurity", "cyber security", "\u0633\u064a\u0628\u0631\u0627\u0646\u064a", "\u0627\u0644\u0634\u0628\u0643\u0627\u062a"))


async def fetch_subjects(context: ContextTypes.DEFAULT_TYPE | None = None) -> list[dict]:
    department_kind = context.application.bot_data.get("department_kind") if context is not None else None
    department_id = "AI" if department_kind == "ai" else "Cybersecurity" if department_kind else None
    subjects_url = BACKEND_URL + "/api/subjects"
    if department_id:
        subjects_url += f"?department_id={department_id}"
    print(f"[DEBUG] Fetching subjects from: {subjects_url}")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(subjects_url, headers=api_key_headers())
    if response.status_code == 404:
        print(f"[DEBUG] Subjects API returned 404 for: {subjects_url}")
    print(f"Subject List Status: {response.status_code}, Response: {response.text}")
    response.raise_for_status()
    result = response.json()
    subjects = result.get("subjects")
    if result.get("status") != "success" or not isinstance(subjects, list):
        raise RuntimeError("Sajjilni API returned an invalid subject list.")
    if department_kind:
        return [subject for subject in subjects if subject_matches_bot_department(subject, department_kind)]
    return subjects


async def update_subject_topic(context: ContextTypes.DEFAULT_TYPE, subject_id: int, thread_id: int) -> None:
    url = f"{BACKEND_URL}/api/subjects/{subject_id}/topic"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.put(url, headers=api_key_headers(), json={"topic_id": thread_id})
    print(f"Subject topic update status: {response.status_code}, Response: {response.text}")
    response.raise_for_status()


def match_subject(subjects: list[dict], subject_input: str) -> dict | None:
    if subject_input.isdecimal():
        return next((row for row in subjects if str(row.get("id")) == subject_input), None)
    def normalized(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        value = "".join(char for char in value if not unicodedata.combining(char) and char != "ـ")
        value = value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))
        return " ".join("".join(char if char.isalnum() else " " for char in value).split())

    requested_name = normalized(subject_input)
    normalized_rows = [(row, normalized(str(row.get("name") or ""))) for row in subjects]
    exact = [row for row, name in normalized_rows if name == requested_name]
    if len(exact) == 1:
        return exact[0]
    if len(requested_name) >= 4:
        partial = [row for row, name in normalized_rows if name and (name in requested_name or requested_name in name)]
        if len(partial) == 1:
            return partial[0]
    return None


def allowed_chat_ids(setting_name: str = "TELEGRAM_LECTURE_CHAT_IDS") -> set[int]:
    raw_ids = os.getenv(setting_name, "")
    ids = set()
    for value in raw_ids.split(","):
        value = value.strip()
        if value:
            try:
                ids.add(int(value))
            except ValueError as exc:
                raise RuntimeError(f"{setting_name} must contain comma-separated numeric chat IDs.") from exc
    return ids


def telegram_backup_link(message) -> str | None:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id = str(chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.message_id}"
    return None


async def submit_lecture_record(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            API_URL,
            json=payload,
            headers=api_key_headers(),
        )
    print(f"DB Save Status: {response.status_code}, Response: {response.text}")
    response.raise_for_status()
    result = response.json()
    if result.get("status") != "success":
        raise RuntimeError("Sajjilni API did not accept the lecture record.")
    print(f"Resolved database subject_id: {result.get('subject_id')!r}")
    return result


async def lecture_already_ingested(telegram_file_id: str) -> bool:
    exists_url = API_URL.rsplit("/", 1)[0] + "/exists"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            exists_url,
            json={"telegram_file_id": telegram_file_id},
            headers=api_key_headers(),
        )
    response.raise_for_status()
    result = response.json()
    if result.get("status") != "success":
        raise RuntimeError("Sajjilni API could not check lecture duplication.")
    return bool(result.get("exists"))


async def reply_safely(message, text: str, thread_id: int | None, parse_mode: str | None = None) -> None:
    try:
        await message.reply_text(text, message_thread_id=thread_id, parse_mode=parse_mode)
    except Exception:
        logger.exception("Could not send the Telegram ingestion status reply")


def format_subject_list(subjects: list[dict], chat_id: int | None = None) -> str:
    legacy_topics: dict[str, list[str]] = {}
    try:
        for key, value in load_topic_map().items():
            if ":" not in str(key):
                continue
            mapped_chat, thread_id = str(key).split(":", 1)
            if chat_id is not None and mapped_chat != str(chat_id):
                continue
            mapped_id = value.get("subject_id") if isinstance(value, dict) else value
            if mapped_id is not None:
                legacy_topics.setdefault(str(mapped_id), []).append(thread_id)
    except (OSError, ValueError, json.JSONDecodeError):
        logger.warning("Could not read legacy topic map for display", exc_info=True)
    lines = ["\U0001F4CC \u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0645\u0648\u0627\u062f \u0627\u0644\u0645\u062a\u0627\u062d\u0629:"]
    for subject in subjects:
        subject_id = str(subject.get("id"))
        line = f"\u2022 ID: {subject_id} | Name: {subject.get('name')}"
        topic_id = subject.get("topic_id")
        if topic_id is None and legacy_topics.get(subject_id):
            topic_id = ", ".join(sorted(set(legacy_topics[subject_id])))
        unlinked = "\u063a\u064a\u0631 \u0645\u0631\u0628\u0648\u0637"
        line += f" | Topic ID: {topic_id if topic_id is not None else unlinked}"
        lines.append(line)
    return "\n".join(lines)

async def list_subjects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message:
        return
    try:
        subjects = await fetch_subjects(context)
        if not subjects:
            await reply_safely(message, "لا توجد مواد مسجلة حالياً.", message.message_thread_id)
            return
        text = format_subject_list(subjects, chat.id if chat else None)
        chunks = []
        current = ""
        for line in text.splitlines():
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 3500 and current:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        for chunk in chunks:
            await reply_safely(message, chunk, message.message_thread_id)
    except httpx.HTTPStatusError as exc:
        logger.error("Subject list API failed: HTTP %s; body=%s", exc.response.status_code, exc.response.text)
        await reply_safely(message, f"تعذر جلب قائمة المواد (HTTP {exc.response.status_code}).", message.message_thread_id)
    except Exception as exc:
        logger.exception("Could not list subjects")
        await reply_safely(message, f"تعذر جلب قائمة المواد: {exc}", message.message_thread_id)


async def link_subject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    thread_id = message.message_thread_id
    if chat.type not in ("group", "supergroup") or not getattr(chat, "is_forum", False) or thread_id is None:
        await reply_safely(message, "استخدم هذا الأمر داخل موضوع (Topic) في مجموعة منتدى.", thread_id)
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception as exc:
        logger.exception("Could not verify admin status for user %s in chat %s", user.id, chat.id)
        await reply_safely(message, f"تعذر التحقق من صلاحية المسؤول: {exc}", thread_id)
        return
    if member.status not in ("administrator", "creator"):
        await reply_safely(message, "هذا الأمر متاح لمسؤولي المجموعة فقط.", thread_id)
        return

    subject_input = " ".join(context.args or []).strip()
    if not subject_input:
        await reply_safely(message, "الصيغة: /link_subject <subject_id أو اسم المادة>", thread_id)
        return
    try:
        subjects = await fetch_subjects(context)
        matched = match_subject(subjects, subject_input)
        if matched is None:
            available = format_subject_list(subjects, chat.id) if subjects else "لا توجد مواد مسجلة حالياً."
            await reply_safely(
                message,
                f"لم أجد مادة مطابقة. استخدم اسماً مطابقاً أو رقماً من /list_subjects.\n{available}",
                thread_id,
            )
            return
        matched_id = int(matched["id"])
        matched_name = str(matched["name"])
        await update_subject_topic(context, matched_id, thread_id)
        logger.info(
            "Linked forum topic chat_id=%s thread_id=%s to subject_id=%s subject=%r",
            chat.id, thread_id, matched_id, matched_name,
        )
        await reply_safely(
            message,
            f"✅ تم ربط هذا الموضوع بمادة: <b>{html.escape(matched_name)}</b> (ID: {matched_id}) بنجاح!",
            thread_id,
            parse_mode="HTML",
        )
    except httpx.HTTPStatusError as exc:
        logger.error("Subject lookup API failed: HTTP %s; body=%s", exc.response.status_code, exc.response.text)
        await reply_safely(message, f"تعذر جلب المواد (HTTP {exc.response.status_code}). أعد المحاولة لاحقاً.", thread_id)
    except Exception as exc:
        logger.exception("Could not link topic to subject")
        await reply_safely(message, f"تعذر ربط المادة: {exc}", thread_id)


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.document:
        return

    document = message.document
    original_name = unicodedata.normalize("NFC", Path(document.file_name or "lecture.pdf").name)
    thread_id = message.message_thread_id
    if Path(original_name).suffix.lower() == ".pdf":
        logger.info("[INGEST] Received PDF: %s | Chat ID: %s | Topic ID: %s", original_name, chat.id, thread_id)
        logger.info("[INGEST] File size: %s bytes", document.file_size or "unknown")
    else:
        logger.info("[INGEST] Ignored non-PDF document: %s | Chat ID: %s | Topic ID: %s", original_name, chat.id, thread_id)
    if Path(original_name).suffix.lower() != ".pdf" or document.mime_type not in (None, "application/pdf"):
        await reply_safely(message, "⚠️ يرجى إرسال ملف PDF فقط.", thread_id)
        return
    if document.file_size and document.file_size > MAX_PDF_BYTES:
        logger.warning("Skipped oversized PDF from chat %s", chat.id)
        await reply_safely(message, "⚠️ حجم الملف يتجاوز الحد المسموح به (20 ميغابايت).", thread_id)
        return

    caption = (message.caption or "").strip()
    topic_names = context.application.bot_data.setdefault("topic_names", {})
    topic_name = topic_names.get((chat.id, thread_id), "") if thread_id is not None else ""
    if thread_id is not None and not topic_name:
        get_topic = getattr(context.bot, "get_forum_topic", None)
        if get_topic:
            try:
                topic = await get_topic(chat_id=chat.id, message_thread_id=thread_id)
                topic_name = str(getattr(topic, "name", "") or "").strip()
                if topic_name:
                    topic_names[(chat.id, thread_id)] = topic_name
            except Exception:
                logger.debug("Could not retrieve Telegram topic title for chat=%s thread=%s", chat.id, thread_id, exc_info=True)
    lecture_title = caption or Path(original_name).stem
    bot_data = context.application.bot_data
    department_kind = str(bot_data.get("department_kind") or "cybersecurity")
    group_map = json_setting("TELEGRAM_LECTURE_DEPARTMENTS")
    group_config = group_map.get(str(chat.id), {})
    group_name = str(group_config.get("group_name") or "") if isinstance(group_config, dict) else ""
    department = AI_DEPARTMENT if department_kind == "ai" else CYBERSECURITY_DEPARTMENT
    try:
        available_subjects = await fetch_subjects(context)
    except Exception as exc:
        logger.exception("Could not fetch subjects for topic resolution")
        await reply_safely(message, f"\u062a\u0639\u0630\u0631 \u062c\u0644\u0628 \u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0645\u0648\u0627\u062f: {exc}", thread_id)
        return

    topic_subject = next(
        (subject for subject in available_subjects if str(subject.get("topic_id")) == str(thread_id)),
        None,
    )
    if topic_subject is None:
        # Read legacy mappings during migration; new links are stored on the subject row.
        try:
            legacy_mappings = load_topic_map()
            legacy_value = legacy_mappings.get(topic_map_key(chat.id, thread_id))
            if legacy_value is None:
                legacy_value = legacy_mappings.get(str(thread_id))
            if isinstance(legacy_value, dict):
                legacy_value = legacy_value.get("subject_id") or legacy_value.get("subject")
            if legacy_value is not None:
                topic_subject = match_subject(available_subjects, str(legacy_value))
        except Exception:
            logger.warning("Could not read legacy topic_map.json", exc_info=True)

    if topic_subject is None and thread_id is not None:
        for candidate in (topic_name, caption, Path(original_name).stem):
            topic_subject = match_subject(available_subjects, candidate) if candidate else None
            if topic_subject is not None:
                try:
                    await update_subject_topic(context, int(topic_subject["id"]), thread_id)
                    topic_subject["topic_id"] = thread_id
                    logger.info("Auto-linked topic %r (thread_id=%s) to subject_id=%s", topic_name, thread_id, topic_subject["id"])
                except Exception:
                    logger.exception("Could not persist topic link for thread %s", thread_id)
                    topic_subject = None
                break

    if topic_subject is None:
        logger.warning("Unlinked forum topic chat_id=%s thread_id=%s topic_name=%r", chat.id, thread_id, topic_name)
        await reply_safely(message, "\u26a0\ufe0f \u0647\u0630\u0627 \u0627\u0644\u0645\u0648\u0636\u0648\u0639 \u063a\u064a\u0631 \u0645\u0631\u0628\u0648\u0637 \u0628\u0645\u0627\u062f\u0629. \u0623\u0631\u0633\u0644 /subject_list \u0644\u0645\u0639\u0631\u0641\u0629 \u0627\u0644\u0645\u0648\u0627\u062f \u0627\u0644\u0645\u062a\u0627\u062d\u0629 \u0623\u0648 \u0627\u0633\u062a\u062e\u062f\u0645 /link_subject \u0627\u0633\u0645_\u0627\u0644\u0645\u0627\u062f\u0629.", thread_id)
        return
    mapping_input = str(topic_subject["id"])
    if not mapping_input:
        await reply_safely(message, "⚠️ ربط المادة غير صالح. أعد ربط الموضوع باستخدام /link_subject اسم_المادة", thread_id)
        return
    try:
        stable_file_id = document.file_unique_id or document.file_id
        if await lecture_already_ingested(stable_file_id):
            logger.info("Skipped previously ingested Telegram PDF %s", stable_file_id)
            return

        subjects = await fetch_subjects(context)
        resolved_subject = match_subject(subjects, mapping_input)
        if resolved_subject is None:
            logger.error(
                "Mapped subject no longer matches the backend catalog (chat_id=%s, thread_id=%s, mapping=%r)",
                chat.id, thread_id, mapping_input,
            )
            await reply_safely(
                message,
                "⚠️ المادة المرتبطة غير موجودة في قائمة المواد. استخدم /list_subjects ثم أعد الربط عبر /link_subject.",
                thread_id,
            )
            return
        subject = unicodedata.normalize("NFC", str(resolved_subject["name"]).strip())
        mapped_subject_id = int(resolved_subject["id"])
        subject_department = str(resolved_subject.get("department") or "").strip()
        if department_kind == "ai":
            if not subject_matches_bot_department(resolved_subject, "ai"):
                raise ValueError("The selected subject does not belong to the AI department.")
            department = subject_department or AI_DEPARTMENT
        elif chat.id == CYBERSECURITY_CHAT_ID:
            department = CYBERSECURITY_DEPARTMENT
        elif subject_department:
            department = unicodedata.normalize("NFC", subject_department)
        logger.info(
            "Using catalog subject department=%r for subject_id=%s (chat_department=%r)",
            department, mapped_subject_id, group_config.get("department") if isinstance(group_config, dict) else group_config,
        )

        telegram_file = await document.get_file()
        pdf_bytes = bytes(await telegram_file.download_as_bytearray())
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise ValueError("Downloaded PDF exceeds the configured file-size limit.")
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("The uploaded document does not contain a valid PDF header.")

        drive_record = await asyncio.to_thread(
            upload_pdf_stream_to_drive,
            pdf_bytes,
            original_name,
            department_name=department,
            subject_name=subject,
        )
        created_at = message.date.astimezone(timezone.utc).isoformat()
        payload = {
            "title": lecture_title,
            "lecture_title": lecture_title,
            "subject": subject,
            "subject_name": subject,
            "subject_id": mapped_subject_id,
            "file_name": original_name,
            "drive_url": drive_record["web_view_link"],
            "google_drive_url": drive_record["web_view_link"],
            "google_drive_download_url": drive_record["direct_download_link"],
            "drive_file_id": drive_record["file_id"],
            "telegram_backup_url": telegram_backup_link(message),
            "telegram_file_id": document.file_id,
            "telegram_file_unique_id": stable_file_id,
            "upload_date": created_at,
            "created_at": created_at,
            "department": department,
            "department_name": department,
            "bot_department": "AI" if department_kind == "ai" else "Cybersecurity",
            "group_name": group_name,
            "topic_id": thread_id,
            "chat_id": chat.id,
            "file_size": len(pdf_bytes),
        }
        logger.info(
            "Resolved lecture subject: chat_id=%s topic_id=%s topic_name=%r subject=%r configured_subject_id=%r",
            chat.id, thread_id, topic_name, subject, mapped_subject_id,
        )
        print(f"Selected subject_id: {mapped_subject_id!r} (subject={subject!r}, chat_id={chat.id}, topic_id={thread_id})")
        print("DB payload: " + json.dumps(payload, ensure_ascii=False, default=str))
        try:
            result = await submit_lecture_record(payload)
        except Exception as e:
            print(f"❌ DB Save Failed: {e}")
            logger.exception(
                "DB/API save failed after Drive upload (drive_file_id=%s, subject_id=%r)",
                drive_record.get("file_id"), mapped_subject_id,
            )
            raise
        if result.get("database_saved") is False:
            e = result.get("database_error") or result.get("database_warning") or "unknown database error"
            print(f"❌ DB Save Failed: {e}")
            logger.warning(
                "Drive upload succeeded but database indexing failed: %s (drive_file_id=%s)",
                e,
                drive_record["file_id"],
            )
        logger.info(
            "Lecture ingested: subject=%r file=%r duplicate=%s record_id=%s",
            subject,
            original_name,
            result.get("duplicate", False),
            result.get("id"),
        )
        if department_kind == "ai":
            confirmation = f"\u2705 \u062a\u0645 \u062d\u0641\u0638 \u0627\u0644\u0645\u0644\u0632\u0645\u0629 \u0628\u0646\u062c\u0627\u062d \u0641\u064a \u0642\u0633\u0645 \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a - \u0645\u0627\u062f\u0629: {subject}"
        else:
            confirmation = f"\u2705 \u062a\u0645 \u0631\u0641\u0639 \u0627\u0644\u0645\u0644\u0632\u0645\u0629 \u0628\u0646\u062c\u0627\u062d \u0625\u0644\u0649 \u062a\u0637\u0628\u064a\u0642 \u0633\u062c\u0644\u0646\u064a \u0648 Google Drive!\n\U0001F4CC \u0627\u0644\u0645\u0627\u062f\u0629: {subject}\n\U0001F517 \u0631\u0627\u0628\u0637 \u0627\u0644\u0645\u0639\u0627\u064a\u0646\u0629: {drive_record['web_view_link']}"
        await reply_safely(message, confirmation, thread_id)
    except httpx.HTTPStatusError as exc:
        response = exc.response
        logger.error(
            "FastAPI lecture upload failed: HTTP %s; response body: %s",
            response.status_code,
            response.text,
        )
        await reply_safely(
            message,
            f"❌ فشل حفظ بيانات الملزمة في الخادم (HTTP {response.status_code}).",
            thread_id,
        )
    except (httpx.HTTPError, asyncio.TimeoutError, OSError, RuntimeError, ValueError, KeyError) as exc:
        logger.exception("Lecture PDF ingestion failed for chat %s", chat.id)
        await reply_safely(message, f"❌ تعذر رفع الملزمة أو حفظ بياناتها: {exc}", thread_id)
    except Exception as exc:
        logger.exception("Unexpected lecture PDF ingestion error for chat %s", chat.id)
        await reply_safely(message, f"❌ حدث خطأ غير متوقع أثناء المعالجة: {exc}", thread_id)


async def remember_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message and chat and message.forum_topic_created and message.message_thread_id is not None:
        context.application.bot_data.setdefault("topic_names", {})[(chat.id, message.message_thread_id)] = message.forum_topic_created.name
        logger.info("Registered Telegram topic %s in chat %s", message.forum_topic_created.name, chat.id)


def build_application(
    token: str | None = None,
    *,
    department_kind: str = "cybersecurity",
    chat_id_setting: str = "TELEGRAM_LECTURE_CHAT_IDS",
) -> Application:
    token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("Set the Telegram token environment variable for this lecture worker.")
    chat_ids = allowed_chat_ids(chat_id_setting)
    application = ApplicationBuilder().token(token).concurrent_updates(8).build()
    application.bot_data["department_kind"] = department_kind
    application.bot_data["department_name"] = AI_DEPARTMENT if department_kind == "ai" else CYBERSECURITY_DEPARTMENT
    application.add_handler(CommandHandler("list_subjects", list_subjects_command))
    application.add_handler(CommandHandler("subject_list", list_subjects_command))
    application.add_handler(CommandHandler("link_subject", link_subject_command))
    application.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, remember_topic))
    chat_filter = filters.Chat(chat_id=chat_ids) if chat_ids else filters.ChatType.GROUPS
    application.add_handler(
        MessageHandler(chat_filter & filters.Document.ALL, handle_pdf, block=False)
    )
    return application


async def run_application_polling(application: Application, label: str) -> None:
    initialized = started = polling = False
    try:
        await application.initialize()
        initialized = True
        await application.start()
        started = True
        if application.updater is None:
            raise RuntimeError(f"{label} Telegram application has no polling updater.")
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        polling = True
        logger.info("Started %s Telegram bot listener", label)
        await asyncio.Event().wait()
    finally:
        if polling and application.updater is not None:
            await application.updater.stop()
        if started:
            await application.stop()
        if initialized:
            await application.shutdown()


async def run_applications(applications: list[tuple[str, Application]]) -> None:
    tasks = [asyncio.create_task(run_application_polling(application, label)) for label, application in applications]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    cyber_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    ai_token = os.getenv("AI_BOT_TOKEN", "").strip()
    if not cyber_token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN for the Cybersecurity lecture worker.")

    applications = [(
        "Cybersecurity",
        build_application(cyber_token, department_kind="cybersecurity", chat_id_setting="TELEGRAM_LECTURE_CHAT_IDS"),
    )]
    if ai_token:
        if ai_token == cyber_token:
            raise RuntimeError("AI_BOT_TOKEN must be different from TELEGRAM_BOT_TOKEN.")
        applications.append((
            "AI",
            build_application(ai_token, department_kind="ai", chat_id_setting="AI_BOT_CHAT_IDS"),
        ))
    else:
        logger.warning("AI_BOT_TOKEN is not set; the AI Telegram listener will not start.")

    asyncio.run(run_applications(applications))


if __name__ == "__main__":
    main()
