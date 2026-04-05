# =============================================================================
# Stream Recorder - Backend
# Version: 1.1
# Stack:   Flask + APScheduler + ffmpeg + Waitress
#
# Key features:
#   - REST API for managing recording schedules (CRUD)
#   - APScheduler for cron (daily/weekly) and one-shot (once) triggers
#   - ffmpeg subprocess for actual stream recording
#   - Schedules persisted to /data/schedules.json (UTF-8, Unicode-safe)
#   - Timezone-aware via TZ environment variable
#   - Waitress WSGI server (no dev-server warnings)
#   - Noisy polling endpoints suppressed from werkzeug logs
# =============================================================================

import os
import json
import uuid
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory, abort
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

app = Flask(__name__, static_folder='/app/frontend', static_url_path='')

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE = Path('/data/schedules.json')  # Persistent schedule store (volume mount)
TZ = os.environ.get('TZ', 'UTC')          # Timezone from Docker environment
try:
    TIMEZONE = ZoneInfo(TZ)
except Exception:
    TIMEZONE = ZoneInfo('UTC')            # Fallback to UTC if TZ is invalid

# ── State ─────────────────────────────────────────────────────────────────────
# schedules: in-memory dict of all schedule configs, keyed by UUID
# active_recordings: in-memory dict of currently running ffmpeg processes
# state_lock: protects both dicts from concurrent access across threads
schedules: dict = {}
active_recordings: dict = {}
state_lock = threading.Lock()

# APScheduler runs in a background thread; timezone is set globally here
scheduler = BackgroundScheduler(timezone=TIMEZONE)

# ── Suppress noisy polling endpoint logs ──────────────────────────────────────
# These endpoints are polled every few seconds by the frontend; logging them
# would flood the console. All other requests still log normally.
_SUPPRESS_PATHS = ('/api/time', '/api/recordings/active', '/api/schedules', '/api/health')

class _SuppressPollingLogs(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in _SUPPRESS_PATHS)

logging.getLogger('werkzeug').addFilter(_SuppressPollingLogs())

# ── Persistence ───────────────────────────────────────────────────────────────
def load_schedules():
    """Load schedules from JSON file on startup.
    Supports both list format (legacy) and dict format (current).
    Returns a dict keyed by schedule id."""
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                # Migrate from old list format to dict format
                return {s['id']: s for s in data}
            return data
        except Exception as e:
            print(f'Failed to load schedules: {e}')
    return {}

