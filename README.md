# Well 1D

Ce repo contient 2 fichiers sources :

* Le code exécuté sur le raspberry `well.py`. Ce script est chargé de faire une mesure par seconde et de l'envoyer sur un serveur via une requête HTTPS
* Le code éxécuté côté serveur `server.py`. Ce code en flask fait tourner un serveur web chargé de récupérer les données envoyées chaque minute. Le code permet également l'affichage des données pour les visiteurs.

## Installation

* Créer un environnement virtuel en python 3.11
* `pip install -r requirements.txt`
* On peut télécharger une copie de la DB de prod : `wget http://agaru.eu/static/well.db``
* `python server.py` pour faire tourner en local
