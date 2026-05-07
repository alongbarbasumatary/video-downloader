# app.py — Video Downloader Web App
import os
import re
import time
import uuid
import socket
import logging
import threading
import platform

import psutil
import requests
import speedtest
from flask import Flask, request, jsonify, send_from_directory, render_template_string

# ── Configuration ─────────────────────────────────────────────────────────────
TEMP_DIR    = "./downloads"
SESSION_TTL = 3600   # auto-delete downloaded files after 1 hour
HOST        = "0.0.0.0"
PORT        = 8000

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
os.makedirs(TEMP_DIR, exist_ok=True)

API_HEADERS = {
    "accept":       "application/json",
    "content-type": "application/json",
    "origin":       "https://www.savethevideo.com",
    "referer":      "https://www.savethevideo.com/",
    "user-agent":   "Mozilla/5.0",
}

# ── Core Logic ────────────────────────────────────────────────────────────────

def schedule_delete(path: str, delay: int = SESSION_TTL) -> None:
    def _delete():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Auto-deleted: %s", path)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
    threading.Thread(target=_delete, daemon=True).start()


def send_request(url: str) -> dict:
    api_url = "https://api.v02.savethevideo.com/tasks"
    payload = {"type": "info", "url": url}
    res = requests.post(api_url, json=payload, headers=API_HEADERS, timeout=15)
    res.raise_for_status()
    data = res.json()
    if data.get("state") == "completed":
        return data
    if "href" not in data:
        raise ValueError("Unexpected API response — no task href.")
    return monitor_task(data["href"])


def monitor_task(task_href: str, retries: int = 20) -> dict:
    task_url = f"https://api.v02.savethevideo.com{task_href}"
    for attempt in range(retries):
        res = requests.get(task_url, headers=API_HEADERS, timeout=10)
        res.raise_for_status()
        data = res.json()
        state = data.get("state")
        if state == "completed":
            return data
        if state == "failed":
            raise RuntimeError("Video processing failed on the remote server.")
        time.sleep(min(2 ** attempt, 8))
    raise TimeoutError("Timed out waiting for the video to be processed.")


def parse_formats(data: dict) -> list:
    results = data.get("result", [])
    if not results:
        return []
    formats = results[0].get("formats", [])
    filtered = []
    for fmt in formats:
        url = fmt.get("url", "")
        if (
            fmt.get("ext") == "mp4"
            and fmt.get("vcodec") != "none"
            and fmt.get("acodec") != "none"
            and "m3u8" not in url
            and url
        ):
            size_mb = round(fmt.get("filesize", 0) / (1024 * 1024), 1)
            filtered.append({
                "resolution":  fmt.get("resolution", "Unknown"),
                "url":         url,
                "filesize_mb": size_mb,
            })
    filtered.sort(
        key=lambda x: int(m.group()) if (m := re.search(r"\d+", x["resolution"])) else 0,
        reverse=True,
    )
    return filtered


