"""
Pilotage de l'arrosage depuis le web : page /watering.

Le conteneur ne peut pas joindre la passerelle LinkTap (LAN Freebox) ; il passe
par `water_agent.py` sur le raspberry, joignable par le VPN.

Authentification : mot de passe unique, en challenge-réponse **par action**. Un
cookie de session seul serait rejouable par quiconque écoute ; ici chaque
démarrage ou arrêt exige un nonce frais à usage unique, et le message signé lie
l'action et sa durée. La session ne sert qu'à afficher le panneau.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, time as clock_time, timedelta

import pytz
import requests
from flask import Blueprint, current_app, jsonify, render_template, request, session
from itsdangerous import BadSignature, URLSafeTimedSerializer

import wellsig

watering_bp = Blueprint('watering', __name__)
log = logging.getLogger(__name__)

WATERING_DB = os.environ.get('WATERING_DB', 'data/watering.db')
AGENT_URL = os.environ.get('AGENT_URL', 'http://10.15.8.27:8787').rstrip('/')
AGENT_HMAC_KEY = os.environ.get('AGENT_HMAC_KEY', '').encode()
PASSWORD_SHA256 = os.environ.get('WATERING_PASSWORD_SHA256', '').strip().lower()

MAX_MINUTES = 120
# Débit de l'arrosage, en litres par minute de vanne ouverte. Il n'y a pas de
# compteur : la valeur est déduite de la chute de niveau de la citerne pendant
# les arrosages tracés (voir README). Surchargeable sans rebuild, parce qu'elle
# changera au premier goutteur ajouté ou au premier réglage de la vanne.
LITERS_PER_WATERING_MINUTE = float(os.environ.get('LITERS_PER_WATERING_MINUTE', '4.2'))
NONCE_TTL = 120
NONCE_SALT = 'well1d-watering-nonce'
AGENT_TIMEOUT = (2, 6)
ORPHAN_TTL = 60           # une réservation non finalisée au-delà est un worker mort
GATEWAY_LAG = 20          # la GW-02 met quelques secondes à refléter cmd 6
# La vanne dort pour économiser ses piles et n'écoute la passerelle qu'à
# intervalles réguliers, de l'ordre de la minute : un cmd 6 acquitté met un
# cycle de réveil à être délivré (19 s mesurées le 24/08/2026). Au-delà de ce
# délai, une vanne qui rapporte encore l'arrosage précédent n'a jamais reçu le
# nôtre — la passerelle a acquitté un ordre qu'elle n'a pas su délivrer.
CONFIRM_DEADLINE = 150    # un cycle de réveil, large
SETTLE_WINDOW = 900       # au-delà, on cesse de sonder pour un run sans verdict
POLL_TTL_ACTIVE = 5
POLL_TTL_IDLE = 20        # < la cadence de sondage du navigateur au repos (60 s)
MAX_AUTH_FAILURES = 8
AUTH_WINDOW = 600
HISTORY_LIMIT = 20
SCHEDULE_GRACE = 900      # au-delà, un arrosage programmé manqué ne part plus
MISSED_WINDOW = 172800    # les manqués restent affichés 48 h

local_timezone = pytz.timezone('Europe/Paris')

CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'self'"


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS watering_run (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  requested_at  INTEGER NOT NULL,
  started_at    INTEGER,
  planned_end   INTEGER,
  ended_at      INTEGER,
  duration_s    INTEGER NOT NULL,
  status        TEXT NOT NULL,
  active        INTEGER,
  stop_reason   TEXT,
  source        TEXT NOT NULL DEFAULT 'web',
  gateway_ret   INTEGER,
  error         TEXT,
  client_ip     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_watering_active
  ON watering_run(active) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_watering_requested ON watering_run(requested_at DESC);

CREATE TABLE IF NOT EXISTS watering_schedule (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at  INTEGER NOT NULL,
  at_ts       INTEGER NOT NULL,
  duration_s  INTEGER NOT NULL,
  status      TEXT NOT NULL,      -- pending | fired | missed | cancelled
  run_id      INTEGER,
  reason      TEXT,
  client_ip   TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedule_at ON watering_schedule(at_ts);
-- deux programmations à la même minute n'auraient aucun sens
CREATE UNIQUE INDEX IF NOT EXISTS ux_schedule_pending
  ON watering_schedule(at_ts) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS used_nonce (
  nonce_hash TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nonce_exp ON used_nonce(expires_at);

CREATE TABLE IF NOT EXISTS auth_failure (ts INTEGER NOT NULL, ip TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_auth_failure ON auth_failure(ts);

CREATE TABLE IF NOT EXISTS gateway_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  polled_at INTEGER NOT NULL, reachable INTEGER NOT NULL,
  is_watering INTEGER, remain_s INTEGER, total_s INTEGER,
  battery INTEGER, signal INTEGER, rf_linked INTEGER, raw TEXT, error TEXT
);
INSERT OR IGNORE INTO gateway_state (id, polled_at, reachable) VALUES (1, 0, 0);
"""


