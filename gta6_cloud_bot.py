import os
import re
import sys
import json
import time
import random
import warnings
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
MAX_RUN_SECONDS = int(5.4 * 3600)  # 5 saat 24 dakika sonra bayrak devri
TOTAL_TARGET_MINUTES = 250 * 60    # 15.000 dakika (250 saat)

GTA6_TR_CATEGORY_URL = "https://kick.com/category/grand-theft-auto-vi?sort=viewers_high_to_low&languages=tr"
DROPS_CAMPAIGNS_URL = "https://kick.com/drops/campaigns"

FALLBACK_SLUGS = ["rraenee", "eray", "mithrain", "hype", "elraenn", "cordiseps", "wtcn"]

# Son 50 log satırını bellekte tut (/log komutu için)
LOG_BUFFER = deque(maxlen=50)

def log(msg):
    ts = datetime.now(timezone(timedelta(hours=3))).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_BUFFER.append(line)

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
    default_state = {
        "progress_ratio": 0.0,
        "percent": 0.0,
        "remaining_text": "Henüz taranmadı",
        "completed": False,
        "paused": False,
        "silent_mode": False,
        "current_streamer": "Yok",
        "blacklist": [],
        "session_count": 0,
        "stuck_scans": 0,
        "last_update_offset": 0
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
# 4. TELEGRAM KONTROL PANELİ & BİLDİRİMLER
# ==========================================
def get_control_keyboard(silent_mode=False):
    silent_label = "🔔 Sessiz Mod: AÇIK" if silent_mode else "🔕 Sessiz Mod: KAPALI"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Anlık Durum", "callback_data": "/durum"},
                {"text": "📸 Ekran Görüntüsü", "callback_data": "/ekran"}
            ],
            [
                {"text": "🎁 Drops Tara", "callback_data": "/drops"},
                {"text": "🔄 Yayıncı Değiştir", "callback_data": "/degistir"}
            ],
            [
                {"text": "📜 Son Loglar", "callback_data": "/log"},
                {"text": silent_label, "callback_data": "/sessiz"}
            ],
            [
                {"text": "♻️ Yeniden Başlat", "callback_data": "/restart"},
                {"text": "🛑 Durdur", "callback_data": "/durdur"}
            ]
        ]
    }

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
    filled = int(round((pct / 100.0) * length))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)

