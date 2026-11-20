import os
import subprocess
import re
import sys
import json
import time
import queue
import random
import threading
import warnings
import traceback
import requests
import socket
import urllib3.util.connection
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
from collections import deque
from datetime import datetime, timedelta, timezone

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
_raw_proxy = os.environ.get("TELEGRAM_PROXY_URL", "").strip().strip('"').strip("'").rstrip("/")
if _raw_proxy and not _raw_proxy.startswith(("http://", "https://")):
    _raw_proxy = f"https://{_raw_proxy}"
TELEGRAM_API_BASE = _raw_proxy or "https://api.telegram.org"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()

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

def now_tr():
    return datetime.now(TR_TZ)

def is_dnd_hours():
    """Gece 01:00 - 09:00 TR arası sessiz bildirim (DND)."""
    h = now_tr().hour
    return 1 <= h < 9

def log(msg):
    ts = now_tr().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
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
})();
"""

# ==========================================
# 3. KALICI HAFIZA (STATE) YÖNETİMİ
# ==========================================
def load_state():
    today_str = now_tr().strftime("%Y-%m-%d")
    default_state = {
        "progress_ratio": 0.0,
        "percent": 0.0,
        "remaining_text": "Henüz taranmadı",
        "completed": False,
        "paused": False,
        "silent_mode": False,
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

# ==========================================
# 4. TELEGRAM API & UYGULAMA BENZERİ UI/UX
# ==========================================
def tg_api(method, data=None, files=None, timeout=12, retries=2):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"

    # 1. Önce Python requests ile dene
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
            log(f"[TG API RED - {method}] HTTP {r.status_code}: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            if attempt == retries:
                log(f"[TG API TIMEOUT - {method}] requests başarısız, curl -4 deneniyor... ({e})")
            else:
                time.sleep(1.5)

    # 2. Yedek: Sistem curl -4 (HTTP/2 + libcurl) ile gönder
    try:
        cmd = ["curl", "-4", "-sS", "--max-time", "15", "-X", "POST", url]
        if files:
            for k, v in (data or {}).items():
                cmd += ["-F", f"{k}={v}"]
            for k, fobj in files.items():
                cmd += ["-F", f"{k}=@{fobj.name}"]
        else:
            for k, v in (data or {}).items():
                cmd += ["--data-urlencode", f"{k}={v}"]

        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=20).decode("utf-8", errors="ignore")
        res = json.loads(out)
        if res.get("ok"):
            return res
        log(f"[TG CURL RED - {method}] {out[:200]}")
    except Exception as e:
        log(f"[TG CURL HATA - {method}] {e}")
    return None

def setup_telegram_menu():
    commands = [
        {"command": "durum", "description": "🏠 Canlı kontrol panelini aç"},
        {"command": "yayincilar", "description": "📺 Canlı GTA VI yayıncılarını listele"},
        {"command": "ekran", "description": "📸 Kırpılmış canlı yayın ekranı"},
        {"command": "drops", "description": "🎁 SunsetK Badge ilerlemesini tara"},
        {"command": "ayarlar", "description": "⚙️ Kara liste, favoriler ve sistem"},
        {"command": "log", "description": "📜 Son 25 çalışma kaydı"}
    ]
    if TELEGRAM_BOT_TOKEN:
        try:
            requests.post(
                f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
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
    }, timeout=5)

def delete_message(msg_id):
    if msg_id and TELEGRAM_CHAT_ID:
        tg_api("deleteMessage", {"chat_id": TELEGRAM_CHAT_ID, "message_id": msg_id}, timeout=8)

def pin_panel_message(msg_id):
    if msg_id and TELEGRAM_CHAT_ID:
        tg_api("pinChatMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": msg_id,
            "disable_notification": "true"
        }, timeout=8)

def build_progress_bar(pct, length=10):
    filled = max(0, min(length, int(round((pct / 100.0) * length))))
    return "█" * filled + "░" * (length - filled)

def get_main_panel_text(state, start_time):
    elapsed_min = int((time.time() - start_time) // 60)
    pct = state.get("percent", 0.0)
    bar = build_progress_bar(pct)

    rem_min = max(0, int(round(TOTAL_TARGET_MINUTES * (1.0 - (pct / 100.0)))))
    eta_dt = now_tr() + timedelta(minutes=rem_min)
    eta_str = eta_dt.strftime("%d.%m.%Y %H:%M") if pct > 0 else "Hesaplanıyor..."

    cur = state.get("current_streamer", "Yok")
    is_fav = "⭐ " if cur in state.get("favorites", []) else ""
    title = (state.get("current_title") or "GTA VI")[:55]
    viewers = format_viewers(state.get("current_viewers", 0))
    eff = state.get("efficiency_text", "Hesaplanıyor...")
    dnd = "🌙 DND" if is_dnd_hours() else "☀️ Aktif"
    updated_at = now_tr().strftime("%H:%M:%S")

    return (
        f"🎮 <b>GTA VI 250 SAAT CANLI PANO</b>  <i>({updated_at})</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 <b>Kanal:</b> {is_fav}<code>{cur}</code>  (👁️ {viewers})\n"
        f"📝 <b>Başlık:</b> <i>{title}</i>\n"
        f"🎁 <b>SunsetK:</b> <code>{state['remaining_text']}</code>\n"
        f"📊 <b>İlerleme:</b> <code>[{bar}] %{pct:.2f}</code>\n"
        f"📈 <b>Verimlilik:</b> <code>{eff}</code>\n"
        f"📅 <b>Tahmini Bitiş:</b> <code>{eta_str}</code>\n"
        f"🔁 <b>Oturum:</b> <code>#{state['session_count']}</code> | ⏱️ <code>{elapsed_min} dk</code> | {dnd}"
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
        row.append({"text": f"{icon} {slug} ({format_viewers(v)})", "callback_data": f"watch:{slug}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Sayfalama satırı
    nav_row = []
    if page_idx > 0:
        nav_row.append({"text": "⬅️ Önceki", "callback_data": f"page:{page_idx - 1}"})
    nav_row.append({"text": f"📄 {page_idx + 1}/{total_pages}", "callback_data": "noop"})
    if page_idx < total_pages - 1:
        nav_row.append({"text": "Sonraki ➡️", "callback_data": f"page:{page_idx + 1}"})
    rows.append(nav_row)

    # Dil geçişi ve yenileme
    lang_btn = "🌍 Global Listeye Geç" if state.get("list_lang", "tr") == "tr" else "🇹🇷 Türkçe Listeye Geç"
    rows.append([
        {"text": lang_btn, "callback_data": "act:toggle_lang"},
        {"text": "🔄 Listeyi Yenile", "callback_data": "act:refresh_list"}
    ])
    return {"inline_keyboard": rows}

def get_settings_panel_kb(state):
    silent_txt = "🔔 Sessiz Mod: AÇIK" if state.get("silent_mode") else "🔕 Sessiz Mod: KAPALI"
    rows = [
        [
            {"text": "🏠 Ana Panel", "callback_data": "tab:home"},
            {"text": "📺 Kanallar", "callback_data": "tab:channels"},
            {"text": "⚙️ Ayarlar (Aktif)", "callback_data": "tab:settings"}
        ],
        [
            {"text": silent_txt, "callback_data": "act:toggle_silent"},
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
    return {"inline_keyboard": rows}

def get_manage_list_kb(items, prefix, title_icon):
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
    silent = "true" if (not critical and (state.get("silent_mode") or is_dnd_hours())) else "false"

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

def send_temp_message(text, critical=False, state=None):
    """Altında [🗑️ Bu Mesajı Sil] butonu olan geçici bilgi mesajı."""
    if not TELEGRAM_CHAT_ID:
        return
    silent = "true" if (not critical and ((state and state.get("silent_mode")) or is_dnd_hours())) else "false"
    tg_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "disable_notification": silent,
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🗑️ Bu Mesajı Sil", "callback_data": "act:del_msg"}]]})
    })

def send_cropped_screenshot(page, caption, critical=False, state=None):
    """Mobil uyumlu kırpılmış video oynatıcı ekran görüntüsü + [🗑️ Bu Mesajı Sil] butonu."""
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

        silent = "true" if (not critical and ((state and state.get("silent_mode")) or is_dnd_hours())) else "false"
        with open(path, "rb") as photo:
            tg_api("sendPhoto", data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:1000],
                "parse_mode": "HTML",
                "disable_notification": silent,
                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🗑️ Bu Mesajı Sil", "callback_data": "act:del_msg"}]]})
            }, files={"photo": photo}, timeout=25)
    except Exception as e:
        log(f"⚠️ Ekran görüntüsü hatası: {e}")

# ==========================================
# 5. ARKA PLAN TELEGRAM DİNLEYİCİSİ (SIFIR GECİKME)
# ==========================================
def telegram_listener_thread(state_ref, stop_event):
    """Tarayıcı meşgulken bile Telegram butonlarına anında (<0.5sn) toast tepkisi verir ve kuyruğa atar."""
    offset = state_ref.get("last_update_offset", 0)
    while not stop_event.is_set():
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(5)
            continue
        try:
            r = requests.get(
                f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset + 1, "timeout": 10},
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

                    # Anında görsel geri bildirim (Toast)
                    toast_map = {
                        "tab:home": "🏠 Ana Panel",
                        "tab:channels": "📺 Kanallar yükleniyor...",
                        "tab:settings": "⚙️ Ayarlar",
                        "act:drops": "🎁 Drops taranıyor...",
                        "act:screen": "📸 Ekran görüntüsü alınıyor...",
                        "act:next": "🔄 Sonraki GTA VI kanalına geçiliyor...",
                        "act:prev": "↩️ Önceki kanala dönülüyor...",
                        "act:refresh_list": "🔄 Canlı GTA VI listesi yenileniyor...",
                        "act:toggle_lang": "🌍 Dil filtresi değiştiriliyor...",
                        "act:log": "📜 Loglar getiriliyor..."
                    }
                    answer_toast(cb_id, toast_map.get(data, "⏳ İşleniyor..."))
                    CMD_QUEUE.put(("cb", data, msg_id))

                elif "message" in upd and "text" in upd["message"]:
                    txt = upd["message"]["text"].strip()
                    CMD_QUEUE.put(("msg", txt, None))
        except Exception:
            time.sleep(2)

# ==========================================
# 6. CLOUDFLARE, KAYNAK ENGELLEYİCİ & ÇEREZ
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
        if (
            "challenges.cloudflare.com" in url or "live-video.net" in url
            or ".m3u8" in url or ".ts" in url or "kick.com/api/" in url
            or "websockets.kick.com" in url
        ):
            route.continue_()
            return
        if any(d in url for d in blocked_domains) or res_type in ("font", "image"):
            route.abort()
            return
        route.continue_()
    context.route("**/*", route_handler)

def parse_and_load_cookies(context):
    if not KICK_COOKIES_RAW:
        log("❌ KICK_COOKIES_JSON secret bulunamadı!")
        return False
    cookies_to_add = []
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
# 7. SADECE GERÇEK GTA VI YAYINCILARINI ÇEKME (DÜZELTİLDİ)
# ==========================================
def fetch_gta6_streamers_strict(context, state):
    """
    Sadece <main> içindeki kategori kartlarını tarar ve her kanalı Kick API'den
    doğrulayarak SADECE 'grand-theft-auto-vi' oynayan canlı yayıncıları döner.
    """
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

        # SADECE <main> alanından çek (sol menüdeki başka oyun oynayanları asla alma!)
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

        # Favorileri de başa ekleyip kontrol et
        for f in fav_set:
            if f not in bl_set and f not in candidates:
                candidates.insert(0, f)

        verified = []
        for slug in candidates[:18]:
            info = scan_page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch('https://kick.com/api/v2/channels/{slug}');
                    if (!r.ok) return null;
                    const d = await r.json();
                    if (!d || !d.livestream || !d.livestream.is_live) return null;
                    const cats = d.livestream.categories || [];
                    const isGta6 = cats.some(c =>
                        (c.slug || '').toLowerCase().includes('grand-theft-auto-vi') ||
                        (c.name || '').toLowerCase().includes('grand theft auto vi')
                    );
                    if (!isGta6) return null;
                    return {{
                        slug: '{slug}',
                        viewers: d.livestream.viewer_count || 0,
                        title: d.livestream.session_title || 'GTA VI'
                    }};
                }} catch(e) {{ return null; }}
            }}""")
            if info:
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
        verified = [{"slug": s, "viewers": 0, "title": "GTA VI"} for s in FALLBACK_SLUGS if s not in bl_set]
    else:
        # Favoriler en üstte, sonra izleyici sayısına göre büyükten küçüğe sırala
        verified.sort(key=lambda x: (x["slug"] in fav_set, x["viewers"]), reverse=True)

    state["cached_streamers"] = verified
    save_state(state)
    log(f"✅ Doğrulanmış GTA VI yayıncıları ({len(verified)}): {', '.join(x['slug'] for x in verified[:5])}")
    return verified

