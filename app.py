from flask import Flask, render_template_string, jsonify, session, request, send_file, redirect, url_for
import requests
import time
import threading
import uuid
import os
import json
import smtplib
import io
import logging
from collections import deque
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SmartPowerHub")

WEASYPRINT_AVAILABLE = False
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception:
    pass

REPORTLAB_AVAILABLE = False
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    REPORTLAB_AVAILABLE = True
except Exception:
    pass

app = Flask(__name__)
app.secret_key = "shelly_smart_power_hub_sec_token_2026_isolated"

# --- HARDWARE & CLOUD ---
SHELLY_CLOUD_URL = "https://shelly-274-eu.shelly.cloud"
AUTH_KEY = "NDcwMzFkdWlkF9839F81801CF17665B14F2EED9BDC41514AEAB2C6C041201D306ABBC40BDE2A0AD2F80ACE98C596"
DEVICE_ID = "08927249a904"
PHYSICAL_STATION_TOKEN = "SEC-STATION-2026-X99Q-ALPHA-77"
ADMIN_SECRET_TOKEN = "SEC-ADMIN-MASTER-2026-OMEGA"
STROMPREIS_PER_KWH = 0.35

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = ""

# =====================================================================
# GLOBALER STATUS - EINZIGE QUELLE DER WAHRHEIT FÜR SHELLY-MESSWERTE
# Der Worker schreibt hier rein, /status liest von hier.
# =====================================================================
state_lock = threading.Lock()
global_state = {
    "last_watt": 0.0,         # Aktueller Rohwert von Shelly Cloud
    "last_amp": 0.0,
    "last_volt": 230.0,
    "last_poll_ok": False,    # War der letzte API-Aufruf erfolgreich?
    "last_poll_time": 0.0,    # Unix-Timestamp des letzten erfolgreichen Polls
    "relay_on": False,
    "active_user_id": None,
    "total_historical_sessions": 0,
    "total_historical_kwh": 0.0,
    "total_historical_revenue": 0.0,
    "device_type_stats": {}
}

user_sessions = {}
session_history_records = []
HISTORY_FILE = "station_history.json"

