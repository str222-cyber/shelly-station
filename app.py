from flask import Flask, render_template_string, jsonify, session
import requests
import time
import threading
import uuid

app = Flask(__name__)
app.secret_key = "shelly_geheimer_schluessel_123"

# --- DEINE ECHTEN SHELLY CLOUD DATEN ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

STROMPREIS_PER_KWH = 0.35

current_watt = 0.0
user_sessions = {}
last_cloud_error = "Kein Fehler"

@app.after_request
def add_universal_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

# --- STEUERUNG ÜBER SHELLY CLOUD ---
def cloud_control(turn_on=True):
    global last_cloud_error
    turn_str = "on" if turn_on else "off"
    
    # 1. Versuch: Standard Relay API
    payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID,
        "turn": turn_str,
        "channel": 0
    }
    try:
        res = requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control", data=payload, timeout=5).json()
        if res.get("isok"):
            last_cloud_error = "OK (Relay)"
            return True
    except Exception as e:
        pass

    # 2. Versuch: Gen2 RPC Call
    rpc_payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID,
        "method": "Switch.Set",
        "params": {"id": 0, "on": turn_on}
    }
    try:
        res_rpc = requests.post(f"{SHELLY_CLOUD_URL}/device/rpc", json=rpc_payload, timeout=5).json()
        if res_rpc.get("isok"):
            last_cloud_error = "OK (RPC)"
            return True
        else:
            last_cloud_error = str(res_rpc)
    except Exception as e:
        last_cloud_error = str(e)

    return False

def cloud_get_status():
    payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID
    }
    try:
        res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data=payload, timeout=5).json()
        if res.get("isok"):
            status = res.get("data", {}).get("device_status", {})
            if "switch:0" in status:
                return float(status["switch:0"].get("apower", 0.0))
            elif "meters" in status and len(status["meters"]) > 0:
                return float(status["meters"][0].get("power", 0.0))
    except Exception as e:
        pass
    return 0.0

def poll_shelly():
    global current_watt
    while True:
        any_active = any(u.get("active", False) for u in user_sessions.values())
        if any_active:
            current_watt = cloud_get_status()
            now = time.time()
            for uid, u in user_sessions.items():
                if u.get("active", False) and u.get("last_check"):
                    time_diff = now - u["last_check"]
                    kwh_added = (current_watt * time_diff) / 3600000.0
                    u["total_kwh"] += kwh_added
                    u["total_seconds"] += int(time_diff)
                    u["last_check"] = now

                    if current_watt > u.get("max_watt", 0):
                        u["max_watt"] = current_watt

                    total_wh = u["total_kwh"] * 1000.0

                    if u["max_watt"] > 5.0 and current_watt < 2.0 and total_wh > 2.0:
                        if not u.get("full_triggered", False):
                            u["active"] = False
                            u["full_triggered"] = True
                            u["auto_stopped"] = True
                            cloud_control(turn_on=False)
        else:
            current_watt = 0.0
        time.sleep(3)

