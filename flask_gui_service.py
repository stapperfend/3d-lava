#!/usr/bin/env python3
import json
import socket
import threading
import time
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request, render_template_string

# --- CONFIGURATION (Requirement 14) ---
CRIO_IP = "192.168.137.100"     # Adjust to cRIO IP
CRIO_TCP_PORT = 5020
UDP_LISTEN_IP = "0.0.0.0"
UDP_LISTEN_PORT = 5021

app = Flask(__name__)

# --- IN-MEMORY STATE (Requirement 2) ---
state_lock = threading.Lock()
latest_state: Dict[str, Any] = {
    "connected": False,
    "last_udp_time": None,
    "crio": None,
    "error": None,
}

# --- HTML INTERFACE (Requirement 9 & 15) ---
HTML = """
<!doctype html>
<html>
<head>
    <title>cRIO Process Control</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, sans-serif; margin: 30px; background: #f4f7f6; color: #333; }
        h1 { color: #1a73e8; }
        .ok { color: #28a745; font-weight: bold; }
        .bad { color: #dc3545; font-weight: bold; }
        .box { background: white; border: 1px solid #ddd; padding: 20px; margin-bottom: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        h2 { margin-top: 0; color: #1a73e8; border-bottom: 1px solid #eee; padding-bottom: 10px; }
        button { margin: 4px; padding: 10px 14px; border: none; border-radius: 5px; cursor: pointer; font-weight: 600; }
        .relay-btn { width: 120px; }
        input { padding: 8px; border: 1px solid #ccc; border-radius: 4px; width: 80px; }
        pre { background: #2d2d2d; color: #ccc; padding: 15px; border-radius: 8px; font-size: 12px; height: 300px; overflow: auto; }
        .data-label { font-weight: 600; color: #666; width: 180px; display: inline-block; }
        .data-val { font-family: 'JetBrains Mono', monospace; color: #111; }
    </style>
</head>
<body>
    <h1>cRIO Control Center</h1>

    <div class="grid">
        <div class="box">
            <h2>System Status</h2>
            <div><span class="data-label">UDP Connection:</span> <span id="connection"></span></div>
            <div><span class="data-label">cRIO Timestamp:</span> <span id="ts" class="data-val">---</span></div>
            <div><span class="data-label">Sequence Number:</span> <span id="seq" class="data-val">---</span></div>
            <div><span class="data-label">Health Status:</span> <span id="health">---</span></div>
            <div><span class="data-label">Active Errors:</span> <span id="errors" class="bad">None</span></div>
        </div>

        <div class="box">
            <h2>Pyrometer (RS232/Analog)</h2>
            <div><span class="data-label">Serial Status:</span> <span id="pyro-serial">---</span></div>
            <div><span class="data-label">Raw Temperature:</span> <span id="pyro-raw" class="data-val">---</span></div>
            <div><span class="data-label">Converted (cRIO):</span> <span id="pyro-conv" class="data-val">---</span></div>
            <div><span class="data-label">Actual Emissivity:</span> <span id="em-actual" class="data-val">---</span></div>
            <div><span class="data-label">Verification:</span> <span id="em-verify">---</span></div>
        </div>
    </div>

    <div class="box">
        <h2>Relay Command States (Mod3)</h2>
        <div id="relays" style="display:grid; grid-template-columns: repeat(4, 1fr); gap:10px;"></div>
        <button onclick="sendRelays()" style="background:#1a73e8; color:white; width:100%; margin-top:15px">Sync Relay States to cRIO</button>
    </div>

    <div class="grid">
        <div class="box">
            <h2>Mod2: Thermocouples (Raw)</h2>
            <div id="mod2-data" class="data-val" style="font-size:13px; line-height: 1.6;"></div>
        </div>
        <div class="box">
            <h2>Mod4: Analog Inputs (U/I)</h2>
            <div><strong>Voltage (0-10V):</strong> <div id="mod4-v" class="data-val" style="margin-top:5px"></div></div>
            <div style="margin-top:15px"><strong>Current (4-20mA):</strong> <div id="mod4-i" class="data-val" style="margin-top:5px"></div></div>
        </div>
    </div>

    <div class="box">
        <h2>Control Setpoints</h2>
        <span class="data-label">Emissivity:</span>
        <input id="emissivity" type="number" min="20" max="100" value="85"> %
        <button onclick="setEmissivity()" style="background:#34a853; color:white">Set Emissivity</button>
        <div style="margin-top:10px"><span class="data-label">Commanded Emissivity:</span> <span id="em-cmd" class="data-val">---</span></div>
    </div>

    <div class="box">
        <h2>Raw JSON Telemetry</h2>
        <pre id="raw"></pre>
    </div>

<script>
let relayState = Array(16).fill(false);

function renderRelays() {
    const div = document.getElementById("relays");
    div.innerHTML = "";
    relayState.forEach((v, i) => {
        const btn = document.createElement("button");
        btn.className = "relay-btn";
        btn.textContent = "Relay " + i + ": " + (v ? "ON" : "OFF");
        btn.style.background = v ? "#34a853" : "#eaecf0";
        btn.style.color = v ? "white" : "#333";
        btn.onclick = () => {
            relayState[i] = !relayState[i];
            renderRelays();
        };
        div.appendChild(btn);
    });
}

async function sendRelays() {
    try {
        const r = await fetch("/api/relays", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({relays: relayState})
        });
        const res = await r.json();
        if(!res.ok && res.error) alert("Error: " + res.error);
    } catch(e) { alert("Network Error"); }
}

async function setEmissivity() {
    const percent = parseInt(document.getElementById("emissivity").value);
    try {
        const r = await fetch("/api/emissivity", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({percent})
        });
        const res = await r.json();
        if(!res.ok && res.error) alert("Error: " + res.error);
    } catch(e) { alert("Network Error"); }
}

async function refresh() {
    try {
        const r = await fetch("/api/state");
        const data = await r.json();

        document.getElementById("connection").innerHTML = data.connected 
            ? "<span class='ok'>● CONNECTED</span>" 
            : "<span class='bad'>○ DISCONNECTED (WATCHDOG)</span>";

        if (data.crio) {
            const c = data.crio;
            document.getElementById("ts").textContent = c.timestamp || "---";
            document.getElementById("seq").textContent = c.sequence || "---";
            document.getElementById("health").textContent = c.health || "Good";
            document.getElementById("errors").textContent = c.error || "None";

            const p = c.pyrometer || {};
            document.getElementById("pyro-serial").innerHTML = p.connected ? "<span class='ok'>ONLINE</span>" : "<span class='bad'>OFFLINE</span>";
            document.getElementById("pyro-raw").textContent = (p.temperature_raw || 0).toFixed(2);
            document.getElementById("pyro-conv").textContent = (p.temperature_c || 0).toFixed(2) + " °C";
            document.getElementById("em-actual").textContent = (p.emissivity_actual_percent || 0) + "%";
            document.getElementById("em-cmd").textContent = (p.emissivity_commanded_percent || 0) + "%";
            document.getElementById("em-verify").innerHTML = p.emissivity_verified ? "<span class='ok'>TRUE</span>" : "<span class='bad'>FALSE</span>";

            const tc = c.mod2_tc || [];
            document.getElementById("mod2-data").innerHTML = tc.map((v, i) => `CH${i}: ${v.toFixed(4)}V`).join(" | ");
            
            const v = c.mod4_volt || [];
            document.getElementById("mod4-v").textContent = v.map(x => x.toFixed(2) + "V").join(", ");
            
            const i = c.mod4_curr || [];
            document.getElementById("mod4-i").textContent = i.map(x => x.toFixed(2) + "mA").join(", ");
        }

        document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
    } catch(e) {}
}

renderRelays();
setInterval(refresh, 500);
</script>
</body>
</html>
"""

