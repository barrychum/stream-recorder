import os
import json
import uuid
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

# ── Config ────────────────────────────────────────────────────────
DATA_FILE = Path('/data/schedules.json')
TZ = os.environ.get('TZ', 'UTC')
try:
    TIMEZONE = ZoneInfo(TZ)
except Exception:
    TIMEZONE = ZoneInfo('UTC')

# ── State ─────────────────────────────────────────────────────────
schedules: dict = {}        # id -> schedule dict
active_recordings: dict = {}  # id -> {'process': Popen, 'started_at': str, 'output_path': str}
state_lock = threading.Lock()

scheduler = BackgroundScheduler(timezone=TIMEZONE)

# ── Persistence ───────────────────────────────────────────────────
def load_schedules():
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            # Support both list (old) and dict (new) format
            if isinstance(data, list):
                return {s['id']: s for s in data}
            return data
        except Exception as e:
            print(f'Failed to load schedules: {e}')
    return {}

def save_schedules():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(list(schedules.values()), indent=2))

# ── Recording ─────────────────────────────────────────────────────
def get_output_path(schedule: dict, dt: datetime = None) -> str:
    dt = dt or datetime.now(TIMEZONE)
    filename = f"{schedule['programName']}-{dt.strftime('%Y-%m-%d')}.mp3"
    return str(Path(schedule['filePath']) / filename)

def start_recording(schedule_id: str):
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

    cmd = ['ffmpeg', '-loglevel', 'warning']

    # Skip TLS verification for self-signed certs on local streams
    if s['streamUrl'].startswith('https://'):
        cmd += ['-tls_verify', '0']

    cmd += [
        '-i', s['streamUrl'],
        '-t', str(int(s['duration']) * 60),
        '-acodec', 'libmp3lame',
        '-ab', f"{s['bitrate']}k",
        '-y',
        output_path,
    ]

    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    except FileNotFoundError:
        print('ERROR: ffmpeg not found in PATH')
        return

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
        try:
            _, stderr = proc.communicate()
            if stderr:
                for line in stderr.decode(errors='replace').splitlines():
                    if line.strip():
                        print(f"[{s['programName']}] ffmpeg: {line}")
        except Exception as e:
            print(f"[{s['programName']}] wait error: {e}")
        finally:
            exit_code = proc.returncode if proc.returncode is not None else -1
            print(f"[{s['programName']}] Recording finished (exit {exit_code}) -> cleaning up")
            with state_lock:
                active_recordings.pop(schedule_id, None)
                if schedule_id in schedules:
                    schedules[schedule_id]['status'] = 'idle' if exit_code == 0 else 'error'
                    schedules[schedule_id]['lastExitCode'] = exit_code
                    if schedules[schedule_id].get('recurrence') == 'once':
                        schedules[schedule_id]['enabled'] = False
                    save_schedules()
            print(f"[{s['programName']}] Cleanup done, active_recordings now: {list(active_recordings.keys())}")

    threading.Thread(target=_wait, daemon=True).start()

def stop_recording(schedule_id: str) -> bool:
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

# ── Scheduling ────────────────────────────────────────────────────
def make_job_id(schedule_id: str) -> str:
    return f'rec_{schedule_id}'

