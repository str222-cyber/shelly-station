from flask import Flask, render_template_string, jsonify, session, request, send_file, redirect, url_for
import requests
import time
import datetime
import threading
import uuid
import os
import json
import smtplib
import io
from collections import deque
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# Versuche WeasyPrint zu laden, sonst ReportLab Fallback
WEASYPRINT_AVAILABLE = False
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception:
    WEASYPRINT_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False


app = Flask(__name__)
app.secret_key = "shelly_smart_power_hub_sec_token_2026_isolated"

# --- HARDWARE & CLOUD KONFIGURATION ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"

# --- SICHERHEITS-TOKEN ---
PHYSICAL_STATION_TOKEN = "SEC-STATION-2026-X99Q-ALPHA-77"
ADMIN_SECRET_TOKEN = "SEC-ADMIN-MASTER-2026-OMEGA"

STROMPREIS_PER_KWH = 0.35  # 0,35 € pro kWh

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = ""

# --- GERÄTEPROFILE & AI-PARAMETER ---
DEVICE_PROFILES = {
    "phone": {
        "name": "Smartphone / Tablet",
        "icon": "📱",
        "is_battery": True,
        "nominal_wh": 18.0,
        "typical_w": 18.0,
        "cv_ratio": 0.50,
        "trickle_w": 1.2
    },
    "laptop": {
        "name": "Laptop / Ultrabook",
        "icon": "💻",
        "is_battery": True,
        "nominal_wh": 65.0,
        "typical_w": 60.0,
        "cv_ratio": 0.45,
        "trickle_w": 3.0
    },
    "ebike_std": {
        "name": "E-Bike Ladegerät (Standard)",
        "icon": "🚲",
        "is_battery": True,
        "nominal_wh": 500.0,
        "typical_w": 160.0,
        "cv_ratio": 0.40,
        "trickle_w": 8.0
    },
    "ebike_fast": {
        "name": "E-Bike Schnelllader / Workstation",
        "icon": "⚡",
        "is_battery": True,
        "nominal_wh": 750.0,
        "typical_w": 350.0,
        "cv_ratio": 0.35,
        "trickle_w": 12.0
    },
    "lamp": {
        "name": "Lampe / Beleuchtung",
        "icon": "💡",
        "is_battery": False,
        "nominal_wh": 0.0,
        "typical_w": 15.0,
        "cv_ratio": 0.0,
        "trickle_w": 0.0
    },
    "tv": {
        "name": "TV / Monitor / Audio",
        "icon": "📺",
        "is_battery": False,
        "nominal_wh": 0.0,
        "typical_w": 85.0,
        "cv_ratio": 0.0,
        "trickle_w": 0.0
    },
    "appliance": {
        "name": "Großgerät / Dauerbetrieb",
        "icon": "🍳",
        "is_battery": False,
        "nominal_wh": 0.0,
        "typical_w": 1200.0,
        "cv_ratio": 0.0,
        "trickle_w": 0.0
    },
    "custom": {
        "name": "Individuelles Gerät",
        "icon": "🔌",
        "is_battery": True,
        "nominal_wh": 80.0,
        "typical_w": 45.0,
        "cv_ratio": 0.40,
        "trickle_w": 2.5
    }
}

# --- GLOBALER STATUS (THREAD-SICHER) ---
state_lock = threading.Lock()
global_state = {
    "active_user_id": None,
    "relay_on": False,
    "last_watt": 0.0,
    "last_amp": 0.0,
    "last_volt": 230.0,
    "last_fetch_time": 0.0,
    "transfer_requested": False,
    "transfer_requester_id": None,
    "history_w": deque(maxlen=60),
    "consecutive_zero_w_count": 0,
    "total_historical_sessions": 0,
    "total_historical_kwh": 0.0,
    "total_historical_revenue": 0.0,
    "device_type_stats": {k: {"count": 0, "wh": 0.0, "revenue": 0.0} for k in DEVICE_PROFILES.keys()}
}

user_sessions = {}
session_history_records = []

# --- PERSISTENZ DER HISTORISCHEN DATEN ---
HISTORY_FILE = "station_history.json"

def load_history():
    global session_history_records
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                session_history_records = data.get("records", [])
                global_state["total_historical_sessions"] = data.get("total_sessions", len(session_history_records))
                global_state["total_historical_kwh"] = data.get("total_kwh", 0.0)
                global_state["total_historical_revenue"] = data.get("total_revenue", 0.0)
                saved_dev_stats = data.get("device_stats", {})
                for k, v in saved_dev_stats.items():
                    if k in global_state["device_type_stats"]:
                        global_state["device_type_stats"][k] = v
        except Exception as e:
            print(f"Fehler beim Laden der Historie: {e}")

def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "total_sessions": global_state["total_historical_sessions"],
                "total_kwh": global_state["total_historical_kwh"],
                "total_revenue": global_state["total_historical_revenue"],
                "device_stats": global_state["device_type_stats"],
                "records": session_history_records[-200:]
            }, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Fehler beim Speichern der Historie: {e}")

load_history()

# --- SHELLY CLOUD HARDWARE-STEUERUNG ---
def async_cloud_control(turn_on=True):
    def _worker():
        with state_lock:
            global_state["relay_on"] = turn_on
        
        # 1. Gen 1 / Legacy REST Endpunkt
        turn_str = "on" if turn_on else "off"
        payload = {"auth_key": AUTH_KEY, "id": DEVICE_ID, "turn": turn_str, "channel": 0}
        try:
            requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control", data=payload, timeout=2.5)
        except Exception:
            pass

        # 2. Gen 2 / Gen 3 RPC Endpunkt (Switch.Set)
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

def fetch_live_cloud_metrics():
    now = time.time()
    with state_lock:
        if now - global_state["last_fetch_time"] < 0.8:
            return global_state["last_watt"], global_state["last_amp"], global_state["last_volt"]

    payload = {"auth_key": AUTH_KEY, "id": DEVICE_ID}
    try:
        res = requests.post(f"{SHELLY_CLOUD_URL}/device/status", data=payload, timeout=2.0).json()
        if res.get("isok"):
            status = res.get("data", {}).get("device_status", {})
            watt = 0.0
            amp = 0.0
            volt = 230.0

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
            elif "relays" in status and len(status["relays"]) > 0:
                watt = float(status["relays"][0].get("power", 0.0))
                amp = watt / 230.0 if watt > 0 else 0.0

            with state_lock:
                global_state["last_watt"] = max(0.0, watt)
                global_state["last_amp"] = max(0.0, amp)
                global_state["last_volt"] = volt if volt > 50 else 230.0
                global_state["last_fetch_time"] = now
                global_state["history_w"].append(watt)
            return global_state["last_watt"], global_state["last_amp"], global_state["last_volt"]
    except Exception:
        pass

    with state_lock:
        return global_state["last_watt"], global_state["last_amp"], global_state["last_volt"]

