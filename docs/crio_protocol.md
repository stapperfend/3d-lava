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
- **Format**: one raw UTF-8 JSON object per UDP datagram. No newline or CRLF delimiter is required.

### UDP Payload Structure
```json
{
  "timestamp": 1714402145.123,
  "sequence": 1,
  "cjc_temp_c": 22.5,
  "cjc_source": "hardware",
  "mod2_tc": [float, float, float, float],
  "mod2_temp_ni_k_c": [float, float, float, float],
  "mod4_volt": [float, ...],
  "mod4_curr": [float, ...],
  "pyrometer": {
    "temperature_c": 25.0,
    "connected": true,
    "emissivity_verified": true
  },
  "error": string|null            // Diagnostic feedback
}
```

---

## 2. Command & Control Interface (TCP)
Used for reliable, transactional updates to system state.

- **IP**: 192.168.137.100 (cRIO)
- **Port**: 5020
- **Model**: Sequential request-response. The server may keep the connection open and process one command per line; the GUI currently opens one short TCP connection per command.
- **Termination**: Newline (`\n`)

### Supported Actions

#### A. `get_state`
Read current state of all relays and temperature channels.
**Request**: `{"action": "get_state"}`
**Response**:
```json
{
  "ok": true
}
```

#### B. `set_relays`
Set the 16 relay command targets as a single array.
**Request**: `{"action": "set_relays", "relays": [false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false]}`
**Response**: `{"ok": true}`

#### C. `set_emissivity`
Set the emissivity of the pyrometer (Metis RS232).
**Request**: `{"action": "set_emissivity", "percent": 85}`
**Response**: `{"ok": true}`
*Note: Value "100" is internally mapped to "00" per hardware spec.*

#### D. `shutdown`
Request the cRIO service to stop.
**Request**: `{"action": "shutdown"}`
**Response**: `{"ok": true}`

---

## 3. Safety Watchdog
The cRIO implements a **2.0 second safety watchdog**. 
- If no TCP command (or sync action) is received for **>2.0s**, all relays (`relay_0` through `relay_5`) will automatically switch to **False (OFF)**.
- The Python driver (`crio.py`) automatically sends a heartbeat `get_state` command to prevent accidental timeout during idle periods.

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