# ==========================================
# 8. DROPS TARAYICI + ANLIK VERİMLİLİK ÖLÇER
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

            # En az 3 dakika (180 sn) geçmişse verimliliği hesapla
            if last_ts > 0 and (now_ts - last_ts) >= 180:
                real_min = (now_ts - last_ts) / 60.0
                gained_min = max(0.0, (ratio - last_ratio) * TOTAL_TARGET_MINUTES)
                eff_pct = min(100, int(round((gained_min / real_min) * 100)))
                icon = "🟢" if eff_pct >= 85 else ("🟡" if eff_pct >= 50 else "🔴")
                state["efficiency_text"] = f"{icon} %{eff_pct} ({int(round(gained_min))}/{int(round(real_min))} dk)"

            if old_ratio > 0 and ratio <= old_ratio:
                state["stuck_scans"] = state.get("stuck_scans", 0) + 1
            else:
                state["stuck_scans"] = 0

            pct = round(ratio * 100.0, 2)
            watched_min = int(round(ratio * TOTAL_TARGET_MINUTES))
            rem_min = max(0, TOTAL_TARGET_MINUTES - watched_min)
            state["progress_ratio"] = ratio
            state["percent"] = pct
            state["last_scan_ts"] = now_ts
            state["last_scan_ratio"] = ratio
            state["remaining_text"] = f"{rem_min // 60} sa. {rem_min % 60} dk. kaldı (İzlenen: {watched_min // 60} sa. {watched_min % 60} dk.)"
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
        // Sohbet panelini kapat/boşalt (RAM koruması)
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

    meta = page.evaluate(f"""async () => {{
        try {{
            const r = await fetch('https://kick.com/api/v2/channels/{slug}');
            if (!r.ok) return null;
            const d = await r.json();
            return d && d.livestream ? {{ viewers: d.livestream.viewer_count || 0, title: d.livestream.session_title || 'GTA VI' }} : null;
        }} catch(e) {{ return null; }}
    }}""")

    state["current_streamer"] = slug
    if meta:
        state["current_viewers"] = meta["viewers"]
        state["current_title"] = meta["title"]
    save_state(state)

