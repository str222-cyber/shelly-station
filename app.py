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
from datetime import datetime
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

# --- SECRETS & TOKENS ---
STATION_PHYSICAL_TOKEN = "SEC-STATION-2026-X99Q-ALPHA-77"
ADMIN_DASHBOARD_TOKEN = "SEC-ADMIN-MASTER-2026-OMEGA"

# --- SHELLY CLOUD KONFIGURATION ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

STROMPREIS_PER_KWH = 0.35

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = ""

HISTORY_FILE = "charge_history.json" 

DEVICE_PROFILES = {
    "phone": {"name": "📱 Smartphone / Tablet", "icon": "📱", "is_battery": True, "capacity_wh": 20.0},
    "laptop": {"name": "💻 Laptop / Monitor", "icon": "💻", "is_battery": True, "capacity_wh": 65.0},
    "ebike_std": {"name": "🚲 E-Bike Akku Standard", "icon": "🚲", "is_battery": True, "capacity_wh": 500.0},
    "other_battery": {"name": "🔋 Anderes Gerät (Mit Akku)", "icon": "🔋", "is_battery": True, "capacity_wh": 100.0},
    "lamp": {"name": "💡 Lampe / Leuchte", "icon": "💡", "is_battery": False, "capacity_wh": 0},
    "tv": {"name": "📺 Fernseher / Display", "icon": "📺", "is_battery": False, "capacity_wh": 0},
    "other_continuous": {"name": "🔌 Anderes Gerät (Dauerbetrieb)", "icon": "🔌", "is_battery": False, "capacity_wh": 0}
}

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f: return json.load(f)
        except: pass
    return []

def append_to_history(record):
    hist = load_history()
    hist.append(record)
    try:
        with open(HISTORY_FILE, "w") as f: json.dump(hist, f, indent=2)
    except: pass

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

def save_sub_session(u, uid):
    if u["total_kwh"] > 0.0001: 
        prof_name = DEVICE_PROFILES.get(u["device_key"], {}).get("name", "Unbekannt")
        c_name = u.get("custom_name", "").strip()
        final_name = f"{c_name} ({prof_name})" if c_name else prof_name
        
        record = {
            "session_id": str(uuid.uuid4())[:8],
            "user_id": uid,
            "date": time.strftime('%Y-%m-%d'),
            "timestamp": time.strftime('%H:%M:%S'),
            "device_key": u["device_key"],
            "device_name": final_name,
            "wh": u["total_kwh"] * 1000.0,
            "cost": u["total_kwh"] * STROMPREIS_PER_KWH,
            "duration_sec": u["total_seconds"]
        }
        if "completed_sub_sessions" not in u: u["completed_sub_sessions"] = []
        u["completed_sub_sessions"].append(record)
        append_to_history(record) 

    u["total_kwh"] = 0.0
    u["total_seconds"] = 0.0
    u["analysis_samples"] = []
    u["recent_samples"] = []
    u["session_peak_watt"] = 0.0
    u["manually_selected"] = False
    u["battery_full_triggered"] = False
    u["eighty_percent_triggered"] = False
    u["device_key"] = "phone"
    u["custom_name"] = ""
    
