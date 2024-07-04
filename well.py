import time
import serial
import random
import requests
from datetime import datetime

SERVER_URL = 'https://well1d.somebod.com/upload_data'
DEBUG=False

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
    try:
        response = requests.post(SERVER_URL, json=data)
        if response.status_code == 200:
            print(f"Data sent successfully: {data}")
        else:
            print(f"Failed to send data: {response.status_code}")
    except Exception as e:
        print(f"Error sending data: {e}")

def log_water_height():
    while True:
        height = get_depth_mm()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        send_data(timestamp, height)
        time.sleep(60)  # Sleep for 1 minute

if __name__ == "__main__":
    log_water_height()



# if __name__ == "__main__":
#     height  = get_depth_mm()
#     if height is not None:
#         print(f'profondeur : {height} mm')
       
