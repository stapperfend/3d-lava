"""
drivers/furnace.py  —  COBES i-class compact Induction Furnace driver
======================================================================
Protocol source: 800 0031.02_BA_EN_Bedienungsanleitung i-class compact.pdf

MAIN CONTROL (cyclic, port 5010)
  PLC → ICC:  28 bytes   BSTSTART + ctrl_word(U16) + curr_sp(f32) + pwr_sp(f32) + prog_no(U16) + BSTENDTX
  ICC → PLC: 134 bytes   BSTSTART + all status fields + BSTENDTX  (every 100 ms)

SERVICE PROTOCOL (port 4660) — heating programmes
  SET_HEATPROG  / GET_HEATPROG  telegrams (section 9.5)
"""

import json
import math
import os
import random
import socket
import struct
import threading
import time

import config

# ─────────────────────────────────────────────────────────────
# Packet constants
# ─────────────────────────────────────────────────────────────
PREFIX = b"BSTSTART"
SUFFIX = b"BSTENDTX"
INPUT_PACKET_SIZE = 134

# Control word bits (section 9.3.1)
BIT_HEATING_ON   = 1 << 0
BIT_CTRL_MODE    = 1 << 1   # 0=manual, 1=auto/program
BIT_SEL_TC       = 1 << 2   # bits 2-3: temp source (01=thermocouple)
BIT_RESET_ENERGY = 1 << 7
BIT_ACK_ERROR    = 1 << 8
BIT_HEARTBEAT    = 1 << 15

# Status word bits (section 9.2.1)
SBIT_READY     = 1 << 0
SBIT_ACTIVE    = 1 << 1
SBIT_ERROR     = 1 << 2
SBIT_PROG_DONE = 1 << 3
SBIT_PROG_ERR  = 1 << 4
SBIT_ESTOP     = 1 << 8
SBIT_HEARTBEAT = 1 << 15

FSM_STATES = {
    2: "Init", 3: "InitNetwork", 4: "StartNetServices",
    5: "WaitForBusMaster", 6: "PrepareSupplies", 7: "PrepareGateDrivers",
    8: "WaitForDCLink", 9: "Ready", 10: "Active",
    11: "Error", 12: "NetworkError", 14: "UnrecoverableError",
}

# All 32 error bits from section 9.2.15
ERROR_BIT_NAMES = [
    "PWM evaluation error",
    "Pulse width modulator error",
    "Gate driver error",
    "Overcurrent primary",
    "Overcurrent DC link",
    "Overvoltage DC link",
    "Undervoltage DC link",
    "Capacitive switching out of range",
    "Overtemp: transformer",
    "Overtemp: heatsink",
    "Overtemp: pyrometer",
    "Overtemp: thermocouple",
    "Coolant flow below minimum",
    "Reserved (bit 13)",
    "Sensor cable open / not connected",
    "Sensor data implausible",
    "24V supply: limit violated",
    "Internal 15V supply: limit violated",
    "Internal 5V supply: limit violated",
    "Internal 3.3V (digital): limit violated",
    "Internal 3.0V (analog): limit violated",
    "Isolated supply: limit violated",
    "Gate driver supply: limit violated",
    "Reserved (bit 23)",
    "Reserved (bit 24)",
    "Safety: locking could not be verified",
    "Safety: power blocked by interlock",
    "Communication error (heartbeat)",
    "Communication error (bus master timeout)",
    "Memory error: stack overflow",
    "Program error / invalid data",
    "Undefined error in program/communication",
]

# Heating phase struct (40 bytes each, section 9.6)
# Format: B B B B I I I I I I I I I I  (little-endian)
# Fields: Mode Forwarding CtrlMode Status Current Power Time EnergySetpoint EnergyMin EnergyMax TempSetpoint TempMin TempMax _pad
PHASE_STRUCT = "<BBBBI IIII II II"
PHASE_SIZE   = 40

def _default_phase(phase_idx=0):
    return {
        "mode":        0,    # 0=energy, 1=temperature
        "forwarding":  0,    # 0=wait for time, 1=switch when setpoint reached
        "ctrl_mode":   1,    # 0=current, 1=power
        "active":      1,    # 0=active, 1=inactive (yes, inverted in protocol)
        "current_pm":  0,    # per-mille of max current
        "power_pm":    0,    # per-mille of max power
        "time_ms":     5000, # ms
        "energy_sp":   0,    # Ws
        "energy_min":  0,    # Ws
        "energy_max":  0,    # Ws
        "temp_sp":     0,    # °C
        "temp_min":    0,    # °C
        "temp_max":    9999, # °C
    }

