# cRIO DAQ & Control Protocol Specification
## Hybrid UDP Telemetry + TCP Transactional Interface

This document defines the communication protocol between the Python Flask server (host PC) and the NI cRIO real-time controller.

---

## 1. Real-Time Telemetry Stream (UDP)
The cRIO broadcasts a continuous telemetry stream for high-frequency monitoring.

- **Source**: NI cRIO-9057
- **Destination**: 192.168.137.1 (PC)
- **Port**: 5021
- **Frequency**: 10Hz (~100ms interval)
- **Format**: Newline-terminated JSON string (`\n`)

### UDP Payload Structure
```json
{
  "timestamp": 1714402145.123,
  "relays": {
    "relay_0": false, ..., "relay_15": false
  },
  "temperatures": {
    "tc": [float, ...],           // 16 channels (Mod2)
    "volt": [float, ...],         // 8 channels (Mod4 AI 0-7)
    "curr": [float, ...],         // 8 channels (Mod4 AI 8-15)
    "pyro_digital": float,        // Metis MY51 Digital Temp
    "pyro_analog": float,         // Metis MY51 Analog Backup
    "emissivity": int             // Active pyrometer emissivity
  },
  "error": string|null            // Diagnostic feedback
}
```

---

## 2. Command & Control Interface (TCP)
Used for reliable, transactional updates to system state.

- **IP**: 192.168.137.100 (cRIO)
- **Port**: 5020
- **Model**: Sequential Request-Response
- **Termination**: Newline (`\n`)

### Supported Actions

#### A. `get_all`
Read current state of all relays and temperature channels.
**Request**: `{"action": "get_all"}`
**Response**:
```json
{
  "ok": true,
  "relays": {"relay_0": false, ..., "relay_5": false},
  "temperatures": {
    "temp_0": 25.1, "temp_1": 25.3, "temp_2": 25.0, "temp_3": 25.2,
    "temp_pyro": 850.0
  }
}
```

#### B. `set_relay`
Set a single relay output.
**Request**: `{"action": "set_relay", "channel": "relay_0", "state": true}`
**Response**: `{"ok": true}`

#### C. `set_emissivity`
Set the emissivity of the pyrometer (Metis RS232).
**Request**: `{"action": "set_emissivity", "value": "85"}`
**Response**: `{"ok": true}`
*Note: Value "100" is internally mapped to "00" per hardware spec.*

#### D. `read_temp`
Read a single temperature channel.
**Request**: `{"action": "read_temp", "channel": "temp_0"}`
**Response**: `{"ok": true, "value": 25.1}`

---

## 3. Safety Watchdog
The cRIO implements a **2.0 second safety watchdog**. 
- If no TCP command (or sync action) is received for **>2.0s**, all relays (`relay_0` through `relay_5`) will automatically switch to **False (OFF)**.
- The Python driver (`crio.py`) automatically sends a heartbeat `get_all` command every 1.5s to prevent accidental timeout during idle periods.

---

## LabVIEW RT Implementation Guide

### Recommended Loop Structure
1. **Telemetry Loop (100ms)**: Read all AI/DI, format JSON, send via UDP `192.168.137.1:5021`.
2. **TCP Listener Loop**: Accept connection, read JSON, execute case structure, respond, close.
3. **Watchdog Monitor**: Monitor the timestamp of the last TCP command. If >2.0s, reset DO outputs.

### NI Module Mappings
| Channel ID | Function | NI Module |
|---|---|---|
| `relay_0`…`relay_5` | Digital Output (Relay) | Mod3 (NI 9485) |
| `temp_0`…`temp_3` | Thermocouple Input | Mod2 (NI 9214) |
| `pyro` | RS232 Pyrometer | Serial Port |
