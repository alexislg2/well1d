import os
import sqlite3
from flask import Flask, request, render_template, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import math
import pytz
from datetime import datetime, timedelta

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
    raise RuntimeError("SECRET_KEY manquant dans l'environnement (voir .env.example)")

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
# Permissif par défaut : une migration où le serveur passe avant le raspberry
# ne doit pas faire perdre de mesures. .env.example le met à 1, et c'est le
# levier de retour arrière si la signature pose problème en production.
UPLOAD_REQUIRE_SIGNATURE = os.environ.get('UPLOAD_REQUIRE_SIGNATURE', '0') == '1' 

DATABASE = 'well.db'
WELL_RADIUS = .94425  # Dernière mesure du 16 juillet : 1000 litres mesurés au compteur pour passer de 3062 à 2705 mm
# Note : le rayon est plus faible dans la partie haute de la citerne, près du niveau du sol
WELL_HEIGHT = 3069
local_timezone = pytz.timezone("Europe/Paris")

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
    conn = sqlite3.connect(DATABASE)
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
    conn = sqlite3.connect(DATABASE)
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

def get_data():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM water_height WHERE height_mm IS NOT NULL")
    data = c.fetchall()
    conn.close()
    return [x for x in data if x[0] and x[1]]

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
    latest_measure = get_data()[-1]
    timestamp = datetime.fromtimestamp(latest_measure[0], local_timezone).strftime('%Y-%m-%d %H:%M:%S')
    height_mm = latest_measure[1]
    volume_liters = mm_to_liters(height_mm)
    return jsonify({"litters": int(volume_liters), "timestamp": timestamp, "height_mm": height_mm})

@app.route('/data')
def data():
    data = [(datetime.fromtimestamp(row[0], local_timezone).strftime('%Y-%m-%d %H:%M:%S'), row[1]) for row in get_data()]
    return jsonify(data)

def parse_timestamp_arg(value):
    """Accepte un timestamp unix ('1755000000') ou une date locale
    ('2025-08-12', '2025-08-12 14:30:00', '2025-08-12T14:30:00').
    Lève ValueError si le format n'est pas reconnu."""
    value = value.strip()
    if value.lstrip('-').isdigit():
        return int(value)
    value = value.replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return int(local_timezone.localize(naive).timestamp())
    raise ValueError(f"Format de date invalide : {value!r}")

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

def stats():
    conn = sqlite3.connect(DATABASE)
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
        "max": {"mm": max_mm, "liters": int(mm_to_liters(max_mm)), "dt": format_timestamp(max_timestamp)},
        "min": {"mm": min_mm, "liters": int(mm_to_liters(min_mm)), "dt": format_timestamp(min_timestamp)},
        "count": count
    }

@app.route('/')
def plot():
    from_timestamp = request.args.get('from')
    to_timestamp = request.args.get('to')
    n = int(request.args.get('n', 5))  # Default to 1 minute aggregation
    display_mode = request.args.get('display_mode', 'lines')  # Default to 'lines'


    if from_timestamp:
        from_timestamp_dt = datetime.strptime(from_timestamp, '%Y-%m-%d %H:%M:%S')
        to_timestamp_dt = datetime.strptime(to_timestamp, '%Y-%m-%d %H:%M:%S') if to_timestamp \
            else datetime.now()
    else:
        to_timestamp_dt = datetime.now()
        from_timestamp_dt = to_timestamp_dt - timedelta(days=7)

    from_timestamp_unix = int(from_timestamp_dt.timestamp())
    to_timestamp_unix = int(to_timestamp_dt.timestamp())

    data = get_n_minute_averages(n, from_timestamp_unix, to_timestamp_unix)
    timestamps_unix = [int(row[0]) for row in data]
    heights = [row[1] for row in data]
    volumes = [round(mm_to_liters(h), 1) for h in heights]

    chart_data = {
        "timestamps": timestamps_unix,
        "volumes": volumes,
        "heights": [round(h, 1) for h in heights],
        "max_volume": round(mm_to_liters(WELL_HEIGHT), 1),
    }

    water_level = round(heights[-1]) if heights else None

    period_duration = to_timestamp_dt - from_timestamp_dt
    prev_from = (from_timestamp_dt - period_duration).strftime('%Y-%m-%d %H:%M:%S')
    prev_to = (to_timestamp_dt - period_duration).strftime('%Y-%m-%d %H:%M:%S')
    next_from = (from_timestamp_dt + period_duration).strftime('%Y-%m-%d %H:%M:%S')
    next_to = (to_timestamp_dt + period_duration).strftime('%Y-%m-%d %H:%M:%S')

    return render_template('plot.html',
                           chart_data=json.dumps(chart_data),
                           now=datetime.now(), timedelta=timedelta,
                           prev_from=prev_from, prev_to=prev_to,
                           next_from=next_from, next_to=next_to, n=n,
                           display_mode=display_mode, well_height=WELL_HEIGHT, well_radius=WELL_RADIUS,
                           water_level=water_level, stats=stats())
if __name__ == '__main__':
    create_database()
    app.run(host='0.0.0.0', port=5000)
