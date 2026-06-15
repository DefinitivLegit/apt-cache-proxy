import json
import requests
import re
from threading import Lock
from utils.logger import logger
from pathlib import Path
from services.mirrors import get_all_mirrors
from utils.config import get_config

PEERS_CACHE = {} 
# In-memory support for peers
# Structure: {'distro': [urls ...]}

peers_lock = Lock()

def valid_peer_filename(path_object):
    # Validation: 
    # peers are designed to be dynamically configured and automatically approved
    # to avoid malicious code injection, we will restrict its use to packages only.
    # Metadata and package lists will be downloaded directly from upstream to ensure
    # that packages are properly verified.
    peers_blacklist = get_config("peers_blacklist")
    path_components = { "fullpath": str(path_object), "name": path_object.name, "stem": path_object.stem, "suffix": path_object.suffix }
    for key in peers_blacklist:
        path_component = path_components[key]
        for reg_exp in peers_blacklist[key]:
            if re.match(reg_exp, path_component):
                return False

    return True

def get_peers_urls(distro, package_path):
    """Get urls of peers where package is already cached"""
    valid_urls = []
    path_object = Path(package_path)

    if not valid_peer_filename(path_object):
        return []

    package_name=path_object.name
    # Validation: Check distro availability in peers list
    if distro in PEERS_CACHE:
        for url in PEERS_CACHE[distro]:
            # Validation: Check if url is reachable and the availability of tne package in remote cache
            if validate_peer(url, distro, package_name):
                # concatenate distro name to url to get a valid 'upstream' mirror
                valid_urls.append(url+"/"+distro)

    return valid_urls

def validate_peer(url, distro, package_name):
    """Check if the peer URL is reachable and package is available from peers cache"""
    url_query=url+"/api/cache/search"
    try:
        resp = requests.get(url_query, params= {"q": package_name},timeout=3, allow_redirects=True)
        if resp.status_code < 400 :
            resp_data_list = resp.json()
            # Validation: check distro name
            for resp_data in resp_data_list:
                if ("distro" in resp_data) and (distro == resp_data["distro"]):
                    return True

    except Exception as e:
        logger.error(f"Error validating peer: {e}")

    return False

def del_url_from_peer(distro, urls):
    """Remove existing peer's URLs in cache"""
    try:
        with peers_lock:
            if distro not in PEERS_CACHE:
                 return False
            current_data = PEERS_CACHE[distro]
            for url in urls:
                if url in current_data:
                    current_data.remove(url)
            
            PEERS_CACHE[distro] = current_data
            if current_data:
                logger.info(f"Updated peer: {distro} -> {current_data}")
                return True
    except Exception as e:
        logger.error(f"Error updating peer: {e}")
        return False

    # not current_data case
    return delete_peer(distro)

def add_url_to_peer(distro, urls):
    """Add existing peer's URLs in cache"""
    if distro not in PEERS_CACHE:
        return add_peer(distro, urls)

    try:
        with peers_lock:
            current_data = PEERS_CACHE[distro]
            for url in urls:
                if url not in current_data:
                    current_data.append(url)
            
            PEERS_CACHE[distro] = current_data
            logger.info(f"Updated peers urls for {distro} -> {current_data}")
            return True
    except Exception as e:
        logger.error(f"Error updating peer: {e}")
        return False

def add_peer(distro, urls):
    """Add a new dynamic peer to cache"""
    if distro not in get_all_mirrors():
        logger.error("Error adding peer (invalid distro)")
        return False

    try:
        with peers_lock:
            PEERS_CACHE[distro] = urls
            logger.info(f"Added peers for {distro} -> {urls}")
            return True
    except Exception as e:
        logger.error(f"Error adding peer: {e}")
        return False

def delete_peer(distro):
    """Delete a peer from the cache"""
    try:
        with peers_lock:
            if distro in PEERS_CACHE:
                del PEERS_CACHE[distro]
                
            logger.info(f"Deleted ALL peers for {distro}")
            return True
    except Exception as e:
        logger.error(f"Error deleting peer: {e}")
        return False

def get_all_peers():
    """Get ALL peers"""
    return PEERS_CACHE

def get_distros_by_peer(url):
    current_distros = []
    for distro in PEERS_CACHE:
        if url in PEERS_CACHE[distro] :
            current_distros.append(distro)

    return current_distros

def update_distros_by_peer(url, distros):
    current_distros = get_distros_by_peer(url)
    logger.info(f"INFO: {url} {distros}")
    try:
        for current_distro in current_distros:
            if not current_distro in distros:
                del_url_from_peer(current_distro, [url])

    except Exception as e:
        logger.error(f"Error deleting peer: {e}")
        return False

    try:
        for distro in distros:
            if (distro not in PEERS_CACHE) or (url not in PEERS_CACHE[distro]):
                add_url_to_peer(distro, [url])

        return True

    except Exception as e:
        logger.error(f"Error Adding peer: {e}")
        return False


