import json
import os
import time
import threading
from functools import wraps
import werkzeug.utils

from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS

import cloudinary
import cloudinary.uploader
import cloudinary.api
from dotenv import load_dotenv

load_dotenv()

cloudinary.config( 
  cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"), 
  api_key = os.getenv("CLOUDINARY_API_KEY"), 
  api_secret = os.getenv("CLOUDINARY_API_SECRET") 
)

app = Flask(__name__)
CORS(app)
UPLOAD_FOLDER = "/tmp/uploads" 
CLOUDINARY_FILES_JSON = "cloudinary_files.json"

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def load_files():
    if os.path.exists(CLOUDINARY_FILES_JSON):
        try:
            with open(CLOUDINARY_FILES_JSON, "r") as f:
                return json.load(f)
        except:
            pass
    return []

def save_files(files):
    with open(CLOUDINARY_FILES_JSON, "w") as f:
        json.dump(files, f, indent=4)

PUBLIC_SETTINGS = {
    "require_password": False,
    "guest_username": "guest",
    "guest_password": "0000",
    "allow_uploads": True,
    "allow_downloads": True
}

SETTINGS_JSON = "settings.json"
def load_settings():
    global PUBLIC_SETTINGS
    if os.path.exists(SETTINGS_JSON):
        try:
            with open(SETTINGS_JSON, "r") as f:
                PUBLIC_SETTINGS.update(json.load(f))
        except:
            pass

def save_settings():
    with open(SETTINGS_JSON, "w") as f:
        json.dump(PUBLIC_SETTINGS, f)

load_settings()

def check_guest_auth(username, password):
    return username == PUBLIC_SETTINGS["guest_username"] and password == PUBLIC_SETTINGS["guest_password"]

def check_admin_auth(username, password):
    return username == 'admin' and password == '1234'

