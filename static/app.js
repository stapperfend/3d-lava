/**
 * app.js — Process Control Dashboard
 * SocketIO live updates, relay controls, furnace manual/auto,
 * heating program modal, error decoder modal, Duet workflow.
 */
"use strict";

// ── Helpers ────────────────────────────────────────────────
const el = id => document.getElementById(id);
const fmt = (v, d = 1) => (v == null || v === "" || isNaN(+v)) ? "—" : (+v).toFixed(d);
console.log("[APP] v1.1.2 - Active");
const post = (url, body = {}) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(r => r.json());
const get = url => fetch(url).then(r => r.json());

// ── Heartbeat Helper ───────────────────────────────────────
function triggerHB(panelId, type) {
  try {
    const dot = document.querySelector(`#${panelId}-hb .hb-${type}`);
    if (dot) {
        dot.classList.remove("blink");
        void dot.offsetWidth;
        dot.classList.add("blink");
    }
  } catch (e) {}
}

// ── Local state ────────────────────────────────────────────
const S = {
  furnaceEnabled: false,
  furnaceMode: 0,   // 0=manual, 1=auto
  progNo: 1,
  progLoaded: false,
  currentProgram: [],
  wfState: "idle",
  lastErrWord: -1,
  _isProgFocus: false, // Focus tracking
  tcPrefs: JSON.parse(localStorage.getItem("tc_prefs") || "{}"),
  cjcMode: localStorage.getItem("cjc_mode") || "auto", // "auto" | "manual"
  cjcManualVal: parseFloat(localStorage.getItem("cjc_val") || "22.0"),
  currentCjc: 22.0
};

// Requirement 9 & 10: Default mapping & migration
const DEFAULT_TC_TYPES = {
  "temp_0": "C",
  "temp_1": "C",
  "temp_2": "K"
};

const MIG_VER = "v2_tc_map";
if (localStorage.getItem("tc_mig") !== MIG_VER) {
  Object.entries(DEFAULT_TC_TYPES).forEach(([id, type]) => {
    S.tcPrefs[id] = type;
  });
  localStorage.setItem("tc_prefs", JSON.stringify(S.tcPrefs));
  localStorage.setItem("tc_mig", MIG_VER);
}

// ── Thermocouple Logic ──────────────────────────────────────
const TC_TABLES = {
  // NIST Type K (-200 to 1372 °C)
  K: [
    [-200, -5.891], [-100, -3.554], [0, 0.000], [25, 1.000], [50, 2.023], 
    [100, 4.096], [250, 10.153], [500, 20.644], [750, 31.213], [1000, 41.276], [1372, 54.886]
  ],
  // NIST Type C (W5Re/W26Re) (0 to 2315 °C)
  C: [
    [0, 0.000], [100, 1.107], [250, 3.510], [500, 8.120], [750, 13.220], 
    [1000, 18.610], [1500, 29.980], [2000, 34.120], [2315, 37.070]
  ]
};

function interpolate(val, table, xCol, yCol) {
  if (val < table[0][xCol]) return "LOW";
  if (val > table[table.length-1][xCol]) return "HIGH";
  
  for (let i = 0; i < table.length - 1; i++) {
    const [x0, y0] = [table[i][xCol], table[i][yCol]];
    const [x1, y1] = [table[i+1][xCol], table[i+1][yCol]];
    if (val >= x0 && val <= x1) {
      return y0 + (val - x0) * (y1 - y0) / (x1 - x0);
    }
  }
  return null;
}

function temp_to_mv(type, temp_c) {
  const table = TC_TABLES[type];
  if (!table) return 0.0;
  const res = interpolate(temp_c, table, 0, 1);
  return typeof res === "number" ? res : 0.0;
}

function mv_to_temp(type, mv) {
  const table = TC_TABLES[type];
  if (!table) return null;
  return interpolate(mv, table, 1, 0);
}

// ── TC3 Diagnostics State ──────────────────────────────────
let _tc3Last = { mv: null, t: null, time: 0 };

function convertTC(id, mv, type, cjc_temp_c = 0.0) {
  if (mv === null || mv === undefined || isNaN(mv)) return { val: null, msg: "Missing Data" };
  if (Math.abs(mv) > 70) return { val: null, msg: "Open Chan" };
  if (type === "RAW") return { val: mv, msg: null };

  const mv_cjc = temp_to_mv(type, cjc_temp_c);
  const total_mv = mv + mv_cjc;
  const temp = mv_to_temp(type, total_mv);

  // Requirement 2 & 3: Throttled TC3 Diagnostics (~1Hz)
  if (id === "temp_2") {
    const now = Date.now();
    if (now - _tc3Last.time > 1000) {
      const deltaRef = (typeof temp === "number") ? (temp - 9.978) : null;
      
      // Requirement 4: Monotonicity check
      let mono = "N/A";
      if (_tc3Last.mv !== null && _tc3Last.t !== null && typeof temp === "number") {
        const mvInc = mv > _tc3Last.mv;
        const tInc = temp > _tc3Last.t;
        const mvDec = mv < _tc3Last.mv;
        const tDec = temp < _tc3Last.t;
        if ((mvInc && tInc) || (mvDec && tDec) || (mv === _tc3Last.mv)) {
          mono = "PASS";
        } else {
          mono = "FAIL (Inverted Response!)";
        }
      }

      console.groupCollapsed(`[TC3-DIAG] @ ${new Date().toLocaleTimeString()} - ${temp?.toFixed(2)}°C`);
      console.log(`raw_mv:     ${mv.toFixed(4)} mV`);
      console.log(`cjc_t:      ${cjc_temp_c.toFixed(2)} °C`);
      console.log(`cjc_mv:     ${mv_cjc.toFixed(4)} mV`);
      console.log(`total_mv:   ${total_mv.toFixed(4)} mV`);
      console.log(`calc_temp:  ${temp?.toFixed(3)} °C`);
      console.log(`delta_ref:  ${deltaRef?.toFixed(3)} °C (vs 9.978)`);
      console.log(`monotonic:  ${mono} (prev_mv:${_tc3Last.mv?.toFixed(4)} -> curr_mv:${mv.toFixed(4)})`);
      
      // Requirement 5: Sanity Check Warning
      if (Math.abs(mv) < 1.0 && typeof temp === "number") {
        if (Math.abs(temp - cjc_temp_c) > 10.0) {
          console.warn("WARNING: Large deviation between TC and CJC (>10°C) — check wiring/polarity!");
        }
      }
      console.groupEnd();

      _tc3Last = { mv, t: temp, time: now };
    }
  }

  if (temp === "LOW") return { val: null, msg: "Range Low" };
  if (temp === "HIGH") return { val: null, msg: "Range High" };
  if (temp === null) return { val: null, msg: "Conv Fail" };
  
  return { val: temp, msg: null };
}

window.saveTCPreference = (chan, type) => {
  S.tcPrefs[chan] = type;
  localStorage.setItem("tc_prefs", JSON.stringify(S.tcPrefs));
  syncChartLabels();
};

window.saveCjcOverride = (val) => {
  S.cjcManualVal = parseFloat(val);
  S.cjcMode = "manual";
  localStorage.setItem("cjc_val", S.cjcManualVal);
  localStorage.setItem("cjc_mode", S.cjcMode);
};

window.toggleCjcAuto = () => {
  S.cjcMode = "auto";
  localStorage.setItem("cjc_mode", S.cjcMode);
};

function syncChartLabels() {
  if (!_chart) return;
  _chart.data.datasets.forEach(ds => {
    if (ds._key && ds._key.startsWith("crio_temp_")) {
      const idx = ds._key.split("_").pop();
      const chanId = `temp_${idx}`;
      const type = S.tcPrefs[chanId] || "K";
      const baseLabel = `cRIO Temp ${parseInt(idx)+1}`;
      ds.label = type === "RAW" ? `${baseLabel} (Raw mV)` : `${baseLabel} (Type ${type})`;
    }
  });
  _chart.update("none");
}

// ── Clock ──────────────────────────────────────────────────
setInterval(() => { el("clock").textContent = new Date().toLocaleTimeString("en-GB", { hour12: false }); }, 1000);

// ── GCode log ──────────────────────────────────────────────
function appendLog(msg, cls = "log-info") {
  const log = el("gcode-log"); if (!log) return;
  const d = document.createElement("div"); d.className = cls; d.textContent = msg;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  while (log.children.length > 250) log.removeChild(log.firstChild);
}

