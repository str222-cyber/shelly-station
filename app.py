from flask import Flask, render_template_string, jsonify, session, request
import requests
import time
import uuid

app = Flask(__name__)
app.secret_key = "shelly_smart_unplug_10s_verified_2026"

# --- SHELLY CLOUD DATEN ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

STROMPREIS_PER_KWH = 0.35

global_state = {
    "active_user_id": None,
    "waiting_user_id": None,
    "request_transfer": False,
    "last_seen_active": 0
}

user_sessions = {}

learned_profiles = {
    "lamp": {"name": "💡 Lampe / Beleuchtung", "icon": "💡", "min_w": 2.0, "max_w": 40.0, "is_battery": False},
    "phone": {"name": "📱 Smartphone / Tablet", "icon": "📱", "min_w": 4.0, "max_w": 35.0, "is_battery": True},
    "laptop": {"name": "💻 Laptop / Monitor", "icon": "💻", "min_w": 35.0, "max_w": 100.0, "is_battery": True},
    "ebike_std": {"name": "🚲 E-Bike Ladegerät (Standard)", "icon": "🚲", "min_w": 100.0, "max_w": 250.0, "is_battery": True},
    "ebike_fast": {"name": "⚡ E-Bike Schnelllader / PC", "icon": "⚡", "min_w": 250.0, "max_w": 600.0, "is_battery": True},
    "appliance": {"name": "🍳 Dauerbetrieb / Großgerät", "icon": "🍳", "min_w": 600.0, "max_w": 3500.0, "is_battery": False}
}

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
    <title>Smart Power Hub</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        :root {
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --accent-primary: #2563eb;
            --accent-green: #059669;
            --accent-amber: #d97706;
            --accent-red: #dc2626;
            --border-color: #e2e8f0;
            --shadow-md: 0 10px 25px -5px rgba(15, 23, 42, 0.07), 0 8px 10px -6px rgba(15, 23, 42, 0.04);
        }

        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; padding: 18px 12px; min-height: 100vh; }
        
        .container { width: 100%; max-width: 410px; margin: auto; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 22px 18px; box-shadow: var(--shadow-md); border: 1px solid var(--border-color); text-align: center; }
        
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .title { font-size: 18px; font-weight: 700; color: var(--text-main); letter-spacing: -0.3px; }
        .rate-badge { background: #f1f5f9; color: var(--text-muted); font-size: 12px; padding: 4px 10px; border-radius: 20px; font-weight: 600; }

        .ai-banner {
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
            border: 1px solid var(--border-color);
            border-radius: 18px;
            padding: 14px;
            margin-bottom: 14px;
            text-align: left;
        }
        .ai-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
        .ai-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent-primary); letter-spacing: 0.5px; }
        .btn-edit { background: #e2e8f0; color: var(--text-main); border: none; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 8px; cursor: pointer; }
        
        .ai-body { display: flex; align-items: center; gap: 10px; }
        .ai-icon { font-size: 26px; }
        .ai-detected { font-size: 14px; font-weight: 700; color: var(--text-main); }
        .ai-mode { font-size: 11px; color: var(--text-muted); margin-top: 1px; }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            font-weight: 600;
            padding: 5px 12px;
            border-radius: 30px;
            margin-bottom: 14px;
        }
        .status-on { background: #ecfdf5; color: #065f46; }
        .status-off { background: #f1f5f9; color: var(--text-muted); }
        .status-unplug { background: #fef3c7; color: #92400e; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; }
        .status-on .status-dot { background: var(--accent-green); box-shadow: 0 0 8px rgba(16,185,129,0.6); }
        .status-off .status-dot { background: #94a3b8; }
        .status-unplug .status-dot { background: var(--accent-amber); }

        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .stat-card { background: #f8fafc; border: 1px solid var(--border-color); border-radius: 16px; padding: 12px; text-align: left; }
        .stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.4px; }
        .stat-val { font-size: 20px; font-weight: 700; color: var(--text-main); margin-top: 3px; }
        .stat-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

        .stat-watt .stat-val { color: var(--accent-primary); }
        .stat-cost .stat-val { color: var(--accent-green); }
        .stat-time .stat-val { font-family: monospace; }

        .bar-wrap { margin-top: 6px; width: 100%; height: 5px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
        .bar-fill { height: 100%; width: 0%; border-radius: 3px; transition: width 0.4s ease; }
        .bar-blue { background: var(--accent-primary); }
        .bar-green { background: var(--accent-green); }

        .btn-group { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
        button {
            width: 100%;
            padding: 13px;
            font-size: 15px;
            font-weight: 600;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            transition: transform 0.1s ease;
        }
        button:active { transform: scale(0.98); }
        .btn-start { background: var(--text-main); color: white; }
        .btn-stop { background: #f1f5f9; color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-finish { background: #fee2e2; color: var(--accent-red); }

        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(15, 23, 42, 0.75);
            backdrop-filter: blur(4px);
            z-index: 999;
            padding: 20px;
            align-items: center;
            justify-content: center;
        }
        .modal-box {
            background: white;
            border-radius: 24px;
            padding: 24px 20px;
            text-align: center;
            max-width: 340px;
            width: 100%;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.2);
            animation: popIn 0.3s ease-out;
        }
        @keyframes popIn { from { transform: scale(0.85); opacity: 0; } to { transform: scale(1); opacity: 1; } }

        .device-option-btn {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 10px;
            text-align: left;
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
            width: 100%;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
        }
        .device-option-btn:hover { background: #edf2f7; }

        .receipt-card { display: none; text-align: left; }
        .receipt-header { text-align: center; margin-bottom: 18px; }
        .receipt-row { display: flex; justify-content: space-between; padding: 9px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
        .receipt-total { border-top: 2px solid var(--text-main); border-bottom: none; font-size: 17px; font-weight: 700; margin-top: 10px; padding-top: 12px; }

        .busy-card { display: none; text-align: center; }
    </style>
</head>
<body>
    <!-- MODAL 1: TRAINIEREN -->
    <div id="deviceModal" class="modal-overlay">
        <div class="modal-box">
            <h3 style="margin-bottom: 6px;">Gerät manuell auswählen</h3>
            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 14px;">Wähle den Typ. Das System merkt sich das Lastprofil.</p>
            
            <button class="device-option-btn" onclick="saveDeviceProfile('lamp')">💡 Lampe / Beleuchtung (Dauerbetrieb)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('phone')">📱 Smartphone / Tablet (Akku)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('laptop')">💻 Laptop / Monitor (Akku)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('ebike_std')">🚲 E-Bike Ladegerät Standard (Akku)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('ebike_fast')">⚡ E-Bike Schnelllader (Akku)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('appliance')">🍳 Großgerät / Dauerbetrieb</button>

            <button class="btn-stop" style="margin-top: 6px;" onclick="document.getElementById('deviceModal').style.display='none'">Abbrechen</button>
        </div>
    </div>

    <!-- MODAL 2: FREIGABE-ANFRAGE -->
    <div id="transferModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-amber);">
            <div style="font-size: 40px; margin-bottom: 6px;">👋🔔</div>
            <h3 style="color: var(--accent-amber); margin-bottom: 6px;">Freigabe-Anfrage!</h3>
            <p style="font-size: 13px; color: var(--text-main); margin-bottom: 14px;">
                Ein anderer Nutzer hat den QR-Code gescannt und möchte laden. Bist du fertig?
            </p>
            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="acceptTransfer()">✅ Ja, beenden & freigeben</button>
            <button class="btn-stop" onclick="rejectTransfer()">Ich lade noch weiter</button>
        </div>
    </div>

    <!-- MODAL 3: 100% VOLL ALARM -->
    <div id="fullModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-green);">
            <div style="font-size: 48px; margin-bottom: 8px;">🔋✨</div>
            <h2 style="font-size: 20px; color: var(--accent-green); margin-bottom: 6px;">Akku 100% Vollgeladen!</h2>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">Der Stromfluss wurde automatisch gestoppt.</p>
            <div style="background: #fef3c7; border: 1px solid #fde68a; color: #92400e; font-size: 13px; font-weight: 600; padding: 10px; border-radius: 12px; margin-bottom: 16px;">
                ⚠️ Bitte trenne jetzt dein Ladekabel, damit andere die Station nutzen können!
            </div>
            <button class="btn-start" style="background: var(--accent-green);" onclick="dismissFullAlarm()">🔕 Alarm Stoppen & Quittung</button>
        </div>
    </div>

    <div class="container">
        <!-- BESETZT-KARTE -->
        <div class="card busy-card" id="busyCard">
            <div style="font-size: 48px; margin-bottom: 10px;">⏳🔒</div>
            <div class="title" style="margin-bottom: 6px;">Steckdose aktuell belegt</div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">
                Ein anderer Nutzer lädt gerade aktiv an dieser Steckdose.
            </p>
            <div style="background: #f1f5f9; padding: 12px; border-radius: 14px; margin-bottom: 16px; text-align: left; font-size: 13px;">
                Aktuelle Leistung: <b id="busyWatt">0.0 W</b><br>
                Laufzeit: <b id="busyTimer">00:00:00</b>
            </div>
            <button class="btn-start" style="background: var(--accent-primary);" onclick="requestSlot()">🔔 Nutzer anfragen (Bescheid geben)</button>
            <div id="requestSentText" style="display:none; color: var(--accent-green); font-size: 12px; font-weight: 600; margin-top: 10px;">
                ✅ Anfrage gesendet! Der aktive Nutzer wurde benachrichtigt.
            </div>
        </div>

        <!-- HAUPTKARTE -->
        <div class="card" id="mainCard">
            <div class="header">
                <span class="title">⚡ Smart Power Hub</span>
                <span class="rate-badge">""" + str(STROMPREIS_PER_KWH) + """ €/kWh</span>
            </div>

            <div>
                <div id="statusBadge" class="status-pill status-off">
                    <span class="status-dot"></span>
                    <span id="statusText">Bereit / Aus</span>
                </div>
            </div>

            <div class="ai-banner">
                <div class="ai-header">
                    <span class="ai-title">Erkanntes Gerät</span>
                    <button class="btn-edit" onclick="document.getElementById('deviceModal').style.display='flex'">✏️ Ändern / Trainieren</button>
                </div>
                <div class="ai-body">
                    <div class="ai-icon" id="devIcon">🔍</div>
                    <div>
                        <div class="ai-detected" id="detectedName">Warte auf Start...</div>
                        <div class="ai-mode" id="detectedMode">Check-Intervall: 60s</div>
                    </div>
                </div>
            </div>

            <div class="grid-2">
                <div class="stat-card stat-watt">
                    <div class="stat-label">Leistung</div>
                    <div class="stat-val"><span id="watt">0.0</span> <span style="font-size:13px; font-weight:500;">W</span></div>
                    <div class="stat-sub" id="wattSub">Kein Strom</div>
                </div>
                <div class="stat-card stat-time">
                    <div class="stat-label">Laufzeit</div>
                    <div class="stat-val" id="timer">00:00:00</div>
                    <div class="stat-sub" id="nextCheckSub">Check in --s</div>
                </div>
            </div>

            <div class="grid-2">
                <div class="stat-card">
                    <div class="stat-label">Verbrauch (Wh)</div>
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
                <button class="btn-start" id="mainStartBtn" onclick="startSession()">▶️ Start / Fortsetzen</button>
                <button class="btn-stop" onclick="sendAction('/stop')">⏸️ Pause</button>
                <button class="btn-finish" onclick="logout()">🧾 Beenden & Abrechnen</button>
            </div>
        </div>

        <!-- QUITTUNG -->
        <div class="card receipt-card" id="receiptCard">
            <div class="receipt-header">
                <div style="font-size: 40px; margin-bottom: 4px;">🧾</div>
                <div class="title">Lade- & Stromquittung</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 3px;">Sitzung erfolgreich beendet</div>
            </div>

            <div class="receipt-row"><span>Gerät:</span> <b id="rDevice">-</b></div>
            <div class="receipt-row"><span>Betriebsart:</span> <b id="rMode">-</b></div>
            <div class="receipt-row"><span>Gesamte Zeit:</span> <b id="rTime">00:00:00</b></div>
            <div class="receipt-row"><span>Verbrauch:</span> <b id="rWh">0.00 Wh</b></div>
            <div class="receipt-row"><span>Verbrauch (kWh):</span> <b id="rKwh">0.0000 kWh</b></div>
            <div class="receipt-row receipt-total"><span>Gesamtbetrag:</span> <span id="rCost" style="color: var(--accent-green);">0,00 €</span></div>

            <button class="btn-start" style="margin-top:20px;" onclick="window.location.reload()">🔄 Neue Sitzung</button>
        </div>
    </div>

    <script>
        let isTerminated = false;
        let alarmInterval = null;
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

        let windowSamples = [];
        let historicalPeakWatt = 0.0;
        let activePowerObserved = false;
        let isBatteryDevice = false;
        let manualOverride = false;
        let unplugCounter = 0; // Zähler für 10-Sekunden-Filter
        let isUnplugged = false;
        let currentClassification = { name: "Warte auf Messung...", icon: "🔍", isBattery: false };

        function playContinuousTone() {
            try {
                if (audioCtx.state === 'suspended') { audioCtx.resume(); }
                let osc = audioCtx.createOscillator();
                let gain = audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.value = 880;
                gain.gain.value = 0.25;
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start();
                setTimeout(() => osc.stop(), 350);
            } catch(e) {}
        }

        function startAudioAlert() {
            if (alarmInterval) return;
            playContinuousTone();
            alarmInterval = setInterval(playContinuousTone, 700);
        }

        function stopAudioAlert() {
            if (alarmInterval) {
                clearInterval(alarmInterval);
                alarmInterval = null;
            }
        }

        function startSession() {
            if (audioCtx.state === 'suspended') { audioCtx.resume(); }
            isUnplugged = false;
            unplugCounter = 0;
            sendAction('/start');
        }

        async function sendAction(url, data={}) {
            try {
                let res = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                return await res.json();
            } catch(e) { return {}; }
        }

        async function saveDeviceProfile(key) {
            manualOverride = true;
            document.getElementById('deviceModal').style.display = 'none';
            await sendAction('/train_profile', { key: key, peak_w: historicalPeakWatt });
            
            let prof = {
                'lamp': { name: "💡 Lampe / Beleuchtung", icon: "💡", isBattery: false },
                'phone': { name: "📱 Smartphone / Tablet", icon: "📱", isBattery: true },
                'laptop': { name: "💻 Laptop / Monitor", icon: "💻", isBattery: true },
                'ebike_std': { name: "🚲 E-Bike Ladegerät (Standard)", icon: "🚲", isBattery: true },
                'ebike_fast': { name: "⚡ E-Bike Schnelllader / PC", icon: "⚡", isBattery: true },
                'appliance': { name: "🍳 Großgerät / Dauerbetrieb", icon: "🍳", isBattery: false }
            }[key];

            currentClassification = prof;
            isBatteryDevice = prof.isBattery;
            document.getElementById('devIcon').innerText = prof.icon;
            document.getElementById('detectedName').innerText = prof.name + " (Bestätigt)";
            document.getElementById('detectedMode').innerText = isBatteryDevice ? "🔋 Akku (Auto-Stop bei 100%)" : "💡 Dauerbetrieb (Kein Auto-Stop)";
        }

        async function requestSlot() {
            await sendAction('/request_transfer');
            document.getElementById('requestSentText').style.display = 'block';
        }

        async function acceptTransfer() {
            document.getElementById('transferModal').style.display = 'none';
            await logout();
        }

        async function rejectTransfer() {
            document.getElementById('transferModal').style.display = 'none';
            await sendAction('/reject_transfer');
        }

        async function dismissFullAlarm() {
            stopAudioAlert();
            document.getElementById('fullModal').style.display = 'none';
            await logout();
        }

        function evaluateProfile(avgW, peakW) {
            if (avgW < 2.0 && peakW < 3.0) {
                return { name: "Standby / Leerlauf", icon: "🔌", isBattery: false };
            } else if (avgW >= 2.0 && avgW < 35.0) {
                return { name: "💡 Lampe / 📱 Smartphone", icon: "💡", isBattery: false };
            } else if (avgW >= 35.0 && avgW < 100.0) {
                return { name: "💻 Laptop / Monitor", icon: "💻", isBattery: true };
            } else if (avgW >= 100.0 && avgW < 250.0) {
                return { name: "🚲 E-Bike Ladegerät (Standard)", icon: "🚲", isBattery: true };
            } else if (avgW >= 250.0 && avgW < 600.0) {
                return { name: "⚡ E-Bike Schnelllader / PC", icon: "⚡", isBattery: true };
            } else {
                return { name: "🍳 Großverbraucher / Dauerbetrieb", icon: "🍳", isBattery: false };
            }
        }

        async function logout() {
            isTerminated = true;
            try {
                let report = await sendAction('/logout');
                
                document.getElementById('rDevice').innerText = currentClassification.icon + " " + currentClassification.name;
                document.getElementById('rMode').innerText = currentClassification.isBattery ? "Akku-Ladeüberwachung" : "Dauerbetrieb";
                document.getElementById('rTime').innerText = report.time_formatted;
                document.getElementById('rWh').innerText = report.wh.toFixed(2) + " Wh";
                document.getElementById('rKwh').innerText = report.kwh.toFixed(4) + " kWh";
                document.getElementById('rCost').innerText = report.cost.toFixed(3).replace('.', ',') + " €";
                
                document.getElementById('mainCard').style.display = 'none';
                document.getElementById('busyCard').style.display = 'none';
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
                
                if (data.is_busy_for_other) {
                    document.getElementById('mainCard').style.display = 'none';
                    document.getElementById('busyCard').style.display = 'block';
                    document.getElementById('busyWatt').innerText = data.watt.toFixed(1) + " W";
                    let sec = data.elapsed_seconds;
                    let h = Math.floor(sec / 3600).toString().padStart(2, '0');
                    let m = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
                    let s = Math.floor(sec % 60).toString().padStart(2, '0');
                    document.getElementById('busyTimer').innerText = `${h}:${m}:${s}`;
                    return;
                } else {
                    document.getElementById('busyCard').style.display = 'none';
                    document.getElementById('mainCard').style.display = 'block';
                }

                if (data.transfer_requested) {
                    document.getElementById('transferModal').style.display = 'flex';
                }

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
                let startBtn = document.getElementById('mainStartBtn');
                
                if (data.active) {
                    windowSamples.push(currentW);
                    if (currentW > historicalPeakWatt) historicalPeakWatt = currentW;

                    if (currentW > 3.0) {
                        activePowerObserved = true;
                    }

                    // --- 10-SEKUNDEN UNPLUG ERKENNUNGS-FILTER ---
                    if (activePowerObserved && currentW < 0.2) {
                        unplugCounter++;
                        document.getElementById('wattSub').innerText = `Verbindung getrennt? Prüfe (${unplugCounter}/10s)...`;
                        
                        // Erst wenn 10 aufeinanderfolgende Sekunden 0 W anliegen:
                        if (unplugCounter >= 10) {
                            isUnplugged = true;
                            unplugCounter = 0;
                            await sendAction('/stop');
                            badge.className = "status-pill status-unplug";
                            text.innerText = "🔌 Kabel ausgesteckt – Strom pausiert";
                            document.getElementById('wattSub').innerText = "Warte auf Wiedereinstecken & Start";
                            startBtn.innerText = "▶️ Kabel wieder drin? Weiterladen";
                            return;
                        }
                    } else {
                        unplugCounter = 0; // Reset, sobald auch nur 1 Watt fließt (Netzschwankung)
                        document.getElementById('wattSub').innerText = currentW > 0.5 ? "Fließt stabil" : "Keine Last";
                    }

                    badge.className = "status-pill status-on";
                    text.innerText = "Aktiv / Strom fließt";
                    startBtn.innerText = "▶️ Läuft bereits";

                    // Re-Klassifizierung alle 60s
                    let secInMinute = sec % 60;
                    let nextCheck = 60 - secInMinute;
                    document.getElementById('nextCheckSub').innerText = `Check in ${nextCheck}s`;

                    if (!manualOverride) {
                        if (sec >= 30 && (sec === 30 || secInMinute === 0)) {
                            let avg = windowSamples.reduce((a, b) => a + b, 0) / windowSamples.length;
                            let peak = Math.max(...windowSamples);
                            
                            currentClassification = evaluateProfile(avg, peak);
                            isBatteryDevice = currentClassification.isBattery;

                            document.getElementById('devIcon').innerText = currentClassification.icon;
                            document.getElementById('detectedName').innerText = currentClassification.name;
                            document.getElementById('detectedMode').innerText = isBatteryDevice 
                                ? "🔋 Akku (Auto-Stop bei 100%)" 
                                : "💡 Dauerbetrieb (Kein Auto-Stop)";
                            
                            windowSamples = [];
                        } else if (sec < 30) {
                            document.getElementById('detectedName').innerText = `Analysiere... (${30 - sec}s)`;
                        }
                    }

                    // --- 100% VOLLGELADEN BEI AKKUS ---
                    // Typisch: Netzteil bleibt in Steckdose (zieht 0,4W - 2,0W), lädt aber nicht mehr aktiv
                    if (isBatteryDevice && historicalPeakWatt > 8.0 && currentW >= 0.3 && currentW < 2.2 && data.wh > 2.0) {
                        await sendAction('/stop');
                        document.getElementById('fullModal').style.display = 'flex';
                        startAudioAlert();
                    }

                } else {
                    if (isUnplugged) {
                        badge.className = "status-pill status-unplug";
                        text.innerText = "🔌 Kabel ausgesteckt – Strom pausiert";
                        startBtn.innerText = "▶️ Kabel wieder drin? Fortsetzen";
                    } else {
                        badge.className = "status-pill status-off";
                        text.innerText = "Pausiert / Bereit";
                        startBtn.innerText = "▶️ Start / Fortsetzen";
                    }
                    
                    if (sec === 0) {
                        document.getElementById('detectedName').innerText = "Warte auf Start...";
                        document.getElementById('devIcon').innerText = "🔍";
                        windowSamples = [];
                        historicalPeakWatt = 0;
                        activePowerObserved = false;
                        manualOverride = false;
                        unplugCounter = 0;
                    }
                }

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
    return user_sessions[uid], uid

@app.route('/')
def home():
    get_user_data()
    return render_template_string(HTML)

@app.route('/start', methods=['POST', 'GET'])
def start():
    u, uid = get_user_data()
    global_state["active_user_id"] = uid
    global_state["last_seen_active"] = time.time()
    u["active"] = True
    u["last_check"] = time.time()
    cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST', 'GET'])
def stop():
    u, _ = get_user_data()
    u["active"] = False
    u["last_check"] = None
    u["current_watt"] = 0.0
    cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/train_profile', methods=['POST'])
def train_profile():
    data = request.get_json() or {}
    key = data.get("key")
    peak_w = float(data.get("peak_w", 0.0))
    if key in learned_profiles and peak_w > 0:
        learned_profiles[key]["max_w"] = max(learned_profiles[key]["max_w"], peak_w * 1.2)
    return jsonify({"status": "trained"})

@app.route('/request_transfer', methods=['POST'])
def request_transfer():
    global_state["request_transfer"] = True
    return jsonify({"status": "requested"})

@app.route('/reject_transfer', methods=['POST'])
def reject_transfer():
    global_state["request_transfer"] = False
    return jsonify({"status": "rejected"})

@app.route('/logout', methods=['POST', 'GET'])
def logout():
    u, uid = get_user_data()
    u["active"] = False
    cloud_control(turn_on=False)

    if global_state["active_user_id"] == uid:
        global_state["active_user_id"] = None
        global_state["request_transfer"] = False

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

    if uid in user_sessions:
        del user_sessions[uid]
    session.clear()

    return jsonify(report)

@app.route('/status')
def status():
    u, uid = get_user_data()
    active_uid = global_state.get("active_user_id")
    
    is_busy = False
    if active_uid and active_uid != uid and user_sessions.get(active_uid, {}).get("active", False):
        is_busy = True

    if u["active"]:
        now = time.time()
        w = cloud_get_watt()
        u["current_watt"] = w
        global_state["last_seen_active"] = now
        
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
        "elapsed_seconds": u["total_seconds"],
        "is_busy_for_other": is_busy,
        "transfer_requested": global_state.get("request_transfer", False) and (active_uid == uid)
    })

if __name__ == '__main__':
    app.run()
