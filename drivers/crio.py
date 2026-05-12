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
    "parse_error": None,
    "rx_count": 0,
    "last_addr": None,
    "last_len": 0,
}

# For Protocol Inspector
_last_raw = {"tx": None, "rx": None, "time": 0}
_tcp_lock = threading.Lock()
_tcp_state = {
    "last_attempt_time": 0,
    "last_success_time": 0,
    "last_error_time": 0,
    "last_error": None,
    "last_command": None,
    "last_response": None,
    "failure_count": 0,
}
_background_started = False
_background_lock = threading.Lock()

_UDP_ONLINE_SECONDS = 2.0
_TCP_ONLINE_SECONDS = 5.0

# ---------------------------------------------------------------------------
# TCP Control
# ---------------------------------------------------------------------------

def _describe_tcp_error(error: str | None) -> str | None:
    if not error:
        return None
    lowered = error.lower()
    if "10061" in error or "actively refused" in lowered or "connection refused" in lowered:
        return "cRIO service offline (TCP 5020 refused)"
    if "10060" in error or "timed out" in lowered or "timeout" in lowered:
        return "cRIO TCP 5020 timeout (network or service unavailable)"
    if "10065" in error or "no route" in lowered or "unreachable" in lowered:
        return "cRIO network unreachable"
    return f"cRIO TCP 5020 unavailable: {error}"

def _tcp_snapshot(now: float | None = None, include_payload: bool = True) -> dict:
    now = time.time() if now is None else now
    with _tcp_lock:
        snap = dict(_tcp_state)
    last_attempt = snap["last_attempt_time"]
    last_success = snap["last_success_time"]
    online = bool(
        last_success
        and last_success >= last_attempt
        and now - last_success <= _TCP_ONLINE_SECONDS
    )
    result = {
        "tcp_service_online": online,
        "tcp_last_attempt_age_s": round(now - last_attempt, 3) if last_attempt else None,
        "tcp_last_success_age_s": round(now - last_success, 3) if last_success else None,
        "tcp_last_error": snap["last_error"],
        "tcp_error_message": None if online else _describe_tcp_error(snap["last_error"]),
        "tcp_failure_count": snap["failure_count"],
        "tcp_last_command": snap["last_command"],
    }
    if include_payload:
        result["tcp_last_response"] = snap["last_response"]
    return result

def _send_tcp_command(cmd: dict, is_heartbeat: bool = False) -> dict:
    global _last_raw
    payload_str = json.dumps(cmd) + "\n"
    _last_raw["tx"] = payload_str.strip()
    _last_raw["time"] = time.time()

    with _tcp_lock:
        _tcp_state["last_attempt_time"] = _last_raw["time"]
        _tcp_state["last_command"] = payload_str.strip()
    
    try:
        with _lock:
            with socket.create_connection((config.CRIO_IP, config.CRIO_PORT), timeout=1.0) as sock:
                sock.sendall(payload_str.encode("utf-8"))
                f = sock.makefile("r")
                line = f.readline()
                if not line:
                    raise RuntimeError("No response")
        
        resp_str = line.strip()
        _last_raw["rx"] = resp_str
        resp = json.loads(resp_str)
        with _tcp_lock:
            _tcp_state["last_success_time"] = time.time()
            _tcp_state["last_error"] = None
            _tcp_state["last_response"] = resp_str
        return resp
    except Exception as e:
        err = str(e)
        _last_raw["rx"] = f"ERROR: {err}"
        with _tcp_lock:
            _tcp_state["last_error_time"] = time.time()
            _tcp_state["last_error"] = err
            _tcp_state["failure_count"] += 1
        return {"ok": False, "error": err}

# ---------------------------------------------------------------------------
# UDP Telemetry (Port 5021)
# ---------------------------------------------------------------------------

