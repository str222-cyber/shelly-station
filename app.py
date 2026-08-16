from flask import Flask, render_template_string, jsonify, session, request, send_file, redirect
import requests
import time
import threading
import uuid
import secrets
import smtplib
import io
from functools import wraps
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from weasyprint import HTML

app = Flask(__name__)

# --- SICHERHEITS- & SESSION-KONFIGURATION ---
app.secret_key = secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=14400  # 4 Stunden maximale Gültigkeit
)

# GEHEIMER VOR-ORT TOKEN (Muss exakt mit dem QR-Code übereinstimmen)
STATION_PHYSICAL_TOKEN = "SEC-STATION-2026-X99Q-ALPHA-77"

# --- SHELLY CLOUD KONFIGURATION ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

STROMPREIS_PER_KWH = 0.35  # 0,35 € pro kWh

# OPTIONALE SMTP-DATEN FÜR E-MAIL-VERSAND
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = ""

global_state = {
    "active_user_id": None,
    "transfer_requested": False,
    "transfer_requester_id": None,
    "last_watt": 0.0,
    "last_amp": 0.0,
    "last_volt": 230.0
}

user_sessions = {}

# Geräteprofile mit Lastbereichen und Akkukapazitäten
DEVICE_PROFILES = {
    "lamp": {"name": "💡 Lampe / Beleuchtung", "icon": "💡", "is_battery": False, "capacity_wh": 0},
    "phone": {"name": "📱 Smartphone / Tablet", "icon": "📱", "is_battery": True, "capacity_wh": 25.0},
    "laptop": {"name": "💻 Laptop / Monitor", "icon": "💻", "is_battery": True, "capacity_wh": 65.0},
    "ebike_std": {"name": "🚲 E-Bike Akku Standard", "icon": "🚲", "is_battery": True, "capacity_wh": 500.0},
    "ebike_fast": {"name": "⚡ E-Bike Schnelllader / PC", "icon": "⚡", "is_battery": True, "capacity_wh": 750.0},
    "appliance": {"name": "🍳 Großgerät / Dauerbetrieb", "icon": "🍳", "is_battery": False, "capacity_wh": 0}
}

@app.after_request
def add_security_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

# SICHERHEITS-GUARD: Blockiert Aufrufe ohne Vor-Ort-Authentifizierung
def require_physical_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("authenticated_on_site") or session.get("station_token") != STATION_PHYSICAL_TOKEN:
            return jsonify({"status": "unauthorized", "message": "Bitte QR-Code vor Ort scannen."}), 401
        return f(*args, **kwargs)
    return decorated_function

# ASYNCHRONES SCHALTEN (Kein Blockieren der HTTP-Requests)
def async_cloud_control(turn_on=True):
    def _worker():
        turn_str = "on" if turn_on else "off"
        payload = {"auth_key": AUTH_KEY, "id": DEVICE_ID, "turn": turn_str, "channel": 0}
        try:
            requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control", data=payload, timeout=2.5)
        except Exception:
            pass
        rpc_payload = {
            "auth_key": AUTH_KEY,
            "id": DEVICE_ID,
            "method": "Switch.Set",
            "params": {"id": 0, "on": turn_on}
        }
        try:
            requests.post(f"{SHELLY_CLOUD_URL}/device/rpc", json=rpc_payload, timeout=2.5)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

# AUTOMATISCHE LAST-ANALYSE
def classify_power_profile(avg_w, peak_w):
    if avg_w < 3.0 and peak_w < 6.0:
        return "lamp"
    elif 3.0 <= avg_w < 25.0:
        return "phone"
    elif 25.0 <= avg_w < 85.0:
        return "laptop"
    elif 85.0 <= avg_w < 240.0:
        return "ebike_std"
    elif 240.0 <= avg_w < 650.0:
        return "ebike_fast"
    else:
        return "appliance"

