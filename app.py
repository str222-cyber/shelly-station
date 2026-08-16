from flask import Flask, render_template_string, jsonify, session, request, send_file, redirect
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import time
import threading
import uuid
import smtplib
import json
import os
import io
import math
from functools import wraps
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from weasyprint import HTML

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.secret_key = "shelly_smart_hub_absolute_stable_2026"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=14400
)

# GEHEIMER VOR-ORT TOKEN
STATION_PHYSICAL_TOKEN = "SEC-STATION-2026-X99Q-ALPHA-77"

# --- SHELLY CLOUD KONFIGURATION ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

STROMPREIS_PER_KWH = 0.35

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = ""

AI_MODEL_FILE = "ai_learned_models.json"

DEFAULT_AI_PROFILES = {
    "lamp": {"avg_w": 1.2, "peak_w": 2.0, "variance": 0.1, "count": 1},
    "phone": {"name": "📱 Smartphone / Tablet", "avg_w": 10.0, "peak_w": 20.0, "variance": 3.0, "count": 1},
    "laptop": {"name": "💻 Laptop / Monitor", "avg_w": 45.0, "peak_w": 75.0, "variance": 12.0, "count": 1},
    "ebike_std": {"name": "🚲 E-Bike Akku Standard", "avg_w": 140.0, "peak_w": 170.0, "variance": 8.0, "count": 1},
    "ebike_fast": {"name": "⚡ E-Bike Schnelllader / PC", "avg_w": 350.0, "peak_w": 420.0, "variance": 15.0, "count": 1},
    "appliance": {"name": "🍳 Großgerät / Dauerbetrieb", "avg_w": 850.0, "peak_w": 1200.0, "variance": 50.0, "count": 1}
}

DEVICE_PROFILES = {
    "lamp": {"name": "💡 Lampe / Dauerbetrieb", "icon": "💡", "is_battery": False, "capacity_wh": 0},
    "phone": {"name": "📱 Smartphone / Tablet", "icon": "📱", "is_battery": True, "capacity_wh": 20.0},
    "laptop": {"name": "💻 Laptop / Monitor", "icon": "💻", "is_battery": True, "capacity_wh": 65.0},
    "ebike_std": {"name": "🚲 E-Bike Akku Standard", "icon": "🚲", "is_battery": True, "capacity_wh": 500.0},
    "ebike_fast": {"name": "⚡ E-Bike Schnelllader / PC", "icon": "⚡", "is_battery": True, "capacity_wh": 750.0},
    "appliance": {"name": "🍳 Großgerät / Dauerbetrieb", "icon": "🍳", "is_battery": False, "capacity_wh": 0}
}

def load_ai_models():
    if os.path.exists(AI_MODEL_FILE):
        try:
            with open(AI_MODEL_FILE, "r") as f: return json.load(f)
        except Exception: pass
    return DEFAULT_AI_PROFILES

def save_ai_models(models):
    try:
        with open(AI_MODEL_FILE, "w") as f: json.dump(models, f, indent=2)
    except Exception: pass

global_state = {
    "active_user_id": None,
    "transfer_requested": False,
    "transfer_requester_id": None,
    "last_watt": 0.0,
    "last_amp": 0.0,
    "last_volt": 230.0
}
user_sessions = {}
WORKER_STARTED = False
WORKER_LOCK = threading.Lock()

def ensure_worker():
    global WORKER_STARTED
    if not WORKER_STARTED:
        with WORKER_LOCK:
            if not WORKER_STARTED:
                threading.Thread(target=background_meter_worker, daemon=True).start()
                WORKER_STARTED = True

@app.after_request
def add_security_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

def check_authenticated():
    if request.headers.get("X-Station-Token") == STATION_PHYSICAL_TOKEN: return True
    return session.get("authenticated_on_site") and session.get("station_token") == STATION_PHYSICAL_TOKEN

def require_physical_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_authenticated():
            return jsonify({"status": "unauthorized", "message": "Bitte QR-Code vor Ort scannen."}), 401
        return f(*args, **kwargs)
    return decorated_function

def async_cloud_control(turn_on=True):
    def _worker():
        turn_str = "on" if turn_on else "off"
        try:
            requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control", data={"auth_key": AUTH_KEY, "id": DEVICE_ID, "turn": turn_str, "channel": 0}, timeout=2.5)
            requests.post(f"{SHELLY_CLOUD_URL}/device/rpc", json={"auth_key": AUTH_KEY, "id": DEVICE_ID, "method": "Switch.Set", "params": {"id": 0, "on": turn_on}}, timeout=2.5)
        except: pass
    threading.Thread(target=_worker, daemon=True).start()

def estimate_current_soc(profile_key, samples):
    if not samples: return 0.0
    avg_w = sum(samples) / len(samples)
    mid = len(samples) // 2
    trend = 0.0
    if mid > 0:
        trend = (sum(samples[mid:]) / (len(samples) - mid)) - (sum(samples[:mid]) / mid)
        
    is_cv_phase = trend < -0.2
    
    if profile_key == "phone":
        if is_cv_phase and avg_w < 8.0: return 88.0
        if avg_w >= 15.0: return 20.0
        elif avg_w >= 10.0: return 40.0
        elif avg_w >= 6.0: return 70.0
        elif avg_w >= 3.0: return 85.0
        elif avg_w >= 1.0: return 95.0
        else: return 99.0
        
    elif profile_key == "laptop":
        if is_cv_phase and avg_w < 35.0: return 85.0
        if avg_w >= 50.0: return 20.0
        elif avg_w >= 35.0: return 50.0
        elif avg_w >= 20.0: return 75.0
        elif avg_w >= 10.0: return 88.0
        elif avg_w >= 5.0: return 96.0
        else: return 99.0
        
    return 0.0

