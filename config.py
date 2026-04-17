# =============================================================================
# config.py  —  Central configuration for all hardware connections
# Edit this file to match your network and hardware setup.
# =============================================================================

# ---------------------------------------------------------------------------
# Global mock mode
# Set MOCK = True to run the GUI without any real hardware connected.
# ---------------------------------------------------------------------------
MOCK = False

# ---------------------------------------------------------------------------
# NI cRIO
# The cRIO must run a LabVIEW RT TCP server — see docs/crio_protocol.md
# ---------------------------------------------------------------------------
CRIO_IP      = "192.168.1.10"
CRIO_PORT    = 5010
CRIO_TIMEOUT = 2.0

# Relay channel definitions  {display_name: cRIO_channel_id}
RELAY_CHANNELS = {
    "Relay 1": "relay_0",
    "Relay 2": "relay_1",
    "Relay 3": "relay_2",
    "Relay 4": "relay_3",
    "Relay 5": "relay_4",
    "Relay 6": "relay_5",
}

# Temperature channel definitions  {display_name: cRIO_channel_id}
TEMP_CHANNELS = {
    "Temp 1": "temp_0",
    "Temp 2": "temp_1",
    "Temp 3": "temp_2",
    "Temp 4": "temp_3",
}

# ---------------------------------------------------------------------------
# Traffic-light process indicators (read-only — driven by cRIO, not the user)
# Three visual states based on Vacuum + Furnace relay combination:
#   Green  = Vacuum OFF, Furnace OFF  → STANDBY
#   Yellow = Vacuum ON,  Furnace OFF  → VACUUM ACTIVE
#   Red    = Vacuum ON,  Furnace ON   → PROCESS ACTIVE
# ---------------------------------------------------------------------------
TRAFFIC_RELAYS = {
    "vacuum_relay":  "relay_4",   # <-- set to the actual cRIO relay channel ID
    "furnace_relay": "relay_5",   # <-- set to the actual cRIO relay channel ID
}

# ---------------------------------------------------------------------------
# Duet 3 6HC  (RepRapFirmware 3.x)
# ---------------------------------------------------------------------------
DUET_IP      = "192.168.1.20"
DUET_TIMEOUT = 5.0

# ---------------------------------------------------------------------------
# Induction Furnace (UDP) — COBES i-class compact
# Protocol: "800 0031.02_BA_EN_Bedienungsanleitung i-class compact.pdf"
# ---------------------------------------------------------------------------
FURNACE_IP           = "192.168.1.191"
HOST_IP              = "192.168.1.192" # Force UDP to use the physical Ethernet adapter
FURNACE_PORT_SEND    = 5010    # Main control  telegrams  → ICC (section 9.1)
FURNACE_PORT_RECV    = 5010    # Main status   telegrams ← ICC (section 9.2)
FURNACE_SERVICE_PORT = 4660    # Service protocol: heating programs (section 9.4)
FURNACE_CONSOLE_PORT = 4661    # Text console — send "HELLO" to activate
FURNACE_TIMEOUT         = 0.05    # Prevent Python from stalling and missing the hardware 500ms keep-alive
FURNACE_SERVICE_TIMEOUT = 1.0     # More relaxed timeout for retrieval of programs/diagnostics

# Furnace setpoint limits (safety clamp)
FURNACE_MIN_SP = 0.0
FURNACE_MAX_SP = 1600.0   # °C

# Number of heating programs the ICC can store (section 9.5)
FURNACE_NUM_PROGRAMS = 100
FURNACE_NUM_PHASES   = 8

# Named heating program presets shown as a dropdown in the Auto-mode panel.
# Each entry: {"name": "Display label", "prog_no": <1-100>}
# Edit freely — duplicates are allowed (multiple names can point to the same program).
FURNACE_PROGRAM_PRESETS = [
    {"name": "— select preset —",  "prog_no": None},
    {"name": "Preheat  300 °C",    "prog_no": 1},
    {"name": "Sinter   950 °C",    "prog_no": 2},
    {"name": "Anneal   750 °C",    "prog_no": 3},
    {"name": "Full ramp 1200 °C",  "prog_no": 4},
    {"name": "Cool-down cycle",    "prog_no": 5},
]


# ---------------------------------------------------------------------------
# Camera streams (deferred — configure later)
# ---------------------------------------------------------------------------
CAMERAS = {
    # "Basler 1": {"type": "basler", "serial": "12345678"},
    # "Optris":   {"type": "optris", "url": "http://192.168.1.40/stream"},
}

# ---------------------------------------------------------------------------
# Flask server
# ---------------------------------------------------------------------------
FLASK_HOST  = "0.0.0.0"
FLASK_PORT  = 5000
FLASK_DEBUG = True
STATUS_POLL_INTERVAL_MS = 200   # default interval

# Parallel refresh intervals (for the decoupling broadcaster)
CRIO_UPDATE_MS      = 250   # 4Hz
DUET_UPDATE_MS      = 500   # 2Hz
FURNACE_UPDATE_MS   = 100   # 10Hz (Safety critical)
