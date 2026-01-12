import os
import json
import logging
from pathlib import Path
from threading import Lock
from utils.logger import logger

# Global configuration
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG = {}
config_lock = Lock()

def is_docker():
    """Check if running inside a Docker container"""
    return os.path.exists('/.dockerenv')

DEFAULT_CONFIG = {
  "host": "0.0.0.0",
  "port": 8080,
  "storage_path": "storage",
  "database_path": "data/stats.db",
  "cache_days": 7,
  "cache_retention_enabled": True,
  "log_level": "INFO",
  "passthrough_mode": True,
  "admin_token": "changeme_to_secure_random_string"
}

if is_docker():
    DEFAULT_CONFIG.pop('storage_path', None)
    DEFAULT_CONFIG.pop('database_path', None)

def get_config_path():
    """Returns the path to the config file, ensuring the directory exists."""
    data_dir = BASE_DIR / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / 'config.json'

def get_config(key, default=None):
    with config_lock:
        return CONFIG.get(key, default)

def save_config_value(key, value):
    """Update a single config value and save to disk"""
    global CONFIG
    try:
        config_path = get_config_path()
        
        # Read current file first to preserve comments/structure if possible (though json lib won't)
        # or just use current memory state? Better to read fresh to avoid overwriting external changes
        if config_path.exists():
            with open(config_path, 'r') as f:
                current_disk_config = json.load(f)
        else:
            current_disk_config = CONFIG.copy()
            
        current_disk_config[key] = value
        
        with open(config_path, 'w') as f:
            json.dump(current_disk_config, f, indent=2)
            
        # Update memory
        with config_lock:
            CONFIG[key] = value
            # Special handling for side effects
            if key == 'log_level':
                logger.setLevel(getattr(logging, value))
                
        logger.info(f"Config updated: {key} = {value}")
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False

def load_config():
    """Load configuration from JSON file"""
    global CONFIG
    try:
        config_path = get_config_path()
        
        if not config_path.exists():
            logger.info(f"Config file not found at {config_path}, creating default.")
            with open(config_path, 'w') as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            new_config = DEFAULT_CONFIG
        else:
            with open(config_path, 'r') as f:
                new_config = json.load(f)
            
        with config_lock:
            CONFIG.clear()
            CONFIG.update(new_config)
            
            if is_docker():
                CONFIG['storage_path'] = 'storage'
                CONFIG['database_path'] = 'data/stats.db'

            # Ensure storage path exists
            storage_path_str = CONFIG.get('storage_path', 'storage')
            if os.path.isabs(storage_path_str):
                storage_path = Path(storage_path_str)
            else:
                storage_path = BASE_DIR / storage_path_str
            
            storage_path.mkdir(parents=True, exist_ok=True)
            # Store resolved path back to config for easier access
            CONFIG['storage_path_resolved'] = str(storage_path)
            
            # Update log level if changed
            logger.setLevel(getattr(logging, CONFIG.get('log_level', 'INFO')))
            
        logger.info(f"Configuration loaded successfully from {config_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return False
