import os
import sqlite3
from flask import Flask, request, render_template, jsonify
import plotly
import plotly.graph_objs as go
import json
import math
import pytz
from datetime import datetime, timedelta

app = Flask(__name__)

DATABASE = 'well.db'
WELL_RADIUS = .94425  # Dernière mesure du 16 juillet : 1000 litres mesurés au compteur pour passer de 3062 à 2705 mm
# Note : le rayon est plus faible dans la partie haute de la citerne, près du niveau du sol
WELL_HEIGHT = 3055

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
        FROM water_height
    """
    params = [n]

    if from_timestamp is not None and to_timestamp is not None:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([from_timestamp, to_timestamp])

    query += " GROUP BY interval"

    c.execute(query, params)
    data = c.fetchall()
    conn.close()
    return data

def get_data():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM water_height")
    data = c.fetchall()
    conn.close()
    return [x for x in data if x[0] and x[1]]

@app.route('/upload_data', methods=['POST'])
def upload_data():
    data = request.json
    timestamp = int(data['timestamp'])
    height = data['height_mm']
    insert_data(timestamp, height)
    return "Data received", 200

@app.route('/latest')
def latest():
    latest_measure = get_data()[-1]
    timestamp = datetime.fromtimestamp(latest_measure[0], pytz.timezone("Europe/Paris")).strftime('%Y-%m-%d %H:%M:%S')
    height_mm = latest_measure[1]
    volume_liters = mm_to_liters(height_mm)
    return jsonify({"litters": int(volume_liters), "timestamp": timestamp, "height_mm": height_mm})

@app.route('/data')
def data():
    data = [(datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%Y-%m-%d %H:%M:%S'), row[1]) for row in get_data()]
    return jsonify(data)

def mm_to_liters(mm):
    return math.pow(WELL_RADIUS, 2) * math.pi * mm

def format_timestamp(ts):
    if ts:
        return datetime.fromtimestamp(ts).strftime('%d/%m/%Y %H:%M')
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
    print(max_mm, min_mm)
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
    timestamps = [datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%Y-%m-%d %H:%M:%S') for row in data]
    timestamps_human_readable = [datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%d/%m/%Y %H:%M') for row in data]
    heights = [row[1] for row in data]
    volumes = [mm_to_liters(h) for h in heights]
    hover_texts = [f"Heure: {ts}<br>Hauteur: {height:.0f} mm<br>Volume estimé: {liter:.0f} L" for ts, height, liter in zip(timestamps_human_readable,heights, volumes)]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=timestamps, 
        y=volumes, 
        mode=display_mode, 
        name='Volume d\'eau', 
        text=hover_texts, hoverinfo='text'))
    fig.add_shape(
        type="line",
        x0=timestamps[0], x1=timestamps[-1],
        y0=mm_to_liters(WELL_HEIGHT), y1=mm_to_liters(WELL_HEIGHT),
        line=dict(
            color="Red",
            width=2,
            dash="dashdot",
        ),
        name="Hauteur max supposée du puits"
    )
    fig.add_annotation(
        x=timestamps[len(timestamps)//10],
        y=mm_to_liters(WELL_HEIGHT),
        text="Hauteur max supposée du puits",
        showarrow=False,
        yshift=10,
        font=dict(
            color="Red",
            size=12
        )
    )
    fig.update_layout(
        title="Volume d'eau dans le puits",
        xaxis_title='Temps',
        yaxis_title='Volume estimé (litres)',
        height=800,
        xaxis=dict(
            spikesnap='cursor',
            showspikes=True,
            spikemode='across'
        ),
        yaxis=dict(
            showspikes=True,
            spikemode='across'
        )
    )
    
    graphJSON = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    now = datetime.now()

    period_duration = to_timestamp_dt - from_timestamp_dt
    prev_from = (from_timestamp_dt - period_duration).strftime('%Y-%m-%d %H:%M:%S')
    prev_to = (to_timestamp_dt - period_duration).strftime('%Y-%m-%d %H:%M:%S')
    next_from = (from_timestamp_dt + period_duration).strftime('%Y-%m-%d %H:%M:%S')
    next_to = (to_timestamp_dt + period_duration).strftime('%Y-%m-%d %H:%M:%S')

    return render_template('plot.html', graphJSON=graphJSON, now=datetime.now(), timedelta=timedelta,
                           prev_from=prev_from, prev_to=prev_to, next_from=next_from, next_to=next_to, n=n,
                           display_mode=display_mode, well_height=WELL_HEIGHT, well_radius=WELL_RADIUS,
                           water_level=round(heights[-1]), stats=stats())
if __name__ == '__main__':
    create_database()
    app.run(host='0.0.0.0', port=5000)
