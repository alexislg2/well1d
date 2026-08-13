# API Well 1D

API HTTP du serveur `server.py` qui expose les mesures de hauteur d'eau de la citerne.

* **Base URL (prod)** : `https://well1d.somebod.com`
* **Base URL (local)** : `http://127.0.0.1:5000`
* **Authentification** : aucune. Tous les endpoints sont publics.
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

## `POST /upload_data`

Utilisé par le raspberry (`well.py`) pour pousser une mesure. Sans authentification : n'importe qui peut insérer un point.

### Corps de la requête

```json
{ "timestamp": 1755000000, "height_mm": 2000 }
```

| Champ | Type | Description |
|---|---|---|
| `timestamp` | entier | Timestamp Unix en secondes. |
| `height_mm` | entier ou `null` | Hauteur mesurée. `null` est accepté et stocké (capteur muet), puis filtré à la lecture. |

### Réponse

`200` avec le corps texte `Data received`. Aucune déduplication : deux appels avec le même timestamp créent deux lignes.

---

## `GET /`

Page HTML du graphe (pas une API). Accepte `from`, `to`, `n` — au format `YYYY-MM-DD HH:MM:SS` uniquement — ainsi que `display_mode` (`lines` par défaut). Par défaut : les 7 derniers jours avec `n=5`.
