from flask import Flask, render_template_string, jsonify, session
import requests
import time
import uuid

app = Flask(__name__)
app.secret_key = "shelly_secret_key_cloud_render_999"

# --- SHELLY CLOUD DATEN ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

STROMPREIS_PER_KWH = 0.35  # 0,35 € pro kWh

user_sessions = {}

@app.after_request
def add_universal_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

def cloud_control(turn_on=True):
    turn_str = "on" if turn_on else "off"
    payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID,
        "turn": turn_str,
        "channel": 0
    }
    try:
        requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control", data=payload, timeout=4)
    except:
        pass

    # Fallback für Gen2 RPC
    rpc_payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID,
        "method": "Switch.Set",
        "params": {"id": 0, "on": turn_on}
    }
    try:
        requests.post(f"{SHELLY_CLOUD_URL}/device/rpc", json=rpc_payload, timeout=4)
    except:
        pass

def cloud_get_watt():
    payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID
    }
    try:
        res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data=payload, timeout=4).json()
        if res.get("isok"):
            status = res.get("data", {}).get("device_status", {})
            
            # Plus / Gen 2 / Gen 3
            if "switch:0" in status:
                return float(status["switch:0"].get("apower", 0.0))
            # Standard Gen 1 Relays / Meters
            elif "meters" in status and len(status["meters"]) > 0:
                return float(status["meters"][0].get("power", 0.0))
            elif "relays" in status and len(status["relays"]) > 0:
                return float(status["relays"][0].get("power", 0.0))
    except:
        pass
    return 0.0

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Shelly Strom- & Ladestation</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; text-align: center; background: #f0f2f5; padding: 15px; margin: 0; }
        .card { background: white; padding: 20px; border-radius: 16px; max-width: 380px; margin: auto; box-shadow: 0 4px 15px rgba(0,0,0,0.08); }
        .rate-badge { background: #e9ecef; color: #495057; font-size: 13px; padding: 6px 12px; border-radius: 20px; display: inline-block; margin-bottom: 12px; font-weight: 500; }
        
        .status-badge { font-size: 13px; padding: 6px 14px; border-radius: 12px; display: inline-block; font-weight: bold; margin-bottom: 15px; }
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
        
        select { width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #ced4da; font-size: 14px; margin-top: 5px; background: white; font-weight: 500; }
        
        .bar-container { margin: 8px 0 4px 0; width: 100%; height: 22px; background: #e9ecef; border-radius: 11px; overflow: hidden; position: relative; border: 1px solid #ced4da; }
        .bar-fill-wh { height: 100%; width: 0%; background: linear-gradient(90deg, #007bff, #17a2b8); transition: width 0.4s ease; }
        .bar-fill-cost { height: 100%; width: 0%; background: linear-gradient(90deg, #28a745, #20c997); transition: width 0.4s ease; }
        .bar-text { position: absolute; width: 100%; text-align: center; top: 2px; font-size: 12px; font-weight: bold; color: #212529; text-shadow: 0 0 3px #ffffff; }

        .battery-outer { width: 100%; height: 26px; background: #e9ecef; border-radius: 13px; overflow: hidden; position: relative; border: 2px solid #adb5bd; margin-top: 6px; }
        .battery-inner { height: 100%; width: 0%; background: linear-gradient(90deg, #28a745, #20c997); transition: width 0.5s ease; }
        .battery-marker-80 { position: absolute; left: 80%; top: 0; bottom: 0; width: 2px; background: #ffc107; z-index: 2; }
        .battery-text { position: absolute; width: 100%; text-align: center; top: 3px; font-size: 12px; font-weight: bold; color: #212529; text-shadow: 0 0 3px #ffffff; z-index: 3; }

        button { width: 100%; padding: 14px; font-size: 16px; font-weight: bold; border: none; border-radius: 12px; margin-top: 8px; cursor: pointer; }
        .btn-start { background: #28a745; color: white; }
        .btn-stop { background: #ffc107; color: #212529; }
        .btn-logout { background: #dc3545; color: white; margin-top: 15px; font-size: 14px; padding: 10px; }
        
        .receipt-card { display: none; background: #ffffff; border: 2px solid #28a745; border-radius: 16px; padding: 20px; text-align: left; }
        .receipt-title { color: #28a745; text-align: center; font-size: 20px; font-weight: bold; margin-bottom: 15px; }
        .receipt-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px dashed #dee2e6; font-size: 14px; }
        .receipt-total { font-size: 18px; font-weight: bold; color: #212529; border-top: 2px solid #212529; margin-top: 10px; padding-top: 10px; }
    </style>
</head>
<body>
    <div class="card" id="mainCard">
        <h2>⚡ Strom- & Ladestation</h2>
        <div class="rate-badge">Tarif: <b>""" + str(STROMPREIS_PER_KWH) + """ €/kWh</b></div><br>
        <div id="statusBadge" class="status-badge status-off">Inaktiv / Pausiert</div>

        <!-- GERÄTE-AUSWAHL -->
        <div style="text-align: left; margin-bottom: 10px;">
            <label style="font-size: 12px; font-weight: bold; color: #495057;">Gerätetyp wählen:</label>
            <select id="deviceType" onchange="updateDeviceProfile()">
                <optgroup label="Akku-Geräte (mit Ladeanzeige)">
                    <option value="battery_20">Smartphone / Google Pixel (ca. 20 Wh)</option>
                    <option value="battery_50">Tablet / Laptop (ca. 50 Wh)</option>
                    <option value="battery_500" selected>E-Bike Akku (ca. 500 Wh)</option>
                    <option value="battery_750">Großer E-Bike Akku (ca. 750 Wh)</option>
                </optgroup>
                <optgroup label="Dauerverbraucher / Haushaltsgeräte">
                    <option value="continuous_tv">📺 Fernseher / TV</option>
                    <option value="continuous_lamp">💡 Lampe / Beleuchtung</option>
                    <option value="continuous_appliance">🍳 Küchen- / Haushaltsgerät</option>
                    <option value="continuous_other">🔌 Sonstiges Elektrogerät</option>
                </optgroup>
            </select>
        </div>
        
        <!-- AKKU BEREICH (nur sichtbar bei Akku-Geräten) -->
        <div class="box battery-box" id="batteryBox">
            <div class="box-title">🔋 Geschätzter Akku-Ladezustand</div>
            <div class="battery-outer">
                <div class="battery-marker-80"></div>
                <div class="battery-inner" id="batteryFill"></div>
                <div class="battery-text"><span id="batteryPercent">0</span>% Geladen</div>
            </div>
        </div>

        <div class="box time-box">
            <div class="box-title">Gesamte Laufzeit</div>
            <div class="val time-val"><span id="timer">00:00:00</span></div>
        </div>

        <div class="box">
            <div class="box-title">Aktuelle Leistung</div>
            <div class="val"><span id="watt">0.00</span> W</div>
        </div>

        <!-- ENERGIE IN WH -->
        <div class="box">
            <div class="box-title">⚡ Verbrauch / Geladene Energie</div>
            <div class="val" style="color:#007bff;"><span id="wh">0.0000</span> Wh</div>
            <div class="bar-container">
                <div class="bar-fill-wh" id="whBarFill"></div>
                <div class="bar-text" id="whBarText">0.0 Wh</div>
            </div>
            <div class="sub-val">(<span id="kwh">0.000000</span> kWh)</div>
        </div>

        <!-- KOSTEN IN EURO -->
        <div class="box cost-box">
            <div class="box-title">💶 Gesamtkosten (€)</div>
            <div class="val cost-val"><span id="cost">0,0000</span> €</div>
            <div class="bar-container">
                <div class="bar-fill-cost" id="costBarFill"></div>
                <div class="bar-text" id="costBarText">0,00 €</div>
            </div>
        </div>

        <button class="btn-start" onclick="sendAction('/start')">▶️ Start / Einschalten</button>
        <button class="btn-stop" onclick="sendAction('/stop')">⏸️ Pause / Ausschalten</button>
        <button class="btn-logout" onclick="logout()">🧾 Abrechnen & Beenden</button>
    </div>

    <!-- QUITTUNG -->
    <div class="card receipt-card" id="receiptCard">
        <div class="receipt-title">🧾 Strom-Quittung</div>
        <div class="receipt-row"><span>Status:</span> <b style="color:#28a745;">Beendet & Abgerechnet</b></div>
        <div class="receipt-row"><span>Gewähltes Gerät:</span> <b id="rDevice">E-Bike</b></div>
        <div class="receipt-row"><span>Gesamte Zeit:</span> <b id="rTime">00:00:00</b></div>
        <div class="receipt-row"><span>Verbrauch (Wh):</span> <b id="rWh">0.00 Wh</b></div>
        <div class="receipt-row"><span>Verbrauch (kWh):</span> <b id="rKwh">0.0000 kWh</b></div>
        <div class="receipt-row receipt-total"><span>Gesamtbetrag:</span> <span id="rCost" style="color: #28a745;">0,00 €</span></div>
        <button class="btn-start" style="margin-top:20px;" onclick="window.location.reload()">🔄 Neue Messung starten</button>
    </div>

    <script>
        let isBattery = true;
        let batteryCapacityWh = 500;
        let isTerminated = false;

        function updateDeviceProfile() {
            let val = document.getElementById('deviceType').value;
            let batteryBox = document.getElementById('batteryBox');
            
            if (val.startsWith('battery_')) {
                isBattery = true;
                batteryCapacityWh = parseFloat(val.replace('battery_', ''));
                batteryBox.style.display = 'block';
            } else {
                isBattery = false;
                batteryBox.style.display = 'none';
            }
        }

        async function sendAction(url) {
            try {
                await fetch(url, { cache: 'no-store' });
            } catch(e) {}
        }

        async function logout() {
            isTerminated = true;
            try {
                let res = await fetch('/logout', { cache: 'no-store' });
                let report = await res.json();
                
                let devName = document.getElementById('deviceType').options[document.getElementById('deviceType').selectedIndex].text;
                document.getElementById('rDevice').innerText = devName;
                document.getElementById('rTime').innerText = report.time_formatted;
                document.getElementById('rWh').innerText = report.wh.toFixed(2) + " Wh";
                document.getElementById('rKwh').innerText = report.kwh.toFixed(5) + " kWh";
                document.getElementById('rCost').innerText = report.cost.toFixed(4).replace('.', ',') + " €";
                
                document.getElementById('mainCard').style.display = 'none';
                document.getElementById('receiptCard').style.display = 'block';
            } catch(e) {
                isTerminated = false;
            }
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
                if(data.active) {
                    badge.innerText = "⚡ Strom eingeschaltet";
                    badge.className = "status-badge status-on";
                } else {
                    badge.innerText = "⏸️ Ausgeschaltet / Bereit";
                    badge.className = "status-badge status-off";
                }

                if (isBattery) {
                    let pct = Math.min(100, (data.wh / batteryCapacityWh) * 100);
                    document.getElementById('batteryFill').style.width = pct.toFixed(1) + '%';
                    document.getElementById('batteryPercent').innerText = pct.toFixed(1);
                    
                    document.getElementById('whBarFill').style.width = pct.toFixed(1) + '%';
                    document.getElementById('whBarText').innerText = data.wh.toFixed(1) + " / " + batteryCapacityWh + " Wh";
                } else {
                    let maxScale = 500;
                    let p = Math.min(100, (data.wh / maxScale) * 100);
                    document.getElementById('whBarFill').style.width = p.toFixed(1) + '%';
                    document.getElementById('whBarText').innerText = data.wh.toFixed(2) + " Wh";
                }

                let costP = Math.min(100, (data.cost / 2.0) * 100);
                document.getElementById('costBarFill').style.width = costP.toFixed(1) + '%';
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
            "current_watt": 0.0
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
    return jsonify({"status": "ok"})

@app.route('/stop')
def stop():
    u = get_user_data()
    u["active"] = False
    u["last_check"] = None
    u["current_watt"] = 0.0
    cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/logout')
def logout():
    u = get_user_data()
    u["active"] = False
    cloud_control(turn_on=False)

    sec = u["total_seconds"]
    h = str(sec // 3600).zfill(2)
    m = str((sec % 3600) // 60).zfill(2)
    s = str(sec % 60).zfill(2)

    report = {
        "time_formatted": f"{h}:{m}:{s}",
        "wh": u["total_kwh"] * 1000.0,
        "kwh": u["total_kwh"],
        "cost": u["total_kwh"] * STROMPREIS_PER_KWH
    }

    uid = session.get("user_id")
    if uid in user_sessions:
        del user_sessions[uid]
    session.clear()

    return jsonify(report)

@app.route('/status')
def status():
    u = get_user_data()
    if u["active"]:
        now = time.time()
        w = cloud_get_watt()
        u["current_watt"] = w
        
        if u["last_check"]:
            dt = now - u["last_check"]
            if dt > 0:
                kwh_inc = (w * dt) / 3600000.0
                u["total_kwh"] += kwh_inc
                u["total_seconds"] += int(dt)
        u["last_check"] = now
    else:
        u["current_watt"] = 0.0

    return jsonify({
        "active": u["active"],
        "watt": u["current_watt"],
        "wh": u["total_kwh"] * 1000.0,
        "kwh": u["total_kwh"],
        "cost": u["total_kwh"] * STROMPREIS_PER_KWH,
        "elapsed_seconds": u["total_seconds"]
    })

if __name__ == '__main__':
    app.run()
