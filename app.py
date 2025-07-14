import os
import re
import time
import threading
import hashlib
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
import httpx

app = Flask(__name__)

# Configuration
BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp_downloads"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
CLEANUP_INTERVAL = 60  # Seconds
FILE_LIFETIME = 1800  # 30 minutes in seconds

# Create temp directory if not exists
TEMP_DIR.mkdir(exist_ok=True)

# Thread-safe file tracking
file_tracker = {}
tracker_lock = threading.Lock()

def validate_url(url):
    """Validate HTTP/HTTPS URL format"""
    return re.match(r'^https?://[^\s/$.?#].[^\s]*$', url, re.IGNORECASE)

def sanitize_filename(filename):
    """Sanitize filename and hash if too long"""
    safe_name = re.sub(r'[^\w\.-]', '_', filename)
    if len(safe_name) > 100:
        prefix = safe_name[:50]
        suffix = safe_name[-10:]
        hash_str = hashlib.md5(safe_name.encode()).hexdigest()[:8]
        safe_name = f"{prefix}_{hash_str}_{suffix}"
    return safe_name

def download_file(url):
    """Download file with streaming support"""
    with httpx.Client(timeout=30.0) as client:
        with client.stream('GET', url) as response:
            response.raise_for_status()
            
            # Get filename
            filename = url.split('/')[-1]
            if 'content-disposition' in response.headers:
                match = re.search(r'filename="?([^"]+)"?', response.headers['content-disposition'])
                if match:
                    filename = match.group(1)
            
            # Sanitize filename
            filename = sanitize_filename(filename)
            file_path = TEMP_DIR / filename
            
            # Ensure unique filename
            counter = 1
            while file_path.exists():
                stem = file_path.stem
                suffix = file_path.suffix
                file_path = TEMP_DIR / f"{stem}_{counter}{suffix}"
                counter += 1
            
            # Stream to file
            file_size = 0
            with open(file_path, 'wb') as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    if file_size + len(chunk) > MAX_FILE_SIZE:
                        raise ValueError("File size exceeds limit")
                    f.write(chunk)
                    file_size += len(chunk)
            
            # Track file with timestamp
            with tracker_lock:
                file_tracker[str(file_path)] = time.time()
            
            return file_path.name

def cleanup_old_files():
    """Delete files older than 30 minutes"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        with tracker_lock:
            to_delete = []
            for file_path, timestamp in file_tracker.items():
                if now - timestamp > FILE_LIFETIME:
                    to_delete.append(file_path)
            
            for path in to_delete:
                try:
                    os.unlink(path)
                    del file_tracker[path]
                except Exception as e:
                    print(f"Cleanup error: {str(e)}")

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate_link():
    try:
        data = request.get_json()
        if not data or 'file_url' not in data:
            return jsonify({"error": "Invalid request format"}), 400
        
        url = data['file_url'].strip()
        if not validate_url(url):
            return jsonify({"error": "Invalid URL format"}), 400
        
        try:
            filename = download_file(url)
            download_url = f"{request.host_url}download/{filename}"
            return jsonify({"download_url": download_url})
        except httpx.HTTPError as e:
            return jsonify({"error": f"Download failed: {str(e)}"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/download/<filename>')
def download_file_route(filename):
    try:
        # Prevent path traversal
        if '..' in filename or '/' in filename:
            raise ValueError("Invalid filename")
        
        file_path = TEMP_DIR / filename
        if not file_path.exists():
            raise FileNotFoundError("File not found")
        
        # Update access time
        with tracker_lock:
            if str(file_path) in file_tracker:
                file_tracker[str(file_path)] = time.time()
        
        return send_file(file_path, as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