def connect():
    conn = sqlite3.connect(WATERING_DB, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, ddl):
    """CREATE TABLE IF NOT EXISTS ne fait rien sur une table déjà créée : une
    colonne ajoutée après coup a besoin de son propre ALTER."""
    existantes = {r['name'] for r in conn.execute("PRAGMA table_info({})".format(table))}
    if column not in existantes:
        conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, ddl))


def init_db():
    """Appelé à l'import : create_database() de server.py ne tourne que sous
    __main__, donc jamais sous gunicorn."""
    directory = os.path.dirname(os.path.abspath(WATERING_DB))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        ensure_column(conn, 'gateway_state', 'rf_linked', 'INTEGER')
        ensure_column(conn, 'watering_run', 'confirmed_at', 'INTEGER')
        ensure_column(conn, 'watering_run', 'pre_total_s', 'INTEGER')
        ensure_column(conn, 'watering_run', 'pre_remain_s', 'INTEGER')
    finally:
        conn.close()


def sweep(conn, now):
    """Finalisation paresseuse : évite tout cron ou thread de fond. Appelée en
    tête de chaque transaction qui lit ou écrit l'état."""
    conn.execute(
        "UPDATE watering_run SET status='done', active=NULL, ended_at=planned_end,"
        " stop_reason='expired'"
        " WHERE active=1 AND status='running' AND planned_end <= ?", (now,))
    conn.execute(
        "UPDATE watering_run SET status='failed', active=NULL, ended_at=?,"
        " stop_reason='agent_error', error=COALESCE(error,'réservation orpheline')"
        " WHERE active=1 AND status='pending' AND requested_at < ?",
        (now, now - ORPHAN_TTL))


def active_run(conn):
    return conn.execute("SELECT * FROM watering_run WHERE active = 1").fetchone()


def history(conn):
    return conn.execute(
        "SELECT * FROM watering_run ORDER BY requested_at DESC LIMIT ?",
        (HISTORY_LIMIT,)).fetchall()


def reserve_run(conn, now, duration_s, source, ip):
    """Réserve le créneau atomiquement entre les 16 workers. Retourne l'id du
    run, ou None si un arrosage est déjà en cours. L'index unique partiel
    ux_watering_active est la vraie garantie : même un bug futur ne peut pas
    produire deux runs actifs."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        sweep(conn, now)
        if active_run(conn):
            conn.execute("ROLLBACK")
            return None
        cursor = conn.execute(
            "INSERT INTO watering_run (requested_at, duration_s, status, active, source, client_ip)"
            " VALUES (?, ?, 'pending', 1, ?, ?)", (now, duration_s, source, ip))
        conn.execute("COMMIT")
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return None


# --------------------------------------------------------------------------
# Authentification
# --------------------------------------------------------------------------

def serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=NONCE_SALT)


def client_ip():
    return request.remote_addr or '?'


def rate_limited(conn, now, ip):
    conn.execute("DELETE FROM auth_failure WHERE ts < ?", (now - 3600,))
    count, = conn.execute(
        "SELECT COUNT(*) FROM auth_failure WHERE ip = ? AND ts > ?",
        (ip, now - AUTH_WINDOW)).fetchone()
    return count >= MAX_AUTH_FAILURES


def record_failure(conn, now, ip):
    conn.execute("INSERT INTO auth_failure (ts, ip) VALUES (?, ?)", (now, ip))


def burn_nonce(conn, now, nonce):
    """L'INSERT *est* le test : une IntegrityError signale un rejeu, de façon
    atomique entre les 16 workers gunicorn."""
    conn.execute("DELETE FROM used_nonce WHERE expires_at < ?", (now,))
    try:
        conn.execute("INSERT INTO used_nonce (nonce_hash, expires_at) VALUES (?, ?)",
                     (hashlib.sha256(nonce.encode()).hexdigest(), now + NONCE_TTL))
    except sqlite3.IntegrityError:
        return False
    return True


def consume_nonce(nonce):
    """Consomme un nonce dans la table partagée, hors de toute transaction en
    cours. Utilisé par /upload_data, qui n'a pas de transaction à lui."""
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = burn_nonce(conn, now, nonce)
        conn.execute("COMMIT")
        return fresh
    finally:
        conn.close()