def _pack_phase(p: dict) -> bytes:
    """Pack a phase dict into 40 bytes."""
    raw = struct.pack(
        PHASE_STRUCT,
        p["mode"], p["forwarding"], p["ctrl_mode"], p["active"],
        p["current_pm"], p["power_pm"], p["time_ms"],
        p["energy_sp"], p["energy_min"], p["energy_max"],
        p["temp_sp"], p["temp_min"], p["temp_max"],
    )
    # Pad to 40 bytes
    return raw + b"\x00" * (PHASE_SIZE - len(raw))

def _unpack_phase(data: bytes, offset: int) -> dict:
    """Unpack 40 bytes at offset into a phase dict."""
    vals = struct.unpack_from(PHASE_STRUCT, data, offset)
    return {
        "mode": vals[0], "forwarding": vals[1], "ctrl_mode": vals[2], "active": vals[3],
        "current_pm": vals[4], "power_pm": vals[5], "time_ms": vals[6],
        "energy_sp": vals[7], "energy_min": vals[8], "energy_max": vals[9],
        "temp_sp": vals[10], "temp_min": vals[11], "temp_max": vals[12],
    }

# ─────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────
_lock = threading.Lock()

_ctrl = {
    "heating_on":   False,
    "ctrl_mode":    0,      # 0=manual, 1=auto
    "temp_source":  2,      # bits 2-3: 0=off, 1=thermocouple, 2=pyrometer, 3=both
    "reset_energy": False,
    "ack_error":    False,
    "heartbeat":    False,
    "current_sp":   0.0,    # % of max
    "power_sp":     0.0,    # % of max
    "heatprog_no":  0,
    "setpoint_c":   0.0,    # °C — for mock sim + display
}

_status = {
    "ready":          False,
    "active":         False,
    "error":          False,
    "estop":          False,
    "prog_done":      False,
    "prog_error":     False,
    "fsm_state":      "Unknown",
    "actual_temp":    25.0,
    "actual_power":   0.0,
    "actual_current": 0.0,
    "actual_freq":    0.0,
    "cap_voltage":    0.0,
    "dc_voltage":     0.0,
    "actual_energy":  0.0,
    "water_flow":     0.0,
    "error_word":     0,
    "error_bits":     [],   # list of {"bit": n, "name": "..."}
    "status_word":    0,
    "error":          None,
}

# In-memory heating program store (100 programs × 8 phases)
_programs: dict[int, list[dict]] = {}

def _empty_program(prog_no: int) -> list[dict]:
    return [_default_phase(i) for i in range(config.FURNACE_NUM_PHASES)]

# Last raw TX / RX bytes for the protocol inspector
_last_tx_bytes: bytes = b"\x00" * 28
_last_rx_bytes: bytes = b"\x00" * INPUT_PACKET_SIZE

# ─────────────────────────────────────────────────────────────
# Packet builders / parsers
# ─────────────────────────────────────────────────────────────
def _build_ctrl_word(ctrl: dict, hb: bool) -> int:
    cw = 0
    if ctrl["heating_on"]:   cw |= BIT_HEATING_ON
    if ctrl["ctrl_mode"]:    cw |= BIT_CTRL_MODE
    cw |= ((ctrl["temp_source"] & 0x3) << 2)
    if ctrl["reset_energy"]: cw |= BIT_RESET_ENERGY
    if ctrl["ack_error"]:    cw |= BIT_ACK_ERROR
    if hb:                   cw |= BIT_HEARTBEAT
    return cw

def _build_output_packet(ctrl: dict, hb: bool) -> bytes:
    cw = _build_ctrl_word(ctrl, hb)
    pkt  = PREFIX
    pkt += struct.pack(">H", cw)
    pkt += struct.pack(">f", ctrl["current_sp"])
    pkt += struct.pack(">f", ctrl["power_sp"])
    pkt += struct.pack(">H", max(0, int(ctrl["heatprog_no"])))
    pkt += SUFFIX
    return pkt  # 28 bytes

def _decode_error_bits(err_word: int) -> list[dict]:
    active = []
    for bit in range(32):
        if err_word & (1 << bit):
            active.append({"bit": bit, "name": ERROR_BIT_NAMES[bit]})
    return active

