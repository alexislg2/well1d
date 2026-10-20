#!/bin/bash
# Installé dans /usr/local/bin/ du raspberry, lancé toutes les 10 min par la
# crontab root (voir crontab-raspberry.txt).
#
# Le Pi est dans une boîte étanche : au soleil, l'effet de serre peut l'amener au
# bridage (80 °C). Alerte Pushover à l'entrée en surchauffe ou en sous-tension,
# puis une seule fois au retour à la normale.
#
# Identifiants dans /etc/well-pushover.env (root, 600), hors du dépôt :
#   PUSHOVER_TOKEN=...
#   PUSHOVER_USER=...
#
# L'état d'alerte est dans /run (tmpfs) pour ne pas écrire sur la carte SD : après
# un reboot en pleine surchauffe, l'alerte repart, ce qui est voulu.

STATE=/run/check-temp.alert
MAX_TEMP=${MAX_TEMP:-70}

temp=$(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))
throttled=$(vcgencmd get_throttled | cut -d= -f2)

# Bits 0-3 : état courant (sous-tension, fréquence plafonnée, bridage, limite
# douce). Les bits 16-19 (« déjà arrivé depuis le boot ») alerteraient sans fin.
problems=()
[ "$temp" -ge "$MAX_TEMP" ] && problems+=("température ${temp} °C")
(( throttled & 0x1 )) && problems+=("sous-tension")
(( throttled & 0xE )) && problems+=("bridage (throttled=$throttled)")

logger -t check-temp "${temp} °C throttled=$throttled"

notify() {
    # shellcheck source=/dev/null
    . /etc/well-pushover.env
    curl -s --max-time 20 \
        --form-string "token=$PUSHOVER_TOKEN" \
        --form-string "user=$PUSHOVER_USER" \
        --form-string "priority=$1" \
        --form-string "message=$2" \
        https://api.pushover.net/1/messages.json >/dev/null
}

if [ ${#problems[@]} -gt 0 ]; then
    if [ ! -e "$STATE" ]; then
        msg="zero1d : $(IFS=,; echo "${problems[*]}" | sed 's/,/, /g')"
        logger -t check-temp "alerte : $msg"
        notify 1 "$msg" && touch "$STATE"
    fi
elif [ -e "$STATE" ]; then
    notify 0 "zero1d : retour à la normale (${temp} °C)" && rm -f "$STATE"
fi