def background_meter_worker():
    last_loop = time.time()
    while True:
        now = time.time()
        dt = max(1.0, min(5.0, now - last_loop))
        last_loop = now

        try:
            active_uid = global_state.get("active_user_id")
            
            for uid, u in user_sessions.items():
                if not u.get("active", False) and not u.get("detection_mode", False): continue
                
                watt, amp, volt = 0.0, 0.0, 230.0
                try:
                    res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data={"auth_key": AUTH_KEY, "id": DEVICE_ID}, timeout=2.0).json()
                    if res.get("isok"):
                        status = res.get("data", {}).get("device_status", {})
                        if "switch:0" in status:
                            watt, amp, volt = float(status["switch:0"].get("apower", 0.0)), float(status["switch:0"].get("current", 0.0)), float(status["switch:0"].get("voltage", 230.0))
                        elif "meters" in status and len(status["meters"]) > 0:
                            watt = float(status["meters"][0].get("power", 0.0))
                            amp = float(status["meters"][0].get("current", 0.0)) if "current" in status["meters"][0] else (watt / 230.0 if watt > 0 else 0.0)
                            volt = float(status["meters"][0].get("voltage", 230.0)) if "voltage" in status["meters"][0] else 230.0
                except:
                    watt, amp, volt = u.get("current_watt", 0.0), u.get("current_ampere", 0.0), u.get("current_voltage", 230.0)

                u["current_watt"], u["current_ampere"], u["current_voltage"] = watt, amp, volt
                u["smoothed_watt"] = watt if u.get("smoothed_watt") is None else (u["smoothed_watt"] * 0.8) + (watt * 0.2)
                
                if uid == active_uid: global_state["last_watt"], global_state["last_amp"], global_state["last_volt"] = watt, amp, volt

                if "recent_samples" not in u: u["recent_samples"] = []
                if watt > 0.1 or len(u["recent_samples"]) > 0:
                    u["recent_samples"].append(watt)
                    if len(u["recent_samples"]) > 40: u["recent_samples"].pop(0)

                u["session_peak_watt"] = max(u.get("session_peak_watt", 0.0), watt)

                # --- 1. DETECTION MODE (WARTEN AUF GERÄT) ---
                if not u["active"] and u.get("detection_mode", False):
                    if time.time() - u.get("last_seen", time.time()) > 15.0:
                        u["detection_mode"], u["show_start_prompt"] = False, False
                        async_cloud_control(turn_on=False)
                        continue
                    
                    # Sobald Strom fließt -> Gerät Auswahl Prompt zeigen
                    u["show_start_prompt"] = (watt > 0.5 or amp > 0.01)
                    continue 

                # --- 2. AKTIVE SESSION ---
                if u.get("active", False):
                    u["total_seconds"] += dt
                    if watt > 0.05: u["total_kwh"] += (watt * dt) / 3600000.0

                    # Dynamische SOC Nachkalibrierung im Hintergrund
                    if len(u["recent_samples"]) >= 5 and u.get("manually_selected"):
                        est_soc = estimate_current_soc(u["device_key"], u["recent_samples"])
                        cap = DEVICE_PROFILES.get(u["device_key"], {}).get("capacity_wh", 20.0)
                        current_calc_soc = u.get("estimated_soc_0", 0.0) + (((u.get("total_kwh", 0.0) * 1000.0) / max(1,cap)) * 100.0)
                        
                        if u["total_seconds"] <= 40.0: 
                            u["estimated_soc_0"] = est_soc - (((u.get("total_kwh", 0.0) * 1000.0) / max(1,cap)) * 100.0)
                        else:
                            if est_soc > current_calc_soc + 5.0: 
                                u["estimated_soc_0"] = est_soc - (((u.get("total_kwh", 0.0) * 1000.0) / max(1,cap)) * 100.0)

                    # --- ZUVERLÄSSIGE AUSSTECK-ERKENNUNG ---
                    if watt > 0.5 or amp > 0.01: 
                        u["had_power_draw"], u["zero_power_counter"] = True, 0.0
                        
                    if u.get("had_power_draw"):
                        if watt <= 0.1 and amp <= 0.005:
                            u["zero_power_counter"] += dt
                            if u["zero_power_counter"] >= 10.0: # 10s Harter Abbruch
                                u["active"], u["had_power_draw"] = False, False
                                save_sub_session(u, uid)
                                u["detection_mode"] = True # Wartet auf nächstes Gerät! Relais bleibt AN!
                                u["show_start_prompt"] = False
                        elif watt <= 0.2:
                            u["zero_power_counter"] += dt
                            if u["zero_power_counter"] >= 65.0: # 65s Weicher Abbruch 
                                u["active"], u["had_power_draw"] = False, False
                                save_sub_session(u, uid)
                                u["detection_mode"] = True
                                u["show_start_prompt"] = False
                        else:
                            u["zero_power_counter"] = 0.0

                    # 80% / 100% Limitierung für Akkus
                    prof = DEVICE_PROFILES.get(u.get("device_key"), {})
                    if prof.get("is_battery", False) and u["total_seconds"] > 40.0:
                        current_pct = u.get("estimated_soc_0", 0.0) + (((u["total_kwh"] * 1000.0) / prof.get("capacity_wh", 20.0)) * 100.0)
                        if current_pct >= 80.0 and not u.get("eighty_percent_triggered", False):
                            u["eighty_percent_triggered"], u["active"] = True, False
                            save_sub_session(u, uid)
                            u["detection_mode"] = True
                            async_cloud_control(turn_on=False) # Sicherheitshalber bei 80% aus

                        if watt > 5.0: u["had_charging_phase"] = True
                        if u.get("had_charging_phase", False) and 0.2 <= watt < 1.5 and (u["total_kwh"] * 1000.0) > 1.0:
                            u["battery_full_counter"] += dt
                            if u["battery_full_counter"] >= 20.0:
                                u["battery_full_triggered"], u["active"] = True, False
                                save_sub_session(u, uid)
                                u["detection_mode"] = True
                                async_cloud_control(turn_on=False)
                        else: u["battery_full_counter"] = 0.0
        except: pass
        time.sleep(1.0)

