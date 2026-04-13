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

def _send_command(cmd: dict) -> dict:
    """Send a JSON command to the cRIO and return the JSON response."""
    payload = (json.dumps(cmd) + "\n").encode("utf-8")
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
    return json.loads(buf.decode("utf-8").strip())


# ---------------------------------------------------------------------------
# Mock state (used when config.MOCK = True)
# ---------------------------------------------------------------------------

_mock_relays: dict[str, bool] = {ch: False for ch in config.RELAY_CHANNELS.values()}
_mock_temps: dict[str, float] = {
    "temp_0": 215.3,
    "temp_1":  60.1,
    "temp_2":  35.7,
    "temp_3":  22.4,
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
        return {"relays": resp["relays"], "temperatures": resp["temperatures"], "error": None}
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
        return {"ok": resp.get("ok", False)}
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
        return {"value": resp.get("value"), "unit": "C"}
    except Exception as e:
        return {"value": None, "error": str(e)}