// ── SocketIO ────────────────────────────────────────────────
const socket = io({ transports: ["websocket", "polling"] });
socket.on("connect", () => { el("conn-dot").className = "status-dot connected"; el("conn-label").textContent = "Connected"; appendLog("⚡ Connected.", "log-info"); });
socket.on("disconnect", () => { el("conn-dot").className = "status-dot disconnected"; el("conn-label").textContent = "Disconnected"; appendLog("✖ Disconnected.", "log-error"); });
socket.on("status_update", data => {
  if (data.crio) { updateCrioUI(data.crio); triggerHB("crio", "rx"); }
  if (data.duet) { updateDuetUI(data.duet); triggerHB("duet", "rx"); }
  if (data.furnace) { updateFurnaceUI(data.furnace); triggerHB("furnace", "rx"); }
  if (data.traffic) updateTrafficLight(data.traffic);
  if (data.furnace) _appendChartPoint(data.furnace, data.crio);
  // Live position → visualizer
  if (data.duet && data.duet.position) {
    const p = data.duet.position;
    updateVizPosition(p.X ?? p.x ?? null, p.Y ?? p.y ?? null);
  }
});

// ── Traffic Light ────────────────────────────────────────────
function updateTrafficLight(data) {
  // Update topbar widget
  ["green", "yellow", "red"].forEach(c => {
    el(`tl-${c}`)?.classList.toggle("active", !!data[c]);
  });
  
  // Logic for the label (top bar) - show highest priority color
  let activeLabel = "STANDBY";
  let activeColor = "var(--muted)";
  if (data.red) { activeLabel = "PROCESS ACTIVE"; activeColor = "var(--red)"; }
  else if (data.yellow) { activeLabel = "VACUUM ACTIVE"; activeColor = "var(--yellow)"; }
  else if (data.green) { activeLabel = "STANDBY"; activeColor = "var(--green)"; }
  
  const lbl = el("tl-label");
  if (lbl) {
    lbl.textContent = activeLabel;
    lbl.style.color = activeColor;
  }
}

function setFurnaceTab(mode) {
  S.furnaceMode = mode;
  el("manual-controls").style.display = mode === 0 ? "block" : "none";
  el("auto-controls").style.display = mode === 1 ? "block" : "none";
  el("tab-btn-manual").classList.toggle("active", mode === 0);
  el("tab-btn-prog").classList.toggle("active", mode === 1);
  
  // Sync mode to backend
  triggerHB("furnace", "tx");
  post("/api/furnace/mode", { mode: mode });
}

// ── cRIO UI ─────────────────────────────────────────────────
function updateCrioUI(data) {
  console.log("[CRIO] UI Update:", data);
  const badge = el("crio-status-badge");
  const isConn = data.connected;
  badge.textContent = isConn ? (data.error ? "ERROR" : "OK") : "OFFLINE";
  badge.className = "conn-badge " + (isConn ? (data.error ? "error" : "ok") : "error");

  // CJC Determination
  let cjcVal = 22.0;
  let cjcSource = "Default";
  
  if (S.cjcMode === "manual") {
    cjcVal = S.cjcManualVal;
    cjcSource = "Manual";
  } else {
    // Look for hardware CJC in various possible keys
    const hwCjc = data.cjc_temp_c ?? data.cjc ?? data.mod2_cjc;
    if (hwCjc !== undefined && hwCjc !== null) {
      cjcVal = hwCjc;
      cjcSource = data.cjc_source || "Hardware";
    } else {
      cjcVal = 22.0;
      cjcSource = "Default";
    }
  }
  S.currentCjc = cjcVal;

  // Update CJC UI
  if (el("cjc-val")) el("cjc-val").textContent = cjcVal.toFixed(1);
  if (el("cjc-source")) el("cjc-source").textContent = cjcSource;
  if (el("cjc-override")) el("cjc-override").value = S.cjcManualVal;
  if (el("cjc-auto-btn")) {
    el("cjc-auto-btn").style.background = S.cjcMode === "auto" ? "var(--purple)" : "var(--surface3)";
    el("cjc-auto-btn").style.color = S.cjcMode === "auto" ? "white" : "var(--muted)";
  }

  if (el("crio-ts")) el("crio-ts").textContent = data.timestamp || "---";
  if (el("crio-seq")) el("crio-seq").textContent = data.sequence || "---";

  if (data.relays) {
    for (const [id, on] of Object.entries(data.relays)) {
      const cb = document.querySelector(`.relay-cb[data-channel="${id}"]`);
      const item = el(`relay-${id}`);
      const now = Date.now();
      const inCooldown = cb && cb._lastClick && (now - cb._lastClick < 1000);
      if (cb && !inCooldown) { 
        cb.checked = on; 
        item?.classList.toggle("active", on); 
      }
    }
  }
  
  const m4 = el("mod4-summary");
  if (m4 && data.mod4) {
    const v = (data.mod4.volt || []).map(x => x.toFixed(2) + "V").join(", ");
    const i = (data.mod4.curr || []).map(x => x.toFixed(2) + "mA").join(", ");
    m4.textContent = `U: [${v}] | I: [${i}]`;
  }

  const pi = data.pyro_info || {};
  const pyroErr = el("pyro-error-msg");
  const pyroBadge = el("pyro-status-badge");
  const verifyBadge = el("pyro-verify-badge");

  if (pyroErr) pyroErr.classList.toggle("active", !pi.connected);
  if (pyroBadge) {
      pyroBadge.textContent = pi.connected ? "ONLINE" : "OFFLINE";
      pyroBadge.className = "badge " + (pi.connected ? "conn-badge ok" : "conn-badge error");
  }
  if (verifyBadge) {
      const verified = pi.emissivity_verified;
      verifyBadge.textContent = verified ? "VERIFIED" : "UNVERIFIED";
      verifyBadge.style.backgroundColor = verified ? "var(--green)" : "#666";
  }

  if (data.temperatures) {
    for (const [id, rawMv] of Object.entries(data.temperatures)) {
      const vEl = el(`tempval-${id}`); 
      const bEl = el(`tempbar-${id}`);
      const rEl = el(`tempraw-${id}`);
      const uEl = el(`tempunit-${id}`);
      const sEl = el(`tc-type-${id}`);

      if (vEl) {
        if (id === "temp_pyro" && rawMv === -1.0) {
          vEl.textContent = "Warming...";
          vEl.classList.add("warming-text");
        } else if (id === "temp_pyro") {
          vEl.textContent = fmt(rawMv);
          vEl.classList.remove("warming-text");
        } else {
          const tcType = S.tcPrefs[id] || "K";
          if (sEl && sEl.value !== tcType) sEl.value = tcType; 

          const res = convertTC(id, rawMv, tcType, S.currentCjc);
          vEl.textContent = (res.val !== null && !isNaN(res.val)) ? fmt(res.val, 1) : (res.msg || "---");
          if (uEl) uEl.textContent = tcType === "RAW" ? "mV" : "°C";
          if (rEl) rEl.textContent = (rawMv !== null && !isNaN(rawMv)) ? rawMv.toFixed(4) + " mV" : "---";
          
          if (bEl) {
            const barVal = (res.val !== null && !isNaN(res.val)) ? res.val : 0;
            const maxVal = tcType === "RAW" ? 50 : 1500;
            bEl.style.width = Math.min(100, Math.max(0, barVal / maxVal * 100)).toFixed(1) + "%";
          }
        }
      }
    }
  }
}

async function setEmissivity(value) {
  console.log(`[CRIO] Setting emissivity to ${value}%`);
  triggerHB("crio", "tx");
  const d = await post("/api/crio/emissivity", { value: parseInt(value) });
  console.log(`[CRIO] Emissivity response:`, d);
  if (!d.ok) appendLog(`✖ Emissivity failed: ${d.error}`, "log-error");
}

// ── Duet UI ─────────────────────────────────────────────────
function updateDuetUI(data) {
  const badge = el("duet-status-badge");
  badge.textContent = data.error ? "OFFLINE" : "OK";
  badge.className = "conn-badge " + (data.error ? "error" : "ok");

  const st = data.state || "—";
  const stBadge = el("wf-state-badge");
  if (stBadge) {
    const duetLabel = st === "idle" ? "IDLE" : st.toUpperCase();
    stBadge.style.color = st === "idle" ? "var(--green)" : st === "printing" ? "var(--orange)" : st === "offline" ? "var(--red)" : "var(--muted)";
  }
  if (data.position) {
    ["x", "y", "z"].forEach(a => { const e = el(`duet-${a}`); if (e) e.textContent = fmt(data.position[a.toUpperCase()], a === "z" ? 3 : 2); });
  }
  if (data.temperatures) {
    const c = el("duet-heaters"); if (!c) return;
    c.innerHTML = "";
    for (const [name, h] of Object.entries(data.temperatures)) {
      const b = document.createElement("div"); b.className = "heater-box";
      b.innerHTML = `<div class="heater-name">${name}</div><div><span class="heater-actual">${fmt(h.current)}°C</span><span class="heater-target"> / ${fmt(h.target)}°C</span></div>`;
      c.appendChild(b);
    }
  }
  if (data.process) updateWorkflowUI(data.process);
}

