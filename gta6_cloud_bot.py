import os
import re
import sys
import json
import time
import queue
import random
import socket
import threading
import warnings
import traceback
import requests
import urllib3.util.connection
from collections import deque
from datetime import datetime, timedelta, timezone

# IPv4 önceliği
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
warnings.filterwarnings("ignore")

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    from playwright.sync_api import sync_playwright

# ==========================================
# 1. TEMEL AYARLAR & SABİTLER
# ==========================================
KICK_COOKIES_RAW = os.environ.get("KICK_COOKIES", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
CURRENT_RUN_ID = os.environ.get("GITHUB_RUN_ID", f"local-{int(time.time())}")

WORKFLOW_FILE = "gta6_drops.yml"
STATE_FILE = "marathon_state.json"
MAX_RUN_SECONDS = int(5.4 * 3600)  # 5 saat 24 dakika sonra bayrak devri
TOTAL_TARGET_MINUTES = 250 * 60    # 15.000 dakika (250 saat)

TR_TZ = timezone(timedelta(hours=3))
GTA6_TR_URL = "https://kick.com/category/grand-theft-auto-vi?sort=viewers_high_to_low&languages=tr"
GTA6_GLOBAL_URL = "https://kick.com/category/grand-theft-auto-vi?sort=viewers_high_to_low"
DROPS_CAMPAIGNS_URL = "https://kick.com/drops/campaigns"

FALLBACK_SLUGS = ["rraenee", "eray", "mithrain", "hype", "elraenn", "cordiseps", "wtcn"]

LOG_BUFFER = deque(maxlen=50)
CMD_QUEUE = queue.Queue()
TEMP_MESSAGES = []  # [(msg_id, expire_timestamp), ...]

def now_tr():
    return datetime.now(TR_TZ)

def is_dnd_hours():
    """Gece 01:00 - 09:00 TR arası sessiz bildirim (DND)."""
    h = now_tr().hour
    return 1 <= h < 9

def sanitize_text(text):
    """Log ve hata mesajlarındaki Telegram token ve çerez değerlerini otomatik maskeler."""
    s = str(text)
    if TELEGRAM_BOT_TOKEN:
        s = s.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")
    s = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{25,60}\b", "***TOKEN***", s)
    s = re.sub(r"(session_token[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;]+)", r"\1***COOKIE***", s, flags=re.IGNORECASE)
    return s

def log(msg):
    ts = now_tr().strftime("%H:%M:%S")
    line = f"[{ts}] {sanitize_text(msg)}"
    print(line, flush=True)
    LOG_BUFFER.append(line)

def format_viewers(count):
    try:
        c = int(count)
        return f"{c / 1000:.1f}K" if c >= 1000 else str(c)
    except Exception:
        return "?"

# ==========================================
# 2. GÖRÜNÜRLÜK KORUMA SCRIPTI
# ==========================================
VISIBILITY_JS = r"""
(() => {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    document.hasFocus = () => true;
    window.addEventListener('blur', (e) => e.stopImmediatePropagation(), true);

    // WebSocket Kancası (Hayalet İzleme / Heartbeat Motoru)
    const activeSockets = new Set();
    let watchPayload = null;
    let handshakePayload = null;
    let pingCount = 0;

    const OrigWebSocket = window.WebSocket;
    const origWSSend = OrigWebSocket.prototype.send;

    window.WebSocket = function(...args) {
        const ws = new OrigWebSocket(...args);
        activeSockets.add(ws);
        return ws;
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;
    window.WebSocket.CONNECTING = OrigWebSocket.CONNECTING;
    window.WebSocket.OPEN = OrigWebSocket.OPEN;
    window.WebSocket.CLOSING = OrigWebSocket.CLOSING;
    window.WebSocket.CLOSED = OrigWebSocket.CLOSED;

    OrigWebSocket.prototype.send = function(data) {
        activeSockets.add(this);
        if (typeof data === 'string') {
            if (data.includes('tracking.user.watch.livestream')) watchPayload = data;
            if (data.includes('channel_handshake')) handshakePayload = data;
        }
        return origWSSend.apply(this, arguments);
    };

    window.__INIT_WATCH_PAYLOAD__ = function(cid, lid, vid) {
        if (!watchPayload && cid && lid) {
            watchPayload = JSON.stringify({
                type: "user_event",
                data: { message: { name: "tracking.user.watch.livestream", channel_id: cid, livestream_id: lid, vod_id: vid || crypto.randomUUID() } }
            });
        }
        if (!handshakePayload && cid) {
            handshakePayload = JSON.stringify({
                type: "channel_handshake",
                data: { message: { channelId: String(cid) } }
            });
        }
    };

    window.__SEND_WS_HEARTBEAT__ = function() {
        activeSockets.forEach(ws => { if (ws.readyState !== 1) activeSockets.delete(ws); });
        let sent = false;
        activeSockets.forEach(ws => {
            if (ws.readyState === 1) {
                try {
                    origWSSend.call(ws, JSON.stringify({ type: "ping" }));
                    if (handshakePayload) origWSSend.call(ws, handshakePayload);
                    if (watchPayload) origWSSend.call(ws, watchPayload);
                    sent = true;
                } catch (e) {}
            }
        });
        if (sent) pingCount++;
        return { wsCount: activeSockets.size, pingCount: pingCount, hasPayload: Boolean(watchPayload) };
    };
})();
"""

# ==========================================
# 3. KALICI HAFIZA (STATE) & ÇİFT SUNUCU KİLİDİ
# ==========================================
def load_state():
    today_str = now_tr().strftime("%Y-%m-%d")
    default_state = {
        "progress_ratio": 0.0,
        "percent": 0.0,
        "remaining_text": "Henüz taranmadı",
        "completed": False,
        "paused": False,
        "notify_switch": True,
        "notify_milestone": True,
        "auto_clean_temp": True,
        "last_milestone": 0,
        "active_run_id": "",
        "health_status": "🟢 Canlı",
        "current_streamer": "Yok",
        "previous_streamer": "",
        "current_title": "Bilinmiyor",
        "current_viewers": 0,
        "blacklist": [],
        "favorites": [],
        "list_lang": "tr",
        "list_page": 0,
        "cached_streamers": [],
        "panel_message_id": None,
        "session_count": 0,
        "stuck_scans": 0,
        "last_update_offset": 0,
        "last_scan_ts": 0.0,
        "last_scan_ratio": 0.0,
        "hourly_history": [],
        "efficiency_text": "Hesaplanıyor (15 dk içinde netleşir)",
        "last_daily_report_date": today_str,
        "daily_start_ratio": 0.0,
        "daily_channel_switches": 0
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
        except Exception as e:
            log(f"⚠️ State okuma hatası: {e}")
    return default_state

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ State yazma hatası: {e}")

def is_session_overtaken():
    """Başka bir sunucu (yeni run_id) kontrolü devraldı mı kontrol eder."""
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            disk_state = json.load(f)
        disk_run = disk_state.get("active_run_id", "")
        if disk_run and disk_run != CURRENT_RUN_ID:
            return True
    except Exception:
        pass
    return False

# ==========================================
# 4. TELEGRAM API & UYGULAMA BENZERİ UI/UX
# ==========================================
def tg_api(method, data=None, files=None, timeout=15, retries=2):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                url,
                data=data,
                files=files,
                headers={"Connection": "close"},
                timeout=(8, timeout)
            )
            if r.ok:
                return r.json()
            if r.status_code == 400 and "message is not modified" in r.text.lower():
                return {"ok": True}
            log(f"[TG API RED - {method}] HTTP {r.status_code}: {sanitize_text(r.text[:150])}")
            return None
        except requests.exceptions.RequestException as e:
            if attempt == retries:
                log(f"[TG API HATA - {method}] {sanitize_text(e)}")
            else:
                time.sleep(1.5)
    return None

def setup_telegram_menu():
    commands = [
        {"command": "durum", "description": "🏠 Canlı kontrol panelini aç"},
        {"command": "yayincilar", "description": "📺 Canlı GTA VI yayıncılarını listele"},
        {"command": "ekran", "description": "📸 Kırpılmış canlı yayın ekranı"},
        {"command": "drops", "description": "🎁 SunsetK Badge ilerlemesini tara"},
        {"command": "ayarlar", "description": "⚙️ Bildirimler, kara liste ve sistem"},
        {"command": "log", "description": "📜 Son 25 çalışma kaydı"}
    ]
    if TELEGRAM_BOT_TOKEN:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
                json={"commands": commands},
                timeout=10
            )
            log("✅ Telegram Menü (setMyCommands) güncellendi.")
        except Exception:
            pass

def answer_toast(cb_id, text, alert=False):
    tg_api("answerCallbackQuery", {
        "callback_query_id": cb_id,
        "text": text[:180],
        "show_alert": "true" if alert else "false"
    }, timeout=5, retries=1)

