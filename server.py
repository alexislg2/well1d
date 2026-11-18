import fcntl
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

import pytz
from flask import Flask, Response, abort, jsonify, render_template, request, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

# Derrière Apache, sans ProxyFix, request.remote_addr vaut 127.0.0.1 pour tout
# le monde : le rate-limiting de /watering bannirait tous les visiteurs au
# premier essai raté. Ne faire confiance qu'à un seul saut n'est sûr que parce
# qu'Apache est le seul à pouvoir joindre l'app (bind 127.0.0.1 dans
# docker-compose.yml) : il ajoute la vraie IP en queue de X-Forwarded-For, donc
# une valeur envoyée par le client se retrouve devant, et est ignorée.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    # Sous gunicorn (prod), chaque worker tirerait sa propre clé : les sessions
    # casseraient sans bruit, donc on refuse de démarrer. Un `python server.py`
    # local n'a qu'un processus, une clé jetable y suffit (déconnexion au
    # redémarrage).
    if __name__ != '__main__':
        raise RuntimeError("SECRET_KEY manquant dans l'environnement (voir .env.example)")
    import secrets
    SECRET_KEY = secrets.token_hex(32)
    print("SECRET_KEY absent : clé aléatoire pour ce lancement local")

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=16 * 1024,
)

import watering
import wellsig
app.register_blueprint(watering.watering_bp)

UPLOAD_HMAC_KEY = os.environ.get('AGENT_HMAC_KEY', '').encode()
# Strict par défaut : en permissif, n'importe qui peut remplir well.db. Le
# passer à 0 reste le levier de retour arrière si la signature pose problème.
UPLOAD_REQUIRE_SIGNATURE = os.environ.get('UPLOAD_REQUIRE_SIGNATURE', '1') == '1'

# Dans data/, comme watering.db : un bind mount de répertoire fait atterrir le
# journal SQLite sur l'hôte, où les scripts qui lisent cette base peuvent le
# voir. Monté fichier par fichier, il restait invisible dans le conteneur.
DATABASE = os.environ.get('WELL_DB', 'data/well.db')
WELL_RADIUS = .94425  # Dernière mesure du 16 juillet : 1000 litres mesurés au compteur pour passer de 3062 à 2705 mm
# Note : le rayon est plus faible dans la partie haute de la citerne, près du niveau du sol
WELL_HEIGHT = 3069
local_timezone = pytz.timezone("Europe/Paris")

# Bornes contre la saturation : toutes les routes publiques qui lisent well.db
# sont gratuites pour l'appelant et chères pour le serveur.
MAX_POINTS = 50_000        # 1 mois brut, 1 an à n=15
QUERY_TIMEOUT_S = 10       # < timeout gunicorn : la requête meurt avant le worker
HEAVY_SLOTS = 4            # lectures lourdes simultanées, tous workers confondus
IP_BUCKETS = 256
TS_MIN, TS_MAX = 0, 4102444800  # 1970 → 2100
STATS_TTL = 300
LOCK_DIR = os.environ.get('HEAVY_LOCK_DIR', tempfile.gettempdir())
DISPLAY_MODES = ('lines', 'markers', 'lines+markers')


class Busy(Exception):
    pass