def requires_guest_auth_if_enabled(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if PUBLIC_SETTINGS["require_password"]:
            auth = request.authorization
            if not auth or not check_guest_auth(auth.username, auth.password):
                return Response('{"error": "Unauthorized"}', 401, {'WWW-Authenticate': 'Basic realm="Guest Login Required"', 'Content-Type': 'application/json'})
        return f(*args, **kwargs)
    return decorated

def requires_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_admin_auth(auth.username, auth.password):
            return Response('{"error": "Unauthorized"}', 401, {'WWW-Authenticate': 'Basic realm="Login Required"', 'Content-Type': 'application/json'})
        return f(*args, **kwargs)
    return decorated

def delete_old_files():
    now = time.time()
    c_files = load_files()
    new_files = []
    deleted_any = False
    for f in c_files:
        if now - f.get("timestamp", now) > 3600:
            try:
                if f['public_id'].startswith('local:'):
                    local_file = os.path.join(UPLOAD_FOLDER, f['public_id'].split('local:')[1])
                    if os.path.exists(local_file):
                        os.remove(local_file)
                else:
                    cloudinary.uploader.destroy(f['public_id'])
                deleted_any = True
            except:
                pass
        else:
            new_files.append(f)
            
    if deleted_any:
        save_files(new_files)

def run_cleaner():
    while True:
        delete_old_files()
        time.sleep(300)

threading.Thread(target=run_cleaner, daemon=True).start()

@app.route('/')
def index():
    return jsonify({"status": "API is running"})

@app.route('/api/download/local/<path:filename>', methods=['GET'])
def download_local(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify({
        "require_password": PUBLIC_SETTINGS["require_password"],
        "allow_uploads": PUBLIC_SETTINGS["allow_uploads"],
        "allow_downloads": PUBLIC_SETTINGS["allow_downloads"]
    })

@app.route('/api/files', methods=['GET'])
@requires_guest_auth_if_enabled
def list_files():
    if not PUBLIC_SETTINGS["allow_downloads"]:
        return jsonify({"error": "Downloads disabled"}), 403
    return jsonify({"files": load_files()})

@app.route('/api/upload', methods=['POST'])
@requires_guest_auth_if_enabled
def upload_file():
    if not PUBLIC_SETTINGS["allow_uploads"]:
        return jsonify({"error": "Uploads disabled"}), 403
        
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    if file:
        safe_name = werkzeug.utils.secure_filename(file.filename)
        if not safe_name:
            safe_name = f"upload_{int(time.time())}"
            
        temp_path = os.path.abspath(os.path.join(UPLOAD_FOLDER, safe_name))
        file.save(temp_path)
        try:
            file_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
            file_ext = os.path.splitext(temp_path)[1].lower()
            
            # Hybrid Storage: Use local disk for files > 9.5MB OR for PDFs (Cloudinary strictly blocks inline PDF rendering on free tiers)
            if file_size_mb > 9.5 or file_ext == ".pdf":
                unique_local_name = f"{int(time.time())}_{safe_name}"
                local_path = os.path.join(UPLOAD_FOLDER, unique_local_name)
                os.rename(temp_path, local_path)
                
                c_files = load_files()
                file_data = {
                    "filename": file.filename,
                    "public_id": f"local:{unique_local_name}",
                    "url": request.host_url.rstrip('/') + f"/api/download/local/{unique_local_name}",
                    "timestamp": time.time(),
                    "size_mb": file_size_mb
                }
                c_files.append(file_data)
                save_files(c_files)
                return jsonify({"success": True, "file": file_data})

            # Force 'raw' resource type and 'authenticated' type for PDFs to bypass strict security policies
            file_ext = os.path.splitext(temp_path)[1].lower()
            res_type = "raw" if file_ext == ".pdf" else "auto"
            upload_type = "authenticated" if file_ext == ".pdf" else "upload"
            
            result = cloudinary.uploader.upload(temp_path, resource_type=res_type, type=upload_type, use_filename=True, unique_filename=True, folder="themover")
            
            # Generate a signed URL for authenticated resources, forcing direct download
            signed_url, options = cloudinary.utils.cloudinary_url(
                result["public_id"],
                resource_type=result.get("resource_type", res_type),
                type=upload_type,
                flags="attachment",
                sign_url=True
            )
            
            c_files = load_files()
            file_data = {
                "filename": file.filename,
                "public_id": result["public_id"],
                "url": signed_url,
                "timestamp": time.time(),
                "size_mb": os.path.getsize(temp_path) / (1024 * 1024)
            }
            c_files.append(file_data)
            save_files(c_files)
            return jsonify({"success": True, "file": file_data})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

@app.route('/api/admin/files', methods=['GET'])
@requires_admin_auth
def admin_list_files():
    return jsonify({
        "files": load_files(),
        "settings": PUBLIC_SETTINGS
    })

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
    data = request.json
    public_id = data.get('public_id')
    if not public_id:
        return jsonify({"error": "Missing public_id"}), 400
        
    try:
        if public_id.startswith('local:'):
            local_file = os.path.join(UPLOAD_FOLDER, public_id.split('local:')[1])
            if os.path.exists(local_file):
                os.remove(local_file)
        else:
            cloudinary.uploader.destroy(public_id)
    except Exception as e:
        pass
        
    c_files = load_files()
    c_files = [f for f in c_files if f['public_id'] != public_id]
    save_files(c_files)
    
    return jsonify({"success": True})

@app.route('/api/admin/delete_all', methods=['POST'])
@requires_admin_auth
def admin_delete_all():
    c_files = load_files()
    for f in c_files:
        try:
            if f['public_id'].startswith('local:'):
                local_file = os.path.join(UPLOAD_FOLDER, f['public_id'].split('local:')[1])
                if os.path.exists(local_file):
                    os.remove(local_file)
            else:
                cloudinary.uploader.destroy(f['public_id'])
        except:
            pass
    save_files([])
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