def delete_message(msg_id):
    if msg_id and TELEGRAM_CHAT_ID:
        tg_api("deleteMessage", {"chat_id": TELEGRAM_CHAT_ID, "message_id": msg_id}, timeout=8, retries=1)

def pin_panel_message(msg_id):
    if msg_id and TELEGRAM_CHAT_ID:
        tg_api("pinChatMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": msg_id,
            "disable_notification": "true"
        }, timeout=8, retries=1)

def register_temp_message(msg_id, state):
    if msg_id and state and state.get("auto_clean_temp", True):
        TEMP_MESSAGES.append((msg_id, time.time() + 600))  # 10 dakika sonra otomatik sil

def cleanup_expired_temp_messages(state):
    if not state.get("auto_clean_temp", True) or not TEMP_MESSAGES:
        return
    now_ts = time.time()
    remaining = []
    for mid, exp_ts in TEMP_MESSAGES:
        if now_ts >= exp_ts:
            delete_message(mid)
        else:
            remaining.append((mid, exp_ts))
    TEMP_MESSAGES[:] = remaining

def build_progress_bar(pct, length=10):
    filled = max(0, min(length, int(round((pct / 100.0) * length))))
    return "█" * filled + "░" * (length - filled)

def calc_hourly_gain_min(state):
    hist = state.get("hourly_history", [])
    if len(hist) < 2:
        return None
    now_ts = time.time()
    cur_ratio = state.get("progress_ratio", 0.0)
    # Son 65 dakika içindeki en eski kaydı bul
    oldest = None
    for ts, r in hist:
        if (now_ts - ts) <= 3900:
            oldest = (ts, r)
            break
    if not oldest or (now_ts - oldest[0]) < 300:
        return None
    gained = max(0.0, (cur_ratio - oldest[1]) * TOTAL_TARGET_MINUTES)
    return int(round(gained))

def get_main_panel_text(state, start_time, session_start_ratio=0.0, banner=""):
    elapsed_min = int((time.time() - start_time) // 60)
    pct = state.get("percent", 0.0)
    bar = build_progress_bar(pct)

    rem_min = max(0, int(round(TOTAL_TARGET_MINUTES * (1.0 - (pct / 100.0)))))
    rem_days = rem_min / 1440.0
    eta_dt = now_tr() + timedelta(minutes=rem_min)
    eta_str = eta_dt.strftime("%d.%m.%Y %H:%M") if pct > 0 else "Hesaplanıyor..."

    # Oturumluk ve saatlik ilerleme kıyası
    sess_gain = 0
    if session_start_ratio > 0 and state.get("progress_ratio", 0.0) >= session_start_ratio:
        sess_gain = int(round((state["progress_ratio"] - session_start_ratio) * TOTAL_TARGET_MINUTES))
    hourly_gain = calc_hourly_gain_min(state)
    hourly_str = f"+{hourly_gain} dk" if hourly_gain is not None else "Ölçülüyor"

    cur = state.get("current_streamer", "Yok")
    is_fav = "⭐ " if cur in state.get("favorites", []) else ""
    title = (state.get("current_title") or "GTA VI")[:55]
    viewers = format_viewers(state.get("current_viewers", 0))
    eff = state.get("efficiency_text", "Hesaplanıyor...")
    pill = state.get("health_status", "🟢 Canlı")
    dnd = "🌙 DND" if is_dnd_hours() else "☀️ Aktif"
    updated_at = now_tr().strftime("%H:%M:%S")

    banner_line = f"⏳ <b>İşlem:</b> <i>{banner}</i>\n━━━━━━━━━━━━━━━━━━━━━━\n" if banner else ""

    return (
        f"{banner_line}"
        f"🎮 <b>GTA VI 250 SAAT CANLI PANO</b>  {pill}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 <b>Kanal:</b> {is_fav}<code>{cur}</code>  (👁️ {viewers})\n"
        f"📝 <b>Başlık:</b> <i>{title}</i>\n"
        f"🎁 <b>SunsetK:</b> <code>{state['remaining_text']}</code>\n"
        f"📊 <b>İlerleme:</b> <code>[{bar}] %{pct:.2f}</code>  (<code>~{rem_days:.1f} gün</code>)\n"
        f"⚡ <b>Kazanç:</b> Bu Oturum <code>+{sess_gain} dk</code> | Son 1 Sa: <code>{hourly_str}</code>\n"
        f"📈 <b>Verimlilik:</b> <code>{eff}</code>\n"
        f"📅 <b>Tahmini Bitiş:</b> <code>{eta_str}</code>\n"
        f"🔁 <b>Oturum:</b> <code>#{state['session_count']}</code> | ⏱️ <code>{elapsed_min} dk</code> | {dnd} | <i>{updated_at}</i>"
    )

def get_main_panel_kb(state):
    cur = state.get("current_streamer", "")
    fav_txt = "💔 Favoriden Çıkar" if cur in state.get("favorites", []) else "⭐ Favoriye Ekle"
    row_quick = [{"text": "🔄 Sonraki Kanal", "callback_data": "act:next"}]
    if state.get("previous_streamer") and state["previous_streamer"] != cur:
        row_quick.append({"text": f"↩️ {state['previous_streamer']}", "callback_data": "act:prev"})

    return {
        "inline_keyboard": [
            [
                {"text": "🏠 Ana Panel", "callback_data": "tab:home"},
                {"text": "📺 Kanallar", "callback_data": "tab:channels"},
                {"text": "⚙️ Ayarlar", "callback_data": "tab:settings"}
            ],
            [
                {"text": "🎁 Drops Tara", "callback_data": "act:drops"},
                {"text": "📸 Canlı Ekran", "callback_data": "act:screen"}
            ],
            row_quick,
            [
                {"text": fav_txt, "callback_data": "act:toggle_fav"},
                {"text": "🚫 Kanalı Engelle", "callback_data": "act:block_cur"}
            ]
        ]
    }

def get_channels_panel_kb(state):
    streamers = state.get("cached_streamers", [])
    page_idx = state.get("list_page", 0)
    per_page = 8
    total_pages = max(1, (len(streamers) + per_page - 1) // per_page)
    page_idx = max(0, min(page_idx, total_pages - 1))
    state["list_page"] = page_idx

    cur = state.get("current_streamer", "")
    favs = set(state.get("favorites", []))
    chunk = streamers[page_idx * per_page : (page_idx + 1) * per_page]

    rows = [
        [
            {"text": "🏠 Ana Panel", "callback_data": "tab:home"},
            {"text": "📺 Kanallar (Aktif)", "callback_data": "tab:channels"},
            {"text": "⚙️ Ayarlar", "callback_data": "tab:settings"}
        ]
    ]

    row = []
    for item in chunk:
        slug = item["slug"]
        v = item.get("viewers", 0)
        icon = "▶️" if slug == cur else ("⭐" if slug in favs else ("🔥" if v >= 5000 else "🟢"))
        row.append({"text": f"{icon} {slug} ({format_viewers(v)})", "callback_data": f"info:{slug}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav_row = []
    if page_idx > 0:
        nav_row.append({"text": "⬅️ Önceki", "callback_data": f"page:{page_idx - 1}"})
    nav_row.append({"text": f"📄 {page_idx + 1}/{total_pages}", "callback_data": "noop"})
    if page_idx < total_pages - 1:
        nav_row.append({"text": "Sonraki ➡️", "callback_data": f"page:{page_idx + 1}"})
    rows.append(nav_row)

    lang_btn = "🌍 Global Liste" if state.get("list_lang", "tr") == "tr" else "🇹🇷 Türkçe Liste"
    rows.append([
        {"text": lang_btn, "callback_data": "act:toggle_lang"},
        {"text": "🔎 Kanal Ara / Yaz", "callback_data": "act:search_channel"},
        {"text": "🔄 Yenile", "callback_data": "act:refresh_list"}
    ])
    return {"inline_keyboard": rows}

def get_streamer_detail_kb(slug, state):
    is_fav = slug in state.get("favorites", [])
    fav_watch_txt = "▶️ İzle (Favori ⭐)" if is_fav else "⭐ Favoriye Ekle & İzle"
    return {
        "inline_keyboard": [
            [
                {"text": "▶️ Bu Kanalı İzle", "callback_data": f"watch:{slug}"},
                {"text": fav_watch_txt, "callback_data": f"favwatch:{slug}"}
            ],
            [
                {"text": "🔙 Kanallar Listesine Dön", "callback_data": "tab:channels"}
            ]
        ]
    }

def get_settings_panel_kb(state):
    sw_txt = "🔄 Kanal Değişimi: AÇIK" if state.get("notify_switch", True) else "🔄 Kanal Değişimi: KAPALI"
    ms_txt = "📊 %5 Kilometre Taşı: AÇIK" if state.get("notify_milestone", True) else "📊 %5 Kilometre Taşı: KAPALI"
    ac_txt = "🧹 Oto-Temizlik (10 dk): AÇIK" if state.get("auto_clean_temp", True) else "🧹 Oto-Temizlik: KAPALI"
    return {
        "inline_keyboard": [
            [
                {"text": "🏠 Ana Panel", "callback_data": "tab:home"},
                {"text": "📺 Kanallar", "callback_data": "tab:channels"},
                {"text": "⚙️ Ayarlar (Aktif)", "callback_data": "tab:settings"}
            ],
            [{"text": sw_txt, "callback_data": "act:toggle_notify_switch"}],
            [{"text": ms_txt, "callback_data": "act:toggle_notify_milestone"}],
            [
                {"text": ac_txt, "callback_data": "act:toggle_autoclean"},
                {"text": "📜 Son Loglar", "callback_data": "act:log"}
            ],
            [
                {"text": f"🚫 Kara Liste ({len(state.get('blacklist', []))})", "callback_data": "tab:blacklist"},
                {"text": f"⭐ Favoriler ({len(state.get('favorites', []))})", "callback_data": "tab:favorites"}
            ],
            [
                {"text": "♻️ Yeniden Başlat", "callback_data": "confirm:restart"},
                {"text": "🛑 Maratonu Durdur", "callback_data": "confirm:stop"}
            ]
        ]
    }

def get_manage_list_kb(items, prefix):
    rows = []
    row = []
    for slug in items[:12]:
        row.append({"text": f"❌ {slug}", "callback_data": f"{prefix}:{slug}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🔙 Ayarlara Dön", "callback_data": "tab:settings"}])
    return {"inline_keyboard": rows}

def get_confirm_kb(action):
    label = "✅ Evet, Durdur" if action == "stop" else "✅ Evet, Yeniden Başlat"
    return {
        "inline_keyboard": [
            [
                {"text": label, "callback_data": f"exec:{action}"},
                {"text": "❌ İptal", "callback_data": "tab:settings"}
            ]
        ]
    }

def render_panel(state, text, kb, msg_id=None, create_new=False, critical=False):
    """Tek mesajda canlı pano: mevcut paneli günceller, yoksa yeni oluşturup sabitler."""
    if not TELEGRAM_CHAT_ID:
        return None
    target_id = msg_id or state.get("panel_message_id")
    silent = "true" if (not critical and is_dnd_hours()) else "false"

    if target_id and not create_new:
        res = tg_api("editMessageText", {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": target_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(kb)
        })
        if res and res.get("ok"):
            return target_id

    res = tg_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "disable_notification": silent,
        "reply_markup": json.dumps(kb)
    })
    if res and res.get("ok"):
        new_id = res["result"]["message_id"]
        state["panel_message_id"] = new_id
        save_state(state)
        pin_panel_message(new_id)
        return new_id
    return None

def send_temp_message(text, critical=False, state=None, auto_clean=True):
    """Altında [🗑️ Bu Mesajı Sil] butonu olan ve isteğe bağlı 10 dk sonra otomatik silinen mesaj."""
    if not TELEGRAM_CHAT_ID:
        return None
    silent = "true" if (not critical and is_dnd_hours()) else "false"
    res = tg_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": sanitize_text(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "disable_notification": silent,
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🗑️ Bu Mesajı Sil", "callback_data": "act:del_msg"}]]})
    })
    if res and res.get("ok"):
        mid = res["result"]["message_id"]
        if auto_clean:
            register_temp_message(mid, state)
        return mid
    return None

