# Stream Recorder (Flask)

Same functionality as the Node.js version, rewritten in Python using Flask + APScheduler.

## Stack

- **Flask** — REST API + static file serving
- **APScheduler** — cron and one-shot scheduling with native timezone support
- **ffmpeg** — audio stream recording
- **Python 3.12** on `python:3.12-slim`

## Quick Start

```bash
docker compose up -d
docker compose logs -f
```

Open http://localhost:3000

## Build manually

```bash
docker build -t stream-recorder-flask .

docker run -d \
  --name stream-recorder-flask \
  -p 3000:3000 \
  -v $(pwd)/data:/data \
  -v $(pwd)/recordings:/recordings \
  -e TZ=Asia/Hong_Kong \
  stream-recorder-flask
```

## Volumes

| Path | Purpose |
|---|---|
| `/data` | `schedules.json` persistence |
| `/recordings` | Default MP3 output location |

## Update files without rebuilding

```bash
# Backend only
docker compose cp backend/app.py stream-recorder:/app/app.py
docker compose restart stream-recorder

# Frontend only (no restart needed, just hard-refresh browser)
docker compose cp frontend/index.html stream-recorder:/app/frontend/index.html
```

## Key differences vs Node.js version

| | Node.js | Flask |
|---|---|---|
| Scheduler | node-cron | APScheduler |
| One-shot jobs | setTimeout | DateTrigger (native) |
| Day-of-week convention | JS (0=Sun) | converted to APScheduler (0=Mon) |
| Persistence | custom JSON file | custom JSON file |
| Image base | node:20-alpine | python:3.12-slim |