def background_meter_worker():
    last_loop = time.time()
    while True:
        now = time.time()
        dt = max(1.0, min(5.0, now - last_loop))
        last_loop = now

        try:
            active_uid = global_state.get("active_user_id")
            
            for uid, u in user_sessions.items():
                if not u.get("active", False) and not u.get("detection_mode", False):
                    continue
                
                watt, amp, volt = 0.0, 0.0, 230.0
                try:
                    res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data={"auth_key": AUTH_KEY, "id": DEVICE_ID}, timeout=2.0).json()
                    if res.get("isok"):
                        status = res.get("data", {}).get("device_status", {})
                        if "switch:0" in status:
                            watt = float(status["switch:0"].get("apower", 0.0))
                            amp = float(status["switch:0"].get("current", 0.0))
                            volt = float(status["switch:0"].get("voltage", 230.0))
                        elif "meters" in status and len(status["meters"]) > 0:
                            watt = float(status["meters"][0].get("power", 0.0))
                            amp = float(status["meters"][0].get("current", 0.0)) if "current" in status["meters"][0] else (watt / 230.0 if watt > 0 else 0.0)
                            volt = float(status["meters"][0].get("voltage", 230.0)) if "voltage" in status["meters"][0] else 230.0
                except:
                    watt, amp, volt = u.get("current_watt", 0.0), u.get("current_ampere", 0.0), u.get("current_voltage", 230.0)

                u["current_watt"], u["current_ampere"], u["current_voltage"] = watt, amp, volt
                u["smoothed_watt"] = watt if u.get("smoothed_watt") is None else (u["smoothed_watt"] * 0.8) + (watt * 0.2)
                
                if uid == active_uid:
                    global_state["last_watt"], global_state["last_amp"], global_state["last_volt"] = watt, amp, volt

                # --- LIVE KONTINUIERLICHES FENSTER (Letzte 40 Sekunden) ---
                if "recent_samples" not in u: u["recent_samples"] = []
                if watt > 0.1 or len(u["recent_samples"]) > 0:
                    u["recent_samples"].append(watt)
                    if len(u["recent_samples"]) > 40:
                        u["recent_samples"].pop(0)

                u["session_peak_watt"] = max(u.get("session_peak_watt", 0.0), watt)

                # --- 1. DETECTION MODE (WARTEN AUF KABEL) ---
                if not u["active"] and u.get("detection_mode", False):
                    if time.time() - u.get("last_seen", time.time()) > 15.0:
                        u["detection_mode"] = False
                        u["show_start_prompt"] = False
                        async_cloud_control(turn_on=False)
                        continue

                    if watt > 0.5: u["show_start_prompt"] = True
                    else: u["show_start_prompt"] = False
                    continue 

                # --- 2. AKTIVE SESSION ---
                if u.get("active", False):
                    u["total_seconds"] += dt
                    if watt > 0.05:
                        u["total_kwh"] += (watt * dt) / 3600000.0

                    # --- KONTINUIERLICHE KI LIVE-ÜBERWACHUNG ---
                    if len(u["recent_samples"]) >= 5 and not u.get("manually_selected"):
                        peak = u["session_peak_watt"]
                        tiers = {"lamp": 0, "phone": 1, "laptop": 2, "ebike_std": 3, "ebike_fast": 4, "appliance": 0}
                        curr_tier = tiers.get(u.get("device_key", "phone"), 0)
                        
                        new_key = u.get("device_key", "phone")
                        if peak > 250.0: new_key = "ebike_fast"
                        elif peak > 90.0: new_key = "ebike_std"
                        elif peak >= 26.0: new_key = "laptop"
                        elif peak >= 0.1: new_key = "phone"
                        else: new_key = "lamp"
                        
                        if tiers.get(new_key, 0) > curr_tier:
                            u["device_key"] = new_key
                            
                        est_soc = estimate_current_soc(u["device_key"], u["recent_samples"])
                        cap = DEVICE_PROFILES.get(u["device_key"], {}).get("capacity_wh", 20.0)
                        
                        current_calc_soc = u.get("estimated_soc_0", 0.0) + (((u.get("total_kwh", 0.0) * 1000.0) / cap) * 100.0)
                        
                        if u["total_seconds"] <= 40.0:
                            u["estimated_soc_0"] = est_soc - (((u.get("total_kwh", 0.0) * 1000.0) / cap) * 100.0)
                        else:
                            if est_soc > current_calc_soc + 5.0:
                                u["estimated_soc_0"] = est_soc - (((u.get("total_kwh", 0.0) * 1000.0) / cap) * 100.0)

                        if u["total_seconds"] >= 40.0:
                            u["analysis_completed"] = True

                    # 65 SEKUNDEN STRIKTES AUSSTECK-TIMEOUT
                    if watt > 0.5:
                        u["had_power_draw"] = True
                        u["zero_power_counter"] = 0.0
                        
                    if u.get("had_power_draw") and watt <= 0.15:
                        u["zero_power_counter"] += dt
                        if u["zero_power_counter"] >= 65.0:  
                            u["active"], u["unplugged_detected"], u["had_power_draw"] = False, True, False
                            async_cloud_control(turn_on=False)
                    elif watt > 0.15:
                        u["zero_power_counter"] = 0.0

                    # 80% & 100% Erkennung
                    prof = DEVICE_PROFILES.get(u.get("device_key"), {})
                    if prof.get("is_battery", False) and u["total_seconds"] > 40.0:
                        cap = prof.get("capacity_wh", 20.0)
                        current_pct = u.get("estimated_soc_0", 0.0) + (((u["total_kwh"] * 1000.0) / cap) * 100.0)

                        if current_pct >= 80.0 and not u.get("eighty_percent_triggered", False):
                            u["eighty_percent_triggered"], u["active"] = True, False
                            async_cloud_control(turn_on=False)

                        if watt > 5.0: u["had_charging_phase"] = True
                        if u.get("had_charging_phase", False) and 0.2 <= watt < 1.5 and (u["total_kwh"] * 1000.0) > 1.0:
                            u["battery_full_counter"] += dt
                            if u["battery_full_counter"] >= 20.0:
                                u["battery_full_triggered"], u["active"] = True, False
                                async_cloud_control(turn_on=False)
                        else:
                            u["battery_full_counter"] = 0.0
        except: pass
        time.sleep(1.0)

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
            <tr><td><strong>Verbrauchte Energie (Wh)</strong></td><td>{report_data.get('wh', 0.0):.4f}</td><td>Wh</td></tr>
            <tr><td><strong>Verbrauchte Energie (kWh)</strong></td><td>{report_data.get('kwh', 0.0):.6f}</td><td>kWh</td></tr>
            <tr><td><strong>Arbeitspreis</strong></td><td>{STROMPREIS_PER_KWH:.3f}</td><td>€ / kWh</td></tr>
        </tbody>
    </table>
    <div class="total">
        <div class="total-title">Gesamtbetrag</div>
        <div class="total-val">{report_data.get('cost', 0.0):.5f} €</div>
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
        <p>Ein direkter Web-Aufruf über das Internet ist nicht gestattet.</p>
        <p style="margin-top: 12px; color: #e2e8f0; font-weight: 600;">Bitte scanne den QR-Code auf dem Laptop-Bildschirm oder an der Ladestation.</p>
    </div>
