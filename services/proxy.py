import socket
import select
import requests
import os
import time
from flask import Response, request, send_file
from utils.logger import logger
from utils.config import get_config
from services.stats import STATS, stats_lock, add_log
from services.mirrors import get_all_mirrors, get_upstream_key
from services.cache_manager import get_cache_path, is_cache_valid, stream_and_cache

def serve_from_cache(cache_path):
    """Serve file from local cache"""
    logger.info(f"Serving from cache: {cache_path}")
    add_log(f"HIT: {cache_path.name}", "SUCCESS")
    try:
        # Update access time (atime) to track last hit
        try:
            os.utime(cache_path, (time.time(), os.path.getmtime(cache_path)))
        except Exception as e:
            logger.warning(f"Failed to update atime for {cache_path}: {e}")

        # Use send_file to handle conditional GETs (If-Modified-Since) automatically
        file_size = cache_path.stat().st_size
        with stats_lock:
            STATS['bytes_served'] += file_size
            
        return send_file(cache_path)
    except Exception as e:
        logger.error(f"Error reading cache {cache_path}: {e}")
        add_log(f"Error reading cache {cache_path.name}: {e}", "ERROR")
        return Response(f"Error reading cache: {e}", status=500)

def direct_proxy(url, headers):
    """Directly proxy a request without caching (for unknown distros/passthrough)"""
    try:
        logger.info(f"Direct proxying: {url}")
        add_log(f"PROXY: {url}", "INFO")
        # Increased chunk size for better throughput
        resp = requests.get(url, headers=headers, stream=True, timeout=20, allow_redirects=True)
        
        def generate():
            # Increased chunk size to 1MB
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    with stats_lock:
                        STATS['bytes_served'] += len(chunk)
                    yield chunk
        
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.raw.headers.items()
                   if name.lower() not in excluded_headers]
        
        return Response(generate(), status=resp.status_code, headers=headers)
    except Exception as e:
        logger.error(f"Direct proxy error for {url}: {e}")
        add_log(f"Proxy error for {url}: {e}", "ERROR")
        return Response(f"Proxy error: {e}", status=502)

def proxy_package_logic(distro, package_path):
    """Logic to handle caching for a known distro"""
    with stats_lock:
        STATS['requests_total'] += 1

    cache_path = get_cache_path(distro, package_path)
    if is_cache_valid(cache_path):
        with stats_lock:
            STATS['cache_hits'] += 1
        return serve_from_cache(cache_path)

    upstream_key = get_upstream_key(distro, package_path)
    mirrors_config = get_all_mirrors()
    
    # Fallback to distro if key not found (e.g. noble-updates -> ubuntu)
    if upstream_key not in mirrors_config:
        if distro in mirrors_config:
            upstream_key = distro
        else:
            logger.warning(f"No upstream configured for: {upstream_key}")
            add_log(f"Unsupported upstream: {upstream_key}", "WARNING")
            return Response(f"Unsupported: {upstream_key}", status=404)
    
    mirrors = mirrors_config[upstream_key]
    if isinstance(mirrors, str):
        mirrors = [mirrors]
    
    upstream_urls = [f"{mirror}/{package_path}" for mirror in mirrors]
    
    logger.info(f"Request: /{distro}/{package_path} -> {upstream_key}")
    add_log(f"MISS: {package_path} -> {upstream_key}", "INFO")
    
    headers = {key: value for key, value in request.headers if key.lower() != 'host'}
    
    response = stream_and_cache(upstream_urls, cache_path, headers)

    if response.status_code == 304:
        with stats_lock:
            STATS['cache_hits'] += 1
    else:
        with stats_lock:
            STATS['cache_misses'] += 1

    return response

def handle_connect(path):
    """Handle HTTPS CONNECT tunneling"""
    # Prefer request.host as it is parsed from headers/request line and is most reliable for CONNECT
    target = request.host
    
    # Fallback to path if request.host is empty or looks wrong (e.g. just port)
    if not target or (target.isdigit() and path and not path.isdigit()):
        target = path
        
    if not target:
        logger.error("CONNECT request with no target")
        return Response("Cannot determine CONNECT target", status=400)

    host_port = target
    try:
        if ':' in host_port:
            host, port = host_port.rsplit(':', 1)
            try:
                port = int(port)
            except ValueError:
                port = 443
        else:
            host = host_port
            port = 443
            
        logger.info(f"CONNECT Tunneling to {host}:{port}")
        add_log(f"CONNECT: {host}:{port}", "INFO")
        
        # Connect to upstream
        upstream_sock = socket.create_connection((host, port), timeout=10)
        
        # Get client socket from Werkzeug environment
        client_sock = request.environ.get('werkzeug.socket')
        if not client_sock:
            logger.error("CONNECT failed: No client socket found in environment")
            upstream_sock.close()
            return Response("CONNECT not supported by this server configuration", status=501)

        # Send 200 Connection Established to client
        try:
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except Exception as e:
            logger.error(f"Failed to send 200 OK to client: {e}")
            upstream_sock.close()
            return Response("", status=500)
        
        # Start bidirectional tunneling
        def tunnel(client, upstream):
            try:
                sockets = [client, upstream]
                while True:
                    r, _, _ = select.select(sockets, [], [], 10)
                    if not r: continue
                    
                    for s in r:
                        # Increased buffer size for better throughput
                        data = s.recv(65536)
                        if not data:
                            return
                        if s is client:
                            upstream.sendall(data)
                        else:
                            client.sendall(data)
            except Exception as e:
                # Normal disconnects happen here
                pass
            finally:
                try: client.close()
                except: pass
                try: upstream.close()
                except: pass

        # We need to detach from Flask/Werkzeug to prevent it from closing the socket
        # This is tricky. In Werkzeug dev server, we can just hijack it and block.
        tunnel(client_sock, upstream_sock)
        
        # Return a dummy response, though the socket is likely closed/consumed
        return Response("", status=200)
        
    except Exception as e:
        logger.error(f"CONNECT error for {target}: {e}")
        add_log(f"CONNECT failed: {target} ({e})", "ERROR")
        return Response(f"CONNECT failed: {e}", status=502)