def download_video(video_url: str, resolution: str) -> str:
    safe_res  = resolution.replace("x", "_").replace(" ", "_")
    filename  = f"{safe_res}_{uuid.uuid4().hex[:8]}.mp4"
    filepath  = os.path.join(TEMP_DIR, filename)
    with requests.get(video_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return filepath, filename


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    body = request.get_json(silent=True) or {}
    url  = (body.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "Invalid URL."}), 400
    try:
        data    = send_request(url)
        formats = parse_formats(data)
        if not formats:
            return jsonify({"error": "No downloadable MP4 formats found."}), 404
        return jsonify({"formats": formats})
    except Exception as exc:
        logger.exception("api_fetch error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    body       = request.get_json(silent=True) or {}
    video_url  = (body.get("url") or "").strip()
    resolution = (body.get("resolution") or "video").strip()
    if not video_url.startswith("http"):
        return jsonify({"error": "Invalid video URL."}), 400
    try:
        filepath, filename = download_video(video_url, resolution)
        size_mb = round(os.path.getsize(filepath) / (1024 * 1024), 1)
        schedule_delete(filepath)
        return jsonify({
            "filename":   filename,
            "size_mb":    size_mb,
            "download":   f"/files/{filename}",
        })
    except Exception as exc:
        logger.exception("api_download error")
        return jsonify({"error": str(exc)}), 500


@app.route("/files/<filename>")
def serve_file(filename):
    # Safety: only allow files inside TEMP_DIR
    safe = os.path.abspath(os.path.join(TEMP_DIR, filename))
    if not safe.startswith(os.path.abspath(TEMP_DIR)):
        return "Forbidden", 403
    return send_from_directory(TEMP_DIR, filename, as_attachment=True)


@app.route("/api/sysinfo")
def api_sysinfo():
    try:
        cpu       = platform.processor() or platform.machine()
        mem       = psutil.virtual_memory()
        disk      = psutil.disk_usage("/")
        hostname  = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "N/A"
        try:
            ip_data   = requests.get("https://ipinfo.io/json", timeout=5).json()
            public_ip = ip_data.get("ip", "N/A")
            isp       = ip_data.get("org", "N/A")
            location  = f"{ip_data.get('city','')}, {ip_data.get('country','')}".strip(", ")
        except Exception:
            public_ip = isp = location = "N/A"
        return jsonify({
            "cpu":        cpu,
            "ram_total":  round(mem.total  / (1024**3), 2),
            "ram_used":   mem.percent,
            "disk_total": round(disk.total / (1024**3), 2),
            "disk_used":  disk.percent,
            "hostname":   hostname,
            "local_ip":   local_ip,
            "public_ip":  public_ip,
            "isp":        isp,
            "location":   location,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/speedtest")
def api_speedtest():
    try:
        st = speedtest.Speedtest(secure=True)
        st.get_best_server()
        return jsonify({
            "download": round(st.download() / 1_000_000, 2),
            "upload":   round(st.upload()   / 1_000_000, 2),
            "ping":     round(st.results.ping, 2),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/files")
def api_files():
    files = []
    for f in sorted(os.listdir(TEMP_DIR)):
        path = os.path.join(TEMP_DIR, f)
        files.append({
            "name":    f,
            "size_mb": round(os.path.getsize(path) / (1024 * 1024), 1),
            "url":     f"/files/{f}",
        })
    return jsonify({"files": files})


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    removed = 0
    for f in os.listdir(TEMP_DIR):
        try:
            os.remove(os.path.join(TEMP_DIR, f))
            removed += 1
        except OSError:
            pass
    return jsonify({"removed": removed})


# ── Web UI ────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VidFetch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:      #0a0a0f;
    --surface: #13131a;
    --card:    #1a1a24;
    --border:  #2a2a3a;
    --accent:  #7c5cfc;
    --accent2: #fc5c7c;
    --text:    #e8e8f0;
    --muted:   #6868a0;
    --green:   #5cfca0;
    --yellow:  #fcdc5c;
    --mono:    'JetBrains Mono', monospace;
    --sans:    'Syne', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Background grid */
  body::before {
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(var(--border) 1px, transparent 1px),
      linear-gradient(90deg, var(--border) 1px, transparent 1px);
    background-size: 48px 48px;
    opacity: 0.3;
    pointer-events: none;
    z-index: 0;
  }

  .glow {
    position: fixed;
    width: 600px; height: 600px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(124,92,252,.12) 0%, transparent 70%);
    top: -200px; left: -200px;
    pointer-events: none; z-index: 0;
    animation: drift 12s ease-in-out infinite alternate;
  }
  .glow2 {
    background: radial-gradient(circle, rgba(252,92,124,.08) 0%, transparent 70%);
    top: auto; bottom: -200px; left: auto; right: -200px;
    animation-delay: -6s;
  }
  @keyframes drift {
    from { transform: translate(0,0); }
    to   { transform: translate(80px, 60px); }
  }

  .wrap {
    position: relative; z-index: 1;
    max-width: 860px;
    margin: 0 auto;
    padding: 40px 20px 80px;
  }

  /* Header */
  header {
    display: flex; align-items: center; gap: 16px;
    margin-bottom: 48px;
  }
  .logo {
    width: 44px; height: 44px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px;
  }
  header h1 { font-size: 1.6rem; font-weight: 800; letter-spacing: -1px; }
  header h1 span { color: var(--accent); }
  .tabs {
    margin-left: auto;
    display: flex; gap: 4px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 4px;
  }
  .tab {
    padding: 7px 16px;
    border-radius: 7px;
    border: none;
    background: transparent;
    color: var(--muted);
    font-family: var(--sans);
    font-size: .85rem;
    font-weight: 600;
    cursor: pointer;
    transition: all .2s;
  }
  .tab.active { background: var(--accent); color: #fff; }
  .tab:hover:not(.active) { color: var(--text); }

  /* Panels */
  .panel { display: none; animation: fadeIn .3s ease; }
  .panel.active { display: block; }
  @keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }

  /* Card */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px;
    margin-bottom: 20px;
  }
  .card-title {
    font-size: .75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--muted);
    margin-bottom: 18px;
  }

  /* Input row */
  .input-row {
    display: flex; gap: 10px;
    flex-wrap: wrap;
  }
  .url-input {
    flex: 1; min-width: 220px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text);
    font-family: var(--mono);
    font-size: .9rem;
    padding: 12px 16px;
    outline: none;
    transition: border-color .2s;
  }
  .url-input:focus { border-color: var(--accent); }
  .url-input::placeholder { color: var(--muted); }

  .btn {
    padding: 12px 24px;
    border-radius: 10px;
    border: none;
    font-family: var(--sans);
    font-weight: 700;
    font-size: .9rem;
    cursor: pointer;
    transition: all .2s;
    white-space: nowrap;
  }
  .btn-primary {
    background: linear-gradient(135deg, var(--accent), #5c3ccc);
    color: #fff;
    box-shadow: 0 4px 20px rgba(124,92,252,.3);
  }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 24px rgba(124,92,252,.4); }
  .btn-primary:active { transform: none; }
  .btn-primary:disabled { opacity: .5; cursor: not-allowed; transform: none; }
  .btn-danger {
    background: rgba(252,92,124,.15);
    color: var(--accent2);
    border: 1px solid rgba(252,92,124,.3);
  }
  .btn-danger:hover { background: rgba(252,92,124,.25); }
  .btn-sm { padding: 8px 16px; font-size: .8rem; border-radius: 8px; }

  /* Status / spinner */
  .status {
    font-family: var(--mono);
    font-size: .85rem;
    color: var(--muted);
    margin-top: 14px;
    min-height: 22px;
    display: flex; align-items: center; gap: 8px;
  }
  .spinner {
    width: 14px; height: 14px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
    display: none;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner.active { display: block; }

  /* Format list — radio style */
  .formats { display: flex; flex-direction: column; gap: 0; margin-top: 8px;
             border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  .fmt-row {
    display: flex; align-items: center; gap: 14px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 18px;
    cursor: pointer;
    transition: background .15s;
    user-select: none;
  }
  .fmt-row:last-child { border-bottom: none; }
  .fmt-row:hover { background: rgba(124,92,252,.07); }
  .fmt-row.selected { background: rgba(124,92,252,.13); }

  /* Custom radio circle */
  .fmt-radio {
    width: 20px; height: 20px; min-width: 20px;
    border-radius: 50%;
    border: 2px solid var(--muted);
    display: flex; align-items: center; justify-content: center;
    transition: border-color .2s;
  }
  .fmt-row.selected .fmt-radio { border-color: var(--accent); }
  .fmt-radio-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--accent);
    display: none;
  }
  .fmt-row.selected .fmt-radio-dot { display: block; }

  /* Labels */
  .fmt-info { flex: 1; display: flex; align-items: center; gap: 10px; }
  .fmt-res {
    font-weight: 700;
    font-size: .95rem;
    color: var(--text);
  }
  .fmt-badge {
    font-size: .72rem; font-weight: 700;
    padding: 2px 8px; border-radius: 6px;
    background: rgba(124,92,252,.18);
    color: var(--accent);
    text-transform: uppercase; letter-spacing: .5px;
  }
  .fmt-size {
    margin-left: auto;
    font-family: var(--mono);
    font-size: .8rem;
    color: var(--muted);
  }

  /* Single download button below list */
  .dl-btn {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%;
    margin-top: 14px;
    padding: 13px 24px;
    border-radius: 10px;
    border: none;
    background: linear-gradient(135deg, var(--accent), #5c3ccc);
    color: #fff;
    font-family: var(--sans);
    font-weight: 700;
    font-size: .95rem;
    cursor: pointer;
    transition: all .2s;
    box-shadow: 0 4px 20px rgba(124,92,252,.3);
  }
  .dl-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 24px rgba(124,92,252,.4); }
  .dl-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }

  /* Progress bar */
  .progress-wrap { margin-top: 14px; display: none; }
  .progress-wrap.active { display: block; }
  .progress-bar-bg {
    background: var(--surface);
    border-radius: 99px;
    height: 6px;
    overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    border-radius: 99px;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    width: 0%;
    transition: width .4s ease;
    animation: shimmer 1.5s infinite;
    background-size: 200% 100%;
  }
  @keyframes shimmer {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }

  /* System info grid */
  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px;
  }
  .info-item {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .info-label {
    font-size: .72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .info-value {
    font-family: var(--mono);
    font-size: .9rem;
    color: var(--text);
    font-weight: 500;
  }

  /* Usage bar */
  .usage-bar {
    margin-top: 6px;
    background: var(--border);
    border-radius: 99px;
    height: 4px;
    overflow: hidden;
  }
  .usage-fill {
    height: 100%;
    border-radius: 99px;
    background: var(--accent);
  }
  .usage-fill.warn  { background: var(--yellow); }
  .usage-fill.danger{ background: var(--accent2); }

  /* Speed boxes */
  .speed-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
    text-align: center;
  }
  .speed-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 10px;
  }
  .speed-val {
    font-size: 1.8rem;
    font-weight: 800;
    font-family: var(--mono);
    color: var(--accent);
    line-height: 1;
  }
  .speed-unit { font-size: .75rem; color: var(--muted); margin-top: 4px; }
  .speed-label { font-size: .8rem; color: var(--text); margin-top: 8px; font-weight: 600; }

  /* Files table */
  .files-table { width: 100%; border-collapse: collapse; }
  .files-table th {
    text-align: left;
    font-size: .72rem;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--muted);
    padding: 0 0 10px;
    border-bottom: 1px solid var(--border);
  }
  .files-table td {
    padding: 12px 0;
    font-family: var(--mono);
    font-size: .85rem;
    border-bottom: 1px solid rgba(42,42,58,.5);
    vertical-align: middle;
  }
  .files-table td:last-child { text-align: right; }
  .tag {
    display: inline-block;
    background: rgba(92,252,160,.1);
    color: var(--green);
    border: 1px solid rgba(92,252,160,.2);
    border-radius: 6px;
    padding: 2px 8px;
    font-size: .75rem;
  }

  /* Toast */
  .toast {
    position: fixed;
    bottom: 28px; right: 28px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 20px;
    font-size: .85rem;
    color: var(--text);
    z-index: 999;
    opacity: 0;
    transform: translateY(10px);
    transition: all .3s;
    pointer-events: none;
    max-width: 320px;
  }
  .toast.show { opacity: 1; transform: none; }
  .toast.error { border-color: rgba(252,92,124,.5); color: var(--accent2); }
  .toast.ok    { border-color: rgba(92,252,160,.5); color: var(--green); }

  @media (max-width: 600px) {
    header { flex-wrap: wrap; }
    .tabs { order: 3; width: 100%; }
    .speed-grid { grid-template-columns: repeat(3,1fr); gap: 8px; }
    .speed-val { font-size: 1.3rem; }
  }
