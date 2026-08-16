from flask import Flask, render_template_string, jsonify, session
import requests
import time
import uuid

app = Flask(__name__)
app.secret_key = "shelly_harmonisch_cloud_secret_888"

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
        requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control", data=payload, timeout=3.5)
    except:
        pass

    # Gen 2 / Gen 3 Fallback
    rpc_payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID,
        "method": "Switch.Set",
        "params": {"id": 0, "on": turn_on}
    }
    try:
        requests.post(f"{SHELLY_CLOUD_URL}/device/rpc", json=rpc_payload, timeout=3.5)
    except:
        pass

def cloud_get_watt():
    payload = {
        "auth_key": AUTH_KEY,
        "id": DEVICE_ID
    }
    try:
        res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data=payload, timeout=3.5).json()
        if res.get("isok"):
            status = res.get("data", {}).get("device_status", {})
            if "switch:0" in status:
                return float(status["switch:0"].get("apower", 0.0))
            elif "meters" in status and len(status["meters"]) > 0:
                return float(status["meters"][0].get("power", 0.0))
            elif "relays" in status and len(status["relays"]) > 0:
                return float(status["relays"][0].get("power", 0.0))
    except:
        pass
    return 0.0

HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Charge & Power</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        :root {
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --accent-primary: #3b82f6;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --accent-violet: #6366f1;
            --border-color: #e2e8f0;
            --shadow-sm: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02);
            --shadow-md: 0 10px 25px -5px rgba(15, 23, 42, 0.06), 0 8px 10px -6px rgba(15, 23, 42, 0.04);
        }

        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; padding: 20px 12px; min-height: 100vh; }
        
        .container { width: 100%; max-width: 410px; margin: auto; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 24px 20px; box-shadow: var(--shadow-md); border: 1px solid var(--border-color); }
        
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
        .title { font-size: 19px; font-weight: 700; color: var(--text-main); letter-spacing: -0.3px; }
        .rate-badge { background: #f1f5f9; color: var(--text-muted); font-size: 12px; padding: 4px 10px; border-radius: 20px; font-weight: 600; }

        /* Auto-Erkennungsbanner */
        .ai-banner {
            background: linear-gradient(135deg, #eef2ff 0%, #f0fdf4 100%);
            border: 1px solid #cbd5e1;
            border-radius: 16px;
            padding: 12px 14px;
            margin-bottom: 16px;
            text-align: left;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .ai-icon { font-size: 20px; }
        .ai-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent-violet); letter-spacing: 0.5px; }
        .ai-detected { font-size: 14px; font-weight: 600; color: var(--text-main); margin-top: 1px; }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 13px;
            font-weight: 600;
            padding: 6px 14px;
            border-radius: 30px;
            margin-bottom: 16px;
        }
        .status-on { background: #ecfdf5; color: #065f46; }
        .status-off { background: #f1f5f9; color: var(--text-muted); }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; }
        .status-on .status-dot { background: var(--accent-green); box-shadow: 0 0 8px rgba(16,185,129,0.6); }
        .status-off .status-dot { background: #94a3b8; }

        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
        .stat-card {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 14px;
            text-align: left;
        }
        .stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.4px; }
        .stat-val { font-size: 21px; font-weight: 700; color: var(--text-main); margin-top: 4px; }
        .stat-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

        /* Farbige Akzente */
        .stat-watt .stat-val { color: var(--accent-primary); }
        .stat-cost .stat-val { color: var(--accent-green); }
        .stat-time .stat-val { color: var(--text-main); font-family: monospace; }

        /* Fortschritts-Balken */
        .bar-wrap { margin-top: 6px; width: 100%; height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
        .bar-fill { height: 100%; width: 0%; border-radius: 3px; transition: width 0.4s ease; }
        .bar-blue { background: var(--accent-primary); }
        .bar-green { background: var(--accent-green); }

        /* Tasten */
        .btn-group { display: flex; flex-direction: column; gap: 8px; margin-top: 18px; }
        button {
            width: 100%;
            padding: 14px;
            font-size: 15px;
            font-weight: 600;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            transition: transform 0.1s ease, opacity 0.2s ease;
        }
        button:active { transform: scale(0.98); }
        .btn-start { background: var(--text-main); color: white; }
        .btn-stop { background: #f1f5f9; color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-finish { background: #fee2e2; color: #991b1b; }

        /* Quittung */
        .receipt-card { display: none; text-align: left; }
        .receipt-header { text-align: center; margin-bottom: 20px; }
        .receipt-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
        .receipt-total { border-top: 2px solid var(--text-main); border-bottom: none; font-size: 17px; font-weight: 700; margin-top: 10px; padding-top: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <!-- HAUPTANSICHT -->
        <div class="card" id="mainCard">
            <div class="header">
                <span class="title">⚡ Energie Monitor</span>
                <span class="rate-badge">""" + str(STROMPREIS_PER_KWH) + """ €/kWh</span>
            </div>

            <div style="text-align: center;">
                <div id="statusBadge" class="status-pill status-off">
                    <span class="status-dot"></span>
                    <span id="statusText">Bereit / Aus</span>
                </div>
            </div>

            <!-- AUTO DETECT BANNER -->
            <div class="ai-banner">
                <div class="ai-icon">🔍</div>
                <div>
                    <div class="ai-title">Automatische Erkennung</div>
                    <div class="ai-detected" id="detectedName">Warte auf Start...</div>
                </div>
            </div>

            <!-- LEISTUNG & ZEIT -->
            <div class="grid-2">
                <div class="stat-card stat-watt">
                    <div class="stat-label">Aktuelle Last</div>
                    <div class="stat-val"><span id="watt">0.0</span> <span style="font-size:14px; font-weight:500;">W</span></div>
                    <div class="stat-sub" id="wattSub">Kein Verbrauch</div>
                </div>
                <div class="stat-card stat-time">
                    <div class="stat-label">Laufzeit</div>
                    <div class="stat-val" id="timer">00:00:00</div>
                    <div class="stat-sub">Sekundengenau</div>
                </div>
            </div>

            <!-- ENERGIE & KOSTEN -->
            <div class="grid-2">
                <div class="stat-card">
                    <div class="stat-label">Energie (Wh)</div>
                    <div class="stat-val" style="color:var(--accent-primary);"><span id="wh">0.0</span></div>
                    <div class="bar-wrap"><div class="bar-fill bar-blue" id="whBar"></div></div>
                </div>
                <div class="stat-card stat-cost">
                    <div class="stat-label">Kosten (€)</div>
                    <div class="stat-val"><span id="cost">0,00</span> €</div>
                    <div class="bar-wrap"><div class="bar-fill bar-green" id="costBar"></div></div>
                </div>
            </div>

            <div class="btn-group">
                <button class="btn-start" onclick="sendAction('/start')">▶️ Start / Einschalten</button>
                <button class="btn-stop" onclick="sendAction('/stop')">⏸️ Pause / Ausschalten</button>
                <button class="btn-finish" onclick="logout()">🧾 Beenden & Abrechnen</button>
            </div>
        </div>

        <!-- QUITTUNGS-ANSICHT -->
        <div class="card receipt-card" id="receiptCard">
            <div class="receipt-header">
                <div style="font-size: 38px; margin-bottom: 6px;">🧾</div>
                <div class="title">Verbrauchs-Abrechnung</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Sitzung erfolgreich beendet</div>
            </div>

            <div class="receipt-row"><span>Erkanntes Gerät:</span> <b id="rDevice">-</b></div>
            <div class="receipt-row"><span>Gesamte Zeit:</span> <b id="rTime">00:00:00</b></div>
            <div class="receipt-row"><span>Verbrauch:</span> <b id="rWh">0.00 Wh</b></div>
            <div class="receipt-row"><span>Verbrauch (kWh):</span> <b id="rKwh">0.0000 kWh</b></div>
            <div class="receipt-row receipt-total"><span>Gesamtbetrag:</span> <span id="rCost" style="color: var(--accent-green);">0,00 €</span></div>

            <button class="btn-start" style="margin-top:20px;" onclick="window.location.reload()">🔄 Neue Sitzung</button>
        </div>
    </div>

    <script>
        let isTerminated = false;
        let samples = [];
        let detectedDeviceName = "Wird analysiert...";

        // Automatische Klassifizierung nach Lastprofil
        function classifyLoad(avgWatt, maxWatt) {
            if (avgWatt < 2.0 && maxWatt < 3.0) {
                return "Standby / Leerlauf (< 3W)";
            } else if (avgWatt >= 2.0 && avgWatt < 35.0) {
                return "📱 Smartphone / Tablet / LED-Licht";
            } else if (avgWatt >= 35.0 && avgWatt < 110.0) {
                return "💻 Laptop / TV / Bildschirm";
            } else if (avgWatt >= 110.0 && avgWatt < 260.0) {
                return "🚲 E-Bike Ladegerät (Standard 2A-4A)";
            } else if (avgWatt >= 260.0 && avgWatt < 650.0) {
                return "⚡ E-Bike Schnelllader / PC / Kleingerät";
            } else {
                return "🍳 Großverbraucher / Küchengerät (> 650W)";
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
                
                document.getElementById('rDevice').innerText = detectedDeviceName;
                document.getElementById('rTime').innerText = report.time_formatted;
                document.getElementById('rWh').innerText = report.wh.toFixed(2) + " Wh";
                document.getElementById('rKwh').innerText = report.kwh.toFixed(4) + " kWh";
                document.getElementById('rCost').innerText = report.cost.toFixed(3).replace('.', ',') + " €";
                
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
                
                let currentW = data.watt;
                document.getElementById('watt').innerText = currentW.toFixed(1);
                document.getElementById('wh').innerText = data.wh.toFixed(2);
                document.getElementById('cost').innerText = data.cost.toFixed(3).replace('.', ',');
                
                let sec = data.elapsed_seconds;
                let h = Math.floor(sec / 3600).toString().padStart(2, '0');
                let m = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
                let s = Math.floor(sec % 60).toString().padStart(2, '0');
                document.getElementById('timer').innerText = `${h}:${m}:${s}`;

                let badge = document.getElementById('statusBadge');
                let text = document.getElementById('statusText');
                if (data.active) {
                    badge.className = "status-pill status-on";
                    text.innerText = "Aktiv / Strom fließt";
                    
                    // Automatische Erkennung in den ersten 30 Sekunden
                    if (sec < 30) {
                        samples.push(currentW);
                        let remainingSec = 30 - sec;
                        document.getElementById('detectedName').innerText = `Analysiere Stromprofil... (${remainingSec}s)`;
                    } else {
                        if (samples.length > 0) {
                            let avg = samples.reduce((a, b) => a + b, 0) / samples.length;
                            let max = Math.max(...samples);
                            detectedDeviceName = classifyLoad(avg, max);
                            document.getElementById('detectedName').innerText = detectedDeviceName;
                        }
                    }
                } else {
                    badge.className = "status-pill status-off";
                    text.innerText = "Pausiert / Bereit";
                    if (sec === 0) {
                        document.getElementById('detectedName').innerText = "Warte auf Start...";
                        samples = [];
                    }
                }

                // Subtitle
                document.getElementById('wattSub').innerText = currentW > 0.5 ? "Fließt stabil" : "Keine Last";

                // Balken Animationen
                let whP = Math.min(100, (data.wh / 500.0) * 100);
                document.getElementById('whBar').style.width = whP + '%';

                let costP = Math.min(100, (data.cost / 2.0) * 100);
                document.getElementById('costBar').style.width = costP + '%';

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