# SERVER-SEITIGER HINTERGRUND-MESSWORKER (Autarke Messung unabhängig vom Handy-Display)
def background_sensor_poller():
    last_poll = time.time()
    while True:
        try:
            now = time.time()
            dt = now - last_poll
            last_poll = now

            active_uid = global_state.get("active_user_id")
            if active_uid and user_sessions.get(active_uid, {}).get("active", False):
                u = user_sessions[active_uid]
                
                payload = {"auth_key": AUTH_KEY, "id": DEVICE_ID}
                res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data=payload, timeout=2.0).json()
                
                watt = 0.0
                amp = 0.0
                volt = 230.0

                if res.get("isok"):
                    status = res.get("data", {}).get("device_status", {})
                    if "switch:0" in status:
                        sw = status["switch:0"]
                        watt = float(sw.get("apower", 0.0))
                        amp = float(sw.get("current", 0.0))
                        volt = float(sw.get("voltage", 230.0))
                    elif "meters" in status and len(status["meters"]) > 0:
                        m = status["meters"][0]
                        watt = float(m.get("power", 0.0))
                        amp = float(m.get("current", 0.0)) if "current" in m else (watt / 230.0 if watt > 0 else 0.0)
                        volt = float(m.get("voltage", 230.0)) if "voltage" in m else 230.0

                global_state["last_watt"] = watt
                global_state["last_amp"] = amp
                global_state["last_volt"] = volt

                u["current_watt"] = watt
                u["current_ampere"] = amp
                u["current_voltage"] = volt

                # Autarke Zeit- und Energieintegration
                if dt > 0:
                    u["total_seconds"] += dt
                    u["total_kwh"] += (watt * dt) / 3600000.0

                # 30-Sekunden-Lernphase sammeln
                if u["total_seconds"] < 30 and not u["manually_selected"]:
                    u["analysis_samples"].append(watt)
                elif u["total_seconds"] >= 30 and not u["analysis_completed"] and not u["manually_selected"]:
                    if len(u["analysis_samples"]) > 0:
                        avg_w = sum(u["analysis_samples"]) / len(u["analysis_samples"])
                        peak_w = max(u["analysis_samples"])
                        u["device_key"] = classify_power_profile(avg_w, peak_w)
                    u["analysis_completed"] = True

                # Akku 100% Erkennung
                prof = DEVICE_PROFILES.get(u["device_key"], {})
                if prof.get("is_battery", False):
                    if watt > 5.0:
                        u["had_charging_phase"] = True
                    
                    # Wenn Akku voll (Standbylast < 1.8W nach aktiver Phase)
                    if u["had_charging_phase"] and 0.4 <= watt < 1.8 and (u["total_kwh"] * 1000.0) > 3.0:
                        u["battery_full_triggered"] = True
                        u["active"] = False
                        async_cloud_control(turn_on=False)
            else:
                global_state["last_watt"] = 0.0
                global_state["last_amp"] = 0.0
        except Exception:
            pass
        time.sleep(1.2)

threading.Thread(target=background_sensor_poller, daemon=True).start()

