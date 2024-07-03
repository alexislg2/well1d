import os
import sqlite3
from flask import Flask, request, render_template, jsonify
import plotly
import plotly.graph_objs as go
import json

app = Flask(__name__)

DATABASE = 'water_height_data.db'

def create_database():
    if not os.path.exists(DATABASE):
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS water_height
                     (timestamp TEXT, height_mm INTEGER)''')
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
    return data

@app.route('/upload_data', methods=['POST'])
def upload_data():
    data = request.json
    timestamp = data['timestamp']
    height = data['height_mm']
    insert_data(timestamp, height)
    return "Data received", 200

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/data')
def data():
    data = get_data()
    return jsonify(data)

@app.route('/plot')
def plot():
    data = get_data()
    timestamps = [row[0] for row in data]
    heights = [row[1] for row in data]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=timestamps, y=heights, mode='lines', name='Water Height'))
    fig.update_layout(title='Water Height Over Time',
                      xaxis_title='Time',
                      yaxis_title='Water Height (mm)',
                      xaxis=dict(tickangle=-45))

    graphJSON = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    return render_template('plot.html', graphJSON=graphJSON)

if __name__ == '__main__':
    create_database()
    app.run(host='0.0.0.0', port=5000)