def generate_pdf_invoice(report_data):
    rows = ""
    for idx, item in enumerate(report_data.get('items', [])):
        rows += f"<tr><td>Gerät {idx+1}: {item['name']}</td><td>{item['wh']:.2f} Wh</td><td>{item['cost']:.4f} €</td></tr>"
        
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
    </style>
    </head>
    <body>
    <div class="header">
        <div class="brand">⚡ Smart Power Hub</div>
        <div class="meta">Gesamtquittung • Vorgangs-ID: {report_data.get('invoice_id')} • Datum: {time.strftime('%d.%m.%Y %H:%M')}</div>
    </div>
    <div style="font-size: 11pt; margin-bottom: 10px;">Geladene Geräte in dieser Sitzung:</div>
    <table>
        <thead><tr><th>Position / Gerät</th><th>Verbrauch (Wh)</th><th>Kosten</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    <div class="total">
        <div class="total-title">Gesamtsumme (bei {STROMPREIS_PER_KWH:.2f} €/kWh)</div>
        <div class="total-val">{report_data.get('total_cost', 0.0):.5f} €</div>
    </div>
    </body>
    </html>
    """
    pdf_buffer = io.BytesIO()
    HTML(string=html_invoice).write_pdf(pdf_buffer)
    pdf_buffer.seek(0)
    return pdf_buffer


# --- ADMIN ROUTE & TEMPLATE ---
HTML_ADMIN = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8"><title>Betreiber Dashboard</title><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: white; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        .card { background: #1e293b; border-radius: 16px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
        h1 { color: #38bdf8; margin-top: 0; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .stat { background: #0f172a; padding: 15px; border-radius: 12px; border: 1px solid #334155; text-align: center; }
        .stat-val { font-size: 24px; font-weight: bold; color: #10b981; margin-top: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Betreiber Dashboard</h1>
        <div class="card">
            <h3>Heutige Statistik ({{ date }})</h3>
            <div class="grid">
                <div class="stat">Umsatz Heute<div class="stat-val">{{ "%.4f"|format(total_euro) }} €</div></div>
                <div class="stat">Energie Heute<div class="stat-val" style="color:#3b82f6;">{{ "%.2f"|format(total_wh) }} Wh</div></div>
                <div class="stat">Ladevorgänge<div class="stat-val" style="color:#f59e0b;">{{ total_sessions }}</div></div>
                <div class="stat">Aktive User<div class="stat-val" style="color:#8b5cf6;">{{ total_users }}</div></div>
            </div>
        </div>
        <div class="card">
            <h3>Geräte-Breakdown (Heute)</h3>
            <table>
                <tr><th>Gerätetyp</th><th>Anzahl</th><th>Verbrauch (Wh)</th><th>Umsatz (€)</th></tr>
                {% for dev, data in device_stats.items() %}
                <tr><td>{{ data.name }}</td><td>{{ data.count }}</td><td>{{ "%.2f"|format(data.wh) }}</td><td>{{ "%.4f"|format(data.cost) }}</td></tr>
                {% endfor %}
            </table>
        </div>
    </div>
</body>
</html>
"""

@app.route(f'/admin/{ADMIN_DASHBOARD_TOKEN}')
def admin_dashboard():
    history = load_history()
    today = time.strftime('%Y-%m-%d')
    today_records = [r for r in history if r.get("date") == today]
    
    total_wh = sum(r.get("wh", 0) for r in today_records)
    total_euro = sum(r.get("cost", 0) for r in today_records)
    total_sessions = len(today_records)
    total_users = len(set(r.get("user_id") for r in today_records))
    
    device_stats = {}
    for r in today_records:
        k = r.get("device_name", "Unbekannt")
        if k not in device_stats: device_stats[k] = {"name": k, "count": 0, "wh": 0.0, "cost": 0.0}
        device_stats[k]["count"] += 1
        device_stats[k]["wh"] += r.get("wh", 0)
        device_stats[k]["cost"] += r.get("cost", 0)

    return render_template_string(HTML_ADMIN, date=today, total_wh=total_wh, total_euro=total_euro, total_sessions=total_sessions, total_users=total_users, device_stats=device_stats)

