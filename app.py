import asyncio
import json
import threading
import math
import time
from datetime import datetime
from collections import deque
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from streamlit_echarts import st_echarts
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

st.set_page_config(page_title="Spine Monitor Dashboard", layout="wide")

# ========================
# KONFIGURASI FIREBASE
# ========================
FIREBASE_URL = "https://posture-4fe84-default-rtdb.firebaseio.com"
FIREBASE_SECRET = "i2cto2788r0G60TgzlAoTj1xGdB9OZpwzvjpQyLJ"
POLL_INTERVAL = 1.0

# Link dashboard yang dikirim ke Telegram (Streamlit default port 8501).
DASHBOARD_URL = "https://koreksiposture.streamlit.app/"

DURASI_DUDUK = 18 * 60
DURASI_ISTIRAHAT = 12 * 60
BATT_LOW = 20
BATT_CRIT = 10
REMINDER_JAM_MULAI = 8
REMINDER_JAM_AKHIR = 21
TIMEZONE = "Asia/Makassar"  # WITA

# ========================
# KONFIGURASI TELEGRAM BOT
# ========================
TELEGRAM_TOKEN = "8849972300:AAFvOzPZ3PjQBWV7mAMsMXfL5uIQAuXDu0E"

SUBS_FILE = "telegram_subscribers.json"
MAX_DATA_POINTS = 20