def _parse_input_packet(data: bytes) -> dict | None:
    if len(data) < 58: # Minimum to get through basic status fields
        with _lock:
            _status["error"] = f"Packet too short: {len(data)} bytes (expected ~134)"
        return None
    # Pad data with zeros if it's shorter than the full structure to prevent unpack errors
    if len(data) < INPUT_PACKET_SIZE:
        data = data.ljust(INPUT_PACKET_SIZE, b"\x00")

    if data[:8] != PREFIX:
        bad_pre = data[:8].decode("ascii", errors="replace")
        with _lock:
            _status["error"] = f"Bad signature: pfx='{bad_pre}'"
        return None
    try:
        status_w = struct.unpack_from(">H", data, 8)[0]
        ctrl_sw  = struct.unpack_from(">H", data, 10)[0]
        i_actual = struct.unpack_from(">f", data, 12)[0]
        freq     = struct.unpack_from(">f", data, 20)[0]
        power    = struct.unpack_from(">f", data, 24)[0]
        cap_v    = struct.unpack_from(">f", data, 28)[0]
        dc_v     = struct.unpack_from(">f", data, 32)[0]
        energy   = struct.unpack_from(">f", data, 36)[0]
        water    = struct.unpack_from(">f", data, 40)[0]
        temp     = struct.unpack_from(">H", data, 44)[0]
        fsm_raw  = struct.unpack_from(">H", data, 52)[0]
        err_word = struct.unpack_from(">I", data, 54)[0]
        
        return {
            "ready":          bool(status_w & SBIT_READY),
            "active":         bool(status_w & SBIT_ACTIVE),
            "error":          bool(status_w & SBIT_ERROR),
            "estop":          bool(status_w & SBIT_ESTOP),
            "prog_done":      bool(status_w & SBIT_PROG_DONE),
            "prog_error":     bool(status_w & SBIT_PROG_ERR),
            "heartbeat":      bool(status_w & SBIT_HEARTBEAT),
            "fsm_state":      FSM_STATES.get(fsm_raw, f"State{fsm_raw}"),
            "actual_temp":    float(temp),
            "actual_power":   round(power, 1),
            "actual_current": round(i_actual, 2),
            "actual_freq":    round(freq, 1),
            "cap_voltage":    round(cap_v, 1),
            "dc_voltage":     round(dc_v, 1),
            "actual_energy":  round(energy, 1),
            "water_flow":     round(water, 2),
            "status_word":    status_w,
            "ctrl_status":    ctrl_sw,
            "error_word":     err_word,
            "error_bits":     _decode_error_bits(err_word),
            "error":          None,
        }
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# Service protocol — heating programs (UDP port 4660)
# ─────────────────────────────────────────────────────────────
_SET_PREAMBLE = b"SET_HEATPROG"   # 12 bytes, no null
_GET_PREAMBLE = b"GET_HEATPROG"   # 12 bytes, no null
_PROG_SUFFIX  = b"END_HEATPROG"   # 12 bytes, no null

def _build_set_heatprog(prog_no: int, phases: list[dict]) -> bytes:
    """Build a SET_HEATPROG service telegram (347 bytes)."""
    pkt  = _SET_PREAMBLE
    pkt += struct.pack("<H", prog_no)
    for ph in phases:
        pkt += _pack_phase(ph)
    pkt += _PROG_SUFFIX
    return pkt

def _build_get_heatprog(prog_no: int) -> bytes:
    """Build a GET_HEATPROG request telegram (26 bytes)."""
    return _GET_PREAMBLE + struct.pack("<H", prog_no) + _PROG_SUFFIX

def _parse_heatprog_response(data: bytes) -> list[dict] | None:
    if len(data) < 347:
        return None
    # preamble (12) + prog_no (2) + 8×phase (320) + suffix (12) = 346 bytes, 0-indexed last = 345
    phases = []
    offset = 14   # skip 12-byte preamble + 2-byte prog_no
    for _ in range(config.FURNACE_NUM_PHASES):
        phases.append(_unpack_phase(data, offset))
        offset += PHASE_SIZE
    return phases

def _service_send_recv(pkt: bytes) -> bytes | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(config.FURNACE_TIMEOUT)
        sock.sendto(pkt, (config.FURNACE_IP, config.FURNACE_SERVICE_PORT))
        data, _ = sock.recvfrom(512)
        return data
    except Exception:
        return None
    finally:
        sock.close()

