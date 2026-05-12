from gevent import monkey
monkey.patch_all()

"""
app.py — Flask Process Control Dashboard
=========================================
Serves the dashboard UI and REST + SocketIO APIs for:
  • NI cRIO (relays + temperatures)
  • Duet 3 6HC (GCode, homing, process loop)
  • Induction Furnace COBES i-class compact (UDP cyclic + heating programs)
  • Camera streams (placeholder)
"""

import json
import os
import threading
import time

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

import config
import history
from drivers import crio, duet, furnace

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "process_control_secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

_SERVICES_STARTED = False
_SERVICES_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------
def _template_context():
    return dict(
        crio_ip        = config.CRIO_IP,
        duet_ip        = config.DUET_IP,
        furnace_ip     = config.FURNACE_IP,
        relay_channels = config.RELAY_CHANNELS,
        temp_channels  = config.TEMP_CHANNELS,
        traffic_relays = config.TRAFFIC_RELAYS,
        cameras_list   = [{"id": k, "type": v["type"]} for k, v in config.CAMERAS.items()],
        poll_interval  = config.STATUS_POLL_INTERVAL_MS,
        furnace_max    = config.FURNACE_MAX_SP,
        furnace_min    = config.FURNACE_MIN_SP,
        furnace_num_programs = config.FURNACE_NUM_PROGRAMS,
        furnace_num_phases   = config.FURNACE_NUM_PHASES,
        furnace_presets      = config.FURNACE_PROGRAM_PRESETS,
        control_token        = getattr(config, "CONTROL_API_TOKEN", ""),
    )

def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}

def _numeric_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

@app.before_request
def _require_control_token():
    token = getattr(config, "CONTROL_API_TOKEN", "")
    if not token or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if not request.path.startswith("/api/"):
        return None
    supplied = request.headers.get("X-Control-Token") or request.args.get("token")
    if supplied != token:
        return jsonify({"ok": False, "error": "Unauthorized control request"}), 401
    return None

@app.before_request
def _start_services_on_first_request():
    start_background_services()
    return None

# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", **_template_context())

# ---------------------------------------------------------------------------
# Routes — NI cRIO
# ---------------------------------------------------------------------------
@app.route("/api/crio/status")
def api_crio_status():
    return jsonify(crio.get_all_status())

@app.route("/api/crio/raw_data")
def api_crio_raw_data():
    return jsonify(crio.get_raw_data())

@app.route("/api/crio/relay/<channel_id>", methods=["POST"])
def api_crio_relay(channel_id):
    data  = _json_body()
    state = bool(data.get("state", False))
    res   = crio.set_relay(channel_id, state)
    if res.get("ok"):
        _RELAYS_LATEST[channel_id] = state
    return jsonify(res)

@app.route("/api/crio/emissivity", methods=["POST"])
def api_crio_emissivity():
    data  = _json_body()
    try:
        value = int(data.get("value", 85))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid emissivity value"}), 400
    return jsonify(crio.set_emissivity(value))

@app.route("/api/crio/debug")
def api_crio_debug():
    return jsonify(crio.get_debug_info())

# ---------------------------------------------------------------------------
# Routes — Duet 3 6HC
# ---------------------------------------------------------------------------
@app.route("/api/duet/status")
def api_duet_status():
    status = duet.get_status()
    status["process"] = duet.get_process_state()
    return jsonify(status)

@app.route("/api/duet/gcode", methods=["POST"])
def api_duet_gcode():
    data = _json_body()
    cmd  = str(data.get("command", "")).strip()
    if not cmd:
        return jsonify({"ok": False, "error": "Empty command"})
    return jsonify(duet.send_gcode(cmd))

@app.route("/api/duet/gcode/<name>", methods=["GET"])
def api_duet_gcode_get(name):
    if name not in ("home", "process"):
        return jsonify({"ok": False, "error": "Unknown gcode name"}), 400
    return jsonify({"ok": True, "text": duet.read_gcode(name)})

@app.route("/api/duet/gcode/<name>", methods=["POST"])
def api_duet_gcode_save(name):
    if name not in ("home", "process"):
        return jsonify({"ok": False, "error": "Unknown gcode name"}), 400
    data = _json_body()
    return jsonify(duet.save_gcode(name, str(data.get("text", ""))))

