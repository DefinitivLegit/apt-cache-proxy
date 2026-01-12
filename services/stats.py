import json
import os
import time
from datetime import datetime
from threading import Lock
from utils.logger import logger
from services.database import db_lock, get_db_connection
from utils.config import get_config
from pathlib import Path

STATS = {
    'requests_total': 0,
    'cache_hits': 0,
    'cache_misses': 0,
    'bytes_served': 0,
    'start_time': datetime.now()
}
FILE_STATS = {
    'total_files': 0,
    'total_size': 0,
    'distro_stats': {}
}
LOG_BUFFER = []
MAX_LOG_BUFFER = 100

stats_lock = Lock()
file_stats_lock = Lock()
log_lock = Lock()

def add_log(message, level='INFO'):
    """Add a log message to the in-memory buffer"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = {'time': timestamp, 'level': level, 'message': message}
    
    with log_lock:
        LOG_BUFFER.append(entry)
        if len(LOG_BUFFER) > MAX_LOG_BUFFER:
            LOG_BUFFER.pop(0)

def load_stats_from_db():
    """Load statistics from database into memory"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM stats')
            rows = cursor.fetchall()
            
            with stats_lock:
                for row in rows:
                    if row['key'] in STATS:
                        STATS[row['key']] = row['value']
            
            conn.close()
            logger.info("Stats loaded from database")
    except Exception as e:
        logger.error(f"Error loading stats from DB: {e}")

def save_stats_to_db():
    """Save current in-memory stats to database"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            with stats_lock:
                for key in ['requests_total', 'cache_hits', 'cache_misses', 'bytes_served']:
                    cursor.execute('UPDATE stats SET value = ? WHERE key = ?', (STATS[key], key))
            
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error saving stats to DB: {e}")

def update_file_stats():
    """Calculate file statistics (expensive operation)"""
    try:
        total_files = 0
        total_size = 0
        distro_stats = {}
        storage_path_str = get_config('storage_path_resolved')
        
        if storage_path_str:
            storage_path = Path(storage_path_str)
            if storage_path.exists():
                # Use os.scandir for better performance
                with os.scandir(storage_path) as it:
                    for entry in it:
                        if entry.is_dir() and not entry.name.startswith('.'):
                            distro = entry.name
                            d_files = 0
                            d_size = 0
                            
                            # Recursive scan for this distro using stack
                            stack = [entry.path]
                            while stack:
                                current_dir = stack.pop()
                                try:
                                    with os.scandir(current_dir) as scanner:
                                        for item in scanner:
                                            if item.is_file():
                                                d_files += 1
                                                try:
                                                    # item.stat() is cached
                                                    d_size += item.stat().st_size
                                                except:
                                                    pass
                                            elif item.is_dir():
                                                stack.append(item.path)
                                except Exception:
                                    pass
                            
                            distro_stats[distro] = {'files': d_files, 'size': d_size}
                            total_files += d_files
                            total_size += d_size
        
        with file_stats_lock:
            FILE_STATS['total_files'] = total_files
            FILE_STATS['total_size'] = total_size
            FILE_STATS['distro_stats'] = distro_stats
        
        logger.info("File stats updated")
    except Exception as e:
        logger.error(f"Error updating file stats: {e}")