def send_cropped_screenshot(page, caption, critical=False, state=None):
    """Mobil uyumlu kırpılmış video oynatıcı ekran görüntüsü + 10 dk otomatik temizlik."""
    if not TELEGRAM_CHAT_ID:
        return
    path = "live_screen.png"
    try:
        video_el = page.locator("video").first
        box = video_el.bounding_box() if video_el.count() > 0 else None
        if box and box["width"] > 200 and box["height"] > 120:
            page.screenshot(path=path, clip=box)
        else:
            page.screenshot(path=path, full_page=False)

        silent = "true" if (not critical and is_dnd_hours()) else "false"
        with open(path, "rb") as photo:
            res = tg_api("sendPhoto", data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": sanitize_text(caption)[:1000],
                "parse_mode": "HTML",
                "disable_notification": silent,
                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🗑️ Bu Mesajı Sil", "callback_data": "act:del_msg"}]]})
            }, files={"photo": photo}, timeout=25)
            if res and res.get("ok") and not critical:
                register_temp_message(res["result"]["message_id"], state)
    except Exception as e:
        log(f"⚠️ Ekran görüntüsü hatası: {e}")

# ==========================================
# 5. ARKA PLAN TELEGRAM DİNLEYİCİSİ (WHITELIST + COOLDOWN)
# ==========================================
def telegram_listener_thread(state_ref, stop_event):
    """
    Sıkı chat_id + from.id beyaz listesi (Hayalet Mod) ve
    kuyruk taşma/spam koruması (Cooldown) ile arka planda dinler.
    """
    offset = state_ref.get("last_update_offset", 0)
    last_action_ts = {}
    heavy_actions = {"act:next", "act:prev", "act:drops", "act:refresh_list", "act:toggle_lang", "act:screen"}

    while not stop_event.is_set():
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(5)
            continue
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset + 1, "timeout": 10},
                headers={"Connection": "close"},
                timeout=15
            )
            if not r.ok:
                time.sleep(2)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"]
                state_ref["last_update_offset"] = offset

                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    from_id = str(cb.get("from", {}).get("id", ""))
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))

                    # Sıkı Beyaz Liste (Hayalet Mod)
                    if TELEGRAM_CHAT_ID and (from_id != str(TELEGRAM_CHAT_ID) or chat_id != str(TELEGRAM_CHAT_ID)):
                        continue

                    cb_id = cb["id"]
                    data = (cb.get("data") or "").strip()
                    msg_id = cb.get("message", {}).get("message_id")

                    if data == "noop":
                        answer_toast(cb_id, "Mevcut sayfa")
                        continue
                    if data == "act:del_msg":
                        answer_toast(cb_id, "Mesaj silindi 🗑️")
                        delete_message(msg_id)
                        continue

                    # Spam / Cooldown Koruması (Ağır işlemlerde 3 sn bekleme)
                    now_ts = time.time()
                    if data in heavy_actions and (now_ts - last_action_ts.get(data, 0)) < 3.0:
                        answer_toast(cb_id, "⏳ Bu işlem zaten sıraya alındı, lütfen bekle...")
                        continue
                    last_action_ts[data] = now_ts

                    toast_map = {
                        "tab:home": "🏠 Ana Panel",
                        "tab:channels": "📺 Kanallar açılıyor...",
                        "tab:settings": "⚙️ Ayarlar",
                        "act:drops": "🎁 Drops taranıyor...",
                        "act:screen": "📸 Ekran görüntüsü alınıyor...",
                        "act:next": "🔄 Sonraki GTA VI kanalına geçiliyor...",
                        "act:prev": "↩️ Önceki kanala dönülüyor...",
                        "act:refresh_list": "🔄 Canlı GTA VI listesi yenileniyor...",
                        "act:toggle_lang": "🌍 Dil filtresi değiştiriliyor...",
                        "act:search_channel": "🔎 Kanal adını yazıp gönder...",
                        "act:log": "📜 Loglar getiriliyor..."
                    }
                    answer_toast(cb_id, toast_map.get(data, "⏳ İşleniyor..."))
                    CMD_QUEUE.put(("cb", data, msg_id))

                elif "message" in upd and "text" in upd["message"]:
                    msg = upd["message"]
                    from_id = str(msg.get("from", {}).get("id", ""))
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    # Sıkı Beyaz Liste (Hayalet Mod)
                    if TELEGRAM_CHAT_ID and (from_id != str(TELEGRAM_CHAT_ID) or chat_id != str(TELEGRAM_CHAT_ID)):
                        continue

                    txt = msg["text"].strip()
                    reply_to = msg.get("reply_to_message", {}).get("text", "")
                    CMD_QUEUE.put(("msg", txt, reply_to))
        except Exception:
            time.sleep(2)

# ==========================================
# 6. CLOUDFLARE, KAYNAK ENGELLEYİCİ & ÇEREZ EXPIRY
# ==========================================
def bypass_cloudflare(page):
    for attempt in range(4):
        try:
            title = (page.title() or "").lower()
            body = page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
            is_cf = (
                "just a moment" in title or "bir dakika" in title
                or "güvenlik doğrulaması" in body or "verify you are human" in body
                or "gerçek kişi olduğunuzu" in body
            )
            if not is_cf:
                return True

            log(f"🛡️ Cloudflare tespit edildi (Deneme {attempt + 1}/4)...")
            page.wait_for_timeout(3000)

            box = None
            for frame in page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    box = frame.frame_element().bounding_box()
                    if box:
                        break

            if box:
                tx = box["x"] + 28 + random.uniform(-3, 3)
                ty = box["y"] + (box["height"] / 2) + random.uniform(-3, 3)
                page.mouse.move(tx - 60, ty + 20, steps=10)
                page.wait_for_timeout(random.randint(150, 300))
                page.mouse.move(tx, ty, steps=12)
                page.mouse.click(tx, ty)
            else:
                page.mouse.move(215, 295, steps=12)
                page.mouse.click(215, 295)
            page.wait_for_timeout(5000)
        except Exception:
            page.wait_for_timeout(2000)
    return False