# --- SERVER-HINTERGRUND-THREAD (TRACKING & AI ENGINE) ---
# Immun gegen Handy-Standby / Verbindungsabbruch
def background_metering_loop():
    last_loop_time = time.time()
    while True:
        try:
            time.sleep(1.0)
            now = time.time()
            dt = max(0.1, min(5.0, now - last_loop_time))
            last_loop_time = now

            watt, amp, volt = fetch_live_cloud_metrics()

            with state_lock:
                active_uid = global_state["active_user_id"]
                if not active_uid or active_uid not in user_sessions:
                    continue

                u = user_sessions[active_uid]
                if not u.get("active") or u.get("terminated") or u.get("paused"):
                    continue

                # 1. Kontinuierliche Integration
                wh_increment = (watt * dt) / 3600.0
                u["accumulated_seconds"] += dt
                u["total_wh"] += wh_increment
                u["total_kwh"] = u["total_wh"] / 1000.0
                u["total_cost"] = u["total_kwh"] * STROMPREIS_PER_KWH

                # Aktuelles aktives Einzelgerät aktualisieren
                curr_idx = u.get("current_device_idx", 0)
                if 0 <= curr_idx < len(u.get("devices", [])):
                    dev = u["devices"][curr_idx]
                    dev["duration_sec"] += dt
                    dev["wh"] += wh_increment
                    dev["cost"] = (dev["wh"] / 1000.0) * STROMPREIS_PER_KWH
                    dev["peak_w"] = max(dev.get("peak_w", 0.0), watt)

                    prof = DEVICE_PROFILES.get(dev["key"], DEVICE_PROFILES["custom"])

                    # 2. AI-Ladestufen & SoC Berechnung
                    if prof["is_battery"]:
                        peak_w = max(dev["peak_w"], prof["typical_w"] * 0.7)
                        nom_wh = prof["nominal_wh"] or 20.0
                        
                        energy_soc = (dev["wh"] / nom_wh) * 100.0
                        
                        if watt > peak_w * prof["cv_ratio"]:
                            stage_name = "Bulk / Schnellladung (CC)"
                            soc = min(75.0, max(5.0, energy_soc))
                        elif watt > prof["trickle_w"]:
                            stage_name = "Sättigung / CV (Absorption)"
                            ratio_in_cv = max(0.0, min(1.0, 1.0 - (watt / (peak_w * prof["cv_ratio"] or 1.0))))
                            soc = min(98.0, max(75.0, 75.0 + ratio_in_cv * 23.0))
                        else:
                            stage_name = "Erhaltungsladung / Voll (Trickle)"
                            soc = 100.0

                        dev["stage"] = stage_name
                        dev["soc_pct"] = round(min(100.0, soc), 1)
                        dev["wh_to_100"] = max(0.0, round(nom_wh * (1.0 - (dev["soc_pct"] / 100.0)), 2))

                        # 3. Automatischer 80% Lade-Stopp (Batterieschutz)
                        if u.get("battery_80_protection_enabled") and not u.get("battery_80_triggered") and dev["soc_pct"] >= 80.0:
                            u["battery_80_triggered"] = True
                            u["active"] = False
                            u["paused"] = True
                            u["stop_reason"] = "battery_80_protection"
                            async_cloud_control(turn_on=False)

                        # 4. Automatischer 100% Lade-Stopp (Vollladung)
                        if not u.get("battery_100_triggered") and (dev["soc_pct"] >= 99.5 or (dev["duration_sec"] > 45 and watt <= prof["trickle_w"])):
                            u["battery_100_triggered"] = True
                            u["active"] = False
                            u["paused"] = True
                            u["stop_reason"] = "battery_100_full"
                            async_cloud_control(turn_on=False)
                    else:
                        dev["stage"] = "Dauerbetrieb"
                        dev["soc_pct"] = 100.0
                        dev["wh_to_100"] = 0.0

                # 5. Erkennung: Gerät ausgesteckt / Null-Last
                if watt < 0.3 and u.get("active") and not u.get("paused"):
                    global_state["consecutive_zero_w_count"] += 1
                    if global_state["consecutive_zero_w_count"] >= 4 and u["accumulated_seconds"] > 10:
                        u["unplug_dialog_active"] = True
                        u["active"] = False
                        u["paused"] = True
                        u["stop_reason"] = "unplugged"
                        async_cloud_control(turn_on=False)
                else:
                    global_state["consecutive_zero_w_count"] = 0

        except Exception as e:
            print(f"Hintergrund-Thread Fehler: {e}")

# Hintergrund-Thread starten
threading.Thread(target=background_metering_loop, daemon=True).start()

# --- PDF-GENERATOR ---
def generate_pdf_invoice(report_data):
    if WEASYPRINT_AVAILABLE:
        try:
            device_rows_html = ""
            for idx, d in enumerate(report_data.get("devices", []), 1):
                device_rows_html += f"""
                <tr>
                    <td style="text-align:center;">{idx}</td>
                    <td><strong>{d.get('icon', '🔌')} {d.get('name', 'Gerät')}</strong><br><span style="font-size:8pt; color:#64748b;">Modus: {'Akku' if d.get('is_battery') else 'Dauerlast'} • {d.get('stage', '-')}</span></td>
                    <td style="text-align:center;">{format_time(d.get('duration_sec', 0))}</td>
                    <td style="text-align:right;">{d.get('wh', 0.0):.3f} Wh</td>
                    <td style="text-align:right;"><strong>{d.get('cost', 0.0):.5f} €</strong></td>
                </tr>
                """

            html_invoice = f"""
            <!DOCTYPE html>
            <html lang="de">
            <head>
            <meta charset="utf-8">
            <style>
            @page {{ size: A4; margin: 18mm 15mm; background-color: #ffffff; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #090d16; margin: 0; padding: 0; }}
            .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #2563eb; padding-bottom: 15px; margin-bottom: 20px; }}
            .brand {{ font-size: 20pt; font-weight: 800; color: #2563eb; letter-spacing: -0.5px; }}
            .meta {{ font-size: 9pt; color: #64748b; margin-top: 4px; }}
            .sec-badge {{ background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; font-size: 8.5pt; font-weight: 700; padding: 4px 8px; border-radius: 6px; display: inline-block; margin-top: 6px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
            th {{ background-color: #f1f5f9; color: #334155; text-align: left; padding: 9px 10px; font-size: 9pt; border-bottom: 2px solid #cbd5e1; text-transform: uppercase; }}
            td {{ padding: 9px 10px; font-size: 9pt; border-bottom: 1px solid #e2e8f0; vertical-align: middle; }}
            tr:nth-child(even) {{ background-color: #fafbfc; }}
            .total-box {{ margin-top: 25px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 14px 18px; text-align: right; }}
            .total-label {{ font-size: 10pt; color: #166534; font-weight: 600; }}
            .total-amount {{ font-size: 22pt; font-weight: 800; color: #15803d; }}
            .footer {{ margin-top: 40px; font-size: 8.5pt; color: #94a3b8; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 12px; line-height: 1.4; }}
            </style>
            </head>
            <body>
            <div class="header">
                <div>
                    <div class="brand">⚡ Smart Power Hub</div>
                    <div class="meta">Offizielle Lade- & Stromquittung • Beleg-Nr.: {report_data.get('invoice_id')}</div>
                    <div class="sec-badge">🔒 Physische Station: {PHYSICAL_STATION_TOKEN}</div>
                </div>
                <div style="text-align:right; font-size:9pt; color:#64748b;">
                    Datum: {time.strftime('%d.%m.%Y %H:%M')}<br>
                    Tarif: {STROMPREIS_PER_KWH:.3f} € / kWh
                </div>
            </div>

            <table style="margin-bottom:15px;">
                <thead>
                    <tr>
                        <th style="width:6%; text-align:center;">Pos</th>
                        <th style="width:44%;">Geladenes Gerät / Modus</th>
                        <th style="width:16%; text-align:center;">Ladedauer</th>
                        <th style="width:17%; text-align:right;">Energie</th>
                        <th style="width:17%; text-align:right;">Teilbetrag</th>
                    </tr>
                </thead>
                <tbody>
                    {device_rows_html}
                </tbody>
            </table>

            <div class="total-box">
                <div class="total-label">Gesamtsumme ({len(report_data.get('devices', []))} Geräte / {format_time(report_data.get('total_seconds', 0))})</div>
                <div class="total-amount">{report_data.get('total_cost', 0.0):.5f} €</div>
                <div style="font-size:8.5pt; color:#166534; margin-top:3px;">Gesamtverbrauch: {report_data.get('total_wh', 0.0):.4f} Wh ({report_data.get('total_kwh', 0.0):.6f} kWh)</div>
            </div>

            <div class="footer">
                Vielen Dank für die Nutzung der Smart Power Hub Ladestation!<br>
                Shelly Cloud Gen 2/3 IoT-Abrechnungssystem • Autarkes Server-Metering
            </div>
            </body>
            </html>
            """
            pdf_buf = io.BytesIO()
            HTML(string=html_invoice).write_pdf(pdf_buf)
            pdf_buf.seek(0)
            return pdf_buf
        except Exception as e:
            print(f"WeasyPrint Fehler, wechsle zu ReportLab: {e}")

    # Fallback auf ReportLab
    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor("#2563eb"), spaceAfter=4)
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor("#64748b"), spaceAfter=12)

    story.append(Paragraph("⚡ Smart Power Hub", title_style))
    story.append(Paragraph(f"Sammelquittung • Beleg-Nr: {report_data.get('invoice_id')} • Datum: {time.strftime('%d.%m.%Y %H:%M')}<br/>Station: {PHYSICAL_STATION_TOKEN} | Arbeitspreis: {STROMPREIS_PER_KWH:.3f} €/kWh", meta_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563eb"), spaceAfter=14))

    table_data = [["Pos", "Gerät", "Dauer", "Energie (Wh)", "Betrag (€)"]]
    for idx, d in enumerate(report_data.get("devices", []), 1):
        table_data.append([
            str(idx),
            d.get("name", "Gerät"),
            format_time(d.get("duration_sec", 0)),
            f"{d.get('wh', 0.0):.3f} Wh",
            f"{d.get('cost', 0.0):.5f} €"
        ])

    table_data.append([
        "",
        "GESAMT",
        format_time(report_data.get("total_seconds", 0)),
        f"{report_data.get('total_wh', 0.0):.4f} Wh",
        f"{report_data.get('total_cost', 0.0):.5f} €"
    ])

    t = Table(table_data, colWidths=[30, 210, 80, 100, 90])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5f9")),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor("#334155")),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
        ('LINEBELOW', (0,0), (-1,0), 1.5, colors.HexColor("#cbd5e1")),
        ('LINEBELOW', (0,1), (-1,-2), 0.5, colors.HexColor("#e2e8f0")),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor("#f0fdf4")),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0,-1), (-1,-1), colors.HexColor("#15803d")),
        ('LINEABOVE', (0,-1), (-1,-1), 1.5, colors.HexColor("#15803d")),
    ]))
    story.append(t)
    story.append(Spacer(1, 20))
    story.append(Paragraph("Vielen Dank für die Nutzung der Smart Power Hub Ladestation!", meta_style))

    doc.build(story)
    pdf_buf.seek(0)
    return pdf_buf