</body>
</html>
"""

HTML_PAGE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Power Hub • AI Powered</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        :root {
            --bg-color: #f8fafc; --card-bg: #ffffff; --text-main: #0f172a;
            --text-muted: #64748b; --accent-primary: #2563eb; --accent-green: #059669;
            --accent-amber: #d97706; --accent-red: #dc2626; --accent-cyan: #0891b2;
            --border-color: #e2e8f0; --shadow-md: 0 10px 25px -5px rgba(15, 23, 42, 0.07);
        }
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; padding: 18px 12px; min-height: 100vh; }
        .container { width: 100%; max-width: 420px; margin: auto; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 22px 18px; box-shadow: var(--shadow-md); border: 1px solid var(--border-color); text-align: center; margin-bottom: 12px; }
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .title { font-size: 18px; font-weight: 700; color: var(--text-main); letter-spacing: -0.3px; }
        .rate-badge { background: #f1f5f9; color: var(--text-muted); font-size: 12px; padding: 4px 10px; border-radius: 20px; font-weight: 600; }
        .security-badge { background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 12px; padding: 5px 10px; font-size: 11px; color: #065f46; font-weight: 600; margin-bottom: 12px; display: inline-flex; align-items: center; gap: 6px; }
        
        .ai-banner { background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); border: 1px solid var(--border-color); border-radius: 18px; padding: 12px 14px; margin-bottom: 12px; text-align: left; }
        .ai-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
        .ai-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent-primary); letter-spacing: 0.5px; }
        .btn-edit { background: #e2e8f0; color: var(--text-main); border: none; font-size: 11px; font-weight: 600; padding: 4px 9px; border-radius: 8px; cursor: pointer; }
        .ai-body { display: flex; align-items: center; gap: 10px; }
        .ai-icon { font-size: 26px; }
        .ai-detected { font-size: 14px; font-weight: 700; color: var(--text-main); }
        .ai-mode { font-size: 11px; color: var(--text-muted); margin-top: 1px; }

        .battery-card { display: none; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 16px; padding: 12px 14px; margin-bottom: 12px; text-align: left; }
        .battery-header { display: flex; justify-content: space-between; font-size: 12px; color: #166534; margin-bottom: 6px; }
        .battery-bar-wrap { width: 100%; height: 10px; background: #dcfce7; border-radius: 5px; overflow: hidden; border: 1px solid #86efac; }
        .battery-bar-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #22c55e, #16a34a); transition: width 0.4s ease; }
        .battery-meta { display: flex; justify-content: space-between; font-size: 11px; color: #15803d; margin-top: 5px; font-weight: 600; }

        .status-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; padding: 5px 12px; border-radius: 30px; margin-bottom: 12px; height: 28px; }
        .status-on { background: #ecfdf5; color: #065f46; }
        .status-off { background: #f1f5f9; color: var(--text-muted); }
        .status-unplug { background: #fef3c7; color: #92400e; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; }
        .status-on .status-dot { background: var(--accent-green); box-shadow: 0 0 8px rgba(16,185,129,0.6); }
        .status-off .status-dot { background: #94a3b8; }
        .status-unplug .status-dot { background: #94a3b8; }

        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .stat-card { background: #f8fafc; border: 1px solid var(--border-color); border-radius: 16px; padding: 12px; text-align: left; min-height: 80px; }
        .stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.4px; }
        .stat-val { font-size: 18px; font-weight: 700; color: var(--text-main); margin-top: 3px; font-family: monospace; white-space: nowrap; }
        .stat-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; font-family: monospace; }
        .stat-watt .stat-val { color: var(--accent-primary); }
        .stat-cost .stat-val { color: var(--accent-green); }
        .stat-volt .stat-val { color: var(--accent-amber); }
        .stat-amp .stat-val { color: var(--accent-cyan); }

        .btn-group { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
        button { width: 100%; padding: 13px; font-size: 15px; font-weight: 600; border: none; border-radius: 14px; cursor: pointer; transition: transform 0.1s ease; }
        button:active { transform: scale(0.98); }
        .btn-start { background: var(--text-main); color: white; }
        .btn-stop { background: #f1f5f9; color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-finish { background: #fee2e2; color: var(--accent-red); }

        .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(4px); z-index: 999; padding: 20px; align-items: center; justify-content: center; }
        .modal-box { background: white; border-radius: 24px; padding: 24px 20px; text-align: center; max-width: 340px; width: 100%; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.2); animation: popIn 0.3s ease-out; }
        @keyframes popIn { from { transform: scale(0.85); opacity: 0; } to { transform: scale(1); opacity: 1; } }

        .device-option-btn { background: #f8fafc; border: 1px solid var(--border-color); border-radius: 12px; padding: 11px; text-align: left; display: flex; align-items: center; gap: 10px; margin-bottom: 8px; width: 100%; font-size: 13px; font-weight: 600; cursor: pointer; }
        .receipt-card { display: none; text-align: left; }
        .receipt-header { text-align: center; margin-bottom: 18px; }
        .receipt-row { display: flex; justify-content: space-between; padding: 9px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
        .receipt-total { border-top: 2px solid var(--text-main); border-bottom: none; font-size: 17px; font-weight: 700; margin-top: 10px; padding-top: 12px; }

        .email-box { margin-top: 18px; background: #f8fafc; border: 1px solid var(--border-color); border-radius: 14px; padding: 14px; }
        .email-input { width: 100%; padding: 11px; border: 1px solid var(--border-color); border-radius: 10px; font-size: 14px; margin-bottom: 8px; box-sizing: border-box; }
        .busy-card, .pause-wait-card { display: none; text-align: center; }
    </style>
</head>
<body>
    
    <!-- KABEL ERKANNT POP-UP -->
    <div id="plugDetectedModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-cyan);">
            <div style="font-size: 48px; margin-bottom: 8px;">🔌⚡</div>
            <h2 style="font-size: 20px; color: var(--accent-cyan); margin-bottom: 6px;">Kabel eingesteckt!</h2>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">Wir haben erkannt, dass ein Gerät angeschlossen wurde. Möchtest du den Ladevorgang starten?</p>
            <button class="btn-start" style="background: var(--accent-cyan); margin-bottom: 8px;" onclick="confirmStart()">▶️ Ja, Ladevorgang starten</button>
            <button class="btn-stop" onclick="closeAppEarly()">❌ Nein, abbrechen</button>
        </div>
    </div>

    <!-- GERÄTE MODAL -->
    <div id="deviceModal" class="modal-overlay">
        <div class="modal-box">
            <h3 style="margin-bottom: 6px;">Gerät manuell festlegen</h3>
            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 14px;">Wähle dein Gerät aus. Das Schwarmwissen der AI lernt dadurch automatisch mit.</p>
            <button class="device-option-btn" onclick="saveDeviceProfile('lamp')">💡 Lampe / Dauerbetrieb</button>
            <button class="device-option-btn" onclick="saveDeviceProfile('phone')">📱 Smartphone / Tablet (Akku ~20 Wh)</button>
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
            <p style="font-size: 13px; margin-bottom: 14px;">Ein anderer Nutzer hat den QR-Code gescannt und möchte laden. Möchtest du die Steckdose jetzt übergeben?</p>
            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="acceptTransfer()">✅ Ja, überlassen</button>
            <button class="btn-stop" onclick="rejectTransfer()">Nein, ich nutze weiter</button>
        </div>
    </div>

    <!-- 80% AKKU POP-UP -->
    <div id="eightyModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-primary);">
            <div style="font-size: 48px; margin-bottom: 8px;">🔋⚡</div>
            <h2 style="font-size: 19px; color: var(--accent-primary); margin-bottom: 6px;">Device already 80% charged</h2>
            <p style="font-size: 13px; margin-bottom: 12px;">Please unplug to protect battery life. (Charging stopped automatically)</p>
            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="logout(true)">✅ Show Receipt & Summary</button>
            <button class="btn-stop" onclick="dismissEightyModal()">Continue charging to 100%</button>
        </div>
    </div>

    <!-- 100% AKKU VOLL POP-UP -->
    <div id="fullModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-green);">
            <div style="font-size: 48px; margin-bottom: 8px;">🔋✨</div>
            <h2 style="font-size: 20px; color: var(--accent-green); margin-bottom: 6px;">Akku 100% Vollgeladen!</h2>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">Der Stromfluss wurde automatisch gestoppt. Kein weiterer Strombedarf.</p>
            <button class="btn-start" style="background: var(--accent-green);" onclick="logout(true)">🧾 Quittung & Rechnung anzeigen</button>
        </div>
    </div>

    <div class="container">
        <!-- HAUPTKARTE -->
        <div class="card" id="mainCard">
            <div class="header">
                <span class="title">⚡ Smart Power Hub</span>
                <span class="rate-badge">""" + str(STROMPREIS_PER_KWH) + """ €/kWh</span>
            </div>
            <div>
                <span class="security-badge">🛡️ Gesicherte Vor-Ort Sitzung</span><br>
                <div id="statusBadge" class="status-pill status-off">
                    <span class="status-dot"></span><span id="statusText">Bereit / Aus</span>
                </div>
            </div>

            <!-- GERÄTE ERKENNUNG (KONTINUIERLICH) -->
            <div class="ai-banner">
                <div class="ai-header">
                    <span class="ai-title" id="aiStatusTitle">AI Live-Überwachung</span>
                    <button class="btn-edit" onclick="document.getElementById('deviceModal').style.display='flex'">✏️ Ändern</button>
                </div>
                <div class="ai-body">
                    <div class="ai-icon" id="devIcon">📱</div>
                    <div>
                        <div class="ai-detected" id="detectedName">Smartphone (Standard)</div>
                        <div class="ai-mode" id="detectedMode">AI Lastanalyse (40s)...</div>
                    </div>
                </div>
            </div>
            
            <!-- NEU: INFO BOX ZUR AI LÄRNSCHWÄCHE -->
            <div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 14px; padding: 12px; margin-bottom: 12px; text-align: left; font-size: 11px; color: #92400e; line-height: 1.4;">
                <b style="color: #b45309;">ℹ️ Hinweis zur AI-Erkennung:</b> Bei bestimmten Ladephasen (z.B. voller Laptop vs. schnellladendes Smartphone) ist eine automatische Unterscheidung schwer, da beide z.B. 20 Watt ziehen. Falls die KI sich irrt, wähle das Gerät bitte über <b>"Ändern"</b> manuell aus. Die Berechnungen passen sich sofort an.
            </div>

            <!-- AKKU LADESTAND -->
            <div class="battery-card" id="batteryCard">
                <div class="battery-header">
                    <span style="font-weight: 600;">🔋 Phase: <span id="batteryPhaseText" style="color: var(--text-main);">Analysiere...</span></span>
                    <span id="batteryPercentText" style="font-weight: 700; color: var(--text-main);">0%</span>
                </div>
                <div class="battery-bar-wrap"><div class="battery-bar-fill" id="batteryBarFill"></div></div>
                <div class="battery-meta">
                    <span>Geladen: <span id="batteryWhLoaded">0.0 / 0 Wh</span></span>
                    <span id="batteryRemWhBox" style="display:none; color: #15803d; font-weight: bold;">Noch nötig: <span id="batteryRemWh">0.0</span> Wh</span>
                </div>
                <div class="battery-meta" style="margin-top: 2px;">
                    <span></span>
                    <span id="batteryTimeRemaining">Restzeit: --</span>
                </div>
            </div>

            <!-- DAUERBETRIEB PROGNOSE (NUR FÜR LAMPEN/ETC SICHTBAR) -->
            <div id="continuousPrediction" style="display: none; margin-bottom: 12px;">
                <div style="font-size: 12px; font-weight: 700; color: var(--text-main); margin-bottom: 8px; text-align: left; padding-left: 4px;">🔮 Verbrauchsprognose (Dauerbetrieb)</div>
                <div class="grid-2">
                    <div class="stat-card" style="background: #f8fafc; border-color: #e2e8f0; min-height: 60px;">
                        <div class="stat-label">Nächste 1 Stunde</div>
                        <div class="stat-val" style="font-size: 15px; color: var(--accent-primary);"><span id="pred1hWh">0.0</span> Wh</div>
                        <div class="stat-sub"><span id="pred1hCost">0.000</span> €</div>
                    </div>
                    <div class="stat-card" style="background: #f8fafc; border-color: #e2e8f0; min-height: 60px;">
                        <div class="stat-label">Nächste 24 Stunden</div>
                        <div class="stat-val" style="font-size: 15px; color: var(--accent-primary);"><span id="pred24hWh">0.0</span></div>
                        <div class="stat-sub"><span id="pred24hCost">0.000</span> €</div>
                    </div>
                </div>
            </div>

            <!-- NETZDATEN GRID -->
            <div class="grid-2">
                <div class="stat-card stat-volt">
                    <div class="stat-label">Spannung (U)</div>
                    <div class="stat-val"><span id="volt">230.0</span> V</div>
                    <div class="stat-sub">Wechselspannung</div>
                </div>
                <div class="stat-card stat-amp">
                    <div class="stat-label">Strom (I)</div>
                    <div class="stat-val"><span id="amp">0.000</span> A</div>
                    <div class="stat-sub"><span id="milliAmp">0</span> mA</div>
                </div>
            </div>

            <!-- LEISTUNG & LAUFZEIT GRID -->
            <div class="grid-2">
                <div class="stat-card stat-watt">
                    <div class="stat-label">Wirkleistung (P)</div>
                    <div class="stat-val"><span id="watt">0.000</span> W</div>
                    <div class="stat-sub" id="wattSub">Kein Strom</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Laufzeit</div>
                    <div class="stat-val" id="timer" style="color: var(--text-main);">00:00:00</div>
                    <div class="stat-sub">Autarke Server-Messung</div>
                </div>
            </div>
            
            <!-- INFO BOX WATT SCHWANKUNG -->
            <div style="background: #f8fafc; border: 1px solid var(--border-color); border-radius: 14px; padding: 12px; margin-bottom: 12px; text-align: left; font-size: 11px; color: var(--text-muted); line-height: 1.4;">
                <b style="color: var(--text-main);">ℹ️ Info zu schwankenden Watt-Werten:</b> Moderne Ladegeräte kommunizieren permanent mit dem Akku. Um Überhitzung zu vermeiden, taktet und pulsiert das Netzteil den Strom. Diese realen Mikroschwankungen misst das System live mit.
            </div>

            <!-- ENERGIE & KOSTEN GRID -->
            <div class="grid-2">
                <div class="stat-card">
                    <div class="stat-label">Bisheriger Verbrauch</div>
                    <div class="stat-val" style="color:var(--accent-primary);"><span id="wh">0.0000</span> Wh</div>
                    <div class="stat-sub"><span id="mwh">0.0</span> mWh</div>
                </div>
                <div class="stat-card stat-cost" style="background: #f0fdf4; border-color: #bbf7d0;">
                    <div class="stat-label" style="color: #166534;">Aktuelle Kosten</div>
                    <div class="stat-val"><span id="cost">0.00000</span> €</div>
                    <div class="stat-sub" id="predictedCostBox" style="margin-top: 4px; color: #166534; font-weight: bold; display: none;">
                        100% Prognose: <span id="predictedCost">0.0000</span> €
                    </div>
                    <div class="stat-sub" id="microCostBox"><span id="microCost">0.00</span> Cent</div>
                </div>
            </div>

            <div class="btn-group">
                <button class="btn-start" id="mainStartBtn" onclick="startSession()">▶️ Start / Fortsetzen</button>
                <button class="btn-stop" onclick="pauseSession()">⏸️ Pause</button>
                <button class="btn-finish" onclick="logout(true)">🧾 Beenden & Abrechnen</button>
                
                <button class="btn-stop" style="background: #fef2f2; color: var(--accent-red); border-color: #fecaca; margin-top: 8px;" onclick="closeAppEarly()">❌ Fenster komplett schließen</button>
            </div>
        </div>

        <!-- QUITTUNG NACH ÜBERGABE / BEENDEN -->
        <div class="card receipt-card" id="receiptCard">
            <div class="receipt-header">
                <div style="font-size: 40px; margin-bottom: 4px;">🧾</div>
                <div class="title" id="receiptTitle">Lade- & Stromquittung</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 3px;" id="receiptSubtitle">Sitzung erfolgreich beendet</div>
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

            <div style="margin-top: 24px; display: flex; flex-direction: column; gap: 10px;">
                <button onclick="closeApp()" style="background: #ef4444; color: white; font-size: 16px; padding: 16px; font-weight: bold; border-radius: 14px; box-shadow: 0 4px 6px -1px rgba(239,68,68,0.3);">
                    ❌ App komplett schließen
                </button>
                <button class="btn-stop" style="font-size: 14px; padding: 12px; border: 1px solid var(--border-color);" onclick="startNewSessionCompletely()">
                    🔄 Neuer QR-Scan / Neustart
                </button>
                
                <div id="transferChoiceBox" style="display: flex; flex-direction: column; gap: 8px;">
                    <button class="btn-stop" style="background: #e0f2fe; color: #0369a1; border-color: #bae6fd;" onclick="setWaitingMode()">⏳ Sitzung pausieren (Warten)</button>
                </div>
            </div>
        </div>
        
        <!-- BESETZT-KARTE -->
        <div class="card busy-card" id="busyCard">
            <div style="font-size: 48px; margin-bottom: 10px;">⏳🔒</div>
            <div class="title" style="margin-bottom: 6px;">Steckdose aktuell belegt</div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">Ein anderer Nutzer lädt gerade aktiv an dieser Station.</p>
            <div style="background: #f1f5f9; padding: 12px; border-radius: 14px; margin-bottom: 16px; text-align: left; font-size: 13px;">
                Aktuelle Leistung: <b id="busyWatt">0.000 W</b>
            </div>
            <button class="btn-start" id="btnRequestSlot" style="background: var(--accent-primary);" onclick="requestSlot()">🔔 Nutzer anfragen (Bescheid geben)</button>
            <div id="requestSentText" style="display:none; color: var(--accent-green); font-size: 12px; font-weight: 600; margin-top: 10px;">✅ Anfrage gesendet! Der aktive Nutzer wurde benachrichtigt.</div>
        </div>

        <!-- WARTE-KARTE FÜR PAUSIERTEN NUTZER 1 -->
        <div class="card pause-wait-card" id="pauseWaitCard">
            <div style="font-size: 48px; margin-bottom: 10px;">⏸️⏳</div>
            <div class="title" style="margin-bottom: 6px;">Sitzung pausiert</div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">Deine bisherige Messung ist gesichert. Die Steckdose wird derzeit von einem anderen Nutzer verwendet.</p>
            <div style="background: #f1f5f9; padding: 12px; border-radius: 14px; margin-bottom: 16px; text-align: left; font-size: 13px;">
                Bisheriger Verbrauch: <b id="pauseWh">0.0000 Wh</b><br>
                Bisherige Kosten: <b id="pauseCost">0.00000 €</b>
            </div>
            <p style="font-size: 12px; color: var(--accent-primary); font-weight: 600; margin-bottom: 12px;">Sobald die Station wieder frei ist, wirst du automatisch hierher zurückgeleitet und kannst fortsetzen!</p>
            <button class="btn-finish" onclick="finalizeTermination()">🛑 Jetzt endgültig beenden & abrechnen</button>
        </div>
    </div>

    <script>
        let isTerminated = false;
        let isWaitingForResume = false;
        let lastReport = null;
        let transferModalOpen = false;
        let eightyModalDismissed = false;
        let startPromptShown = false;
        let stationToken = 'SEC-STATION-2026-X99Q-ALPHA-77';
        
        let userId = localStorage.getItem('hub_user_id');
        if (!userId) {
            userId = 'usr_' + Math.random().toString(36).substr(2, 9) + Date.now();
            localStorage.setItem('hub_user_id', userId);
        }

        // --- POLLING INTERVAL ---
        let syncInterval = setInterval(fetchSyncData, 1000);
        sendAction('/init_detection');

        // --- SCHLIESSEN-LOGIK (SOFORTIGES BEENDEN) ---
        function closeAppEarly() {
            clearInterval(syncInterval);
            try { window.open('','_self').close(); } catch (e) {}
            document.body.innerHTML = `
                <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; text-align:center; background:#0f172a; color:#f8fafc; margin: -18px -12px;">
                    <div style="font-size: 60px; margin-bottom:20px;">🚪</div>
                    <h2 style="margin-bottom: 10px; color: #38bdf8;">Abgebrochen</h2>
                    <p style="color: #94a3b8; max-width:280px; font-size: 15px; line-height: 1.5;">Kein Stromfluss gestartet.<br><br>Bitte scanne den QR-Code erneut, falls du doch laden möchtest.</p>
                </div>
            `;
            document.body.style.background = '#0f172a';
            sendAction('/stop'); 
        }

        function closeApp() {
            clearInterval(syncInterval);
            try { window.open('','_self').close(); } catch (e) {}
            document.body.innerHTML = `
                <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; text-align:center; background:#0f172a; color:#f8fafc; margin: -18px -12px;">
                    <div style="font-size: 60px; margin-bottom:20px;">🔒</div>
                    <h2 style="margin-bottom: 10px; color: #38bdf8;">Sitzung sicher beendet</h2>
                    <p style="color: #94a3b8; max-width:280px; font-size: 15px; line-height: 1.5;">Der Stromfluss ist abgeschaltet. Du kannst diesen Browser-Tab oder das Fenster nun problemlos schließen.</p>
                </div>
            `;
            document.body.style.background = '#0f172a';
        }

        async function confirmStart() {
            document.getElementById('plugDetectedModal').style.display = 'none';
            startPromptShown = false;
            await startSession();
        }

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
                    headers: { 'Content-Type': 'application/json', 'X-Station-Token': stationToken, 'X-User-Id': userId },
                    body: JSON.stringify(data)
                });
                return await res.json();
            } catch(e) { return {}; }
        }

        async function startSession() {
            if (isTerminated) return;
            document.getElementById('statusBadge').className = "status-pill status-on";
            document.getElementById('statusText').innerText = "Aktiv / Strom fließt";
            await sendAction('/start');
            fetchSyncData();
        }

        async function pauseSession() {
            if (isTerminated) return;
            document.getElementById('statusBadge').className = "status-pill status-off";
            document.getElementById('statusText').innerText = "Pausiert / Bereit";
            await sendAction('/stop');
            fetchSyncData();
        }

        async function saveDeviceProfile(key) {
            document.getElementById('deviceModal').style.display = 'none';
            let res = await sendAction('/save_device', { key: key });
            applyProfile(key, res.profile);
        }

        function applyProfile(key, prof) {
            document.getElementById('devIcon').innerText = prof.icon;
            document.getElementById('detectedName').innerText = prof.name;
            document.getElementById('detectedMode').innerText = prof.is_battery ? "🔋 Akku-Ladeüberwachung" : "💡 Dauerbetrieb";
            
            document.getElementById('batteryCard').style.display = prof.is_battery ? 'block' : 'none';
            document.getElementById('continuousPrediction').style.display = prof.is_battery ? 'none' : 'block';
            
            if (!prof.is_battery) {
                document.getElementById('batteryRemWhBox').style.display = 'none';
                document.getElementById('predictedCostBox').style.display = 'none';
                document.getElementById('microCostBox').style.display = 'block';
            }
        }

        function dismissEightyModal() { eightyModalDismissed = true; document.getElementById('eightyModal').style.display = 'none'; }

        async function requestSlot() {
            await sendAction('/request_transfer');
            document.getElementById('requestSentText').style.display = 'block';
            document.getElementById('btnRequestSlot').disabled = true;
            document.getElementById('btnRequestSlot').style.opacity = '0.6';
        }

        async function acceptTransfer() {
            transferModalOpen = false; document.getElementById('transferModal').style.display = 'none';
            lastReport = (await sendAction('/accept_transfer')).report;
            setWaitingMode();
        }

        async function rejectTransfer() {
            transferModalOpen = false; document.getElementById('transferModal').style.display = 'none';
            await sendAction('/reject_transfer');
        }

        async function logout(finalTerminate = false) {
            try {
                let report = await sendAction('/logout', { final: finalTerminate });
                lastReport = report;
                
                document.getElementById('rDevice').innerText = document.getElementById('detectedName').innerText;
                document.getElementById('rMode').innerText = document.getElementById('detectedMode').innerText;
                document.getElementById('rTime').innerText = report.time_formatted;
                document.getElementById('rWh').innerText = report.wh.toFixed(4) + " Wh";
                document.getElementById('rKwh').innerText = report.kwh.toFixed(6) + " kWh";
                document.getElementById('rCost').innerText = report.cost.toFixed(5) + " €";
                
                document.getElementById('mainCard').style.display = 'none';
                document.getElementById('busyCard').style.display = 'none';
                document.getElementById('pauseWaitCard').style.display = 'none';
                document.getElementById('fullModal').style.display = 'none';
                document.getElementById('eightyModal').style.display = 'none';
                document.getElementById('receiptCard').style.display = 'block';

                if (finalTerminate) {
                    isTerminated = true;
                    document.getElementById('receiptSubtitle').innerText = "Sitzung endgültig abgeschlossen";
                    document.getElementById('transferChoiceBox').style.display = 'none';
                } else {
                    document.getElementById('transferChoiceBox').style.display = 'flex';
                }
            } catch(e) {}
        }

        async function startNewSessionCompletely() {
            await sendAction('/new_session');
            localStorage.removeItem('hub_user_id');
            window.location.href = '/';
        }

        function setWaitingMode() {
            isWaitingForResume = true;
            document.getElementById('receiptCard').style.display = 'none';
            document.getElementById('mainCard').style.display = 'none';
            document.getElementById('busyCard').style.display = 'none';
            if (lastReport) {
                document.getElementById('pauseWh').innerText = lastReport.wh.toFixed(4) + " Wh";
                document.getElementById('pauseCost').innerText = lastReport.cost.toFixed(5) + " €";
            }
            document.getElementById('pauseWaitCard').style.display = 'block';
        }

        async function finalizeTermination() {
            isTerminated = true; isWaitingForResume = false;
            await sendAction('/finalize');
            document.getElementById('pauseWaitCard').style.display = 'none';
            document.getElementById('receiptSubtitle').innerText = "Sitzung endgültig abgeschlossen";
            document.getElementById('transferChoiceBox').style.display = 'none';
            document.getElementById('receiptCard').style.display = 'block';
        }

        async function sendInvoiceEmail() {
            let email = document.getElementById('emailInput').value.trim();
            let fb = document.getElementById('emailFeedback');
            if (!email || !email.includes('@')) {
                fb.style.display = 'block'; fb.style.color = 'var(--accent-red)'; fb.innerText = 'Gültige E-Mail erforderlich.'; return;
            }
            fb.style.display = 'block'; fb.style.color = 'var(--accent-primary)'; fb.innerText = 'Versende...';
            let res = await sendAction('/send_email_invoice', { email: email, report: lastReport, device: document.getElementById('rDevice').innerText, mode: document.getElementById('rMode').innerText });
            fb.style.color = res.status === 'ok' ? 'var(--accent-green)' : 'var(--accent-amber)';
            fb.innerText = res.status === 'ok' ? '✅ Erfolgreich gesendet!' : res.message;
        }

        function downloadInvoicePdf() { window.open('/download_invoice', '_blank'); }

        async function fetchSyncData() {
            if (isTerminated) return;
            try {
                let res = await fetch('/status', { cache: 'no-store', headers: { 'X-Station-Token': stationToken, 'X-User-Id': userId } });
                let data = await res.json();

                if (data.session_terminated) { await logout(true); return; }

                if (isWaitingForResume) {
                    if (!data.is_busy_for_other) {
                        isWaitingForResume = false;
                        document.getElementById('pauseWaitCard').style.display = 'none';
                        document.getElementById('mainCard').style.display = 'block';
                        document.getElementById('statusBadge').className = "status-pill status-off";
                        document.getElementById('statusText').innerText = "Bereit zum Fortsetzen";
                    }
                    return;
                }

                if (data.is_busy_for_other) {
                    document.getElementById('mainCard').style.display = 'none';
                    document.getElementById('receiptCard').style.display = 'none';
                    document.getElementById('busyCard').style.display = 'block';
                    document.getElementById('busyWatt').innerText = data.global_watt.toFixed(3) + " W";
                    return;
                } else if (document.getElementById('receiptCard').style.display !== 'block') {
                    document.getElementById('busyCard').style.display = 'none';
                    document.getElementById('mainCard').style.display = 'block';
                }

                if (data.show_start_prompt && !startPromptShown && !data.active) {
                    document.getElementById('plugDetectedModal').style.display = 'flex';
                    startPromptShown = true;
                } else if (!data.show_start_prompt && startPromptShown) {
                    document.getElementById('plugDetectedModal').style.display = 'none';
                    startPromptShown = false;
                }

                if (data.transfer_requested && !transferModalOpen) { transferModalOpen = true; document.getElementById('transferModal').style.display = 'flex'; }
                if (data.current_profile) applyProfile(data.current_profile_key, data.current_profile);

                updateTimerUI(data.elapsed_seconds);

                document.getElementById('volt').innerText = data.voltage.toFixed(1);
                document.getElementById('amp').innerText = data.current_ampere.toFixed(3);
                document.getElementById('milliAmp').innerText = (data.current_ampere * 1000.0).toFixed(0);
                document.getElementById('watt').innerText = data.watt.toFixed(3);
                document.getElementById('wh').innerText = data.wh.toFixed(4);
                document.getElementById('mwh').innerText = (data.wh * 1000.0).toFixed(1);
                document.getElementById('cost').innerText = data.cost.toFixed(5);
                document.getElementById('microCost').innerText = (data.cost * 100.0).toFixed(3);

                // KÖNIGSDISZIPLIN: Vorhersage für fehlende Wh und totale Kosten (Nur bei Akku)
                if (data.current_profile && data.current_profile.is_battery && (data.elapsed_seconds >= data.analysis_threshold || data.manually_selected)) {
                    document.getElementById('batteryRemWh').innerText = data.rem_wh.toFixed(1);
                    document.getElementById('batteryRemWhBox').style.display = 'inline';
                    
                    document.getElementById('predictedCost').innerText = data.predicted_cost.toFixed(4);
                    document.getElementById('predictedCostBox').style.display = 'block';
                    document.getElementById('microCostBox').style.display = 'none';
                } else if (data.current_profile && !data.current_profile.is_battery) {
                    // KÖNIGSDISZIPLIN TEIL 2: Prognose für Dauerbetrieb (Lampe etc.)
                    document.getElementById('pred1hWh').innerText = data.pred_1h_wh.toFixed(1);
                    document.getElementById('pred1hCost').innerText = data.pred_1h_cost.toFixed(4);
                    
                    let wh24 = data.pred_24h_wh;
                    document.getElementById('pred24hWh').innerText = wh24 >= 1000 ? (wh24/1000).toFixed(2) + " kWh" : wh24.toFixed(1) + " Wh";
                    document.getElementById('pred24hCost').innerText = data.pred_24h_cost.toFixed(2);
                } else {
                    document.getElementById('batteryRemWhBox').style.display = 'none';
                    document.getElementById('predictedCostBox').style.display = 'none';
                    document.getElementById('microCostBox').style.display = 'block';
                }

                // ANTI-RUCKEL UI & LIVE-MONITORING ANZEIGE
                if (data.active && data.elapsed_seconds < data.analysis_threshold && !data.manually_selected) {
                    let remain = Math.max(0, Math.floor(data.analysis_threshold - data.elapsed_seconds));
                    document.getElementById('aiStatusTitle').innerText = `AI Erst-Analyse (${data.analysis_threshold}s)`;
                    document.getElementById('detectedName').innerText = `Ladekurve... (${remain}s)`;
                    
                    if (data.current_profile && data.current_profile.is_battery) {
                        document.getElementById('batteryPhaseText').innerText = "KI misst Profil...";
                        document.getElementById('batteryPercentText').innerText = "---%";
                        document.getElementById('batteryTimeRemaining').innerText = "Restzeit: wird berechnet";
                        document.getElementById('batteryWhLoaded').innerText = "---";
                    }
                } else if (data.active && !data.manually_selected) {
                    document.getElementById('aiStatusTitle').innerText = `🤖 AI Live-Überwachung`;
                } else if (data.manually_selected) {
                    document.getElementById('aiStatusTitle').innerText = `Vom Nutzer angelernt`;
                }

                // WENN ERSTANALYSE FERTIG IST -> WERTE ANZEIGEN
                if (data.current_profile && data.current_profile.is_battery && (data.elapsed_seconds >= data.analysis_threshold || data.manually_selected)) {
                    let pct = data.battery_pct;
                    document.getElementById('batteryPhaseText').innerText = data.charge_phase;
                    document.getElementById('batteryPercentText').innerText = "~" + pct.toFixed(0) + "%";
                    document.getElementById('batteryBarFill').style.width = pct.toFixed(1) + "%";
                    document.getElementById('batteryWhLoaded').innerText = `${data.wh.toFixed(1)} / ${data.current_profile.capacity_wh.toFixed(0)} Wh`;
                    document.getElementById('batteryTimeRemaining').innerText = `Restzeit: ${data.remaining_time_str}`;

                    if (pct >= 80.0 && pct < 99.0 && !eightyModalDismissed && !data.battery_full_triggered) {
                        document.getElementById('eightyModal').style.display = 'flex';
                    }
                }

                let badge = document.getElementById('statusBadge');
                let statusText = document.getElementById('statusText');
                let startBtn = document.getElementById('mainStartBtn');

                if (data.unplugged_detected) {
                    badge.className = "status-pill status-unplug";
                    statusText.innerText = "🔌 Kabel ausgesteckt";
                    document.getElementById('wattSub').innerText = "Kein Stromfluss gemessen";
                    startBtn.innerText = "▶️ Kabel wieder drin? Fortsetzen";
                } else if (data.active) {
                    badge.className = "status-pill status-on";
                    statusText.innerText = "Aktiv / Strom fließt";
                    document.getElementById('wattSub').innerText = data.watt > 0.1 ? "Fließt stabil" : "Bereit / Standby";
                    startBtn.innerText = "▶️ Läuft bereits";
                } else {
                    badge.className = "status-pill status-off";
                    statusText.innerText = "Bereit für Gerät";
                    document.getElementById('wattSub').innerText = "Stecke ein Kabel ein...";
                    startBtn.innerText = "▶️ Start / Fortsetzen";
                }

                if (data.battery_full_triggered) {
                    document.getElementById('eightyModal').style.display = 'none';
                    document.getElementById('fullModal').style.display = 'flex';
                }

            } catch(e) {}
        }
        
    </script>
</body>
</html>
"""

