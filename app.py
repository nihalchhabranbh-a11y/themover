import json
import os
import time
import threading
import random
import string
from functools import wraps
import werkzeug.utils

from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room

import cloudinary
import cloudinary.uploader
import cloudinary.api
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
  cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
  api_key    = os.getenv("CLOUDINARY_API_KEY"),
  api_secret = os.getenv("CLOUDINARY_API_SECRET"),
  secure     = True
)

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

UPLOAD_FOLDER = "/tmp/uploads"
CLOUDINARY_FILES_JSON = "cloudinary_files.json"
WORKSPACES_JSON = "workspaces.json"
SETTINGS_JSON = "settings.json"

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# ─── File helpers ────────────────────────────────────────────────────────────

def load_files():
    if os.path.exists(CLOUDINARY_FILES_JSON):
        try:
            with open(CLOUDINARY_FILES_JSON, "r") as f:
                return json.load(f)
        except:
            pass

    print("Database missing. Rebuilding from Cloudinary...")
    try:
        import datetime
        rebuilt = []
        pub  = cloudinary.api.resources(type="upload",        prefix="themover/", max_results=100, tags=True)
        auth = cloudinary.api.resources(type="authenticated", prefix="themover/", max_results=100, tags=True)

        for r in pub.get('resources', []) + auth.get('resources', []):
            dt = datetime.datetime.strptime(r['created_at'], "%Y-%m-%dT%H:%M:%SZ")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            group = 'public'
            for t in r.get('tags', []):
                if str(t).startswith('group_'):
                    group = str(t).replace('group_', '')
            file_data = {
                "filename": r.get('original_filename', 'file') + '.' + r.get('format', 'bin'),
                "public_id": r['public_id'],
                "url": r['secure_url'],
                "timestamp": dt.timestamp(),
                "size_mb": r['bytes'] / (1024 * 1024),
                "group": group
            }
            if r.get('type') == 'authenticated':
                signed_url, _ = cloudinary.utils.cloudinary_url(
                    r["public_id"], resource_type=r.get("resource_type","image"),
                    type="authenticated", flags="attachment", sign_url=True)
                file_data["url"] = signed_url
            elif r.get('type') == 'upload':
                dl_url, _ = cloudinary.utils.cloudinary_url(
                    r["public_id"], resource_type=r.get("resource_type","image"),
                    type="upload", flags="attachment")
                file_data["url"] = dl_url
            rebuilt.append(file_data)
        save_files(rebuilt)
        return rebuilt
    except Exception as e:
        print("Cloudinary Rebuild Error:", e)
        return []

def save_files(files):
    with open(CLOUDINARY_FILES_JSON, "w") as f:
        json.dump(files, f, indent=4)

# ─── Settings ─────────────────────────────────────────────────────────────────

PUBLIC_SETTINGS = {
    "require_password": False,
    "guest_username": "guest",
    "guest_password": "0000",
    "allow_uploads": True,
    "allow_downloads": True
}

def load_settings():
    global PUBLIC_SETTINGS
    if os.path.exists(SETTINGS_JSON):
        try:
            with open(SETTINGS_JSON) as f:
                PUBLIC_SETTINGS.update(json.load(f))
        except:
            pass

def save_settings():
    with open(SETTINGS_JSON, "w") as f:
        json.dump(PUBLIC_SETTINGS, f)

load_settings()

# ─── Workspaces ───────────────────────────────────────────────────────────────

workspaces = {}   # { CODE: { name, created_at, messages: [] } }

def load_workspaces():
    global workspaces
    if os.path.exists(WORKSPACES_JSON):
        try:
            with open(WORKSPACES_JSON) as f:
                workspaces = json.load(f)
        except:
            workspaces = {}

def save_workspaces():
    with open(WORKSPACES_JSON, "w") as f:
        json.dump(workspaces, f, indent=2)

