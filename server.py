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

def get_data():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM water_height")
    data = c.fetchall()
    conn.close()
    return [(datetime.fromtimestamp(row[0], pytz.timezone("Europe/Paris")).strftime('%Y-%m-%d %H:%M:%S'), row[1]) for row in data]

@app.route('/upload_data', methods=['POST'])
def upload_data():
    data = request.json
    timestamp = int(data['timestamp'])
    height = data['height_mm']
    insert_data(timestamp, height)
    return "Data received", 200

@app.route('/data')
def data():
    data = get_data()
    return jsonify(data)

def mm_to_liters(mm):
    WELL_RADIUS = .78
    return math.pow(WELL_RADIUS, 2) * math.pi * mm

@app.route('/')
def plot():
    data = get_data()
    timestamps = [row[0] for row in data]
    heights = [mm_to_liters(row[1]) for row in data]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=timestamps, y=heights, mode='lines', name='Volume d\'eau'))
    fig.update_layout(title='Volume d\'eau dans le puits',
                      xaxis_title='Temps',
                      yaxis_title='Volume d\'eau (litres)',
                      xaxis=dict(tickangle=-45),
                      height=800)

    graphJSON = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return render_template('plot.html', graphJSON=graphJSON)

if __name__ == '__main__':
    create_database()
    app.run(host='0.0.0.0', port=5000)
