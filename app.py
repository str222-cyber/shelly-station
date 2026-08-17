from flask import Flask, render_template_string, jsonify, session, request, send_file, redirect, url_for
import requests as http_requests  # Umbenennung um Konflikte zu vermeiden
import time
import threading
import uuid
import os
import json
import smtplib
import io
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SPH")

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
# ARCHITEKTUR v4 - KEIN HINTERGRUND-THREAD FUER KERNFUNKTIONEN
#
# PROBLEM: Gunicorn forkt Worker-Prozesse. Daemon-Threads, die beim
# Modul-Import gestartet werden, sterben beim Fork. Das Flag bleibt
# aber True -> Thread wird nie neu gestartet -> total_seconds bleibt 0.
#
# LOESUNG: ALLE Kernfunktionen laufen direkt in den HTTP-Handlern:
# 1. Timer: On-the-fly berechnet (time.time() - start_time)
# 2. Shelly: Direkt in /status gepollt (mit 1.5s Cache)
# 3. Wh: In /status akkumuliert (dt seit letztem Aufruf)
# 4. KI: In /status berechnet (alle 5 Aufrufe)
#
# KEIN Hintergrund-Thread noetig. Funktioniert garantiert mit
# Gunicorn, Render, Heroku, Docker, egal was.
# =====================================================================

lock = threading.Lock()

# Shelly-Rohwerte (gecacht, max 1.5s alt)
shelly = {
    "watt": 0.0, "amp": 0.0, "volt": 230.0,
    "ok": False, "poll_time": 0.0, "error": ""
}

# Globaler Lade-Zustand
charge = {
    "active": False,
    "paused": False,
    "terminated": False,
    "relay_on": False,
    # Timer: On-the-fly berechnet aus diesen 2 Werten
    "accumulated_seconds": 0.0,  # Gespeicherte Zeit aus frueheren Aktiv-Phasen
    "last_start_time": None,     # Beginn der aktuellen Aktiv-Phase (oder None)
    # Energie: Akkumuliert bei jedem /status-Aufruf
    "last_wh_time": None,        # Letzter Zeitpunkt der Wh-Berechnung
    "total_wh": 0.0,
    "total_kwh": 0.0,
    "total_cost": 0.0,
    # KI
    "power_history": [],
    "ai_result": None,
    "ai_tick": 0,
    # Geraete
    "devices": [],
    "current_device_idx": 0,
    "last_report": None,
}

# Historie
history_records = []
history_stats = {"sessions": 0, "kwh": 0.0, "revenue": 0.0}
HISTORY_FILE = "station_history.json"


