"""
drivers/crio.py  —  NI cRIO communication driver
=================================================
Protocol: JSON over TCP

The cRIO must run a LabVIEW RT VI that:
  1. Listens on TCP port CRIO_PORT
  2. Accepts one connection at a time (or multiple if desired)
  3. For each connection, reads a newline-terminated JSON string, processes
     the command, and replies with a newline-terminated JSON string.

See docs/crio_protocol.md for the full protocol specification.
"""

import json
import socket
import random
import threading
import time

import config


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_lock = threading.Lock()   # Serialize TCP access across Flask threads
_last_temp_print_time = 0.0

# Protocol Inspector state
_last_raw = {
    "tx": None,
    "rx": None,
    "time": 0
}

def _send_command(cmd: dict) -> dict:
    """Send a JSON command to the cRIO and return the JSON response."""
    payload_str = json.dumps(cmd) + "\n"
    payload = payload_str.encode("utf-8")
    
    _last_raw["tx"] = payload_str.strip()
    _last_raw["time"] = time.time()

    with _lock:
        with socket.create_connection(
            (config.CRIO_IP, config.CRIO_PORT),
            timeout=config.CRIO_TIMEOUT
        ) as sock:
            sock.sendall(payload)
            # Read until newline
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    
    resp_str = buf.decode("utf-8").strip()
    _last_raw["rx"] = resp_str
    
    return json.loads(resp_str)

def get_raw_data() -> dict:
    """Return the last TX and RX JSON strings for the protocol inspector."""
    return _last_raw


# ---------------------------------------------------------------------------
# Mock state (used when config.MOCK = True)
# ---------------------------------------------------------------------------

_mock_relays: dict[str, bool] = {ch: False for ch in config.RELAY_CHANNELS.values()}
_mock_temps: dict[str, float] = {
    "temp_0": 215.3,
    "temp_1":  60.1,
    "temp_2":  35.7,
    "temp_3":  22.4,
    "temp_pyro": -1.0,  # Starts in warming phase
}

def _mock_noise(value: float, sigma: float = 0.3) -> float:
    return round(value + random.gauss(0, sigma), 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_all_status() -> dict:
    """
    Returns:
        {
            "relays":       {"relay_0": false, "relay_1": true, ...},
            "temperatures": {"temp_0": 215.3, ...},
            "error":        null  (or error string)
        }
    """
    if config.MOCK:
        return {
            "relays": dict(_mock_relays),
            "temperatures": {k: _mock_noise(v) for k, v in _mock_temps.items()},
            "error": None,
        }
    try:
        resp = _send_command({"action": "get_all"})
        
        # REMAPPING: The cRIO returns nested data, we need to flatten it for the dashboard
        raw_temps = resp.get("temperatures", {})
        tc_array = raw_temps.get("tc", [])
        
        # Priority for pyrometer: pyro_digital (new) then temp_pyro (old/fallback)
        # If neither exists, we return None to indicate missing data
        pyro_val = raw_temps.get("pyro_digital")
        if pyro_val is None:
            pyro_val = raw_temps.get("temp_pyro")

        mapped_temps = {
            "temp_0": tc_array[0] if len(tc_array) > 0 else 0.0,
            "temp_1": tc_array[1] if len(tc_array) > 1 else 0.0,
            "temp_2": tc_array[2] if len(tc_array) > 2 else 0.0,
            "temp_3": tc_array[3] if len(tc_array) > 3 else 0.0,
            "temp_pyro": pyro_val, 
        }
        
        # Throttled printout for pyrometer temperature (every 5 seconds)
        global _last_temp_print_time
        now = time.time()
        if now - _last_temp_print_time > 5.0:
            pyro_display = "N/A" if pyro_val is None else pyro_val
            print(f"[CRIO] Incoming Temp — Pyrometer: {pyro_display} °C")
            if pyro_val is None:
                # Log raw keys to help debug why it's missing
                print(f"[CRIO] DEBUG: temperatures keys found: {list(raw_temps.keys())}")
            _last_temp_print_time = now
            
        return {"relays": resp["relays"], "temperatures": mapped_temps, "error": None}
    except Exception as e:
        return {"relays": {}, "temperatures": {}, "error": str(e)}


def set_relay(channel_id: str, state: bool) -> dict:
    """
    Set a relay channel on or off.
    Returns {"ok": true} or {"ok": false, "error": "..."}
    """
    if config.MOCK:
        if channel_id in _mock_relays:
            _mock_relays[channel_id] = state
            return {"ok": True}
        return {"ok": False, "error": f"Unknown channel: {channel_id}"}
    try:
        resp = _send_command({"action": "set_relay", "channel": channel_id, "state": state})
        return {
            "ok": resp.get("ok", False),
            "error": resp.get("error", "Unknown cRIO error") if not resp.get("ok") else None
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_temperature(channel_id: str) -> dict:
    """
    Read a single temperature channel.
    Returns {"value": 215.3, "unit": "C"} or {"value": null, "error": "..."}
    """
    if config.MOCK:
        if channel_id in _mock_temps:
            return {"value": _mock_noise(_mock_temps[channel_id]), "unit": "C"}
        return {"value": None, "error": f"Unknown channel: {channel_id}"}
    try:
        resp = _send_command({"action": "read_temp", "channel": channel_id})
        return {"value": resp.get("value"), "unit": "C", "error": resp.get("error")}
    except Exception as e:
        return {"value": None, "error": str(e)}


def set_emissivity(value: int) -> dict:
    """
    Set the emissivity (20-100%).
    Value '100' is sent as string '00', others as '20'-'99'.
    """
    if config.MOCK:
        print(f"[MOCK] Setting emissivity to {value}%")
        # In mock mode, if we set emissivity, let's "warm up" the pyrometer
        if "temp_pyro" in _mock_temps:
            _mock_temps["temp_pyro"] = 850.0
        return {"ok": True}
    
    # Format value: 100 -> "00", else two-digit string
    val_str = "00" if value >= 100 else f"{max(20, min(99, value)):02d}"
    
    try:
        print(f"[CRIO] Sending Command — Emissivity: {value}% (LabVIEW hex/string: {val_str})")
        resp = _send_command({"action": "set_emissivity", "value": val_str})
        
        # Flex check: LabVIEW might return ok:false but reply:"ok"
        success = resp.get("ok", False) or resp.get("reply") == "ok"
        
        return {
            "ok": success,
            "error": resp.get("error", "Unknown cRIO error") if not success else None
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