</style>
</head>
<body>
<div class="glow"></div>
<div class="glow glow2"></div>

<div class="wrap">
  <header>
    <div class="logo">⬇</div>
    <h1>Vid<span>Fetch</span></h1>
    <nav class="tabs">
      <button class="tab active" onclick="switchTab('downloader',this)">Downloader</button>
      <button class="tab" onclick="switchTab('files',this)">Files</button>
      <button class="tab" onclick="switchTab('sysinfo',this)">System</button>
      <button class="tab" onclick="switchTab('speedtest',this)">Speed</button>
    </nav>
  </header>

  <!-- ── Downloader ── -->
  <div id="panel-downloader" class="panel active">
    <div class="card">
      <div class="card-title">Paste Video URL</div>
      <div class="input-row">
        <input id="urlInput" class="url-input" type="url"
               placeholder="https://youtube.com/watch?v=..." autocomplete="off">
        <button class="btn btn-primary" id="fetchBtn" onclick="fetchFormats()">Fetch</button>
      </div>
      <div class="status">
        <div class="spinner" id="fetchSpinner"></div>
        <span id="fetchStatus"></span>
      </div>
    </div>

    <div class="card" id="formatsCard" style="display:none">
      <div class="card-title">Available Formats</div>
      <div class="formats" id="formatsList"></div>
      <button class="dl-btn" id="dlBtn" onclick="startDownload()" style="display:none">
        ⬇ Download MP4
      </button>
      <div class="progress-wrap" id="progressWrap">
        <div class="progress-bar-bg"><div class="progress-bar" id="progressBar"></div></div>
        <div class="status"><span id="dlStatus"></span></div>
      </div>
    </div>
  </div>

  <!-- ── Files ── -->
  <div id="panel-files" class="panel">
    <div class="card">
      <div class="card-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>Downloaded Files</span>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm btn-primary" onclick="loadFiles()">↺ Refresh</button>
          <button class="btn btn-sm btn-danger" onclick="cleanupFiles()">🗑 Cleanup</button>
        </div>
      </div>
      <div id="filesContent"><div class="status"><span style="color:var(--muted)">Loading…</span></div></div>
    </div>
  </div>

  <!-- ── System Info ── -->
  <div id="panel-sysinfo" class="panel">
    <div class="card">
      <div class="card-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>System Information</span>
        <button class="btn btn-sm btn-primary" onclick="loadSysinfo()">↺ Refresh</button>
      </div>
      <div id="sysinfoContent"><div class="status"><span style="color:var(--muted)">Loading…</span></div></div>
    </div>
  </div>

  <!-- ── Speed Test ── -->
  <div id="panel-speedtest" class="panel">
    <div class="card">
      <div class="card-title">Internet Speed Test</div>
      <button class="btn btn-primary" id="speedBtn" onclick="runSpeedtest()">▶ Run Speed Test</button>
      <div class="status"><div class="spinner" id="speedSpinner"></div><span id="speedStatus"></span></div>
      <div id="speedResult" style="display:none;margin-top:20px">
        <div class="speed-grid">
          <div class="speed-box">
            <div class="speed-val" id="spDownload">—</div>
            <div class="speed-unit">Mbps</div>
            <div class="speed-label">⬇ Download</div>
          </div>
          <div class="speed-box">
            <div class="speed-val" id="spUpload">—</div>
            <div class="speed-unit">Mbps</div>
            <div class="speed-label">⬆ Upload</div>
          </div>
          <div class="speed-box">
            <div class="speed-val" id="spPing">—</div>
            <div class="speed-unit">ms</div>
            <div class="speed-label">📶 Ping</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  let currentFormats = [];

  // ── Tab switching ──────────────────────────────────────────────────────────
  function switchTab(name, el) {
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById('panel-' + name).classList.add('active');
    el.classList.add('active');
    if (name === 'files')    loadFiles();
    if (name === 'sysinfo')  loadSysinfo();
  }

  // ── Toast ──────────────────────────────────────────────────────────────────
  function toast(msg, type='') {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show ' + type;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.className = 'toast', 3000);
  }

  // ── Fetch formats ──────────────────────────────────────────────────────────
  async function fetchFormats() {
    const url = document.getElementById('urlInput').value.trim();
    if (!url) { toast('Please paste a video URL.', 'error'); return; }

    setFetchLoading(true, 'Fetching video info…');
    document.getElementById('formatsCard').style.display = 'none';

    try {
      const res  = await fetch('/api/fetch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url})
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      currentFormats = data.formats;
      renderFormats(data.formats);
      setFetchLoading(false, `Found ${data.formats.length} format(s).`);
    } catch(e) {
      setFetchLoading(false, '');
      toast(e.message, 'error');
    }
  }

  function setFetchLoading(on, msg) {
    document.getElementById('fetchBtn').disabled = on;
    document.getElementById('fetchSpinner').classList.toggle('active', on);
    document.getElementById('fetchStatus').textContent = msg;
  }

  function renderFormats(formats) {
    const list = document.getElementById('formatsList');
    list.innerHTML = formats.map((f, i) => {
      const label = f.resolution.includes('x')
        ? f.resolution
        : f.resolution;
      // Derive quality badge from resolution string
      const num = parseInt((f.resolution.match(/\d+/) || ['0'])[0]);
      const badge = num >= 1080 ? 'FHD' : num >= 720 ? 'HD' : num >= 480 ? 'SD' : num > 0 ? 'LD' : 'MP4';
      const size  = f.filesize_mb > 0 ? f.filesize_mb + ' MB' : '';
      const sel   = i === 0 ? 'selected' : '';
      return `
        <div class="fmt-row ${sel}" onclick="selectFormat(${i}, this)">
          <div class="fmt-radio"><div class="fmt-radio-dot"></div></div>
          <div class="fmt-info">
            <span class="fmt-res">${label}</span>
            <span class="fmt-badge">${badge}</span>
          </div>
          ${size ? `<span class="fmt-size">${size}</span>` : ''}
        </div>`;
    }).join('');

    document.getElementById('formatsCard').style.display = 'block';
    document.getElementById('progressWrap').classList.remove('active');
    document.getElementById('dlBtn').style.display = 'flex';
    selectedFormatIdx = 0;
  }

  let selectedFormatIdx = 0;

  function selectFormat(idx, el) {
    document.querySelectorAll('.fmt-row').forEach(r => r.classList.remove('selected'));
    el.classList.add('selected');
    selectedFormatIdx = idx;
  }

  // ── Download ───────────────────────────────────────────────────────────────
  async function startDownload() {
    const fmt = currentFormats[selectedFormatIdx];
    document.getElementById('dlBtn').disabled = true;
    document.querySelectorAll('.fmt-row').forEach(r => r.style.pointerEvents = 'none');

    const progressWrap = document.getElementById('progressWrap');
    const progressBar  = document.getElementById('progressBar');
    const dlStatus     = document.getElementById('dlStatus');

    progressWrap.classList.add('active');
    progressBar.style.width = '10%';
    dlStatus.textContent = `Downloading ${fmt.resolution}…`;

    // Animate progress (indeterminate since we stream server-side)
    let pct = 10;
    const interval = setInterval(() => {
      pct = Math.min(pct + Math.random() * 6, 88);
      progressBar.style.width = pct + '%';
    }, 600);

    try {
      const res  = await fetch('/api/download', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url: fmt.url, resolution: fmt.resolution})
      });
      const data = await res.json();
      clearInterval(interval);

      if (data.error) throw new Error(data.error);

      progressBar.style.width = '100%';
      dlStatus.textContent = `✅ Ready — ${data.size_mb} MB`;

      // Trigger browser download
      const a = document.createElement('a');
      a.href = data.download;
      a.download = data.filename;
      a.click();
      toast(`Downloaded: ${data.filename}`, 'ok');

    } catch(e) {
      clearInterval(interval);
      progressBar.style.width = '0%';
      dlStatus.textContent = '❌ ' + e.message;
      toast(e.message, 'error');
    } finally {
      document.getElementById('dlBtn').disabled = false;
      document.querySelectorAll('.fmt-row').forEach(r => r.style.pointerEvents = '');
    }
  }

  // ── Files ──────────────────────────────────────────────────────────────────
  async function loadFiles() {
    const el = document.getElementById('filesContent');
    el.innerHTML = '<div class="status"><span style="color:var(--muted)">Loading…</span></div>';
    try {
      const data = await fetch('/api/files').then(r=>r.json());
      if (!data.files.length) {
        el.innerHTML = '<div class="status"><span style="color:var(--muted)">No files in downloads folder.</span></div>';
        return;
      }
      el.innerHTML = `<table class="files-table">
        <thead><tr><th>Filename</th><th>Size</th><th>Action</th></tr></thead>
        <tbody>${data.files.map(f=>`
          <tr>
            <td>${f.name}</td>
            <td><span class="tag">${f.size_mb} MB</span></td>
            <td><a href="${f.url}" download style="text-decoration:none">
              <button class="btn btn-sm btn-primary">⬇</button></a></td>
          </tr>`).join('')}
        </tbody></table>`;
    } catch(e) { el.innerHTML = '<div class="status" style="color:var(--accent2)">Failed to load files.</div>'; }
  }

  async function cleanupFiles() {
    if (!confirm('Delete all downloaded files?')) return;
    try {
      const data = await fetch('/api/cleanup',{method:'POST'}).then(r=>r.json());
      toast(`Removed ${data.removed} file(s).`, 'ok');
      loadFiles();
    } catch(e) { toast('Cleanup failed.','error'); }
  }

  // ── System Info ────────────────────────────────────────────────────────────
  async function loadSysinfo() {
    const el = document.getElementById('sysinfoContent');
    el.innerHTML = '<div class="status"><span style="color:var(--muted)">Fetching…</span></div>';
    try {
      const d = await fetch('/api/sysinfo').then(r=>r.json());
      if (d.error) throw new Error(d.error);
      const ramColor  = d.ram_used  > 80 ? 'danger' : d.ram_used  > 60 ? 'warn' : '';
      const diskColor = d.disk_used > 80 ? 'danger' : d.disk_used > 60 ? 'warn' : '';
      el.innerHTML = `<div class="info-grid">
        ${infoItem('CPU', d.cpu)}
        ${infoItem('RAM', `${d.ram_used}% of ${d.ram_total} GB`, d.ram_used, ramColor)}
        ${infoItem('Disk', `${d.disk_used}% of ${d.disk_total} GB`, d.disk_used, diskColor)}
        ${infoItem('Hostname', d.hostname)}
        ${infoItem('Private IP', d.local_ip)}
        ${infoItem('Public IP', d.public_ip)}
        ${infoItem('Location', d.location)}
        ${infoItem('ISP', d.isp)}
      </div>`;
    } catch(e) { el.innerHTML = `<div class="status" style="color:var(--accent2)">${e.message}</div>`; }
  }

  function infoItem(label, value, pct, color='') {
    const bar = pct != null ? `<div class="usage-bar"><div class="usage-fill ${color}" style="width:${pct}%"></div></div>` : '';
    return `<div class="info-item">
      <div class="info-label">${label}</div>
      <div class="info-value">${value}</div>${bar}
    </div>`;
  }

  // ── Speed Test ─────────────────────────────────────────────────────────────
  async function runSpeedtest() {
    document.getElementById('speedBtn').disabled = true;
    document.getElementById('speedSpinner').classList.add('active');
    document.getElementById('speedStatus').textContent = 'Running… this may take ~30s';
    document.getElementById('speedResult').style.display = 'none';
    try {
      const d = await fetch('/api/speedtest').then(r=>r.json());
      if (d.error) throw new Error(d.error);
      document.getElementById('spDownload').textContent = d.download;
      document.getElementById('spUpload').textContent   = d.upload;
      document.getElementById('spPing').textContent     = d.ping;
      document.getElementById('speedResult').style.display = 'block';
      document.getElementById('speedStatus').textContent = '';
      toast('Speed test complete!', 'ok');
    } catch(e) {
      document.getElementById('speedStatus').textContent = '❌ ' + e.message;
      toast(e.message, 'error');
    } finally {
      document.getElementById('speedBtn').disabled = false;
      document.getElementById('speedSpinner').classList.remove('active');
    }
  }

  // Enter key on URL input
  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') fetchFormats();
  });
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🌐 Starting VidFetch on http://0.0.0.0:%d", PORT)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
