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
  "peers_enabled": True,
  "peers_blacklist": { "fullpath": [], "name": [ "^Packages(\\.gz){0,1}$", "^Release$", "^InRelease$" ], "stem": [], "suffix": [ "^\\.gpg$", "^\\.md5$", "^\\.sha(\\d){0,3}$" ] },
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
        # If running in Docker, we don't save to config.json
        if is_docker():
            logger.warning("Running in Docker, skipping save to config.json. Configuration is managed via environment variables.")
            # Still update memory for runtime changes if needed, but they won't persist across restarts
            with config_lock:
                CONFIG[key] = value
                if key == 'log_level':
                    logger.setLevel(getattr(logging, value))
            return True

        config_path = get_config_path()
        
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
    """Load configuration from JSON file or Environment Variables"""
    global CONFIG
    try:
        new_config = DEFAULT_CONFIG.copy()
        
        if is_docker():
            logger.info("Running in Docker environment. Loading configuration from environment variables.")
            
            # Map environment variables to config keys
            # Environment variables should be prefixed with APT_PROXY_ or just match the key in uppercase
            
            # Helper to get env var with fallback
            def get_env(key, default):
                return os.environ.get(f"APT_PROXY_{key.upper()}", os.environ.get(key.upper(), default))

            new_config['host'] = '0.0.0.0'
            new_config['port'] = 8080
            new_config['storage_path'] = 'storage'
            new_config['database_path'] = 'data/stats.db'
            new_config['cache_days'] = int(get_env('cache_days', 7))

            ## THIS STILL NEEDS TO BE IN THE JSON
            retention = get_env('cache_retention_enabled', 'True')
            new_config['cache_retention_enabled'] = retention.lower() in ('true', '1', 'yes')
            
            new_config['log_level'] = get_env('log_level', 'INFO')

            ## THIS STILL NEEDS TO BE IN THE JSON
            passthrough = get_env('passthrough_mode', 'True')
            new_config['passthrough_mode'] = passthrough.lower() in ('true', '1', 'yes')
            
            new_config['admin_token'] = get_env('admin_token', 'changeme_to_secure_random_string')
            
        else:
            config_path = get_config_path()
            
            if not config_path.exists():
                logger.info(f"Config file not found at {config_path}, creating default.")
                with open(config_path, 'w') as f:
                    json.dump(DEFAULT_CONFIG, f, indent=2)
                new_config = DEFAULT_CONFIG
            else:
                with open(config_path, 'r') as f:
                    file_config = json.load(f)
                    new_config.update(file_config)
            
        with config_lock:
            CONFIG.clear()
            CONFIG.update(new_config)
            
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
            
        logger.info(f"Configuration loaded successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return False