class HeavySlot:
    """Sémaphore inter-workers par flock : sur 16 workers gunicorn, au plus
    HEAVY_SLOTS servent des lectures lourdes, les autres restent libres pour
    l'arrosage et les dépôts du raspberry. Une seule lecture lourde à la fois
    par IP (au hash près). Le noyau relâche les verrous si le worker meurt."""

    def __init__(self, ip):
        self.fds = []
        bucket = int(hashlib.sha256(ip.encode()).hexdigest(), 16) % IP_BUCKETS
        if not self._take([f'well1d-ip-{bucket}.lock']):
            raise Busy(429)
        if not self._take([f'well1d-heavy-{i}.lock' for i in range(HEAVY_SLOTS)]):
            self.release()
            raise Busy(503)

    def _take(self, names):
        for name in names:
            fd = os.open(os.path.join(LOCK_DIR, name), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            self.fds.append(fd)
            return True
        return False

    def release(self):
        while self.fds:
            os.close(self.fds.pop())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def heavy_slot():
    return HeavySlot(request.remote_addr or '?')


@app.errorhandler(Busy)
def busy(e):
    code = e.args[0]
    message = ("une requête de cette adresse est déjà en cours" if code == 429
               else "serveur occupé, réessayez dans quelques secondes")
    return jsonify({"error": message}), code, {"Retry-After": "5"}


@app.errorhandler(sqlite3.OperationalError)
def sqlite_error(e):
    if 'interrupted' in str(e):
        return jsonify({"error": "requête trop longue, réduisez la plage ou augmentez n"}), 503
    app.logger.exception("erreur sqlite")
    return jsonify({"error": "erreur interne"}), 500


@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    return response


def connect_well():
    """Toute lecture est interrompue au-delà de QUERY_TIMEOUT_S."""
    conn = sqlite3.connect(DATABASE, timeout=10.0)
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    return conn


def check_span(from_ts, to_ts, n):
    """Message d'erreur si la plage demandée produirait plus de MAX_POINTS
    points (une mesure par minute), sinon None."""
    if from_ts is None:
        conn = connect_well()
        try:
            from_ts, = conn.execute("SELECT MIN(timestamp) FROM water_height").fetchone()
        finally:
            conn.close()
        if from_ts is None:
            return None
    if to_ts is None:
        to_ts = int(time.time())
    points = (to_ts - from_ts) / (60 * max(n, 1))
    if points > MAX_POINTS:
        n_min = math.ceil((to_ts - from_ts) / 60 / MAX_POINTS)
        return f"plage trop large pour n={n} : {int(points)} points (max {MAX_POINTS}), utilisez n >= {n_min}"
    return None


def create_database():
    if not os.path.exists(DATABASE):
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS water_height
                     (timestamp INTEGER, height_mm INTEGER)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_timestamp ON water_height (timestamp)''')
        conn.commit()
        conn.close()

def insert_data(timestamp, height):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT INTO water_height (timestamp, height_mm) VALUES (?, ?)", (timestamp, height))
    conn.commit()
    conn.close()

def get_n_minute_averages(n, from_timestamp=None, to_timestamp=None):
    conn = connect_well()
    c = conn.cursor()

    query = """
        SELECT (timestamp - (timestamp % (? * 60))) as interval, AVG(height_mm) as avg_height
        FROM water_height WHERE height_mm IS NOT NULL
    """
    params = [n]

    if from_timestamp is not None:
        query += " AND timestamp >= ?"
        params.append(from_timestamp)
    if to_timestamp is not None:
        query += " AND timestamp <= ?"
        params.append(to_timestamp)

    query += " GROUP BY interval ORDER BY interval"

    c.execute(query, params)
    data = c.fetchall()
    conn.close()
    return data

def get_raw_measurements(from_timestamp=None, to_timestamp=None):
    conn = connect_well()
    c = conn.cursor()

    query = "SELECT timestamp, height_mm FROM water_height WHERE height_mm IS NOT NULL"
    params = []

    if from_timestamp is not None:
        query += " AND timestamp >= ?"
        params.append(from_timestamp)
    if to_timestamp is not None:
        query += " AND timestamp <= ?"
        params.append(to_timestamp)

    query += " ORDER BY timestamp"

    c.execute(query, params)
    data = c.fetchall()
    conn.close()
    return data

def get_latest():
    conn = connect_well()
    try:
        return conn.execute(
            "SELECT timestamp, height_mm FROM water_height"
            " WHERE height_mm IS NOT NULL AND height_mm != 0 AND timestamp"
            " ORDER BY timestamp DESC LIMIT 1").fetchone()
    finally:
        conn.close()

@app.route('/upload_data', methods=['POST'])
def upload_data():
    refusal = wellsig.verify(UPLOAD_HMAC_KEY, 'POST', '/upload_data',
                             request.get_data(), request.headers,
                             seen=watering.consume_nonce)
    if refusal:
        if UPLOAD_REQUIRE_SIGNATURE:
            app.logger.warning("upload refusé depuis %s : %s", request.remote_addr, refusal)
            return jsonify({"error": refusal}), 401
        app.logger.warning("upload non signé accepté depuis %s : %s (migration en cours)",
                           request.remote_addr, refusal)

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'timestamp' not in data or 'height_mm' not in data:
        return jsonify({"error": "timestamp et height_mm sont requis"}), 400
    try:
        timestamp = int(data['timestamp'])
    except (TypeError, ValueError):
        return jsonify({"error": "timestamp invalide"}), 400

    # height_mm peut être NULL : la sonde est parfois muette, et cette absence
    # de mesure est une information qu'on conserve.
    insert_data(timestamp, data['height_mm'])
    return "Data received", 200

@app.route('/latest')
def latest():
    latest_measure = get_latest()
    if latest_measure is None:
        return jsonify({"error": "aucune mesure"}), 404
    timestamp = datetime.fromtimestamp(latest_measure[0], local_timezone).strftime('%Y-%m-%d %H:%M:%S')
    height_mm = latest_measure[1]
    volume_liters = mm_to_liters(height_mm)
    return jsonify({"litters": int(volume_liters), "timestamp": timestamp, "height_mm": height_mm})

@app.route('/data')
def data():
    # Toute la base : en flux, pour qu'un appel ne coûte pas des centaines de
    # Mo de mémoire au worker, et sous un créneau lourd tenu jusqu'au bout.
    slot = heavy_slot()

    def generate():
        conn = sqlite3.connect(DATABASE, timeout=10.0)
        try:
            cursor = conn.execute(
                "SELECT timestamp, height_mm FROM water_height"
                " WHERE height_mm IS NOT NULL AND height_mm != 0 AND timestamp")
            yield '['
            first = True
            while rows := cursor.fetchmany(5000):
                chunk = ','.join(json.dumps(
                    [datetime.fromtimestamp(ts, local_timezone).strftime('%Y-%m-%d %H:%M:%S'), h])
                    for ts, h in rows)
                yield chunk if first else ',' + chunk
                first = False
            yield ']'
        finally:
            conn.close()
            slot.release()

    # call_on_close en plus du finally : un générateur jamais démarré (HEAD,
    # client parti avant le premier octet) n'exécute pas son finally, et le
    # créneau resterait pris jusqu'à la mort du worker.
    response = Response(stream_with_context(generate()), mimetype='application/json')
    response.call_on_close(slot.release)
    return response

def parse_timestamp_arg(value):
    """Accepte un timestamp unix ('1755000000') ou une date locale
    ('2025-08-12', '2025-08-12 14:30:00', '2025-08-12T14:30:00').
    Lève ValueError si le format n'est pas reconnu."""
    value = value.strip()[:32]
    if value.lstrip('-').isdigit():
        ts = int(value)
    else:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                naive = datetime.strptime(value.replace('T', ' '), fmt)
            except ValueError:
                continue
            ts = int(local_timezone.localize(naive).timestamp())
            break
        else:
            raise ValueError(f"Format de date invalide : {value!r}")
    if not TS_MIN <= ts <= TS_MAX:
        raise ValueError(f"Date hors limites : {value!r}")
    return ts

@app.route('/api/measurements')
def api_measurements():
    try:
        from_timestamp = parse_timestamp_arg(request.args['from']) if request.args.get('from') else None
        to_timestamp = parse_timestamp_arg(request.args['to']) if request.args.get('to') else None
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        n = int(request.args.get('n', 5))
    except ValueError:
        return jsonify({"error": "Le paramètre 'n' doit être un entier"}), 400
    if not 0 <= n <= 1440:
        return jsonify({"error": "Le paramètre 'n' doit être compris entre 0 et 1440"}), 400

    if from_timestamp is not None and to_timestamp is not None and from_timestamp > to_timestamp:
        return jsonify({"error": "'from' doit être antérieur à 'to'"}), 400

    with heavy_slot():
        too_wide = check_span(from_timestamp, to_timestamp, n)
        if too_wide:
            return jsonify({"error": too_wide}), 400
        if n <= 1:
            rows = get_raw_measurements(from_timestamp, to_timestamp)
        else:
            rows = get_n_minute_averages(n, from_timestamp, to_timestamp)

    points = [{
        "timestamp": int(ts),
        "datetime": datetime.fromtimestamp(ts, local_timezone).isoformat(),
        "height_mm": round(height, 1),
        "liters": round(mm_to_liters(height), 1),
    } for ts, height in rows]

    return jsonify({
        "from": from_timestamp,
        "to": to_timestamp,
        "n": n,
        "count": len(points),
        "well": {
            "height_mm": WELL_HEIGHT,
            "radius_m": WELL_RADIUS,
            "max_liters": round(mm_to_liters(WELL_HEIGHT), 1),
        },
        "points": points,
    })

def mm_to_liters(mm):
    return math.pow(WELL_RADIUS, 2) * math.pi * mm

def format_timestamp(ts):
    if ts:
        return datetime.fromtimestamp(ts, local_timezone).strftime('%d/%m/%Y %H:%M')
    return "N/A"

_stats_cache = (0.0, None)


def stats():
    """Deux parcours complets de la table (pas d'index sur height_mm) : mis
    en cache, sinon chaque affichage de / les refait."""
    global _stats_cache
    computed_at, value = _stats_cache
    if value is None or time.monotonic() - computed_at > STATS_TTL:
        value = compute_stats()
        _stats_cache = (time.monotonic(), value)
    return value


def compute_stats():
    conn = connect_well()
    c = conn.cursor()
    c.execute("""
    SELECT
        (SELECT height_mm FROM water_height WHERE height_mm IS NOT NULL ORDER BY height_mm DESC, timestamp DESC LIMIT 1),
        (SELECT timestamp FROM water_height WHERE height_mm IS NOT NULL ORDER BY height_mm DESC, timestamp DESC LIMIT 1),
        (SELECT height_mm FROM water_height WHERE height_mm IS NOT NULL ORDER BY height_mm ASC, timestamp ASC LIMIT 1),
        (SELECT timestamp FROM water_height WHERE height_mm IS NOT NULL ORDER BY height_mm ASC, timestamp ASC LIMIT 1),
        (SELECT COUNT(*) FROM water_height)
""")
    max_mm, max_timestamp, min_mm, min_timestamp, count = c.fetchone()
    conn.close()
    return {
        "max": {"mm": max_mm, "liters": int(mm_to_liters(max_mm or 0)), "dt": format_timestamp(max_timestamp)},
        "min": {"mm": min_mm, "liters": int(mm_to_liters(min_mm or 0)), "dt": format_timestamp(min_timestamp)},
        "count": count
    }

@app.route('/')
def plot():
    from_timestamp = request.args.get('from')
    to_timestamp = request.args.get('to')
    try:
        n = int(request.args.get('n', 5))
    except ValueError:
        abort(400)
    if not 1 <= n <= 1440:
        abort(400)
    display_mode = request.args.get('display_mode', 'lines')
    if display_mode not in DISPLAY_MODES:
        display_mode = 'lines'

    try:
        if from_timestamp:
            from_timestamp_dt = datetime.strptime(from_timestamp, '%Y-%m-%d %H:%M:%S')
            to_timestamp_dt = datetime.strptime(to_timestamp, '%Y-%m-%d %H:%M:%S') if to_timestamp \
                else datetime.now()
        else:
            to_timestamp_dt = datetime.now()
            from_timestamp_dt = to_timestamp_dt - timedelta(days=7)
        from_timestamp_unix = int(from_timestamp_dt.timestamp())
        to_timestamp_unix = int(to_timestamp_dt.timestamp())
        period_duration = to_timestamp_dt - from_timestamp_dt
        prev_from = (from_timestamp_dt - period_duration).strftime('%Y-%m-%d %H:%M:%S')
        prev_to = (to_timestamp_dt - period_duration).strftime('%Y-%m-%d %H:%M:%S')
        next_from = (from_timestamp_dt + period_duration).strftime('%Y-%m-%d %H:%M:%S')
        next_to = (to_timestamp_dt + period_duration).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OverflowError, OSError):
        abort(400)
    if not (TS_MIN <= from_timestamp_unix <= TS_MAX and TS_MIN <= to_timestamp_unix <= TS_MAX):
        abort(400)

    # Une page n'a pas de message d'erreur à montrer : on lisse davantage
    # plutôt que de refuser une plage trop large.
    span_s = max(0, to_timestamp_unix - from_timestamp_unix)
    n = max(n, math.ceil(span_s / 60 / MAX_POINTS))

    with heavy_slot():
        data = get_n_minute_averages(n, from_timestamp_unix, to_timestamp_unix)
        page_stats = stats()
    timestamps_unix = [int(row[0]) for row in data]
    heights = [row[1] for row in data]
    volumes = [round(mm_to_liters(h), 1) for h in heights]

    try:
        waterings = watering.runs_in_range(from_timestamp_unix, to_timestamp_unix)
    except Exception:
        # Les deux bases sont indépendantes : le niveau reste lisible même si
        # watering.db manque ou est verrouillé.
        app.logger.exception("arrosages illisibles, graphe tracé sans eux")
        waterings = []

    chart_data = {
        "timestamps": timestamps_unix,
        "volumes": volumes,
        "heights": [round(h, 1) for h in heights],
        "max_volume": round(mm_to_liters(WELL_HEIGHT), 1),
        "waterings": waterings,
    }

    water_level = round(heights[-1]) if heights else None

    return render_template('plot.html',
                           chart_data=json.dumps(chart_data),
                           now=datetime.now(), timedelta=timedelta,
                           prev_from=prev_from, prev_to=prev_to,
                           next_from=next_from, next_to=next_to, n=n,
                           display_mode=display_mode, well_height=WELL_HEIGHT, well_radius=WELL_RADIUS,
                           water_level=water_level, stats=page_stats)
if __name__ == '__main__':
    create_database()
    app.run(host='0.0.0.0', port=5000)
