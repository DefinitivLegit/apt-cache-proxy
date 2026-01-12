import os
from pathlib import Path
from datetime import datetime
from flask import Blueprint, Response, request, render_template, jsonify, send_file
from utils.config import get_config, load_config, save_config_value
from services.stats import STATS, FILE_STATS, LOG_BUFFER, stats_lock, file_stats_lock, log_lock
from services.mirrors import get_all_mirrors, get_mirrors_management, update_mirror, delete_mirror, save_mirror_to_db
from services.cache_manager import clean_old_cache, delete_cached_file, get_blacklist_patterns, add_blacklist_pattern, remove_blacklist_pattern, manual_cache_package, search_upstream_packages

routes = Blueprint('routes', __name__)

def check_auth():
    """Check for admin token in Authorization header"""
    token = get_config('admin_token')
    if not token:
        # If no token configured, allow access (or deny? safer to deny if not configured but requested)
        # But user asked for auth.
        return True
        
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return False
        
    # Support "Bearer <token>" or just "<token>"
    if auth_header.startswith('Bearer '):
        received_token = auth_header.split(' ')[1]
    else:
        received_token = auth_header
        
    return received_token == token

@routes.route('/acng-report.html')
def acng_report():
    """Redirect legacy apt-cacher-ng report URL to dashboard"""
    return render_template('dashboard.html')

@routes.route('/health')
def health_check():
    return {'status': 'ok', 'cache_path': get_config('storage_path_resolved')}

@routes.route('/api/stats')
def api_stats():
    """Return cache statistics for dashboard"""
    uptime = datetime.now() - STATS['start_time']
    
    with stats_lock:
        response = {
            'requests_total': STATS['requests_total'],
            'cache_hits': STATS['cache_hits'],
            'cache_misses': STATS['cache_misses'],
            'bytes_served': STATS['bytes_served'],
            'uptime': str(uptime).split('.')[0],
        }
        
    with file_stats_lock:
        response['total_cached_files'] = FILE_STATS['total_files']
        response['total_cache_size_mb'] = round(FILE_STATS['total_size'] / (1024 * 1024), 2)
        response['distro_stats'] = FILE_STATS['distro_stats']
    
    # Only return approved mirrors for public stats
    response['mirrors'] = get_all_mirrors()
    
    with log_lock:
        response['logs'] = list(LOG_BUFFER)
        
    return response

@routes.route('/api/admin/config', methods=['GET'])
def api_get_config():
    """Get current configuration for admin panel"""
    if not check_auth():
        return Response("Unauthorized", status=401)
    return jsonify({
        'cache_days': get_config('cache_days', 7),
        'cache_retention_enabled': get_config('cache_retention_enabled', True)
    })

@routes.route('/api/admin/config', methods=['PUT'])
def api_update_config():
    """Update configuration"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    
    success = True
    
    if 'cache_days' in data:
        try:
            days = int(data['cache_days'])
            if days < 1:
                return Response("Cache days must be at least 1", status=400)
            if not save_config_value('cache_days', days):
                success = False
        except ValueError:
            return Response("Invalid value for cache_days", status=400)
            
    if 'cache_retention_enabled' in data:
        enabled = bool(data['cache_retention_enabled'])
        if not save_config_value('cache_retention_enabled', enabled):
            success = False
            
    if success:
        return jsonify({'status': 'success'})
    return Response("Failed to save config", status=500)

@routes.route('/api/admin/mirrors', methods=['GET'])
def api_admin_mirrors():
    """Get all mirrors with status for admin panel"""
    if not check_auth():
        return Response("Unauthorized", status=401)
    return jsonify(get_mirrors_management())

@routes.route('/api/admin/mirrors', methods=['POST'])
def api_add_mirror():
    """Add a new mirror"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    name = data.get('name')
    urls = data.get('urls')
    status = data.get('status', 'approved')
    
    if not name or not urls:
        return Response("Missing name or urls", status=400)
        
    if isinstance(urls, str):
        urls = [urls]
        
    if save_mirror_to_db(name, urls, status):
        return jsonify({'status': 'success'})
    return Response("Failed to add mirror (invalid URLs or self-reference)", status=400)

@routes.route('/api/admin/mirrors/<name>', methods=['PUT'])
def api_update_mirror(name):
    """Update mirror status or URLs"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    status = data.get('status')
    urls = data.get('urls')
    
    if update_mirror(name, urls=urls, status=status):
        return jsonify({'status': 'success'})
    return Response("Failed to update mirror", status=500)

@routes.route('/api/admin/mirrors/<name>', methods=['DELETE'])
def api_delete_mirror(name):
    """Delete a mirror"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    if delete_mirror(name):
        return jsonify({'status': 'success'})
    return Response("Failed to delete mirror", status=500)

