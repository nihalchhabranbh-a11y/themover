#!/usr/bin/env python3
"""
TheMover LOCAL MODE
-------------------
Run this script to use TheMover fully offline on your local Wi-Fi.
No internet required. Mac + Phone on same Wi-Fi is enough.

Usage:  python3 themover_local.py
Then:   Open the URL shown on your phone browser.
"""

import os, sys, json, time, socket, threading, webbrowser, subprocess
from pathlib import Path

# ── find local IP ──────────────────────────────────────────────────────────────
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except:
        return "127.0.0.1"
    finally:
        s.close()

LOCAL_IP   = get_local_ip()
PORT       = 5001
UPLOAD_DIR = Path("/tmp/themover_local")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── install deps if missing ────────────────────────────────────────────────────
def pip_install(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])

try:
    from flask import Flask, request, jsonify, send_from_directory, Response
    from flask_cors import CORS
    from flask_socketio import SocketIO, emit
except ImportError:
    print("Installing dependencies…")
    pip_install("flask", "flask-cors", "flask-socketio", "eventlet")
    from flask import Flask, request, jsonify, send_from_directory, Response
    from flask_cors import CORS
    from flask_socketio import SocketIO, emit

# ── Flask app ──────────────────────────────────────────────────────────────────
app     = Flask(__name__, static_folder=None)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

files_db      = {}   # public_id → {name, size, path, url}
online_uploaders = {}  # public_id → sid

# ── serve inline frontend (no Vercel needed) ───────────────────────────────────
FRONTEND_HTML = open(Path(__file__).parent / "frontend" / "index.html").read()

@app.route("/")
@app.route("/index.html")
def serve_index():
    # Inject local server URL so the page never calls Render
    html = FRONTEND_HTML.replace(
        'io("https://themover-3r8d.onrender.com")',
        f'io("http://{LOCAL_IP}:{PORT}")'
    ).replace(
        '"https://themover-3r8d.onrender.com"',
        f'"http://{LOCAL_IP}:{PORT}"'
    ).replace(
        'const API_BASE = "https://themover-3r8d.onrender.com/api"',
        f'const API_BASE = "http://{LOCAL_IP}:{PORT}/api"'
    )
    return html, 200, {"Content-Type": "text/html"}

@app.route("/manifest.json")
def serve_manifest():
    mf = Path(__file__).parent / "frontend" / "manifest.json"
    if mf.exists():
        return send_from_directory(str(mf.parent), "manifest.json")
    return jsonify({"name": "TheMover Local"}), 200

# ── API: Chunked Upload (matches frontend flow) ────────────────────────────────
chunk_uploads = {}  # upload_id -> { filename, total, chunks: {index: bytes} }

@app.route("/api/upload/init", methods=["POST"])
def upload_init():
    data = request.get_json()
    uid = data.get("upload_id") or (str(int(time.time())) + str(len(chunk_uploads)))
    chunk_uploads[uid] = {
        "filename": data.get("filename", "file"),
        "total": int(data.get("total_chunks", 1)),
        "chunks": {}
    }
    return jsonify({"upload_id": uid})

@app.route("/api/upload/chunk", methods=["POST"])
def upload_chunk():
    uid = request.form.get("upload_id")
    idx = int(request.form.get("index", 0))
    chunk_file = request.files.get("chunk")
    if not uid or not chunk_file:
        return jsonify({"error": "missing data"}), 400
    if uid not in chunk_uploads:
        chunk_uploads[uid] = {"filename": request.form.get("filename","file"), "total": 1, "chunks": {}}
    chunk_uploads[uid]["chunks"][idx] = chunk_file.read()
    return jsonify({"status": "ok"})

@app.route("/api/upload/finalize", methods=["POST"])
def upload_finalize():
    data = request.get_json()
    uid = data.get("upload_id")
    filename = data.get("filename", "file")
    group = data.get("group", "")
    
    if uid not in chunk_uploads:
        return jsonify({"error": "upload not found"}), 404
    
    info = chunk_uploads.pop(uid)
    safe = filename.replace("/", "_")
    dest = UPLOAD_DIR / safe
    
    # Write all chunks in order
    with open(str(dest), "wb") as f:
        for i in sorted(info["chunks"].keys()):
            f.write(info["chunks"][i])
    
    size = dest.stat().st_size
    public_id = "local:" + safe
    url = f"http://{LOCAL_IP}:{PORT}/api/local/{safe}"
    
    file_record = {
        "filename": safe, "size_mb": size / (1024*1024),
        "url": url, "public_id": public_id, "path": str(dest), "group": group
    }
    files_db[public_id] = file_record
    return jsonify({"file": file_record})

# ── API: legacy single upload (kept for compat) ────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    safe = f.filename.replace("/", "_")
    dest = UPLOAD_DIR / safe
    f.save(str(dest))
    public_id = "local:" + safe
    url = f"http://{LOCAL_IP}:{PORT}/api/local/{safe}"
    files_db[public_id] = {"filename": safe, "size_mb": dest.stat().st_size/(1024*1024),
                            "url": url, "public_id": public_id, "path": str(dest)}
    return jsonify({"file": {"url": url, "public_id": public_id,
                              "filename": safe, "size_mb": dest.stat().st_size/(1024*1024)}})

# ── API: list files ────────────────────────────────────────────────────────────
@app.route("/api/files")
def list_files():
    group = request.args.get("group", "")
    # Return all files (local mode doesn't enforce group filter strictly)
    return jsonify({"files": list(files_db.values())})