def expected_proof(nonce, action, params):
    key = bytes.fromhex(PASSWORD_SHA256)
    message = '{}|{}|{}'.format(nonce, action, params).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(action, params, payload):
    """Retourne None si la preuve est valide, sinon (code, message)."""
    nonce = payload.get('nonce')
    proof = payload.get('proof')
    if not isinstance(nonce, str) or not isinstance(proof, str):
        return 400, "requête incomplète"

    now = int(time.time())
    ip = client_ip()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if rate_limited(conn, now, ip):
            conn.execute("COMMIT")
            log.warning("arrosage: trop de tentatives depuis %s", ip)
            return 429, "trop de tentatives, réessayez dans quelques minutes"

        try:
            claim = serializer().loads(nonce, max_age=NONCE_TTL)
        except BadSignature:
            record_failure(conn, now, ip)
            conn.execute("COMMIT")
            return 401, "nonce invalide ou expiré"

        if claim.get('a') != action:
            record_failure(conn, now, ip)
            conn.execute("COMMIT")
            return 401, "nonce émis pour une autre action"

        if not burn_nonce(conn, now, nonce):
            conn.execute("COMMIT")
            log.warning("arrosage: rejeu de nonce depuis %s sur %s", ip, action)
            return 401, "nonce déjà utilisé"

        if not hmac.compare_digest(expected_proof(nonce, action, params), proof):
            record_failure(conn, now, ip)
            conn.execute("COMMIT")
            log.warning("arrosage: preuve invalide depuis %s sur %s", ip, action)
            return 401, "mot de passe invalide"

        conn.execute("COMMIT")
    finally:
        conn.close()
    return None


def authed():
    return bool(session.get('watering_auth'))


# --------------------------------------------------------------------------
# Agent raspberry
# --------------------------------------------------------------------------

def agent_call(path, payload=None):
    """Retourne (ok, réponse_json, erreur). Ne lève jamais : la page doit
    s'afficher même agent mort."""
    body = json.dumps(payload or {}).encode()
    headers = {'Content-Type': 'application/json'}
    headers.update(wellsig.sign(AGENT_HMAC_KEY, 'POST', path, body))
    try:
        resp = requests.post(AGENT_URL + path, data=body, headers=headers,
                             timeout=AGENT_TIMEOUT)
    except requests.RequestException as e:
        log.warning("arrosage: agent injoignable (%s) : %s", path, e)
        return False, None, "agent injoignable"

    try:
        data = resp.json()
    except ValueError:
        return False, None, "réponse agent illisible ({})".format(resp.status_code)
    if not data.get('ok'):
        return False, data, data.get('error') or data.get('message') or "échec agent"
    return True, data, None


def refresh_gateway(conn, now, max_age):
    """Sonde la vanne si le cache partagé est périmé, puis réconcilie l'état de
    la base avec l'état réel. Le cache évite que 16 workers × N onglets ne
    deviennent autant de transactions RF sur une vanne à pile."""
    row = conn.execute("SELECT * FROM gateway_state WHERE id = 1").fetchone()
    if now - row['polled_at'] < max_age:
        return row

    ok, data, error = agent_call('/status')

    conn.execute("BEGIN IMMEDIATE")
    sweep(conn, now)
    previous = conn.execute("SELECT * FROM gateway_state WHERE id = 1").fetchone()
    if ok:
        conn.execute(
            "UPDATE gateway_state SET polled_at=?, reachable=1, is_watering=?, remain_s=?,"
            " total_s=?, battery=?, signal=?, rf_linked=?, raw=?, error=NULL WHERE id=1",
            (now, int(bool(data.get('is_watering'))), data.get('remain_s'),
             data.get('total_s'), data.get('battery'), data.get('signal'),
             None if data.get('rf_linked') is None else int(bool(data.get('rf_linked'))),
             json.dumps(data.get('raw'))[:2000]))
        reconcile(conn, now, previous, data)
        confirm(conn, now, data)
        settle(conn, now, data)
    else:
        conn.execute(
            "UPDATE gateway_state SET polled_at=?, reachable=0, error=? WHERE id=1",
            (now, error))
    conn.execute("COMMIT")
    return conn.execute("SELECT * FROM gateway_state WHERE id = 1").fetchone()


