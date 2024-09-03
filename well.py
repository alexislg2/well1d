import time
import serial
import random
import requests
from datetime import datetime, timedelta
from requests.exceptions import RequestException

DEBUG = False
SERVER_URL = 'https://well1d.somebod.com/upload_data'
LOG_PERIOD = 60
RETRY_DELAY = 10  # seconds to wait before retrying after a failure
MAX_RETRIES = 3   # maximum number of retries

if DEBUG:
    SERVER_URL = "http://127.0.0.1:5000/upload_data"
    LOG_PERIOD = 6

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

def send_data(timestamp, height):
    data = {'timestamp': timestamp, 'height_mm': height}
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(SERVER_URL, json=data, timeout=10)
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
        send_data(timestamp, height)
        time.sleep(max(0, (now + timedelta(seconds=LOG_PERIOD) - datetime.now()).total_seconds()))

if __name__ == "__main__":
    log_water_height()