# ─────────────────────────────────────────────────────────────
# Mock RX packet builder (for inspector in mock mode)
# ─────────────────────────────────────────────────────────────
def _build_mock_rx_packet(st: dict) -> bytes:
    """Build a plausible 134-byte ICC→PLC status packet from _status."""
    sw = 0
    if st.get("ready"):     sw |= SBIT_READY
    if st.get("active"):    sw |= SBIT_ACTIVE
    if st.get("error"):     sw |= SBIT_ERROR
    if st.get("estop"):     sw |= SBIT_ESTOP
    if st.get("prog_done"): sw |= SBIT_PROG_DONE
    if st.get("prog_error"):sw |= SBIT_PROG_ERR

    fsm_rev = {v: k for k, v in FSM_STATES.items()}
    fsm_raw = fsm_rev.get(st.get("fsm_state", "Ready"), 9)

    pkt  = PREFIX                                              # bytes 0-7
    pkt += struct.pack(">H", sw)                              # bytes 8-9   status_word (big-endian to match parser)
    pkt += b"\x00" * 2                                        # bytes 10-11 reserved
    pkt += struct.pack(">f", float(st.get("actual_current", 0))) # bytes 12-15 (big-endian to match parser)
    pkt += b"\x00" * 4                                        # bytes 16-19 reserved
    pkt += struct.pack(">f", float(st.get("actual_freq",  0)))   # bytes 20-23 (big-endian to match parser)
    pkt += struct.pack(">f", float(st.get("actual_power", 0)))   # bytes 24-27 (big-endian to match parser)
    pkt += struct.pack(">f", float(st.get("cap_voltage",  0)))   # bytes 28-31 (big-endian to match parser)
    pkt += struct.pack(">f", float(st.get("dc_voltage",   0)))   # bytes 32-35 (big-endian to match parser)
    pkt += struct.pack(">f", float(st.get("actual_energy",0)))   # bytes 36-39 (big-endian to match parser)
    pkt += struct.pack(">f", float(st.get("water_flow",   0)))   # bytes 40-43 (big-endian to match parser)
    pkt += struct.pack(">H", int(st.get("actual_temp",    0)))   # bytes 44-45 (big-endian to match parser)
    pkt += b"\x00" * 6                                        # bytes 46-51 reserved
    pkt += struct.pack(">H", fsm_raw)                         # bytes 52-53 (big-endian to match parser)
    pkt += struct.pack(">I", int(st.get("error_word",   0)))  # bytes 54-57 (big-endian to match parser)
    # Pad to position 126 (suffix at 126-133)
    pad_len = 126 - len(pkt)
    pkt += b"\x00" * pad_len
    pkt += SUFFIX                                              # bytes 126-133
    # Safety: ensure exactly 134 bytes
    if len(pkt) < INPUT_PACKET_SIZE:
        pkt += b"\x00" * (INPUT_PACKET_SIZE - len(pkt))
    return pkt[:INPUT_PACKET_SIZE]

# ─────────────────────────────────────────────────────────────
# Background IO loops
# ─────────────────────────────────────────────────────────────
def _real_io_loop():
    global _last_tx_bytes, _last_rx_bytes
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(config.FURNACE_TIMEOUT)
    try:
        sock.bind((getattr(config, "HOST_IP", ""), config.FURNACE_PORT_RECV))
    except Exception as e:
        print(f"[furnace driver] Could not bind to HOST_IP: {e}. Falling back to 0.0.0.0")
        sock.bind(("", config.FURNACE_PORT_RECV))
    hb = False
    hb_last = time.time()
    while True:
        time.sleep(0.05)
        now = time.time()
        if now - hb_last >= 1.0:
            hb = not hb
            hb_last = now
        with _lock:
            pkt  = _build_output_packet(_ctrl, hb)
            dest = (config.FURNACE_IP, config.FURNACE_PORT_SEND)
            _last_tx_bytes = pkt
        try:
            sock.sendto(pkt, dest)
            # print(f"[furnace debug] SENT: {pkt.hex()} (hb={hb})")
        except Exception as e:
            with _lock:
                _status["error"] = f"TX error: {e}"
            continue
        try:
            data, addr = sock.recvfrom(512)
            print(f"RECEIVED {len(data)} bytes from {addr}: {data}")
            print(f"[DEBUG] RX len={len(data)}, prefix_ok={data[:8] == PREFIX if len(data) >= 8 else False}, will_store={len(data) >= INPUT_PACKET_SIZE and data[:8] == PREFIX}")
            # update raw bytes even if full parse fails, so UI can show it
            # Only accept data that is at least INPUT_PACKET_SIZE bytes (134) to avoid
            # storing our own 28-byte TX packets as RX data (UDP echo on same port)
            if len(data) >= INPUT_PACKET_SIZE and data[:8] == PREFIX:
                with _lock:
                    _last_rx_bytes = data[:INPUT_PACKET_SIZE]
            
            parsed  = _parse_input_packet(data)
            if parsed:
                with _lock:
                    _status.update(parsed)
                    if parsed.get("ready"):
                        print(f"[furnace debug] Parsed OK - FSM: {parsed['fsm_state']}, Temp: {parsed['actual_temp']}")
        except socket.timeout:
            # print("[furnace debug] No response from furnace...")
            with _lock:
                _status["error"] = f"No response from ICC at {config.FURNACE_IP}"
        except Exception as e:
            print(f"[furnace debug] RX Error: {e}")
            with _lock:
                _status["error"] = f"RX error: {e}"


