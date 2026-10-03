# Telegram lecture PDF bot: 3-step setup

## 1. Create Google Drive access

Generate an OAuth user token with `authenticate.py` and keep `token.json` in the project root; `.gitignore` excludes the token. The configured root folder is `12Dz6_w7C3OAZmyOHx5kq1L8okOYla_7A`. The OAuth user must have access to that folder. The worker looks up or creates a subfolder named for the resolved subject, makes a new subfolder link-viewable, then uploads the PDF and makes the file link-viewable. Google Workspace policy must allow public reader permissions for both folder and file.

## 2. Configure Telegram groups and topics

The standalone worker in `bot.py` is the only process that polls `TELEGRAM_BOT_TOKEN`; FastAPI does not poll Telegram. The worker posts metadata to `SAJJILNI_API_URL` using the `X-API-Key` header and `LECTURE_INGEST_API_KEY`. Add the bot to each target supergroup and set `TELEGRAM_LECTURE_CHAT_IDS` to allowed numeric chat IDs, comma-separated (leave empty to accept every group). `TELEGRAM_LECTURE_DEPARTMENTS` maps chat IDs to departments, optionally with a group name. Use `/list_subjects` to fetch the backend catalog. An administrator must link each forum topic once by sending `/link_subject <subject_id or exact subject name>` inside that topic. The bot saves the matched numeric subject ID in `topic_map.json`, keyed primarily by `chat_id:thread_id` (with a thread-only key for simple lookup); the mapping survives worker restarts. Unlinked topics reject PDFs and ask an administrator to link the topic first.

Drive folders follow `root / department / subject`, so same-named subjects in different departments remain separate. Lecture ingestion resolves the subject name to a department-scoped `lecture_subjects.id` and stores `subject_id`, department, and topic metadata in both `materials` and the `lectures` table.

## 3. Connect the worker to Sajjilni

Set the same random `LECTURE_INGEST_API_KEY` on the FastAPI service and the bot worker, and set `SAJJILNI_API_URL` to the deployed backend's `/api/lectures/add` URL. Install dependencies with `pip install -r requirements.txt`, then run the worker with `python bot.py`. In Render, deploy the web service and worker from `render.yaml`, add the OAuth `token.json` under the worker's **Environment → Secret Files** as `/etc/secrets/token.json`, and set allowed chat IDs, group/dept mappings, folder ID, and backend URL. The worker's persistent disk stores `topic_map.json` at `/var/data/topic_map.json`. The worker downloads each PDF to memory, uploads it to Drive, and submits the Drive viewer/download URLs, Telegram message backup URL, subject, department, group, title, timestamp, and Telegram file ID to `materials`.

The bot validates PDF names, MIME type, file size, and PDF header before upload. It uses Telegram message links as backups; private-group links remain accessible only to Telegram members with access to that chat. It does not expose a Telegram bot token in the backup URL.
