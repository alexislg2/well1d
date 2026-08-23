"""
Signature HMAC partagée entre le raspberry et le serveur.

Utilisée dans les deux sens, avec la même clé (`AGENT_HMAC_KEY`) : les deux
machines se font mutuellement confiance, une seconde clé n'isolerait rien.

    serveur -> pi   commandes d'arrosage   watering.py  -> water_agent.py
    pi -> serveur   dépôt des mesures      well.py      -> server.py

Le message signé contient la méthode, le chemin et le hash du corps : une
signature émise pour /upload_data ne peut donc pas être rejouée sur /start, ni
son contenu modifié en vol.

Stdlib uniquement : ce module tourne aussi sur le raspberry.
"""

import hashlib
import hmac
import secrets
import time

MAX_SKEW = 120        # le pi n'a pas de RTC, sa date au boot mérite de la marge

HEADER_TS = "X-Well-Ts"
HEADER_NONCE = "X-Well-Nonce"
HEADER_SIG = "X-Well-Sig"


def _message(ts, nonce, method, path, body):
    return "\n".join([ts, nonce, method, path, hashlib.sha256(body).hexdigest()])


def sign(key, method, path, body):
    """Retourne les en-têtes à joindre à la requête."""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    digest = hmac.new(key, _message(ts, nonce, method, path, body).encode(), hashlib.sha256)
    return {HEADER_TS: ts, HEADER_NONCE: nonce, HEADER_SIG: digest.hexdigest()}


def verify(key, method, path, body, headers, seen=None):
    """Retourne None si la requête est authentique, sinon le motif du refus.

    `headers` doit être insensible à la casse (Flask et http.server le sont).
    `seen(nonce) -> bool` consomme le nonce et indique s'il était neuf ; il
    n'est appelé qu'une fois la signature validée, pour qu'un tiers ne puisse
    pas épuiser le stock de nonces."""
    ts = headers.get(HEADER_TS) or ""
    nonce = headers.get(HEADER_NONCE) or ""
    signature = headers.get(HEADER_SIG) or ""
    if not (ts and nonce and signature):
        return "en-têtes de signature manquants"
    try:
        skew = abs(time.time() - int(ts))
    except ValueError:
        return "horodatage invalide"
    if skew > MAX_SKEW:
        return "horodatage hors tolérance ({:.0f} s)".format(skew)

    expected = hmac.new(key, _message(ts, nonce, method, path, body).encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "signature invalide"
    if seen is not None and not seen(nonce):
        return "nonce déjà utilisé"
    return None
