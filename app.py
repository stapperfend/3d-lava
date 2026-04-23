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
app.config["SECRET_KEY"] = "process_control_secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------
def _template_context():
    return dict(
        mock_mode      = config.MOCK,
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
    )

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
    data  = request.get_json(force=True)
    state = bool(data.get("state", False))
    return jsonify(crio.set_relay(channel_id, state))

@app.route("/api/crio/emissivity", methods=["POST"])
def api_crio_emissivity():
    data  = request.get_json(force=True)
    value = int(data.get("value", 85))
    return jsonify(crio.set_emissivity(value))

# ---------------------------------------------------------------------------
# Routes — Duet 3 6HC — GCode terminal
# ---------------------------------------------------------------------------
@app.route("/api/duet/status")
def api_duet_status():
    status = duet.get_status()
    status["process"] = duet.get_process_state()
    return jsonify(status)

@app.route("/api/duet/gcode", methods=["POST"])
def api_duet_gcode():
    data = request.get_json(force=True)
    cmd  = str(data.get("command", "")).strip()
    if not cmd:
        return jsonify({"ok": False, "error": "Empty command"})
    return jsonify(duet.send_gcode(cmd))

# Routes — GCode file editor
@app.route("/api/duet/gcode/<name>", methods=["GET"])
def api_duet_gcode_get(name):
    if name not in ("home", "process"):
        return jsonify({"ok": False, "error": "Unknown gcode name"}), 400
    return jsonify({"ok": True, "text": duet.read_gcode(name)})

@app.route("/api/duet/gcode/<name>", methods=["POST"])
def api_duet_gcode_save(name):
    if name not in ("home", "process"):
        return jsonify({"ok": False, "error": "Unknown gcode name"}), 400
    data = request.get_json(force=True)
    return jsonify(duet.save_gcode(name, str(data.get("text", ""))))

# Routes — Homing & Process loop
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
# Routes — Induction Furnace (main cyclic control)
# ---------------------------------------------------------------------------
@app.route("/api/furnace/status")
def api_furnace_status():
    return jsonify(furnace.get_status())

@app.route("/api/furnace/setpoint", methods=["POST"])
def api_furnace_setpoint():
    data = request.get_json(force=True)
    return jsonify(furnace.set_setpoint(float(data.get("setpoint", 0))))

@app.route("/api/furnace/enable", methods=["POST"])
def api_furnace_enable():
    data   = request.get_json(force=True)
    return jsonify(furnace.set_enable(bool(data.get("enable", False))))

@app.route("/api/furnace/program/select", methods=["POST"])
def api_furnace_program_select():
    data    = request.get_json(force=True)
    prog_no = int(data.get("prog_no", 0))
    return jsonify(furnace.set_selected_program(prog_no))

@app.route("/api/furnace/mode", methods=["POST"])
def api_furnace_mode():
    data    = request.get_json(force=True)
    mode    = int(data.get("mode", 0))
    return jsonify(furnace.set_mode(mode))

@app.route("/api/furnace/ack_error", methods=["POST"])
def api_furnace_ack_error():
    return jsonify(furnace.acknowledge_error())

@app.route("/api/furnace/reset_energy", methods=["POST"])
def api_furnace_reset_energy():
    return jsonify(furnace.reset_energy_meter())

@app.route("/api/furnace/manual", methods=["POST"])
def api_furnace_manual():
    """Manual mode: set power% and current% (no temperature setpoint in cyclic telegram)."""
    data = request.get_json(force=True)
    return jsonify(furnace.set_manual_control(
        float(data.get("power_pct",   0)),
        float(data.get("current_pct", 0)),
    ))

@app.route("/api/furnace/start_program", methods=["POST"])
def api_furnace_start_program():
    """Switch to auto mode, set program number, and enable the furnace."""
    data    = request.get_json(force=True)
    prog_no = int(data.get("prog_no", 1))
    return jsonify(furnace.start_program(prog_no))

# Routes — Heating programs (service port 4660)
@app.route("/api/furnace/program/<int:prog_no>", methods=["GET"])
def api_furnace_program_get(prog_no):
    return jsonify(furnace.get_program(prog_no))