def _load_subs():
    try:
        with open(SUBS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


# ========================
# STATE GLOBAL (dibagi lintas rerun & sesi)
# Streamlit menjalankan ulang skrip setiap interaksi, jadi state
# harus disimpan di cache_resource agar tidak ter-reset.
# ========================
@st.cache_resource
def get_shared():
    return {
        "timer_state": {"seconds": DURASI_DUDUK, "phase": "duduk"},
        "usage_state": {"seconds": 0},
        "event_state": {"id": 0, "icon": "", "title": "", "msg": ""},
        "notif_state": {"online": None, "last": {}, "batt_level": 0, "lepas_count": 0, "tick": None},
        "sensor_data": {
            "battery_pct": None, "battery_v": None, "distance": None,
            "fsr1": None, "fsr2": None, "duduk": None, "alarm": None,
            "mpu1": None, "mpu2": None, "timestamp": 0,
            "last_update_time": None, "online": False, "waktu": "-",
        },
        "time_buffer": deque(maxlen=MAX_DATA_POINTS),
        "fsr1_buffer": deque(maxlen=MAX_DATA_POINTS),
        "fsr2_buffer": deque(maxlen=MAX_DATA_POINTS),
        "mpu1_buffer": deque(maxlen=MAX_DATA_POINTS),
        "mpu2_buffer": deque(maxlen=MAX_DATA_POINTS),
        "dist_buffer": deque(maxlen=MAX_DATA_POINTS),
        "last_raw": None,
        "subscribers": _load_subs(),
        "telegram_app": Application.builder().token(TELEGRAM_TOKEN).build(),
    }


S = get_shared()
timer_state = S["timer_state"]
usage_state = S["usage_state"]
event_state = S["event_state"]
notif_state = S["notif_state"]
sensor_data = S["sensor_data"]
time_buffer = S["time_buffer"]
fsr1_buffer = S["fsr1_buffer"]
fsr2_buffer = S["fsr2_buffer"]
mpu1_buffer = S["mpu1_buffer"]
mpu2_buffer = S["mpu2_buffer"]
dist_buffer = S["dist_buffer"]
subscribers = S["subscribers"]
telegram_app = S["telegram_app"]


# ========================
# HANDLER TELEGRAM BOT
# ========================
def _save_subs():
    with open(SUBS_FILE, "w") as f:
        json.dump(list(subscribers), f)


async def kirim_notif(teks):
    for cid in list(subscribers):
        try:
            await telegram_app.bot.send_message(chat_id=cid, text=teks)
        except Forbidden:
            subscribers.discard(cid)
            _save_subs()
        except Exception as e:
            print(f"Gagal kirim notif ke {cid}: {e}")


def ringkasan_status():
    if not sensor_data["online"]:
        return "📴 Alat sedang offline / tidak terhubung."
    pct = get_battery_pct()
    m, s = divmod(int(timer_state["seconds"]), 60)
    fase = "Duduk" if timer_state["phase"] == "duduk" else "Peregangan"
    return (
        "📊 Status Spine Monitor\n"
        f"Postur: {'Buruk (membungkuk)' if sensor_data.get('alarm_bungkuk') else 'Baik'}\n"
        f"Posisi: {'Duduk' if sensor_data.get('duduk', True) else 'Berdiri'}\n"
        f"Baterai: {pct if pct is not None else '--'}%\n"
        f"Fase: {fase}, sisa {m:02d}:{s:02d}\n"
        f"Dashboard: {DASHBOARD_URL}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    _save_subs()
    await update.message.reply_text(
        f"Halo {update.effective_user.first_name}! Notifikasi Spine Monitor sudah aktif.\n\n"
        "/status - lihat kondisi saat ini\n"
        "/dashboard - link dashboard\n"
        "/stop - matikan notifikasi\n\n"
        f"Dashboard: {DASHBOARD_URL}"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    _save_subs()
    await update.message.reply_text("Notifikasi dimatikan. Kirim /start untuk mengaktifkan lagi.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ringkasan_status())


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔗 Dashboard: {DASHBOARD_URL}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "bungkuk" in text or "status" in text:
        await update.message.reply_text(ringkasan_status())
    else:
        await update.message.reply_text("Perintah: /status, /dashboard, /stop")


# ========================
# FIREBASE FUNCTIONS
# ========================
def _fetch_firebase_sync(node="latest", extra_params=None):
    try:
        params = {"auth": FIREBASE_SECRET} if FIREBASE_SECRET else {}
        if extra_params:
            params.update(extra_params)
        endpoint = f"{FIREBASE_URL}/spine_monitor/{node}.json"
        r = requests.get(endpoint, params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def ambil_data_firebase():
    data = await asyncio.to_thread(_fetch_firebase_sync, "latest")
    now = datetime.now()

    if data and isinstance(data, dict):
        if S["last_raw"] is None:
            S["last_raw"] = data
        elif data != S["last_raw"]:
            S["last_raw"] = data
            sensor_data["last_update_time"] = now

        sensor_data.update(data)

        if sensor_data["last_update_time"] is not None:
            selisih_detik = (now - sensor_data["last_update_time"]).total_seconds()
            sensor_data["online"] = (selisih_detik <= 10.0)
        else:
            sensor_data["online"] = False
        return True
    else:
        sensor_data["online"] = False
        return False


def ambil_history_firebase():
    LABEL_STATUS = {
    "aman": "Tegak",
    "bungkuk": "Membungkuk",
    "longgar": "Korset longgar",
    "berdiri": "Berdiri",
    "peregangan": "Peregangan",
    "waktu_duduk_habis": "Waktu duduk habis",
    "waktu_peregangan_habis": "Waktu peregangan habis",
} 
    query_params = {"orderBy": '"$key"', "limitToLast": 30}
    history_data = _fetch_firebase_sync("history", query_params)
    rows = []

    if history_data and isinstance(history_data, dict):
        for key, val in history_data.items():
            if isinstance(val, dict):
                d = _to_float(val.get('distance'))
                jarak = "Tidak terhubung" if d is None else "Tidak terdeteksi" if d == -1 else f"{d:.0f} cm"

                m1 = _mpu_y_dari(val.get('mpu1'))
                m2 = _mpu_y_dari(val.get('mpu2'))
                f1 = _fsr_valid(val.get('fsr1'))
                f2 = _fsr_valid(val.get('fsr2'))

                status_raw = val.get('status')
                
                if status_raw:
                    postur = LABEL_STATUS.get(status_raw, status_raw)
                elif m1 is None and m2 is None:
                    postur = "Tidak terhubung"
                else:
                    postur = 'Membungkuk' if val.get('alarm') else 'Tegak'

                rows.append({
                    'id': key,
                    'waktu': val.get('waktu', '-'),
                    'postur': postur,
                    'jarak': jarak,
                    'fsr1': f1 if f1 is not None else "Tidak terhubung",
                    'fsr2': f2 if f2 is not None else "Tidak terhubung",
                    'mpu1': f"{m1}°" if m1 is not None else "Tidak terhubung",
                    'mpu2': f"{m2}°" if m2 is not None else "Tidak terhubung",
                })
        rows.reverse()

    return rows


@st.cache_data(ttl=5.0)
def history_cached():
    return ambil_history_firebase()


# ========================
# BATERAI
# ========================
_BATT_CURVE = [
    (8.40, 100), (8.20, 90), (8.00, 80), (7.80, 65),
    (7.60, 50), (7.40, 35), (7.20, 20), (7.00, 10), (6.60, 0),
]


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def hitung_persen_dari_volt(v):
    if v >= _BATT_CURVE[0][0]:
        return 100
    if v <= _BATT_CURVE[-1][0]:
        return 0
    for (v1, p1), (v2, p2) in zip(_BATT_CURVE, _BATT_CURVE[1:]):
        if v2 <= v <= v1:
            return int(p2 + (v - v2) / (v1 - v2) * (p1 - p2))
    return 0


def get_battery_pct():
    for key in ("battery_pct", "batt_pct", "battery", "baterai", "persen"):
        pct = _to_float(sensor_data.get(key))
        if pct is not None:
            return max(0, min(100, int(pct)))
    for key in ("battery_v", "batt_v", "voltage", "tegangan"):
        v = _to_float(sensor_data.get(key))
        if v is not None and v > 0:
            return hitung_persen_dari_volt(v)
    return None


def get_battery_icon(pct):
    if pct is None:
        return "battery_unknown"
    if pct >= 95: return "battery_full"
    if pct >= 80: return "battery_6_bar"
    if pct >= 65: return "battery_5_bar"
    if pct >= 50: return "battery_4_bar"
    if pct >= 35: return "battery_3_bar"
    if pct >= 20: return "battery_2_bar"
    if pct >= 10: return "battery_1_bar"
    return "battery_alert"


# ========================
# MONITOR GLOBAL
# ========================
def _mpu_y_dari(d):
    """Sudut kemiringan (derajat) dari sumbu Y (vertikal) MPU6050."""
    if not isinstance(d, dict):
        return None
    x, y, z = (_to_float(d.get(k)) for k in ("x", "y", "z"))
    if x is None or y is None or z is None:
        return None
    return int(round(math.degrees(math.atan2(math.hypot(x, z), y)))) -16


def _mpu_z(key):
    return _mpu_y_dari(sensor_data.get(key))


def _fsr_valid(v):
    f = _to_float(v)
    return None if f is None else int(f)


def fsr_val(key):
    return _fsr_valid(sensor_data.get(key))

def korset_dipakai():
    if timer_state["phase"] != "duduk":
        return True  # fase peregangan: korset boleh dilonggarkan
    return bool(sensor_data.get("korset", False))

def _boleh_kirim(key, cooldown):
    now = time.time()
    if now - notif_state["last"].get(key, 0) >= cooldown:
        notif_state["last"][key] = now
        return True
    return False


def _reset_kirim(key):
    notif_state["last"].pop(key, None)


def _buat_event(icon, title, msg):
    event_state.update(id=event_state["id"] + 1, icon=icon, title=title, msg=msg)


async def proses_state():
    tick = time.monotonic()
    dt = min(tick - notif_state["tick"], 5.0) if notif_state["tick"] else 0.0
    notif_state["tick"] = tick

    online = sensor_data["online"]
    prev = notif_state["online"]

    if online != prev:
        notif_state["dipakai"] = None
        notif_state["online"] = online
        notif_state["last"].clear()
        notif_state["lepas_count"] = 0
        notif_state["batt_level"] = 0
        notif_state["fase_fw"] = None
        if online:
            await kirim_notif(f"✅ Alat sudah terhubung.\n🔗 Dashboard: {DASHBOARD_URL}")
        elif prev is None:
            pass  # Awal start up, diam saja jika tidak online
        else:
            await kirim_notif("⚠️ Koneksi alat terputus. Timer dihentikan sementara.")

    if not online:
        return

    usage_state["seconds"] += dt   # timer keseluruhan: jalan terus selama alat online

    dist = _to_float(sensor_data.get("distance")) if online else None
    if dist is None:
        dist_txt, dist_cls = "Tidak terhubung", "big-off"
    else:
        dist_txt, dist_cls = f"{dist:.0f} cm", "big"
        
    time_buffer.append(datetime.now().strftime("%H:%M:%S"))
    fsr1_buffer.append(fsr_val("fsr1"))
    fsr2_buffer.append(fsr_val("fsr2"))
    mpu1_buffer.append(_mpu_z("mpu1"))
    mpu2_buffer.append(_mpu_z("mpu2"))
    dist_buffer.append(dist)

    # --- Baterai (tetap dicek walau korset belum dipakai) ---
    pct = get_battery_pct()
    if pct is not None:
        lvl = 2 if pct <= BATT_CRIT else 1 if pct <= BATT_LOW else 0
        if pct >= BATT_LOW + 5:
            notif_state["batt_level"] = 0
        elif lvl > notif_state["batt_level"]:
            notif_state["batt_level"] = lvl
            if lvl == 2:
                await kirim_notif(f"🪫 Baterai kritis ({pct}%)! Segera cas alat.")
            else:
                await kirim_notif(f"🔋 Baterai tinggal {pct}%. Siapkan charger.")

    # --- Sinkron timer & fase dari Arduino ---
    fase_fw = sensor_data.get("fase")
    sisa_fw = _to_float(sensor_data.get("sisa"))
    if fase_fw in ("duduk", "peregangan") and sisa_fw is not None:
        fase_baru = "duduk" if fase_fw == "duduk" else "istirahat"
        fase_lama = notif_state.get("fase_fw")
        data_baru = sensor_data.get("waktu") != notif_state.get("waktu_lalu")
        notif_state["waktu_lalu"] = sensor_data.get("waktu")

        timer_state["phase"] = fase_baru
        if data_baru:
            timer_state["seconds"] = sisa_fw
        else:
            timer_state["seconds"] = max(0, timer_state["seconds"] - dt)
        notif_state["fase_fw"] = fase_baru

        if fase_lama is not None and fase_lama != fase_baru:
            if fase_baru == "istirahat":
                _buat_event("self_improvement", "Waktunya Peregangan!",
                            f"Kamu sudah duduk {DURASI_DUDUK // 60} menit. Longgarkan atau lepas korset dan lakukan peregangan selama {DURASI_ISTIRAHAT // 60} menit.")
                await kirim_notif(f"🧘 Waktunya peregangan! Kamu sudah duduk {DURASI_DUDUK // 60} menit.\n"
                                  f"Longgarkan korset dan lakukan peregangan selama {DURASI_ISTIRAHAT // 60} menit.")
            else:
                _buat_event("event_seat", "Peregangan Selesai",
                            "Waktu peregangan selesai, silakan duduk kembali.")
                await kirim_notif("✅ Waktu peregangan selesai, silakan duduk kembali.")

    # --- Korset dipakai? ---
    dipakai = korset_dipakai()
    sebelum = notif_state.get("dipakai")
    notif_state["dipakai"] = dipakai

    if not dipakai:
        notif_state["lepas_count"] += 1
        if notif_state["lepas_count"] >= 5 and _boleh_kirim("korset", 120):
            await kirim_notif("👕 Korset belum dipakai atau longgar.")
        return

    notif_state["lepas_count"] = 0
    _reset_kirim("korset")
    if sebelum is False:
        await kirim_notif("✅ Korset terdeteksi dipakai.")

    # --- Postur (hanya fase duduk) ---
    if timer_state["phase"] != "duduk":
        return

    alarm = bool(sensor_data.get("alarm_bungkuk", False))
    duduk = bool(sensor_data.get("duduk", True))
    mpu_ok = _mpu_z("mpu1") is not None or _mpu_z("mpu2") is not None

    if alarm and duduk and mpu_ok:
        if _boleh_kirim("bungkuk", 60):
            await kirim_notif("🪑 Terdeteksi membungkuk. Harap perbaiki posisi duduk.")
    elif not alarm:
        _reset_kirim("bungkuk")


async def monitor_loop():
    while True:
        try:
            await ambil_data_firebase()
            await proses_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Error monitor_loop: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def daily_reminder_loop():
    """Mengirim pengingat harian jam 08:00 WITA untuk menggunakan korset."""
    terkirim_hari_ini = False
    tz = ZoneInfo(TIMEZONE)

    while True:
        try:
            now = datetime.now(tz)

            if now.hour == REMINDER_JAM_MULAI and now.minute == 0:
                if not terkirim_hari_ini:
                    await kirim_notif(
                        "⏰ *Pengingat Harian*\n"
                        "Selamat pagi! Waktunya menggunakan korset pemantau postur untuk hari ini. "
                        "Jangan lupa nyalakan alat dan pastikan terhubung ke sistem!"
                    )
                    terkirim_hari_ini = True
            elif now.hour != REMINDER_JAM_MULAI:
                terkirim_hari_ini = False

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Error daily_reminder_loop: {e}")

        await asyncio.sleep(30)


# ========================
# STARTUP (pengganti @app.on_startup) - jalan sekali di thread background
# ========================
@st.cache_resource
def start_backend():
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("dashboard", dashboard_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def _main():
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        asyncio.create_task(monitor_loop())
        asyncio.create_task(daily_reminder_loop())
        print("Bot Telegram, monitor, dan pengingat harian berhasil dijalankan!")

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_main())
        loop.run_forever()

    threading.Thread(target=_runner, daemon=True).start()
    return True


start_backend()


# ========================
# HALAMAN DASHBOARD STREAMLIT
# ========================
st.markdown("""
<style>
.stApp { background-color: #F7F6F1; font-family: sans-serif; color: #000; }
header[data-testid="stHeader"] { display: none; }
.block-container { max-width: 1400px; padding: 1rem 2rem; }
.ms { font-family: 'Material Symbols Rounded', 'Material Icons'; font-size: 32px; line-height: 1; vertical-align: middle; font-weight: normal; }
.card { border-radius: 16px; padding: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.1); }
.lbl-sm { font-size: 14px; }
.big { font-size: 36px; font-weight: 700; }
.st-key-status_card { background:#9ACDFF; border-radius:16px; padding:16px; box-shadow:0 1px 2px rgba(0,0,0,.1); }
.st-key-gauge_card { background:#fff; border-radius:12px; padding:12px 16px; overflow:hidden; }
.st-key-gauge_card iframe { color-scheme: light; background: transparent !important; }
.g-lbl { text-align:center; font-size:12px; color:#4b5563; margin-top:-4px; }
.g-num { display:none; text-align:center; font-size:24px; font-weight:700; color:#000; }

/* HP: gauge disembunyikan, tampil angka saja */
@media (max-width: 768px) {
    .st-key-gw_atas, .st-key-gw_bawah { display:none !important; }
    .g-num { display:block; }
}
.st-key-btn_bot a { background:#7BAFD4 !important; color:#fff !important; font-weight:700; border-radius:6px; border:none; }

/* Popup: kartu di tengah layar dengan lebar tetap */
.st-key-popup_offline, .st-key-popup_timer {
    position: fixed !important; top: 50%; left: 50%; transform: translate(-50%,-50%);
    width: 360px !important; max-width: 90vw; z-index: 9999;
    background: #fff; color: #000; padding: 24px; border-radius: 12px;
    align-items: center; text-align: center; gap: 8px;
    box-shadow: 0 0 0 100vmax rgba(0,0,0,.5);
}
.st-key-popup_offline button, .st-key-popup_timer button { background:#3E5C76; color:#fff; border:none; }

/* Tombol Tabel/Grafik agar teks tidak terpotong */
.st-key-btn_tabel button, .st-key-btn_grafik button {
    border-radius:6px; padding:4px 24px; box-shadow:none; min-width:90px; white-space:nowrap;
}

/* Tabel riwayat (meniru tampilan ui.table NiceGUI) */
.tbl-wrap { max-height: 600px; overflow: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; color: #000; }
.tbl th { position: sticky; top: 0; z-index: 1; background: #3E5C76; color: #fff;
        border: 1px solid #9ca3af; padding: 6px 8px; text-align: left; }
.tbl td { border: 1px solid #9ca3af; padding: 4px 8px; }

.big-off { font-size: 20px; font-weight: 700; line-height: 1.2; margin: 8px 0; color: #6b7280; }
.g-off { display:flex; align-items:center; justify-content:center; height:100px;
        text-align:center; font-size:14px; font-weight:600; color:#6b7280; }

/* ===== RESPONSIVE HP: kolom tertentu tetap sejajar ===== */
@media (max-width: 768px) {
    /* Tombol Tabel & Grafik bersampingan */
    .st-key-row_btn [data-testid="stHorizontalBlock"] {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 8px !important;
    }
    .st-key-row_btn [data-testid="stColumn"] {
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
    }

    /* Kartu FSR kanan & kiri bersebelahan */
    .st-key-row_fsr [data-testid="stHorizontalBlock"],
    .st-key-row_dist [data-testid="stHorizontalBlock"],
    .st-key-gauge_card [data-testid="stHorizontalBlock"] {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 8px !important;
    }
    .st-key-row_fsr [data-testid="stColumn"],
    .st-key-gauge_card [data-testid="stColumn"] {
        width: 50% !important;
        flex: 1 1 0 !important;
        min-width: 0 !important;
    }
    .st-key-row_dist [data-testid="stColumn"]:nth-child(1) { flex: 3 1 0 !important; width: 60% !important; min-width: 0 !important; }
    .st-key-row_dist [data-testid="stColumn"]:nth-child(2) { flex: 2 1 0 !important; width: 40% !important; min-width: 0 !important; }

    /* Kartu FSR lebih ringkas di HP */
    .st-key-row_fsr .big { font-size: 28px; }
    .st-key-row_fsr .card { padding: 12px 8px; }
}

</style>
""", unsafe_allow_html=True)

if "view" not in st.session_state:
    st.session_state.view = "tabel"
if "conn_prev" not in st.session_state:
    st.session_state.conn_prev = None
if "pernah_online" not in st.session_state:
    st.session_state.pernah_online = False
if "seen_event" not in st.session_state:
    st.session_state.seen_event = event_state["id"]


def set_view(v):
    st.session_state.view = v


def icon(name, color):
    return f'<span class="ms" style="color:{color}">{name}</span>'


def gauge(value, key):
    st_echarts({
        'backgroundColor': 'transparent',
        'series': [{
            'type': 'gauge',
            'startAngle': 210, 'endAngle': -30,
            'min': -90, 'max': 90,
            'center': ['50%', '58%'],
            'radius': '100%',
            'progress': {'show': True, 'width': 10, 'itemStyle': {'color': '#3E5C76'}},
            'axisLine': {'lineStyle': {'width': 10, 'color': [[1, '#E3E8EE']]}},
            'axisTick': {'show': False},
            'splitLine': {'show': False},
            'axisLabel': {'show': False},
            'pointer': {'width': 4, 'length': '65%', 'itemStyle': {'color': '#3E5C76'}},
            'anchor': {'show': True, 'size': 8, 'itemStyle': {'color': '#3E5C76'}},
            'detail': {'formatter': '{value}°', 'fontSize': 18, 'fontWeight': 'bold',
            'color': '#000', 'offsetCenter': [0, '62%']},
            'data': [{'value': max(-90, min(90, value))}],
        }]
    }, height="140px", width="100%", key=key)


def line_chart(title, legend, series, xdata, key):
    opt = {
        'title': {'text': title, 'textStyle': {'fontSize': 14}},
        'tooltip': {'trigger': 'axis'},
        'xAxis': {'type': 'category', 'data': xdata},
        'yAxis': {'type': 'value'},
        'series': series,
    }
    if legend:
        opt['legend'] = {'data': legend, 'bottom': 0}
    st_echarts(opt, height="340px", key=key)

def render_tabel(rows):
    headers = ['Waktu', 'Status Postur', 'Jarak', 'FSR 1 (Kanan)', 'FSR 2 (Kiri)', 'MPU 1 (Atas)', 'MPU 2 (Bawah)']
    keys = ['waktu', 'postur', 'jarak', 'fsr1', 'fsr2', 'mpu1', 'mpu2']
    th = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{r[k]}</td>" for k in keys) + "</tr>" for r in rows)
    st.markdown(
        f'<div class="tbl-wrap"><table class="tbl"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>',
        unsafe_allow_html=True,
    )

@st.fragment(run_every=POLL_INTERVAL)
def dashboard():
    online = sensor_data['online']

    # KONTROL POPUP OFFLINE
    prev = st.session_state.conn_prev
    if online != prev:
        if online:
            if prev is False:
                st.toast("Alat terhubung kembali", icon="✅")
            st.session_state.pernah_online = True
        st.session_state.conn_prev = online

    if not online:
        with st.container(key="popup_offline"):
            st.markdown(icon("wifi_off", "#ef4444").replace("32px", "48px"), unsafe_allow_html=True)
            if st.session_state.pernah_online:
                st.markdown("### Koneksi Terputus")
                st.caption("Alat tidak terhubung Wi-Fi. Timer dihentikan sementara.")
            else:
                st.markdown("### Alat Belum Terhubung")
                st.caption("Nyalakan alat dan pastikan WiFi terhubung. Timer akan mulai setelah alat online.")
            if st.button("🔄 Refresh", key="refresh_btn"):
                st.rerun()

    # KONTROL NOTIF TIMER PEREGANGAN
    if event_state["id"] != st.session_state.seen_event:
        with st.container(key="popup_timer"):
            st.markdown(icon(event_state["icon"], "#3E5C76").replace("32px", "48px"), unsafe_allow_html=True)
            st.markdown(f"### {event_state['title']}")
            st.caption(event_state["msg"])
            if st.button("Oke", key="oke_btn"):
                st.session_state.seen_event = event_state["id"]
                st.rerun(scope="fragment")

    col_kiri, col_kanan = st.columns(2, gap="large")

    # ================= KOLOM KIRI =================
    with col_kiri:
        batt_pct = get_battery_pct() if online else None
        if not online or batt_pct is None:
            batt_color = "#9ca3af"
        elif batt_pct < 20:
            batt_color = "#ef4444"
        elif batt_pct < 50:
            batt_color = "#f97316"
        else:
            batt_color = "#16a34a"
        batt_icon_name = get_battery_icon(batt_pct) if online else "battery_unknown"
        batt_txt = f"{batt_pct}%" if (online and batt_pct is not None) else "--%"

        st.markdown(f"""
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
            {icon('wifi' if online else 'wifi_off', '#22c55e' if online else '#ef4444')}
            <div style="display:flex;align-items:center;gap:4px">
                <span style="font-size:14px;font-weight:700;color:#374151">{batt_txt}</span>
                {icon(batt_icon_name, batt_color)}
            </div>
        </div>
        """, unsafe_allow_html=True)

        total = int(usage_state["seconds"])
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        st.markdown(f"""
        <div style="border:2px solid black;border-radius:9999px;padding:8px;text-align:center;margin:8px 0">
            <div style="font-size:12px;font-weight:500">Durasi pemakaian</div>
            <div style="font-size:20px;font-weight:600">{hours:02d}:{minutes:02d}:{seconds:02d}</div>
        </div>
        """, unsafe_allow_html=True)

        # STATUS POSTUR + GAUGE
        alarm = sensor_data.get("alarm_bungkuk", False)
        m1_z = _mpu_z("mpu1") if online else None
        m2_z = _mpu_z("mpu2") if online else None

        dipakai = korset_dipakai() if online else False

        if not online:
            status_txt, status_color, status_cls = "Terputus", "#9ca3af", "big"
        elif not dipakai:
            status_txt, status_color, status_cls = "Belum dipakai / longgar", "#b45309", "big-off"
        elif m1_z is None and m2_z is None:
            status_txt, status_color, status_cls = "Tidak terhubung", "#9ca3af", "big-off"
        else:
            status_txt = "Buruk" if alarm else "Baik"
            status_color = "#dc2626" if alarm else "#000"
            status_cls = "big"

        def kolom_gauge(z, nama, key_gauge, key_wrap):
            if z is None:
                st.markdown(f'<div class="g-off">Tidak<br>terhubung</div><div class="g-lbl">{nama}</div>',
                            unsafe_allow_html=True)
            else:
                with st.container(key=key_wrap):
                    gauge(z, key_gauge)
                st.markdown(f'<div class="g-num">{z}°</div><div class="g-lbl">{nama}</div>',
                            unsafe_allow_html=True)

        with st.container(key="status_card"):
            c1, c2 = st.columns([1, 1.4])
            with c1:
                st.markdown(f"""
                <div style="font-size:18px;color:black">Status postur</div>
                <div class="{status_cls}" style="color:{status_color}">{status_txt}</div>
                """, unsafe_allow_html=True)
            with c2:
                with st.container(key="gauge_card"):
                    st.markdown('<div style="font-size:14px;font-weight:700;text-align:center;color:black">Kurva (Y)</div>',
                                unsafe_allow_html=True)
                    g1, g2 = st.columns(2)
                    with g1:
                        kolom_gauge(m1_z, "Atas", "g_atas", "gw_atas")
                    with g2:
                        kolom_gauge(m2_z, "Bawah", "g_bawah", "gw_bawah")

        # FSR
        fsr1_val = fsr_val("fsr1") if online else None
        fsr2_val = fsr_val("fsr2") if online else None

        def fsr_card(label, v):
            if v is None:
                angka = '<div class="big-off">Tidak terhubung</div>'
                status = "-"
            else:
                angka = f'<div class="big" style="margin:8px 0">{v}</div>'
                th = _to_float(sensor_data.get("fsr_th"))
                status = "Terpakai" if (th is not None and v > th) else "Longgar"
            return f"""
            <div class="card" style="background:#7BAFD4;text-align:center;color:black;margin-top:16px;min-height:130px">
                <div class="lbl-sm">{label}</div>
                {angka}
                <div class="lbl-sm">{status}</div>
            </div>"""

        with st.container(key="row_fsr"):
            f1, f2 = st.columns(2)
            with f1:
                st.markdown(fsr_card("Pressure Kanan", fsr1_val), unsafe_allow_html=True)
            with f2:
                st.markdown(fsr_card("Pressure Kiri", fsr2_val), unsafe_allow_html=True)

        # JARAK + COUNTDOWN + POSISI
        dist = _to_float(sensor_data.get("distance")) if online else None
        if dist is None:
            dist_txt, dist_cls = "Tidak terhubung", "big-off"
        else:
            dist_txt, dist_cls = f"{dist:.0f} cm", "big"

        duduk = sensor_data.get("duduk") if online else None
        duduk_txt = "Duduk" if duduk is True else "Berdiri" if duduk is False else "Tidak terhubung"

        t_mins, t_secs = divmod(int(timer_state["seconds"]), 60)
                
        if online and not dipakai:
            pesan = "Menunggu<br>korset<br>dipakai"
        elif timer_state["phase"] == "duduk":
            pesan = "Harap<br>Melakukan<br>Perenggangan"
        else:
            pesan = "Bisa<br>kembali<br>duduk"

        d1, d2 = st.columns([3, 2])
        with d1:
            st.markdown(f"""
            <div class="card" style="background:#3E5C76;color:white;margin-top:16px;height:190px;display:flex;flex-direction:column;justify-content:space-between">
                <div style="text-align:center">
                    <div class="lbl-sm">Jarak Ultrasonik</div>
                    <div class="{dist_cls}" style="margin:8px 0">{dist_txt}</div>
                </div>
                <div style="display:flex;justify-content:space-between;align-items:flex-end">
                    <div>
                        <div style="font-size:12px">Setelah</div>
                        <div style="border:1px solid white;padding:0 4px;border-radius:4px;font-size:12px">{t_mins:02d}:{t_secs:02d}</div>
                    </div>
                    <div style="font-size:12px;text-align:right;line-height:1.2">{pesan}</div>
                </div>
            </div>""", unsafe_allow_html=True)
        with d2:
            st.markdown(f"""
            <div class="card" style="background:#3E5C76;color:white;margin-top:16px;height:190px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
                <div class="lbl-sm">Posisi Saat<br>ini</div>
                <div style="font-size:20px;font-weight:700;margin-top:8px">{duduk_txt}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        with st.container(key="btn_bot"):
            st.link_button("➤ KEMBALI KE BOT", "https://t.me/Postur_Alert_bot", use_container_width=True)

    # ================= KOLOM KANAN =================
    with col_kanan:
        view = st.session_state.view
        active = "background:#fff;color:#000;border:1px solid #9ca3af;"
        inactive = "background:#3E5C76;color:#fff;border:none;"
        st.markdown(f"""<style>
        .st-key-btn_tabel button {{ {active if view == 'tabel' else inactive} }}
        .st-key-btn_grafik button {{ {active if view == 'grafik' else inactive} }}
        </style>""", unsafe_allow_html=True)

        with st.container(key="row_btn"):
            b1, b2, _ = st.columns([2, 2, 6])
            with b1:
                st.button("Tabel", key="btn_tabel", on_click=set_view, args=("tabel",))
            with b2:
                st.button("Grafik", key="btn_grafik", on_click=set_view, args=("grafik",))

        if view == "tabel":
            st.markdown('<div style="font-size:20px;font-weight:700;color:black">Riwayat Sesi Postur</div>',
                        unsafe_allow_html=True)
            rows = history_cached()
            if rows:
                render_tabel(rows)
            else:
                st.caption("Belum ada data riwayat.")
        else:
            xdata = list(time_buffer)
            g_a, g_b = st.columns(2)
            with g_a:
                line_chart('Pressure (FSR)', ['Kanan', 'Kiri'], [
                    {'name': 'Kanan', 'type': 'line', 'data': list(fsr1_buffer), 'smooth': True, 'color': '#7BAFD4'},
                    {'name': 'Kiri', 'type': 'line', 'data': list(fsr2_buffer), 'smooth': True, 'color': '#FF8A80'},
                ], xdata, "chart_fsr")
            with g_b:
                line_chart('Kurva (Y)', ['Atas', 'Bawah'], [
                    {'name': 'Atas', 'type': 'line', 'data': list(mpu1_buffer), 'smooth': True, 'color': '#9ACDFF'},
                    {'name': 'Bawah', 'type': 'line', 'data': list(mpu2_buffer), 'smooth': True, 'color': '#FFD54F'},
                ], xdata, "chart_mpu")
            line_chart('Jarak Ultrasonik', None, [
                {'type': 'line', 'data': list(dist_buffer), 'smooth': True, 'color': '#3E5C76'},
            ], xdata, "chart_dist")


dashboard()