HTML_ACCESS_DENIED = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Zugriff Verweigert</title><meta name="viewport" content="width=device-width, initial-scale=1.0"><style>body { font-family: -apple-system, sans-serif; background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; padding: 20px; }.box { background: #1e293b; padding: 30px; border-radius: 20px; border: 1px solid #334155; }</style></head><body><div class="box"><div style="font-size: 50px;">🔒</div><h2 style="color: #f87171;">Sicherheits-Sperre</h2><p>Bitte scanne den QR-Code an der Ladestation.</p></div></body></html>"""

HTML_PAGE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Power Hub</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        :root { --bg-color: #f8fafc; --card-bg: #ffffff; --text-main: #0f172a; --text-muted: #64748b; --accent-primary: #2563eb; --accent-green: #059669; --accent-amber: #d97706; --accent-red: #dc2626; --accent-cyan: #0891b2; --border-color: #e2e8f0; --shadow-md: 0 10px 25px -5px rgba(15, 23, 42, 0.07); }
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; padding: 18px 12px; min-height: 100vh; }
        .container { width: 100%; max-width: 420px; margin: auto; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 22px 18px; box-shadow: var(--shadow-md); border: 1px solid var(--border-color); text-align: center; margin-bottom: 12px; }
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .title { font-size: 18px; font-weight: 700; color: var(--text-main); letter-spacing: -0.3px; }
        .rate-badge { background: #f1f5f9; color: var(--text-muted); font-size: 12px; padding: 4px 10px; border-radius: 20px; font-weight: 600; }
        .security-badge { background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 12px; padding: 5px 10px; font-size: 11px; color: #065f46; font-weight: 600; margin-bottom: 12px; display: inline-flex; align-items: center; gap: 6px; }
        
        .cart-box { background: #f1f5f9; border: 1px dashed #cbd5e1; border-radius: 14px; padding: 10px; margin-bottom: 12px; text-align: left; display: none; }
        .cart-item { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-main); padding: 4px 0; border-bottom: 1px solid #e2e8f0; }
        .cart-item:last-child { border: none; }
        
        .ai-banner { background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); border: 1px solid var(--border-color); border-radius: 18px; padding: 12px 14px; margin-bottom: 12px; text-align: left; }
        .ai-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
        .ai-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--accent-primary); letter-spacing: 0.5px; }
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

        .btn-group { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
        button { width: 100%; padding: 13px; font-size: 15px; font-weight: 600; border: none; border-radius: 14px; cursor: pointer; transition: transform 0.1s ease; }
        button:active { transform: scale(0.98); }
        .btn-start { background: var(--text-main); color: white; }
        .btn-stop { background: #f1f5f9; color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-finish { background: #fee2e2; color: var(--accent-red); }

        .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(4px); z-index: 999; padding: 20px; align-items: center; justify-content: center; }
        .modal-box { background: white; border-radius: 24px; padding: 24px 20px; text-align: center; max-width: 340px; width: 100%; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.2); }
        
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
    
    <!-- MANUELLE GERÄTE AUSWAHL BEIM EINSTECKEN -->
    <div id="deviceSelectionModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-cyan);">
            <div style="font-size: 48px; margin-bottom: 8px;">🔌⚡</div>
            <h2 style="font-size: 20px; color: var(--accent-cyan); margin-bottom: 6px;">Gerät erkannt!</h2>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 16px;">Was hast du soeben eingesteckt?</p>
            
            <select id="deviceTypeSelect" class="email-input" style="font-weight: bold; background: #f8fafc;">
                <option value="phone">📱 Smartphone / Tablet (Akku)</option>
                <option value="laptop">💻 Laptop / Monitor (Akku)</option>
                <option value="ebike_std">🚲 E-Bike Akku Standard</option>
                <option value="lamp">💡 Lampe / Leuchte (Dauerbetrieb)</option>
                <option value="tv">📺 TV / Display (Dauerbetrieb)</option>
                <option value="other_battery">🔋 Anderes Gerät (Mit Akku)</option>
                <option value="other_continuous">🔌 Anderes Gerät (Dauerbetrieb)</option>
            </select>

            <input type="text" id="customDeviceName" placeholder="Name (optional, z.B. Mein iPad)" class="email-input" style="margin-bottom: 16px;">
            
            <button class="btn-start" style="background: var(--accent-cyan); margin-bottom: 8px;" onclick="submitDeviceSelection()">▶️ Ladevorgang starten</button>
            <button class="btn-stop" onclick="cancelDeviceSelection()">❌ Abbrechen & Strom aus</button>
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
            <h2 style="font-size: 19px; color: var(--accent-primary); margin-bottom: 6px;">Gerät voll genug geladen (80%)</h2>
            <p style="font-size: 13px; margin-bottom: 12px;">Der Stromfluss wurde gestoppt. Bitte Gerät abstecken für den Akkuschutz.</p>
            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="document.getElementById('eightyModal').style.display='none'">✅ Okay, verstanden</button>
            <button class="btn-stop" onclick="dismissEightyModal()">Ignorieren & weiterladen</button>
        </div>
    </div>

    <div class="container">
        <!-- HAUPTKARTE -->
        <div class="card" id="mainCard">
            <div class="header">
                <span class="title">⚡ Smart Power Hub</span>
                <span class="rate-badge">{{ strompreis }} €/kWh</span>
            </div>
            
            <div id="statusBadge" class="status-pill status-off">
                <span class="status-dot"></span><span id="statusText">Bereit / Aus</span>
            </div>
            
            <!-- MULTI-SESSION WARENKORB -->
            <div id="cartBox" class="cart-box">
                <div style="font-size: 11px; font-weight:bold; margin-bottom: 4px; color: var(--text-muted);">Erfolgreich geladene Geräte (Diese Sitzung)</div>
                <div id="cartList"></div>
            </div>

            <!-- GERÄTE ERKENNUNG -->
            <div class="ai-banner">
                <div class="ai-header">
                    <span class="ai-title" id="aiStatusTitle">Gewähltes Gerät</span>
                </div>
                <div class="ai-body">
                    <div class="ai-icon" id="devIcon">📱</div>
                    <div>
                        <div class="ai-detected" id="detectedName">Smartphone</div>
                        <div class="ai-mode" id="detectedMode">Akku-Ladeüberwachung</div>
                    </div>
                </div>
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

            <!-- DAUERBETRIEB PROGNOSE -->
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

            <!-- MESSDATEN GRIDS -->
            <div class="grid-2">
                <div class="stat-card stat-volt"><div class="stat-label">Spannung (U)</div><div class="stat-val"><span id="volt">230.0</span> V</div></div>
                <div class="stat-card stat-amp"><div class="stat-label">Strom (I)</div><div class="stat-val"><span id="amp">0.000</span> A</div></div>
            </div>
            <div class="grid-2">
                <div class="stat-card stat-watt"><div class="stat-label">Wirkleistung (P)</div><div class="stat-val"><span id="watt">0.000</span> W</div><div class="stat-sub" id="wattSub">Kein Strom</div></div>
                <div class="stat-card"><div class="stat-label">Laufzeit</div><div class="stat-val" id="timer">00:00:00</div></div>
            </div>

            <!-- ENERGIE & KOSTEN -->
            <div class="grid-2">
                <div class="stat-card"><div class="stat-label">Verbrauch</div><div class="stat-val" style="color:var(--accent-primary);"><span id="wh">0.0000</span> Wh</div></div>
                <div class="stat-card stat-cost" style="background: #f0fdf4; border-color: #bbf7d0;">
                    <div class="stat-label" style="color: #166534;">Kosten (€)</div>
                    <div class="stat-val"><span id="cost">0.00000</span> €</div>
                    <div class="stat-sub" id="predictedCostBox" style="margin-top: 4px; color: #166534; font-weight: bold; display: none;">100% Prognose: <span id="predictedCost">0.0000</span> €</div>
                </div>
            </div>

            <div class="btn-group">
                <button class="btn-finish" onclick="logout(true)">🧾 Gesamtsitzung Beenden & Abrechnen</button>
                <button class="btn-stop" style="background: #fef2f2; color: var(--accent-red); border-color: #fecaca; margin-top: 8px;" onclick="closeAppEarly()">❌ Fenster komplett schließen</button>
            </div>
        </div>

        <!-- QUITTUNG (MULTI-SESSION) -->
        <div class="card receipt-card" id="receiptCard">
            <div class="receipt-header">
                <div style="font-size: 40px; margin-bottom: 4px;">🧾</div>
                <div class="title" id="receiptTitle">Lade- & Stromquittung</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 3px;" id="receiptSubtitle">Sitzung erfolgreich beendet</div>
            </div>
            
            <div id="receiptDynamicContent" style="margin-bottom: 20px;"></div>

            <div class="receipt-row receipt-total"><span>Gesamtsumme:</span> <span id="rTotalCost" style="color: var(--accent-green);">0.00000 €</span></div>

            <div class="email-box">
                <div style="font-size:12px; font-weight:700; margin-bottom:6px; color:var(--text-main);">📧 Rechnung als PDF zusenden:</div>
                <input type="email" id="emailInput" class="email-input" placeholder="deine-email@beispiel.de">
                <button class="btn-start" style="background:var(--accent-primary); font-size:14px; padding:11px;" onclick="sendInvoiceEmail()">Rechnung per E-Mail senden</button>
                <button class="btn-stop" style="font-size:13px; padding:8px; margin-top:6px;" onclick="downloadInvoicePdf()">📥 PDF direkt herunterladen</button>
                <div id="emailFeedback" style="display:none; font-size:12px; font-weight:600; margin-top:8px;"></div>
            </div>

            <div style="margin-top: 24px; display: flex; flex-direction: column; gap: 10px;">
                <button onclick="closeApp()" style="background: #ef4444; color: white; font-size: 16px; padding: 16px; font-weight: bold; border-radius: 14px; border: none; cursor: pointer;">❌ App komplett schließen</button>
                <button class="btn-stop" style="font-size: 14px; padding: 12px;" onclick="startNewSessionCompletely()">🔄 Neuer QR-Scan / Neustart</button>
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
    </div>

    <script>
        let isTerminated = false;
        let lastReport = null;
        let startPromptShown = false;
        let stationToken = 'SEC-STATION-2026-X99Q-ALPHA-77';
        
        let userId = localStorage.getItem('hub_user_id');
        if (!userId) { userId = 'usr_' + Math.random().toString(36).substr(2, 9) + Date.now(); localStorage.setItem('hub_user_id', userId); }

        let syncInterval = setInterval(fetchSyncData, 1000);
        sendAction('/init_detection');

        function closeAppEarly() {
            clearInterval(syncInterval);
            document.body.innerHTML = `<div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; text-align:center; background:#0f172a; color:#f8fafc; margin:-18px -12px;"><div style="font-size: 60px; margin-bottom:20px;">🚪</div><h2 style="color: #38bdf8;">Abgebrochen</h2><p>Du kannst das Fenster schließen.</p></div>`;
            sendAction('/stop'); 
        }

        function closeApp() {
            clearInterval(syncInterval);
            document.body.innerHTML = `<div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; text-align:center; background:#0f172a; color:#f8fafc; margin:-18px -12px;"><div style="font-size: 60px; margin-bottom:20px;">🔒</div><h2 style="color: #38bdf8;">Sicher beendet</h2><p>Du kannst das Fenster schließen.</p></div>`;
        }

        async function submitDeviceSelection() {
            let key = document.getElementById('deviceTypeSelect').value;
            let name = document.getElementById('customDeviceName').value;
            document.getElementById('deviceSelectionModal').style.display = 'none';
            startPromptShown = false;
            await sendAction('/start_device', {device_key: key, custom_name: name});
            fetchSyncData();
        }

        async function cancelDeviceSelection() {
            document.getElementById('deviceSelectionModal').style.display = 'none';
            startPromptShown = false;
            await sendAction('/stop');
        }

        function updateTimerUI(sec) {
            let h = Math.floor(sec / 3600).toString().padStart(2, '0');
            let m = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
            let s = Math.floor(sec % 60).toString().padStart(2, '0');
            document.getElementById('timer').innerText = `${h}:${m}:${s}`;
        }

        async function sendAction(url, data={}) {
            try {
                let res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Station-Token': stationToken, 'X-User-Id': userId }, body: JSON.stringify(data) });
                return await res.json();
            } catch(e) { return {}; }
        }

        function applyProfile(key, prof, custom_name) {
            document.getElementById('devIcon').innerText = prof.icon;
            let dispName = custom_name ? `${custom_name} (${prof.name})` : prof.name;
            document.getElementById('detectedName').innerText = dispName;
            document.getElementById('detectedMode').innerText = prof.is_battery ? "🔋 Akku-Ladeüberwachung" : "💡 Dauerbetrieb";
            
            document.getElementById('batteryCard').style.display = prof.is_battery ? 'block' : 'none';
            document.getElementById('continuousPrediction').style.display = prof.is_battery ? 'none' : 'block';
            
            if (!prof.is_battery) {
                document.getElementById('batteryRemWhBox').style.display = 'none';
                document.getElementById('predictedCostBox').style.display = 'none';
            }
        }

        function dismissEightyModal() { document.getElementById('eightyModal').style.display = 'none'; sendAction('/start_device'); }

        async function requestSlot() {
            await sendAction('/request_transfer');
            document.getElementById('requestSentText').style.display = 'block';
            document.getElementById('btnRequestSlot').disabled = true;
            document.getElementById('btnRequestSlot').style.opacity = '0.6';
        }

        async function acceptTransfer() {
            document.getElementById('transferModal').style.display = 'none';
            lastReport = (await sendAction('/accept_transfer')).report;
        }

        async function rejectTransfer() {
            document.getElementById('transferModal').style.display = 'none';
            await sendAction('/reject_transfer');
        }

        async function logout(finalTerminate = false) {
            try {
                let report = await sendAction('/logout', { final: finalTerminate });
                lastReport = report;
                
                let html = "";
                for(let i=0; i<report.items.length; i++) {
                    let item = report.items[i];
                    html += `<div style="padding: 10px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: 8px;">
                                <div style="font-weight: bold; margin-bottom: 4px;">Gerät ${i+1}: ${item.name}</div>
                                <div style="display:flex; justify-content:space-between; font-size:13px;"><span>Verbrauch:</span> <b>${item.wh.toFixed(2)} Wh</b></div>
                                <div style="display:flex; justify-content:space-between; font-size:13px;"><span>Kosten:</span> <b>${item.cost.toFixed(4)} €</b></div>
                             </div>`;
                }
                document.getElementById('receiptDynamicContent').innerHTML = html;
                document.getElementById('rTotalCost').innerText = report.total_cost.toFixed(5) + " €";
                
                document.getElementById('mainCard').style.display = 'none';
                document.getElementById('receiptCard').style.display = 'block';
                isTerminated = true;
            } catch(e) {}
        }

        async function startNewSessionCompletely() { await sendAction('/new_session'); window.location.href = '/'; }
        function downloadInvoicePdf() { window.open('/download_invoice', '_blank'); }

        async function sendInvoiceEmail() {
            let email = document.getElementById('emailInput').value.trim();
            let fb = document.getElementById('emailFeedback');
            if (!email || !email.includes('@')) { fb.style.display = 'block'; fb.innerText = 'Ungültig'; return; }
            fb.style.display = 'block'; fb.innerText = 'Versende...';
            let res = await sendAction('/send_email_invoice', { email: email, report: lastReport });
            fb.innerText = res.status === 'ok' ? '✅ Erfolgreich gesendet!' : res.message;
        }

        async function fetchSyncData() {
            if (isTerminated) return;
            try {
                let res = await fetch('/status', { cache: 'no-store', headers: { 'X-Station-Token': stationToken, 'X-User-Id': userId } });
                let data = await res.json();

                if (data.session_terminated) { await logout(true); return; }

                if (data.show_start_prompt && !startPromptShown && !data.active) {
                    document.getElementById('deviceSelectionModal').style.display = 'flex';
                    startPromptShown = true;
                } else if (!data.show_start_prompt && startPromptShown) {
                    document.getElementById('deviceSelectionModal').style.display = 'none';
                    startPromptShown = false;
                }

                if (data.cart_items && data.cart_items.length > 0) {
                    document.getElementById('cartBox').style.display = 'block';
                    let cHtml = "";
                    data.cart_items.forEach((c, idx) => {
                        cHtml += `<div class="cart-item"><span>${idx+1}. ${c.name}</span><b>${c.cost.toFixed(4)} €</b></div>`;
                    });
                    document.getElementById('cartList').innerHTML = cHtml;
                } else {
                    document.getElementById('cartBox').style.display = 'none';
                }

                if (data.current_profile) applyProfile(data.current_profile_key, data.current_profile, data.custom_name);

                updateTimerUI(data.elapsed_seconds);

                document.getElementById('volt').innerText = data.voltage.toFixed(1);
                document.getElementById('amp').innerText = data.current_ampere.toFixed(3);
                document.getElementById('watt').innerText = data.watt.toFixed(3);
                document.getElementById('wh').innerText = data.wh.toFixed(4);
                document.getElementById('cost').innerText = data.cost.toFixed(5);

                if (data.current_profile && data.current_profile.is_battery && data.active) {
                    document.getElementById('batteryRemWh').innerText = data.rem_wh.toFixed(1);
                    document.getElementById('batteryRemWhBox').style.display = 'inline';
                    document.getElementById('predictedCost').innerText = data.predicted_cost.toFixed(4);
                    document.getElementById('predictedCostBox').style.display = 'block';
                } else if (data.current_profile && !data.current_profile.is_battery && data.active) {
                    document.getElementById('pred1hWh').innerText = data.pred_1h_wh.toFixed(1);
                    document.getElementById('pred1hCost').innerText = data.pred_1h_cost.toFixed(4);
                    let wh24 = data.pred_24h_wh;
                    document.getElementById('pred24hWh').innerText = wh24 >= 1000 ? (wh24/1000).toFixed(2) + " kWh" : wh24.toFixed(1) + " Wh";
                    document.getElementById('pred24hCost').innerText = data.pred_24h_cost.toFixed(2);
                }

                if (data.current_profile && data.current_profile.is_battery && data.active) {
                    let pct = data.battery_pct;
                    document.getElementById('batteryPhaseText').innerText = data.charge_phase;
                    document.getElementById('batteryPercentText').innerText = "~" + pct.toFixed(0) + "%";
                    document.getElementById('batteryBarFill').style.width = pct.toFixed(1) + "%";
                    document.getElementById('batteryWhLoaded').innerText = `${data.wh.toFixed(1)} / ${data.current_profile.capacity_wh.toFixed(0)} Wh`;
                    document.getElementById('batteryTimeRemaining').innerText = `Restzeit: ${data.remaining_time_str}`;

                    if (pct >= 80.0 && pct < 99.0 && !data.battery_full_triggered) {
                        document.getElementById('eightyModal').style.display = 'flex';
                    }
                }

                let badge = document.getElementById('statusBadge');
                let statusText = document.getElementById('statusText');

                if (data.active) {
                    badge.className = "status-pill status-on";
                    statusText.innerText = "Aktiv / Strom fließt";
                    document.getElementById('wattSub').innerText = data.watt > 0.1 ? "Fließt stabil" : "Bereit / Standby";
                } else {
                    badge.className = "status-pill status-off";
                    statusText.innerText = "Warten auf Gerät";
                    document.getElementById('wattSub').innerText = "Bitte Gerät einstecken";
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
            "active": False, "terminated": False, "had_power_draw": False,
            "zero_power_counter": 0.0, "total_kwh": 0.0, "total_seconds": 0.0,
            "current_watt": 0.0, "smoothed_watt": 0.0, "current_ampere": 0.0, "current_voltage": 230.0,
            "device_key": "phone", "custom_name": "", "recent_samples": [],
            "estimated_soc_0": 0.0, "eighty_percent_triggered": False, "last_report": None,
            "detection_mode": False, "show_start_prompt": False, "last_seen": time.time(), 
            "completed_sub_sessions": []
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
    return render_template_string(HTML_PAGE, strompreis=STROMPREIS_PER_KWH)

@app.route('/scan/<token>')
def scan_qr_entry(token):
    if token != STATION_PHYSICAL_TOKEN: return render_template_string(HTML_ACCESS_DENIED), 403
    session["authenticated_on_site"] = True
    session["station_token"] = STATION_PHYSICAL_TOKEN
    get_user_data()
    return render_template_string(HTML_PAGE, strompreis=STROMPREIS_PER_KWH)

@app.route('/init_detection', methods=['POST'])
@require_physical_auth
def init_detection():
    ensure_worker()
    u, _ = get_user_data()
    if not u.get("active") and not u.get("terminated"):
        u["detection_mode"] = True
        async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/start_device', methods=['POST'])
@require_physical_auth
def start_device():
    ensure_worker()
    u, uid = get_user_data()
    if u.get("terminated", False): return jsonify({"status": "forbidden"}), 403
    
    data = request.get_json() or {}
    key = data.get("device_key", "phone")
    cname = data.get("custom_name", "").strip()

    global_state.update({"active_user_id": uid})
    
    u.update({
        "active": True, 
        "detection_mode": False, 
        "show_start_prompt": False,
        "device_key": key,
        "custom_name": cname[:30],
        "had_power_draw": True,
        "zero_power_counter": 0.0
    })
    
    if key in DEVICE_PROFILES and DEVICE_PROFILES[key]["is_battery"]:
        u["estimated_soc_0"] = estimate_current_soc(key, u.get("recent_samples", []))
        
    async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST'])
@require_physical_auth
def stop():
    ensure_worker()
    u, uid = get_user_data()
    u["active"] = False
    u["detection_mode"] = False
    u["show_start_prompt"] = False
    async_cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/logout', methods=['POST'])
@require_physical_auth
def logout():
    u, uid = get_user_data()
    u["active"] = False
    u["detection_mode"] = False
    if (request.get_json() or {}).get("final", False): u["terminated"] = True
    async_cloud_control(turn_on=False)
    
    save_sub_session(u, uid)
    
    items = []
    total_wh = 0.0
    total_cost = 0.0
    for s in u.get("completed_sub_sessions", []):
        items.append({"name": s["device_name"], "wh": s["wh"], "cost": s["cost"]})
        total_wh += s["wh"]
        total_cost += s["cost"]
        
    report = {
        "invoice_id": f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}",
        "items": items,
        "total_cost": total_cost,
        "total_wh": total_wh
    }
    u["last_report"] = report
    return jsonify(report)

@app.route('/new_session', methods=['POST'])
@require_physical_auth
def new_session():
    u, uid = get_user_data()
    u.update({"terminated": False, "active": False, "total_kwh": 0.0, "total_seconds": 0.0, "recent_samples": [], "eighty_percent_triggered": False, "completed_sub_sessions": [], "custom_name": ""})
    return jsonify({"status": "ok"})

@app.route('/download_invoice', methods=['GET'])
@require_physical_auth
def download_invoice():
    u, _ = get_user_data()
    report = u.get("last_report")
    if not report: report = {"invoice_id": "RE-SAMPLE", "items": [{"name": "📱 Smartphone / Tablet", "wh": 10.5, "cost": 0.00367}], "total_wh": 10.5, "total_cost": 0.00367}
    return send_file(generate_pdf_invoice(report), mimetype="application/pdf", as_attachment=True, download_name=f"{report.get('invoice_id')}.pdf")

@app.route('/status')
@require_physical_auth
def status():
    ensure_worker()
    u, uid = get_user_data()
    u["last_seen"] = time.time()

    if u.get("terminated", False): return jsonify({"session_terminated": True})
    
    dev_key = u.get("device_key", "phone")
    prof = DEVICE_PROFILES.get(dev_key, DEVICE_PROFILES["phone"])
    
    wh, cap = u["total_kwh"] * 1000.0, prof.get("capacity_wh", 20.0)
    battery_pct = min(100.0, u.get("estimated_soc_0", 0.0) + ((wh / max(1, cap)) * 100.0)) if prof.get("is_battery") else 0.0
    
    curr_w = u.get("smoothed_watt", 0.0)
    remaining_str = "--"
    charge_phase = "Bereit"
    rem_wh = 0.0
    predicted_cost = 0.0
    
    pred_1h_wh = 0.0
    pred_1h_cost = 0.0
    pred_24h_wh = 0.0
    pred_24h_cost = 0.0
    
    if prof.get("is_battery"):
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
        pred_1h_wh = curr_w * 1.0
        pred_1h_cost = (pred_1h_wh / 1000.0) * STROMPREIS_PER_KWH
        pred_24h_wh = curr_w * 24.0
        pred_24h_cost = (pred_24h_wh / 1000.0) * STROMPREIS_PER_KWH

    cart_items = [{"name": x["device_name"], "cost": x["cost"]} for x in u.get("completed_sub_sessions", [])]

    return jsonify({
        "active": u["active"],
        "watt": curr_w, "current_ampere": u.get("current_ampere", 0.0), "voltage": u.get("current_voltage", 230.0),
        "wh": wh, "cost": u["total_kwh"] * STROMPREIS_PER_KWH,
        "elapsed_seconds": int(u["total_seconds"]),
        "current_profile_key": dev_key, "current_profile": prof,
        "custom_name": u.get("custom_name", ""),
        "battery_pct": battery_pct, "remaining_time_str": remaining_str,
        "charge_phase": charge_phase, "rem_wh": rem_wh, "predicted_cost": predicted_cost,
        "pred_1h_wh": pred_1h_wh, "pred_1h_cost": pred_1h_cost, "pred_24h_wh": pred_24h_wh, "pred_24h_cost": pred_24h_cost,
        "session_terminated": False,
        "show_start_prompt": u.get("show_start_prompt", False),
        "cart_items": cart_items
    })

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