def check_stream_health(page, state, last_video_time):
    slug = state["current_streamer"]
    try:
        res = page.evaluate(f"""async () => {{
            const v = document.querySelector('video');
            let curTime = -1;
            if (v) {{
                if (v.paused) v.play().catch(() => {{}});
                curTime = v.currentTime;
            }}
            try {{
                const r = await fetch('https://kick.com/api/v2/channels/{slug}');
                if (!r.ok) return {{ ok: true, curTime: curTime, isGta6: true }};
                const d = await r.json();
                const isLive = Boolean(d && d.livestream && d.livestream.is_live);
                const cats = (d && d.livestream && d.livestream.categories) ? d.livestream.categories : [];
                const isGta6 = cats.some(c => (c.slug || '').includes('grand-theft-auto-vi') || (c.name || '').toLowerCase().includes('grand theft auto vi'));
                return {{ ok: isLive, curTime: curTime, isGta6: isGta6, viewers: d?.livestream?.viewer_count || 0, title: d?.livestream?.session_title || '' }};
            }} catch(e) {{ return {{ ok: true, curTime: curTime, isGta6: true }}; }}
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
        if last_video_time > 0 and cur_time == last_video_time:
            return "frozen", cur_time
        return "healthy", cur_time
    except Exception:
        return "healthy", last_video_time

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
        send_temp_message(msg, critical=True, state=state)
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
    while not CMD_QUEUE.empty():
        kind, payload, msg_id = CMD_QUEUE.get_nowait()

        if kind == "msg":
            cmd = payload.lower()
            if cmd.startswith("/durum") or cmd.startswith("/start"):
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), create_new=True, critical=True)
            elif cmd.startswith("/yayincilar"):
                if not state.get("cached_streamers"):
                    fetch_gta6_streamers_strict(context, state)
                lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
                render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\nGeçmek istediğin kanala dokun:", get_channels_panel_kb(state), create_new=True, critical=True)
            elif cmd.startswith("/ayarlar"):
                render_panel(state, "⚙️ <b>BOT AYARLARI VE YÖNETİM</b>\nAşağıdan istediğin ayarı seç:", get_settings_panel_kb(state), create_new=True, critical=True)
            elif cmd.startswith("/ekran"):
                send_cropped_screenshot(page, f"📸 <b>Canlı Yayın:</b> <code>{state['current_streamer']}</code>", critical=True, state=state)
            elif cmd.startswith("/drops"):
                scan_and_claim_drops(context, state)
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), critical=True)
            elif cmd.startswith("/log"):
                logs_txt = "\n".join(list(LOG_BUFFER)[-25:]) or "Henüz log yok."
                send_temp_message(f"📜 <b>Son 25 Log Satırı:</b>\n<pre>{logs_txt[-3500:]}</pre>", critical=True, state=state)
            elif cmd.startswith("/izle "):
                target = payload.split(" ", 1)[1].strip().lower()
                if target:
                    open_stream(page, target, state, count_switch=True)
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), critical=True)
            continue

        # Callback (Buton) İşlemleri -> Tek Mesajda Canlı Güncelleme
        data = payload
        if data == "tab:home":
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "tab:channels":
            if not state.get("cached_streamers"):
                fetch_gta6_streamers_strict(context, state)
            lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
            render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\n<i>Sadece doğrulanmış GTA VI yayınları listelenir:</i>", get_channels_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "tab:settings":
            render_panel(state, "⚙️ <b>BOT AYARLARI VE YÖNETİM</b>\n<i>Tüm ayarlar kalıcı hafızaya kaydedilir:</i>", get_settings_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "tab:blacklist":
            bl = state.get("blacklist", [])
            txt = "🚫 <b>KARA LİSTE</b>\nEngelini kaldırmak istediğin kanala tıkla:" if bl else "🚫 <b>KARA LİSTE BOŞ</b>"
            render_panel(state, txt, get_manage_list_kb(bl, "unblock", "🚫"), msg_id=msg_id, critical=True)

        elif data == "tab:favorites":
            favs = state.get("favorites", [])
            txt = "⭐ <b>FAVORİ KANALLAR</b>\nFavoriden çıkarmak istediğin kanala tıkla:" if favs else "⭐ <b>FAVORİ LİSTESİ BOŞ</b>"
            render_panel(state, txt, get_manage_list_kb(favs, "unfav", "⭐"), msg_id=msg_id, critical=True)

        elif data.startswith("page:"):
            state["list_page"] = int(data.split(":")[1])
            save_state(state)
            lang_label = "🇹🇷 Türkçe" if state.get("list_lang", "tr") == "tr" else "🌍 Global"
            render_panel(state, f"📺 <b>CANLI GTA VI YAYINCILARI ({lang_label})</b>\n<i>Sadece doğrulanmış GTA VI yayınları listelenir:</i>", get_channels_panel_kb(state), msg_id=msg_id, critical=True)

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

        elif data.startswith("watch:"):
            target = data.split(":", 1)[1]
            open_stream(page, target, state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:next":
            streamers = state.get("cached_streamers") or fetch_gta6_streamers_strict(context, state)
            bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
            open_stream(page, streamers[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:prev":
            prev = state.get("previous_streamer")
            if prev:
                open_stream(page, prev, state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:drops":
            scan_and_claim_drops(context, state)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

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
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data == "act:block_cur":
            cur = state.get("current_streamer", "")
            if cur and cur != "Yok" and cur not in state["blacklist"]:
                state["blacklist"].append(cur)
                save_state(state)
            streamers = fetch_gta6_streamers_strict(context, state)
            open_stream(page, streamers[0]["slug"], state, count_switch=True)
            render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state), msg_id=msg_id, critical=True)

        elif data.startswith("unblock:"):
            target = data.split(":", 1)[1]
            if target in state.get("blacklist", []):
                state["blacklist"].remove(target)
                save_state(state)
            render_panel(state, "🚫 <b>KARA LİSTE GÜNCELLENDİ</b>", get_manage_list_kb(state["blacklist"], "unblock", "🚫"), msg_id=msg_id, critical=True)

        elif data.startswith("unfav:"):
            target = data.split(":", 1)[1]
            if target in state.get("favorites", []):
                state["favorites"].remove(target)
                save_state(state)
            render_panel(state, "⭐ <b>FAVORİLER GÜNCELLENDİ</b>", get_manage_list_kb(state["favorites"], "unfav", "⭐"), msg_id=msg_id, critical=True)

        elif data == "act:toggle_silent":
            state["silent_mode"] = not state.get("silent_mode", False)
            save_state(state)
            render_panel(state, "⚙️ <b>BOT AYARLARI VE YÖNETİM</b>", get_settings_panel_kb(state), msg_id=msg_id, critical=True)

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

    if not parse_and_load_cookies(context):
        send_temp_message("❌ <b>KRİTİK HATA:</b> KICK_COOKIES_JSON okunamadı!", critical=True, state=state)
        return

    stop_event = threading.Event()
    tg_thread = threading.Thread(target=telegram_listener_thread, args=(state, stop_event), daemon=True)
    tg_thread.start()

    bot_ctx = {"state": state, "start_time": time.time(), "stream_idx": 0, "force_exit": False}
    page = context.pages[0] if context.pages else context.new_page()

    try:
        streamers = fetch_gta6_streamers_strict(context, state)
        open_stream(page, streamers[0]["slug"], state, count_switch=False)

        if not check_session_authenticated(page):
            send_cropped_screenshot(page, "⚠️ <b>KRİTİK UYARI:</b> Kick oturumu düşmüş görünüyor! Lütfen <code>KICK_COOKIES_JSON</code> değerini yenile.", critical=True, state=state)

        scan_and_claim_drops(context, state)
        render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state))

        last_health = time.time()
        last_drops = time.time()
        last_video_time = -1

        while (time.time() - bot_ctx["start_time"]) < MAX_RUN_SECONDS:
            if bot_ctx["force_exit"] or state.get("completed"):
                break

            process_queued_commands(page, context, bot_ctx)
            check_daily_report(state)

            # Her 3 dakikada bir yayın sağlığı, kategori ve donma kontrolü
            if (time.time() - last_health) > 180:
                last_health = time.time()
                status, last_video_time = check_stream_health(page, state, last_video_time)

                if status in ("offline", "wrong_game"):
                    reason = "yayını kapattı" if status == "offline" else "GTA VI'dan başka oyuna geçti"
                    log(f"⚠️ {state['current_streamer']} {reason}, yeni GTA VI kanalına geçiliyor...")
                    streamers = fetch_gta6_streamers_strict(context, state)
                    bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                    open_stream(page, streamers[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)
                    render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state))
                    last_video_time = -1
                elif status == "frozen":
                    log(f"❄️ {state['current_streamer']} dondu, sayfa yenileniyor...")
                    page.reload(wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(4000)
                    last_video_time = -1

            # Her 15 dakikada bir Drops taraması + Sabit Pano Güncelleme
            if (time.time() - last_drops) > 900:
                last_drops = time.time()
                scan_and_claim_drops(context, state)
                render_panel(state, get_main_panel_text(state, bot_ctx["start_time"]), get_main_panel_kb(state))

                if state.get("stuck_scans", 0) >= 2:
                    log("🚨 Süre üst üste 2 taramada ilerlemedi! Yayıncı değiştiriliyor...")
                    send_temp_message(f"🚨 <b>Süre İlerlemiyor!</b> <code>{state['current_streamer']}</code> kanalından çıkılıp yeni GTA VI yayınına geçiliyor...", critical=True, state=state)
                    state["stuck_scans"] = 0
                    save_state(state)
                    streamers = fetch_gta6_streamers_strict(context, state)
                    bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                    open_stream(page, streamers[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)

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
    state["session_count"] = state.get("session_count", 0) + 1
    save_state(state)

    log(f"🚀 GTA VI Bulut Maraton Botu başlatılıyor (Oturum #{state['session_count']})...")
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
        err_short = str(e)[:300]
        log(f"💥 BEKLENMEDİK ÇÖKME: {err_short}\n{traceback.format_exc()}")
        send_temp_message(f"💥 <b>Bot Çöktü!</b> Otomatik kurtarma (Katman 2) devreye giriyor...\n<code>{err_short}</code>", critical=True, state=state)
    finally:
        state = load_state()
        if crashed and not state.get("completed") and not state.get("paused"):
            trigger_next_workflow(state, reason="Katman 2 otomatik çökme kurtarması")

if __name__ == "__main__":
    main()