# =====================================================================
# KI-GERAETEERKENNUNG
# =====================================================================
class DeviceAI:
    @staticmethod
    def classify(power_history):
        if len(power_history) < 3:
            return {"type": "unknown", "icon": "\U0001f50c", "name": "Erkennung...",
                    "confidence": 0, "stage": "Daten werden gesammelt...", "soc_pct": 0,
                    "is_battery": None, "peak_w": 0, "avg_w": 0, "trend_ratio": 1.0, "cv": 0}
        watts = [w for _, w in power_history if w > 0.05]
        if not watts:
            return {"type": "unknown", "icon": "\U0001f50c", "name": "Kein Verbrauch",
                    "confidence": 0, "stage": "Warte auf Strom...", "soc_pct": 0,
                    "is_battery": None, "peak_w": 0, "avg_w": 0, "trend_ratio": 1.0, "cv": 0}
        cw, pw, aw, n = watts[-1], max(watts), sum(watts)/len(watts), len(watts)
        if n >= 5:
            fh = sum(watts[:n//2]) / max(1, n//2)
            sh = sum(watts[n//2:]) / max(1, n - n//2)
            tr = sh / fh if fh > 0 else 1.0
            var = sum((w - aw)**2 for w in watts) / n
            cv = (var**0.5) / aw if aw > 0 else 0
        else:
            tr, cv = 1.0, 0.0
        ib, conf = None, 0
        if cv < 0.15 and tr > 0.90:
            ib, conf = False, min(95, 50 + n * 5)
            if pw < 15: t, ic, nm = "lamp", "\U0001f4a1", "Lampe / LED"
            elif pw < 60: t, ic, nm = "tv", "\U0001f4fa", "TV / Monitor"
            elif pw < 250: t, ic, nm = "appliance_s", "\u2615", "Kleines Geraet"
            else: t, ic, nm = "appliance", "\U0001f373", "Grossgeraet"
        elif tr < 0.88 or (pw < 120 and cw < pw * 0.75 and n > 8):
            ib, conf = True, min(92, 40 + n * 6)
            if pw < 25: t, ic, nm = "phone", "\U0001f4f1", "Smartphone / Tablet"
            elif pw < 100: t, ic, nm = "laptop", "\U0001f4bb", "Laptop"
            elif pw < 300: t, ic, nm = "ebike", "\U0001f6b2", "E-Bike Akku"
            else: t, ic, nm = "ebike_fast", "\u26a1", "E-Bike Schnelllader"
        else:
            ib, conf = None, 25
            t, ic, nm = "unknown", "\U0001f50c", "Analyse..."
        soc, stage = 0, "Dauerbetrieb"
        if ib and n >= 2:
            if cw >= pw * 0.85: stage, soc = "Schnellladung (CC)", min(75, max(5, int((1-tr)*200)))
            elif cw >= pw * 0.35: stage, soc = "Saettigung (CV)", min(95, max(75, 75+int((1-(cw/pw))*60)))
            else: stage, soc = "Erhaltungsladung", 98
        elif ib is False: stage, soc = "Dauerbetrieb aktiv", 100
        return {"type": t, "icon": ic, "name": nm, "confidence": conf, "stage": stage,
                "soc_pct": soc, "is_battery": ib, "peak_w": round(pw,2), "avg_w": round(aw,3),
                "current_w": round(cw,3), "trend_ratio": round(tr,3), "cv": round(cv,3)}


# =====================================================================
# PERSISTENZ
# =====================================================================
def load_history():
    global history_records, history_stats
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                history_records = data.get("records", [])
                history_stats = data.get("stats", history_stats)
        except Exception as e:
            logger.error(f"History: {e}")

def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"stats": history_stats, "records": history_records[-200:]},
                      f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save: {e}")

load_history()


# =====================================================================
# SHELLY CLOUD - POLL & RELAIS
# =====================================================================
def poll_shelly():
    """Pollt Shelly Cloud API. Ergebnis wird in shelly-Dict gecacht.
    Aufgerufen aus /status (vom Browser ca. jede Sekunde).
    Cache: Maximal 1 Abfrage alle 2.0 Sekunden, um Rate-Limits (HTTP 429) zu vermeiden."""
    now = time.time()
    if now < shelly["poll_time"] + 2.0:
        return  # Cache noch frisch

    try:
        r = http_requests.post(
            f"{SHELLY_CLOUD_URL}/device/status",
            data={"auth_key": AUTH_KEY, "id": DEVICE_ID},
            timeout=3.5
        )
        if r.status_code == 200:
            j = r.json()
            if j.get("isok"):
                ds = j.get("data", {}).get("device_status", {})
                if "switch:0" in ds:
                    sw = ds["switch:0"]
                    shelly["watt"] = float(sw.get("apower", 0) or 0)
                    shelly["amp"]  = float(sw.get("current", 0) or 0)
                    shelly["volt"] = float(sw.get("voltage", 230) or 230)
                    shelly["ok"] = True
                    shelly["error"] = ""
                elif "meters" in ds and ds["meters"]:
                    m = ds["meters"][0]
                    shelly["watt"] = float(m.get("power", 0) or 0)
                    shelly["amp"]  = float(m.get("current", shelly["watt"]/230 if shelly["watt"] > 0 else 0))
                    shelly["volt"] = float(m.get("voltage", 230) or 230)
                    shelly["ok"] = True
                    shelly["error"] = ""
                else:
                    shelly["error"] = f"Unbekannte Keys: {list(ds.keys())[:3]}"
            else:
                shelly["error"] = j.get("error", "isok=false")
        elif r.status_code == 429:
            shelly["error"] = "Rate Limit (429)"
            shelly["poll_time"] = now + 4.0  # 4s Pause bei Rate Limit
            return
        else:
            shelly["error"] = f"HTTP {r.status_code}"
    except http_requests.exceptions.Timeout:
        shelly["error"] = "Timeout"
    except Exception as e:
        shelly["error"] = str(e)[:80]

    shelly["poll_time"] = now


def relay_control(turn_on):
    """Relais schalten (asynchron, blockiert nicht)."""
    def _do():
        s = "on" if turn_on else "off"
        try:
            http_requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control",
                               data={"auth_key": AUTH_KEY, "id": DEVICE_ID, "turn": s, "channel": 0}, timeout=4)
        except: pass
        try:
            http_requests.post(f"{SHELLY_CLOUD_URL}/device/rpc",
                               json={"auth_key": AUTH_KEY, "id": DEVICE_ID,
                                     "method": "Switch.Set", "params": {"id": 0, "on": turn_on}}, timeout=4)
        except: pass
        charge["relay_on"] = turn_on
        logger.info(f"Relay -> {'EIN' if turn_on else 'AUS'}")
    threading.Thread(target=_do, daemon=True).start()


# =====================================================================
# KERNFUNKTIONEN (aufgerufen aus HTTP-Handlern, KEIN Thread)
# =====================================================================

def get_elapsed():
    """Berechnet verstrichene Sekunden on-the-fly. Immer exakt."""
    if charge["active"] and charge["last_start_time"]:
        return charge["accumulated_seconds"] + (time.time() - charge["last_start_time"])
    return charge["accumulated_seconds"]


def accumulate_energy():
    """Akkumuliert Wh basierend auf aktuellem Watt und dt seit letztem Aufruf.
    Wird bei jedem /status Aufruf aufgerufen (ca. 1x pro Sekunde)."""
    if not charge["active"]:
        return

    now = time.time()
    last = charge.get("last_wh_time")

    if last and last > 0:
        dt = now - last
        # Nur realistische dt-Werte akzeptieren (0.2s - 15s)
        if 0.2 < dt < 15.0:
            w = shelly["watt"]
            delta_wh = 0.0
            if w > 0.05:
                delta_wh = (w * dt) / 3600.0
                charge["total_wh"] += delta_wh
                charge["total_kwh"] = charge["total_wh"] / 1000.0
                charge["total_cost"] = charge["total_kwh"] * STROMPREIS_PER_KWH

            # Power-History fuer KI (max 120 Punkte)
            charge["power_history"].append((now, w))
            if len(charge["power_history"]) > 120:
                charge["power_history"] = charge["power_history"][-120:]

            # Aktuelles Geraet aktualisieren
            idx = charge["current_device_idx"]
            devs = charge["devices"]
            if 0 <= idx < len(devs):
                d = devs[idx]
                d["duration_sec"] = d.get("duration_sec", 0) + dt
                d["wh"] = d.get("wh", 0) + delta_wh
                d["cost"] = (d["wh"] / 1000.0) * STROMPREIS_PER_KWH
                d["peak_w"] = max(d.get("peak_w", 0), w)

    charge["last_wh_time"] = now

    # KI alle 5 Aufrufe
    charge["ai_tick"] = charge.get("ai_tick", 0) + 1
    if charge["ai_tick"] % 5 == 0 or charge["ai_result"] is None:
        ai = DeviceAI.classify(charge["power_history"])
        charge["ai_result"] = ai
        idx = charge["current_device_idx"]
        devs = charge["devices"]
        if 0 <= idx < len(devs):
            devs[idx]["ai_name"] = ai.get("name", "?")
            devs[idx]["ai_icon"] = ai.get("icon", "\U0001f50c")
            devs[idx]["ai_type"] = ai.get("type", "unknown")
            devs[idx]["ai_confidence"] = ai.get("confidence", 0)
            devs[idx]["is_battery"] = ai.get("is_battery")
            devs[idx]["stage"] = ai.get("stage", "?")
            devs[idx]["soc_pct"] = ai.get("soc_pct", 0)


# =====================================================================
# HILFSFUNKTIONEN
# =====================================================================
def fmt_time(s):
    s = int(max(0, s))
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def new_device_entry(idx):
    return {"id": idx, "key": "auto",
            "ai_name": "Erkennung...", "ai_icon": "\U0001f50c",
            "ai_type": "unknown", "ai_confidence": 0, "is_battery": None,
            "duration_sec": 0, "wh": 0, "cost": 0, "peak_w": 0,
            "stage": "Bereit", "soc_pct": 0}


# =====================================================================
# ROUTES
# =====================================================================

@app.after_request
def headers(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r

@app.route('/')
def index():
    if not session.get("station_verified"):
        return render_template_string(LOCK_HTML,
            required_token=PHYSICAL_STATION_TOKEN, admin_token=ADMIN_SECRET_TOKEN)
    return render_template_string(MAIN_HTML, strompreis=f"{STROMPREIS_PER_KWH:.2f}")

@app.route('/scan/<token>')
def scan(token):
    if token == PHYSICAL_STATION_TOKEN:
        session["station_verified"] = True
        session.permanent = True
        session.modified = True
        return redirect(url_for('index'))
    return "Ungueltiger Token.", 403


# --- STATUS: HERZSTÜCK - berechnet alles on-the-fly ---
@app.route('/status')
def get_status():
    # 1. Shelly pollen (gecacht, max alle 1.5s)
    poll_shelly()

    # 2. Energie akkumulieren (wenn aktiv)
    with lock:
        accumulate_energy()

        # 3. Timer on-the-fly berechnen
        elapsed = get_elapsed()

        return jsonify({
            "active": charge["active"],
            "paused": charge["paused"],
            "terminated": charge["terminated"],
            "relay_on": charge["relay_on"],
            "watt": round(shelly["watt"], 3),
            "current_ampere": round(shelly["amp"], 3),
            "voltage": round(shelly["volt"], 1),
            "shelly_ok": shelly["ok"],
            "shelly_error": shelly["error"],
            "elapsed_seconds": round(elapsed, 1),
            "wh": round(charge["total_wh"], 4),
            "kwh": round(charge["total_kwh"], 6),
            "cost": round(charge["total_cost"], 5),
            "ai_result": charge["ai_result"] or {},
            "devices": [dict(d) for d in charge["devices"]],
            "session_terminated": charge["terminated"],
            "report": charge["last_report"] if charge["terminated"] else None
        })


# --- START ---
@app.route('/start', methods=['POST', 'GET'])
def start_charge():
    with lock:
        if charge["terminated"]:
            # Neue Sitzung
            charge["terminated"] = False
            charge["last_report"] = None
            charge["accumulated_seconds"] = 0.0
            charge["last_start_time"] = None
            charge["last_wh_time"] = None
            charge["total_wh"] = 0.0
            charge["total_kwh"] = 0.0
            charge["total_cost"] = 0.0
            charge["power_history"] = []
            charge["ai_result"] = None
            charge["ai_tick"] = 0
            charge["devices"] = [new_device_entry(1)]
            charge["current_device_idx"] = 0

        if not charge["active"]:
            charge["active"] = True
            charge["paused"] = False
            charge["last_start_time"] = time.time()
            charge["last_wh_time"] = time.time()
            if not charge["devices"]:
                charge["devices"] = [new_device_entry(1)]
                charge["current_device_idx"] = 0
            charge["power_history"] = []
            charge["ai_result"] = None
            charge["ai_tick"] = 0
            logger.info(f">>> START (t_acc={charge['accumulated_seconds']:.1f}s) <<<")

    relay_control(True)
    return jsonify({"status": "ok"})


# --- STOP / PAUSE ---
@app.route('/stop', methods=['POST', 'GET'])
def stop_charge():
    with lock:
        if charge["active"] and charge["last_start_time"]:
            # Akkumulierte Zeit sichern
            charge["accumulated_seconds"] += time.time() - charge["last_start_time"]
            accumulate_energy()  # Letzte Wh berechnen
        charge["active"] = False
        charge["paused"] = True
        charge["last_start_time"] = None
        charge["last_wh_time"] = None
    relay_control(False)
    logger.info(f">>> PAUSE (t_acc={charge['accumulated_seconds']:.1f}s) <<<")
    return jsonify({"status": "ok"})


# --- NEUES GERAET ---
@app.route('/new_device', methods=['POST'])
def new_device():
    with lock:
        idx = len(charge["devices"]) + 1
        charge["devices"].append(new_device_entry(idx))
        charge["current_device_idx"] = len(charge["devices"]) - 1
        charge["power_history"] = []
        charge["ai_result"] = None
        charge["ai_tick"] = 0
    return jsonify({"status": "ok"})


# --- BEENDEN & QUITTUNG ---
@app.route('/logout', methods=['POST', 'GET'])
def logout():
    with lock:
        if charge["active"] and charge["last_start_time"]:
            charge["accumulated_seconds"] += time.time() - charge["last_start_time"]
            accumulate_energy()

        charge["active"] = False
        charge["paused"] = False
        charge["terminated"] = True
        charge["last_start_time"] = None
        charge["last_wh_time"] = None

        elapsed = charge["accumulated_seconds"]
        invoice_id = f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

        for d in charge["devices"]:
            d.setdefault("ai_icon", "\U0001f50c")
            d.setdefault("ai_name", d.get("name", "Geraet"))

        report = {
            "invoice_id": invoice_id,
            "date": time.strftime('%d.%m.%Y %H:%M'),
            "total_seconds": elapsed,
            "time_formatted": fmt_time(elapsed),
            "total_wh": charge["total_wh"],
            "total_kwh": charge["total_kwh"],
            "total_cost": charge["total_cost"],
            "devices": [dict(d) for d in charge["devices"]]
        }
        charge["last_report"] = report

        history_stats["sessions"] += 1
        history_stats["kwh"] += report["total_kwh"]
        history_stats["revenue"] += report["total_cost"]
        history_records.append(report)

    relay_control(False)
    save_history()
    logger.info(f"LOGOUT {invoice_id} | {fmt_time(elapsed)} | {report['total_wh']:.3f}Wh | {report['total_cost']:.5f}EUR")
    return jsonify(report)


# --- DEBUG ---
@app.route('/debug')
def debug():
    elapsed = get_elapsed()
    return jsonify({
        "shelly": dict(shelly),
        "charge_active": charge["active"],
        "charge_paused": charge["paused"],
        "accumulated_seconds": charge["accumulated_seconds"],
        "last_start_time": charge["last_start_time"],
        "elapsed_calculated": round(elapsed, 1),
        "total_wh": charge["total_wh"],
        "total_cost": charge["total_cost"],
        "devices_count": len(charge["devices"]),
        "power_history_len": len(charge["power_history"]),
        "ai_result": charge["ai_result"],
        "server_time": time.time()
    })


# --- PDF ---
@app.route('/download_invoice')
def download_invoice():
    report = charge.get("last_report") or {
        "invoice_id": "SAMPLE", "date": time.strftime('%d.%m.%Y %H:%M'),
        "total_seconds": 0, "time_formatted": "00:00:00",
        "total_wh": 0, "total_kwh": 0, "total_cost": 0, "devices": []}
    pdf = generate_pdf(report)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{report.get('invoice_id', 'Q')}.pdf")

def generate_pdf(report):
    buf = io.BytesIO()
    if REPORTLAB_AVAILABLE:
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story = []
        ts = ParagraphStyle('T', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor("#2563eb"))
        ms = ParagraphStyle('M', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor("#64748b"), spaceAfter=12)
        story.append(Paragraph("Smart Power Hub - Quittung", ts))
        story.append(Paragraph(f"Nr: {report.get('invoice_id')} | {report.get('date')} | {STROMPREIS_PER_KWH:.2f} EUR/kWh", ms))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563eb"), spaceAfter=14))
        tbl = [["Pos", "Geraet", "Dauer", "Wh", "EUR"]]
        for i, d in enumerate(report.get("devices", []), 1):
            tbl.append([str(i), d.get("ai_name", "Geraet"), fmt_time(d.get("duration_sec", 0)),
                        f"{d.get('wh', 0):.3f}", f"{d.get('cost', 0):.5f}"])
        tbl.append(["", "GESAMT", fmt_time(report.get("total_seconds", 0)),
                     f"{report.get('total_wh', 0):.4f}", f"{report.get('total_cost', 0):.5f}"])
        t = Table(tbl, colWidths=[30, 210, 80, 100, 90])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5f9")),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
            ('LINEBELOW', (0,0), (-1,0), 1.5, colors.HexColor("#cbd5e1")),
            ('LINEBELOW', (0,1), (-1,-2), 0.5, colors.HexColor("#e2e8f0")),
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor("#f0fdf4")),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ]))
        story.append(t)
        doc.build(story)
    else:
        buf.write(b"PDF nicht verfuegbar.")
    buf.seek(0)
    return buf