@routes.route('/api/admin/cache', methods=['DELETE'])
def api_delete_cache_file():
    """Delete a specific file from cache"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    file_path = request.args.get('path')
    if not file_path:
        return Response("Missing path parameter", status=400)
        
    if delete_cached_file(file_path):
        return jsonify({'status': 'success'})
    return Response("Failed to delete file (not found or invalid path)", status=404)

@routes.route('/api/admin/cache/manual', methods=['POST'])
def api_manual_cache():
    """Manually download and cache a package"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    distro = data.get('distro')
    path = data.get('path')
    
    if not distro or not path:
        return Response("Missing distro or path", status=400)
        
    success, message = manual_cache_package(distro, path)
    if success:
        return jsonify({'status': 'success', 'message': message})
    return Response(f"Failed: {message}", status=500)

@routes.route('/api/admin/upstream/search', methods=['GET'])
def api_search_upstream():
    """Search for packages in upstream mirrors"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    distro = request.args.get('distro')
    query = request.args.get('q')
    
    if not distro or not query:
        return jsonify([])
        
    results = search_upstream_packages(distro, query)
    return jsonify(results)

@routes.route('/api/admin/blacklist', methods=['GET'])
def api_get_blacklist():
    """Get all blacklist patterns"""
    if not check_auth():
        return Response("Unauthorized", status=401)
    return jsonify(get_blacklist_patterns())

@routes.route('/api/admin/blacklist', methods=['POST'])
def api_add_blacklist():
    """Add a blacklist pattern"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    pattern = data.get('pattern')
    
    if not pattern:
        return Response("Missing pattern", status=400)
        
    if add_blacklist_pattern(pattern):
        return jsonify({'status': 'success'})
    return Response("Failed to add pattern", status=500)

@routes.route('/api/admin/blacklist', methods=['DELETE'])
def api_remove_blacklist():
    """Remove a blacklist pattern"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    pattern = request.args.get('pattern')
    if not pattern:
        return Response("Missing pattern", status=400)
        
    if remove_blacklist_pattern(pattern):
        return jsonify({'status': 'success'})
    return Response("Failed to remove pattern", status=500)

@routes.route('/api/cache/search')
def api_search_cache():
    """Search for cached files"""
    query = request.args.get('q', '').lower()
    if not query:
        return jsonify([])
        
    results = []
    storage_path_str = get_config('storage_path_resolved')
    if not storage_path_str:
        return jsonify([])
        
    storage_path = Path(storage_path_str)
    
    # Limit results to prevent overload
    limit = 100
    count = 0
    
    for root, dirs, files in os.walk(storage_path):
        for filename in files:
            # We store files as hash_filename, so we need to check the real filename part
            # Format: {hash}_{filename}
            parts = filename.split('_', 1)
            if len(parts) > 1:
                real_name = parts[1]
                if query in real_name.lower():
                    rel_path = os.path.relpath(os.path.join(root, filename), storage_path)
                    distro = rel_path.split(os.sep)[0]
                    
                    file_stat = os.stat(os.path.join(root, filename))
                    
                    # Determine last hit time (atime)
                    # Fallback to mtime if atime is older (which shouldn't happen if updated correctly)
                    last_hit = file_stat.st_atime
                    if file_stat.st_mtime > last_hit:
                        last_hit = file_stat.st_mtime
                    
                    results.append({
                        'name': real_name,
                        'distro': distro,
                        'size': file_stat.st_size,
                        'mtime': datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                        'atime': datetime.fromtimestamp(last_hit).strftime('%Y-%m-%d %H:%M:%S'),
                        'path': rel_path
                    })
                    count += 1
                    if count >= limit:
                        break
        if count >= limit:
            break
            
    return jsonify(results)

@routes.route('/api/cache/download')
def api_download_cache():
    """Download a specific cached file"""
    file_path = request.args.get('path')
    if not file_path:
        return Response("Missing path parameter", status=400)
    
    # Security check: prevent directory traversal
    if '..' in file_path or file_path.startswith('/'):
        return Response("Invalid path", status=400)
        
    storage_path_str = get_config('storage_path_resolved')
    if not storage_path_str:
        return Response("Storage not configured", status=500)
        
    full_path = Path(storage_path_str) / file_path
    
    if not full_path.exists():
        return Response("File not found", status=404)
        
    # Extract real filename from hash_filename
    filename = full_path.name
    parts = filename.split('_', 1)
    if len(parts) > 1:
        download_name = parts[1]
    else:
        download_name = filename
        
    return send_file(full_path, as_attachment=True, download_name=download_name)

@routes.route('/stats')
def stats():
    return api_stats()

@routes.route('/cleanup')
def manual_cleanup():
    if not check_auth():
        return Response("Unauthorized", status=401)
    clean_old_cache()
    return {'status': 'cleanup completed'}

@routes.route('/reload')
def reload_configuration():
    """Reload configuration from disk"""
    if not check_auth():
        return Response("Unauthorized", status=401)
    if load_config():
        return {'status': 'configuration reloaded'}
    return {'status': 'failed to reload configuration'}, 500

@routes.route('/')
def dashboard():
    return render_template('dashboard.html')

@routes.route('/admin')
def admin_panel():
    return render_template('admin.html')

@routes.route('/favicon.ico')
def favicon():
    return Response(status=204)