function updateWorkflowUI({ state: wf, loop_count }) {
  S.wfState = wf;
  const b = el("wf-state-badge");
  if (b) {
    const labels = { idle: "IDLE", homing: "HOMING…", homed: "HOMED", running: "RUNNING", paused: "PAUSED" };
    b.textContent = labels[wf] || wf.toUpperCase();
    b.style.color = wf === "running" ? "var(--green)" : wf === "paused" ? "var(--yellow)" : wf === "homing" ? "var(--purple)" : wf === "error" ? "var(--red)" : "var(--muted)";
  }
  const loops = el("wf-loops");
  if (loops) loops.textContent = loop_count > 0 ? `Loop #${loop_count}` : "";
  if (el("wf-home-btn")) el("wf-home-btn").disabled = !["idle", "homed"].includes(wf);
  if (el("wf-start-btn")) el("wf-start-btn").disabled = wf !== "homed";
  if (el("wf-pause-btn")) {
    el("wf-pause-btn").disabled = !["running", "paused"].includes(wf);
    el("wf-pause-btn").textContent = wf === "paused" ? "▶ Resume" : "⏸ Pause";
  }
  if (el("wf-stop-btn")) el("wf-stop-btn").disabled = !["running", "paused"].includes(wf);
}

// ── Furnace UI ──────────────────────────────────────────────
function updateFurnaceUI(data) {
  const badge = el("furnace-status-badge");
  badge.textContent = data.error ? "ERROR" : "OK";
  badge.className = "conn-badge " + (data.error ? "error" : "ok");

  // Gauge
  const actual = parseFloat(data.actual ?? 0);
  const pct = Math.min(1, actual / window.PCFG.furnaceMax);
  const h = Math.round(30 - pct * 30);
  if (el("furnace-actual")) { el("furnace-actual").textContent = fmt(actual, 1); el("furnace-actual").style.color = `hsl(${h},95%,60%)`; }
  const arc = el("gauge-arc");
  if (arc) { arc.style.strokeDashoffset = (226 * (1 - pct)).toFixed(1); arc.style.stroke = `hsl(${h},95%,55%)`; }
  if (el("gauge-pct")) el("gauge-pct").textContent = Math.round(pct * 100) + "%";

  // FSM
  const fsmEl = el("furnace-fsm");
  if (fsmEl) {
    fsmEl.textContent = data.fsm_state || "—";
    fsmEl.style.color = data.fsm_state === "Active" ? "var(--green)" : data.fsm_state === "Error" ? "var(--red)" : "var(--muted)";
  }

  // Enable state
  S.furnaceEnabled = !!data.enabled;
  const enableBtn = el("furnace-enable-btn");
  if (enableBtn) enableBtn.classList.toggle("on", S.furnaceEnabled);
  if (el("furnace-enable-text")) el("furnace-enable-text").textContent = S.furnaceEnabled ? "Disable" : "Enable";
  const stateEl = el("furnace-state");
  if (stateEl) {
    stateEl.textContent = data.estop ? "E-STOP" : (S.furnaceEnabled ? "ENABLED" : "DISABLED");
    stateEl.className = "furnace-state-label" + (S.furnaceEnabled && !data.estop ? " enabled" : "");
  }

  // ── Live data grid (always visible) ─────────────────────
  const ld = (id, v, unit, dec = 1) => { const e = el(id); if (e) e.textContent = fmt(v, dec) + " " + unit; };
  ld("ld-power", data.actual_power, "W", 1);
  ld("ld-current", data.actual_current, "A", 2);
  ld("ld-freq", data.actual_freq, "Hz", 0);
  ld("ld-water", data.water_flow, "l/m", 2);
  ld("ld-energy", data.actual_energy, "Ws", 0);
  ld("ld-dc", data.dc_voltage, "V", 1);
  ld("ld-cap", data.cap_voltage, "V", 1);
  if (el("ld-sw")) el("ld-sw").textContent = data.status_word ? `0x${data.status_word.toString(16).toUpperCase().padStart(4, "0")}` : "0x0000";

  // ── Mode Tab Remote Sync ──
  if (data.ctrl_mode !== undefined && data.ctrl_mode !== S.furnaceMode) {
    const m = data.ctrl_mode;
    S.furnaceMode = m;
    el("manual-controls").style.display = m === 0 ? "block" : "none";
    el("auto-controls").style.display = m === 1 ? "block" : "none";
    el("tab-btn-manual").classList.toggle("active", m === 0);
    el("tab-btn-prog").classList.toggle("active", m === 1);
  }

  // ── Program Selection Sync ──
  const numInput = el("furnace-prog-no");
  if (numInput && data.heating_program !== undefined) {
    // Only Sync if NOT focused AND the process has GONE ACTIVE
    if (!S._isProgFocus && data.active) {
      if (parseInt(numInput.value) !== data.heating_program) {
        numInput.value = data.heating_program;
        hpLoad(); // Sync phase descriptions once the program starts
      }
    }
  }

  // Progress Panel Detail
  const panel = el("prog-progress-panel");
  if (panel) {
    const isProgActive = !!(data.heating_program && data.active);
    panel.style.display = isProgActive ? "block" : "none";

    if (isProgActive) {
      const phIdx = (data.heating_program_phase || 1) - 1;
      const totalPhases = S.currentProgram.filter(p => p.active === 0).length || 8;
      const currPh = S.currentProgram[phIdx] || {};
      const nextPh = S.currentProgram[phIdx + 1] || null;

      const getDesc = (p, idx) => {
        if (!p || Object.keys(p).length === 0) return "Finished / None";
        const m = p.mode === 1 ? "Temp" : "Power";
        const val = p.mode === 1 ? `${p.temp_sp}°C` : `${p.power_pm}%`;
        return `Step ${idx+1}: ${m} → ${val}`;
      };

      if (el("pp-curr")) el("pp-curr").textContent = getDesc(currPh, phIdx);
      if (el("pp-next")) el("pp-next").textContent = nextPh ? getDesc(nextPh, phIdx + 1) : "None (End of Program)";
      const bar = el("pp-bar");
      if (bar) bar.style.width = Math.round(((phIdx + 1) / totalPhases) * 100) + "%";
    }
  }

  const sp = data.target_temp || data.temp_sp || 0;
  const fmap = { ready: data.ready, active: data.active, error: data.error, estop: data.estop, prog_done: data.prog_done, prog_error: data.prog_error };
  for (const [key, val] of Object.entries(fmap)) {
    const f = el(`flag-${key}`); if (!f) continue;
    f.classList.remove("active", "flag-error");
    if (["error", "estop", "prog_error"].includes(key)) { if (val) f.classList.add("flag-error"); }
    else { if (val) f.classList.add("active"); }
  }

  // ── Error decoder (modal) ────────────────────────────────
  const ew = data.error_word ?? 0;
  if (ew !== S.lastErrWord) {
    S.lastErrWord = ew;
    const hexStr = "0x" + ew.toString(16).padStart(8, "0").toUpperCase();
    if (el("err-word-hex-modal")) el("err-word-hex-modal").textContent = hexStr;
    // Error button badge
    const errBtn = el("err-modal-btn");
    if (errBtn) errBtn.style.color = ew ? "var(--red)" : "";
    // Rebuild error grid
    const grid = el("error-grid");
    if (grid) {
      grid.innerHTML = "";
      const bits = data.error_bits || [];
      const bitSet = new Set(bits.map(b => b.bit));
      for (let bit = 0; bit < 32; bit++) {
        const row = document.createElement("div");
        row.className = "error-row" + (bitSet.has(bit) ? " active" : "");
        const name = (bits.find(b => b.bit === bit) || {}).name || `Bit ${bit}`;
        row.innerHTML = `<span class="error-bit">${bit}</span><span>${name}</span>`;
        grid.appendChild(row);
      }
    }
  }
}

// ── Relay toggle ────────────────────────────────────────────
async function toggleRelay(channelId, newState) {
  console.log(`[CRIO] Toggling relay ${channelId} to ${newState}`);
  triggerHB("crio", "tx");
  const cb = document.querySelector(`.relay-cb[data-channel="${channelId}"]`);
  if (cb) cb._lastClick = Date.now(); // Start cooldown
  
  const d = await post(`/api/crio/relay/${channelId}`, { state: newState });
  console.log(`[CRIO] Relay response:`, d);
  if (!d.ok && cb) {
      cb.checked = !newState;
      cb._lastClick = 0; // Reset cooldown on error so status update can fix it
  } else {
      el(`relay-${channelId}`)?.classList.toggle("active", newState);
  }
}

