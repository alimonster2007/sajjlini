# Sajjilni deployment

## Local setup

1. Copy `.env.example` to `.env` and fill in the Telegram settings. The application loads this file locally; deployment platforms should receive the same values through their secret/environment-variable settings.
2. Install the backend dependencies and start the app:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   uvicorn main:app --reload
   ```

The root page serves `login.html`; `logo.png`, `manifest.json`, and the `static/` assets are served by the FastAPI app.

## GitHub and auto-deploy

Create a new **private** GitHub repository with no README or starter files. From this project directory, run:

```powershell
git init
git branch -M main
git add .
git status --short
git commit -m "Prepare Sajjilni deployment"
git remote add origin https://github.com/YOUR-ACCOUNT/YOUR-REPOSITORY.git
git push -u origin main
```

Review `git status --short` before committing. `.gitignore` excludes local databases, `.env` secrets, generated attendance photos, and downloaded material PDFs while retaining the static app icons. Keep the repository private because the schedule and application source are specific to the institution.

## Render

`render.yaml` defines a FastAPI web service and a separate Telegram worker. FastAPI handles HTTP requests and does not poll Telegram; `bot.py` is the only process that polls the lecture bot token and posts lecture metadata to the API with `X-API-Key`. Set `ATTENDANCE_BOT_TOKEN` and `ATTENDANCE_CHAT_ID` on the web service for attendance OTP delivery. For department/group-specific destinations, configure `TELEGRAM_GROUP_CHAT_IDS` as JSON. Set `APP_TIMEZONE=Asia/Baghdad` for the automatic schedule-based OTP dispatcher. Configure `TELEGRAM_LECTURE_CHAT_IDS` and `TELEGRAM_LECTURE_DEPARTMENTS` separately for PDF ingestion.

Locally, set `ATTENDANCE_BOT_TOKEN` in `.env`, then save the attendance group's numeric chat ID with `python configure_attendance_chat.py --chat-id -1001234567890`. The automatic dispatcher reads each due slot from `schedules` and sends once at its Baghdad-time start minute.

The persistent disk is necessary for this app's SQLite database and uploaded files; Render's default filesystem is ephemeral. The Blueprint's `starter` service plan with a disk is a paid configuration, so check the current plan and disk charges before creating it. Keep the SQLite web service to one instance. The Telegram worker runs separately and is the sole polling owner.

After the first deploy, Render can automatically deploy commits pushed to the connected branch. The database is initialized when the FastAPI module loads. The local `attendance.db`, existing attendance records, snapshots, and ignored PDFs are not copied to the cloud; transfer any data you need through a controlled migration process rather than committing those files.

Telegram polling diagnostics were removed from the FastAPI service. The bot worker logs polling and ingestion failures to its process output.

## Vercel

Vercel can run Python functions, including FastAPI, but this app writes to SQLite and local upload directories, and the Telegram worker needs a persistent process. Deploy the backend and worker on Render (or another persistent host). Vercel is suitable for a separately hosted static frontend after changing it to call the backend URL.

## Secret rotation

Store Telegram credentials only in `.env` locally and the hosting provider's secret settings. Never add `.env` or a database file to Git.