def format_time(seconds):
    sec = int(seconds)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# --- HILFSFUNKTIONEN FÜR DIE NUTZERSITZUNG ---
def get_or_create_user_session():
    if "user_id" not in session:
        session["user_id"] = str(uuid.uuid4())
    uid = session["user_id"]

    with state_lock:
        if uid not in user_sessions:
            user_sessions[uid] = {
                "user_id": uid,
                "active": False,
                "paused": False,
                "terminated": False,
                "station_verified": session.get("station_verified", False),
                "start_timestamp": None,
                "accumulated_seconds": 0.0,
                "total_wh": 0.0,
                "total_kwh": 0.0,
                "total_cost": 0.0,
                "battery_80_protection_enabled": True,
                "battery_80_triggered": False,
                "battery_100_triggered": False,
                "stop_reason": None,
                "unplug_dialog_active": False,
                "devices": [
                    {
                        "id": 1,
                        "key": "phone",
                        "name": "📱 Smartphone / Tablet",
                        "icon": "📱",
                        "is_battery": True,
                        "start_timestamp": None,
                        "duration_sec": 0.0,
                        "wh": 0.0,
                        "cost": 0.0,
                        "peak_w": 0.0,
                        "stage": "Bereit",
                        "soc_pct": 10.0,
                        "wh_to_100": 16.0
                    }
                ],
                "current_device_idx": 0,
                "last_report": None
            }
        else:
            if session.get("station_verified", False):
                user_sessions[uid]["station_verified"] = True
        return user_sessions[uid], uid

@app.after_request
def add_security_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

# --- HTML UI TEMPLATES ---

