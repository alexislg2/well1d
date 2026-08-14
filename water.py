#!/usr/bin/env python3
"""
Pilotage local d'une vanne LinkTap G1-S via la passerelle GW-02 (Local HTTP API).

Usage :
    python water.py 60        # arrose 60 secondes
    python water.py 300       # arrose 5 minutes
    python water.py stop      # arrête l'arrosage en cours
    python water.py 0         # équivalent à stop

Prérequis :
    - "Local HTTP API" activé dans l'interface web de la passerelle
      (http://192.168.168.148)
    - GW_ID et DEV_ID renseignés ci-dessous (16 caractères hexa,
      visibles sur la page web de la passerelle ou dans l'app LinkTap)

Aucune dépendance externe (stdlib uniquement).
"""

import json
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GATEWAY_IP = "192.168.176.148"
GW_ID = "2FEE0E30004B1200"   # ID de la passerelle GW-02 (16 caractères)
DEV_ID = "3A62E71D004B1200"  # ID de la vanne G1-S (16 caractères)

API_URL = f"http://{GATEWAY_IP}/api.shtml"
TIMEOUT = 10
MAX_DURATION = 86400

# Codes retour documentés de l'API locale LinkTap
RET_CODES = {
    0: "OK",
    1: "Format de message invalide",
    2: "Commande non supportée",
    3: "gateway ID incorrect",
    4: "device ID introuvable (vanne non appairée à cette passerelle ?)",
    5: "Passerelle occupée, réessayer",
}


def send(payload: dict) -> dict:
    """POST JSON vers la passerelle, retourne la réponse décodée."""
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def start_watering(duration_s: int) -> dict:
    return send({
        "cmd": 6,
        "gw_id": GW_ID,
        "dev_id": DEV_ID,
        "duration": duration_s,
    })


def stop_watering() -> dict:
    return send({
        "cmd": 7,
        "gw_id": GW_ID,
        "dev_id": DEV_ID,
    })


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    arg = sys.argv[1].lower()

    try:
        if arg == "stop" or arg == "0":
            resp = stop_watering()
            action = "Arrêt demandé"
        else:
            try:
                duration = int(arg)
            except ValueError:
                print(f"Argument invalide : {arg!r} (attendu : durée en secondes ou 'stop')")
                return 2
            if not (0 < duration <= MAX_DURATION):
                print(f"Durée hors limites : {duration} (1 à {MAX_DURATION} s)")
                return 2
            resp = start_watering(duration)
            action = f"Arrosage {duration} s demandé"
    except urllib.error.URLError as e:
        print(f"Erreur réseau vers {API_URL} : {e}")
        return 1
    except (json.JSONDecodeError, TimeoutError) as e:
        print(f"Réponse invalide de la passerelle : {e}")
        return 1

    ret = resp.get("ret", -1)
    if ret == 0:
        print(f"{action} — OK")
        return 0

    print(f"{action} — échec (ret={ret} : {RET_CODES.get(ret, 'code inconnu')})")
    print(f"Réponse brute : {resp}")
    return 1


if __name__ == "__main__":
    sys.exit(main())