def _mock_loop():
    global _last_tx_bytes, _last_rx_bytes
    tau = 25.0
    dt  = 0.5
    energy_acc = 0.0
    while True:
        time.sleep(dt)
        with _lock:
            sp     = _ctrl["setpoint_c"] if _ctrl["heating_on"] else 25.0
            actual = _status["actual_temp"]
            new_t  = actual + (sp - actual) * (dt / tau) + random.gauss(0, 0.1)
            new_t  = round(max(20.0, new_t), 2)
            pwr    = round(max(0.0, abs(sp - actual) * 2.8 + random.gauss(0, 0.5)), 1) if _ctrl["heating_on"] else 0.0
            energy_acc += pwr * dt
            _status.update({
                "actual_temp":    new_t,
                "actual_power":   pwr,
                "actual_current": round(pwr / 230.0, 2),
                "actual_freq":    round(62000 + random.gauss(0, 20), 0),
                "cap_voltage":    round(random.gauss(340, 2), 1),
                "dc_voltage":     round(random.gauss(560, 3), 1),
                "actual_energy":  round(energy_acc, 0),
                "water_flow":     round(3.2 + random.gauss(0, 0.04), 2),
                "ready":          True,
                "active":         _ctrl["heating_on"],
                "fsm_state":      "Active" if _ctrl["heating_on"] else "Ready",
                "error":          None,
                "error_word":     0,
                "error_bits":     [],
            })
            # Update mock TX bytes
            _last_tx_bytes = _build_output_packet(_ctrl, False)
            # Build a plausible mock RX packet from current _status
            _last_rx_bytes = _build_mock_rx_packet(_status)

# ─────────────────────────────────────────────────────────────
# Start background thread
# ─────────────────────────────────────────────────────────────
# When Flask debug mode is enabled, the reloader forks a child process and
# re-imports all modules. We only want ONE IO thread, so we check for the
# WERKZEUG_RUN_MAIN env var which is set in the child process.
import os
_is_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"

if _is_reloader_child:
    # This is the Flask reloader child process - start the IO thread
    if config.MOCK:
        threading.Thread(target=_mock_loop, daemon=True).start()
    else:
        threading.Thread(target=_real_io_loop, daemon=True).start()

# ─────────────────────────────────────────────────────────────
# Public API — main control
# ─────────────────────────────────────────────────────────────
def get_status() -> dict:
    with _lock:
        return {
            "enabled":        _ctrl["heating_on"],
            "ctrl_mode":      _ctrl["ctrl_mode"],
            "setpoint":       _ctrl["setpoint_c"],
            "heatprog_no":    _ctrl["heatprog_no"],
            "actual":         _status["actual_temp"],
            "actual_power":   _status["actual_power"],
            "actual_current": _status["actual_current"],
            "actual_freq":    _status["actual_freq"],
            "cap_voltage":    _status["cap_voltage"],
            "dc_voltage":     _status["dc_voltage"],
            "actual_energy":  _status["actual_energy"],
            "water_flow":     _status["water_flow"],
            "fsm_state":      _status["fsm_state"],
            "ready":          _status["ready"],
            "active":         _status["active"],
            "estop":          _status["estop"],
            "prog_done":      _status["prog_done"],
            "prog_error":     _status["prog_error"],
            "status_word":    _status["status_word"],
            "error_word":     _status["error_word"],
            "error_bits":     list(_status["error_bits"]),
            "error":          _status["error"],
        }

def set_setpoint(setpoint: float) -> dict:
    sp = max(config.FURNACE_MIN_SP, min(config.FURNACE_MAX_SP, float(setpoint)))
    with _lock:
        _ctrl["setpoint_c"] = sp
        pct = (sp / config.FURNACE_MAX_SP) * 100.0
        _ctrl["power_sp"]   = round(pct, 2)
        _ctrl["current_sp"] = round(pct, 2)
    return {"ok": True, "error": None}

def set_enable(enable: bool) -> dict:
    with _lock:
        _ctrl["heating_on"] = bool(enable)
    return {"ok": True, "error": None}

def set_mode(mode: int, prog_no: int = 0) -> dict:
    """Set ICC operating mode: 0=manual, 1=auto (heating program)."""
    with _lock:
        _ctrl["ctrl_mode"]   = int(mode)
        _ctrl["heatprog_no"] = int(prog_no)
    return {"ok": True, "error": None}

