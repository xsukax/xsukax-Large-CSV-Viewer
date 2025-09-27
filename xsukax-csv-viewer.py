#!/usr/bin/env python3
"""
xsukax Large CSV Viewer - Professional CSV File Viewer
"""

import os
import sys
import csv
import io
import time
import uuid
import re
import threading
import json
import platform
from pathlib import Path

# Fix CSV field size limit for different platforms
try:
    # Windows has a smaller max value for C long
    if platform.system() == 'Windows':
        csv.field_size_limit(2147483647)  # 2^31 - 1 (max 32-bit signed int)
    else:
        csv.field_size_limit(sys.maxsize)
except OverflowError:
    # Fallback to a reasonable large value
    csv.field_size_limit(10**9)  # 1 billion characters per field

try:
    from flask import Flask, render_template_string, request, jsonify, Response
    import psutil
except ImportError:
    print("=" * 60)
    print("Missing required packages!")
    print("Please install: pip install flask psutil")
    print("=" * 60)
    sys.exit(1)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 * 1024  # 50GB max
app.config['JSON_AS_ASCII'] = False

# Configuration
UPLOAD_DIR = Path('xsukax_uploads')
UPLOAD_DIR.mkdir(exist_ok=True)
CHUNK_SIZE = 65536  # 64KB chunks for reading
PROGRESS_UPDATE_INTERVAL = 0.5  # Progress update frequency

# Global storage
sessions = {}
current_file = None
upload_progress = {}

class FileInfo:
    """File information container"""
    def __init__(self):
        self.path = None
        self.name = None
        self.size = 0
        self.lines = 0
        self.columns = []
        self.delimiter = ','
        self.encoding = 'utf-8'
        self.type = None

class SearchSession:
    """Search session management"""
    def __init__(self, sid):
        self.id = sid
        self.term = ""
        self.active = True
        self.complete = False
        self.results = []
        self.searched_lines = 0
        self.total_matches = 0
        self.start_time = time.time()
        self.error = None
        self.last_update = 0

def detect_encoding(file_path, sample_size=65536):
    """Intelligently detect file encoding"""
    with open(file_path, 'rb') as f:
        raw = f.read(sample_size)
    
    # Try encodings in order of likelihood
    encodings = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
    
    for enc in encodings:
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    
    # Fallback to latin-1 (accepts any byte values)
    return 'latin-1'

def detect_delimiter(file_path, encoding):
    """Detect CSV delimiter"""
    with open(file_path, 'r', encoding=encoding, errors='replace') as f:
        sample = f.read(32768)
    
    # Try to detect using csv.Sniffer
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        return dialect.delimiter
    except:
        # Count occurrences of common delimiters
        delimiters = {',': sample.count(','), ';': sample.count(';'), 
                     '\t': sample.count('\t'), '|': sample.count('|')}
        return max(delimiters.items(), key=lambda x: x[1])[0]

def analyze_file(file_path, progress_key):
    """Comprehensive file analysis with progress tracking"""
    file_size = os.path.getsize(file_path)
    file_ext = file_path.lower().rsplit('.', 1)[-1]
    
    # Update progress
    upload_progress[progress_key] = {'status': 'Detecting encoding...', 'percent': 10}
    encoding = detect_encoding(file_path)
    
    result = {
        'encoding': encoding,
        'size': file_size,
        'type': file_ext
    }
    
    if file_ext == 'csv':
        # Detect delimiter
        upload_progress[progress_key] = {'status': 'Detecting delimiter...', 'percent': 20}
        delimiter = detect_delimiter(file_path, encoding)
        result['delimiter'] = delimiter
        
        # Get headers
        upload_progress[progress_key] = {'status': 'Reading headers...', 'percent': 30}
        with open(file_path, 'r', encoding=encoding, errors='replace') as f:
            try:
                reader = csv.reader(f, delimiter=delimiter)
                headers = next(reader, [])
                result['columns'] = [h.strip() for h in headers] if headers else ['Column 1']
            except:
                result['columns'] = ['Column 1']
    else:
        result['columns'] = ['Content']
        result['delimiter'] = None
    
    # Count lines with progress
    upload_progress[progress_key] = {'status': 'Counting lines...', 'percent': 40}
    lines = 0
    bytes_read = 0
    
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)  # Read 1MB at a time
            if not chunk:
                break
            lines += chunk.count(b'\n')
            bytes_read += len(chunk)
            
            # Update progress
            if file_size > 0:
                percent = 40 + int((bytes_read / file_size) * 50)
                upload_progress[progress_key] = {
                    'status': f'Counting lines: {lines:,}...',
                    'percent': min(percent, 90)
                }
    
    result['lines'] = lines + 1  # Add 1 for last line without newline
    
    upload_progress[progress_key] = {'status': 'Analysis complete!', 'percent': 100}
    return result