def register_job(s: dict):
    if not s.get('enabled'):
        return

    job_id = make_job_id(s['id'])
    hour, minute = s['startTime'].split(':')
    recurrence = s.get('recurrence', 'daily')

    if recurrence == 'once':
        # One-shot: use DateTrigger for next occurrence of that time
        now = datetime.now(TIMEZONE)
        target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        trigger = DateTrigger(run_date=target, timezone=TIMEZONE)
        print(f"Scheduling one-shot [{s['programName']}] at {target}")

        def _once_job(sid=s['id']):
            start_recording(sid)
            # Note: disabling happens inside _wait() after ffmpeg finishes
            # so the card correctly shows isRecording=True while recording,
            # then disappears from active once done.

        scheduler.add_job(_once_job, trigger=trigger, id=job_id, replace_existing=True)

    else:
        # daily or weekly (specific days)
        if recurrence == 'daily':
            day_of_week = None
        else:
            # weekDays is list of ints 0=Sun..6=Sat (JS convention)
            # APScheduler uses 0=Mon..6=Sun, so convert
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
    job_id = make_job_id(schedule_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

def init_all_jobs():
    for s in schedules.values():
        if s.get('enabled'):
            register_job(s)

import logging

# ── Suppress noisy polling endpoint logs ──────────────────────────
_SUPPRESS_PATHS = ('/api/time', '/api/recordings/active', '/api/schedules', '/api/health')

class _SuppressPollingLogs(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in _SUPPRESS_PATHS)

logging.getLogger('werkzeug').addFilter(_SuppressPollingLogs())

# ── API ───────────────────────────────────────────────────────────
def schedule_with_status(s: dict) -> dict:
    is_recording = s['id'] in active_recordings
    # Keep status field in sync with actual recording state
    status = 'recording' if is_recording else s.get('status', 'idle')
    return {**s, 'isRecording': is_recording, 'status': status}

@app.get('/api/schedules')
def list_schedules():
    return jsonify([schedule_with_status(s) for s in schedules.values()])

@app.get('/api/schedules/<sid>')
def get_schedule(sid):
    s = schedules.get(sid)
    if not s:
        abort(404)
    return jsonify(schedule_with_status(s))

@app.post('/api/schedules')
def create_schedule():
    body = request.json or {}
    required = ['programName', 'streamUrl', 'filePath', 'startTime', 'duration']
    if not all(body.get(k) for k in required):
        return jsonify({'error': 'Missing required fields'}), 400

    s = {
        'id': str(uuid.uuid4()),
        'programName': ''.join(c if c.isalnum() or c in '-_' else '_' for c in body['programName']),
        'streamUrl': body['streamUrl'],
        'filePath': body['filePath'],
        'startTime': body['startTime'],
        'duration': int(body['duration']),
        'bitrate': int(body.get('bitrate', 96)),
        'recurrence': body.get('recurrence', 'daily'),
        'weekDays': body.get('weekDays', []),
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
    if sid not in schedules:
        abort(404)
    body = request.json or {}

    unregister_job(sid)

    with state_lock:
        existing = schedules[sid]
        name_raw = body.get('programName', existing['programName'])
        updated = {
            **existing,
            **body,
            'id': sid,
            'programName': ''.join(c if c.isalnum() or c in '-_' else '_' for c in name_raw),
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
    if sid not in schedules:
        abort(404)
    unregister_job(sid)
    stop_recording(sid)
    with state_lock:
        schedules.pop(sid)
        save_schedules()
    return jsonify({'success': True})

@app.post('/api/schedules/<sid>/record')
def record_now(sid):
    s = schedules.get(sid)
    if not s:
        abort(404)
    if sid in active_recordings:
        return jsonify({'error': 'Already recording'}), 409
    threading.Thread(target=start_recording, args=(sid,), daemon=True).start()
    return jsonify({'success': True, 'scheduleId': sid, 'programName': s['programName']})

@app.post('/api/schedules/<sid>/stop')
def stop_now(sid):
    stopped = stop_recording(sid)
    return jsonify({'success': stopped})

@app.get('/api/recordings/active')
def list_active():
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
    now = datetime.now(TIMEZONE)
    return jsonify({
        'iso': now.isoformat(),
        'local': now.strftime('%d/%m/%Y, %H:%M:%S'),
        'timezone': TZ,
    })

@app.get('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'schedules': len(schedules),
        'activeRecordings': len(active_recordings),
    })

# Serve frontend
@app.get('/')
@app.get('/<path:path>')
def frontend(path=''):
    return send_from_directory('/app/frontend', 'index.html')

# ── Main ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    from waitress import serve
    schedules.update(load_schedules())
    scheduler.start()
    init_all_jobs()
    port = int(os.environ.get('PORT', 3000))
    print(f'Stream Recorder (Flask/Waitress) running on port {port}  tz={TZ}')
    serve(app, host='0.0.0.0', port=port, threads=8)
