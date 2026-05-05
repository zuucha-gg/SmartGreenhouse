import sys
import os
import subprocess

# ================== AUTO VENV ==================
VENV_PYTHON = "/home/zuucha/iot_env/bin/python"
if sys.executable != VENV_PYTHON:
    if os.path.exists(VENV_PYTHON):
        subprocess.call([VENV_PYTHON] + sys.argv)
        sys.exit()

import time
import threading
import sqlite3
import json
import math
import random

# ── VIRTUAL MODE DETECTION ──
# Pass --virtual flag OR hardware libs missing → virtual mode
VIRTUAL = "--virtual" in sys.argv

if not VIRTUAL:
    try:
        import board
        import busio
        import adafruit_dht
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        import digitalio
    except Exception as _hw_import_err:
        print(f"⚠️  Hardware libs unavailable ({_hw_import_err}) → falling back to VIRTUAL mode")
        VIRTUAL = True

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
    from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False
    print("⚠️  python-telegram-bot not found — Telegram disabled")

from flask import Flask, render_template_string, request, redirect, url_for, jsonify

# ================== 1. SYSTEM CONFIG ==================
TELEGRAM_TOKEN = "8606796819:AAH_aYU-HcfQwFC-zC4CcxyMjjpAlz1Ixek"
ADMIN_IDS      = [5933582361]
TARGET_CHAT_ID = "5933582361"
VERSION        = "4.1.0"

# GPIO pin constants — only referenced when VIRTUAL=False (board is imported above)
if not VIRTUAL:
    PUMP_PIN  = board.D22
    MOTOR_PIN = board.D27
    DHT_PIN   = board.D4
else:
    PUMP_PIN = MOTOR_PIN = DHT_PIN = None

app = Flask(__name__)

# Thread-safety locks
_pump_lock  = threading.Lock()   # guards pump relay & cfg["pump"]
_motor_lock = threading.Lock()   # guards motor relay & cfg["motor_pos"]
_alert_lock = threading.Lock()   # guards _last_alert_ts writes

cfg = {
    # Thresholds
    "th_soil":         30.0,
    "th_soil_crit":    15.0,
    "th_air":          40.0,
    "th_hum_high":     85.0,
    "th_light":        80.0,
    "th_rain":         25.0,
    "th_temp_high":    35.0,
    "th_temp_low":     10.0,
    # Timing
    "water_duration":  5,
    "motor_duration":  3.0,
    "water_cooldown":  300,
    "report_interval": 300,
    "night_start":     22,
    "night_end":       6,
    # Flags
    "auto_mode":       True,
    "alert_enabled":   True,
    "night_mode":      False,
    "rain_skip_water": True,
    "smart_curtain":   True,
    # Runtime state
    "pump":            "OFF",
    "pump_manual_on":  False,   # NEW: manual persistent ON
    "motor_pos":       "OPEN",
    "is_motor_running":False,
    "last_event":      "System ready",
    "last_watered_ts": 0,
    "last_watered":    "Never",
    "boot_time":       time.time(),
    "plant_profile":   "custom",
    # === NEW: Party Mode (relay alternating) ===
    "party_mode":      False,
    "party_relay_a_on":  3,    # seconds relay A stays ON
    "party_relay_a_off": 3,    # seconds relay A stays OFF
    "party_relay_b_on":  3,    # seconds relay B stays ON
    "party_relay_b_off": 3,    # seconds relay B stays OFF
    "party_sync":        False, # True = A then B sequential, False = independent cycles
}

data = {
    "temp": 0.0, "hum": 0.0, "soil": 0.0, "light": 0.0, "rain": 0.0,
    "is_raining": False, "is_bright": False,
    "temp_trend": "→", "hum_trend": "→", "soil_trend": "→",
    "max_t": None, "min_t": None,
    "max_h": None, "min_h": None,
    "max_soil": None, "min_soil": None,
    "max_light": 0.0,
    "water_count":    0,
    "alert_count":    0,
    "motor_count":    0,
    "consecutive_dry": 0,
    "sensor_errors":  0,
    "last_read_ok":   True,
    "_temp_hist": [], "_hum_hist": [], "_soil_hist": [],
    # === NEW computed metrics ===
    "vpd":       0.0,   # Vapour Pressure Deficit kPa
    "dew_point": 0.0,   # Dew point °C
    "heat_index":0.0,   # Heat index °C
    "abs_hum":   0.0,   # Absolute humidity g/m³
    "comfort":   "—",   # Comfort label
    "evap_rate": 0.0,   # Estimated evapotranspiration mm/h
    "co2_est":   "—",   # CO2 comfort estimate (from hum)
    "soil_ec":   0.0,   # Estimated soil EC (rough)
    # Party mode stats
    "party_cycles": 0,
    "party_relay_a_state": False,
    "party_relay_b_state": False,
}

PLANT_PROFILES = {
    "tomato":  {"name":"🍅 Tomato",   "th_soil":40,"th_air":50,"th_temp_high":32,"th_temp_low":15,"water_duration":8},
    "cactus":  {"name":"🌵 Cactus",   "th_soil":15,"th_air":20,"th_temp_high":45,"th_temp_low":5, "water_duration":3},
    "orchid":  {"name":"🌸 Orchid",   "th_soil":45,"th_air":60,"th_temp_high":30,"th_temp_low":15,"water_duration":5},
    "lettuce": {"name":"🥬 Lettuce",  "th_soil":50,"th_air":55,"th_temp_high":25,"th_temp_low":5, "water_duration":6},
    "herb":    {"name":"🌿 Herb Mix", "th_soil":35,"th_air":45,"th_temp_high":30,"th_temp_low":10,"water_duration":5},
    "custom":  {"name":"⚙️ Custom",   "th_soil":30,"th_air":40,"th_temp_high":35,"th_temp_low":10,"water_duration":5},
}

watering_schedule = []   # {"id","hour","minute","duration","days","enabled"}
_last_alert_ts    = {}
ALERT_COOLDOWN    = 600

