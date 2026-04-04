# Stream Recorder

A web-based audio stream scheduler and recorder, packaged as a Docker container.
Records internet radio / audio streams to MP3 files on a schedule using `ffmpeg`.

## Features

- Schedule recordings from any audio stream URL (HTTP/HTTPS)
- Define program name, output directory, start time, duration, bitrate
- Recurrence options: Daily, Weekdays, Weekends, Weekly, Once
- Enable/disable schedules without deleting them
- Manually trigger or stop a recording instantly
- Output filename format: `programname-yyyy-mm-dd.mp3`
- Schedules persist across container restarts

---

## Quick Start

### Using Docker Compose (recommended)

```bash
# Clone / copy files, then:
docker compose up -d

# View logs
docker compose logs -f
```

Open http://localhost:3000 in your browser.

---

### Build the image manually

```bash
docker build -t stream-recorder .
```

### Run the container

```bash
docker run -d \
  --name stream-recorder \
  -p 3000:3000 \
  -v $(pwd)/data:/data \
  -v $(pwd)/recordings:/recordings \
  -e TZ=Europe/London \
  stream-recorder
```

---

## Volumes

| Container path | Purpose |
|---|---|
| `/data` | Stores `schedules.json` (schedule persistence) |
| `/recordings` | Default location for recorded MP3 files |

You can mount any host path to `/recordings`, or use different per-schedule output directories.

---

## Schedule Fields

| Field | Description |
|---|---|
| Program Name | Used in the filename: `programname-yyyy-mm-dd.mp3`. Spaces/special chars replaced with `_` |
| Stream URL | Full URL to the audio stream, e.g. `https://192.168.38.10:8000/stream.mp3` |
| Output Directory | Directory inside the container where files are saved |
| Start Time | HH:MM in 24-hour format |
| Duration | Recording length in minutes |
| Audio Bitrate | Target MP3 bitrate: 64/96/128/192/256/320 kbps |
| Recurrence | Daily, Weekdays, Weekends, Weekly (pick day), or Once |

---

## Notes on Self-Signed Certificates

The streams at `192.168.38.x` likely use self-signed TLS certificates.
The backend automatically disables TLS verification for `https://` stream URLs,
so `ffmpeg` will connect without errors.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/schedules` | List all schedules |
| POST | `/api/schedules` | Create a schedule |
| PUT | `/api/schedules/:id` | Update a schedule |
| DELETE | `/api/schedules/:id` | Delete a schedule |
| POST | `/api/schedules/:id/record` | Start recording immediately |
| POST | `/api/schedules/:id/stop` | Stop active recording |
| GET | `/api/recordings/active` | List currently recording streams |
| GET | `/api/health` | Health check |