// ── GCode terminal ──────────────────────────────────────────
async function sendGcode() {
  const input = el("gcode-input"); const cmd = input.value.trim(); if (!cmd) return;
  triggerHB("duet", "tx");
  appendLog(`>> ${cmd}`, "log-send"); input.value = "";
  const d = await post("/api/duet/gcode", { command: cmd });
  appendLog(d.ok ? `<< ${d.response || "(ok)"}` : `✖ ${d.error || "error"}`, d.ok ? "log-recv" : "log-error");
}
function gcodeQuick(cmd) { el("gcode-input").value = cmd; sendGcode(); }

// ── Duet workflow ────────────────────────────────────────────
async function loadGcodeEditors() {
  for (const name of ["home", "process"]) {
    const ta = el(`gcode-${name}-editor`); if (!ta || ta._loaded) continue;
    const d = await get(`/api/duet/gcode/${name}`); ta.value = d.text || ""; ta._loaded = true;
  }
  renderViz && renderViz();  // Refresh visualizer after GCode is populated
}
async function saveGcode(name) {
  const ta = el(`gcode-${name}-editor`); if (!ta) return;
  const d = await post(`/api/duet/gcode/${name}`, { text: ta.value });
  appendLog(d.ok ? `✔ Saved ${name}.gcode` : `✖ ${d.error}`, d.ok ? "log-info" : "log-error");
}
async function wfHome() { loadGcodeEditors(); appendLog("🏠 Homing…", "log-info"); await post("/api/duet/home/run"); }
async function wfStart() { appendLog("▶ Starting process loop…", "log-info"); await post("/api/duet/process/start"); }
async function wfPause() { const d = await post("/api/duet/process/pause"); appendLog(`⏸ ${d.state || ""}`, "log-info"); }
async function wfStop() { appendLog("⏹ Stop.", "log-info"); await post("/api/duet/process/stop"); }

// Eagerly load editors on page load
document.addEventListener("DOMContentLoaded", loadGcodeEditors);

// ── Furnace mode switching ────────────────────────────────────
function setFurnaceMode(mode) {
  S.furnaceMode = mode;
  el("manual-controls").style.display = mode === 0 ? "flex" : "none";
  el("auto-controls").style.display = mode === 1 ? "flex" : "none";
  el("prog-no-wrap").style.display = mode === 1 ? "inline" : "none";
  post("/api/furnace/mode", { mode, prog_no: S.progNo });
}

// ── Manual power / current sliders ───────────────────────────
function syncManual() {
  const p = parseFloat(el("pwr-slider").value);
  const c = parseFloat(el("cur-slider").value);
  el("pwr-val").textContent = p;
  el("cur-val").textContent = c;
}
// Debounce manual sends
let _manualTimer = null;
["pwr-slider", "cur-slider"].forEach(id => {
  el(id)?.addEventListener("input", () => {
    syncManual();
    clearTimeout(_manualTimer);
    _manualTimer = setTimeout(() => {
      triggerHB("furnace", "tx");
      post("/api/furnace/manual", {
        power_pct: parseFloat(el("pwr-slider").value),
        current_pct: parseFloat(el("cur-slider").value),
      });
    }, 150);
  });
});

// ── Auto mode: load program from ICC ─────────────────────────
async function hpLoad() {
  const n = parseInt(el("furnace-prog-no")?.value || S.progNo);
  if (!n || isNaN(n)) return;
  S.progNo = n;
  
  // Update Backend selection
  triggerHB("furnace", "tx");
  const resp = await post("/api/furnace/program/select", { prog_no: n });
  if (!resp.ok) {
     appendLog(`✖ Selection Failed: ${resp.error}`, "log-error");
     return;
  }

  const summary = el("prog-summary");
  if (summary) summary.textContent = `Syncing program ${n}...`;

  const d = await get(`/api/furnace/program/${n}`);
  if (d.ok) {
    S.currentProgram = d.phases;
    const actives = d.phases.filter(p => p.active === 0).length;
    if (summary) summary.textContent = `Program ${n} synced: ${actives} active phases ready.`;
  } else {
    if (summary) summary.innerHTML = `<span style="color:var(--red)">✖ Phase Sync Failed: ${d.error}</span>`;
  }
}

async function toggleFurnace() { 
  triggerHB("furnace", "tx");
  await post("/api/furnace/enable", { enable: !S.furnaceEnabled }); 
}
async function furnaceAckError() { triggerHB("furnace", "tx"); await post("/api/furnace/ack_error"); appendLog("⚡ ACK Error.", "log-info"); }
async function furnaceResetEnergy() { triggerHB("furnace", "tx"); await post("/api/furnace/reset_energy"); appendLog("⚡ Energy reset.", "log-info"); }

// ── Heating Program Modal ────────────────────────────────────
function openProgModal() {
  el("prog-modal").style.display = "flex";
  // Sync program number from auto-mode control if present
  const fpn = el("furnace-prog-no");
  if (fpn && el("prog-select")) el("prog-select").value = fpn.value;
  _buildHpTable(S.currentProgram.length ? S.currentProgram : _emptyProgram());
}
function closeProgModal() { el("prog-modal").style.display = "none"; }

async function hpRead() {
  const n = parseInt(el("prog-select").value) || 1;
  S.progNo = n;
  const ps = el("prog-status"); if (ps) { ps.textContent = "Reading…"; ps.className = "prog-status"; }
  const d = await get(`/api/furnace/program/${n}`);
  if (!d.ok) { if (ps) { ps.textContent = "✖ " + d.error; ps.className = "prog-status err"; } return; }
  S.currentProgram = d.phases;
  _buildHpTable(d.phases);
  if (ps) { ps.textContent = `✔ Program ${n} loaded`; ps.className = "prog-status ok"; }
  // Update auto-mode summary
  if (el("prog-summary")) el("prog-summary").textContent = `Program ${n} — ${d.phases.filter(p => p.active === 0).length} active phases`;
}

async function hpWrite() {
  const n = parseInt(el("prog-select").value) || 1;
  const phases = _readHpTable();
  const ps = el("prog-status"); if (ps) { ps.textContent = "Writing…"; ps.className = "prog-status"; }
  triggerHB("furnace", "tx");
  const d = await post(`/api/furnace/program/${n}`, { phases });
  if (ps) { ps.textContent = d.ok ? `✔ Program ${n} written` : "✖ " + d.error; ps.className = "prog-status " + (d.ok ? "ok" : "err"); }
  if (d.ok) S.currentProgram = phases;
}