def search_csv(file_path, search_term, session, encoding, delimiter, columns):
    """Search CSV file without size limits"""
    search_lower = search_term.lower()
    
    try:
        with open(file_path, 'r', encoding=encoding, errors='replace', buffering=CHUNK_SIZE) as f:
            reader = csv.reader(f, delimiter=delimiter)
            
            # Skip header
            try:
                next(reader, None)
            except StopIteration:
                session.error = "Empty file"
                return
            
            for row_num, row in enumerate(reader, 2):
                if not session.active:
                    break
                
                try:
                    # Check for match in any field
                    has_match = False
                    for cell in row:
                        if search_lower in str(cell).lower():
                            has_match = True
                            break
                    
                    if has_match:
                        # Build complete result row
                        result = {'Row': row_num}
                        
                        # Include all columns with proper highlighting
                        for i, col_name in enumerate(columns):
                            if i < len(row):
                                cell_value = str(row[i])
                                
                                # Apply highlighting if matches
                                if search_lower in cell_value.lower():
                                    # Preserve case in highlighting
                                    pattern = re.compile(re.escape(search_term), re.IGNORECASE)
                                    highlighted = pattern.sub(
                                        lambda m: f'<mark>{m.group()}</mark>', 
                                        cell_value
                                    )
                                    result[col_name] = highlighted
                                else:
                                    result[col_name] = cell_value
                            else:
                                result[col_name] = ''
                        
                        session.results.append(result)
                        session.total_matches += 1
                    
                    session.searched_lines = row_num
                    
                    # Update progress periodically
                    current_time = time.time()
                    if current_time - session.last_update > PROGRESS_UPDATE_INTERVAL:
                        session.last_update = current_time
                        time.sleep(0.001)  # Yield to other threads
                        
                except Exception as e:
                    print(f"Error processing row {row_num}: {e}")
                    continue
                    
    except Exception as e:
        session.error = f"Search error: {str(e)}"
        print(f"CSV search error: {e}")
    finally:
        session.complete = True
        session.active = False

def search_txt(file_path, search_term, session, encoding):
    """Search text file without size limits"""
    search_lower = search_term.lower()
    
    try:
        with open(file_path, 'r', encoding=encoding, errors='replace', buffering=CHUNK_SIZE) as f:
            for line_num, line in enumerate(f, 1):
                if not session.active:
                    break
                
                line_text = line.rstrip('\n\r')
                
                if search_lower in line_text.lower():
                    # Apply highlighting
                    pattern = re.compile(re.escape(search_term), re.IGNORECASE)
                    highlighted = pattern.sub(
                        lambda m: f'<mark>{m.group()}</mark>',
                        line_text
                    )
                    
                    session.results.append({
                        'Row': line_num,
                        'Content': highlighted
                    })
                    session.total_matches += 1
                
                session.searched_lines = line_num
                
                # Update progress periodically
                current_time = time.time()
                if current_time - session.last_update > PROGRESS_UPDATE_INTERVAL:
                    session.last_update = current_time
                    time.sleep(0.001)
                    
    except Exception as e:
        session.error = f"Search error: {str(e)}"
    finally:
        session.complete = True
        session.active = False

