# API Well 1D

API HTTP du serveur `server.py` qui expose les mesures de hauteur d'eau de la citerne.

* **Base URL (prod)** : `https://well1d.somebod.com`
* **Base URL (local)** : `http://127.0.0.1:5000`
* **Authentification** : aucune en lecture, tous les endpoints de mesure sont publics. L'écriture est signée : `/upload_data` par HMAC (voir [Dépôt des mesures](#post-upload_data)), `/watering/*` par mot de passe (voir [Arrosage](#arrosage--watering)).
* **Format** : JSON (`Content-Type: application/json`), UTF-8.
* **Fuseau horaire** : toutes les dates lisibles sont exprimées en heure locale `Europe/Paris`. Les timestamps sont en secondes Unix (UTC).

## Modèle de données

Le capteur du raspberry envoie une mesure par minute. Une mesure est une **hauteur d'eau en millimètres** (`height_mm`) associée à un timestamp Unix.

Le volume est dérivé de la hauteur en assimilant la citerne à un cylindre :

```
liters = π × WELL_RADIUS² × height_mm       avec WELL_RADIUS = 0.94425 m
```

| Constante | Valeur | Signification |
|---|---|---|
| `WELL_RADIUS` | `0.94425` m | Rayon de la citerne (calibré le 16/07 : 1000 L mesurés au compteur entre 3062 et 2705 mm) |
| `WELL_HEIGHT` | `3069` mm | Hauteur totale de la citerne, soit ≈ 8596 L à plein |

> ⚠️ Le rayon est plus faible dans la partie haute de la citerne, près du niveau du sol : le volume calculé est donc légèrement surestimé sur les niveaux hauts.

Les mesures dont `height_mm` est `NULL` (capteur muet) sont **toujours exclues** des réponses.

---

## `GET /api/measurements`

Récupère les points entre deux timestamps, avec lissage optionnel. C'est l'endpoint principal.

### Paramètres

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `from` | timestamp ou date | — | Début de la plage, **inclus**. Si omis : depuis le premier point de la base. |
| `to` | timestamp ou date | — | Fin de la plage, **incluse**. Si omis : jusqu'au dernier point. |
| `n` | entier `0`–`1440` | `5` | Lissage : taille en **minutes** du seau d'agrégation. Voir ci-dessous. |

#### Formats acceptés pour `from` / `to`

Les deux paramètres acceptent indifféremment :

| Format | Exemple | Interprétation |
|---|---|---|
| Timestamp Unix (secondes) | `1755000000` | UTC |
| Date seule | `2025-08-12` | `00:00:00`, heure de Paris |
| Date + heure | `2025-08-12 14:30` | heure de Paris |
| Date + heure + secondes | `2025-08-12 14:30:00` | heure de Paris |
| ISO 8601 | `2025-08-12T14:30:00` | heure de Paris |

Pensez à encoder l'espace (`%20`) dans une URL, ou utilisez la forme `T`.

#### Lissage (`n`)

