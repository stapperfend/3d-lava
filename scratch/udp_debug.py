import socket

IP = "0.0.0.0"
PORT = 5010

print(f"DEBUG: Listening for UDP on {IP}:{PORT}...")
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.bind((IP, PORT))
except Exception as e:
    print(f"FAILED TO BIND: {e}")
    exit(1)

sock.settimeout(5.0)
while True:
    try:
        data, addr = sock.recvfrom(1024)
        print(f"RECEIVED {len(data)} bytes from {addr}: {data}")
    except socket.timeout:
        print("Timeout - no packets received.")
    except KeyboardInterrupt:
        break