def generate_pdf_invoice(report_data):
    html_invoice = f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
    <meta charset="utf-8">
    <style>
    @page {{ size: A4; margin: 20mm 15mm; background-color: #ffffff; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 0; padding: 0; }}
    .header {{ border-bottom: 2px solid #2563eb; padding-bottom: 15px; margin-bottom: 25px; }}
    .brand {{ font-size: 22pt; font-weight: 800; color: #2563eb; }}
    .meta {{ font-size: 10pt; color: #64748b; margin-top: 5px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
    th {{ background-color: #f1f5f9; color: #334155; text-align: left; padding: 10px; font-size: 10pt; border-bottom: 1px solid #cbd5e1; }}
    td {{ padding: 10px; font-size: 10pt; border-bottom: 1px solid #e2e8f0; }}
    tr:nth-child(even) {{ background-color: #f8fafc; }}
    .total {{ margin-top: 25px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 15px; text-align: right; }}
    .total-title {{ font-size: 11pt; color: #166534; font-weight: 600; }}
    .total-val {{ font-size: 20pt; font-weight: 800; color: #15803d; }}
    .footer {{ margin-top: 50px; font-size: 9pt; color: #94a3b8; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 15px; }}
    </style>
    </head>
    <body>
    <div class="header">
        <div class="brand">⚡ Smart Power Hub</div>
        <div class="meta">Lade- & Stromquittung • Vorgangs-ID: {report_data.get('invoice_id')} • Datum: {time.strftime('%d.%m.%Y %H:%M')}</div>
    </div>
    <table>
        <thead>
            <tr><th>Position / Parameter</th><th>Wert</th><th>Einheit</th></tr>
        </thead>
        <tbody>
            <tr><td><strong>Angeschlossenes Gerät</strong></td><td>{report_data.get('device')}</td><td>-</td></tr>
            <tr><td><strong>Betriebsmodus</strong></td><td>{report_data.get('mode')}</td><td>-</td></tr>
            <tr><td><strong>Gesamte Nutzungsdauer</strong></td><td>{report_data.get('time_formatted')}</td><td>hh:mm:ss</td></tr>
            <tr><td><strong>Verbrauchte Energie (Wh)</strong></td><td>{report_data.get('wh'):.4f}</td><td>Wh</td></tr>
            <tr><td><strong>Verbrauchte Energie (kWh)</strong></td><td>{report_data.get('kwh'):.6f}</td><td>kWh</td></tr>
            <tr><td><strong>Arbeitspreis</strong></td><td>{STROMPREIS_PER_KWH:.3f}</td><td>€ / kWh</td></tr>
        </tbody>
    </table>
    <div class="total">
        <div class="total-title">Gesamtbetrag</div>
        <div class="total-val">{report_data.get('cost'):.5f} €</div>
    </div>
    <div class="footer">Vielen Dank für die Nutzung der Smart Power Station!</div>
    </body>
    </html>
    """
    pdf_buffer = io.BytesIO()
    HTML(string=html_invoice).write_pdf(pdf_buffer)
    pdf_buffer.seek(0)
    return pdf_buffer

HTML_ACCESS_DENIED = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Zugriff Verweigert</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; padding: 20px; }
        .box { background: #1e293b; padding: 30px; border-radius: 20px; border: 1px solid #334155; max-width: 360px; }
        .icon { font-size: 50px; margin-bottom: 15px; }
        h2 { font-size: 20px; margin-bottom: 10px; color: #f87171; }
        p { font-size: 14px; color: #94a3b8; line-height: 1.5; }
    </style>
</head>
<body>
    <div class="box">
        <div class="icon">🔒🚫</div>
        <h2>Sicherheits-Sperre</h2>
        <p>Ein direkter Web-Zugriff ist aus Sicherheitsgründen gesperrt.</p>
        <p style="margin-top: 12px; color: #e2e8f0; font-weight: 600;">Bitte scanne den offiziellen QR-Code direkt an der Ladestation vor Ort.</p>
    </div>
</body>
</html>
"""

HTML_PAGE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Power Hub • Authentifiziert</title>
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
            --accent-cyan: #0891b2;
            --border-color: #e2e8f0;
            --shadow-md: 0 10px 25px -5px rgba(15, 23, 42, 0.07), 0 8px 10px -6px rgba(15, 23, 42, 0.04);
        }

        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; padding: 18px 12px; min-height: 100vh; }
        
        .container { width: 100%; max-width: 420px; margin: auto; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 22px 18px; box-shadow: var(--shadow-md); border: 1px solid var(--border-color); text-align: center; }
        
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .title { font-size: 18px; font-weight: 700; color: var(--text-main); letter-spacing: -0.3px; }
        .rate-badge { background: #f1f5f9; color: var(--text-muted); font-size: 12px; padding: 4px 10px; border-radius: 20px; font-weight: 600; }

        .security-badge {
            background: #ecfdf5;
            border: 1px solid #a7f3d0;
            border-radius: 12px;
            padding: 5px 10px;
            font-size: 11px;
            color: #065f46;
            font-weight: 600;
            margin-bottom: 12px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .ai-banner {
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
            border: 1px solid var(--border-color);
            border-radius: 18px;
            padding: 12px 14px;
            margin-bottom: 12px;
            text-align: left;
            min-height: 68px;
        }
        .ai-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
        .ai-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent-primary); letter-spacing: 0.5px; }
        .btn-edit { background: #e2e8f0; color: var(--text-main); border: none; font-size: 11px; font-weight: 600; padding: 4px 9px; border-radius: 8px; cursor: pointer; }
        
        .ai-body { display: flex; align-items: center; gap: 10px; }
        .ai-icon { font-size: 26px; }
        .ai-detected { font-size: 14px; font-weight: 700; color: var(--text-main); }
        .ai-mode { font-size: 11px; color: var(--text-muted); margin-top: 1px; }

        /* AKKU LADEBALKEN & PROGNOSE */
        .battery-card {
            display: none;
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            border-radius: 16px;
            padding: 12px 14px;
            margin-bottom: 12px;
            text-align: left;
        }
        .battery-header { display: flex; justify-content: space-between; font-size: 12px; font-weight: 700; color: #166534; margin-bottom: 6px; }
        .battery-bar-wrap { width: 100%; height: 10px; background: #dcfce7; border-radius: 5px; overflow: hidden; border: 1px solid #86efac; }
        .battery-bar-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #22c55e, #16a34a); transition: width 0.5s ease; }
        .battery-meta { display: flex; justify-content: space-between; font-size: 11px; color: #15803d; margin-top: 5px; font-weight: 600; }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            font-weight: 600;
            padding: 5px 12px;
            border-radius: 30px;
            margin-bottom: 12px;
            height: 28px;
        }
        .status-on { background: #ecfdf5; color: #065f46; }
        .status-off { background: #f1f5f9; color: var(--text-muted); }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; }
        .status-on .status-dot { background: var(--accent-green); box-shadow: 0 0 8px rgba(16,185,129,0.6); }
        .status-off .status-dot { background: #94a3b8; }

        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .stat-card {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 12px;
            text-align: left;
            min-height: 80px;
        }
        .stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.4px; }
        .stat-val {
            font-size: 18px;
            font-weight: 700;
            color: var(--text-main);
            margin-top: 3px;
            font-variant-numeric: tabular-nums;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            white-space: nowrap;
        }
        .stat-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; font-variant-numeric: tabular-nums; }

        .stat-watt .stat-val { color: var(--accent-primary); }
        .stat-cost .stat-val { color: var(--accent-green); }
        .stat-volt .stat-val { color: var(--accent-amber); }
        .stat-amp .stat-val { color: var(--accent-cyan); }

        .btn-group { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
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
            padding: 11px;
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

        .email-box {
            margin-top: 18px;
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 14px;
        }
        .email-input {
            width: 100%;
            padding: 11px;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            font-size: 14px;
            margin-bottom: 8px;
            box-sizing: border-box;
        }

        .busy-card { display: none; text-align: center; }
    </style>
</head>
<body>
    <div id="deviceModal" class="modal-overlay">
        <div class="modal-box">
            <h3 style="margin-bottom: 6px;">Gerät manuell festlegen</h3>
            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 14px;">Wähle dein Gerät aus. Die Anzeige passt sich automatisch an.</p>
            <button class="device-option-btn" onclick="saveDeviceProfile('lamp')">💡 Lampe / Beleuchtung (Dauerbetrieb)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('phone')">📱 Smartphone / Tablet (Akku ~25 Wh)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('laptop')">💻 Laptop / Monitor (Akku ~65 Wh)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('ebike_std')">🚲 E-Bike Akku Standard (Akku ~500 Wh)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('ebike_fast')">⚡ E-Bike Schnelllader (Akku ~750 Wh)</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('appliance')">🍳 Großgerät / Dauerbetrieb</button>
            <button class="btn-stop" style="margin-top: 6px;" onclick="document.getElementById('deviceModal').style.display='none'">Abbrechen</button>
        </div>
    </div>

    <!-- ANFRAGE MODAL BEI NUTZER 1 -->
    <div id="transferModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-amber);">
            <div style="font-size: 40px; margin-bottom: 6px;">👋🔔</div>
            <h3 style="color: var(--accent-amber); margin-bottom: 6px;">Freigabe-Anfrage</h3>
            <p style="font-size: 13px; color: var(--text-main); margin-bottom: 14px;">
                Ein anderer Nutzer hat den QR-Code vor Ort gescannt und möchte laden. Möchtest du die Steckdose jetzt überlassen?
            </p>
            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="acceptTransfer()">✅ Ja, beenden & freigeben</button>
            <button class="btn-stop" onclick="rejectTransfer()">Nein, ich nutze weiter</button>
        </div>
    </div>

    <div class="container">
        <!-- BESETZT-KARTE -->
        <div class="card busy-card" id="busyCard">
            <div style="font-size: 48px; margin-bottom: 10px;">⏳🔒</div>
            <div class="title" style="margin-bottom: 6px;">Steckdose aktuell belegt</div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">
                Ein anderer authentifizierter Nutzer lädt gerade aktiv an dieser Station.
            </p>
            <div style="background: #f1f5f9; padding: 12px; border-radius: 14px; margin-bottom: 16px; text-align: left; font-size: 13px;">
                Aktuelle Leistung: <b id="busyWatt">0.000 W</b>
            </div>
            <button class="btn-start" id="btnRequestSlot" style="background: var(--accent-primary);" onclick="requestSlot()">🔔 Nutzer anfragen (Bescheid geben)</button>
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
                <span class="security-badge">🛡️ Gesicherte Vor-Ort Sitzung</span><br>
                <div id="statusBadge" class="status-pill status-off">
                    <span class="status-dot"></span>
                    <span id="statusText">Bereit / Aus</span>
                </div>
            </div>

            <!-- GERÄTE ERKENNUNG -->
            <div class="ai-banner">
                <div class="ai-header">
                    <span class="ai-title" id="aiStatusTitle">Erkennung</span>
                    <button class="btn-edit" onclick="document.getElementById('deviceModal').style.display='flex'">✏️ Ändern</button>
                </div>
                <div class="ai-body">
                    <div class="ai-icon" id="devIcon">🔍</div>
                    <div>
                        <div class="ai-detected" id="detectedName">Warte auf Start...</div>
                        <div class="ai-mode" id="detectedMode">Automatische Lastanalyse (30s)</div>
                    </div>
                </div>
            </div>

            <!-- AKKU LADESTAND & RESTZEIT -->
            <div class="battery-card" id="batteryCard">
                <div class="battery-header">
                    <span>🔋 Geschätzter Ladefortschritt</span>
                    <span id="batteryPercentText">0%</span>
                </div>
                <div class="battery-bar-wrap">
                    <div class="battery-bar-fill" id="batteryBarFill"></div>
                </div>
                <div class="battery-meta">
                    <span id="batteryWhLoaded">0.0 / 0 Wh</span>
                    <span id="batteryTimeRemaining">Restzeit: --</span>
                </div>
            </div>

            <!-- NETZDATEN -->
            <div class="grid-2">
                <div class="stat-card stat-volt">
                    <div class="stat-label">Netzspannung (U)</div>
                    <div class="stat-val"><span id="volt">230.0</span> V</div>
                    <div class="stat-sub">Wechselspannung</div>
                </div>
                <div class="stat-card stat-amp">
                    <div class="stat-label">Stromstärke (I)</div>
                    <div class="stat-val"><span id="amp">0.000</span> A</div>
                    <div class="stat-sub"><span id="milliAmp">0</span> mA</div>
                </div>
            </div>

            <!-- LEISTUNG & LAUFZEIT -->
            <div class="grid-2">
                <div class="stat-card stat-watt">
                    <div class="stat-label">Wirkleistung (P)</div>
                    <div class="stat-val"><span id="watt">0.000</span> W</div>
                    <div class="stat-sub" id="wattSub">Kein Strom</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Laufzeit</div>
                    <div class="stat-val" id="timer">00:00:00</div>
                    <div class="stat-sub">Autarke Server-Messung</div>
                </div>
            </div>

            <!-- ENERGIE & KOSTEN -->
            <div class="grid-2">
                <div class="stat-card">
                    <div class="stat-label">Verbrauch</div>
                    <div class="stat-val" style="color:var(--accent-primary); font-family:monospace; font-size:17px;"><span id="wh">0.0000</span> Wh</div>
                    <div class="stat-sub"><span id="mwh">0.0</span> mWh</div>
                </div>
                <div class="stat-card stat-cost">
                    <div class="stat-label">Kosten (€)</div>
                    <div class="stat-val"><span id="cost">0.00000</span> €</div>
                    <div class="stat-sub"><span id="microCost">0.00</span> Cent</div>
                </div>
            </div>

            <div class="btn-group">
                <button class="btn-start" id="mainStartBtn" onclick="startSession()">▶️ Start / Fortsetzen</button>
                <button class="btn-stop" onclick="pauseSession()">⏸️ Pause</button>
                <button class="btn-finish" onclick="logout()">🧾 Beenden & Abrechnen</button>
            </div>
        </div>

        <!-- QUITTUNG -->
        <div class="card receipt-card" id="receiptCard">
            <div class="receipt-header">
                <div style="font-size: 40px; margin-bottom: 4px;">🧾</div>
                <div class="title">Lade- & Stromquittung</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 3px;">Sitzung erfolgreich beendet & Steckdose freigegeben</div>
            </div>

            <div class="receipt-row"><span>Gerät:</span> <b id="rDevice">-</b></div>
            <div class="receipt-row"><span>Betriebsart:</span> <b id="rMode">-</b></div>
            <div class="receipt-row"><span>Gesamte Zeit:</span> <b id="rTime">00:00:00</b></div>
            <div class="receipt-row"><span>Verbrauch (Wh):</span> <b id="rWh">0.0000 Wh</b></div>
            <div class="receipt-row"><span>Verbrauch (kWh):</span> <b id="rKwh">0.000000 kWh</b></div>
            <div class="receipt-row receipt-total"><span>Gesamtbetrag:</span> <span id="rCost" style="color: var(--accent-green);">0.00000 €</span></div>

            <div class="email-box">
                <div style="font-size:12px; font-weight:700; margin-bottom:6px; color:var(--text-main);">📧 Rechnung als PDF zusenden:</div>
                <input type="email" id="emailInput" class="email-input" placeholder="deine-email@beispiel.de">
                <button class="btn-start" style="background:var(--accent-primary); font-size:14px; padding:11px;" onclick="sendInvoiceEmail()">Rechnung per E-Mail senden</button>
                <button class="btn-stop" style="font-size:13px; padding:8px; margin-top:6px;" onclick="downloadInvoicePdf()">📥 PDF direkt herunterladen</button>
                <div id="emailFeedback" style="display:none; font-size:12px; font-weight:600; margin-top:8px;"></div>
            </div>

            <div style="margin-top: 20px; font-size: 12px; color: var(--text-muted);">
                ℹ️ Um die Steckdose erneut zu nutzen, scanne bitte den QR-Code an der Station vor Ort neu.
            </div>
        </div>
    </div>

    <script>
        let isTerminated = false;
        let lastReport = null;
        let transferModalOpen = false;
        let currentProfileKey = "lamp";

        function updateTimerUI(sec) {
            let h = Math.floor(sec / 3600).toString().padStart(2, '0');
            let m = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
            let s = Math.floor(sec % 60).toString().padStart(2, '0');
            document.getElementById('timer').innerText = `${h}:${m}:${s}`;
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

        function startSession() {
            if (isTerminated) return;
            document.getElementById('statusBadge').className = "status-pill status-on";
            document.getElementById('statusText').innerText = "Aktiv / Strom fließt";
            sendAction('/start');
            setTimeout(fetchSyncData, 150);
        }

        function pauseSession() {
            if (isTerminated) return;
            document.getElementById('statusBadge').className = "status-pill status-off";
            document.getElementById('statusText').innerText = "Pausiert / Bereit";
            sendAction('/stop');
            setTimeout(fetchSyncData, 150);
        }

        async function saveDeviceProfile(key) {
            document.getElementById('deviceModal').style.display = 'none';
            let res = await sendAction('/save_device', { key: key });
            applyProfile(key, res.profile);
        }

        function applyProfile(key, prof) {
            currentProfileKey = key;
            document.getElementById('devIcon').innerText = prof.icon;
            document.getElementById('detectedName').innerText = prof.name;
            document.getElementById('detectedMode').innerText = prof.is_battery ? "🔋 Akku-Ladeüberwachung" : "💡 Dauerbetrieb";
            
            let bCard = document.getElementById('batteryCard');
            if (prof.is_battery) {
                bCard.style.display = 'block';
            } else {
                bCard.style.display = 'none';
            }
        }

        async function requestSlot() {
            await sendAction('/request_transfer');
            document.getElementById('requestSentText').style.display = 'block';
            document.getElementById('btnRequestSlot').disabled = true;
            document.getElementById('btnRequestSlot').style.opacity = '0.6';
        }

        async function acceptTransfer() {
            transferModalOpen = false;
            document.getElementById('transferModal').style.display = 'none';
            await logout();
        }

        async function rejectTransfer() {
            transferModalOpen = false;
            document.getElementById('transferModal').style.display = 'none';
            await sendAction('/reject_transfer');
        }

        async function logout() {
            isTerminated = true;
            try {
                let report = await sendAction('/logout');
                lastReport = report;
                
                document.getElementById('rDevice').innerText = document.getElementById('detectedName').innerText;
                document.getElementById('rMode').innerText = document.getElementById('detectedMode').innerText;
                document.getElementById('rTime').innerText = report.time_formatted;
                document.getElementById('rWh').innerText = report.wh.toFixed(4) + " Wh";
                document.getElementById('rKwh').innerText = report.kwh.toFixed(6) + " kWh";
                document.getElementById('rCost').innerText = report.cost.toFixed(5) + " €";
                
                document.getElementById('mainCard').style.display = 'none';
                document.getElementById('busyCard').style.display = 'none';
                document.getElementById('receiptCard').style.display = 'block';
            } catch(e) {}
        }

        async function sendInvoiceEmail() {
            let email = document.getElementById('emailInput').value.trim();
            let fb = document.getElementById('emailFeedback');
            if (!email || !email.includes('@')) {
                fb.style.display = 'block';
                fb.style.color = 'var(--accent-red)';
                fb.innerText = 'Bitte eine gültige E-Mail-Adresse eingeben.';
                return;
            }
            fb.style.display = 'block';
            fb.style.color = 'var(--accent-primary)';
            fb.innerText = 'Erstelle PDF und versende E-Mail...';

            let res = await sendAction('/send_email_invoice', {
                email: email,
                report: lastReport,
                device: document.getElementById('rDevice').innerText,
                mode: document.getElementById('rMode').innerText
            });

            if (res.status === 'ok') {
                fb.style.color = 'var(--accent-green)';
                fb.innerText = '✅ Rechnung wurde erfolgreich an deine E-Mail gesendet!';
            } else {
                fb.style.color = 'var(--accent-amber)';
                fb.innerText = res.message;
            }
        }

        function downloadInvoicePdf() {
            window.open('/download_invoice', '_blank');
        }

        // 1-SEKUNDEN STATUSABGLEICH
        async function fetchSyncData() {
            if (isTerminated) return;
            try {
                let res = await fetch('/status', { cache: 'no-store' });
                let data = await res.json();

                if (data.session_terminated) {
                    await logout();
                    return;
                }

                if (data.is_busy_for_other) {
                    document.getElementById('mainCard').style.display = 'none';
                    document.getElementById('busyCard').style.display = 'block';
                    document.getElementById('busyWatt').innerText = data.global_watt.toFixed(3) + " W";
                    return;
                } else {
                    document.getElementById('busyCard').style.display = 'none';
                    document.getElementById('mainCard').style.display = 'block';
                }

                if (data.transfer_requested && !transferModalOpen) {
                    transferModalOpen = true;
                    document.getElementById('transferModal').style.display = 'flex';
                }

                // Profil & Analyse-Status
                if (data.current_profile) {
                    applyProfile(data.current_profile_key, data.current_profile);
                }

                if (data.active && data.elapsed_seconds < 30 && !data.manually_selected) {
                    let remain = 30 - Math.floor(data.elapsed_seconds);
                    document.getElementById('aiStatusTitle').innerText = "Analyse läuft...";
                    document.getElementById('detectedName').innerText = `Analysiere Last... (${remain}s)`;
                } else if (data.analysis_completed && !data.manually_selected) {
                    document.getElementById('aiStatusTitle').innerText = "Erkannt";
                } else if (data.manually_selected) {
                    document.getElementById('aiStatusTitle').innerText = "Manuell gewählt";
                }

                // Server-synchronisierter Timer
                updateTimerUI(Math.floor(data.elapsed_seconds));

                let currentW = data.watt;
                let currentA = data.current_ampere || 0.0;
                let currentV = data.voltage || 230.0;

                document.getElementById('volt').innerText = currentV.toFixed(1);
                document.getElementById('amp').innerText = currentA.toFixed(3);
                document.getElementById('milliAmp').innerText = (currentA * 1000.0).toFixed(0);

                document.getElementById('watt').innerText = currentW.toFixed(3);
                document.getElementById('wh').innerText = data.wh.toFixed(4);
                document.getElementById('mwh').innerText = (data.wh * 1000.0).toFixed(1);
                document.getElementById('cost').innerText = data.cost.toFixed(5);
                document.getElementById('microCost').innerText = (data.cost * 100.0).toFixed(3);

                // AKKU-LADEZUSTAND (%) & RESTZEIT-PROGNOSE
                if (data.current_profile && data.current_profile.is_battery) {
                    let cap = data.current_profile.capacity_wh || 50.0;
                    let loadedWh = data.wh;
                    let pct = Math.min(100, (loadedWh / cap) * 100.0);
                    
                    document.getElementById('batteryPercentText').innerText = pct.toFixed(1) + "%";
                    document.getElementById('batteryBarFill').style.width = pct.toFixed(1) + "%";
                    document.getElementById('batteryWhLoaded').innerText = `${loadedWh.toFixed(2)} / ${cap.toFixed(0)} Wh`;

                    if (currentW > 1.0 && pct < 100) {
                        let remainingWh = Math.max(0, cap - loadedWh);
                        let remainingHours = remainingWh / currentW;
                        let rMin = Math.round(remainingHours * 60);
                        if (rMin > 60) {
                            let rH = Math.floor(rMin / 60);
                            let rM = rMin % 60;
                            document.getElementById('batteryTimeRemaining').innerText = `Restzeit: ca. ${rH}h ${rM}m`;
                        } else {
                            document.getElementById('batteryTimeRemaining').innerText = `Restzeit: ca. ${rMin} Min.`;
                        }
                    } else if (pct >= 100) {
                        document.getElementById('batteryTimeRemaining').innerText = "Akku Voll (100%)";
                    } else {
                        document.getElementById('batteryTimeRemaining').innerText = "Restzeit: --";
                    }
                }

                if (data.active) {
                    document.getElementById('statusBadge').className = "status-pill status-on";
                    document.getElementById('statusText').innerText = "Aktiv / Strom fließt";
                    document.getElementById('wattSub').innerText = currentW > 0.1 ? "Fließt stabil" : "Bereit / Standby";
                } else {
                    document.getElementById('statusBadge').className = "status-pill status-off";
                    document.getElementById('statusText').innerText = "Pausiert / Bereit";
                }

            } catch(e) {}
        }

        setInterval(fetchSyncData, 1000);
        fetchSyncData();
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
            "terminated": False,
            "total_kwh": 0.0,
            "total_seconds": 0.0,
            "current_watt": 0.0,
            "current_ampere": 0.0,
            "current_voltage": 230.0,
            "device_key": "lamp",
            "analysis_samples": [],
            "analysis_completed": False,
            "manually_selected": False,
            "had_charging_phase": False,
            "battery_full_triggered": False,
            "last_report": None
        }
    return user_sessions[uid], uid

@app.route('/')
def home():
    if not session.get("authenticated_on_site") or session.get("station_token") != STATION_PHYSICAL_TOKEN:
        return render_template_string(HTML_ACCESS_DENIED), 403
    
    if "user_id" in session:
        old_uid = session["user_id"]
        if user_sessions.get(old_uid, {}).get("terminated", False):
            session.pop("user_id", None)
            
    get_user_data()
    return render_template_string(HTML_PAGE)

@app.route('/scan/<token>')
def scan_qr_entry(token):
    if token != STATION_PHYSICAL_TOKEN:
        return render_template_string(HTML_ACCESS_DENIED), 403

    session["authenticated_on_site"] = True
    session["station_token"] = STATION_PHYSICAL_TOKEN
    session["user_id"] = str(uuid.uuid4())
    return redirect('/')

@app.route('/start', methods=['POST', 'GET'])
@require_physical_auth
def start():
    u, uid = get_user_data()
    if u.get("terminated", False):
        return jsonify({"status": "forbidden"}), 403

    active_uid = global_state.get("active_user_id")
    if active_uid and active_uid != uid:
        other_user = user_sessions.get(active_uid, {})
        if other_user.get("active", False) and not other_user.get("terminated", False):
            return jsonify({"status": "busy"})
        
    global_state["active_user_id"] = uid
    global_state["transfer_requested"] = False
    global_state["transfer_requester_id"] = None
    u["active"] = True
    async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST', 'GET'])
@require_physical_auth
def stop():
    u, uid = get_user_data()
    if u.get("terminated", False) or global_state.get("active_user_id") != uid:
        return jsonify({"status": "forbidden"}), 403

    u["active"] = False
    u["current_watt"] = 0.0
    u["current_ampere"] = 0.0
    async_cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/save_device', methods=['POST'])
@require_physical_auth
def save_device():
    u, uid = get_user_data()
    if u.get("terminated", False) or global_state.get("active_user_id") != uid:
        return jsonify({"status": "forbidden"}), 403

    data = request.get_json() or {}
    key = data.get("key", "lamp")
    if key in DEVICE_PROFILES:
        u["device_key"] = key
        u["manually_selected"] = True
        u["analysis_completed"] = True
    return jsonify({
        "status": "saved",
        "profile": DEVICE_PROFILES.get(u["device_key"])
    })

@app.route('/request_transfer', methods=['POST'])
@require_physical_auth
def request_transfer():
    _, uid = get_user_data()
    if global_state["active_user_id"] and global_state["active_user_id"] != uid:
        global_state["transfer_requested"] = True
        global_state["transfer_requester_id"] = uid
    return jsonify({"status": "requested"})

@app.route('/reject_transfer', methods=['POST'])
@require_physical_auth
def reject_transfer():
    global_state["transfer_requested"] = False
    global_state["transfer_requester_id"] = None
    return jsonify({"status": "rejected"})

@app.route('/logout', methods=['POST', 'GET'])
@require_physical_auth
def logout():
    u, uid = get_user_data()
    u["active"] = False
    u["terminated"] = True
    async_cloud_control(turn_on=False)

    if global_state["active_user_id"] == uid:
        global_state["active_user_id"] = None
        global_state["transfer_requested"] = False
        global_state["transfer_requester_id"] = None

    sec = int(u["total_seconds"])
    h = str(sec // 3600).zfill(2)
    m = str((sec % 3600) // 60).zfill(2)
    s = str(sec % 60).zfill(2)

    invoice_num = f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

    report = {
        "invoice_id": invoice_num,
        "time_formatted": f"{h}:{m}:{s}",
        "wh": u["total_kwh"] * 1000.0,
        "kwh": u["total_kwh"],
        "cost": u["total_kwh"] * STROMPREIS_PER_KWH
    }
    u["last_report"] = report

    return jsonify(report)

@app.route('/download_invoice', methods=['GET'])
@require_physical_auth
def download_invoice():
    u, _ = get_user_data()
    report = u.get("last_report") or {
        "invoice_id": "RE-SAMPLE",
        "device": "💡 Lampe / Beleuchtung",
        "mode": "Dauerbetrieb",
        "time_formatted": "00:15:00",
        "wh": 10.5,
        "kwh": 0.0105,
        "cost": 0.00367
    }
    pdf_buffer = generate_pdf_invoice(report)
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=f"{report.get('invoice_id', 'Rechnung')}.pdf")

@app.route('/send_email_invoice', methods=['POST'])
@require_physical_auth
def send_email_invoice():
    data = request.get_json() or {}
    recipient = data.get("email")
    report = data.get("report") or {}
    report["device"] = data.get("device", "💡 Lampe / Beleuchtung")
    report["mode"] = data.get("mode", "Dauerbetrieb")

    if not recipient or "@" not in recipient:
        return jsonify({"status": "error", "message": "Ungültige E-Mail-Adresse"})

    if not SMTP_USER or not SMTP_PASSWORD:
        return jsonify({
            "status": "error",
            "message": "Hinweis: Keine SMTP-Zugangsdaten konfiguriert. Bitte '📥 PDF direkt herunterladen' nutzen."
        })

    pdf_buffer = generate_pdf_invoice(report)

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = recipient
        msg["Subject"] = f"Deine Stromquittung ({report.get('invoice_id')})"

        body_text = f"Hallo,\n\nvielen Dank für die Nutzung der Smart Power Station.\n\nZusammenfassung:\n- Dauer: {report.get('time_formatted')}\n- Verbrauch: {report.get('wh', 0):.2f} Wh ({report.get('kwh', 0):.5f} kWh)\n- Gesamtbetrag: {report.get('cost', 0):.5f} €\n\nIm Anhang findest du deine detaillierte PDF-Rechnung."
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

        pdf_attachment = MIMEApplication(pdf_buffer.read(), _subtype="pdf")
        pdf_attachment.add_header('Content-Disposition', 'attachment', filename=f"{report.get('invoice_id')}.pdf")
        msg.attach(pdf_attachment)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=8)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()

        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Versand fehlgeschlagen ({str(e)}). Bitte lade das PDF direkt herunter."
        })

@app.route('/status')
@require_physical_auth
def status():
    u, uid = get_user_data()
    active_uid = global_state.get("active_user_id")

    if u.get("terminated", False):
        return jsonify({"session_terminated": True, "is_busy_for_other": False})
    
    is_busy = False
    global_w = 0.0
    if active_uid and active_uid != uid:
        if user_sessions.get(active_uid, {}).get("active", False):
            is_busy = True
            global_w = global_state["last_watt"]

    dev_key = u.get("device_key", "lamp")
    
    return jsonify({
        "active": u["active"],
        "watt": u["current_watt"],
        "current_ampere": u["current_ampere"],
        "voltage": u["current_voltage"],
        "global_watt": global_w,
        "wh": u["total_kwh"] * 1000.0,
        "kwh": u["total_kwh"],
        "cost": u["total_kwh"] * STROMPREIS_PER_KWH,
        "elapsed_seconds": u["total_seconds"],
        "is_busy_for_other": is_busy,
        "transfer_requested": global_state.get("transfer_requested", False) and (active_uid == uid),
        "current_profile_key": dev_key,
        "current_profile": DEVICE_PROFILES.get(dev_key),
        "analysis_completed": u.get("analysis_completed", False),
        "manually_selected": u.get("manually_selected", False),
        "session_terminated": False
    })

if __name__ == '__main__':
    app.run()
