from flask import Blueprint, Response, request, jsonify
from utils.routes  import check_auth
from services.peers import get_all_peers, add_url_to_peer, del_url_from_peer, update_distros_by_peer
from services.mirrors import get_all_mirrors

routes_peers = Blueprint('routes_peers', __name__)

@routes_peers.route('/api/peers', methods=['GET'])
def api_peers_get():
    """Get peers list"""
    return jsonify(get_all_peers())

@routes_peers.route('/api/peers', methods=['PUT'])
def api_peers_put():
    """Add/Update distro list for a peer"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    url = data.get('url')
    distros = data.get('distros')

    if not url or not distros:
        return Response("Missing url or distros", status=400)
            
    if isinstance(distros, str):
        distros = [distros]
        
    if update_distros_by_peer(url, distros):
        return jsonify({'status': 'success'})

    return Response("Failed to update peer", status=500)

@routes_peers.route('/api/peers', methods=['DELETE'])
def api_peers_del():
    """Delete peer from some/all distros"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    url = data.get('url')
    distros = data.get('distros')

    if not url:
        return Response("Missing url", status=400)
            
    if not distros:
        distros = []

    if isinstance(distros, str):
        distros = [distros]
        
    if update_distros_by_peer(url, distros):
        return jsonify({'status': 'success'})

    return Response("Failed to delete peer distros", status=500)

@routes_peers.route('/api/peers/<distro>', methods=['GET'])
def api_peers_distro_get(distro):
    """Get url peer list for distro"""
    peers = get_all_peers()
    if distro in peers:
        return jsonify(peers[distro])
    
    return []

@routes_peers.route('/api/peers/<distro>', methods=['PUT'])
def api_peers_distro_put(distro):
    """Add/update url peer list for distro"""
    if not check_auth():
        return Response("Unauthorized", status=401)

    urls = data.get('urls')

    if not urls:
        return Response("Missing urls", status=400)

    if add_url_to_peer(distro, urls=urls):
        return jsonify({'status': 'success'})

    return Response("Failed to add peer URLs", status=500)    
        
@routes_peers.route('/api/peers/<distro>', methods=['DELETE'])
def api_peers_distro_del(distro):
    """Delete peers urls for distro"""
    if not check_auth():
        return Response("Unauthorized", status=401)
        
    data = request.get_json()
    urls = data.get('urls')
    
    if not urls:
        return Response("Missing urls", status=400)

    if del_url_from_peer(distro, urls=urls):
        return jsonify({'status': 'success'})

    return Response("Failed to delete peer URL", status=500)

@routes_peers.route('/api/mirrors', methods=['GET'])
def api_mirrors_get():
    """Get all approved mirrors"""
    return jsonify(get_all_mirrors())

