import os
import re
import sys
import json
import time
import random
import warnings
import traceback
import requests
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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()

WORKFLOW_FILE = "gta6_drops.yml"
STATE_FILE = "marathon_state.json"
MAX_RUN_SECONDS = int(5.4 * 3600)  # 5 saat 24 dakika sonra normal bayrak devri
TOTAL_TARGET_MINUTES = 250 * 60    # 15.000 dakika (250 saat)

TR_TZ = timezone(timedelta(hours=3))
GTA6_TR_CATEGORY_URL = "https://kick.com/category/grand-theft-auto-vi?sort=viewers_high_to_low&languages=tr"
DROPS_CAMPAIGNS_URL = "https://kick.com/drops/campaigns"

FALLBACK_SLUGS = ["rraenee", "eray", "mithrain", "hype", "elraenn", "cordiseps", "wtcn"]

LOG_BUFFER = deque(maxlen=50)

def now_tr():
    return datetime.now(TR_TZ)

def log(msg):
    ts = now_tr().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_BUFFER.append(line)

def format_viewers(count):
    try:
        c = int(count)
        if c >= 1000:
            return f"{c / 1000:.1f}K"
        return str(c)
    except Exception:
        return "?"

# ==========================================
# 2. GÖRÜNÜRLÜK VE OYNATICI KORUMA SCRIPTI
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
        "current_title": "Bilinmiyor",
        "current_viewers": 0,
        "blacklist": [],
        "session_count": 0,
        "stuck_scans": 0,
        "last_update_offset": 0,
        "last_scan_ts": 0.0,
        "last_scan_ratio": 0.0,
        "efficiency_text": "Hesaplanıyor...",
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
# 4. TELEGRAM MENÜ, KLAVYE VE BİLDİRİMLER
# ==========================================
def setup_telegram_menu():
    """Telegram sohbetindeki sol alt Menü (☰) butonuna tek tıkla komutları tanımlar."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "durum", "description": "📊 Anlık durum, yayın detayı ve verimlilik"},
        {"command": "yayincilar", "description": "📺 Canlı Türkçe GTA VI yayıncılarını listele"},
        {"command": "ekran", "description": "📸 Canlı yayın ekran görüntüsü al"},
        {"command": "drops", "description": "🎁 SunsetK Badge ilerlemesini hemen tara"},
        {"command": "degistir", "description": "🔄 Sonraki GTA VI yayıncısına geç"},
        {"command": "log", "description": "📜 Botun son 25 çalışma kaydını gör"},
        {"command": "sessiz", "description": "🔕 Sessiz modu aç/kapat"},
        {"command": "restart", "description": "♻️ Yeni bulut sunucusu başlat"},
        {"command": "durdur", "description": "🛑 Maratonu durdur"}
    ]
    try:
        requests.post(url, json={"commands": commands}, timeout=10)
        log("✅ Telegram sol alt menü komutları (setMyCommands) güncellendi.")
    except Exception as e:
        log(f"⚠️ setMyCommands hatası: {e}")

def get_control_keyboard(silent_mode=False):
    silent_label = "🔔 Sessiz: AÇIK" if silent_mode else "🔕 Sessiz: KAPALI"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Anlık Durum", "callback_data": "/durum"},
                {"text": "📸 Ekran Görüntüsü", "callback_data": "/ekran"}
            ],
            [
                {"text": "📺 Yayıncıları Listele", "callback_data": "/yayincilar"},
                {"text": "🎁 Drops Tara", "callback_data": "/drops"}
            ],
            [
                {"text": "🔄 Yayıncı Değiştir", "callback_data": "/degistir"},
                {"text": "📜 Son Loglar", "callback_data": "/log"}
            ],
            [
                {"text": silent_label, "callback_data": "/sessiz"},
                {"text": "♻️ Yeniden Başlat", "callback_data": "/restart"},
                {"text": "🛑 Durdur", "callback_data": "/durdur"}
            ]
        ]
    }

def build_streamers_keyboard(streamers_info):
    """Canlı yayıncıları 2 sütunlu tıklanabilir butonlar halinde düzenler."""
    rows = []
    row = []
    for item in streamers_info[:10]:
        slug = item["slug"]
        v_str = format_viewers(item.get("viewers", 0))
        row.append({"text": f"🟢 {slug} ({v_str})", "callback_data": f"watch:{slug}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        {"text": "🔄 Listeyi Yenile", "callback_data": "/yayincilar"},
        {"text": "🔙 Ana Menü", "callback_data": "/durum"}
    ])
    return {"inline_keyboard": rows}

def tg_send(text, reply_markup=None, critical=False, state=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if state and state.get("silent_mode") and not critical:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        log(f"[TG HATA] {e}")

def send_screenshot(page, caption="", critical=False, state=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if state and state.get("silent_mode") and not critical:
        return
    path = "live_screen.png"
    try:
        page.screenshot(path=path, full_page=False)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        silent = state.get("silent_mode", False) if state else False
        with open(path, "rb") as photo:
            requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption[:1000],
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(get_control_keyboard(silent))
                },
                files={"photo": photo},
                timeout=25
            )
    except Exception as e:
        log(f"[SCREENSHOT HATA] {e}")

def build_progress_bar(pct, length=10):
    filled = max(0, min(length, int(round((pct / 100.0) * length))))
    return "█" * filled + "░" * (length - filled)

def format_status_message(state, start_time):
    elapsed_min = int((time.time() - start_time) // 60)
    pct = state.get("percent", 0.0)
    bar = build_progress_bar(pct)

    rem_min = max(0, int(round(TOTAL_TARGET_MINUTES * (1.0 - (pct / 100.0)))))
    eta_dt = now_tr() + timedelta(minutes=rem_min)
    eta_str = eta_dt.strftime("%d.%m.%Y %H:%M") if pct > 0 else "Hesaplanıyor..."

    title = (state.get("current_title") or "Bilinmiyor")[:60]
    viewers = format_viewers(state.get("current_viewers", 0))
    eff = state.get("efficiency_text", "Hesaplanıyor...")

    return (
        f"🎮 <b>GTA VI 250 SAAT BULUT MARATONU</b>\n\n"
        f"📺 <b>Kanal:</b> <code>{state['current_streamer']}</code> (👁️ {viewers})\n"
        f"📝 <b>Başlık:</b> <i>{title}</i>\n"
        f"🎁 <b>SunsetK Badge:</b> <code>{state['remaining_text']}</code>\n"
        f"📊 <b>İlerleme:</b> <code>[{bar}] %{pct:.2f}</code>\n"
        f"📈 <b>Verimlilik:</b> <code>{eff}</code>\n"
        f"📅 <b>Tahmini Bitiş (TR):</b> <code>{eta_str}</code>\n"
        f"🔁 <b>Oturum:</b> <code>#{state['session_count']}</code> | ⏱️ <b>Aktif:</b> <code>{elapsed_min} dk</code>"
    )

# ==========================================
# 5. CLOUDFLARE TURNSTILE & AGRESİF KAYNAK ENGELLEYİCİ
# ==========================================
def bypass_cloudflare(page):
    """Cloudflare doğrulama ekranı çıkarsa Turnstile kutusuna insan gibi tıklayıp geçer."""
    for attempt in range(4):
        try:
            title = (page.title() or "").lower()
            body = page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
            is_cf = (
                "just a moment" in title
                or "bir dakika" in title
                or "güvenlik doğrulaması" in body
                or "verify you are human" in body
                or "gerçek kişi olduğunuzu" in body
            )
            if not is_cf:
                return True

            log(f"🛡️ Cloudflare tespit edildi (Deneme {attempt + 1}/4)...")
            page.wait_for_timeout(3000)

            box = None
            for frame in page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    el = frame.frame_element()
                    box = el.bounding_box()
                    if box:
                        break

            if box:
                tx = box["x"] + 28 + random.uniform(-3, 3)
                ty = box["y"] + (box["height"] / 2) + random.uniform(-3, 3)
                page.mouse.move(tx - 60, ty + 20, steps=10)
                page.wait_for_timeout(random.randint(150, 350))
                page.mouse.move(tx, ty, steps=12)
                page.wait_for_timeout(random.randint(100, 250))
                page.mouse.click(tx, ty)
                log(f"🖱️ Turnstile kutusuna tıklandı ({int(tx)}, {int(ty)}).")
            else:
                page.mouse.move(215, 295, steps=12)
                page.mouse.click(215, 295)

            page.wait_for_timeout(5000)
        except Exception as e:
            log(f"⚠️ CF Bypass uyarısı: {e}")
            page.wait_for_timeout(2000)
    return False

def setup_resource_blocker(context):
    """Video akışı, Cloudflare ve Kick API dışındaki ağır font, reklam ve görselleri engeller."""
    blocked_domains = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.net", "datadoghq.com", "sentry.io", "hotjar.com", "intercom.io"
    )

    def route_handler(route):
        req = route.request
        url = req.url.lower()
        res_type = req.resource_type

        # Cloudflare, canlı video akışı (IVS/HLS) ve Kick API'ye asla dokunma
        if (
            "challenges.cloudflare.com" in url
            or "live-video.net" in url
            or ".m3u8" in url
            or ".ts" in url
            or "kick.com/api/" in url
            or "websockets.kick.com" in url
        ):
            route.continue_()
            return

        # Analitik / reklam izleyicilerini ve ağır font/görselleri kes
        if any(d in url for d in blocked_domains) or res_type in ("font", "image"):
            route.abort()
            return

        route.continue_()

    context.route("**/*", route_handler)
    log("🛡️ Agresif kaynak engelleyici (Image/Font/Tracker Blocker) aktif edildi.")

# ==========================================
# 6. ÇEREZ YÜKLEYİCİ & OTURUM KONTROLÜ
# ==========================================
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
                    "name": str(name),
                    "value": str(value),
                    "domain": domain if domain.startswith(".") else f".{domain}",
                    "path": c.get("path", "/"),
                    "secure": bool(c.get("secure", True)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "sameSite": "Lax"
                })
    except Exception:
        pass

    if not cookies_to_add and "=" in KICK_COOKIES_RAW:
        for part in KICK_COOKIES_RAW.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies_to_add.append({
                    "name": k.strip(),
                    "value": v.strip(),
                    "domain": ".kick.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax"
                })

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
# 7. CANLI TÜRKÇE GTA VI YAYINCILARINI ÇEKME
# ==========================================
def fetch_tr_gta6_streamers_detailed(page, blacklist):
    """En çok izlenen 10 Türkçe GTA VI yayıncısını izleyici sayılarıyla birlikte döner."""
    bl_set = set(s.lower() for s in blacklist)
    slugs = []

    try:
        log("🔍 Türkçe GTA VI yayıncıları taranıyor...")
        page.goto(GTA6_TR_CATEGORY_URL, wait_until="domcontentloaded", timeout=35000)
        bypass_cloudflare(page)
        page.wait_for_timeout(4000)

        dom_slugs = page.evaluate(r"""() => {
            const ignore = new Set([
                'categories', 'category', 'drops', 'browse', 'following',
                'search', 'dashboard', 'settings', 'terms', 'privacy', 'transactions'
            ]);
            const found = [];
            document.querySelectorAll('a[href^="/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/^\/([a-zA-Z0-9_]{3,25})$/);
                if (m) {
                    const s = m[1].toLowerCase();
                    if (!ignore.has(s) && !found.includes(s)) found.push(s);
                }
            });
            return found.slice(0, 15);
        }""")
        for s in dom_slugs:
            if s not in bl_set and s not in slugs:
                slugs.append(s)
    except Exception as e:
        log(f"⚠️ Kategori tarama uyarısı: {e}")

    if not slugs:
        slugs = [s for s in FALLBACK_SLUGS if s not in bl_set]

    detailed = []
    for slug in slugs[:10]:
        try:
            info = page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch('https://kick.com/api/v2/channels/{slug}');
                    if (!r.ok) return null;
                    const d = await r.json();
                    if (d && d.livestream && d.livestream.is_live) {{
                        return {{
                            slug: '{slug}',
                            viewers: d.livestream.viewer_count || 0,
                            title: d.livestream.session_title || 'GTA VI'
                        }};
                    }}
                    return null;
                }} catch(e) {{ return null; }}
            }}""")
            if info:
                detailed.append(info)
        except Exception:
            pass

    if not detailed:
        detailed = [{"slug": s, "viewers": 0, "title": "GTA VI"} for s in slugs[:5]]
    else:
        detailed.sort(key=lambda x: x["viewers"], reverse=True)

    log(f"📋 Canlı Türkçe GTA VI havuzu ({len(detailed)}): {', '.join(x['slug'] for x in detailed[:5])}")
    return detailed