def setup_resource_blocker(context):
    blocked_domains = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.net", "datadoghq.com", "sentry.io", "hotjar.com", "intercom.io"
    )
    def route_handler(route):
        url = route.request.url.lower()
        res_type = route.request.resource_type
        # Ağır video parçalarını engelle (Hayalet izleme için sadece WebSocket + API yeterli)
        if ".ts" in url or ".m4s" in url or "live-video.net" in url:
            route.abort()
            return
        if (
            "challenges.cloudflare.com" in url or "kick.com/api/" in url
            or "websockets.kick.com" in url
        ):
            route.continue_()
            return
        if any(d in url for d in blocked_domains) or res_type in ("font", "image", "media"):
            route.abort()
            return
            return
        route.continue_()
    context.route("**/*", route_handler)

def parse_and_load_cookies(context, state):
    """Çerezleri yükler ve oturum çerezlerinin son kullanma tarihini (24 sa uyarısı) kontrol eder."""
    if not KICK_COOKIES_RAW:
        log("❌ KICK_COOKIES_JSON secret bulunamadı!")
        return False

    cookies_to_add = []
    expiring_soon = []
    now_ts = time.time()

    try:
        raw_list = json.loads(KICK_COOKIES_RAW)
        if isinstance(raw_list, dict) and "cookies" in raw_list:
            raw_list = raw_list["cookies"]
        if isinstance(raw_list, list):
            for c in raw_list:
                name, value = c.get("name"), c.get("value")
                if not name or value is None:
                    continue
                domain = c.get("domain", ".kick.com")
                if "kick.com" not in domain:
                    continue

                # Çerez geçerlilik süresi (expirationDate) kontrolü
                exp = c.get("expirationDate")
                if exp and any(k in str(name).lower() for k in ("session", "token", "auth", "kick")):
                    rem_sec = float(exp) - now_ts
                    if 0 < rem_sec < 86400:
                        expiring_soon.append((str(name), round(rem_sec / 3600.0, 1)))

                cookies_to_add.append({
                    "name": str(name), "value": str(value),
                    "domain": domain if domain.startswith(".") else f".{domain}",
                    "path": c.get("path", "/"), "secure": bool(c.get("secure", True)),
                    "httpOnly": bool(c.get("httpOnly", False)), "sameSite": "Lax"
                })
    except Exception:
        pass

    if cookies_to_add:
        context.add_cookies(cookies_to_add)
        log(f"✅ {len(cookies_to_add)} adet Kick çerezi yüklendi.")
        if expiring_soon:
            details = ", ".join(f"{n} (~{h} sa)" for n, h in expiring_soon[:3])
            log(f"⚠️ Çerez süresi dolmak üzere: {details}")
            send_temp_message(
                f"⚠️ <b>ÇEREZ SÜRESİ UYARISI!</b>\n"
                f"Kick oturum çerezlerinin süresi 24 saatten az kaldı: <code>{details}</code>\n"
                f"Oturum düşmeden önce <code>KICK_COOKIES_JSON</code> değerini yenilemen önerilir.",
                critical=True, state=state, auto_clean=False
            )
        return True
    return False

def check_session_authenticated(page):
    try:
        return bool(page.evaluate(r"""() => {
            const html = document.documentElement.innerHTML || "";
            if (html.includes('"status":"authenticated"') || html.includes('session_token')) return true;
            const loginBtn = Array.from(document.querySelectorAll('button, a')).find(el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'giriş yap' || t === 'log in';
            });
            return !loginBtn;
        }"""))
    except Exception:
        return True

# ==========================================
# 7. SADECE GERÇEK GTA VI YAYINCILARINI ÇEKME & TEKİL DOĞRULAMA
# ==========================================
def fetch_single_channel_info(page, slug):
    """Tek bir kanalın canlılık, GTA VI kategorisi, başlık, izleyici ve drops bilgisini çeker."""
    try:
        return page.evaluate(f"""async () => {{
            try {{
                const r = await fetch('https://kick.com/api/v2/channels/{slug}');
                if (!r.ok) return null;
                const d = await r.json();
                if (!d || !d.livestream || !d.livestream.is_live) return {{ slug: '{slug}', is_live: false }};
                const cats = d.livestream.categories || [];
                const isGta6 = cats.some(c =>
                    (c.slug || '').toLowerCase().includes('grand-theft-auto-vi') ||
                    (c.name || '').toLowerCase().includes('grand theft auto vi')
                );
                const tags = (d.livestream.tags || []).map(t => String(t).toLowerCase());
                const dropsActive = isGta6 || tags.some(t => t.includes('drop'));
                return {{
                    slug: '{slug}',
                    is_live: true,
                    is_gta6: isGta6,
                    drops_active: dropsActive,
                    viewers: d.livestream.viewer_count || 0,
                    title: d.livestream.session_title || 'GTA VI'
                }};
            }} catch(e) {{ return null; }}
        }}""")
    except Exception:
        return None

def fetch_gta6_streamers_strict(context, state):
    lang = state.get("list_lang", "tr")
    url = GTA6_TR_URL if lang == "tr" else GTA6_GLOBAL_URL
    bl_set = set(s.lower() for s in state.get("blacklist", []))
    fav_set = set(s.lower() for s in state.get("favorites", []))

    scan_page = None
    candidates = []
    try:
        log(f"🔍 GTA VI ({lang.upper()}) kategorisi taranıyor...")
        scan_page = context.new_page()
        scan_page.goto(url, wait_until="domcontentloaded", timeout=35000)
        bypass_cloudflare(scan_page)
        scan_page.wait_for_timeout(4000)

        dom_slugs = scan_page.evaluate(r"""() => {
            const ignore = new Set([
                'categories', 'category', 'drops', 'browse', 'following',
                'search', 'dashboard', 'settings', 'terms', 'privacy', 'transactions'
            ]);
            const root = document.querySelector('main') || document.body;
            const found = [];
            root.querySelectorAll('a[href^="/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/^\/([a-zA-Z0-9_]{3,25})$/);
                if (m) {
                    const s = m[1].toLowerCase();
                    if (!ignore.has(s) && !found.includes(s)) found.push(s);
                }
            });
            return found.slice(0, 24);
        }""")

        for s in dom_slugs:
            if s not in bl_set and s not in candidates:
                candidates.append(s)

        for f in fav_set:
            if f not in bl_set and f not in candidates:
                candidates.insert(0, f)

        verified = []
        for slug in candidates[:18]:
            info = fetch_single_channel_info(scan_page, slug)
            if info and info.get("is_live") and info.get("is_gta6"):
                verified.append(info)

        scan_page.close()
    except Exception as e:
        log(f"⚠️ Yayıncı tarama uyarısı: {e}")
        if scan_page:
            try:
                scan_page.close()
            except Exception:
                pass
        verified = []

    if not verified:
        verified = [{"slug": s, "viewers": 0, "title": "GTA VI", "drops_active": True} for s in FALLBACK_SLUGS if s not in bl_set]
    else:
        verified.sort(key=lambda x: (x["slug"] in fav_set, x["viewers"]), reverse=True)

    state["cached_streamers"] = verified
    save_state(state)
    log(f"✅ Doğrulanmış GTA VI yayıncıları ({len(verified)}): {', '.join(x['slug'] for x in verified[:5])}")
    return verified