@app.route("/api/furnace/program/<int:prog_no>", methods=["POST"])
def api_furnace_program_set(prog_no):
    data   = request.get_json(force=True)
    phases = data.get("phases", [])
    return jsonify(furnace.set_program(prog_no, phases))

@app.route("/api/furnace/programs")
def api_furnace_programs_list():
    return jsonify(furnace.list_programs())

@app.route("/api/furnace/raw_packets")
def api_furnace_raw_packets():
    """Protocol Inspector — returns annotated TX + RX byte arrays."""
    return jsonify(furnace.get_raw_packets())


# ---------------------------------------------------------------------------
# Routes — History / Process Chart
# ---------------------------------------------------------------------------
@app.route("/api/history")
def api_history():
    """Return time-series samples. Optional ?window=<seconds> to limit range."""
    window = request.args.get("window", type=float)
    samples = history.get_last_seconds(window) if window else history.get_all()
    return jsonify(samples)

# ---------------------------------------------------------------------------
# Routes — Camera streams (placeholder)
# ---------------------------------------------------------------------------
@app.route("/stream/camera/<cam_id>")
def stream_camera(cam_id):
    return "Camera stream not yet implemented", 501

# ---------------------------------------------------------------------------
# SocketIO — real-time status broadcaster
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Parallel Broadcasters — Decoupled real-time status
# ---------------------------------------------------------------------------

_LATEST = {
    "crio": {},
    "duet": {},
    "furnace": {},
}
_LATEST_LOCK = threading.Lock()

def _update_latest(key, data):
    with _LATEST_LOCK:
        _LATEST[key] = data

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
            
            # Derive traffic-light states
            tr_red    = config.TRAFFIC_RELAYS["red"]
            tr_yellow = config.TRAFFIC_RELAYS["yellow"]
            tr_green  = config.TRAFFIC_RELAYS["green"]
            
            red_on    = bool(st.get("relays", {}).get(tr_red,    False))
            yellow_on = bool(st.get("relays", {}).get(tr_yellow, False))
            green_on  = bool(st.get("relays", {}).get(tr_green,  False))
            
            socketio.emit("status_update", {
                "crio": st,
                "traffic": {
                    "red": red_on, 
                    "yellow": yellow_on, 
                    "green": green_on
                }
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
    # Log to history at a stable 200ms
    interval = 0.2
    while True:
        time.sleep(interval)
        with _LATEST_LOCK:
            furnace_st = _LATEST["furnace"]
            crio_st    = _LATEST["crio"]
            duet_st    = _LATEST["duet"]
        
        if not furnace_st: continue # Wait for first data
        
        history.append({
            "t":       time.time(),
            "temp":    furnace_st.get("actual"),
            "power":   furnace_st.get("actual_power"),
            "current": furnace_st.get("actual_current"),
            "freq":    furnace_st.get("actual_freq"),
            "water":   furnace_st.get("water_flow"),
            "energy":  furnace_st.get("actual_energy"),
            "cap_v":   furnace_st.get("cap_voltage"),
            "dc_v":    furnace_st.get("dc_voltage"),
            "fsm":     furnace_st.get("fsm_state"),
            "crio_temps": dict(crio_st.get("temperatures", {})),
        })

# ---------------------------------------------------------------------------
# Start background broadcasters
# ---------------------------------------------------------------------------
threading.Thread(target=_broadcaster_furnace, daemon=True).start()
threading.Thread(target=_broadcaster_crio,    daemon=True).start()
threading.Thread(target=_broadcaster_duet,    daemon=True).start()
threading.Thread(target=_history_logger,      daemon=True).start()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "="*55)
    print("  3D Print Process Control GUI")
    print(f"  Mock mode: {'ON  (no hardware needed)' if config.MOCK else 'OFF (real hardware)'}")
    print(f"  Open: http://localhost:{config.FLASK_PORT}")
    print("="*55 + "\n")
    socketio.run(
        app,
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
        allow_unsafe_werkzeug=True,
    )