def get_user_data():
    uid = request.headers.get("X-User-Id", session.get("user_id", str(uuid.uuid4())))
    session["user_id"] = uid
    if uid not in user_sessions:
        user_sessions[uid] = {
            "active": False, "terminated": False, "paused_by_transfer": False, "unplugged_detected": False, "had_power_draw": False,
            "zero_power_counter": 0.0, "battery_full_counter": 0.0, "total_kwh": 0.0, "total_seconds": 0.0,
            "current_watt": 0.0, "smoothed_watt": 0.0, "current_ampere": 0.0, "current_voltage": 230.0,
            "device_key": "phone", "analysis_samples": [], "analysis_completed": False, "manually_selected": False,
            "estimated_soc_0": 0.0, "battery_full_triggered": False, "eighty_percent_triggered": False, "last_report": None,
            "detection_mode": False, "show_start_prompt": False, "last_seen": time.time(), "session_peak_watt": 0.0,
            "recent_samples": []
        }
    return user_sessions[uid], uid

@app.route('/')
def home():
    if request.args.get("token") == STATION_PHYSICAL_TOKEN:
        session["authenticated_on_site"] = True
        session["station_token"] = STATION_PHYSICAL_TOKEN
        session.setdefault("user_id", str(uuid.uuid4()))
    if not check_authenticated(): return render_template_string(HTML_ACCESS_DENIED), 403
    get_user_data()
    return render_template_string(HTML_PAGE)

