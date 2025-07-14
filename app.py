# app.py - Complete Flask File Download Generator (Single File for Render)
import os
import re
import time
import threading
import hashlib
import shutil
import atexit
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
import httpx

app = Flask(__name__)

# ======================
# CONFIGURATION
# ======================
BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp_downloads"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
CLEANUP_INTERVAL = 60  # Seconds (check every minute)
FILE_LIFETIME = 1800  # 30 minutes in seconds
PORT = int(os.environ.get('PORT', 5000))

# Create temp directory if not exists
TEMP_DIR.mkdir(exist_ok=True)

# Thread-safe file tracking
file_tracker = {}
tracker_lock = threading.Lock()

# ======================
# UTILITY FUNCTIONS
# ======================
def validate_url(url):
    """Validate HTTP/HTTPS URL format using regex"""
    return re.match(r'^https?://[^\s/$.?#].[^\s]*$', url, re.IGNORECASE) is not None

def sanitize_filename(filename):
    """Sanitize filename and hash if too long"""
    # Remove unsafe characters
    safe_name = re.sub(r'[^\w\.-]', '_', filename)
    
    # Shorten if too long
    if len(safe_name) > 100:
        prefix = safe_name[:50]
        suffix = safe_name[-10:] if len(safe_name) > 10 else safe_name
        hash_str = hashlib.md5(safe_name.encode()).hexdigest()[:8]
        safe_name = f"{prefix}_{hash_str}_{suffix}"
    
    return safe_name

def download_file(url):
    """Download file with streaming support"""
    with httpx.Client(timeout=30.0) as client:
        with client.stream('GET', url) as response:
            response.raise_for_status()
            
            # Get filename from URL or headers
            filename = url.split('/')[-1].split('?')[0]  # Remove query params
            if 'content-disposition' in response.headers:
                match = re.search(r'filename\*?=["\']?(?:UTF-\d[\'"]*)?([^;\r\n"\'\)]*)', 
                                response.headers['content-disposition'], 
                                flags=re.IGNORECASE)
                if not match:
                    match = re.search(r'filename=["\']?([^"\'\n\r;]*)', 
                                     response.headers['content-disposition'],
                                     flags=re.IGNORECASE)
                if match:
                    filename = match.group(1).strip()
            
            # Sanitize filename
            filename = sanitize_filename(filename)
            if not filename:
                filename = "download_" + hashlib.md5(url.encode()).hexdigest()[:8]
            
            file_path = TEMP_DIR / filename
            
            # Ensure unique filename
            counter = 1
            while file_path.exists():
                stem = file_path.stem
                suffix = file_path.suffix
                file_path = TEMP_DIR / f"{stem}_{counter}{suffix}"
                counter += 1
            
            # Stream to file with size limit
            file_size = 0
            with open(file_path, 'wb') as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    if file_size + len(chunk) > MAX_FILE_SIZE:
                        raise ValueError("File size exceeds 500MB limit")
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
                    if os.path.exists(path):
                        os.unlink(path)
                    del file_tracker[path]
                except Exception as e:
                    print(f"Cleanup error: {str(e)}")

def clean_temp_folder():
    """Empty temp folder on exit"""
    try:
        for filename in os.listdir(TEMP_DIR):
            file_path = TEMP_DIR / filename
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}: {e}")
        print("Temp folder cleaned")
    except Exception as e:
        print(f"Error cleaning temp folder: {str(e)}")

# ======================
# BACKGROUND THREAD SETUP
# ======================
cleanup_thread = threading.Thread(target=cleanup_old_files)
cleanup_thread.daemon = True  # Daemonize thread
cleanup_thread.start()

# Register cleanup on exit
atexit.register(clean_temp_folder)