# ==========================================
# 8. DROPS TARAYICI + ANLIK VERİMLİLİK ÖLÇER
# ==========================================
def scan_and_claim_drops(context, state):
    """https://kick.com/drops/campaigns üzerinden SunsetK Badge ilerlemesini okur ve verimliliği hesaplar."""
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
            send_screenshot(drop_page, "🎉🏆 <b>MÜJDE! 250 Saatlik GTA VI SunsetK Badge Ödülü Otomatik Talep Edildi!</b>", critical=True, state=state)
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

            # İlk kez taranıyorsa günlük başlangıç oranını kaydet
            if state.get("daily_start_ratio", 0.0) == 0.0:
                state["daily_start_ratio"] = ratio

            # Anlık Verimlilik Hesabı (En az 5 dakika geçmişse hesapla)
            if last_ts > 0 and (now_ts - last_ts) >= 300:
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
            rem_h, rem_m = rem_min // 60, rem_min % 60
            w_h, w_m = watched_min // 60, watched_min % 60

            state["progress_ratio"] = ratio
            state["percent"] = pct
            state["last_scan_ts"] = now_ts
            state["last_scan_ratio"] = ratio
            state["remaining_text"] = f"{rem_h} sa. {rem_m} dk. kaldı (İzlenen: {w_h} sa. {w_m} dk.)"
            save_state(state)
            summary = f"{state['remaining_text']} (%{pct:.2f})"
            log(f"🎁 Drops güncellendi: {summary} | Verimlilik: {state['efficiency_text']}")
        else:
            log("⚠️ SunsetK Badge progressbar DOM içinde bulunamadı.")

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
# 9. YAYIN AÇICI, 160P, SOHBET KAPATICI & SAĞLIK KONTROLÜ
# ==========================================
def open_stream(page, slug, state, count_switch=False):
    url = f"https://kick.com/{slug}"
    log(f"📺 Yayın açılıyor: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=35000)
    bypass_cloudflare(page)
    page.wait_for_timeout(4500)

    # 18+ uyarıları, sessiz oynatma ve SOHBET KAPATICI (Hafif DOM Modu)
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
        // Sağdaki sohbet panelini gizle / DOM yükünü azalt
        const chatSelectors = ['#channel-chatroom', '[data-testid="chatroom"]', 'aside'];
        chatSelectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => {
                if ((el.innerText || '').toLowerCase().includes('sohbet') || (el.innerText || '').toLowerCase().includes('chat') || sel.includes('chat')) {
                    el.style.display = 'none';
                    el.innerHTML = '';
                }
            });
        });
    }""")

    # 160p kaliteye düşür
    try:
        settings_btn = page.locator('button[aria-label*="Settings"], button[aria-label*="Ayarlar"]').first
        if settings_btn.is_visible(timeout=2000):
            settings_btn.click()
            page.wait_for_timeout(600)
            q_btn = page.locator('text=/160p/i').first
            if q_btn.is_visible(timeout=1500):
                q_btn.click()
                log("⚙️ Video kalitesi 160p yapıldı.")
    except Exception:
        pass

    # Canlı yayın başlığı ve izleyici sayısını çek
    meta = page.evaluate(f"""async () => {{
        try {{
            const r = await fetch('https://kick.com/api/v2/channels/{slug}');
            if (!r.ok) return null;
            const d = await r.json();
            if (d && d.livestream) {{
                return {{
                    viewers: d.livestream.viewer_count || 0,
                    title: d.livestream.session_title || 'GTA VI'
                }};
            }}
            return null;
        }} catch(e) {{ return null; }}
    }}""")

    if count_switch and state.get("current_streamer") not in ("Yok", slug):
        state["daily_channel_switches"] = state.get("daily_channel_switches", 0) + 1

    state["current_streamer"] = slug
    if meta:
        state["current_viewers"] = meta.get("viewers", 0)
        state["current_title"] = meta.get("title", "GTA VI")
    save_state(state)

def check_stream_health_and_category(page, state, last_video_time):
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
                return {{
                    ok: isLive,
                    curTime: curTime,
                    isGta6: isGta6,
                    viewers: d?.livestream?.viewer_count || 0,
                    title: d?.livestream?.session_title || ''
                }};
            }} catch(e) {{
                return {{ ok: true, curTime: curTime, isGta6: true }};
            }}
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
# 10. GÜNLÜK PERFORMANS ÖZETİ (00:00 TR RAPORU)
# ==========================================
def check_and_send_daily_report(state):
    today_str = now_tr().strftime("%Y-%m-%d")
    last_date = state.get("last_daily_report_date", today_str)
    if today_str != last_date:
        gained_ratio = max(0.0, state.get("progress_ratio", 0.0) - state.get("daily_start_ratio", 0.0))
        gained_min = int(round(gained_ratio * TOTAL_TARGET_MINUTES))
        g_h, g_m = gained_min // 60, gained_min % 60

        rem_min = max(0, int(round((1.0 - state.get("progress_ratio", 0.0)) * TOTAL_TARGET_MINUTES)))
        rem_days = round(rem_min / 1440.0, 1)

        msg = (
            f"🌙 <b>GÜNLÜK PERFORMANS ÖZETİ ({last_date})</b>\n\n"
            f"⏱️ <b>Son 24 Saatte Kasılan:</b> <code>{g_h} sa. {g_m} dk.</code>\n"
            f"🔄 <b>Kanal Değişimi:</b> <code>{state.get('daily_channel_switches', 0)} kez</code>\n"
            f"📊 <b>Toplam İlerleme:</b> <code>%{state.get('percent', 0.0):.2f}</code>\n"
            f"📅 <b>Hedefe Kalan Tahmini Gün:</b> <code>~{rem_days} gün</code>"
        )
        tg_send(msg, reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

        state["last_daily_report_date"] = today_str
        state["daily_start_ratio"] = state.get("progress_ratio", 0.0)
        state["daily_channel_switches"] = 0
        save_state(state)

# ==========================================
# 11. OTOMATİK BAYRAK DEVRİ (KATMAN 1 & 2)
# ==========================================
def trigger_next_workflow(state, reason="5.4 saatlik oturum tamamlandı"):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log("⚠️ GITHUB_TOKEN veya GITHUB_REPOSITORY eksik.")
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    try:
        r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
        if r.status_code in (200, 204):
            log(f"🔄 Yeni workflow tetiklendi ({reason}).")
            tg_send(f"🔄 <b>Bayrak Devri:</b> {reason}. Yeni bulut sunucusu başlatıldı.", state=state)
            return True
    except Exception as e:
        log(f"❌ Workflow tetikleme hatası: {e}")
    return False

# ==========================================
# 12. TELEGRAM KOMUT & BUTON DİNLEYİCİSİ
# ==========================================
def poll_telegram_commands(page, context, bot_ctx):
    if not TELEGRAM_BOT_TOKEN:
        return
    state = bot_ctx["state"]
    offset = state.get("last_update_offset", 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset + 1}&timeout=1"
    try:
        res = requests.get(url, timeout=5)
        if not res.ok:
            return
        for upd in res.json().get("result", []):
            state["last_update_offset"] = upd["update_id"]
            save_state(state)

            raw_cmd = ""
            if "message" in upd and "text" in upd["message"]:
                raw_cmd = upd["message"]["text"].strip()
            elif "callback_query" in upd:
                raw_cmd = upd["callback_query"].get("data", "").strip()
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                    data={"callback_query_id": upd["callback_query"]["id"]},
                    timeout=5
                )

            if not raw_cmd:
                continue
            cmd = raw_cmd.lower()

            if cmd.startswith("/durum"):
                tg_send(format_status_message(state, bot_ctx["start_time"]), reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

            elif cmd.startswith("/ekran"):
                send_screenshot(page, f"📸 <b>Canlı Ekran:</b> <code>{state['current_streamer']}</code>", critical=True, state=state)

            elif cmd.startswith("/yayincilar"):
                tg_send("🔍 <i>Canlı Türkçe GTA VI yayıncıları listeleniyor...</i>", critical=True, state=state)
                detailed = fetch_tr_gta6_streamers_detailed(page, state["blacklist"])
                # Kategori sayfasından sonra tekrar izlenen kanala dön
                open_stream(page, state["current_streamer"], state, count_switch=False)
                tg_send(
                    "📺 <b>CANLI TÜRKÇE GTA VI YAYINCILARI</b>\nGeçmek istediğin yayıncıya tek tıkla:",
                    reply_markup=build_streamers_keyboard(detailed),
                    critical=True,
                    state=state
                )

            elif cmd.startswith("watch:"):
                target = raw_cmd.split(":", 1)[1].strip().lower()
                if target:
                    open_stream(page, target, state, count_switch=True)
                    send_screenshot(page, f"✅ <b>Kanala Geçildi:</b> <code>{target}</code> (👁️ {format_viewers(state['current_viewers'])})", critical=True, state=state)

            elif cmd.startswith("/drops"):
                tg_send("🔍 <i>Drops kampanyası taranıyor...</i>", critical=True, state=state)
                summary = scan_and_claim_drops(context, state)
                tg_send(f"🎁 <b>Güncel SunsetK Badge:</b>\n<code>{summary}</code>\n📈 <b>Verimlilik:</b> <code>{state['efficiency_text']}</code>", reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

            elif cmd.startswith("/degistir"):
                detailed = fetch_tr_gta6_streamers_detailed(page, state["blacklist"])
                bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(detailed)
                new_slug = detailed[bot_ctx["stream_idx"]]["slug"]
                open_stream(page, new_slug, state, count_switch=True)
                send_screenshot(page, f"🔄 <b>Yayıncı Değiştirildi:</b> <code>{new_slug}</code>", critical=True, state=state)

            elif cmd.startswith("/izle "):
                target = raw_cmd.split(" ", 1)[1].strip().lower()
                if target:
                    open_stream(page, target, state, count_switch=True)
                    send_screenshot(page, f"🎯 <b>Özel Kanal Açıldı:</b> <code>{target}</code>", critical=True, state=state)

            elif cmd.startswith("/engelle "):
                target = raw_cmd.split(" ", 1)[1].strip().lower()
                if target and target not in state["blacklist"]:
                    state["blacklist"].append(target)
                    save_state(state)
                    tg_send(f"🚫 <code>{target}</code> kara listeye eklendi.", critical=True, state=state)
                    if state["current_streamer"] == target:
                        detailed = fetch_tr_gta6_streamers_detailed(page, state["blacklist"])
                        open_stream(page, detailed[0]["slug"], state, count_switch=True)

            elif cmd.startswith("/log"):
                logs_text = "\n".join(list(LOG_BUFFER)[-25:]) or "Henüz log yok."
                tg_send(f"📜 <b>Son 25 Log Satırı:</b>\n<pre>{logs_text[-3500:]}</pre>", critical=True, state=state)

            elif cmd.startswith("/sessiz"):
                state["silent_mode"] = not state.get("silent_mode", False)
                save_state(state)
                durum = "AÇIK (Sadece kritik alarmlar)" if state["silent_mode"] else "KAPALI (Tüm bildirimler)"
                tg_send(f"🔕 <b>Sessiz Mod:</b> {durum}", reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

            elif cmd.startswith("/restart"):
                tg_send("♻️ <b>Uzaktan Yeniden Başlatma:</b> Yeni sunucu tetikleniyor...", critical=True, state=state)
                trigger_next_workflow(state, reason="Telegram /restart komutu")
                bot_ctx["force_exit"] = True

            elif cmd.startswith("/durdur"):
                state["paused"] = True
                save_state(state)
                tg_send("🛑 <b>Maraton Durduruldu!</b> Tekrar başlatmak için GitHub Actions üzerinden Run Workflow yapabilirsin.", critical=True, state=state)
                bot_ctx["force_exit"] = True
    except Exception:
        pass

# ==========================================
# 13. ANA ÇALIŞMA DÖNGÜSÜ & KATMAN 2 KURTARMA
# ==========================================
def run_bot(context, state):
    context.add_init_script(VISIBILITY_JS)
    setup_resource_blocker(context)
    setup_telegram_menu()

    if not parse_and_load_cookies(context):
        tg_send("❌ <b>KRİTİK HATA:</b> KICK_COOKIES_JSON okunamadı!", critical=True, state=state)
        return

    bot_ctx = {
        "state": state,
        "start_time": time.time(),
        "stream_idx": 0,
        "force_exit": False
    }

    page = context.pages[0] if context.pages else context.new_page()

    detailed = fetch_tr_gta6_streamers_detailed(page, state["blacklist"])
    open_stream(page, detailed[0]["slug"], state, count_switch=False)

    if not check_session_authenticated(page):
        send_screenshot(page, "⚠️ <b>KRİTİK UYARI:</b> Kick oturumu düşmüş görünüyor! Lütfen <code>KICK_COOKIES_JSON</code> secret değerini yenile.", critical=True, state=state)

    scan_and_claim_drops(context, state)
    send_screenshot(page, format_status_message(state, bot_ctx["start_time"]), state=state)

    last_health_check = time.time()
    last_drops_scan = time.time()
    last_video_time = -1

    while (time.time() - bot_ctx["start_time"]) < MAX_RUN_SECONDS:
        if bot_ctx["force_exit"] or state.get("completed"):
            break

        poll_telegram_commands(page, context, bot_ctx)
        check_and_send_daily_report(state)

        # Her 3 dakikada bir yayın sağlığı, kategori ve donma kontrolü
        if (time.time() - last_health_check) > 180:
            last_health_check = time.time()
            status, last_video_time = check_stream_health_and_category(page, state, last_video_time)

            if status in ("offline", "wrong_game"):
                reason = "yayını kapattı" if status == "offline" else "GTA VI'dan başka oyuna geçti"
                log(f"⚠️ {state['current_streamer']} {reason}, yeni kanala geçiliyor...")
                detailed = fetch_tr_gta6_streamers_detailed(page, state["blacklist"])
                bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(detailed)
                open_stream(page, detailed[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)
                last_video_time = -1

            elif status == "frozen":
                log(f"❄️ {state['current_streamer']} oynatıcısı dondu, sayfa yenileniyor...")
                page.reload(wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(4000)
                last_video_time = -1

        # Her 30 dakikada bir Drops taraması + Takılma alarmı
        if (time.time() - last_drops_scan) > 1800:
            last_drops_scan = time.time()
            scan_and_claim_drops(context, state)

            if state.get("stuck_scans", 0) >= 2:
                log("🚨 Süre üst üste 2 taramada ilerlemedi! Yayıncı değiştiriliyor...")
                tg_send(
                    f"🚨 <b>SÜRE İLERLEMİYOR ALARMI!</b>\n"
                    f"<code>{state['current_streamer']}</code> kanalında son 2 taramada SunsetK Badge ilerlemedi. Başka kanala geçiliyor...",
                    critical=True,
                    state=state
                )
                state["stuck_scans"] = 0
                save_state(state)
                detailed = fetch_tr_gta6_streamers_detailed(page, state["blacklist"])
                bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(detailed)
                open_stream(page, detailed[bot_ctx["stream_idx"]]["slug"], state, count_switch=True)

        time.sleep(4)

    # Katman 1: Normal 5.4 saatlik bayrak devri
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
        tg_send(f"💥 <b>Bot Çöktü!</b> Otomatik kurtarma (Katman 2) devreye giriyor...\n<code>{err_short}</code>", critical=True, state=state)
    finally:
        # Katman 2: Beklenmedik çökmede ödül alınmadıysa ve durdurulmadıysa yeni sunucu başlat
        state = load_state()
        if crashed and not state.get("completed") and not state.get("paused"):
            trigger_next_workflow(state, reason="Katman 2 otomatik çökme kurtarması")

if __name__ == "__main__":
    main()
