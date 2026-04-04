const express = require('express');
const cron = require('node-cron');
const { v4: uuidv4 } = require('uuid');
const cors = require('cors');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static('/app/frontend'));

// In-memory store (persisted to JSON file)
const DATA_FILE = '/data/schedules.json';
let schedules = [];
let activeRecordings = {}; // scheduleId -> { process, pid, ... }
let cronJobs = {};         // scheduleId -> cron job instance

function loadSchedules() {
  try {
    if (fs.existsSync(DATA_FILE)) {
      const data = fs.readFileSync(DATA_FILE, 'utf8');
      schedules = JSON.parse(data);
      console.log(`Loaded ${schedules.length} schedules`);
    }
  } catch (e) {
    console.error('Failed to load schedules:', e.message);
    schedules = [];
  }
}

function saveSchedules() {
  try {
    fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
    fs.writeFileSync(DATA_FILE, JSON.stringify(schedules, null, 2));
  } catch (e) {
    console.error('Failed to save schedules:', e.message);
  }
}

function getOutputPath(schedule, date) {
  const d = date || new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const filename = `${schedule.programName}-${yyyy}-${mm}-${dd}.mp3`;
  return path.join(schedule.filePath, filename);
}

function startRecording(schedule) {
  const outputPath = getOutputPath(schedule);
  
  // Ensure output directory exists
  try {
    fs.mkdirSync(schedule.filePath, { recursive: true });
  } catch (e) {
    console.error(`Failed to create directory ${schedule.filePath}:`, e.message);
  }

  console.log(`[${schedule.programName}] Starting recording -> ${outputPath}`);

  const ffmpegArgs = [
    '-loglevel', 'warning',
    '-i', schedule.streamUrl,
    '-t', String(schedule.duration * 60), // duration in minutes -> seconds
    '-acodec', 'libmp3lame',
    '-ab', `${schedule.bitrate}k`,
    '-y', // overwrite
    outputPath
  ];

  // Skip SSL verification for self-signed certs (common for local streams)
  if (schedule.streamUrl.startsWith('https://')) {
    ffmpegArgs.unshift('-tls_verify', '0');
  }

  const proc = spawn('ffmpeg', ffmpegArgs);

  const recordingInfo = {
    scheduleId: schedule.id,
    programName: schedule.programName,
    outputPath,
    startedAt: new Date().toISOString(),
    pid: proc.pid
  };

  activeRecordings[schedule.id] = {
    ...recordingInfo,
    process: proc
  };

  // Update schedule status
  const s = schedules.find(x => x.id === schedule.id);
  if (s) {
    s.lastRun = new Date().toISOString();
    s.lastOutput = outputPath;
    s.status = 'recording';
    saveSchedules();
  }

  proc.stderr.on('data', (data) => {
    console.log(`[${schedule.programName}] ffmpeg: ${data.toString().trim()}`);
  });

  proc.on('close', (code) => {
    console.log(`[${schedule.programName}] Recording finished (exit ${code}) -> ${outputPath}`);
    delete activeRecordings[schedule.id];
    const s = schedules.find(x => x.id === schedule.id);
    if (s) {
      s.status = code === 0 ? 'idle' : 'error';
      s.lastExitCode = code;
      saveSchedules();
    }
  });

  proc.on('error', (err) => {
    console.error(`[${schedule.programName}] ffmpeg error:`, err.message);
    delete activeRecordings[schedule.id];
    const s = schedules.find(x => x.id === schedule.id);
    if (s) { s.status = 'error'; saveSchedules(); }
  });

  return recordingInfo;
}

function stopRecording(scheduleId) {
  const rec = activeRecordings[scheduleId];
  if (rec && rec.process) {
    rec.process.kill('SIGTERM');
    delete activeRecordings[scheduleId];
    const s = schedules.find(x => x.id === scheduleId);
    if (s) { s.status = 'idle'; saveSchedules(); }
    return true;
  }
  return false;
}

function parseCronExpression(schedule) {
  const [hour, minute] = schedule.startTime.split(':');
  if (schedule.recurrence === 'daily') {
    return `${minute} ${hour} * * *`;
  } else if (schedule.recurrence === 'weekly') {
    const days = (schedule.weekDays || []).join(',') || '*';
    return `${minute} ${hour} * * ${days}`;
  } else {
    return `${minute} ${hour} * * *`;
  }
}

function msUntil(timeStr) {
  // Returns ms until next occurrence of HH:MM today, or tomorrow if already passed
  const now = new Date();
  const [hour, minute] = timeStr.split(':').map(Number);
  const target = new Date(now);
  target.setHours(hour, minute, 0, 0);
  if (target <= now) target.setDate(target.getDate() + 1);
  return target - now;
}

function registerCronJob(schedule) {
  if (!schedule.enabled) return;

  // 'once' uses a one-shot setTimeout, not a repeating cron
  if (schedule.recurrence === 'once') {
    const delay = msUntil(schedule.startTime);
    const when = new Date(Date.now() + delay);
    console.log(`Scheduling one-shot [${schedule.programName}] at ${when.toLocaleString()} (in ${Math.round(delay/1000)}s)`);
    const timer = setTimeout(() => {
      if (!activeRecordings[schedule.id] || !activeRecordings[schedule.id].process) {
        startRecording(schedule);
      }
      // Disable after firing so it won't re-arm on container restart
      const s = schedules.find(x => x.id === schedule.id);
      if (s) { s.enabled = false; saveSchedules(); }
      delete cronJobs[schedule.id];
    }, delay);
    cronJobs[schedule.id] = { stop: () => clearTimeout(timer) };
    return;
  }

  try {
    const expression = parseCronExpression(schedule);
    const tz = process.env.TZ || 'UTC';
    console.log(`Registering cron [${schedule.programName}]: ${expression} (${tz})`);

    const job = cron.schedule(expression, () => {
      // Check if already recording
      if (activeRecordings[schedule.id] && activeRecordings[schedule.id].process) {
        console.log(`[${schedule.programName}] Already recording, skipping`);
        return;
      }
      startRecording(schedule);
    }, { timezone: tz });

    cronJobs[schedule.id] = job;

  } catch (e) {
    console.error(`Failed to register cron for ${schedule.programName}:`, e.message);
  }
}