def set_manual_control(power_pct: float, current_pct: float) -> dict:
    """
    Manual mode: set power and current setpoints as % of maximum.
    In the COBES cyclic telegram, bytes 11-14 = current_sp (REAL, %),
    bytes 15-18 = power_sp (REAL, %). Temperature target does NOT exist
    in the cyclic telegram — temperature is measurement-only in manual mode.
    """
    with _lock:
        _ctrl["ctrl_mode"]   = 0   # ensure manual
        _ctrl["power_sp"]    = max(0.0, min(100.0, float(power_pct)))
        _ctrl["current_sp"]  = max(0.0, min(100.0, float(current_pct)))
    return {"ok": True, "error": None}

def start_program(prog_no: int) -> dict:
    """Switch to auto mode with the given program number and enable heating."""
    with _lock:
        _ctrl["ctrl_mode"]   = 1
        _ctrl["heatprog_no"] = int(prog_no)
        _ctrl["heating_on"]  = True
    return {"ok": True, "error": None}

def acknowledge_error() -> dict:
    with _lock:
        _ctrl["ack_error"] = True
    time.sleep(0.15)
    with _lock:
        _ctrl["ack_error"] = False
    return {"ok": True}

def reset_energy_meter() -> dict:
    with _lock:
        _ctrl["reset_energy"] = True
        _status["actual_energy"] = 0.0
    time.sleep(0.15)
    with _lock:
        _ctrl["reset_energy"] = False
    return {"ok": True}

# ─────────────────────────────────────────────────────────────
# Public API — heating programs
# ─────────────────────────────────────────────────────────────
def get_program(prog_no: int) -> dict:
    """
    Read heating program N from ICC (or in-memory cache in mock mode).
    Returns {"ok": True, "phases": [...]} or {"ok": False, "error": "..."}
    """
    n = int(prog_no)
    if config.MOCK:
        with _lock:
            if n not in _programs:
                _programs[n] = _empty_program(n)
            return {"ok": True, "phases": list(_programs[n])}

    pkt  = _build_get_heatprog(n)
    resp = _service_send_recv(pkt)
    if resp is None:
        return {"ok": False, "error": f"No response from ICC at {config.FURNACE_IP}:{config.FURNACE_SERVICE_PORT}"}
    phases = _parse_heatprog_response(resp)
    if phases is None:
        return {"ok": False, "error": "Malformed GET_HEATPROG response"}
    with _lock:
        _programs[n] = phases
    return {"ok": True, "phases": phases}

def set_program(prog_no: int, phases: list[dict]) -> dict:
    """
    Write heating program N to ICC (and to in-memory cache).
    phases must be a list of exactly 8 phase dicts.
    """
    n = int(prog_no)
    if len(phases) != config.FURNACE_NUM_PHASES:
        return {"ok": False, "error": f"Must supply exactly {config.FURNACE_NUM_PHASES} phases"}

    with _lock:
        _programs[n] = phases

    if config.MOCK:
        return {"ok": True}

    pkt  = _build_set_heatprog(n, phases)
    resp = _service_send_recv(pkt)
    if resp is None:
        return {"ok": False, "error": f"No response from ICC (SET_HEATPROG)"}
    return {"ok": True}

def list_programs() -> dict:
    """Return set of program numbers that have been loaded into memory."""
    with _lock:
        return {"ok": True, "loaded": sorted(_programs.keys())}


# ─────────────────────────────────────────────────────────────
# Public API — Protocol Inspector
# ─────────────────────────────────────────────────────────────
def _hex(data: bytes, offset: int, length: int) -> str:
    return "0x" + data[offset:offset+length].hex().upper()

def _bits_info(word: int, bit_defs: list[tuple]) -> list[dict]:
    """bit_defs: list of (bit_index, name) tuples."""
    return [
        {"bit": idx, "name": name, "value": bool(word & (1 << idx))}
        for idx, name in bit_defs
    ]

_CTRL_BITS = [
    (0,  "HEATING_ON"),
    (1,  "CTRL_MODE (0=manual, 1=auto)"),
    (2,  "TEMP_SRC bit0"),
    (3,  "TEMP_SRC bit1"),
    (4,  "reserved"),
    (5,  "reserved"),
    (6,  "reserved"),
    (7,  "RESET_ENERGY"),
    (8,  "ACK_ERROR"),
    (9,  "reserved"),
    (10, "reserved"),
    (11, "reserved"),
    (12, "reserved"),
    (13, "reserved"),
    (14, "reserved"),
    (15, "HEARTBEAT"),
]