# ================== 2. DATABASE ==================
DB_PATH = "/tmp/greenhouse4.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sensor_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, temperature REAL, humidity REAL,
        soil REAL, light REAL, rain REAL, pump_on INTEGER,
        vpd REAL, dew_point REAL, heat_index REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, event_type TEXT, message TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS watering_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, duration INTEGER, reason TEXT,
        soil_before REAL, hum_before REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alert_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, alert_type TEXT, value REAL, message TEXT)''')
    conn.commit(); conn.close()

def log_sensor():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO sensor_log (ts,temperature,humidity,soil,light,rain,pump_on,vpd,dew_point,heat_index) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()),data["temp"],data["hum"],data["soil"],
         data["light"],data["rain"],1 if cfg["pump"]=="ON" else 0,
         data["vpd"],data["dew_point"],data["heat_index"]))
    c.execute("DELETE FROM sensor_log WHERE ts < ?", (int(time.time())-7*86400,))
    conn.commit(); conn.close()

def log_event(etype, msg):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO events (ts,event_type,message) VALUES (?,?,?)",(int(time.time()),etype,msg))
    conn.commit(); conn.close()

def log_watering(duration, reason, soil_before, hum_before):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO watering_log (ts,duration,reason,soil_before,hum_before) VALUES (?,?,?,?,?)",
        (int(time.time()),duration,reason,soil_before,hum_before))
    conn.commit(); conn.close()

def log_alert(alert_type, value, message):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO alert_log (ts,alert_type,value,message) VALUES (?,?,?,?)",
        (int(time.time()),alert_type,value,message))
    conn.commit(); conn.close()

def get_history(hours=24, downsample=80):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    since = int(time.time()) - hours*3600
    c.execute("SELECT ts,temperature,humidity,soil,light,rain,vpd,dew_point FROM sensor_log WHERE ts>? ORDER BY ts ASC",(since,))
    rows = c.fetchall(); conn.close()
    if len(rows) > downsample:
        step = max(1, len(rows)//downsample); rows = rows[::step]
    return rows

def get_events(limit=20):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT ts,event_type,message FROM events ORDER BY ts DESC LIMIT ?",(limit,))
    rows = c.fetchall(); conn.close(); return rows

def get_watering_history(limit=12):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT ts,duration,reason,soil_before,hum_before FROM watering_log ORDER BY ts DESC LIMIT ?",(limit,))
    rows = c.fetchall(); conn.close(); return rows

def get_alert_history(limit=10):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT ts,alert_type,value,message FROM alert_log ORDER BY ts DESC LIMIT ?",(limit,))
    rows = c.fetchall(); conn.close(); return rows

def get_db_stats():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sensor_log");  sc = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM watering_log"); wc = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM alert_log");    ac = c.fetchone()[0]
    conn.close(); return sc, wc, ac

# ================== 3. HARDWARE ==================
# ================== 3. HARDWARE / VIRTUAL SHIMS ==================

if VIRTUAL:
    # ── Virtual relay: just a flag, no GPIO ──
    class _VRelay:
        def __init__(self): self.value = False
    pump_relay  = _VRelay()
    motor_relay = _VRelay()

    # ── Virtual sensor state (updated by sim loop) ──
    _vsim = {"temp":25.0,"hum":60.0,"soil":55.0,"light":50.0,"rain":5.0}

    # ── Virtual DHT shim ──
    class _VDht:
        @property
        def temperature(self): return _vsim["temp"]
        @property
        def humidity(self):    return _vsim["hum"]
        def exit(self): pass
    dht_device = _VDht()

    # ── Virtual ADS channel shims ──
    class _VChan:
        def __init__(self, key, scale=3.3): self._k=key; self._s=scale
        @property
        def voltage(self): return (1 - _vsim[self._k]/100) * self._s
    l_chan = _VChan("light", 3.3)
    s_chan = _VChan("soil",  2.5)
    r_chan = _VChan("rain",  3.3)

    print("🟡 VIRTUAL MODE — no GPIO, simulated sensors")

else:
    # ── Real hardware ──
    dht_device  = adafruit_dht.DHT22(DHT_PIN)
    pump_relay  = digitalio.DigitalInOut(PUMP_PIN)
    pump_relay.direction  = digitalio.Direction.OUTPUT; pump_relay.value  = False
    motor_relay = digitalio.DigitalInOut(MOTOR_PIN)
    motor_relay.direction = digitalio.Direction.OUTPUT; motor_relay.value = False

    try:
        i2c    = busio.I2C(board.SCL, board.SDA)
        ads    = ADS.ADS1115(i2c)
        l_chan = AnalogIn(ads, 0)
        s_chan = AnalogIn(ads, 1)
        r_chan = AnalogIn(ads, 2)
    except Exception as e:
        print(f"❌ ADS1115 init failed: {e}"); sys.exit()

# ================== 3b. VIRTUAL SENSOR SIMULATOR ==================

def _virtual_sim_loop():
    """Runs only in virtual mode. Generates realistic-looking sensor data."""
    STEP = 2.0   # seconds between updates (same as core_monitor sleep)
    soil = 65.0  # soil starts moist, drains, auto-waters back up

    while True:
        time.sleep(STEP)
        hr = time.localtime().tm_hour + time.localtime().tm_min / 60

        # Temperature: 20–32°C daily sine + small noise
        base_temp = 26 + 6 * math.sin((hr - 6) * math.pi / 12)
        _vsim["temp"] = round(base_temp + random.gauss(0, 0.3), 1)

        # Humidity: inversely related to temp, 45–80%
        base_hum = 80 - 30 * math.sin((hr - 6) * math.pi / 12)
        _vsim["hum"]  = round(max(30, min(95, base_hum + random.gauss(0, 1))), 1)

        # Light: follows a bell curve peaking at noon
        if 6 <= hr <= 20:
            base_light = 100 * math.sin((hr - 6) * math.pi / 14)
        else:
            base_light = 0
        _vsim["light"] = round(max(0, min(100, base_light + random.gauss(0, 2))), 1)

        # Rain: random occasional rain bursts
        if random.random() < 0.002:   # ~0.2% chance each tick → sporadic
            _vsim["rain"] = round(random.uniform(30, 90), 1)
        else:
            _vsim["rain"] = max(0, _vsim["rain"] - random.uniform(0, 3))
            _vsim["rain"] = round(_vsim["rain"], 1)

        # Soil: drains slowly, refills when pump runs
        drain = random.uniform(0.02, 0.08)
        if cfg["pump"] == "ON":
            soil = min(100, soil + random.uniform(1.5, 3.0))
        else:
            soil = max(0, soil - drain)
        _vsim["soil"] = round(soil, 1)
        soil = _vsim["soil"]

# ================== 4. ACTUATORS ==================

def run_motor(target_pos, reason="manual"):
    if cfg["party_mode"]: return False          # party mode owns the relay
    with _motor_lock:
        if cfg["is_motor_running"] or cfg["motor_pos"] == target_pos: return False
        cfg["is_motor_running"] = True
    cfg["last_event"] = f"Curtain → {target_pos}..."
    motor_relay.value = True
    time.sleep(cfg["motor_duration"])
    motor_relay.value = False
    with _motor_lock:
        cfg["motor_pos"] = target_pos; cfg["is_motor_running"] = False
    data["motor_count"] += 1
    cfg["last_event"] = f"Curtain {target_pos} @ {time.strftime('%H:%M')}"
    log_event("motor", f"Curtain → {target_pos} | reason:{reason}")
    return True

def run_pump_action(reason="manual", override_duration=None):
    """Timed pump run (original behavior). Won't run if manual ON or party is active."""
    with _pump_lock:
        if cfg["pump"] != "OFF": return False      # already running / party / manual
        if cfg["pump_manual_on"]: return False
        elapsed = time.time() - cfg["last_watered_ts"]
        if reason == "auto" and elapsed < cfg["water_cooldown"]: return False
        cfg["pump"] = "ON"
        pump_relay.value = True
    duration    = override_duration or cfg["water_duration"]
    soil_before = data["soil"]; hum_before = data["hum"]
    data["water_count"] += 1
    time.sleep(duration)
    with _pump_lock:
        if not cfg["pump_manual_on"]:  # didn't get overridden during sleep
            pump_relay.value = False; cfg["pump"] = "OFF"
    cfg["last_watered_ts"] = time.time()
    cfg["last_watered"]    = time.strftime('%H:%M:%S')
    cfg["last_event"]      = f"Irrigation {duration}s @ {time.strftime('%H:%M')}"
    log_watering(duration, reason, soil_before, hum_before)
    log_event("water", f"Pump {duration}s | soil={soil_before:.1f}% | {reason}")
    return True

def pump_manual_on():
    """Keep pump running until manually turned off."""
    with _pump_lock:
        cfg["pump_manual_on"] = True
        cfg["pump"] = "ON"
        pump_relay.value = True
    cfg["last_event"] = f"Pump MANUAL ON @ {time.strftime('%H:%M')}"
    log_event("water", "Pump manual ON activated")

def pump_manual_off():
    """Force pump off, clear manual hold."""
    with _pump_lock:
        cfg["pump_manual_on"] = False
        pump_relay.value = False
        cfg["pump"] = "OFF"
    cfg["last_event"] = f"Pump MANUAL OFF @ {time.strftime('%H:%M')}"
    log_event("water", "Pump manual OFF activated")

# ================== 5. PARTY MODE (RELAY ALTERNATING) ==================

_party_thread = None

def _party_relay_a_loop():
    """Independent sub-loop for Relay A (pump)."""
    while cfg["party_mode"]:
        # ON phase
        pump_relay.value = True
        data["party_relay_a_state"] = True
        t = 0.0
        while cfg["party_mode"] and t < cfg["party_relay_a_on"]:
            time.sleep(0.05); t += 0.05
        # OFF phase
        pump_relay.value = False
        data["party_relay_a_state"] = False
        t = 0.0
        while cfg["party_mode"] and t < cfg["party_relay_a_off"]:
            time.sleep(0.05); t += 0.05
        if cfg["party_mode"]:
            data["party_cycles"] += 1

def _party_relay_b_loop():
    """Independent sub-loop for Relay B (motor)."""
    while cfg["party_mode"]:
        # ON phase
        motor_relay.value = True
        data["party_relay_b_state"] = True
        t = 0.0
        while cfg["party_mode"] and t < cfg["party_relay_b_on"]:
            time.sleep(0.05); t += 0.05
        # OFF phase
        motor_relay.value = False
        data["party_relay_b_state"] = False
        t = 0.0
        while cfg["party_mode"] and t < cfg["party_relay_b_off"]:
            time.sleep(0.05); t += 0.05

def _party_loop():
    """Main party loop dispatcher."""
    data["party_cycles"] = 0

    if cfg["party_sync"]:
        # Synchronized: A on → A off/B on → B off → repeat
        phase = "A_on"
        timer = 0.0
        while cfg["party_mode"]:
            time.sleep(0.05)
            timer += 0.05
            if phase == "A_on":
                pump_relay.value = True;  motor_relay.value = False
                data["party_relay_a_state"] = True; data["party_relay_b_state"] = False
                if timer >= max(0.05, cfg["party_relay_a_on"]):
                    phase = "A_off"; timer = 0.0
            elif phase == "A_off":
                pump_relay.value = False; motor_relay.value = False
                data["party_relay_a_state"] = False; data["party_relay_b_state"] = False
                if timer >= max(0.05, cfg["party_relay_a_off"]):
                    phase = "B_on"; timer = 0.0
            elif phase == "B_on":
                pump_relay.value = False; motor_relay.value = True
                data["party_relay_a_state"] = False; data["party_relay_b_state"] = True
                if timer >= max(0.05, cfg["party_relay_b_on"]):
                    phase = "B_off"; timer = 0.0
            elif phase == "B_off":
                pump_relay.value = False; motor_relay.value = False
                data["party_relay_a_state"] = False; data["party_relay_b_state"] = False
                if timer >= max(0.05, cfg["party_relay_b_off"]):
                    phase = "A_on"; timer = 0.0; data["party_cycles"] += 1
    else:
        # Independent: A and B run their own ON/OFF cycles in separate threads
        t_a = threading.Thread(target=_party_relay_a_loop, daemon=True)
        t_b = threading.Thread(target=_party_relay_b_loop, daemon=True)
        t_a.start(); t_b.start()
        t_a.join(); t_b.join()

    # Cleanup
    pump_relay.value  = False
    motor_relay.value = False
    cfg["pump"] = "OFF"
    data["party_relay_a_state"] = False
    data["party_relay_b_state"] = False
    log_event("party", f"Party mode stopped after {data['party_cycles']} cycles")

def start_party():
    global _party_thread
    if cfg["party_mode"]: return
    # Stop manual pump hold if active
    if cfg["pump_manual_on"]: pump_manual_off()
    # Ensure motor not running
    cfg["is_motor_running"] = False
    motor_relay.value = False
    cfg["party_mode"] = True
    cfg["pump"] = "PARTY"
    log_event("party", (
        f"Party mode started! sync={cfg['party_sync']} "
        f"A:{cfg['party_relay_a_on']}s ON/{cfg['party_relay_a_off']}s OFF  "
        f"B:{cfg['party_relay_b_on']}s ON/{cfg['party_relay_b_off']}s OFF"
    ))
    _party_thread = threading.Thread(target=_party_loop, daemon=True)
    _party_thread.start()
    cfg["last_event"] = f"🎉 Party Mode ON @ {time.strftime('%H:%M')}"

def stop_party():
    cfg["party_mode"] = False   # signals all sub-loops to exit
    # Give loop time to clean up, then force-off
    time.sleep(0.2)
    pump_relay.value  = False
    motor_relay.value = False
    cfg["pump"] = "OFF"
    data["party_relay_a_state"] = False
    data["party_relay_b_state"] = False
    cfg["last_event"] = f"🛑 Party Mode OFF @ {time.strftime('%H:%M')}"

# ================== 6. COMPUTED METRICS ==================

def compute_vpd(temp, rh):
    """Vapour Pressure Deficit in kPa"""
    try:
        svp = 0.6108 * math.exp((17.27*temp)/(temp+237.3))
        avp = svp * rh / 100
        return round(svp - avp, 3)
    except: return 0.0

def compute_dew_point(temp, rh):
    """Magnus formula dew point °C"""
    try:
        a, b = 17.27, 237.3
        alpha = ((a*temp)/(b+temp)) + math.log(rh/100.0)
        return round((b*alpha)/(a-alpha), 1)
    except: return 0.0

def compute_heat_index(temp, rh):
    """Simplified heat index °C"""
    try:
        tf = temp*9/5+32
        hi = (-42.379 + 2.04901523*tf + 10.14333127*rh
              - 0.22475541*tf*rh - 0.00683783*tf**2
              - 0.05481717*rh**2 + 0.00122874*tf**2*rh
              + 0.00085282*tf*rh**2 - 0.00000199*tf**2*rh**2)
        return round((hi-32)*5/9, 1)
    except: return temp

def compute_abs_humidity(temp, rh):
    """Absolute humidity g/m³"""
    try:
        return round(6.112 * math.exp((17.67*temp)/(temp+243.5)) * rh * 2.1674 / (273.15+temp), 2)
    except: return 0.0

def comfort_label(temp, rh, vpd):
    if temp < 10: return "❄️ Cold"
    if temp > 35: return "🔥 Hot"
    if rh < 30:   return "🏜️ Dry"
    if rh > 80:   return "💦 Muggy"
    if vpd < 0.4: return "😓 Stuffy"
    if vpd > 1.6: return "🌵 Arid"
    if 18<=temp<=26 and 40<=rh<=65: return "😊 Ideal"
    return "👍 OK"

def co2_est_label(rh):
    """Rough CO2 comfort estimation from humidity as proxy"""
    if rh > 75: return "⚠️ Poor ventilation likely"
    if rh > 60: return "🟡 Moderate"
    return "🟢 Good air exchange"

def compute_evap_rate(temp, rh, light):
    """Rough Penman-Monteith inspired evapotranspiration estimate mm/h"""
    try:
        rn = light / 100 * 0.8  # simplified net radiation proxy
        vpd_val = compute_vpd(temp, rh)
        et = max(0, (0.0023*(temp+17.8)*rn + 0.00026*(temp+20)*vpd_val*2))
        return round(et, 3)
    except: return 0.0

def soil_ec_estimate(soil_pct):
    """Very rough EC estimate from moisture %"""
    # Just a toy model: drier = higher resistance, lower EC
    try: return round(0.05 + (soil_pct/100)*1.8, 2)
    except: return 0.0

# ================== 7. UTILITIES ==================

def is_night():
    h = time.localtime().tm_hour
    ns, ne = cfg["night_start"], cfg["night_end"]
    return (h >= ns or h < ne) if ns > ne else (ns <= h < ne)

def can_alert(alert_type):
    now = time.time()
    with _alert_lock:
        if now - _last_alert_ts.get(alert_type, 0) >= ALERT_COOLDOWN:
            _last_alert_ts[alert_type] = now; return True
    return False

def compute_trend(hist, current):
    if len(hist) < 3: return "→"
    avg = sum(hist[-3:]) / 3
    return "↑" if current > avg+1 else ("↓" if current < avg-1 else "→")

def health_score():
    s = 100
    if data["soil"]  < cfg["th_soil_crit"]:  s -= 30
    elif data["soil"] < cfg["th_soil"]:       s -= 15
    if data["hum"]   < cfg["th_air"]:         s -= 15
    if data["hum"]   > cfg["th_hum_high"]:    s -= 10
    if data["temp"]  > cfg["th_temp_high"]:   s -= 20
    if data["temp"]  < cfg["th_temp_low"]:    s -= 20
    if data["is_raining"]:                     s -= 5
    if not data["last_read_ok"]:              s -= 10
    vpd = data.get("vpd", 0)
    if vpd > 2.0: s -= 10
    if vpd < 0.3 and vpd > 0: s -= 5
    return max(0, min(100, s))

def health_label(s):
    if s >= 85: return "Excellent 🌟"
    if s >= 65: return "Good ✅"
    if s >= 45: return "Fair ⚠️"
    if s >= 25: return "Poor 🔴"
    return "Critical ‼️"

def uptime_str():
    s = int(time.time()-cfg["boot_time"]); h,rem = divmod(s,3600); m = rem//60
    return f"{h}h {m}m"

def fmt_ts(ts): return time.strftime('%m/%d %H:%M', time.localtime(ts))

def cooldown_remain():
    """Return 0 if pump is manually on or just triggered, else remaining cooldown seconds."""
    if cfg["pump_manual_on"] or cfg["party_mode"]: return 0
    return max(0, int(cfg["water_cooldown"] - (time.time() - cfg["last_watered_ts"])))

# ================== 8. CORE MONITOR ==================

def core_monitor():
    log_interval = 0; sched_tick = 0
    while True:
        try:
            lv = l_chan.voltage; sv = s_chan.voltage; rv = r_chan.voltage
            data["light"] = max(0, min(100, (1-(lv/3.3))*100))
            data["soil"]  = max(0, min(100, (1-(sv/2.5))*100))
            data["rain"]  = max(0, min(100, (1-(rv/3.3))*100))

            try:
                t, h = dht_device.temperature, dht_device.humidity
                if t is not None and h is not None:
                    data["temp"], data["hum"] = t, h; data["last_read_ok"] = True
                    for key, val in [("_temp_hist",t),("_hum_hist",h),("_soil_hist",data["soil"])]:
                        data[key].append(val)
                        if len(data[key]) > 20: data[key].pop(0)
                    data["temp_trend"] = compute_trend(data["_temp_hist"], t)
                    data["hum_trend"]  = compute_trend(data["_hum_hist"],  h)
                    data["soil_trend"] = compute_trend(data["_soil_hist"], data["soil"])
            except: data["sensor_errors"] += 1; data["last_read_ok"] = False

            # Update computed metrics
            t, h = data["temp"], data["hum"]
            data["vpd"]       = compute_vpd(t, h)
            data["dew_point"] = compute_dew_point(t, h)
            data["heat_index"]= compute_heat_index(t, h)
            data["abs_hum"]   = compute_abs_humidity(t, h)
            data["comfort"]   = comfort_label(t, h, data["vpd"])
            data["evap_rate"] = compute_evap_rate(t, h, data["light"])
            data["co2_est"]   = co2_est_label(h)
            data["soil_ec"]   = soil_ec_estimate(data["soil"])

            if data["temp"]  > (data["max_t"] or -999):  data["max_t"]    = data["temp"]
            if data["min_t"] is None or data["temp"]  < data["min_t"]: data["min_t"]    = data["temp"]
            if data["hum"]   > (data["max_h"] or -999):  data["max_h"]    = data["hum"]
            if data["min_h"] is None or data["hum"]   < data["min_h"]: data["min_h"]    = data["hum"]
            if data["soil"]  > (data["max_soil"] or -999):data["max_soil"] = data["soil"]
            if data["min_soil"] is None or data["soil"]< data["min_soil"]:data["min_soil"]= data["soil"]
            if data["light"] > data["max_light"]:         data["max_light"]= data["light"]

            data["is_raining"] = data["rain"]  > cfg["th_rain"]
            data["is_bright"]  = data["light"] > cfg["th_light"]
            night = is_night(); cfg["night_mode"] = night

            # Auto mode logic (skip if party mode active)
            if cfg["auto_mode"] and not cfg["party_mode"]:
                if cfg["smart_curtain"]:
                    if (data["is_raining"] or data["is_bright"]) and cfg["motor_pos"] == "OPEN":
                        r = "rain" if data["is_raining"] else "bright"
                        threading.Thread(target=run_motor, args=("CLOSED",r), daemon=True).start()
                    elif night and cfg["motor_pos"] == "OPEN":
                        threading.Thread(target=run_motor, args=("CLOSED","night"), daemon=True).start()
                    elif not data["is_raining"] and not data["is_bright"] and not night and cfg["motor_pos"] == "CLOSED":
                        threading.Thread(target=run_motor, args=("OPEN","auto"), daemon=True).start()

                if data["soil"] < cfg["th_soil"] and data["hum"] < cfg["th_air"]:
                    data["consecutive_dry"] += 1
                else:
                    data["consecutive_dry"] = 0
                if (data["consecutive_dry"] >= 2 and cfg["pump"] == "OFF"
                        and not cfg["pump_manual_on"]
                        and (not data["is_raining"] or not cfg["rain_skip_water"]) and not night):
                    if run_pump_action("auto"): data["consecutive_dry"] = 0

            # DB log every ~60s
            log_interval += 1
            if log_interval >= 30: log_sensor(); log_interval = 0

            # Schedule check every ~60s
            sched_tick += 1
            if sched_tick >= 30:
                sched_tick = 0
                now_t  = time.localtime()
                now_h  = now_t.tm_hour
                now_m  = now_t.tm_min
                now_wd = now_t.tm_wday   # 0=Mon … 6=Sun
                for slot in watering_schedule:
                    if not slot["enabled"]: continue
                    if slot["hour"] != now_h or slot["minute"] != now_m: continue
                    days = slot.get("days", "all")
                    if days == "weekday" and now_wd > 4: continue   # skip Sat/Sun
                    if days == "weekend" and now_wd < 5: continue   # skip Mon-Fri
                    with _pump_lock:
                        if cfg["pump"] != "OFF": continue
                    threading.Thread(target=run_pump_action,
                        args=("schedule", slot["duration"]), daemon=True).start()

            hs = health_score()
            mint = f"{data['min_t']:.1f}" if data['min_t'] is not None else "--"
            maxt = f"{data['max_t']:.1f}" if data['max_t'] is not None else "--"
            minh = f"{data['min_h']:.1f}" if data['min_h'] is not None else "--"
            maxh = f"{data['max_h']:.1f}" if data['max_h'] is not None else "--"
            mins = f"{data['min_soil']:.1f}" if data['min_soil'] is not None else "--"
            maxs = f"{data['max_soil']:.1f}" if data['max_soil'] is not None else "--"
            os.system('clear')
            print("═"*70)
            print(f"  🌿 GreenHouse OS v{VERSION} {'🟡 VIRTUAL' if VIRTUAL else '🟢 REAL HW'} | {time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  Uptime:{uptime_str()} | Health:{hs}% {health_label(hs)} | {'🌙 Night' if night else '☀️ Day'}")
            print("═"*70)
            print(f"  🌡️  Temp:    {data['temp']:.1f}°C {data['temp_trend']}  (↕ {mint}~{maxt})")
            print(f"  💧  Humid:   {data['hum']:.1f}% {data['hum_trend']}  (↕ {minh}~{maxh}%)")
            print(f"  🪴  Soil:    {data['soil']:.1f}% {data['soil_trend']}  (↕ {mins}~{maxs}%)")
            print(f"  ☀️   Light:   {data['light']:.1f}%  (peak {data['max_light']:.1f}%) {'[BRIGHT⚠️]' if data['is_bright'] else ''}")
            print(f"  🌧️  Rain:    {data['rain']:.1f}%  {'[RAINING🌧️]' if data['is_raining'] else '[DRY]'}")
            print("─"*70)
            print(f"  📊  VPD:     {data['vpd']:.3f} kPa | Dew:{data['dew_point']:.1f}°C | HI:{data['heat_index']:.1f}°C")
            print(f"  💨  AbsHum:  {data['abs_hum']:.2f} g/m³ | ET:{data['evap_rate']:.3f}mm/h | Soil EC:~{data['soil_ec']:.2f}dS/m")
            print(f"  😊  Comfort: {data['comfort']} | CO2: {data['co2_est']}")
            print("─"*70)
            print(f"  🪟  Curtain: {cfg['motor_pos']} ({'Running' if cfg['is_motor_running'] else 'Idle'}) | Moves:{data['motor_count']}")
            print(f"  🚿  Pump:    {cfg['pump']} {'[MANUAL ON]' if cfg['pump_manual_on'] else ''} | Last:{cfg['last_watered']} | Count:{data['water_count']}")
            print(f"  🤖  Mode:    {'AUTO✅' if cfg['auto_mode'] else 'MANUAL🔴'} | Profile:{PLANT_PROFILES.get(cfg['plant_profile'],{}).get('name','?')}")
            print(f"  🎉  Party:   {'ON🎉' if cfg['party_mode'] else 'OFF'} | Cycles:{data['party_cycles']}")
            print(f"  🔌  Sensor: {'OK✅' if data['last_read_ok'] else 'ERR❌'} | Errors:{data['sensor_errors']} | Dry:{data['consecutive_dry']}/2")
            print(f"  📋  Event:  {cfg['last_event']}")
            print(f"  📆  Scheds: {len([s for s in watering_schedule if s['enabled']])} active / {len(watering_schedule)} total")
            print("═"*70)
        except Exception as e: print(f"Core error: {e}")
        time.sleep(2)

# ================== 9. WEB DASHBOARD ==================

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🌿 GreenHouse OS v4</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#04080a;--panel:#0b1820;--border:rgba(52,211,153,0.10);
  --accent:#34d399;--blue:#38bdf8;--amber:#fbbf24;--red:#f87171;
  --purple:#a78bfa;--pink:#f472b6;--teal:#2dd4bf;
  --text:#d1fae5;--muted:#3d6457;--font:'Space Grotesk',sans-serif;
  --mono:'JetBrains Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:0;
  background:
    radial-gradient(ellipse 80% 60% at 10% 15%,rgba(52,211,153,0.06) 0%,transparent 55%),
    radial-gradient(ellipse 60% 50% at 90% 85%,rgba(56,189,248,0.05) 0%,transparent 55%),
    radial-gradient(ellipse 40% 40% at 50% 50%,rgba(167,139,250,0.02) 0%,transparent 60%),
    repeating-linear-gradient(0deg,transparent,transparent 39px,rgba(52,211,153,0.015) 39px,rgba(52,211,153,0.015) 40px),
    repeating-linear-gradient(90deg,transparent,transparent 39px,rgba(52,211,153,0.015) 39px,rgba(52,211,153,0.015) 40px);
  pointer-events:none}
.wrap{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:20px 16px}

/* ── NAV TABS ── */
.nav-tabs{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:8px}
.nav-tab{padding:8px 16px;border-radius:10px;border:none;font-family:var(--font);font-size:.78rem;font-weight:600;cursor:pointer;color:var(--muted);background:transparent;transition:all .2s}
.nav-tab.active,.nav-tab:hover{background:rgba(52,211,153,0.12);color:var(--accent)}
.tab-section{display:none}.tab-section.active{display:block}

/* ── HEADER ── */
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:12px}
.brand{display:flex;align-items:center;gap:14px}
.brand-mark{width:50px;height:50px;border-radius:14px;background:linear-gradient(135deg,#34d399,#059669 50%,#0891b2);display:flex;align-items:center;justify-content:center;font-size:24px;box-shadow:0 0 30px rgba(52,211,153,0.35)}
.brand-title{font-size:1.3rem;font-weight:700;color:#fff;letter-spacing:-.02em}
.brand-sub{font-family:var(--mono);font-size:.62rem;color:var(--muted);display:flex;gap:10px;margin-top:2px;flex-wrap:wrap}
.brand-sub b{color:var(--accent)}
.header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.live-badge{display:flex;align-items:center;gap:7px;background:rgba(52,211,153,0.07);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-family:var(--mono);font-size:.7rem;color:var(--muted)}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent);animation:blink 1.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.party-badge{background:linear-gradient(90deg,#f472b6,#a78bfa,#38bdf8);background-size:200%;border-radius:20px;padding:7px 14px;font-family:var(--mono);font-size:.7rem;font-weight:700;color:#fff;animation:rainbow 2s linear infinite;display:none}
@keyframes rainbow{0%{background-position:0%}100%{background-position:200%}}
.party-badge.on{display:flex;align-items:center;gap:6px}

/* ── HEALTH BANNER ── */
.health-banner{display:flex;align-items:center;justify-content:space-between;background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:16px 22px;margin-bottom:14px;gap:16px;flex-wrap:wrap}
.health-score-wrap{display:flex;align-items:center;gap:14px}
.health-ring{width:60px;height:60px;flex-shrink:0}
.health-info h3{font-size:1.1rem;font-weight:700;color:#fff}
.health-info p{font-size:.68rem;color:var(--muted);font-family:var(--mono);margin-top:2px}
.health-pills{display:flex;gap:7px;flex-wrap:wrap}
.h-pill{display:flex;align-items:center;gap:5px;background:rgba(52,211,153,.07);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-family:var(--mono);font-size:.63rem;color:var(--muted)}
.h-pill b{color:var(--accent)}.h-pill.warn b{color:var(--amber)}.h-pill.bad b{color:var(--red)}

/* ── ENV BAR ── */
.env-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.env-pill{display:flex;align-items:center;gap:7px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:8px 12px;font-family:var(--mono);font-size:.68rem;color:var(--muted);flex:1;min-width:140px}
.env-pill.ok{border-color:rgba(52,211,153,.4);color:var(--accent)}.env-pill.warn{border-color:rgba(251,191,36,.4);color:var(--amber)}.env-pill.bad{border-color:rgba(248,113,113,.4);color:var(--red)}
.env-pill-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.env-pill-val{margin-left:auto;font-weight:500;color:#fff}
.night-bar{display:flex;align-items:center;gap:7px;background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.2);border-radius:8px;padding:6px 14px;font-family:var(--mono);font-size:.63rem;color:var(--purple);margin-bottom:10px}

/* ── METRIC CARDS ── */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:18px 20px 15px;position:relative;overflow:hidden;transition:transform .22s,border-color .22s;cursor:default}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.35}
.card.warn{border-color:rgba(251,191,36,.35)}.card.warn::before{background:linear-gradient(90deg,transparent,var(--amber),transparent);opacity:.8}
.card.bad{border-color:rgba(248,113,113,.35)}.card.bad::before{background:linear-gradient(90deg,transparent,var(--red),transparent);opacity:.8}
.card.active{border-color:rgba(52,211,153,.5);box-shadow:0 0 20px rgba(52,211,153,.12)}
.card.party-card{border-color:rgba(244,114,182,.5);box-shadow:0 0 20px rgba(244,114,182,.15);animation:party-glow 2s ease-in-out infinite alternate}
@keyframes party-glow{from{box-shadow:0 0 20px rgba(244,114,182,.15)}to{box-shadow:0 0 30px rgba(167,139,250,.3)}}
.card:hover{transform:translateY(-3px)}
.card-icon{font-size:1.4rem;margin-bottom:8px}
.card-label{font-family:var(--mono);font-size:.57rem;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:7px}
.card-val{font-family:var(--mono);font-size:2rem;font-weight:500;color:#fff;line-height:1}
.card-val.g{color:var(--accent)}.card-val.r{color:var(--red)}.card-val.a{color:var(--amber)}.card-val.b{color:var(--blue)}.card-val.p{color:var(--purple)}.card-val.t{color:var(--teal)}
.card-sub{font-family:var(--mono);font-size:.58rem;color:var(--muted);margin-top:6px;line-height:1.55}
.card-tag{display:inline-block;margin-top:7px;padding:3px 8px;border-radius:5px;font-family:var(--mono);font-size:.56rem;font-weight:600}
.tag-g{background:rgba(52,211,153,.12);color:var(--accent)}.tag-a{background:rgba(251,191,36,.12);color:var(--amber)}.tag-r{background:rgba(248,113,113,.12);color:var(--red)}.tag-b{background:rgba(56,189,248,.12);color:var(--blue)}.tag-p{background:rgba(167,139,250,.12);color:var(--purple)}.tag-t{background:rgba(45,212,191,.12);color:var(--teal)}
.trend{font-size:1rem;margin-left:3px;opacity:.7}
.bar-t{height:4px;background:rgba(255,255,255,.06);border-radius:2px;margin-top:9px;overflow:hidden}
.bar-f{height:100%;border-radius:2px;transition:width .6s cubic-bezier(.4,0,.2,1)}

/* ── STAT ROW ── */
.stat-row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.stat-mini{flex:1;min-width:100px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:11px 13px}
.sm-label{font-family:var(--mono);font-size:.52rem;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px}
.sm-val{font-family:var(--mono);font-size:1.15rem;font-weight:500;color:#fff;margin-top:3px}
.sm-unit{font-size:.58rem;color:var(--muted)}

/* ── LAYOUT ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.three-col{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:900px){.two-col,.three-col{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:18px 20px}
.panel-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.panel-title{font-size:.88rem;font-weight:700;color:#fff}
.tab-row{display:flex;gap:5px}
.tab{padding:4px 10px;border-radius:7px;border:1px solid var(--border);background:transparent;color:var(--muted);font-family:var(--mono);font-size:.62rem;cursor:pointer;transition:all .15s}
.tab.on{background:rgba(52,211,153,.12);color:var(--accent);border-color:rgba(52,211,153,.3)}

/* ── FORMS ── */
.ctrl-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.field-row{display:flex;flex-direction:column;gap:4px}
.field-label{font-family:var(--mono);font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
input[type=number],input[type=text],select{width:100%;padding:8px 10px;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:8px;color:#fff;font-family:var(--mono);font-size:.78rem;outline:none;transition:border-color .15s}
input[type=number]:focus,input[type=text]:focus,select:focus{border-color:var(--accent)}
input[type=range]{width:100%;accent-color:var(--accent)}
select option{background:#101f24}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 15px;border-radius:10px;border:none;font-family:var(--font);font-size:.78rem;font-weight:600;cursor:pointer;text-decoration:none;transition:all .18s;white-space:nowrap}
.btn-g{background:linear-gradient(135deg,#34d399,#059669);color:#001a0e}
.btn-b{background:linear-gradient(135deg,#38bdf8,#0284c7);color:#001a2a}
.btn-a{background:linear-gradient(135deg,#fbbf24,#d97706);color:#1a0e00}
.btn-p{background:linear-gradient(135deg,#a78bfa,#7c3aed);color:#fff}
.btn-r{background:linear-gradient(135deg,#f87171,#dc2626);color:#fff}
.btn-t{background:linear-gradient(135deg,#2dd4bf,#0d9488);color:#001a18}
.btn-party{background:linear-gradient(135deg,#f472b6,#a78bfa,#38bdf8);background-size:200%;color:#fff;animation:rainbow 2s linear infinite}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--muted)}
.btn:hover{opacity:.84;transform:translateY(-1px)}.btn:active{transform:scale(.98)}
.save-btn{width:100%;margin-top:11px;padding:10px;font-size:.83rem}
.btn-row{display:flex;flex-wrap:wrap;gap:8px}

/* ── TABLES ── */
.wlog-table,.sched-table,.cmd-table{width:100%;border-collapse:collapse;font-size:.7rem}
.wlog-table th,.sched-table th,.cmd-table th{font-family:var(--mono);font-size:.53rem;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;padding:5px 7px;text-align:left;border-bottom:1px solid var(--border)}
.wlog-table td,.sched-table td,.cmd-table td{padding:7px;border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle}
.wlog-table tr:last-child td,.sched-table tr:last-child td,.cmd-table tr:last-child td{border-bottom:none}
.sched-on{color:var(--accent)}.sched-off{color:var(--muted)}
.cmd-table td:first-child{font-family:var(--mono);color:var(--accent);font-weight:600;white-space:nowrap}
.cmd-table .cmd-cat{background:rgba(52,211,153,.05);color:var(--muted);font-size:.6rem;font-weight:700;letter-spacing:2px;text-transform:uppercase;padding:8px 7px}
.cmd-table .cmd-cat td{border-bottom:1px solid rgba(52,211,153,.1)}

/* ── PROFILES ── */
.profile-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:7px;margin-bottom:9px}
.profile-card{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:9px;padding:10px;cursor:pointer;transition:all .18s;text-align:center;text-decoration:none;display:block}
.profile-card:hover,.profile-card.active{background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.4)}
.profile-card.active{box-shadow:0 0 14px rgba(52,211,153,.15)}
.pc-icon{font-size:1.4rem;margin-bottom:3px}
.pc-name{font-family:var(--mono);font-size:.58rem;color:var(--muted)}

/* ── CURTAIN VIS ── */
.curtain-vis{display:flex;align-items:flex-end;justify-content:center;gap:4px;height:44px;margin:9px 0}
.curtain-slat{width:9px;border-radius:3px 3px 0 0;background:linear-gradient(180deg,#34d399,#059669);transition:height .8s cubic-bezier(.4,0,.2,1)}

/* ── PARTY SECTION ── */
.party-control{background:linear-gradient(135deg,rgba(244,114,182,.06),rgba(167,139,250,.06));border:1px solid rgba(244,114,182,.25);border-radius:16px;padding:18px 20px}
.party-viz{display:flex;gap:12px;justify-content:center;align-items:center;padding:16px 0}
.relay-block{display:flex;flex-direction:column;align-items:center;gap:6px}
.relay-lamp{width:48px;height:48px;border-radius:50%;border:2px solid rgba(255,255,255,.1);transition:all .3s;display:flex;align-items:center;justify-content:center;font-size:1.2rem}
.relay-lamp.a-off{background:#1a0020;box-shadow:none}
.relay-lamp.a-on{background:radial-gradient(#f472b6,#db2777);box-shadow:0 0 20px #f472b6;border-color:#f472b6}
.relay-lamp.b-off{background:#00101a;box-shadow:none}
.relay-lamp.b-on{background:radial-gradient(#38bdf8,#0284c7);box-shadow:0 0 20px #38bdf8;border-color:#38bdf8}
.relay-label{font-family:var(--mono);font-size:.62rem;color:var(--muted)}
.relay-arrow{font-size:1.4rem;color:var(--muted);animation:blink 1s ease-in-out infinite}

/* ── EVT LIST ── */
.evt-list{list-style:none;max-height:240px;overflow-y:auto}
.evt-item{display:flex;align-items:flex-start;gap:9px;padding:7px 2px;border-bottom:1px solid rgba(255,255,255,.03)}
.evt-item:last-child{border-bottom:none}
.evt-dot{width:7px;height:7px;border-radius:50%;margin-top:4px;flex-shrink:0}
.evt-dot.water{background:var(--blue)}.evt-dot.motor{background:var(--purple)}.evt-dot.system{background:var(--accent)}.evt-dot.alert{background:var(--red)}.evt-dot.party{background:var(--pink)}
.evt-time{font-family:var(--mono);font-size:.6rem;color:var(--muted);white-space:nowrap}
.evt-msg{font-family:var(--mono);font-size:.65rem;color:var(--text);flex:1}

/* ── VPD GAUGE ── */
.vpd-gauge{display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 0}
.vpd-arc-wrap{position:relative;width:120px;height:65px;overflow:hidden}

/* ── PUMP MANUAL CONTROLS ── */
.pump-manual-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px}

/* ── FOOTER ── */
.footer{text-align:center;margin-top:20px;font-family:var(--mono);font-size:.58rem;color:var(--muted)}

/* ── ALERT STRIP ── */
.alert-strip{display:none;background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);border-radius:10px;padding:10px 16px;margin-bottom:12px;font-family:var(--mono);font-size:.72rem;color:var(--red);align-items:center;gap:8px}
.alert-strip.show{display:flex}
</style>
</head>
<body>
<div class="wrap">

<header class="header">
  <div class="brand">
    <div class="brand-mark">🌿</div>
    <div>
      <div class="brand-title">GreenHouse OS</div>
      <div class="brand-sub">
        <b>v{{version}}</b>
        <span>uptime:{{uptime}}</span>
        <span>{{profile_name}}</span>
        <span>{{time_now}}</span>
        {% if virtual_mode %}<span style="color:var(--amber);font-weight:700">🟡 VIRTUAL</span>{% endif %}
      </div>
    </div>
  </div>
  <div class="header-right">
    <div class="party-badge {{'on' if c.party_mode else ''}}">🎉 PARTY MODE</div>
    <div class="live-badge" id="refreshBadge" onclick="toggleRefresh()" title="Click to pause/resume auto-refresh" style="cursor:pointer">
      <div class="pulse" id="refreshPulse"></div>
      <span id="refreshLabel">LIVE</span>
      <span id="refreshCountdown" style="margin-left:4px;color:var(--accent)">5s</span>
    </div>
    <select id="refreshInterval" onchange="changeInterval(this.value)"
      style="background:rgba(52,211,153,0.07);border:1px solid var(--border);border-radius:8px;
             color:var(--muted);font-family:var(--mono);font-size:.65rem;padding:5px 8px;cursor:pointer;outline:none">
      <option value="5">5s</option>
      <option value="10">10s</option>
      <option value="30">30s</option>
      <option value="60">60s</option>
      <option value="120">2min</option>
    </select>
  </div>
</header>

{% if night_mode %}<div class="night-bar">🌙 Night Mode — alerts suppressed until {{c.night_end}}:00 | Smart curtain closed</div>{% endif %}

{% if virtual_mode %}
<div style="display:flex;align-items:center;gap:10px;background:linear-gradient(90deg,rgba(251,191,36,.12),rgba(251,191,36,.06));border:1px solid rgba(251,191,36,.4);border-radius:12px;padding:11px 18px;margin-bottom:12px;font-family:var(--mono);font-size:.72rem;flex-wrap:wrap;gap:8px">
  <span style="font-size:1.1rem">🟡</span>
  <b style="color:var(--amber)">VIRTUAL MODE</b>
  <span style="color:var(--muted)">— 无真实硬件，传感器数据为模拟值，继电器不会动作。</span>
  <span style="color:var(--muted)">启动时加 <code style="background:rgba(255,255,255,.07);padding:2px 6px;border-radius:4px;color:#fff">--virtual</code> 参数或硬件库缺失时自动启用。</span>
</div>
{% endif %}

{% if not d.last_read_ok %}
<div class="alert-strip show">⚠️ Sensor Error — DHT22 read failed! Total errors: {{d.sensor_errors}}</div>
{% endif %}

<!-- NAV TABS -->
<nav class="nav-tabs">
  <button class="nav-tab active" onclick="showTab('overview',this)">📊 Overview</button>
  <button class="nav-tab" onclick="showTab('advanced',this)">🔬 Advanced Data</button>
  <button class="nav-tab" onclick="showTab('controls',this)">🎮 Controls</button>
  <button class="nav-tab" onclick="showTab('party',this)">🎉 Party Mode</button>
  <button class="nav-tab" onclick="showTab('charts',this)">📈 Charts</button>
  <button class="nav-tab" onclick="showTab('logs',this)">📋 Logs</button>
  <button class="nav-tab" onclick="showTab('settings',this)">⚙️ Settings</button>
  <button class="nav-tab" onclick="showTab('telegram',this)">🤖 Telegram</button>
</nav>

<!-- ══════════ TAB: OVERVIEW ══════════ -->
<section class="tab-section active" id="tab-overview">

<!-- HEALTH -->
<div class="health-banner">
  <div class="health-score-wrap">
    <svg class="health-ring" viewBox="0 0 60 60">
      <circle cx="30" cy="30" r="25" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="5"/>
      <circle cx="30" cy="30" r="25" fill="none" stroke="{{health_color}}" stroke-width="5"
        stroke-dasharray="{{health_dash}} 999" stroke-linecap="round" transform="rotate(-90 30 30)"/>
      <text x="30" y="35" text-anchor="middle" fill="#fff" font-size="11" font-family="JetBrains Mono" font-weight="500">{{health_score}}</text>
    </svg>
    <div class="health-info">
      <h3>Plant Health — {{health_label}}</h3>
      <p>Comfort: {{d.comfort}} | Profile: {{profile_name}}</p>
    </div>
  </div>
  <div class="health-pills">
    <div class="h-pill {{'bad' if d.temp > c.th_temp_high or d.temp < c.th_temp_low else ''}}">🌡️ Temp <b>{{d.temp|round(1)}}°C</b></div>
    <div class="h-pill {{'bad' if d.soil < c.th_soil_crit else ('warn' if d.soil < c.th_soil else '')}}">🪴 Soil <b>{{d.soil|round(1)}}%</b></div>
    <div class="h-pill {{'warn' if d.hum < c.th_air else ''}}">💧 Hum <b>{{d.hum|round(1)}}%</b></div>
    <div class="h-pill {{'warn' if d.is_bright else ''}}">☀️ Light <b>{{d.light|round(1)}}%</b></div>
    <div class="h-pill {{'warn' if d.is_raining else ''}}">🌧️ Rain <b>{{d.rain|round(1)}}%</b></div>
    <div class="h-pill {{'warn' if d.vpd > 1.6 or d.vpd < 0.3 else ''}}">💨 VPD <b>{{d.vpd}}kPa</b></div>
    <div class="h-pill {{'bad' if not d.last_read_ok else ''}}">🔌 <b>{{'ERR' if not d.last_read_ok else 'OK'}}</b></div>
  </div>
</div>

<!-- ENV BAR -->
<div class="env-bar">
  <div class="env-pill {{'bad' if d.temp > c.th_temp_high or d.temp < c.th_temp_low else 'ok'}}"><div class="env-pill-dot"></div>🌡️ Temp<span class="env-pill-val">{{d.temp|round(1)}}°C {{d.temp_trend}}</span></div>
  <div class="env-pill {{'warn' if d.is_bright else 'ok'}}"><div class="env-pill-dot"></div>☀️ Light<span class="env-pill-val">{{d.light|round(1)}}%</span></div>
  <div class="env-pill {{'warn' if d.is_raining else 'ok'}}"><div class="env-pill-dot"></div>🌧️ Rain<span class="env-pill-val">{{'🌧️ RAIN' if d.is_raining else '☀️ DRY'}}</span></div>
  <div class="env-pill {{'ok' if c.auto_mode else 'warn'}}"><div class="env-pill-dot"></div>🤖 Mode<span class="env-pill-val">{{'AUTO' if c.auto_mode else 'MANUAL'}}</span></div>
  <div class="env-pill {{'bad' if c.pump == 'ON' else ('warn' if c.pump == 'PARTY' else 'ok')}}"><div class="env-pill-dot"></div>🚿 Pump<span class="env-pill-val">{{c.pump}}{{'🔒' if c.pump_manual_on else ''}}</span></div>
  <div class="env-pill {{'warn' if night_mode else 'ok'}}"><div class="env-pill-dot"></div>🕐 Period<span class="env-pill-val">{{'NIGHT 🌙' if night_mode else 'DAY ☀️'}}</span></div>
  <div class="env-pill {{'warn' if d.vpd > 1.6 or d.vpd < 0.4 else 'ok'}}"><div class="env-pill-dot"></div>💨 VPD<span class="env-pill-val">{{d.vpd}}kPa</span></div>
  <div class="env-pill ok"><div class="env-pill-dot"></div>🌡️ Dew<span class="env-pill-val">{{d.dew_point}}°C</span></div>
</div>

<!-- METRIC CARDS -->
<div class="metrics">
  <div class="card {{'bad' if d.temp > c.th_temp_high or d.temp < c.th_temp_low else ''}}">
    <div class="card-icon">🌡️</div><div class="card-label">Temperature</div>
    <div class="card-val {{'r' if d.temp > c.th_temp_high or d.temp < c.th_temp_low else 'g'}}">{{d.temp|round(1)}}°<span class="trend">{{d.temp_trend}}</span></div>
    <div class="card-sub">Today {{d.min_t|round(1) if d.min_t is not none else '--'}} – {{d.max_t|round(1) if d.max_t is not none else '--'}}°C<br>Heat Index: {{d.heat_index|round(1)}}°C</div>
    {% if d.temp > c.th_temp_high %}<span class="card-tag tag-r">⚠ Too Hot</span>{% elif d.temp < c.th_temp_low %}<span class="card-tag tag-r">⚠ Too Cold</span>{% else %}<span class="card-tag tag-g">✓ Normal</span>{% endif %}
    <div class="bar-t"><div class="bar-f" style="width:{{((d.temp+10)/55*100)|int}}%;background:linear-gradient(90deg,#34d399,#fbbf24,#f87171)"></div></div>
  </div>
  <div class="card">
    <div class="card-icon">💧</div><div class="card-label">Air Humidity</div>
    <div class="card-val b">{{d.hum|round(1)}}%<span class="trend">{{d.hum_trend}}</span></div>
    <div class="card-sub">Target >{{c.th_air}}% | High >{{c.th_hum_high}}%<br>Abs: {{d.abs_hum}}g/m³ | Today {{d.min_h|round(1) if d.min_h is not none else '--'}}–{{d.max_h|round(1) if d.max_h is not none else '--'}}%</div>
    {% if d.hum > c.th_hum_high %}<span class="card-tag tag-a">⚠ Mold Risk</span>{% elif d.hum < c.th_air %}<span class="card-tag tag-a">⚠ Low</span>{% else %}<span class="card-tag tag-g">✓ Good</span>{% endif %}
    <div class="bar-t"><div class="bar-f" style="width:{{d.hum|int}}%;background:#38bdf8"></div></div>
  </div>
  <div class="card {{'bad' if d.soil < c.th_soil_crit else ('warn' if d.soil < c.th_soil else '')}}">
    <div class="card-icon">🪴</div><div class="card-label">Soil Moisture</div>
    <div class="card-val {{'r' if d.soil < c.th_soil_crit else ('a' if d.soil < c.th_soil else 'g')}}">{{d.soil|round(1)}}%<span class="trend">{{d.soil_trend}}</span></div>
    <div class="card-sub">Crit&lt;{{c.th_soil_crit}}% | Target>{{c.th_soil}}%<br>EC ~{{d.soil_ec}} dS/m | Today {{d.min_soil|round(1) if d.min_soil is not none else '--'}}–{{d.max_soil|round(1) if d.max_soil is not none else '--'}}%</div>
    {% if d.soil < c.th_soil_crit %}<span class="card-tag tag-r">‼ Critical Dry</span>{% elif d.soil < c.th_soil %}<span class="card-tag tag-a">⚠ Dry</span>{% else %}<span class="card-tag tag-g">✓ Moist</span>{% endif %}
    <div class="bar-t"><div class="bar-f" style="width:{{d.soil|int}}%;background:#34d399"></div></div>
  </div>
  <div class="card {{'warn' if d.is_bright else ''}}">
    <div class="card-icon">☀️</div><div class="card-label">Light Level</div>
    <div class="card-val {{'a' if d.is_bright else 'g'}}">{{d.light|round(1)}}%</div>
    <div class="card-sub">Threshold {{c.th_light}}% | Peak {{d.max_light|round(1)}}%<br>ET ~{{d.evap_rate}}mm/h</div>
    <span class="card-tag {{'tag-a' if d.is_bright else 'tag-g'}}">{{'☀️ Bright' if d.is_bright else ('Moderate' if d.light > 40 else 'Low Light')}}</span>
    <div class="bar-t"><div class="bar-f" style="width:{{d.light|int}}%;background:#fbbf24"></div></div>
  </div>
  <div class="card {{'warn' if d.is_raining else ''}}">
    <div class="card-icon">🌧️</div><div class="card-label">Rain Sensor</div>
    <div class="card-val {{'b' if d.is_raining else 'g'}}">{{d.rain|round(1)}}%</div>
    <div class="card-sub">Trigger >{{c.th_rain}}%<br>Skip irrigation: {{'✓' if c.rain_skip_water else '✗'}}</div>
    <span class="card-tag {{'tag-a' if d.is_raining else 'tag-g'}}">{{'🌧️ Raining' if d.is_raining else '☀️ Dry'}}</span>
    <div class="bar-t"><div class="bar-f" style="width:{{d.rain|int}}%;background:#38bdf8"></div></div>
  </div>
  <div class="card {{'warn' if d.vpd > 1.6 or d.vpd < 0.3 else ''}}">
    <div class="card-icon">💨</div><div class="card-label">VPD</div>
    <div class="card-val t">{{d.vpd}}<span style="font-size:.9rem"> kPa</span></div>
    <div class="card-sub">Dew Point: {{d.dew_point}}°C<br>Ideal: 0.4–1.2 kPa</div>
    {% if d.vpd < 0.3 %}<span class="card-tag tag-b">💦 Saturated</span>
    {% elif d.vpd < 0.8 %}<span class="card-tag tag-g">🌱 Seedling Zone</span>
    {% elif d.vpd < 1.2 %}<span class="card-tag tag-g">✓ Veg Optimal</span>
    {% elif d.vpd < 1.6 %}<span class="card-tag tag-a">🌸 Flower Zone</span>
    {% else %}<span class="card-tag tag-r">⚠ High Stress</span>{% endif %}
    <div class="bar-t"><div class="bar-f" style="width:{{(d.vpd/3*100)|int}}%;background:var(--teal)"></div></div>
  </div>
  <div class="card {{'active' if c.motor_pos == 'CLOSED' else ''}}">
    <div class="card-icon">🪟</div><div class="card-label">Curtain / Cover</div>
    <div class="card-val {{'p' if c.motor_pos == 'CLOSED' else 'g'}}">{{c.motor_pos}}</div>
    <div class="card-sub">Cycles: {{d.motor_count}} | Travel: {{c.motor_duration}}s<br>{{'⚙️ Running...' if c.is_motor_running else 'Idle'}}</div>
    <div class="curtain-vis" id="curtainVis"></div>
  </div>
  <div class="card {{'party-card' if c.party_mode else ('bad' if c.pump == 'ON' else '')}}">
    <div class="card-icon">{{'🎉' if c.party_mode else '🚿'}}</div><div class="card-label">{{'PARTY' if c.party_mode else 'Irrigation'}}</div>
    <div class="card-val {{'p' if c.party_mode else ('r' if c.pump == 'ON' else 'g')}}">{{c.pump}}{{'🔒' if c.pump_manual_on else ''}}</div>
    <div class="card-sub">Last: {{c.last_watered}} | Count: {{d.water_count}}<br>Cooldown: {{cooldown_remain}}s</div>
    {% if c.party_mode %}<span class="card-tag tag-p">🎉 Party Cycles: {{d.party_cycles}}</span>
    {% elif c.pump_manual_on %}<span class="card-tag tag-r">🔒 Manual ON</span>
    {% else %}<span class="card-tag tag-g">Ready</span>{% endif %}
    <div class="bar-t"><div class="bar-f" style="width:{{'100' if c.pump == 'ON' else '0'}}%;background:#f87171;transition:width .3s"></div></div>
  </div>
  <div class="card">
    <div class="card-icon">🤖</div><div class="card-label">Auto Control</div>
    <div class="card-val {{'g' if c.auto_mode else 'a'}}" style="font-size:1.25rem">{{'AUTO' if c.auto_mode else 'MANUAL'}}</div>
    <div class="card-sub">Dry streak: {{d.consecutive_dry}}/2<br>{{c.last_event}}</div>
    <span class="card-tag {{'tag-g' if c.auto_mode else 'tag-a'}}">{{'🌙 Night' if night_mode else '☀️ Day'}}</span>
  </div>
  <div class="card">
    <div class="card-icon">😊</div><div class="card-label">Comfort Index</div>
    <div class="card-val g" style="font-size:1.15rem">{{d.comfort}}</div>
    <div class="card-sub">{{d.co2_est}}<br>AbsHum: {{d.abs_hum}} g/m³</div>
    <span class="card-tag tag-t">ET: {{d.evap_rate}} mm/h</span>
  </div>
  <div class="card">
    <div class="card-icon">🔌</div><div class="card-label">System</div>
    <div class="card-val {{'r' if not d.last_read_ok else 'g'}}" style="font-size:1.25rem">{{'ERROR' if not d.last_read_ok else 'OK'}}</div>
    <div class="card-sub">Errors: {{d.sensor_errors}} | DB: {{db_sc}} records<br>Alerts today: {{d.alert_count}}</div>
    <span class="card-tag tag-g">⏱ {{uptime}}</span>
  </div>
</div>

<!-- STAT ROW -->
<div class="stat-row">
  <div class="stat-mini"><div class="sm-label">Waterings</div><div class="sm-val">{{d.water_count}} <span class="sm-unit">today</span></div></div>
  <div class="stat-mini"><div class="sm-label">Curtain</div><div class="sm-val">{{d.motor_count}} <span class="sm-unit">moves</span></div></div>
  <div class="stat-mini"><div class="sm-label">Alerts</div><div class="sm-val">{{d.alert_count}} <span class="sm-unit">today</span></div></div>
  <div class="stat-mini"><div class="sm-label">Party Cycles</div><div class="sm-val">{{d.party_cycles}} <span class="sm-unit">total</span></div></div>
  <div class="stat-mini"><div class="sm-label">Temp Range</div><div class="sm-val">{{(d.min_t|round(1)) if d.min_t is not none else '--'}}–{{(d.max_t|round(1)) if d.max_t is not none else '--'}} <span class="sm-unit">°C</span></div></div>
  <div class="stat-mini"><div class="sm-label">Hum Range</div><div class="sm-val">{{(d.min_h|round(1)) if d.min_h is not none else '--'}}–{{(d.max_h|round(1)) if d.max_h is not none else '--'}} <span class="sm-unit">%</span></div></div>
  <div class="stat-mini"><div class="sm-label">Soil Range</div><div class="sm-val">{{(d.min_soil|round(1)) if d.min_soil is not none else '--'}}–{{(d.max_soil|round(1)) if d.max_soil is not none else '--'}} <span class="sm-unit">%</span></div></div>
  <div class="stat-mini"><div class="sm-label">Peak Light</div><div class="sm-val">{{d.max_light|round(1)}} <span class="sm-unit">%</span></div></div>
  <div class="stat-mini"><div class="sm-label">VPD</div><div class="sm-val">{{d.vpd}} <span class="sm-unit">kPa</span></div></div>
  <div class="stat-mini"><div class="sm-label">Dew Point</div><div class="sm-val">{{d.dew_point}} <span class="sm-unit">°C</span></div></div>
  <div class="stat-mini"><div class="sm-label">Abs Humidity</div><div class="sm-val">{{d.abs_hum}} <span class="sm-unit">g/m³</span></div></div>
  <div class="stat-mini"><div class="sm-label">Uptime</div><div class="sm-val">{{uptime}}</div></div>
</div>

</section>

<!-- ══════════ TAB: ADVANCED DATA ══════════ -->
<section class="tab-section" id="tab-advanced">
<div class="two-col">
  <div class="panel">
    <div class="panel-header"><span class="panel-title">💨 Vapour Pressure Deficit</span></div>
    <div style="font-family:var(--mono);font-size:.75rem;line-height:2.3">
      <div>VPD: <b style="color:var(--teal);font-size:1.3rem">{{d.vpd}} kPa</b></div>
      <div style="color:var(--muted)">
        &lt;0.3 kPa = Overhydrated / disease risk<br>
        0.3–0.8 = Seedlings / clones ideal<br>
        0.8–1.2 = Vegetative optimal ✅<br>
        1.2–1.6 = Flowering optimal ✅<br>
        &gt;1.6 = Heat stress / wilting risk
      </div>
      <div style="margin-top:8px">
        <div class="bar-t" style="height:8px;border-radius:4px">
          <div class="bar-f" style="width:{{(d.vpd/3*100)|int}}%;background:linear-gradient(90deg,#38bdf8,#34d399,#fbbf24,#f87171);height:100%;border-radius:4px"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:.52rem;color:var(--muted);margin-top:3px">
          <span>0</span><span>0.8</span><span>1.2</span><span>1.6</span><span>3.0</span>
        </div>
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🌡️ Derived Temperature Metrics</span></div>
    <div style="font-family:var(--mono);font-size:.75rem;line-height:2.2">
      <div>Actual Temp: <b style="color:#fff">{{d.temp|round(2)}}°C</b></div>
      <div>Dew Point: <b style="color:var(--blue)">{{d.dew_point}}°C</b></div>
      <div>Heat Index: <b style="color:var(--amber)">{{d.heat_index|round(1)}}°C</b></div>
      <div>Dew Spread: <b style="color:var(--teal)">{{(d.temp - d.dew_point)|round(1)}}°C</b></div>
      <div style="color:var(--muted);font-size:.62rem;margin-top:4px">
        Dew spread &lt;3°C = very high condensation risk<br>
        Heat index warns of perceived heat stress
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">💧 Humidity Analysis</span></div>
    <div style="font-family:var(--mono);font-size:.75rem;line-height:2.2">
      <div>Relative Humidity: <b style="color:var(--blue)">{{d.hum|round(1)}}%</b></div>
      <div>Absolute Humidity: <b style="color:#fff">{{d.abs_hum}} g/m³</b></div>
      <div>Today Range: <b style="color:var(--muted)">{{(d.min_h|round(1)) if d.min_h is not none else '--'}} – {{(d.max_h|round(1)) if d.max_h is not none else '--'}}%</b></div>
      <div>Trend: <b style="color:#fff">{{d.hum_trend}}</b></div>
      <div>Air Quality: <b style="color:var(--accent)">{{d.co2_est}}</b></div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🪴 Soil Analysis</span></div>
    <div style="font-family:var(--mono);font-size:.75rem;line-height:2.2">
      <div>Moisture: <b style="color:var(--accent)">{{d.soil|round(2)}}%</b></div>
      <div>Est. EC: <b style="color:var(--teal)">~{{d.soil_ec}} dS/m</b></div>
      <div>Today Range: <b style="color:var(--muted)">{{(d.min_soil|round(1)) if d.min_soil is not none else '--'}} – {{(d.max_soil|round(1)) if d.max_soil is not none else '--'}}%</b></div>
      <div>Trend: <b style="color:#fff">{{d.soil_trend}}</b></div>
      <div>Consecutive Dry: <b style="color:{{'#f87171' if d.consecutive_dry >= 2 else '#fbbf24' if d.consecutive_dry == 1 else '#34d399'}}">{{d.consecutive_dry}} / 2</b></div>
      <div style="color:var(--muted);font-size:.62rem;margin-top:4px">EC estimate is approximate; calibrate with meter</div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">☀️ Evapotranspiration</span></div>
    <div style="font-family:var(--mono);font-size:.75rem;line-height:2.2">
      <div>ET Rate: <b style="color:var(--amber)">{{d.evap_rate}} mm/h</b></div>
      <div>Light: <b style="color:#fff">{{d.light|round(1)}}%</b> (peak {{d.max_light|round(1)}}%)</div>
      <div>Comfort Zone: <b style="color:var(--accent)">{{d.comfort}}</b></div>
      <div style="color:var(--muted);font-size:.62rem;margin-top:4px">
        Higher ET = plants need more water.<br>
        Based on simplified Penman-Monteith model.
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">📊 System Stats</span></div>
    <div style="font-family:var(--mono);font-size:.75rem;line-height:2.2">
      <div>Sensor Errors: <b style="color:{{'#f87171' if d.sensor_errors > 0 else '#34d399'}}">{{d.sensor_errors}}</b></div>
      <div>DB Records: <b style="color:#fff">{{db_sc}}</b></div>
      <div>Watering Records: <b style="color:#fff">{{db_wc}}</b></div>
      <div>Alert Records: <b style="color:#fff">{{db_ac}}</b></div>
      <div>Uptime: <b style="color:var(--accent)">{{uptime}}</b></div>
      <div>Party Cycles: <b style="color:var(--purple)">{{d.party_cycles}}</b></div>
    </div>
  </div>
</div>
</section>

<!-- ══════════ TAB: CONTROLS ══════════ -->
<section class="tab-section" id="tab-controls">
<div class="three-col">
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🚿 Pump Controls</span></div>
    <div class="btn-row" style="flex-direction:column;gap:8px">
      <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);padding:4px 0">⏱ TIMED RUN (original)</div>
      <a href="/water" class="btn btn-b">🚿 Water Now ({{c.water_duration}}s)</a>

      <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);padding:8px 0 4px;border-top:1px solid var(--border);margin-top:4px">🔒 MANUAL HOLD (stays on until you stop)</div>
      <a href="/pump_on" class="btn {{'btn-r' if c.pump_manual_on else 'btn-a'}}">
        {{'🔒 MANUAL ON (active)' if c.pump_manual_on else '▶ Pump Manual ON'}}
      </a>
      <a href="/pump_off" class="btn {{'btn-g' if c.pump_manual_on else 'btn-ghost'}}">
        {{'⏹ Stop Manual Pump' if c.pump_manual_on else '⏹ Pump Manual OFF'}}
      </a>

      <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);padding:8px 0 4px;border-top:1px solid var(--border);margin-top:4px">🧪 TEST & EMERGENCY</div>
      <a href="/test_pump" class="btn btn-ghost">🧪 Test Pump (1s)</a>
      <a href="/emergency_stop" class="btn btn-r">🛑 Emergency Stop All</a>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🪟 Curtain Controls</span></div>
    <div class="btn-row" style="flex-direction:column;gap:8px">
      <a href="/motor" class="btn btn-p">🪟 Toggle Curtain (→ {{'OPEN' if c.motor_pos == 'CLOSED' else 'CLOSE'}})</a>
      <a href="/motor_open" class="btn btn-ghost">🪟 Force OPEN</a>
      <a href="/motor_close" class="btn btn-ghost">🪟 Force CLOSE</a>
      <a href="/test_motor" class="btn btn-ghost">🧪 Test Motor</a>
      <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-top:8px">
        Current: <b style="color:var(--accent)">{{c.motor_pos}}</b><br>
        Smart Curtain: <b style="color:{{'#34d399' if c.smart_curtain else '#fbbf24'}}">{{ 'ON' if c.smart_curtain else 'OFF'}}</b>
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🎮 Quick Actions</span></div>
    <div class="btn-row" style="flex-direction:column;gap:8px">
      <a href="/toggle_auto" class="btn {{'btn-a' if c.auto_mode else 'btn-g'}}">🤖 Switch to {{'Manual' if c.auto_mode else 'Auto'}}</a>
      <a href="/toggle_alert" class="btn {{'btn-r' if c.alert_enabled else 'btn-ghost'}}">🔔 Alerts {{'ON — Disable' if c.alert_enabled else 'OFF — Enable'}}</a>
      <a href="/toggle_rain_skip" class="btn btn-ghost">🌧️ Rain-skip: {{'ON' if c.rain_skip_water else 'OFF'}}</a>
      <a href="/toggle_smart_curtain" class="btn btn-ghost">🪟 Smart Curtain: {{'ON' if c.smart_curtain else 'OFF'}}</a>
      <a href="/reset_stats" class="btn btn-ghost">↺ Reset Daily Stats</a>
      <a href="/reset_extremes" class="btn btn-ghost">↺ Reset Min/Max Records</a>
      <a href="/api/status" class="btn btn-ghost" target="_blank">📡 API Status JSON</a>
    </div>
  </div>
</div>

<!-- PROFILES -->
<div class="panel" style="margin-bottom:12px">
  <div class="panel-header"><span class="panel-title">🌱 Plant Profiles</span></div>
  <div class="profile-grid">
    {% for pid, prof in profiles.items() %}
    <a href="/set_profile/{{pid}}" class="profile-card {{'active' if c.plant_profile == pid else ''}}">
      <div class="pc-icon">{{prof.name.split()[0]}}</div>
      <div class="pc-name">{{prof.name.split(' ',1)[1] if ' ' in prof.name else prof.name}}</div>
    </a>
    {% endfor %}
  </div>
  <div style="font-size:.65rem;color:var(--muted);font-family:var(--mono);margin-top:6px">
    Active: <b style="color:var(--accent)">{{profile_name}}</b>
  </div>
</div>

<!-- SCHEDULE -->
<div class="two-col">
  <div class="panel">
    <div class="panel-header"><span class="panel-title">⏰ Watering Schedule</span></div>
    <table class="sched-table">
      <thead><tr><th>#</th><th>Time</th><th>Dur</th><th>Days</th><th>Status</th><th>Action</th></tr></thead>
      <tbody>
        {% for slot in schedule %}
        <tr>
          <td style="color:var(--muted)">{{loop.index}}</td>
          <td>{{"%02d:%02d"|format(slot.hour,slot.minute)}}</td>
          <td>{{slot.duration}}s</td><td>{{slot.days}}</td>
          <td class="{{'sched-on' if slot.enabled else 'sched-off'}}">{{'● ON' if slot.enabled else '○ OFF'}}</td>
          <td style="display:flex;gap:5px">
            <a href="/schedule_toggle/{{slot.id}}" class="btn btn-ghost" style="padding:3px 7px;font-size:.6rem">toggle</a>
            <a href="/schedule_del/{{slot.id}}" class="btn btn-ghost" style="padding:3px 7px;font-size:.6rem;color:var(--red)">del</a>
          </td>
        </tr>
        {% else %}<tr><td colspan="6" style="color:var(--muted);padding:9px 0">No schedules yet.</td></tr>{% endfor %}
      </tbody>
    </table>
    <form action="/schedule_add" method="post" style="margin-top:12px">
      <div class="ctrl-grid">
        <div class="field-row"><label class="field-label">Hour (0–23)</label><input type="number" name="hour" min="0" max="23" value="8"></div>
        <div class="field-row"><label class="field-label">Minute (0–59)</label><input type="number" name="minute" min="0" max="59" value="0"></div>
        <div class="field-row"><label class="field-label">Duration (s)</label><input type="number" name="duration" min="1" max="120" value="{{c.water_duration}}"></div>
        <div class="field-row"><label class="field-label">Days</label>
          <select name="days"><option value="all">Every Day</option><option value="weekday">Weekdays</option><option value="weekend">Weekend</option></select>
        </div>
      </div>
      <button type="submit" class="btn btn-b save-btn">＋ Add Schedule</button>
    </form>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🗄️ Database Info</span></div>
    <div style="font-family:var(--mono);font-size:.72rem;color:var(--muted);line-height:2.2">
      Sensor records: <b style="color:#fff">{{db_sc}}</b><br>
      Watering records: <b style="color:#fff">{{db_wc}}</b><br>
      Alert records: <b style="color:#fff">{{db_ac}}</b><br>
      Retention: <b style="color:#fff">7 days</b><br>
      Schedules active: <b style="color:#fff">{{schedule|selectattr('enabled')|list|length}} / {{schedule|length}}</b><br>
      Path: <span style="font-size:.6rem">{{db_path}}</span>
    </div>
  </div>
</div>
</section>

<!-- ══════════ TAB: PARTY MODE ══════════ -->
<section class="tab-section" id="tab-party">
<div class="party-control" style="margin-bottom:12px">
  <div class="panel-header">
    <span class="panel-title">🎉 Party Mode — Relay Alternator</span>
    <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted)">Make the relays do a little dance 💃</div>
  </div>

  <!-- VISUALIZER -->
  <div class="party-viz">
    <div class="relay-block">
      <div class="relay-lamp {{'a-on' if d.party_relay_a_state else 'a-off'}}" id="lampA">🚿</div>
      <div class="relay-label">Relay A (Pump)</div>
      <div class="relay-label" style="color:{{'#f472b6' if d.party_relay_a_state else 'var(--muted)'}}">{{ 'ON 🟢' if d.party_relay_a_state else 'OFF ⚫' }}</div>
    </div>
    <div class="relay-arrow">⇄</div>
    <div class="relay-block">
      <div class="relay-lamp {{'b-on' if d.party_relay_b_state else 'b-off'}}" id="lampB">🪟</div>
      <div class="relay-label">Relay B (Motor)</div>
      <div class="relay-label" style="color:{{'#38bdf8' if d.party_relay_b_state else 'var(--muted)'}}">{{ 'ON 🟢' if d.party_relay_b_state else 'OFF ⚫' }}</div>
    </div>
  </div>

  <div style="text-align:center;font-family:var(--mono);font-size:.75rem;color:var(--muted);margin-bottom:16px">
    Status: <b style="color:{{'#f472b6' if c.party_mode else 'var(--muted)'}}">{{ '🎉 RUNNING' if c.party_mode else '⏸ STOPPED' }}</b>
    &nbsp;|&nbsp; Cycles: <b style="color:#fff">{{d.party_cycles}}</b>
  </div>

  <!-- CONTROLS -->
  <div class="two-col">
    <div>
      <form action="/party_config" method="post">
        <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:1px">Relay A (Pump) Timing</div>
        <div class="ctrl-grid" style="margin-bottom:12px">
          <div class="field-row">
            <label class="field-label">ON duration (s)</label>
            <input type="number" name="a_on" value="{{c.party_relay_a_on}}" min="0.1" max="300" step="0.1">
          </div>
          <div class="field-row">
            <label class="field-label">OFF duration (s)</label>
            <input type="number" name="a_off" value="{{c.party_relay_a_off}}" min="0.1" max="300" step="0.1">
          </div>
        </div>
        <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:1px">Relay B (Motor) Timing</div>
        <div class="ctrl-grid" style="margin-bottom:12px">
          <div class="field-row">
            <label class="field-label">ON duration (s)</label>
            <input type="number" name="b_on" value="{{c.party_relay_b_on}}" min="0.1" max="300" step="0.1">
          </div>
          <div class="field-row">
            <label class="field-label">OFF duration (s)</label>
            <input type="number" name="b_off" value="{{c.party_relay_b_off}}" min="0.1" max="300" step="0.1">
          </div>
        </div>
        <div class="field-row" style="margin-bottom:12px">
          <label class="field-label">Mode</label>
          <select name="sync">
            <option value="1" {{'selected' if c.party_sync else ''}}>Synchronized (A then B, alternate)</option>
            <option value="0" {{'selected' if not c.party_sync else ''}}>Independent (A & B run own cycles)</option>
          </select>
        </div>
        <button type="submit" class="btn btn-p save-btn">💾 Save Party Config</button>
      </form>
    </div>
    <div style="display:flex;flex-direction:column;gap:10px;justify-content:flex-start;padding-top:20px">
      <a href="/party_start" class="btn btn-party" style="justify-content:center;padding:14px">🎉 START PARTY MODE</a>
      <a href="/party_stop" class="btn btn-r" style="justify-content:center">🛑 STOP PARTY</a>
      <div style="font-family:var(--mono);font-size:.64rem;color:var(--muted);line-height:1.7;margin-top:8px;padding:12px;background:rgba(255,255,255,.03);border-radius:10px;border:1px solid var(--border)">
        ⚠️ <b style="color:var(--amber)">Warning:</b> Party mode bypasses auto irrigation and directly controls hardware relays.<br><br>
        🚿 Relay A = Pump (GPIO22)<br>
        🪟 Relay B = Motor/Curtain (GPIO27)<br><br>
        Use short ON times for pumps to avoid overflow. Motor relay controls curtain travel — don't exceed safe travel time.
      </div>
    </div>
  </div>
</div>
</section>

<!-- ══════════ TAB: CHARTS ══════════ -->
<section class="tab-section" id="tab-charts">
<div class="two-col">
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">📈 Sensor History</span>
      <div class="tab-row">
        <button class="tab hist-tab on" onclick="loadChart(1,this)">1h</button>
        <button class="tab hist-tab" onclick="loadChart(6,this)">6h</button>
        <button class="tab hist-tab" onclick="loadChart(24,this)">24h</button>
        <button class="tab hist-tab" onclick="loadChart(72,this)">3d</button>
      </div>
    </div>
    <canvas id="histChart"></canvas>
  </div>
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">💨 VPD & Dew History</span>
      <div class="tab-row">
        <button class="tab vpd-tab on" onclick="loadVpdChart(1,this)">1h</button>
        <button class="tab vpd-tab" onclick="loadVpdChart(6,this)">6h</button>
        <button class="tab vpd-tab" onclick="loadVpdChart(24,this)">24h</button>
      </div>
    </div>
    <canvas id="vpdChart"></canvas>
  </div>
</div>
</section>

<!-- ══════════ TAB: LOGS ══════════ -->
<section class="tab-section" id="tab-logs">
<div class="two-col">
  <div class="panel">
    <div class="panel-header"><span class="panel-title">📋 Event Log</span></div>
    <ul class="evt-list">
      {% for ev in events %}
      <li class="evt-item">
        <div class="evt-dot {{ev.type}}"></div>
        <span class="evt-time">{{ev.time}}</span>
        <span class="evt-msg">{{ev.msg}}</span>
      </li>
      {% else %}<li style="font-size:.74rem;color:var(--muted);padding:10px 0">No events yet.</li>{% endfor %}
    </ul>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">🚿 Watering Log</span></div>
    <table class="wlog-table">
      <thead><tr><th>Time</th><th>Dur</th><th>Reason</th><th>Soil</th><th>Humidity</th></tr></thead>
      <tbody>
        {% for row in water_log %}
        <tr><td>{{row.time}}</td><td>{{row.duration}}s</td><td>{{row.reason}}</td><td>{{row.soil_before|round(1)}}%</td><td>{{row.hum_before|round(1)}}%</td></tr>
        {% else %}<tr><td colspan="5" style="color:var(--muted);padding:10px 0">No records yet.</td></tr>{% endfor %}
      </tbody>
    </table>
  </div>
</div>
<div class="panel">
  <div class="panel-header"><span class="panel-title">🔔 Alert History</span></div>
  <table class="wlog-table">
    <thead><tr><th>Time</th><th>Type</th><th>Value</th><th>Message</th></tr></thead>
    <tbody>
      {% for row in alert_log %}
      <tr><td>{{row.time}}</td><td style="color:var(--red)">{{row.alert_type}}</td><td>{{row.value|round(2)}}</td><td>{{row.message}}</td></tr>
      {% else %}<tr><td colspan="4" style="color:var(--muted);padding:10px 0">No alerts yet.</td></tr>{% endfor %}
    </tbody>
  </table>
</div>
</section>

<!-- ══════════ TAB: SETTINGS ══════════ -->
<section class="tab-section" id="tab-settings">
<div class="two-col">
  <div class="panel">
    <div class="panel-header"><span class="panel-title">⚙️ Thresholds & Timing</span></div>
    <form action="/update" method="post">
      <div class="ctrl-grid">
        <div class="field-row"><label class="field-label">Soil Min (%)</label><input type="number" name="th_s"  value="{{c.th_soil}}" step=".5"></div>
        <div class="field-row"><label class="field-label">Soil Critical (%)</label><input type="number" name="th_sc" value="{{c.th_soil_crit}}" step=".5"></div>
        <div class="field-row"><label class="field-label">Air Min (%)</label><input type="number" name="th_a"  value="{{c.th_air}}" step=".5"></div>
        <div class="field-row"><label class="field-label">Hum High (%)</label><input type="number" name="th_hh" value="{{c.th_hum_high}}" step=".5"></div>
        <div class="field-row"><label class="field-label">Light (%)</label><input type="number" name="th_l"  value="{{c.th_light}}" step=".5"></div>
        <div class="field-row"><label class="field-label">Rain (%)</label><input type="number" name="th_r"  value="{{c.th_rain}}" step=".5"></div>
        <div class="field-row"><label class="field-label">Temp High (°C)</label><input type="number" name="th_th" value="{{c.th_temp_high}}"></div>
        <div class="field-row"><label class="field-label">Temp Low (°C)</label><input type="number" name="th_tl" value="{{c.th_temp_low}}"></div>
        <div class="field-row"><label class="field-label">Pump Duration (s)</label><input type="number" name="w_dur" value="{{c.water_duration}}" min="1" max="120"></div>
        <div class="field-row"><label class="field-label">Motor Travel (s)</label><input type="number" name="m_dur" value="{{c.motor_duration}}" step=".5" min=".5"></div>
        <div class="field-row"><label class="field-label">Cooldown (s)</label><input type="number" name="cooldown" value="{{c.water_cooldown}}" min="30"></div>
        <div class="field-row"><label class="field-label">Night Start (h)</label><input type="number" name="ns" value="{{c.night_start}}" min="0" max="23"></div>
        <div class="field-row"><label class="field-label">Night End (h)</label><input type="number" name="ne" value="{{c.night_end}}" min="0" max="23"></div>
        <div class="field-row"><label class="field-label">Report Interval (s)</label><input type="number" name="ri" value="{{c.report_interval}}" min="60"></div>
      </div>
      <button type="submit" class="btn btn-g save-btn">💾 Save All Settings</button>
    </form>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="panel-title">📡 API Reference</span></div>
    <div style="font-family:var(--mono);font-size:.68rem;line-height:2;color:var(--muted)">
      <div><a href="/api/status" target="_blank" style="color:var(--accent)">/api/status</a> — full JSON snapshot</div>
      <div><a href="/api/history?hours=1" target="_blank" style="color:var(--accent)">/api/history?hours=N</a> — sensor history</div>
      <div><a href="/api/events" target="_blank" style="color:var(--accent)">/api/events</a> — event log</div>
      <div><a href="/api/alerts" target="_blank" style="color:var(--accent)">/api/alerts</a> — alert log</div>
      <div><a href="/api/watering" target="_blank" style="color:var(--accent)">/api/watering</a> — watering log</div>
      <div style="margin-top:8px;color:var(--muted);font-size:.6rem">All endpoints return JSON. Use ?limit=N for logs.</div>
    </div>
  </div>
</div>
</section>

<!-- ══════════ TAB: TELEGRAM ══════════ -->
<section class="tab-section" id="tab-telegram">
<div class="panel" style="margin-bottom:12px">
  <div class="panel-header"><span class="panel-title">🤖 Telegram Bot Commands Reference</span></div>
  <div style="font-family:var(--mono);font-size:.68rem;color:var(--muted);margin-bottom:12px">
    Bot Token: <b style="color:var(--accent)">{{tg_token_masked}}</b> | Admin IDs: <b style="color:#fff">{{tg_admin_ids}}</b>
  </div>
  <table class="cmd-table">
    <thead><tr><th>Command</th><th>Description</th><th>Example</th></tr></thead>
    <tbody>
      <tr class="cmd-cat"><td colspan="3">📊 Information</td></tr>
      <tr><td>/start</td><td>Main menu with inline keyboard</td><td>/start</td></tr>
      <tr><td>/help</td><td>Full command list</td><td>/help</td></tr>
      <tr><td>/status</td><td>Live sensor readings snapshot</td><td>/status</td></tr>
      <tr><td>/health</td><td>Plant health score + analysis</td><td>/health</td></tr>
      <tr><td>/history</td><td>24h summary (min/max/avg)</td><td>/history</td></tr>
      <tr><td>/stats</td><td>Today's counters (water/motor/alerts)</td><td>/stats</td></tr>
      <tr><td>/events</td><td>Recent event log</td><td>/events</td></tr>
      <tr><td>/wlog</td><td>Watering history with soil/hum data</td><td>/wlog</td></tr>
      <tr><td>/alerts</td><td>Alert history</td><td>/alerts</td></tr>
      <tr><td>/sysinfo</td><td>System info, uptime, DB stats</td><td>/sysinfo</td></tr>

      <tr class="cmd-cat"><td colspan="3">🎮 Control</td></tr>
      <tr><td>/water [s]</td><td>Manual pump run (optional duration)</td><td>/water 10</td></tr>
      <tr><td>/pump_on</td><td>Pump manual hold ON</td><td>/pump_on</td></tr>
      <tr><td>/pump_off</td><td>Pump manual OFF / stop hold</td><td>/pump_off</td></tr>
      <tr><td>/motor [pos]</td><td>Toggle or set curtain OPEN/CLOSED</td><td>/motor OPEN</td></tr>
      <tr><td>/auto_on</td><td>Enable auto irrigation mode</td><td>/auto_on</td></tr>
      <tr><td>/auto_off</td><td>Disable auto mode (manual)</td><td>/auto_off</td></tr>
      <tr><td>/alert_on</td><td>Enable Telegram alerts</td><td>/alert_on</td></tr>
      <tr><td>/alert_off</td><td>Disable Telegram alerts</td><td>/alert_off</td></tr>

      <tr class="cmd-cat"><td colspan="3">🎉 Party Mode</td></tr>
      <tr><td>/party_on</td><td>Start relay party mode</td><td>/party_on</td></tr>
      <tr><td>/party_off</td><td>Stop party mode</td><td>/party_off</td></tr>
      <tr><td>/party_set A_on A_off B_on B_off</td><td>Set relay timings (seconds)</td><td>/party_set 2 3 2 3</td></tr>
      <tr><td>/party_sync [0/1]</td><td>Sync mode: 1=alternate, 0=independent</td><td>/party_sync 1</td></tr>

      <tr class="cmd-cat"><td colspan="3">⏰ Schedule</td></tr>
      <tr><td>/schedule</td><td>View all watering schedules</td><td>/schedule</td></tr>
      <tr><td>/add_sched HH MM dur days</td><td>Add schedule (days: all/weekday/weekend)</td><td>/add_sched 08 00 5 all</td></tr>
      <tr><td>/del_sched id</td><td>Delete schedule by ID</td><td>/del_sched 1234</td></tr>

      <tr class="cmd-cat"><td colspan="3">🌱 Profiles</td></tr>
      <tr><td>/profiles</td><td>List all plant profiles + details</td><td>/profiles</td></tr>
      <tr><td>/profile name</td><td>Apply a plant profile</td><td>/profile tomato</td></tr>

      <tr class="cmd-cat"><td colspan="3">⚙️ Thresholds</td></tr>
      <tr><td>/th_soil val</td><td>Set soil moisture threshold %</td><td>/th_soil 35</td></tr>
      <tr><td>/th_air val</td><td>Set air humidity threshold %</td><td>/th_air 45</td></tr>
      <tr><td>/th_light val</td><td>Set light threshold %</td><td>/th_light 80</td></tr>
      <tr><td>/th_rain val</td><td>Set rain threshold %</td><td>/th_rain 25</td></tr>
      <tr><td>/th_temp high low</td><td>Set temp alert range °C</td><td>/th_temp 35 10</td></tr>
      <tr><td>/pump_time s</td><td>Set pump duration seconds</td><td>/pump_time 5</td></tr>
      <tr><td>/motor_time s</td><td>Set motor travel seconds</td><td>/motor_time 3.5</td></tr>
      <tr><td>/cooldown s</td><td>Set watering cooldown seconds</td><td>/cooldown 300</td></tr>
      <tr><td>/reset</td><td>Reset daily counters</td><td>/reset</td></tr>
    </tbody>
  </table>
</div>
</section>

<div class="footer">
  GreenHouse OS v{{version}} ·
  <span id="footerStatus">auto-refresh in <span id="cd">5</span>s</span>
  · <a href="/api/status" style="color:var(--muted)">API</a>
</div>
</div>

<script>
// ── TAB NAVIGATION ──
function showTab(id, el){
  document.querySelectorAll('.tab-section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  if(el) el.classList.add('active');
  if(id==='charts'){
    const htabs=document.querySelectorAll('#tab-charts .hist-tab');
    const vtabs=document.querySelectorAll('#tab-charts .vpd-tab');
    loadChart(1,htabs[0]);
    loadVpdChart(1,vtabs[0]);
  }
}

// ── CURTAIN VIS ──
(function(){
  const vis=document.getElementById('curtainVis');
  if(!vis) return;
  const closed='{{c.motor_pos}}'==='CLOSED';
  [40,46,50,46,40,34,28,34].forEach(h=>{
    const s=document.createElement('div');
    s.className='curtain-slat';
    s.style.height=(closed?h:h*0.22)+'px';
    s.style.opacity=closed?'0.85':'0.22';
    vis.appendChild(s);
  });
})();

// ── CHARTS ──
let myChart=null, vpdChartObj=null;

async function loadChart(hours, btn){
  document.querySelectorAll('#tab-charts .hist-tab').forEach(b=>b.classList.remove('on'));
  if(btn) btn.classList.add('on');
  try{
    const res=await fetch('/api/history?hours='+hours);
    const rows=await res.json();
    const labels=rows.map(r=>{const d=new Date(r[0]*1000);return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')});
    if(myChart) myChart.destroy();
    const ctx=document.getElementById('histChart').getContext('2d');
    myChart=new Chart(ctx,{type:'line',data:{labels,datasets:[
      {label:'Temp°C', data:rows.map(r=>r[1]!=null?r[1]:null), borderColor:'#f87171',borderWidth:1.5,pointRadius:0,tension:.4,fill:false,spanGaps:true},
      {label:'Humid%', data:rows.map(r=>r[2]!=null?r[2]:null), borderColor:'#38bdf8',borderWidth:1.5,pointRadius:0,tension:.4,fill:false,spanGaps:true},
      {label:'Soil%',  data:rows.map(r=>r[3]!=null?r[3]:null), borderColor:'#34d399',borderWidth:1.5,pointRadius:0,tension:.4,fill:false,spanGaps:true},
      {label:'Light%', data:rows.map(r=>r[4]!=null?r[4]:null), borderColor:'#fbbf24',borderWidth:1.2,pointRadius:0,tension:.4,fill:false,spanGaps:true},
      {label:'Rain%',  data:rows.map(r=>r[5]!=null?r[5]:null), borderColor:'#a78bfa',borderWidth:1.2,pointRadius:0,tension:.4,fill:false,spanGaps:true},
    ]},options:{responsive:true,maintainAspectRatio:true,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{color:'#3d6457',font:{size:9,family:"'JetBrains Mono'"}}}},
      scales:{x:{ticks:{color:'#3d6457',font:{size:8},maxTicksLimit:10},grid:{color:'rgba(255,255,255,0.02)'}},
              y:{ticks:{color:'#3d6457',font:{size:8}},grid:{color:'rgba(255,255,255,0.04)'}}}}});
  } catch(e){ console.error('Chart load failed:',e); }
}

async function loadVpdChart(hours, btn){
  document.querySelectorAll('#tab-charts .vpd-tab').forEach(b=>b.classList.remove('on'));
  if(btn) btn.classList.add('on');
  try{
    const res=await fetch('/api/history?hours='+hours);
    const rows=await res.json();
    const labels=rows.map(r=>{const d=new Date(r[0]*1000);return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')});
    if(vpdChartObj) vpdChartObj.destroy();
    const ctx=document.getElementById('vpdChart').getContext('2d');
    vpdChartObj=new Chart(ctx,{type:'line',data:{labels,datasets:[
      {label:'VPD kPa', data:rows.map(r=>r[6]!=null?r[6]:null), borderColor:'#2dd4bf',borderWidth:1.5,pointRadius:0,tension:.4,fill:false,spanGaps:true},
      {label:'DewPt°C', data:rows.map(r=>r[7]!=null?r[7]:null), borderColor:'#a78bfa',borderWidth:1.5,pointRadius:0,tension:.4,fill:false,spanGaps:true},
    ]},options:{responsive:true,maintainAspectRatio:true,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{color:'#3d6457',font:{size:9,family:"'JetBrains Mono'"}}}},
      scales:{x:{ticks:{color:'#3d6457',font:{size:8},maxTicksLimit:10},grid:{color:'rgba(255,255,255,0.02)'}},
              y:{ticks:{color:'#3d6457',font:{size:8}},grid:{color:'rgba(255,255,255,0.04)'}}}}});
  } catch(e){ console.error('VPD chart load failed:',e); }
}

// ── AUTO REFRESH ──
(function(){
  // Restore saved prefs from localStorage
  const savedPaused   = localStorage.getItem('gh_paused')   === '1';
  const savedInterval = parseInt(localStorage.getItem('gh_interval') || '5', 10);

  let paused   = savedPaused;
  let interval = savedInterval;
  let counter  = interval;

  // Sync interval <select> to saved value
  const sel = document.getElementById('refreshInterval');
  if(sel){
    [...sel.options].forEach(o => { if(parseInt(o.value)===interval) o.selected=true; });
  }

  function updateBadge(){
    const pulse    = document.getElementById('refreshPulse');
    const label    = document.getElementById('refreshLabel');
    const countdown= document.getElementById('refreshCountdown');
    const footer   = document.getElementById('footerStatus');
    const cdEl     = document.getElementById('cd');
    if(paused){
      if(pulse)    { pulse.style.background='#6b7280'; pulse.style.boxShadow='none'; }
      if(label)    label.textContent = 'PAUSED';
      if(countdown)countdown.textContent = '';
      if(footer)   footer.textContent = '⏸ refresh paused';
      if(cdEl)     cdEl.textContent = '—';
    } else {
      if(pulse)    { pulse.style.background=''; pulse.style.boxShadow=''; }
      if(label)    label.textContent = 'LIVE';
      if(countdown)countdown.textContent = counter+'s';
      if(footer)   footer.innerHTML = 'auto-refresh in <span id="cd">'+counter+'</span>s';
      if(cdEl)     cdEl.textContent = counter;
    }
  }

  window.toggleRefresh = function(){
    paused = !paused;
    counter = interval;
    localStorage.setItem('gh_paused', paused ? '1' : '0');
    updateBadge();
  };

  window.changeInterval = function(val){
    interval = parseInt(val, 10);
    counter  = interval;
    localStorage.setItem('gh_interval', String(interval));
    updateBadge();
  };

  updateBadge();

  setInterval(()=>{
    if(paused) return;
    counter--;
    // Update countdown text directly without re-calling updateBadge every second
    const countdown = document.getElementById('refreshCountdown');
    const cdEl      = document.getElementById('cd');
    if(countdown) countdown.textContent = counter+'s';
    if(cdEl)      cdEl.textContent = counter;
    // Also patch footer span if it's there
    const footer = document.getElementById('footerStatus');
    if(footer && !paused) footer.innerHTML = 'auto-refresh in <span id="cd">'+counter+'</span>s';
    if(counter <= 0) location.reload();
  }, 1000);
})();
</script>
</body>
</html>"""

# ================== 10. FLASK ROUTES ==================

@app.route('/')
def index():
    hs = health_score(); circ = 2*math.pi*25; hd = round(circ*hs/100, 2)
    hc = "#34d399" if hs>=65 else ("#fbbf24" if hs>=45 else "#f87171")
    evts = [{"time":time.strftime('%H:%M',time.localtime(ts)),"type":et,"msg":msg}
            for ts,et,msg in get_events(20)]
    wlog = [{"time":fmt_ts(ts),"duration":dur,"reason":rsn,"soil_before":sb,"hum_before":hb}
            for ts,dur,rsn,sb,hb in get_watering_history(12)]
    alog = [{"time":fmt_ts(ts),"alert_type":at,"value":v,"message":msg}
            for ts,at,v,msg in get_alert_history(10)]
    sc,wc,ac = get_db_stats()
    prof = PLANT_PROFILES.get(cfg["plant_profile"], PLANT_PROFILES["custom"])
    masked = TELEGRAM_TOKEN[:8]+"***"+TELEGRAM_TOKEN[-4:]
    return render_template_string(HTML,
        d=data, c=cfg, version=VERSION, uptime=uptime_str(),
        time_now=time.strftime('%H:%M:%S'), events=evts, water_log=wlog,
        alert_log=alog, schedule=watering_schedule, profiles=PLANT_PROFILES,
        profile_name=prof["name"], night_mode=is_night(),
        cooldown_remain=cooldown_remain(), health_score=hs,
        health_label=health_label(hs), health_color=hc, health_dash=hd,
        db_sc=sc, db_wc=wc, db_ac=ac, db_path=DB_PATH,
        tg_token_masked=masked, tg_admin_ids=str(ADMIN_IDS),
        virtual_mode=VIRTUAL)

@app.route('/api/history')
def api_history(): return jsonify(get_history(int(request.args.get('hours',24))))

@app.route('/api/status')
def api_status():
    return jsonify({**data,**{k:v for k,v in cfg.items() if k not in ('boot_time','last_watered_ts')},
        "health_score":health_score(),"uptime":uptime_str(),
        "night_mode":is_night(),"cooldown_remain":cooldown_remain(),
        "virtual_mode":VIRTUAL})

@app.route('/api/events')
def api_events():
    limit=int(request.args.get('limit',20))
    return jsonify([{"ts":ts,"type":et,"msg":msg} for ts,et,msg in get_events(limit)])

@app.route('/api/alerts')
def api_alerts():
    limit=int(request.args.get('limit',10))
    return jsonify([{"ts":ts,"type":at,"value":v,"msg":msg} for ts,at,v,msg in get_alert_history(limit)])

@app.route('/api/watering')
def api_watering():
    limit=int(request.args.get('limit',12))
    return jsonify([{"ts":ts,"duration":d,"reason":r,"soil_before":sb,"hum_before":hb}
                    for ts,d,r,sb,hb in get_watering_history(limit)])

@app.route('/water')
def web_water():
    threading.Thread(target=run_pump_action,args=("web",),daemon=True).start(); return redirect(url_for('index'))

@app.route('/pump_on')
def web_pump_on():
    threading.Thread(target=pump_manual_on,daemon=True).start(); return redirect(url_for('index'))

@app.route('/pump_off')
def web_pump_off():
    pump_manual_off(); return redirect(url_for('index'))

@app.route('/emergency_stop')
def emergency_stop():
    cfg["party_mode"]=False; cfg["pump_manual_on"]=False
    pump_relay.value=False; motor_relay.value=False
    cfg["pump"]="OFF"; cfg["motor_pos"]="OPEN"
    data["party_relay_a_state"]=False; data["party_relay_b_state"]=False
    log_event("system","EMERGENCY STOP activated")
    return redirect(url_for('index'))

@app.route('/motor')
def web_motor():
    t="CLOSED" if cfg["motor_pos"]=="OPEN" else "OPEN"
    threading.Thread(target=run_motor,args=(t,"web"),daemon=True).start(); return redirect(url_for('index'))

@app.route('/motor_open')
def web_motor_open():
    threading.Thread(target=run_motor,args=("OPEN","web"),daemon=True).start(); return redirect(url_for('index'))

@app.route('/motor_close')
def web_motor_close():
    threading.Thread(target=run_motor,args=("CLOSED","web"),daemon=True).start(); return redirect(url_for('index'))

@app.route('/toggle_auto')
def toggle_auto(): cfg["auto_mode"]=not cfg["auto_mode"]; log_event("system",f"Mode→{'Auto' if cfg['auto_mode'] else 'Manual'}"); return redirect(url_for('index'))

@app.route('/toggle_alert')
def toggle_alert(): cfg["alert_enabled"]=not cfg["alert_enabled"]; return redirect(url_for('index'))

@app.route('/toggle_rain_skip')
def toggle_rain_skip(): cfg["rain_skip_water"]=not cfg["rain_skip_water"]; return redirect(url_for('index'))

@app.route('/toggle_smart_curtain')
def toggle_smart_curtain(): cfg["smart_curtain"]=not cfg["smart_curtain"]; return redirect(url_for('index'))

@app.route('/set_profile/<pid>')
def set_profile(pid):
    if pid in PLANT_PROFILES:
        p=PLANT_PROFILES[pid]; cfg.update({"plant_profile":pid,"th_soil":p["th_soil"],"th_air":p["th_air"],
            "th_temp_high":p["th_temp_high"],"th_temp_low":p["th_temp_low"],"water_duration":p["water_duration"]})
        log_event("system",f"Profile→{p['name']}")
    return redirect(url_for('index'))

@app.route('/party_start')
def web_party_start():
    threading.Thread(target=start_party,daemon=True).start(); return redirect(url_for('index'))

@app.route('/party_stop')
def web_party_stop():
    stop_party(); return redirect(url_for('index'))

@app.route('/party_config', methods=['POST'])
def web_party_config():
    f=request.form
    try: cfg["party_relay_a_on"]=float(f.get("a_on",3))
    except: pass
    try: cfg["party_relay_a_off"]=float(f.get("a_off",3))
    except: pass
    try: cfg["party_relay_b_on"]=float(f.get("b_on",3))
    except: pass
    try: cfg["party_relay_b_off"]=float(f.get("b_off",3))
    except: pass
    cfg["party_sync"] = f.get("sync","1") == "1"
    log_event("party",f"Config updated: A:{cfg['party_relay_a_on']}/{cfg['party_relay_a_off']} B:{cfg['party_relay_b_on']}/{cfg['party_relay_b_off']} sync:{cfg['party_sync']}")
    return redirect(url_for('index'))

@app.route('/schedule_add', methods=['POST'])
def schedule_add():
    watering_schedule.append({"id":int(time.time()),"hour":int(request.form.get("hour",8)),
        "minute":int(request.form.get("minute",0)),"duration":int(request.form.get("duration",cfg["water_duration"])),
        "days":request.form.get("days","all"),"enabled":True})
    log_event("system","Schedule added"); return redirect(url_for('index'))

@app.route('/schedule_toggle/<int:sid>')
def schedule_toggle(sid):
    for s in watering_schedule:
        if s["id"]==sid: s["enabled"]=not s["enabled"]
    return redirect(url_for('index'))

@app.route('/schedule_del/<int:sid>')
def schedule_del(sid):
    watering_schedule[:]=[s for s in watering_schedule if s["id"]!=sid]; return redirect(url_for('index'))

@app.route('/reset_stats')
def reset_stats():
    data["water_count"]=data["motor_count"]=data["alert_count"]=0
    log_event("system","Daily stats reset"); return redirect(url_for('index'))

@app.route('/reset_extremes')
def reset_extremes():
    data.update({"max_t":None,"min_t":None,"max_h":None,"min_h":None,
                 "max_soil":None,"min_soil":None,"max_light":0.0})
    log_event("system","Min/Max records reset")
    return redirect(url_for('index'))

@app.route('/test_pump')
def test_pump():
    threading.Thread(target=run_pump_action,args=("test",1),daemon=True).start(); return redirect(url_for('index'))

@app.route('/test_motor')
def test_motor():
    t="CLOSED" if cfg["motor_pos"]=="OPEN" else "OPEN"
    threading.Thread(target=run_motor,args=(t,"test"),daemon=True).start(); return redirect(url_for('index'))

@app.route('/update', methods=['POST'])
def update():
    f=request.form
    for k,fk,cast in [("th_soil","th_s",float),("th_soil_crit","th_sc",float),("th_air","th_a",float),
        ("th_hum_high","th_hh",float),("th_light","th_l",float),("th_rain","th_r",float),
        ("th_temp_high","th_th",float),("th_temp_low","th_tl",float),
        ("water_duration","w_dur",int),("motor_duration","m_dur",float),
        ("water_cooldown","cooldown",int),("night_start","ns",int),
        ("night_end","ne",int),("report_interval","ri",int)]:
        if f.get(fk):
            try: cfg[k]=cast(f[fk])
            except: pass
    log_event("system","Settings updated via web"); return redirect(url_for('index'))

# ================== 11. TELEGRAM BOT ==================

import functools

if TELEGRAM_OK:
    def admin_only(func):
        """Decorator: reject non-admin users with a clear message."""
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            uid = (update.effective_user.id if update.effective_user else None)
            if uid not in ADMIN_IDS:
                target = update.message or (update.callback_query.message if update.callback_query else None)
                if target:
                    await target.reply_text("⛔ Unauthorized. This bot is private.")
                return
            return await func(update, context)
        return wrapper
else:
    # Stub so @admin_only on function defs below doesn't NameError at parse time
    def admin_only(func): return func

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status",         callback_data="status"),
         InlineKeyboardButton("🌱 Health",          callback_data="health")],
        [InlineKeyboardButton("📈 History",         callback_data="history"),
         InlineKeyboardButton("📋 Events",          callback_data="events")],
        [InlineKeyboardButton("🚿 Watering Log",    callback_data="wlog"),
         InlineKeyboardButton("📊 Stats",           callback_data="stats")],
        [InlineKeyboardButton("🚿 Water Now",       callback_data="water"),
         InlineKeyboardButton("🔒 Pump ON",         callback_data="pump_on")],
        [InlineKeyboardButton("⏹ Pump OFF",        callback_data="pump_off"),
         InlineKeyboardButton("🪟 Curtain",         callback_data="motor")],
        [InlineKeyboardButton("🤖 Toggle Mode",     callback_data="toggle_auto"),
         InlineKeyboardButton("🔔 Toggle Alerts",   callback_data="toggle_alert")],
        [InlineKeyboardButton("🎉 Party ON",        callback_data="party_on"),
         InlineKeyboardButton("🛑 Party OFF",       callback_data="party_off")],
        [InlineKeyboardButton("🌱 Profiles",        callback_data="profiles"),
         InlineKeyboardButton("⏰ Schedule",        callback_data="schedule")],
        [InlineKeyboardButton("⚙️ Settings",        callback_data="settings"),
         InlineKeyboardButton("🖥 Sys Info",        callback_data="sysinfo")],
        [InlineKeyboardButton("🛑 Emergency Stop",  callback_data="emergency"),
         InlineKeyboardButton("❓ Help",            callback_data="help")],
    ])

def build_status():
    d2,c2=data,cfg; cool=cooldown_remain(); hs=health_score()
    def _r(v): return f"{v:.1f}" if v is not None else "--"
    virt = " 🟡VIRTUAL" if VIRTUAL else ""
    return (
        f"📊 *Live Status* — {time.strftime('%H:%M:%S')}{virt}\n"
        f"{'🌙 Night' if is_night() else '☀️ Day'} | Health: *{hs}%* {health_label(hs)}\n\n"
        f"🌡️ Temp: *{d2['temp']:.1f}°C* {d2['temp_trend']} (↕{_r(d2['min_t'])}–{_r(d2['max_t'])})\n"
        f"💧 Humidity: *{d2['hum']:.1f}%* {d2['hum_trend']} (abs:{d2['abs_hum']}g/m³)\n"
        f"🪴 Soil: *{d2['soil']:.1f}%* {d2['soil_trend']} (EC~{d2['soil_ec']}dS/m)\n"
        f"☀️ Light: *{d2['light']:.1f}%* (peak {_r(d2['max_light'])}%)\n"
        f"🌧️ Rain: *{d2['rain']:.1f}%* {'🌧️ RAINING' if d2['is_raining'] else '☀️ Dry'}\n"
        f"💨 VPD: *{d2['vpd']} kPa* | Dew: {d2['dew_point']}°C | HI: {d2['heat_index']:.1f}°C\n"
        f"😊 Comfort: {d2['comfort']}\n\n"
        f"🪟 Curtain: {c2['motor_pos']} | Moves: {d2['motor_count']}\n"
        f"🚿 Pump: {c2['pump']}{'🔒' if c2['pump_manual_on'] else ''} | Last: {c2['last_watered']} | ⏳ {cool}s\n"
        f"🎉 Party: {'ON 🎉' if c2['party_mode'] else 'OFF'} | Cycles: {d2['party_cycles']}\n"
        f"🤖 Mode: {'Auto ✅' if c2['auto_mode'] else 'Manual 🔴'}\n"
        f"📋 {c2['last_event']}"
    )

async def register_commands(bot):
    await bot.set_my_commands([
        BotCommand("start",      "🌿 Main menu"),
        BotCommand("help",       "❓ Full command list"),
        BotCommand("status",     "📊 Live sensor readings"),
        BotCommand("health",     "🌱 Plant health score"),
        BotCommand("history",    "📈 24h summary"),
        BotCommand("stats",      "📋 Today's statistics"),
        BotCommand("events",     "📋 Event log"),
        BotCommand("wlog",       "🚿 Watering history"),
        BotCommand("alerts",     "🔔 Alert history"),
        BotCommand("sysinfo",    "🖥 System info"),
        BotCommand("water",      "🚿 Timed pump run [secs]"),
        BotCommand("pump_on",    "🔒 Pump manual hold ON"),
        BotCommand("pump_off",   "⏹ Pump manual OFF"),
        BotCommand("motor",      "🪟 Toggle curtain [OPEN/CLOSED]"),
        BotCommand("auto_on",    "🤖 Enable auto mode"),
        BotCommand("auto_off",   "🔴 Disable auto mode"),
        BotCommand("alert_on",   "🔔 Enable alerts"),
        BotCommand("alert_off",  "🔕 Disable alerts"),
        BotCommand("party_on",   "🎉 Start party mode"),
        BotCommand("party_off",  "🛑 Stop party mode"),
        BotCommand("party_set",  "⚙️ Set party timings A_on A_off B_on B_off"),
        BotCommand("party_sync", "🔄 Party sync mode 0/1"),
        BotCommand("schedule",   "⏰ View schedule"),
        BotCommand("add_sched",  "⏰ Add HH MM dur days"),
        BotCommand("del_sched",  "🗑 Delete by id"),
        BotCommand("profiles",   "🌱 List plant profiles"),
        BotCommand("profile",    "🌱 Apply profile name"),
        BotCommand("th_soil",    "⚙️ Soil threshold %"),
        BotCommand("th_air",     "⚙️ Air humidity %"),
        BotCommand("th_light",   "⚙️ Light threshold %"),
        BotCommand("th_rain",    "⚙️ Rain threshold %"),
        BotCommand("th_temp",    "⚙️ Temp high low"),
        BotCommand("pump_time",  "⚙️ Pump duration s"),
        BotCommand("motor_time", "⚙️ Motor travel s"),
        BotCommand("cooldown",   "⚙️ Watering cooldown s"),
        BotCommand("reset",      "↺ Reset daily stats"),
        BotCommand("emergency",  "🛑 Emergency stop all"),
    ])

@admin_only
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await register_commands(c.bot)
    await u.message.reply_text(
        f"🌿 *GreenHouse OS v{VERSION}*\n\n"
        "Welcome! All commands are in the menu (tap ☰ or /).\n"
        "New in v4: Manual pump ON/OFF 🔒, Party Mode 🎉, VPD/Dew metrics 💨\n\n"
        "Use buttons below or type any command:",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    await target.reply_text(
        f"*GreenHouse OS v{VERSION} — Commands*\n\n"
        "*📊 Info:* /status /health /history /stats /events /wlog /alerts /sysinfo\n"
        "*🚿 Pump:* /water [s] | /pump\\_on | /pump\\_off\n"
        "*🪟 Curtain:* /motor [OPEN/CLOSED]\n"
        "*🎉 Party:* /party\\_on | /party\\_off | /party\\_set A\\_on A\\_off B\\_on B\\_off | /party\\_sync 0/1\n"
        "*🤖 Mode:* /auto\\_on | /auto\\_off | /alert\\_on | /alert\\_off\n"
        "*⏰ Schedule:* /schedule | /add\\_sched HH MM dur days | /del\\_sched id\n"
        "*🌱 Profiles:* /profiles | /profile name\n"
        "*⚙️ Thresholds:* /th\\_soil /th\\_air /th\\_light /th\\_rain /th\\_temp /pump\\_time /motor\\_time /cooldown\n"
        "*🔧 Maintenance:* /reset | /emergency\n",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    await target.reply_text(build_status(), parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    hs=health_score(); d2=data; c2=cfg
    vpd_note=("🌱 Seedling zone" if d2['vpd']<0.8 else ("✅ Optimal" if d2['vpd']<1.6 else "⚠️ High stress"))
    await target.reply_text(
        f"🌱 *Plant Health Report*\n\n"
        f"Overall: *{hs}%* {health_label(hs)}\n\n"
        f"🌡️ Temp: {d2['temp']:.1f}°C → {'✅' if c2['th_temp_low']<d2['temp']<c2['th_temp_high'] else '❌'}\n"
        f"💧 Humidity: {d2['hum']:.1f}% → {'✅' if d2['hum']>c2['th_air'] else '❌'}\n"
        f"🪴 Soil: {d2['soil']:.1f}% → {'✅' if d2['soil']>c2['th_soil'] else ('‼️' if d2['soil']<c2['th_soil_crit'] else '⚠️')}\n"
        f"💨 VPD: {d2['vpd']} kPa → {vpd_note}\n"
        f"🌡️ Dew Point: {d2['dew_point']}°C | Heat Index: {d2['heat_index']:.1f}°C\n"
        f"😊 Comfort: {d2['comfort']}\n"
        f"🌿 Profile: {PLANT_PROFILES.get(c2['plant_profile'],{}).get('name','?')}",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message; d2=data; c2=cfg
    await target.reply_text(
        f"📊 *Today's Statistics*\n\n"
        f"🚿 Waterings: {d2['water_count']}\n"
        f"🪟 Curtain moves: {d2['motor_count']}\n"
        f"🔔 Alerts: {d2['alert_count']}\n"
        f"🎉 Party cycles: {d2['party_cycles']}\n"
        f"🌡️ Temp: {d2['min_t']:.1f}–{d2['max_t']:.1f}°C\n"
        f"💧 Hum: {d2['min_h']:.1f}–{d2['max_h']:.1f}%\n"
        f"🪴 Soil: {d2['min_soil']:.1f}–{d2['max_soil']:.1f}%\n"
        f"☀️ Peak light: {d2['max_light']:.1f}%\n"
        f"⏱ Uptime: {uptime_str()}\n"
        f"🔌 Sensor errors: {d2['sensor_errors']}",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_sysinfo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    sc,wc,ac=get_db_stats()
    await target.reply_text(
        f"🖥 *System Info*\n\n"
        f"Version: v{VERSION}\n"
        f"Uptime: {uptime_str()}\n"
        f"DB path: `{DB_PATH}`\n"
        f"Sensor records: {sc}\n"
        f"Watering records: {wc}\n"
        f"Alert records: {ac}\n"
        f"Schedules: {len(watering_schedule)}\n"
        f"Sensor OK: {'✅' if data['last_read_ok'] else '❌'}\n"
        f"Party mode: {'🎉 ON' if cfg['party_mode'] else 'OFF'}\n"
        f"Pump manual hold: {'🔒 ON' if cfg['pump_manual_on'] else 'OFF'}",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_water(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        dur=int(c.args[0]) if c.args else None
        threading.Thread(target=run_pump_action,args=("telegram",dur),daemon=True).start()
        await u.message.reply_text(f"🚿 Pump started ({dur or cfg['water_duration']}s)",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /water [seconds]")

@admin_only
async def cmd_pump_on(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    threading.Thread(target=pump_manual_on,daemon=True).start()
    await target.reply_text("🔒 Pump manual ON activated. Use /pump\\_off to stop.",parse_mode='Markdown',reply_markup=main_kb())

@admin_only
async def cmd_pump_off(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    pump_manual_off()
    await target.reply_text("⏹ Pump manual OFF. Ready.",reply_markup=main_kb())

@admin_only
async def cmd_motor(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        target=u.message or u.callback_query.message
        if c.args:
            t=c.args[0].upper()
            if t not in ("OPEN","CLOSED"): await target.reply_text("❌ /motor OPEN or CLOSED"); return
        else:
            t="CLOSED" if cfg["motor_pos"]=="OPEN" else "OPEN"
        threading.Thread(target=run_motor,args=(t,"telegram"),daemon=True).start()
        await target.reply_text(f"🪟 Curtain → {t}",reply_markup=main_kb())
    except Exception as e: await u.message.reply_text(f"❌ Error: {e}")

@admin_only
async def cmd_party_on(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    threading.Thread(target=start_party,daemon=True).start()
    await target.reply_text(
        f"🎉 Party mode started!\n"
        f"A: {cfg['party_relay_a_on']}s ON / {cfg['party_relay_a_off']}s OFF\n"
        f"B: {cfg['party_relay_b_on']}s ON / {cfg['party_relay_b_off']}s OFF\n"
        f"Sync: {'✅' if cfg['party_sync'] else '❌'}",
        reply_markup=main_kb())

@admin_only
async def cmd_party_off(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    stop_party()
    await target.reply_text(f"🛑 Party mode stopped. Cycles: {data['party_cycles']}",reply_markup=main_kb())

@admin_only
async def cmd_party_set(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        a_on,a_off,b_on,b_off=[float(x) for x in c.args[:4]]
        cfg["party_relay_a_on"]=a_on; cfg["party_relay_a_off"]=a_off
        cfg["party_relay_b_on"]=b_on; cfg["party_relay_b_off"]=b_off
        log_event("party",f"Timing set via Telegram: A:{a_on}/{a_off} B:{b_on}/{b_off}")
        await u.message.reply_text(f"✅ Party timing set!\nA: {a_on}s/{a_off}s | B: {b_on}s/{b_off}s",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /party_set A_on A_off B_on B_off\nExample: /party_set 2 3 2 3")

@admin_only
async def cmd_party_sync(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        cfg["party_sync"]=bool(int(c.args[0]))
        await u.message.reply_text(f"✅ Party sync: {'ON (A then B alternate)' if cfg['party_sync'] else 'OFF (independent)'}",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /party_sync 1 (on) or 0 (off)")

@admin_only
async def cmd_emergency(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    cfg["party_mode"]=False; cfg["pump_manual_on"]=False
    pump_relay.value=False; motor_relay.value=False
    cfg["pump"]="OFF"; data["party_relay_a_state"]=False; data["party_relay_b_state"]=False
    log_event("system","EMERGENCY STOP via Telegram")
    await target.reply_text("🛑 *EMERGENCY STOP* — All relays OFF.",parse_mode='Markdown',reply_markup=main_kb())

@admin_only
async def cmd_history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message; d2=data
    await target.reply_text(
        f"📈 *24h Summary*\n\n"
        f"🌡️ Temp: {d2['min_t']:.1f}–{d2['max_t']:.1f}°C (now {d2['temp']:.1f}°C)\n"
        f"💧 Hum: {d2['min_h']:.1f}–{d2['max_h']:.1f}% (now {d2['hum']:.1f}%)\n"
        f"🪴 Soil: {d2['min_soil']:.1f}–{d2['max_soil']:.1f}% (now {d2['soil']:.1f}%)\n"
        f"☀️ Peak light: {d2['max_light']:.1f}%\n"
        f"💨 VPD: {d2['vpd']} kPa | Dew: {d2['dew_point']}°C",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_events(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    rows=get_events(10)
    lines=[f"`{fmt_ts(ts)}` [{et}] {msg}" for ts,et,msg in rows]
    await target.reply_text("📋 *Recent Events*\n\n"+"\n".join(lines) if lines else "No events.",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_wlog(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    rows=get_watering_history(8)
    lines=[f"`{fmt_ts(ts)}` {dur}s [{rsn}] soil:{sb:.1f}%" for ts,dur,rsn,sb,hb in rows]
    await target.reply_text("🚿 *Watering Log*\n\n"+"\n".join(lines) if lines else "No records.",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_alert_hist(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    rows=get_alert_history(8)
    lines=[f"`{fmt_ts(ts)}` [{at}] {v:.1f} — {msg}" for ts,at,v,msg in rows]
    await target.reply_text("🔔 *Alert History*\n\n"+"\n".join(lines) if lines else "No alerts.",
        parse_mode='Markdown', reply_markup=main_kb())

@admin_only
async def cmd_schedule(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    if not watering_schedule:
        await target.reply_text("⏰ No schedules. Use /add\\_sched HH MM dur days",parse_mode='Markdown',reply_markup=main_kb()); return
    lines=[f"{'✅' if s['enabled'] else '❌'} {s['hour']:02d}:{s['minute']:02d} {s['duration']}s [{s['days']}] id:{s['id']}" for s in watering_schedule]
    await target.reply_text("⏰ *Schedule*\n\n"+"\n".join(lines),parse_mode='Markdown',reply_markup=main_kb())

@admin_only
async def cmd_add_sched(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        h,m,dur=int(c.args[0]),int(c.args[1]),int(c.args[2])
        days=c.args[3] if len(c.args)>3 else "all"
        watering_schedule.append({"id":int(time.time()),"hour":h,"minute":m,"duration":dur,"days":days,"enabled":True})
        log_event("system",f"Schedule added via Telegram {h:02d}:{m:02d} {dur}s {days}")
        await u.message.reply_text(f"✅ Schedule: {h:02d}:{m:02d} {dur}s [{days}]",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /add_sched HH MM dur [all/weekday/weekend]")

@admin_only
async def cmd_del_sched(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        sid=int(c.args[0])
        watering_schedule[:]=[s for s in watering_schedule if s["id"]!=sid]
        await u.message.reply_text(f"✅ Schedule {sid} deleted.",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /del_sched id")

@admin_only
async def cmd_profiles(u: Update, c: ContextTypes.DEFAULT_TYPE):
    target=u.message or u.callback_query.message
    lines=[f"{'→ ' if cfg['plant_profile']==pid else ''}*{p['name']}*\n  soil>{p['th_soil']}% air>{p['th_air']}% {p['th_temp_low']}–{p['th_temp_high']}°C pump:{p['water_duration']}s"
           for pid,p in PLANT_PROFILES.items()]
    kb=InlineKeyboardMarkup([[InlineKeyboardButton(PLANT_PROFILES[pid]["name"],callback_data=f"profile_{pid}")] for pid in PLANT_PROFILES])
    await target.reply_text("🌱 *Plant Profiles*\n\n"+"\n\n".join(lines)+"\n\nTap to apply:",parse_mode='Markdown',reply_markup=kb)

@admin_only
async def cmd_profile(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        pid=c.args[0].lower()
        if pid not in PLANT_PROFILES: await u.message.reply_text(f"❌ Options: {','.join(PLANT_PROFILES)}"); return
        p=PLANT_PROFILES[pid]
        cfg.update({"plant_profile":pid,"th_soil":p["th_soil"],"th_air":p["th_air"],
            "th_temp_high":p["th_temp_high"],"th_temp_low":p["th_temp_low"],"water_duration":p["water_duration"]})
        log_event("system",f"Profile→{p['name']} (Telegram)")
        await u.message.reply_text(f"✅ Profile: *{p['name']}*",parse_mode='Markdown',reply_markup=main_kb())
    except: await u.message.reply_text("❌ /profile tomato")

@admin_only
async def cmd_th_soil(u,c):
    try: cfg["th_soil"]=float(c.args[0]); await u.message.reply_text(f"✅ Soil→{cfg['th_soil']}%",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /th_soil 35")
@admin_only
async def cmd_th_air(u,c):
    try: cfg["th_air"]=float(c.args[0]); await u.message.reply_text(f"✅ Air→{cfg['th_air']}%",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /th_air 45")
@admin_only
async def cmd_th_light(u,c):
    try: cfg["th_light"]=float(c.args[0]); await u.message.reply_text(f"✅ Light→{cfg['th_light']}%",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /th_light 80")
@admin_only
async def cmd_th_rain(u,c):
    try: cfg["th_rain"]=float(c.args[0]); await u.message.reply_text(f"✅ Rain→{cfg['th_rain']}%",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /th_rain 25")
@admin_only
async def cmd_th_temp(u,c):
    try: cfg["th_temp_high"],cfg["th_temp_low"]=float(c.args[0]),float(c.args[1]); await u.message.reply_text(f"✅ Temp→{cfg['th_temp_low']}–{cfg['th_temp_high']}°C",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /th_temp 35 10")
@admin_only
async def cmd_pump_time(u,c):
    try: cfg["water_duration"]=int(c.args[0]); await u.message.reply_text(f"✅ Pump→{cfg['water_duration']}s",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /pump_time 5")
@admin_only
async def cmd_motor_time(u,c):
    try: cfg["motor_duration"]=float(c.args[0]); await u.message.reply_text(f"✅ Motor→{cfg['motor_duration']}s",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /motor_time 3.5")
@admin_only
async def cmd_cooldown(u,c):
    try: cfg["water_cooldown"]=int(c.args[0]); await u.message.reply_text(f"✅ Cooldown→{cfg['water_cooldown']}s",reply_markup=main_kb())
    except: await u.message.reply_text("❌ /cooldown 300")
@admin_only
async def cmd_reset(u,c):
    data["water_count"]=data["motor_count"]=data["alert_count"]=0
    log_event("system","Stats reset (Telegram)"); await u.message.reply_text("✅ Daily counters reset.",reply_markup=main_kb())

@admin_only
async def btn_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q=u.callback_query; await q.answer(); act=q.data
    if act=="status":      await cmd_status(u,c)
    elif act=="health":    await cmd_health(u,c)
    elif act=="history":   await cmd_history(u,c)
    elif act=="events":    await cmd_events(u,c)
    elif act=="stats":     await cmd_stats(u,c)
    elif act=="wlog":      await cmd_wlog(u,c)
    elif act=="profiles":  await cmd_profiles(u,c)
    elif act=="schedule":  await cmd_schedule(u,c)
    elif act=="sysinfo":   await cmd_sysinfo(u,c)
    elif act=="help":      await cmd_help(u,c)
    elif act=="water":
        threading.Thread(target=run_pump_action,args=("telegram",),daemon=True).start()
        await q.message.reply_text(f"🚿 Pump {cfg['water_duration']}s started",reply_markup=main_kb())
    elif act=="pump_on":   await cmd_pump_on(u,c)
    elif act=="pump_off":  await cmd_pump_off(u,c)
    elif act=="motor":
        t="CLOSED" if cfg["motor_pos"]=="OPEN" else "OPEN"
        threading.Thread(target=run_motor,args=(t,"telegram"),daemon=True).start()
        await q.message.reply_text(f"🪟 Curtain → {t}",reply_markup=main_kb())
    elif act=="toggle_auto":
        cfg["auto_mode"]=not cfg["auto_mode"]
        await q.message.reply_text(f"🤖 → {'Auto✅' if cfg['auto_mode'] else 'Manual🔴'}",reply_markup=main_kb())
    elif act=="toggle_alert":
        cfg["alert_enabled"]=not cfg["alert_enabled"]
        await q.message.reply_text(f"🔔 Alerts {'on✅' if cfg['alert_enabled'] else 'off🔕'}",reply_markup=main_kb())
    elif act=="party_on":  await cmd_party_on(u,c)
    elif act=="party_off": await cmd_party_off(u,c)
    elif act=="emergency": await cmd_emergency(u,c)
    elif act=="settings":
        c2=cfg
        await q.message.reply_text(
            f"⚙️ *Settings*\n\n"
            f"Soil:{c2['th_soil']}% (crit {c2['th_soil_crit']}%) | Air:{c2['th_air']}% | HumHigh:{c2['th_hum_high']}%\n"
            f"Light:{c2['th_light']}% | Rain:{c2['th_rain']}%\n"
            f"Temp:{c2['th_temp_low']}–{c2['th_temp_high']}°C\n"
            f"Pump:{c2['water_duration']}s | Motor:{c2['motor_duration']}s | Cooldown:{c2['water_cooldown']}s\n"
            f"Rain-skip:{'✅' if c2['rain_skip_water'] else '❌'} | SmartCurtain:{'✅' if c2['smart_curtain'] else '❌'}\n"
            f"Night:{c2['night_start']}:00–{c2['night_end']}:00\n"
            f"Party: A:{c2['party_relay_a_on']}s/{c2['party_relay_a_off']}s B:{c2['party_relay_b_on']}s/{c2['party_relay_b_off']}s sync:{'✅' if c2['party_sync'] else '❌'}",
            parse_mode='Markdown')
    elif act.startswith("profile_"):
        pid=act.split("_",1)[1]
        if pid in PLANT_PROFILES:
            p=PLANT_PROFILES[pid]
            cfg.update({"plant_profile":pid,"th_soil":p["th_soil"],"th_air":p["th_air"],
                "th_temp_high":p["th_temp_high"],"th_temp_low":p["th_temp_low"],"water_duration":p["water_duration"]})
            log_event("system",f"Profile→{p['name']} (btn)")
            await q.message.reply_text(f"✅ Profile: *{p['name']}*",parse_mode='Markdown',reply_markup=main_kb())

# ================== 12. SCHEDULED JOBS ==================

async def job_report(c: ContextTypes.DEFAULT_TYPE):
    hs=health_score(); d2=data
    await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',
        text=(f"⏰ *Report* {time.strftime('%H:%M')} | Health:{hs}% {health_label(hs)}\n"
              f"🌡️{d2['temp']:.1f}°C 💧{d2['hum']:.1f}% 🪴{d2['soil']:.1f}% ☀️{d2['light']:.1f}% 🌧️{d2['rain']:.1f}%\n"
              f"💨VPD:{d2['vpd']}kPa 🌡️Dew:{d2['dew_point']}°C 😊{d2['comfort']}\n"
              f"🪟{cfg['motor_pos']} 🚿{cfg['pump']}{'🔒' if cfg['pump_manual_on'] else ''} 🎉{'ON' if cfg['party_mode'] else 'OFF'}"
              f" | W:{d2['water_count']} A:{d2['alert_count']}"))

# Wrapper that re-reads cfg["report_interval"] each tick so changing the
# setting in the web UI / Telegram takes effect on the next cycle.
_report_job_ref = None   # holds the APScheduler job so we can reschedule it

async def job_report_dynamic(c: ContextTypes.DEFAULT_TYPE):
    """Wrapper: send report then reschedule itself at the current interval."""
    global _report_job_ref
    await job_report(c)
    desired = max(30, int(cfg["report_interval"]))
    # Only reschedule if interval actually changed
    try:
        current = int(_report_job_ref.trigger.interval.total_seconds())
    except Exception:
        current = -1
    if current != desired and _report_job_ref is not None:
        _report_job_ref.schedule_removal()
        _report_job_ref = c.job_queue.run_repeating(
            job_report_dynamic, interval=desired, first=desired)
        log_event("system", f"Report interval updated → {desired}s")
async def job_alerts(c: ContextTypes.DEFAULT_TYPE):
    if not cfg["alert_enabled"] or (is_night() and cfg["night_mode"]): return
    t=data["temp"]
    if t>cfg["th_temp_high"] and can_alert("temp_high"):
        data["alert_count"]+=1; log_alert("temp_high",t,f"High:{t:.1f}°C")
        await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',text=f"🔥 *High Temp!* {t:.1f}°C > {cfg['th_temp_high']}°C")
    elif t<cfg["th_temp_low"] and can_alert("temp_low"):
        data["alert_count"]+=1; log_alert("temp_low",t,f"Low:{t:.1f}°C")
        await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',text=f"🥶 *Low Temp!* {t:.1f}°C < {cfg['th_temp_low']}°C")
    if data["soil"]<cfg["th_soil_crit"] and can_alert("soil_crit"):
        data["alert_count"]+=1; log_alert("soil_crit",data["soil"],f"Crit:{data['soil']:.1f}%")
        await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',text=f"‼️ *Critical Dry!* {data['soil']:.1f}% < {cfg['th_soil_crit']}%")
    if data["hum"]>cfg["th_hum_high"] and can_alert("hum_high"):
        data["alert_count"]+=1; log_alert("hum_high",data["hum"],f"High:{data['hum']:.1f}%")
        await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',text=f"💦 *High Humidity!* {data['hum']:.1f}% > {cfg['th_hum_high']}% (mold risk)")
    if data["vpd"]>2.0 and can_alert("vpd_high"):
        data["alert_count"]+=1; log_alert("vpd_high",data["vpd"],f"VPD:{data['vpd']}kPa")
        await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',text=f"🌵 *High VPD!* {data['vpd']} kPa — plant stress risk!")
    if not data["last_read_ok"] and can_alert("sensor_err"):
        await c.bot.send_message(TARGET_CHAT_ID,parse_mode='Markdown',text=f"⚠️ *Sensor Error!* DHT22 failed. Total:{data['sensor_errors']}")

# ================== 13. MAIN ==================

def main():
    init_db(); log_event("system",f"GreenHouse OS v{VERSION} started {'(VIRTUAL)' if VIRTUAL else '(REAL)'}")
    threading.Thread(target=core_monitor,daemon=True).start()
    if VIRTUAL:
        threading.Thread(target=_virtual_sim_loop, daemon=True).start()
        print("🟡 Virtual sensor simulator started")
    threading.Thread(target=lambda:app.run(host='0.0.0.0',port=5000,debug=False,use_reloader=False),daemon=True).start()

    if not TELEGRAM_OK:
        print("⚠️  Telegram disabled — running web-only mode")
        print(f"🚀 GreenHouse OS v{VERSION} {'[VIRTUAL]' if VIRTUAL else '[REAL]'} started!")
        print("   🌐 Dashboard: http://0.0.0.0:5000")
        while True: time.sleep(60)
        return

    bot=Application.builder().token(TELEGRAM_TOKEN).build()
    cmds=[
        ("start",cmd_start),("help",cmd_help),("status",cmd_status),
        ("health",cmd_health),("history",cmd_history),("stats",cmd_stats),
        ("events",cmd_events),("wlog",cmd_wlog),("alerts",cmd_alert_hist),
        ("sysinfo",cmd_sysinfo),
        ("water",cmd_water),("pump_on",cmd_pump_on),("pump_off",cmd_pump_off),
        ("motor",cmd_motor),
        ("party_on",cmd_party_on),("party_off",cmd_party_off),
        ("party_set",cmd_party_set),("party_sync",cmd_party_sync),
        ("schedule",cmd_schedule),("add_sched",cmd_add_sched),("del_sched",cmd_del_sched),
        ("profiles",cmd_profiles),("profile",cmd_profile),
        ("th_soil",cmd_th_soil),("th_air",cmd_th_air),("th_light",cmd_th_light),
        ("th_rain",cmd_th_rain),("th_temp",cmd_th_temp),
        ("pump_time",cmd_pump_time),("motor_time",cmd_motor_time),("cooldown",cmd_cooldown),
        ("reset",cmd_reset),("emergency",cmd_emergency),
    ]
    for cmd,fn in cmds: bot.add_handler(CommandHandler(cmd,fn))

    # Simple toggle commands — proper async with admin guard
    @admin_only
    async def _auto_on(u,c):  cfg["auto_mode"]=True;      log_event("system","Auto ON (Telegram)");  await u.message.reply_text("🤖 Auto mode ON ✅",reply_markup=main_kb())
    @admin_only
    async def _auto_off(u,c): cfg["auto_mode"]=False;     log_event("system","Auto OFF (Telegram)"); await u.message.reply_text("🔴 Manual mode",reply_markup=main_kb())
    @admin_only
    async def _alert_on(u,c): cfg["alert_enabled"]=True;  log_event("system","Alerts ON (Telegram)"); await u.message.reply_text("🔔 Alerts enabled ✅",reply_markup=main_kb())
    @admin_only
    async def _alert_off(u,c):cfg["alert_enabled"]=False; log_event("system","Alerts OFF (Telegram)");await u.message.reply_text("🔕 Alerts disabled",reply_markup=main_kb())
    for cmd,fn in [("auto_on",_auto_on),("auto_off",_auto_off),("alert_on",_alert_on),("alert_off",_alert_off)]:
        bot.add_handler(CommandHandler(cmd,fn))
    bot.add_handler(CallbackQueryHandler(btn_callback))
    jq=bot.job_queue
    # job_report_dynamic reschedules itself whenever cfg["report_interval"] changes
    global _report_job_ref
    _report_job_ref = jq.run_repeating(
        job_report_dynamic, interval=cfg["report_interval"], first=10)
    jq.run_repeating(job_alerts, interval=60)

    print(f"🚀 GreenHouse OS v{VERSION} {'[VIRTUAL]' if VIRTUAL else '[REAL HW]'} started!")
    print("   🌐 Dashboard: http://0.0.0.0:5000")
    print("   🤖 Telegram: connected")
    bot.run_polling()

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: pass
    finally:
        try: cfg["party_mode"]=False
        except: pass
        try: pump_relay.value=False; motor_relay.value=False; dht_device.exit()
        except: pass