@app.route('/scan/<token>')
def scan_qr_entry(token):
    if token != STATION_PHYSICAL_TOKEN: return render_template_string(HTML_ACCESS_DENIED), 403
    session["authenticated_on_site"] = True
    session["station_token"] = STATION_PHYSICAL_TOKEN
    get_user_data()
    return render_template_string(HTML_PAGE)

@app.route('/init_detection', methods=['POST'])
@require_physical_auth
def init_detection():
    ensure_worker()
    u, _ = get_user_data()
    if not u.get("active") and not u.get("terminated"):
        u["detection_mode"] = True
        async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/start', methods=['POST'])
@require_physical_auth
def start():
    ensure_worker()
    u, uid = get_user_data()
    if u.get("terminated", False): return jsonify({"status": "forbidden"}), 403
    if global_state.get("active_user_id") and global_state["active_user_id"] != uid: return jsonify({"status": "busy"})
    global_state.update({"active_user_id": uid, "transfer_requested": False, "transfer_requester_id": None})
    u.update({"active": True, "unplugged_detected": False, "zero_power_counter": 0.0, "paused_by_transfer": False, "detection_mode": False, "show_start_prompt": False})
    async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST'])
@require_physical_auth
def stop():
    ensure_worker()
    u, uid = get_user_data()
    if u.get("terminated", False) or global_state.get("active_user_id") != uid: return jsonify({"status": "forbidden"}), 403
    u["active"] = False
    u["detection_mode"] = False
    u["show_start_prompt"] = False
    async_cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/save_device', methods=['POST'])
