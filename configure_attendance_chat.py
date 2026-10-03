"""Save the Telegram attendance group's numeric chat ID into the project .env."""

import argparse
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def save_chat_id(chat_id: str, env_file: Path) -> None:
    chat_id = chat_id.strip()
    if not re.fullmatch(r"-?\d+", chat_id):
        raise ValueError("Telegram chat ID must be numeric (group IDs are usually negative).")

    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    output = []
    replaced = False
    for line in lines:
        if line.partition("=")[0].strip() == "ATTENDANCE_CHAT_ID":
            output.append(f"ATTENDANCE_CHAT_ID={chat_id}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"ATTENDANCE_CHAT_ID={chat_id}")

    env_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_file.with_name(env_file.name + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, env_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", required=True, help="Numeric Telegram attendance group chat ID")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="Environment file (default: project .env)")
    args = parser.parse_args()
    save_chat_id(args.chat_id, args.env_file.expanduser().resolve())
    print(f"Saved ATTENDANCE_CHAT_ID to {args.env_file.expanduser().resolve()}")


if __name__ == "__main__":
    main()
