from flask import Flask, render_template_string, jsonify, session, request, send_file, redirect, url_for
import requests as http_requests
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
STROMPREIS_PER_KWH = 0.35  # Netto-Arbeitspreis in € / kWh

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = ""
SMTP_PASSWORD = ""

# =====================================================================
# LAENDER- & MEHRWERTSTEUER-TABELLE (VAT / MwSt. fuer Strom)
# =====================================================================
COUNTRY_VAT_RATES = {
    "DE": {"code": "DE", "name": "Deutschland", "flag": "🇩🇪", "vat_name": "MwSt.", "rate": 19.0},
    "AT": {"code": "AT", "name": "Österreich", "flag": "🇦🇹", "vat_name": "USt.", "rate": 20.0},
    "CH": {"code": "CH", "name": "Schweiz", "flag": "🇨🇭", "vat_name": "MWST", "rate": 8.1},
    "FR": {"code": "FR", "name": "Frankreich", "flag": "🇫🇷", "vat_name": "TVA", "rate": 20.0},
    "IT": {"code": "IT", "name": "Italien", "flag": "🇮🇹", "vat_name": "IVA", "rate": 22.0},
    "ES": {"code": "ES", "name": "Spanien", "flag": "🇪🇸", "vat_name": "IVA", "rate": 21.0},
    "NL": {"code": "NL", "name": "Niederlande", "flag": "🇳🇱", "vat_name": "BTW", "rate": 21.0},
    "BE": {"code": "BE", "name": "Belgien", "flag": "🇧🇪", "vat_name": "TVA", "rate": 21.0},
    "LU": {"code": "LU", "name": "Luxemburg", "flag": "🇱🇺", "vat_name": "TVA", "rate": 17.0},
    "GB": {"code": "GB", "name": "Großbritannien", "flag": "🇬🇧", "vat_name": "VAT", "rate": 20.0},
    "PL": {"code": "PL", "name": "Polen", "flag": "🇵🇱", "vat_name": "VAT", "rate": 23.0},
    "DK": {"code": "DK", "name": "Dänemark", "flag": "🇩🇰", "vat_name": "Moms", "rate": 25.0},
    "SE": {"code": "SE", "name": "Schweden", "flag": "🇸🇪", "vat_name": "Moms", "rate": 25.0},
    "NO": {"code": "NO", "name": "Norwegen", "flag": "🇳🇴", "vat_name": "Mva", "rate": 25.0},
    "CUSTOM_0": {"code": "CUSTOM_0", "name": "Steuerfrei / B2B (0%)", "flag": "🌐", "vat_name": "VAT 0%", "rate": 0.0}
}

# =====================================================================
# GERAETE-PROFILE & MODI (Dauerbetrieb vs. Akku)
# =====================================================================
DEVICE_PROFILES = {
    # --- DAUERBETRIEB ---
    "lamp": {
        "key": "lamp",
        "name": "Lampe / Beleuchtung",
        "icon": "💡",
        "mode": "continuous",
        "is_battery": False,
        "nominal_wh": 0.0,
        "desc": "Dauerbetrieb (stetige Last)"
    },
    "tv": {
        "key": "tv",
        "name": "TV / Monitor / Audio",
        "icon": "📺",
        "mode": "continuous",
        "is_battery": False,
        "nominal_wh": 0.0,
        "desc": "Dauerbetrieb (Unterhaltungselektronik)"
    },
    "appliance_s": {
        "key": "appliance_s",
        "name": "Kleingerät / Router",
        "icon": "☕",
        "mode": "continuous",
        "is_battery": False,
        "nominal_wh": 0.0,
        "desc": "Dauerbetrieb (konstante Kleinlast)"
    },
    "appliance": {
        "key": "appliance",
        "name": "Großgerät / Dauerlast",
        "icon": "🍳",
        "mode": "continuous",
        "is_battery": False,
        "nominal_wh": 0.0,
        "desc": "Dauerbetrieb (starke Verbraucher)"
    },
    "continuous_custom": {
        "key": "continuous_custom",
        "name": "Individueller Dauerbetrieb",
        "icon": "🔌",
        "mode": "continuous",
        "is_battery": False,
        "nominal_wh": 0.0,
        "desc": "Dauerbetrieb ohne Akku"
    },

    # --- AKKU-GERAETE ---
    "phone": {
        "key": "phone",
        "name": "Smartphone / Tablet",
        "icon": "📱",
        "mode": "battery",
        "is_battery": True,
        "nominal_wh": 18.0,
        "desc": "Akku ca. 18 Wh"
    },
    "laptop": {
        "key": "laptop",
        "name": "Laptop / Ultrabook",
        "icon": "💻",
        "mode": "battery",
        "is_battery": True,
        "nominal_wh": 65.0,
        "desc": "Akku ca. 65 Wh"
    },
    "ebike": {
        "key": "ebike",
        "name": "E-Bike Akku (Standard)",
        "icon": "🚲",
        "mode": "battery",
        "is_battery": True,
        "nominal_wh": 500.0,
        "desc": "Akku ca. 500 Wh"
    },
    "ebike_fast": {
        "key": "ebike_fast",
        "name": "E-Bike Schnelllader",
        "icon": "⚡",
        "mode": "battery",
        "is_battery": True,
        "nominal_wh": 750.0,
        "desc": "Akku ca. 750 Wh"
    },
    "battery_custom": {
        "key": "battery_custom",
        "name": "Individueller Akku",
        "icon": "🔋",
        "mode": "battery",
        "is_battery": True,
        "nominal_wh": 80.0,
        "desc": "Akku ca. 80 Wh"
    }
}

# =====================================================================
# GLOBALER STATE
# =====================================================================
lock = threading.Lock()

shelly = {
    "watt": 0.0, "amp": 0.0, "volt": 230.0,
    "ok": False, "poll_time": 0.0, "error": ""
}

charge = {
    "active": False,
    "paused": False,
    "terminated": False,
    "relay_on": False,
    "selected_country": "DE",  # Standard: Deutschland (19% MwSt.)
    
    # Intelligente Stecker- & Gerätewechsel-State-Machine:
    # "IDLE" (Normal) | "SWAP_PENDING" (Altes Gerät steckt noch, zählt weiter) | "WAITING_FOR_PLUG" (Altes Gerät weg, warte auf Einstecken)
    "swap_state": "IDLE",
    "unplug_detected": False,
    "last_zero_amp_time": None,
    
    "accumulated_seconds": 0.0,
    "last_start_time": None,
    "last_wh_time": None,
    "total_wh": 0.0,
    "total_kwh": 0.0,
    "total_cost_netto": 0.0,
    "total_vat_amount": 0.0,
    "total_cost_brutto": 0.0,
    "power_history": [],
    "ai_result": None,
    "ai_tick": 0,
    "devices": [],
    "current_device_idx": 0,
    "last_report": None,
}

history_records = []
history_stats = {"sessions": 0, "kwh": 0.0, "revenue_brutto": 0.0}
HISTORY_FILE = "station_history.json"