@app.route('/')
def index():
    """Main page"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload', methods=['POST'])
def upload():
    """Handle file upload"""
    global current_file
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    
    # Save file with timestamp
    timestamp = int(time.time() * 1000)
    safe_name = re.sub(r'[^\w\-_\.]', '_', file.filename)
    file_path = UPLOAD_DIR / f"{timestamp}_{safe_name}"
    
    # Create progress key
    progress_key = str(uuid.uuid4())
    upload_progress[progress_key] = {'status': 'Saving file...', 'percent': 5}
    
    # Save file
    file.save(str(file_path))
    
    # Analyze file
    analysis = analyze_file(str(file_path), progress_key)
    
    # Store file info
    current_file = FileInfo()
    current_file.path = str(file_path)
    current_file.name = file.filename
    current_file.size = analysis['size']
    current_file.lines = analysis['lines']
    current_file.columns = analysis['columns']
    current_file.encoding = analysis['encoding']
    current_file.delimiter = analysis.get('delimiter', ',')
    current_file.type = analysis['type']
    
    # Clean up progress after delay
    threading.Timer(5.0, lambda: upload_progress.pop(progress_key, None)).start()
    
    return jsonify({
        'success': True,
        'progress_key': progress_key,
        'file': {
            'name': current_file.name,
            'type': current_file.type.upper(),
            'size_mb': round(current_file.size / (1024 * 1024), 2),
            'lines': current_file.lines,
            'columns': len(current_file.columns),
            'column_names': current_file.columns[:10]  # First 10 columns for preview
        }
    })

@app.route('/progress/<key>')
def get_progress(key):
    """Get upload progress"""
    if key in upload_progress:
        return jsonify(upload_progress[key])
    return jsonify({'status': 'Complete', 'percent': 100})

@app.route('/preview')
def preview():
    """Get file preview"""
    if not current_file:
        return jsonify({'error': 'No file loaded'}), 400
    
    rows = []
    limit = min(200, int(request.args.get('limit', 100)))
    
    try:
        if current_file.type == 'csv':
            with open(current_file.path, 'r', encoding=current_file.encoding, errors='replace') as f:
                reader = csv.reader(f, delimiter=current_file.delimiter)
                next(reader, None)  # Skip header
                
                for i, row in enumerate(reader, 2):
                    if len(rows) >= limit:
                        break
                    
                    result = {'Row': i}
                    for j, col in enumerate(current_file.columns):
                        result[col] = row[j] if j < len(row) else ''
                    rows.append(result)
        else:
            with open(current_file.path, 'r', encoding=current_file.encoding, errors='replace') as f:
                for i, line in enumerate(f, 1):
                    if len(rows) >= limit:
                        break
                    rows.append({
                        'Row': i,
                        'Content': line.rstrip('\n\r')
                    })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
    columns = ['Row'] + current_file.columns
    return jsonify({'columns': columns, 'rows': rows, 'total': len(rows)})

@app.route('/search', methods=['POST'])
def search():
    """Start search"""
    if not current_file:
        return jsonify({'error': 'No file loaded'}), 400
    
    data = request.get_json()
    search_term = data.get('term', '').strip()
    
    if not search_term:
        return jsonify({'error': 'Please enter a search term'}), 400
    
    # Create search session
    sid = str(uuid.uuid4())
    session = SearchSession(sid)
    session.term = search_term
    sessions[sid] = session
    
    # Start search in background
    if current_file.type == 'csv':
        thread = threading.Thread(
            target=search_csv,
            args=(current_file.path, search_term, session, 
                  current_file.encoding, current_file.delimiter, current_file.columns),
            daemon=True
        )
    else:
        thread = threading.Thread(
            target=search_txt,
            args=(current_file.path, search_term, session, current_file.encoding),
            daemon=True
        )
    
    thread.start()
    
    return jsonify({'session_id': sid, 'message': f'Searching for "{search_term}"...'})

@app.route('/status/<sid>')
def status(sid):
    """Get search status"""
    session = sessions.get(sid)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    
    elapsed = round(time.time() - session.start_time, 1)
    
    return jsonify({
        'active': session.active,
        'complete': session.complete,
        'searched_lines': session.searched_lines,
        'total_matches': session.total_matches,
        'elapsed_time': elapsed,
        'error': session.error,
        'has_results': len(session.results) > 0
    })

@app.route('/results/<sid>')
def results(sid):
    """Get search results"""
    session = sessions.get(sid)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    
    page = max(1, int(request.args.get('page', 1)))
    size = min(500, int(request.args.get('size', 100)))
    
    start = (page - 1) * size
    end = start + size
    page_results = session.results[start:end]
    
    total_results = len(session.results)
    total_pages = (total_results + size - 1) // size
    
    # Get columns
    if page_results:
        columns = list(page_results[0].keys())
    elif current_file:
        columns = ['Row'] + current_file.columns
    else:
        columns = []
    
    return jsonify({
        'columns': columns,
        'rows': page_results,
        'page': page,
        'total_pages': total_pages,
        'total_results': total_results,
        'showing_from': start + 1 if page_results else 0,
        'showing_to': min(end, total_results)
    })

@app.route('/stop/<sid>', methods=['POST'])
def stop(sid):
    """Stop search"""
    session = sessions.get(sid)
    if session and session.active:
        session.active = False
        return jsonify({'success': True, 'message': 'Search stopped'})
    return jsonify({'success': False, 'message': 'Search not active'})

@app.route('/export/<sid>')
def export(sid):
    """Export results as CSV"""
    session = sessions.get(sid)
    if not session or not session.results:
        return jsonify({'error': 'No results to export'}), 400
    
    # Build CSV
    output = io.StringIO()
    
    if session.results:
        columns = list(session.results[0].keys())
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        
        # Remove HTML tags
        tag_remover = re.compile(r'<[^>]+>')
        for row in session.results:
            clean_row = {}
            for key, value in row.items():
                clean_row[key] = tag_remover.sub('', str(value))
            writer.writerow(clean_row)
    
    output.seek(0)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    filename = f"xsukax_search_{session.term[:20]}_{timestamp}.csv"
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )

@app.route('/info')
def info():
    """Get system info"""
    try:
        process = psutil.Process()
        memory_mb = process.memory_info().rss / (1024 * 1024)
    except:
        memory_mb = 0
    
    return jsonify({
        'memory_mb': round(memory_mb, 2),
        'sessions': len(sessions),
        'platform': platform.system()
    })

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>xsukax Large CSV Viewer</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
.header { background: rgba(255,255,255,0.95); padding: 20px; text-align: center; box-shadow: 0 2px 20px rgba(0,0,0,0.1); }
.logo { font-size: 28px; font-weight: 700; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.subtitle { color: #6b7280; font-size: 14px; margin-top: 5px; }
.container { max-width: 1400px; margin: 0 auto; padding: 20px; }
.card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); }
.upload-zone { border: 3px dashed #667eea; border-radius: 12px; padding: 60px 20px; text-align: center; transition: all 0.3s; cursor: pointer; background: #f9fafb; }
.upload-zone:hover { background: #f3f4f6; border-color: #764ba2; transform: translateY(-2px); }
.upload-zone.active { background: #ede9fe; border-color: #764ba2; }
.upload-icon { font-size: 48px; margin-bottom: 10px; }
.btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 24px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.3s; font-size: 14px; }
.btn:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(102,126,234,0.4); }
.btn:disabled { background: #9ca3af; cursor: not-allowed; transform: none; }
.btn-secondary { background: #6b7280; }
.btn-secondary:hover { background: #4b5563; box-shadow: 0 4px 12px rgba(107,114,128,0.4); }
.btn-danger { background: #ef4444; }
.btn-danger:hover { background: #dc2626; box-shadow: 0 4px 12px rgba(239,68,68,0.4); }
.btn-success { background: #10b981; }
.btn-success:hover { background: #059669; box-shadow: 0 4px 12px rgba(16,185,129,0.4); }
.info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 20px; margin: 20px 0; }
.info-box { background: #f9fafb; padding: 15px; border-radius: 8px; text-align: center; }
.info-label { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 1px; }
.info-value { font-size: 24px; font-weight: 700; color: #1f2937; margin-top: 5px; }
.search-bar { display: flex; gap: 10px; margin-bottom: 20px; }
.search-input { flex: 1; padding: 12px 16px; border: 2px solid #e5e7eb; border-radius: 8px; font-size: 16px; transition: all 0.3s; }
.search-input:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
.status-bar { background: #f3f4f6; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
.status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 15px; text-align: center; }
.progress-bar { height: 6px; background: #e5e7eb; border-radius: 3px; overflow: hidden; margin-top: 10px; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #667eea, #764ba2); transition: width 0.3s; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.8; } }
.table-wrapper { overflow: auto; max-height: 60vh; border: 1px solid #e5e7eb; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { background: #f9fafb; padding: 12px; text-align: left; font-weight: 600; position: sticky; top: 0; z-index: 10; border-bottom: 2px solid #e5e7eb; color: #374151; }
td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; color: #1f2937; }
tr:hover { background: #f9fafb; }
mark { background: #fef08a; padding: 2px 4px; border-radius: 3px; font-weight: 600; color: #713f12; }
.pagination { display: flex; justify-content: center; align-items: center; gap: 5px; padding: 20px; }
.page-btn { padding: 8px 12px; border: 1px solid #e5e7eb; background: white; border-radius: 6px; cursor: pointer; transition: all 0.2s; }
.page-btn:hover { background: #f3f4f6; }
.page-btn.active { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; }
.hidden { display: none !important; }
.alert { padding: 12px 16px; border-radius: 8px; margin: 10px 0; font-weight: 500; }
.alert-success { background: #d1fae5; color: #065f46; border: 1px solid #a7f3d0; }
.alert-error { background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
.alert-info { background: #dbeafe; color: #1e40af; border: 1px solid #bfdbfe; }
.footer { text-align: center; padding: 20px; color: white; }
.stats { color: #6b7280; font-size: 14px; margin: 10px 0; text-align: center; }
</style>
</head>
<body>
<div class="header">
    <div class="logo">xsukax Large CSV Viewer</div>
    <div class="subtitle">Professional Large CSV File Search Tool</div>
</div>

<div class="container">
    <div class="card">
        <div class="upload-zone" id="uploadZone">
            <div class="upload-icon">📂</div>
            <div style="font-size: 18px; font-weight: 600; color: #374151; margin-bottom: 10px;">
                Drop your CSV/TXT file here
            </div>
            <div style="color: #6b7280; margin-bottom: 15px;">or click to browse</div>
            <button class="btn">Select File</button>
        </div>
        <input type="file" id="fileInput" class="hidden" accept=".csv,.txt">
        
        <div id="fileInfo" class="info-grid hidden"></div>
    </div>
    
    <div id="alerts"></div>
    
    <div id="searchCard" class="card hidden">
        <div class="search-bar">
            <input type="text" id="searchInput" class="search-input" placeholder="Enter search term..." autocomplete="off">
            <button id="searchBtn" class="btn" onclick="startSearch()">🔍 Search</button>
            <button id="stopBtn" class="btn btn-danger hidden" onclick="stopSearch()">⏹ Stop</button>
            <button class="btn btn-secondary" onclick="showPreview()">👁 Preview</button>
        </div>
        
        <div id="statusBar" class="status-bar hidden">
            <div class="status-grid">
                <div>
                    <div class="info-label">Lines Searched</div>
                    <div class="info-value" id="linesSearched">0</div>
                </div>
                <div>
                    <div class="info-label">Matches Found</div>
                    <div class="info-value" id="matchesFound">0</div>
                </div>
                <div>
                    <div class="info-label">Time</div>
                    <div class="info-value" id="searchTime">0s</div>
                </div>
                <div>
                    <div class="info-label">Status</div>
                    <div class="info-value" id="searchStatus">Ready</div>
                </div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" id="searchProgress" style="width: 0%"></div>
            </div>
        </div>
        
        <button id="exportBtn" class="btn btn-success hidden" onclick="exportResults()">📥 Export Results</button>
    </div>
    
    <div id="resultsCard" class="card hidden">
        <div class="stats" id="resultsStats"></div>
        <div class="table-wrapper">
            <table>
                <thead id="tableHead"></thead>
                <tbody id="tableBody"></tbody>
            </table>
        </div>
        <div id="pagination" class="pagination"></div>
    </div>
</div>

<div class="footer">
    <div id="systemInfo" style="opacity: 0.8;"></div>
</div>

<script>
let currentSession = null;
let monitorInterval = null;
let fileData = null;
let totalLines = 0;

// File upload handling
const fileInput = document.getElementById('fileInput');
const uploadZone = document.getElementById('uploadZone');

uploadZone.onclick = () => fileInput.click();
uploadZone.ondragover = e => { e.preventDefault(); uploadZone.classList.add('active'); };
uploadZone.ondragleave = () => uploadZone.classList.remove('active');
uploadZone.ondrop = e => {
    e.preventDefault();
    uploadZone.classList.remove('active');
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
};

fileInput.onchange = e => { if (e.target.files[0]) handleFile(e.target.files[0]); };

function handleFile(file) {
    const formData = new FormData();
    formData.append('file', file);
    
    // Show upload progress
    showUploadProgress();
    
    fetch('/upload', { method: 'POST', body: formData })
        .then(r => r.json())
        .then(data => {
            if (data.error) throw new Error(data.error);
            
            fileData = data.file;
            totalLines = data.file.lines;
            
            // Monitor progress
            if (data.progress_key) {
                monitorUploadProgress(data.progress_key);
            }
            
            // Display file info
            displayFileInfo(data.file);
            
            // Show search card
            document.getElementById('searchCard').classList.remove('hidden');
            showAlert('File loaded successfully!', 'success');
        })
        .catch(err => {
            resetUploadZone();
            showAlert(err.message, 'error');
        });
}

function showUploadProgress() {
    uploadZone.innerHTML = `
        <div style="width: 100%; max-width: 400px; margin: 0 auto;">
            <div style="font-size: 18px; font-weight: 600; margin-bottom: 15px;">Analyzing File...</div>
            <div class="progress-bar" style="height: 8px;">
                <div class="progress-fill" id="uploadProgress" style="width: 0%"></div>
            </div>
            <div id="uploadStatus" style="margin-top: 10px; color: #6b7280;"></div>
        </div>
    `;
}

function resetUploadZone() {
    uploadZone.innerHTML = `
        <div class="upload-icon">📂</div>
        <div style="font-size: 18px; font-weight: 600; color: #374151; margin-bottom: 10px;">
            Drop your CSV/TXT file here
        </div>
        <div style="color: #6b7280; margin-bottom: 15px;">or click to browse</div>
        <button class="btn">Select File</button>
    `;
}

function monitorUploadProgress(key) {
    const interval = setInterval(() => {
        fetch(`/progress/${key}`)
            .then(r => r.json())
            .then(data => {
                const progress = document.getElementById('uploadProgress');
                const status = document.getElementById('uploadStatus');
                
                if (progress) progress.style.width = data.percent + '%';
                if (status) status.innerText = data.status;
                
                if (data.percent >= 100) {
                    clearInterval(interval);
                    setTimeout(resetUploadZone, 1000);
                }
            })
            .catch(() => clearInterval(interval));
    }, 200);
}

function displayFileInfo(file) {
    const info = document.getElementById('fileInfo');
    info.innerHTML = `
        <div class="info-box">
            <div class="info-label">File Type</div>
            <div class="info-value">${file.type}</div>
        </div>
        <div class="info-box">
            <div class="info-label">Size</div>
            <div class="info-value">${file.size_mb} MB</div>
        </div>
        <div class="info-box">
            <div class="info-label">Total Lines</div>
            <div class="info-value">${file.lines.toLocaleString()}</div>
        </div>
        <div class="info-box">
            <div class="info-label">Columns</div>
            <div class="info-value">${file.columns}</div>
        </div>
    `;
    info.classList.remove('hidden');
}

function startSearch() {
    const term = document.getElementById('searchInput').value.trim();
    if (!term) return showAlert('Please enter a search term', 'error');
    
    // Reset UI
    clearResults();
    document.getElementById('searchBtn').disabled = true;
    document.getElementById('stopBtn').classList.remove('hidden');
    document.getElementById('statusBar').classList.remove('hidden');
    document.getElementById('searchStatus').innerText = 'Searching...';
    
    fetch('/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({term})
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) throw new Error(data.error);
        currentSession = data.session_id;
        monitorSearch();
    })
    .catch(err => {
        resetSearchUI();
        showAlert(err.message, 'error');
    });
}

function monitorSearch() {
    monitorInterval = setInterval(() => {
        if (!currentSession) return;
        
        fetch(`/status/${currentSession}`)
            .then(r => r.json())
            .then(data => {
                // Update status
                document.getElementById('linesSearched').innerText = data.searched_lines.toLocaleString();
                document.getElementById('matchesFound').innerText = data.total_matches.toLocaleString();
                document.getElementById('searchTime').innerText = data.elapsed_time + 's';
                
                // Progress bar
                if (totalLines > 0) {
                    const percent = (data.searched_lines / totalLines) * 100;
                    document.getElementById('searchProgress').style.width = Math.min(percent, 100) + '%';
                }
                
                // Load results if available
                if (data.has_results) {
                    loadResults();
                }
                
                // Check completion
                if (data.complete || data.error) {
                    clearInterval(monitorInterval);
                    resetSearchUI();
                    
                    if (data.error) {
                        showAlert(data.error, 'error');
                        document.getElementById('searchStatus').innerText = 'Error';
                    } else {
                        document.getElementById('searchStatus').innerText = 'Complete';
                        if (data.total_matches > 0) {
                            document.getElementById('exportBtn').classList.remove('hidden');
                            showAlert(`Found ${data.total_matches.toLocaleString()} matches!`, 'success');
                        } else {
                            showAlert('No matches found', 'info');
                        }
                    }
                }
            })
            .catch(err => console.error(err));
    }, 500);
}

function stopSearch() {
    if (currentSession) {
        fetch(`/stop/${currentSession}`, {method: 'POST'})
            .then(r => r.json())
            .then(data => showAlert(data.message, 'info'));
        
        clearInterval(monitorInterval);
        resetSearchUI();
        document.getElementById('searchStatus').innerText = 'Stopped';
    }
}

function resetSearchUI() {
    document.getElementById('searchBtn').disabled = false;
    document.getElementById('stopBtn').classList.add('hidden');
    clearInterval(monitorInterval);
}

function loadResults(page = 1) {
    if (!currentSession) return;
    
    fetch(`/results/${currentSession}?page=${page}&size=100`)
        .then(r => r.json())
        .then(data => {
            displayTable(data.columns, data.rows);
            displayPagination(data);
            
            // Update stats
            const stats = document.getElementById('resultsStats');
            stats.innerText = `Showing ${data.showing_from}-${data.showing_to} of ${data.total_results.toLocaleString()} results`;
            
            document.getElementById('resultsCard').classList.remove('hidden');
        })
        .catch(err => console.error(err));
}

function showPreview() {
    if (!fileData) return showAlert('No file loaded', 'error');
    
    clearResults();
    
    fetch('/preview?limit=100')
        .then(r => r.json())
        .then(data => {
            displayTable(data.columns, data.rows);
            document.getElementById('resultsStats').innerText = `Preview: First ${data.total} rows`;
            document.getElementById('resultsCard').classList.remove('hidden');
            document.getElementById('pagination').innerHTML = '';
        })
        .catch(err => showAlert(err.message, 'error'));
}

function displayTable(columns, rows) {
    const head = document.getElementById('tableHead');
    const body = document.getElementById('tableBody');
    
    head.innerHTML = '<tr>' + columns.map(c => `<th>${c}</th>`).join('') + '</tr>';
    body.innerHTML = rows.map(row => 
        '<tr>' + columns.map(c => `<td>${row[c] !== undefined ? row[c] : ''}</td>`).join('') + '</tr>'
    ).join('');
}

function displayPagination(data) {
    const pag = document.getElementById('pagination');
    if (data.total_pages <= 1) return pag.innerHTML = '';
    
    let html = [];
    
    // Previous button
    if (data.page > 1) {
        html.push(`<button class="page-btn" onclick="loadResults(${data.page - 1})">←</button>`);
    }
    
    // Page numbers
    const start = Math.max(1, data.page - 2);
    const end = Math.min(data.total_pages, data.page + 2);
    
    if (start > 1) {
        html.push(`<button class="page-btn" onclick="loadResults(1)">1</button>`);
        if (start > 2) html.push(`<span style="padding: 0 5px;">...</span>`);
    }
    
    for (let i = start; i <= end; i++) {
        html.push(`<button class="page-btn ${i === data.page ? 'active' : ''}" onclick="loadResults(${i})">${i}</button>`);
    }
    
    if (end < data.total_pages) {
        if (end < data.total_pages - 1) html.push(`<span style="padding: 0 5px;">...</span>`);
        html.push(`<button class="page-btn" onclick="loadResults(${data.total_pages})">${data.total_pages}</button>`);
    }
    
    // Next button
    if (data.page < data.total_pages) {
        html.push(`<button class="page-btn" onclick="loadResults(${data.page + 1})">→</button>`);
    }
    
    pag.innerHTML = html.join('');
}

function clearResults() {
    document.getElementById('tableHead').innerHTML = '';
    document.getElementById('tableBody').innerHTML = '';
    document.getElementById('pagination').innerHTML = '';
    document.getElementById('resultsStats').innerText = '';
}

function exportResults() {
    if (!currentSession) return;
    
    const link = document.createElement('a');
    link.href = `/export/${currentSession}`;
    link.download = 'results.csv';
    link.click();
    
    showAlert('Exporting results...', 'success');
}

function showAlert(message, type) {
    const alerts = document.getElementById('alerts');
    const alert = document.createElement('div');
    alert.className = `alert alert-${type}`;
    alert.innerText = message;
    alerts.appendChild(alert);
    
    setTimeout(() => alert.remove(), 5000);
}

// System info
function updateSystemInfo() {
    fetch('/info')
        .then(r => r.json())
        .then(data => {
            document.getElementById('systemInfo').innerText = 
                `Memory: ${data.memory_mb} MB • Platform: ${data.platform}`;
        });
}

// Keyboard shortcuts
document.getElementById('searchInput').addEventListener('keypress', e => {
    if (e.key === 'Enter') startSearch();
});

// Update system info periodically
updateSystemInfo();
setInterval(updateSystemInfo, 5000);
</script>
</body>
</html>
'''

if __name__ == '__main__':
    print("=" * 60)
    print("xsukax Large CSV Viewer")
    print("=" * 60)
    print(f"Server: http://localhost:5000")
    print("Press Ctrl+C to stop")
    print("=" * 60)
    
    try:
        app.run(host='127.0.0.1', port=5000, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[Server stopped]")
    except Exception as e:
        print(f"\nError: {e}")
        print("Please check if port 5000 is available")