def note_valve(conn, now, is_watering):
    """Après un cmd 6/7 acquitté, la vanne met quelques secondes à le refléter :
    la sonder tout de suite renverrait l'état d'avant. On inscrit donc l'état
    commandé dans le cache ; le sondage suivant (≤ 20 s) rétablit la vérité.
    À appeler dans une transaction ouverte par l'appelant."""
    conn.execute("UPDATE gateway_state SET is_watering=?, reachable=1, polled_at=? WHERE id=1",
                 (int(is_watering), now))


def reconcile(conn, now, previous, data):
    """La vanne fait autorité, mais avec deux garde-fous : un délai de grâce
    après le démarrage, et deux sondages concordants — la GW-02 rapporte
    is_watering=0 pendant quelques secondes après un cmd 6 accepté."""
    if data.get('is_watering'):
        return
    if not (previous['reachable'] and previous['is_watering'] == 0):
        return
    conn.execute(
        "UPDATE watering_run SET status='stopped', active=NULL, ended_at=?,"
        " stop_reason='gateway_off'"
        " WHERE active=1 AND status='running' AND started_at < ?",
        (now, now - GATEWAY_LAG))


# Un run sans accusé de réception : soit il attend encore son verdict, soit
# la vanne l'a exécuté sans qu'on l'ait vu. `stopped/gateway_off` en fait
# partie — c'est ainsi que reconcile() clôt un arrosage qui n'a jamais démarré.
UNCONFIRMED_RUNS = (
    "SELECT * FROM watering_run WHERE confirmed_at IS NULL"
    " AND started_at IS NOT NULL AND started_at > ?"
    " AND (status IN ('running', 'done', 'unconfirmed')"
    "      OR (status = 'stopped' AND stop_reason = 'gateway_off'))")


def receipt(run, data):
    """Ce que les compteurs de la vanne disent de l'ordre qu'on lui a passé.

    `total_duration` et `remain_duration` appartiennent à la vanne, pas à la
    passerelle : c'est le seul endroit du système où « l'ordre a réellement été
    exécuté » soit lisible. Le `ret=0` d'un cmd 6, lui, n'atteste que du parsing.

    True : elle l'a exécuté. False : elle rapporte encore autre chose, donc elle
    ne l'a jamais reçu. None : indiscernable — l'arrosage précédent avait la même
    durée et on ne l'a jamais vue couler. Dans ce cas on n'accuse pas."""
    total = data.get('total_s')
    if total is None:
        return None
    if total != run['duration_s']:
        return False
    if data.get('is_watering'):
        return True
    if run['pre_total_s'] is None:
        return None
    if run['pre_total_s'] != total:
        return True
    if run['pre_remain_s'] is None or data.get('remain_s') == run['pre_remain_s']:
        return None
    return True


def confirm(conn, now, data):
    """Pose l'accusé de réception dès qu'un sondage prouve que la vanne exécute
    l'ordre. À appeler dans la transaction ouverte par refresh_gateway."""
    for run in conn.execute(UNCONFIRMED_RUNS, (now - SETTLE_WINDOW,)).fetchall():
        if receipt(run, data) is not True:
            continue
        conn.execute("UPDATE watering_run SET confirmed_at=? WHERE id=?", (now, run['id']))
        if run['status'] == 'unconfirmed':
            conn.execute("UPDATE watering_run SET status='done', stop_reason='expired'"
                         " WHERE id=?", (run['id'],))