# =====================================================================
# KI-GERAETEERKENNUNG & VORSCHLAEGE
# =====================================================================
class DeviceAI:
    @staticmethod
    def classify(power_history):
        if len(power_history) < 2:
            return {
                "suggested_key": "lamp",
                "name": "Erkennung läuft...",
                "icon": "💡",
                "mode": "continuous",
                "is_battery": False,
                "confidence": 0,
                "reason": "Sammle Leistungsdaten...",
                "peak_w": 0.0, "avg_w": 0.0, "current_w": 0.0
            }

        watts = [w for _, w in power_history]
        cw = watts[-1]
        valid_watts = [w for w in watts if w > 0.1]

        if not valid_watts:
            return {
                "suggested_key": "lamp",
                "name": "Kein Stromfluss",
                "icon": "🔌",
                "mode": "continuous",
                "is_battery": False,
                "confidence": 0,
                "reason": "Stecker abgezogen oder Gerät ausgeschaltet",
                "peak_w": 0.0, "avg_w": 0.0, "current_w": cw
            }

        pw = max(valid_watts)
        aw = sum(valid_watts) / len(valid_watts)
        n = len(valid_watts)

        # Trend- & Stabilitäts-Analyse
        if n >= 4:
            fh = sum(valid_watts[:n//2]) / max(1, n//2)
            sh = sum(valid_watts[n//2:]) / max(1, n - n//2)
            trend_ratio = sh / fh if fh > 0 else 1.0
            variance = sum((w - aw)**2 for w in valid_watts) / n
            cv = (variance**0.5) / aw if aw > 0 else 0
        else:
            trend_ratio = 1.0
            cv = 0.0

        # Klassifizierung
        if cv < 0.18 and trend_ratio > 0.88:
            confidence = min(95, 45 + n * 6)
            if pw < 20:
                s_key = "lamp"
                reason = f"Gleichmäßige Last ({aw:.1f} W) typisch für Beleuchtung"
            elif pw < 80:
                s_key = "tv"
                reason = f"Konstante mittlere Last ({aw:.1f} W) typisch für Monitor/TV"
            elif pw < 250:
                s_key = "appliance_s"
                reason = f"Dauerhafte mittlere Leistung ({aw:.1f} W)"
            else:
                s_key = "appliance"
                reason = f"Hohe Dauerlast ({aw:.1f} W)"
        elif trend_ratio < 0.88 or (pw < 120 and cw < pw * 0.75 and n >= 6):
            confidence = min(92, 40 + n * 6)
            if pw < 25:
                s_key = "phone"
                reason = f"Ladekurve bis {pw:.1f} W typisch für Smartphone/Tablet"
            elif pw < 100:
                s_key = "laptop"
                reason = f"Ladekurve bis {pw:.1f} W typisch für Laptop"
            elif pw < 350:
                s_key = "ebike"
                reason = f"Starke Ladeleistung ({pw:.1f} W) typisch für E-Bike"
            else:
                s_key = "ebike_fast"
                reason = f"Sehr hohe Ladeleistung ({pw:.1f} W)"
        else:
            confidence = 30
            if pw < 30:
                s_key = "lamp"
                reason = f"Aktuelle Leistung {aw:.1f} W"
            elif pw < 100:
                s_key = "tv"
                reason = f"Aktuelle Leistung {aw:.1f} W"
            else:
                s_key = "appliance"
                reason = f"Aktuelle Leistung {aw:.1f} W"

        prof = DEVICE_PROFILES.get(s_key, DEVICE_PROFILES["lamp"])
        return {
            "suggested_key": s_key,
            "name": prof["name"],
            "icon": prof["icon"],
            "mode": prof["mode"],
            "is_battery": prof["is_battery"],
            "nominal_wh": prof["nominal_wh"],
            "confidence": confidence,
            "reason": reason,
            "peak_w": round(pw, 2),
            "avg_w": round(aw, 2),
            "current_w": round(cw, 2)
        }


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
    now = time.time()
    if now < shelly["poll_time"] + 2.0:
        return

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
                shelly["error"] = j.get("error", "isok=false")
        elif r.status_code == 429:
            shelly["error"] = "Rate Limit (429)"
            shelly["poll_time"] = now + 4.0
            return
        else:
            shelly["error"] = f"HTTP {r.status_code}"
    except http_requests.exceptions.Timeout:
        shelly["error"] = "Timeout"
    except Exception as e:
        shelly["error"] = str(e)[:80]

    shelly["poll_time"] = now


def relay_control(turn_on):
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
# KERNFUNKTIONEN
# =====================================================================
def get_elapsed():
    if charge["active"] and charge["last_start_time"]:
        return charge["accumulated_seconds"] + (time.time() - charge["last_start_time"])
    return charge["accumulated_seconds"]


def new_device_entry(idx, key="lamp"):
    prof = DEVICE_PROFILES.get(key, DEVICE_PROFILES["lamp"])
    return {
        "id": idx,
        "key": prof["key"],
        "name": f"Gerät #{idx}: {prof['name']}" if idx > 1 else prof["name"],
        "icon": prof["icon"],
        "mode": prof["mode"],
        "is_battery": prof["is_battery"],
        "nominal_wh": prof["nominal_wh"],
        "user_confirmed": False,
        "duration_sec": 0.0,
        "wh": 0.0,
        "cost_netto": 0.0,
        "vat_amount": 0.0,
        "cost_brutto": 0.0,
        "cost": 0.0,
        "peak_w": 0.0
    }


def accumulate_energy():
    if not charge["active"]:
        return

    now = time.time()
    last = charge.get("last_wh_time")

    c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
    vat_rate = c_info["rate"]

    w = shelly["watt"]
    a = shelly["amp"]
    is_flowing = (a > 0.025 or w > 0.4)
    charge["unplug_detected"] = (not is_flowing)

    # =====================================================================
    # INTELLIGENTE STECKER- & WECHSEL-LOGIK (Smart Swap State Machine)
    # =====================================================================
    if charge["swap_state"] == "SWAP_PENDING":
        # Fall 1: Nutzer hat 'Gerät wechseln' geklickt, altes Gerät zieht aber noch Strom
        # -> Strom wird solange sauber auf dem alten Gerät weitergezählt bis Stecker gezogen wird!
        if not is_flowing:
            charge["swap_state"] = "WAITING_FOR_PLUG"
            logger.info("⚡ Altes Gerät abgesteckt -> Wechsle in WAITING_FOR_PLUG (Warte auf neues Gerät)")

    elif charge["swap_state"] == "WAITING_FOR_PLUG":
        # Fall 2: Altes Gerät ist abgesteckt, Relais bleibt aktiv -> Sobald Strom fließt, startet Gerät #N
        if is_flowing:
            idx = len(charge["devices"]) + 1
            charge["devices"].append(new_device_entry(idx, "lamp"))
            charge["current_device_idx"] = len(charge["devices"]) - 1
            charge["power_history"] = []
            charge["ai_result"] = None
            charge["ai_tick"] = 0
            charge["swap_state"] = "IDLE"
            logger.info(f"⚡ Neues Gerät eingesteckt! Gerät #{idx} automatisch aktiviert.")

    elif charge["swap_state"] == "IDLE":
        # Fall 3: Automatische Erkennung eines Gerätewechsels im laufenden Betrieb
        # Wenn der Stecker für mehr als 4 Sekunden gezogen war (>4s Nullstrom) und jetzt ein neues Gerät ansteckt:
        if not is_flowing:
            if charge["last_zero_amp_time"] is None:
                charge["last_zero_amp_time"] = now
        else:
            if charge["last_zero_amp_time"] is not None:
                zero_dur = now - charge["last_zero_amp_time"]
                # Wenn das vorherige Gerät bereits nennenswerte Energie hatte (> 0.05 Wh) und Strom > 4s weg war:
                cur_d = charge["devices"][charge["current_device_idx"]] if charge["devices"] else None
                if zero_dur > 4.0 and cur_d and cur_d.get("wh", 0) > 0.05:
                    idx = len(charge["devices"]) + 1
                    charge["devices"].append(new_device_entry(idx, "lamp"))
                    charge["current_device_idx"] = len(charge["devices"]) - 1
                    charge["power_history"] = []
                    charge["ai_result"] = None
                    charge["ai_tick"] = 0
                    logger.info(f"⚡ Automatischer Gerätewechsel nach {zero_dur:.1f}s Steckerpause -> Gerät #{idx} angelegt.")
                charge["last_zero_amp_time"] = None

    # =====================================================================
    # ENERGIE- & KOSTEN-AKKUMULATION
    # =====================================================================
    if last and last > 0:
        dt = now - last
        if 0.2 < dt < 15.0:
            delta_wh = (w * dt) / 3600.0 if is_flowing else 0.0

            charge["total_wh"] += delta_wh
            charge["total_kwh"] = charge["total_wh"] / 1000.0
            
            charge["total_cost_netto"] = charge["total_kwh"] * STROMPREIS_PER_KWH
            charge["total_vat_amount"] = charge["total_cost_netto"] * (vat_rate / 100.0)
            charge["total_cost_brutto"] = charge["total_cost_netto"] + charge["total_vat_amount"]

            charge["power_history"].append((now, w))
            if len(charge["power_history"]) > 120:
                charge["power_history"] = charge["power_history"][-120:]

            idx = charge["current_device_idx"]
            devs = charge["devices"]
            if 0 <= idx < len(devs):
                d = devs[idx]
                d["duration_sec"] = d.get("duration_sec", 0) + dt
                d["wh"] = d.get("wh", 0) + delta_wh
                d["cost_netto"] = (d["wh"] / 1000.0) * STROMPREIS_PER_KWH
                d["vat_amount"] = d["cost_netto"] * (vat_rate / 100.0)
                d["cost_brutto"] = d["cost_netto"] + d["vat_amount"]
                d["cost"] = d["cost_brutto"]
                d["peak_w"] = max(d.get("peak_w", 0), w)

    charge["last_wh_time"] = now

    # KI-Klassifizierung alle 3 Status-Aufrufe
    charge["ai_tick"] = charge.get("ai_tick", 0) + 1
    if charge["ai_tick"] % 3 == 0 or charge["ai_result"] is None:
        ai = DeviceAI.classify(charge["power_history"])
        charge["ai_result"] = ai

        idx = charge["current_device_idx"]
        devs = charge["devices"]
        if 0 <= idx < len(devs):
            cur_dev = devs[idx]
            if not cur_dev.get("user_confirmed", False) and ai.get("suggested_key") in DEVICE_PROFILES:
                s_prof = DEVICE_PROFILES[ai["suggested_key"]]
                cur_dev["key"] = s_prof["key"]
                base_nm = s_prof["name"]
                cur_dev["name"] = f"Gerät #{idx+1}: {base_nm}" if idx > 0 else base_nm
                cur_dev["icon"] = s_prof["icon"]
                cur_dev["mode"] = s_prof["mode"]
                cur_dev["is_battery"] = s_prof["is_battery"]
                cur_dev["nominal_wh"] = s_prof["nominal_wh"]


def fmt_time(s):
    s = int(max(0, s))
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


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
        with lock:
            charge["terminated"] = False
            charge["last_report"] = None
            charge["active"] = False
            charge["paused"] = False
            charge["swap_state"] = "IDLE"
            charge["unplug_detected"] = False
            charge["last_zero_amp_time"] = None
            charge["accumulated_seconds"] = 0.0
            charge["last_start_time"] = None
            charge["last_wh_time"] = None
            charge["total_wh"] = 0.0
            charge["total_kwh"] = 0.0
            charge["total_cost_netto"] = 0.0
            charge["total_vat_amount"] = 0.0
            charge["total_cost_brutto"] = 0.0
            charge["power_history"] = []
            charge["ai_result"] = None
            charge["ai_tick"] = 0
            charge["devices"] = [new_device_entry(1, "lamp")]
            charge["current_device_idx"] = 0
        relay_control(False)
        return redirect(url_for('index'))
    return "Ungültiger Token.", 403

@app.route('/reset_session', methods=['POST', 'GET'])
def reset_session():
    with lock:
        charge["terminated"] = False
        charge["last_report"] = None
        charge["active"] = False
        charge["paused"] = False
        charge["swap_state"] = "IDLE"
        charge["unplug_detected"] = False
        charge["last_zero_amp_time"] = None
        charge["accumulated_seconds"] = 0.0
        charge["last_start_time"] = None
        charge["last_wh_time"] = None
        charge["total_wh"] = 0.0
        charge["total_kwh"] = 0.0
        charge["total_cost_netto"] = 0.0
        charge["total_vat_amount"] = 0.0
        charge["total_cost_brutto"] = 0.0
        charge["power_history"] = []
        charge["ai_result"] = None
        charge["ai_tick"] = 0
        charge["devices"] = [new_device_entry(1, "lamp")]
        charge["current_device_idx"] = 0
    relay_control(False)
    logger.info(">>> SITZUNG ZURÜCKGESETZT / BEREIT FÜR NEUEN LADEVORGANG <<<")
    return jsonify({"status": "ok"})

@app.route('/status')
def get_status():
    poll_shelly()

    with lock:
        accumulate_energy()
        elapsed = get_elapsed()
        curr_idx = charge["current_device_idx"]
        devices = [dict(d) for d in charge["devices"]]
        active_dev = devices[curr_idx] if 0 <= curr_idx < len(devices) else new_device_entry(1)

        c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
        vat_rate = c_info["rate"]

        netto = charge["total_cost_netto"]
        vat_amt = charge["total_vat_amount"]
        brutto = charge["total_cost_brutto"]

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
            
            # Stecker- & Wechsel-Status
            "swap_state": charge["swap_state"],
            "unplug_detected": charge["unplug_detected"],
            
            # Mehrwertsteuer & Beträge
            "country": c_info,
            "vat_rate": vat_rate,
            "vat_name": c_info["vat_name"],
            "cost_netto": round(netto, 5),
            "vat_amount": round(vat_amt, 5),
            "cost_brutto": round(brutto, 5),
            "cost": round(brutto, 5),
            
            "ai_result": charge["ai_result"] or {},
            "active_device": active_dev,
            "devices": devices,
            "current_device_idx": curr_idx,
            "session_terminated": charge["terminated"],
            "report": charge["last_report"] if charge["terminated"] else None
        })

@app.route('/start', methods=['POST', 'GET'])
def start_charge():
    with lock:
        if charge["terminated"]:
            charge["terminated"] = False
            charge["last_report"] = None
            charge["accumulated_seconds"] = 0.0
            charge["last_start_time"] = None
            charge["last_wh_time"] = None
            charge["total_wh"] = 0.0
            charge["total_kwh"] = 0.0
            charge["total_cost_netto"] = 0.0
            charge["total_vat_amount"] = 0.0
            charge["total_cost_brutto"] = 0.0
            charge["power_history"] = []
            charge["ai_result"] = None
            charge["ai_tick"] = 0
            charge["devices"] = [new_device_entry(1, "lamp")]
            charge["current_device_idx"] = 0

        if not charge["active"]:
            charge["active"] = True
            charge["paused"] = False
            charge["last_start_time"] = time.time()
            charge["last_wh_time"] = time.time()
            if not charge["devices"]:
                charge["devices"] = [new_device_entry(1, "lamp")]
                charge["current_device_idx"] = 0
            charge["power_history"] = []
            charge["ai_result"] = None
            charge["ai_tick"] = 0
            logger.info(f">>> START (t_acc={charge['accumulated_seconds']:.1f}s) <<<")

    relay_control(True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST', 'GET'])
def stop_charge():
    with lock:
        if charge["active"] and charge["last_start_time"]:
            charge["accumulated_seconds"] += time.time() - charge["last_start_time"]
            accumulate_energy()
        charge["active"] = False
        charge["paused"] = True
        charge["last_start_time"] = None
        charge["last_wh_time"] = None
    relay_control(False)
    logger.info(f">>> PAUSE (t_acc={charge['accumulated_seconds']:.1f}s) <<<")
    return jsonify({"status": "ok"})

# --- SMART SWAP ENDPOINTS ---
@app.route('/request_swap', methods=['POST'])
def request_swap():
    """Benutzer leitet Gerätewechsel ein.
    Wenn altes Gerät noch Strom zieht: SWAP_PENDING (Strom zählt weiter auf altem Gerät).
    Wenn bereits kein Strom fließt: WAITING_FOR_PLUG."""
    with lock:
        is_flowing = (shelly["amp"] > 0.025 or shelly["watt"] > 0.4)
        if is_flowing:
            charge["swap_state"] = "SWAP_PENDING"
            logger.info("Gerätewechsel eingeleitet: Altes Gerät zieht noch Strom -> SWAP_PENDING")
        else:
            charge["swap_state"] = "WAITING_FOR_PLUG"
            logger.info("Gerätewechsel eingeleitet: Kein Strom -> WAITING_FOR_PLUG")
        return jsonify({"status": "ok", "swap_state": charge["swap_state"]})

@app.route('/cancel_swap', methods=['POST'])
def cancel_swap():
    with lock:
        charge["swap_state"] = "IDLE"
        logger.info("Gerätewechsel abgebrochen -> IDLE")
        return jsonify({"status": "ok"})

@app.route('/force_new_device', methods=['POST'])
def force_new_device():
    """Erzwingt sofortiges Anlegen eines neuen Geräts."""
    with lock:
        idx = len(charge["devices"]) + 1
        charge["devices"].append(new_device_entry(idx, "lamp"))
        charge["current_device_idx"] = len(charge["devices"]) - 1
        charge["power_history"] = []
        charge["ai_result"] = None
        charge["ai_tick"] = 0
        charge["swap_state"] = "IDLE"
        logger.info(f"Manuelles Anlegen erzwungen: Gerät #{idx} gestartet.")
    return jsonify({"status": "ok", "current_device_idx": charge["current_device_idx"]})

@app.route('/set_country', methods=['POST'])
def set_country():
    data = request.get_json() or {}
    code = data.get("country_code", "DE")
    if code in COUNTRY_VAT_RATES:
        with lock:
            charge["selected_country"] = code
            c_info = COUNTRY_VAT_RATES[code]
            vat_rate = c_info["rate"]
            charge["total_vat_amount"] = charge["total_cost_netto"] * (vat_rate / 100.0)
            charge["total_cost_brutto"] = charge["total_cost_netto"] + charge["total_vat_amount"]
            for d in charge["devices"]:
                d["vat_amount"] = d.get("cost_netto", 0.0) * (vat_rate / 100.0)
                d["cost_brutto"] = d.get("cost_netto", 0.0) + d["vat_amount"]
                d["cost"] = d["cost_brutto"]
            logger.info(f"Land/MwSt angepasst: {code} ({c_info['name']} {c_info['rate']}%)")
            return jsonify({"status": "ok", "country": c_info})
    return jsonify({"status": "error", "message": "Land nicht gefunden"}), 400

@app.route('/set_device', methods=['POST'])
def set_device():
    data = request.get_json() or {}
    key = data.get("key")
    confirmed = data.get("confirmed", True)

    with lock:
        idx = charge["current_device_idx"]
        if 0 <= idx < len(charge["devices"]):
            dev = charge["devices"][idx]
            if key in DEVICE_PROFILES:
                prof = DEVICE_PROFILES[key]
                dev["key"] = prof["key"]
                base_nm = prof["name"]
                dev["name"] = f"Gerät #{idx+1}: {base_nm}" if idx > 0 else base_nm
                dev["icon"] = prof["icon"]
                dev["mode"] = prof["mode"]
                dev["is_battery"] = prof["is_battery"]
                dev["nominal_wh"] = prof["nominal_wh"]
                dev["user_confirmed"] = confirmed
                logger.info(f"Gerät #{idx+1} konfiguriert: {dev['name']} ({dev['mode']}) | Bestätigt: {confirmed}")
                return jsonify({"status": "ok", "device": dev})
    return jsonify({"status": "error", "message": "Gerät nicht gefunden"}), 400

@app.route('/logout', methods=['POST', 'GET'])
def logout():
    with lock:
        if charge["active"] and charge["last_start_time"]:
            charge["accumulated_seconds"] += time.time() - charge["last_start_time"]
            accumulate_energy()

        charge["active"] = False
        charge["paused"] = False
        charge["terminated"] = True
        charge["swap_state"] = "IDLE"
        charge["last_start_time"] = None
        charge["last_wh_time"] = None

        elapsed = charge["accumulated_seconds"]
        invoice_id = f"RE-{time.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

        c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
        vat_rate = c_info["rate"]

        netto = charge["total_cost_netto"]
        vat_amt = charge["total_vat_amount"]
        brutto = charge["total_cost_brutto"]

        report = {
            "invoice_id": invoice_id,
            "date": time.strftime('%d.%m.%Y %H:%M'),
            "total_seconds": elapsed,
            "time_formatted": fmt_time(elapsed),
            "total_wh": charge["total_wh"],
            "total_kwh": charge["total_kwh"],
            
            "country_code": c_info["code"],
            "country_name": c_info["name"],
            "country_flag": c_info["flag"],
            "vat_name": c_info["vat_name"],
            "vat_rate": vat_rate,
            "total_cost_netto": netto,
            "total_vat_amount": vat_amt,
            "total_cost_brutto": brutto,
            "total_cost": brutto,
            
            "devices": [dict(d) for d in charge["devices"]]
        }
        charge["last_report"] = report

        history_stats["sessions"] += 1
        history_stats["kwh"] += report["total_kwh"]
        history_stats["revenue_brutto"] += report["total_cost_brutto"]
        history_records.append(report)

    relay_control(False)
    save_history()
    logger.info(f"LOGOUT {invoice_id} | Netto: {netto:.5f}€ + MwSt({vat_rate}%): {vat_amt:.5f}€ = Brutto: {brutto:.5f}€")
    return jsonify(report)

@app.route('/download_invoice')
def download_invoice():
    report = charge.get("last_report") or {
        "invoice_id": "SAMPLE", "date": time.strftime('%d.%m.%Y %H:%M'),
        "total_seconds": 0, "time_formatted": "00:00:00",
        "total_wh": 0, "total_kwh": 0,
        "country_name": "Deutschland", "country_flag": "🇩🇪", "vat_name": "MwSt.", "vat_rate": 19.0,
        "total_cost_netto": 0.0, "total_vat_amount": 0.0, "total_cost_brutto": 0.0, "total_cost": 0.0,
        "devices": []
    }
    pdf = generate_pdf(report)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{report.get('invoice_id', 'Quittung')}.pdf")

def generate_pdf(report):
    buf = io.BytesIO()
    if REPORTLAB_AVAILABLE:
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()
        story = []
        
        c_name = report.get("country_name", "Deutschland")
        vat_name = report.get("vat_name", "MwSt.")
        vat_rate = report.get("vat_rate", 19.0)
        netto = report.get("total_cost_netto", 0.0)
        vat_amt = report.get("total_vat_amount", 0.0)
        brutto = report.get("total_cost_brutto", report.get("total_cost", 0.0))

        ts = ParagraphStyle('T', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor("#2563eb"))
        ms = ParagraphStyle('M', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor("#64748b"), spaceAfter=10)
        
        story.append(Paragraph("⚡ Smart Power Hub • Offizielle Stromquittung", ts))
        story.append(Paragraph(
            f"Beleg-Nr.: <b>{report.get('invoice_id')}</b> | Datum: {report.get('date')}<br/>"
            f"Steuerland: <b>{c_name}</b> | Arbeitspreis (Netto): {STROMPREIS_PER_KWH:.2f} €/kWh", ms))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563eb"), spaceAfter=14))

        tbl = [["Pos", "Gerät & Modus", "Dauer", "Energie", "Netto (€)", f"{vat_name} ({vat_rate:.1f}%)", "Brutto (€)"]]
        for i, d in enumerate(report.get("devices", []), 1):
            m_label = "Akku" if d.get("is_battery") else "Dauerbetrieb"
            d_netto = d.get("cost_netto", (d.get("wh", 0) / 1000.0) * STROMPREIS_PER_KWH)
            d_vat = d.get("vat_amount", d_netto * (vat_rate / 100.0))
            d_brutto = d.get("cost_brutto", d_netto + d_vat)
            tbl.append([
                str(i),
                f"{d.get('name','Gerät')} ({m_label})",
                fmt_time(d.get("duration_sec", 0)),
                f"{d.get('wh', 0):.3f} Wh",
                f"{d_netto:.5f} €",
                f"{d_vat:.5f} €",
                f"{d_brutto:.5f} €"
            ])
            
        t = Table(tbl, colWidths=[24, 150, 60, 75, 65, 75, 75])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5f9")),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8.5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
            ('LINEBELOW', (0,0), (-1,0), 1.5, colors.HexColor("#cbd5e1")),
            ('LINEBELOW', (0,1), (-1,-1), 0.5, colors.HexColor("#e2e8f0")),
        ]))
        story.append(t)
        story.append(Spacer(1, 14))

        sum_tbl = [
            ["Nettobetrag (Zwischensumme):", f"{netto:.5f} €"],
            [f"zzgl. {vat_name} ({vat_rate:.1f}% für {c_name}):", f"+ {vat_amt:.5f} €"],
            ["GESAMTBETRAG (Brutto inkl. Steuern):", f"{brutto:.5f} €"]
        ]
        st = Table(sum_tbl, colWidths=[360, 164])
        st.setStyle(TableStyle([
            ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor("#f0fdf4")),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0,-1), (-1,-1), colors.HexColor("#15803d")),
            ('FONTSIZE', (0,-1), (-1,-1), 11),
            ('LINEABOVE', (0,-1), (-1,-1), 1.5, colors.HexColor("#15803d")),
        ]))
        story.append(st)
        story.append(Spacer(1, 20))
        story.append(Paragraph("Vielen Dank für die Nutzung der Smart Power Hub Ladestation!", ms))
        doc.build(story)
    else:
        buf.write(b"PDF nicht verfuegbar.")
    buf.seek(0)
    return buf