# --- CORE LOGIC ---

def udp_receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_LISTEN_IP, UDP_LISTEN_PORT))
    print(f"UDP Receiver active on port {UDP_LISTEN_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            msg = json.loads(data.decode("utf-8"))
            with state_lock:
                latest_state["connected"] = True
                latest_state["last_udp_time"] = time.time()
                latest_state["crio"] = msg
        except: pass

def watchdog():
    while True:
        with state_lock:
            last = latest_state.get("last_udp_time")
            latest_state["connected"] = bool(last and time.time() - last < 2.0)
        time.sleep(0.5)

def send_tcp_command(command: Dict[str, Any]) -> Dict[str, Any]:
    with socket.create_connection((CRIO_IP, CRIO_TCP_PORT), timeout=2.0) as sock:
        sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
        f = sock.makefile("r")
        line = f.readline()
        if not line: raise RuntimeError("No response from cRIO")
        return json.loads(line)

# --- API ENDPOINTS ---

@app.route("/")
def index(): return render_template_string(HTML)

@app.route("/api/state")
def api_state():
    with state_lock: return jsonify(latest_state)

@app.route("/api/relays", methods=["POST"])
def api_relays():
    data = request.get_json(force=True)
    try:
        reply = send_tcp_command({
            "action": "set_relays",
            "relays": [bool(x) for x in data.get("relays", [])]
        })
        return jsonify(reply)
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/emissivity", methods=["POST"])
def api_emissivity():
    data = request.get_json(force=True)
    try:
        reply = send_tcp_command({
            "action": "set_emissivity",
            "percent": int(data.get("percent", 100))
        })
        return jsonify(reply)
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/crio_get_state")
def api_crio_get_state():
    try: return jsonify(send_tcp_command({"action": "get_state"}))
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=udp_receiver, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"Flask Service active at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000)
