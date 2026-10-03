import sqlite3
import hashlib
import hmac
import secrets
import csv
import json
import re
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"), override=False)

DEFAULT_DATABASE_PATH = Path(__file__).with_name("database.db")


def resolve_database_path(db_path=None) -> str:
    if db_path:
        path = Path(db_path).expanduser()
    else:
        configured_path = os.getenv("DATABASE_PATH", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()
        if configured_path:
            path = Path(configured_path).expanduser()
        elif database_url.startswith("sqlite:///"):
            path = Path(database_url[len("sqlite:///"):]).expanduser()
        elif database_url:
            raise ValueError("This app currently supports SQLite DATABASE_URL values only; set DATABASE_PATH for SQLite storage.")
        else:
            path = DEFAULT_DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


PASSWORD_HASH_PREFIX = "pbkdf2_sha256$"
PASSWORD_HASH_ITERATIONS = 310_000
DEFAULT_STUDENT_PASSWORD = "B2026"
DEFAULT_DOCTOR_PASSWORD = "B2026"
DOCTOR_ACCOUNTS = (
    ("qabas@uob.edu.iq", "\u062f\u0643\u062a\u0648\u0631\u0629 \u0642\u0628\u0633", "CyberSecurity"),
    ("m.talal@uob.edu.iq", "\u062f. \u0645\u062d\u0645\u062f \u0637\u0644\u0627\u0644", "CyberSecurity"),
    ("dr.ali@uob.edu.iq", "\u062f. \u0639\u0644\u064a", "CyberSecurity"),
    ("narjis@uob.edu.iq", "\u0645.\u0645. \u0646\u0631\u062c\u0633", "CyberSecurity"),
    ("nermin@uob.edu.iq", "\u0645.\u0645. \u0646\u064a\u0631\u0645\u064a\u0646 \u0645\u062c\u064a\u062f", "CyberSecurity"),
    ("doctor@uob.edu.iq", "\u062d\u0633\u0627\u0628 \u062f\u0643\u062a\u0648\u0631 \u062a\u062c\u0631\u064a\u0628\u064a", "CyberSecurity"),
    ("abbas@uob.edu.iq", "\u062f. \u0639\u0628\u0627\u0633", "CyberSecurity"),
    ("rana@uob.edu.iq", "\u062f. \u0631\u0646\u0627", "CyberSecurity"),
    ("nawras@uob.edu.iq", "\u0645.\u0645. \u0646\u0648\u0631\u0633", "CyberSecurity"),
    ("shihab@uob.edu.iq", "\u062f. \u0634\u0647\u0627\u0628", "AI_Robotics"),
    ("tiba@uob.edu.iq", "\u062f. \u0637\u064a\u0628\u0629", "AI_Robotics"),
    ("zainab@uob.edu.iq", "\u062f. \u0632\u064a\u0646\u0628", "AI_Robotics"),
)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return f"{PASSWORD_HASH_PREFIX}{PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_password: str) -> bool:
    if not stored_password:
        return False
    if not stored_password.startswith(PASSWORD_HASH_PREFIX):
        return hmac.compare_digest(password, stored_password)
    try:
        _, iterations, salt_hex, digest_hex = stored_password.split("$", 3)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def ensure_user_schema(conn: sqlite3.Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, role TEXT NOT NULL, class_id TEXT, device_uuid TEXT,
        department TEXT, group_name TEXT, display_role TEXT, college TEXT, stage TEXT,
        is_first_login INTEGER NOT NULL DEFAULT 1, reference_face_path TEXT, password_hash TEXT
    )''')
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    additions = {
        "department": "TEXT", "group_name": "TEXT", "display_role": "TEXT",
        "department_id": "TEXT", "created_at": "DATETIME",
        "college": "TEXT", "stage": "TEXT", "is_first_login": "INTEGER NOT NULL DEFAULT 1",
        "reference_face_path": "TEXT", "password_hash": "TEXT",
    }
    for column, declaration in additions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
    conn.execute("UPDATE users SET department_id=COALESCE(department_id,class_id)")
    conn.execute("UPDATE users SET password_hash=COALESCE(password_hash,password)")


def seed_doctor_accounts(conn: sqlite3.Connection) -> None:
    """Create/update configured doctor profiles without resetting changed passwords."""
    ensure_user_schema(conn)
    for email, name, department in DOCTOR_ACCOUNTS:
        existing = conn.execute(
            "SELECT password, is_first_login FROM users WHERE lower(email)=lower(?)",
            (email,),
        ).fetchone()
        password_hash = None
        if existing is None or (existing[1] and not verify_password(DEFAULT_DOCTOR_PASSWORD, existing[0])):
            password_hash = hash_password(DEFAULT_DOCTOR_PASSWORD)
        if existing is None:
            conn.execute(
                """INSERT INTO users
                   (name,email,password,password_hash,role,class_id,department,group_name,is_first_login)
                   VALUES (?,?,?,?,'doctor',?,?,NULL,1)""",
                (name, email, password_hash, password_hash, department, department),
            )
        else:
            conn.execute(
                """UPDATE users SET name=?, role='doctor', class_id=?,
                   department=?, group_name=NULL,
                   password=COALESCE(?,password),password_hash=COALESCE(?,password_hash) WHERE lower(email)=lower(?)""",
                (name, department, department, password_hash, password_hash, email),
            )


SCHEDULE_DAY_INDEX = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
    "Friday": 4, "Saturday": 5, "Sunday": 6,
}
SCHEDULE_DEPARTMENT_LABELS = {
    "CyberSecurity": "\u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a",
    "AI_Robotics": "\u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a \u0648\u0627\u0644\u0631\u0648\u0628\u0648\u062a\u0627\u062a",
}


def ensure_schedule_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS lecture_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT, weekday INTEGER NOT NULL, start_time TEXT NOT NULL,
        department TEXT NOT NULL, group_name TEXT NOT NULL, subject TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1, end_time TEXT, lecture_type TEXT, instructor TEXT,
        room TEXT, stage TEXT, doctor_email TEXT, department_code TEXT, doctor_name TEXT,
        UNIQUE(weekday,start_time,department,group_name)
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lecture_schedule)")}
    for column in ("end_time", "lecture_type", "instructor", "room", "stage", "doctor_email", "department_code", "doctor_name"):
        if column not in columns:
            conn.execute(f"ALTER TABLE lecture_schedule ADD COLUMN {column} TEXT")


def _schedule_time_to_24h(value: str, is_end: bool = False, start_hour: int | None = None) -> str:
    hour_text, minute_text = value.strip().split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    if hour in (1, 2) and (not is_end or start_hour is None or hour < start_hour):
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def seed_master_schedule(conn: sqlite3.Connection, schedule_data=None) -> int:
    ensure_schedule_schema(conn)
    if schedule_data is None:
        with Path(__file__).with_name("schedule.json").open("r", encoding="utf-8") as source:
            schedule_data = json.load(source)
    conn.execute("UPDATE lecture_schedule SET is_active=0")
    rows = []
    for item in schedule_data:
        day = str(item["day"]).strip()
        department_code = str(item["department"]).strip()
        group = str(item["group"]).strip().replace("Group ", "").upper()
        start_label, end_label = [part.strip() for part in str(item["time"]).split("-", 1)]
        start_time = _schedule_time_to_24h(start_label)
        start_hour = int(start_time.split(":", 1)[0])
        end_time = _schedule_time_to_24h(end_label, is_end=True, start_hour=start_hour)
        department_label = SCHEDULE_DEPARTMENT_LABELS[department_code]
        rows.append((
            SCHEDULE_DAY_INDEX[day], start_time, department_label, group, str(item["subject"]).strip(),
            end_time, str(item["type"]).strip(), "", str(item["classroom"]).strip(),
            str(item.get("stage") or "Second Stage").strip(), str(item["doctor_email"]).strip().lower(),
            department_code, str(item.get("doctor_name") or "").strip() or None,
        ))
    conn.executemany(
        """INSERT INTO lecture_schedule
           (weekday,start_time,department,group_name,subject,end_time,lecture_type,instructor,room,stage,doctor_email,department_code,doctor_name,is_active)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(weekday,start_time,department,group_name) DO UPDATE SET
           subject=excluded.subject,end_time=excluded.end_time,lecture_type=excluded.lecture_type,
           instructor=excluded.instructor,room=excluded.room,stage=excluded.stage,
           doctor_email=excluded.doctor_email,department_code=excluded.department_code,
           doctor_name=excluded.doctor_name,is_active=1""",
        rows,
    )
    return len(rows)


def import_students(rows, db_path=None):
    """Insert new Group B students using the one-time default password."""
    conn = sqlite3.connect(resolve_database_path(db_path))
    try:
        ensure_user_schema(conn)
        inserted = 0
        skipped = 0
        for email, full_name in rows:
            email = str(email or "").strip().lower()
            full_name = str(full_name or "").strip()
            if not re.fullmatch(r"[^@\s]+@uob\.edu\.iq", email, flags=re.IGNORECASE) or not full_name:
                skipped += 1
                continue
            cursor = conn.execute(
                """INSERT OR IGNORE INTO users
                   (name,email,password,role,class_id,department,group_name,is_first_login)
                   VALUES (?,?,?,'student','CyberSecurity','CyberSecurity','B',1)""",
                (full_name, email, hash_password(DEFAULT_STUDENT_PASSWORD)),
            )
            inserted += cursor.rowcount
            skipped += 1 - cursor.rowcount
        conn.commit()
        return {"inserted": inserted, "skipped": skipped}
    finally:
        conn.close()


def import_students_csv(csv_path, db_path=None):
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not {"email", "name"}.issubset({field.strip().lower() for field in reader.fieldnames}):
            raise ValueError("CSV must include email and name columns.")
        field_map = {field.strip().lower(): field for field in reader.fieldnames}
        return import_students(
            ((row.get(field_map["email"]), row.get(field_map["name"])) for row in reader),
            db_path=db_path,
        )

def init_db(db_path=None):
    conn = sqlite3.connect(resolve_database_path(db_path))
    cursor = conn.cursor()
    
    # 1. جدول المستخدمين الموحد
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,         -- doctor, representative, deputy_rep, student
            class_id TEXT,
            device_uuid TEXT,           -- بصمة الجهاز لتمنع فتح أكثر من حساب من نفس الموبايل
            department TEXT,
            group_name TEXT,
            display_role TEXT,
            college TEXT,
            stage TEXT,
            is_first_login INTEGER NOT NULL DEFAULT 1
        )
    ''')
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    for column in ("department", "group_name", "display_role", "college", "stage"):
        if column not in user_columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    if "is_first_login" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_first_login INTEGER NOT NULL DEFAULT 1")
    
    # 2. جدول سجل الحضور اليومي
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_email TEXT NOT NULL,
            class_id TEXT NOT NULL,
            status TEXT NOT NULL,        -- 'present', 'absent', 'appealed'
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # إضافة حسابات تجريبية
    users_data = [
        ("د. أحمد", "doctor@uob.edu.iq", "123456", "doctor", "CyberSecurity", None),
        ("سجاد (الممثل المساعد)", "sajjad@uob.edu.iq", "sajjad2026", "deputy_rep", "CyberSecurity", None),
        ("محمد (طالب 1)", "student1@uob.edu.iq", "pass123", "student", "CyberSecurity", None),
        ("حسن (طالب 2)", "student2@uob.edu.iq", "pass123", "student", "CyberSecurity", None)
    ]
    
    try:
        cursor.executemany("INSERT INTO users (name, email, password, role, class_id, device_uuid) VALUES (?, ?, ?, ?, ?, ?)", users_data)
        print("✅ تم إعادة تأسيس قاعدة البيانات وإضافة الجداول الحسابات بنجاح!")
    except sqlite3.IntegrityError:
        print("الحسابات موجودة مسبقاً.")

    cursor.execute("""
        INSERT INTO users (name, email, password, role, class_id, device_uuid)
        VALUES (?, ?, ?, 'doctor', 'CyberSecurity', NULL)
        ON CONFLICT(email) DO UPDATE SET
            name=excluded.name, password=excluded.password, role='doctor', class_id=excluded.class_id
    """, ("د. أحمد المحمداوي", "dr.ahmed@uob.edu.iq", "doc12345"))

    cursor.execute("UPDATE users SET name = ? WHERE email = ?", ("د. أحمد المحمداوي", "dr.ahmed@uob.edu.iq"))
    cursor.execute("UPDATE users SET name = ? WHERE email = ?", ("\u062f. \u0623\u062d\u0645\u062f \u0627\u0644\u0645\u062d\u0645\u062f\u0627\u0648\u064a", "dr.ahmed@uob.edu.iq"))
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    if "department" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN department TEXT")
    if "group_name" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN group_name TEXT")
    attendance_columns = {row[1] for row in cursor.execute("PRAGMA table_info(attendance)").fetchall()}
    if "department" not in attendance_columns:
        cursor.execute("ALTER TABLE attendance ADD COLUMN department TEXT")
    if "group_name" not in attendance_columns:
        cursor.execute("ALTER TABLE attendance ADD COLUMN group_name TEXT")
    cursor.execute("""CREATE TABLE IF NOT EXISTS lecture_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT, weekday INTEGER NOT NULL, start_time TEXT NOT NULL,
        department TEXT NOT NULL, group_name TEXT NOT NULL, subject TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1, UNIQUE(weekday,start_time,department,group_name)
    )""")
    cyber = "\u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a"
    ai = "\u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a"
    cursor.execute("""INSERT INTO users (name,email,password,role,class_id,department,group_name)
        VALUES (?,?,'student123','student','CyberSecurity',?,'B')
        ON CONFLICT(email) DO UPDATE SET name=excluded.name,password=excluded.password,role='student',
        class_id='CyberSecurity',department=excluded.department,group_name='B'""",
        ("\u0639\u0644\u064a \u0639\u0644\u064a", "student@uob.edu.iq", cyber))
    cursor.execute("""CREATE TABLE IF NOT EXISTS schedule_overrides (
        schedule_id INTEGER NOT NULL, lecture_date TEXT NOT NULL, new_time TEXT,
        status TEXT NOT NULL DEFAULT 'active', PRIMARY KEY(schedule_id,lecture_date)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS scheduled_otp_log (
        schedule_id INTEGER NOT NULL, lecture_date TEXT NOT NULL, sent_at TEXT NOT NULL,
        PRIMARY KEY(schedule_id,lecture_date)
    )""")
    schedule_rows = [
        (6, '08:30', cyber, 'A', "\u062a\u0634\u0641\u064a\u0631 \u0648\u0623\u0645\u0646 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a"),
        (6, '10:30', cyber, 'B', "\u062a\u0634\u0641\u064a\u0631 \u0648\u0623\u0645\u0646 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a"),
        (0, '08:30', ai, 'A', "\u0645\u0642\u062f\u0645\u0629 \u0641\u064a \u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a"),
        (0, '10:30', ai, 'B', "\u0645\u0642\u062f\u0645\u0629 \u0641\u064a \u0647\u0646\u062f\u0633\u0629 \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a"),
    ]
    cursor.executemany("INSERT OR IGNORE INTO lecture_schedule(weekday,start_time,department,group_name,subject) VALUES(?,?,?,?,?)", schedule_rows)
    seed_doctor_accounts(conn)
    seed_master_schedule(conn)
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()


