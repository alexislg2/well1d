import os
import sqlite3
from flask import Flask, request, render_template, jsonify
import plotly
import plotly.graph_objs as go
import json
import math
import pytz
from datetime import datetime

app = Flask(__name__)

DATABASE = 'well.db'
WELL_RADIUS = .78
WELL_HEIGHT = 3155

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

def get_n_minute_averages(n):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(f"""
        SELECT (timestamp - (timestamp % {60 * n})) as interval, AVG(height_mm) as avg_height
        FROM water_height
        GROUP BY interval
    """)
    data = c.fetchall()
    conn.close()
    return data

def get_data():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM water_height")
    data = c.fetchall()
    conn.close()
    return data

@app.route('/upload_data', methods=['POST'])
def upload_data():
    data = request.json
    timestamp = int(data['timestamp'])
    height = data['height_mm']
    insert_data(timestamp, height)
    return "Data received", 200

@app.route('/data')
def data():
    data = [(datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%Y-%m-%d %H:%M:%S'), row[1]) for row in get_data()]
    return jsonify(data)

def mm_to_liters(mm):
    return math.pow(WELL_RADIUS, 2) * math.pi * mm

@app.route('/')
def plot():
    data = get_n_minute_averages(5)
    timestamps = [datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%Y-%m-%d %H:%M:%S') for row in data]
    timestamps_human_readable = [datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%d/%m/%Y %H:%M') for row in data]
    heights = [row[1] for row in data]
    volumes = [mm_to_liters(h) for h in heights]
    hover_texts = [f"Heure: {ts}<br>Hauteur: {height} mm<br>Volume estimé: {liter:.2f} L" for ts, height, liter in zip(timestamps_human_readable,heights, volumes)]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=timestamps, y=volumes, mode='lines', name='Volume d\'eau', text=hover_texts, hoverinfo='text'))
    # fig.add_shape(
    #     type="line",
    #     x0=timestamps[0], x1=timestamps[-1],
    #     y0=mm_to_liters(WELL_HEIGHT), y1=mm_to_liters(WELL_HEIGHT),
    #     line=dict(
    #         color="Red",
    #         width=2,
    #         dash="dashdot",
    #     ),
    #     name="Hauteur max du puits"
    # )
    # fig.add_annotation(
    #     x=timestamps[len(timestamps)//8],
    #     y=mm_to_liters(WELL_HEIGHT),
    #     text="Hauteur max du puits",
    #     showarrow=False,
    #     yshift=10,
    #     font=dict(
    #         color="Red",
    #         size=12
    #     )
    # )
    fig.update_layout(
        title="Volume d'eau dans le puits",
        xaxis_title='Temps',
        yaxis_title='Volume estime (litres)',
        xaxis=dict(tickangle=-45),
        height=800
    )
    
    graphJSON = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return render_template('plot.html', graphJSON=graphJSON)

if __name__ == '__main__':
    create_database()
    app.run(host='0.0.0.0', port=5000)