@require_physical_auth
def save_device():
    u, uid = get_user_data()
    key = (request.get_json() or {}).get("key", "lamp")
    if key in DEVICE_PROFILES:
        u.update({"device_key": key, "manually_selected": True, "analysis_completed": True})
        if u.get("recent_samples"):
            save_ai_models(load_ai_models()) 
            u["estimated_soc_0"] = estimate_current_soc(key, u["recent_samples"])
    return jsonify({"status": "saved", "profile": DEVICE_PROFILES.get(u["device_key"])})

@app.route('/request_transfer', methods=['POST'])
@require_physical_auth
def request_transfer():
    _, uid = get_user_data()
    if global_state["active_user_id"] and global_state["active_user_id"] != uid:
        global_state.update({"transfer_requested": True, "transfer_requester_id": uid})
    return jsonify({"status": "requested"})

@app.route('/accept_transfer', methods=['POST'])
@require_physical_auth
def accept_transfer():
    u, uid = get_user_data()
    u.update({"active": False, "paused_by_transfer": True})
    async_cloud_control(turn_on=False)
    global_state.update({"active_user_id": global_state.get("transfer_requester_id"), "transfer_requested": False, "transfer_requester_id": None})
    sec = int(u["total_seconds"])
    report = {
        "invoice_id": f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}",
        "time_formatted": f"{str(sec//3600).zfill(2)}:{str((sec%3600)//60).zfill(2)}:{str(sec%60).zfill(2)}",
        "wh": u["total_kwh"] * 1000.0, "kwh": u["total_kwh"], "cost": u["total_kwh"] * STROMPREIS_PER_KWH
    }
    u["last_report"] = report
    return jsonify({"status": "ok", "report": report})