@app.route('/send_email_invoice', methods=['POST'])
def send_email():
    data = request.get_json() or {}
    email_to = data.get("email")
    report = data.get("report") or charge.get("last_report") or {}
    if not email_to or "@" not in email_to:
        return jsonify({"status": "error", "message": "Ungültige E-Mail"})
    if not SMTP_USER:
        return jsonify({"status": "error", "message": "SMTP nicht konfiguriert."})
    try:
        pdf = generate_pdf(report)
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER; msg["To"] = email_to
        msg["Subject"] = f"Quittung {report.get('invoice_id', '')} ({report.get('total_cost_brutto', 0):.5f} €)"
        msg.attach(MIMEText(f"Gesamtbetrag (Brutto): {report.get('total_cost_brutto',0):.5f} EUR (inkl. {report.get('vat_rate',19)}% {report.get('vat_name','MwSt')})", "plain", "utf-8"))
        att = MIMEApplication(pdf.read(), _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename="Quittung.pdf")
        msg.attach(att)
        s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=8)
        s.starttls(); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg); s.quit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/debug')
def debug():
    elapsed = get_elapsed()
    c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
    return jsonify({
        "shelly": dict(shelly),
        "charge_active": charge["active"],
        "charge_paused": charge["paused"],
        "swap_state": charge["swap_state"],
        "unplug_detected": charge["unplug_detected"],
        "country": c_info,
        "accumulated_seconds": charge["accumulated_seconds"],
        "elapsed_calculated": round(elapsed, 1),
        "total_wh": charge["total_wh"],
        "cost_netto": charge["total_cost_netto"],
        "vat_amount": charge["total_vat_amount"],
        "cost_brutto": charge["total_cost_brutto"],
        "devices": charge["devices"],
        "ai_result": charge["ai_result"],
        "server_time": time.time()
    })