# --- EMAIL ---
@app.route('/send_email_invoice', methods=['POST'])
def send_email():
    data = request.get_json() or {}
    email_to = data.get("email")
    report = data.get("report") or charge.get("last_report") or {}
    if not email_to or "@" not in email_to:
        return jsonify({"status": "error", "message": "Ungueltige E-Mail"})
    if not SMTP_USER:
        return jsonify({"status": "error", "message": "SMTP nicht konfiguriert."})
    try:
        pdf = generate_pdf(report)
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER; msg["To"] = email_to
        msg["Subject"] = f"Quittung {report.get('invoice_id', '')}"
        msg.attach(MIMEText(f"Betrag: {report.get('total_cost',0):.5f} EUR", "plain", "utf-8"))
        att = MIMEApplication(pdf.read(), _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename="Quittung.pdf")
        msg.attach(att)
        s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=8)
        s.starttls(); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg); s.quit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# --- ADMIN ---
@app.route(f'/admin/{ADMIN_SECRET_TOKEN}')
def admin():
    today = time.strftime('%d.%m.%Y')
    today_recs = [r for r in history_records if r.get("date", "").startswith(today)]
    return render_template_string(ADMIN_HTML,
        physical_token=PHYSICAL_STATION_TOKEN, device_id=DEVICE_ID,
        today_revenue=sum(r.get("total_cost", 0) for r in today_recs),
        total_revenue=history_stats["revenue"],
        today_wh=sum(r.get("total_wh", 0) for r in today_recs),
        total_kwh=history_stats["kwh"],
        today_sessions=len(today_recs),
        total_sessions=history_stats["sessions"],
        live_active=charge["active"], relay_on=charge["relay_on"],
        live_watt=shelly["watt"], live_amp=shelly["amp"], live_volt=shelly["volt"],
        last_poll_ok=shelly["ok"],
        history_records=history_records[-30:])