_STATUS_BITS = [
    (0,  "READY"),
    (1,  "ACTIVE"),
    (2,  "ERROR"),
    (3,  "PROG_DONE"),
    (4,  "PROG_ERR"),
    (5,  "reserved"),
    (6,  "reserved"),
    (7,  "reserved"),
    (8,  "E-STOP"),
    (9,  "reserved"),
    (10, "reserved"),
    (11, "reserved"),
    (12, "reserved"),
    (13, "reserved"),
    (14, "reserved"),
    (15, "HEARTBEAT"),
]

def get_raw_packets() -> dict:
    """
    Return annotated TX (28 bytes) and RX (134 bytes) packet data for the
    Protocol Inspector UI.

    Each direction has:
      "bytes"  : list of integers (0-255), one per byte
      "fields" : list of field descriptors:
          { "name", "offset", "length", "fmt", "raw_hex", "decoded",
            "bits" (only for word fields) }
    """
    with _lock:
        tx = bytes(_last_tx_bytes)
        rx = bytes(_last_rx_bytes)

    # ── TX (PLC → ICC, 28 bytes) ───────────────────────────────
    # Ensure even the inspection values use Big-Endian packing to match reality
    cw = struct.unpack_from(">H", tx, 8)[0] if len(tx) >= 10 else 0
    ci = struct.unpack_from(">f", tx, 10)[0] if len(tx) >= 14 else 0.0
    pi = struct.unpack_from(">f", tx, 14)[0] if len(tx) >= 18 else 0.0
    pn = struct.unpack_from(">H", tx, 18)[0] if len(tx) >= 20 else 0

    cw_parts = []
    if cw & BIT_HEATING_ON:   cw_parts.append("HEATING_ON")
    if cw & BIT_CTRL_MODE:    cw_parts.append("CTRL_MODE")
    if cw & (0x3 << 2):      cw_parts.append("SEL_TC")
    if cw & BIT_RESET_ENERGY: cw_parts.append("RESET_ENERGY")
    if cw & BIT_ACK_ERROR:    cw_parts.append("ACK_ERROR")
    if cw & BIT_HEARTBEAT:    cw_parts.append("HEARTBEAT")

    cw_decoded = " | ".join(cw_parts) if cw_parts else "—"

    tx_fields = [
        {"name": "Prefix (BSTSTART)",  "offset": 0,  "length": 8,
         "fmt": "ASCII", "raw_hex": _hex(tx, 0, 8),
         "decoded": tx[0:8].decode("ascii", errors="replace")},
        {"name": "Control Word",       "offset": 8,  "length": 2,
         "fmt": "uint16-BE", "raw_hex": _hex(tx, 8, 2),
         "decoded": cw_decoded,
         "bits": _bits_info(cw, _CTRL_BITS)},
        {"name": "Current Setpoint %", "offset": 10, "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(tx, 10, 4),
         "decoded": f"{ci:.2f} %"},
        {"name": "Power Setpoint %",   "offset": 14, "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(tx, 14, 4),
         "decoded": f"{pi:.2f} %"},
        {"name": "Program Number",     "offset": 18, "length": 2,
         "fmt": "uint16-BE", "raw_hex": _hex(tx, 18, 2),
         "decoded": str(pn)},
        {"name": "Suffix (BSTENDTX)",  "offset": 20, "length": 8,
         "fmt": "ASCII", "raw_hex": _hex(tx, 20, 8),
         "decoded": tx[20:28].decode("ascii", errors="replace")},
    ]

    # ── RX (ICC → PLC, 134 bytes) ─────────────────────────────
    if len(rx) >= INPUT_PACKET_SIZE:
        sw     = struct.unpack_from(">H", rx, 8)[0]
        i_act  = struct.unpack_from(">f", rx, 12)[0]
        freq   = struct.unpack_from(">f", rx, 20)[0]
        power  = struct.unpack_from(">f", rx, 24)[0]
        cap_v  = struct.unpack_from(">f", rx, 28)[0]
        dc_v   = struct.unpack_from(">f", rx, 32)[0]
        energy = struct.unpack_from(">f", rx, 36)[0]
        water  = struct.unpack_from(">f", rx, 40)[0]
        temp   = struct.unpack_from(">H", rx, 44)[0]
        fsm    = struct.unpack_from(">H", rx, 52)[0]
        err_w  = struct.unpack_from(">I", rx, 54)[0]
    else:
        sw = i_act = freq = power = cap_v = dc_v = energy = water = 0
        temp = fsm = err_w = 0

    sw_parts = []
    if sw & SBIT_READY:     sw_parts.append("READY")
    if sw & SBIT_ACTIVE:    sw_parts.append("ACTIVE")
    if sw & SBIT_ERROR:     sw_parts.append("ERROR")
    if sw & SBIT_PROG_DONE: sw_parts.append("PROG_DONE")
    if sw & SBIT_PROG_ERR:  sw_parts.append("PROG_ERR")
    if sw & SBIT_ESTOP:     sw_parts.append("E-STOP")
    if sw & SBIT_HEARTBEAT: sw_parts.append("HEARTBEAT")
    sw_decoded = " | ".join(sw_parts) if sw_parts else "—"

    fsm_name = FSM_STATES.get(fsm, f"State{fsm}")
    err_bits_active = [
        {"bit": b, "name": ERROR_BIT_NAMES[b], "value": True}
        for b in range(32) if err_w & (1 << b)
    ]
    # Add inactive bits for completeness
    err_bits_all = [
        {"bit": b, "name": ERROR_BIT_NAMES[b], "value": bool(err_w & (1 << b))}
        for b in range(32)
    ]

    rx_fields = [
        {"name": "Prefix (BSTSTART)",   "offset": 0,   "length": 8,
         "fmt": "ASCII", "raw_hex": _hex(rx, 0, 8),
         "decoded": rx[0:8].decode("ascii", errors="replace")},
        {"name": "Status Word",         "offset": 8,   "length": 2,
         "fmt": "uint16-BE", "raw_hex": _hex(rx, 8, 2),
         "decoded": sw_decoded,
         "bits": _bits_info(sw, _STATUS_BITS)},
        {"name": "Controller Status",   "offset": 10,  "length": 2,
         "fmt": "uint16-BE", "raw_hex": _hex(rx, 10, 2), "decoded": "—"},
        {"name": "Actual Current (A)",  "offset": 12,  "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 12, 4),
         "decoded": f"{i_act:.3f} A"},
        {"name": "reserved",            "offset": 16,  "length": 4,
         "fmt": "bytes", "raw_hex": _hex(rx, 16, 4), "decoded": "—"},
        {"name": "Actual Frequency (Hz)","offset": 20, "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 20, 4),
         "decoded": f"{freq:.0f} Hz"},
        {"name": "Actual Power (W)",    "offset": 24,  "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 24, 4),
         "decoded": f"{power:.1f} W"},
        {"name": "Capacitor Voltage (V)","offset": 28, "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 28, 4),
         "decoded": f"{cap_v:.1f} V"},
        {"name": "DC Link Voltage (V)", "offset": 32,  "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 32, 4),
         "decoded": f"{dc_v:.1f} V"},
        {"name": "Energy (Ws)",         "offset": 36,  "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 36, 4),
         "decoded": f"{energy:.1f} Ws"},
        {"name": "Water Flow (l/min)",  "offset": 40,  "length": 4,
         "fmt": "float32-BE", "raw_hex": _hex(rx, 40, 4),
         "decoded": f"{water:.2f} l/min"},
        {"name": "Temperature (°C)",    "offset": 44,  "length": 2,
         "fmt": "uint16-BE", "raw_hex": _hex(rx, 44, 2),
         "decoded": f"{temp} °C"},
        {"name": "reserved",            "offset": 46,  "length": 6,
         "fmt": "bytes", "raw_hex": _hex(rx, 46, 6), "decoded": "—"},
        {"name": "FSM State",           "offset": 52,  "length": 2,
         "fmt": "uint16-BE", "raw_hex": _hex(rx, 52, 2),
         "decoded": f"{fsm_name} (raw {fsm})"},
        {"name": "Error Word",          "offset": 54,  "length": 4,
         "fmt": "uint32-BE", "raw_hex": _hex(rx, 54, 4),
         "decoded": f"0x{err_w:08X}" + (" — " + ", ".join(e["name"] for e in err_bits_active) if err_bits_active else " — no errors"),
         "bits": err_bits_all},
        {"name": "Payload (reserved)",  "offset": 58,  "length": 68,
         "fmt": "bytes", "raw_hex": _hex(rx, 58, 68), "decoded": f"68 reserved bytes"},
        {"name": "Suffix (BSTENDTX)",   "offset": 126, "length": 8,
         "fmt": "ASCII", "raw_hex": _hex(rx, 126, 8),
         "decoded": rx[126:134].decode("ascii", errors="replace")},
    ]

    return {
        "tx": {"bytes": list(tx), "fields": tx_fields,  "total": len(tx)},
        "rx": {"bytes": list(rx), "fields": rx_fields, "total": len(rx)},
    }