@app.route('/reject_transfer', methods=['POST'])
@require_physical_auth
def reject_transfer():
    global_state.update({"transfer_requested": False, "transfer_requester_id": None})
    return jsonify({"status": "rejected"})

@app.route('/logout', methods=['POST'])
@require_physical_auth
def logout():
    u, uid = get_user_data()
    u["active"] = False
    u["detection_mode"] = False
    if (request.get_json() or {}).get("final", False): u["terminated"] = True
    if global_state["active_user_id"] == uid:
        global_state.update({"active_user_id": None, "transfer_requested": False, "transfer_requester_id": None})
    async_cloud_control(turn_on=False)
    sec = int(u["total_seconds"])
    report = {
        "invoice_id": f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}",
        "time_formatted": f"{str(sec//3600).zfill(2)}:{str((sec%3600)//60).zfill(2)}:{str(sec%60).zfill(2)}",
        "wh": u["total_kwh"] * 1000.0, "kwh": u["total_kwh"], "cost": u["total_kwh"] * STROMPREIS_PER_KWH
    }
    u["last_report"] = report
    return jsonify(report)

@app.route('/finalize', methods=['POST'])
@require_physical_auth
def finalize():
    u, _ = get_user_data()
    u.update({"terminated": True, "paused_by_transfer": False})
    return jsonify({"status": "finalized"})

