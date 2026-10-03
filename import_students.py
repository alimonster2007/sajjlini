"""Bulk-import Group B student accounts from a CSV file.

CSV headers: email,name
New accounts use the one-time password B2026 and must change it at first login.
"""

import argparse
import os
import sys
from pathlib import Path

from database import import_students_csv


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Group B students from CSV")
    parser.add_argument("csv_file", help="UTF-8 CSV with email and name columns")
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH") or str(Path(__file__).with_name("attendance.db")),
        help="SQLite database path (defaults to DATABASE_PATH or attendance.db)",
    )
    args = parser.parse_args()

    try:
        result = import_students_csv(args.csv_file, db_path=args.database)
    except (OSError, ValueError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    print(f"Imported: {result['inserted']}; skipped or already present: {result['skipped']}")
    print("New students sign in with the temporary password B2026 and must set a new password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
