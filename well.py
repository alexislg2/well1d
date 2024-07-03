import time
import serial
# Define the serial port and baud rate.



def get_depth_mm():
    # Define the serial port and baud rate.
    port = '/dev/ttyACM0'
    baudrate = 9600
    # Create a serial connection.
    ser = serial.Serial(port, baudrate, timeout=1)
    # Convert the hexadecimal command to bytes.
    command = bytes.fromhex('01 03 00 04 00 01 C5 CB')
    # Send the command to the serial port.
    ser.write(command)
    # Wait for a response (adjust the sleep time if necessary).
    time.sleep(.1)
    # Read the response from the serial port.
    response = ser.read(7)  # Read 8 bytes (adjust the number of bytes to read according to your expected response length)
    # Close the serial connection.
    ser.close()
    if response == b'':
        return None
    return int.from_bytes(response[3:5], byteorder='big')

if __name__ == "__main__":
    height  = get_depth_mm()
    if height is not None:
        print(f'profondeur : {height} mm')
       