def format_status_message(state, start_time):
    elapsed_min = int((time.time() - start_time) // 60)
    pct = state.get("percent", 0.0)
    bar = build_progress_bar(pct)

    # Tahmini bitiş tarihi (TR saati)
    rem_min = max(0, int(round(TOTAL_TARGET_MINUTES * (1.0 - (pct / 100.0)))))
    eta_dt = datetime.now(timezone(timedelta(hours=3))) + timedelta(minutes=rem_min)
    eta_str = eta_dt.strftime("%d.%m.%Y %H:%M") if pct > 0 else "Hesaplanıyor..."

    return (
        f"🎮 <b>GTA VI 250 SAAT BULUT MARATONU</b>\n\n"
        f"📺 <b>İzlenen Kanal:</b> <code>{state['current_streamer']}</code>\n"
        f"🎁 <b>SunsetK Badge:</b> <code>{state['remaining_text']}</code>\n"
        f"📊 <b>İlerleme:</b> <code>[{bar}] %{pct:.2f}</code>\n"
        f"📅 <b>Tahmini Bitiş (TR):</b> <code>{eta_str}</code>\n"
        f"🔁 <b>Toplam Oturum:</b> <code>#{state['session_count']}</code> | ⏱️ <b>Aktif:</b> <code>{elapsed_min} dk</code>\n\n"
        f"<i>Komutlar: /izle kanal_adi | /engelle kanal_adi</i>"
    )

# ==========================================
# 5. ÇEREZ YÜKLEYİCİ & OTURUM SAĞLIK KONTROLÜ
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
    """Kick hesabının gerçekten giriş yapmış durumda olduğunu kontrol eder."""
    try:
        is_auth = page.evaluate(r"""() => {
            const html = document.documentElement.innerHTML || "";
            if (html.includes('"status":"authenticated"') || html.includes('session_token')) return true;
            const loginBtn = Array.from(document.querySelectorAll('button, a')).find(el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'giriş yap' || t === 'log in';
            });
            return !loginBtn;
        }""")
        return bool(is_auth)
    except Exception:
        return True

# ==========================================
# 6. TÜRKÇE GTA VI YAYINCISI BULUCU
# ==========================================
def find_tr_gta6_streamers(page, blacklist):
    """En çok izlenen Türkçe GTA VI yayıncılarını çeker, engellileri eler."""
    live_list = []
    bl_set = set(s.lower() for s in blacklist)

    try:
        log("🔍 Türkçe GTA VI yayıncıları taranıyor...")
        page.goto(GTA6_TR_CATEGORY_URL, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(4500)

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
            return found.slice(0, 20);
        }""")

        for s in dom_slugs:
            if s not in bl_set and s not in live_list:
                live_list.append(s)
    except Exception as e:
        log(f"⚠️ Kategori sayfası tarama uyarısı: {e}")

    # Liste boşsa yedek havuzdan canlı ve GTA VI oynayanları ekle
    if not live_list:
        for slug in FALLBACK_SLUGS:
            if slug not in bl_set:
                live_list.append(slug)

    log(f"📋 Bulunan Türkçe GTA VI havuzu ({len(live_list)}): {', '.join(live_list[:6])}")
    return live_list

# ==========================================
# 7. DROPS KAMPANYA TARAYICI (ARIA-VALUENOW HASSAS OKUMA)
# ==========================================
def scan_and_claim_drops(context, state):
    """https://kick.com/drops/campaigns üzerinden SunsetK Badge ilerlemesini (aria-valuenow) okur ve ödülü talep eder."""
    drop_page = None
    summary = "Drops sayfası taranamadı."
    try:
        drop_page = context.new_page()
        drop_page.goto(DROPS_CAMPAIGNS_URL, wait_until="domcontentloaded", timeout=35000)
        drop_page.wait_for_timeout(5500)

        # 1. Aktif "Talep et" / "Claim" butonu varsa tıkla
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

        # 2. Doğrudan SunsetK Badge <li> kartı içindeki role="progressbar" -> aria-valuenow değerini oku
        card_data = drop_page.evaluate(r"""() => {
            const items = Array.from(document.querySelectorAll('li'));
            for (const li of items) {
                if ((li.innerText || '').includes('SunsetK Badge')) {
                    const bar = li.querySelector('[role="progressbar"]');
                    const valNow = bar ? (bar.getAttribute('aria-valuenow') || bar.getAttribute('data-value')) : null;
                    const subtleSpan = li.querySelector('span.text-surface-fg-subtle');
                    const remText = subtleSpan ? subtleSpan.innerText.trim() : '';
                    return { valNow: valNow ? parseFloat(valNow) : null, remText: remText };
                }
            }
            return null;
        }""")

        if card_data and card_data.get("valNow") is not None:
            ratio = float(card_data["valNow"])
            old_ratio = float(state.get("progress_ratio", 0.0))

            # Süre ilerlemiyor kontrolü
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
            state["remaining_text"] = f"{rem_h} sa. {rem_m} dk. kaldı (İzlenen: {w_h} sa. {w_m} dk.)"
            save_state(state)
            summary = f"{state['remaining_text']} (%{pct:.2f})"
            log(f"🎁 Drops güncellendi: {summary}")
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
# 8. YAYIN AÇICI, 160P AYARI & ENGEL TEMİZLEYİCİ
# ==========================================
def open_stream(page, slug, state):
    url = f"https://kick.com/{slug}"
    log(f"📺 Yayın açılıyor: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=35000)
    page.wait_for_timeout(4500)

    # 18+ uyarıları, çerez pop-up'ları ve sessiz oynatma
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
    }""")

    # 160p kaliteye düşürmeyi dene (RAM/CPU tasarrufu)
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

    state["current_streamer"] = slug
    save_state(state)

def check_stream_health_and_category(page, slug, last_video_time):
    """Yayın canlı mı, hala GTA VI mı oynuyor ve video donmuş mu kontrol eder."""
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
                return {{ ok: isLive, curTime: curTime, isGta6: isGta6 }};
            }} catch(e) {{
                return {{ ok: true, curTime: curTime, isGta6: true }};
            }}
        }}""")

        is_live = res.get("ok", True)
        is_gta6 = res.get("isGta6", True)
        cur_time = res.get("curTime", -1)

        if not is_live:
            return "offline", cur_time
        if not is_gta6:
            return "wrong_game", cur_time
        if last_video_time > 0 and cur_time == last_video_time:
            return "frozen", cur_time
        return "healthy", cur_time
    except Exception:
        return "healthy", last_video_time