@app.route("/api/duet/home/run", methods=["POST"])
def api_duet_home_run():
    return jsonify(duet.start_homing())

@app.route("/api/duet/process/start", methods=["POST"])
def api_duet_process_start():
    return jsonify(duet.start_process())

@app.route("/api/duet/process/pause", methods=["POST"])
def api_duet_process_pause():
    return jsonify(duet.pause_process())

@app.route("/api/duet/process/stop", methods=["POST"])
def api_duet_process_stop():
    return jsonify(duet.stop_process())

@app.route("/api/duet/process/state")
def api_duet_process_state():
    return jsonify(duet.get_process_state())

# ---------------------------------------------------------------------------
# Routes — Induction Furnace
# ---------------------------------------------------------------------------
@app.route("/api/furnace/status")
def api_furnace_status():
    return jsonify(furnace.get_status())

@app.route("/api/furnace/setpoint", methods=["POST"])
def api_furnace_setpoint():
    data = _json_body()
    try:
        setpoint = float(data.get("setpoint", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid setpoint"}), 400
    return jsonify(furnace.set_setpoint(setpoint))

@app.route("/api/furnace/enable", methods=["POST"])
def api_furnace_enable():
    data   = _json_body()
    return jsonify(furnace.set_enable(bool(data.get("enable", False))))

@app.route("/api/furnace/mode", methods=["POST"])
def api_furnace_mode():
    data = _json_body()
    try:
        mode = int(data.get("mode", 0))
        prog_no = int(data.get("prog_no", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid furnace mode request"}), 400
    return jsonify(furnace.set_mode(mode, prog_no=prog_no))

@app.route("/api/furnace/ack_error", methods=["POST"])
def api_furnace_ack_error():
    return jsonify(furnace.acknowledge_error())

@app.route("/api/furnace/reset_energy", methods=["POST"])
def api_furnace_reset_energy():
    return jsonify(furnace.reset_energy_meter())

@app.route("/api/furnace/manual", methods=["POST"])
def api_furnace_manual():
    data = _json_body()
    try:
        power_pct = float(data.get("power_pct", 0))
        current_pct = float(data.get("current_pct", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid manual control values"}), 400
    return jsonify(furnace.set_manual_control(power_pct, current_pct))

@app.route("/api/furnace/start_program", methods=["POST"])
def api_furnace_start_program():
    data = _json_body()
    try:
        prog_no = int(data.get("prog_no", 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid program number"}), 400
    return jsonify(furnace.start_program(prog_no))

@app.route("/api/furnace/console")
def api_furnace_console():
    return jsonify({"logs": furnace.get_console_logs()})

@app.route("/api/furnace/console/command", methods=["POST"])
def api_furnace_console_command():
    data = _json_body()
    cmd  = str(data.get("command", ""))
    return jsonify({"ok": furnace.send_console_command(cmd)})

@app.route("/api/furnace/program/<int:prog_no>", methods=["GET"])
def api_furnace_program_get(prog_no):
    return jsonify(furnace.get_program(prog_no))

@app.route("/api/furnace/program/select", methods=["POST"])
def api_furnace_program_select():
    data = _json_body()
    try:
        prog_no = int(data.get("prog_no", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid program number"}), 400
    return jsonify(furnace.set_selected_program(prog_no))

@app.route("/api/furnace/program/<int:prog_no>", methods=["POST"])
def api_furnace_program_set(prog_no):
    data   = _json_body()
    phases = data.get("phases", [])
    return jsonify(furnace.set_program(prog_no, phases))

@app.route("/api/furnace/programs")
def api_furnace_programs_list():
    return jsonify(furnace.list_programs())

@app.route("/api/furnace/raw_packets")
def api_furnace_raw_packets():
    return jsonify(furnace.get_raw_packets())

@app.route("/api/furnace/debug")
def api_furnace_debug():
    return jsonify(furnace.get_debug_info())

# ---------------------------------------------------------------------------
# Routes — History / Process Chart
# ---------------------------------------------------------------------------
@app.route("/api/history")
def api_history():
    window = request.args.get("window", type=float)
    samples = history.get_last_seconds(window) if window else history.get_all()
    return jsonify(samples)

@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    history.clear()
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Routes — Camera streams (placeholder)
# ---------------------------------------------------------------------------
@app.route("/stream/camera/<cam_id>")
def stream_camera(cam_id):
    return "Camera stream not yet implemented", 501

# ---------------------------------------------------------------------------
# Parallel Broadcasters — Decoupled real-time status
# ---------------------------------------------------------------------------
_LATEST = {
    "crio": {},
    "duet": {},
    "furnace": {},
}
_LATEST_LOCK = threading.Lock()
_RELAYS_LATEST = {}  # Cache relay states since we no longer read them from hardware for the UI

def _update_latest(key, data):
    with _LATEST_LOCK:
        _LATEST[key] = data

def start_background_services():
    global _SERVICES_STARTED
    with _SERVICES_LOCK:
        if _SERVICES_STARTED:
            return
        crio.start_background_tasks()
        furnace.start_background_tasks()
        threading.Thread(target=_broadcaster_furnace, daemon=True).start()
        threading.Thread(target=_broadcaster_crio,    daemon=True).start()
        threading.Thread(target=_broadcaster_duet,    daemon=True).start()
        threading.Thread(target=_history_logger,      daemon=True).start()
        _SERVICES_STARTED = True

def _broadcaster_furnace():
    interval = config.FURNACE_UPDATE_MS / 1000.0
    while True:
        try:
            st = furnace.get_status()
            _update_latest("furnace", st)
            socketio.emit("status_update", {"furnace": st})
        except Exception as e:
            print(f"[broadcaster-furnace] {e}")
        time.sleep(interval)

def _broadcaster_crio():
    interval = config.CRIO_UPDATE_MS / 1000.0
    while True:
        try:
            st = crio.get_all_status()
            _update_latest("crio", st)
            tr_red    = config.TRAFFIC_RELAYS["red"]
            tr_yellow = config.TRAFFIC_RELAYS["yellow"]
            tr_green  = config.TRAFFIC_RELAYS["green"]
            red_on    = bool(_RELAYS_LATEST.get(tr_red,    False))
            yellow_on = bool(_RELAYS_LATEST.get(tr_yellow, False))
            green_on  = bool(_RELAYS_LATEST.get(tr_green,  False))
            socketio.emit("status_update", {
                "crio": st,
                "traffic": {"red": red_on, "yellow": yellow_on, "green": green_on}
            })
        except Exception as e:
            print(f"[broadcaster-crio] {e}")
        time.sleep(interval)

def _broadcaster_duet():
    interval = config.DUET_UPDATE_MS / 1000.0
    while True:
        try:
            st = duet.get_status()
            st["process"] = duet.get_process_state()
            _update_latest("duet", st)
            socketio.emit("status_update", {"duet": st})
        except Exception as e:
            print(f"[broadcaster-duet] {e}")
        time.sleep(interval)

def _history_logger():
    interval = 0.2
    while True:
        time.sleep(interval)
        with _LATEST_LOCK:
            f_st = _LATEST["furnace"]
            c_st = _LATEST["crio"]
        if not f_st: continue
        history.append({
            "t":       time.time(),
            "temp":    _numeric_or_none(f_st.get("actual")),
            "power":   f_st.get("actual_power"),
            "current": f_st.get("actual_current"),
            "freq":    f_st.get("actual_freq"),
            "water":   f_st.get("water_flow"),
            "energy":  f_st.get("actual_energy"),
            "cap_v":   f_st.get("cap_voltage"),
            "dc_v":    f_st.get("dc_voltage"),
            "fsm":     f_st.get("fsm_state"),
            "status":  f_st.get("status_fsm_raw"),
            "crio_temps": dict(c_st.get("temperatures", {})),
            "crio_mod4":  dict(c_st.get("mod4", {})),
        })

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  3D Print Process Control GUI")
    print(f"  Status: {'REAL HARDWARE MODE'}")
    print(f"  Open: http://localhost:{config.FLASK_PORT}")
    print("="*55 + "\n")
    socketio.run(app, host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG, use_reloader=False, allow_unsafe_werkzeug=True)