@app.route('/admin_api/override', methods=['POST'])
def admin_override():
    data = request.get_json() or {}
    action = data.get("action")
    if action == "force_on":
        relay_control(True)
        return jsonify({"status": "ok", "message": "Relais EIN"})
    elif action == "force_off":
        relay_control(False)
        with lock:
            if charge["active"] and charge["last_start_time"]:
                charge["accumulated_seconds"] += time.time() - charge["last_start_time"]
            charge["active"] = False
            charge["last_start_time"] = None
        return jsonify({"status": "ok", "message": "Relais AUS"})
    return jsonify({"status": "error"}), 400


# =====================================================================
# HTML TEMPLATES
# =====================================================================

LOCK_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Smart Power Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:#090d16;color:#f8fafc;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:16px}
.card{background:#111827;border-radius:24px;padding:32px 24px;border:1px solid #1f2937;text-align:center;max-width:440px;width:100%;box-shadow:0 25px 50px -12px rgba(0,0,0,.5)}
h1{font-size:22px;font-weight:800;margin:16px 0 8px}
p{font-size:13.5px;color:#94a3b8;line-height:1.5;margin-bottom:20px}
.tbox{background:#0f172a;border:1px dashed #3b82f6;border-radius:14px;padding:14px;margin-bottom:20px;text-align:left}
.tlbl{font-size:11px;text-transform:uppercase;color:#60a5fa;font-weight:700;margin-bottom:4px}
.tcode{font-family:monospace;font-size:13px;word-break:break-all}
.btn{display:block;width:100%;padding:14px;font-size:15px;font-weight:700;background:#3b82f6;color:#fff;border:none;border-radius:14px;text-decoration:none;cursor:pointer;margin-top:10px}
.btn2{background:transparent;border:1px solid #1f2937;color:#94a3b8;font-size:12px;padding:9px}
</style></head><body>
<div class="card">
<div style="font-size:54px">🔒</div>
<h1>Vor-Ort-Sicherheitspruefung</h1>
<p>Scanne den QR-Code an der Ladestation.</p>
<div class="tbox"><div class="tlbl">Token:</div><div class="tcode">{{ required_token }}</div></div>
<a href="/scan/{{ required_token }}" class="btn">📲 Station freischalten</a>
<a href="/admin/{{ admin_token }}" class="btn btn2" style="margin-top:12px;text-decoration:none">⚙️ Admin</a>
</div></body></html>"""


MAIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Smart Power Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
:root{--bg:#f8fafc;--card:#fff;--text:#090d16;--muted:#64748b;--blue:#2563eb;--green:#059669;--red:#dc2626;--border:#e2e8f0}
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:var(--bg);color:var(--text);display:flex;justify-content:center;padding:18px 12px;min-height:100vh}
.wrap{width:100%;max-width:440px}
.card{background:var(--card);border-radius:24px;padding:22px 18px;box-shadow:0 12px 30px -6px rgba(15,23,42,.08);border:1px solid var(--border)}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.brand{font-size:18px;font-weight:800;letter-spacing:-.3px}
.rate{background:#f1f5f9;color:var(--muted);font-size:11.5px;padding:4px 10px;border-radius:20px;font-weight:700}
.badges{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;padding:5px 13px;border-radius:30px}
.pill-g{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.pill-off{background:#f1f5f9;color:var(--muted);border:1px solid var(--border)}
.pill-on{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.pill-p{background:#fffbeb;color:#92400e;border:1px solid #fde68a}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}
.pill-on .dot{background:#059669;box-shadow:0 0 8px rgba(5,150,105,.7)}
.pill-p .dot{background:#d97706}
.pill-off .dot{background:#94a3b8}
.ai-b{background:#f8fafc;border:1px solid var(--border);border-radius:18px;padding:16px;margin-bottom:14px}
.ai-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.ai-l{font-size:10.5px;font-weight:800;text-transform:uppercase;color:var(--blue);letter-spacing:.6px;background:#eff6ff;padding:3px 8px;border-radius:6px}
.ai-c{font-size:11px;color:var(--muted);font-weight:600}
.ai-row{display:flex;align-items:center;gap:12px;margin-bottom:8px}
.ai-i{font-size:34px}
.ai-n{font-size:16px;font-weight:800}
.ai-s{font-size:12px;font-weight:600;color:var(--blue);margin-top:2px}
.ai-sub{font-size:11px;color:var(--muted);margin-top:2px}
.soc-w{background:#e2e8f0;border-radius:10px;height:7px;overflow:hidden;margin-top:4px}
.soc-f{background:linear-gradient(90deg,#2563eb,#10b981);height:100%;border-radius:10px;transition:width .6s}
.soc-r{display:flex;justify-content:space-between;font-size:10.5px;color:var(--muted);margin-top:4px;font-weight:600}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.st{background:#f8fafc;border:1px solid var(--border);border-radius:16px;padding:12px;text-align:left}
.st-l{font-size:10.5px;font-weight:700;text-transform:uppercase;color:var(--muted);letter-spacing:.4px}
.st-v{font-size:19px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace}
.st-s{font-size:10.5px;color:var(--muted);margin-top:2px}
.bl .st-v{color:var(--blue)}
.gr .st-v{color:var(--green)}
.btns{display:flex;flex-direction:column;gap:8px;margin-top:14px}
.btn{width:100%;padding:13px;font-size:14.5px;font-weight:700;border:none;border-radius:14px;cursor:pointer;transition:transform .1s}
.btn:active{transform:scale(.98)}
.bp{background:#0f172a;color:#fff}
.bs{background:#f1f5f9;color:var(--text);border:1px solid var(--border)}
.bd{background:#fee2e2;color:var(--red)}
.modal{display:none;position:fixed;inset:0;background:rgba(9,13,22,.75);backdrop-filter:blur(5px);z-index:999;padding:20px;align-items:center;justify-content:center}
.mbox{background:#fff;border-radius:24px;padding:24px 20px;text-align:center;max-width:360px;width:100%;animation:pop .25s ease-out}
@keyframes pop{from{transform:scale(.88);opacity:0}to{transform:scale(1);opacity:1}}
.mbox h3{font-size:17px;margin-bottom:6px}
.mbox p{font-size:12.5px;color:var(--muted);margin-bottom:16px}
.receipt{display:none}
.rtbl{width:100%;border-collapse:collapse;margin:14px 0;font-size:12.5px}
.rtbl th{background:#f1f5f9;padding:8px;font-size:11px;text-transform:uppercase;color:var(--muted)}
.rtbl td{padding:8px;border-bottom:1px solid var(--border)}
.tbox{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px;padding:14px;text-align:right;margin-top:14px}
.ein{width:100%;padding:11px;border:1px solid var(--border);border-radius:10px;font-size:13.5px;margin-bottom:8px}
</style></head><body>

<div id="swapM" class="modal"><div class="mbox" style="border:2px solid var(--blue)">
<div style="font-size:38px;margin-bottom:6px">🔄🔌</div>
<h3>Pause / Geraet wechseln</h3><p>Wie fortfahren?</p>
<button class="btn bp" style="background:var(--green);margin-bottom:8px" onclick="devAct('continue')">▶️ Gleiches Geraet</button>
<button class="btn bp" style="background:var(--blue);margin-bottom:8px" onclick="startNew()">➕ Neues Geraet</button>
<button class="btn bd" onclick="devAct('finish')">🧾 Beenden</button>
</div></div>

<div class="wrap">
<div class="card" id="mainC">
<div class="hdr"><span class="brand">⚡ Smart Power Hub</span><span class="rate">{{ strompreis }} EUR/kWh</span></div>
<div class="badges">
<span class="pill pill-g">🔒 Verifiziert</span>
<span class="pill pill-off" id="sPill"><span class="dot"></span><span id="sTxt">Bereit</span></span>
</div>

<div class="ai-b">
<div class="ai-h"><span class="ai-l">⚡ KI-Erkennung</span><span class="ai-c" id="aiC">Warte...</span></div>
<div class="ai-row"><div class="ai-i" id="aiI">🔌</div><div><div class="ai-n" id="aiN">Automatische Erkennung</div><div class="ai-s" id="aiS">Start druecken</div><div class="ai-sub" id="aiSub">KI analysiert den Stromfluss</div></div></div>
<div id="socSec" style="display:none"><div class="soc-w"><div class="soc-f" id="socF" style="width:5%"></div></div><div class="soc-r"><span>Ladestand: <b id="socP">5%</b></span><span id="socE"></span></div></div>
</div>

<div class="g2">
<div class="st"><div class="st-l">Spannung (U)</div><div class="st-v"><span id="volt">230.0</span> V</div></div>
<div class="st"><div class="st-l">Strom (I)</div><div class="st-v"><span id="amp">0.000</span> A</div><div class="st-s"><span id="ma">0</span> mA</div></div>
</div>
<div class="g2">
<div class="st bl"><div class="st-l">Leistung (P)</div><div class="st-v"><span id="watt">0.000</span> W</div><div class="st-s" id="wSub">Kein Strom</div></div>
<div class="st"><div class="st-l">Laufzeit</div><div class="st-v" id="timer">00:00:00</div></div>
</div>
<div class="g2">
<div class="st bl"><div class="st-l">Verbrauch</div><div class="st-v"><span id="wh">0.0000</span> Wh</div><div class="st-s"><span id="mwh">0.0</span> mWh</div></div>
<div class="st gr"><div class="st-l">Kosten</div><div class="st-v"><span id="cost">0.00000</span> EUR</div><div class="st-s"><span id="cent">0.000</span> Cent</div></div>
</div>

<div class="btns">
<button class="btn bp" onclick="doStart()">▶️ Start / Fortsetzen</button>
<button class="btn bs" onclick="doStop()">⏸️ Pause</button>
<button class="btn bs" style="background:#eff6ff;color:var(--blue);border-color:#bfdbfe" onclick="showM('swapM')">🔄 Geraet wechseln</button>
<button class="btn bd" onclick="devAct('finish')">🧾 Beenden & Quittung</button>
</div>
</div>

<div class="card receipt" id="recC" style="margin-top:0">
<div style="text-align:center;margin-bottom:18px">
<div style="font-size:44px;margin-bottom:6px">🧾</div>
<div style="font-size:20px;font-weight:800">Stromquittung</div>
</div>
<table class="rtbl"><thead><tr><th>Geraet</th><th style="text-align:center">Dauer</th><th style="text-align:right">Wh</th><th style="text-align:right">EUR</th></tr></thead><tbody id="recB"></tbody></table>
<div class="tbox">
<div style="font-size:11px;color:#166534;font-weight:700">GESAMTBETRAG</div>
<div style="font-size:24px;font-weight:800;color:#15803d" id="rCost">0 EUR</div>
<div style="font-size:11px;color:#166534;margin-top:2px"><span id="rWh">0</span> Wh (<span id="rKwh">0</span> kWh)</div>
</div>
<div style="margin-top:18px;background:#f8fafc;border:1px solid var(--border);border-radius:14px;padding:14px">
<div style="font-size:12px;font-weight:700;margin-bottom:6px">📧 Quittung per E-Mail:</div>
<input type="email" id="emIn" class="ein" placeholder="deine@email.de">
<button class="btn bp" style="background:var(--blue);font-size:13.5px;padding:11px" onclick="sendEm()">Senden</button>
<button class="btn bs" style="font-size:13px;padding:9px;margin-top:6px" onclick="window.open('/download_invoice','_blank')">📥 PDF</button>
<div id="emFb" style="display:none;font-size:12px;font-weight:600;margin-top:8px"></div>
</div>
</div>
</div>

<script>
var done=false, lastR=null;

function fs(s){s=Math.floor(Math.max(0,s));return String(Math.floor(s/3600)).padStart(2,'0')+':'+String(Math.floor((s%3600)/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function showM(id){document.getElementById(id).style.display='flex'}
function hideM(id){document.getElementById(id).style.display='none'}

function post(u,d){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})}).then(function(r){return r.json()}).catch(function(){return {}})}

function doStart(){
  if(done)return;
  post('/start').then(function(){poll()});
}
function doStop(){
  if(done)return;
  post('/stop').then(function(){poll()});
}
function startNew(){hideM('swapM');post('/new_device').then(function(){doStart()})}
function devAct(a){
  hideM('swapM');
  if(a==='continue'){doStart()}
  else if(a==='finish'){post('/logout').then(function(r){showReceipt(r)})}
}

function showReceipt(rp){
  done=true;lastR=rp;
  document.getElementById('mainC').style.display='none';
  document.getElementById('recC').style.display='block';
  var tb=document.getElementById('recB');tb.innerHTML='';
  (rp.devices||[]).forEach(function(d){var tr=document.createElement('tr');tr.innerHTML='<td><b>'+(d.ai_icon||'🔌')+' '+(d.ai_name||'Geraet')+'</b></td><td style="text-align:center">'+fs(d.duration_sec||0)+'</td><td style="text-align:right">'+(d.wh||0).toFixed(3)+'</td><td style="text-align:right"><b>'+(d.cost||0).toFixed(5)+'</b></td>';tb.appendChild(tr)});
  document.getElementById('rCost').innerText=(rp.total_cost||0).toFixed(5)+' EUR';
  document.getElementById('rWh').innerText=(rp.total_wh||0).toFixed(4);
  document.getElementById('rKwh').innerText=(rp.total_kwh||0).toFixed(6);
}

function poll(){
  if(done)return;
  fetch('/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
    if(d.session_terminated&&d.report){showReceipt(d.report);return}

    // Timer: Server-Wert ist die EINZIGE Wahrheit (on-the-fly berechnet)
    document.getElementById('timer').innerText=fs(d.elapsed_seconds||0);

    // Status-Badge
    var pill=document.getElementById('sPill'), txt=document.getElementById('sTxt');
    if(d.active){pill.className='pill pill-on';txt.innerText='Aktiv'}
    else if(d.paused){pill.className='pill pill-p';txt.innerText='Pause'}
    else{pill.className='pill pill-off';txt.innerText='Bereit'}

    // Messwerte direkt vom Server
    document.getElementById('volt').innerText=(d.voltage||230).toFixed(1);
    document.getElementById('amp').innerText=(d.current_ampere||0).toFixed(3);
    document.getElementById('ma').innerText=((d.current_ampere||0)*1000).toFixed(0);
    document.getElementById('watt').innerText=(d.watt||0).toFixed(3);
    document.getElementById('wh').innerText=(d.wh||0).toFixed(4);
    document.getElementById('mwh').innerText=((d.wh||0)*1000).toFixed(1);
    document.getElementById('cost').innerText=(d.cost||0).toFixed(5);
    document.getElementById('cent').innerText=((d.cost||0)*100).toFixed(3);
    document.getElementById('wSub').innerText=(d.watt>0.1)?'Strom fliesst':'Kein Strom';

    // KI
    var ai=d.ai_result||{};
    document.getElementById('aiI').innerText=ai.icon||'🔌';
    document.getElementById('aiN').innerText=ai.name||'Erkenne...';
    document.getElementById('aiS').innerText=ai.stage||'Analyse';
    var c=ai.confidence||0;
    document.getElementById('aiC').innerText=c>0?'Sicherheit: '+c+'%':'Warte...';
    document.getElementById('aiSub').innerText=ai.is_battery===true?'Akku | Spitze: '+(ai.peak_w||0)+'W':ai.is_battery===false?'Dauerbetrieb | Avg '+(ai.avg_w||0)+'W':'KI sammelt Daten...';
    if(ai.is_battery===true&&ai.soc_pct>0){document.getElementById('socSec').style.display='block';document.getElementById('socF').style.width=Math.min(100,ai.soc_pct)+'%';document.getElementById('socP').innerText=ai.soc_pct+'%';document.getElementById('socE').innerText=ai.stage||''}
    else{document.getElementById('socSec').style.display='none'}
  }).catch(function(){});
}

function sendEm(){
  var em=document.getElementById('emIn').value.trim(),fb=document.getElementById('emFb');
  if(em.indexOf('@')<0){fb.style.display='block';fb.style.color='#dc2626';fb.innerText='Gueltige E-Mail!';return}
  fb.style.display='block';fb.style.color='var(--blue)';fb.innerText='Sende...';
  post('/send_email_invoice',{email:em,report:lastR}).then(function(r){fb.style.color=r.status==='ok'?'#059669':'#d97706';fb.innerText=r.status==='ok'?'Gesendet!':r.message||'Fehler.'});
}

// Polling: alle 1 Sekunde
setInterval(poll,1000);
poll();
</script></body></html>"""


ADMIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:#090d16;color:#f8fafc;padding:24px 16px}
.wrap{max-width:1020px;margin:auto}
.hdr{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1e293b;padding-bottom:16px;margin-bottom:24px}
.brand{font-size:22px;font-weight:800}
.badge{background:#1e3a8a;color:#93c5fd;font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:8px}
.g4{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:24px}
.kpi{background:#111827;border:1px solid #1e293b;border-radius:18px;padding:18px}
.kpi-l{font-size:11.5px;text-transform:uppercase;font-weight:700;color:#94a3b8}
.kpi-v{font-size:26px;font-weight:800;color:#fff;margin-top:6px}
.kpi-s{font-size:11.5px;color:#94a3b8;margin-top:4px}
.sec{background:#111827;border:1px solid #1e293b;border-radius:18px;padding:20px;margin-bottom:24px}
.sec-t{font-size:16px;font-weight:700;color:#fff;margin-bottom:14px;display:flex;justify-content:space-between}
.lb{background:#1f2937;border-radius:14px;padding:14px;display:flex;flex-wrap:wrap;gap:20px}
.li .ll{font-size:10.5px;text-transform:uppercase;color:#94a3b8;font-weight:700}
.li .lv{font-size:18px;font-weight:800;color:#fff;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#1f2937;color:#94a3b8;padding:10px;text-align:left;font-size:11px;text-transform:uppercase}
td{padding:10px;border-bottom:1px solid #1e293b}
.btn{padding:9px 14px;font-size:12.5px;font-weight:700;border-radius:10px;border:none;cursor:pointer}
.bon{background:#10b981;color:#fff}
.boff{background:#ef4444;color:#fff}
</style></head><body>
<div class="wrap">
<div class="hdr"><div><div class="brand">⚡ Admin Dashboard</div><div style="font-size:12px;color:#94a3b8;margin-top:4px">Station: <code>{{ physical_token }}</code></div></div><span class="badge">Admin</span></div>
<div class="g4">
<div class="kpi"><div class="kpi-l">Umsatz</div><div class="kpi-v" style="color:#10b981">{{ "%.5f"|format(today_revenue) }} EUR</div><div class="kpi-s">Gesamt: {{ "%.5f"|format(total_revenue) }} EUR</div></div>
<div class="kpi"><div class="kpi-l">Energie</div><div class="kpi-v" style="color:#3b82f6">{{ "%.2f"|format(today_wh) }} Wh</div><div class="kpi-s">Gesamt: {{ "%.4f"|format(total_kwh) }} kWh</div></div>
<div class="kpi"><div class="kpi-l">Sitzungen</div><div class="kpi-v">{{ today_sessions }}</div><div class="kpi-s">Gesamt: {{ total_sessions }}</div></div>
<div class="kpi"><div class="kpi-l">Live</div><div class="kpi-v" style="color:{% if live_active %}#10b981{% else %}#94a3b8{% endif %}">{% if live_active %}AKTIV{% else %}BEREIT{% endif %}</div><div class="kpi-s">Relais: {% if relay_on %}EIN{% else %}AUS{% endif %}</div></div>
</div>
<div class="sec">
<div class="sec-t"><span>Telemetrie</span><div style="display:flex;gap:8px"><button class="btn bon" onclick="ovr('force_on')">EIN</button><button class="btn boff" onclick="ovr('force_off')">AUS</button></div></div>
<div class="lb">
<div class="li"><div class="ll">Watt</div><div class="lv" style="color:#3b82f6">{{ "%.3f"|format(live_watt) }} W</div></div>
<div class="li"><div class="ll">Ampere</div><div class="lv">{{ "%.3f"|format(live_amp) }} A</div></div>
<div class="li"><div class="ll">Volt</div><div class="lv">{{ "%.1f"|format(live_volt) }} V</div></div>
<div class="li"><div class="ll">API</div><div class="lv" style="color:{% if last_poll_ok %}#10b981{% else %}#ef4444{% endif %}">{% if last_poll_ok %}OK{% else %}?{% endif %}</div></div>
</div></div>
<div class="sec">
<div class="sec-t">Sitzungen</div>
<table><thead><tr><th>Beleg</th><th>Datum</th><th>Geraet</th><th>Dauer</th><th>Wh</th><th>EUR</th></tr></thead>
<tbody>{% for r in history_records|reverse %}<tr><td><code>{{ r.invoice_id }}</code></td><td>{{ r.date }}</td><td>{% for d in r.devices %}{{ d.ai_icon|default('🔌') }} {{ d.ai_name|default('?') }}<br>{% endfor %}</td><td>{{ r.time_formatted }}</td><td>{{ "%.3f"|format(r.total_wh) }}</td><td><b style="color:#10b981">{{ "%.5f"|format(r.total_cost) }}</b></td></tr>{% else %}<tr><td colspan="6" style="text-align:center;color:#94a3b8">Keine.</td></tr>{% endfor %}</tbody></table>
</div></div>
<script>function ovr(a){fetch('/admin_api/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})}).then(function(r){return r.json()}).then(function(d){alert(d.message||'OK');location.reload()})}</script>
</body></html>"""


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)