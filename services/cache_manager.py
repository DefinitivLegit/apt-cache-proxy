import os
import hashlib
import time
import threading
import requests
import re
import gzip
from pathlib import Path
from datetime import datetime, timedelta
from flask import Response
from utils.logger import logger
from utils.config import get_config
from services.stats import STATS, stats_lock, add_log, save_stats_to_db
from services.database import db_lock, get_db_connection
from services.mirrors import get_all_mirrors, get_upstream_key

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

def search_upstream_packages(distro, query):
    """Search for packages in upstream mirror by checking Packages.gz if available or simple path check"""
    results = []
    
    # 1. If query looks like a path, check directly
    if '/' in query:
        upstream_key = get_upstream_key(distro, query)
        mirrors_config = get_all_mirrors()
        
        if upstream_key in mirrors_config:
            mirrors = mirrors_config[upstream_key]
            if isinstance(mirrors, str):
                mirrors = [mirrors]
            
            for mirror in mirrors:
                url = f"{mirror}/{query}"
                try:
                    resp = requests.head(url, timeout=2)
                    if resp.status_code == 200:
                        # Check if already cached
                        cache_path = get_cache_path(distro, query)
                        is_cached = is_cache_valid(cache_path)
                        
                        results.append({
                            'name': os.path.basename(query), 
                            'path': query, 
                            'distro': distro, 
                            'url': url,
                            'cached': is_cached
                        })
                        # Return immediately if found direct match
                        return results
                except:
                    pass

    # 2. If not a path, or path not found, try to search in cached Packages files
    # We look for *Packages.gz* or *Packages* files in our cache for this distro
    storage_path_str = get_config('storage_path_resolved')
    if not storage_path_str:
        return results
        
    storage_path = Path(storage_path_str) / distro
    if not storage_path.exists():
        return results

    # Limit search to avoid timeout
    limit = 20
    count = 0
    
    # Helper to parse Packages file content
    def parse_packages_file(filepath):
        matches = []
        try:
            if str(filepath).endswith('.gz'):
                f = gzip.open(filepath, 'rt', encoding='utf-8', errors='ignore')
            else:
                f = open(filepath, 'r', encoding='utf-8', errors='ignore')
            
            with f:
                current_pkg = {}
                for line in f:
                    line = line.strip()
                    if not line:
                        if current_pkg and 'Package' in current_pkg and 'Filename' in current_pkg:
                            if query.lower() in current_pkg['Package'].lower():
                                # Check if cached
                                pkg_path = current_pkg['Filename']
                                cache_path = get_cache_path(distro, pkg_path)
                                is_cached = is_cache_valid(cache_path)
                                
                                matches.append({
                                    'name': current_pkg['Package'],
                                    'path': pkg_path,
                                    'distro': distro,
                                    'version': current_pkg.get('Version', 'unknown'),
                                    'cached': is_cached
                                })
                        current_pkg = {}
                        continue
                    
                    if ':' in line:
                        key, val = line.split(':', 1)
                        current_pkg[key.strip()] = val.strip()
        except Exception:
            pass
        return matches

    # Find Packages files in cache
    # They are usually stored as hash_Packages or hash_Packages.gz
    for root, dirs, files in os.walk(storage_path):
        for filename in files:
            if 'Packages' in filename:
                # Check if it's a real Packages file (by name part)
                parts = filename.split('_', 1)
                real_name = parts[1] if len(parts) > 1 else filename
                
                if 'Packages' in real_name:
                    file_matches = parse_packages_file(os.path.join(root, filename))
                    for m in file_matches:
                        results.append(m)
                        count += 1
                        if count >= limit:
                            return results
        if count >= limit:
            break
            
    return results

def manual_cache_package(distro, package_path):
    """Manually download and cache a package"""
    try:
        cache_path = get_cache_path(distro, package_path)
        
        # Check if already cached
        if is_cache_valid(cache_path):
            return True, "File already cached"

        upstream_key = get_upstream_key(distro, package_path)
        mirrors_config = get_all_mirrors()
        
        if upstream_key not in mirrors_config:
            if distro in mirrors_config:
                upstream_key = distro
            else:
                return False, f"No upstream configured for: {upstream_key}"
        
        mirrors = mirrors_config[upstream_key]
        if isinstance(mirrors, str):
            mirrors = [mirrors]
        
        upstream_urls = [f"{mirror}/{package_path}" for mirror in mirrors]
        
        # Use stream_and_cache but consume the response to force download
        headers = {'User-Agent': 'apt-cache-proxy-manual'}
        response = stream_and_cache(upstream_urls, cache_path, headers)
        
        if response.status_code == 200:
            # Consume the generator to ensure file is written
            for _ in response.response:
                pass
            return True, "Successfully cached"
        else:
            return False, f"Failed to download: HTTP {response.status_code}"
            
    except Exception as e:
        logger.error(f"Error manual caching {distro}/{package_path}: {e}")
        return False, str(e)

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
