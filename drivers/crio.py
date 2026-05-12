import json
import socket
import threading
import time

import config

# ---------------------------------------------------------------------------
# Robust State tracking
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_NUM_RELAYS = 16
_target_relays = [False] * _NUM_RELAYS
_target_lock = threading.Lock()
_target_emissivity = 100

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

def _normalize_emissivity_percent(value) -> int:
    percent = float(value)
    if 0 < percent <= 1:
        percent *= 100
    if percent <= 0 or percent > 100:
        raise ValueError("Emissivity must be 0.01..1.00 or 1..100 percent")
    return int(round(percent))

def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "on", "yes"):
            return True
        if lowered in ("0", "false", "off", "no"):
            return False
    return bool(value)

def _relay_vector(value, width: int = _NUM_RELAYS) -> list:
    relays = [None] * width
    if isinstance(value, dict):
        for i in range(width):
            key = f"relay_{i}"
            if key in value:
                relays[i] = _coerce_bool(value[key])
            elif str(i) in value:
                relays[i] = _coerce_bool(value[str(i)])
        return relays
    if isinstance(value, (list, tuple)):
        for i, relay_value in enumerate(value[:width]):
            relays[i] = _coerce_bool(relay_value)
    return relays

def _relay_dict(values: list, include_unknown: bool = True) -> dict:
    return {
        f"relay_{i}": values[i]
        for i in range(min(len(values), _NUM_RELAYS))
        if include_unknown or values[i] is not None
    }

def _known_relay_values(values: list) -> bool:
    return any(value is not None for value in values)

def _relay_status_from_payload(payload: dict) -> dict:
    relay_command = _relay_vector(payload.get("relay_command"))
    command_source = "udp"
    if not _known_relay_values(relay_command):
        with _target_lock:
            relay_command = list(_target_relays)
        command_source = "host_last_accepted"

    relay_last_written = _relay_vector(payload.get("relay_last_written"))
    relay_last_write_error = payload.get("relay_last_write_error")
    if relay_last_write_error == "":
        relay_last_write_error = None
    mismatch_channels = [
        f"relay_{i}"
        for i, (commanded, written) in enumerate(zip(relay_command, relay_last_written))
        if commanded is not None and written is not None and commanded != written
    ]
    written_available = _known_relay_values(relay_last_written)

    relay_last_written_dict = _relay_dict(relay_last_written, include_unknown=False)
    return {
        "relay_command": _relay_dict(relay_command),
        "relay_command_source": command_source,
        "relay_last_written": relay_last_written_dict,
        "relay_last_write_time": payload.get("relay_last_write_time"),
        "relay_last_write_error": relay_last_write_error,
        "relay_write_pending": bool(mismatch_channels),
        "relay_write_mismatch_channels": mismatch_channels,
        "relay_write_confirmed": written_available and relay_last_write_error is None and not mismatch_channels,
        # Backward-compatible alias for UI code that expects relay state. This is
        # intentionally the last successful DAQmx write, not the accepted command.
        "relays": relay_last_written_dict,
    }

def _latest_relay_command_targets() -> list:
    with _telemetry_lock:
        relay_command = _relay_vector(_latest_telemetry["parsed_json"].get("relay_command"))
    with _target_lock:
        fallback = list(_target_relays)
    if not _known_relay_values(relay_command):
        return fallback
    return [
        bool(value) if value is not None else fallback[i]
        for i, value in enumerate(relay_command)
    ]

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
        "pyro_info": {},
        "emissivity_cmd": _target_emissivity,
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
    relay_status = _relay_status_from_payload(d)
    if not connected:
        status = _offline_status(now, arrival, parse_error, rx_count, tcp)
        status.update(relay_status)
        return status

    # Requirement 5: Accept exact keys
    tc = d.get("mod2_tc", [])
    volt = d.get("mod4_volt", [])
    curr = d.get("mod4_curr", [])
    pyro = d.get("pyrometer", {})

    return {
        "connected": True,
        "timestamp": d.get("timestamp"),
        "sequence": d.get("sequence"),
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
        **relay_status,
        **tcp,
    }

def set_relay(channel_id: str, state: bool) -> dict:
    try:
        idx = int(channel_id.replace("relay_", ""))
        if idx < 0 or idx >= _NUM_RELAYS:
            raise ValueError(f"Relay index out of range: {channel_id}")
        relays = _latest_relay_command_targets()
        relays[idx] = bool(state)
        response = _send_tcp_command({
            "action": "set_relays",
            "relays": relays
        })
        if response.get("ok"):
            with _target_lock:
                _target_relays[:] = relays
        return response
    except Exception as e: return {"ok": False, "error": str(e)}

def set_emissivity(value: int) -> dict:
    global _target_emissivity
    try:
        percent = _normalize_emissivity_percent(value)
        _target_emissivity = percent
        return _send_tcp_command({
            "action": "set_emissivity",
            "percent": percent
        })
    except Exception as e:
        return {"ok": False, "error": str(e)}