# ── API: download local file ───────────────────────────────────────────────────
@app.route("/api/local/<path:filename>")
def serve_local(filename):
    return send_from_directory(str(UPLOAD_DIR), filename)

# ── API: delete ────────────────────────────────────────────────────────────────
@app.route("/api/delete/<public_id>", methods=["DELETE"])
def delete_file(public_id):
    info = files_db.pop(public_id, None)
    if info:
        try: os.remove(info["path"])
        except: pass
    return jsonify({"status": "ok"})

# ── API: settings (stub) ──────────────────────────────────────────────────────
@app.route("/api/settings")
def settings():
    return jsonify({"require_password": False, "room": "default"})

@app.route("/api/settings", methods=["POST"])
def set_settings():
    return jsonify({"status": "ok"})

# ── Socket.io: relay transfer ─────────────────────────────────────────────────
@socketio.on("register_uploader")
def reg_uploader(data):
    online_uploaders[data["public_id"]] = request.sid

@socketio.on("disconnect")
def on_disconnect():
    for k, v in list(online_uploaders.items()):
        if v == request.sid:
            del online_uploaders[k]

@socketio.on("request_download")
def req_download(data):
    pid = data.get("public_id")
    if pid in online_uploaders:
        emit("relay_send_file",
             {"downloader_sid": request.sid, "public_id": pid},
             to=online_uploaders[pid])
    elif pid in files_db:
        # File is on disk — tell downloader to use local URL
        emit("use_cloud", {"public_id": pid, "url": files_db[pid]["url"]})
    else:
        emit("use_cloud", {"public_id": pid})

@socketio.on("relay_chunk")
def relay_chunk(data, *args):
    dst = data.get("downloader_sid")
    if dst:
        emit("relay_chunk", data, to=dst)

@socketio.on("uploader_unavailable")
def uploader_na(data):
    dst = data.get("downloader_sid")
    pid = data.get("public_id")
    if dst:
        # File might still be on disk
        url = files_db.get(pid, {}).get("url")
        emit("use_cloud", {"public_id": pid, "url": url}, to=dst)

from flask_socketio import join_room, leave_room
workspace_members = {}
workspaces = {}

@socketio.on('join_workspace')
def handle_join_workspace(data):
    code = str(data.get('code', '')).upper()
    name = str(data.get('name', 'Anonymous'))[:30]
    if not code: return

    join_room(code)
    workspace_members.setdefault(code, {})[request.sid] = {'name': name}

    ws = workspaces.setdefault(code, {'messages': []})
    emit('chat_history', ws['messages'][-200:])

    members = [{'sid': sid, 'name': v['name']} for sid, v in workspace_members.get(code, {}).items()]
    emit('members_list', members, to=code)
    emit('user_join', {'sid': request.sid, 'name': name}, to=code, include_self=False)

@socketio.on('chat_message')
def handle_chat_message(data):
    code = str(data.get('code', '')).upper()
    msg  = {
        'text':      str(data.get('text', ''))[:2000],
        'name':      str(data.get('name', 'Anonymous'))[:30],
        'file_url':  data.get('file_url'),
        'file_name': data.get('file_name'),
        'time':      time.time()
    }
    ws = workspaces.get(code)
    if ws:
        ws.setdefault('messages', []).append(msg)
        if len(ws['messages']) > 200:
            ws['messages'] = ws['messages'][-200:]
    emit('chat_message', msg, to=code)

@socketio.on('cursor_move')
def handle_cursor_move(data):
    code = data.get('code')
    if code and code in workspace_members and request.sid in workspace_members[code]:
        name = workspace_members[code][request.sid]['name']
        emit('cursor_moved', {
            'sid': request.sid,
            'name': name,
            'x': data.get('x'),
            'y': data.get('y')
        }, room=code, include_self=False)

@socketio.on('file_edit')
def handle_file_edit(data):
    code = str(data.get('code', '')).upper()
    emit('file_edited', {
        'public_id': data.get('public_id'),
        'text': data.get('text'),
        'sid': request.sid
    }, to=code, include_self=False)

@socketio.on('call_join')
def handle_call_join(data):
    code = str(data.get('code', '')).upper()
    name = str(data.get('name', 'Anonymous'))
    emit('call_peer_joined', {'sid': request.sid, 'name': name}, to=code, include_self=False)

@socketio.on('call_offer')
def handle_call_offer(data):
    target = data.get('target')
    if target: emit('call_offer', {'offer': data.get('offer'), 'sender': request.sid, 'name': data.get('name', '')}, to=target)

@socketio.on('call_answer')
def handle_call_answer(data):
    target = data.get('target')
    if target: emit('call_answer', {'answer': data.get('answer'), 'sender': request.sid}, to=target)

@socketio.on('call_ice')
def handle_call_ice(data):
    target = data.get('target')
    if target: emit('call_ice', {'candidate': data.get('candidate'), 'sender': request.sid}, to=target)

@socketio.on('call_leave')
def handle_call_leave(data):
    code = str(data.get('code', '')).upper()
    emit('call_peer_left', {'sid': request.sid}, to=code, include_self=False)

# ── main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    url = f"http://{LOCAL_IP}:{PORT}"
    print()
    print("=" * 52)
    print("  🚀  TheMover LOCAL MODE")
    print("=" * 52)
    print(f"  Mac  → {url}")
    print(f"  Phone → open the URL above in your browser")
    print()
    print("  📱  QR Code (scan with phone camera):")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"       {url}")
        print("  (pip install qrcode  for QR code)")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 52)

    # Open browser on Mac automatically
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
