#!/usr/bin/env python3
"""
Client de la passerelle LinkTap GW-02 (Local HTTP API).

Importé par `water_agent.py`, l'agent HTTP que le serveur appelle par le VPN.

S'utilise aussi en ligne de commande, pour diagnostiquer la passerelle sans
passer par le serveur ni le VPN :

    python3 linktap.py status    # état de la vanne
    python3 linktap.py 300       # arrose 5 minutes
    python3 linktap.py stop      # arrête l'arrosage en cours
    python3 linktap.py 0         # équivalent à stop

Configuration par variables d'environnement : LINKTAP_IP, LINKTAP_GW_ID,
LINKTAP_DEV_ID. Aucune dépendance externe.

Prérequis : "Local HTTP API" activé dans l'interface web de la passerelle.
"""

import json
import os
import sys
import urllib.error
import urllib.request

GATEWAY_IP = os.environ.get("LINKTAP_IP", "192.168.1.7")
GW_ID = os.environ.get("LINKTAP_GW_ID", "2FEE0E30004B1200")   # passerelle GW-02
DEV_ID = os.environ.get("LINKTAP_DEV_ID", "3A62E71D004B1200")  # vanne G1-S

API_URL = "http://{}/api.shtml".format(GATEWAY_IP)
TIMEOUT = 10
MAX_DURATION = 7200

CMD_STATUS = 3
CMD_START = 6
CMD_STOP = 7

RET_CODES = {
    0: "OK",
    1: "Format de message invalide",
    2: "Commande non supportée",
    3: "gateway ID incorrect",
    4: "device ID introuvable (vanne non appairée à cette passerelle ?)",
    5: "Passerelle occupée, réessayer",
}


class GatewayError(Exception):
    pass


def _parse(raw):
    """La passerelle enveloppe sa réponse dans du HTML tant que "Wrap the
    gateway's response in HTML" est actif dans son admin — et un reset usine
    réactive ce réglage silencieusement."""
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end < start:
        raise GatewayError("réponse illisible : {!r}".format(raw[:200]))
    try:
        return json.loads(raw[start:end + 1])
    except ValueError as e:
        raise GatewayError("JSON invalide : {}".format(e))


def send(payload):
    """POST JSON vers la passerelle, retourne la réponse décodée."""
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise GatewayError("passerelle {} injoignable : {}".format(API_URL, e))
    return _parse(raw)


def ret_message(ret):
    return RET_CODES.get(ret, "code inconnu ({})".format(ret))


def start(duration_s):
    if not 0 < duration_s <= MAX_DURATION:
        raise ValueError("durée hors limites : {} (1 à {} s)".format(duration_s, MAX_DURATION))
    return send({"cmd": CMD_START, "gw_id": GW_ID, "dev_id": DEV_ID, "duration": duration_s})


def stop():
    return send({"cmd": CMD_STOP, "gw_id": GW_ID, "dev_id": DEV_ID})


def _find_device(resp):
    """La forme exacte de la réponse cmd 3 varie selon le firmware : parfois un
    tableau `dev_stat`, parfois les champs à plat."""
    for key in ("dev_stat", "devices", "end_dev"):
        entries = resp.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("dev_id") == DEV_ID:
                    return entry
            if entries and isinstance(entries[0], dict):
                return entries[0]
        elif isinstance(entries, dict):
            return entries
    return resp


def status():
    """Retourne l'état normalisé de la vanne."""
    resp = send({"cmd": CMD_STATUS, "gw_id": GW_ID, "dev_id": DEV_ID})
    dev = _find_device(resp)

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return {
        "is_watering": bool(dev.get("is_watering")),
        "remain_s": as_int(dev.get("remain_duration")),
        "total_s": as_int(dev.get("total_duration")),
        "battery": as_int(dev.get("battery")),
        "signal": as_int(dev.get("signal")),
        "rf_linked": dev.get("is_rf_linked"),
        "raw": resp,
    }


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    arg = sys.argv[1].lower()
    try:
        if arg == "status":
            print(json.dumps(status(), indent=2, ensure_ascii=False))
            return 0
        if arg in ("stop", "0"):
            resp = stop()
            action = "Arrêt demandé"
        else:
            try:
                duration = int(arg)
            except ValueError:
                print("Argument invalide : {!r} (attendu : durée en secondes, 'stop' ou 'status')".format(arg))
                return 2
            resp = start(duration)
            action = "Arrosage {} s demandé".format(duration)
    except ValueError as e:
        print(e)
        return 2
    except GatewayError as e:
        print("Erreur : {}".format(e))
        return 1

    ret = resp.get("ret", -1)
    if ret == 0:
        print("{} — OK".format(action))
        return 0

    print("{} — échec (ret={} : {})".format(action, ret, ret_message(ret)))
    print("Réponse brute : {}".format(resp))
    return 1


if __name__ == "__main__":
    sys.exit(main())
