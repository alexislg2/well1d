#!/bin/bash
# Installé dans /usr/local/bin/ du raspberry, lancé toutes les 10 min par la
# crontab root (voir crontab-raspberry.txt).
#
# On ne surveille que la box (passerelle par défaut, Ethernet ou Wi-Fi) : une
# panne d'internet seule ne doit pas faire redémarrer, car sans NTP l'horloge
# repartirait de la dernière sauvegarde fake-hwclock (jusqu'à 1 h de retard) et
# les mesures partiraient avec des timestamps faux.
#
# Compteur d'échecs dans /run (tmpfs) : aucune écriture sur la carte SD, remis à
# zéro au démarrage. Le nombre de reboots consécutifs, lui, doit survivre au
# reboot : il est sur la carte, mais n'est écrit qu'au moment de redémarrer.
# Au-delà de MAX_REBOOTS, la box est sans doute morte : on se contente de relancer
# le réseau, ce qui reconnecte le Pi dès qu'elle revient.

FAILS=/run/check-network.fails
MAX_FAILS=3
REBOOTS=/var/lib/check-network/reboots
MAX_REBOOTS=2

gw=$(ip -4 route show default | awk '{print $3; exit}')
if [ -n "$gw" ] && ping -c3 -W3 "$gw" >/dev/null 2>&1; then
    rm -f "$FAILS"
    [ -e "$REBOOTS" ] && rm -f "$REBOOTS"
    exit 0
fi

n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAILS"
r=$(cat "$REBOOTS" 2>/dev/null || echo 0)

if [ "$n" -ge "$MAX_FAILS" ] && [ "$r" -lt "$MAX_REBOOTS" ]; then
    logger -t check-network "box injoignable depuis $n contrôles, redémarrage $((r + 1))/$MAX_REBOOTS"
    mkdir -p "$(dirname "$REBOOTS")"
    echo $((r + 1)) > "$REBOOTS"
    sync
    /usr/sbin/reboot
elif [ "$n" -ge "$MAX_FAILS" ]; then
    logger -t check-network "box toujours injoignable après $r redémarrages, relance du réseau seulement"
    systemctl restart dhcpcd
else
    logger -t check-network "box injoignable ($n/$MAX_FAILS), relance du réseau"
    systemctl restart dhcpcd
fi