# ==========================================
# 8. DROPS TARAYICI + VERİMLİLİK + %5 KİLOMETRE TAŞI
# ==========================================
def scan_and_claim_drops(context, state):
    drop_page = None
    summary = "Drops sayfası taranamadı."
    try:
        drop_page = context.new_page()
        drop_page.goto(DROPS_CAMPAIGNS_URL, wait_until="domcontentloaded", timeout=35000)
        bypass_cloudflare(drop_page)
        drop_page.wait_for_timeout(5000)

        claimed = drop_page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                const t = (b.innerText || '').trim().toLowerCase();
                if ((t === 'talep et' || t === 'claim' || t.includes('ödülü al')) && !b.disabled) {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")

        if claimed:
            drop_page.wait_for_timeout(3000)
            state["completed"] = True
            state["progress_ratio"] = 1.0
            state["percent"] = 100.0
            state["remaining_text"] = "0 sa. 0 dk. (TAMAMLANDI! 🏆)"
            save_state(state)
            send_cropped_screenshot(drop_page, "🎉🏆 <b>MÜJDE! 250 Saatlik GTA VI SunsetK Badge Ödülü Otomatik Talep Edildi!</b>", critical=True, state=state)
            drop_page.close()
            return "Ödül talep edildi!"

        card_data = drop_page.evaluate(r"""() => {
            const items = Array.from(document.querySelectorAll('li'));
            for (const li of items) {
                if ((li.innerText || '').includes('SunsetK Badge')) {
                    const bar = li.querySelector('[role="progressbar"]');
                    const valNow = bar ? (bar.getAttribute('aria-valuenow') || bar.getAttribute('data-value')) : null;
                    return { valNow: valNow ? parseFloat(valNow) : null };
                }
            }
            return null;
        }""")

        if card_data and card_data.get("valNow") is not None:
            ratio = float(card_data["valNow"])
            now_ts = time.time()
            old_ratio = float(state.get("progress_ratio", 0.0))
            last_ts = float(state.get("last_scan_ts", 0.0))
            last_ratio = float(state.get("last_scan_ratio", 0.0))

            if state.get("daily_start_ratio", 0.0) == 0.0:
                state["daily_start_ratio"] = ratio

            # Saatlik geçmişi güncelle (son 2 saati tut)
            hist = state.get("hourly_history", [])
            hist.append([now_ts, ratio])
            state["hourly_history"] = [x for x in hist if (now_ts - x[0]) <= 7200][-20:]

            # Verimlilik hesabı (en az 3 dakika geçmişse)
            if last_ts > 0 and (now_ts - last_ts) >= 180:
                real_min = (now_ts - last_ts) / 60.0
                gained_min = max(0.0, (ratio - last_ratio) * TOTAL_TARGET_MINUTES)
                eff_pct = min(100, int(round((gained_min / real_min) * 100)))
                icon = "🟢" if eff_pct >= 85 else ("🟡" if eff_pct >= 50 else "🔴")
                state["efficiency_text"] = f"{icon} %{eff_pct} ({int(round(gained_min))}/{int(round(real_min))} dk)"

            if old_ratio > 0 and ratio <= old_ratio:
                state["stuck_scans"] = state.get("stuck_scans", 0) + 1
                if state["stuck_scans"] >= 2:
                    state["health_status"] = "🔴 Takılma Uyarısı"
            else:
                state["stuck_scans"] = 0
                state["health_status"] = "🟢 Canlı"

            pct = round(ratio * 100.0, 2)
            watched_min = int(round(ratio * TOTAL_TARGET_MINUTES))
            rem_min = max(0, TOTAL_TARGET_MINUTES - watched_min)
            state["progress_ratio"] = ratio
            state["percent"] = pct
            state["last_scan_ts"] = now_ts
            state["last_scan_ratio"] = ratio
            state["remaining_text"] = f"{rem_min // 60} sa. {rem_min % 60} dk. kaldı (İzlenen: {watched_min // 60} sa. {watched_min % 60} dk.)"

            # %5 İlerleme Kilometre Taşı Bildirimi
            current_milestone = int(pct // 5) * 5
            if current_milestone >= 5 and current_milestone > state.get("last_milestone", 0):
                state["last_milestone"] = current_milestone
                if state.get("notify_milestone", True):
                    send_temp_message(
                        f"🎯 <b>KİLOMETRE TAŞI: %{current_milestone} GEÇİLDİ!</b>\n"
                        f"🎁 <code>{state['remaining_text']}</code>",
                        critical=False, state=state, auto_clean=False
                    )

            save_state(state)
            summary = f"{state['remaining_text']} (%{pct:.2f})"
            log(f"🎁 Drops: {summary} | Verimlilik: {state['efficiency_text']}")

        drop_page.close()
    except Exception as e:
        log(f"⚠️ Drops tarama hatası: {e}")
        if drop_page:
            try:
                drop_page.close()
            except Exception:
                pass
    return summary

# ==========================================
# 9. YAYIN AÇICI, 160P & SOHBET KAPATICI
# ==========================================
def open_stream(page, slug, state, count_switch=False):
    old_slug = state.get("current_streamer", "")
    if old_slug and old_slug != "Yok" and old_slug != slug:
        state["previous_streamer"] = old_slug
        if count_switch:
            state["daily_channel_switches"] = state.get("daily_channel_switches", 0) + 1

    url = f"https://kick.com/{slug}"
    log(f"📺 Yayın açılıyor: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=35000)
    bypass_cloudflare(page)
    page.wait_for_timeout(4000)

    page.evaluate(r"""() => {
        const keywords = ['start watching', 'izlemeye başla', '18+', 'kabul et', 'accept', 'onayla'];
        document.querySelectorAll('button').forEach(b => {
            const t = (b.innerText || '').trim().toLowerCase();
            if (keywords.some(k => t.includes(k))) {
                try { b.click(); } catch(e) {}
            }
        });
        const v = document.querySelector('video');
        if (v) {
            v.muted = true;
            v.play().catch(() => {});
        }
        ['#channel-chatroom', '[data-testid="chatroom"]', 'aside'].forEach(sel => {
            document.querySelectorAll(sel).forEach(el => {
                if ((el.innerText || '').toLowerCase().includes('sohbet') || (el.innerText || '').toLowerCase().includes('chat') || sel.includes('chat')) {
                    el.style.display = 'none';
                    el.innerHTML = '';
                }
            });
        });
    }""")

    try:
        settings_btn = page.locator('button[aria-label*="Settings"], button[aria-label*="Ayarlar"]').first
        if settings_btn.is_visible(timeout=2000):
            settings_btn.click()
            page.wait_for_timeout(500)
            q_btn = page.locator('text=/160p/i').first
            if q_btn.is_visible(timeout=1500):
                q_btn.click()
    except Exception:
        pass

    # API'den channel_id ve livestream_id çekip WebSocket hayalet izleme payload'ını kilitle
    page.evaluate(f"""async () => {{
        try {{
            const r = await fetch('https://kick.com/api/v2/channels/{slug}');
            if (!r.ok) return;
            const d = await r.json();
            if (d && d.livestream) {{
                const vid = d.livestream.video?.uuid || d.recent_streams?.[0]?.video?.uuid || '';
                if (window.__INIT_WATCH_PAYLOAD__) window.__INIT_WATCH_PAYLOAD__(d.id, d.livestream.id, vid);
                if (window.__SEND_WS_HEARTBEAT__) window.__SEND_WS_HEARTBEAT__();
            }}
        }} catch(e) {{}}
    }}""")

    meta = fetch_single_channel_info(page, slug)
    state["current_streamer"] = slug
    state["health_status"] = "🟢 Hayalet İzleme"
    if meta and meta.get("is_live"):
        state["current_viewers"] = meta.get("viewers", 0)
        state["current_title"] = meta.get("title", "GTA VI")
    save_state(state)

def check_stream_health(page, state, last_video_time):
    slug = state["current_streamer"]
    try:
        res = page.evaluate(f"""async () => {{
            const v = document.querySelector('video');
            let curTime = -1;
            let readyState = 4;
            if (v) {{
                if (v.paused) v.play().catch(() => {{}});
                curTime = v.currentTime;
                readyState = v.readyState;
            }}
            const bodyTxt = (document.body ? document.body.innerText : '').toLowerCase();
            const isDisconnected = (
                bodyTxt.includes('bağlantı kesildi') ||
                bodyTxt.includes('şimdi yeniden dene') ||
                bodyTxt.includes('connection lost') ||
                bodyTxt.includes('try again now')
            );
            try {{
                const r = await fetch('https://kick.com/api/v2/channels/{slug}');
                if (!r.ok) return {{ ok: true, curTime: curTime, readyState: readyState, isDisconnected: isDisconnected, isGta6: true }};
                const d = await r.json();
                const isLive = Boolean(d && d.livestream && d.livestream.is_live);
                const cats = (d && d.livestream && d.livestream.categories) ? d.livestream.categories : [];
                const isGta6 = cats.some(c => (c.slug || '').includes('grand-theft-auto-vi') || (c.name || '').toLowerCase().includes('grand theft auto vi'));
                return {{ ok: isLive, curTime: curTime, readyState: readyState, isDisconnected: isDisconnected, isGta6: isGta6, viewers: d?.livestream?.viewer_count || 0, title: d?.livestream?.session_title || '' }};
            }} catch(e) {{ return {{ ok: true, curTime: curTime, readyState: readyState, isDisconnected: isDisconnected, isGta6: true }}; }}
        }}""")

        if res.get("viewers"):
            state["current_viewers"] = res["viewers"]
        if res.get("title"):
            state["current_title"] = res["title"]
        save_state(state)

        cur_time = res.get("curTime", -1)
        if not res.get("ok", True):
            return "offline", cur_time
        if not res.get("isGta6", True):
            return "wrong_game", cur_time
        # Hayalet modda video yerine WebSocket sağlığını kontrol et
        ws_ok = page.evaluate("() => window.__SEND_WS_HEARTBEAT__ ? window.__SEND_WS_HEARTBEAT__().wsCount > 0 : true")
        if not ws_ok:
            return "disconnected", cur_time
        return "healthy", cur_time
    except Exception:
        return "healthy", last_video_time

def recover_stream_player(page, state):
    """
    Aşama 1: 'Şimdi yeniden dene' butonuna tıklar.
    Aşama 2: Düzelmezse F5 atıp 160p + sohbet gizlemeyi tekrar uygular.
    Düzelirse True, hala kopuksa False (kanal değiştir) döner.
    """
    def _is_playing():
        try:
            t1 = page.evaluate("() => { const v = document.querySelector('video'); return v ? v.currentTime : -1; }")
            page.wait_for_timeout(3000)
            t2 = page.evaluate("() => { const v = document.querySelector('video'); return v ? v.currentTime : -1; }")
            return t2 > t1 and t2 > 0
        except Exception:
            return False

    # Aşama 1: Ekrandaki 'Şimdi yeniden dene' butonuna tıkla
    try:
        clicked = page.evaluate(r"""() => {
            const keys = ['şimdi yeniden dene', 'try again', 'yeniden dene', 'reconnect'];
            for (const b of document.querySelectorAll('button')) {
                const t = (b.innerText || '').trim().toLowerCase();
                if (keys.some(k => t.includes(k))) { b.click(); return true; }
            }
            return false;
        }""")
        if clicked:
            log("🔄 'Şimdi yeniden dene' butonuna tıklandı, akış kontrol ediliyor...")
            page.wait_for_timeout(4000)
            if _is_playing():
                log("✅ Aşama 1 başarılı: Yayın kaldığı yerden devam ediyor.")
                return True
    except Exception:
        pass

    # Aşama 2: F5 (Reload) + 160p + Sohbet Kapatma
    try:
        log("🔄 Aşama 2: Sayfa yenileniyor (F5)...")
        page.reload(wait_until="domcontentloaded", timeout=35000)
        bypass_cloudflare(page)
        page.wait_for_timeout(4000)
        page.evaluate(r"""() => {
            const v = document.querySelector('video');
            if (v) { v.muted = true; v.play().catch(() => {}); }
            ['#channel-chatroom', '[data-testid="chatroom"]', 'aside'].forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    if ((el.innerText || '').toLowerCase().includes('sohbet') || (el.innerText || '').toLowerCase().includes('chat') || sel.includes('chat')) {
                        el.style.display = 'none'; el.innerHTML = '';
                    }
                });
            });
        }""")
        try:
            settings_btn = page.locator('button[aria-label*="Settings"], button[aria-label*="Ayarlar"]').first
            if settings_btn.is_visible(timeout=2000):
                settings_btn.click()
                page.wait_for_timeout(500)
                q_btn = page.locator('text=/160p/i').first
                if q_btn.is_visible(timeout=1500):
                    q_btn.click()
        except Exception:
            pass

        if _is_playing():
            log("✅ Aşama 2 başarılı: F5 sonrası yayın akıyor.")
            return True
    except Exception:
        pass

    return False

# ==========================================
# 10. GÜNLÜK RAPOR & BAYRAK DEVRİ
# ==========================================
def check_daily_report(state):
    today_str = now_tr().strftime("%Y-%m-%d")
    last_date = state.get("last_daily_report_date", today_str)
    if today_str != last_date:
        gained_min = int(round(max(0.0, state.get("progress_ratio", 0.0) - state.get("daily_start_ratio", 0.0)) * TOTAL_TARGET_MINUTES))
        rem_days = round(max(0, (1.0 - state.get("progress_ratio", 0.0)) * TOTAL_TARGET_MINUTES) / 1440.0, 1)
        msg = (
            f"🌙 <b>GÜNLÜK PERFORMANS ÖZETİ ({last_date})</b>\n\n"
            f"⏱️ <b>Son 24 Saatte Kasılan:</b> <code>{gained_min // 60} sa. {gained_min % 60} dk.</code>\n"
            f"🔄 <b>Kanal Değişimi:</b> <code>{state.get('daily_channel_switches', 0)} kez</code>\n"
            f"📊 <b>Toplam İlerleme:</b> <code>%{state.get('percent', 0.0):.2f}</code>\n"
            f"📅 <b>Hedefe Kalan Tahmini Gün:</b> <code>~{rem_days} gün</code>"
        )
        send_temp_message(msg, critical=True, state=state, auto_clean=False)
        state["last_daily_report_date"] = today_str
        state["daily_start_ratio"] = state.get("progress_ratio", 0.0)
        state["daily_channel_switches"] = 0
        save_state(state)

def trigger_next_workflow(state, reason="5.4 saatlik oturum tamamlandı"):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"ref": "main"},
            timeout=15
        )
        if r.status_code in (200, 204):
            log(f"🔄 Yeni workflow tetiklendi ({reason}).")
            return True
    except Exception as e:
        log(f"❌ Workflow tetikleme hatası: {e}")
    return False

# ==========================================
# 11. KUYRUKTAKİ KOMUTLARI İŞLEME (UI/UX YÖNETİCİSİ)
# ==========================================
def process_queued_commands(page, context, bot_ctx):
    state = bot_ctx["state"]
    sess_ratio = bot_ctx["session_start_ratio"]

    # Kuyruktaki tüm bekleyen komutları çek ve aynı ağır komutları birleştir (deduplication)
    pending = []
    seen_heavy = set()
    while not CMD_QUEUE.empty():
        item = CMD_QUEUE.get_nowait()
        kind, payload, extra = item
        if kind == "cb" and payload in ("act:next", "act:drops", "act:refresh_list", "act:screen"):
            if payload in seen_heavy:
                continue
            seen_heavy.add(payload)
        pending.append(item)

    for kind, payload, extra in pending:
        if kind == "msg":
            txt = payload
            reply_to = extra or ""
            cmd = txt.lower()

            # ForceReply ile gelen kanal arama cevabı
            if bot_ctx.get("awaiting_channel_reply") or "kanal adını yaz" in reply_to.lower():
                bot_ctx["awaiting_channel_reply"] = False
                if not cmd.startswith("/"):
                    target = re.sub(r"[^a-zA-Z0-9_]", "", txt.strip().lower())
                    if target:
                        info = fetch_single_channel_info(page, target)
                        if info and info.get("is_live") and info.get("is_gta6"):
                            open_stream(page, target, state, count_switch=True)
                            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), critical=True)
                            send_temp_message(f"✅ <code>{target}</code> doğrulandı (GTA VI canlı) ve açıldı!", critical=True, state=state)
                        elif info and info.get("is_live"):
                            send_temp_message(f"⚠️ <code>{target}</code> şu an canlı ama <b>GTA VI</b> kategorisinde değil!", critical=True, state=state)
                        else:
                            send_temp_message(f"❌ <code>{target}</code> şu an yayında değil veya bulunamadı.", critical=True, state=state)
                    continue

            if cmd.startswith("/durum") or cmd.startswith("/start"):
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), create_new=True, critical=True)
            elif cmd.startswith("/yayincilar"):
                if not state.get("cached_streamers"):
                    fetch_gta6_streamers_strict(context, state)
                lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
                render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\nDetay ve seçim için kanala dokun:", get_channels_panel_kb(state), create_new=True, critical=True)
            elif cmd.startswith("/ayarlar"):
                render_panel(state, "⚙️ <b>BOT AYARLARI VE BİLDİRİM TERCİHLERİ</b>\n<i>🚨 Kritik Uyarılar (Oturum/Takılma): Her Zaman Açık</i>", get_settings_panel_kb(state), create_new=True, critical=True)
            elif cmd.startswith("/ekran"):
                send_cropped_screenshot(page, f"📸 <b>Canlı Yayın:</b> <code>{state['current_streamer']}</code>", critical=True, state=state)
            elif cmd.startswith("/drops"):
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio, banner="SunsetK Badge taranıyor..."), get_main_panel_kb(state), critical=True)
                scan_and_claim_drops(context, state)
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), critical=True)
            elif cmd.startswith("/log"):
                logs_txt = "\n".join(list(LOG_BUFFER)[-25:]) or "Henüz log yok."
                send_temp_message(f"📜 <b>Son 25 Log Satırı:</b>\n<pre>{logs_txt[-3500:]}</pre>", critical=True, state=state)
            elif cmd.startswith("/izle "):
                target = txt.split(" ", 1)[1].strip().lower()
                if target:
                    open_stream(page, target, state, count_switch=True)
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), critical=True)
            continue

        # Callback (Buton) İşlemleri
        data = payload
        msg_id = extra

        if data == "tab:home":
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "tab:channels":
            if not state.get("cached_streamers"):
                fetch_gta6_streamers_strict(context, state)
            lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
            render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\n<i>Detay görmek ve izlemek için kanala dokun:</i>", get_channels_panel_kb(state), msg_id=msg_id, critical=True)

        elif data.startswith("info:"):
            # Akıllı Yayıncı Kartı & Seçim Detayı
            slug = data.split(":", 1)[1]
            item = next((x for x in state.get("cached_streamers", []) if x["slug"] == slug), None)
            if not item:
                item = fetch_single_channel_info(page, slug) or {"slug": slug, "viewers": 0, "title": "GTA VI", "drops_active": True}
            drops_str = "✅ Aktif (GTA VI)" if item.get("drops_active", True) else "⚠️ Belirsiz"
            card_txt = (
                f"📺 <b>YAYINCI DETAY KARTI</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Kanal:</b> <code>{slug}</code>\n"
                f"👁️ <b>İzleyici:</b> <code>{format_viewers(item.get('viewers', 0))}</code>\n"
                f"🎁 <b>Drops Durumu:</b> {drops_str}\n"
                f"📝 <b>Başlık:</b> <i>{(item.get('title') or 'GTA VI')[:90]}</i>"
            )
            render_panel(state, card_txt, get_streamer_detail_kb(slug, state), msg_id=msg_id, critical=True)

        elif data.startswith("watch:") or data.startswith("favwatch:"):
            target = data.split(":", 1)[1]
            if data.startswith("favwatch:") and target not in state.get("favorites", []):
                state.setdefault("favorites", []).append(target)
                save_state(state)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio, banner=f"{target} kanalına bağlanılıyor..."), get_main_panel_kb(state), msg_id=msg_id, critical=True)
            open_stream(page, target, state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:search_channel":
            bot_ctx["awaiting_channel_reply"] = True
            res = tg_api("sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "🔎 <b>Hızlı Kanal Arama</b>\nİzlemek istediğin Kick yayıncısının <b>kanal adını yaz</b> ve gönder (örn: <code>rraenee</code>):",
                "parse_mode": "HTML",
                "reply_markup": json.dumps({"force_reply": True, "input_field_placeholder": "Kanal adını yaz..."})
            })
            if res and res.get("ok"):
                register_temp_message(res["result"]["message_id"], state)

        elif data == "tab:settings":
            render_panel(state, "⚙️ <b>BOT AYARLARI VE BİLDİRİM TERCİHLERİ</b>\n<i>🚨 Kritik Uyarılar (Oturum/Takılma): Her Zaman Açık</i>", get_settings_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "tab:blacklist":
            bl = state.get("blacklist", [])
            txt = "🚫 <b>KARA LİSTE</b>\nEngelini kaldırmak istediğin kanala tıkla:" if bl else "🚫 <b>KARA LİSTE BOŞ</b>"
            render_panel(state, txt, get_manage_list_kb(bl, "unblock"), msg_id=msg_id, critical=True)

        elif data == "tab:favorites":
            favs = state.get("favorites", [])
            txt = "⭐ <b>FAVORİ KANALLAR</b>\nFavoriden çıkarmak istediğin kanala tıkla:" if favs else "⭐ <b>FAVORİ LİSTESİ BOŞ</b>"
            render_panel(state, txt, get_manage_list_kb(favs, "unfav"), msg_id=msg_id, critical=True)

        elif data.startswith("page:"):
            state["list_page"] = int(data.split(":")[1])
            save_state(state)
            lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
            render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\n<i>Detay görmek ve izlemek için kanala dokun:</i>", get_channels_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:toggle_lang":
            state["list_lang"] = "global" if state.get("list_lang", "tr") == "tr" else "tr"
            state["list_page"] = 0
            fetch_gta6_streamers_strict(context, state)
            lang_label = "🇹🇷 Türkçe" if state["list_lang"] == "tr" else "🌍 Global"
            render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\n<i>Liste güncellendi:</i>", get_channels_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:refresh_list":
            state["list_page"] = 0
            fetch_gta6_streamers_strict(context, state)
            lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
            render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\n<i>Az önce yenilendi ({now_tr().strftime('%H:%M:%S')}):</i>", get_channels_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:next":
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio, banner="Yeni GTA VI yayınına bağlanılıyor..."), get_main_panel_kb(state), msg_id=msg_id, critical=True)
            streamers = state.get("cached_streamers") or fetch_gta6_streamers_strict(context, state)
            bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
            open_stream(page, streamers[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:prev":
            prev = state.get("previous_streamer")
            if prev:
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio, banner=f"{prev} kanalına dönülüyor..."), get_main_panel_kb(state), msg_id=msg_id, critical=True)
                open_stream(page, prev, state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:drops":
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio, banner="SunsetK Badge ilerlemesi taranıyor..."), get_main_panel_kb(state), msg_id=msg_id, critical=True)
            scan_and_claim_drops(context, state)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:screen":
            send_cropped_screenshot(page, f"📸 <b>Canlı Yayın:</b> <code>{state['current_streamer']}</code> (👁️ {format_viewers(state['current_viewers'])})", critical=True, state=state)

        elif data == "act:toggle_fav":
            cur = state.get("current_streamer", "")
            if cur and cur != "Yok":
                favs = state.setdefault("favorites", [])
                if cur in favs:
                    favs.remove(cur)
                else:
                    favs.append(cur)
                save_state(state)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:block_cur":
            cur = state.get("current_streamer", "")
            if cur and cur != "Yok" and cur not in state["blacklist"]:
                state["blacklist"].append(cur)
                save_state(state)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio, banner=f"{cur} engellendi, yeni kanala geçiliyor..."), get_main_panel_kb(state), msg_id=msg_id, critical=True)
            streamers = fetch_gta6_streamers_strict(context, state)
            open_stream(page, streamers[0]["slug"], state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], sess_ratio), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data.startswith("unblock:"):
            target = data.split(":", 1)[1]
            if target in state.get("blacklist", []):
                state["blacklist"].remove(target)
                save_state(state)
            render_panel(state, "🚫 <b>KARA LİSTE GÜNCELLENDİ</b>", get_manage_list_kb(state["blacklist"], "unblock"), msg_id=msg_id, critical=True)

        elif data.startswith("unfav:"):
            target = data.split(":", 1)[1]
            if target in state.get("favorites", []):
                state["favorites"].remove(target)
                save_state(state)
            render_panel(state, "⭐ <b>FAVORİLER GÜNCELLENDİ</b>", get_manage_list_kb(state["favorites"], "unfav"), msg_id=msg_id, critical=True)

        elif data == "act:toggle_notify_switch":
            state["notify_switch"] = not state.get("notify_switch", True)
            save_state(state)
            render_panel(state, "⚙️ <b>BOT AYARLARI VE BİLDİRİM TERCİHLERİ</b>\n<i>🚨 Kritik Uyarılar: Her Zaman Açık</i>", get_settings_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:toggle_notify_milestone":
            state["notify_milestone"] = not state.get("notify_milestone", True)
            save_state(state)
            render_panel(state, "⚙️ <b>BOT AYARLARI VE BİLDİRİM TERCİHLERİ</b>\n<i>🚨 Kritik Uyarılar: Her Zaman Açık</i>", get_settings_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:toggle_autoclean":
            state["auto_clean_temp"] = not state.get("auto_clean_temp", True)
            save_state(state)
            render_panel(state, "⚙️ <b>BOT AYARLARI VE BİLDİRİM TERCİHLERİ</b>\n<i>🚨 Kritik Uyarılar: Her Zaman Açık</i>", get_settings_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:log":
            logs_txt = "\n".join(list(LOG_BUFFER)[-25:]) or "Henüz log yok."
            send_temp_message(f"📜 <b>Son 25 Log Satırı:</b>\n<pre>{logs_txt[-3500:]}</pre>", critical=True, state=state)

        elif data.startswith("confirm:"):
            action = data.split(":")[1]
            q_txt = "⚠️ <b>Maratonu durdurmak istediğine emin misin?</b>" if action == "stop" else "⚠️ <b>Yeni bulut sunucusu başlatılsın mı?</b>"
            render_panel(state, q_txt, get_confirm_kb(action), msg_id=msg_id, critical=True)

        elif data == "exec:restart":
            render_panel(state, "♻️ <b>Yeni bulut sunucusu tetiklendi, bu oturum kapatılıyor...</b>", get_main_panel_kb(state), msg_id=msg_id, critical=True)
            trigger_next_workflow(state, reason="Telegram /restart komutu")
            bot_ctx["force_exit"] = True

        elif data == "exec:stop":
            state["paused"] = True
            save_state(state)
            render_panel(state, "🛑 <b>Maraton Durduruldu!</b>\nTekrar başlatmak için GitHub Actions üzerinden Run Workflow yapabilirsin.", get_main_panel_kb(state), msg_id=msg_id, critical=True)
            bot_ctx["force_exit"] = True

# ==========================================
# 12. ANA ÇALIŞMA DÖNGÜSÜ
# ==========================================
def run_bot(context, state):
    context.add_init_script(VISIBILITY_JS)
    setup_resource_blocker(context)
    setup_telegram_menu()

    if not parse_and_load_cookies(context, state):
        send_temp_message("❌ <b>KRİTİK HATA:</b> KICK_COOKIES_JSON okunamadı!", critical=True, state=state, auto_clean=False)
        return

    stop_event = threading.Event()
    tg_thread = threading.Thread(target=telegram_listener_thread, args=(state, stop_event), daemon=True)
    tg_thread.start()

    bot_ctx = {
        "state": state,
        "start_time": time.time(),
        "session_start_ratio": state.get("progress_ratio", 0.0),
        "stream_idx": 0,
        "force_exit": False,
        "awaiting_channel_reply": False
    }
    page = context.pages[0] if context.pages else context.new_page()

    try:
        streamers = fetch_gta6_streamers_strict(context, state)
        open_stream(page, streamers[0]["slug"], state, count_switch=False)

        if not check_session_authenticated(page):
            send_cropped_screenshot(page, "⚠️ <b>KRİTİK UYARI:</b> Kick oturumu düşmüş görünüyor! Lütfen <code>KICK_COOKIES_JSON</code> değerini yenile.", critical=True, state=state)

        scan_and_claim_drops(context, state)
        if bot_ctx["session_start_ratio"] == 0.0:
            bot_ctx["session_start_ratio"] = state.get("progress_ratio", 0.0)

        render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"]), get_main_panel_kb(state))

        last_health = time.time()
        last_drops = time.time()
        last_lock_check = time.time()
        last_heartbeat = time.time()
        next_hb_delay = random.uniform(27.0, 33.0)
        last_video_time = -1
        disconnect_times = []

        while (time.time() - bot_ctx["start_time"]) < MAX_RUN_SECONDS:
            if bot_ctx["force_exit"] or state.get("completed"):
                break

            process_queued_commands(page, context, bot_ctx)
            cleanup_expired_temp_messages(state)
            check_daily_report(state)

            # Hayalet İzleme: Her ~30 saniyede bir WebSocket Sinyali (Heartbeat)
            if (time.time() - last_heartbeat) >= next_hb_delay:
                last_heartbeat = time.time()
                next_hb_delay = random.uniform(27.0, 33.0)
                try:
                    hb = page.evaluate("() => window.__SEND_WS_HEARTBEAT__ ? window.__SEND_WS_HEARTBEAT__() : null")
                    if hb and hb.get("pingCount", 0) % 5 == 0:
                        log(f"⚡ Hayalet Sinyal #{hb['pingCount']} | WS: {hb['wsCount']} | Payload: {'🟢' if hb['hasPayload'] else '🟡'}")
                except Exception:
                    pass

            # Çift Sunucu Çakışma Kilidi (Her 30 sn)
            if (time.time() - last_lock_check) > 30:
                last_lock_check = time.time()
                if is_session_overtaken():
                    log("🔒 Yeni bir sunucu (active_run_id) kontrolü devraldı, bu oturum sessizce kapanıyor.")
                    bot_ctx["force_exit"] = True
                    break

            # Her 60 saniyede bir yayın sağlığı, kopma ve donma kontrolü
            if (time.time() - last_health) > 60:
                last_health = time.time()
                status, last_video_time = check_stream_health(page, state, last_video_time)

                if status in ("offline", "wrong_game"):
                    reason = "yayını kapattı" if status == "offline" else "GTA VI'dan başka oyuna geçti"
                    log(f"⚠️ {state['current_streamer']} {reason}, yeni GTA VI kanalına geçiliyor...")
                    state["health_status"] = "🟡 Yenileniyor"
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"], banner=f"Yeni kanala geçiliyor ({reason})..."), get_main_panel_kb(state))

                    old_ch = state["current_streamer"]
                    streamers = fetch_gta6_streamers_strict(context, state)
                    bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                    new_ch = streamers[bot_ctx["stream_idx"]]["slug"]
                    open_stream(page, new_ch, state, count_switch=True)
                    disconnect_times.clear()

                    if state.get("notify_switch", True):
                        send_temp_message(f"🔄 <b>Kanal Değişti:</b> <code>{old_ch}</code> ({reason}) ➡️ <code>{new_ch}</code>", critical=False, state=state)
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"]), get_main_panel_kb(state))
                    last_video_time = -1

                elif status in ("disconnected", "frozen"):
                    now_ts = time.time()
                    disconnect_times = [t for t in disconnect_times if (now_ts - t) <= 900]
                    disconnect_times.append(now_ts)

                    reason_tr = "Bağlantı kesildi" if status == "disconnected" else "Oynatıcı dondu"
                    log(f"❄️ {state['current_streamer']} - {reason_tr} (Son 15 dk: {len(disconnect_times)}. kez)")
                    state["health_status"] = "🟡 Yenileniyor"
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"], banner=f"{reason_tr}, kurtarılıyor..."), get_main_panel_kb(state))

                    # 15 dk içinde 2. kopmaysa veya Aşama 1+2 başarısız olursa başka GTA VI kanalına geç
                    recovered = False
                    if len(disconnect_times) < 2:
                        recovered = recover_stream_player(page, state)

                    if recovered:
                        state["health_status"] = "🟢 Canlı"
                        render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"]), get_main_panel_kb(state))
                        last_video_time = -1
                    else:
                        old_ch = state["current_streamer"]
                        log(f"🚨 Aşama 3: {old_ch} kurtarılamadı / sık kopuyor, yeni GTA VI yayınına geçiliyor...")
                        streamers = fetch_gta6_streamers_strict(context, state)
                        bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                        new_ch = streamers[bot_ctx["stream_idx"]]["slug"]
                        open_stream(page, new_ch, state, count_switch=True)
                        disconnect_times.clear()

                        if state.get("notify_switch", True):
                            send_temp_message(f"🔄 <b>Otomatik Kanal Değişimi:</b> <code>{old_ch}</code> ({reason_tr}) ➡️ <code>{new_ch}</code>", critical=False, state=state)
                        render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"]), get_main_panel_kb(state))
                        last_video_time = -1

            # Her 15 dakikada bir Drops taraması + Sabit Pano Güncelleme
            if (time.time() - last_drops) > 900:
                last_drops = time.time()
                scan_and_claim_drops(context, state)
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"]), get_main_panel_kb(state))

                if state.get("stuck_scans", 0) >= 2:
                    log("🚨 Süre üst üste 2 taramada ilerlemedi! Yayıncı değiştiriliyor...")
                    send_temp_message(f"🚨 <b>Süre İlerlemiyor!</b> <code>{state['current_streamer']}</code> kanalından çıkılıp yeni GTA VI yayınına geçiliyor...", critical=True, state=state)
                    state["stuck_scans"] = 0
                    save_state(state)
                    streamers = fetch_gta6_streamers_strict(context, state)
                    bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                    open_stream(page, streamers[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"], bot_ctx["session_start_ratio"]), get_main_panel_kb(state))

            time.sleep(1)
    finally:
        stop_event.set()
        save_state(state)

    if not bot_ctx["force_exit"] and not state.get("completed") and not state.get("paused"):
        trigger_next_workflow(state, reason="5.4 saatlik oturum tamamlandı")

def main():
    state = load_state()
    if state.get("completed"):
        log("🏆 250 saatlik ödül zaten alınmış, bot boşa çalışmadan kapanıyor.")
        return

    state["paused"] = False
    state["active_run_id"] = CURRENT_RUN_ID
    state["health_status"] = "🟢 Canlı"
    state["session_count"] = state.get("session_count", 0) + 1
    save_state(state)

    log(f"🚀 GTA VI Bulut Maraton Botu başlatılıyor (Oturum #{state['session_count']} | Run: {CURRENT_RUN_ID})...")
    crashed = False
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir="/tmp/kick_chrome_profile",
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 720},
                locale="tr-TR",
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            run_bot(context, state)
            context.close()
    except Exception as e:
        crashed = True
        err_short = sanitize_text(str(e))[:300]
        log(f"💥 BEKLENMEDİK ÇÖKME: {err_short}\n{sanitize_text(traceback.format_exc())}")
        send_temp_message(f"💥 <b>Bot Çöktü!</b> Otomatik kurtarma (Katman 2) devreye giriyor...\n<code>{err_short}</code>", critical=True, state=state, auto_clean=False)
    finally:
        state = load_state()
        if crashed and not state.get("completed") and not state.get("paused"):
            trigger_next_workflow(state, reason="Katman 2 otomatik çökme kurtarması")

if __name__ == "__main__":
    main()
