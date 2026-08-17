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

# Farben für Visualisierungsbalken
DEVICE_COLORS = ["#2563eb", "#059669", "#7c3aed", "#d97706", "#db2777", "#0891b2", "#ea580c"]

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
poll_lock = threading.Lock()

shelly = {
    "watt": 0.0, "amp": 0.0, "volt": 230.0,
    "ok": False, "poll_time": 0.0, "error": ""
}

charge = {
    "active": False,
    "paused": False,
    "terminated": False,
    "relay_on": False,
    "selected_country": "DE",
    
    # --- PRÄZISER ABSTECK- & WECHSEL-WORKFLOW MIT SCHONFRIST ---
    "unplug_modal": None,            # None | "ASK_UNPLUG" | "ASK_NEXT_DEVICE"
    "battery_modal": None,           # None | "BATTERY_80" | "BATTERY_100"
    "power_shift_modal": None,       # None | {"from_w": 12.0, "to_w": 85.0}
    "power_shift_cooldown_until": 0.0,
    "last_stable_w": 0.0,
    "stable_samples_count": 0,
    
    "unplug_cooldown_until": 0.0,    # Timestamp: Bis wann kein Absteck-Popup getriggert werden darf (Schonfrist)
    "flow_continuous_seconds": 0.0,  # Zählt zusammenhängende Sekunden stabilen Stromflusses
    "waiting_for_new_plug": False,   # True, wenn Relais wieder EIN ist und auf Strom gewartet wird
    "target_is_new": True,           # True -> erstellt neues Gerät; False -> verwendet selected_device_idx
    "had_flowing": False,            # True, erst nachdem Strom mindestens 3s stabil floss
    "is_flowing": False,
    
    # --- DURCHGEHENDER SERVER-MASTER-TIMER (STOPPT NIE) ---
    "session_start_time": None,      # Timestamp des ersten Starts in dieser Sitzung
    "total_session_seconds": 0.0,
    "total_flow_seconds": 0.0,       # Reine Stromflusszeit
    "total_idle_seconds": 0.0,       # Reine Pausenzeit
    
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
AI_LEARNED_FILE = "ai_learned_models.json"


# =====================================================================
# SELBSTLERNENDE KI-GERAETEERKENNUNG & FINGERPRINTING
# =====================================================================
class DeviceAI:
    learned_models = {}

    @classmethod
    def load_learned(cls):
        if os.path.exists(AI_LEARNED_FILE):
            try:
                with open(AI_LEARNED_FILE, "r", encoding="utf-8") as f:
                    cls.learned_models = json.load(f)
                    logger.info(f"🧠 [KI-Modell] {len(cls.learned_models)} gelernte Fingerprints geladen.")
            except Exception as e:
                logger.error(f"Fehler beim Laden der gelernten KI-Muster: {e}")

    @classmethod
    def save_learned(cls):
        try:
            with open(AI_LEARNED_FILE, "w", encoding="utf-8") as f:
                json.dump(cls.learned_models, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Fehler beim Speichern der gelernten KI-Muster: {e}")

    @classmethod
    def learn_from_feedback(cls, key, power_history, current_w=0.0):
        """Lernt kontinuierlich von Benutzer-Bestätigungen und Geräte-Verhaltensmustern (inkl. Standby/Sleep-Dynamik)"""
        if not key or key not in DEVICE_PROFILES:
            return
        
        valid_watts = [w for _, w in power_history if w > 0.1] if power_history else []
        if not valid_watts and current_w > 0.1:
            valid_watts = [current_w]
        
        if not valid_watts:
            return

        pw = max(valid_watts)
        aw = sum(valid_watts) / len(valid_watts)
        n = len(valid_watts)

        if n >= 4:
            fh = sum(valid_watts[:n//2]) / max(1, n//2)
            sh = sum(valid_watts[n//2:]) / max(1, n - n//2)
            trend_ratio = sh / fh if fh > 0 else 1.0
            variance = sum((w - aw)**2 for w in valid_watts) / n
            cv = (variance**0.5) / aw if aw > 0 else 0
        else:
            trend_ratio = 1.0
            cv = 0.0

        sample = {
            "avg_w": round(aw, 2),
            "peak_w": round(pw, 2),
            "cv": round(cv, 3),
            "trend": round(trend_ratio, 3),
            "samples_count": n,
            "ts": time.time()
        }

        if key not in cls.learned_models:
            cls.learned_models[key] = {
                "name": DEVICE_PROFILES[key]["name"],
                "confirmations": 0,
                "samples": [],
                "centroid": {}
            }

        m = cls.learned_models[key]
        m["confirmations"] += 1
        m["samples"].append(sample)
        if len(m["samples"]) > 60:
            m["samples"] = m["samples"][-60:]

        # Berechne neuen Zentroiden (Mittelwert des Gerätemodells)
        s_list = m["samples"]
        c_aw = sum(s["avg_w"] for s in s_list) / len(s_list)
        c_pw = sum(s["peak_w"] for s in s_list) / len(s_list)
        c_cv = sum(s["cv"] for s in s_list) / len(s_list)
        c_tr = sum(s["trend"] for s in s_list) / len(s_list)

        m["centroid"] = {
            "avg_w": round(c_aw, 2),
            "peak_w": round(c_pw, 2),
            "cv": round(c_cv, 3),
            "trend": round(c_tr, 3)
        }

        cls.save_learned()
        logger.info(f"🧠 [KI-Lernen] Profil für '{key}' aktualisiert! Bestätigungen={m['confirmations']}, Zentroid: Ø {c_aw:.1f} W (Peak {c_pw:.1f} W)")

    @classmethod
    def classify(cls, power_history):
        total_learned = sum(m.get("confirmations", 0) for m in cls.learned_models.values())

        if len(power_history) < 2:
            return {
                "suggested_key": "lamp",
                "name": "Erkennung läuft...",
                "icon": "💡",
                "mode": "continuous",
                "is_battery": False,
                "confidence": 0,
                "reason": "Sammle Leistungsdaten...",
                "learned_match": False,
                "total_learned_confirmations": total_learned,
                "peak_w": 0.0, "avg_w": 0.0, "current_w": 0.0
            }

        watts = [w for _, w in power_history]
        cw = watts[-1]
        valid_watts = [w for w in watts if w > 0.1]

        if not valid_watts:
            return {
                "suggested_key": "lamp",
                "name": "Standby / Schlafmodus (0 W)",
                "icon": "🔌",
                "mode": "continuous",
                "is_battery": False,
                "confidence": 0,
                "reason": "Gerät im Schlafmodus, Standby oder Stecker abgezogen",
                "learned_match": False,
                "total_learned_confirmations": total_learned,
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

        # =====================================================================
        # 1. ADAPTIVES MUSTER-MATCHING (K-NEAREST GELERNTES MODELL)
        # =====================================================================
        best_learned_key = None
        best_sim = 0.0

        for l_key, l_data in cls.learned_models.items():
            if l_data.get("confirmations", 0) > 0 and "centroid" in l_data and l_data["centroid"]:
                cent = l_data["centroid"]
                d_aw = abs(aw - cent["avg_w"]) / max(5.0, cent["avg_w"])
                d_pw = abs(pw - cent["peak_w"]) / max(5.0, cent["peak_w"])
                d_cv = abs(cv - cent["cv"]) / 0.25
                d_tr = abs(trend_ratio - cent["trend"]) / 0.35

                dist = ( (d_aw**2)*2.5 + (d_pw**2)*1.5 + (d_cv**2)*0.8 + (d_tr**2)*0.8 ) ** 0.5
                sim = max(0.0, 1.0 - (dist / 2.3))

                if sim > best_sim:
                    best_sim = sim
                    best_learned_key = l_key

        # Wenn eine gelernte Nutzersignatur stark übereinstimmt (> 68%):
        if best_learned_key and best_sim >= 0.68:
            l_info = cls.learned_models[best_learned_key]
            prof = DEVICE_PROFILES.get(best_learned_key, DEVICE_PROFILES["lamp"])
            conf = min(99, int(76 + best_sim * 20 + min(4, l_info['confirmations']) * 1))
            reason = f"🎯 Aus deinen Bestätigungen gelernt: {int(best_sim*100)}% Übereinstimmung mit bisherigem '{prof['name']}' (Ø {l_info['centroid']['avg_w']:.1f} W)"
            return {
                "suggested_key": best_learned_key,
                "name": prof["name"],
                "icon": prof["icon"],
                "mode": prof["mode"],
                "is_battery": prof["is_battery"],
                "nominal_wh": prof["nominal_wh"],
                "confidence": conf,
                "reason": reason,
                "learned_match": True,
                "learned_count": l_info['confirmations'],
                "total_learned_confirmations": total_learned,
                "peak_w": round(pw, 2),
                "avg_w": round(aw, 2),
                "current_w": round(cw, 2)
            }

        # =====================================================================
        # 2. EXPERTENSYSTEM HEURISTIK (PRAXISERPROBTE LADESTATION-MUSTER)
        # =====================================================================
        confidence = min(96, 75 + n * 4)
        if pw < 0.5:
            s_key = "lamp"
            reason = "Kein messbarer Stromfluss"
            confidence = 10
        elif pw < 35.0:
            s_key = "phone"
            reason = f"Ladeleistung (Ø {aw:.1f} W / Peak {pw:.1f} W) typisch für Smartphone, Tablet oder Akku"
        elif pw < 105.0:
            s_key = "laptop"
            reason = f"Ladeleistung (Ø {aw:.1f} W / Peak {pw:.1f} W) typisch für Laptop / Ultrabook"
        elif pw < 340.0:
            s_key = "ebike"
            reason = f"Starke Ladeleistung (Ø {aw:.1f} W / Peak {pw:.1f} W) typisch für E-Bike / Pedelec"
        elif pw < 1200.0:
            s_key = "ebike_fast"
            reason = f"Sehr hohe Schnelllade-Leistung (Ø {aw:.1f} W / Peak {pw:.1f} W) für Großakku"
        else:
            s_key = "appliance"
            reason = f"Hohe Dauerlast ({aw:.1f} W) für Großgerät"

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
            "learned_match": False,
            "total_learned_confirmations": total_learned,
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
DeviceAI.load_learned()

USER_FEEDBACK_FILE = "user_feedback_log.json"
user_feedback_entries = []

class AssistantBot:
    @classmethod
    def load_feedback(cls):
        global user_feedback_entries
        if os.path.exists(USER_FEEDBACK_FILE):
            try:
                with open(USER_FEEDBACK_FILE, "r", encoding="utf-8") as f:
                    user_feedback_entries = json.load(f)
                    logger.info(f"💬 [Feedback-Log] {len(user_feedback_entries)} Interaktionen geladen.")
            except Exception as e:
                logger.error(f"Fehler beim Laden des Feedback-Logs: {e}")

    @classmethod
    def log_interaction(cls, user_msg, bot_response, context_snapshot, executed_action=None):
        global user_feedback_entries
        entry = {
            "timestamp": time.time(),
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_message": user_msg,
            "bot_response": bot_response,
            "executed_action": executed_action,
            "context": context_snapshot
        }
        user_feedback_entries.insert(0, entry)
        if len(user_feedback_entries) > 200:
            user_feedback_entries = user_feedback_entries[:200]
        try:
            with open(USER_FEEDBACK_FILE, "w", encoding="utf-8") as f:
                json.dump(user_feedback_entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Fehler beim Speichern des Feedback-Logs: {e}")

    @classmethod
    def respond(cls, user_msg, charge_state, shelly_state):
        msg = user_msg.strip().lower()
        now = time.time()
        
        curr_idx = charge_state.get("current_device_idx", 0)
        devs = charge_state.get("devices", [])
        cur_d = devs[curr_idx] if 0 <= curr_idx < len(devs) else None
        
        w = shelly_state.get("watt", 0.0)
        a = shelly_state.get("amp", 0.0)
        v = shelly_state.get("volt", 230.0)
        wh = charge_state.get("total_wh", 0.0)
        cost = charge_state.get("total_cost_brutto", 0.0)
        is_active = charge_state.get("active", False) and not charge_state.get("paused", False)
        is_paused = charge_state.get("paused", False)
        
        dev_name = cur_d["name"] if cur_d else "Gerät 1"
        is_batt = cur_d.get("is_battery", False) if cur_d else False
        nom_wh = cur_d.get("nominal_wh", 0.0) if cur_d else 0.0
        cur_wh = cur_d.get("wh", 0.0) if cur_d else 0.0
        soc_pct = min(100.0, (cur_wh / nom_wh * 100.0)) if is_batt and nom_wh > 0 else None

        executed_action = None
        action_payload = None

        # 1. AKTION: PAUSE
        if any(kw in msg for kw in ["pause", "pausieren", "stoppen", "halt", "aus schalten", "ausschalten"]):
            executed_action = "pause"
            bot_text = f"⏸️ Ich habe die Stromzufuhr für **{dev_name}** pausiert. Die Steckdose ist jetzt abgeschaltet (0.0 W). Du kannst jederzeit mit 'Fortsetzen' oder über den Button weiterladen!"

        # 2. AKTION: FORTSETZEN
        elif any(kw in msg for kw in ["fortsetzen", "weiter", "start", "einschalten", "wieder an", "an schalten"]):
            executed_action = "resume"
            bot_text = f"▶️ Ladevorgang für **{dev_name}** fortgesetzt! Das Relais wurde eingeschaltet und Strom fließt wieder."

        # 3. AKTION: WECHSEL AUF GERÄT X
        elif "wechsel" in msg or "zu gerät" in msg or "auf gerät" in msg:
            target_idx = None
            for idx, d in enumerate(devs):
                if str(d.get("num", idx+1)) in msg:
                    target_idx = idx
                    break
            if target_idx is not None and 0 <= target_idx < len(devs):
                executed_action = "switch_device"
                action_payload = {"device_idx": target_idx}
                bot_text = f"👉 Ich habe die aktive Erfassung auf **{devs[target_idx]['name']}** umgeschaltet. Alle weiteren Messwerte fließen jetzt hierhin."
            else:
                bot_text = f"ℹ️ Du hast {len(devs)} Geräte erfasst. Klicke einfach oben auf den Reiter des gewünschten Geräts oder sage mir z. B. 'Wechsle zu Gerät 1'."

        # 4. AKTION: 100% AKKU FREIGEBEN
        elif "100" in msg or "voll" in msg:
            executed_action = "charge_100"
            bot_text = f"⚡ 100%-Modus aktiviert! Der 80%-Schutzstop wird übersprungen und **{dev_name}** wird bis zur vollen Kapazität ({nom_wh:.0f} Wh) geladen."

        # 5. FRAGE: AKKUSTAND / RESTDAUER
        elif any(kw in msg for kw in ["wie lange", "dauer", "akku", "prozent", "fertig", "wann voll"]):
            if is_batt and nom_wh > 0:
                needed_wh = max(0.0, nom_wh * 0.8 - cur_wh) if not cur_d.get("charge_to_100") else max(0.0, nom_wh - cur_wh)
                target_pct = 100 if cur_d.get("charge_to_100") else 80
                if w > 1.0:
                    hours_left = needed_wh / w
                    mins_left = int(hours_left * 60)
                    bot_text = f"🔋 **{dev_name}** ist zu ca. **{soc_pct:.0f}%** geladen ({cur_wh:.1f} von {nom_wh:.0f} Wh). Bei aktueller Leistung ({w:.1f} W) dauert es noch ca. **{mins_left} Minuten** bis zu den optimalen **{target_pct}%**."
                else:
                    bot_text = f"🔋 **{dev_name}** hat bisher **{cur_wh:.2f} Wh** geladen ({soc_pct:.0f}%). Aktuell fließt kein messbarer Strom ({w:.1f} W)."
            else:
                bot_text = f"ℹ️ **{dev_name}** ist als Dauerbetrieb konfiguriert ({w:.1f} W). Hier gibt es keine feste Akkugrenze. Du kannst es jederzeit über 'Ändern' als Akku-Gerät einstellen."

        # 6. FRAGE: WATT-SCHWANKUNGEN / 0 WATT
        elif any(kw in msg for kw in ["0 watt", "0w", "schwankt", "warum 0", "warum schwankt", "warum steht 0", "warum so wenig"]):
            bot_text = f"⚡ **Warum schwankt die Leistung?**\n" \
                       f"• Moderne Netzteile & Akkus (z. B. Smartphone / E-Bike) regulieren die Stromstärke dynamisch: Am Anfang laden sie mit voller Leistung, gegen Ende sinkt die Wattzahl stetig (CV-Phase).\n" \
                       f"• Steht 0.0 W, ist das Gerät entweder voll geladen, im Standby oder die Steckdose wurde pausiert. Aktuelle Spannung: {v:.1f} V."

        # 7. FRAGE: KOSTEN / PREIS
        elif any(kw in msg for kw in ["preis", "kosten", "teuer", "kwh", "euro", "cent"]):
            c_info = COUNTRY_VAT_RATES.get(charge_state.get("selected_country"), COUNTRY_VAT_RATES["DE"])
            bot_text = f"💶 **Kostenübersicht:**\n" \
                       f"• Bisheriger Verbrauch: **{wh:.3f} Wh** ({wh/1000.0:.6f} kWh)\n" \
                       f"• Aktueller Gesamtbetrag: **{cost:.4f} €** (inkl. {c_info['rate']}% {c_info['vat_name']})\n" \
                       f"• Tarif: **0.40 € pro kWh** (0.336 € netto)."

        # 8. FRAGE / ANLEITUNG: GERÄTEWECHSEL
        elif any(kw in msg for kw in ["umstecken", "abstecken", "neues gerät", "wie wechsel"]):
            bot_text = f"🔄 **So funktioniert der Gerätewechsel:**\n" \
                       f"1. Ziehe einfach den Stecker des aktuellen Geräts ab.\n" \
                       f"2. Es öffnet sich automatisch das Wechsel-Menü, wo du das nächste Gerät (z. B. Smartphone oder Laptop) antippen kannst.\n" \
                       f"3. Alternativ kannst du jederzeit oben auf den Button **[➕ Neues Gerät]** tippen oder zwischen den Reitern wechseln."

        # 9. FEEDBACK / ALLGEMEIN
        else:
            bot_text = f"🤖 **Ich habe deine Nachricht erfasst:** „*{user_msg}*“.\n\n" \
                       f"💡 **Aktueller Live-Status:**\n" \
                       f"• Gerät: **{dev_name}** ({w:.1f} W | {wh:.2f} Wh)\n" \
                       f"• Status: {'🟢 Aktiv lädt' if is_active else ('⏸️ Pausiert' if is_paused else 'Bereit')}\n" \
                       f"• Tippe unten auf eine Frage oder sage mir z. B. *'Pausiere'*, *'Wie lange noch?'* oder *'Auf 100% laden'*."

        context_snapshot = {
            "watt": round(w, 2),
            "amp": round(a, 3),
            "volt": round(v, 1),
            "wh": round(wh, 4),
            "cost_brutto": round(cost, 4),
            "active_device": dev_name,
            "is_active": is_active,
            "is_paused": is_paused,
            "device_count": len(devs)
        }

        cls.log_interaction(user_msg, bot_text, context_snapshot, executed_action)
        return bot_text, executed_action, action_payload

AssistantBot.load_feedback()


# =====================================================================
# SHELLY CLOUD - ZENTRALER GEPUFFERTER POLLER (RATE-LIMIT SCHUTZ)
# =====================================================================
def poll_shelly():
    now = time.time()
    if now < shelly["poll_time"]:
        return

    # Nächsten regulären Poll-Zeitpunkt auf +3.5s setzen (sicher unter Shelly Cloud Rate Limit)
    shelly["poll_time"] = now + 3.5

    try:
        r = http_requests.post(
            f"{SHELLY_CLOUD_URL}/device/status",
            data={"auth_key": AUTH_KEY, "id": DEVICE_ID},
            timeout=4.0
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
                err = j.get("error", "isok=false")
                shelly["error"] = err
                # Bei Rate Limit: 7 Sekunden Pause einlegen
                if "TOO_MANY" in str(err).upper() or "REQUESTS" in str(err).upper():
                    shelly["poll_time"] = now + 7.0
                    logger.warning("⚠️ Shelly Cloud Rate-Limit erreicht -> 7s Cooldown aktiviert.")
        elif r.status_code == 429:
            shelly["error"] = "Rate Limit (429)"
            shelly["poll_time"] = now + 8.0
        else:
            shelly["error"] = f"HTTP {r.status_code}"
            shelly["poll_time"] = now + 4.0
    except Exception as e:
        shelly["error"] = str(e)[:80]
        shelly["poll_time"] = now + 4.0


def relay_control(turn_on):
    def _do():
        s = "on" if turn_on else "off"
        try:
            http_requests.post(f"{SHELLY_CLOUD_URL}/device/relay/control",
                               data={"auth_key": AUTH_KEY, "id": DEVICE_ID, "turn": s, "channel": 0}, timeout=4)
        except Exception as e:
            logger.error(f"Relay control error: {e}")
        charge["relay_on"] = turn_on
        logger.info(f"Relay -> {'EIN' if turn_on else 'AUS'}")
    threading.Thread(target=_do, daemon=True).start()


# =====================================================================
# KERNFUNKTIONEN
# =====================================================================
def get_session_elapsed():
    """Kontinuierliche Gesamtlaufzeit, die seit dem ersten Start durchgehend zählt"""
    if charge["terminated"]:
        return charge["total_session_seconds"]
    if charge["session_start_time"]:
        return time.time() - charge["session_start_time"]
    return 0.0


def new_device_entry(num, key="lamp"):
    prof = DEVICE_PROFILES.get(key, DEVICE_PROFILES["lamp"])
    color_idx = (num - 1) % len(DEVICE_COLORS)
    return {
        "num": num,
        "key": prof["key"],
        "name": f"Gerät {num}: {prof['name']}",
        "raw_name": prof["name"],
        "icon": prof["icon"],
        "color": DEVICE_COLORS[color_idx],
        "mode": prof["mode"],
        "is_battery": prof["is_battery"],
        "nominal_wh": prof["nominal_wh"],
        "user_confirmed": False,
        
        # 80% & 100% Akkuschutz Status
        "charge_to_100": False,
        "notified_80": False,
        "notified_100": False,
        
        "duration_sec": 0.0,
        "flow_duration_sec": 0.0,
        "idle_duration_sec": 0.0,
        "avg_flow_w": 0.0,
        "wh": 0.0,
        "cost_netto": 0.0,
        "vat_amount": 0.0,
        "cost_brutto": 0.0,
        "cost": 0.0,
        "peak_w": 0.0
    }

charge["devices"] = [new_device_entry(1, "lamp")]

def accumulate_energy():
    now = time.time()
    last = charge.get("last_wh_time")

    if not charge.get("devices"):
        charge["devices"] = [new_device_entry(1, "lamp")]
        charge["current_device_idx"] = 0

    c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
    vat_rate = c_info["rate"]

    w = shelly["watt"]
    a = shelly["amp"]
    
    # Echter Stromfluss (> 0.05 W oder > 0.005 A)
    is_flowing = (a >= 0.005 or w >= 0.05)
    charge["is_flowing"] = is_flowing

    # =====================================================================
    # 1. ERKENNUNG: GERAET EINGESTECKT / WIEDERANLAUF
    # =====================================================================
    if is_flowing and not charge["paused"] and not charge["terminated"]:
        if charge["session_start_time"] is None:
            charge["session_start_time"] = now
            charge["active"] = True
            charge["last_wh_time"] = now

        if charge["waiting_for_new_plug"] or not charge["active"]:
            charge["waiting_for_new_plug"] = False
            charge["active"] = True
            charge["last_wh_time"] = now
            charge["unplug_cooldown_until"] = now + 15.0
            charge["flow_continuous_seconds"] = 0.0
            charge["had_flowing"] = False
            charge["last_stable_w"] = w
            charge["stable_samples_count"] = 1
            
            curr_idx = charge.get("current_device_idx", 0)
            devs = charge.get("devices", [])
            prev_d = devs[curr_idx] if (0 <= curr_idx < len(devs)) else None
            
            # Neues separates Gerät NUR anlegen, wenn target_is_new explizit True gesetzt wurde!
            if charge.get("target_is_new", False) and prev_d and prev_d.get("wh", 0) > 0.04:
                charge["target_is_new"] = False
                next_num = len(devs) + 1
                charge["power_history"] = [(now, w)]
                ai = DeviceAI.classify(charge["power_history"])
                s_key = ai.get("suggested_key", "phone")
                prof = DEVICE_PROFILES.get(s_key, DEVICE_PROFILES["phone"])
                
                new_dev = new_device_entry(next_num, s_key)
                new_dev["raw_name"] = prof["name"]
                new_dev["name"] = f"Gerät {next_num}: {prof['name']}"
                new_dev["icon"] = prof["icon"]
                new_dev["mode"] = prof["mode"]
                new_dev["is_battery"] = prof["is_battery"]
                new_dev["nominal_wh"] = prof["nominal_wh"]
                new_dev["user_confirmed"] = False
                
                charge["devices"].append(new_dev)
                charge["current_device_idx"] = len(charge["devices"]) - 1
                charge["ai_result"] = ai
                logger.info(f"⚡ Neues separates Gerät #{next_num} ({new_dev['name']}) gestartet ({w:.1f} W).")
            else:
                # Gleiches Gerät fortsetzen!
                charge["target_is_new"] = False
                if prev_d:
                    logger.info(f"⚡ Gleiches Gerät ({prev_d['name']}) wird nahtlos fortgesetzt ({w:.1f} W).")
                else:
                    charge["power_history"] = [(now, w)]
                    ai = DeviceAI.classify(charge["power_history"])
                    charge["ai_result"] = ai

    # =====================================================================
    # 2. STABILER STROMFLUSS-AUFBAU (Schonfrist & Standby-Berücksichtigung)
    # =====================================================================
    if last and last > 0:
        dt = min(15.0, max(0.0, now - last))
        if is_flowing and charge["active"]:
            charge["flow_continuous_seconds"] += dt
            if charge["flow_continuous_seconds"] >= 3.0 and now > charge["unplug_cooldown_until"]:
                charge["had_flowing"] = True
        else:
            charge["flow_continuous_seconds"] = 0.0

    # =====================================================================
    # 3. ERKENNUNG: ABSTECKEN DES KABELS (AMPERE -> 0.000 A)
    # =====================================================================
    if charge["active"] and not charge["paused"] and charge["had_flowing"] and now > charge["unplug_cooldown_until"]:
        if not is_flowing and (a < 0.015 and w < 0.25):
            charge["waiting_for_new_plug"] = True
            charge["had_flowing"] = False
            charge["flow_continuous_seconds"] = 0.0
            charge["unplug_cooldown_until"] = now + 6.0
            charge["last_stable_w"] = 0.0
            charge["stable_samples_count"] = 0
            logger.info("🔌 Kabel abgezogen (0.0 A) -> Warte auf nächstes Gerät oder Fortsetzung...")

    # =====================================================================
    # 4. ERKENNUNG: DRASTISCHER LASTWECHSEL / STATUSÄNDERUNG (Gerätewechsel?)
    # =====================================================================
    if charge["active"] and is_flowing and now > charge.get("power_shift_cooldown_until", 0.0) and now > charge.get("unplug_cooldown_until", 0.0):
        base_w = charge.get("last_stable_w", 0.0)
        s_cnt = charge.get("stable_samples_count", 0)
        
        if s_cnt >= 6 and base_w > 4.0:
            ratio = w / base_w
            delta = abs(w - base_w)
            
            # Signifikanter Lastsprung (z.B. von 12W auf 85W oder 120W auf 10W bei Delta > 20W)
            if (ratio >= 2.8 or ratio <= 0.35) and delta >= 20.0:
                charge["power_shift_modal"] = {
                    "from_w": round(base_w, 1),
                    "to_w": round(w, 1),
                    "created_at": now
                }
                charge["power_shift_modal_created_at"] = now
                charge["power_shift_cooldown_until"] = now + 35.0
                logger.info(f"⚡ Signifikanter Lastsprung: {base_w:.1f} W -> {w:.1f} W (Delta {delta:.1f} W)!")

        # Laufende Aktualisierung der stabilen Referenzleistung
        if abs(w - base_w) < max(4.0, base_w * 0.30):
            charge["stable_samples_count"] = s_cnt + 1
            charge["last_stable_w"] = (base_w * 0.85) + (w * 0.15)
        else:
            charge["stable_samples_count"] = 1
            charge["last_stable_w"] = w

    # Automatischer 20s-Timeout: Falls Nutzer nicht reagiert, gleiches Gerät beibehalten
    if charge.get("power_shift_modal"):
        created_at = charge.get("power_shift_modal_created_at", now)
        if (now - created_at) >= 20.0:
            idx = charge["current_device_idx"]
            devs = charge["devices"]
            if 0 <= idx < len(devs):
                cur_d = devs[idx]
                if cur_d.get("key") in DEVICE_PROFILES:
                    DeviceAI.learn_from_feedback(cur_d["key"], charge.get("power_history", []), w)
                logger.info(f"⏳ 20s Timeout: Lastwechsel automatisch als gleiches Gerät bestätigt ({cur_d['name']}).")
            charge["power_shift_modal"] = None
            charge["power_shift_cooldown_until"] = now + 35.0

    # =====================================================================
    # 5. ENERGIE- & ZEIT-AKKUMULATION + 80% / 100% AKKUSCHUTZ
    # =====================================================================
    if last and last > 0 and charge["session_start_time"] is not None:
        dt = now - last
        if 0.2 < dt < 15.0:
            if is_flowing and charge["active"]:
                delta_wh = (w * dt) / 3600.0
                charge["total_flow_seconds"] += dt
                charge["total_wh"] += delta_wh
                charge["total_kwh"] = charge["total_wh"] / 1000.0
                
                charge["total_cost_netto"] = charge["total_kwh"] * STROMPREIS_PER_KWH
                charge["total_vat_amount"] = charge["total_cost_netto"] * (vat_rate / 100.0)
                charge["total_cost_brutto"] = charge["total_cost_netto"] + charge["total_vat_amount"]

                charge["power_history"].append((now, w))
                if len(charge["power_history"]) > 120:
                    charge["power_history"] = charge["power_history"][-120:]

                # Auf aktives Gerät buchen
                idx = charge["current_device_idx"]
                devs = charge["devices"]
                if 0 <= idx < len(devs):
                    d = devs[idx]
                    d["duration_sec"] = d.get("duration_sec", 0) + dt
                    d["flow_duration_sec"] = d.get("flow_duration_sec", 0.0) + dt
                    d["wh"] = d.get("wh", 0) + delta_wh
                    if d["flow_duration_sec"] > 0:
                        d["avg_flow_w"] = (d["wh"] * 3600.0) / d["flow_duration_sec"]
                    
                    d["cost_netto"] = (d["wh"] / 1000.0) * STROMPREIS_PER_KWH
                    d["vat_amount"] = d["cost_netto"] * (vat_rate / 100.0)
                    d["cost_brutto"] = d["cost_netto"] + d["vat_amount"]
                    d["cost"] = d["cost_brutto"]
                    d["peak_w"] = max(d.get("peak_w", 0), w)

                    # --- 80% & 100% AKKU-ABSCHALTAUTOMATIK ---
                    if d.get("is_battery") and d.get("nominal_wh", 0) > 0:
                        nom_wh = d["nominal_wh"]
                        cur_wh = d["wh"]
                        soc_pct = (cur_wh / nom_wh) * 100.0

                        # Fall A: 80% Akkuschutz schlägt an
                        if soc_pct >= 80.0 and not d.get("charge_to_100", False) and not d.get("notified_80", False):
                            d["notified_80"] = True
                            charge["active"] = False
                            charge["paused"] = True
                            charge["battery_modal"] = "BATTERY_80"
                            relay_control(False)
                            logger.info(f"🔋 80% Akkuschutz erreicht ({cur_wh:.2f} Wh / {nom_wh} Wh) -> Relais AUS, Modal BATTERY_80!")

                        # Fall B: 100% Vollladung erreicht
                        elif (soc_pct >= 100.0 or (soc_pct >= 92.0 and w < 1.5)) and d.get("charge_to_100", False) and not d.get("notified_100", False):
                            d["notified_100"] = True
                            charge["active"] = False
                            charge["paused"] = True
                            charge["battery_modal"] = "BATTERY_100"
                            relay_control(False)
                            logger.info(f"🔋 100% Akku voll geladen ({cur_wh:.2f} Wh / {nom_wh} Wh) -> Relais AUS, Modal BATTERY_100!")

            else:
                # Standby / Pause-Zeit nur buchen, wenn Session läuft und (Relais aus ODER Cooldown abgelaufen)
                if charge["session_start_time"] and (charge["paused"] or not charge["relay_on"] or now > charge.get("unplug_cooldown_until", 0)):
                    charge["total_idle_seconds"] += dt
                    idx = charge["current_device_idx"]
                    devs = charge["devices"]
                    if 0 <= idx < len(devs):
                        d = devs[idx]
                        d["idle_duration_sec"] = d.get("idle_duration_sec", 0.0) + dt

    charge["last_wh_time"] = now

    # KI-Klassifizierung
    if charge["active"]:
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
                    cur_dev["raw_name"] = s_prof["name"]
                    cur_dev["name"] = f"Gerät {cur_dev['num']}: {s_prof['name']}"
                    cur_dev["icon"] = s_prof["icon"]
                    cur_dev["mode"] = s_prof["mode"]
                    cur_dev["is_battery"] = s_prof["is_battery"]
                    cur_dev["nominal_wh"] = s_prof["nominal_wh"]


# =====================================================================
# KONTINUIERLICHER SERVER-SEITIGER HINTERGRUND-THREAD (24/7 DAEMON)
# =====================================================================
daemon_pid = None
daemon_lock = threading.Lock()

def background_energy_daemon():
    """Zählt auch bei Screensaver, Schlafmodus oder ausgeschaltetem Handy-Display 100% akkurat weiter!"""
    logger.info(f"⚡ Master Background Energy Daemon läuft aktiv in PID {os.getpid()}!")
    while True:
        try:
            poll_shelly()
            with lock:
                accumulate_energy()
        except Exception as e:
            logger.error(f"Energy daemon error: {e}")
        time.sleep(1.2)

def ensure_daemon_started():
    global daemon_pid
    curr_pid = os.getpid()
    if daemon_pid != curr_pid:
        with daemon_lock:
            if daemon_pid != curr_pid:
                daemon_pid = curr_pid
                t = threading.Thread(target=background_energy_daemon, daemon=True)
                t.start()
                logger.info(f"🚀 Master Energy Daemon gestartet in PID {curr_pid}!")


def fmt_time(s):
    s = int(max(0, s))
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


# =====================================================================
# ROUTES
# =====================================================================
@app.before_request
def check_worker_daemon():
    ensure_daemon_started()

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
            charge["active"] = True
            charge["paused"] = False
            charge["relay_on"] = True
            charge["unplug_modal"] = None
            charge["battery_modal"] = None
            charge["power_shift_modal"] = None
            charge["power_shift_cooldown_until"] = 0.0
            charge["last_stable_w"] = 0.0
            charge["stable_samples_count"] = 0
            charge["unplug_cooldown_until"] = 0.0
            charge["flow_continuous_seconds"] = 0.0
            charge["waiting_for_new_plug"] = False
            charge["target_is_new"] = True
            charge["had_flowing"] = False
            charge["session_start_time"] = None
            charge["total_session_seconds"] = 0.0
            charge["total_flow_seconds"] = 0.0
            charge["total_idle_seconds"] = 0.0
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
        relay_control(True)
        logger.info(">>> QR-CODE GESCANNT -> RELAIS SOFORT EIN & BEREIT FÜR STROMFLUSS <<<")
        return redirect(url_for('index'))
    return "Ungültiger Token.", 403

@app.route('/reset_session', methods=['POST', 'GET'])
def reset_session():
    with lock:
        charge["terminated"] = False
        charge["last_report"] = None
        charge["active"] = True
        charge["paused"] = False
        charge["relay_on"] = True
        charge["unplug_modal"] = None
        charge["battery_modal"] = None
        charge["power_shift_modal"] = None
        charge["power_shift_cooldown_until"] = 0.0
        charge["last_stable_w"] = 0.0
        charge["stable_samples_count"] = 0
        charge["unplug_cooldown_until"] = 0.0
        charge["flow_continuous_seconds"] = 0.0
        charge["waiting_for_new_plug"] = False
        charge["target_is_new"] = True
        charge["had_flowing"] = False
        charge["session_start_time"] = None
        charge["total_session_seconds"] = 0.0
        charge["total_flow_seconds"] = 0.0
        charge["total_idle_seconds"] = 0.0
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
    relay_control(True)
    logger.info(">>> SITZUNG ZURÜCKGESETZT -> RELAIS SOFORT EIN & BEREIT FÜR STROMFLUSS <<<")
    return jsonify({"status": "ok"})

@app.route('/status')
def get_status():
    with lock:
        elapsed = get_session_elapsed()
        curr_idx = charge["current_device_idx"]
        devices = [dict(d) for d in charge["devices"]]
        active_dev = devices[curr_idx] if 0 <= curr_idx < len(devices) else new_device_entry(1)

        c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
        vat_rate = c_info["rate"]

        netto = charge["total_cost_netto"]
        vat_amt = charge["total_vat_amount"]
        brutto = charge["total_cost_brutto"]

        # Live Watt & Ampere: Wenn tatsächlich Strom fließt (> 0.2W), immer echte Messung ausgeben!
        if shelly["watt"] > 0.2 or shelly["amp"] > 0.015:
            live_watt = round(shelly["watt"], 3)
            live_amp  = round(shelly["amp"], 3)
            if not charge["active"] and not charge["paused"] and not charge["terminated"]:
                charge["active"] = True
        elif charge["paused"] or charge["terminated"] or (not charge["relay_on"]):
            live_watt = 0.0
            live_amp  = 0.0
        else:
            live_watt = round(shelly["watt"], 3)
            live_amp  = round(shelly["amp"], 3)

        return jsonify({
            "active": charge["active"],
            "paused": charge["paused"],
            "terminated": charge["terminated"],
            "relay_on": charge["relay_on"],
            "watt": live_watt,
            "current_ampere": live_amp,
            "voltage": round(shelly["volt"], 1),
            "shelly_ok": shelly["ok"],
            "shelly_error": shelly["error"],
            
            # Laufzeiten (kontinuierlich auf dem Server)
            "elapsed_seconds": round(elapsed, 1),
            "total_flow_seconds": round(charge["total_flow_seconds"], 1),
            "total_idle_seconds": round(charge["total_idle_seconds"], 1),
            "session_started": charge["session_start_time"] is not None,
            
            "wh": round(charge["total_wh"], 4),
            "kwh": round(charge["total_kwh"], 6),
            
            # Absteck-, Lastwechsel- & Akku-Status
            "unplug_modal": charge["unplug_modal"],
            "battery_modal": charge["battery_modal"],
            "power_shift_modal": charge["power_shift_modal"],
            "waiting_for_new_plug": charge["waiting_for_new_plug"],
            "had_flowing": charge["had_flowing"],
            "is_flowing": charge["is_flowing"],
            
            # Mehrwertsteuer & Beträge
            "country": c_info,
            "vat_rate": vat_rate,
            "vat_name": c_info["vat_name"],
            "cost_netto": round(netto, 5),
            "vat_amount": round(vat_amt, 5),
            "cost_brutto": round(brutto, 5),
            "cost": round(brutto, 5),
            
            "ai_result": charge["ai_result"] or {},
            "learned_profiles_count": len(DeviceAI.learned_models),
            "active_device": active_dev,
            "devices": devices,
            "current_device_idx": curr_idx,
            "session_terminated": charge["terminated"],
            "report": charge["last_report"] if charge["terminated"] else None
        })

@app.route('/start', methods=['POST', 'GET'])
def start_charge():
    with lock:
        now = time.time()
        if charge["terminated"]:
            charge["terminated"] = False
            charge["last_report"] = None
            charge["session_start_time"] = None
            charge["total_session_seconds"] = 0.0
            charge["total_flow_seconds"] = 0.0
            charge["total_idle_seconds"] = 0.0
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

        if charge["session_start_time"] is None:
            charge["session_start_time"] = now

        charge["active"] = True
        charge["paused"] = False
        charge["relay_on"] = True
        charge["unplug_modal"] = None
        charge["battery_modal"] = None
        charge["power_shift_modal"] = None
        charge["unplug_cooldown_until"] = now + 15.0
        charge["flow_continuous_seconds"] = 0.0
        charge["had_flowing"] = False
        charge["waiting_for_new_plug"] = False
        charge["target_is_new"] = False
        charge["last_wh_time"] = now
        charge["last_stable_w"] = shelly["watt"]
        charge["stable_samples_count"] = 1
        if not charge["devices"]:
            charge["devices"] = [new_device_entry(1, "lamp")]
            charge["current_device_idx"] = 0
        logger.info(f">>> START / FORTSETZEN: Relais EIN, selbes Gerät aktiv fortgesetzt <<<")

    relay_control(True)
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST', 'GET'])
def stop_charge():
    with lock:
        accumulate_energy()
        charge["active"] = False
        charge["paused"] = True
        charge["relay_on"] = False
        charge["last_wh_time"] = None
        shelly["watt"] = 0.0
        shelly["amp"] = 0.0
    relay_control(False)
    logger.info(">>> PAUSE: Relais AUS, Watt & Ampere auf 0.0 gesetzt <<<")
    return jsonify({"status": "ok"})

# =====================================================================
# ABSTECK- & WIEDERVERWENDUNGS-WORKFLOW ENDPOINT
# =====================================================================
@app.route('/unplug_action', methods=['POST'])
def unplug_action():
    data = request.get_json() or {}
    action = data.get("action")
    now = time.time()

    with lock:
        if action == "no_resume":
            charge["unplug_modal"] = None
            charge["waiting_for_new_plug"] = False
            charge["active"] = True
            charge["paused"] = False
            charge["unplug_cooldown_until"] = now + 10.0
            charge["flow_continuous_seconds"] = 0.0
            charge["last_wh_time"] = now
            relay_control(True)
            logger.info("Unplug: Selbes Gerät direkt fortgesetzt -> Relais EIN, Schonfrist +10s.")
            return jsonify({"status": "ok", "state": "resumed"})

        elif action == "yes_unplugged":
            charge["unplug_modal"] = "ASK_NEXT_DEVICE"
            charge["waiting_for_new_plug"] = False
            charge["unplug_cooldown_until"] = now + 10.0
            logger.info("Unplug: Bestätigt -> Zeige Geräteauswahl (früheres Gerät oder neues Gerät).")
            return jsonify({"status": "ok", "state": "ask_next_device", "devices": charge["devices"]})

        elif action == "select_existing":
            dev_idx = int(data.get("device_idx", 0))
            if 0 <= dev_idx < len(charge["devices"]):
                charge["current_device_idx"] = dev_idx
                charge["target_is_new"] = False
                charge["unplug_modal"] = None
                charge["waiting_for_new_plug"] = True
                charge["unplug_cooldown_until"] = now + 10.0
                charge["flow_continuous_seconds"] = 0.0
                charge["had_flowing"] = False
                charge["active"] = False
                charge["paused"] = True
                relay_control(True)
                cur_d = charge["devices"][dev_idx]
                logger.info(f"Unplug: Früheres Gerät gewählt ({cur_d['name']}) -> Relais EIN, warte auf Stromfluss!")
                return jsonify({"status": "ok", "state": "waiting_for_plug", "reused_device": cur_d})

        elif action == "prep_new_device":
            device_key = data.get("device_key", "phone")
            prof = DEVICE_PROFILES.get(device_key, DEVICE_PROFILES["phone"])
            next_num = len(charge["devices"]) + 1
            new_dev = new_device_entry(next_num, device_key)
            new_dev["raw_name"] = prof["name"]
            new_dev["name"] = f"Gerät {next_num}: {prof['name']}"
            new_dev["icon"] = prof["icon"]
            new_dev["mode"] = prof["mode"]
            new_dev["is_battery"] = prof["is_battery"]
            new_dev["nominal_wh"] = prof["nominal_wh"]
            new_dev["user_confirmed"] = True

            charge["devices"].append(new_dev)
            charge["current_device_idx"] = len(charge["devices"]) - 1
            charge["power_history"] = []
            charge["target_is_new"] = False
            charge["unplug_modal"] = None
            charge["waiting_for_new_plug"] = True
            charge["unplug_cooldown_until"] = now + 15.0
            charge["flow_continuous_seconds"] = 0.0
            charge["had_flowing"] = False
            charge["active"] = True
            charge["paused"] = False
            charge["relay_on"] = True
            relay_control(True)
            logger.info(f"Unplug: Neues Gerät #{next_num} ({new_dev['name']}) gewählt -> Relais EIN, bereit!")
            return jsonify({"status": "ok", "state": "device_selected", "device": new_dev})

        elif action == "finish":
            charge["unplug_modal"] = None
            charge["waiting_for_new_plug"] = False
            return logout()

    return jsonify({"status": "error"}), 400

# =====================================================================
# LASTWECHSEL / STATUSÄNDERUNGS-ENDPOINT
# =====================================================================
@app.route('/power_shift_action', methods=['POST'])
def power_shift_action():
    data = request.get_json() or {}
    action = data.get("action")  # "new_device" | "same_device"
    now = time.time()

    with lock:
        charge["power_shift_modal"] = None
        charge["power_shift_cooldown_until"] = now + 25.0

        if action == "new_device":
            next_num = len(charge["devices"]) + 1
            w = shelly["watt"]
            charge["power_history"] = [(now, w)] if w > 0.1 else []
            ai = DeviceAI.classify(charge["power_history"])
            s_key = ai.get("suggested_key", "phone")
            prof = DEVICE_PROFILES.get(s_key, DEVICE_PROFILES["phone"])

            new_dev = new_device_entry(next_num, s_key)
            new_dev["raw_name"] = prof["name"]
            new_dev["name"] = f"Gerät {next_num}: {prof['name']}"
            new_dev["icon"] = prof["icon"]
            new_dev["mode"] = prof["mode"]
            new_dev["is_battery"] = prof["is_battery"]
            new_dev["nominal_wh"] = prof["nominal_wh"]
            new_dev["user_confirmed"] = False

            charge["devices"].append(new_dev)
            charge["current_device_idx"] = len(charge["devices"]) - 1
            charge["ai_result"] = ai
            logger.info(f"Lastwechsel: Neues separates Gerät #{next_num} ({new_dev['name']}) angelegt ({w:.1f} W).")
            return jsonify({"status": "ok", "device": new_dev})

        elif action == "same_device":
            idx = charge["current_device_idx"]
            devs = charge["devices"]
            if 0 <= idx < len(devs):
                cur_d = devs[idx]
                if cur_d.get("key") in DEVICE_PROFILES:
                    DeviceAI.learn_from_feedback(cur_d["key"], charge.get("power_history", []), shelly["watt"])
                logger.info(f"Lastwechsel: Gleiches Gerät bestätigt ({cur_d['name']}). Dynamik gelernt.")
            return jsonify({"status": "ok", "state": "same_device_confirmed"})

    return jsonify({"status": "error"}), 400

@app.route('/add_new_device', methods=['POST'])
def add_new_device():
    with lock:
        now = time.time()
        next_num = len(charge["devices"]) + 1
        w = shelly["watt"]
        charge["power_history"] = [(now, w)] if w > 0.1 else []
        ai = DeviceAI.classify(charge["power_history"])
        s_key = ai.get("suggested_key", "phone")
        prof = DEVICE_PROFILES.get(s_key, DEVICE_PROFILES["phone"])
        
        new_dev = new_device_entry(next_num, s_key)
        new_dev["raw_name"] = prof["name"]
        new_dev["name"] = f"Gerät {next_num}: {prof['name']}"
        new_dev["icon"] = prof["icon"]
        new_dev["mode"] = prof["mode"]
        new_dev["is_battery"] = prof["is_battery"]
        new_dev["nominal_wh"] = prof["nominal_wh"]
        new_dev["user_confirmed"] = False
        
        charge["devices"].append(new_dev)
        charge["current_device_idx"] = len(charge["devices"]) - 1
        charge["ai_result"] = ai
        logger.info(f"➕ Neues Gerät #{next_num} ({new_dev['name']}) manuell hinzugefügt.")
        return jsonify({"status": "ok", "device": new_dev})

@app.route('/merge_device', methods=['POST'])
def merge_device():
    data = request.get_json() or {}
    target_idx = data.get("target_idx", 0)
    with lock:
        curr_idx = charge["current_device_idx"]
        devs = charge["devices"]
        if curr_idx > 0 and 0 <= target_idx < len(devs) and target_idx != curr_idx:
            cur_d = devs.pop(curr_idx)
            tgt_d = devs[target_idx]
            tgt_d["wh"] = tgt_d.get("wh", 0.0) + cur_d.get("wh", 0.0)
            tgt_d["flow_duration_sec"] = tgt_d.get("flow_duration_sec", 0.0) + cur_d.get("flow_duration_sec", 0.0)
            tgt_d["idle_duration_sec"] = tgt_d.get("idle_duration_sec", 0.0) + cur_d.get("idle_duration_sec", 0.0)
            tgt_d["duration_sec"] = tgt_d["flow_duration_sec"] + tgt_d["idle_duration_sec"]
            if tgt_d["flow_duration_sec"] > 0:
                tgt_d["avg_flow_w"] = (tgt_d["wh"] * 3600.0) / tgt_d["flow_duration_sec"]
            
            c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
            vat_rate = c_info["rate"]
            tgt_d["cost_netto"] = (tgt_d["wh"] / 1000.0) * STROMPREIS_PER_KWH
            tgt_d["vat_amount"] = tgt_d["cost_netto"] * (vat_rate / 100.0)
            tgt_d["cost_brutto"] = tgt_d["cost_netto"] + tgt_d["vat_amount"]
            tgt_d["cost"] = tgt_d["cost_brutto"]
            
            charge["current_device_idx"] = target_idx
            charge["power_history"] = [(time.time(), shelly["watt"])] if shelly["watt"] > 0.1 else []
            logger.info(f"🔁 Gerät {cur_d['name']} in Gerät {tgt_d['name']} zusammengeführt!")
            return jsonify({"status": "ok"})
    return jsonify({"status": "error"}), 400

# =====================================================================
# 80% & 100% AKKUSCHUTZ ENDPOINT
# =====================================================================
@app.route('/battery_action', methods=['POST'])
def battery_action():
    data = request.get_json() or {}
    action = data.get("action")  # "continue_100" | "finish"
    now = time.time()

    with lock:
        charge["battery_modal"] = None
        idx = charge["current_device_idx"]
        devs = charge["devices"]
        cur_d = devs[idx] if 0 <= idx < len(devs) else None

        if action == "continue_100" and cur_d:
            cur_d["charge_to_100"] = True
            charge["active"] = True
            charge["paused"] = False
            charge["unplug_cooldown_until"] = now + 10.0
            charge["last_wh_time"] = now
            relay_control(True)
            logger.info(f"🔋 80% Akkuschutz freigegeben -> Lade weiter bis 100% auf {cur_d['name']}.")
            return jsonify({"status": "ok", "charge_to_100": True})
        elif action == "finish":
            return logout()

    return jsonify({"status": "error"}), 400

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

@app.route('/switch_active_device', methods=['POST'])
def switch_active_device():
    data = request.get_json() or {}
    dev_idx = int(data.get("device_idx", 0))
    with lock:
        devs = charge["devices"]
        if 0 <= dev_idx < len(devs):
            charge["current_device_idx"] = dev_idx
            cur_d = devs[dev_idx]
            logger.info(f"👉 Aktives Gerät proaktiv gewechselt auf Gerät #{cur_d['num']} ({cur_d['name']})!")
            return jsonify({"status": "ok", "current_device_idx": dev_idx, "device": cur_d})
    return jsonify({"status": "error", "message": "Ungültiger Index"}), 400

@app.route('/set_device', methods=['POST'])
def set_device():
    data = request.get_json() or {}
    key = data.get("key")
    dev_idx = data.get("device_idx", None)

    with lock:
        idx = int(dev_idx) if dev_idx is not None else charge["current_device_idx"]
        if 0 <= idx < len(charge["devices"]):
            dev = charge["devices"][idx]
            if key in DEVICE_PROFILES:
                prof = DEVICE_PROFILES[key]
                dev["key"] = prof["key"]
                dev["raw_name"] = prof["name"]
                dev["name"] = f"Gerät {dev['num']}: {prof['name']}"
                dev["icon"] = prof["icon"]
                dev["mode"] = prof["mode"]
                dev["is_battery"] = prof["is_battery"]
                dev["nominal_wh"] = prof["nominal_wh"]
                dev["user_confirmed"] = True
                logger.info(f"Gerät #{dev['num']} manuell auf '{prof['name']}' ({prof['mode']}) gesetzt.")
                return jsonify({"status": "ok", "device": dev})
    return jsonify({"status": "error"}), 400

@app.route('/assistant_chat', methods=['POST'])
def assistant_chat():
    data = request.get_json() or {}
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"status": "error", "message": "Leere Nachricht"}), 400

    with lock:
        bot_text, action, payload = AssistantBot.respond(user_msg, charge, shelly)
        
        # Ausführen von Live-Aktionen direkt on-the-fly
        if action == "pause":
            accumulate_energy()
            charge["active"] = False
            charge["paused"] = True
            charge["relay_on"] = False
            shelly["watt"] = 0.0
            shelly["amp"] = 0.0
            relay_control(False)
            logger.info("🤖 [KI-Assistent] Aktion PAUSE ausgeführt.")
        elif action == "resume":
            now = time.time()
            charge["active"] = True
            charge["paused"] = False
            charge["relay_on"] = True
            charge["unplug_cooldown_until"] = now + 15.0
            charge["power_shift_cooldown_until"] = now + 45.0
            charge["last_wh_time"] = now
            relay_control(True)
            logger.info("🤖 [KI-Assistent] Aktion FORTSETZEN ausgeführt.")
        elif action == "switch_device" and payload:
            idx = payload.get("device_idx", 0)
            if 0 <= idx < len(charge["devices"]):
                charge["current_device_idx"] = idx
                logger.info(f"🤖 [KI-Assistent] Aktion WECHSEL AUF GERÄT #{idx+1} ausgeführt.")
        elif action == "charge_100":
            idx = charge["current_device_idx"]
            if 0 <= idx < len(charge["devices"]):
                charge["devices"][idx]["charge_to_100"] = True
                charge["active"] = True
                charge["paused"] = False
                charge["relay_on"] = True
                relay_control(True)
                logger.info(f"🤖 [KI-Assistent] Aktion 100% VOLLLADUNG freigegeben.")

    return jsonify({
        "status": "ok",
        "reply": bot_text,
        "action": action,
        "active_device_idx": charge["current_device_idx"]
    })

@app.route('/feedback_api')
def get_feedback_log():
    return jsonify({
        "status": "ok",
        "count": len(user_feedback_entries),
        "entries": user_feedback_entries
    })

@app.route('/logout', methods=['POST', 'GET'])
def logout():
    with lock:
        accumulate_energy()
        charge["active"] = False
        charge["paused"] = False
        charge["terminated"] = True
        charge["unplug_modal"] = None
        charge["battery_modal"] = None
        charge["power_shift_modal"] = None
        charge["waiting_for_new_plug"] = False
        
        elapsed = get_session_elapsed()
        charge["total_session_seconds"] = elapsed
        charge["last_wh_time"] = None

        # 🧠 KI-LERNEN: Trainiere alle in dieser Sitzung bestätigten Geräte
        for d in charge.get("devices", []):
            if d.get("wh", 0) > 0.05 and d.get("key") in DEVICE_PROFILES:
                DeviceAI.learn_from_feedback(d["key"], charge.get("power_history", []), d.get("avg_flow_w", 0))

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
            "total_flow_seconds": charge["total_flow_seconds"],
            "total_idle_seconds": charge["total_idle_seconds"],
            "time_formatted": fmt_time(elapsed),
            "flow_time_formatted": fmt_time(charge["total_flow_seconds"]),
            "idle_time_formatted": fmt_time(charge["total_idle_seconds"]),
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
    logger.info(f"LOGOUT {invoice_id} | Total: {fmt_time(elapsed)} (Aktiv: {fmt_time(charge['total_flow_seconds'])}, Pausen: {fmt_time(charge['total_idle_seconds'])}) | {brutto:.5f}€")
    return jsonify(report)

@app.route('/download_invoice')
def download_invoice():
    report = charge.get("last_report") or {
        "invoice_id": "SAMPLE", "date": time.strftime('%d.%m.%Y %H:%M'),
        "total_seconds": 0, "total_flow_seconds": 0, "total_idle_seconds": 0,
        "time_formatted": "00:00:00", "flow_time_formatted": "00:00:00", "idle_time_formatted": "00:00:00",
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
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36)
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
            f"Steuerland: <b>{c_name}</b> | Tarif: {STROMPREIS_PER_KWH:.2f} €/kWh (Netto)<br/>"
            f"Gesamtlaufzeit: <b>{report.get('time_formatted', '00:00:00')}</b> (davon aktiv Strom gezogen: <b>{report.get('flow_time_formatted', '00:00:00')}</b> | Pausen/Standby: <b>{report.get('idle_time_formatted', '00:00:00')}</b>)", ms))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563eb"), spaceAfter=12))

        tbl = [["Pos", "Gerät & Modus", "Ladezeit", "Ø Leistung", "Energie", "Netto (€)", f"{vat_name} ({vat_rate:.1f}%)", "Brutto (€)"]]
        for i, d in enumerate(report.get("devices", []), 1):
            m_label = "Akku" if d.get("is_battery") else "Dauerbetrieb"
            d_netto = d.get("cost_netto", (d.get("wh", 0) / 1000.0) * STROMPREIS_PER_KWH)
            d_vat = d.get("vat_amount", d_netto * (vat_rate / 100.0))
            d_brutto = d.get("cost_brutto", d_netto + d_vat)
            avg_w = d.get("avg_flow_w", (d.get("wh", 0)*3600.0/max(1, d.get("flow_duration_sec", 1))))
            tbl.append([
                str(i),
                f"{d.get('name','Gerät')} ({m_label})",
                fmt_time(d.get("flow_duration_sec", d.get("duration_sec", 0))),
                f"{avg_w:.1f} W",
                f"{d.get('wh', 0):.3f} Wh",
                f"{d_netto:.5f} €",
                f"{d_vat:.5f} €",
                f"{d_brutto:.5f} €"
            ])
            
        t = Table(tbl, colWidths=[20, 140, 55, 55, 65, 60, 65, 64])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5f9")),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
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
    elapsed = get_session_elapsed()
    c_info = COUNTRY_VAT_RATES.get(charge["selected_country"], COUNTRY_VAT_RATES["DE"])
    return jsonify({
        "shelly": dict(shelly),
        "charge_active": charge["active"],
        "charge_paused": charge["paused"],
        "unplug_modal": charge["unplug_modal"],
        "battery_modal": charge["battery_modal"],
        "power_shift_modal": charge["power_shift_modal"],
        "unplug_cooldown_until": charge["unplug_cooldown_until"],
        "flow_continuous_seconds": charge["flow_continuous_seconds"],
        "waiting_for_new_plug": charge["waiting_for_new_plug"],
        "target_is_new": charge.get("target_is_new", True),
        "session_elapsed": round(elapsed, 1),
        "total_flow_seconds": charge["total_flow_seconds"],
        "total_idle_seconds": charge["total_idle_seconds"],
        "country": c_info,
        "total_wh": charge["total_wh"],
        "cost_netto": charge["total_cost_netto"],
        "vat_amount": charge["total_vat_amount"],
        "cost_brutto": charge["total_cost_brutto"],
        "devices": charge["devices"],
        "ai_result": charge["ai_result"],
        "ai_learned_models": DeviceAI.learned_models,
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
            charge["active"] = False
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


ADMIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Admin Cockpit – Smart Power Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
:root{--bg:#090d16;--card:#111827;--card-border:#1f2937;--text:#f8fafc;--muted:#94a3b8;--blue:#3b82f6;--green:#10b981;--red:#ef4444}
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{background:var(--bg);color:var(--text);padding:16px;display:flex;justify-content:center}
.wrap{max-width:960px;width:100%}
.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid var(--card-border)}
.title{font-size:20px;font-weight:800}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.cstat{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:16px}
.cstat-l{font-size:11px;text-transform:uppercase;color:var(--muted);font-weight:700}
.cstat-v{font-size:22px;font-weight:800;margin-top:4px;color:var(--text)}
.tabs{display:flex;gap:8px;margin-bottom:16px;border-bottom:1px solid var(--card-border);padding-bottom:8px}
.tab{background:transparent;border:1px solid var(--card-border);color:var(--muted);padding:8px 16px;border-radius:12px;cursor:pointer;font-weight:700;font-size:13px}
.tab.active{background:var(--blue);color:#fff;border-color:var(--blue)}
.pane{display:none;background:var(--card);border:1px solid var(--card-border);border-radius:18px;padding:20px}
.pane.active{display:block}
.tbl{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}
.tbl th{text-align:left;padding:10px 8px;background:#0f172a;color:var(--muted);font-weight:700;border-bottom:1px solid var(--card-border)}
.tbl td{padding:10px 8px;border-bottom:1px solid var(--card-border)}
.btn-act{padding:8px 14px;border-radius:10px;border:none;cursor:pointer;font-weight:700;font-size:12px}
.btn-on{background:var(--green);color:#fff}
.btn-off{background:var(--red);color:#fff}
.chip{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700}
</style></head><body>
<div class="wrap">
  <div class="hdr">
    <div>
      <div class="title">⚙️ Admin Cockpit</div>
      <div style="font-size:12px;color:var(--muted)">Station ID: {{ device_id }} | Physischer Token: <code>{{ physical_token }}</code></div>
    </div>
    <div style="display:flex;gap:8px">
      <a href="/scan/{{ physical_token }}" class="btn-act" style="background:#1f2937;color:#fff;text-decoration:none;display:inline-flex;align-items:center;gap:6px">📲 Zur Station</a>
    </div>
  </div>

  <div class="grid4">
    <div class="cstat"><div class="cstat-l">Umsatz Heute</div><div class="cstat-v">{{ "%.4f"|format(today_revenue) }} €</div></div>
    <div class="cstat"><div class="cstat-l">Umsatz Gesamt</div><div class="cstat-v">{{ "%.4f"|format(total_revenue) }} €</div></div>
    <div class="cstat"><div class="cstat-l">Verbrauch Heute</div><div class="cstat-v">{{ "%.2f"|format(today_wh) }} Wh</div></div>
    <div class="cstat"><div class="cstat-l">Sitzungen Gesamt</div><div class="cstat-v">{{ total_sessions }}</div></div>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="showTab('tabCtrl')">⚡ Live-Steuerung</button>
    <button class="tab" onclick="showTab('tabHist')">📜 Lade-Historie ({{ history_records|length }})</button>
    <button class="tab" onclick="showTab('tabChat')">💬 KI-Chat & Feedback-Log</button>
  </div>

  <!-- PANE 1: LIVE STEUERUNG -->
  <div id="tabCtrl" class="pane active">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <div style="font-size:16px;font-weight:800">Shelly Relais-Status</div>
        <div style="font-size:12px;color:var(--muted)">Aktuelle Leistung: <b>{{ "%.1f"|format(live_watt) }} W</b> | Strom: <b>{{ "%.3f"|format(live_amp) }} A</b> | Spannung: <b>{{ "%.1f"|format(live_volt) }} V</b></div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn-act btn-on" onclick="overrideRelay('force_on')">⚡ Relais EIN (Force ON)</button>
        <button class="btn-act btn-off" onclick="overrideRelay('force_off')">⏹️ Relais AUS (Force OFF)</button>
      </div>
    </div>
  </div>

  <!-- PANE 2: HISTORIE -->
  <div id="tabHist" class="pane">
    <div style="font-size:16px;font-weight:800;margin-bottom:12px">Abgeschlossene Ladesitzungen</div>
    <table class="tbl">
      <thead><tr><th>Rechnungs-ID</th><th>Datum</th><th>Dauer</th><th>Verbrauch</th><th>Land</th><th>Betrag</th></tr></thead>
      <tbody>
        {% for r in history_records %}
        <tr>
          <td><b>{{ r.get('invoice_id', '-') }}</b></td>
          <td>{{ r.get('date', '-') }}</td>
          <td>{{ r.get('time_formatted', '-') }}</td>
          <td>{{ "%.2f"|format(r.get('total_wh', 0)) }} Wh</td>
          <td>{{ r.get('country_flag', '') }} {{ r.get('country_name', '-') }}</td>
          <td><b style="color:var(--green)">{{ "%.4f"|format(r.get('total_cost_brutto', 0)) }} €</b></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- PANE 3: KI-CHAT & FEEDBACK -->
  <div id="tabChat" class="pane">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div style="font-size:16px;font-weight:800">Gesammelte Nutzer-Fragen & Feedback</div>
      <button class="btn-act" style="background:#2563eb;color:#fff" onclick="window.open('/feedback_api','_blank')">📥 JSON Exportieren</button>
    </div>
    <div id="feedbackLogContainer" style="font-size:12px;color:var(--muted)">Lade Feedback-Log...</div>
  </div>
</div>

<script>
function showTab(id){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById(id).classList.add('active');
  if(id === 'tabChat') loadFeedback();
}

function overrideRelay(act){
  fetch('/admin_api/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:act})})
    .then(r=>r.json()).then(d=>alert(d.message || 'Ausgeführt'));
}

function loadFeedback(){
  fetch('/feedback_api').then(r=>r.json()).then(d=>{
    var c = document.getElementById('feedbackLogContainer');
    if(!d.entries || d.entries.length === 0){
      c.innerHTML = '<i>Bisher keine Chat-Interaktionen oder Feedback vorhanden.</i>';
      return;
    }
    var html = '<table class="tbl"><thead><tr><th>Zeit</th><th>Nutzer-Nachricht</th><th>KI-Antwort / Ausgeführte Aktion</th><th>Live-Telemetrie</th></tr></thead><tbody>';
    d.entries.forEach(function(e){
      var ctx = e.context || {};
      html += '<tr>' +
        '<td style="white-space:nowrap;color:var(--muted)">' + (e.datetime || '-') + '</td>' +
        '<td><b>' + (e.user_message || '-') + '</b></td>' +
        '<td style="color:#93c5fd">' + (e.bot_response || '-') + (e.executed_action ? '<br/><span class="chip" style="background:#065f46;color:#34d399;margin-top:4px">Aktion: ' + e.executed_action + '</span>' : '') + '</td>' +
        '<td style="font-family:monospace;font-size:11px">' + (ctx.watt || 0) + ' W | ' + (ctx.wh || 0) + ' Wh<br/>' + (ctx.active_device || '-') + '</td>' +
        '</tr>';
    });
    html += '</tbody></table>';
    c.innerHTML = html;
  });
}
</script>
</body></html>"""


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
.pill-warn{background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}
.pill-on .dot{background:#059669;box-shadow:0 0 8px rgba(5,150,105,.7)}
.pill-p .dot{background:#d97706}
.pill-off .dot{background:#94a3b8}

/* GERÄTE-TABS ANZEIGE (Gerät 1, Gerät 2...) */
.dev-pills{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px;margin-bottom:10px;scrollbar-width:none}
.dev-pill{font-size:11px;font-weight:700;padding:4px 10px;border-radius:20px;background:#f1f5f9;color:var(--muted);white-space:nowrap;border:1px solid var(--border)}
.dev-pill.active{background:#eff6ff;color:var(--blue);border-color:#bfdbfe}

/* BANNER: WARTE AUF EINSTECKEN / STROMERKENNUNG */
.waiting-banner{background:#eff6ff;border:2px dashed #3b82f6;border-radius:16px;padding:14px;margin-bottom:12px;text-align:center;animation:pulsebox 1.5s infinite}
@keyframes pulsebox{0%,100%{background:#eff6ff;border-color:#3b82f6}50%{background:#dbeafe;border-color:#1d4ed8}}
.wb-title{font-size:14.5px;font-weight:800;color:#1e40af;margin-bottom:4px}
.wb-sub{font-size:12px;color:#3b82f6;line-height:1.4}

/* DYNAMISCHES MULTI-SEGMENT SCHAUBILD (GERÄTE 1, 2, 3... & PAUSEN) */
.timeline-card{background:#f8fafc;border:1px solid var(--border);border-radius:18px;padding:14px;margin-bottom:12px;text-align:left}
.timeline-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.timeline-title{font-size:11px;font-weight:800;text-transform:uppercase;color:var(--muted);letter-spacing:.5px}
.timeline-stat{font-size:11px;font-weight:700;color:var(--text)}

.time-bar{height:14px;background:#e2e8f0;border-radius:12px;display:flex;overflow:hidden;margin-bottom:10px;box-shadow:inset 0 1px 2px rgba(0,0,0,.08)}
.tb-seg{height:100%;transition:width .4s ease}
.tb-pause{background:#cbd5e1;background-image:repeating-linear-gradient(45deg,#cbd5e1,#cbd5e1 4px,#94a3b8 4px,#94a3b8 8px)}

.time-chips-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px}
.time-chip{background:#ffffff;border:1px solid var(--border);border-radius:10px;padding:6px 8px;display:flex;align-items:center;justify-content:space-between}
.time-chip-left{display:flex;align-items:center;gap:6px}
.time-chip-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.time-chip-val{font-weight:800;font-family:ui-monospace,monospace;color:var(--text)}

/* KI-VORSCHLAG & GERAETE-LEISTE */
.ai-box{background:#f8fafc;border:1.5px solid var(--border);border-radius:18px;padding:14px;margin-bottom:12px;text-align:left;transition:border-color .2s}
.ai-box.confirmed{border-color:#bbf7d0;background:#f0fdf4}
.ai-box.learned{border-color:#bfdbfe;background:#f0f7ff}
.ai-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.ai-tag{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;padding:3px 8px;border-radius:6px;background:#eff6ff;color:var(--blue)}
.ai-box.confirmed .ai-tag{background:#dcfce7;color:#15803d}
.ai-box.learned .ai-tag{background:#dbeafe;color:#1e40af}
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
.modal{display:none;position:fixed;inset:0;background:rgba(9,13,22,.82);backdrop-filter:blur(6px);z-index:999;padding:16px;align-items:center;justify-content:center}
.mbox{background:#fff;border-radius:24px;padding:24px 20px;text-align:left;max-width:390px;width:100%;max-height:90vh;overflow-y:auto;animation:pop .2s ease-out}
@keyframes pop{from{transform:scale(.92);opacity:0}to{transform:scale(1);opacity:1}}
.m-sec-title{font-size:11px;font-weight:800;text-transform:uppercase;color:var(--muted);letter-spacing:.5px;margin:12px 0 6px}

.dev-option{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border:1px solid var(--border);border-radius:12px;margin-bottom:6px;cursor:pointer;background:#ffffff;transition:background .15s, border-color .15s}
.dev-option:hover{background:#f8fafc;border-color:var(--blue)}
.dev-opt-left{display:flex;align-items:center;gap:10px}
.dev-opt-ico{font-size:22px}
.dev-opt-nm{font-size:13.5px;font-weight:700;color:var(--text)}
.dev-opt-sub{font-size:11px;color:var(--muted)}
.dev-opt-tag{font-size:10px;font-weight:700;padding:3px 7px;border-radius:6px}

.reuse-card{border:1.5px solid #bfdbfe;background:#eff6ff;border-radius:14px;padding:12px;margin-bottom:8px;cursor:pointer;transition:transform .1s, background .15s}
.reuse-card:hover{background:#dbeafe}
.reuse-card:active{transform:scale(.98)}
.reuse-title{font-size:13.5px;font-weight:800;color:#1e40af;display:flex;align-items:center;justify-content:space-between}
.reuse-meta{font-size:11.5px;color:#3b82f6;margin-top:3px}

.receipt{display:none}
.rtbl{width:100%;border-collapse:collapse;margin:14px 0;font-size:11.5px}
.rtbl th{background:#f1f5f9;padding:8px 4px;font-size:10px;text-transform:uppercase;color:var(--muted)}
.rtbl td{padding:8px 4px;border-bottom:1px solid var(--border)}
.tbox{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px;padding:14px;text-align:right;margin-top:14px}
.ein{width:100%;padding:11px;border:1px solid var(--border);border-radius:10px;font-size:13.5px;margin-bottom:8px}

/* FLOATING KI-ASSISTENT & CHAT-DRAWER */
.fab-chat{position:fixed;bottom:20px;right:20px;background:#2563eb;color:#ffffff;border:none;border-radius:30px;padding:11px 17px;font-size:13px;font-weight:800;box-shadow:0 8px 24px rgba(37,99,235,.35);cursor:pointer;z-index:999;display:flex;align-items:center;gap:8px;transition:transform .2s,box-shadow .2s}
.fab-chat:hover{transform:translateY(-2px);box-shadow:0 12px 28px rgba(37,99,235,.45)}
.fab-dot{width:8px;height:8px;background:#4ade80;border-radius:50%;box-shadow:0 0 6px #4ade80}
.chat-drawer{position:fixed;bottom:76px;right:18px;width:360px;max-width:calc(100vw - 36px);height:480px;max-height:calc(100vh - 120px);background:#ffffff;border-radius:22px;box-shadow:0 20px 50px rgba(0,0,0,.22);border:1.5px solid #e2e8f0;display:none;flex-direction:column;z-index:1000;overflow:hidden;animation:pop .2s ease-out}
.chat-hdr{background:#0f172a;color:#ffffff;padding:12px 16px;display:flex;align-items:center;justify-content:space-between}
.chat-title{font-size:13.5px;font-weight:800;display:flex;align-items:center;gap:6px}
.chat-body{flex:1;padding:12px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;background:#f8fafc}
.chat-msg{max-width:86%;padding:8px 12px;border-radius:14px;font-size:12px;line-height:1.45;word-wrap:break-word}
.chat-msg.bot{background:#ffffff;color:#0f172a;align-self:flex-start;border:1px solid #e2e8f0;border-bottom-left-radius:3px}
.chat-msg.user{background:#2563eb;color:#ffffff;align-self:flex-end;border-bottom-right-radius:3px}
.chat-chips{display:flex;gap:5px;overflow-x:auto;padding:6px 10px;background:#ffffff;border-top:1px solid #e2e8f0;scrollbar-width:none}
.chat-chip{background:#f1f5f9;border:1px solid #cbd5e1;color:#334155;font-size:10.5px;font-weight:700;padding:4px 8px;border-radius:12px;white-space:nowrap;cursor:pointer;transition:background .15s}
.chat-chip:hover{background:#e2e8f0}
.chat-input-bar{display:flex;padding:8px 10px;background:#ffffff;border-top:1px solid #e2e8f0;gap:6px}
.chat-in{flex:1;padding:8px 12px;border:1.5px solid #cbd5e1;border-radius:20px;font-size:12px;outline:none;font-family:inherit}
.chat-in:focus{border-color:#2563eb}
.chat-send{background:#2563eb;color:#fff;border:none;border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:13px;flex-shrink:0}
</style></head><body>

<!-- DIALOG 1: WURDE GERÄT ABGESTECKT ODER SCHLAFMODUS? -->
<div id="modalAskUnplug" class="modal">
<div class="mbox" style="text-align:center;border:2px solid var(--red)">
  <div style="font-size:42px;margin-bottom:6px">🔌💤</div>
  <div style="font-size:18px;font-weight:800;margin-bottom:6px;color:#991b1b">Stromfluss auf 0.0 A abgefallen</div>
  <p style="font-size:13px;color:var(--muted);margin-bottom:18px;line-height:1.4">
    Das Strom-Zählen wurde pausiert.<br/>
    <b>Wurde das Gerät ausgesteckt oder ist es im Standby / Schlafmodus?</b>
  </p>
  
  <div style="display:flex;flex-direction:column;gap:10px">
    <button class="btn bp" style="background:#059669;padding:14px;font-size:14px" onclick="handleUnplugResponse('no_resume')">
      💤 Gleiches Gerät (Standby / Weiterladen)
    </button>
    <button class="btn bs" style="background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5;padding:14px;font-size:14px;font-weight:800" onclick="handleUnplugResponse('yes_unplugged')">
      ✅ Ja, Gerät ist ausgesteckt
    </button>
  </div>
</div>
</div>

<!-- DIALOG 2: FRÜHERES GERÄT WIEDERVERWENDEN ODER NEUES GERÄT? -->
<div id="modalAskNextDevice" class="modal">
<div class="mbox" style="text-align:left;border:2px solid var(--blue)">
  <div style="text-align:center;margin-bottom:12px">
    <div style="font-size:38px;margin-bottom:4px">🔄⚡</div>
    <div style="font-size:17.5px;font-weight:800;color:#1e40af">Gerätewechsel</div>
    <p style="font-size:12.5px;color:var(--muted);margin-top:2px">
      Welches Gerät möchtest du als Nächstes einstecken?
    </p>
  </div>

  <div class="m-sec-title">➕ Neues Gerät auswählen:</div>
  <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px">
    <div class="dev-option" onclick="handleUnplugResponse('prep_new_device', null, 'phone')">
      <div class="dev-opt-left">
        <span class="dev-opt-ico">📱</span>
        <div>
          <div class="dev-opt-nm">Smartphone / Tablet / Akku</div>
          <div class="dev-opt-sub">Akku-Modus (80%/100% Schutz)</div>
        </div>
      </div>
      <span class="dev-opt-tag" style="background:#d1fae5;color:#065f46">Wählen</span>
    </div>

    <div class="dev-option" onclick="handleUnplugResponse('prep_new_device', null, 'laptop')">
      <div class="dev-opt-left">
        <span class="dev-opt-ico">💻</span>
        <div>
          <div class="dev-opt-nm">Laptop / Ultrabook</div>
          <div class="dev-opt-sub">Akku-Modus (65 Wh)</div>
        </div>
      </div>
      <span class="dev-opt-tag" style="background:#d1fae5;color:#065f46">Wählen</span>
    </div>

    <div class="dev-option" onclick="handleUnplugResponse('prep_new_device', null, 'ebike')">
      <div class="dev-opt-left">
        <span class="dev-opt-ico">🚲</span>
        <div>
          <div class="dev-opt-nm">E-Bike / Pedelec (Standard)</div>
          <div class="dev-opt-sub">Großakku (500 Wh)</div>
        </div>
      </div>
      <span class="dev-opt-tag" style="background:#d1fae5;color:#065f46">Wählen</span>
    </div>

    <div class="dev-option" onclick="handleUnplugResponse('prep_new_device', null, 'ebike_fast')">
      <div class="dev-opt-left">
        <span class="dev-opt-ico">⚡</span>
        <div>
          <div class="dev-opt-nm">E-Bike Schnelllader</div>
          <div class="dev-opt-sub">Schnellladung (750 Wh)</div>
        </div>
      </div>
      <span class="dev-opt-tag" style="background:#d1fae5;color:#065f46">Wählen</span>
    </div>

    <div class="dev-option" onclick="handleUnplugResponse('prep_new_device', null, 'lamp')">
      <div class="dev-opt-left">
        <span class="dev-opt-ico">💡</span>
        <div>
          <div class="dev-opt-nm">Lampe / Dauerbetrieb</div>
          <div class="dev-opt-sub">Dauerhafter Verbrauch</div>
        </div>
      </div>
      <span class="dev-opt-tag" style="background:#f1f5f9;color:var(--text)">Wählen</span>
    </div>
  </div>

  <div id="reusableSec" style="display:none">
    <div class="m-sec-title">🔁 Bisheriges Gerät fortsetzen:</div>
    <div id="reusableDevicesContainer"></div>
  </div>

  <button class="btn bs" style="background:#f1f5f9;color:var(--text);padding:11px;font-size:13px;margin-top:8px" onclick="handleUnplugResponse('finish')">
    🧾 Sitzung beenden & Quittung
  </button>
</div>
</div>

<!-- DIALOG 3: DRASTISCHER LASTWECHSEL ERKANNT (NEUES GERÄT ODER DISPLAY/STANDBY?) -->
<div id="modalPowerShift" class="modal">
<div class="mbox" style="text-align:center;border:2px solid var(--amber)">
  <div style="font-size:42px;margin-bottom:6px">⚡📈</div>
  <div style="font-size:18px;font-weight:800;margin-bottom:6px;color:#b45309">Lastwechsel erkannt</div>
  <p style="font-size:13px;color:var(--muted);margin-bottom:12px;line-height:1.4">
    Die Leistung hat sich verändert von <b id="psFromW" style="color:var(--text)">-- W</b> auf <b id="psToW" style="color:var(--blue)">-- W</b>.<br/>
    Wurde ein neues / anderes Gerät eingesteckt?
  </p>

  <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:8px 12px;margin-bottom:14px;font-size:12px;color:#92400e;display:flex;align-items:center;justify-content:center;gap:6px;font-weight:700">
    <span>⏳ Fortsetzung als gleiches Gerät in</span>
    <span id="psCountdown" style="font-size:14px;color:#b45309;font-family:ui-monospace,monospace;font-weight:800">20s</span>
  </div>
  
  <div style="display:flex;flex-direction:column;gap:10px">
    <button class="btn bp" style="background:var(--blue);padding:13px;font-size:14px" onclick="handlePowerShift('new_device')">
      🔄 Ja, neues separates Gerät erfassen
    </button>
    <button class="btn bs" style="background:#f8fafc;color:var(--text);padding:12px;font-size:13.5px;font-weight:700" onclick="handlePowerShift('same_device')">
      ✅ Nein, gleiches Gerät (Lastwechsel / Standby)
    </button>
  </div>
</div>
</div>

<!-- DIALOG: 80% AKKUSCHUTZ ERREICHT -->
<div id="modalBattery80" class="modal">
<div class="mbox" style="text-align:center;border:2px solid #059669">
  <div style="font-size:42px;margin-bottom:6px">🛡️🔋</div>
  <div style="font-size:18px;font-weight:800;margin-bottom:6px;color:#065f46">80% Akkuschutz erreicht!</div>
  <p style="font-size:13px;color:var(--muted);margin-bottom:18px;line-height:1.4">
    Dein Akku ist zu <b>80% voll geladen</b>.<br/>
    Die Steckdose wurde automatisch ausgeschaltet, um die <b>Akkuzellen optimal zu schonen</b>.
  </p>
  
  <div style="display:flex;flex-direction:column;gap:10px">
    <button class="btn bp" style="background:#059669;padding:14px;font-size:14.5px" onclick="handleBatteryAction('continue_100')">
      ⚡ Weiterladen bis 100% voll
    </button>
    <button class="btn bs" style="background:#f1f5f9;color:var(--text);padding:12px;font-size:13.5px" onclick="handleBatteryAction('finish')">
      🧾 Ladevorgang beenden & Quittung
    </button>
  </div>
</div>
</div>

<!-- DIALOG: 100% AKKU VOLLGELADEN -->
<div id="modalBattery100" class="modal">
<div class="mbox" style="text-align:center;border:2px solid #2563eb">
  <div style="font-size:42px;margin-bottom:6px">✅🔋</div>
  <div style="font-size:18px;font-weight:800;margin-bottom:6px;color:#1e40af">100% Akku voll geladen!</div>
  <p style="font-size:13px;color:var(--muted);margin-bottom:18px;line-height:1.4">
    Der Ladevorgang ist <b>vollständig abgeschlossen (100%)</b>.<br/>
    Die Steckdose wurde automatisch stromlos geschaltet.
  </p>
  
  <button class="btn bp" style="background:var(--blue);padding:14px;font-size:15px" onclick="handleBatteryAction('finish')">
    🧾 Beenden & Quittung anzeigen
  </button>
</div>
</div>

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

<!-- MODAL: GERAET / PROFIL WAHLEN -->
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
  <span class="pill pill-warn" id="unplugPill" style="display:none">⚠️ 0.0 A (Standby / Pause)</span>
</div>

<!-- BANNER WENN AUF NEUES GERAET GEWARTET WIRD -->
<div class="waiting-banner" id="waitingPlugBanner" style="display:none">
  <div class="wb-title" id="waitingPlugTitle">🔌 Steckdose aktiv – Gerät einstecken</div>
  <div class="wb-sub" id="waitingPlugSub">Stecke das Gerät ein. Sobald Strom fließt, läuft die Erfassung automatisch weiter!</div>
</div>

<!-- GERÄTE-TABS DER AKTUELLEN SITZUNG -->
<div class="dev-pills" id="devPillsContainer"></div>

<!-- DYNAMISCHES MULTI-SEGMENT SCHAUBILD -->
<div class="timeline-card" id="timelineCard">
  <div class="timeline-hdr">
    <span class="timeline-title">📊 Dynamischer Lade- & Pausen-Zeitstrahl</span>
    <span class="timeline-stat" id="timelineFlowPct">0% Strom aktiv</span>
  </div>
  <div class="time-bar" id="timeBarVisual"></div>
  <div class="time-chips-grid" id="timeChipsGrid"></div>
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
      <div class="ai-title" id="aiTitle">Gerät 1: Lampe / Beleuchtung</div>
      <span class="ai-mode-badge badge-cont" id="aiModeBadge">Dauerbetrieb</span>
    </div>
  </div>
  <div class="ai-reason" id="aiReason">Analyse des Stromflusses läuft...</div>
  <div class="ai-actions">
    <button class="btn-confirm" id="btnConfirm" onclick="confirmSuggestion()">✅ Bestätigen & KI trainieren</button>
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
  <div class="st"><div class="st-l">Gesamtlaufzeit (Server-Master)</div><div class="st-v" id="timer">00:00:00</div></div>
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
  <button class="btn bp" id="btnStart" onclick="doStart()" style="background:#059669">▶️ Fortsetzen & Laden</button>
  <button class="btn bs" id="btnPause" onclick="doStop()">⏸️ Pause einlegen</button>
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

<div class="timeline-card" style="margin-bottom:12px;background:#f8fafc">
  <div class="timeline-hdr">
    <span class="timeline-title">⏱️ Gesamtlaufzeit- & Pausenaufteilung</span>
    <span class="timeline-stat" id="rTimelinePct">--</span>
  </div>
  <div class="time-chips-grid" id="rTimeChipsGrid" style="margin-top:6px"></div>
</div>

<table class="rtbl">
  <thead><tr><th>Pos / Gerät</th><th style="text-align:center">Ladezeit</th><th style="text-align:center">Ø W</th><th style="text-align:right">Wh</th><th style="text-align:right">Netto</th><th style="text-align:right">Brutto</th></tr></thead>
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

<!-- FLOATING CHAT BUTTON -->
<button class="fab-chat" id="fabChat" onclick="toggleChatDrawer()">
  <span class="fab-dot"></span>
  💬 KI-Assistent
</button>

<!-- SLIDING CHAT DRAWER -->
<div class="chat-drawer" id="chatDrawer">
  <div class="chat-hdr">
    <div class="chat-title">🤖 <span>Smart Power Bot</span></div>
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:10px;color:#86efac;font-weight:700">🟢 Live</span>
      <button style="background:none;border:none;color:#94a3b8;font-size:18px;cursor:pointer;padding:0" onclick="toggleChatDrawer()">✕</button>
    </div>
  </div>
  
  <div class="chat-body" id="chatBody">
    <div class="chat-msg bot">
      👋 <b>Hallo! Ich bin dein intelligenter Ladeassistent.</b><br/>
      Ich überwache deinen Stromfluss in Echtzeit. Frag mich z. B.:<br/>
      • <i>„Wie lange lädt der Akku noch?“</i><br/>
      • <i>„Warum schwankt die Watt-Zahl?“</i><br/>
      • Oder sage mir einfach <i>„Pausiere“</i> oder <i>„Auf 100% laden“</i>!
    </div>
  </div>

  <div class="chat-chips">
    <span class="chat-chip" onclick="sendQuickChip('Wie lange lädt mein Akku noch?')">🔋 Restdauer?</span>
    <span class="chat-chip" onclick="sendQuickChip('Pausiere bitte mal kurz')">⏸️ Pause</span>
    <span class="chat-chip" onclick="sendQuickChip('Setze das Laden fort')">▶️ Fortsetzen</span>
    <span class="chat-chip" onclick="sendQuickChip('Warum schwankt die Watt-Anzeige?')">⚡ Watt-Schwankung?</span>
    <span class="chat-chip" onclick="sendQuickChip('Lade meinen Akku bis 100% voll')">⚡ 100% laden</span>
    <span class="chat-chip" onclick="sendQuickChip('Wie wechsle ich das Gerät?')">🔄 Gerät wechseln?</span>
    <span class="chat-chip" onclick="sendQuickChip('Was kostet der Strom bisher?')">💶 Kosten?</span>
  </div>

  <div class="chat-input-bar">
    <input type="text" id="chatIn" class="chat-in" placeholder="Frage stellen oder Befehl eingeben..." onkeydown="if(event.key==='Enter') sendChatMessage()">
    <button class="chat-send" onclick="sendChatMessage()">➔</button>
  </div>
</div>

<script>
var done = false, lastR = null;
var localElapsed = 0;
var sessionStarted = false;
var localTimerInterval = null;
var lastActionLocalTime = 0;

var STROMPREIS_NETTO = 0.35;
var currentCountry = { code: 'DE', name: 'Deutschland', flag: '🇩🇪', vat_name: 'MwSt.', rate: 19.0 };
var currentDevice = { num: 1, name: 'Gerät 1: Lampe / Beleuchtung', mode: 'continuous', is_battery: false, nominal_wh: 0, user_confirmed: false };
var latestAiSuggestion = null;
var allRecordedDevices = [];

async function requestWakeLock(){
  try {
    if('wakeLock' in navigator){ await navigator.wakeLock.request('screen'); }
  } catch(e){}
}

document.addEventListener('visibilitychange', function(){
  if(!document.hidden){
    requestWakeLock();
    poll();
  }
});

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

function startContinuousTimer(){
  if (localTimerInterval) return;
  localTimerInterval = setInterval(function(){
    if (sessionStarted && !done) {
      localElapsed += 1;
      updateTimerDisplay();
    }
  }, 1000);
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
  lastActionLocalTime = Date.now();
  post('/reset_session').then(function(){
    done = false;
    lastR = null;
    localElapsed = 0;
    sessionStarted = false;
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
    document.getElementById('timeBarVisual').innerHTML = '';
    document.getElementById('timeChipsGrid').innerHTML = '';
    document.getElementById('waitingPlugBanner').style.display = 'none';
    hideM('modalAskUnplug');
    hideM('modalAskNextDevice');
    hideM('modalPowerShift');
    hideM('modalBattery80');
    hideM('modalBattery100');
    poll();
  });
}

function doStart(){
  if(done) return;
  lastActionLocalTime = Date.now();
  requestWakeLock();
  sessionStarted = true;
  startContinuousTimer();
  document.getElementById('sPill').className = 'pill pill-on';
  document.getElementById('sTxt').innerText = 'Aktiv';
  post('/start').then(function(){
    setTimeout(poll, 600);
    setTimeout(poll, 1400);
    setTimeout(poll, 2200);
  });
}

function doStop(){
  if(done) return;
  lastActionLocalTime = Date.now();
  document.getElementById('sPill').className = 'pill pill-p';
  document.getElementById('sTxt').innerText = 'Pause';
  post('/stop').then(function(){ poll(); });
}

function devAct(a){
  if(a === 'continue'){
    doStart();
  } else if(a === 'finish'){
    lastActionLocalTime = Date.now();
    post('/logout').then(function(r){ showReceipt(r); });
  }
}

// ABSTECK- & WECHSEL-ANTWORTEN
function handleUnplugResponse(action, devIdx, devKey){
  lastActionLocalTime = Date.now();
  hideM('modalAskUnplug');
  hideM('modalAskNextDevice');
  var payload = { action: action };
  if(action === 'select_existing'){
    payload.device_idx = devIdx;
  }
  if(devKey){
    payload.device_key = devKey;
  }
  post('/unplug_action', payload).then(function(res){
    if(action === 'finish'){
      showReceipt(res);
    } else {
      setTimeout(poll, 300);
      setTimeout(poll, 1000);
    }
  });
}

var powerShiftTimer = null;
var powerShiftSecondsLeft = 20;

function startPowerShiftCountdown(){
  if(powerShiftTimer) return;
  powerShiftSecondsLeft = 20;
  var el = document.getElementById('psCountdown');
  if(el) el.innerText = powerShiftSecondsLeft + 's';

  powerShiftTimer = setInterval(function(){
    powerShiftSecondsLeft -= 1;
    if(el) el.innerText = Math.max(0, powerShiftSecondsLeft) + 's';
    if(powerShiftSecondsLeft <= 0){
      clearInterval(powerShiftTimer);
      powerShiftTimer = null;
      handlePowerShift('same_device');
    }
  }, 1000);
}

function stopPowerShiftCountdown(){
  if(powerShiftTimer){
    clearInterval(powerShiftTimer);
    powerShiftTimer = null;
  }
}

// LASTWECHSEL-ANTWORTEN
function handlePowerShift(action){
  stopPowerShiftCountdown();
  lastActionLocalTime = Date.now();
  hideM('modalPowerShift');
  post('/power_shift_action', { action: action }).then(function(res){
    poll();
  });
}

// 80% & 100% AKKU-AKTIONEN
function handleBatteryAction(action){
  lastActionLocalTime = Date.now();
  hideM('modalBattery80');
  hideM('modalBattery100');
  post('/battery_action', { action: action }).then(function(res){
    if(action === 'finish'){
      showReceipt(res);
    } else {
      poll();
    }
  });
}

function renderReusableDevices(devs){
  var c = document.getElementById('reusableDevicesContainer');
  var sec = document.getElementById('reusableSec');
  if(!c) return;
  c.innerHTML = '';
  var validDevs = (devs || []).filter(function(d){ return (d.wh || 0) > 0.01; });
  if(validDevs.length > 0 && sec){
    sec.style.display = 'block';
  } else if(sec){
    sec.style.display = 'none';
  }
  (devs || []).forEach(function(d, idx){
    if((d.wh || 0) <= 0.01) return;
    var div = document.createElement('div');
    div.className = 'reuse-card';
    div.onclick = function(){ handleUnplugResponse('select_existing', idx); };
    div.innerHTML = '<div class="reuse-title"><span>' + (d.icon || '🔌') + ' ' + (d.name || ('Gerät ' + (idx+1))) + '</span><span style="font-size:11px;background:#bfdbfe;color:#1e40af;padding:2px 8px;border-radius:10px">Fortsetzen</span></div>' +
                    '<div class="reuse-meta">Bisher: <b>' + fs(d.flow_duration_sec || 0) + '</b> Stromfluss · <b>' + (d.wh || 0).toFixed(3) + ' Wh</b></div>';
    c.appendChild(div);
  });
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
  aiTitle.innerText = dev.name || ('Gerät ' + (dev.num || 1));

  if(isBatt){
    aiModeBadge.className = 'ai-mode-badge badge-batt';
    aiModeBadge.innerText = '🔋 Akku-Lademodus';
  } else {
    aiModeBadge.className = 'ai-mode-badge badge-cont';
    aiModeBadge.innerText = '🔌 Dauerbetrieb';
  }

  var isLearned = latestAiSuggestion && latestAiSuggestion.learned_match;

  if(dev.user_confirmed){
    aiBox.className = 'ai-box confirmed';
    aiTag.innerText = '✓ Bestätigt & Gelernt';
    btnConf.style.display = 'none';
  } else if(isLearned){
    aiBox.className = 'ai-box learned';
    aiTag.innerText = '🎯 Aus Muster gelernt (' + (latestAiSuggestion.confidence || 90) + '%)';
    btnConf.style.display = 'inline-block';
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

// DYNAMISCHER MULTI-SEGMENT-BALKEN & CHIPS
function updateDynamicTimeline(devs, totalFlowSec, totalIdleSec, totalElapsedSec){
  var tot = Math.max(1, totalElapsedSec, (totalFlowSec + totalIdleSec));
  var bar = document.getElementById('timeBarVisual');
  var grid = document.getElementById('timeChipsGrid');
  if(!bar || !grid) return;

  bar.innerHTML = '';
  grid.innerHTML = '';

  var flowPct = Math.min(100, Math.round((totalFlowSec / tot) * 100));
  var idlePct = 100 - flowPct;
  document.getElementById('timelineFlowPct').innerText = flowPct + '% Strom aktiv (' + idlePct + '% Standby/Pausen)';

  // 1. Segmente für jedes Gerät
  (devs || []).forEach(function(d, idx){
    var dFlow = d.flow_duration_sec || 0;
    var dPct = ((dFlow / tot) * 100).toFixed(1);
    var color = d.color || '#2563eb';

    if(dFlow > 0){
      var seg = document.createElement('div');
      seg.className = 'tb-seg';
      seg.style.width = dPct + '%';
      seg.style.background = color;
      bar.appendChild(seg);
    }

    var chip = document.createElement('div');
    chip.className = 'time-chip';
    chip.innerHTML = '<div class="time-chip-left"><span class="time-chip-dot" style="background:' + color + '"></span><span>' + (d.icon || '🔌') + ' Gerät ' + (d.num || idx+1) + '</span></div><div class="time-chip-val">' + fs(dFlow) + ' <span style="font-size:9.5px;color:#64748b">(' + dPct + '%)</span></div>';
    grid.appendChild(chip);
  });

  // 2. Segment für Standby / Pausen
  if(totalIdleSec > 0 || totalFlowSec === 0){
    var pausePct = ((totalIdleSec / tot) * 100).toFixed(1);
    var pSeg = document.createElement('div');
    pSeg.className = 'tb-seg tb-pause';
    pSeg.style.width = pausePct + '%';
    bar.appendChild(pSeg);

    var pChip = document.createElement('div');
    pChip.className = 'time-chip';
    pChip.innerHTML = '<div class="time-chip-left"><span class="time-chip-dot" style="background:#94a3b8"></span><span>⏸️ Standby (0 W)</span></div><div class="time-chip-val" style="color:#64748b">' + fs(totalIdleSec) + ' <span style="font-size:9.5px">(' + pausePct + '%)</span></div>';
    grid.appendChild(pChip);
  }
}

function showReceipt(rp){
  done = true;
  lastR = rp;
  document.getElementById('mainC').style.display = 'none';
  document.getElementById('recC').style.display = 'block';

  var vatRate = rp.vat_rate || currentCountry.rate || 19.0;
  var vatName = rp.vat_name || currentCountry.vat_name || 'MwSt.';
  var cName = rp.country_name || currentCountry.name || 'Deutschland';
  var netto = rp.total_cost_netto || 0.0;
  var vatAmt = rp.total_vat_amount || 0.0;
  var brutto = rp.total_cost_brutto || rp.total_cost || 0.0;

  var flowSec = rp.total_flow_seconds || 0;
  var idleSec = rp.total_idle_seconds || 0;
  var totalSec = rp.total_seconds || (flowSec + idleSec) || 1;
  var flowPct = Math.round((flowSec / totalSec) * 100);

  document.getElementById('rCountryName').innerText = cName;
  document.getElementById('rVatRate').innerText = vatRate.toFixed(1);
  document.getElementById('rVatName').innerText = vatName;
  document.getElementById('rTimelinePct').innerText = 'Gesamtdauer: ' + fs(totalSec) + ' · ' + flowPct + '% Stromfluss | ' + (100 - flowPct) + '% Standby';

  var rGrid = document.getElementById('rTimeChipsGrid');
  rGrid.innerHTML = '';
  (rp.devices || []).forEach(function(d, idx){
    var chip = document.createElement('div');
    chip.className = 'time-chip';
    chip.innerHTML = '<div class="time-chip-left"><span class="time-chip-dot" style="background:' + (d.color || '#2563eb') + '"></span><span>' + (d.icon || '🔌') + ' ' + (d.name || ('Gerät ' + (idx+1))) + '</span></div><div class="time-chip-val">' + fs(d.flow_duration_sec || 0) + '</div>';
    rGrid.appendChild(chip);
  });
  var pChip = document.createElement('div');
  pChip.className = 'time-chip';
  pChip.innerHTML = '<div class="time-chip-left"><span class="time-chip-dot" style="background:#94a3b8"></span><span>⏸️ Standby/Pausen</span></div><div class="time-chip-val" style="color:#64748b">' + fs(idleSec) + '</div>';
  rGrid.appendChild(pChip);

  var tb = document.getElementById('recB');
  tb.innerHTML = '';
  (rp.devices || []).forEach(function(d, idx){
    var m = d.is_battery ? 'Akku' : 'Dauerbetrieb';
    var dNetto = d.cost_netto || (d.wh / 1000.0) * STROMPREIS_NETTO;
    var dBrutto = d.cost_brutto || d.cost || (dNetto * (1 + vatRate/100));
    var avgW = d.avg_flow_w || (d.flow_duration_sec > 0 ? (d.wh * 3600 / d.flow_duration_sec) : 0);
    var tr = document.createElement('tr');
    tr.innerHTML = '<td><b>' + (d.icon || '🔌') + ' ' + (d.name || ('Gerät ' + (idx+1))) + '</b><br><span style="font-size:9.5px;color:#64748b">' + m + '</span></td><td style="text-align:center">' + fs(d.flow_duration_sec || d.duration_sec || 0) + '</td><td style="text-align:center">' + avgW.toFixed(1) + ' W</td><td style="text-align:right">' + (d.wh || 0).toFixed(3) + '</td><td style="text-align:right">' + dNetto.toFixed(4) + ' €</td><td style="text-align:right"><b>' + dBrutto.toFixed(4) + ' €</b></td>';
    tb.appendChild(tr);
  });

  document.getElementById('rNetto').innerText = netto.toFixed(5) + ' €';
  document.getElementById('rVatPct').innerText = vatRate.toFixed(1) + '% ' + vatName;
  document.getElementById('rVatAmt').innerText = '+ ' + vatAmt.toFixed(5) + ' €';
  document.getElementById('rCost').innerText = brutto.toFixed(5) + ' €';
  document.getElementById('rWh').innerText = (rp.total_wh || 0).toFixed(4);
  document.getElementById('rKwh').innerText = (rp.total_kwh || 0).toFixed(6);
}

function renderDevicePills(devs, activeIdx){
  var c = document.getElementById('devPillsContainer');
  if(!c) return;
  c.style.display = 'flex';
  c.innerHTML = '';
  (devs || []).forEach(function(d, idx){
    var isCur = (idx === activeIdx);
    var sp = document.createElement('span');
    sp.className = 'dev-pill' + (isCur ? ' active' : '');
    sp.style.cursor = 'pointer';
    sp.title = isCur ? 'Aktives Aufzeichnungs-Gerät' : 'Hier klicken, um dieses Gerät aktiv zu laden';
    sp.onclick = function(){
      if(idx !== activeIdx){
        post('/switch_active_device', { device_idx: idx }).then(function(){ poll(); });
      }
    };
    sp.innerHTML = (d.icon || '🔌') + ' <b>' + (d.raw_name || d.name || ('Gerät ' + (idx+1))) + '</b> <span style="opacity:0.8">(' + (d.wh || 0).toFixed(2) + ' Wh)</span>' +
                   (isCur ? ' <span style="background:#059669;color:#fff;font-size:9px;font-weight:800;padding:1px 5px;border-radius:6px;margin-left:4px">AKTIV</span>' : '');
    c.appendChild(sp);
  });
  
  var addBtn = document.createElement('span');
  addBtn.className = 'dev-pill';
  addBtn.style.cursor = 'pointer';
  addBtn.style.background = '#eff6ff';
  addBtn.style.color = '#1e40af';
  addBtn.style.borderColor = '#93c5fd';
  addBtn.innerHTML = '➕ <b>Neues Gerät</b>';
  addBtn.onclick = function(){
    post('/add_new_device').then(function(){ poll(); });
  };
  c.appendChild(addBtn);

  if(activeIdx > 0){
    var mergeBtn = document.createElement('span');
    mergeBtn.className = 'dev-pill';
    mergeBtn.style.cursor = 'pointer';
    mergeBtn.style.background = '#fef3c7';
    mergeBtn.style.color = '#92400e';
    mergeBtn.style.borderColor = '#fcd34d';
    mergeBtn.innerHTML = '🔁 <b>Zu Gerät 1</b>';
    mergeBtn.title = 'Aktuelles Gerät mit Gerät 1 zusammenführen';
    mergeBtn.onclick = function(){
      if(confirm('Möchtest du dieses Gerät mit Gerät 1 zusammenführen?')){
        post('/merge_device', { target_idx: 0 }).then(function(){ poll(); });
      }
    };
    c.appendChild(mergeBtn);
  }
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
    var flowSec = d.total_flow_seconds || 0.0;
    var idleSec = d.total_idle_seconds || 0.0;

    allRecordedDevices = d.devices || [];

    // Land
    if(d.country){
      currentCountry = d.country;
      updateCountryDisplays();
    }

    // Kontinuierliche Daueruhr (Master auf Server)
    if(d.session_started){
      sessionStarted = true;
      startContinuousTimer();
      if(Math.abs(localElapsed - srvSec) > 1.5){
        localElapsed = Math.floor(srvSec);
        updateTimerDisplay();
      }
    } else {
      sessionStarted = false;
      localElapsed = 0;
      updateTimerDisplay();
    }

    var bStart = document.getElementById('btnStart');
    var bPause = document.getElementById('btnPause');

    if(d.active && !d.paused){
      document.getElementById('sPill').className = 'pill pill-on';
      document.getElementById('sTxt').innerText = 'Aktiv';
      if(bStart) bStart.style.display = 'none';
      if(bPause){
        bPause.style.display = 'block';
        bPause.style.background = '#f1f5f9';
        bPause.style.color = '#0f172a';
        bPause.innerHTML = '⏸️ Pause einlegen';
      }
    } else if(d.paused){
      document.getElementById('sPill').className = 'pill pill-p';
      document.getElementById('sTxt').innerText = 'Pause';
      if(bStart){
        bStart.style.display = 'block';
        bStart.style.background = '#059669';
        bStart.style.color = '#ffffff';
        bStart.innerHTML = '▶️ Fortsetzen & Laden';
      }
      if(bPause) bPause.style.display = 'none';
    } else {
      document.getElementById('sPill').className = 'pill pill-off';
      document.getElementById('sTxt').innerText = 'Bereit';
      if(bStart){
        bStart.style.display = 'block';
        bStart.style.background = '#0f172a';
        bStart.style.color = '#ffffff';
        bStart.innerHTML = '▶️ Starten';
      }
      if(bPause) bPause.style.display = 'none';
    }

    // Modal Status Management
    var isRecentAction = (Date.now() - lastActionLocalTime < 6000);
    
    // 1. Absteck-Modale
    if(d.unplug_modal === 'ASK_UNPLUG' && !isRecentAction){
      showM('modalAskUnplug');
      hideM('modalAskNextDevice');
      hideM('modalPowerShift');
      hideM('modalBattery80');
      hideM('modalBattery100');
    } else if(d.unplug_modal === 'ASK_NEXT_DEVICE' && !isRecentAction){
      hideM('modalAskUnplug');
      renderReusableDevices(d.devices);
      showM('modalAskNextDevice');
      hideM('modalPowerShift');
      hideM('modalBattery80');
      hideM('modalBattery100');
    } else if(d.power_shift_modal && !isRecentAction){
      document.getElementById('psFromW').innerText = d.power_shift_modal.from_w + ' W';
      document.getElementById('psToW').innerText = d.power_shift_modal.to_w + ' W';
      hideM('modalAskUnplug');
      hideM('modalAskNextDevice');
      showM('modalPowerShift');
      startPowerShiftCountdown();
      hideM('modalBattery80');
      hideM('modalBattery100');
    } else if(d.battery_modal === 'BATTERY_80' && !isRecentAction){
      stopPowerShiftCountdown();
      hideM('modalAskUnplug');
      hideM('modalAskNextDevice');
      hideM('modalPowerShift');
      showM('modalBattery80');
      hideM('modalBattery100');
    } else if(d.battery_modal === 'BATTERY_100' && !isRecentAction){
      stopPowerShiftCountdown();
      hideM('modalAskUnplug');
      hideM('modalAskNextDevice');
      hideM('modalPowerShift');
      hideM('modalBattery80');
      showM('modalBattery100');
    } else if(!d.unplug_modal && !d.battery_modal && !d.power_shift_modal || isRecentAction){
      stopPowerShiftCountdown();
      hideM('modalAskUnplug');
      hideM('modalAskNextDevice');
      hideM('modalPowerShift');
      hideM('modalBattery80');
      hideM('modalBattery100');
    }

    // Waiting for plug banner & Status (bereits eingesteckt vs. einzustecken)
    var wb = document.getElementById('waitingPlugBanner');
    if(d.waiting_for_new_plug){
      wb.style.display = 'block';
      if(curW > 0.3 || curA > 0.02){
        document.getElementById('waitingPlugTitle').innerText = '⚡ Strom fließt – Gerät bereits eingesteckt';
        document.getElementById('waitingPlugSub').innerText = 'Das Gerät zieht bereits ' + curW.toFixed(1) + ' W. Erfassung läuft!';
      } else {
        document.getElementById('waitingPlugTitle').innerText = '🔌 Steckdose aktiv – Gerät einstecken';
        document.getElementById('waitingPlugSub').innerText = 'Stecke das Gerät jetzt ein. Sobald Strom fließt, wird automatisch gestartet!';
      }
    } else {
      wb.style.display = 'none';
    }

    // Stecker gezogen / Standby Indikator
    var unplugPill = document.getElementById('unplugPill');
    if(!d.is_flowing && (d.active || d.paused) && d.had_flowing){
      unplugPill.style.display = 'inline-flex';
    } else {
      unplugPill.style.display = 'none';
    }

    // DYNAMISCHES MULTI-SEGMENT SCHAUBILD
    updateDynamicTimeline(d.devices, flowSec, idleSec, srvSec);

    // Geräte-Tabs
    renderDevicePills(d.devices, d.current_device_idx);

    // Messwerte
    document.getElementById('volt').innerText = (d.voltage || 230).toFixed(1);
    document.getElementById('amp').innerText = curA.toFixed(3);
    document.getElementById('ma').innerText = (curA * 1000).toFixed(0);
    document.getElementById('watt').innerText = curW.toFixed(3);
    document.getElementById('wh').innerText = curWh.toFixed(4);
    document.getElementById('mwh').innerText = (curWh * 1000).toFixed(1);

    // Kosten
    var brutto = d.cost_brutto || d.cost || 0.0;
    var netto = d.cost_netto || 0.0;
    var vatAmt = d.vat_amount || 0.0;

    document.getElementById('costBrutto').innerText = brutto.toFixed(5);
    document.getElementById('costCent').innerText = (brutto * 100).toFixed(3);
    document.getElementById('costNetto').innerText = netto.toFixed(5) + ' €';
    document.getElementById('vatAmount').innerText = vatAmt.toFixed(5) + ' €';

    document.getElementById('wSub').innerText = (curW > 0.1) ? 'Strom fließt' : 'Standby / 0 W';

    // KI & Aktives Gerät
    if(d.active_device){
      currentDevice = d.active_device;
    }

    latestAiSuggestion = d.ai_result || {};
    var conf = latestAiSuggestion.confidence || 0;
    
    if(latestAiSuggestion.learned_match){
      document.getElementById('aiConf').innerText = '🎯 Gelernt (' + conf + '% Match)';
    } else {
      document.getElementById('aiConf').innerText = conf > 0 ? ('Sicherheit: ' + conf + '%') : 'Sammle Daten...';
    }
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

// KI-LADEASSISTENT CHATBOT JS
var chatOpen = false;
function toggleChatDrawer(){
  var d = document.getElementById('chatDrawer');
  if(!d) return;
  chatOpen = !chatOpen;
  d.style.display = chatOpen ? 'flex' : 'none';
  if(chatOpen){
    var inp = document.getElementById('chatIn');
    if(inp) inp.focus();
    scrollChat();
  }
}

function scrollChat(){
  var b = document.getElementById('chatBody');
  if(b) b.scrollTop = b.scrollHeight;
}

function appendChatMsg(sender, htmlText){
  var b = document.getElementById('chatBody');
  if(!b) return;
  var div = document.createElement('div');
  div.className = 'chat-msg ' + (sender === 'user' ? 'user' : 'bot');
  div.innerHTML = htmlText.replace(/\n/g, '<br/>');
  b.appendChild(div);
  scrollChat();
}

function sendQuickChip(txt){
  var inp = document.getElementById('chatIn');
  if(inp) inp.value = txt;
  sendChatMessage();
}

function sendChatMessage(){
  var inp = document.getElementById('chatIn');
  if(!inp) return;
  var txt = inp.value.trim();
  if(!txt) return;
  inp.value = '';
  
  appendChatMsg('user', txt);
  
  var b = document.getElementById('chatBody');
  var typingDiv = document.createElement('div');
  typingDiv.className = 'chat-msg bot';
  typingDiv.id = 'chatTyping';
  typingDiv.innerHTML = '<i>Denkt nach...</i>';
  b.appendChild(typingDiv);
  scrollChat();
  
  post('/assistant_chat', { message: txt }).then(function(res){
    var t = document.getElementById('chatTyping');
    if(t) t.remove();
    if(res && res.reply){
      appendChatMsg('bot', res.reply);
      setTimeout(poll, 300);
    } else {
      appendChatMsg('bot', '⚠️ Fehler bei der Verbindung zum Assistenten.');
    }
  }).catch(function(){
    var t = document.getElementById('chatTyping');
    if(t) t.remove();
    appendChatMsg('bot', '⚠️ Netzwerkfehler. Bitte erneut versuchen.');
  });
}

setInterval(poll, 1000);
poll();
</script></body></html>"""


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)