import os
import hashlib
import time
import threading
import requests
import re
from pathlib import Path
from datetime import datetime, timedelta
from flask import Response
from utils.logger import logger
from utils.config import get_config
from services.stats import STATS, stats_lock, add_log, save_stats_to_db
from services.database import db_lock, get_db_connection

# In-memory blacklist cache
BLACKLIST_PATTERNS = []
blacklist_lock = threading.Lock()

def load_blacklist_from_db():
    """Load package blacklist patterns from database"""
    global BLACKLIST_PATTERNS
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT pattern FROM package_blacklist')
            rows = cursor.fetchall()
            
            with blacklist_lock:
                BLACKLIST_PATTERNS = [row['pattern'] for row in rows]
                
            logger.info(f"Loaded {len(BLACKLIST_PATTERNS)} blacklist patterns")
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}")

def add_blacklist_pattern(pattern):
    """Add a pattern to the blacklist"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('INSERT OR IGNORE INTO package_blacklist (pattern) VALUES (?)', (pattern,))
            conn.commit()
            conn.close()
            
        with blacklist_lock:
            if pattern not in BLACKLIST_PATTERNS:
                BLACKLIST_PATTERNS.append(pattern)
                
        logger.info(f"Added blacklist pattern: {pattern}")
        return True
    except Exception as e:
        logger.error(f"Error adding blacklist pattern: {e}")
        return False

def remove_blacklist_pattern(pattern):
    """Remove a pattern from the blacklist"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM package_blacklist WHERE pattern = ?', (pattern,))
            conn.commit()
            conn.close()
            
        with blacklist_lock:
            if pattern in BLACKLIST_PATTERNS:
                BLACKLIST_PATTERNS.remove(pattern)
                
        logger.info(f"Removed blacklist pattern: {pattern}")
        return True
    except Exception as e:
        logger.error(f"Error removing blacklist pattern: {e}")
        return False

def get_blacklist_patterns():
    with blacklist_lock:
        return list(BLACKLIST_PATTERNS)

def is_blacklisted(filename):
    """Check if a filename matches any blacklist pattern"""
    with blacklist_lock:
        for pattern in BLACKLIST_PATTERNS:
            try:
                # Simple wildcard matching or regex? Let's assume simple substring or regex
                # If pattern contains *, treat as simple glob-like regex
                if '*' in pattern:
                    regex = pattern.replace('.', '\.').replace('*', '.*')
                    if re.search(regex, filename, re.IGNORECASE):
                        return True
                elif pattern.lower() in filename.lower():
                    return True
            except:
                pass
    return False

def get_cache_path(distro, path):
    """Generate a safe cache file path"""
    storage_path = Path(get_config('storage_path_resolved'))
    path_hash = hashlib.md5(path.encode()).hexdigest()
    filename = os.path.basename(path) if os.path.basename(path) else 'index'
    cache_dir = storage_path / distro / path_hash[:2]
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{path_hash}_{filename}"

def is_cache_valid(cache_path):
    """Check if cached file is still valid"""
    if not cache_path.exists():
        return False
    
    # Check if retention is enabled
    if not get_config('cache_retention_enabled', True):
        return True

    cache_days = get_config('cache_days', 7)
    
    # Check last access time (atime) if available, otherwise mtime
    try:
        last_access = cache_path.stat().st_atime
    except:
        last_access = cache_path.stat().st_mtime
        
    file_age = datetime.now() - datetime.fromtimestamp(last_access)
    return file_age < timedelta(days=cache_days)

def clean_old_cache():
    """Remove cache files older than CACHE_DAYS based on last access"""
    try:
        if not get_config('cache_retention_enabled', True):
            logger.info("Cache retention disabled, skipping cleanup")
            return

        storage_path_str = get_config('storage_path_resolved')
        if not storage_path_str:
            return

        storage_path = Path(storage_path_str)
        cache_days = get_config('cache_days', 7)
        cutoff_time = time.time() - (cache_days * 24 * 60 * 60)
        
        cleaned_count = 0
        
        # Use stack-based scandir for better performance
        stack = [str(storage_path)]
        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as scanner:
                    for entry in scanner:
                        if entry.is_dir():
                            stack.append(entry.path)
                        elif entry.is_file():
                            try:
                                # entry.stat() is cached
                                stat = entry.stat()
                                last_access = stat.st_atime
                                
                                # Fallback to mtime if atime is not updated/reliable or older than mtime
                                if stat.st_mtime > last_access:
                                    last_access = stat.st_mtime

                                if last_access < cutoff_time:
                                    os.remove(entry.path)
                                    cleaned_count += 1
                            except Exception:
                                pass
            except Exception as e:
                logger.error(f"Error scanning {current_dir}: {e}")
        
        if cleaned_count > 0:
            logger.info(f"Cleanup: Removed {cleaned_count} old files (accessed > {cache_days} days ago)")
            add_log(f"Cleanup: Removed {cleaned_count} old files", "INFO")
            
    except Exception as e:
        logger.error(f"Error during cache cleanup: {e}")
        add_log(f"Error during cache cleanup: {e}", "ERROR")