threading.Thread(target=poll_shelly, daemon=True).start()

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Shelly Ladestation</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; text-align: center; background: #f0f2f5; padding: 15px; margin: 0; }
        .card { background: white; padding: 20px; border-radius: 16px; max-width: 380px; margin: auto; box-shadow: 0 4px 15px rgba(0,0,0,0.08); }
        .rate-badge { background: #e9ecef; color: #495057; font-size: 13px; padding: 6px 12px; border-radius: 20px; display: inline-block; margin-bottom: 12px; font-weight: 500; }
        .status-badge { font-size: 12px; padding: 4px 10px; border-radius: 12px; display: inline-block; font-weight: bold; margin-bottom: 15px; }
        .status-on { background: #d4edda; color: #155724; }
        .status-off { background: #f8d7da; color: #721c24; }
        .box { background: #f8f9fa; padding: 12px; border-radius: 12px; margin: 10px 0; border-left: 5px solid #007bff; text-align: left; }
        .box-title { font-size: 12px; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; }
        .cost-box { border-left-color: #28a745; background: #eef9f1; }
        .time-box { border-left-color: #ffc107; background: #fffdf5; }
        .battery-box { border-left-color: #17a2b8; background: #f0fbfc; }
        .val { font-size: 24px; font-weight: bold; margin-top: 3px; color: #212529; }
        .cost-val { color: #28a745; font-size: 26px; }
        .time-val { color: #d39e00; font-family: monospace; }
        .sub-val { font-size: 12px; color: #6c757d; margin-top: 2px; }
        select { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #ced4da; font-size: 14px; margin-top: 5px; }
        .bar-container { margin: 8px 0 4px 0; width: 100%; height: 22px; background: #e9ecef; border-radius: 11px; overflow: hidden; position: relative; border: 1px solid #ced4da; }
        .bar-fill-wh { height: 100%; width: 0%; background: linear-gradient(90deg, #007bff, #17a2b8); transition: width 0.4s ease; }
        .bar-fill-cost { height: 100%; width: 0%; background: linear-gradient(90deg, #28a745, #20c997); transition: width 0.4s ease; }
        .bar-text { position: absolute; width: 100%; text-align: center; top: 2px; font-size: 12px; font-weight: bold; color: #212529; text-shadow: 0 0 3px #ffffff; }
        .battery-outer { width: 100%; height: 26px; background: #e9ecef; border-radius: 13px; overflow: hidden; position: relative; border: 2px solid #adb5bd; margin-top: 6px; }
        .battery-inner { height: 100%; width: 0%; background: linear-gradient(90deg, #28a745, #20c997); transition: width 0.5s ease; }
        .battery-marker-80 { position: absolute; left: 80%; top: 0; bottom: 0; width: 2px; background: #ffc107; z-index: 2; }
        .battery-text { position: absolute; width: 100%; text-align: center; top: 3px; font-size: 12px; font-weight: bold; color: #212529; text-shadow: 0 0 3px #ffffff; z-index: 3; }
        .estimate-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; font-size: 12px; }
        .estimate-card { background: #ffffff; padding: 8px; border-radius: 8px; border: 1px solid #e0e0e0; }
        button { width: 100%; padding: 14px; font-size: 16px; font-weight: bold; border: none; border-radius: 12px; margin-top: 8px; cursor: pointer; }
        .btn-start { background: #28a745; color: white; }
        .btn-stop { background: #ffc107; color: #212529; }
        .btn-logout { background: #dc3545; color: white; margin-top: 15px; font-size: 14px; padding: 10px; }
        .debug-info { font-size: 11px; color: #888; margin-top: 12px; background: #eee; padding: 6px; border-radius: 6px; }
    </style>
</head>
<body>
    <div class="card" id="mainCard">
        <h2>⚡ Ladestation (24/7 Cloud)</h2>
        <div class="rate-badge">Tarif: <b>""" + str(STROMPREIS_PER_KWH) + """ €/kWh</b></div><br>
        <div id="statusBadge" class="status-badge status-off">Inaktiv / Pausiert</div>

        <div style="text-align: left; margin-bottom: 10px;">
            <label style="font-size: 12px; font-weight: bold; color: #495057;">Geräte-Akkutyp wählen:</label>
            <select id="batteryType" onchange="updateBatteryProfile()">
                <option value="20">Smartphone (ca. 20 Wh)</option>
                <option value="50">Tablet / Laptop (ca. 50 Wh)</option>
                <option value="500" selected>E-Bike Akku (ca. 500 Wh)</option>
                <option value="750">Großer E-Bike Akku (ca. 750 Wh)</option>
            </select>
        </div>
        
        <div class="box battery-box">
            <div class="box-title">🔋 Geschätzter Akku-Ladezustand</div>
            <div class="battery-outer">
                <div class="battery-marker-80"></div>
                <div class="battery-inner" id="batteryFill"></div>
                <div class="battery-text"><span id="batteryPercent">0</span>% Geladen</div>
            </div>
            <div class="estimate-grid">
                <div class="estimate-card">
                    <b>Bis 80% voll:</b><br>
                    <span id="rem80Wh">0</span> Wh fehlen<br>
                    <small>⏱️ ca. <span id="rem80Min">0</span> Min</small>
                </div>
                <div class="estimate-card">
                    <b>Bis 100% voll:</b><br>
                    <span id="rem100Wh">0</span> Wh fehlen<br>
                    <small>⏱️ ca. <span id="rem100Min">0</span> Min</small>
                </div>
            </div>
        </div>

        <div class="box time-box">
            <div class="box-title">Gesamte Nutzungsdauer</div>
            <div class="val time-val"><span id="timer">00:00:00</span></div>
        </div>

        <div class="box">
            <div class="box-title">Aktuelle Ladeleistung</div>
            <div class="val"><span id="watt">0.00</span> W</div>
        </div>

        <div class="box">
            <div class="box-title">⚡ Kumulierte Energie (Wh)</div>
            <div class="val" style="color:#007bff;"><span id="wh">0.0000</span> Wh</div>
            <div class="bar-container">
                <div class="bar-fill-wh" id="whBarFill"></div>
                <div class="bar-text" id="whBarText">0.0 Wh</div>
            </div>
            <div class="sub-val">(<span id="kwh">0.000000</span> kWh)</div>
        </div>

        <div class="box cost-box">
            <div class="box-title">💶 Akkumulierte Kosten (€)</div>
            <div class="val cost-val"><span id="cost">0,000000</span> €</div>
            <div class="bar-container">
                <div class="bar-fill-cost" id="costBarFill"></div>
                <div class="bar-text" id="costBarText">0,00 €</div>
            </div>
        </div>

        <button class="btn-start" onclick="sendAction('/start')">▶️ Start / Strom Aktivieren</button>
        <button class="btn-stop" onclick="sendAction('/stop')">⏸️ Pause / Stopp</button>
        <button class="btn-logout" onclick="logout()">🚪 Sitzung beenden</button>
        
        <div class="debug-info">Status: <span id="cloudStatus">Bereit</span></div>
    </div>

    <script>
        let batteryCapacityWh = 500;
        let isTerminated = false;

        function updateBatteryProfile() {
            batteryCapacityWh = parseFloat(document.getElementById('batteryType').value);
        }

        async function sendAction(url) {
            document.getElementById('cloudStatus').innerText = "Sende Befehl...";
            try {
                let res = await fetch(url, { cache: 'no-store' });
                let data = await res.json();
                document.getElementById('cloudStatus').innerText = "Status: " + data.cloud_result;
            } catch(e) {
                document.getElementById('cloudStatus').innerText = "Verbindungsfehler";
            }
        }

        async function logout() {
            isTerminated = true;
            await fetch('/logout', { cache: 'no-store' });
            window.location.reload();
        }

        setInterval(async () => {
            if (isTerminated) return;
            try {
                let res = await fetch('/status', { cache: 'no-store' });
                let data = await res.json();

                document.getElementById('watt').innerText = data.watt.toFixed(2);
                document.getElementById('wh').innerText = data.wh.toFixed(4);
                document.getElementById('kwh').innerText = data.kwh.toFixed(6);
                document.getElementById('cost').innerText = data.cost.toFixed(4).replace('.', ',');

                let sec = data.elapsed_seconds;
                let h = Math.floor(sec / 3600).toString().padStart(2, '0');
                let m = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
                let s = Math.floor(sec % 60).toString().padStart(2, '0');
                document.getElementById('timer').innerText = `${h}:${m}:${s}`;

                let badge = document.getElementById('statusBadge');
                if (data.active) {
                    badge.innerText = "⚡ Strom fließt aktiv...";
                    badge.className = "status-badge status-on";
                } else {
                    badge.innerText = "⏸️ Pausiert / Bereit";
                    badge.className = "status-badge status-off";
                }

                let chargedWh = data.wh;
                let pct = Math.min(100, (chargedWh / batteryCapacityWh) * 100);
                document.getElementById('batteryFill').style.width = pct.toFixed(1) + '%';
                document.getElementById('batteryPercent').innerText = pct.toFixed(1);

                let wh80 = Math.max(0, (batteryCapacityWh * 0.8) - chargedWh);
                let wh100 = Math.max(0, batteryCapacityWh - chargedWh);
                document.getElementById('rem80Wh').innerText = wh80.toFixed(1);
                document.getElementById('rem100Wh').innerText = wh100.toFixed(1);

                if (data.watt > 1.0) {
                    document.getElementById('rem80Min').innerText = Math.ceil((wh80 / data.watt) * 60);
                    document.getElementById('rem100Min').innerText = Math.ceil((wh100 / data.watt) * 60);
                } else {
                    document.getElementById('rem80Min').innerText = "--";
                    document.getElementById('rem100Min').innerText = "--";
                }

                document.getElementById('whBarFill').style.width = pct.toFixed(1) + '%';
                document.getElementById('whBarText').innerText = chargedWh.toFixed(2) + " Wh / " + batteryCapacityWh + " Wh";

                let costPct = Math.min(100, (data.cost / 2.0) * 100);
                document.getElementById('costBarFill').style.width = costPct.toFixed(1) + '%';
                document.getElementById('costBarText').innerText = data.cost.toFixed(4).replace('.', ',') + " €";
            } catch(e) {}
        }, 1000);
    </script>
</body>
</html>
"""

def get_user_data():
    if "user_id" not in session:
        session["user_id"] = str(uuid.uuid4())
    uid = session["user_id"]
    if uid not in user_sessions:
        user_sessions[uid] = {
            "active": False,
            "total_kwh": 0.0,
            "total_seconds": 0,
            "last_check": None,
            "max_watt": 0.0,
            "full_triggered": False,
            "auto_stopped": False
        }
    return user_sessions[uid]

@app.route('/')
def home():
    get_user_data()
    return render_template_string(HTML)

@app.route('/start')
def start():
    u = get_user_data()
    u["active"] = True
    u["last_check"] = time.time()
    cloud_control(turn_on=True)
    return jsonify({"status": "ok", "cloud_result": last_cloud_error})

@app.route('/stop')
def stop():
    u = get_user_data()
    u["active"] = False
    cloud_control(turn_on=False)
    return jsonify({"status": "ok", "cloud_result": last_cloud_error})

@app.route('/logout')
def logout():
    u = get_user_data()
    u["active"] = False
    cloud_control(turn_on=False)
    uid = session.get("user_id")
    if uid in user_sessions:
        del user_sessions[uid]
    session.clear()
    return jsonify({"status": "logged_out"})

@app.route('/status')
def status():
    u = get_user_data()
    total_wh = u["total_kwh"] * 1000.0
    cost = u["total_kwh"] * STROMPREIS_PER_KWH
    return jsonify({
        "active": u["active"],
        "watt": current_watt if u["active"] else 0.0,
        "wh": total_wh,
        "kwh": u["total_kwh"],
        "cost": cost,
        "elapsed_seconds": u["total_seconds"],
        "cloud_last": last_cloud_error
    })

if __name__ == '__main__':
    app.run()