def save_schedules():
    """Persist current schedules to JSON file.
    Uses ensure_ascii=False to preserve Unicode characters (e.g. Chinese program names)."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(list(schedules.values()), indent=2, ensure_ascii=False),
        encoding='utf-8'
    )

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_output_path(schedule: dict, dt: datetime = None) -> str:
    """Build the final output file path for a recording.
    Format: <filePath>/<programName>-YYYY-MM-DD.mp3"""
    dt = dt or datetime.now(TIMEZONE)
    filename = f"{schedule['programName']}-{dt.strftime('%Y-%m-%d')}.mp3"
    return str(Path(schedule['filePath']) / filename)

def sanitize_name(name: str) -> str:
    """Sanitize a program name for use in file names.
    Allows Unicode letters and digits (e.g. Chinese characters), hyphens,
    and underscores. Replaces all other characters with underscores."""
    import unicodedata
    result = []
    for c in name:
        cat = unicodedata.category(c)
        # L* = letters (any script), N* = numbers
        if cat.startswith('L') or cat.startswith('N') or c in '-_':
            result.append(c)
        else:
            result.append('_')
    return ''.join(result).strip('_') or 'unnamed'

# ── Recording ─────────────────────────────────────────────────────────────────
def start_recording(schedule_id: str):
    """Launch an ffmpeg process to record a stream.
    - Runs ffmpeg as a subprocess with the configured URL, duration, and bitrate
    - Tracks the process in active_recordings
    - Spawns a _wait() thread to clean up when ffmpeg exits
    - Writes directly to the final output file (no temp file)"""

    # Check schedule exists and is not already recording (thread-safe)
    with state_lock:
        s = schedules.get(schedule_id)
        if not s:
            print(f'Schedule {schedule_id} not found, skipping')
            return
        if schedule_id in active_recordings:
            print(f"[{s['programName']}] Already recording, skipping")
            return

    output_path = get_output_path(s)
    Path(s['filePath']).mkdir(parents=True, exist_ok=True)

    print(f"[{s['programName']}] Starting recording -> {output_path}")

    # Build ffmpeg command
    cmd = ['ffmpeg', '-loglevel', 'error']

    # Disable TLS cert verification for self-signed certs on local streams
    if s['streamUrl'].startswith('https://'):
        cmd += ['-tls_verify', '0']

    cmd += [
        '-i', s['streamUrl'],                    # Input stream URL
        '-t', str(int(s['duration']) * 60),      # Duration in seconds
        '-acodec', 'libmp3lame',                 # MP3 encoder
        '-ab', f"{s['bitrate']}k",               # Audio bitrate
        '-y',                                    # Overwrite output if exists
        output_path,
    ]

    print(f"[{s['programName']}] CMD: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    except FileNotFoundError:
        print('ERROR: ffmpeg not found in PATH')
        return

    # Register the active recording (thread-safe)
    with state_lock:
        active_recordings[schedule_id] = {
            'process': proc,
            'started_at': datetime.now(TIMEZONE).isoformat(),
            'output_path': output_path,
            'program_name': s['programName'],
            'pid': proc.pid,
        }
        schedules[schedule_id]['lastRun'] = datetime.now(TIMEZONE).isoformat()
        schedules[schedule_id]['lastOutput'] = output_path
        schedules[schedule_id]['status'] = 'recording'
        save_schedules()

    def _wait():
        """Background thread: waits for ffmpeg to finish, then cleans up state."""
        try:
            _, stderr = proc.communicate()
            stderr_text = stderr.decode(errors='replace').strip() if stderr else ''
            if stderr_text:
                for line in stderr_text.splitlines():
                    if line.strip():
                        print(f"[{s['programName']}] ffmpeg: {line}")
            else:
                print(f"[{s['programName']}] ffmpeg: (no output)")
        except Exception as e:
            print(f"[{s['programName']}] wait error: {e}")
        finally:
            exit_code = proc.returncode if proc.returncode is not None else -1
            print(f"[{s['programName']}] Recording finished (exit {exit_code})")
            with state_lock:
                active_recordings.pop(schedule_id, None)
                if schedule_id in schedules:
                    schedules[schedule_id]['status'] = 'idle' if exit_code == 0 else 'error'
                    schedules[schedule_id]['lastExitCode'] = exit_code
                    # Disable 'once' schedules after they complete so they don't re-arm on restart
                    if schedules[schedule_id].get('recurrence') == 'once':
                        schedules[schedule_id]['enabled'] = False
                    save_schedules()
            print(f"[{s['programName']}] Cleanup done, active_recordings now: {list(active_recordings.keys())}")

    threading.Thread(target=_wait, daemon=True).start()

def stop_recording(schedule_id: str) -> bool:
    """Terminate an active ffmpeg recording process.
    Returns True if a recording was stopped, False if nothing was running."""
    with state_lock:
        rec = active_recordings.get(schedule_id)
        if not rec:
            return False
        rec['process'].terminate()
        active_recordings.pop(schedule_id, None)
        if schedule_id in schedules:
            schedules[schedule_id]['status'] = 'idle'
            save_schedules()
    return True

# ── Scheduling ────────────────────────────────────────────────────────────────
def make_job_id(schedule_id: str) -> str:
    """Generate a unique APScheduler job ID from a schedule UUID."""
    return f'rec_{schedule_id}'

def register_job(s: dict):
    """Register an APScheduler job for a schedule.

    Recurrence modes:
      - 'once':   DateTrigger fires at the next occurrence of startTime today
                  (or tomorrow if that time has already passed). Disables itself
                  after firing via _wait() in start_recording().
      - 'daily':  CronTrigger fires every day at startTime.
      - 'weekly': CronTrigger fires on selected weekDays at startTime.
                  weekDays uses JS convention (0=Sun..6=Sat) and is converted
                  to APScheduler convention (0=Mon..6=Sun)."""
    if not s.get('enabled'):
        return

    job_id = make_job_id(s['id'])
    hour, minute = s['startTime'].split(':')
    recurrence = s.get('recurrence', 'daily')

    if recurrence == 'once':
        now = datetime.now(TIMEZONE)
        target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        trigger = DateTrigger(run_date=target, timezone=TIMEZONE)
        print(f"Scheduling one-shot [{s['programName']}] at {target}")

        def _once_job(sid=s['id']):
            start_recording(sid)
            # Disabling happens inside _wait() after ffmpeg finishes, not here,
            # so the card correctly shows isRecording=True while recording.

        scheduler.add_job(_once_job, trigger=trigger, id=job_id, replace_existing=True)

    else:
        # Convert JS weekDays (0=Sun) to APScheduler day_of_week (0=Mon)
        if recurrence == 'daily':
            day_of_week = None   # No day filter = every day
        else:
            js_to_ap = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
            ap_days = [js_to_ap[d] for d in s.get('weekDays', [])]
            day_of_week = ','.join(str(d) for d in ap_days) if ap_days else None

        trigger = CronTrigger(
            hour=int(hour),
            minute=int(minute),
            day_of_week=day_of_week,
            timezone=TIMEZONE,
        )
        sid = s['id']
        scheduler.add_job(
            lambda sid=sid: start_recording(sid),
            trigger=trigger,
            id=job_id,
            replace_existing=True,
        )
        print(f"Registered cron [{s['programName']}]: {hour}:{minute} days={day_of_week or 'every'} ({TZ})")

def unregister_job(schedule_id: str):
    """Remove a schedule's APScheduler job if it exists."""
    job_id = make_job_id(schedule_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

def init_all_jobs():
    """Called on startup: register APScheduler jobs for all enabled schedules."""
    for s in schedules.values():
        if s.get('enabled'):
            register_job(s)

# ── API Helpers ───────────────────────────────────────────────────────────────
def get_next_run(s: dict):
    """Ask APScheduler for the next scheduled run time for a given schedule.
    Returns a datetime or None if the schedule is disabled or has no pending job."""
    if not s.get('enabled'):
        return None
    job = scheduler.get_job(make_job_id(s['id']))
    if job and job.next_run_time:
        return job.next_run_time
    return None

def schedule_with_status(s: dict) -> dict:
    """Enrich a schedule dict with live runtime fields before sending to the client:
      - isRecording: whether ffmpeg is currently running for this schedule
      - status:      authoritative status derived from active_recordings (not stored value)
      - nextRun:     ISO timestamp of next scheduled run from APScheduler"""
    is_recording = s['id'] in active_recordings
    # Derive status live so it can never be stale vs. the actual process state
    status = 'recording' if is_recording else s.get('status', 'idle')
    next_run = get_next_run(s)
    return {
        **s,
        'isRecording': is_recording,
        'status': status,
        'nextRun': next_run.isoformat() if next_run else None,
    }

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get('/api/schedules')
def list_schedules():
    """Return all schedules sorted by urgency:
    1. Currently recording  (top)
    2. Soonest next run     (ascending)
    3. Disabled / no next run (bottom, alphabetical)"""
    result = [schedule_with_status(s) for s in schedules.values()]

    def sort_key(s):
        if s['isRecording']:
            return (0, '')
        if s['nextRun']:
            return (1, s['nextRun'])
        return (2, s.get('programName', ''))

    result.sort(key=sort_key)
    return jsonify(result)

@app.get('/api/schedules/<sid>')
def get_schedule(sid):
    """Return a single schedule by ID."""
    s = schedules.get(sid)
    if not s:
        abort(404)
    return jsonify(schedule_with_status(s))

@app.post('/api/schedules')
def create_schedule():
    """Create a new schedule and register its APScheduler job.
    Required fields: programName, streamUrl, filePath, startTime, duration."""
    body = request.json or {}
    required = ['programName', 'streamUrl', 'filePath', 'startTime', 'duration']
    if not all(body.get(k) for k in required):
        return jsonify({'error': 'Missing required fields'}), 400

    s = {
        'id': str(uuid.uuid4()),
        'programName': sanitize_name(body['programName']),    # Unicode-safe filename
        'streamUrl': body['streamUrl'].strip(),                # Strip accidental whitespace
        'filePath': body['filePath'].strip(),
        'startTime': body['startTime'],
        'duration': int(body['duration']),
        'bitrate': int(body.get('bitrate', 96)),              # Default 96kbps
        'recurrence': body.get('recurrence', 'daily'),
        'weekDays': body.get('weekDays', []),                  # JS convention: 0=Sun..6=Sat
        'enabled': body.get('enabled', True),
        'createdAt': datetime.now(TIMEZONE).isoformat(),
        'lastRun': None,
        'lastOutput': None,
        'status': 'idle',
    }

    with state_lock:
        schedules[s['id']] = s
        save_schedules()

    if s['enabled']:
        register_job(s)

    return jsonify(s), 201

@app.put('/api/schedules/<sid>')
def update_schedule(sid):
    """Update an existing schedule.
    Unregisters the old job, applies changes, re-registers if enabled."""
    if sid not in schedules:
        abort(404)
    body = request.json or {}

    # Remove old job before re-registering with new settings
    unregister_job(sid)

    with state_lock:
        existing = schedules[sid]
        name_raw = body.get('programName', existing['programName'])
        updated = {
            **existing,
            **body,
            'id': sid,                                                          # Preserve ID
            'programName': sanitize_name(name_raw),
            'streamUrl': body.get('streamUrl', existing['streamUrl']).strip(),
            'filePath': body.get('filePath', existing['filePath']).strip(),
            'duration': int(body.get('duration', existing['duration'])),
            'bitrate': int(body.get('bitrate', existing['bitrate'])),
            'weekDays': body.get('weekDays', existing.get('weekDays', [])),
        }
        schedules[sid] = updated
        save_schedules()

    if updated['enabled']:
        register_job(updated)

    return jsonify(updated)

@app.delete('/api/schedules/<sid>')
def delete_schedule(sid):
    """Delete a schedule, stopping any active recording and removing its job."""
    if sid not in schedules:
        abort(404)
    unregister_job(sid)
    stop_recording(sid)   # No-op if not currently recording
    with state_lock:
        schedules.pop(sid)
        save_schedules()
    return jsonify({'success': True})

@app.post('/api/schedules/<sid>/record')
def record_now(sid):
    """Manually trigger an immediate recording for a schedule (ignores schedule time)."""
    s = schedules.get(sid)
    if not s:
        abort(404)
    if sid in active_recordings:
        return jsonify({'error': 'Already recording'}), 409
    # Run in a thread so the HTTP response returns immediately
    threading.Thread(target=start_recording, args=(sid,), daemon=True).start()
    return jsonify({'success': True, 'scheduleId': sid, 'programName': s['programName']})

@app.post('/api/schedules/<sid>/stop')
def stop_now(sid):
    """Manually stop an active recording."""
    stopped = stop_recording(sid)
    return jsonify({'success': stopped})

@app.get('/api/recordings/active')
def list_active():
    """Return a list of currently active (recording) sessions.
    Polled by the frontend every 5 seconds to update card states."""
    result = []
    for sid, rec in active_recordings.items():
        result.append({
            'scheduleId': sid,
            'programName': rec['program_name'],
            'outputPath': rec['output_path'],
            'startedAt': rec['started_at'],
            'pid': rec['pid'],
        })
    return jsonify(result)

@app.get('/api/time')
def server_time():
    """Return the container's current time in the configured timezone.
    Used by the frontend clock display to show server time (not browser time)."""
    now = datetime.now(TIMEZONE)
    return jsonify({
        'iso': now.isoformat(),
        'local': now.strftime('%d/%m/%Y, %H:%M:%S'),
        'timezone': TZ,
    })

@app.get('/api/health')
def health():
    """Basic health check endpoint."""
    return jsonify({
        'status': 'ok',
        'schedules': len(schedules),
        'activeRecordings': len(active_recordings),
    })

# Serve the single-page frontend for all non-API routes
@app.get('/')
@app.get('/<path:path>')
def frontend(path=''):
    return send_from_directory('/app/frontend', 'index.html')

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    from waitress import serve

    schedules.update(load_schedules())
    scheduler.start()
    init_all_jobs()

    port = int(os.environ.get('PORT', 3000))
    print(f'Stream Recorder v1.1 (Flask/Waitress) running on port {port}  tz={TZ}')
    serve(app, host='0.0.0.0', port=port, threads=8)