def settle(conn, now, data):
    """Tranche le sort d'un arrosage que la vanne n'a jamais accusé, passé le
    délai de réveil. Sans cela un ordre jamais délivré s'affiche « Terminé »."""
    if data.get('total_s') is None:
        return                    # passerelle sans compteurs : rien à juger
    for run in conn.execute(UNCONFIRMED_RUNS + " AND started_at < ?",
                            (now - SETTLE_WINDOW, now - CONFIRM_DEADLINE)).fetchall():
        verdict = receipt(run, data)
        if verdict is False:
            conn.execute(
                "UPDATE watering_run SET status='failed', active=NULL,"
                " ended_at=COALESCE(ended_at, ?), stop_reason='not_delivered', error=?"
                " WHERE id=?",
                (now, "la vanne n'a jamais accusé réception de l'ordre", run['id']))
            log.warning("arrosage: run %s jamais exécuté — vanne injoignable", run['id'])
        elif verdict is None and run['status'] == 'done':
            conn.execute("UPDATE watering_run SET status='unconfirmed' WHERE id=?", (run['id'],))


def launch(conn, duration_s, source, ip):
    """Réserve, commande la vanne, finalise. Retourne (ok, run_id, erreur).
    `run_id` vaut None si la réservation a été refusée : rien n'a été tenté.

    Jamais de transaction ouverte pendant l'appel réseau au raspberry — six
    secondes de verrou d'écriture bloqueraient tous les autres workers."""
    gateway = refresh_gateway(conn, int(time.time()), POLL_TTL_ACTIVE)
    if gateway['reachable'] and gateway['is_watering'] and not active_run(conn):
        return False, None, "la vanne est déjà ouverte (arrosage lancé hors de cette page)"

    run_id = reserve_run(conn, int(time.time()), duration_s, source, ip)
    if run_id is None:
        return False, None, "un arrosage est déjà en cours"

    ok, data, error = agent_call('/start', {'duration_s': duration_s})
    started = int(time.time())

    # Une tentative ratée reste dans l'historique : c'est exactement ce qu'on
    # veut voir en cas de pépin.
    conn.execute("BEGIN IMMEDIATE")
    if ok:
        # Photo des compteurs juste avant l'ordre : c'est elle qui permettra de
        # reconnaître l'accusé de réception de la vanne, y compris quand
        # l'arrosage précédent avait exactement la même durée.
        conn.execute(
            "UPDATE watering_run SET status='running', started_at=?, planned_end=?,"
            " gateway_ret=0, pre_total_s=?, pre_remain_s=? WHERE id=?",
            (started, started + duration_s,
             gateway['total_s'] if gateway['reachable'] else None,
             gateway['remain_s'] if gateway['reachable'] else None, run_id))
        note_valve(conn, started, True)
    else:
        conn.execute(
            "UPDATE watering_run SET status='failed', active=NULL, ended_at=?,"
            " stop_reason='agent_error', gateway_ret=?, error=? WHERE id=?",
            (started, (data or {}).get('ret'), (error or '')[:300], run_id))
    conn.execute("COMMIT")
    return ok, run_id, error


# --------------------------------------------------------------------------
# Programmation
# --------------------------------------------------------------------------

def next_occurrence(hour, minute, now):
    """Prochain instant où il sera h:m, heure de Paris — aujourd'hui s'il est
    encore devant, demain sinon. On repasse par une date naïve à chaque essai :
    remplacer l'heure d'un datetime déjà localisé conserverait le décalage UTC
    de l'ancienne heure, et se tromperait d'une heure aux changements d'heure."""
    today = datetime.fromtimestamp(now, local_timezone).date()
    for offset in (0, 1):
        naive = datetime.combine(today + timedelta(days=offset), clock_time(hour, minute))
        candidate = int(local_timezone.localize(naive).timestamp())
        if candidate > now + 30:
            return candidate
    raise ValueError("aucune occurrence trouvée")


def pending_schedules(conn):
    return conn.execute(
        "SELECT * FROM watering_schedule WHERE status='pending' ORDER BY at_ts").fetchall()


def recent_missed(conn, now):
    return conn.execute(
        "SELECT * FROM watering_schedule WHERE status='missed' AND at_ts > ?"
        " ORDER BY at_ts DESC LIMIT 5", (now - MISSED_WINDOW,)).fetchall()