def _udp_listener():
    """Listen for one JSON object per UDP datagram."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Requirement 1: Bind to 0.0.0.0:5021
    sock.bind(("0.0.0.0", config.CRIO_UDP_PORT))
    print(f"[CRIO-UDP] Listening on 0.0.0.0:{config.CRIO_UDP_PORT}")
    
    while True:
        try:
            data, addr = sock.recvfrom(65535)
            raw_text = data.decode("utf-8").strip()
            arrival_time = time.time()
            
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
                _latest_telemetry["rx_count"] += 1
                _latest_telemetry["last_addr"] = f"{addr[0]}:{addr[1]}"
                _latest_telemetry["last_len"] = len(data)
                
        except Exception as e:
            print(f"[CRIO-UDP] Socket Error: {e}")
            time.sleep(1.0)

def _watchdog_pusher():
    """Keep the cRIO connection alive."""
    while True:
        time.sleep(2.0)
        _send_tcp_command({"action": "get_state"}, is_heartbeat=True)

def start_background_tasks():
    global _background_started
    with _background_lock:
        if _background_started:
            return
        threading.Thread(target=_udp_listener, daemon=True).start()
        threading.Thread(target=_watchdog_pusher, daemon=True).start()
        _background_started = True

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Requirement 7: Debug Info
def get_debug_info() -> dict:
    now = time.time()
    with _telemetry_lock:
        arrival = _latest_telemetry["arrival_time"]
        udp_online = (now - arrival < _UDP_ONLINE_SECONDS) if arrival else False
        udp_received = _latest_telemetry["rx_count"] > 0
        parsed = dict(_latest_telemetry["parsed_json"])
        raw_text = _latest_telemetry["raw_text"]
        parse_error = _latest_telemetry["parse_error"]
        rx_count = _latest_telemetry["rx_count"]
        last_addr = _latest_telemetry["last_addr"]
        last_len = _latest_telemetry["last_len"]

    return {
        "last_raw_udp_text": raw_text,
        "last_parsed_udp_json": parsed,
        "last_udp_arrival_time": arrival,
        "last_udp_age_s": round(now - arrival, 3) if arrival else None,
        "last_udp_addr": last_addr,
        "last_udp_len": last_len,
        "udp_rx_count": rx_count,
        "udp_received": udp_received,
        "udp_online": udp_online,
        "current_pc_time": now,
        "online": udp_online and parse_error is None,
        "parse_error": parse_error,
        **_tcp_snapshot(now),
    }

def get_raw_data() -> dict:
    """Compatibility for existing inspector."""
    dbg = get_debug_info()
    return {
        "tx": _last_raw["tx"],
        "rx": _last_raw["rx"],
        "time": dbg["last_udp_arrival_time"],
        "telemetry": dbg["last_parsed_udp_json"],
        "debug": dbg,
    }

def _offline_status(now: float, arrival: float, parse_error: str | None, rx_count: int, tcp: dict) -> dict:
    last_age = round(now - arrival, 3) if arrival else None
    if parse_error and arrival and now - arrival < _UDP_ONLINE_SECONDS:
        error = f"cRIO UDP parse error: {parse_error}"
        state = "udp_parse_error"
    elif not rx_count:
        tcp_msg = tcp.get("tcp_error_message")
        if tcp_msg:
            error = f"{tcp_msg}; no UDP telemetry received"
            state = "service_offline"
        else:
            error = "No cRIO UDP telemetry received yet"
            state = "waiting_for_udp"
    else:
        error = f"cRIO UDP telemetry stale (last packet {last_age:.1f}s ago)"
        state = "udp_stale"

    return {
        "connected": False,
        "error": error,
        "service_state": state,
        "udp_received": rx_count > 0,
        "udp_online": False,
        "udp_rx_count": rx_count,
        "last_udp_age_s": last_age,
        "last_udp_arrival_time": arrival,
        "parse_error": parse_error,
        **tcp,
    }

def get_all_status() -> dict:
    now = time.time()
    with _telemetry_lock:
        d = dict(_latest_telemetry["parsed_json"])
        arrival = _latest_telemetry["arrival_time"]
        parse_error = _latest_telemetry["parse_error"]
        rx_count = _latest_telemetry["rx_count"]
        last_addr = _latest_telemetry["last_addr"]
        last_len = _latest_telemetry["last_len"]
        # Requirement 3 & 4: Watchdog uses PC arrival time
        udp_online = (now - arrival < _UDP_ONLINE_SECONDS) if arrival > 0 else False
        connected = udp_online and parse_error is None

    tcp = _tcp_snapshot(now, include_payload=False)
    if not connected:
        return _offline_status(now, arrival, parse_error, rx_count, tcp)

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
        "source": "udp",
        "service_state": "online" if tcp["tcp_service_online"] else "udp_only",
        "udp_received": rx_count > 0,
        "udp_online": True,
        "udp_rx_count": rx_count,
        "last_udp_age_s": round(now - arrival, 3) if arrival else None,
        "last_udp_addr": last_addr,
        "last_udp_len": last_len,
        **tcp,
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
