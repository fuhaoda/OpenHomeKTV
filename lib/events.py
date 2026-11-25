"""JSON notifications and independently managed browser connections."""

import json
import logging
import threading
import uuid

from flask import has_request_context, request


# Connection IDs, rather than IP addresses, keep tabs on the same phone independent.
ip2websock = {}
ip2pane = {}  # Kept for compatibility with older callers; broadcasts do not use it.
_client_ips = {}
_send_locks = {}
_clients_lock = threading.RLock()


def register_client(socket, client_ip):
    client_id = uuid.uuid4().hex
    with _clients_lock:
        ip2websock[client_id] = socket
        _client_ips[client_id] = client_ip
        _send_locks[client_id] = threading.RLock()
    return client_id


def unregister_client(client_id):
    with _clients_lock:
        ip2websock.pop(client_id, None)
        _client_ips.pop(client_id, None)
        _send_locks.pop(client_id, None)


def send_client(client_id, message):
    with _clients_lock:
        socket = ip2websock.get(client_id)
        send_lock = _send_locks.get(client_id)
    if socket is None:
        return False
    try:
        # Notifications and periodic updates can originate on different threads.
        with send_lock:
            socket.send(json.dumps(message, ensure_ascii=False))
        return True
    except Exception:
        logging.debug("Removing disconnected browser %s", client_id, exc_info=True)
        unregister_client(client_id)
        return False


def broadcast(message):
    with _clients_lock:
        client_ids = list(ip2websock)
    for client_id in client_ids:
        send_client(client_id, message)


def ws_send(client_ip, message):
    with _clients_lock:
        client_ids = [key for key, ip in _client_ips.items() if ip == client_ip]
    for client_id in client_ids:
        send_client(client_id, message)


def flash(message, category="message", client_ip=""):
    if not client_ip and has_request_context():
        client_ip = request.remote_addr
    ws_send(client_ip, {
        "type": "notification",
        "data": {"message": str(message), "category": category},
    })