@app.route('/new_session', methods=['POST'])
@require_physical_auth
def new_session():
    u, uid = get_user_data()
    u.update({"terminated": False, "active": False, "total_kwh": 0.0, "total_seconds": 0.0, "analysis_samples": [], "recent_samples": [], "session_peak_watt": 0.0, "analysis_completed": False, "manually_selected": False, "battery_full_triggered": False, "eighty_percent_triggered": False})
    return jsonify({"status": "ok"})

@app.route('/download_invoice', methods=['GET'])
@require_physical_auth
def download_invoice():
    u, _ = get_user_data()
    report = u.get("last_report") or {"invoice_id": "RE-SAMPLE", "device": "📱 Smartphone / Tablet", "mode": "Akku-Ladeüberwachung", "time_formatted": "00:15:00", "wh": 10.5, "kwh": 0.0105, "cost": 0.00367}
    return send_file(generate_pdf_invoice(report), mimetype="application/pdf", as_attachment=True, download_name=f"{report.get('invoice_id')}.pdf")

@app.route('/send_email_invoice', methods=['POST'])
@require_physical_auth
def send_email_invoice():
    data = request.get_json() or {}
    if "@" not in data.get("email", ""): return jsonify({"status": "error", "message": "Ungültige E-Mail-Adresse"})
    if not SMTP_USER: return jsonify({"status": "error", "message": "SMTP nicht konfiguriert. Bitte PDF laden."})
    
    report = data.get("report", {})
    report.update({"device": data.get("device", "-"), "mode": data.get("mode", "-")})
    
    try:
        msg = MIMEMultipart()
        msg["From"], msg["To"], msg["Subject"] = SMTP_USER, data["email"], f"Stromquittung ({report.get('invoice_id')})"
        msg.attach(MIMEText(f"Verbrauch: {report.get('wh',0):.2f} Wh\nKosten: {report.get('cost',0):.5f} €\nDetails im PDF.", "plain", "utf-8"))
        pdf = MIMEApplication(generate_pdf_invoice(report).read(), _subtype="pdf")
        pdf.add_header('Content-Disposition', 'attachment', filename=f"{report.get('invoice_id')}.pdf")
        msg.attach(pdf)
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=8)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": "Fehler beim Versand."})

@app.route('/status')
@require_physical_auth
def status():
    ensure_worker()
    u, uid = get_user_data()
    active_uid = global_state.get("active_user_id")
    u["last_seen"] = time.time()

    if u.get("terminated", False): return jsonify({"session_terminated": True, "is_busy_for_other": False})
    
    is_busy = active_uid and active_uid != uid
    dev_key = u.get("device_key", "phone")
    prof = DEVICE_PROFILES.get(dev_key, DEVICE_PROFILES["phone"])
    
    wh, cap = u["total_kwh"] * 1000.0, prof.get("capacity_wh", 20.0)
    battery_pct = min(100.0, u.get("estimated_soc_0", 0.0) + ((wh / max(1, cap)) * 100.0)) if prof.get("is_battery") else 0.0
    
    curr_w = u.get("smoothed_watt", 0.0)
    remaining_str = "--"
    charge_phase = "Bereit"
    rem_wh = 0.0
    predicted_cost = 0.0
    
    # NEU: Prognose Variablen für Dauerbetrieb (Lampe, Großgerät)
    pred_1h_wh = 0.0
    pred_1h_cost = 0.0
    pred_24h_wh = 0.0
    pred_24h_cost = 0.0
    
    if prof.get("is_battery"):
        # Ladephase berechnen für Akkus
        if battery_pct >= 95.0: charge_phase = "Erhaltungsladung"
        elif battery_pct >= 80.0: charge_phase = "Sättigung (CV)"
        elif battery_pct >= 30.0: charge_phase = "Normalladung (CC)"
        else: charge_phase = "Schnellladung (Bulk)"

        if battery_pct >= 100.0: remaining_str = "100% Voll"
        elif curr_w > 1.0:
            rem_mins = int((((100.0 - battery_pct) / 100.0) * cap) / curr_w * 60)
            remaining_str = f"ca. {rem_mins//60}h {rem_mins%60}m" if rem_mins >= 60 else f"ca. {rem_mins} Min."
            
        rem_wh = max(0.0, cap * (100.0 - battery_pct) / 100.0)
        predicted_cost = (u["total_kwh"] + (rem_wh / 1000.0)) * STROMPREIS_PER_KWH
    else:
        # KÖNIGSDISZIPLIN TEIL 2: Prognose-Berechnung für Dauerbetrieb
        pred_1h_wh = curr_w * 1.0
        pred_1h_cost = (pred_1h_wh / 1000.0) * STROMPREIS_PER_KWH
        pred_24h_wh = curr_w * 24.0
        pred_24h_cost = (pred_24h_wh / 1000.0) * STROMPREIS_PER_KWH

    return jsonify({
        "active": u["active"] and not is_busy,
        "unplugged_detected": u.get("unplugged_detected", False),
        "watt": curr_w if not is_busy else 0.0,
        "current_ampere": u.get("current_ampere", 0.0),
        "voltage": u.get("current_voltage", 230.0),
        "global_watt": global_state["last_watt"] if is_busy else 0.0,
        "wh": wh, "kwh": u["total_kwh"], "cost": u["total_kwh"] * STROMPREIS_PER_KWH,
        "elapsed_seconds": int(u["total_seconds"]),
        "is_busy_for_other": is_busy,
        "is_paused_by_transfer": u.get("paused_by_transfer", False),
        "transfer_requested": global_state.get("transfer_requested") and not is_busy,
        "current_profile_key": dev_key, "current_profile": prof,
        "battery_pct": battery_pct, "remaining_time_str": remaining_str,
        "charge_phase": charge_phase,
        "rem_wh": rem_wh,
        "predicted_cost": predicted_cost,
        "pred_1h_wh": pred_1h_wh,
        "pred_1h_cost": pred_1h_cost,
        "pred_24h_wh": pred_24h_wh,
        "pred_24h_cost": pred_24h_cost,
        "battery_full_triggered": u.get("battery_full_triggered", False),
        "analysis_completed": u.get("analysis_completed", False),
        "analysis_threshold": 40.0,
        "manually_selected": u.get("manually_selected", False),
        "session_terminated": False,
        "show_start_prompt": u.get("show_start_prompt", False)
    })

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
