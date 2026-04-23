# cRIO TCP JSON Protocol Specification
## For the LabVIEW RT VI Developer

This document defines the TCP communication protocol between the Python Flask server (host PC) and the NI cRIO real-time controller.

---

## Connection Model

- **Server**: LabVIEW RT VI on cRIO listens on **TCP port 5020** (configurable in `config.py`)
- **Client**: Python driver (`drivers/crio.py`) connects, sends a command, reads the reply, and closes the connection
- **Format**: Each message is a single **UTF-8 JSON string terminated by a newline character** (`\n`)
- **One request → one response per connection** (simplest model; can be extended to persistent connection later)

---

## Message Format

### Request (Host → cRIO)
```json
{
  "action": "<action_name>",
  ... action-specific fields ...
}
\n
```

### Response (cRIO → Host)
```json
{
  "ok": true,
  ... action-specific fields ...
}
\n
```
On error:
```json
{
  "ok": false,
  "error": "Human readable error description"
}
\n
```

---

## Actions

### 1. `get_all`
Read all relay states and all temperature channels at once.

**Request:**
```json
{"action": "get_all"}
```

**Response:**
```json
{
  "ok": true,
  "relays": {
    "relay_0": false,
    "relay_1": true,
    "relay_2": false,
    "relay_3": false,
    "relay_4": false,
    "relay_5": false
  },
  "temperatures": {
    "temp_0": 215.3,
    "temp_1": 60.1,
    "temp_2": 35.7,
    "temp_3": 22.4,
    "temp_pyro": 850.0
  }
}
```

---

### 2. `set_relay`
Set a single relay output.

**Request:**
```json
{"action": "set_relay", "channel": "relay_2", "state": true}
```
- `channel`: string matching one of the relay IDs defined in `config.py`
- `state`: boolean (`true` = energised / on, `false` = de-energised / off)

**Response:**
```json
{"ok": true}
```

---

### 3. `read_temp`
Read a single temperature channel.

**Request:**
```json
{"action": "read_temp", "channel": "temp_0"}
```

**Response:**
```json
{"ok": true, "value": 215.3}
```
Value is in **°C**. If the channel does not exist or the measurement fails, return `{"ok": false, "error": "..."}`.
Special case for `temp_pyro`: returns `-1.0` during warming/stabilization phase.

---

### 4. `set_emissivity`
Set the emissivity of the pyrometer (Metis RS232).

**Request:**
```json
{"action": "set_emissivity", "value": "85"}
```
- `value`: two-digit string ("20"..."99", or "00" for 100%)

**Response:**
```json
{"ok": true}
```

---

## LabVIEW RT Implementation Guide

### Recommended VI Structure

```
TCP Listen (port 5010)
  └── Loop:
        1. TCP Accept Connection → connection ID
        2. TCP Read until \n    → JSON string
        3. Parse JSON string     → cluster (action, channel, state, ...)
        4. Switch on "action":
             "get_all"   → read all DIO + AI channels, build response cluster
             "set_relay" → write DO channel[channel] = state, respond ok
             "read_temp" → read AI channel[channel], respond value
             default     → respond {"ok": false, "error": "Unknown action"}
        5. Flatten response cluster to JSON string + \n
        6. TCP Write response
        7. TCP Close Connection
        8. Loop back to Accept
```

### NI Module Mappings (example — adjust to your hardware)

| channel ID | Module Function       | NI Module Example |
|---|---|---|
| `relay_0`…`relay_5` | Digital Output (relay) | NI 9485, NI 9474 |
| `temp_0`…`temp_3`   | Thermocouple AI input  | NI 9214, NI 9213 |

### JSON Parsing in LabVIEW RT
Use the **Unflatten from JSON** VI (available in LabVIEW 2016+) or the **JKISH JSON** library for the cRIO target.

---

## Example Python Test Script

Run this on the host PC to test the cRIO VI independently of the Flask server:

```python
import socket, json

CRIO_IP   = "192.168.137.1"
CRIO_PORT = 5020

def send(cmd):
    payload = (json.dumps(cmd) + "\n").encode()
    with socket.create_connection((CRIO_IP, CRIO_PORT), timeout=3) as s:
        s.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(4096)
    return json.loads(buf.decode().strip())

print(send({"action": "get_all"}))
print(send({"action": "set_relay", "channel": "relay_0", "state": True}))
print(send({"action": "read_temp", "channel": "temp_0"}))
```
