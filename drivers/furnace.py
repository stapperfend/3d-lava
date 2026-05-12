"""
drivers/furnace.py  —  COBES i-class compact Induction Furnace driver
======================================================================
Protocol source: 800 0031.03_BA_Bedienungsanleitung i-class compact.pdf

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

_DriverThread = threading.Thread
_DriverLock = threading.Lock
_DriverSocket = socket.socket
_sleep = time.sleep

# ─────────────────────────────────────────────────────────────
# Packet constants
# ─────────────────────────────────────────────────────────────
PREFIX = b"BSTSTART"
SUFFIX = b"BSTENDTX"
ALT_SUFFIX = b"\x00\x00BSTEND"
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
    0: "Reserved [NONE]", 1: "Reserved [_ANY]",
    2: "Init", 3: "InitNetwork", 4: "StartNetServices",
    5: "WaitForBusMaster", 6: "PrepareOnBoardSupplies", 7: "PrepareGateDrivers",
    8: "WaitForDCLinkCharged", 9: "Ready", 10: "Active",
    11: "Error", 12: "NetworkError", 13: "UnrecoverableError",
    14: "Interlock",
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

# Process status bits (9.2.2)
REGULATION_MODES = {
    0: "Controller inactive / Power output disabled",
    1: "Current limitation",
    2: "Power limitation",
    3: "Voltage limitation (U_Cap)",
    4: "Phase angle limitation φ",
    5: "Switching loss limitation PV",
    6: "Frequency limitation",
}

CTRL_STATUS_BITS = [
    (4, "Minimum limit violation (H-Prog)"),
    (5, "Maximum limit violation (H-Prog)"),
]

# Heating phase struct (40 bytes each, section 9.6)
# Format: B B B B I I I I I I I I I I  (Big-Endian)
# Fields: Mode Forwarding CtrlMode Status Current Power Time EnergySetpoint EnergyMin EnergyMax TempSetpoint TempMin TempMax _pad
PHASE_STRUCT = ">BBBBI IIII II II"
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
_lock = _DriverLock()

_ctrl = {
    "heating_on":   False,
    "ctrl_mode":    0,      # 0=manual, 1=auto
    "temp_source": 2,      # bits 2-3: 0=off, 1=thermocouple, 2=pyrometer, 3=both
    "reset_energy": False,
    "ack_error":    False,
    "heartbeat":    False,
    "current_sp":   0.0,    # % of max
    "power_sp":     0.0,    # % of max
    "heatprog_no":  0,
}

# Completion tracking
_last_prog_done = False
_ctrl["setpoint_c"] = 0.0    # °C — for mock sim + display

_status = {
    "connected":      False,
    "comm_error":     "No valid furnace packet received yet",
    "actual_temp":    0.0,
    "actual":         0.0,
    "furnace_pyrometer_temp_c": 0.0,
    "actual_power":   0.0,
    "actual_current": 0.0,
    "actual_freq":    0.0,
    "phase_angle":    0.0,
    "cap_voltage":    0.0,
    "dc_voltage":     0.0,
    "actual_energy":  0.0,
    "water_flow":     0.0,
    "fsm_state":      "Init",
    "ready":          False,
    "active":         False,
    "icc_error":      False,
    "estop":          False,
    "prog_done":      False,
    "prog_error":     False,
    "heartbeat":      False,
    "error_word":     0,
    "error_bits":     [],
    "status_word":    0,
    "last_error_msg": None,
    "heating_program": 0,
    "heating_program_phase": 0,
    "error_word_prog": 0,
    "phase_energies":  [0] * 8,
    "phase_temps":     [0.0] * 8,
    "tx_count":        0,
    "rx_count":        0,
    "parse_failures":  0,
    "last_tx_time":    0.0,
    "last_rx_time":    0.0,
    "last_valid_rx_time": 0.0,
    "last_rx_len":     0,
    "last_rx_addr":    None,
    "last_rx_prefix":  "",
}

# In-memory heating program store (100 programs × 8 phases)
_programs: dict[int, list[dict]] = {}

def _empty_program(prog_no: int) -> list[dict]:
    return [_default_phase(i) for i in range(config.FURNACE_NUM_PHASES)]

_last_tx_bytes: bytes = b"\x00" * 28
_last_rx_bytes: bytes = b"\x00" * INPUT_PACKET_SIZE
_last_any_rx_bytes: bytes = b""

# Console buffer (Service 4661)
_console_logs = []
_console_lock = _DriverLock()
_console_started = False
_background_started = False
_background_lock = _DriverLock()

def _make_udp_socket(label: str, bind_port: int = 0, timeout: float | None = None,
                     force_wildcard: bool = False, preferred_ip: str | None = None) -> socket.socket:
    """Create a UDP socket, preferring a configured local IP but falling back to wildcard."""
    sock = _DriverSocket(socket.AF_INET, socket.SOCK_DGRAM)
    if timeout is not None:
        sock.settimeout(timeout)

    host_ip = "" if force_wildcard else (preferred_ip if preferred_ip is not None else getattr(config, "HOST_IP", ""))
    if host_ip:
        try:
            sock.bind((host_ip, bind_port))
            return sock
        except OSError as e:
            print(f"[{label}] Could not bind to {host_ip}:{bind_port}: {e}. Falling back to 0.0.0.0")

    sock.bind(("", bind_port))
    return sock

def _sendto_or_rebind(sock: socket.socket, payload: bytes, dest: tuple[str, int], label: str,
                      bind_port: int = 0, timeout: float | None = None) -> socket.socket:
    """Send UDP data; if Windows rejects the bound address, retry on wildcard."""
    try:
        sock.sendto(payload, dest)
        return sock
    except OSError as e:
        if getattr(e, "winerror", None) != 10049:
            raise
        host_ip = getattr(config, "HOST_IP", "")
        print(f"[{label}] HOST_IP {host_ip!r} cannot reach {dest[0]}:{dest[1]} ({e}). Retrying with 0.0.0.0")
        try:
            sock.close()
        except Exception:
            pass
        retry = _make_udp_socket(label, bind_port=bind_port, timeout=timeout, force_wildcard=True)
        retry.sendto(payload, dest)
        return retry

def _console_loop():
    """Background thread to listen for console messages on port 4661."""
    global _console_logs
    sock = _make_udp_socket("furnace console", timeout=2.0)

    # Initiate session
    dest = (config.FURNACE_IP, getattr(config, "FURNACE_CONSOLE_PORT", 4661))
    print(f"[furnace console] Session starting on port {dest[1]}...")
    sock = _sendto_or_rebind(sock, b"HELLO\r\n", dest, "furnace console", timeout=2.0)
    last_hello = time.time()

    while True:
        try:
            if time.time() - last_hello > 5.0:
                sock = _sendto_or_rebind(sock, b"HELLO\r\n", dest, "furnace console", timeout=2.0)
                last_hello = time.time()
            data, addr = sock.recvfrom(4096)
            msg = data.decode("ascii", errors="replace").strip()
            if msg:
                timestamp = time.strftime("%H:%M:%S")
                with _console_lock:
                    _console_logs.append(f"[{timestamp}] {msg}")
                    # Keep last 500 lines
                    if len(_console_logs) > 500:
                        _console_logs.pop(0)
        except socket.timeout:
            # Re-ping if it goes quiet? Manual says "activates", but let's be safe
            # Actually, just keep waiting. If we want to restart, we can send another HELLO.
            pass
        except Exception as e:
            print(f"[furnace console] Error: {e}")
            _sleep(1.0)

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
    packet_len = len(data)
    if packet_len < 60: # Minimum to get through error_word field
        with _lock:
            _status["last_error_msg"] = f"Packet too short: {packet_len} bytes (expected {INPUT_PACKET_SIZE})"
        return None

    # Some ICC firmware revisions omit trailing bytes. The fields used by the
    # dashboard are present before the suffix, so preserve the old tolerant path.
    if packet_len < INPUT_PACKET_SIZE:
        data = data.ljust(INPUT_PACKET_SIZE, b"\x00")

    if data[:8] != PREFIX:
        bad_pre = data[:8].decode("ascii", errors="replace")
        with _lock:
            _status["last_error_msg"] = f"Bad signature: pfx='{bad_pre}'"
        return None
    
    # Suffix may be at the physical end or at the canonical 134-byte offset.
    suffix_candidates = (data[packet_len-8:packet_len], data[INPUT_PACKET_SIZE-8:INPUT_PACKET_SIZE])
    if packet_len >= 8 and SUFFIX not in suffix_candidates and ALT_SUFFIX not in suffix_candidates:
        bad_sfx = data[max(0, packet_len-8):packet_len].hex()
        with _lock:
            _status["last_error_msg"] = f"Bad suffix: sfx=0x{bad_sfx} at len={packet_len}"
        return None

    try:
        # Table 9.1.2 - 134 bytes total
        is_134 = len(data) >= 134
        
        sw_inverter    = struct.unpack_from(">H", data, 8)[0]
        sw_controller  = struct.unpack_from(">H", data, 10)[0]
        i_actual       = struct.unpack_from(">f", data, 12)[0]
        phase_ang      = struct.unpack_from(">f", data, 16)[0]
        freq           = struct.unpack_from(">f", data, 20)[0]
        power          = struct.unpack_from(">f", data, 24)[0]
        cap_v          = struct.unpack_from(">f", data, 28)[0]
        dc_v           = struct.unpack_from(">f", data, 32)[0]
        energy         = struct.unpack_from(">f", data, 36)[0]
        water          = struct.unpack_from(">f", data, 40)[0]
        temp           = struct.unpack_from(">f", data, 44)[0] # REAL (float32) per Table 9.1.2
        
        prog_no        = struct.unpack_from(">H", data, 48)[0]
        prog_phase     = struct.unpack_from(">H", data, 50)[0]
        fsm_raw        = struct.unpack_from(">H", data, 52)[0]
        err_word_sys   = struct.unpack_from(">I", data, 54)[0] # DWORD
        err_word_prog  = struct.unpack_from(">I", data, 58)[0] # DWORD
        
        # Arrays at 62 and 94
        p_energies = list(struct.unpack_from(">8I", data, 62))
        p_temps    = list(struct.unpack_from(">8f", data, 94))
        
        # Keep the numeric pyrometer packet value for CSV export. The GUI display
        # still applies offset and "Cold" logic below.
        furnace_pyrometer_temp_c = round(temp, 1)
        display_temp = temp + config.FURNACE_TEMP_OFFSET
        if temp < config.FURNACE_COLD_THRESHOLD:
            display_temp = "Cold"
        else:
            display_temp = round(display_temp, 1)

        res = {
            "ready":          bool(sw_inverter & SBIT_READY),
            "active":         bool(sw_inverter & SBIT_ACTIVE),
            "icc_error":      bool(sw_inverter & SBIT_ERROR),
            "estop":          bool(sw_inverter & SBIT_ESTOP),
            "prog_done":      bool(sw_inverter & SBIT_PROG_DONE),
            "prog_error":     bool(sw_inverter & SBIT_PROG_ERR),
            "heartbeat":      bool(sw_inverter & SBIT_HEARTBEAT),
            "fsm_state":      FSM_STATES.get(fsm_raw, f"State{fsm_raw}"),
            "furnace_pyrometer_temp_c": furnace_pyrometer_temp_c,
            "actual_temp":    display_temp,
            "actual":         display_temp,
            "actual_power":   round(power, 1),
            "actual_current": round(i_actual, 2),
            "actual_freq":    round(freq, 1),
            "phase_angle":    round(phase_ang, 1),
            "cap_voltage":    round(cap_v, 1),
            "dc_voltage":     round(dc_v, 1),
            "actual_energy":  round(energy, 1),
            "water_flow":     round(water, 2),
            "status_word":    sw_inverter,
            "process_status": sw_controller,
            "error_word":     err_word_sys,
            "error_word_prog": err_word_prog,
            "heating_program": prog_no,
            "heating_program_phase": prog_phase,
            "error_bits":     _decode_error_bits(err_word_sys),
            "phase_energies":  p_energies,
            "phase_temps":     p_temps,
            "status_fsm_raw": fsm_raw,
            "connected":      True,
            "comm_error":     None,
            "last_error_msg": None,
        }

        # --- AUTO-DISABLE ON COMPLETION ---
        global _last_prog_done
        current_done = bool(sw_inverter & SBIT_PROG_DONE)
        if current_done and not _last_prog_done:
            # Transition to FINISHED detected
            with _lock:
                if _ctrl["ctrl_mode"] == 1:
                    _ctrl["heating_on"] = False
                    _ctrl["ctrl_mode"]  = 0
        _last_prog_done = current_done
        # ----------------------------------

        return res
    except Exception as e:
        with _lock:
            _status["last_error_msg"] = f"Parse error: {e}"
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
    pkt += struct.pack(">H", prog_no)
    for ph in phases:
        pkt += _pack_phase(ph)
    pkt += _PROG_SUFFIX
    return pkt

def _build_get_heatprog(prog_no: int) -> bytes:
    """Build a GET_HEATPROG request telegram (26 bytes)."""
    return _GET_PREAMBLE + struct.pack(">H", prog_no) + _PROG_SUFFIX

def _parse_heatprog_response(data: bytes) -> list[dict] | None:
    # Manual: Preamble(12) + Num(2) + 8*Phase(40) + Suffix(12) = 346 bytes
    if len(data) < 346:
        print(f"[furnace service] Malformed packet: received {len(data)} bytes, expected 346. (Data start: {data[:12].hex()})")
        return None
    
    if data[:12] != _GET_PREAMBLE:
        print(f"[furnace service] Malformed packet: bad preamble '{data[:12].decode('ascii', errors='replace')}'")
        return None

    phases = []
    offset = 14   # skip 12-byte preamble + 2-byte prog_no
    for _ in range(config.FURNACE_NUM_PHASES):
        phases.append(_unpack_phase(data, offset))
        offset += PHASE_SIZE
    return phases

def _service_send_recv(pkt: bytes) -> bytes | None:
    sock = None
    try:
        # Use a more relaxed timeout for the service protocol
        timeout = getattr(config, "FURNACE_SERVICE_TIMEOUT", 2.0)
        sock = _make_udp_socket("furnace service", timeout=timeout)
        dest = (config.FURNACE_IP, config.FURNACE_SERVICE_PORT)
        sock = _sendto_or_rebind(sock, pkt, dest, "furnace service", timeout=timeout)
        data, _ = sock.recvfrom(4096)
        return data
    except socket.timeout:
        # Don't print full stack trace for a timeout
        return None
    except Exception as e:
        print(f"[furnace service] Communication error: {e}")
        return None
    finally:
        if sock is not None:
            sock.close()

# ─────────────────────────────────────────────────────────────
# Mock RX packet builder (for inspector in mock mode)
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Background IO loops
# ─────────────────────────────────────────────────────────────
def _real_io_loop():
    global _last_tx_bytes, _last_rx_bytes, _last_any_rx_bytes
    bind_ip = getattr(config, "FURNACE_BIND_IP", "")
    sock = _make_udp_socket(
        "furnace driver",
        bind_port=config.FURNACE_PORT_RECV,
        timeout=config.FURNACE_TIMEOUT,
        preferred_ip=bind_ip,
    )
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    with _lock:
        _status["bound_addr"] = f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
    hb = False
    hb_last = time.time()
    while True:
        _sleep(0.05)
        now = time.time()
        if now - hb_last >= 1.0:
            hb = not hb
            hb_last = now
        with _lock:
            pkt  = _build_output_packet(_ctrl, hb)
            dest = (config.FURNACE_IP, config.FURNACE_PORT_SEND)
            _last_tx_bytes = pkt
        try:
            new_sock = _sendto_or_rebind(
                sock,
                pkt,
                dest,
                "furnace driver",
                bind_port=config.FURNACE_PORT_RECV,
                timeout=config.FURNACE_TIMEOUT,
            )
            if new_sock is not sock:
                sock = new_sock
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                with _lock:
                    _status["bound_addr"] = f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
            with _lock:
                _status["tx_count"] += 1
                _status["last_tx_time"] = time.time()
            # if hb != hb_last: # Print once per heartbeat toggle (every 1s)
            #     cw_dbg = _build_ctrl_word(_ctrl, hb)
            #     # print(f"[furnace TX] CW=0x{cw_dbg:04X} SP_I={_ctrl['current_sp']}% SP_P={_ctrl['power_sp']}% HB={hb}")
        except Exception as e:
            with _lock:
                _status["connected"] = False
                _status["comm_error"] = f"TX error: {e}"
            continue
        try:
            data, addr = sock.recvfrom(512)
            now_rx = time.time()
            with _lock:
                _last_any_rx_bytes = data
                _status["rx_count"] += 1
                _status["last_rx_time"] = now_rx
                _status["last_rx_len"] = len(data)
                _status["last_rx_addr"] = f"{addr[0]}:{addr[1]}"
                _status["last_rx_prefix"] = data[:8].decode("ascii", errors="replace")

            # Only accept data that has the furnace signature to avoid
            # storing our own 28-byte TX packets as valid RX data if the network echoes.
            if len(data) >= 60 and data[:8] == PREFIX:
                with _lock:
                    _last_rx_bytes = data[:INPUT_PACKET_SIZE].ljust(INPUT_PACKET_SIZE, b"\x00")
            
            parsed  = _parse_input_packet(data)
            if parsed:
                with _lock:
                    _status.update(parsed)
                    _status["last_valid_rx_time"] = now_rx
                    # if hb != hb_last:
                    #     # print(f"[furnace RX] FSM={_status['fsm_state']} (raw {parsed.get('status_fsm_raw')}) DC={_status['dc_voltage']}V")
            else:
                with _lock:
                    _status["parse_failures"] += 1
                    _status["connected"] = False
                    _status["comm_error"] = _status.get("last_error_msg") or "Invalid furnace packet"
        except socket.timeout:
            with _lock:
                last_valid = _status.get("last_valid_rx_time", 0.0)
                if not last_valid or time.time() - last_valid > 1.0:
                    _status["connected"] = False
                    _status["comm_error"] = f"No response from ICC at {config.FURNACE_IP}:{config.FURNACE_PORT_SEND}"
        except Exception as e:
            with _lock:
                _status["connected"] = False
                _status["comm_error"] = f"RX error: {e}"


# ─────────────────────────────────────────────────────────────
# Start background thread
# ─────────────────────────────────────────────────────────────
# When Flask debug mode is enabled, the reloader forks a child process and
# re-imports all modules. We only want ONE IO thread, so we check for the
# WERKZEUG_RUN_MAIN env var which is set in the child process.
def start_background_tasks():
    """Start the background IO and console loops."""
    global _background_started
    with _background_lock:
        if _background_started:
            return
        _DriverThread(target=_real_io_loop, daemon=True).start()
        _DriverThread(target=_console_loop, daemon=True).start()
        _background_started = True

# ─────────────────────────────────────────────────────────────
# Public API — main control
# ─────────────────────────────────────────────────────────────
def get_status() -> dict:
    with _lock:
        now = time.time()
        last_valid_rx = _status.get("last_valid_rx_time", 0.0)
        connected = bool(last_valid_rx and now - last_valid_rx <= 2.0 and not _status.get("comm_error"))
        icc_error = bool(_status.get("icc_error", False))
        status_error = _status.get("comm_error") or _status.get("last_error_msg")
        if not status_error and icc_error:
            status_error = "ICC reports an active error bit"
        return {
            "connected":      connected,
            "enabled":        _ctrl["heating_on"],
            "ctrl_mode":      _ctrl["ctrl_mode"],
            "setpoint":       _ctrl["setpoint_c"],
            "heatprog_no":    _ctrl["heatprog_no"],
            "furnace_pyrometer_temp_c": _status.get("furnace_pyrometer_temp_c"),
            "actual":         _status["actual_temp"],
            "actual_power":   _status["actual_power"],
            "actual_current": _status["actual_current"],
            "actual_freq":    _status["actual_freq"],
            "phase_angle":    _status["phase_angle"],
            "cap_voltage":    _status["cap_voltage"],
            "dc_voltage":     _status["dc_voltage"],
            "actual_energy":  _status["actual_energy"],
            "water_flow":     _status["water_flow"],
            "actual_temp":    _status["actual_temp"],
            "actual":         _status["actual_temp"],
            "fsm_state":      _status["fsm_state"],
            "ready":          _status["ready"],
            "active":         _status["active"],
            "icc_error":      icc_error,
            "estop":          _status["estop"],
            "prog_done":      _status["prog_done"],
            "prog_error":     _status["prog_error"],
            "status_word":    _status["status_word"],
            "process_status": _status.get("process_status", 0),
            "error_word":     _status["error_word"],
            "error_word_prog": _status["error_word_prog"],
            "error_bits":     list(_status["error_bits"]),
            "heating_program": _status.get("heating_program", 0),
            "heating_program_phase": _status.get("heating_program_phase", 0),
            "phase_energies":  list(_status["phase_energies"]),
            "phase_temps":     list(_status["phase_temps"]),
            "error":          status_error,
            "comm_error":     _status.get("comm_error"),
            "last_error_msg": _status.get("last_error_msg"),
            "last_rx_len":    _status.get("last_rx_len", 0),
            "last_rx_addr":   _status.get("last_rx_addr"),
            "last_rx_prefix": _status.get("last_rx_prefix"),
            "tx_count":       _status.get("tx_count", 0),
            "rx_count":       _status.get("rx_count", 0),
            "parse_failures": _status.get("parse_failures", 0),
            "last_valid_rx_age_s": round(now - last_valid_rx, 3) if last_valid_rx else None,
        }

def get_debug_info() -> dict:
    with _lock:
        now = time.time()
        last_tx = _status.get("last_tx_time", 0.0)
        last_rx = _status.get("last_rx_time", 0.0)
        last_valid_rx = _status.get("last_valid_rx_time", 0.0)
        any_rx = bytes(_last_any_rx_bytes)
        return {
            "bind": _status.get("bound_addr") or f"{getattr(config, 'FURNACE_BIND_IP', '')}:{config.FURNACE_PORT_RECV}",
            "target": f"{config.FURNACE_IP}:{config.FURNACE_PORT_SEND}",
            "connected": bool(last_valid_rx and now - last_valid_rx <= 2.0 and not _status.get("comm_error")),
            "comm_error": _status.get("comm_error"),
            "last_error_msg": _status.get("last_error_msg"),
            "tx_count": _status.get("tx_count", 0),
            "rx_count": _status.get("rx_count", 0),
            "parse_failures": _status.get("parse_failures", 0),
            "last_tx_age_s": round(now - last_tx, 3) if last_tx else None,
            "last_rx_age_s": round(now - last_rx, 3) if last_rx else None,
            "last_valid_rx_age_s": round(now - last_valid_rx, 3) if last_valid_rx else None,
            "last_rx_len": _status.get("last_rx_len", 0),
            "last_rx_addr": _status.get("last_rx_addr"),
            "last_rx_prefix": _status.get("last_rx_prefix"),
            "last_rx_hex_head": any_rx[:32].hex(" "),
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
    """
    Toggle heating on/off.
    SMART: if a program > 0 is selected, automatically switch to AUTO mode.
    """
    with _lock:
        e = bool(enable)
        _ctrl["heating_on"] = e
        if e:
            # If we are in AUTO mode (Tab 1), ensure we have a program
            if _ctrl["ctrl_mode"] == 1:
                if _ctrl["heatprog_no"] == 0:
                    # Fallback to manual if no program selected
                    _ctrl["ctrl_mode"] = 0
            # If we are in MANUAL mode (Tab 0), ensure it stays manual
            else:
                _ctrl["ctrl_mode"] = 0
    return {"ok": True, "error": None}

def set_mode(mode: int, prog_no: int = 0) -> dict:
    """
    Set ICC operating mode: 0=manual, 1=auto (heating program).
    STRICT: if switching to manual, ensure we don't accidentally run a program.
    """
    with _lock:
        m = int(mode)
        _ctrl["ctrl_mode"] = m
        if prog_no > 0:
            _ctrl["heatprog_no"] = int(prog_no)
        # Note: we do NOT reset heatprog_no to 0 here because the user wants to see it,
        # but the ICC will ignore it because ctrl_mode is 0.
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

def set_selected_program(prog_no: int) -> dict:
    """Set the target program number without starting it yet."""
    with _lock:
        _ctrl["heatprog_no"] = int(prog_no)
        # If set to 0, ensure we are in manual mode
        if _ctrl["heatprog_no"] == 0:
            _ctrl["ctrl_mode"] = 0
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
    debug = get_debug_info()

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

    # ── RX (ICC → PLC, 132 bytes) ─────────────────────────────
    if len(rx) >= INPUT_PACKET_SIZE:
        sw          = struct.unpack_from(">H", rx, 8)[0]
        proc_st     = struct.unpack_from(">H", rx, 10)[0]
        i_act       = struct.unpack_from(">f", rx, 12)[0]
        phase_ang   = struct.unpack_from(">f", rx, 16)[0]
        freq        = struct.unpack_from(">f", rx, 20)[0]
        power       = struct.unpack_from(">f", rx, 24)[0]
        cap_v       = struct.unpack_from(">f", rx, 28)[0]
        dc_v        = struct.unpack_from(">f", rx, 32)[0]
        energy      = struct.unpack_from(">f", rx, 36)[0]
        water       = struct.unpack_from(">f", rx, 40)[0]
        temp        = struct.unpack_from(">f", rx, 44)[0]
        prog_no     = struct.unpack_from(">H", rx, 48)[0]
        prog_phase  = struct.unpack_from(">H", rx, 50)[0]
        fsm         = struct.unpack_from(">H", rx, 52)[0]
        err_w       = struct.unpack_from(">I", rx, 54)[0]
        err_w_prog  = struct.unpack_from(">I", rx, 58)[0]
    else:
        sw = proc_st = i_act = phase_ang = freq = power = cap_v = dc_v = energy = water = temp = prog_no = prog_phase = fsm = err_w = err_w_prog = 0

    sw_parts = []
    if sw & SBIT_READY:     sw_parts.append("READY")
    if sw & SBIT_ACTIVE:    sw_parts.append("ACTIVE")
    if sw & SBIT_ERROR:     sw_parts.append("ERROR")
    if sw & SBIT_PROG_DONE: sw_parts.append("PROG_DONE")
    if sw & SBIT_PROG_ERR:  sw_parts.append("PROG_ERR")
    if sw & SBIT_ESTOP:     sw_parts.append("E-STOP")
    if sw & SBIT_HEARTBEAT: sw_parts.append("HEARTBEAT")
    sw_decoded = " | ".join(sw_parts) if sw_parts else "—"

    # Controller Status (9.2.2)
    reg_mode = proc_st & 0x0F
    reg_name = REGULATION_MODES.get(reg_mode, f"Reserved ({reg_mode})")
    ctrl_flags = [f["name"] for f in _bits_info(proc_st, CTRL_STATUS_BITS) if f["value"]]
    ctrl_decoded = reg_name + (" | " + " | ".join(ctrl_flags) if ctrl_flags else "")

    fsm_flags = [
        {"bit": s_val, "name": s_name, "value": (fsm == s_val)}
        for s_val, s_name in sorted(FSM_STATES.items())
    ]

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

    # Table 9.1.2 - 134 bytes
    e_p = list(struct.unpack_from(">8I", rx, 62))
    t_p = list(struct.unpack_from(">8f", rx, 94))
    
    rx_fields = [
        {"name": "Prefix", "offset": 0, "length": 8, "fmt": "ASCII", "raw_hex": _hex(rx, 0, 8), "decoded": rx[0:8].decode("ascii", errors="replace")},
        {"name": "StatusWord_Inverter", "offset": 8, "length": 2, "fmt": "uint16-BE", "raw_hex": _hex(rx, 8, 2), "decoded": sw_decoded, "bits": _bits_info(sw, _STATUS_BITS)},
        {"name": "StatusWord_Controller", "offset": 10, "length": 2, "fmt": "uint16-BE", "raw_hex": _hex(rx, 10, 2), "decoded": ctrl_decoded, "bits": _bits_info(proc_st, CTRL_STATUS_BITS)},
        {"name": "PrimaryCurrent (A)", "offset": 12, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 12, 4), "decoded": f"{i_act:.3f} A"},
        {"name": "PhaseAngle (°)", "offset": 16, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 16, 4), "decoded": f"{phase_ang:.1f} °"},
        {"name": "Frequency (Hz)", "offset": 20, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 20, 4), "decoded": f"{freq:.0f} Hz"},
        {"name": "Power (W)", "offset": 24, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 24, 4), "decoded": f"{power:.1f} W"},
        {"name": "CapVoltage (V)", "offset": 28, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 28, 4), "decoded": f"{cap_v:.1f} V"},
        {"name": "DCLinkVoltage (V)", "offset": 32, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 32, 4), "decoded": f"{dc_v:.1f} V"},
        {"name": "EnergyCounter (Ws)", "offset": 36, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 36, 4), "decoded": f"{energy:.1f} Ws"},
        {"name": "CoolingWaterFlow (l/min)", "offset": 40, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 40, 4), "decoded": f"{water:.2f} l/min"},
        {"name": "Temperature (°C)", "offset": 44, "length": 4, "fmt": "float32-BE", "raw_hex": _hex(rx, 44, 4), "decoded": f"{temp:.1f} °C"},
        {"name": "Active_HeatingProgram", "offset": 48, "length": 2, "fmt": "uint16-BE", "raw_hex": _hex(rx, 48, 2), "decoded": str(prog_no)},
        {"name": "Active_HeatingPhase", "offset": 50, "length": 2, "fmt": "uint16-BE", "raw_hex": _hex(rx, 50, 2), "decoded": str(prog_phase)},
        {"name": "Status_FSM", "offset": 52, "length": 2, "fmt": "uint16-BE", "raw_hex": _hex(rx, 52, 2), "decoded": f"{fsm_name} (raw {fsm})", "bits": fsm_flags},
        {"name": "ErrorWord_System", "offset": 54, "length": 4, "fmt": "uint32-BE", "raw_hex": _hex(rx, 54, 4), "decoded": f"0x{err_w:08X}", "bits": err_bits_all},
        {"name": "ErrorWord_HeatingProgram", "offset": 58, "length": 4, "fmt": "uint32-BE", "raw_hex": _hex(rx, 58, 4), "decoded": f"0x{struct.unpack_from('>I', rx, 58)[0]:08X}"},
        {"name": "EnergyCounters_Phases", "offset": 62, "length": 32, "fmt": "8xDWORD", "raw_hex": _hex(rx, 62, 32), "decoded": f"{e_p}"},
        {"name": "Temperatures_Phases", "offset": 94, "length": 32, "fmt": "8xREAL", "raw_hex": _hex(rx, 94, 32), "decoded": f"{t_p}"},
        {"name": "Suffix", "offset": 126, "length": 8, "fmt": "ASCII", "raw_hex": _hex(rx, 126, 8), "decoded": rx[126:134].decode("ascii", errors="replace").strip()},
    ]

    return {
        "tx": {"bytes": list(tx), "fields": tx_fields,  "total": len(tx)},
        "rx": {"bytes": list(rx), "fields": rx_fields, "total": len(rx)},
        "debug": debug,
    }


def get_console_logs() -> list:
    """Return the buffered console text lines."""
    with _console_lock:
        return list(_console_logs)


def send_console_command(cmd: str) -> bool:
    """Send a custom command string to the console port."""
    sock = None
    try:
        sock = _make_udp_socket("furnace console command")
        payload = cmd.strip().encode("ascii") + b"\r\n"
        dest = (config.FURNACE_IP, getattr(config, "FURNACE_CONSOLE_PORT", 4661))
        sock = _sendto_or_rebind(sock, payload, dest, "furnace console command")
        return True
    except Exception as e:
        print(f"[furnace console] Send error: {e}")
        return False
    finally:
        if sock is not None:
            sock.close()

