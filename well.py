import time
import serial
import random
import requests
import sqlite3
from datetime import datetime, timedelta
from requests.exceptions import RequestException
import os

DEBUG = False
SERVER_URL = 'https://well1d.somebod.com/upload_data'
LOG_PERIOD = 60
RETRY_DELAY = 10  # seconds to wait before retrying after a failure
MAX_RETRIES = 3   # maximum number of retries
DATABASE = 'failed_uploads.db'

if DEBUG:
    SERVER_URL = "http://127.0.0.1:5000/upload_data"
    LOG_PERIOD = 6

def initialize_database():
    database_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), DATABASE)
    if not os.path.exists(database_path):
        conn = sqlite3.connect(database_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS failed_uploads
                    (timestamp INTEGER PRIMARY KEY, height_mm INTEGER)''')
        conn.commit()
        conn.close()

def save_failed_data(timestamp, height):
    database_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), DATABASE)
    conn = sqlite3.connect(database_path)
    c = conn.cursor()
    c.execute("INSERT INTO failed_uploads (timestamp, height_mm) VALUES (?, ?)", (timestamp, height))
    conn.commit()
    conn.close()

def retry_failed_uploads():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM failed_uploads")
    rows = c.fetchall()

    for row in rows:
        timestamp, height_mm = row
        if send_data(timestamp, height_mm, retry=False):
            c.execute("DELETE FROM failed_uploads WHERE timestamp = ?", (timestamp,))
            conn.commit()

    conn.close()

def get_depth_mm():
    if DEBUG:
        return random.randint(2000, 3000)
    port = '/dev/ttyACM0'
    baudrate = 9600
    ser = serial.Serial(port, baudrate, timeout=1)
    command = bytes.fromhex('01 03 00 04 00 01 C5 CB')
    ser.write(command)
    time.sleep(.1)
    response = ser.read(7)
    ser.close()
    if response == b'':
        return None
    return int.from_bytes(response[3:5], byteorder='big')

def send_data(timestamp, height, retry=True):
    data = {'timestamp': timestamp, 'height_mm': height}
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(SERVER_URL, json=data, timeout=10)  # 10 seconds timeout
            if response.status_code == 200:
                print(f"Data sent successfully: {data}")
                return True
            else:
                print(f"Failed to send data: {response.status_code}")
        except RequestException as e:
            print(f"Error sending data (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
        time.sleep(RETRY_DELAY)
    print(f"Failed to send data after {MAX_RETRIES} attempts.")
    return False

def log_water_height():
    while True:
        height = get_depth_mm()
        now = datetime.now()
        timestamp = int(time.time())
        
        if not send_data(timestamp, height):
            save_failed_data(timestamp, height)
        time.sleep(max(0, (now + timedelta(seconds=LOG_PERIOD) - datetime.now()).total_seconds()))

if __name__ == "__main__":
    initialize_database()
    log_water_height()