def fire_due(now=None):
    """Déclenche les arrosages programmés dont l'heure est venue. Appelée
    chaque minute par cron : c'est le seul morceau du système qui tourne sans
    que personne ne regarde la page."""
    now = int(time.time()) if now is None else now
    fired, missed = [], []
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sweep(conn, now)
        due = conn.execute(
            "SELECT * FROM watering_schedule WHERE status='pending' AND at_ts <= ?"
            " ORDER BY at_ts", (now,)).fetchall()
        conn.execute("COMMIT")

        for row in due:
            if now - row['at_ts'] > SCHEDULE_GRACE:
                reason = "plus de {} min de retard".format(SCHEDULE_GRACE // 60)
                ok, run_id, error = False, None, reason
            else:
                ok, run_id, error = launch(conn, row['duration_s'], 'schedule', None)

            conn.execute("BEGIN IMMEDIATE")
            if ok:
                conn.execute("UPDATE watering_schedule SET status='fired', run_id=? WHERE id=?",
                             (run_id, row['id']))
                fired.append(row['id'])
            else:
                # Un run a pu être créé puis échouer : on le garde tracé.
                conn.execute("UPDATE watering_schedule SET status='missed', run_id=?, reason=?"
                             " WHERE id=?", (run_id, (error or '')[:200], row['id']))
                missed.append((row['id'], error))
            conn.execute("COMMIT")

            if ok:
                log.info("arrosage: programmation %s déclenchée, run %s", row['id'], run_id)
            else:
                log.warning("arrosage: programmation %s manquée — %s", row['id'], error)
    finally:
        conn.close()
    return fired, missed


def settle_pending(now=None):
    """Sonde la vanne tant qu'un arrosage attend son verdict. Appelée par le
    cron : sans elle, un arrosage jamais exécuté resterait affiché « Terminé »
    jusqu'à ce que quelqu'un ouvre la page."""
    now = int(time.time()) if now is None else now
    conn = connect()
    try:
        attente = conn.execute(
            "SELECT COUNT(*) AS n FROM watering_run WHERE confirmed_at IS NULL"
            " AND started_at IS NOT NULL AND started_at > ?"
            " AND (status IN ('running', 'done')"
            "      OR (status = 'stopped' AND stop_reason = 'gateway_off'))",
            (now - SETTLE_WINDOW,)).fetchone()['n']
        if attente:
            refresh_gateway(conn, now, 0)
    finally:
        conn.close()
    return attente


# --------------------------------------------------------------------------
# État présenté à la page
# --------------------------------------------------------------------------

def label(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, local_timezone).strftime('%d/%m/%Y %H:%M')


# Seuils arbitraires : LinkTap documente le signal comme un pourcentage mais
# n'en donne aucune échelle de lecture.
def signal_quality(percent):
    if percent is None:
        return None
    if percent >= 70:
        return "bon"
    if percent >= 40:
        return "moyen"
    return "faible"


def schedule_label(ts, now):
    jour = datetime.fromtimestamp(ts, local_timezone).date()
    aujourdhui = datetime.fromtimestamp(now, local_timezone).date()
    quand = {0: "aujourd'hui", 1: "demain"}.get((jour - aujourdhui).days)
    heure = datetime.fromtimestamp(ts, local_timezone).strftime('%H:%M')
    return "{} à {}".format(quand, heure) if quand else label(ts)


def run_to_dict(row):
    if row is None:
        return None
    actual = None
    if row['started_at'] and row['ended_at'] and row['stop_reason'] != 'not_delivered':
        actual = max(0, row['ended_at'] - row['started_at'])
    return {
        'id': row['id'],
        'status': row['status'],
        # Estimation, pas une mesure : elle suppose un débit constant et ne vaut
        # que pour un arrosage dont on sait qu'il a coulé, et combien de temps.
        'volume_l': None if actual is None else round(actual / 60 * LITERS_PER_WATERING_MINUTE),
        'stop_reason': row['stop_reason'],
        'source': row['source'],
        'requested_at': row['requested_at'],
        'started_at': row['started_at'],
        'planned_end': row['planned_end'],
        'ended_at': row['ended_at'],
        'duration_s': row['duration_s'],
        'actual_s': actual,
        'error': row['error'],
        'started_label': label(row['started_at'] or row['requested_at']),
    }


def build_state(max_age=None):
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sweep(conn, now)
        run = active_run(conn)
        conn.execute("COMMIT")

        if max_age is None:
            max_age = POLL_TTL_ACTIVE if run else POLL_TTL_IDLE
        gateway = refresh_gateway(conn, now, max_age)

        conn.execute("BEGIN IMMEDIATE")
        sweep(conn, now)
        run = active_run(conn)
        runs = history(conn)
        scheduled = pending_schedules(conn)
        missed = recent_missed(conn, now)
        conn.execute("COMMIT")
    finally:
        conn.close()

    current = run_to_dict(run)
    remain_s = None
    if current and current['planned_end']:
        remain_s = max(0, current['planned_end'] - now)
        # La vanne connaît son propre décompte : elle fait autorité si frais.
        if gateway['reachable'] and gateway['is_watering'] and gateway['remain_s']:
            remain_s = gateway['remain_s'] - (now - gateway['polled_at'])
            remain_s = max(0, remain_s)

    gateway_view = {
        'reachable': bool(gateway['reachable']),
        'is_watering': bool(gateway['is_watering']) if gateway['reachable'] else None,
        'battery': gateway['battery'],
        'signal': gateway['signal'],
        'signal_quality': signal_quality(gateway['signal']),
        'rf_linked': None if gateway['rf_linked'] is None else bool(gateway['rf_linked']),
        'polled_at': gateway['polled_at'],
        'age_s': max(0, now - gateway['polled_at']) if gateway['polled_at'] else None,
        'error': gateway['error'],
    }

    # Vanne ouverte sans run en base : démarrage hors application. On ne
    # fabrique pas de faux run, mais on refuse d'en démarrer un.
    foreign = bool(gateway['reachable'] and gateway['is_watering'] and current is None)

    return {
        'server_now': now,
        'current': current,
        'remain_s': remain_s,
        'gateway': gateway_view,
        'foreign_watering': foreign,
        'can_start': current is None and not foreign,
        'can_stop': current is not None or foreign,
        'max_minutes': MAX_MINUTES,
        'history': [run_to_dict(r) for r in runs],
        'schedules': [{
            'id': r['id'],
            'at_ts': r['at_ts'],
            'at_label': schedule_label(r['at_ts'], now),
            'duration_s': r['duration_s'],
        } for r in scheduled],
        'missed': [{
            'id': r['id'],
            'at_label': label(r['at_ts']),
            'duration_s': r['duration_s'],
            'reason': r['reason'],
        } for r in missed],
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@watering_bp.after_request
def add_csp(response):
    response.headers['Content-Security-Policy'] = CSP
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@watering_bp.route('/watering')
def watering_page():
    return render_template('watering.html',
                           authed=authed(),
                           max_minutes=MAX_MINUTES,
                           state=json.dumps(build_state()) if authed() else 'null')


@watering_bp.route('/watering/challenge')
def challenge():
    action = request.args.get('action', '')
    if action not in ('login', 'start', 'stop', 'schedule', 'unschedule'):
        return jsonify({'error': "action inconnue"}), 400
    if action != 'login' and not authed():
        return jsonify({'error': "session expirée"}), 401
    nonce = serializer().dumps({'r': secrets.token_urlsafe(16), 'a': action})
    return jsonify({'nonce': nonce, 'expires_in': NONCE_TTL})


@watering_bp.route('/watering/login', methods=['POST'])
def login():
    payload = request.get_json(silent=True) or {}
    failure = verify('login', '', payload)
    if failure:
        return jsonify({'error': failure[1]}), failure[0]
    session.permanent = True
    session['watering_auth'] = True
    log.info("arrosage: connexion depuis %s", client_ip())
    return jsonify(build_state())


@watering_bp.route('/watering/state')
def state():
    if not authed():
        return jsonify({'error': "session expirée"}), 401
    return jsonify(build_state())


@watering_bp.route('/watering/start', methods=['POST'])
def start():
    if not authed():
        return jsonify({'error': "session expirée"}), 401
    payload = request.get_json(silent=True) or {}

    try:
        minutes = int(payload.get('minutes'))
    except (TypeError, ValueError):
        return jsonify({'error': "durée invalide"}), 400
    if not 0 < minutes <= MAX_MINUTES:
        return jsonify({'error': "durée hors limites (1 à {} minutes)".format(MAX_MINUTES)}), 400

    failure = verify('start', str(minutes), payload)
    if failure:
        return jsonify({'error': failure[1]}), failure[0]

    conn = connect()
    try:
        ok, run_id, error = launch(conn, minutes * 60, 'web', client_ip())
    finally:
        conn.close()
    if run_id is None:
        return jsonify({'error': error}), 409

    log.info("arrosage: start %s min par %s -> %s", minutes, client_ip(), "ok" if ok else error)
    if not ok:
        return jsonify({'error': error, 'state': build_state()}), 502
    return jsonify(build_state())


@watering_bp.route('/watering/stop', methods=['POST'])
def stop():
    if not authed():
        return jsonify({'error': "session expirée"}), 401
    payload = request.get_json(silent=True) or {}
    failure = verify('stop', '', payload)
    if failure:
        return jsonify({'error': failure[1]}), failure[0]

    ok, data, error = agent_call('/stop')
    now = int(time.time())
    ip = client_ip()
    log.info("arrosage: stop par %s -> %s", ip, "ok" if ok else error)

    if not ok:
        # Ne pas clore le run : la vanne est peut-être toujours ouverte.
        return jsonify({'error': "la vanne n'a pas confirmé l'arrêt : {}".format(error),
                        'state': build_state()}), 502

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sweep(conn, now)
        conn.execute(
            "UPDATE watering_run SET status='stopped', active=NULL, ended_at=?,"
            " stop_reason='manual' WHERE active=1", (now,))
        note_valve(conn, now, False)
        conn.execute("COMMIT")
    finally:
        conn.close()

    return jsonify(build_state())


@watering_bp.route('/watering/schedule', methods=['POST'])
def schedule():
    if not authed():
        return jsonify({'error': "session expirée"}), 401
    payload = request.get_json(silent=True) or {}

    at = payload.get('at')
    if not isinstance(at, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', at):
        return jsonify({'error': "heure invalide (attendu HH:MM)"}), 400
    try:
        minutes = int(payload.get('minutes'))
    except (TypeError, ValueError):
        return jsonify({'error': "durée invalide"}), 400
    if not 0 < minutes <= MAX_MINUTES:
        return jsonify({'error': "durée hors limites (1 à {} minutes)".format(MAX_MINUTES)}), 400

    failure = verify('schedule', '{}|{}'.format(at, minutes), payload)
    if failure:
        return jsonify({'error': failure[1]}), failure[0]

    now = int(time.time())
    hour, minute = (int(x) for x in at.split(':'))
    at_ts = next_occurrence(hour, minute, now)

    conn = connect()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO watering_schedule (created_at, at_ts, duration_s, status, client_ip)"
                " VALUES (?, ?, ?, 'pending', ?)", (now, at_ts, minutes * 60, client_ip()))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return jsonify({'error': "un arrosage est déjà programmé à cette heure"}), 409
    finally:
        conn.close()

    log.info("arrosage: programmé à %s pour %s min par %s", at, minutes, client_ip())
    return jsonify(build_state())


@watering_bp.route('/watering/unschedule', methods=['POST'])
def unschedule():
    if not authed():
        return jsonify({'error': "session expirée"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        schedule_id = int(payload.get('id'))
    except (TypeError, ValueError):
        return jsonify({'error': "identifiant invalide"}), 400

    failure = verify('unschedule', str(schedule_id), payload)
    if failure:
        return jsonify({'error': failure[1]}), failure[0]

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        annulees = conn.execute(
            "UPDATE watering_schedule SET status='cancelled' WHERE id=? AND status='pending'",
            (schedule_id,)).rowcount
        conn.execute("COMMIT")
    finally:
        conn.close()
    if not annulees:
        return jsonify({'error': "programmation introuvable ou déjà passée"}), 404

    log.info("arrosage: programmation %s annulée par %s", schedule_id, client_ip())
    return jsonify(build_state())


try:
    init_db()
except Exception:
    log.exception("arrosage: initialisation de %s impossible", WATERING_DB)


if __name__ == '__main__':
    # Point d'entrée du cron, une fois par minute. Aucun contexte Flask n'est
    # nécessaire : seules la base et l'agent sont sollicités.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    lances, manques = fire_due()
    settle_pending()
    if lances or manques:
        print("déclenchés: {} · manqués: {}".format(lances, manques))
