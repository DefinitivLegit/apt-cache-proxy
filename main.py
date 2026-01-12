import threading
import time
from urllib.parse import urlparse
from flask import Flask, Response, request
from utils.logger import logger
from utils.routes import routes
from utils.config import get_config, load_config
from services.database import init_db
from services.stats import load_stats_from_db, save_stats_to_db, update_file_stats, add_log
from services.mirrors import load_mirrors_from_db, save_mirror_to_db, get_all_mirrors, get_upstream_key
from services.cache_manager import clean_old_cache, load_blacklist_from_db
from services.proxy import handle_connect, proxy_package_logic, direct_proxy


app = Flask(__name__)
app.register_blueprint(routes)

def background_tasks():
    """Background thread to clean cache and save stats periodically"""
    last_cleanup = 0
    last_save = 0
    last_file_scan = 0
    
    # Wait for config to be loaded
    while not get_config('storage_path_resolved'):
        time.sleep(1)

    # Initial scan
    try:
        update_file_stats()
    except Exception as e:
        logger.error(f"Initial file stats update failed: {e}")
    last_file_scan = time.time()

    while True:
        try:
            current_time = time.time()
            
            # Save stats every minute
            if current_time - last_save > 60:
                save_stats_to_db()
                last_save = current_time
                
            # Update file stats every 5 minutes (expensive operation)
            if current_time - last_file_scan > 300:
                update_file_stats()
                last_file_scan = current_time

            # Clean cache every hour
            if current_time - last_cleanup > 3600:
                clean_old_cache()
                last_cleanup = current_time
        except Exception as e:
            logger.error(f"Error in background tasks: {e}")
            
        time.sleep(10)

# Catch-all route MUST be last
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'CONNECT', 'HEAD'])
@app.route('/<path:path>', methods=['GET', 'POST', 'CONNECT', 'HEAD'])
def handle_all(path):
    """Central handler for all requests to support proxying"""
    
    # Handle CONNECT (HTTPS Tunneling attempt)
    if request.method == 'CONNECT':
        return handle_connect(path)

    # Parse the request to see if it's a distro request
    target_url = request.url
    
    # Try to split path into distro and package_path
    # Clean up path if it starts with http/https (proxy request)
    clean_path = path
    if path.startswith('http://') or path.startswith('https://'):
        # Strip scheme and host
        try:
            clean_path = '/' + '/'.join(path.split('/')[3:])
        except:
            pass
            
    parts = clean_path.strip('/').split('/', 1)
    if len(parts) >= 2:
        distro = parts[0]
        package_path = parts[1]
        
        # Check if this distro is configured
        mirrors_config = get_all_mirrors()
        
        # Check if 'distro' is a key, or if we can map it
        upstream_key = get_upstream_key(distro, package_path)
        
        if upstream_key in mirrors_config or distro in mirrors_config:
            # It's a managed distro, use caching logic
            return proxy_package_logic(distro, package_path)

    # If we are here, it's an unknown request.
    # If passthrough is enabled, try to proxy it directly
    if get_config('passthrough_mode', True):
        # If the request came with a full URL (proxy style), use it.
        if request.url.startswith('http'):
            # DYNAMIC EXPANSION:
            # If it's a full URL, we can learn this new mirror!
            try:
                parsed = urlparse(request.url)
                host = parsed.netloc

                # Only learn if it looks like a repo (simple heuristic: has 'dists' or 'pool' or ends in .deb/.rpm)
                # Or just learn everything? Let's be safe and learn everything that is explicitly proxied via HTTP.
                # But we need to be careful not to learn random websites.
                # Let's just add it.
                
                mirrors_config = get_all_mirrors()
                if host not in mirrors_config:
                    new_url = f"{parsed.scheme}://{host}"
                    # Save to DB
                    # New mirrors are added as 'pending' by default in save_mirror_to_db
                    save_mirror_to_db(host, [new_url], status='pending')
                    logger.info(f"Learned new dynamic mirror (pending approval): {host} -> {new_url}")
                    add_log(f"New mirror pending approval: {host}", "WARNING")
                    
                    # Since it's pending, we can't use proxy logic yet.
                    # Fallback to direct proxy
                    return direct_proxy(request.url, {k:v for k,v in request.headers if k.lower() != 'host'})
                else:
                    # Mirror exists, but might be pending or blacklisted
                    # get_all_mirrors() only returns approved ones.
                    # If it's not in there, it's not approved.
                    return direct_proxy(request.url, {k:v for k,v in request.headers if k.lower() != 'host'})
                    
            except Exception as e:
                logger.error(f"Error learning dynamic mirror: {e}")

            return direct_proxy(request.url, {k:v for k,v in request.headers if k.lower() != 'host'})
        else:
            # It might be a relative path request that didn't match any distro.
            return Response(f"Unknown path and not a full proxy URL: {path}", status=404)
            
    return Response(f"Unsupported request: {path}", status=404)

# Load config at module level to ensure it's ready
load_config()
init_db()
load_stats_from_db()
load_mirrors_from_db()
load_blacklist_from_db()

if __name__ == '__main__':
    # Start background tasks thread
    bg_thread = threading.Thread(target=background_tasks, daemon=True)
    bg_thread.start()
    
    # Reload mirrors to reflect cleanup
    load_mirrors_from_db()
    
    host = get_config('host', '0.0.0.0')
    port = get_config('port', 8080)
    
    logger.info(f"Starting APT Proxy on {host}:{port}")
    logger.info(f"Cache directory: {get_config('storage_path_resolved')}")
    
    app.run(
        host=host,
        port=port,
        debug=False,
        threaded=True
    )
