import time
import serial


def get_depth_mm():
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

if __name__ == "__main__":
    height  = get_depth_mm()
    if height is not None:
        print(f'profondeur : {height} mm')
       