function unregisterCronJob(scheduleId) {
  if (cronJobs[scheduleId]) {
    cronJobs[scheduleId].stop();
    delete cronJobs[scheduleId];
  }
  const rec = activeRecordings[scheduleId];
  if (rec && rec.process) {
    rec.process.kill('SIGTERM');
  }
  delete activeRecordings[scheduleId];
}

function initAllCronJobs() {
  schedules.forEach(s => {
    if (s.enabled) registerCronJob(s);
  });
}

// ─── API Routes ────────────────────────────────────────────────────

// GET all schedules
app.get('/api/schedules', (req, res) => {
  const withStatus = schedules.map(s => ({
    ...s,
    isRecording: !!(activeRecordings[s.id] && activeRecordings[s.id].process)
  }));
  res.json(withStatus);
});

// GET single schedule
app.get('/api/schedules/:id', (req, res) => {
  const s = schedules.find(x => x.id === req.params.id);
  if (!s) return res.status(404).json({ error: 'Not found' });
  res.json({ ...s, isRecording: !!(activeRecordings[s.id] && activeRecordings[s.id].process) });
});

// POST create schedule
app.post('/api/schedules', (req, res) => {
  const { programName, streamUrl, filePath, startTime, duration, bitrate, recurrence, weekDays, enabled } = req.body;
  
  if (!programName || !streamUrl || !filePath || !startTime || !duration) {
    return res.status(400).json({ error: 'Missing required fields' });
  }
  
  const schedule = {
    id: uuidv4(),
    programName: programName.replace(/[^a-zA-Z0-9_\-]/g, '_'),
    streamUrl,
    filePath,
    startTime,
    duration: parseInt(duration),
    bitrate: parseInt(bitrate) || 96,
    recurrence: recurrence || 'daily',
    weekDays: weekDays || [],
    enabled: enabled !== false,
    createdAt: new Date().toISOString(),
    lastRun: null,
    lastOutput: null,
    status: 'idle'
  };
  
  schedules.push(schedule);
  saveSchedules();
  
  if (schedule.enabled) registerCronJob(schedule);
  
  res.status(201).json(schedule);
});

// PUT update schedule
app.put('/api/schedules/:id', (req, res) => {
  const idx = schedules.findIndex(x => x.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Not found' });
  
  // Unregister old cron
  unregisterCronJob(req.params.id);
  
  const updated = {
    ...schedules[idx],
    ...req.body,
    id: req.params.id, // preserve ID
    programName: (req.body.programName || schedules[idx].programName).replace(/[^a-zA-Z0-9_\-]/g, '_'),
    duration: parseInt(req.body.duration || schedules[idx].duration),
    bitrate: parseInt(req.body.bitrate || schedules[idx].bitrate),
  };
  
  schedules[idx] = updated;
  saveSchedules();
  
  if (updated.enabled) registerCronJob(updated);
  
  res.json(updated);
});

// DELETE schedule
app.delete('/api/schedules/:id', (req, res) => {
  const idx = schedules.findIndex(x => x.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Not found' });
  
  unregisterCronJob(req.params.id);
  schedules.splice(idx, 1);
  saveSchedules();
  
  res.json({ success: true });
});

// POST manually trigger recording
app.post('/api/schedules/:id/record', (req, res) => {
  const s = schedules.find(x => x.id === req.params.id);
  if (!s) return res.status(404).json({ error: 'Not found' });
  
  if (activeRecordings[s.id] && activeRecordings[s.id].process) {
    return res.status(409).json({ error: 'Already recording' });
  }
  
  const info = startRecording(s);
  res.json({ success: true, ...info });
});

// POST stop recording
app.post('/api/schedules/:id/stop', (req, res) => {
  const stopped = stopRecording(req.params.id);
  res.json({ success: stopped });
});

// GET active recordings
app.get('/api/recordings/active', (req, res) => {
  const active = Object.entries(activeRecordings)
    .filter(([, v]) => v.process)
    .map(([id, v]) => ({
      scheduleId: id,
      programName: v.programName,
      outputPath: v.outputPath,
      startedAt: v.startedAt,
      pid: v.pid
    }));
  res.json(active);
});

// GET current server time
app.get('/api/time', (req, res) => {
  const now = new Date();
  res.json({
    iso: now.toISOString(),
    local: now.toLocaleString('en-GB', { timeZone: process.env.TZ || 'UTC', hour12: false }),
    timezone: process.env.TZ || 'UTC'
  });
});

// GET health
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', schedules: schedules.length, activeRecordings: Object.keys(activeRecordings).length });
});

// Serve frontend for all other routes
app.get('*', (req, res) => {
  res.sendFile('/app/frontend/index.html');
});

// ─── Start ─────────────────────────────────────────────────────────
const PORT = process.env.PORT || 3000;

loadSchedules();
initAllCronJobs();

app.listen(PORT, () => {
  console.log(`Stream Recorder running on port ${PORT}`);
});