def gen_code(length=6):
    chars = string.ascii_uppercase + string.digits
    code = ''.join(random.choices(chars, k=length))
    while code in workspaces:
        code = ''.join(random.choices(chars, k=length))
    return code

load_workspaces()

# ─── Workspace members (in-memory only) ───────────────────────────────────────

# { CODE: { sid: { name } } }
workspace_members = {}

# ─── Chunked upload state ─────────────────────────────────────────────────────

# { upload_id: { chunks: {index: path}, filename, total } }
chunk_uploads = {}

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def check_guest_auth(u, p):
    return u == PUBLIC_SETTINGS["guest_username"] and p == PUBLIC_SETTINGS["guest_password"]

def check_admin_auth(u, p):
    return u == 'admin' and p == '1234'

def requires_guest_auth_if_enabled(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if PUBLIC_SETTINGS["require_password"]:
            auth = request.authorization
            if not auth or not check_guest_auth(auth.username, auth.password):
                return Response('{"error":"Unauthorized"}', 401,
                    {'WWW-Authenticate': 'Basic realm="Guest"', 'Content-Type': 'application/json'})
        return f(*args, **kwargs)
    return decorated

def requires_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_admin_auth(auth.username, auth.password):
            return Response('{"error":"Unauthorized"}', 401,
                {'WWW-Authenticate': 'Basic realm="Admin"', 'Content-Type': 'application/json'})
        return f(*args, **kwargs)
    return decorated

# ─── Auto-cleaner ─────────────────────────────────────────────────────────────

def delete_old_files():
    now = time.time()
    c_files = load_files()
    new_files, deleted_any = [], False
    for f in c_files:
        if now - f.get("timestamp", now) > 3600:
            try:
                if f['public_id'].startswith('local:'):
                    lp = os.path.join(UPLOAD_FOLDER, f['public_id'].split('local:')[1])
                    if os.path.exists(lp): os.remove(lp)
                else:
                    cloudinary.uploader.destroy(f['public_id'])
                deleted_any = True
            except:
                pass
        else:
            new_files.append(f)
    if deleted_any:
        save_files(new_files)

threading.Thread(target=lambda: [delete_old_files() or time.sleep(300) for _ in iter(int, 1)], daemon=True).start()

# ─── Core upload helper ───────────────────────────────────────────────────────

def _do_cloudinary_upload(temp_path, filename, group):
    """Upload assembled file to Cloudinary (or local) and return file_data dict."""
    file_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
    file_ext = os.path.splitext(temp_path)[1].lower()

    if file_size_mb > 9.5 or file_ext == ".pdf":
        unique_name = f"{int(time.time())}_{werkzeug.utils.secure_filename(filename)}"
        local_path = os.path.join(UPLOAD_FOLDER, unique_name)
        os.rename(temp_path, local_path)
        return {
            "filename": filename,
            "public_id": f"local:{unique_name}",
            "url": request.host_url.replace('http://', 'https://').rstrip('/') + f"/api/download/local/{unique_name}",
            "timestamp": time.time(),
            "size_mb": file_size_mb,
            "group": group
        }

    res_type    = "raw"           if file_ext == ".pdf" else "auto"
    upload_type = "authenticated" if file_ext == ".pdf" else "upload"
    result = cloudinary.uploader.upload(
        temp_path, resource_type=res_type, type=upload_type,
        use_filename=True, unique_filename=True, folder="themover", tags=[f"group_{group}"])

    signed_url, _ = cloudinary.utils.cloudinary_url(
        result["public_id"],
        resource_type=result.get("resource_type", res_type),
        type=upload_type, flags="attachment", sign_url=True)
    dl_url, _ = cloudinary.utils.cloudinary_url(
        result["public_id"],
        resource_type=result.get("resource_type", res_type),
        type="upload", flags="attachment")

    return {
        "filename": filename,
        "public_id": result["public_id"],
        "url": signed_url if upload_type == "authenticated" else dl_url,
        "timestamp": time.time(),
        "size_mb": file_size_mb,
        "group": group
    }

# ═════════════════════════════════════════════════════════════════════════════
# REST ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return jsonify({"status": "TheMover API running"})

@app.route('/api/download/local/<path:filename>')
def download_local(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

@app.route('/api/settings')
def get_settings():
    return jsonify({
        "require_password": PUBLIC_SETTINGS["require_password"],
        "allow_uploads":    PUBLIC_SETTINGS["allow_uploads"],
        "allow_downloads":  PUBLIC_SETTINGS["allow_downloads"]
    })

# ─── Files ────────────────────────────────────────────────────────────────────

@app.route('/api/files')
def get_files():
    if PUBLIC_SETTINGS["require_password"]:
        auth = request.authorization
        if not auth or auth.username != PUBLIC_SETTINGS["guest_username"] or auth.password != PUBLIC_SETTINGS["guest_password"]:
            return Response('Could not verify', 401, {'WWW-Authenticate': 'Basic realm="Login"'})
    group_query = request.args.get('group', 'public')
    c_files = [f for f in load_files() if f.get('group', 'public') == group_query]
    return jsonify({"settings": PUBLIC_SETTINGS, "files": c_files})

@app.route('/api/upload', methods=['POST'])
@requires_guest_auth_if_enabled
def upload_file():
    if not PUBLIC_SETTINGS["allow_uploads"]:
        return jsonify({"error": "Uploads disabled"}), 403
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "No file"}), 400

    group     = request.form.get('group', 'public')
    safe_name = werkzeug.utils.secure_filename(file.filename) or f"upload_{int(time.time())}"
    temp_path = os.path.abspath(os.path.join(UPLOAD_FOLDER, safe_name))
    file.save(temp_path)
    try:
        file_data = _do_cloudinary_upload(temp_path, file.filename, group)
        c_files = load_files()
        c_files.append(file_data)
        save_files(c_files)
        return jsonify({"success": True, "file": file_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ─── Chunked upload ───────────────────────────────────────────────────────────

@app.route('/api/upload/chunk', methods=['POST'])
def upload_chunk():
    upload_id   = request.form.get('upload_id')
    chunk_index = int(request.form.get('index', 0))
    total       = int(request.form.get('total', 1))
    filename    = request.form.get('filename', 'file')
    chunk       = request.files.get('chunk')

    if not upload_id or not chunk:
        return jsonify({"error": "Missing data"}), 400

    if upload_id not in chunk_uploads:
        chunk_uploads[upload_id] = {'filename': filename, 'total': total, 'received': 0}

    temp_path = os.path.join(UPLOAD_FOLDER, f"{upload_id}.part")
    mode = 'ab' if chunk_index > 0 else 'wb'
    with open(temp_path, mode) as f:
        f.write(chunk.read())
        
    chunk_uploads[upload_id]['received'] += 1
    return jsonify({"ok": True, "received": chunk_index})

@app.route('/api/upload/finalize', methods=['POST'])
def finalize_upload():
    data      = request.json or {}
    upload_id = data.get('upload_id')
    filename  = data.get('filename', 'file')
    group     = data.get('group', 'public')

    if upload_id not in chunk_uploads:
        return jsonify({"error": "Upload session not found"}), 404

    temp_path = os.path.join(UPLOAD_FOLDER, f"{upload_id}.part")

    safe_name = werkzeug.utils.secure_filename(filename) or f"upload_{int(time.time())}"
    dest = os.path.join(UPLOAD_FOLDER, safe_name)

    try:
        del chunk_uploads[upload_id]
        
        if not os.path.exists(temp_path):
            return jsonify({"error": "Temporary file missing"}), 400

        # Rename .part to actual filename so Cloudinary can detect the correct file extension
        os.rename(temp_path, dest)

        file_data = _do_cloudinary_upload(dest, filename, group)
        c_files = load_files()
        c_files.append(file_data)
        save_files(c_files)
        socketio.emit('file_uploaded', {'file': file_data}, to=group)
        return jsonify({"success": True, "file": file_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in [temp_path, dest]:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass

# ─── Workspaces ───────────────────────────────────────────────────────────────

@app.route('/api/workspace/create', methods=['POST'])
def create_workspace():
    data = request.json or {}
    name = str(data.get('name', 'My Workspace')).strip()[:50]
    admin_name = str(data.get('admin', '')).strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    code = gen_code()
    workspaces[code] = {'name': name, 'created_at': time.time(), 'messages': [], 'admin': admin_name}
    save_workspaces()
    return jsonify({"code": code, "name": name})

@app.route('/api/workspace/<code>')
def get_workspace(code):
    ws = workspaces.get(code.upper())
    if not ws:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"code": code.upper(), "name": ws['name']})

# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route('/api/admin/files')
@requires_admin_auth
def admin_list_files():
    return jsonify({"files": load_files(), "settings": PUBLIC_SETTINGS})

@app.route('/api/admin/toggle/<setting>', methods=['POST'])
@requires_admin_auth
def admin_toggle(setting):
    if setting in PUBLIC_SETTINGS:
        PUBLIC_SETTINGS[setting] = not PUBLIC_SETTINGS[setting]
        save_settings()
        return jsonify({"success": True, "value": PUBLIC_SETTINGS[setting]})
    return jsonify({"error": "Invalid setting"}), 400

@app.route('/api/admin/delete', methods=['POST'])
@requires_admin_auth
def admin_delete():
    public_id = (request.json or {}).get('public_id')
    if not public_id:
        return jsonify({"error": "Missing public_id"}), 400
    try:
        if public_id.startswith('local:'):
            lp = os.path.join(UPLOAD_FOLDER, public_id.split('local:')[1])
            if os.path.exists(lp): os.remove(lp)
        else:
            cloudinary.uploader.destroy(public_id)
    except:
        pass
    c_files = [f for f in load_files() if f['public_id'] != public_id]
    save_files(c_files)
    return jsonify({"success": True})

@app.route('/api/admin/delete_all', methods=['POST'])
@requires_admin_auth
def admin_delete_all():
    for f in load_files():
        try:
            if f['public_id'].startswith('local:'):
                lp = os.path.join(UPLOAD_FOLDER, f['public_id'].split('local:')[1])
                if os.path.exists(lp): os.remove(lp)
            else:
                cloudinary.uploader.destroy(f['public_id'])
        except:
            pass
    save_files([])
    return jsonify({"success": True, "files": [], "settings": PUBLIC_SETTINGS})

# ═════════════════════════════════════════════════════════════════════════════
# SOCKET.IO EVENTS
# ═════════════════════════════════════════════════════════════════════════════

def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

# ─── Workspace presence ───────────────────────────────────────────────────────

@socketio.on('join_workspace')
def handle_join_workspace(data):
    code = str(data.get('code', '')).upper()
    name = str(data.get('name', 'Anonymous'))[:30]
    if not code: return

    join_room(code)
    workspace_members.setdefault(code, {})[request.sid] = {'name': name}

    # Send chat history
    ws = workspaces.get(code, {})
    emit('chat_history', ws.get('messages', [])[-200:])

    # Send member list to everyone
    members = [{'sid': sid, 'name': v['name']} for sid, v in workspace_members.get(code, {}).items()]
    emit('members_list', members, to=code)

    # Announce join
    emit('user_join', {'sid': request.sid, 'name': name}, to=code, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    # Remove from workspace_members
    for code, members in list(workspace_members.items()):
        if request.sid in members:
            name = members[request.sid]['name']
            del members[request.sid]
            emit('user_leave', {'sid': request.sid, 'name': name}, to=code)
            # Update member list
            ml = [{'sid': s, 'name': v['name']} for s, v in members.items()]
            emit('members_list', ml, to=code)
    # Legacy relay cleanup
    to_remove = [k for k, v in online_uploaders.items() if v['sid'] == request.sid]
    for k in to_remove:
        del online_uploaders[k]

# ─── Live Chat ────────────────────────────────────────────────────────────────

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
        save_workspaces()
    emit('chat_message', msg, to=code)

@socketio.on('typing')
def handle_typing(data):
    code = str(data.get('code', '')).upper()
    emit('user_typing', {'name': data.get('name', '')}, to=code, include_self=False)

# ─── Workspace Admin actions ──────────────────────────────────────────────────

@socketio.on('delete_file')
def handle_delete_file(data):
    code = str(data.get('code', '')).upper()
    public_id = data.get('public_id')
    name = data.get('name')
    ws = workspaces.get(code)
    if ws and ws.get('admin') == name:
        try:
            if public_id.startswith('local:'):
                lp = os.path.join(UPLOAD_FOLDER, public_id.split('local:')[1])
                if os.path.exists(lp): os.remove(lp)
            else:
                cloudinary.uploader.destroy(public_id)
        except: pass
        c_files = [f for f in load_files() if f['public_id'] != public_id]
        save_files(c_files)
        emit('file_deleted', {'public_id': public_id}, to=code)

@socketio.on('file_edit')
def handle_file_edit(data):
    code = str(data.get('code', '')).upper()
    if code in workspaces:
        emit('file_edited', {
            'public_id': data.get('public_id'),
            'text': data.get('text'),
            'sid': request.sid
        }, to=code, include_self=False)

@socketio.on('kick_user')
def handle_kick_user(data):
    code = str(data.get('code', '')).upper()
    target_sid = data.get('target_sid')
    name = data.get('name')
    ws = workspaces.get(code)
    if ws and ws.get('admin') == name:
        emit('you_are_kicked', {}, to=target_sid)

@socketio.on('rename_workspace')
def handle_rename_workspace(data):
    code = str(data.get('code', '')).upper()
    new_name = str(data.get('new_name', '')).strip()[:50]
    name = data.get('name')
    ws = workspaces.get(code)
    if ws and ws.get('admin') == name and new_name:
        ws['name'] = new_name
        save_workspaces()
        emit('workspace_renamed', {'new_name': new_name}, to=code)

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

# ─── Video call signaling ─────────────────────────────────────────────────────

@socketio.on('call_join')
def handle_call_join(data):
    code = str(data.get('code', '')).upper()
    name = str(data.get('name', 'Anonymous'))
    emit('call_peer_joined', {'sid': request.sid, 'name': name}, to=code, include_self=False)

@socketio.on('call_offer')
def handle_call_offer(data):
    target = data.get('target')
    if target:
        emit('call_offer', {
            'offer':  data.get('offer'),
            'sender': request.sid,
            'name':   data.get('name', '')
        }, to=target)

@socketio.on('call_answer')
def handle_call_answer(data):
    target = data.get('target')
    if target:
        emit('call_answer', {'answer': data.get('answer'), 'sender': request.sid}, to=target)

@socketio.on('call_ice')
def handle_call_ice(data):
    target = data.get('target')
    if target:
        emit('call_ice', {'candidate': data.get('candidate'), 'sender': request.sid}, to=target)

@socketio.on('call_leave')
def handle_call_leave(data):
    code = str(data.get('code', '')).upper()
    emit('call_peer_left', {'sid': request.sid}, to=code, include_self=False)

# ─── Remote Desktop Control ───────────────────────────────────────────────────
@socketio.on('register_rc_host')
def handle_register_rc_host(data):
    code = str(data.get('code', '')).upper()
    emit('rc_host_available', {}, to=code, include_self=False)

@socketio.on('remote_control')
def handle_remote_control(data):
    code = str(data.get('code', '')).upper()
    # Route directly to the RC Agent in the workspace
    emit('remote_control', data, to=code, include_self=False)

# ─── Legacy relay (kept for backwards compat) ─────────────────────────────────
@app.route('/agent.py')
def get_agent():
    agent_code = """import sys, time
try:
    import socketio, pyautogui
except:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-socketio", "requests", "pyautogui", "websocket-client"])
    import socketio, pyautogui

pyautogui.FAILSAFE = True
sio = socketio.Client()
WS_URL = "https://themover-3r8d.onrender.com"
WORKSPACE_CODE = sys.argv[1] if len(sys.argv) > 1 else ""

@sio.event
def connect():
    print(f"\\n[+] Connected! Registering Host for Workspace: {WORKSPACE_CODE}")
    sio.emit('join_workspace', {'code': WORKSPACE_CODE, 'name': 'AnyDesk_Agent'})
    sio.emit('register_rc_host', {'code': WORKSPACE_CODE})

@sio.on('remote_control')
def on_remote_control(data):
    if data.get('code') != WORKSPACE_CODE: return
    action = data.get('action')
    try:
        if action in ['mousemove', 'mousedown', 'mouseup']:
            x_pct, y_pct = float(data.get('x', 0)), float(data.get('y', 0))
            w, h = pyautogui.size()
            tx, ty = int(x_pct * w), int(y_pct * h)
            if action == 'mousemove': pyautogui.moveTo(tx, ty, duration=0.0)
            elif action == 'mousedown': pyautogui.mouseDown(x=tx, y=ty, button='left')
            elif action == 'mouseup': pyautogui.mouseUp(x=tx, y=ty, button='left')
        elif action == 'keydown':
            key = data.get('key')
            if key:
                if len(key) == 1: pyautogui.press(key)
                elif key == 'Enter': pyautogui.press('enter')
                elif key == 'Backspace': pyautogui.press('backspace')
                elif key == 'Tab': pyautogui.press('tab')
                elif key == 'Escape': pyautogui.press('esc')
    except Exception as e: pass

if __name__ == '__main__':
    if not WORKSPACE_CODE: sys.exit(1)
    sio.connect(WS_URL)
    print("\\n[READY] Your PC is now being shared. Move mouse to corner to abort.")
    sio.wait()
"""
    return agent_code, 200, {'Content-Type': 'text/plain'}

online_uploaders = {}

@socketio.on('register_uploader')
def handle_register(data):
    public_id = data.get('public_id')
    online_uploaders[public_id] = {'sid': request.sid, 'ip': get_client_ip()}

@socketio.on('request_download')
def handle_request_download(data):
    public_id = data.get('public_id')
    if public_id in online_uploaders:
        uploader = online_uploaders[public_id]
        emit('relay_send_file', {'downloader_sid': request.sid, 'public_id': public_id}, to=uploader['sid'])
        return
    emit('use_cloud', {'public_id': public_id}, to=request.sid)

@socketio.on('relay_chunk')
def handle_relay_chunk(data, *args):
    downloader_sid = data.get('downloader_sid')
    if downloader_sid:
        emit('relay_chunk', data, to=downloader_sid)

@socketio.on('uploader_unavailable')
def handle_uploader_unavailable(data):
    downloader_sid = data.get('downloader_sid')
    public_id      = data.get('public_id')
    if downloader_sid:
        emit('use_cloud', {'public_id': public_id}, to=downloader_sid)

# ─── Flash Share signaling (legacy) ───────────────────────────────────────────

xender_rooms = {}

@socketio.on('xender_host')
def handle_xender_host(data):
    room = data.get('room')
    if room: xender_rooms[room] = request.sid

@socketio.on('xender_join')
def handle_xender_join(data):
    room = data.get('room')
    if room and room in xender_rooms:
        emit('xender_joined', {'sid': request.sid}, to=xender_rooms[room])

@socketio.on('xender_signal')
def handle_xender_signal(data):
    target = data.get('target')
    if target:
        emit('xender_signal', {'signal': data.get('signal'), 'sender': request.sid}, to=target)

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5001)