SECURITY_LOCK_HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Power Hub • Vor-Ort Sicherheitsüberprüfung</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        :root {
            --bg-color: #090d16;
            --card-bg: #111827;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent-primary: #3b82f6;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --border-color: #1f2937;
        }
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 16px; }
        .container { width: 100%; max-width: 440px; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 32px 24px; border: 1px solid var(--border-color); text-align: center; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); }
        .shield-icon { font-size: 54px; margin-bottom: 16px; }
        .title { font-size: 22px; font-weight: 800; letter-spacing: -0.4px; color: #ffffff; margin-bottom: 8px; }
        .subtitle { font-size: 13.5px; color: var(--text-muted); line-height: 1.5; margin-bottom: 24px; }
        .token-box { background: #0f172a; border: 1px dashed #3b82f6; border-radius: 14px; padding: 14px; margin-bottom: 24px; text-align: left; }
        .token-label { font-size: 11px; text-transform: uppercase; color: #60a5fa; font-weight: 700; letter-spacing: 0.5px; margin-bottom: 4px; }
        .token-code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 13px; color: #f8fafc; word-break: break-all; }
        .btn-scan { display: inline-block; width: 100%; padding: 14px; font-size: 15px; font-weight: 700; background: var(--accent-primary); color: #ffffff; border: none; border-radius: 14px; text-decoration: none; cursor: pointer; transition: background 0.15s; }
        .btn-scan:hover { background: #2563eb; }
        .btn-bypass { margin-top: 12px; background: transparent; border: 1px solid var(--border-color); color: var(--text-muted); font-size: 12px; padding: 9px; border-radius: 10px; width: 100%; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="shield-icon">🔒</div>
            <div class="title">Vor-Ort-Sicherheitsüberprüfung</div>
            <div class="subtitle">
                Um die Steckdose zu steuern und Strom zu beziehen, musst du dich physisch vor Ort an der Ladestation befinden.
            </div>

            <div class="token-box">
                <div class="token-label">Erforderlicher Station-Token:</div>
                <div class="token-code">{{ required_token }}</div>
            </div>

            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 18px;">
                Scanne bitte den physischen QR-Code am Gehäuse der Ladestation, um deine Session automatisch freizuschalten.
            </p>

            <a href="/scan/{{ required_token }}" class="btn-scan">📲 Station jetzt verifizieren & freischalten</a>
            
            <a href="/admin/{{ admin_token }}" class="btn-bypass" style="display:block; text-decoration:none; margin-top:16px;">
                ⚙️ Zum Betreiber-Admin-Dashboard
            </a>
        </div>
    </div>
</body>
</html>
"""

MAIN_PAGE_HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Power Hub • Shelly Gen 2/3</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        :root {
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #090d16;
            --text-muted: #64748b;
            --accent-primary: #2563eb;
            --accent-green: #059669;
            --accent-amber: #d97706;
            --accent-red: #dc2626;
            --border-color: #e2e8f0;
            --shadow-card: 0 12px 30px -6px rgba(15, 23, 42, 0.08), 0 4px 8px -4px rgba(15, 23, 42, 0.04);
        }

        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); display: flex; justify-content: center; padding: 18px 12px; min-height: 100vh; }
        
        .container { width: 100%; max-width: 440px; margin: auto; }
        .card { background: var(--card-bg); border-radius: 24px; padding: 22px 18px; box-shadow: var(--shadow-card); border: 1px solid var(--border-color); text-align: center; }
        
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .title { font-size: 18px; font-weight: 800; color: var(--text-main); letter-spacing: -0.3px; display: flex; align-items: center; gap: 6px; }
        .rate-badge { background: #f1f5f9; color: var(--text-muted); font-size: 11.5px; padding: 4px 10px; border-radius: 20px; font-weight: 700; }

        .sec-pill { display: inline-flex; align-items: center; gap: 5px; font-size: 10.5px; color: var(--accent-green); background: #ecfdf5; border: 1px solid #a7f3d0; padding: 3px 9px; border-radius: 12px; font-weight: 600; margin-bottom: 12px; }

        /* STATUS BADGE */
        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            font-size: 12.5px;
            font-weight: 700;
            padding: 6px 14px;
            border-radius: 30px;
            margin-bottom: 14px;
        }
        .status-on { background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }
        .status-off { background: #f1f5f9; color: var(--text-muted); border: 1px solid var(--border-color); }
        .status-paused { background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }
        .status-dot { width: 9px; height: 9px; border-radius: 50%; }
        .status-on .status-dot { background: var(--accent-green); box-shadow: 0 0 10px rgba(5,150,105,0.7); }
        .status-off .status-dot { background: #94a3b8; }
        .status-paused .status-dot { background: var(--accent-amber); }

        /* AI LIVE BANNER */
        .ai-banner {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 18px;
            padding: 14px;
            margin-bottom: 14px;
            text-align: left;
        }
        .ai-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .ai-badge { font-size: 10.5px; font-weight: 800; text-transform: uppercase; color: var(--accent-primary); letter-spacing: 0.6px; background: #eff6ff; padding: 3px 8px; border-radius: 6px; }
        .btn-edit-dev { background: #e2e8f0; color: var(--text-main); border: none; font-size: 11.5px; font-weight: 600; padding: 4px 10px; border-radius: 8px; cursor: pointer; }
        
        .ai-body { display: flex; align-items: center; gap: 12px; }
        .ai-icon { font-size: 30px; }
        .ai-details { flex: 1; }
        .ai-name { font-size: 15px; font-weight: 700; color: var(--text-main); }
        .ai-stage { font-size: 12px; font-weight: 600; color: var(--accent-primary); margin-top: 2px; }
        .ai-metrics { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

        .charge-bar-wrap { margin-top: 10px; background: #e2e8f0; border-radius: 10px; height: 8px; width: 100%; overflow: hidden; }
        .charge-bar-fill { background: linear-gradient(90deg, #2563eb, #10b981); height: 100%; width: 10%; border-radius: 10px; transition: width 0.4s ease; }
        .charge-info { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--text-muted); margin-top: 4px; font-weight: 600; }

        /* GRID STATS */
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .stat-card {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 12px;
            text-align: left;
        }
        .stat-label { font-size: 10.5px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.4px; }
        .stat-val {
            font-size: 18px;
            font-weight: 800;
            color: var(--text-main);
            margin-top: 3px;
            font-variant-numeric: tabular-nums;
            font-family: ui-monospace, SFMono-Regular, monospace;
            white-space: nowrap;
        }
        .stat-sub { font-size: 10.5px; color: var(--text-muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
        .stat-watt .stat-val { color: var(--accent-primary); }
        .stat-cost .stat-val { color: var(--accent-green); }

        /* BATTERY 80% TOGGLE */
        .protection-toggle-box {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            border-radius: 14px;
            padding: 10px 14px;
            margin-bottom: 14px;
            text-align: left;
        }
        .toggle-title { font-size: 12px; font-weight: 700; color: #1e40af; }
        .toggle-sub { font-size: 10.5px; color: #3b82f6; }
        .switch { position: relative; display: inline-block; width: 44px; height: 24px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #cbd5e1; transition: .3s; border-radius: 24px; }
        .slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; }
        input:checked + .slider { background-color: #2563eb; }
        input:checked + .slider:before { transform: translateX(20px); }

        /* MULTI-DEVICE SESSION LIST */
        .session-devices-box {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 12px 14px;
            margin-bottom: 14px;
            text-align: left;
        }
        .s-dev-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 8px; }
        .s-dev-item { display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding: 6px 0; border-bottom: 1px solid var(--border-color); }
        .s-dev-item:last-child { border-bottom: none; }
        .s-dev-active { font-weight: 700; color: var(--accent-primary); }

        /* BUTTONS */
        .btn-group { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
        button {
            width: 100%;
            padding: 13px;
            font-size: 14.5px;
            font-weight: 700;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            transition: transform 0.1s ease, opacity 0.15s ease;
        }
        button:active { transform: scale(0.98); }
        .btn-start { background: #0f172a; color: white; }
        .btn-pause { background: #f1f5f9; color: var(--text-main); border: 1px solid var(--border-color); }
        .btn-swap { background: #eff6ff; color: var(--accent-primary); border: 1px solid #bfdbfe; font-size: 13.5px; padding: 11px; }
        .btn-finish { background: #fee2e2; color: var(--accent-red); }

        /* MODALS */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(9, 13, 22, 0.75);
            backdrop-filter: blur(5px);
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
            max-width: 360px;
            width: 100%;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            animation: popIn 0.25s ease-out;
        }
        @keyframes popIn { from { transform: scale(0.88); opacity: 0; } to { transform: scale(1); opacity: 1; } }

        .device-option-btn {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 11px 14px;
            text-align: left;
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 8px;
            width: 100%;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
        }
        .device-option-btn:hover { background: #edf2f7; }

        /* RECEIPT CARD */
        .receipt-card { display: none; text-align: left; }
        .receipt-header { text-align: center; margin-bottom: 18px; }
        .receipt-table { width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 12.5px; }
        .receipt-table th { background: #f1f5f9; padding: 8px; text-align: left; font-size: 11px; text-transform: uppercase; color: var(--text-muted); }
        .receipt-table td { padding: 8px; border-bottom: 1px solid var(--border-color); }
        .receipt-total-box { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 14px; padding: 14px; text-align: right; margin-top: 14px; }
        
        .email-input {
            width: 100%;
            padding: 11px;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            font-size: 13.5px;
            margin-bottom: 8px;
        }
    </style>
</head>
<body>

    <!-- MODAL 1: GERÄT AUSWÄHLEN (INITIAL / WECHSEL) -->
    <div id="deviceModal" class="modal-overlay">
        <div class="modal-box">
            <h3 style="margin-bottom: 6px; font-size:17px;">Gerät festlegen</h3>
            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 14px;">Wähle dein Gerät für die intelligente AI-Ladekurvenanalyse:</p>
            
            <button class="device-option-btn" onclick="selectDeviceProfile('phone')">📱 Smartphone / Tablet (Akku)</button>
            <button class="device-option-btn" onclick="selectDeviceProfile('laptop')">💻 Laptop / Ultrabook (Akku)</button>
            <button class="device-option-btn" onclick="selectDeviceProfile('ebike_std')">🚲 E-Bike Standard (Akku)</button>
            <button class="device-option-btn" onclick="selectDeviceProfile('ebike_fast')">⚡ E-Bike Schnelllader (Akku)</button>
            <button class="device-option-btn" onclick="selectDeviceProfile('lamp')">💡 Lampe / Beleuchtung (Dauerlast)</button>
            <button class="device-option-btn" onclick="selectDeviceProfile('tv')">📺 TV / Monitor (Dauerlast)</button>
            <button class="device-option-btn" onclick="selectDeviceProfile('appliance')">🍳 Großgerät / Küche (Dauerlast)</button>
            
            <div style="margin-top:10px; text-align:left;">
                <label style="font-size:11px; font-weight:700; color:var(--text-muted);">Eigenes Gerät / Name:</label>
                <input type="text" id="customDeviceName" placeholder="z. B. Drohnen-Akku" style="width:100%; padding:9px; border:1px solid var(--border-color); border-radius:8px; font-size:13px; margin-top:4px; margin-bottom:8px;">
                <button class="device-option-btn" style="background:#eff6ff; border-color:#bfdbfe; color:#1e40af;" onclick="selectCustomDevice()">🔌 Eigenes Akku-Gerät hinzufügen</button>
            </div>

            <button class="btn-pause" style="margin-top: 6px;" onclick="closeModal('deviceModal')">Schließen</button>
        </div>
    </div>

    <!-- MODAL 2: PAUSE / GERÄTEWECHSEL DIALOG -->
    <div id="swapModal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-primary);">
            <div style="font-size: 38px; margin-bottom: 6px;">🔄🔌</div>
            <h3 style="font-size: 17px; margin-bottom: 6px;">Pause / Gerät gewechselt</h3>
            <p style="font-size: 12.5px; color: var(--text-muted); margin-bottom: 16px;">
                Die Station wurde pausiert oder kein Stromfluss erkannt. Wie möchtest du fortfahren?
            </p>

            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="handleDeviceAction('continue')">
                ▶️ Gleiches Gerät fortsetzen
            </button>
            <button class="btn-start" style="background: var(--accent-primary); margin-bottom: 8px;" onclick="handleDeviceAction('new_device')">
                ➕ Neues Gerät anschließen
            </button>
            <button class="btn-finish" onclick="handleDeviceAction('finish')">
                🧾 Gesamtsitzung beenden & abrechnen
            </button>
        </div>
    </div>

    <!-- MODAL 3: ENGLISCHES 80% BATTERIESCHUTZ POPUP -->
    <div id="battery80Modal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-amber);">
            <div style="font-size: 40px; margin-bottom: 8px;">🔋🛡️</div>
            <h3 style="color: #b45309; font-size: 17px; margin-bottom: 8px;">Battery Protection Triggered</h3>
            <p style="font-size: 13px; color: var(--text-main); line-height: 1.4; margin-bottom: 16px;">
                Charging paused at <strong>80%</strong> to maximize lithium-ion cycle lifespan and prevent battery degradation. Tap Resume if you need a 100% full charge.
            </p>
            <button class="btn-start" style="background: var(--accent-primary); margin-bottom: 8px;" onclick="resumeBeyond80()">
                ⚡ Resume to 100% (Override)
            </button>
            <button class="btn-finish" onclick="handleDeviceAction('finish')">
                🧾 Finish & Pay Current Wh
            </button>
        </div>
    </div>

    <!-- MODAL 4: 100% VOLLSTÄNDIG GELADEN POPUP -->
    <div id="battery100Modal" class="modal-overlay">
        <div class="modal-box" style="border: 2px solid var(--accent-green);">
            <div style="font-size: 40px; margin-bottom: 8px;">✅🔋</div>
            <h3 style="color: #047857; font-size: 17px; margin-bottom: 8px;">100% Charge Complete</h3>
            <p style="font-size: 13px; color: var(--text-main); line-height: 1.4; margin-bottom: 16px;">
                Your device is fully charged (Float / Trickle state reached). Power has been cut off to save energy and eliminate vampire draw.
            </p>
            <button class="btn-start" style="background: var(--accent-green); margin-bottom: 8px;" onclick="handleDeviceAction('new_device')">
                ➕ Next Device
            </button>
            <button class="btn-finish" onclick="handleDeviceAction('finish')">
                🧾 Finish & Download Receipt
            </button>
        </div>
    </div>

    <!-- HAUPTCONTAINER -->
    <div class="container">
        <!-- HAUPTKARTE DER LADE-SESSION -->
        <div class="card" id="mainCard">
            <div class="header">
                <span class="title">⚡ Smart Power Hub</span>
                <span class="rate-badge">{{ strompreis }} €/kWh</span>
            </div>

            <div style="display:flex; justify-content:center; gap:8px; align-items:center;">
                <div class="sec-pill">🔒 Vor-Ort verifiziert</div>
                <div id="statusBadge" class="status-pill status-off">
                    <span class="status-dot"></span>
                    <span id="statusText">Bereit / Aus</span>
                </div>
            </div>

            <!-- AI LIVE GERÄTE-BANNER -->
            <div class="ai-banner">
                <div class="ai-header">
                    <span class="ai-badge">AI Lastprofil-Analyse</span>
                    <button class="btn-edit-dev" onclick="openModal('deviceModal')">✏️ Gerät ändern</button>
                </div>
                <div class="ai-body">
                    <div class="ai-icon" id="devIcon">📱</div>
                    <div class="ai-details">
                        <div class="ai-name" id="detectedName">📱 Smartphone / Tablet</div>
                        <div class="ai-stage" id="detectedStage">Bulk / Schnellladung (CC)</div>
                        <div class="ai-metrics" id="detectedMetrics">Vorhersage bis 100%: -- Wh</div>
                    </div>
                </div>

                <div id="batteryBarSection" style="display:block;">
                    <div class="charge-bar-wrap">
                        <div id="chargeBarFill" class="charge-bar-fill" style="width: 25%;"></div>
                    </div>
                    <div class="charge-info">
                        <span>Ladestand: <strong id="chargePctText">25%</strong></span>
                        <span id="remainingWhText">Noch ca. 12.5 Wh</span>
                    </div>
                </div>
            </div>

            <!-- BATTERIESCHUTZ 80% TOGGLE -->
            <div class="protection-toggle-box">
                <div>
                    <div class="toggle-title">🛡️ 80% Akkuschutz-Modus</div>
                    <div class="toggle-sub">Stoppt automatisch bei 80% zur Akkuschonung</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="toggle80" checked onchange="toggle80Protection(this.checked)">
                    <span class="slider"></span>
                </label>
            </div>

            <!-- MULTI-DEVICE SESSION ÜBERSICHT -->
            <div class="session-devices-box">
                <div class="s-dev-title">📦 Geräte in dieser Sitzung</div>
                <div id="sessionDeviceList">
                    <!-- Dynamisch -->
                </div>
            </div>

            <!-- NETZDATEN (U, I) -->
            <div class="grid-2">
                <div class="stat-card">
                    <div class="stat-label">Netzspannung (U)</div>
                    <div class="stat-val"><span id="volt">230.0</span> V</div>
                    <div class="stat-sub">Wechselspannung</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Stromstärke (I)</div>
                    <div class="stat-val"><span id="amp">0.000</span> A</div>
                    <div class="stat-sub"><span id="milliAmp">0</span> mA</div>
                </div>
            </div>

            <!-- LEISTUNG & GESAMTZEIT -->
            <div class="grid-2">
                <div class="stat-card stat-watt">
                    <div class="stat-label">Wirkleistung (P)</div>
                    <div class="stat-val"><span id="watt">0.000</span> W</div>
                    <div class="stat-sub" id="wattSub">Kein Strom</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Laufzeit</div>
                    <div class="stat-val" id="timer">00:00:00</div>
                    <div class="stat-sub" id="timerSub">Läuft sekundengenau</div>
                </div>
            </div>

            <!-- ENERGIE & KOSTEN -->
            <div class="grid-2">
                <div class="stat-card">
                    <div class="stat-label">Verbrauch</div>
                    <div class="stat-val" style="color:var(--accent-primary);"><span id="wh">0.0000</span> Wh</div>
                    <div class="stat-sub"><span id="mwh">0.0</span> mWh</div>
                </div>
                <div class="stat-card stat-cost">
                    <div class="stat-label">Kosten (€)</div>
                    <div class="stat-val"><span id="cost">0.00000</span> €</div>
                    <div class="stat-sub"><span id="microCost">0.00</span> Cent</div>
                </div>
            </div>

            <div class="btn-group">
                <button class="btn-start" id="mainStartBtn" onclick="startCharging()">▶️ Start / Fortsetzen</button>
                <button class="btn-pause" onclick="pauseCharging()">⏸️ Pause</button>
                <button class="btn-swap" onclick="openModal('swapModal')">🔄 Pause / Gerät gewechselt</button>
                <button class="btn-finish" onclick="handleDeviceAction('finish')">🧾 Beenden & Sammel-Quittung</button>
            </div>
        </div>

        <!-- SAMMEL-QUITTUNG KARTE -->
        <div class="card receipt-card" id="receiptCard">
            <div class="receipt-header">
                <div style="font-size: 44px; margin-bottom: 6px;">🧾</div>
                <div class="title" style="justify-content:center;">Lade- & Stromquittung</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 3px;">Sitzung beendet • Steckdose freigegeben</div>
            </div>

            <table class="receipt-table">
                <thead>
                    <tr>
                        <th>Gerät</th>
                        <th style="text-align:center;">Dauer</th>
                        <th style="text-align:right;">Wh</th>
                        <th style="text-align:right;">€</th>
                    </tr>
                </thead>
                <tbody id="receiptTableBody">
                    <!-- Dynamische Zeilen -->
                </tbody>
            </table>

            <div class="receipt-total-box">
                <div style="font-size:11px; color:#166534; font-weight:700; text-transform:uppercase;">Gesamtbetrag</div>
                <div style="font-size:24px; font-weight:800; color:#15803d;" id="rGrandCost">0.00000 €</div>
                <div style="font-size:11px; color:#166534; margin-top:2px;">
                    Gesamtverbrauch: <span id="rGrandWh">0.000 Wh</span> (<span id="rGrandKwh">0.0000 kWh</span>)
                </div>
            </div>

            <div style="margin-top: 18px; background: #f8fafc; border: 1px solid var(--border-color); border-radius: 14px; padding: 14px; text-align:left;">
                <div style="font-size:12px; font-weight:700; margin-bottom:6px;">📧 Sammelquittung per E-Mail zusenden:</div>
                <input type="email" id="emailInput" class="email-input" placeholder="deine-email@beispiel.de">
                <button class="btn-start" style="background:var(--accent-primary); font-size:13.5px; padding:11px;" onclick="sendInvoiceEmail()">Rechnung per E-Mail senden</button>
                <button class="btn-pause" style="font-size:13px; padding:9px; margin-top:6px;" onclick="downloadInvoicePdf()">📥 PDF direkt herunterladen</button>
                <div id="emailFeedback" style="display:none; font-size:12px; font-weight:600; margin-top:8px;"></div>
            </div>

            <div style="margin-top: 20px; font-size: 11.5px; color: var(--text-muted); text-align:center;">
                ℹ️ Um die Station neu zu nutzen, scanne bitte den physischen QR-Code erneut.
            </div>
        </div>
    </div>

    <script>
        let isTerminated = false;
        let lastReport = null;
        let active80ModalShown = false;
        let active100ModalShown = false;

        let localElapsedSeconds = 0;
        let isRunningLocally = false;
        let localTimerInterval = null;

        function openModal(id) { document.getElementById(id).style.display = 'flex'; }
        function closeModal(id) { document.getElementById(id).style.display = 'none'; }

        function formatSeconds(sec) {
            let s = Math.floor(sec);
            let h = Math.floor(s / 3600).toString().padStart(2, '0');
            let m = Math.floor((s % 3600) / 60).toString().padStart(2, '0');
            let secRem = Math.floor(s % 60).toString().padStart(2, '0');
            return `${h}:${m}:${secRem}`;
        }

        function updateTimerDisplay(sec) {
            document.getElementById('timer').innerText = formatSeconds(sec);
        }

        function startTimerTick() {
            if (localTimerInterval) clearInterval(localTimerInterval);
            localTimerInterval = setInterval(() => {
                if (isRunningLocally && !isTerminated) {
                    localElapsedSeconds += 1;
                    updateTimerDisplay(localElapsedSeconds);
                }
            }, 1000);
        }

        function stopTimerTick() {
            if (localTimerInterval) {
                clearInterval(localTimerInterval);
                localTimerInterval = null;
            }
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

        async function startCharging() {
            if (isTerminated) return;
            isRunningLocally = true;
            startTimerTick();
            
            document.getElementById('statusBadge').className = "status-pill status-on";
            document.getElementById('statusText').innerText = "Aktiv / Strom fließt";
            
            sendAction('/start');
            setTimeout(fetchTelemetry, 150);
        }

        async function pauseCharging() {
            if (isTerminated) return;
            isRunningLocally = false;
            stopTimerTick();
            
            document.getElementById('statusBadge').className = "status-pill status-paused";
            document.getElementById('statusText').innerText = "Pausiert";
            
            sendAction('/stop');
            setTimeout(fetchTelemetry, 150);
        }

        async function selectDeviceProfile(key) {
            closeModal('deviceModal');
            await sendAction('/set_device', { key: key });
            fetchTelemetry();
        }

        async function selectCustomDevice() {
            let name = document.getElementById('customDeviceName').value.trim() || 'Individuelles Gerät';
            closeModal('deviceModal');
            await sendAction('/set_device', { key: 'custom', name: name });
            fetchTelemetry();
        }

        async function handleDeviceAction(action) {
            closeModal('swapModal');
            closeModal('battery80Modal');
            closeModal('battery100Modal');

            if (action === 'continue') {
                await startCharging();
            } else if (action === 'new_device') {
                openModal('deviceModal');
            } else if (action === 'finish') {
                stopTimerTick();
                let report = await sendAction('/logout');
                renderFinalReceipt(report);
            }
        }

        async function resumeBeyond80() {
            closeModal('battery80Modal');
            await sendAction('/resume_beyond_80');
            startCharging();
        }

        async function toggle80Protection(enabled) {
            await sendAction('/toggle_80_protection', { enabled: enabled });
        }

        function renderFinalReceipt(report) {
            isTerminated = true;
            isRunningLocally = false;
            stopTimerTick();
            lastReport = report;
            document.getElementById('mainCard').style.display = 'none';
            document.getElementById('receiptCard').style.display = 'block';

            let tbody = document.getElementById('receiptTableBody');
            tbody.innerHTML = '';
            (report.devices || []).forEach((d, idx) => {
                let tr = document.createElement('tr');
                tr.innerHTML = `
                    <td><strong>${d.icon || '🔌'} ${d.name}</strong></td>
                    <td style="text-align:center;">${formatSeconds(d.duration_sec)}</td>
                    <td style="text-align:right;">${d.wh.toFixed(3)} Wh</td>
                    <td style="text-align:right;"><strong>${d.cost.toFixed(5)} €</strong></td>
                `;
                tbody.appendChild(tr);
            });

            document.getElementById('rGrandCost').innerText = report.total_cost.toFixed(5) + " €";
            document.getElementById('rGrandWh').innerText = report.total_wh.toFixed(4) + " Wh";
            document.getElementById('rGrandKwh').innerText = report.total_kwh.toFixed(6) + " kWh";
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

            let res = await sendAction('/send_email_invoice', { email: email, report: lastReport });
            if (res.status === 'ok') {
                fb.style.color = 'var(--accent-green)';
                fb.innerText = '✅ Sammelquittung wurde erfolgreich per E-Mail gesendet!';
            } else {
                fb.style.color = 'var(--accent-amber)';
                fb.innerText = res.message || 'Versand nicht möglich.';
            }
        }

        function downloadInvoicePdf() {
            window.open('/download_invoice', '_blank');
        }

        // TELEMETRIE-POLLING VOM SERVER
        async function fetchTelemetry() {
            if (isTerminated) return;
            try {
                let res = await fetch('/status', { cache: 'no-store' });
                let data = await res.json();

                if (data.session_terminated) {
                    renderFinalReceipt(data.report);
                    return;
                }

                // Synchronisiere lokalen Timer mit dem Server
                if (data.active) {
                    isRunningLocally = true;
                    if (!localTimerInterval) startTimerTick();
                    if (Math.abs(localElapsedSeconds - data.elapsed_seconds) > 2) {
                        localElapsedSeconds = Math.floor(data.elapsed_seconds);
                        updateTimerDisplay(localElapsedSeconds);
                    }
                } else {
                    isRunningLocally = false;
                    stopTimerTick();
                    localElapsedSeconds = Math.floor(data.elapsed_seconds);
                    updateTimerDisplay(localElapsedSeconds);
                }

                // UNPLUG / PAUSE DIALOG TRIGGER
                if (data.unplug_dialog_active) {
                    openModal('swapModal');
                }

                // 80% POPUP TRIGGER
                if (data.battery_80_triggered && !active80ModalShown && data.stop_reason === 'battery_80_protection') {
                    active80ModalShown = true;
                    openModal('battery80Modal');
                }

                // 100% POPUP TRIGGER
                if (data.battery_100_triggered && !active100ModalShown && data.stop_reason === 'battery_100_full') {
                    active100ModalShown = true;
                    openModal('battery100Modal');
                }

                // Status Badge
                let badge = document.getElementById('statusBadge');
                let statusText = document.getElementById('statusText');
                if (data.active) {
                    badge.className = "status-pill status-on";
                    statusText.innerText = "Aktiv / Strom fließt";
                    document.getElementById('wattSub').innerText = data.watt > 0.1 ? "Fließt stabil" : "Bereit / Standby";
                } else if (data.paused) {
                    badge.className = "status-pill status-paused";
                    statusText.innerText = "Pausiert";
                    document.getElementById('wattSub').innerText = "Unterbrochen";
                } else {
                    badge.className = "status-pill status-off";
                    statusText.innerText = "Bereit / Aus";
                    document.getElementById('wattSub').innerText = "Kein Strom";
                }

                // Netz & Energie
                document.getElementById('volt').innerText = data.voltage.toFixed(1);
                document.getElementById('amp').innerText = data.current_ampere.toFixed(3);
                document.getElementById('milliAmp').innerText = (data.current_ampere * 1000.0).toFixed(0);
                document.getElementById('watt').innerText = data.watt.toFixed(3);
                document.getElementById('wh').innerText = data.wh.toFixed(4);
                document.getElementById('mwh').innerText = (data.wh * 1000.0).toFixed(1);
                document.getElementById('cost').innerText = data.cost.toFixed(5);
                document.getElementById('microCost').innerText = (data.cost * 100.0).toFixed(3);

                // Aktuelles Gerät & AI
                if (data.current_device) {
                    let cd = data.current_device;
                    document.getElementById('devIcon').innerText = cd.icon || '📱';
                    document.getElementById('detectedName').innerText = cd.name;
                    document.getElementById('detectedStage').innerText = cd.stage || 'Bulk / Schnellladung';
                    
                    if (cd.is_battery) {
                        document.getElementById('batteryBarSection').style.display = 'block';
                        document.getElementById('chargeBarFill').style.width = Math.min(100, Math.max(5, cd.soc_pct || 10)) + '%';
                        document.getElementById('chargePctText').innerText = (cd.soc_pct || 0) + '%';
                        document.getElementById('remainingWhText').innerText = 'Noch ca. ' + (cd.wh_to_100 || 0).toFixed(2) + ' Wh bis 100%';
                        document.getElementById('detectedMetrics').innerText = `Vorhersage: ${(cd.wh_to_100 || 0).toFixed(2)} Wh verbleibend`;
                    } else {
                        document.getElementById('batteryBarSection').style.display = 'none';
                        document.getElementById('detectedMetrics').innerText = "Dauerbetrieb • Kein automatischer Akkustopp";
                    }
                }

                // Multi-Device Liste
                let listHtml = '';
                (data.devices || []).forEach((d, idx) => {
                    let isCurrent = idx === data.current_device_idx;
                    listHtml += `
                        <div class="s-dev-item ${isCurrent ? 's-dev-active' : ''}">
                            <span>${d.icon || '🔌'} ${d.name} ${isCurrent ? '(Aktiv)' : ''}</span>
                            <span>${d.wh.toFixed(3)} Wh • ${d.cost.toFixed(4)} €</span>
                        </div>
                    `;
                });
                document.getElementById('sessionDeviceList').innerHTML = listHtml;

            } catch(e) {}
        }

        setInterval(fetchTelemetry, 1000);
        fetchTelemetry();
    </script>
</body>
</html>
"""

ADMIN_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>Smart Power Hub • Betreiber Admin Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {
            --bg-color: #090d16;
            --card-bg: #111827;
            --card-sub: #1f2937;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent-primary: #3b82f6;
            --accent-green: #10b981;
            --accent-amber: #f59e0b;
            --accent-red: #ef4444;
            --border-color: #1e293b;
        }
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        body { background-color: var(--bg-color); color: var(--text-main); padding: 24px 16px; min-height: 100vh; }
        .container { max-width: 1020px; margin: auto; }
        
        .admin-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 16px; margin-bottom: 24px; }
        .brand-title { font-size: 22px; font-weight: 800; color: #ffffff; display: flex; align-items: center; gap: 8px; }
        .admin-badge { background: #1e3a8a; color: #93c5fd; font-size: 11.5px; font-weight: 700; padding: 4px 10px; border-radius: 8px; }
        
        .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 24px; }
        .kpi-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 18px; padding: 18px; text-align: left; }
        .kpi-label { font-size: 11.5px; text-transform: uppercase; font-weight: 700; color: var(--text-muted); letter-spacing: 0.5px; }
        .kpi-val { font-size: 26px; font-weight: 800; color: #ffffff; margin-top: 6px; font-variant-numeric: tabular-nums; }
        .kpi-sub { font-size: 11.5px; color: var(--text-muted); margin-top: 4px; }

        .section-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 18px; padding: 20px; margin-bottom: 24px; }
        .section-title { font-size: 16px; font-weight: 700; color: #ffffff; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; }

        .live-box { background: var(--card-sub); border-radius: 14px; padding: 14px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 14px; }
        .live-item { text-align: left; }
        .live-label { font-size: 10.5px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; }
        .live-val { font-size: 18px; font-weight: 800; color: #ffffff; margin-top: 2px; }

        .dev-breakdown-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border-color); font-size: 13.5px; }
        .dev-breakdown-row:last-child { border-bottom: none; }

        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
        th { background: var(--card-sub); color: var(--text-muted); padding: 10px; text-align: left; font-size: 11px; text-transform: uppercase; }
        td { padding: 10px; border-bottom: 1px solid var(--border-color); }
        tr:hover { background: rgba(255,255,255,0.02); }

        .btn-ctrl { padding: 9px 14px; font-size: 12.5px; font-weight: 700; border-radius: 10px; border: none; cursor: pointer; }
        .btn-on { background: var(--accent-green); color: white; }
        .btn-off { background: var(--accent-red); color: white; }
    </style>
</head>
<body>
    <div class="container">
        <div class="admin-header">
            <div>
                <div class="brand-title">⚡ Smart Power Hub • Betreiber Dashboard</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">
                    Station: <code>{{ physical_token }}</code> • Shelly Plug ID: <code>{{ device_id }}</code>
                </div>
            </div>
            <span class="admin-badge">Admin Master Authentifiziert</span>
        </div>

        <div class="grid-4">
            <div class="kpi-card">
                <div class="kpi-label">Umsatz Heute</div>
                <div class="kpi-val" style="color:var(--accent-green);">{{ "%.5f"|format(today_revenue) }} €</div>
                <div class="kpi-sub">Gesamt: {{ "%.5f"|format(total_revenue) }} €</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Energie Heute</div>
                <div class="kpi-val" style="color:var(--accent-primary);">{{ "%.2f"|format(today_wh) }} Wh</div>
                <div class="kpi-sub">Gesamt: {{ "%.4f"|format(total_kwh) }} kWh</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Sitzungen Heute</div>
                <div class="kpi-val">{{ today_sessions }}</div>
                <div class="kpi-sub">Gesamt: {{ total_sessions }} Sitzungen</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Aktive Ladung</div>
                <div class="kpi-val" style="color:{% if live_active %}var(--accent-green){% else %}var(--text-muted){% endif %};">
                    {% if live_active %}⚡ LÄUFT{% else %}BEREIT{% endif %}
                </div>
                <div class="kpi-sub">Relais: {% if relay_on %}EIN{% else %}AUS{% endif %}</div>
            </div>
        </div>

        <div class="section-card">
            <div class="section-title">
                <span>🔴 Live Telemetrie & Notfall-Steuerung</span>
                <div style="display:flex; gap:8px;">
                    <button class="btn-ctrl btn-on" onclick="adminOverride('force_on')">🔌 Not-Einschaltung</button>
                    <button class="btn-ctrl btn-off" onclick="adminOverride('force_off')">🛑 Not-Abschaltung</button>
                </div>
            </div>
            <div class="live-box">
                <div class="live-item">
                    <div class="live-label">Wirkleistung</div>
                    <div class="live-val" style="color:var(--accent-primary);">{{ "%.3f"|format(live_watt) }} W</div>
                </div>
                <div class="live-item">
                    <div class="live-label">Stromstärke</div>
                    <div class="live-val">{{ "%.3f"|format(live_amp) }} A</div>
                </div>
                <div class="live-item">
                    <div class="live-label">Netzspannung</div>
                    <div class="live-val">{{ "%.1f"|format(live_volt) }} V</div>
                </div>
                <div class="live-item">
                    <div class="live-label">Aktiver Nutzer</div>
                    <div class="live-val" style="font-size:13px; font-family:monospace;">{{ active_user or 'Keiner' }}</div>
                </div>
            </div>
        </div>

        <div class="section-card">
            <div class="section-title">📊 Detailliertes Geräte-Breakdown</div>
            {% for key, prof in device_profiles.items() %}
            {% set stat = device_stats.get(key, {'count':0, 'wh':0.0, 'revenue':0.0}) %}
            <div class="dev-breakdown-row">
                <div style="display:flex; align-items:center; gap:10px;">
                    <span style="font-size:20px;">{{ prof.icon }}</span>
                    <div>
                        <strong>{{ prof.name }}</strong>
                        <div style="font-size:11px; color:var(--text-muted);">{{ stat.count }} Ladevorgänge</div>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-weight:700; color:var(--accent-primary);">{{ "%.2f"|format(stat.wh) }} Wh</div>
                    <div style="font-size:11.5px; color:var(--accent-green);">{{ "%.4f"|format(stat.revenue) }} €</div>
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="section-card">
            <div class="section-title">📑 Letzte Sitzungen</div>
            <table>
                <thead>
                    <tr>
                        <th>Beleg-ID</th>
                        <th>Zeitpunkt</th>
                        <th>Geräte</th>
                        <th>Dauer</th>
                        <th>Verbrauch</th>
                        <th>Umsatz</th>
                    </tr>
                </thead>
                <tbody>
                    {% for rec in history_records|reverse %}
                    <tr>
                        <td><code>{{ rec.invoice_id }}</code></td>
                        <td>{{ rec.date }}</td>
                        <td>
                            {% for d in rec.devices %}
                            <span>{{ d.icon }} {{ d.name }}</span><br>
                            {% endfor %}
                        </td>
                        <td>{{ rec.time_formatted }}</td>
                        <td>{{ "%.3f"|format(rec.total_wh) }} Wh</td>
                        <td><strong style="color:var(--accent-green);">{{ "%.5f"|format(rec.total_cost) }} €</strong></td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="6" style="text-align:center; color:var(--text-muted); padding:18px;">Noch keine abgeschlossenen Sitzungen gespeichert.</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        async function adminOverride(action) {
            let res = await fetch('/admin_api/override', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: action })
            });
            let data = await res.json();
            alert(data.message || 'Aktion ausgeführt');
            location.reload();
        }
    </script>
</body>
</html>
"""

# --- ROUTES & API ---

@app.route('/')
def index():
    u, uid = get_or_create_user_session()
    
    if not u.get("station_verified"):
        return render_template_string(SECURITY_LOCK_HTML, required_token=PHYSICAL_STATION_TOKEN, admin_token=ADMIN_SECRET_TOKEN)
    
    return render_template_string(MAIN_PAGE_HTML, strompreis=f"{STROMPREIS_PER_KWH:.2f}")

@app.route('/scan/<token>')
def scan_token(token):
    u, uid = get_or_create_user_session()
    if token == PHYSICAL_STATION_TOKEN:
        session["station_verified"] = True
        u["station_verified"] = True
        return redirect(url_for('index'))
    return "❌ Ungültiger Station-Token. Zugriff verweigert.", 403

@app.route('/status')
def get_status():
    u, uid = get_or_create_user_session()
    
    if not u.get("station_verified"):
        return jsonify({"station_verified": False}), 403

    if u.get("terminated"):
        return jsonify({
            "session_terminated": True,
            "report": u.get("last_report")
        })

    now = time.time()
    
    # Präzise Zeitberechnung (kumulierte Sekunden + aktuelle Live-Spanne)
    live_elapsed = u.get("accumulated_seconds", 0.0)
    if u.get("active") and u.get("start_timestamp"):
        live_elapsed = u.get("accumulated_seconds", 0.0) + (now - u["start_timestamp"])

    with state_lock:
        w = global_state["last_watt"]
        a = global_state["last_amp"]
        v = global_state["last_volt"]
        relay = global_state["relay_on"]

    curr_idx = u.get("current_device_idx", 0)
    devices_copy = []
    for idx, d in enumerate(u.get("devices", [])):
        dev_dur = d.get("duration_sec", 0.0)
        if u.get("active") and idx == curr_idx and d.get("start_timestamp"):
            dev_dur += (now - d["start_timestamp"])
        d_dict = dict(d)
        d_dict["duration_sec"] = dev_dur
        devices_copy.append(d_dict)

    current_device = devices_copy[curr_idx] if 0 <= curr_idx < len(devices_copy) else None

    return jsonify({
        "active": u.get("active", False),
        "paused": u.get("paused", False),
        "watt": w,
        "current_ampere": a,
        "voltage": v,
        "relay_on": relay,
        "elapsed_seconds": live_elapsed,
        "wh": u.get("total_wh", 0.0),
        "kwh": u.get("total_kwh", 0.0),
        "cost": u.get("total_cost", 0.0),
        "battery_80_protection_enabled": u.get("battery_80_protection_enabled", True),
        "battery_80_triggered": u.get("battery_80_triggered", False),
        "battery_100_triggered": u.get("battery_100_triggered", False),
        "stop_reason": u.get("stop_reason"),
        "unplug_dialog_active": u.get("unplug_dialog_active", False),
        "current_device_idx": curr_idx,
        "current_device": current_device,
        "devices": devices_copy,
        "session_terminated": False
    })

@app.route('/start', methods=['POST', 'GET'])
def start():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403

    now = time.time()
    with state_lock:
        global_state["active_user_id"] = uid
        global_state["consecutive_zero_w_count"] = 0

    u["active"] = True
    u["paused"] = False
    u["unplug_dialog_active"] = False
    u["stop_reason"] = None
    u["start_timestamp"] = now

    curr_idx = u.get("current_device_idx", 0)
    if 0 <= curr_idx < len(u.get("devices", [])):
        u["devices"][curr_idx]["start_timestamp"] = now

    async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST', 'GET'])
def stop():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403

    now = time.time()
    if u.get("active") and u.get("start_timestamp"):
        u["accumulated_seconds"] += (now - u["start_timestamp"])
    
    u["active"] = False
    u["paused"] = True
    u["start_timestamp"] = None

    curr_idx = u.get("current_device_idx", 0)
    if 0 <= curr_idx < len(u.get("devices", [])):
        dev = u["devices"][curr_idx]
        if dev.get("start_timestamp"):
            dev["duration_sec"] += (now - dev["start_timestamp"])
            dev["start_timestamp"] = None

    async_cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/set_device', methods=['POST'])
def set_device():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403

    now = time.time()
    data = request.get_json() or {}
    key = data.get("key", "phone")
    custom_name = data.get("name")

    prof = DEVICE_PROFILES.get(key, DEVICE_PROFILES["custom"])
    display_name = custom_name if custom_name else prof["name"]

    curr_idx = u.get("current_device_idx", 0)
    if 0 <= curr_idx < len(u["devices"]) and u["devices"][curr_idx].get("duration_sec", 0) > 3:
        # Vorheriges Gerät abschließen
        if u.get("active") and u["devices"][curr_idx].get("start_timestamp"):
            u["devices"][curr_idx]["duration_sec"] += (now - u["devices"][curr_idx]["start_timestamp"])
            u["devices"][curr_idx]["start_timestamp"] = None

        new_dev = {
            "id": len(u["devices"]) + 1,
            "key": key,
            "name": f"{prof['icon']} {display_name}",
            "icon": prof["icon"],
            "is_battery": prof["is_battery"],
            "start_timestamp": now if u.get("active") else None,
            "duration_sec": 0.0,
            "wh": 0.0,
            "cost": 0.0,
            "peak_w": 0.0,
            "stage": "Bulk / Schnellladung (CC)",
            "soc_pct": 10.0,
            "wh_to_100": prof["nominal_wh"] * 0.9
        }
        u["devices"].append(new_dev)
        u["current_device_idx"] = len(u["devices"]) - 1
    else:
        u["devices"][curr_idx]["key"] = key
        u["devices"][curr_idx]["name"] = f"{prof['icon']} {display_name}"
        u["devices"][curr_idx]["icon"] = prof["icon"]
        u["devices"][curr_idx]["is_battery"] = prof["is_battery"]
        u["devices"][curr_idx]["wh_to_100"] = prof["nominal_wh"] * 0.9
        if u.get("active") and not u["devices"][curr_idx].get("start_timestamp"):
            u["devices"][curr_idx]["start_timestamp"] = now

    u["battery_80_triggered"] = False
    u["battery_100_triggered"] = False
    return jsonify({"status": "ok", "current_device": u["devices"][u["current_device_idx"]]})

@app.route('/device_action', methods=['POST'])
def device_action():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403

    data = request.get_json() or {}
    action = data.get("action")

    if action == "continue":
        return start()

    elif action == "finish":
        return logout()

    return jsonify({"status": "unknown_action"})

@app.route('/resume_beyond_80', methods=['POST'])
def resume_beyond_80():
    u, uid = get_or_create_user_session()
    u["battery_80_protection_enabled"] = False
    u["battery_80_triggered"] = False
    u["stop_reason"] = None
    return start()

@app.route('/toggle_80_protection', methods=['POST'])
def toggle_80_protection():
    u, uid = get_or_create_user_session()
    data = request.get_json() or {}
    u["battery_80_protection_enabled"] = bool(data.get("enabled", True))
    return jsonify({"status": "ok", "enabled": u["battery_80_protection_enabled"]})

@app.route('/logout', methods=['POST', 'GET'])
def logout():
    u, uid = get_or_create_user_session()
    now = time.time()
    
    if u.get("active") and u.get("start_timestamp"):
        u["accumulated_seconds"] += (now - u["start_timestamp"])
        curr_idx = u.get("current_device_idx", 0)
        if 0 <= curr_idx < len(u.get("devices", [])):
            dev = u["devices"][curr_idx]
            if dev.get("start_timestamp"):
                dev["duration_sec"] += (now - dev["start_timestamp"])

    u["active"] = False
    u["paused"] = False
    u["terminated"] = True
    u["start_timestamp"] = None
    async_cloud_control(turn_on=False)

    with state_lock:
        if global_state["active_user_id"] == uid:
            global_state["active_user_id"] = None

    invoice_id = f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

    total_sec = u.get("accumulated_seconds", 0.0)
    report = {
        "invoice_id": invoice_id,
        "date": time.strftime('%d.%m.%Y %H:%M'),
        "total_seconds": total_sec,
        "time_formatted": format_time(total_sec),
        "total_wh": u.get("total_wh", 0.0),
        "total_kwh": u.get("total_kwh", 0.0),
        "total_cost": u.get("total_cost", 0.0),
        "devices": u.get("devices", [])
    }
    u["last_report"] = report

    with state_lock:
        global_state["total_historical_sessions"] += 1
        global_state["total_historical_kwh"] += report["total_kwh"]
        global_state["total_historical_revenue"] += report["total_cost"]

        for d in report["devices"]:
            k = d.get("key", "custom")
            if k in global_state["device_type_stats"]:
                global_state["device_type_stats"][k]["count"] += 1
                global_state["device_type_stats"][k]["wh"] += d.get("wh", 0.0)
                global_state["device_type_stats"][k]["revenue"] += d.get("cost", 0.0)

        session_history_records.append(report)
        save_history()

    return jsonify(report)

@app.route('/download_invoice')
def download_invoice():
    u, uid = get_or_create_user_session()
    report = u.get("last_report")
    if not report:
        report = {
            "invoice_id": "RE-SAMPLE-2026",
            "date": time.strftime('%d.%m.%Y %H:%M'),
            "total_seconds": 900,
            "time_formatted": "00:15:00",
            "total_wh": 25.5,
            "total_kwh": 0.0255,
            "total_cost": 0.00892,
            "devices": [
                {"name": "📱 Smartphone", "icon": "📱", "is_battery": True, "duration_sec": 450, "wh": 10.5, "cost": 0.00367, "stage": "100% Voll"},
                {"name": "💻 Laptop", "icon": "💻", "is_battery": True, "duration_sec": 450, "wh": 15.0, "cost": 0.00525, "stage": "80% Schutz"}
            ]
        }
    pdf_buf = generate_pdf_invoice(report)
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True, download_name=f"{report.get('invoice_id', 'Quittung')}.pdf")

@app.route('/send_email_invoice', methods=['POST'])
def send_email_invoice():
    data = request.get_json() or {}
    recipient = data.get("email")
    report = data.get("report")

    if not recipient or "@" not in recipient:
        return jsonify({"status": "error", "message": "Ungültige E-Mail-Adresse"})

    if not SMTP_USER or not SMTP_PASSWORD:
        return jsonify({
            "status": "error",
            "message": "Hinweis: Keine SMTP-Zugangsdaten konfiguriert. Bitte '📥 PDF direkt herunterladen' nutzen."
        })

    pdf_buf = generate_pdf_invoice(report or {})

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = recipient
        msg["Subject"] = f"Deine Sammelquittung ({report.get('invoice_id', 'Rechnung')})"

        body_text = f"Hallo,\n\nvielen Dank für die Nutzung des Smart Power Hubs.\n\nBeleg-Nr.: {report.get('invoice_id')}\nGesamtdauer: {report.get('time_formatted')}\nGesamtenergie: {report.get('total_wh', 0):.2f} Wh ({report.get('total_kwh', 0):.5f} kWh)\nGesamtbetrag: {report.get('total_cost', 0):.5f} €\n\nIm Anhang findest du deine detaillierte PDF-Sammelquittung aller geladenen Geräte."
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

        pdf_att = MIMEApplication(pdf_buf.read(), _subtype="pdf")
        pdf_att.add_header('Content-Disposition', 'attachment', filename=f"{report.get('invoice_id', 'Quittung')}.pdf")
        msg.attach(pdf_att)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=8)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()

        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"E-Mail-Versand fehlgeschlagen: {str(e)}"
        })

# --- BETREIBER-DASHBOARD (ADMIN) ---
@app.route(f'/admin/{ADMIN_SECRET_TOKEN}')
def admin_dashboard():
    with state_lock:
        today_str = time.strftime('%d.%m.%Y')
        today_records = [r for r in session_history_records if r.get("date", "").startswith(today_str)]
        
        today_revenue = sum(r.get("total_cost", 0.0) for r in today_records)
        today_wh = sum(r.get("total_wh", 0.0) for r in today_records)
        today_sessions_count = len(today_records)

        live_w = global_state["last_watt"]
        live_a = global_state["last_amp"]
        live_v = global_state["last_volt"]
        relay = global_state["relay_on"]
        active_u = global_state["active_user_id"]
        is_live_active = bool(active_u and user_sessions.get(active_u, {}).get("active"))

    return render_template_string(
        ADMIN_DASHBOARD_HTML,
        physical_token=PHYSICAL_STATION_TOKEN,
        device_id=DEVICE_ID,
        today_revenue=today_revenue,
        total_revenue=global_state["total_historical_revenue"],
        today_wh=today_wh,
        total_kwh=global_state["total_historical_kwh"],
        today_sessions=today_sessions_count,
        total_sessions=global_state["total_historical_sessions"],
        live_active=is_live_active,
        relay_on=relay,
        live_watt=live_w,
        live_amp=live_a,
        live_volt=live_v,
        active_user=active_u,
        device_profiles=DEVICE_PROFILES,
        device_stats=global_state["device_type_stats"],
        history_records=session_history_records[-30:]
    )

@app.route('/admin_api/override', methods=['POST'])
def admin_override():
    data = request.get_json() or {}
    action = data.get("action")
    if action == "force_on":
        async_cloud_control(turn_on=True)
        return jsonify({"status": "ok", "message": "Relais manuell EINGESCHALTET"})
    elif action == "force_off":
        async_cloud_control(turn_on=False)
        with state_lock:
            if global_state["active_user_id"] and global_state["active_user_id"] in user_sessions:
                user_sessions[global_state["active_user_id"]]["active"] = False
        return jsonify({"status": "ok", "message": "Notabschaltung ausgeführt (Relais AUS)"})
    return jsonify({"status": "error", "message": "Unbekannte Aktion"}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)