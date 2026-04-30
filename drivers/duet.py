"""
drivers/duet.py  —  Duet 3 6HC driver via RepRapFirmware 3.x HTTP API
=======================================================================
Main endpoints:
  POST /machine/code        — send a G/M code, get text response
  GET  /machine/status      — full machine status JSON

GCode workflow:
  - Homing GCode: sent once, Python polls until Duet returns to 'idle'
  - Process GCode: sent in a loop by a background thread until stopped
"""

import os
import threading
import time

import config

# ─────────────────────────────────────────────────────────────
# Optional requests import
# ─────────────────────────────────────────────────────────────
try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ─────────────────────────────────────────────────────────────
# Process loop state
# ─────────────────────────────────────────────────────────────
_STATE_IDLE    = "idle"
_STATE_HOMING  = "homing"
_STATE_HOMED   = "homed"
_STATE_RUNNING = "running"
_STATE_PAUSED  = "paused"

_lock          = threading.Lock()
_process_state = _STATE_IDLE
_loop_count    = 0
_stop_event    = threading.Event()
_pause_event   = threading.Event()  # set = running, clear = paused
_pause_event.set()   # start unpaused
_loop_thread   = None

# ─────────────────────────────────────────────────────────────
# GCode file paths
# ─────────────────────────────────────────────────────────────
_GCODE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gcodes")

def _gcode_path(name: str) -> str:
    return os.path.join(_GCODE_DIR, f"{name}.gcode")

def read_gcode(name: str) -> str:
    """Read gcode file by name ('home' or 'process')."""
    path = _gcode_path(name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def save_gcode(name: str, text: str) -> dict:
    """Save gcode file by name ('home' or 'process')."""
    os.makedirs(_GCODE_DIR, exist_ok=True)
    with open(_gcode_path(name), "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True}

# ─────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────
def _base_url() -> str:
    return f"http://{config.DUET_IP}"

def _send_code(cmd: str) -> dict:
    if not _HAS_REQUESTS:
        return {"ok": False, "response": "", "error": "requests not installed"}
    try:
        r = _requests.post(
            f"{_base_url()}/machine/code",
            data=cmd,
            headers={"Content-Type": "text/plain"},
            timeout=config.DUET_TIMEOUT,
        )
        return {"ok": True, "response": r.text.strip(), "error": None}
    except Exception as e:
        return {"ok": False, "response": "", "error": str(e)}

def _get_machine_status() -> dict:
    if not _HAS_REQUESTS:
        return {"error": "requests not installed"}
    try:
        r = _requests.get(
            f"{_base_url()}/machine/status",
            timeout=config.DUET_TIMEOUT,
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _wait_for_idle(timeout: float = 120.0) -> bool:
    """Poll Duet until machine state is 'idle'. Returns True if reached."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _get_machine_status()
        if isinstance(st, dict) and not st.get("error"):
            state = st.get("state", {})
            if isinstance(state, dict):
                if state.get("status") == "idle":
                    return True
            elif isinstance(state, str):
                if state == "idle":
                    return True
        time.sleep(0.5)
    return False

# ─────────────────────────────────────────────────────────────
# GCode line sender
# ─────────────────────────────────────────────────────────────
def _send_gcode_text(gcode_text: str) -> list[dict]:
    """Send all non-comment lines of a gcode string. Returns list of results."""
    results = []
    for line in gcode_text.splitlines():
        line = line.split(";")[0].strip()   # strip comments
        if not line:
            continue
        result = _send_code(line)
        results.append({"cmd": line, **result})
        time.sleep(0.05)
    return results

# ─────────────────────────────────────────────────────────────
# Homing sequence
# ─────────────────────────────────────────────────────────────
def start_homing() -> dict:
    global _process_state
    with _lock:
        if _process_state not in (_STATE_IDLE, _STATE_HOMED):
            return {"ok": False, "error": f"Cannot home in state '{_process_state}'"}
        _process_state = _STATE_HOMING

    def _run():
        global _process_state
        try:
            gcode = read_gcode("home")
            _send_gcode_text(gcode)
            _wait_for_idle(timeout=120.0)
        finally:
            with _lock:
                _process_state = _STATE_HOMED

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}

# ─────────────────────────────────────────────────────────────
# Process loop
# ─────────────────────────────────────────────────────────────
def _process_loop_worker():
    global _process_state, _loop_count
    while not _stop_event.is_set():
        # Wait if paused
        _pause_event.wait()
        if _stop_event.is_set():
            break
        gcode = read_gcode("process")
        _send_gcode_text(gcode)
        _wait_for_idle(timeout=300.0)
        with _lock:
            _loop_count += 1
        # Check again after completing one loop
        _pause_event.wait()

    with _lock:
        _process_state = _STATE_HOMED

def start_process() -> dict:
    global _process_state, _loop_count, _loop_thread, _stop_event, _pause_event
    with _lock:
        if _process_state not in (_STATE_HOMED, _STATE_PAUSED):
            return {"ok": False, "error": f"Cannot start process in state '{_process_state}' — home first"}
        _process_state = _STATE_RUNNING
        _loop_count    = 0
        _stop_event    = threading.Event()
        _pause_event   = threading.Event()
        _pause_event.set()   # start running (not paused)

    _loop_thread = threading.Thread(target=_process_loop_worker, daemon=True)
    _loop_thread.start()
    return {"ok": True}

def pause_process() -> dict:
    global _process_state
    with _lock:
        if _process_state == _STATE_RUNNING:
            _pause_event.clear()
            _process_state = _STATE_PAUSED
            _send_code("M25")
            return {"ok": True, "state": _STATE_PAUSED}
        elif _process_state == _STATE_PAUSED:
            _pause_event.set()
            _process_state = _STATE_RUNNING
            _send_code("M24")
            return {"ok": True, "state": _STATE_RUNNING}
        return {"ok": False, "error": f"Not in running/paused state (current: '{_process_state}')"}

def stop_process() -> dict:
    global _process_state
    _stop_event.set()
    _pause_event.set()    # unblock if paused
    with _lock:
        _process_state = _STATE_HOMED
    _send_code("M0")  # emergency stop / halt
    return {"ok": True, "state": _STATE_HOMED}

def get_process_state() -> dict:
    with _lock:
        return {
            "state":      _process_state,
            "loop_count": _loop_count,
        }

# ─────────────────────────────────────────────────────────────
# Main status API
# ─────────────────────────────────────────────────────────────
def get_status() -> dict:
    raw = _get_machine_status()
    if "error" in raw and raw["error"]:
        return {"error": raw["error"], "state": "offline"}
    try:
        st  = raw.get("state", {})
        state = st.get("status", "unknown") if isinstance(st, dict) else str(st)
        tools = raw.get("tools", [])
        heaters = raw.get("heat", {}).get("heaters", [])
        temps = {}
        for i, h in enumerate(heaters):
            name = f"tool0" if i < len(tools) else ("bed" if i == len(heaters) - 1 else f"h{i}")
            temps[name] = {"current": h.get("current", 0), "target": h.get("active", 0)}
        axes = raw.get("move", {}).get("axes", [])
        pos = {}
        for ax in axes:
            pos[ax.get("letter", "?")] = round(ax.get("userPosition", 0), 3)
        return {"error": None, "state": state, "temperatures": temps, "position": pos}
    except Exception as e:
        return {"error": str(e), "state": "unknown", "temperatures": {}, "position": {}}

def send_gcode(command: str) -> dict:
    return _send_code(command)

