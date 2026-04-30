import json
import socket
import threading
import time

import config

# ---------------------------------------------------------------------------
# Robust State tracking
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_target_relays = [False] * 16
_target_emissivity = 85

_telemetry_lock = threading.Lock()
_latest_telemetry = {
    "raw_text": None,
    "parsed_json": {},
    "arrival_time": 0,
    "parse_error": None
}

# For Protocol Inspector
_last_raw = {"tx": None, "rx": None, "time": 0}

# ---------------------------------------------------------------------------
# TCP Control
# ---------------------------------------------------------------------------

def _send_tcp_command(cmd: dict, is_heartbeat: bool = False) -> dict:
    global _last_raw
    payload_str = json.dumps(cmd) + "\n"
    _last_raw["tx"] = payload_str.strip()
    _last_raw["time"] = time.time()
    
    try:
        with _lock:
            with socket.create_connection((config.CRIO_IP, config.CRIO_PORT), timeout=1.0) as sock:
                sock.sendall(payload_str.encode("utf-8"))
                f = sock.makefile("r")
                line = f.readline()
                if not line: raise RuntimeError("No response")
        
        resp_str = line.strip()
        _last_raw["rx"] = resp_str
        return json.loads(resp_str)
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------------------
# UDP Telemetry (Port 5021)
# ---------------------------------------------------------------------------

def _udp_listener():
    """Listens for confirmed minimalist UDP broadcast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Requirement 1: Bind to 0.0.0.0:5021
    sock.bind(("0.0.0.0", config.CRIO_UDP_PORT))
    print(f"[CRIO-UDP] Listening on 0.0.0.0:{config.CRIO_UDP_PORT}")
    
    while True:
        try:
            data, addr = sock.recvfrom(65535)
            raw_text = data.decode("utf-8").strip()
            arrival_time = time.time()
            
            # Requirement 8: Print every packet for debugging
            print(f"[CRIO-UDP] Received from {addr}: {raw_text}")
            
            try:
                parsed = json.loads(raw_text)
                parse_err = None
            except Exception as e:
                parsed = {}
                parse_err = str(e)
                print(f"[CRIO-UDP] Parse Error: {e}")

            with _telemetry_lock:
                # Requirement 2: Store raw, parsed, and arrival time
                _latest_telemetry["raw_text"] = raw_text
                _latest_telemetry["parsed_json"] = parsed
                _latest_telemetry["arrival_time"] = arrival_time
                _latest_telemetry["parse_error"] = parse_err
                
        except Exception as e:
            print(f"[CRIO-UDP] Socket Error: {e}")
            time.sleep(1.0)

def _watchdog_pusher():
    """Keep the cRIO connection alive."""
    while True:
        time.sleep(2.0)
        _send_tcp_command({"action": "get_state"}, is_heartbeat=True)

def start_background_tasks():
    threading.Thread(target=_udp_listener, daemon=True).start()
    threading.Thread(target=_watchdog_pusher, daemon=True).start()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Requirement 7: Debug Info
def get_debug_info() -> dict:
    with _telemetry_lock:
        now = time.time()
        return {
            "last_raw_udp_text": _latest_telemetry["raw_text"],
            "last_parsed_udp_json": _latest_telemetry["parsed_json"],
            "last_udp_arrival_time": _latest_telemetry["arrival_time"],
            "current_pc_time": now,
            "online": (now - _latest_telemetry["arrival_time"] < 2.0),
            "parse_error": _latest_telemetry["parse_error"]
        }

def get_raw_data() -> dict:
    """Compatibility for existing inspector."""
    dbg = get_debug_info()
    return {
        "tx": _last_raw["tx"],
        "rx": _last_raw["rx"],
        "time": dbg["last_udp_arrival_time"],
        "telemetry": dbg["last_parsed_udp_json"]
    }

def get_all_status() -> dict:
    now = time.time()
    with _telemetry_lock:
        d = _latest_telemetry["parsed_json"]
        arrival = _latest_telemetry["arrival_time"]
        # Requirement 3 & 4: Watchdog uses PC arrival time
        connected = (now - arrival < 2.0) if arrival > 0 else False

    if not connected:
        return {"connected": False, "error": "cRIO Disconnected (Watchdog)"}

    # Requirement 5: Accept exact keys
    tc = d.get("mod2_tc", [])
    volt = d.get("mod4_volt", [])
    curr = d.get("mod4_curr", [])
    pyro = d.get("pyrometer", {})

    return {
        "connected": True,
        "timestamp": d.get("timestamp"),
        "sequence": d.get("sequence"),
        "relays": {f"relay_{i}": r for i, r in enumerate(_target_relays)},
        "temperatures": {
            "temp_0": tc[0] if len(tc) > 0 else 0.0,
            "temp_1": tc[1] if len(tc) > 1 else 0.0,
            "temp_2": tc[2] if len(tc) > 2 else 0.0,
            "temp_3": tc[3] if len(tc) > 3 else 0.0,
            "temp_pyro": pyro.get("temperature_c", 0.0) if pyro.get("temperature_c") is not None else -1.0,
        },
        "mod4": {
            "volt": volt,
            "curr": curr
        },
        "pyro_info": pyro,
        "cjc_temp_c": d.get("cjc_temp_c"),
        "cjc_source": d.get("cjc_source"),
        "emissivity_cmd": _target_emissivity,
        "error": d.get("error"),
        "source": "udp"
    }

def set_relay(channel_id: str, state: bool) -> dict:
    try:
        idx = int(channel_id.replace("relay_", ""))
        _target_relays[idx] = state
        return _send_tcp_command({
            "action": "set_relays",
            "relays": _target_relays
        })
    except Exception as e: return {"ok": False, "error": str(e)}

def set_emissivity(value: int) -> dict:
    global _target_emissivity
    _target_emissivity = value
    return _send_tcp_command({
        "action": "set_emissivity",
        "percent": value
    })
