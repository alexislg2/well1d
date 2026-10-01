# Remplacer la carte micro SD du raspberry (zero1d)

Image de référence : `well1d-sd-2026-09-29.img.xz` (529 Mo), tirée le 27/09/2026 de
la carte d'origine (Raspbian 11 bullseye, hostname `zero1d`, utilisateur `pi`) puis
réparée le 28/09/2026 : la carte d'origine s'usait et avait abîmé ~180 fichiers
système, remplacés par des copies vérifiées contre les empreintes dpkg (`sudo dpkg
--verify | grep -v ' c /etc/'` ne signale plus rien). Elle démarre
sur Pi Zero comme sur Pi 3.

Réglée le 29/09/2026 pour écrire le moins possible sur la carte (usure et coupures
de courant) : journal systemd en RAM (`/etc/systemd/journald.conf.d/volatile.conf`),
rsyslog (et sa config `/etc/logrotate.d/rsyslog`, dont le postrotate échouerait sans
lui), swap (`dphys-swapfile`) et timers apt/man-db désactivés, sortie de well.py
dans le journal (`journalctl -u well`).
Watchdog matériel actif (`/etc/systemd/system.conf.d/watchdog.conf`) : si le Pi se
fige, il redémarre seul. `check-network.sh` (cron root, toutes les 10 min) relance
le réseau si la box ne répond plus, redémarre au bout de 30 min, 2 fois maximum
d'affilée ; une panne d'internet seule ne fait pas redémarrer. Mot de passe de `pi` : **pi**, à changer
(`passwd`) dès le premier démarrage.

Elle contient tout : well.py, water_agent.py, les services systemd, la clé HMAC
(`/etc/well-agent.env`), la config Wi-Fi, le tunnel WireGuard et la crontab.

⚠️ L'image contient les mots de passe Wi-Fi, la clé HMAC et la clé privée
WireGuard : ne pas la stocker dans un endroit public.

## 1. Acheter la carte

- micro SD **8 Go minimum** (l'image fait 4 Go).
- De préférence **MLC / industrielle / haute endurance** (ex. Gigastone Industriel
  MLC) : bien plus robuste qu'une carte grand public (TLC) sur un appareil allumé en
  permanence et soumis à des coupures de courant.
- Une carte doit rester tiède dans le raspberry : brûlante = problème (alimentation,
  raspberry). En septembre 2026 une SanDisk 64 Go neuve est morte en quelques heures
  dans le Pi 3 (retirée à chaud alors qu'elle chauffait).
- Ne jamais retirer la carte raspberry allumé.

## 2. Vérifier l'image

```
cd <dossier de l'image>
shasum -a 256 -c well1d-sd-2026-09-29.img.xz.sha256
```

Doit afficher `well1d-sd-2026-09-29.img.xz: OK`.

## 3. Écrire l'image sur la carte

### Option A — Raspberry Pi Imager (le plus simple)

1. Appareil : n'importe lequel. Système : **Use custom** → choisir `well1d-sd-2026-09-29.img.xz`.
2. Stockage : la carte SD.
3. À la question « personnaliser l'OS ? » répondre **NON / Pas de personnalisation**
   (sinon Imager écrase hostname, utilisateur et Wi-Fi de l'image).
4. Écrire, attendre la vérification, éjecter.

### Option B — en ligne de commande (dans l'app Terminal, pas dans Claude Code)

```
diskutil list external            # repérer la carte, ex. disk17 (vérifier la taille !)
diskutil unmountDisk disk17
xz -dc well1d-sd-2026-09-29.img.xz | sudo dd of=/dev/rdisk17 bs=4m status=progress
sync && diskutil eject disk17
```

Si `dd` répond « Operation not permitted » : Réglages Système › Confidentialité
et sécurité › Accès complet au disque › activer Terminal, puis relancer Terminal.

## 4. Premier démarrage

1. Mettre la carte dans le raspberry, brancher le capteur (USB, `/dev/ttyACM0`),
   brancher l'alimentation.
2. Attendre ~2 min, puis se connecter :
   - depuis le Mac ou iot, via le tunnel WireGuard (`wg0`, dans l'image) : `ssh zero1d`
     (alias vers `pi@10.15.8.27` dans `~/.ssh/config`) ;
   - sur le réseau local si le tunnel ne monte pas : `ssh pi@zero1d.local`.
3. Agrandir la partition à toute la carte :
   ```
   sudo raspi-config --expand-rootfs && sudo reboot
   ```
   Vérifier après reboot : `df -h /` doit montrer la taille de la carte.
   Juste après l'agrandissement, ext4 initialise l'espace ajouté en tâche de fond
   (`ext4lazyinit`, beaucoup d'écritures pendant quelques heures) : c'est normal.
4. Changer le mot de passe : `passwd`.

Le raspberry n'a pas d'horloge (RTC) : l'heure n'est juste qu'une fois le réseau
joint. Le démarrer **avec le Wi-Fi disponible**, sinon les mesures partent avec des
timestamps faux (et `check-network.sh` le fait redémarrer toutes les 10 min).

Si le Wi-Fi de la maison a changé de nom ou de mot de passe : éditer
`/etc/wpa_supplicant/wpa_supplicant.conf` (réseau `Maison 1D`). Sans réseau, on
peut le faire depuis le Mac avant le premier démarrage… sauf que la partition est
en ext4 : plus simple de brancher le raspberry sur un écran + clavier.

## 5. Vérifier que tout marche

Sur le raspberry :
```
date                                   # heure correcte ?
systemctl status well water-agent      # les deux « active (running) »
ls -l /dev/ttyACM0                     # capteur vu ?
```

Le service well attend 60 s avant de démarrer (`ExecStartPre=/bin/sleep 60`).

Côté serveur, les mesures doivent arriver toutes les minutes :
```
ssh iot 'sqlite3 -readonly ~/well1d/data/well.db "select datetime(timestamp,\"unixepoch\",\"localtime\"), height_mm from water_height order by timestamp desc limit 3"'
```

## 6. Après la remise en route

- Refaire une image si des choses ont changé sur le raspberry depuis le 29/09/2026
  (mêmes étapes que pour la création : copie brute avec `dd`, puis réduction avec
  e2fsprogs `resize2fs -M` et compression xz).
- Si `well.py` a été modifié dans le dépôt depuis, le redéployer sur le raspberry.