# ======================
# FLASK ROUTES
# ======================
@app.route('/')
def index():
    """Serve the frontend interface"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/generate', methods=['POST'])
def generate_link():
    """Generate download link endpoint"""
    try:
        data = request.get_json()
        if not data or 'file_url' not in data:
            return jsonify({"error": "Invalid request format"}), 400
        
        url = data['file_url'].strip()
        if not url:
            return jsonify({"error": "URL cannot be empty"}), 400
            
        if not validate_url(url):
            return jsonify({"error": "Invalid URL format - must start with http:// or https://"}), 400
        
        try:
            filename = download_file(url)
            download_url = f"{request.host_url}download/{filename}"
            return jsonify({
                "download_url": download_url,
                "filename": filename
            })
        except httpx.HTTPError as e:
            return jsonify({"error": f"Download failed: {str(e)}"}), 400
        except httpx.TimeoutException:
            return jsonify({"error": "Download timed out (30s)"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Processing error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/download/<filename>')
def download_file_route(filename):
    """Serve file for download"""
    try:
        # Prevent path traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            raise ValueError("Invalid filename")
        
        file_path = TEMP_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError("File not found or expired")
        
        # Update access time
        with tracker_lock:
            file_tracker[str(file_path)] = time.time()
        
        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='application/octet-stream'
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 404

# ======================
# HTML TEMPLATE (Embedded)
# ======================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>File Download Generator</title>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Poppins', sans-serif;
            background: linear-gradient(135deg, #6a11cb 0%, #2575fc 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .container {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 16px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.15);
            width: 100%;
            max-width: 500px;
            padding: 40px;
            text-align: center;
            backdrop-filter: blur(10px);
        }
        h1 { color: #2c3e50; margin-bottom: 10px; font-size: 28px; }
        .description { color: #7f8c8d; margin-bottom: 30px; font-size: 16px; }
        .input-group { margin-bottom: 25px; }
        input {
            width: 100%;
            padding: 14px 20px;
            border: 2px solid #e0e0e0;
            border-radius: 50px;
            font-size: 16px;
            transition: all 0.3s;
            outline: none;
        }
        input:focus { border-color: #3498db; box-shadow: 0 0 0 3px rgba(52, 152, 219, 0.2); }
        button {
            background: linear-gradient(to right, #3498db, #2c3e50);
            color: white;
            border: none;
            border-radius: 50px;
            padding: 14px 30px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            width: 100%;
            box-shadow: 0 4px 15px rgba(52, 152, 219, 0.3);
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(52, 152, 219, 0.4); }
        button:disabled { background: #bdc3c7; cursor: not-allowed; transform: none; box-shadow: none; }
        .result { margin-top: 30px; padding: 20px; border-radius: 12px; background: #f8f9fa; display: none; }
        .download-link {
            display: inline-block;
            margin-top: 15px;
            padding: 12px 25px;
            background: #2ecc71;
            color: white;
            border-radius: 50px;
            text-decoration: none;
            font-weight: 500;
            transition: all 0.3s;
            word-break: break-all;
        }
        .download-link:hover {
            background: #27ae60;
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(46, 204, 113, 0.3);
        }
        .error {
            color: #e74c3c;
            margin-top: 20px;
            padding: 15px;
            background: #fdeded;
            border-radius: 8px;
            display: none;
        }
        .loading { margin: 25px 0; display: none; }
        .spinner {
            border: 4px solid rgba(0, 0, 0, 0.1);
            border-left-color: #3498db;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto;
        }
        .filename {
            display: block;
            margin-top: 10px;
            color: #7f8c8d;
            font-size: 14px;
            word-break: break-all;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        @media (max-width: 600px) {
            .container { padding: 30px 20px; }
            h1 { font-size: 24px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>File Download Generator</h1>
        <p class="description">Convert any direct file link into a downloadable URL</p>
        
        <div class="input-group">
            <input 
                type="url" 
                id="fileUrl" 
                placeholder="Paste file URL here (http/https)" 
                required
                autocomplete="off"
            >
        </div>
        
        <button id="generateBtn">Generate Download Link</button>
        
        <div class="loading" id="loading">
            <div class="spinner"></div>
            <p>Processing your file...</p>
        </div>
        
        <div class="error" id="error"></div>
        
        <div class="result" id="result">
            <h3>Your file is ready!</h3>
            <p>Link expires in 30 minutes</p>
            <a href="#" id="downloadLink" class="download-link">Download File</a>
            <span id="filenameDisplay" class="filename"></span>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', () => {
            const fileUrl = document.getElementById('fileUrl');
            const generateBtn = document.getElementById('generateBtn');
            const loading = document.getElementById('loading');
            const error = document.getElementById('error');
            const result = document.getElementById('result');
            const downloadLink = document.getElementById('downloadLink');
            const filenameDisplay = document.getElementById('filenameDisplay');
            
            generateBtn.addEventListener('click', async () => {
                const url = fileUrl.value.trim();
                error.style.display = 'none';
                result.style.display = 'none';
                
                if (!url) {
                    showError('Please enter a file URL');
                    return;
                }
                
                if (!url.startsWith('http://') && !url.startsWith('https://')) {
                    showError('URL must start with http:// or https://');
                    return;
                }
                
                try {
                    loading.style.display = 'block';
                    generateBtn.disabled = true;
                    
                    const response = await fetch('/generate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ file_url: url })
                    });
                    
                    const data = await response.json();
                    
                    if (!response.ok) {
                        throw new Error(data.error || 'Server error');
                    }
                    
                    downloadLink.href = data.download_url;
                    downloadLink.textContent = 'Download File';
                    filenameDisplay.textContent = data.filename;
                    result.style.display = 'block';
                    
                } catch (err) {
                    showError(err.message);
                } finally {
                    loading.style.display = 'none';
                    generateBtn.disabled = false;
                }
            });
            
            function showError(message) {
                error.textContent = message;
                error.style.display = 'block';
            }
        });
    </script>
</body>
</html>
'''

# ======================
# START APPLICATION
# ======================
if __name__ == '__main__':
    print(f"Starting server on port {PORT}")
    print(f"Temp directory: {TEMP_DIR}")
    app.run(host='0.0.0.0', port=PORT)
