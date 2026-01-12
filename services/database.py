import os
import sqlite3
import json
from pathlib import Path
from threading import Lock
from utils.logger import logger
from utils.config import get_config, BASE_DIR
from utils.defaults import DEFAULT_MIRRORS

db_lock = Lock()

def get_db_connection():
    db_path_str = get_config('database_path', 'data/stats.db')
    # Handle relative paths
    if os.path.isabs(db_path_str):
        db_path = Path(db_path_str)
    else:
        db_path = BASE_DIR / db_path_str
        
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

def init_db():
    """Initialize the database schema"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Stats table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER
                )
            ''')
            
            # Mirrors table
            # status: 'approved', 'pending', 'blacklisted'
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mirrors (
                    name TEXT PRIMARY KEY,
                    urls TEXT,
                    status TEXT DEFAULT 'approved'
                )
            ''')
            
            # Package Blacklist table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS package_blacklist (
                    pattern TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Check if we need to migrate old schema (missing status column)
            try:
                cursor.execute('SELECT status FROM mirrors LIMIT 1')
            except sqlite3.OperationalError:
                logger.info("Migrating mirrors table to include status column")
                cursor.execute('ALTER TABLE mirrors ADD COLUMN status TEXT DEFAULT "approved"')

            # Initialize default values if not present
            for key in ['requests_total', 'cache_hits', 'cache_misses', 'bytes_served']:
                cursor.execute('INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)', (key,))
            
            # Seed default mirrors if table is empty
            cursor.execute('SELECT count(*) as count FROM mirrors')
            if cursor.fetchone()['count'] == 0:
                logger.info("Seeding default mirrors to database")
                for name, urls in DEFAULT_MIRRORS.items():
                    cursor.execute('INSERT INTO mirrors (name, urls, status) VALUES (?, ?, ?)', 
                                  (name, json.dumps(urls), 'approved'))

            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