@app.route(f'/admin/{ADMIN_SECRET_TOKEN}')
def admin():
    today = time.strftime('%d.%m.%Y')
    today_recs = [r for r in history_records if r.get("date", "").startswith(today)]
    return render_template_string(ADMIN_HTML,
        physical_token=PHYSICAL_STATION_TOKEN, device_id=DEVICE_ID,
        today_revenue=sum(r.get("total_cost_brutto", r.get("total_cost", 0)) for r in today_recs),
        total_revenue=history_stats["revenue_brutto"],
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
# HTML UI TEMPLATES
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
<h1>Vor-Ort-Sicherheitsprüfung</h1>
<p>Scanne den QR-Code an der Ladestation.</p>
<div class="tbox"><div class="tlbl">Token:</div><div class="tcode">{{ required_token }}</div></div>
<a href="/scan/{{ required_token }}" class="btn">📲 Station freischalten</a>
<a href="/admin/{{ admin_token }}" class="btn btn2" style="margin-top:12px;text-decoration:none">⚙️ Admin</a>
</div></body></html>"""


MAIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Smart Power Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
:root{
  --bg:#f8fafc;--card:#ffffff;--text:#090d16;--muted:#64748b;
  --blue:#2563eb;--green:#059669;--amber:#d97706;--red:#dc2626;
  --border:#e2e8f0;--indigo:#4f46e5;--emerald:#10b981;
}
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:0}
body{background:var(--bg);color:var(--text);display:flex;justify-content:center;padding:18px 12px;min-height:100vh}
.wrap{width:100%;max-width:440px}
.card{background:var(--card);border-radius:24px;padding:22px 18px;box-shadow:0 12px 30px -6px rgba(15,23,42,.08);border:1px solid var(--border);position:relative;overflow:hidden}

.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.brand{font-size:18px;font-weight:800;letter-spacing:-.3px}
.country-badge{background:#eff6ff;border:1px solid #bfdbfe;color:#1e40af;font-size:11.5px;padding:4px 10px;border-radius:20px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;transition:background .15s}
.country-badge:hover{background:#dbeafe}

.badges{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;padding:5px 13px;border-radius:30px}
.pill-g{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.pill-off{background:#f1f5f9;color:var(--muted);border:1px solid var(--border)}
.pill-on{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
.pill-p{background:#fffbeb;color:#92400e;border:1px solid #fde68a}
.pill-warn{background:#fff7ed;color:#c2410c;border:1px solid #fed7aa}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}
.pill-on .dot{background:#059669;box-shadow:0 0 8px rgba(5,150,105,.7)}
.pill-p .dot{background:#d97706}
.pill-off .dot{background:#94a3b8}

/* SMART WECHSEL- & STECKER-BANNER */
.swap-banner{border-radius:16px;padding:12px 14px;margin-bottom:12px;text-align:left;animation:fadein .2s ease-out}
.swap-pending{background:#fffbeb;border:1.5px solid #fde68a;color:#92400e}
.swap-waiting{background:#eff6ff;border:1.5px solid #bfdbfe;color:#1e40af;box-shadow:0 0 12px rgba(37,99,235,.15)}
.swap-title{font-size:12.5px;font-weight:800;display:flex;align-items:center;gap:6px;margin-bottom:4px}
.swap-text{font-size:11px;line-height:1.4;margin-bottom:8px}
.swap-btns{display:flex;gap:6px}
.swap-btn-sm{padding:6px 10px;font-size:11.5px;font-weight:700;border-radius:8px;border:none;cursor:pointer}

/* KI-VORSCHLAG & GERAETE-AUSWAHL */
.ai-box{background:#f8fafc;border:1.5px solid var(--border);border-radius:18px;padding:14px;margin-bottom:12px;text-align:left;transition:border-color .2s}
.ai-box.confirmed{border-color:#bbf7d0;background:#f0fdf4}
.ai-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.ai-tag{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;padding:3px 8px;border-radius:6px;background:#eff6ff;color:var(--blue)}
.ai-box.confirmed .ai-tag{background:#dcfce7;color:#15803d}
.ai-conf{font-size:11px;color:var(--muted);font-weight:600}

.ai-main{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.ai-ico{font-size:32px;line-height:1}
.ai-title{font-size:15px;font-weight:800;color:var(--text)}
.ai-mode-badge{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:10px;margin-top:2px}
.badge-cont{background:#e0e7ff;color:#3730a3}
.badge-batt{background:#d1fae5;color:#065f46}

.ai-reason{font-size:11.5px;color:var(--muted);margin-bottom:10px;line-height:1.4}
.ai-actions{display:flex;gap:8px}
.btn-confirm{flex:1;background:#059669;color:#fff;border:none;border-radius:10px;padding:8px 12px;font-size:12.5px;font-weight:700;cursor:pointer}
.btn-change{background:#f1f5f9;color:var(--text);border:1px solid var(--border);border-radius:10px;padding:8px 12px;font-size:12.5px;font-weight:700;cursor:pointer}

/* WYSIWYG ADAPTIVES PROGNOSE-PANEL */
.prognosis-panel{border-radius:18px;padding:14px;margin-bottom:12px;text-align:left;animation:fadein .25s ease-out}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

.panel-continuous{background:#f8fafc;border:1.5px solid #cbd5e1}
.panel-continuous .prog-head{color:#334155;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.panel-continuous .prog-val-big{font-size:22px;font-weight:800;color:#1e293b;font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace}
.panel-continuous .prog-sub{font-size:11.5px;color:#64748b;margin-top:4px}
.prog-grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.prog-chip{background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:8px 10px}
.prog-chip .lbl{font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase}
.prog-chip .val{font-size:14px;font-weight:800;color:#0f172a;margin-top:2px;font-family:ui-monospace,monospace}

.panel-battery{background:#ecfdf5;border:1.5px solid #a7f3d0}
.panel-battery .prog-head{color:#065f46;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.soc-track{background:#d1fae5;height:9px;border-radius:10px;overflow:hidden;margin:8px 0 4px}
.soc-bar{background:linear-gradient(90deg,#059669,#10b981);height:100%;border-radius:10px;transition:width .5s ease}
.soc-labels{display:flex;justify-content:space-between;font-size:11px;font-weight:700;color:#065f46}

/* TELEMETRIE-GRID */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.st{background:#f8fafc;border:1px solid var(--border);border-radius:16px;padding:12px;text-align:left}
.st-l{font-size:10.5px;font-weight:700;text-transform:uppercase;color:var(--muted);letter-spacing:.4px}
.st-v{font-size:19px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace}
.st-s{font-size:10.5px;color:var(--muted);margin-top:2px}
.bl .st-v{color:var(--blue)}
.gr .st-v{color:var(--green)}

/* MWST-INFOZEILE */
.tax-banner{background:#f8fafc;border:1px dashed #cbd5e1;border-radius:12px;padding:8px 12px;margin-bottom:10px;font-size:11px;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
.tax-banner b{color:var(--text)}

/* BUTTONS */
.btns{display:flex;flex-direction:column;gap:8px;margin-top:14px}
.btn{width:100%;padding:13px;font-size:14.5px;font-weight:700;border:none;border-radius:14px;cursor:pointer;transition:transform .1s}
.btn:active{transform:scale(.98)}
.bp{background:#0f172a;color:#fff}
.bs{background:#f1f5f9;color:var(--text);border:1px solid var(--border)}
.bd{background:#fee2e2;color:var(--red)}

/* MODALE */
.modal{display:none;position:fixed;inset:0;background:rgba(9,13,22,.75);backdrop-filter:blur(5px);z-index:999;padding:16px;align-items:center;justify-content:center}
.mbox{background:#fff;border-radius:24px;padding:22px 18px;text-align:left;max-width:390px;width:100%;max-height:90vh;overflow-y:auto;animation:pop .2s ease-out}
@keyframes pop{from{transform:scale(.9);opacity:0}to{transform:scale(1);opacity:1}}
.m-sec-title{font-size:11px;font-weight:800;text-transform:uppercase;color:var(--muted);letter-spacing:.5px;margin:12px 0 6px}
.dev-option{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border:1px solid var(--border);border-radius:12px;margin-bottom:6px;cursor:pointer;background:#ffffff;transition:background .15s, border-color .15s}
.dev-option:hover{background:#f8fafc;border-color:var(--blue)}
.dev-opt-left{display:flex;align-items:center;gap:10px}
.dev-opt-ico{font-size:22px}
.dev-opt-nm{font-size:13.5px;font-weight:700;color:var(--text)}
.dev-opt-sub{font-size:11px;color:var(--muted)}
.dev-opt-tag{font-size:10px;font-weight:700;padding:3px 7px;border-radius:6px}

.receipt{display:none}
.rtbl{width:100%;border-collapse:collapse;margin:14px 0;font-size:12px}
.rtbl th{background:#f1f5f9;padding:8px 6px;font-size:10.5px;text-transform:uppercase;color:var(--muted)}
.rtbl td{padding:8px 6px;border-bottom:1px solid var(--border)}
.tbox{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px;padding:14px;text-align:right;margin-top:14px}
.ein{width:100%;padding:11px;border:1px solid var(--border);border-radius:10px;font-size:13.5px;margin-bottom:8px}
</style></head><body>

<!-- MODAL: LAND & MWST WAHLEN -->
<div id="countryModal" class="modal">
<div class="mbox">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
  <div style="font-size:16px;font-weight:800">🌍 Land & MwSt. / VAT wählen</div>
  <button style="background:none;border:none;font-size:20px;cursor:pointer;color:var(--muted)" onclick="hideM('countryModal')">✕</button>
</div>
<p style="font-size:12px;color:var(--muted);margin-bottom:12px">Wähle dein Land für die korrekte steuerliche Ausweisung auf Quittungen.</p>

<div class="dev-option" onclick="chooseCountry('DE')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇩🇪</span><div><div class="dev-opt-nm">Deutschland</div><div class="dev-opt-sub">19.0% MwSt.</div></div></div>
  <span class="dev-opt-tag badge-cont">19%</span>
</div>
<div class="dev-option" onclick="chooseCountry('AT')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇦🇹</span><div><div class="dev-opt-nm">Österreich</div><div class="dev-opt-sub">20.0% USt.</div></div></div>
  <span class="dev-opt-tag badge-cont">20%</span>
</div>
<div class="dev-option" onclick="chooseCountry('CH')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇨🇭</span><div><div class="dev-opt-nm">Schweiz</div><div class="dev-opt-sub">8.1% MWST</div></div></div>
  <span class="dev-opt-tag badge-cont">8.1%</span>
</div>
<div class="dev-option" onclick="chooseCountry('FR')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇫🇷</span><div><div class="dev-opt-nm">Frankreich</div><div class="dev-opt-sub">20.0% TVA</div></div></div>
  <span class="dev-opt-tag badge-cont">20%</span>
</div>
<div class="dev-option" onclick="chooseCountry('IT')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇮🇹</span><div><div class="dev-opt-nm">Italien</div><div class="dev-opt-sub">22.0% IVA</div></div></div>
  <span class="dev-opt-tag badge-cont">22%</span>
</div>
<div class="dev-option" onclick="chooseCountry('ES')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇪🇸</span><div><div class="dev-opt-nm">Spanien</div><div class="dev-opt-sub">21.0% IVA</div></div></div>
  <span class="dev-opt-tag badge-cont">21%</span>
</div>
<div class="dev-option" onclick="chooseCountry('NL')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇳🇱</span><div><div class="dev-opt-nm">Niederlande</div><div class="dev-opt-sub">21.0% BTW</div></div></div>
  <span class="dev-opt-tag badge-cont">21%</span>
</div>
<div class="dev-option" onclick="chooseCountry('GB')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇬🇧</span><div><div class="dev-opt-nm">Großbritannien</div><div class="dev-opt-sub">20.0% VAT</div></div></div>
  <span class="dev-opt-tag badge-cont">20%</span>
</div>
<div class="dev-option" onclick="chooseCountry('PL')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🇵🇱</span><div><div class="dev-opt-nm">Polen</div><div class="dev-opt-sub">23.0% VAT</div></div></div>
  <span class="dev-opt-tag badge-cont">23%</span>
</div>
<div class="dev-option" onclick="chooseCountry('CUSTOM_0')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🌐</span><div><div class="dev-opt-nm">Steuerfrei / B2B</div><div class="dev-opt-sub">0.0% Steuer</div></div></div>
  <span class="dev-opt-tag badge-batt">0%</span>
</div>

<button class="btn bs" style="margin-top:12px;padding:10px" onclick="hideM('countryModal')">Schließen</button>
</div>
</div>

<!-- MODAL: GERAET WAHLEN -->
<div id="devModal" class="modal">
<div class="mbox">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
  <div style="font-size:16px;font-weight:800">⚡ Gerät / Profil wählen</div>
  <button style="background:none;border:none;font-size:20px;cursor:pointer;color:var(--muted)" onclick="hideM('devModal')">✕</button>
</div>

<div class="m-sec-title">🔌 Dauerbetrieb (ohne Akku)</div>
<div class="dev-option" onclick="chooseDevice('lamp')">
  <div class="dev-opt-left"><span class="dev-opt-ico">💡</span><div><div class="dev-opt-nm">Lampe / Beleuchtung</div><div class="dev-opt-sub">Stetige Last ca. 5–30 W</div></div></div>
  <span class="dev-opt-tag badge-cont">Dauerbetrieb</span>
</div>
<div class="dev-option" onclick="chooseDevice('tv')">
  <div class="dev-opt-left"><span class="dev-opt-ico">📺</span><div><div class="dev-opt-nm">TV / Monitor / Audio</div><div class="dev-opt-sub">Stetige Last ca. 30–120 W</div></div></div>
  <span class="dev-opt-tag badge-cont">Dauerbetrieb</span>
</div>
<div class="dev-option" onclick="chooseDevice('appliance_s')">
  <div class="dev-opt-left"><span class="dev-opt-ico">☕</span><div><div class="dev-opt-nm">Kleingerät / Router</div><div class="dev-opt-sub">Stetige Last ca. 10–250 W</div></div></div>
  <span class="dev-opt-tag badge-cont">Dauerbetrieb</span>
</div>
<div class="dev-option" onclick="chooseDevice('appliance')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🍳</span><div><div class="dev-opt-nm">Großgerät / Dauerlast</div><div class="dev-opt-sub">Hohe Last > 250 W</div></div></div>
  <span class="dev-opt-tag badge-cont">Dauerbetrieb</span>
</div>

<div class="m-sec-title">🔋 Akku-Geräte (Ladevorgang)</div>
<div class="dev-option" onclick="chooseDevice('phone')">
  <div class="dev-opt-left"><span class="dev-opt-ico">📱</span><div><div class="dev-opt-nm">Smartphone / Tablet</div><div class="dev-opt-sub">Akku ca. 18 Wh (5–25 W)</div></div></div>
  <span class="dev-opt-tag badge-batt">Akku</span>
</div>
<div class="dev-option" onclick="chooseDevice('laptop')">
  <div class="dev-opt-left"><span class="dev-opt-ico">💻</span><div><div class="dev-opt-nm">Laptop / Ultrabook</div><div class="dev-opt-sub">Akku ca. 65 Wh (30–90 W)</div></div></div>
  <span class="dev-opt-tag badge-batt">Akku</span>
</div>
<div class="dev-option" onclick="chooseDevice('ebike')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🚲</span><div><div class="dev-opt-nm">E-Bike Akku</div><div class="dev-opt-sub">Akku ca. 500 Wh (100–250 W)</div></div></div>
  <span class="dev-opt-tag badge-batt">Akku</span>
</div>
<div class="dev-option" onclick="chooseDevice('ebike_fast')">
  <div class="dev-opt-left"><span class="dev-opt-ico">⚡</span><div><div class="dev-opt-nm">E-Bike Schnelllader</div><div class="dev-opt-sub">Akku ca. 750 Wh (> 300 W)</div></div></div>
  <span class="dev-opt-tag badge-batt">Akku</span>
</div>
<div class="dev-option" onclick="chooseDevice('battery_custom')">
  <div class="dev-opt-left"><span class="dev-opt-ico">🔋</span><div><div class="dev-opt-nm">Sonstiger Akku</div><div class="dev-opt-sub">Akku ca. 80 Wh</div></div></div>
  <span class="dev-opt-tag badge-batt">Akku</span>
</div>

<button class="btn bs" style="margin-top:12px;padding:10px" onclick="hideM('devModal')">Abbrechen</button>
</div>
</div>

<!-- HAUPTANSICHT -->
<div class="wrap">
<div class="card" id="mainC">
<div class="hdr">
  <span class="brand">⚡ Smart Power Hub</span>
  <span class="country-badge" onclick="showM('countryModal')">
    <span id="hdrFlag">🇩🇪</span> <span id="hdrCountryText">DE · 19% MwSt.</span> ▾
  </span>
</div>

<div class="badges">
  <span class="pill pill-g">🔒 Verifiziert</span>
  <span class="pill pill-off" id="sPill"><span class="dot"></span><span id="sTxt">Bereit</span></span>
  <span class="pill pill-warn" id="unplugPill" style="display:none">⚠️ Stecker abgezogen (0.0 A)</span>
</div>

<!-- DYNAMISCHES GERÄTEWECHSEL-BANNER (State Machine) -->
<div id="swapBannerBox" style="display:none">
  <!-- 1. SWAP_PENDING: Altes Gerät zieht noch Strom -->
  <div id="swapPendingBanner" class="swap-banner swap-pending" style="display:none">
    <div class="swap-title">🔄 Gerätewechsel aktiv – Altes Gerät abstecken</div>
    <div class="swap-text">Das bisherige Gerät zieht aktuell noch Strom (<b id="swapCurWatt">0.0</b> W / <b id="swapCurAmp">0.00</b> A). Die Energie wird bis zum Abstecken weiter auf diesem Gerät gezählt.</div>
    <div class="swap-btns">
      <button class="swap-btn-sm" style="background:#d97706;color:#fff" onclick="forceNewDevice()">Sofort neues Gerät starten</button>
      <button class="swap-btn-sm" style="background:#ffffff;border:1px solid #cbd5e1" onclick="cancelSwap()">Abbrechen</button>
    </div>
  </div>

  <!-- 2. WAITING_FOR_PLUG: Altes Gerät entfernt, warte auf Einstecken des neuen Geräts -->
  <div id="swapWaitingBanner" class="swap-banner swap-waiting" style="display:none">
    <div class="swap-title">🔌 Altes Gerät abgesteckt ✓ – Neues Gerät einstecken</div>
    <div class="swap-text">Stecke jetzt das neue Gerät ein. Sobald Strom fließt, erkennt die Station das neue Gerät automatisch per Ampere-Messung!</div>
    <div class="swap-btns">
      <button class="swap-btn-sm" style="background:#2563eb;color:#fff" onclick="showM('devModal')">Gerät manuell wählen</button>
      <button class="swap-btn-sm" style="background:#fee2e2;color:#dc2626" onclick="devAct('finish')">Sitzung abschließen</button>
    </div>
  </div>
</div>

<!-- KI-VORSCHLAG & GERAETE-LEISTE -->
<div class="ai-box" id="aiBox">
  <div class="ai-top">
    <span class="ai-tag" id="aiTag">⚡ KI-Erkennung</span>
    <span class="ai-conf" id="aiConf">Sammle Daten...</span>
  </div>
  <div class="ai-main">
    <div class="ai-ico" id="aiIco">💡</div>
    <div>
      <div class="ai-title" id="aiTitle">Lampe / Beleuchtung</div>
      <span class="ai-mode-badge badge-cont" id="aiModeBadge">Dauerbetrieb</span>
    </div>
  </div>
  <div class="ai-reason" id="aiReason">Analyse des Stromflusses läuft...</div>
  <div class="ai-actions">
    <button class="btn-confirm" id="btnConfirm" onclick="confirmSuggestion()">✅ Bestätigen</button>
    <button class="btn-change" onclick="showM('devModal')">✏️ Ändern</button>
  </div>
</div>

<!-- WYSIWYG ADAPTIVES PROGNOSE-PANEL -->
<div id="prognosisContainer">
  <!-- 1. DAUERBETRIEB PROGNOSE -->
  <div id="panelContinuous" class="prognosis-panel panel-continuous">
    <div class="prog-head">🔌 24h Dauerbetrieb Prognose</div>
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <div class="prog-val-big"><span id="p24Wh">0.0</span> Wh <span style="font-size:14px;color:#64748b">/ 24h</span></div>
      <div style="font-size:18px;font-weight:800;color:#059669"><span id="p24Cost">0.00</span> € <span style="font-size:12px;color:#64748b">/ Tag</span></div>
    </div>
    <div class="prog-sub">Hochrechnung bei Dauerlast (<span id="p24Watt">0.0</span> W) inkl. <span class="vatRateDisplay">19.0</span>% <span class="vatNameDisplay">MwSt.</span></div>
    <div class="prog-grid2">
      <div class="prog-chip">
        <div class="lbl">Verbrauch / 24h</div>
        <div class="val" id="p24Kwh">0.000 kWh</div>
      </div>
      <div class="prog-chip">
        <div class="lbl">Kosten / 30 Tage (Brutto)</div>
        <div class="val" style="color:#2563eb" id="p30Cost">0.00 €</div>
      </div>
    </div>
  </div>

  <!-- 2. AKKU-LADEMODUS PROGNOSE -->
  <div id="panelBattery" class="prognosis-panel panel-battery" style="display:none">
    <div class="prog-head">🔋 Akku-Ladefortschritt & Restbedarf bis 100%</div>
    <div class="soc-track"><div class="soc-bar" id="socBar" style="width:10%"></div></div>
    <div class="soc-labels">
      <span>Ladestand: <b id="socPctText">10%</b></span>
      <span id="socEtaText">~ -- Min</span>
    </div>
    <div class="prog-grid2" style="margin-top:10px">
      <div class="prog-chip">
        <div class="lbl">Noch bis 100% voll</div>
        <div class="val" style="color:#059669" id="battWhNeeded">-- Wh</div>
      </div>
      <div class="prog-chip">
        <div class="lbl">Restkosten (Brutto)</div>
        <div class="val" id="battCostNeeded">-- €</div>
      </div>
    </div>
  </div>
</div>

<!-- TELEMETRIE-WERTE -->
<div class="g2">
  <div class="st"><div class="st-l">Spannung (U)</div><div class="st-v"><span id="volt">230.0</span> V</div></div>
  <div class="st"><div class="st-l">Strom (I)</div><div class="st-v"><span id="amp">0.000</span> A</div><div class="st-s"><span id="ma">0</span> mA</div></div>
</div>
<div class="g2">
  <div class="st bl"><div class="st-l">Wirkleistung (P)</div><div class="st-v"><span id="watt">0.000</span> W</div><div class="st-s" id="wSub">Kein Strom</div></div>
  <div class="st"><div class="st-l">Laufzeit</div><div class="st-v" id="timer">00:00:00</div></div>
</div>
<div class="g2">
  <div class="st bl"><div class="st-l">Verbrauch (Wh)</div><div class="st-v"><span id="wh">0.0000</span> Wh</div><div class="st-s"><span id="mwh">0.0</span> mWh</div></div>
  <div class="st gr"><div class="st-l">Gesamtbetrag (Brutto)</div><div class="st-v"><span id="costBrutto">0.00000</span> €</div><div class="st-s"><span id="costCent">0.000</span> Cent</div></div>
</div>

<!-- MWST-AUFSCHLUESSELUNG -->
<div class="tax-banner">
  <div>Netto: <b id="costNetto">0.00000 €</b></div>
  <div>+ <span class="vatRateDisplay">19.0</span>% <span class="vatNameDisplay">MwSt.</span>: <b id="vatAmount">0.00000 €</b></div>
</div>

<div class="btns">
  <button class="btn bp" id="btnMainStart" onclick="doStart()">▶️ Start / Fortsetzen</button>
  <button class="btn bs" onclick="doStop()">⏸️ Pause</button>
  <button class="btn bs" style="background:#eff6ff;color:var(--blue);border-color:#bfdbfe" onclick="initiateSwap()">🔄 Gerät wechseln (Smart Swap)</button>
  <button class="btn bd" onclick="devAct('finish')">🧾 Beenden & Quittung</button>
</div>
</div>

<!-- QUITTUNGS-ANSICHT -->
<div class="card receipt" id="recC" style="margin-top:0">
<div style="text-align:center;margin-bottom:14px">
  <div style="font-size:40px;margin-bottom:4px">🧾</div>
  <div style="font-size:20px;font-weight:800">Offizielle Stromquittung</div>
  <div style="font-size:12px;color:var(--muted);margin-top:4px">
    Land: <b id="rCountryName">Deutschland</b> (<span id="rVatRate">19.0</span>% <span id="rVatName">MwSt.</span>)
    <span style="color:var(--blue);cursor:pointer;font-weight:700;margin-left:6px" onclick="showM('countryModal')">✏️ Ändern</span>
  </div>
</div>

<table class="rtbl">
  <thead><tr><th>Pos / Gerät</th><th style="text-align:center">Dauer</th><th style="text-align:right">Wh</th><th style="text-align:right">Netto</th><th style="text-align:right">Brutto</th></tr></thead>
  <tbody id="recB"></tbody>
</table>

<div class="tbox">
  <div style="font-size:11.5px;color:#64748b;margin-bottom:3px">Zwischensumme (Netto): <b id="rNetto" style="color:#0f172a">0.00000 €</b></div>
  <div style="font-size:11.5px;color:#64748b;margin-bottom:6px">zzgl. <span id="rVatPct">19.0% MwSt.</span>: <b id="rVatAmt" style="color:#0f172a">+ 0.00000 €</b></div>
  <div style="font-size:11px;color:#166534;font-weight:800;text-transform:uppercase;border-top:1px dashed #86efac;padding-top:6px">GESAMTBETRAG (BRUTTO)</div>
  <div style="font-size:24px;font-weight:800;color:#15803d" id="rCost">0.00000 €</div>
  <div style="font-size:11px;color:#166534;margin-top:2px">Gesamtverbrauch: <span id="rWh">0</span> Wh (<span id="rKwh">0</span> kWh)</div>
</div>

<div style="margin-top:18px;background:#f8fafc;border:1px solid var(--border);border-radius:14px;padding:14px">
  <div style="font-size:12px;font-weight:700;margin-bottom:6px">📧 Quittung per E-Mail senden:</div>
  <input type="email" id="emIn" class="ein" placeholder="deine@email.de">
  <button class="btn bp" style="background:var(--blue);font-size:13.5px;padding:11px" onclick="sendEm()">Senden</button>
  <button class="btn bs" style="font-size:13px;padding:9px;margin-top:6px" onclick="window.open('/download_invoice','_blank')">📥 PDF-Quittung herunterladen</button>
  <div id="emFb" style="display:none;font-size:12px;font-weight:600;margin-top:8px"></div>
</div>

<button class="btn bp" style="background:#059669;margin-top:14px;padding:14px;font-size:15px;display:flex;align-items:center;justify-content:center;gap:8px" onclick="startFreshSession()">
  <span>⚡</span> <span>Neue Ladesitzung starten</span>
</button>
</div>
</div>

<script>
var done = false, lastR = null;
var localElapsed = 0;
var isChargingActive = false;
var localTimerInterval = null;

var STROMPREIS_NETTO = 0.35;
var currentCountry = { code: 'DE', name: 'Deutschland', flag: '🇩🇪', vat_name: 'MwSt.', rate: 19.0 };
var currentDevice = { mode: 'continuous', is_battery: false, nominal_wh: 0, user_confirmed: false };
var latestAiSuggestion = null;

function fs(s){
  s = Math.floor(Math.max(0, s));
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(sec).padStart(2,'0');
}

function updateTimerDisplay(){
  var el = document.getElementById('timer');
  if (el) el.innerText = fs(localElapsed);
}

function startLocalTimer(){
  if (localTimerInterval) return;
  localTimerInterval = setInterval(function(){
    if (isChargingActive && !done) {
      localElapsed += 1;
      updateTimerDisplay();
    }
  }, 1000);
}

function stopLocalTimer(){
  if (localTimerInterval) {
    clearInterval(localTimerInterval);
    localTimerInterval = null;
  }
}

function showM(id){ document.getElementById(id).style.display = 'flex'; }
function hideM(id){ document.getElementById(id).style.display = 'none'; }

function post(u,d){
  return fetch(u, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(d || {})
  }).then(function(r){return r.json()}).catch(function(){return {}});
}

function startFreshSession(){
  post('/reset_session').then(function(){
    done = false;
    lastR = null;
    localElapsed = 0;
    isChargingActive = false;
    stopLocalTimer();
    document.getElementById('recC').style.display = 'none';
    document.getElementById('mainC').style.display = 'block';
    document.getElementById('sPill').className = 'pill pill-off';
    document.getElementById('sTxt').innerText = 'Bereit';
    document.getElementById('timer').innerText = '00:00:00';
    document.getElementById('watt').innerText = '0.000';
    document.getElementById('wh').innerText = '0.0000';
    document.getElementById('costBrutto').innerText = '0.00000';
    document.getElementById('costNetto').innerText = '0.00000 €';
    document.getElementById('vatAmount').innerText = '0.00000 €';
    poll();
  });
}

function doStart(){
  if(done) return;
  // Sofortige Reaktion ohne Millisekunden-Ruckeln
  isChargingActive = true;
  startLocalTimer();
  document.getElementById('sPill').className = 'pill pill-on';
  document.getElementById('sTxt').innerText = 'Aktiv';
  post('/start').then(function(){ poll(); });
}

function doStop(){
  if(done) return;
  isChargingActive = false;
  stopLocalTimer();
  document.getElementById('sPill').className = 'pill pill-p';
  document.getElementById('sTxt').innerText = 'Pause';
  post('/stop').then(function(){ poll(); });
}

// SMART GERÄTEWECHSEL
function initiateSwap(){
  post('/request_swap').then(function(r){
    poll();
  });
}

function cancelSwap(){
  post('/cancel_swap').then(function(){
    poll();
  });
}

function forceNewDevice(){
  post('/force_new_device').then(function(){
    poll();
  });
}

function devAct(a){
  if(a === 'continue'){
    doStart();
  } else if(a === 'finish'){
    isChargingActive = false;
    stopLocalTimer();
    post('/logout').then(function(r){ showReceipt(r); });
  }
}

function chooseCountry(code){
  hideM('countryModal');
  post('/set_country', { country_code: code }).then(function(res){
    if(res.status === 'ok' && res.country){
      currentCountry = res.country;
      updateCountryDisplays();
    }
    if(done && lastR){
      post('/logout').then(function(newR){ showReceipt(newR); });
    } else {
      poll();
    }
  });
}

function chooseDevice(key){
  hideM('devModal');
  post('/set_device', { key: key, confirmed: true }).then(function(res){
    if(res.status === 'ok' && res.device){
      currentDevice = res.device;
      updateWysiwygLook(currentDevice, parseFloat(document.getElementById('watt').innerText) || 0, parseFloat(document.getElementById('wh').innerText) || 0);
    }
    poll();
  });
}

function confirmSuggestion(){
  if(!latestAiSuggestion) return;
  post('/set_device', { key: latestAiSuggestion.suggested_key, confirmed: true }).then(function(res){
    if(res.status === 'ok' && res.device){
      currentDevice = res.device;
      updateWysiwygLook(currentDevice, parseFloat(document.getElementById('watt').innerText) || 0, parseFloat(document.getElementById('wh').innerText) || 0);
    }
    poll();
  });
}

function updateCountryDisplays(){
  document.getElementById('hdrFlag').innerText = currentCountry.flag || '🇩🇪';
  document.getElementById('hdrCountryText').innerText = (currentCountry.code || 'DE') + ' · ' + (currentCountry.rate || 19) + '% ' + (currentCountry.vat_name || 'MwSt.');
  
  var vRates = document.querySelectorAll('.vatRateDisplay');
  vRates.forEach(function(el){ el.innerText = (currentCountry.rate || 19.0).toFixed(1); });

  var vNames = document.querySelectorAll('.vatNameDisplay');
  vNames.forEach(function(el){ el.innerText = currentCountry.vat_name || 'MwSt.'; });
}

function updateWysiwygLook(dev, curW, curWh){
  var isBatt = dev.is_battery || dev.mode === 'battery';
  var pCont = document.getElementById('panelContinuous');
  var pBatt = document.getElementById('panelBattery');
  var vatFactor = 1.0 + ((currentCountry.rate || 19.0) / 100.0);

  var aiBox = document.getElementById('aiBox');
  var aiTag = document.getElementById('aiTag');
  var btnConf = document.getElementById('btnConfirm');
  var aiIco = document.getElementById('aiIco');
  var aiTitle = document.getElementById('aiTitle');
  var aiModeBadge = document.getElementById('aiModeBadge');

  aiIco.innerText = dev.icon || '🔌';
  aiTitle.innerText = dev.name || 'Gerät';

  if(isBatt){
    aiModeBadge.className = 'ai-mode-badge badge-batt';
    aiModeBadge.innerText = '🔋 Akku-Lademodus';
  } else {
    aiModeBadge.className = 'ai-mode-badge badge-cont';
    aiModeBadge.innerText = '🔌 Dauerbetrieb';
  }

  if(dev.user_confirmed){
    aiBox.className = 'ai-box confirmed';
    aiTag.innerText = '✓ Bestätigt';
    btnConf.style.display = 'none';
  } else {
    aiBox.className = 'ai-box';
    aiTag.innerText = '⚡ KI-Vorschlag';
    btnConf.style.display = 'inline-block';
  }

  if(isBatt){
    pCont.style.display = 'none';
    pBatt.style.display = 'block';

    var nomWh = dev.nominal_wh || 65.0;
    var loadedWh = curWh || 0.0;
    var whNeeded = Math.max(0.0, nomWh - loadedWh);
    var socPct = Math.min(100, Math.max(5, Math.round((loadedWh / nomWh) * 100)));
    if(loadedWh >= nomWh) socPct = 100;

    var etaMin = curW > 0.5 ? Math.round((whNeeded / curW) * 60) : 0;
    var costNeededNetto = (whNeeded / 1000.0) * STROMPREIS_NETTO;
    var costNeededBrutto = costNeededNetto * vatFactor;

    document.getElementById('socBar').style.width = socPct + '%';
    document.getElementById('socPctText').innerText = socPct + '%';
    document.getElementById('socEtaText').innerText = curW > 0.5 ? ('Restzeit: ~' + etaMin + ' Min') : 'Warte auf Strom...';
    document.getElementById('battWhNeeded').innerText = whNeeded.toFixed(2) + ' Wh';
    document.getElementById('battCostNeeded').innerText = '+' + costNeededBrutto.toFixed(4) + ' €';
  } else {
    pBatt.style.display = 'none';
    pCont.style.display = 'block';

    var p24_wh = curW * 24.0;
    var p24_kwh = p24_wh / 1000.0;
    var p24_cost_netto = p24_kwh * STROMPREIS_NETTO;
    var p24_cost_brutto = p24_cost_netto * vatFactor;
    var p30_cost_brutto = p24_cost_brutto * 30.0;

    document.getElementById('p24Wh').innerText = p24_wh.toFixed(1);
    document.getElementById('p24Cost').innerText = p24_cost_brutto.toFixed(2);
    document.getElementById('p24Watt').innerText = curW.toFixed(1);
    document.getElementById('p24Kwh').innerText = p24_kwh.toFixed(3) + ' kWh';
    document.getElementById('p30Cost').innerText = p30_cost_brutto.toFixed(2) + ' €';
  }
}

function showReceipt(rp){
  done = true;
  isChargingActive = false;
  stopLocalTimer();
  lastR = rp;
  document.getElementById('mainC').style.display = 'none';
  document.getElementById('recC').style.display = 'block';

  var vatRate = rp.vat_rate || currentCountry.rate || 19.0;
  var vatName = rp.vat_name || currentCountry.vat_name || 'MwSt.';
  var cName = rp.country_name || currentCountry.name || 'Deutschland';
  var netto = rp.total_cost_netto || 0.0;
  var vatAmt = rp.total_vat_amount || 0.0;
  var brutto = rp.total_cost_brutto || rp.total_cost || 0.0;

  document.getElementById('rCountryName').innerText = cName;
  document.getElementById('rVatRate').innerText = vatRate.toFixed(1);
  document.getElementById('rVatName').innerText = vatName;

  var tb = document.getElementById('recB');
  tb.innerHTML = '';
  (rp.devices || []).forEach(function(d, idx){
    var m = d.is_battery ? 'Akku' : 'Dauerbetrieb';
    var dNetto = d.cost_netto || (d.wh / 1000.0) * STROMPREIS_NETTO;
    var dBrutto = d.cost_brutto || d.cost || (dNetto * (1 + vatRate/100));
    var tr = document.createElement('tr');
    tr.innerHTML = '<td><b>' + (d.icon || '🔌') + ' ' + (d.name || 'Gerät') + '</b><br><span style="font-size:10px;color:#64748b">' + m + '</span></td><td style="text-align:center">' + fs(d.duration_sec || 0) + '</td><td style="text-align:right">' + (d.wh || 0).toFixed(3) + '</td><td style="text-align:right">' + dNetto.toFixed(4) + ' €</td><td style="text-align:right"><b>' + dBrutto.toFixed(4) + ' €</b></td>';
    tb.appendChild(tr);
  });

  document.getElementById('rNetto').innerText = netto.toFixed(5) + ' €';
  document.getElementById('rVatPct').innerText = vatRate.toFixed(1) + '% ' + vatName;
  document.getElementById('rVatAmt').innerText = '+ ' + vatAmt.toFixed(5) + ' €';
  document.getElementById('rCost').innerText = brutto.toFixed(5) + ' €';
  document.getElementById('rWh').innerText = (rp.total_wh || 0).toFixed(4);
  document.getElementById('rKwh').innerText = (rp.total_kwh || 0).toFixed(6);
}

function poll(){
  if(done) return;
  fetch('/status', {cache: 'no-store'}).then(function(r){return r.json()}).then(function(d){
    if(d.session_terminated && d.report){
      showReceipt(d.report);
      return;
    }

    var srvSec = d.elapsed_seconds || 0;
    var curW = d.watt || 0.0;
    var curA = d.current_ampere || 0.0;
    var curWh = d.wh || 0.0;

    // Land
    if(d.country){
      currentCountry = d.country;
      updateCountryDisplays();
    }

    // Timer-Synchronisation ohne Ruckeln
    if(d.active){
      isChargingActive = true;
      startLocalTimer();
      document.getElementById('sPill').className = 'pill pill-on';
      document.getElementById('sTxt').innerText = 'Aktiv';
      if(Math.abs(localElapsed - srvSec) > 2.0){
        localElapsed = Math.floor(srvSec);
        updateTimerDisplay();
      }
    } else {
      isChargingActive = false;
      stopLocalTimer();
      localElapsed = Math.floor(srvSec);
      updateTimerDisplay();
      if(d.paused){
        document.getElementById('sPill').className = 'pill pill-p';
        document.getElementById('sTxt').innerText = 'Pause';
      } else {
        document.getElementById('sPill').className = 'pill pill-off';
        document.getElementById('sTxt').innerText = 'Bereit';
      }
    }

    // Stecker-Abzug Warnung
    var unplugPill = document.getElementById('unplugPill');
    if(d.unplug_detected && d.active && d.swap_state === 'IDLE'){
      unplugPill.style.display = 'inline-flex';
    } else {
      unplugPill.style.display = 'none';
    }

    // SMART SWAP BANNER
    var swapBox = document.getElementById('swapBannerBox');
    var pBanner = document.getElementById('swapPendingBanner');
    var wBanner = document.getElementById('swapWaitingBanner');
    
    if(d.swap_state === 'SWAP_PENDING'){
      swapBox.style.display = 'block';
      pBanner.style.display = 'block';
      wBanner.style.display = 'none';
      document.getElementById('swapCurWatt').innerText = curW.toFixed(1);
      document.getElementById('swapCurAmp').innerText = curA.toFixed(3);
    } else if(d.swap_state === 'WAITING_FOR_PLUG'){
      swapBox.style.display = 'block';
      pBanner.style.display = 'none';
      wBanner.style.display = 'block';
    } else {
      swapBox.style.display = 'none';
      pBanner.style.display = 'none';
      wBanner.style.display = 'none';
    }

    // Messwerte
    document.getElementById('volt').innerText = (d.voltage || 230).toFixed(1);
    document.getElementById('amp').innerText = curA.toFixed(3);
    document.getElementById('ma').innerText = (curA * 1000).toFixed(0);
    document.getElementById('watt').innerText = curW.toFixed(3);
    document.getElementById('wh').innerText = curWh.toFixed(4);
    document.getElementById('mwh').innerText = (curWh * 1000).toFixed(1);

    // Kosten (Netto, MwSt, Brutto)
    var brutto = d.cost_brutto || d.cost || 0.0;
    var netto = d.cost_netto || 0.0;
    var vatAmt = d.vat_amount || 0.0;

    document.getElementById('costBrutto').innerText = brutto.toFixed(5);
    document.getElementById('costCent').innerText = (brutto * 100).toFixed(3);
    document.getElementById('costNetto').innerText = netto.toFixed(5) + ' €';
    document.getElementById('vatAmount').innerText = vatAmt.toFixed(5) + ' €';

    document.getElementById('wSub').innerText = (curW > 0.1) ? 'Strom fließt' : 'Kein Strom';

    // KI & Gerät
    if(d.active_device){
      currentDevice = d.active_device;
    }

    latestAiSuggestion = d.ai_result || {};
    var conf = latestAiSuggestion.confidence || 0;
    document.getElementById('aiConf').innerText = conf > 0 ? ('Sicherheit: ' + conf + '%') : 'Sammle Daten...';
    document.getElementById('aiReason').innerText = latestAiSuggestion.reason || 'Analyse des Stromflusses...';

    updateWysiwygLook(currentDevice, curW, curWh);

  }).catch(function(){});
}

function sendEm(){
  var em = document.getElementById('emIn').value.trim(), fb = document.getElementById('emFb');
  if(em.indexOf('@') < 0){
    fb.style.display = 'block';
    fb.style.color = '#dc2626';
    fb.innerText = 'Gültige E-Mail eingeben!';
    return;
  }
  fb.style.display = 'block';
  fb.style.color = 'var(--blue)';
  fb.innerText = 'Sende...';
  post('/send_email_invoice', {email: em, report: lastR}).then(function(r){
    fb.style.color = r.status === 'ok' ? '#059669' : '#d97706';
    fb.innerText = r.status === 'ok' ? 'Gesendet!' : (r.message || 'Fehler.');
  });
}

setInterval(poll, 1000);
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
<div class="kpi"><div class="kpi-l">Umsatz Heute (Brutto)</div><div class="kpi-v" style="color:#10b981">{{ "%.5f"|format(today_revenue) }} €</div><div class="kpi-s">Gesamt: {{ "%.5f"|format(total_revenue) }} €</div></div>
<div class="kpi"><div class="kpi-l">Energie Heute</div><div class="kpi-v" style="color:#3b82f6">{{ "%.2f"|format(today_wh) }} Wh</div><div class="kpi-s">Gesamt: {{ "%.4f"|format(total_kwh) }} kWh</div></div>
<div class="kpi"><div class="kpi-l">Sitzungen</div><div class="kpi-v">{{ today_sessions }}</div><div class="kpi-s">Gesamt: {{ total_sessions }}</div></div>
<div class="kpi"><div class="kpi-l">Live Status</div><div class="kpi-v" style="color:{% if live_active %}#10b981{% else %}#94a3b8{% endif %}">{% if live_active %}AKTIV{% else %}BEREIT{% endif %}</div><div class="kpi-s">Relais: {% if relay_on %}EIN{% else %}AUS{% endif %}</div></div>
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
<table><thead><tr><th>Beleg</th><th>Datum</th><th>Gerät & Land</th><th>Dauer</th><th>Wh</th><th>Brutto (€)</th></tr></thead>
<tbody>{% for r in history_records|reverse %}<tr><td><code>{{ r.invoice_id }}</code></td><td>{{ r.date }}</td><td>{% for d in r.devices %}{{ d.icon|default('🔌') }} {{ d.name|default('?') }} ({{ 'Akku' if d.is_battery else 'Dauerbetrieb' }})<br>{% endfor %}<span style="font-size:10.5px;color:#94a3b8">{{ r.country_flag|default('🇩🇪') }} {{ r.country_name|default('DE') }} ({{ r.vat_rate|default(19.0) }}% {{ r.vat_name|default('MwSt') }})</span></td><td>{{ r.time_formatted }}</td><td>{{ "%.3f"|format(r.total_wh) }}</td><td><b style="color:#10b981">{{ "%.5f"|format(r.total_cost_brutto|default(r.total_cost|default(0))) }} €</b></td></tr>{% else %}<tr><td colspan="6" style="text-align:center;color:#94a3b8">Keine.</td></tr>{% endfor %}</tbody></table>
</div></div>
<script>function ovr(a){fetch('/admin_api/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})}).then(function(r){return r.json()}).then(function(d){alert(d.message||'OK');location.reload()})}</script>
</body></html>"""


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)