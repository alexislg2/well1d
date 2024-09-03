# Well 1D

Ce repo contient 2 fichiers sources :

* Le code exécuté sur le raspberry `well.py`. Ce script est chargé de faire une mesure par seconde et de l'envoyer sur un serveur via une requête HTTPS
* Le code éxécuté côté serveur `server.py`. Ce code en flask fait tourner un serveur web chargé de récupérer les données envoyées chaque minute. Le code permet également l'affichage des données pour les visiteurs.

## Installation

* Créer un environnement virtuel en python 3.11
* `pip install -r requirements.txt`
* On peut télécharger une copie de la DB de prod : `wget http://agaru.eu/static/well.db`
* `python server.py` pour faire tourner en local


## Points jamais transmis

Etant donné que le raspberry est connecté en wifi de façon assez instable, les points ne peuvent pas toujours être transmis. Quand le raspberry ne peut pas transmettre ses points, il stocke les points non transmis dans un fichier `failed_uploads.db`. Il faut régulièrement envoyer ce fichier sur le serveur par ssh puis, côté serveur, lancer la commande `python server_failed_uploads.py`