# =====================================================================
# KI-GERÄTEERKENNUNG: Autonome Klassifizierung ohne manuelle Auswahl
# =====================================================================
class DeviceAI:
    """
    Analysiert den Stromfluss-Verlauf und erkennt automatisch:
    - Dauerbetrieb (Lampe, TV, Gerät): Stabile, gleichmäßige Leistung
    - Akku-Ladung (Handy, Laptop, E-Bike): Leistung sinkt mit der Zeit (CC -> CV -> Trickle)
    """

    @staticmethod
    def classify(power_history: list) -> dict:
        """
        power_history: Liste von (timestamp, watt) Tupeln, neueste zuletzt.
        Gibt zurück: dict mit 'type', 'icon', 'name', 'confidence', 'stage', 'soc_pct'
        """
        if len(power_history) < 3:
            return {
                "type": "unknown", "icon": "🔌", "name": "Erkennung läuft...",
                "confidence": 0, "stage": "Daten werden gesammelt...", "soc_pct": 0,
                "is_battery": None
            }

        watts = [w for _, w in power_history if w > 0.05]
        if not watts:
            return {
                "type": "unknown", "icon": "🔌", "name": "Kein Verbrauch erkannt",
                "confidence": 0, "stage": "Warte auf Stromfluss...", "soc_pct": 0,
                "is_battery": None
            }

        current_w = watts[-1]
        peak_w = max(watts)
        avg_w = sum(watts) / len(watts)
        n = len(watts)

        # Leistungsbereich klassifizieren
        if peak_w < 3.0:
            device_class = "trickle"    # Sehr gering: Standby / Trickle
        elif peak_w < 25.0:
            device_class = "small"      # Klein: Handy, kleine Geräte
        elif peak_w < 120.0:
            device_class = "medium"     # Mittel: Laptop, Tablets, TV
        elif peak_w < 400.0:
            device_class = "large"      # Groß: E-Bike, kleine Geräte
        else:
            device_class = "xlarge"    # Sehr groß: Haushaltsgeräte, Dauerlast

        # Trend-Analyse: Sinkt die Leistung über die Zeit? (= Akku-Indikator)
        if n >= 5:
            first_half_avg = sum(watts[:n//2]) / (n//2)
            second_half_avg = sum(watts[n//2:]) / (n - n//2)
            trend_ratio = second_half_avg / first_half_avg if first_half_avg > 0 else 1.0

            # Varianz: Gleichmäßige Leistung = Dauerbetrieb
            variance = sum((w - avg_w)**2 for w in watts) / n
            cv = (variance**0.5) / avg_w if avg_w > 0 else 0  # Variationskoeffizient
        else:
            trend_ratio = 1.0
            cv = 0.0

        # --- KI-ENTSCHEIDUNGSBAUM ---
        is_battery = None
        confidence = 0
        device_type = "unknown"
        icon = "🔌"
        name = "Unbekannt"

        # Dauerbetrieb-Indikatoren: Stabile, gleichmäßige Leistung
        if cv < 0.15 and trend_ratio > 0.90:
            is_battery = False
            confidence = min(95, 50 + n * 5)
            if peak_w < 15:
                device_type = "lamp"; icon = "💡"; name = "Lampe / LED"
            elif peak_w < 60:
                device_type = "tv"; icon = "📺"; name = "TV / Monitor"
            elif peak_w < 250:
                device_type = "appliance_small"; icon = "☕"; name = "Kleines Gerät / Dauerlast"
            else:
                device_type = "appliance"; icon = "🍳"; name = "Großgerät / Dauerlast"

        # Akku-Indikatoren: Hohe Anfangsleistung, abnehmender Trend
        elif trend_ratio < 0.88 or (device_class in ("small", "medium") and current_w < peak_w * 0.75 and n > 8):
            is_battery = True
            confidence = min(92, 40 + n * 6)
            if peak_w < 25:
                device_type = "phone"; icon = "📱"; name = "Smartphone / Tablet"
            elif peak_w < 100:
                device_type = "laptop"; icon = "💻"; name = "Laptop / Ultrabook"
            elif peak_w < 300:
                device_type = "ebike_std"; icon = "🚲"; name = "E-Bike Akku"
            else:
                device_type = "ebike_fast"; icon = "⚡"; name = "E-Bike Schnelllader"
        else:
            # Noch unsicher - sammle mehr Daten
            if device_class in ("small", "medium"):
                is_battery = None; icon = "📱💻"; name = "Smartphone oder Laptop?"
                confidence = 20
            else:
                is_battery = False; icon = "🔌"; name = "Dauerbetrieb (Analyse läuft)"
                confidence = 35

        # SoC-Schätzung für Akku-Geräte
        soc_pct = 0
        stage = "Dauerbetrieb"
        if is_battery and n >= 2:
            # Ladefortschritt aus Leistungsabfall schätzen
            if current_w >= peak_w * 0.85:
                stage = "Schnellladung (CC-Phase)"
                soc_pct = min(75, max(5, int((1 - trend_ratio) * 200)))
            elif current_w >= peak_w * 0.35:
                stage = "Sättigung (CV-Phase)"
                soc_pct = min(95, max(75, 75 + int((1 - (current_w / peak_w)) * 60)))
            else:
                stage = "Erhaltungsladung (Voll)"
                soc_pct = 98
        elif is_battery is False:
            stage = "Dauerbetrieb aktiv"
            soc_pct = 100
        else:
            stage = "Analyse läuft... (" + str(n) + " Messpunkte)"
            soc_pct = 0

        return {
            "type": device_type,
            "icon": icon,
            "name": name,
            "confidence": confidence,
            "stage": stage,
            "soc_pct": soc_pct,
            "is_battery": is_battery,
            "peak_w": round(peak_w, 2),
            "current_w": round(current_w, 3),
            "avg_w": round(avg_w, 3),
            "trend_ratio": round(trend_ratio, 3),
            "cv": round(cv, 3)
        }


# =====================================================================
# PERSISTENZ
# =====================================================================
def load_history():
    global session_history_records
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                session_history_records = data.get("records", [])
                global_state["total_historical_sessions"] = data.get("total_sessions", 0)
                global_state["total_historical_kwh"] = data.get("total_kwh", 0.0)
                global_state["total_historical_revenue"] = data.get("total_revenue", 0.0)
                logger.info(f"Historie: {len(session_history_records)} Datensaetze geladen.")
        except Exception as e:
            logger.error(f"Fehler beim Laden der Historie: {e}")

def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "total_sessions": global_state["total_historical_sessions"],
                "total_kwh": global_state["total_historical_kwh"],
                "total_revenue": global_state["total_historical_revenue"],
                "records": session_history_records[-200:]
            }, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Fehler beim Speichern: {e}")

load_history()


# =====================================================================
# SHELLY RELAIS-STEUERUNG
# =====================================================================
def async_cloud_control(turn_on=True):
    def _do():
        turn_str = "on" if turn_on else "off"
        try:
            requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control",
                          data={"auth_key": AUTH_KEY, "id": DEVICE_ID, "turn": turn_str, "channel": 0},
                          timeout=4.0)
        except Exception as e:
            logger.warning(f"[RELAY] REST fehler: {e}")
        try:
            requests.post(f"{SHELLY_CLOUD_URL}/device/rpc",
                          json={"auth_key": AUTH_KEY, "id": DEVICE_ID,
                                "method": "Switch.Set", "params": {"id": 0, "on": turn_on}},
                          timeout=4.0)
        except Exception as e:
            logger.warning(f"[RELAY] RPC fehler: {e}")
        with state_lock:
            global_state["relay_on"] = turn_on
        logger.info(f"[RELAY] Relais -> {'EIN' if turn_on else 'AUS'}")
    threading.Thread(target=_do, daemon=True).start()


# =====================================================================
# BACKGROUND WORKER - ROBUSTE ARCHITEKTUR
#
# PRINZIP: Worker pollt Shelly IMMER (unabhängig von Sessions).
# Ergebnis geht in global_state. Energie-Akkumulation passiert
# AUCH im Worker, aber nur für aktive Sessions (via Lock+Snapshot).
#
# WARUM DAS FUNKTIONIERT:
# - Kein Abhaengigkeit von Session-Iterierung ohne Lock
# - Shelly-Abfrage ist von Session-Logik getrennt
# - Falls Session-Loop crasht, laeuft Shelly-Polling weiter
# =====================================================================
def background_meter_worker():
    logger.info("[WORKER] Hintergrund-Mess-Worker gestartet!")
    last_loop = time.time()

    while True:
        loop_start = time.time()
        dt = max(0.5, min(5.0, loop_start - last_loop))
        last_loop = loop_start

        # ---- SCHRITT 1: SHELLY CLOUD ABFRAGEN ----
        watt, amp, volt, api_ok = 0.0, 0.0, 230.0, False
        try:
            resp = requests.post(
                f"{SHELLY_CLOUD_URL}/device/status",
                data={"auth_key": AUTH_KEY, "id": DEVICE_ID},
                timeout=4.0
            )
            if resp.status_code == 200:
                res = resp.json()
                if res.get("isok"):
                    status = res.get("data", {}).get("device_status", {})
                    # Gen 2/3 (Plus Plug S, Pro, etc.)
                    if "switch:0" in status:
                        sw = status["switch:0"]
                        watt = float(sw.get("apower", 0.0) or 0.0)
                        amp  = float(sw.get("current", 0.0) or 0.0)
                        volt = float(sw.get("voltage", 230.0) or 230.0)
                        api_ok = True
                        logger.info(f"[WORKER] Shelly Gen3: {watt:.2f}W {amp:.3f}A {volt:.1f}V")
                    # Gen 1
                    elif "meters" in status and status["meters"]:
                        m = status["meters"][0]
                        watt = float(m.get("power", 0.0) or 0.0)
                        amp  = float(m.get("current", watt/230.0 if watt > 0 else 0.0))
                        volt = float(m.get("voltage", 230.0) or 230.0)
                        api_ok = True
                        logger.info(f"[WORKER] Shelly Gen1: {watt:.2f}W {amp:.3f}A")
                    else:
                        logger.warning(f"[WORKER] Unbekannte JSON-Keys: {list(status.keys())[:5]}")
                else:
                    logger.warning(f"[WORKER] isok=False: {res.get('error','?')}")
            else:
                logger.warning(f"[WORKER] HTTP {resp.status_code}")
        except requests.exceptions.Timeout:
            logger.warning("[WORKER] Timeout bei Shelly-Abfrage")
        except Exception as e:
            logger.warning(f"[WORKER] Shelly-Fehler: {e}")

        # ---- SCHRITT 2: GLOBAL STATE AKTUALISIEREN ----
        with state_lock:
            if api_ok:
                global_state["last_watt"] = watt
                global_state["last_amp"]  = amp
                global_state["last_volt"] = volt
                global_state["last_poll_ok"]   = True
                global_state["last_poll_time"] = loop_start
            else:
                # Bei Fehler: Werte beibehalten (nicht auf 0 setzen)
                watt = global_state["last_watt"]
                amp  = global_state["last_amp"]
                volt = global_state["last_volt"]
                global_state["last_poll_ok"] = False

        # ---- SCHRITT 3: AKTIVE SESSIONS AKKUMULIEREN ----
        try:
            with state_lock:
                active_uids = [
                    uid for uid, u in user_sessions.items()
                    if u.get("active", False) and not u.get("terminated", False)
                ]

            for uid in active_uids:
                u = user_sessions.get(uid)
                if not u:
                    continue

                # Laufzeit
                u["total_seconds"] = u.get("total_seconds", 0.0) + dt

                # Energie (Wh) akkumulieren - Formel: P[W] * t[s] / 3600 = Wh
                if watt > 0.05:
                    u["total_wh"]  = u.get("total_wh", 0.0) + (watt * dt) / 3600.0
                u["total_kwh"] = u.get("total_wh", 0.0) / 1000.0
                u["total_cost"]= u["total_kwh"] * STROMPREIS_PER_KWH

                # Messwerte in Session speichern (für /status)
                u["current_watt"]    = watt
                u["current_ampere"]  = amp
                u["current_voltage"] = volt

                # Power history für KI-Analyse (max. 120 Punkte = 2 Minuten)
                history = u.get("power_history", [])
                history.append((loop_start, watt))
                if len(history) > 120:
                    history = history[-120:]
                u["power_history"] = history

                # KI-Geräteerkennung (alle 5 Zyklen)
                tick = u.get("_tick", 0) + 1
                u["_tick"] = tick
                if tick % 5 == 0 or u.get("ai_result") is None:
                    ai = DeviceAI.classify(history)
                    u["ai_result"] = ai

                # Aktuelles Gerät in Devices-Liste aktualisieren
                curr_idx = u.get("current_device_idx", 0)
                devs = u.get("devices", [])
                if 0 <= curr_idx < len(devs):
                    dev = devs[curr_idx]
                    dev["duration_sec"] = dev.get("duration_sec", 0.0) + dt
                    if watt > 0.05:
                        dev["wh"]  = dev.get("wh", 0.0) + (watt * dt) / 3600.0
                    dev["cost"]   = (dev.get("wh", 0.0) / 1000.0) * STROMPREIS_PER_KWH
                    dev["peak_w"] = max(dev.get("peak_w", 0.0), watt)

                    ai = u.get("ai_result", {})
                    dev["stage"]   = ai.get("stage", "Messen...")
                    dev["soc_pct"] = ai.get("soc_pct", 0)
                    dev["ai_name"] = ai.get("name", "Erkennung...")
                    dev["ai_icon"] = ai.get("icon", "🔌")
                    dev["ai_type"] = ai.get("type", "unknown")
                    dev["ai_confidence"] = ai.get("confidence", 0)
                    dev["is_battery"] = ai.get("is_battery")

                logger.info(f"[WORKER] {uid[:8]} | t={u['total_seconds']:.0f}s "
                            f"W={watt:.2f} Wh={u['total_wh']:.4f} "
                            f"EUR={u['total_cost']:.5f} AI={u.get('ai_result', {}).get('name', '?')}")

        except Exception as e:
            logger.error(f"[WORKER] Fehler in Akkumulationsschleife: {e}", exc_info=True)

        # Fester 1-Sekunden-Takt
        elapsed = time.time() - loop_start
        sleep_time = max(0.1, 1.0 - elapsed)
        time.sleep(sleep_time)


# =====================================================================
# WORKER STARTEN (EINMALIG, DAEMON)
# =====================================================================
_worker_started = False
_worker_lock = threading.Lock()

def ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            _worker_started = True
            t = threading.Thread(target=background_meter_worker, daemon=True, name="MeterWorker")
            t.start()
            logger.info("[WORKER] Daemon-Thread gestartet.")

ensure_worker()

@app.before_request
def before_req():
    ensure_worker()


# =====================================================================
# HILFSFUNKTIONEN
# =====================================================================
def format_time(seconds):
    sec = int(max(0, seconds))
    return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"

def get_or_create_user_session():
    if "user_id" not in session:
        session["user_id"] = str(uuid.uuid4())
        session.permanent = True
        session.modified = True
    uid = session["user_id"]
    with state_lock:
        if uid not in user_sessions:
            user_sessions[uid] = {
                "user_id": uid,
                "active": False,
                "paused": False,
                "terminated": False,
                "station_verified": session.get("station_verified", False),
                "total_seconds": 0.0,
                "total_wh": 0.0,
                "total_kwh": 0.0,
                "total_cost": 0.0,
                "current_watt": 0.0,
                "current_ampere": 0.0,
                "current_voltage": 230.0,
                "power_history": [],
                "ai_result": None,
                "_tick": 0,
                "devices": [{
                    "id": 1, "key": "auto",
                    "ai_name": "Erkennung läuft...", "ai_icon": "🔌",
                    "ai_type": "unknown", "ai_confidence": 0,
                    "is_battery": None,
                    "duration_sec": 0.0, "wh": 0.0, "cost": 0.0,
                    "peak_w": 0.0, "stage": "Bereit", "soc_pct": 0
                }],
                "current_device_idx": 0,
                "last_report": None
            }
        else:
            if session.get("station_verified", False):
                user_sessions[uid]["station_verified"] = True
    return user_sessions[uid], uid


# =====================================================================
# PDF-INVOICE GENERATOR
# =====================================================================
def generate_pdf_invoice(report_data):
    pdf_buf = io.BytesIO()
    if REPORTLAB_AVAILABLE:
        doc = SimpleDocTemplate(pdf_buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story = []
        title_style = ParagraphStyle('T', parent=styles['Heading1'], fontSize=18,
                                     textColor=colors.HexColor("#2563eb"), spaceAfter=4)
        meta_style = ParagraphStyle('M', parent=styles['Normal'], fontSize=9,
                                    textColor=colors.HexColor("#64748b"), spaceAfter=12)
        story.append(Paragraph("Smart Power Hub", title_style))
        story.append(Paragraph(
            f"Quittung Nr.: {report_data.get('invoice_id')} | {report_data.get('date')} | "
            f"Tarif: {STROMPREIS_PER_KWH:.2f} EUR/kWh", meta_style))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563eb"), spaceAfter=14))

        table_data = [["Pos", "Geraet", "Dauer", "Energie (Wh)", "Betrag (EUR)"]]
        for idx, d in enumerate(report_data.get("devices", []), 1):
            table_data.append([
                str(idx), d.get("ai_name", d.get("name", "Geraet")),
                format_time(d.get("duration_sec", 0)),
                f"{d.get('wh', 0.0):.3f}", f"{d.get('cost', 0.0):.5f}"
            ])
        table_data.append(["", "GESAMT",
                            format_time(report_data.get("total_seconds", 0)),
                            f"{report_data.get('total_wh', 0.0):.4f}",
                            f"{report_data.get('total_cost', 0.0):.5f}"])

        t = Table(table_data, colWidths=[30, 210, 80, 100, 90])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5f9")),
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
        ]))
        story.append(t)
        story.append(Spacer(1, 20))
        story.append(Paragraph("Vielen Dank fuer die Nutzung der Smart Power Hub Ladestation!", meta_style))
        doc.build(story)
    else:
        pdf_buf.write(b"PDF-Erstellung nicht verfuegbar (reportlab fehlt).")
    pdf_buf.seek(0)
    return pdf_buf