def delete_cached_file(rel_path):
    """Delete a specific file from cache"""
    try:
        storage_path_str = get_config('storage_path_resolved')
        if not storage_path_str:
            return False
            
        full_path = Path(storage_path_str) / rel_path
        
        # Security check
        if not str(full_path).startswith(str(Path(storage_path_str).resolve())):
            return False
            
        if full_path.exists():
            full_path.unlink()
            logger.info(f"Deleted cached file: {rel_path}")
            add_log(f"Deleted file: {rel_path}", "INFO")
            return True
        return False
    except Exception as e:
        logger.error(f"Error deleting file {rel_path}: {e}")
        return False

def stream_and_cache(urls, cache_path, headers):
    """Stream content from upstream and cache it locally"""
    if isinstance(urls, str):
        urls = [urls]
    
    # Check blacklist
    filename = cache_path.name
    # The cache path name is hash_filename, we want the real filename
    parts = filename.split('_', 1)
    real_filename = parts[1] if len(parts) > 1 else filename
    
    should_cache = not is_blacklisted(real_filename)
    if not should_cache:
        logger.info(f"File blacklisted, will not cache: {real_filename}")
        add_log(f"BLACKLISTED: {real_filename}", "WARNING")

    last_error = None
    
    for url in urls:
        try:
            logger.info(f"Fetching from upstream: {url}")
            # allow_redirects=True is default, but explicit is good
            # Increase chunk size for better throughput
            response = requests.get(url, stream=True, headers=headers, timeout=20, allow_redirects=True)
            
            if response.status_code == 404:
                logger.warning(f"File not found (404): {url}")
                # Don't return immediately, try other mirrors? 
                # Usually 404 means it's not there, but maybe mirror sync issue.
                last_error = "404 Not Found"
                continue
            
            # Handle success or partial/not-modified
            if response.status_code in [200, 206, 304]:
                
                resp_headers = {}
                excluded_headers = ['transfer-encoding', 'connection', 'content-encoding', 'content-length']
                for key, value in response.headers.items():
                    if key.lower() not in excluded_headers:
                        resp_headers[key] = value

                # If 304 Not Modified, just return it
                if response.status_code == 304:
                    add_log(f"HIT (304): {cache_path.name}", "SUCCESS")
                    return Response(status=304, headers=resp_headers)

                # If 206 Partial Content, stream but don't cache (too complex to merge)
                if response.status_code == 206:
                    add_log(f"PARTIAL: {cache_path.name}", "WARNING")
                    def generate_partial():
                        # Increased chunk size to 1MB
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                with stats_lock:
                                    STATS['bytes_served'] += len(chunk)
                                yield chunk
                    return Response(generate_partial(), status=206, headers=resp_headers)

                # If 200 OK
                if should_cache:
                    # Cache it
                    temp_path = cache_path.with_suffix('.tmp')
                    
                    def generate_cached():
                        try:
                            with open(temp_path, 'wb') as f:
                                # Increased chunk size to 1MB
                                for chunk in response.iter_content(chunk_size=1024 * 1024):
                                    if chunk:
                                        f.write(chunk)
                                        chunk_len = len(chunk)
                                        with stats_lock:
                                            STATS['bytes_served'] += chunk_len
                                        yield chunk
                            
                            temp_path.rename(cache_path)
                            logger.info(f"Cached to: {cache_path}")
                            add_log(f"CACHED: {cache_path.name}", "SUCCESS")
                            # Trigger save occasionally on write
                            if STATS['bytes_served'] % (10 * 1024 * 1024) == 0:
                                # Use a separate thread but don't hold onto request context
                                threading.Thread(target=save_stats_to_db).start()
                        except Exception as e:
                            logger.error(f"Error during caching: {e}")
                            add_log(f"Error caching {cache_path.name}: {e}", "ERROR")
                            if temp_path.exists():
                                temp_path.unlink()
                    
                    return Response(
                        generate_cached(),
                        status=200,
                        headers=resp_headers,
                        direct_passthrough=True
                    )
                else:
                    # Don't cache, just stream
                    def generate_stream():
                        # Increased chunk size to 1MB
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                with stats_lock:
                                    STATS['bytes_served'] += len(chunk)
                                yield chunk
                    return Response(generate_stream(), status=200, headers=resp_headers)
            
            # If we got here, it's an error code (500, 502, 403, etc)
            logger.warning(f"Upstream returned status {response.status_code} for {url}")
            last_error = f"HTTP {response.status_code}"
            continue 
        
        except requests.Timeout:
            logger.error(f"Timeout fetching {url}")
            last_error = "Timeout"
            continue
        except requests.RequestException as e:
            logger.error(f"Error fetching {url}: {e}")
            last_error = str(e)
            continue

    add_log(f"FAILED: {cache_path.name} ({last_error})", "ERROR")
    return Response(f"All upstream mirrors failed. Last error: {last_error}", status=502)