# ==========================================
# 9. OTOMATİK BAYRAK DEVRİ (WORKFLOW DISPATCH)
# ==========================================
def trigger_next_workflow(state, reason="5.4 saatlik oturum tamamlandı"):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log("⚠️ GITHUB_TOKEN veya GITHUB_REPOSITORY eksik, bayrak devri yapılamadı.")
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
# 10. TELEGRAM KOMUT DİNLEYİCİSİ
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

            elif cmd.startswith("/drops"):
                tg_send("🔍 <i>Drops kampanyası taranıyor...</i>", critical=True, state=state)
                summary = scan_and_claim_drops(context, state)
                tg_send(f"🎁 <b>Güncel SunsetK Badge:</b>\n<code>{summary}</code>", reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

            elif cmd.startswith("/degistir"):
                streamers = find_tr_gta6_streamers(page, state["blacklist"])
                bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                new_slug = streamers[bot_ctx["stream_idx"]]
                open_stream(page, new_slug, state)
                tg_send(f"🔄 <b>Yayıncı Değiştirildi:</b> <code>{new_slug}</code>", reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

            elif cmd.startswith("/izle "):
                target = raw_cmd.split(" ", 1)[1].strip().lower()
                if target:
                    open_stream(page, target, state)
                    tg_send(f"🎯 <b>Özel Kanal Açıldı:</b> <code>{target}</code>", reply_markup=get_control_keyboard(state["silent_mode"]), critical=True, state=state)

            elif cmd.startswith("/engelle "):
                target = raw_cmd.split(" ", 1)[1].strip().lower()
                if target and target not in state["blacklist"]:
                    state["blacklist"].append(target)
                    save_state(state)
                    tg_send(f"🚫 <code>{target}</code> kara listeye eklendi.", critical=True, state=state)
                    if state["current_streamer"] == target:
                        streamers = find_tr_gta6_streamers(page, state["blacklist"])
                        open_stream(page, streamers[0], state)

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
# 11. ANA ÇALIŞMA DÖNGÜSÜ
# ==========================================
def run_bot(context):
    context.add_init_script(VISIBILITY_JS)
    state = load_state()

    if state.get("completed"):
        log("🏆 Ödül zaten tamamlanmış, çıkılıyor.")
        return

    state["paused"] = False
    state["session_count"] = state.get("session_count", 0) + 1
    save_state(state)

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

    # 1. İlk Türkçe GTA VI yayıncısını seç ve aç
    streamers = find_tr_gta6_streamers(page, state["blacklist"])
    first_slug = streamers[0]
    open_stream(page, first_slug, state)

    # 2. Oturum / Çerez sağlık kontrolü
    if not check_session_authenticated(page):
        send_screenshot(page, "⚠️ <b>KRİTİK UYARI:</b> Kick oturumu düşmüş görünüyor! Lütfen GitHub Secrets içindeki <code>KICK_COOKIES_JSON</code> değerini yenile.", critical=True, state=state)

    # 3. İlk Drops taraması ve başlangıç bildirimi
    scan_and_claim_drops(context, state)
    send_screenshot(page, format_status_message(state, bot_ctx["start_time"]), state=state)

    last_health_check = time.time()
    last_drops_scan = time.time()
    last_video_time = -1

    # 4. 7/24 İzleme Döngüsü
    while (time.time() - bot_ctx["start_time"]) < MAX_RUN_SECONDS:
        if bot_ctx["force_exit"] or state.get("completed"):
            break

        poll_telegram_commands(page, context, bot_ctx)

        # Her 3 dakikada bir yayın sağlığı, oyun kategorisi ve donma kontrolü
        if (time.time() - last_health_check) > 180:
            last_health_check = time.time()
            status, last_video_time = check_stream_health_and_category(page, state["current_streamer"], last_video_time)

            if status in ("offline", "wrong_game"):
                reason = "yayını kapattı" if status == "offline" else "GTA VI'dan başka oyuna geçti"
                log(f"⚠️ {state['current_streamer']} {reason}, yeni kanala geçiliyor...")
                streamers = find_tr_gta6_streamers(page, state["blacklist"])
                bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                open_stream(page, streamers[bot_ctx["stream_idx"]], state)
                last_video_time = -1

            elif status == "frozen":
                log(f"❄️ {state['current_streamer']} oynatıcısı donmuş görünüyor, sayfa yenileniyor...")
                page.reload(wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(4000)
                last_video_time = -1

        # Her 30 dakikada bir Drops ilerlemesini tara ve takılma alarmını kontrol et
        if (time.time() - last_drops_scan) > 1800:
            last_drops_scan = time.time()
            scan_and_claim_drops(context, state)

            if state.get("stuck_scans", 0) >= 2:
                log("🚨 Süre üst üste 2 taramada ilerlemedi! Yayıncı değiştiriliyor...")
                tg_send(
                    f"🚨 <b>SÜRE İLERLEMİYOR ALARMI!</b>\n"
                    f"<code>{state['current_streamer']}</code> kanalında son 2 taramada SunsetK Badge ilerlemedi. Otomatik başka kanala geçiliyor...",
                    critical=True,
                    state=state
                )
                state["stuck_scans"] = 0
                save_state(state)
                streamers = find_tr_gta6_streamers(page, state["blacklist"])
                bot_ctx["stream_idx"] = (bot_ctx["stream_idx"] + 1) % len(streamers)
                open_stream(page, streamers[bot_ctx["stream_idx"]], state)

        time.sleep(4)

    # Oturum süresi dolduysa ve durdurulmadıysa bayrağı devret
    if not bot_ctx["force_exit"] and not state.get("completed") and not state.get("paused"):
        trigger_next_workflow(state)

def main():
    log("🚀 GTA VI 250 Saat Bulut Maraton Botu başlatılıyor...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir="/tmp/kick_chrome_profile",
            channel="chrome",
            headless=False,
            viewport={"width": 1280, "height": 720},
            locale="tr-TR",
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        run_bot(context)
        context.close()

if __name__ == "__main__":
    main()