# =====================================================================
# HTML TEMPLATES
# =====================================================================

SECURITY_LOCK_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Smart Power Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:#090d16;color:#f8fafc;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:16px}
.card{background:#111827;border-radius:24px;padding:32px 24px;border:1px solid #1f2937;text-align:center;max-width:440px;width:100%;box-shadow:0 25px 50px -12px rgba(0,0,0,.5)}
h1{font-size:22px;font-weight:800;color:#fff;margin:16px 0 8px}
p{font-size:13.5px;color:#94a3b8;line-height:1.5;margin-bottom:20px}
.token-box{background:#0f172a;border:1px dashed #3b82f6;border-radius:14px;padding:14px;margin-bottom:20px;text-align:left}
.token-label{font-size:11px;text-transform:uppercase;color:#60a5fa;font-weight:700;margin-bottom:4px}
.token-code{font-family:monospace;font-size:13px;color:#f8fafc;word-break:break-all}
.btn{display:block;width:100%;padding:14px;font-size:15px;font-weight:700;background:#3b82f6;color:#fff;border:none;border-radius:14px;text-decoration:none;cursor:pointer;margin-top:10px}
.btn-ghost{background:transparent;border:1px solid #1f2937;color:#94a3b8;font-size:12px;padding:9px}
</style>
</head>
<body>
<div class="card">
<div style="font-size:54px">🔒</div>
<h1>Vor-Ort-Sicherheitsüberprüfung</h1>
<p>Um die Ladestation zu nutzen, musst du physisch vor Ort sein und den QR-Code scannen.</p>
<div class="token-box">
<div class="token-label">Erforderlicher Token:</div>
<div class="token-code">{{ required_token }}</div>
</div>
<a href="/scan/{{ required_token }}" class="btn">📲 Station freischalten</a>
<a href="/admin/{{ admin_token }}" class="btn btn-ghost" style="margin-top:12px">⚙️ Admin-Dashboard</a>
</div>
</body>
</html>"""


MAIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Smart Power Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
:root{
  --bg:#f8fafc;--card:#fff;--text:#090d16;--muted:#64748b;
  --blue:#2563eb;--green:#059669;--amber:#d97706;--red:#dc2626;
  --border:#e2e8f0;--shadow:0 12px 30px -6px rgba(15,23,42,.08);
}
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:var(--bg);color:var(--text);display:flex;justify-content:center;padding:18px 12px;min-height:100vh}
.wrap{width:100%;max-width:440px;margin:auto}
.card{background:var(--card);border-radius:24px;padding:22px 18px;box-shadow:var(--shadow);border:1px solid var(--border)}

/* Header */
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.brand{font-size:18px;font-weight:800;letter-spacing:-.3px}
.rate{background:#f1f5f9;color:var(--muted);font-size:11.5px;padding:4px 10px;border-radius:20px;font-weight:700}

/* Status Badges */
.badges{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;padding:5px 13px;border-radius:30px}
.pill-green{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.pill-off{background:#f1f5f9;color:var(--muted);border:1px solid var(--border)}
.pill-on{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.pill-pause{background:#fffbeb;color:#92400e;border:1px solid #fde68a}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}
.pill-on .dot{background:#059669;box-shadow:0 0 8px rgba(5,150,105,.7)}
.pill-pause .dot{background:#d97706}
.pill-off .dot{background:#94a3b8}

/* KI-Banner */
.ai-banner{background:#f8fafc;border:1px solid var(--border);border-radius:18px;padding:16px;margin-bottom:14px;text-align:left}
.ai-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.ai-label{font-size:10.5px;font-weight:800;text-transform:uppercase;color:var(--blue);letter-spacing:.6px;background:#eff6ff;padding:3px 8px;border-radius:6px}
.ai-conf{font-size:11px;color:var(--muted);font-weight:600}
.ai-body{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.ai-icon{font-size:34px}
.ai-name{font-size:16px;font-weight:800;color:var(--text)}
.ai-stage{font-size:12px;font-weight:600;color:var(--blue);margin-top:2px}
.ai-sub{font-size:11px;color:var(--muted);margin-top:2px}

/* SOC-Balken */
.soc-wrap{background:#e2e8f0;border-radius:10px;height:7px;overflow:hidden;margin-top:2px}
.soc-fill{background:linear-gradient(90deg,#2563eb,#10b981);height:100%;border-radius:10px;transition:width .6s ease}
.soc-row{display:flex;justify-content:space-between;font-size:10.5px;color:var(--muted);margin-top:4px;font-weight:600}

/* Stat-Grid */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.stat{background:#f8fafc;border:1px solid var(--border);border-radius:16px;padding:12px;text-align:left}
.stat-lbl{font-size:10.5px;font-weight:700;text-transform:uppercase;color:var(--muted);letter-spacing:.4px}
.stat-val{font-size:19px;font-weight:800;color:var(--text);margin-top:3px;font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace}
.stat-sub{font-size:10.5px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.blue-val .stat-val{color:var(--blue)}
.green-val .stat-val{color:var(--green)}

/* Buttons */
.btn-group{display:flex;flex-direction:column;gap:8px;margin-top:14px}
.btn{width:100%;padding:13px;font-size:14.5px;font-weight:700;border:none;border-radius:14px;cursor:pointer;transition:transform .1s}
.btn:active{transform:scale(.98)}
.btn-primary{background:#0f172a;color:#fff}
.btn-secondary{background:#f1f5f9;color:var(--text);border:1px solid var(--border)}
.btn-danger{background:#fee2e2;color:var(--red)}

/* Modals */
.modal{display:none;position:fixed;inset:0;background:rgba(9,13,22,.75);backdrop-filter:blur(5px);z-index:999;padding:20px;align-items:center;justify-content:center}
.modal-box{background:#fff;border-radius:24px;padding:24px 20px;text-align:center;max-width:360px;width:100%;animation:pop .25s ease-out}
@keyframes pop{from{transform:scale(.88);opacity:0}to{transform:scale(1);opacity:1}}
.modal h3{font-size:17px;margin-bottom:6px}
.modal p{font-size:12.5px;color:var(--muted);margin-bottom:16px}

/* Receipt */
.receipt{display:none;text-align:left}
.rtable{width:100%;border-collapse:collapse;margin:14px 0;font-size:12.5px}
.rtable th{background:#f1f5f9;padding:8px;font-size:11px;text-transform:uppercase;color:var(--muted)}
.rtable td{padding:8px;border-bottom:1px solid var(--border)}
.total-box{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px;padding:14px;text-align:right;margin-top:14px}
.einput{width:100%;padding:11px;border:1px solid var(--border);border-radius:10px;font-size:13.5px;margin-bottom:8px}
</style>
</head>
<body>

<!-- MODAL: Gerätewechsel / Pause -->
<div id="swapModal" class="modal">
<div class="modal-box" style="border:2px solid var(--blue)">
<div style="font-size:38px;margin-bottom:6px">🔄🔌</div>
<h3>Pause / Gerät gewechselt</h3>
<p>Wie möchtest du fortfahren?</p>
<button class="btn btn-primary" style="background:var(--green);margin-bottom:8px" onclick="deviceAction('continue')">▶️ Gleiches Gerät fortsetzen</button>
<button class="btn btn-primary" style="background:var(--blue);margin-bottom:8px" onclick="startNew()">➕ Neues Gerät anschließen</button>
<button class="btn btn-danger" onclick="deviceAction('finish')">🧾 Sitzung beenden & Quittung</button>
</div>
</div>

<!-- HAUPTKARTE -->
<div class="wrap">
<div class="card" id="mainCard">
<div class="hdr">
<span class="brand">⚡ Smart Power Hub</span>
<span class="rate">{{ strompreis }} €/kWh</span>
</div>

<div class="badges">
<span class="pill pill-green">🔒 Vor-Ort verifiziert</span>
<span class="pill pill-off" id="statusPill">
<span class="dot"></span><span id="statusTxt">Bereit</span>
</span>
</div>

<!-- KI-Geräteerkennung Banner -->
<div class="ai-banner">
<div class="ai-header">
<span class="ai-label">⚡ KI-Geräteerkennung</span>
<span class="ai-conf" id="aiConf">Warte auf Start...</span>
</div>
<div class="ai-body">
<div class="ai-icon" id="aiIcon">🔌</div>
<div>
<div class="ai-name" id="aiName">Automatische Erkennung</div>
<div class="ai-stage" id="aiStage">Gerät einstecken & Start drücken</div>
<div class="ai-sub" id="aiSub">Die KI analysiert den Stromfluss automatisch</div>
</div>
</div>
<div id="socSection" style="display:none">
<div class="soc-wrap"><div class="soc-fill" id="socFill" style="width:5%"></div></div>
<div class="soc-row"><span>Ladestand: <strong id="socPct">5%</strong></span><span id="socEta"></span></div>
</div>
</div>

<!-- Netz-Werte -->
<div class="grid2">
<div class="stat">
<div class="stat-lbl">Netzspannung (U)</div>
<div class="stat-val"><span id="volt">230.0</span> V</div>
<div class="stat-sub">Wechselspannung</div>
</div>
<div class="stat">
<div class="stat-lbl">Stromstärke (I)</div>
<div class="stat-val"><span id="amp">0.000</span> A</div>
<div class="stat-sub"><span id="ma">0</span> mA</div>
</div>
</div>

<!-- Leistung & Zeit -->
<div class="grid2">
<div class="stat blue-val">
<div class="stat-lbl">Wirkleistung (P)</div>
<div class="stat-val"><span id="watt">0.000</span> W</div>
<div class="stat-sub" id="wattSub">Kein Strom</div>
</div>
<div class="stat">
<div class="stat-lbl">Laufzeit</div>
<div class="stat-val" id="timer">00:00:00</div>
<div class="stat-sub">Sekundengenau</div>
</div>
</div>

<!-- Energie & Kosten -->
<div class="grid2">
<div class="stat blue-val">
<div class="stat-lbl">Verbrauch</div>
<div class="stat-val"><span id="wh">0.0000</span> Wh</div>
<div class="stat-sub"><span id="mwh">0.0</span> mWh</div>
</div>
<div class="stat green-val">
<div class="stat-lbl">Kosten</div>
<div class="stat-val"><span id="cost">0.00000</span> €</div>
<div class="stat-sub"><span id="cent">0.000</span> Cent</div>
</div>
</div>

<div class="btn-group">
<button class="btn btn-primary" onclick="startCharging()">▶️ Start / Fortsetzen</button>
<button class="btn btn-secondary" onclick="pauseCharging()">⏸️ Pause</button>
<button class="btn btn-secondary" style="background:#eff6ff;color:var(--blue);border-color:#bfdbfe" onclick="showModal('swapModal')">🔄 Pause / Gerät gewechselt</button>
<button class="btn btn-danger" onclick="deviceAction('finish')">🧾 Beenden & Quittung</button>
</div>
</div>

<!-- QUITTUNG -->
<div class="card receipt" id="receiptCard" style="margin-top:0">
<div style="text-align:center;margin-bottom:18px">
<div style="font-size:44px;margin-bottom:6px">🧾</div>
<div style="font-size:20px;font-weight:800">Stromquittung</div>
<div style="font-size:12px;color:var(--muted);margin-top:3px">Sitzung beendet · Steckdose freigegeben</div>
</div>
<table class="rtable">
<thead><tr><th>Gerät (KI-erkannt)</th><th style="text-align:center">Dauer</th><th style="text-align:right">Wh</th><th style="text-align:right">€</th></tr></thead>
<tbody id="receiptBody"></tbody>
</table>
<div class="total-box">
<div style="font-size:11px;color:#166534;font-weight:700;text-transform:uppercase">Gesamtbetrag</div>
<div style="font-size:24px;font-weight:800;color:#15803d" id="rCost">0.00000 €</div>
<div style="font-size:11px;color:#166534;margin-top:2px">Gesamt: <span id="rWh">0</span> Wh (<span id="rKwh">0</span> kWh)</div>
</div>
<div style="margin-top:18px;background:#f8fafc;border:1px solid var(--border);border-radius:14px;padding:14px">
<div style="font-size:12px;font-weight:700;margin-bottom:6px">📧 Quittung per E-Mail:</div>
<input type="email" id="emailInput" class="einput" placeholder="deine@email.de">
<button class="btn btn-primary" style="background:var(--blue);font-size:13.5px;padding:11px" onclick="sendEmail()">Rechnung senden</button>
<button class="btn btn-secondary" style="font-size:13px;padding:9px;margin-top:6px" onclick="dlPdf()">📥 PDF herunterladen</button>
<div id="emailFb" style="display:none;font-size:12px;font-weight:600;margin-top:8px"></div>
</div>
<div style="margin-top:16px;font-size:11.5px;color:var(--muted);text-align:center">Um die Station erneut zu nutzen, scanne den QR-Code.</div>
</div>
</div>

<script>
let terminated = false, lastReport = null;
let localSec = 0, running = false, timerInterval = null;

function fmtSec(s) {
  s = Math.floor(s);
  return String(Math.floor(s/3600)).padStart(2,'0')+':'+String(Math.floor((s%3600)/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
}
function showModal(id) { document.getElementById(id).style.display='flex'; }
function hideModal(id) { document.getElementById(id).style.display='none'; }

function startTick() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => { if(running && !terminated){ localSec++; document.getElementById('timer').innerText=fmtSec(localSec); } }, 1000);
}
function stopTick() { if(timerInterval) { clearInterval(timerInterval); timerInterval=null; } }

async function post(url, data) {
  try {
    let r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data||{})});
    return await r.json();
  } catch(e) { return {}; }
}

async function startCharging() {
  if(terminated) return;
  running = true; startTick();
  document.getElementById('statusPill').className='pill pill-on';
  document.getElementById('statusTxt').innerText='Aktiv';
  post('/start');
  setTimeout(fetchStatus, 300);
}

async function pauseCharging() {
  if(terminated) return;
  running = false; stopTick();
  document.getElementById('statusPill').className='pill pill-pause';
  document.getElementById('statusTxt').innerText='Pausiert';
  post('/stop');
}

async function startNew() {
  hideModal('swapModal');
  await post('/new_device');
  await startCharging();
}

async function deviceAction(action) {
  hideModal('swapModal');
  if(action==='continue') { await startCharging(); }
  else if(action==='finish') {
    stopTick(); running=false;
    let r = await post('/logout');
    renderReceipt(r);
  }
}

function renderReceipt(report) {
  terminated = true; running = false; stopTick();
  lastReport = report;
  document.getElementById('mainCard').style.display='none';
  document.getElementById('receiptCard').style.display='block';
  let tbody = document.getElementById('receiptBody');
  tbody.innerHTML='';
  (report.devices||[]).forEach(d => {
    let tr = document.createElement('tr');
    tr.innerHTML = `<td><strong>${d.ai_icon||'🔌'} ${d.ai_name||d.name||'Gerät'}</strong></td><td style="text-align:center">${fmtSec(d.duration_sec)}</td><td style="text-align:right">${(d.wh||0).toFixed(3)}</td><td style="text-align:right"><strong>${(d.cost||0).toFixed(5)}</strong></td>`;
    tbody.appendChild(tr);
  });
  document.getElementById('rCost').innerText=(report.total_cost||0).toFixed(5)+' €';
  document.getElementById('rWh').innerText=(report.total_wh||0).toFixed(4);
  document.getElementById('rKwh').innerText=(report.total_kwh||0).toFixed(6);
}

async function fetchStatus() {
  if(terminated) return;
  try {
    let d = await (await fetch('/status',{cache:'no-store'})).json();
    if(!d || d.station_verified===false) return;
    if(d.session_terminated) { renderReceipt(d.report); return; }

    // Timer synchronisieren
    let srvSec = d.elapsed_seconds||0;
    if(d.active) {
      running=true;
      if(!timerInterval) startTick();
      if(Math.abs(localSec-srvSec)>2) { localSec=Math.floor(srvSec); document.getElementById('timer').innerText=fmtSec(localSec); }
    } else {
      running=false; stopTick();
      localSec=Math.floor(srvSec);
      document.getElementById('timer').innerText=fmtSec(localSec);
    }

    // Status-Badge
    let pill=document.getElementById('statusPill'), txt=document.getElementById('statusTxt');
    if(d.active){ pill.className='pill pill-on'; txt.innerText='Aktiv / Strom fließt'; }
    else if(d.paused){ pill.className='pill pill-pause'; txt.innerText='Pausiert'; }
    else { pill.className='pill pill-off'; txt.innerText='Bereit'; }

    // Messwerte
    document.getElementById('volt').innerText=(d.voltage||230).toFixed(1);
    document.getElementById('amp').innerText=(d.current_ampere||0).toFixed(3);
    document.getElementById('ma').innerText=((d.current_ampere||0)*1000).toFixed(0);
    document.getElementById('watt').innerText=(d.watt||0).toFixed(3);
    document.getElementById('wh').innerText=(d.wh||0).toFixed(4);
    document.getElementById('mwh').innerText=((d.wh||0)*1000).toFixed(1);
    document.getElementById('cost').innerText=(d.cost||0).toFixed(5);
    document.getElementById('cent').innerText=((d.cost||0)*100).toFixed(3);
    document.getElementById('wattSub').innerText=(d.watt>0.1)?'Fließt stabil':'Kein Strom / Standby';

    // KI-Geräteerkennung
    let ai = d.ai_result || {};
    document.getElementById('aiIcon').innerText=ai.icon||'🔌';
    document.getElementById('aiName').innerText=ai.name||'Erkenne Gerät...';
    document.getElementById('aiStage').innerText=ai.stage||'Analyse läuft';
    let conf = ai.confidence||0;
    document.getElementById('aiConf').innerText = conf>0 ? `Sicherheit: ${conf}%` : 'Warte auf Daten...';
    document.getElementById('aiSub').innerText = ai.is_battery===true
      ? `Akku-Gerät | Spitze: ${ai.peak_w||0}W | Ø ${ai.avg_w||0}W`
      : ai.is_battery===false
        ? `Dauerbetrieb | Ø ${ai.avg_w||0}W | stabil`
        : 'KI sammelt Messdaten...';

    // SoC-Balken (nur bei Akku)
    if(ai.is_battery===true && ai.soc_pct>0) {
      document.getElementById('socSection').style.display='block';
      document.getElementById('socFill').style.width=Math.min(100,ai.soc_pct)+'%';
      document.getElementById('socPct').innerText=ai.soc_pct+'%';
      document.getElementById('socEta').innerText=ai.stage||'';
    } else {
      document.getElementById('socSection').style.display='none';
    }

  } catch(e) {}
}

async function sendEmail() {
  let email=document.getElementById('emailInput').value.trim();
  let fb=document.getElementById('emailFb');
  if(!email.includes('@')){ fb.style.display='block'; fb.style.color='#dc2626'; fb.innerText='Bitte gültige E-Mail eingeben.'; return; }
  fb.style.display='block'; fb.style.color='#2563eb'; fb.innerText='Sende...';
  let r = await post('/send_email_invoice',{email,report:lastReport});
  fb.style.color=r.status==='ok'?'#059669':'#d97706';
  fb.innerText=r.status==='ok'?'Quittung gesendet!':r.message||'Fehler.';
}
function dlPdf() { window.open('/download_invoice','_blank'); }

setInterval(fetchStatus, 1000);
fetchStatus();
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Admin Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:#090d16;color:#f8fafc;padding:24px 16px}
.wrap{max-width:1020px;margin:auto}
.hdr{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1e293b;padding-bottom:16px;margin-bottom:24px}
.brand{font-size:22px;font-weight:800}
.badge{background:#1e3a8a;color:#93c5fd;font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:8px}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:24px}
.kpi{background:#111827;border:1px solid #1e293b;border-radius:18px;padding:18px}
.kpi-lbl{font-size:11.5px;text-transform:uppercase;font-weight:700;color:#94a3b8}
.kpi-val{font-size:26px;font-weight:800;color:#fff;margin-top:6px}
.kpi-sub{font-size:11.5px;color:#94a3b8;margin-top:4px}
.section{background:#111827;border:1px solid #1e293b;border-radius:18px;padding:20px;margin-bottom:24px}
.section-title{font-size:16px;font-weight:700;color:#fff;margin-bottom:14px;display:flex;justify-content:space-between}
.live-box{background:#1f2937;border-radius:14px;padding:14px;display:flex;flex-wrap:wrap;gap:20px}
.live-item .lbl{font-size:10.5px;text-transform:uppercase;color:#94a3b8;font-weight:700}
.live-item .val{font-size:18px;font-weight:800;color:#fff;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#1f2937;color:#94a3b8;padding:10px;text-align:left;font-size:11px;text-transform:uppercase}
td{padding:10px;border-bottom:1px solid #1e293b}
.btn{padding:9px 14px;font-size:12.5px;font-weight:700;border-radius:10px;border:none;cursor:pointer}
.btn-on{background:#10b981;color:#fff}
.btn-off{background:#ef4444;color:#fff}
</style>
</head>
<body>
<div class="wrap">
<div class="hdr">
<div>
<div class="brand">⚡ Smart Power Hub · Admin</div>
<div style="font-size:12px;color:#94a3b8;margin-top:4px">Station: <code>{{ physical_token }}</code> · Device: <code>{{ device_id }}</code></div>
</div>
<span class="badge">Admin</span>
</div>
<div class="grid4">
<div class="kpi"><div class="kpi-lbl">Umsatz Heute</div><div class="kpi-val" style="color:#10b981">{{ "%.5f"|format(today_revenue) }} €</div><div class="kpi-sub">Gesamt: {{ "%.5f"|format(total_revenue) }} €</div></div>
<div class="kpi"><div class="kpi-lbl">Energie Heute</div><div class="kpi-val" style="color:#3b82f6">{{ "%.2f"|format(today_wh) }} Wh</div><div class="kpi-sub">Gesamt: {{ "%.4f"|format(total_kwh) }} kWh</div></div>
<div class="kpi"><div class="kpi-lbl">Sitzungen Heute</div><div class="kpi-val">{{ today_sessions }}</div><div class="kpi-sub">Gesamt: {{ total_sessions }}</div></div>
<div class="kpi"><div class="kpi-lbl">Live Status</div><div class="kpi-val" style="color:{% if live_active %}#10b981{% else %}#94a3b8{% endif %}">{% if live_active %}AKTIV{% else %}BEREIT{% endif %}</div><div class="kpi-sub">Relais: {% if relay_on %}EIN{% else %}AUS{% endif %} · {{ live_watt|round(2) }} W</div></div>
</div>
<div class="section">
<div class="section-title"><span>Live Telemetrie</span><div style="display:flex;gap:8px"><button class="btn btn-on" onclick="ovr('force_on')">Relais EIN</button><button class="btn btn-off" onclick="ovr('force_off')">Relais AUS</button></div></div>
<div class="live-box">
<div class="live-item"><div class="lbl">Watt</div><div class="val" style="color:#3b82f6">{{ "%.3f"|format(live_watt) }} W</div></div>
<div class="live-item"><div class="lbl">Ampere</div><div class="val">{{ "%.3f"|format(live_amp) }} A</div></div>
<div class="live-item"><div class="lbl">Volt</div><div class="val">{{ "%.1f"|format(live_volt) }} V</div></div>
<div class="live-item"><div class="lbl">Letzter Poll</div><div class="val" style="color:{% if last_poll_ok %}#10b981{% else %}#ef4444{% endif %};font-size:13px">{% if last_poll_ok %}OK{% else %}Fehler{% endif %}</div></div>
</div>
</div>
<div class="section">
<div class="section-title">Letzte Sitzungen</div>
<table><thead><tr><th>Beleg</th><th>Datum</th><th>Gerät (KI)</th><th>Dauer</th><th>Wh</th><th>EUR</th></tr></thead>
<tbody>
{% for rec in history_records|reverse %}
<tr><td><code>{{ rec.invoice_id }}</code></td><td>{{ rec.date }}</td><td>{% for d in rec.devices %}{{ d.ai_icon|default('🔌') }} {{ d.ai_name|default(d.name|default('?')) }}<br>{% endfor %}</td><td>{{ rec.time_formatted }}</td><td>{{ "%.3f"|format(rec.total_wh) }}</td><td><strong style="color:#10b981">{{ "%.5f"|format(rec.total_cost) }}</strong></td></tr>
{% else %}
<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:18px">Noch keine Sitzungen.</td></tr>
{% endfor %}
</tbody></table>
</div>
</div>
<script>
async function ovr(a) {
  let r = await fetch('/admin_api/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});
  let d = await r.json(); alert(d.message||'OK'); location.reload();
}
</script>
</body>
</html>"""


# =====================================================================
# ROUTES
# =====================================================================

@app.after_request
def sec_headers(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
    return r

@app.route('/')
def index():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified"):
        return render_template_string(SECURITY_LOCK_HTML,
                                      required_token=PHYSICAL_STATION_TOKEN,
                                      admin_token=ADMIN_SECRET_TOKEN)
    return render_template_string(MAIN_PAGE_HTML, strompreis=f"{STROMPREIS_PER_KWH:.2f}")

@app.route('/scan/<token>')
def scan_token(token):
    u, uid = get_or_create_user_session()
    if token == PHYSICAL_STATION_TOKEN:
        session["station_verified"] = True
        session.permanent = True
        session.modified = True
        u["station_verified"] = True
        logger.info(f"[SCAN] User {uid[:8]} verifiziert.")
        return redirect(url_for('index'))
    return "Ungültiger Token.", 403

@app.route('/status')
def get_status():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified"):
        return jsonify({"station_verified": False}), 403
    if u.get("terminated"):
        return jsonify({"session_terminated": True, "report": u.get("last_report")})

    with state_lock:
        w    = global_state["last_watt"]
        a    = global_state["last_amp"]
        v    = global_state["last_volt"]
        relay = global_state["relay_on"]

    # Session-Werte (vom Worker geschrieben, falls aktiv; sonst aus global)
    w_display = u.get("current_watt", w) if u.get("active") else w
    a_display = u.get("current_ampere", a) if u.get("active") else a
    v_display = u.get("current_voltage", v)

    sec  = u.get("total_seconds", 0.0)
    wh   = u.get("total_wh", 0.0)
    kwh  = u.get("total_kwh", 0.0)
    cost = u.get("total_cost", 0.0)

    curr_idx = u.get("current_device_idx", 0)
    devices  = [dict(d) for d in u.get("devices", [])]
    cur_dev  = devices[curr_idx] if 0 <= curr_idx < len(devices) else None

    ai_result = u.get("ai_result") or {}

    return jsonify({
        "active": u.get("active", False),
        "paused": u.get("paused", False),
        "watt": round(w_display, 3),
        "current_ampere": round(a_display, 3),
        "voltage": round(v_display, 1),
        "relay_on": relay,
        "elapsed_seconds": round(sec, 1),
        "wh": round(wh, 4),
        "kwh": round(kwh, 6),
        "cost": round(cost, 5),
        "ai_result": ai_result,
        "current_device": cur_dev,
        "devices": devices,
        "session_terminated": False
    })

@app.route('/start', methods=['POST', 'GET'])
def start():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403
    with state_lock:
        global_state["active_user_id"] = uid
    u["active"] = True
    u["paused"] = False
    u["power_history"] = []     # Frische KI-Analyse für jeden neuen Start
    u["ai_result"] = None
    u["_tick"] = 0
    curr_idx = u.get("current_device_idx", 0)
    if 0 <= curr_idx < len(u.get("devices", [])):
        u["devices"][curr_idx]["start_timestamp"] = time.time()
    logger.info(f"[START] User {uid[:8]} startet.")
    async_cloud_control(turn_on=True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST', 'GET'])
def stop():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403
    u["active"] = False
    u["paused"] = True
    logger.info(f"[STOP] User {uid[:8]} pausiert.")
    async_cloud_control(turn_on=False)
    return jsonify({"status": "ok"})

@app.route('/new_device', methods=['POST'])
def new_device():
    u, uid = get_or_create_user_session()
    if not u.get("station_verified") or u.get("terminated"):
        return jsonify({"status": "forbidden"}), 403
    # Neues Gerät in Geräte-Liste aufnehmen
    new_dev = {
        "id": len(u.get("devices", [])) + 1,
        "key": "auto",
        "ai_name": "Erkennung läuft...", "ai_icon": "🔌",
        "ai_type": "unknown", "ai_confidence": 0,
        "is_battery": None,
        "duration_sec": 0.0, "wh": 0.0, "cost": 0.0,
        "peak_w": 0.0, "stage": "Bereit", "soc_pct": 0
    }
    u["devices"].append(new_dev)
    u["current_device_idx"] = len(u["devices"]) - 1
    u["power_history"] = []
    u["ai_result"] = None
    u["_tick"] = 0
    logger.info(f"[NEW_DEV] User {uid[:8]} neues Gerät #{len(u['devices'])}")
    return jsonify({"status": "ok"})

@app.route('/logout', methods=['POST', 'GET'])
def logout():
    u, uid = get_or_create_user_session()
    u["active"] = False
    u["paused"] = False
    u["terminated"] = True
    async_cloud_control(turn_on=False)
    with state_lock:
        if global_state.get("active_user_id") == uid:
            global_state["active_user_id"] = None

    invoice_id = f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"
    total_sec = u.get("total_seconds", 0.0)

    # ai_icon und ai_name aus Gerät übernehmen
    for d in u.get("devices", []):
        d.setdefault("ai_icon", "🔌")
        d.setdefault("ai_name", d.get("name", "Gerät"))
        d.setdefault("name", d.get("ai_name", "Gerät"))

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
        session_history_records.append(report)
    save_history()

    logger.info(f"[LOGOUT] {invoice_id} | {format_time(total_sec)} | {report['total_wh']:.3f}Wh | {report['total_cost']:.5f}EUR")
    return jsonify(report)

@app.route('/download_invoice')
def download_invoice():
    u, uid = get_or_create_user_session()
    report = u.get("last_report", {
        "invoice_id": "RE-SAMPLE", "date": time.strftime('%d.%m.%Y %H:%M'),
        "total_seconds": 900, "time_formatted": "00:15:00",
        "total_wh": 15.0, "total_kwh": 0.015, "total_cost": 0.00525,
        "devices": [{"ai_name": "Smartphone", "ai_icon": "📱", "duration_sec": 900, "wh": 15.0, "cost": 0.00525}]
    })
    pdf = generate_pdf_invoice(report)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{report.get('invoice_id','Quittung')}.pdf")

@app.route('/send_email_invoice', methods=['POST'])
def send_email_invoice():
    data = request.get_json() or {}
    recipient = data.get("email")
    report = data.get("report") or {}
    if not recipient or "@" not in recipient:
        return jsonify({"status": "error", "message": "Ungültige E-Mail"})
    if not SMTP_USER or not SMTP_PASSWORD:
        return jsonify({"status": "error", "message": "Kein SMTP konfiguriert. Bitte PDF herunterladen."})
    try:
        pdf = generate_pdf_invoice(report)
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = recipient
        msg["Subject"] = f"Quittung {report.get('invoice_id','')}"
        msg.attach(MIMEText(f"Gesamtbetrag: {report.get('total_cost',0):.5f} EUR | {report.get('total_wh',0):.2f} Wh", "plain", "utf-8"))
        att = MIMEApplication(pdf.read(), _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename=f"{report.get('invoice_id','Quittung')}.pdf")
        msg.attach(att)
        s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=8)
        s.starttls(); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg); s.quit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route(f'/admin/{ADMIN_SECRET_TOKEN}')
def admin_dashboard():
    today_str = time.strftime('%d.%m.%Y')
    today_recs = [r for r in session_history_records if r.get("date","").startswith(today_str)]
    with state_lock:
        lw = global_state["last_watt"]
        la = global_state["last_amp"]
        lv = global_state["last_volt"]
        relay = global_state["relay_on"]
        poll_ok = global_state["last_poll_ok"]
        auid = global_state.get("active_user_id")
        live_active = bool(auid and user_sessions.get(auid, {}).get("active"))
    return render_template_string(ADMIN_HTML,
        physical_token=PHYSICAL_STATION_TOKEN, device_id=DEVICE_ID,
        today_revenue=sum(r.get("total_cost",0) for r in today_recs),
        total_revenue=global_state["total_historical_revenue"],
        today_wh=sum(r.get("total_wh",0) for r in today_recs),
        total_kwh=global_state["total_historical_kwh"],
        today_sessions=len(today_recs),
        total_sessions=global_state["total_historical_sessions"],
        live_active=live_active, relay_on=relay,
        live_watt=lw, live_amp=la, live_volt=lv,
        last_poll_ok=poll_ok,
        history_records=session_history_records[-30:])

@app.route('/admin_api/override', methods=['POST'])
def admin_override():
    data = request.get_json() or {}
    action = data.get("action")
    if action == "force_on":
        async_cloud_control(turn_on=True)
        return jsonify({"status": "ok", "message": "Relais EIN"})
    elif action == "force_off":
        async_cloud_control(turn_on=False)
        with state_lock:
            auid = global_state.get("active_user_id")
            if auid and auid in user_sessions:
                user_sessions[auid]["active"] = False
        return jsonify({"status": "ok", "message": "Relais AUS"})
    return jsonify({"status": "error"}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)