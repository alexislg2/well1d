#!/usr/bin/env python3
"""
Pilotage local d'une vanne LinkTap G1-S via la passerelle GW-02 (Local HTTP API).

Usage :
    python water.py 60        # arrose 60 secondes
    python water.py 300       # arrose 5 minutes
    python water.py stop      # arrête l'arrosage en cours
    python water.py 0         # équivalent à stop
    python water.py status    # état de la vanne

Configuration : voir linktap.py (variables d'environnement LINKTAP_IP,
LINKTAP_GW_ID, LINKTAP_DEV_ID).

Aucune dépendance externe (stdlib uniquement).
"""

import json
import sys

import linktap


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    arg = sys.argv[1].lower()

    try:
        if arg == "status":
            print(json.dumps(linktap.status(), indent=2, ensure_ascii=False))
            return 0
        if arg in ("stop", "0"):
            resp = linktap.stop()
            action = "Arrêt demandé"
        else:
            try:
                duration = int(arg)
            except ValueError:
                print("Argument invalide : {!r} (attendu : durée en secondes, 'stop' ou 'status')".format(arg))
                return 2
            resp = linktap.start(duration)
            action = "Arrosage {} s demandé".format(duration)
    except ValueError as e:
        print(e)
        return 2
    except linktap.GatewayError as e:
        print("Erreur : {}".format(e))
        return 1

    ret = resp.get("ret", -1)
    if ret == 0:
        print("{} — OK".format(action))
        return 0

    print("{} — échec (ret={} : {})".format(action, ret, linktap.ret_message(ret)))
    print("Réponse brute : {}".format(resp))
    return 1


if __name__ == "__main__":
    sys.exit(main())