* `n = 0` ou `n = 1` → **pas de lissage**, les points bruts sont renvoyés tels quels (1 point par minute environ).
* `n ≥ 2` → les points sont regroupés en seaux de `n` minutes et **moyennés**. Le `timestamp` retourné est le début du seau (aligné sur `timestamp - (timestamp % (n × 60))`, donc sur l'époque Unix, pas sur `from`).

Valeurs utilisées par l'interface web selon la période affichée : `n=1` (3 h), `n=5` (24 h et 7 j), `n=15` (1 mois), `n=60` (1 an).

> Sans lissage, une année de données représente ~525 000 points. Choisissez un `n` cohérent avec la largeur de la plage demandée.

### Réponse `200`

```json
{
  "from": 1755000000,
  "to": 1755003600,
  "n": 15,
  "count": 5,
  "well": {
    "height_mm": 3069,
    "radius_m": 0.94425,
    "max_liters": 8596.5
  },
  "points": [
    {
      "timestamp": 1755000000,
      "datetime": "2025-08-12T14:00:00+02:00",
      "height_mm": 2007.0,
      "liters": 5621.7
    }
  ]
}
```

| Champ | Description |
|---|---|
| `from` / `to` | Bornes effectivement appliquées, normalisées en timestamp Unix. `null` si le paramètre n'a pas été fourni. |
| `n` | Lissage appliqué, en minutes. |
| `count` | Nombre de points dans `points`. |
| `well` | Constantes de la citerne, utiles pour calculer un pourcentage de remplissage. |
| `points` | Liste **triée par timestamp croissant**. |
| `points[].timestamp` | Timestamp Unix (secondes, UTC). Début du seau si `n ≥ 2`. |
| `points[].datetime` | Le même instant en ISO 8601 avec offset local (`Europe/Paris`). |
| `points[].height_mm` | Hauteur d'eau en mm, arrondie au dixième (moyenne du seau si `n ≥ 2`). |
| `points[].liters` | Volume en litres, arrondi au dixième. |

Une plage sans données renvoie `200` avec `"count": 0` et `"points": []`.

### Erreurs `400`

```json
{ "error": "Format de date invalide : 'oups'" }
```

| Cas | Message |
|---|---|
| `from` ou `to` non parsable | `Format de date invalide : '...'` |
| `n` non entier | `Le paramètre 'n' doit être un entier` |
| `n` hors bornes | `Le paramètre 'n' doit être compris entre 0 et 1440` |
| `from` postérieur à `to` | `'from' doit être antérieur à 'to'` |

### Exemples

Dernières 24 h, moyennes 5 minutes :

```bash
curl "https://well1d.somebod.com/api/measurements?from=2025-08-12&to=2025-08-13&n=5"
```

Points bruts d'une heure précise :

```bash
curl "https://well1d.somebod.com/api/measurements?from=2025-08-12T14:00:00&to=2025-08-12T15:00:00&n=0"
```

Une année en moyennes horaires, avec timestamps Unix :

```bash
curl "https://well1d.somebod.com/api/measurements?from=1723464000&to=1755000000&n=60"
```

Pourcentage de remplissage courant en Python :

```python
import requests

r = requests.get("https://well1d.somebod.com/api/measurements",
                 params={"from": "2025-08-13T00:00:00", "n": 15}).json()
last = r["points"][-1]
pct = 100 * last["height_mm"] / r["well"]["height_mm"]
print(f'{last["datetime"]} : {last["liters"]:.0f} L ({pct:.1f} %)')
```

---

## `GET /latest`

Dernière mesure connue.

### Réponse `200`

```json
{
  "litters": 5602,
  "timestamp": "2025-08-13 11:47:00",
  "height_mm": 2000
}
```

| Champ | Description |
|---|---|
| `litters` | Volume en litres, tronqué à l'entier. *(le nom du champ contient une faute historique, conservée pour compatibilité)* |
| `timestamp` | Date locale `YYYY-MM-DD HH:MM:SS` — **chaîne**, pas un timestamp Unix. |
| `height_mm` | Hauteur brute en mm. |

> Renvoie une erreur `500` si la base est vide.

---

## `GET /data`

Renvoie **toutes** les mesures de la base, sans filtre ni lissage, sous forme d'un tableau de paires `[date locale, hauteur mm]` :

```json
[["2025-08-12 14:00:00", 2000], ["2025-08-12 14:01:00", 2001]]
```

> Endpoint historique. Il charge la base entière en mémoire (plusieurs centaines de milliers de points) — préférez `/api/measurements` avec `from`/`to`.

---

## `GET /`

Page HTML du graphe (pas une API). Accepte `from`, `to`, `n` — au format `YYYY-MM-DD HH:MM:SS` uniquement — ainsi que `display_mode` (`lines` par défaut). Par défaut : les 7 derniers jours avec `n=5`.

---

## `POST /upload_data`

Dépôt d'une mesure par le raspberry, une fois par minute. **Signé** : le serveur
n'accepte que les requêtes portant une signature HMAC valide, calculée avec la clé
partagée entre les deux machines (`AGENT_HMAC_KEY`).

```
X-Well-Ts:    <secondes unix>
X-Well-Nonce: <32 hex>
X-Well-Sig:   hex(HMAC-SHA256(clé,
                ts \n nonce \n POST \n /upload_data \n sha256hex(corps)))
```

Corps :

```json
{"timestamp": 1755000000, "height_mm": 2900}
```

| Champ | Description |
|---|---|
| `timestamp` | Timestamp Unix de la mesure. Distinct de `X-Well-Ts`, qui date la *requête* : un renvoi de backlog dépose une mesure ancienne dans une requête récente. |
| `height_mm` | Hauteur en mm, ou `null` si la sonde n'a rien renvoyé — cette absence est conservée telle quelle. |

Le message signé inclut la méthode, le chemin et le hash du corps : une signature ne
peut être rejouée sur un autre endpoint, ni la mesure modifiée en vol. L'horodatage est
toléré à ±120 s et le nonce est à usage unique. Chaque tentative de renvoi est
resignée — rejouer un nonce ferait rejeter une reprise après réponse perdue.

L'implémentation est partagée par les deux machines dans `wellsig.py`.

| Code | Cause |
|---|---|
| `400` | `timestamp` ou `height_mm` absent, ou `timestamp` non entier. |
| `401` | Signature absente, invalide, horodatage hors tolérance, ou nonce déjà utilisé. |

> `UPLOAD_REQUIRE_SIGNATURE=0` côté serveur accepte les dépôts non signés en journalisant
> un avertissement. C'est le mode de migration et le levier de retour arrière ; en
> production la variable vaut `1`.

---

## Arrosage — `/watering`

Groupe d'endpoints **authentifiés** qui pilotent la vanne LinkTap du jardin. Le serveur ne parle pas directement à la passerelle : il relaie vers un agent qui tourne sur le raspberry (voir [README.md](README.md)).

### Authentification : challenge-réponse par action

Un cookie de session seul serait rejouable par quiconque écoute la liaison. Ici la session ne sert qu'à afficher le panneau : **chaque action mutante exige un nonce frais à usage unique**, et le message signé lie l'action et ses paramètres — une preuve capturée pour `start|5` ne peut être rejouée ni en `stop`, ni en `start|120`.

```
clé   = SHA-256(mot_de_passe)
nonce = GET /watering/challenge?action=<login|start|stop>
proof = HMAC-SHA256(clé, nonce + "|" + action + "|" + params)
        params = "<minutes>" pour start, "" pour login et stop
POST  /watering/<action>  {"nonce": ..., "proof": ..., ...}
```

Le nonce est signé par le serveur, valable **120 secondes**, et consommé au premier usage. Comme il ne peut pas être lu depuis une autre origine, le CSRF est structurellement impossible : il n'y a pas de jeton CSRF.

Client complet :

```python
import hashlib, hmac, requests

BASE, PASSWORD = "https://well1d.somebod.com", "..."
KEY = hashlib.sha256(PASSWORD.encode()).digest()
s = requests.Session()

def call(action, params="", path=None, **extra):
    nonce = s.get(f"{BASE}/watering/challenge", params={"action": action}).json()["nonce"]
    proof = hmac.new(KEY, f"{nonce}|{action}|{params}".encode(), hashlib.sha256).hexdigest()
    return s.post(f"{BASE}{path or '/watering/' + action}",
                  json={"nonce": nonce, "proof": proof, **extra}).json()

call("login")                              # ouvre la session
call("start", "10", minutes=10)            # arrose 10 minutes
print(s.get(f"{BASE}/watering/state").json())
call("stop")
```

### `GET /watering`

Page HTML de pilotage (pas une API). Affiche le formulaire de mot de passe, ou le panneau si la session est ouverte.

### `GET /watering/challenge`

| Paramètre | Valeurs | Description |
|---|---|---|
| `action` | `login`, `start`, `stop` | Action à laquelle le nonce sera lié. |

Réponse `200` : `{"nonce": "...", "expires_in": 120}`. `login` est accessible sans session ; `start` et `stop` renvoient `401` sans session.

### `GET /watering/state`

État courant, pour le rafraîchissement automatique de la page. Nécessite une session.

```json
{
  "server_now": 1755000000,
  "current": {"id": 42, "status": "running", "duration_s": 600, "started_at": 1754999880,
              "planned_end": 1755000480, "started_label": "12/08/2025 14:38"},
  "remain_s": 480,
  "gateway": {"reachable": true, "is_watering": true, "battery": 87, "signal": 96, "age_s": 3},
  "foreign_watering": false,
  "can_start": false,
  "can_stop": true,
  "max_minutes": 120,
  "history": [ ... 20 derniers arrosages ... ]
}
```

`remain_s` provient du décompte de la vanne quand il est frais, sinon de `planned_end`. Le client doit le rebaser sur l'instant de réception, jamais sur son horloge locale.

`foreign_watering` signale une vanne ouverte sans arrosage correspondant en base : quelqu'un a lancé l'eau depuis l'appli LinkTap ou une programmation. Dans ce cas `can_start` est `false` et `can_stop` reste `true`.

### `POST /watering/start`

Corps : `{"nonce": ..., "proof": ..., "minutes": 10}` — 1 à **120** minutes.
Réponse `200` : le même objet que `/watering/state`.

### `POST /watering/schedule`

Corps : `{"nonce": ..., "proof": ..., "at": "07:30", "minutes": 15}`. `at` est une heure
locale `HH:MM` ; l'arrosage est fixé à sa prochaine occurrence, une seule fois. `params` de
la preuve vaut `"<at>|<minutes>"`.

Réponse `200` : le même objet que `/watering/state`, dont le champ `schedules` :

```json
"schedules": [{"id": 7, "at_ts": 1755066600, "at_label": "demain à 07:30", "duration_s": 900}]
```

Les programmations manquées restent visibles 48 h dans `missed`, avec leur motif.

### `POST /watering/unschedule`

Corps : `{"nonce": ..., "proof": ..., "id": 7}`. `params` de la preuve vaut l'identifiant.
Renvoie `404` si la programmation n'existe pas ou n'est plus en attente.

### `POST /watering/stop`

Corps : `{"nonce": ..., "proof": ...}`. Ne clôt l'arrosage en base **que si** la passerelle a acquitté la fermeture — sinon `502`, pour ne pas afficher « fermée » sur une vanne encore ouverte.

### Statuts d'une programmation

| `status` | Signification |
|---|---|
| `pending` | En attente de son heure. |
| `fired` | Déclenchée ; `run_id` pointe l'arrosage créé. |
| `missed` | Non déclenchée : plus de 15 min de retard, arrosage déjà en cours, ou vanne muette. |
| `cancelled` | Annulée depuis la page. |

### Statuts d'un arrosage

| `status` | Signification |
|---|---|
| `pending` | Créneau réservé, commande pas encore acquittée. Récupéré en `failed` après 60 s. |
| `running` | En cours. |
| `done` | Terminé à l'échéance prévue. |
| `stopped` | Interrompu avant l'échéance. |
| `failed` | La passerelle n'a pas pris la commande. |

| `stop_reason` | Signification |
|---|---|
| `expired` | Durée demandée écoulée. |
| `manual` | Bouton « Arrêter ». |
| `gateway_off` | La vanne s'est fermée d'elle-même (appli LinkTap, pile faible…). |
| `agent_error` | Échec de la commande, ou réservation orpheline récupérée. |

### Codes d'erreur

| Code | Cause |
|---|---|
| `400` | Durée absente, non entière, ou hors de 1–120 minutes. |
| `401` | Pas de session, mot de passe invalide, nonce expiré, nonce déjà utilisé, ou nonce émis pour une autre action. |
| `409` | Un arrosage est déjà en cours, la vanne est déjà ouverte hors de l'application, ou une programmation existe déjà à cette heure. |
| `429` | Plus de 8 échecs d'authentification en 10 minutes depuis la même IP. |
| `502` | L'agent ou la passerelle n'a pas confirmé la commande. |