function hpExport() {
  const n = parseInt(el("prog-select").value) || 1;
  const blob = new Blob([JSON.stringify({ program: n, phases: _readHpTable() }, null, 2)], { type: "application/json" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `heatprog_${n}.json`; a.click();
}
function hpImport() { el("hp-import-file").click(); }
function hpImportFile(ev) {
  const file = ev.target.files[0]; if (!file) return;
  const r = new FileReader();
  r.onload = e => {
    try {
      const obj = JSON.parse(e.target.result);
      if (!obj.phases || obj.phases.length !== window.PCFG.numPhases) { alert("Invalid program file"); return; }
      if (obj.program) el("prog-select").value = obj.program;
      S.currentProgram = obj.phases; _buildHpTable(obj.phases);
      const ps = el("prog-status"); if (ps) { ps.textContent = `✔ Imported prog ${obj.program || "?"} — press Write to upload`; ps.className = "prog-status ok"; }
    } catch { alert("Failed to parse JSON"); }
  };
  r.readAsText(file);
}

function _emptyProgram() {
  return Array.from({ length: window.PCFG?.numPhases || 8 }, () => ({
    mode: 0, forwarding: 0, ctrl_mode: 1, active: 1, current_pm: 0, power_pm: 0,
    time_ms: 5000, energy_sp: 0, energy_min: 0, energy_max: 0, temp_sp: 0, temp_min: 0, temp_max: 9999,
  }));
}

function _buildHpTable(phases) {
  if (!phases || !phases.length) phases = _emptyProgram();
  const tbody = el("hp-tbody"); if (!tbody) return;
  tbody.innerHTML = "";
  phases.forEach((ph, i) => {
    const isActive = ph.active === 0;
    const tr = document.createElement("tr");
    tr.className = isActive ? "" : "inactive-phase";
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><select class="hp-select" data-phase="${i}" data-field="active">
        <option value="0" ${ph.active === 0 ? "selected" : ""}>Yes</option>
        <option value="1" ${ph.active === 1 ? "selected" : ""}>No</option>
      </select></td>
      <td><select class="hp-select" data-phase="${i}" data-field="mode">
        <option value="0" ${ph.mode === 0 ? "selected" : ""}>Energy</option>
        <option value="1" ${ph.mode === 1 ? "selected" : ""}>Temp</option>
      </select></td>
      <td><select class="hp-select" data-phase="${i}" data-field="ctrl_mode">
        <option value="0" ${ph.ctrl_mode === 0 ? "selected" : ""}>Current</option>
        <option value="1" ${ph.ctrl_mode === 1 ? "selected" : ""}>Power</option>
      </select></td>
      <td><select class="hp-select" data-phase="${i}" data-field="forwarding">
        <option value="0" ${ph.forwarding === 0 ? "selected" : ""}>Time</option>
        <option value="1" ${ph.forwarding === 1 ? "selected" : ""}>SP Reached</option>
      </select></td>
      ${["power_pm", "current_pm", "time_ms", "energy_sp", "energy_min", "energy_max", "temp_sp", "temp_min", "temp_max"].map(f =>
      `<td><input class="hp-input" type="number" data-phase="${i}" data-field="${f}" value="${ph[f] ?? 0}"/></td>`
    ).join("")}
    `;
    tbody.appendChild(tr);
    tr.querySelectorAll(".hp-select,.hp-input").forEach(inp => {
      inp.addEventListener("change", () => {
        const phIdx = parseInt(inp.dataset.phase);
        if (!S.currentProgram[phIdx]) S.currentProgram[phIdx] = {};
        S.currentProgram[phIdx][inp.dataset.field] = parseInt(inp.value) || 0;
        if (inp.dataset.field === "active") tr.className = inp.value === "0" ? "" : "inactive-phase";
      });
    });
  });
}

function _readHpTable() {
  const phases = JSON.parse(JSON.stringify(S.currentProgram.length ? S.currentProgram : _emptyProgram()));
  el("hp-tbody")?.querySelectorAll("[data-phase]").forEach(inp => {
    const i = parseInt(inp.dataset.phase);
    if (!phases[i]) phases[i] = {};
    phases[i][inp.dataset.field] = parseInt(inp.value) || 0;
  });
  return phases;
}

// ── Error Modal ────────────────────────────────────────────
function openErrModal() { el("err-modal").style.display = "flex"; }
function closeErrModal() { el("err-modal").style.display = "none"; }

// ── Protocol Inspector Modal ────────────────────────────────
let _protoTimer = null;


function closeProtoModal() {
  el("proto-modal").style.display = "none";
  clearInterval(_protoTimer);
}
// ── Protocol Inspectors ─────────────────────────────────
  
  async function fetchRawPackets() {
    try {
      const data = await get("/api/furnace/raw_packets");
      if (data.tx) renderPacket("tx", data.tx);
      if (data.rx) renderPacket("rx", data.rx);
      const dot = el("proto-refresh-dot");
      if (dot) { dot.style.opacity = "0.3"; setTimeout(() => { if(dot) dot.style.opacity = "1"; }, 200); }
    } catch (e) { console.warn("Protocol inspector fetch failed:", e); }
  }

  window.fetchCrioRawData = async function() {
    try {
      const modal = el("crio-proto-modal");
      if (!modal || modal.style.display === "none") return;
      
      const data = await get("/api/crio/raw_data");
      
      const txBox = el("crio-tx-dump");
      const rxBox = el("crio-rx-dump");
      const telemetryBox = el("crio-telemetry-dump");

      if (txBox) {
          const parsed = JSON.parse(data.tx || "{}");
          txBox.innerHTML = `<pre style="color:var(--purple); font-size:11px; padding:10px;">${JSON.stringify(parsed, null, 2)}</pre>`;
      }
      if (rxBox) {
          const parsed = JSON.parse(data.rx || "{}");
          rxBox.innerHTML = `<pre style="color:var(--blue); font-size:11px; padding:10px;">${JSON.stringify(parsed, null, 2)}</pre>`;
      }
      if (telemetryBox) {
          telemetryBox.innerHTML = `<pre style="color:var(--green); font-size:11px; padding:10px;">${JSON.stringify(data.telemetry || {}, null, 2)}</pre>`;
      }

      const hbStatus = el("crio-hb-status");
      if (hbStatus && data.heartbeat_tx) {
          hbStatus.textContent = `OK (${data.heartbeat_tx.substring(0, 20)}...)`;
      }

      // Auto-refresh logic if visible
      clearTimeout(window._crioProtoTimer);
      window._crioProtoTimer = setTimeout(window.fetchCrioRawData, 1000);
    } catch (e) { console.error("cRIO Inspector fetch failed:", e); }
  }

  window.openCrioInspector = function() {
      el("crio-proto-modal").style.display = "flex";
      window.fetchCrioRawData();
  };

  window.closeCrioInspector = function() {
      el("crio-proto-modal").style.display = "none";
      clearTimeout(window._crioProtoTimer);
  };
  
  window.fetchRawPackets = fetchRawPackets;
  window.openProtoModal = () => {
    el("proto-modal").style.display = "flex";
    fetchRawPackets();
    if (_protoTimer) clearInterval(_protoTimer);
    _protoTimer = setInterval(fetchRawPackets, 500);
  };

  // ── Service Console (Port 4661) ───────────────────────────
  let _consoleTimer = null;
  let _lastConsoleLineCount = 0;

  window.openConsoleModal = () => {
    el("console-modal").style.display = "flex";
    fetchConsoleLogs();
    if (_consoleTimer) clearInterval(_consoleTimer);
    _consoleTimer = setInterval(fetchConsoleLogs, 1000);
  };

  window.closeConsoleModal = () => {
    el("console-modal").style.display = "none";
    clearInterval(_consoleTimer);
  };

  window.clearConsole = () => {
    el("console-output").innerHTML = "";
    _lastConsoleLineCount = 0;
  };

  async function fetchConsoleLogs() {
    try {
      const data = await get("/api/furnace/console");
      const logs = data.logs || [];
      const out = el("console-output");
      if (!out) return;

      // Only append new lines
      if (logs.length > _lastConsoleLineCount) {
        const newLines = logs.slice(_lastConsoleLineCount);
        newLines.forEach(line => {
          const div = document.createElement("div");
          div.className = "console-line";
          div.textContent = line;
          out.appendChild(div);
        });
        _lastConsoleLineCount = logs.length;
        out.scrollTop = out.scrollHeight; // Auto-scroll
      }

      const dot = el("console-refresh-dot");
      if (dot) { dot.style.opacity = "0.3"; setTimeout(() => { if(dot) dot.style.opacity = "1"; }, 200); }
    } catch (e) { console.warn("Console fetch failed:", e); }
  }

  window.sendConsoleCommand = async () => {
    const inp = el("console-input");
    const cmd = inp.value.trim();
    if (!cmd) return;
    try {
      await post("/api/furnace/console/command", { command: cmd });
      inp.value = "";
      // Append local indicator
      const div = document.createElement("div");
      div.className = "console-line console-sent";
      div.textContent = `> ${cmd}`;
      el("console-output").appendChild(div);
      el("console-output").scrollTop = el("console-output").scrollHeight;
    } catch (e) { appendLog(`Console send failed: ${e}`, "log-error"); }
  };

  window.sendConsoleFixed = (cmd) => {
    const inp = el("console-input");
    if (inp) inp.value = cmd;
    sendConsoleCommand();
  };

  // Field palette — maps field index → CSS class
  const FC_PALETTE = [
    "fc-0", "fc-1", "fc-2", "fc-3", "fc-4", "fc-5", "fc-6", "fc-7",
    "fc-8", "fc-9", "fc-10", "fc-11", "fc-12", "fc-13", "fc-14", "fc-15", "fc-16", "fc-17"
  ];
  const FC_DOT_COLORS = [
    "#b8b0ff", "#93d9ff", "#86efb9", "#fdb87e", "#fd8fa9", "#fde88a", "#c4b5fd", "#7ee7d6",
    "#f9a8d4", "#fde047", "#b3b5f8", "#6ee7b7", "#fca5a5", "#7dd3f5", "#fcd34d", "#d8b4fe", "#86efac", "#fca5a5"
  ];

function renderPacket(dir, pkt) {
  const dumpEl = el(`proto-${dir}-dump`);
  const rulerEl = el(`proto-${dir}-ruler`);
  const fieldsEl = el(`proto-${dir}-fields`);
  const countEl = el(`proto-${dir}-count`);
  if (!dumpEl || !fieldsEl) return;

  const bytes = pkt.bytes || [];
  const fields = pkt.fields || [];
  const total = pkt.total || bytes.length;

  if (countEl) countEl.textContent = `${total} bytes`;

  // ── Build byte-to-field map ──────────────────────────────
  // byteMap[i] = { fieldIdx, isReserved, colorClass, dotColor }
  const byteMap = new Array(total).fill(null);
  fields.forEach((f, fi) => {
    const isRes = f.name.toLowerCase().startsWith("reserved") || f.name.toLowerCase().startsWith("payload");
    const cc = isRes ? "fc-res" : (FC_PALETTE[fi % FC_PALETTE.length]);
    const dc = isRes ? "#444860" : FC_DOT_COLORS[fi % FC_DOT_COLORS.length];
    for (let b = f.offset; b < f.offset + f.length && b < total; b++) {
      byteMap[b] = { fieldIdx: fi, colorClass: cc, dotColor: dc, isRes };
    }
  });

  // ── Hex dump ────────────────────────────────────────────
  dumpEl.innerHTML = "";
  const tiles = [];
  bytes.forEach((bval, i) => {
    const info = byteMap[i] || { colorClass: "", dotColor: "#666", fieldIdx: -1 };
    const span = document.createElement("span");
    span.className = "hb " + info.colorClass;
    span.textContent = bval.toString(16).padStart(2, "0").toUpperCase();
    span.title = `Byte ${i} (0x${i.toString(16).padStart(2, "0").toUpperCase()}): 0x${span.textContent}${info.fieldIdx >= 0 ? " — " + fields[info.fieldIdx].name : ""}`;
    span.dataset.fieldIdx = info.fieldIdx;
    tiles.push(span);
    dumpEl.appendChild(span);
  });

  // ── Offset ruler ────────────────────────────────────────
  if (rulerEl) {
    rulerEl.innerHTML = "";
    bytes.forEach((_, i) => {
      const tick = document.createElement("span");
      tick.className = "hr-tick";
      tick.textContent = i % 8 === 0 ? i : (i % 4 === 0 ? "·" : "");
      rulerEl.appendChild(tick);
    });
  }

  // ── Track open details to preserve state across refresh ──
  const openSet = new Set();
  fieldsEl.querySelectorAll("details.fd[open]").forEach(d => openSet.add(+d.dataset.fieldIdx));

  // ── Field list ───────────────────────────────────────────
  fieldsEl.innerHTML = "";
  fields.forEach((f, fi) => {
    const isRes = f.name.toLowerCase().startsWith("reserved") || f.name.toLowerCase().startsWith("payload");
    const cc = isRes ? "fc-res" : FC_PALETTE[fi % FC_PALETTE.length];
    const dc = isRes ? "#444860" : FC_DOT_COLORS[fi % FC_DOT_COLORS.length];
    const hasBits = Array.isArray(f.bits) && f.bits.length > 0;

    const det = document.createElement("details");
    det.className = "fd";
    det.dataset.fieldIdx = fi;
    if (openSet.has(fi)) det.open = true;

    // Summary row ────────────────────────────────────────
    const summary = document.createElement("summary");
    summary.innerHTML = `
      <span class="fd-arrow">▶</span>
      <span class="fd-color-dot" style="background:${dc}"></span>
      <span class="fd-name">${f.name}</span>
      <span class="fd-offset mono">@${f.offset}+${f.length}</span>
      <span class="fd-hex" style="color:${dc}">${f.raw_hex}</span>
      <span class="fd-decoded">${f.decoded}</span>
    `;
    det.appendChild(summary);

    // Detail body ────────────────────────────────────────
    const body = document.createElement("div");
    body.className = "fd-body";
    body.innerHTML = `
      <div class="fd-meta-row">
        <span><b>Offset</b> ${f.offset}</span>
        <span><b>Length</b> ${f.length} byte${f.length > 1 ? "s" : ""}</span>
        <span><b>Format</b> ${f.fmt}</span>
        <span><b>Raw</b> ${f.raw_hex}</span>
      </div>
      <div class="fd-full-decoded">${f.decoded}</div>
    `;

    // Bit sub-rows (for word fields) ─────────────────────
    if (hasBits) {
      const bitTable = document.createElement("div");
      bitTable.className = "bit-table";
      f.bits.forEach(b => {
        const row = document.createElement("div");
        row.className = "bit-row " + (b.value ? "bit-on" : "bit-off");
        row.innerHTML = `
          <span class="bit-dot"></span>
          <span class="bit-idx">${b.bit}</span>
          <span class="bit-name">${b.name}</span>
          <span class="bit-val">${b.value ? "1" : "0"}</span>
        `;
        bitTable.appendChild(row);
      });
      body.appendChild(bitTable);
    }

    det.appendChild(body);
    fieldsEl.appendChild(det);

    // Sync hex-dump highlights when details open/close ───
    det.addEventListener("toggle", () => {
      const isOpen = det.open;
      // Highlight/un-highlight all tiles belonging to this field
      tiles.forEach(t => {
        if (+t.dataset.fieldIdx === fi) t.classList.toggle("hb-hl", isOpen);
      });
    });

    // Re-apply highlight if already open
    if (openSet.has(fi)) {
      tiles.forEach(t => { if (+t.dataset.fieldIdx === fi) t.classList.add("hb-hl"); });
    }
  });
}

// ── Process Chart ───────────────────────────────────────────
// Dataset definitions — order matters for the legend
const CHART_DATASETS = [
  // Left Y axis (temperature °C)
  { key: "temp", label: "Temperature (°C)", color: "#f97316", yAxis: "yL", hidden: false },
  // Right Y axis (electrical / flow / energy)
  { key: "power", label: "Power (W)", color: "#7c6af7", yAxis: "yR", hidden: false },
  { key: "current", label: "Current (A)", color: "#38bdf8", yAxis: "yR", hidden: false },
  { key: "freq", label: "Frequency (Hz)", color: "#34d399", yAxis: "yR", hidden: true },
  { key: "water", label: "Water Flow (l/min)", color: "#fbbf24", yAxis: "yR", hidden: true },
  { key: "energy", label: "Energy (Ws)", color: "#f43f5e", yAxis: "yR", hidden: true },
  { key: "cap_v", label: "Cap Voltage (V)", color: "#a78bfa", yAxis: "yR", hidden: true },
  { key: "dc_v", label: "DC Voltage (V)", color: "#22d3ee", yAxis: "yR", hidden: true },
  // cRIO temperatures (dashed, left axis) — we'll add dynamically on first data
  { key: "crio_temp_0", label: "cRIO Temp 1 (°C)", color: "#fb923c", yAxis: "yL", hidden: true, dash: [5, 3] },
  { key: "crio_temp_1", label: "cRIO Temp 2 (°C)", color: "#fde68a", yAxis: "yL", hidden: true, dash: [5, 3] },
  { key: "crio_temp_2", label: "cRIO Temp 3 (°C)", color: "#86efac", yAxis: "yL", hidden: true, dash: [5, 3] },
  { key: "crio_temp_3", label: "cRIO Temp 4 (°C)", color: "#67e8f9", yAxis: "yL", hidden: true, dash: [5, 3] },
  { key: "temp_pyro",   label: "Pyrometer (°C)",   color: "#f87171", yAxis: "yL", hidden: false, dash: [2, 2] },
];

let _chart = null;
let _chartWindow = "total";   // "total" | "window"
let _chartData = [];        // full visible dataset (copy of what's drawn)

function _makeCRIOKey(channelId) {
  // "temp_0" → "crio_temp_0"
  const idx = channelId.replace(/\D/g, "");
  return `crio_temp_${idx}`;
}

function initChart() {
  const canvas = el("process-chart");
  if (!canvas || !window.Chart) return;

  const datasets = CHART_DATASETS.map(d => ({
    label: d.label,
    data: [],
    borderColor: d.color,
    backgroundColor: "transparent",
    borderWidth: 1.5,
    borderDash: d.dash || [],
    pointRadius: 0,
    tension: 0.2,
    hidden: d.hidden,
    yAxisID: d.yAxis,
    _key: d.key,
  }));

  _chart = new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "top",
          labels: {
            color: "#e4e6f0",
            boxWidth: 12,
            boxHeight: 2,
            font: { size: 10 },
            padding: 10,
          },
        },
        tooltip: {
          backgroundColor: "rgba(19,22,33,0.95)",
          titleColor: "#e4e6f0",
          bodyColor: "#9ca3af",
          borderColor: "rgba(255,255,255,0.07)",
          borderWidth: 1,
          callbacks: {
            title: items => {
              const t = items[0]?.parsed?.x;
              return t ? new Date(t).toLocaleTimeString("en-GB") : "";
            },
            label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y == null ? "—" : ctx.parsed.y.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: {
          type: "time",
          time: { unit: "minute", displayFormats: { minute: "HH:mm" } },
          ticks: { color: "#6b7280", maxTicksLimit: 8, maxRotation: 0, font: { size: 9 } },
          grid: { color: "rgba(255,255,255,0.05)" },
          border: { color: "rgba(255,255,255,0.07)" },
        },
        yL: {
          type: "linear",
          position: "left",
          title: { display: true, text: "Temperature (°C)", color: "#f97316", font: { size: 10 } },
          ticks: { color: "#6b7280", font: { size: 9 } },
          grid: { color: "rgba(255,255,255,0.04)" },
          border: { color: "rgba(255,255,255,0.07)" },
        },
        yR: {
          type: "linear",
          position: "right",
          title: { display: true, text: "Electrical / Flow / Energy", color: "#7c6af7", font: { size: 10 } },
          ticks: { color: "#6b7280", font: { size: 9 } },
          grid: { drawOnChartArea: false },
          border: { color: "rgba(255,255,255,0.07)" },
        },
      },
    },
  });
}

// Convert a flat sample array into Chart.js dataset arrays
function _samplesToDatasets(samples) {
  if (!_chart) return;
  const ds = _chart.data.datasets;
  // Clear all
  ds.forEach(d => { d.data = []; });
  // For each sample, push to each dataset
  samples.forEach(s => {
    const ms = s.t * 1000;  // unix → ms
    ds.forEach(d => {
      let val = null;
      if (d._key.startsWith("crio_temp_")) {
        // e.g. crio_temp_0 → look in crio_temps["temp_0"]
        const chanKey = "temp_" + d._key.split("_").pop();
        val = s.crio_temps?.[chanKey] ?? null;
      } else if (d._key === "temp_pyro") {
        val = s.crio_temps?.["temp_pyro"] ?? null;
        if (val === -1.0) val = null; // Hide warming phase from chart
      } else {
        val = s[d._key] ?? null;
      }
      d.data.push({ x: ms, y: val === null ? null : parseFloat(val) });
    });
  });
  _chart.update("none");
}

async function fetchHistory() {
  try {
    const window_s = _chartWindow === "window"
      ? (parseFloat(el("chart-tw-min")?.value || 5) * 60)
      : null;
    const url = "/api/history" + (window_s ? `?window=${window_s}` : "");
    const samples = await get(url);
    _chartData = samples;
    _samplesToDatasets(samples);
  } catch (e) {
    console.warn("fetchHistory error:", e);
  }
}

// Append a single point from SocketIO to avoid a full re-fetch every 500 ms
function _appendChartPoint(furnaceData, crioData) {
  if (!_chart) return;
  const now = Date.now();
  
  // Convert cRIO temps before storage
  const convertedTemps = {};
  if (crioData && crioData.temperatures) {
    for (const [id, rawMv] of Object.entries(crioData.temperatures)) {
      if (id === "temp_pyro") {
        convertedTemps[id] = rawMv;
      } else {
        const tcType = S.tcPrefs[id] || "K";
        const res = convertTC(id, rawMv, tcType, S.currentCjc);
        convertedTemps[id] = res.val;
      }
    }
  }

  const s = {
    t: now / 1000,
    temp: furnaceData.actual,
    power: furnaceData.actual_power,
    current: furnaceData.actual_current,
    freq: furnaceData.actual_freq,
    water: furnaceData.water_flow,
    energy: furnaceData.actual_energy,
    cap_v: furnaceData.cap_voltage,
    dc_v: furnaceData.dc_voltage,
    crio_temps: convertedTemps,
  };
  _chartData.push(s);

  // In windowed mode, cull old points
  if (_chartWindow === "window") {
    const cutoff = Date.now() - (parseFloat(el("chart-tw-min")?.value || 5) * 60000);
    _chartData = _chartData.filter(x => x.t * 1000 >= cutoff);
    _samplesToDatasets(_chartData);
    return;
  }

  // Total mode — append efficiently
  const ds = _chart.data.datasets;
  ds.forEach(d => {
    let val = null;
    if (d._key.startsWith("crio_temp_")) {
      const chanKey = "temp_" + d._key.split("_").pop();
      val = s.crio_temps?.[chanKey] ?? null;
    } else {
      val = s[d._key] ?? null;
    }
    d.data.push({ x: now, y: val === null ? null : parseFloat(val) });
  });
  _chart.update("none");
}

function setChartWindow(mode) {
  _chartWindow = mode;
  const dl = el("chart-dl-btn");
  if (dl) dl.textContent = mode === "total" ? "⬇ Download Total Data" : "⬇ Download Section";
  fetchHistory();
}

function downloadCSV() {
  if (!_chartData.length) return;
  // Build header row
  const cols = ["timestamp", "temp_C", "power_W", "current_A", "freq_Hz",
    "water_lpm", "energy_Ws", "cap_voltage_V", "dc_voltage_V",
    "crio_temp1_C", "crio_temp2_C", "crio_temp3_C", "crio_temp4_C"];
  const rows = [cols.join(",")];

  _chartData.forEach(s => {
    const ts = new Date(s.t * 1000).toISOString();
    const ct = s.crio_temps || {};
    rows.push([
      ts,
      s.temp ?? "",
      s.power ?? "",
      s.current ?? "",
      s.freq ?? "",
      s.water ?? "",
      s.energy ?? "",
      s.cap_v ?? "",
      s.dc_v ?? "",
      ct["temp_0"] ?? "",
      ct["temp_1"] ?? "",
      ct["temp_2"] ?? "",
      ct["temp_3"] ?? "",
    ].join(","));
  });

  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  const label = _chartWindow === "total" ? "process_total" : "process_section";
  a.href = URL.createObjectURL(blob);
  a.download = `${label}_${new Date().toISOString().slice(0, 19).replace(/:/g, "-")}.csv`;
  a.click();
}

// ── Init ───────────────────────────────────────────────────
appendLog("Process Control GUI ready.", "log-info");
// Initialise chart after DOM is ready
el("manual-controls").style.display = "flex";
el("auto-controls").style.display = "none";

// Initialise chart after DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  initChart();
  syncChartLabels(); // Apply stored TC types to labels
  fetchHistory();
  initViz();
  loadGcodeEditors();

  // Focus tracking for program number
  const fpn = el("furnace-prog-no");
  if (fpn) {
    fpn.addEventListener("focus", () => { S._isProgFocus = true; });
    fpn.addEventListener("blur", () => { S._isProgFocus = false; });
  }

  // Initialize selection
  if (el("furnace-prog-no")) el("furnace-prog-no").value = S.progNo;
});

// ═══════════════════════════════════════════════════════════════
// JOG CONTROLS
// ═══════════════════════════════════════════════════════════════

let _jogStep = 1;

function setJogStep(mm) {
  _jogStep = mm;
  document.querySelectorAll(".jog-step-opt").forEach(b => {
    b.classList.toggle("active", parseInt(b.dataset.step) === mm);
  });
}

async function jog(axis, sign) {
  const step = _jogStep;
  const feed = parseFloat(el("jog-feed")?.value || 3000);
  const cmds = [`G91`, `G1 ${axis}${sign > 0 ? "" : "-"}${Math.abs(step)} F${feed}`, `G90`];
  for (const cmd of cmds) {
    await post("/api/duet/gcode", { command: cmd });
  }
}

async function jogHome() {
  const feed = parseFloat(el("jog-feed")?.value || 3000);
  await post("/api/duet/gcode", { command: `G90` });
  await post("/api/duet/gcode", { command: `G1 X135 Y135 F${feed}` });
}

// ═══════════════════════════════════════════════════════════════
// GCODE VISUALIZER
// ═══════════════════════════════════════════════════════════════

// Machine geometry (mm)
const VIZ_BED_W = 380, VIZ_BED_H = 380;   // physical bed
const VIZ_TRAVEL_W = 270, VIZ_TRAVEL_H = 270;   // max travel
const VIZ_CENTER_X = 135, VIZ_CENTER_Y = 135;   // home target

// SVG canvas size (px equiv units in viewBox)
const VIZ_SVG_W = 400, VIZ_SVG_H = 400;
const VIZ_MARGIN = 15;

// Scale mm → SVG units (fit bed into viewBox with margin)
function mm2svg(x, y) {
  const scaleX = (VIZ_SVG_W - 2 * VIZ_MARGIN) / VIZ_BED_W;
  const scaleY = (VIZ_SVG_H - 2 * VIZ_MARGIN) / VIZ_BED_H;
  // Y is flipped: 0mm at bottom in machine-coords → top of SVG (SVG +Y goes down)
  return {
    x: VIZ_MARGIN + x * scaleX,
    y: VIZ_SVG_H - VIZ_MARGIN - y * scaleY,
  };
}

let _vizMode = "home";          // "home" | "both"
let _vizLiveTrail = [];         // recent positions for trail polyline

function initViz() {
  // Set static SVG element positions that depend on mm2svg
  const svg = document.getElementById("gcode-viz");
  if (!svg) return;

  // Bed rect
  const bTL = mm2svg(0, VIZ_BED_H);
  const bBR = mm2svg(VIZ_BED_W, 0);
  const bed = document.getElementById("viz-bed");
  bed.setAttribute("x", bTL.x); bed.setAttribute("y", bTL.y);
  bed.setAttribute("width", bBR.x - bTL.x);
  bed.setAttribute("height", bBR.y - bTL.y);

  // Travel limits rect
  const tTL = mm2svg(0, VIZ_TRAVEL_H);
  const tBR = mm2svg(VIZ_TRAVEL_W, 0);
  const travel = document.getElementById("viz-travel");
  travel.setAttribute("x", tTL.x); travel.setAttribute("y", tTL.y);
  travel.setAttribute("width", tBR.x - tTL.x);
  travel.setAttribute("height", tBR.y - tTL.y);

  // Center crosshair
  const ctr = mm2svg(VIZ_CENTER_X, VIZ_CENTER_Y);
  const ch = document.getElementById("viz-crosshair");
  ch.innerHTML = `
    <line class="viz-cross" x1="${ctr.x}" y1="0" x2="${ctr.x}" y2="${VIZ_SVG_H}"/>
    <line class="viz-cross" x1="0" y1="${ctr.y}" x2="${VIZ_SVG_W}" y2="${ctr.y}"/>
    <circle cx="${ctr.x}" cy="${ctr.y}" r="2" fill="rgba(251,191,36,0.6)"/>
  `;

  renderViz();
}

function setVizMode(mode) {
  _vizMode = mode;
  renderViz();
}

// ── GCode Parser ──────────────────────────────────────────────
// Returns array of move objects: {type:'G0'|'G1'|'G2'|'G3', x, y, i, j, f}
function parseGcode(text) {
  const moves = [];
  let curX = 0, curY = 0, absolute = true;
  for (let rawLine of (text || "").split("\n")) {
    let line = rawLine.split(";")[0].trim().toUpperCase();
    if (!line) continue;

    const code = (line.match(/^(G\d+\.?\d*|M\d+)/) || [])[0];
    if (!code) continue;

    const gval = (letter) => {
      const m = line.match(new RegExp(`${letter}(-?[\\d.]+)`));
      return m ? parseFloat(m[1]) : undefined;
    };

    if (code === "G90") { absolute = true; continue; }
    if (code === "G91") { absolute = false; continue; }

    if (code === "G0" || code === "G1") {
      const nx = gval("X"), ny = gval("Y"), f = gval("F");
      const newX = nx !== undefined ? (absolute ? nx : curX + nx) : curX;
      const newY = ny !== undefined ? (absolute ? ny : curY + ny) : curY;
      moves.push({ type: code, x0: curX, y0: curY, x: newX, y: newY, f });
      curX = newX; curY = newY;
    } else if (code === "G2" || code === "G3") {
      const nx = gval("X"), ny = gval("Y");
      const ix = gval("I") ?? 0, iy = gval("J") ?? 0;
      const f = gval("F");
      const newX = nx !== undefined ? (absolute ? nx : curX + nx) : curX;
      const newY = ny !== undefined ? (absolute ? ny : curY + ny) : curY;
      // Centre of arc in mm
      const cx = curX + ix, cy = curY + iy;
      moves.push({ type: code, x0: curX, y0: curY, x: newX, y: newY, cx, cy, f });
      curX = newX; curY = newY;
    }
  }
  return moves;
}

// ── Path builder → SVG path string ─────────────────────────
function movesToSvgPath(moves) {
  if (!moves.length) return "";
  let d = "";
  let prevX = null, prevY = null;

  for (const m of moves) {
    const from = mm2svg(m.x0, m.y0);
    const to = mm2svg(m.x, m.y);

    if (prevX !== from.x || prevY !== from.y) {
      d += ` M ${from.x.toFixed(2)} ${from.y.toFixed(2)}`;
    }

    if (m.type === "G0" || m.type === "G1") {
      d += ` L ${to.x.toFixed(2)} ${to.y.toFixed(2)}`;
    } else {
      // G2 (CW in machine → CCW in SVG because Y is flipped), G3 opposite
      const arcCtr = mm2svg(m.cx, m.cy);

      // Radius (in SVG units)
      const r = Math.hypot(from.x - arcCtr.x, from.y - arcCtr.y);

      // Start/end angles in standard math sense
      const startAngle = Math.atan2(from.y - arcCtr.y, from.x - arcCtr.x);
      const endAngle = Math.atan2(to.y - arcCtr.y, to.x - arcCtr.x);

      // SVG arc flags:
      // G2 = CW in machine; because SVG Y is inverted this becomes CCW in SVG
      // sweep-flag=0 means CCW in SVG
      // G3 = CCW in machine → CW in SVG → sweep-flag=1
      const sweep = m.type === "G2" ? 0 : 1;

      // Determine large-arc-flag
      let deltaAngle = endAngle - startAngle;
      if (sweep === 0 && deltaAngle > 0) deltaAngle -= 2 * Math.PI;
      if (sweep === 1 && deltaAngle < 0) deltaAngle += 2 * Math.PI;
      const largeArc = Math.abs(deltaAngle) > Math.PI ? 1 : 0;

      // special case: full circle (start ≈ end)
      if (Math.abs(from.x - to.x) < 0.01 && Math.abs(from.y - to.y) < 0.01) {
        // Draw two half-arcs
        const mid = {
          x: arcCtr.x + (arcCtr.x - from.x),
          y: arcCtr.y + (arcCtr.y - from.y),
        };
        d += ` A ${r.toFixed(2)} ${r.toFixed(2)} 0 0 ${sweep} ${mid.x.toFixed(2)} ${mid.y.toFixed(2)}`;
        d += ` A ${r.toFixed(2)} ${r.toFixed(2)} 0 0 ${sweep} ${to.x.toFixed(2)} ${to.y.toFixed(2)}`;
      } else {
        d += ` A ${r.toFixed(2)} ${r.toFixed(2)} 0 ${largeArc} ${sweep} ${to.x.toFixed(2)} ${to.y.toFixed(2)}`;
      }
    }

    prevX = to.x; prevY = to.y;
  }
  return d;
}

// ── Render both paths into SVG groups ─────────────────────
function renderViz() {
  const homeText = el("gcode-home-editor")?.value || "";
  const processText = el("gcode-process-editor")?.value || "";

  const homeMoves = parseGcode(homeText);
  const processMoves = parseGcode(processText);

  const homePathEl = document.getElementById("viz-home-path");
  const processPathEl = document.getElementById("viz-process-path");

  if (homePathEl) {
    const d = movesToSvgPath(homeMoves);
    homePathEl.innerHTML = d ? `<path d="${d}" class="viz-home-path"/>` : "";
  }

  if (processPathEl) {
    if (_vizMode === "both" && processMoves.length) {
      const d = movesToSvgPath(processMoves);
      processPathEl.innerHTML = d ? `<path d="${d}" class="viz-process-path"/>` : "";
    } else {
      processPathEl.innerHTML = "";
    }
  }
}

// ── Update live position dot + trail ──────────────────────
const VIZ_TRAIL_MAX = 120;

function updateVizPosition(x, y) {
  if (x == null || y == null) return;
  const pos = mm2svg(x, y);

  const dot = document.getElementById("viz-live-dot");
  if (dot) {
    dot.setAttribute("cx", pos.x.toFixed(2));
    dot.setAttribute("cy", pos.y.toFixed(2));
  }

  // Update trail
  _vizLiveTrail.push([pos.x, pos.y]);
  if (_vizLiveTrail.length > VIZ_TRAIL_MAX) _vizLiveTrail.shift();

  const trail = document.getElementById("viz-trail");
  if (trail) {
    trail.setAttribute("points", _vizLiveTrail.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" "));
  }

  // Update position display in header
  const posEl = el("viz-pos");
  if (posEl) posEl.textContent = `X${x.toFixed(1)}  Y${y.toFixed(1)}`;
}

