# Well 1D

Mesure du niveau d'eau d'une citerne, et pilotage de l'arrosage du jardin.

## Architecture

```
                    ┌─ well.py ─────────── capteur /dev/ttyACM0, 1 mesure/min
raspberry (pi) ─────┤                      POST https://well1d.somebod.com/upload_data
10.15.8.27          └─ water_agent.py ──── relais vers la vanne LinkTap
   │                                        (le seul à voir les deux réseaux)
   │  LAN Freebox                                    │ VPN
   ▼                                                 ▼
LinkTap GW-02 192.168.1.7              serveur well1d.somebod.com (10.15.8.1)
   │                                     Apache → docker → server.py (gunicorn)
   ▼                                       ├─ /            graphe du niveau
vanne G1-S                                 └─ /watering    pilotage de l'arrosage
```

Le conteneur ne peut pas joindre `192.168.1.7` : la passerelle LinkTap est sur le LAN
Freebox. Le raspberry est le seul point à cheval sur les deux réseaux, d'où l'agent relais.

## Fichiers

| Fichier | Hôte | Rôle |
|---|---|---|
| `well.py` | raspberry | Lit le capteur chaque minute et POSTe la mesure. Stocke dans `failed_uploads.db` si le réseau est coupé. |
| `linktap.py` | raspberry | Client de la passerelle LinkTap (cmd 3 statut, 6 démarrage, 7 arrêt). |
| `water.py` | raspberry | CLI d'arrosage au-dessus de `linktap.py`. |
| `water_agent.py` | raspberry | Agent HTTP signé HMAC appelé par le serveur via le VPN. |
| `server.py` | serveur | Serveur Flask : collecte, API de mesures, page du graphe. |
| `watering.py` | serveur | Blueprint `/watering` : auth, machine à états, client de l'agent. |
| `detect_changes.py` | serveur | Alertes Pushover (pas de données, pluie, extrema). |
| `server_failed_uploads.py` | serveur | Importe le `failed_uploads.db` rapatrié du raspberry. |

> `water.py` (CLI raspberry) et `watering.py` (module serveur) se ressemblent mais n'ont
> rien à voir. Les ranger dans un sous-dossier `raspberry/` casserait le déploiement par
> `git pull` du pi, dont `well.service` code en dur `/home/pi/well1d/well.py`.

## API

Le serveur expose une API HTTP documentée dans [API.md](API.md) — publique pour les
mesures, protégée par mot de passe pour l'arrosage. L'endpoint principal est
`GET /api/measurements?from=...&to=...&n=...`.

## Installation — serveur

* Créer un environnement virtuel en python 3.11, `pip install -r requirements.txt`
* Copier `.env.example` en `.env` et renseigner les cinq secrets (les commandes de
  génération sont en commentaire dans le fichier). En particulier :

```bash
openssl rand -hex 32                            # SECRET_KEY
openssl rand -base64 18                         # le mot de passe d'arrosage à retenir
printf %s 'LE_MOT_DE_PASSE' | shasum -a 256     # WATERING_PASSWORD_SHA256
openssl rand -hex 32                            # AGENT_HMAC_KEY (identique côté raspberry)
```

* `mkdir -p data` (le mount de répertoire de `watering.db` ; sinon docker le crée en root)
* `docker compose up -d --build`
* On peut télécharger une copie de la DB de prod : `wget http://agaru.familinkframe.com/static/well.db`
* En local : `SECRET_KEY=dev COOKIE_SECURE=0 WATERING_DB=data/watering.db python server.py`

`SECRET_KEY` doit rester **stable** : le changer déconnecte tout le monde et invalide les
nonces en circulation. C'est le bouton panique en cas de fuite du mot de passe.

## Installation — agent d'arrosage sur le raspberry

```bash
sudo install -m 0640 -o root -g pi /dev/stdin /etc/well-agent.env <<'EOF'
AGENT_HMAC_KEY=<la même valeur que dans le .env du serveur>
EOF
sudo cp sysop/water-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now water-agent
curl -sS http://127.0.0.1:8787/health
```

L'agent écoute sur `0.0.0.0:8787` : binder l'IP VPN échouerait au démarrage, avant que
wireguard soit monté — et le pi reboote tous les matins à 4 h. Ce qui protège l'agent,
c'est la signature HMAC obligatoire et le filtrage de l'IP source sur `10.15.8.0/24`.

Vérifier que le conteneur atteint bien l'agent :

```bash
docker compose exec well1d curl -sS --max-time 5 http://10.15.8.27:8787/health
```

Si ça échoue, c'est un problème de routage docker → VPN, pas de code :
`sudo iptables -I DOCKER-USER -s 172.17.0.0/16 -d 10.15.8.0/24 -j ACCEPT`. `AGENT_URL`
est une variable d'environnement précisément pour pouvoir passer par un relais côté hôte
sans toucher au code.

Réglages requis sur la page d'admin de la passerelle : **Local HTTP API** activé.
Le code accepte la réponse enveloppée en HTML comme la réponse JSON nue, donc le réglage
« Wrap the gateway's response in HTML » n'a pas d'importance — utile, car un reset usine
le réactive silencieusement.

## Tests

```bash
python -m unittest test_watering -v
```

Couvre l'authentification, l'anti-rejeu, le verrou d'unicité sous concurrence et la
finalisation paresseuse. Aucun accès réseau : l'agent est simulé.

## Points jamais transmis

Etant donné que le raspberry est connecté en wifi de façon assez instable, les points ne
peuvent pas toujours être transmis. Quand le raspberry ne peut pas transmettre ses points,
il stocke les points non transmis dans un fichier `failed_uploads.db`. Il faut
régulièrement envoyer ce fichier sur le serveur par ssh puis, côté serveur, lancer la
commande `python server_failed_uploads.py`
EDIT 2025-07-16 : ça n'est plus trop le cas maintenant que le raspberry est connecté en ethernet

## Dette connue

* **`POST /upload_data` n'est pas authentifié** et `water_height` n'a pas de clé
  primaire : n'importe qui peut empoisonner le graphe de façon irréversible. Migration en
  trois temps : accepter signé-ou-non, signer côté `well.py` avec le même schéma HMAC que
  l'agent, puis basculer en signé-seulement. Deux mitigations immédiates et indépendantes :
  rejeter un `timestamp` à plus de ±1 jour et un `height_mm` hors `0..3200`.
* **Le vhost 443 n'est pas dans le dépôt** — `sysop/well1d.conf` ne décrit que le `:80`.
  L'y committer, avec `RequestHeader set X-Forwarded-Proto https` : le `ProxyFix` de
  `server.py` en dépend pour voir la vraie IP cliente (sinon le rate-limiting bannit tout
  le monde au premier essai raté).
* **`well.db` est monté fichier par fichier** (`./well.db:/app/well.db`) : son journal
  SQLite atterrit dans le conteneur, invisible des scripts hôte, donc une écriture
  interrompue en plein commit n'est pas récupérable proprement. `watering.db` est pour
  cette raison derrière un mount de répertoire (`./data`) ; `well.db` devrait suivre.
  Corollaire : **jamais de `PRAGMA journal_mode=WAL` sur `well.db`**.
