#!/usr/bin/env python3
"""
Agent HTTP d'arrosage, tourne sur le raspberry.

La passerelle LinkTap est sur le LAN Freebox, le serveur web est dans un docker
joignable uniquement par le VPN : le raspberry est le seul point à cheval sur
les deux réseaux. Cet agent relaie les commandes du serveur vers la passerelle.

    serveur (10.15.8.1) --HTTP signé HMAC--> agent (ici) --> LinkTap 192.168.1.7

Endpoints :
    GET  /health   — sans auth, aucun appel passerelle
    POST /status   — état de la vanne
    POST /start    — {"duration_s": 600}
    POST /stop

Chaque requête authentifiée porte :
    X-Well-Ts, X-Well-Nonce,
    X-Well-Sig: hex(HMAC-SHA256(AGENT_HMAC_KEY,
                    ts \\n nonce \\n method \\n path \\n sha256hex(body)))

Configuration par variables d'environnement (voir /etc/well-agent.env) :
    AGENT_HMAC_KEY   — obligatoire, `openssl rand -hex 32`
    AGENT_PORT       — défaut 8787
    AGENT_ALLOW_CIDR — défaut 10.15.8.0/24

Aucune dépendance externe (stdlib uniquement).
"""

import ipaddress
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import linktap
import wellsig

PORT = int(os.environ.get("AGENT_PORT", "8787"))
ALLOW_CIDR = ipaddress.ip_network(os.environ.get("AGENT_ALLOW_CIDR", "10.15.8.0/24"))
HMAC_KEY = os.environ.get("AGENT_HMAC_KEY", "").encode()

MAX_BODY = 4096
STATUS_TTL = 5

log = logging.getLogger("water_agent")

_lock = threading.Lock()
_seen_nonces = deque()   # (nonce, ts), élaguée au-delà de MAX_SKEW
_status_cache = {"at": 0.0, "value": None}


def _nonce_is_fresh(nonce):
    """Rejet du rejeu. L'agent est mono-processus : une deque suffit, là où le
    serveur et ses 16 workers ont besoin d'une table sqlite."""
    now = time.time()
    with _lock:
        while _seen_nonces and _seen_nonces[0][1] < now - wellsig.MAX_SKEW:
            _seen_nonces.popleft()
        if any(n == nonce for n, _ in _seen_nonces):
            return False
        _seen_nonces.append((nonce, now))
        return True


def cached_status():
    now = time.time()
    with _lock:
        if _status_cache["value"] is not None and now - _status_cache["at"] < STATUS_TTL:
            return _status_cache["value"], True
    value = linktap.status()
    with _lock:
        _status_cache.update(at=time.time(), value=value)
    return value, False


def invalidate_status():
    with _lock:
        _status_cache["value"] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "well1d-water-agent/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Silence le log par requête : /var/log/ramlog est un tmpfs de 50 Mo et
        le serveur sonde /status toutes les 5 s."""

    def _reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, code, reason):
        log.warning("refus %s %s depuis %s : %s", self.command, self.path,
                    self.client_address[0], reason)
        self._reply(code, {"ok": False, "error": reason})

    def _authenticate(self, body):
        try:
            client = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return "adresse source illisible"
        if client not in ALLOW_CIDR:
            return "adresse source hors {}".format(ALLOW_CIDR)

        return wellsig.verify(HMAC_KEY, self.command, self.path, body,
                              self.headers, seen=_nonce_is_fresh)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "version": 1})
        else:
            self._reply(404, {"ok": False, "error": "inconnu"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._reject(400, "Content-Length invalide")
        if length > MAX_BODY:
            return self._reject(413, "corps trop volumineux")
        body = self.rfile.read(length) if length else b""

        reason = self._authenticate(body)
        if reason:
            return self._reject(401, reason)

        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            return self._reject(400, "JSON invalide")
        if not isinstance(payload, dict):
            return self._reject(400, "JSON invalide")

        handler = {"/status": self.handle_status,
                   "/start": self.handle_start,
                   "/stop": self.handle_stop}.get(self.path)
        if handler is None:
            return self._reply(404, {"ok": False, "error": "inconnu"})

        try:
            handler(payload)
        except linktap.GatewayError as e:
            log.warning("passerelle : %s", e)
            self._reply(502, {"ok": False, "error": str(e)})

    def handle_status(self, payload):
        state, cached = cached_status()
        answer = {"ok": True, "cached": cached}
        answer.update(state)
        self._reply(200, answer)

    def handle_start(self, payload):
        try:
            duration_s = int(payload.get("duration_s"))
        except (TypeError, ValueError):
            return self._reject(400, "duration_s invalide")
        if not 0 < duration_s <= linktap.MAX_DURATION:
            return self._reject(400, "duration_s hors limites (1 à {} s)".format(linktap.MAX_DURATION))

        resp = linktap.start(duration_s)
        ret = resp.get("ret", -1)
        invalidate_status()
        log.info("start %s s -> ret=%s (%s)", duration_s, ret, linktap.ret_message(ret))
        self._reply(200 if ret == 0 else 502,
                    {"ok": ret == 0, "ret": ret, "message": linktap.ret_message(ret)})

    def handle_stop(self, payload):
        resp = linktap.stop()
        ret = resp.get("ret", -1)
        invalidate_status()
        log.info("stop -> ret=%s (%s)", ret, linktap.ret_message(ret))
        self._reply(200 if ret == 0 else 502,
                    {"ok": ret == 0, "ret": ret, "message": linktap.ret_message(ret)})


def main():
    # stdout et non stderr (défaut de logging) : sinon tout atterrit dans
    # water_agent_error.log et water_agent.log reste vide. Les tracebacks non
    # rattrapés, eux, gardent stderr — la séparation devient utile.
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(HMAC_KEY) < 32:
        log.error("AGENT_HMAC_KEY absente ou trop courte (attendu >= 32 caractères)")
        return 2

    # Bind sur 0.0.0.0 : binder l'IP VPN échoue en EADDRNOTAVAIL au boot, avant
    # que wireguard soit monté — et le pi reboote tous les matins à 4 h.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("agent à l'écoute sur 0.0.0.0:%s, source autorisée %s, passerelle %s",
             PORT, ALLOW_CIDR, linktap.API_URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
