import os
import sys
import json
import time
import random
import shutil
import traceback
from datetime import datetime, timezone, timedelta

# Türkiye Saat Dilimi (UTC+3)
TR_TZ = timezone(timedelta(hours=3))
STATE_FILE = "marathon_state.json"

def now_tr():
    return datetime.now(TR_TZ)

def load_state():
    default_state = {
        "paused": False,
        "completed": False,
        "last_channel": "",
        "remaining_text": "250 sa. 0 dk.",
        " watched_minutes_est": 0,
        "percent": 0.0,
        "updated_at": ""
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
        except Exception:
            pass
    return default_state

def save_state(state):
    state["updated_at"] = now_tr().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[UYARI] Durum kaydedilemedi: {e}")

# ==============================================================================
# 1. HAFİF ÖN KONTROL (--pre-check) - Hiçbir kütüphane kurulmadan önce çalışır
# ==============================================================================
def run_lightweight_precheck():
    trigger = os.environ.get("TRIGGER_NAME", "")
    input_action = os.environ.get("INPUT_ACTION", "run")
    github_env = os.environ.get("GITHUB_ENV", "")

    state = load_state()

    # Manuel tetiklemede 'reset' seçildiyse duraklatmayı kaldır
    if trigger == "workflow_dispatch" and input_action == "reset":
        state["paused"] = False
        state["completed"] = False
        save_state(state)
        print("[PRE-CHECK] Manuel sıfırlama algılandı. Maraton aktif edildi!")
        sys.exit(0)

    if state.get("completed", False):
        print("🏆 [PRE-CHECK] 250 saatlik SunsetK Badge ödülü zaten tamamlanmış! Sunucu kapatılıyor.")
        if github_env:
            with open(github_env, "a") as f:
                f.write("SKIP_BOT=true\n")
        sys.exit(0)

    if state.get("paused", False) and trigger != "workflow_dispatch":
        print("⏸️ [PRE-CHECK] Maraton kullanıcı tarafından duraklatılmış modda. Sunucu açılmıyor.")
        if github_env:
            with open(github_env, "a") as f:
                f.write("SKIP_BOT=true\n")
        sys.exit(0)

    print("🚀 [PRE-CHECK] Maraton aktif! GTA VI Drops sunucusu başlatılıyor...")
    sys.exit(0)

if "--pre-check" in sys.argv:
    run_lightweight_precheck()

# ==============================================================================
# ANA KÜTÜPHANELER (Sadece --pre-check geçildikten sonra import edilir)
# ==============================================================================
import re
import requests
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
COOKIES_RAW = os.environ.get("KICK_COOKIES_JSON", "").strip()

GTA6_CATEGORY_URL = "https://kick.com/category/grand-theft-auto-vi"
GTA6_DROPS_URL = "https://kick.com/drops"
MAX_RUNTIME_MINUTES = 330  # 5 saat 30 dakika sonra temiz bayrak devri yapar

# ==============================================================================
# 2. DİNAMİK MANIFEST V3 UZANTISI (kick_pro_ext) İNŞA EDİCİ
# ==============================================================================
EXT_DIR = os.path.abspath("kick_pro_ext")

MANIFEST_JSON = {
    "manifest_version": 3,
    "name": "Kick GTA6 Marathon Pro Helper",
    "version": "10.0",
    "description": "WebSocket Watch Tracker & Focus Lock for GTA VI Drops",
    "content_scripts": [
        {
            "matches": ["*://*.kick.com/*"],
            "js": ["content.js"],
            "run_at": "document_start",
            "world": "MAIN"
        }
    ]
}

CONTENT_JS = r"""
(function() {
    if (window.__KICK_GTA6_PRO__) return;
    window.__KICK_GTA6_PRO__ = true;

    const proState = {
        wsConnected: false,
        lastPingTime: 0,
        pingCount: 0,
        activeSlug: "",
        videoMuted: false
    };

    function syncStateToDOM() {
        try {
            if (document.documentElement) {
                document.documentElement.setAttribute('data-kick-pro-state', JSON.stringify(proState));
            }
        } catch (e) {}
    }

    // 1. STEALTH & ODAK KİLİDİ (Her zaman aktif sekme gibi görünür)
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(document, 'visibilityState', { get: () => 'visible' });
        Object.defineProperty(document, 'hidden', { get: () => false });
        Document.prototype.hasFocus = function() { return true; };

        ['visibilitychange', 'blur', 'pagehide', 'freeze'].forEach(evt => {
            window.addEventListener(evt, e => e.stopImmediatePropagation(), true);
            document.addEventListener(evt, e => e.stopImmediatePropagation(), true);
        });
    } catch (e) {}

    // 2. WEBSOCKET YAKALAYICI & HEARTBEAT MOTORU
    const activeSockets = new Set();
    let lastWatchPayload = null;
    const OrigWebSocket = window.WebSocket;

    window.WebSocket = function(url, protocols) {
        const ws = protocols ? new OrigWebSocket(url, protocols) : new OrigWebSocket(url);
        activeSockets.add(ws);
        proState.wsConnected = true;
        syncStateToDOM();

        ws.addEventListener('close', () => {
            activeSockets.delete(ws);
            proState.wsConnected = activeSockets.size > 0;
            syncStateToDOM();
        });
        return ws;
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;
    Object.assign(window.WebSocket, OrigWebSocket);

    const origSend = OrigWebSocket.prototype.send;
    OrigWebSocket.prototype.send = function(data) {
        activeSockets.add(this);
        proState.wsConnected = true;
        try {
            if (typeof data === 'string' && (data.includes('watch') || data.includes('livestream') || data.includes('handshake'))) {
                lastWatchPayload = data;
                proState.lastPingTime = Date.now();
                proState.pingCount++;
                syncStateToDOM();
            }
        } catch (e) {}
        return origSend.apply(this, arguments);
    };

    // Gaussian Jitter (~30s ± 3.5s) ile WebSocket İzleme Sinyali Gönderici
    function scheduleNextHeartbeat() {
        const jitter = (Math.random() - 0.5) * 7000;
        const delay = Math.max(22000, Math.min(38000, 30000 + jitter));
        setTimeout(() => {
            try {
                activeSockets.forEach(ws => {
                    if (ws.readyState === 1 && lastWatchPayload) {
                        origSend.call(ws, lastWatchPayload);
                        proState.lastPingTime = Date.now();
                        proState.pingCount++;
                    }
                });
                syncStateToDOM();
            } catch (e) {}
            scheduleNextHeartbeat();
        }, delay);
    }
    scheduleNextHeartbeat();

    // 3. VİDEO OPTİMİZASYONU (Sıfır CPU / Sessiz & Düşük Kalite)
    setInterval(() => {
        try {
            const v = document.querySelector('video');
            if (v) {
                v.muted = true;
                v.volume = 0;
                proState.videoMuted = true;
                if (v.paused) {
                    v.play().catch(() => {});
                }
            }
            // Otomatik 160p kalite tercihi
            localStorage.setItem('stream_quality', '160');
            syncStateToDOM();
        } catch (e) {}
    }, 5000);

    // 4. GÖRÜNÜR METİN OKUYUCU (Gizli __NEXT_DATA__ scriptlerini hariç tutar)
    window.__GET_VISIBLE_TEXT__ = function() {
        if (!document.body) return "";
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        let node, result = [];
        while (node = walker.nextNode()) {
            const parent = node.parentElement;
            if (!parent) continue;
            const tag = parent.tagName.toLowerCase();
            if (['script', 'style', 'noscript', 'template'].includes(tag)) continue;
            const rect = parent.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                const txt = node.nodeValue.trim();
                if (txt.length > 1) result.push(txt);
            }
        }
        return result.join(" | ");
    };
})();
"""

def build_extension():
    if os.path.exists(EXT_DIR):
        shutil.rmtree(EXT_DIR)
    os.makedirs(EXT_DIR, exist_ok=True)
    with open(os.path.join(EXT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST_JSON, f, indent=2)
    with open(os.path.join(EXT_DIR, "content.js"), "w", encoding="utf-8") as f:
        f.write(CONTENT_JS.strip())

# ==============================================================================
# 3. TELEGRAM HABERLEŞME VE CAM BUTON (INLINE KEYBOARD) PANELİ
# ==============================================================================
def tg_api(method, payload=None, files=None):
    if not BOT_TOKEN or not CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=20)
        else:
            r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[TG HATA] {method}: {e}")
        return None

def get_main_keyboard(paused=False):
    pause_btn = {"text": "▶️ Maratonu Başlat", "callback_data": "resume_bot"} if paused else {"text": "⏸️ Maratonu Duraklat", "callback_data": "pause_bot"}
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Canlı Yenile", "callback_data": "refresh_status"},
                {"text": "📸 Ekran Çek", "callback_data": "take_photo"}
            ],
            [
                {"text": "🎲 Başka GTA VI Kanalı", "callback_data": "switch_random"},
                {"text": "📋 GTA VI Kanalları", "callback_data": "list_channels"}
            ],
            [
                {"text": "🎁 Drops Süresini Tara", "callback_data": "scan_drops"},
                pause_btn
            ]
        ]
    }

def build_progress_bar(percent):
    filled = int(round(percent / 10))
    filled = max(0, min(10, filled))
    return "🟩" * filled + "⬜" * (10 - filled)

def format_status_message(bot_ctx):
    state = bot_ctx["state"]
    elapsed_min = int((time.time() - bot_ctx["start_time"]) / 60)
    rem_server = max(0, MAX_RUNTIME_MINUTES - elapsed_min)

    percent = state.get("percent", 0.0)
    bar = build_progress_bar(percent)
    ws_status = "Aktif 🟢" if bot_ctx.get("ws_ok") else "Bekleniyor 🟡"
    session_status = "Sağlam 🟢" if bot_ctx.get("session_ok", True) else "DÜŞTÜ 🔴 (Çerez Yenile!)"
    cat_ok = "Grand Theft Auto VI ✅" if bot_ctx.get("is_gta6_cat", True) else "Kategori Kaydı (Değiştiriliyor) ⚠️"

    msg = (
        f"🌴 <b>KICK GTA VI DROPS MARATONU (v10.0)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 <b>Aktif Kanal:</b> <code>{bot_ctx.get('current_channel', 'Aranıyor...')}</code>\n"
        f"🎮 <b>Kategori:</b> {cat_ok}\n"
        f"🏆 <b>Ödül:</b> SunsetK Badge (250 Saat)\n"
        f"⏳ <b>Kalan Süre:</b> <code>{state.get('remaining_text', 'Hesaplanıyor...')}</code>\n"
        f"📊 <b>İlerleme:</b> [{bar}] <b>%{percent:.1f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔌 <b>WebSocket:</b> {ws_status} (Ping: {bot_ctx.get('ping_count', 0)})\n"
        f"🔑 <b>Oturum:</b> {session_status}\n"
        f"⏱️ <b>Bu Sunucu:</b> {elapsed_min} dk / {MAX_RUNTIME_MINUTES} dk (Bayrak Devrine {rem_server} dk)\n"
        f"🕒 <b>Son Güncelleme:</b> {now_tr().strftime('%H:%M:%S')}"
    )
    return msg

def send_status(bot_ctx, edit_msg_id=None):
    text = format_status_message(bot_ctx)
    kb = get_main_keyboard(bot_ctx["state"].get("paused", False))
    if edit_msg_id:
        tg_api("editMessageText", {
            "chat_id": CHAT_ID,
            "message_id": edit_msg_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": kb
        })
    else:
        tg_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": kb
        })

def send_screenshot(page, caption=""):
    path = "snap_gta6.png"
    try:
        page.screenshot(path=path, full_page=False)
        with open(path, "rb") as f:
            tg_api("sendPhoto", {
                "chat_id": CHAT_ID,
                "caption": caption[:1000],
                "parse_mode": "HTML"
            }, files={"photo": f})
    except Exception as e:
        print(f"[FOTO HATA] {e}")

# ==============================================================================
# 4. GTA VI KATEGORİ TARAYICI VE KANAL DOĞRULAYICI
# ==============================================================================
def fetch_live_gta6_channels(page):
    """GTA VI kategorisindeki aktif canlı yayınları izleyici sırasıyla döndürür."""
    channels = []
    # Öncelikli Türk & popüler GTA VI yayıncıları (Canlı ve GTA VI oynuyorlarsa öne alınır)
    preferred = ["rraenee", "eray", "mithrain", "hype", "elraenn", "jahrein", "wtcn", "kemalcanparlak", "pqueen", "burakoyunda"]
    try:
        # Kategori sayfasını arka planda veya doğrudan kontrol et
        temp_page = page.context.new_page()
        temp_page.goto(GTA6_CATEGORY_URL, wait_until="domcontentloaded", timeout=35000)
        temp_page.wait_for_timeout(4500)

        found = temp_page.evaluate(r"""() => {
            const results = [];
            const seen = new Set();
            const links = Array.from(document.querySelectorAll('a[href^="/"]'));
            const reserved = new Set(['browse', 'categories', 'category', 'drops', 'following', 'search', 'login', 'signup', 'terms', 'privacy', 'dashboard']);

            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const parts = href.split('?')[0].split('/').filter(Boolean);
                if (parts.length !== 1) continue;
                const slug = parts[0].toLowerCase();
                if (reserved.has(slug) || seen.has(slug) || slug.length < 2) continue;

                // Kartın içinde video/thumbnail veya izleyici sayısı var mı kontrol et
                const card = a.closest('div.group, div[class*="card"], li') || a.parentElement;
                if (!card) continue;
                const rect = card.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 60) continue;

                seen.add(slug);
                results.push({ slug: slug, text: (card.innerText || slug).replace(/\n+/g, ' ').slice(0, 60) });
                if (results.length >= 20) break;
            }
            return results;
        }""")
        temp_page.close()

        if found:
            # Türk yayıncıları listede varsa en başa çek
            pref_list = [c for c in found if c["slug"] in preferred]
            other_list = [c for c in found if c["slug"] not in preferred]
            channels = pref_list + other_list
    except Exception as e:
        print(f"[KANAL TARAMA HATASI] {e}")
        try:
            temp_page.close()
        except Exception:
            pass

    return channels

def verify_channel_is_live_gta6(page, slug):
    """Kanalın hala canlı olup olmadığını ve GTA VI kategorisinde kaldığını doğrular."""
    if not slug:
        return False, False
    try:
        info = page.evaluate("""async (slug) => {
            try {
                const res = await fetch(`https://kick.com/api/v2/channels/${slug}`);
                if (!res.ok) return { ok: false };
                const data = await res.json();
                if (!data || !data.livestream || !data.livestream.is_live) {
                    return { ok: true, is_live: false, is_gta6: false };
                }
                const cats = data.livestream.categories || [];
                const catSlug = (cats[0] && cats[0].slug) ? cats[0].slug.toLowerCase() : "";
                const catName = (cats[0] && cats[0].name) ? cats[0].name.toLowerCase() : "";
                const isGta = catSlug.includes("grand-theft-auto-vi") || catName.includes("grand theft auto vi") || catName.includes("gta vi");
                return { ok: true, is_live: true, is_gta6: isGta, cat_name: catName };
            } catch (e) {
                return { ok: false };
            }
        }""", slug)

        if info and info.get("ok"):
            return info.get("is_live", False), info.get("is_gta6", True)
    except Exception:
        pass

    # API yanıt vermezse DOM üzerinden kontrol et
    try:
        vis_text = page.evaluate("window.__GET_VISIBLE_TEXT__ ? window.__GET_VISIBLE_TEXT__() : document.body.innerText")
        is_offline = "OFFLINE" in vis_text.upper() and "GRAND THEFT AUTO VI" not in vis_text.upper()
        is_gta = "GRAND THEFT AUTO VI" in vis_text.upper() or "GTA VI" in vis_text.upper()
        return (not is_offline), is_gta
    except Exception:
        return True, True

# ==============================================================================
# 5. DROPS SAYFASI TARAYICI (250 Saat Kalan Süre & Otomatik Claim)
# ==============================================================================
def scan_and_claim_gta6_drops(context, bot_ctx):
    """kick.com/drops sayfasını yeni sekmede açar, SunsetK Badge kalan süresini okur ve varsa talep eder."""
    state = bot_ctx["state"]
    drop_page = None
    result_summary = "Drops sayfası taranamadı."
    try:
        drop_page = context.new_page()
        drop_page.goto(GTA6_DROPS_URL, wait_until="domcontentloaded", timeout=35000)
        drop_page.wait_for_timeout(5000)

        # Sunset Drop veya genel görünür metni çek
        vis_text = drop_page.evaluate("window.__GET_VISIBLE_TEXT__ ? window.__GET_VISIBLE_TEXT__() : document.body.innerText")

        # 1. Aktif "Talep et" / "Claim" butonu var mı kontrol et
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
            drop_page.wait_for_timeout(2500)
            state["completed"] = True
            state["percent"] = 100.0
            state["remaining_text"] = "0 sa. 0 dk. (TAMAMLANDI! 🏆)"
            save_state(state)
            send_screenshot(drop_page, "🎉🏆 <b>MÜJDE! 250 Saatlik GTA VI SunsetK Badge Ödülü Otomatik Talep Edildi!</b>")
            drop_page.close()
            return "Ödül talep edildi!"

        # 2. "Kilidi açmak için 247 sa. 59 dk." veya "X hr Y min" kalıbını yakala
        # Türkçe kalıp: (\d+)\s*sa\.?\s*(\d+)?\s*dk\.?
        m_tr = re.search(r"(\d+)\s*sa\.?\s*(?:(\d+)\s*dk\.?)?", vis_text, re.IGNORECASE)
        m_en = re.search(r"(\d+)\s*h(?:r|ours?)?\s*(?:(\d+)\s*m(?:in)?)?", vis_text, re.IGNORECASE)

        rem_hours = None
        rem_mins = 0
        if m_tr:
            rem_hours = int(m_tr.group(1))
            rem_mins = int(m_tr.group(2)) if m_tr.group(2) else 0
        elif m_en:
            rem_hours = int(m_en.group(1))
            rem_mins = int(m_en.group(2)) if m_en.group(2) else 0

        if rem_hours is not None and rem_hours <= 250:
            total_rem_min = rem_hours * 60 + rem_mins
            watched_min = max(0, 15000 - total_rem_min)
            pct = round((watched_min / 15000.0) * 100, 2)
            state["remaining_text"] = f"{rem_hours} sa. {rem_mins} dk. (İzlenen: {watched_min // 60} sa {watched_min % 60} dk)"
            state["percent"] = pct
            save_state(state)
            result_summary = f"Kalan: {state['remaining_text']} (%{pct})"
        else:
            result_summary = f"Drops sayfası kontrol edildi. Mevcut: {state.get('remaining_text')}"

        drop_page.close()
    except Exception as e:
        print(f"[DROPS TARAMA HATASI] {e}")
        if drop_page:
            try:
                drop_page.close()
            except Exception:
                pass
    return result_summary

# ==============================================================================
# 6. ÇEREZ (COOKIE) DÖNÜŞTÜRÜCÜ VE OTURUM KONTROLCÜSÜ
# ==============================================================================
def parse_kick_cookies(raw_json):
    cookies = []
    try:
        data = json.loads(raw_json)
        for c in data:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".kick.com"),
                "path": c.get("path", "/")
            }
            cookies.append(cookie)
    except Exception as e:
        print(f"[ÇEREZ PARSE HATASI] KICK_COOKIES_JSON okunamadı: {e}")
    return cookies

def check_session_logged_in(page):
    """Sağ üst barda 'Giriş Yap' veya 'Log In' butonu varsa oturum düşmüştür."""
    try:
        logged_out = page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll('nav button, nav a, header button, header a'));
            for (const b of btns) {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t === 'giriş yap' || t === 'log in' || t === 'kayıt ol' || t === 'sign up') {
                    return true;
                }
            }
            return false;
        }""")
        return not logged_out
    except Exception:
        return True

# ==============================================================================
# 7. ANA MARATON DÖNGÜSÜ VE TELEGRAM KOMUT DİNLEYİCİ
# ==============================================================================
def switch_to_gta6_channel(page, bot_ctx, target_slug=None, exclude_current=False):
    channels = fetch_live_gta6_channels(page)
    bot_ctx["cached_channels"] = channels

    chosen_slug = target_slug
    if not chosen_slug:
        candidates = [c["slug"] for c in channels]
        if exclude_current and bot_ctx.get("current_channel") in candidates and len(candidates) > 1:
            candidates.remove(bot_ctx["current_channel"])
        if candidates:
            chosen_slug = candidates[0] if not exclude_current else random.choice(candidates[:6])
        else:
            chosen_slug = "rraenee"  # Yedek kanal

    print(f"[KANAL GEÇİŞİ] https://kick.com/{chosen_slug} açılıyor...")
    page.goto(f"https://kick.com/{chosen_slug}", wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(5000)

    # +18 Yaş Sınırı / Yetişkin İçerik butonu çıkarsa otomatik onayla
    try:
        page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                const t = (b.innerText || '').toLowerCase();
                if (t.includes('izlemeye başla') || t.includes('start watching') || t.includes('18+')) {
                    b.click();
                }
            }
        }""")
    except Exception:
        pass

    bot_ctx["current_channel"] = chosen_slug
    bot_ctx["state"]["last_channel"] = chosen_slug
    save_state(bot_ctx["state"])

def sync_extension_state(page, bot_ctx):
    try:
        raw = page.evaluate("document.documentElement.getAttribute('data-kick-pro-state')")
        if raw:
            st = json.loads(raw)
            bot_ctx["ws_ok"] = st.get("wsConnected", False)
            bot_ctx["ping_count"] = st.get("pingCount", 0)
    except Exception:
        pass

def process_telegram_updates(page, context, bot_ctx, last_update_id):
    if not BOT_TOKEN:
        return last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 1},
            timeout=5
        ).json()
        if not r.get("ok"):
            return last_update_id

        for upd in r.get("result", []):
            last_update_id = upd["update_id"]

            # 1. CAM BUTON (CALLBACK_QUERY) TIKLAMALARI
            if "callback_query" in upd:
                cb = upd["callback_query"]
                from_id = str(cb.get("from", {}).get("id", ""))
                if from_id != str(CHAT_ID):
                    continue

                data = cb.get("data", "")
                msg_id = cb.get("message", {}).get("message_id")
                tg_api("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "İşleniyor..."})

                if data == "refresh_status":
                    sync_extension_state(page, bot_ctx)
                    bot_ctx["session_ok"] = check_session_logged_in(page)
                    send_status(bot_ctx, edit_msg_id=msg_id)

                elif data == "take_photo":
                    sync_extension_state(page, bot_ctx)
                    send_screenshot(page, f"📸 <b>Anlık GTA VI Yayını:</b> <code>{bot_ctx.get('current_channel')}</code>")

                elif data == "switch_random":
                    switch_to_gta6_channel(page, bot_ctx, exclude_current=True)
                    sync_extension_state(page, bot_ctx)
                    send_status(bot_ctx, edit_msg_id=msg_id)

                elif data == "list_channels":
                    chans = fetch_live_gta6_channels(page)
                    rows = []
                    for c in chans[:8]:
                        rows.append([{"text": f"🟢 {c['slug']} ({c['text'][:25]})", "callback_data": f"pick_{c['slug']}"}])
                    rows.append([{"text": "🔙 Ana Menüye Dön", "callback_data": "refresh_status"}])
                    tg_api("editMessageText", {
                        "chat_id": CHAT_ID,
                        "message_id": msg_id,
                        "text": "📋 <b>Şu An Canlı GTA VI Yayıncıları:</b>\nGeçmek istediğin kanala dokun:",
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": rows}
                    })

                elif data.startswith("pick_"):
                    slug = data.replace("pick_", "").strip()
                    switch_to_gta6_channel(page, bot_ctx, target_slug=slug)
                    sync_extension_state(page, bot_ctx)
                    send_status(bot_ctx, edit_msg_id=msg_id)

                elif data == "scan_drops":
                    scan_and_claim_gta6_drops(context, bot_ctx)
                    send_status(bot_ctx, edit_msg_id=msg_id)

                elif data == "pause_bot":
                    bot_ctx["state"]["paused"] = True
                    save_state(bot_ctx["state"])
                    send_status(bot_ctx, edit_msg_id=msg_id)
                    tg_api("sendMessage", {
                        "chat_id": CHAT_ID,
                        "text": "⏸️ <b>Maraton Duraklatıldı!</b>\nSunucu kapatılıyor. Tekrar başlatmak için GitHub Actions'tan manuel tetikleyebilirsin.",
                        "parse_mode": "HTML"
                    })
                    sys.exit(0)

                elif data == "resume_bot":
                    bot_ctx["state"]["paused"] = False
                    save_state(bot_ctx["state"])
                    send_status(bot_ctx, edit_msg_id=msg_id)

            # 2. METİN KOMUTLARI (/durum, /ekran, /degistir, /drops)
            elif "message" in upd:
                msg = upd["message"]
                from_id = str(msg.get("from", {}).get("id", ""))
                if from_id != str(CHAT_ID):
                    continue
                txt = (msg.get("text") or "").strip().lower()

                if txt in ["/start", "/durum", "/panel"]:
                    sync_extension_state(page, bot_ctx)
                    send_status(bot_ctx)
                elif txt == "/ekran":
                    send_screenshot(page, f"📸 <b>Anlık Yayın:</b> <code>{bot_ctx.get('current_channel')}</code>")
                elif txt == "/degistir":
                    switch_to_gta6_channel(page, bot_ctx, exclude_current=True)
                    send_status(bot_ctx)
                elif txt == "/drops":
                    scan_and_claim_gta6_drops(context, bot_ctx)
                    send_status(bot_ctx)

    except Exception as e:
        print(f"[TG UPDATE HATA] {e}")
    return last_update_id

def main():
    print("==========================================================")
    print("🌴 KICK GTA VI 250 SAAT MARATON BOTU (v10.0) BAŞLATILIYOR")
    print("==========================================================")

    build_extension()
    state = load_state()

    bot_ctx = {
        "state": state,
        "start_time": time.time(),
        "current_channel": "",
        "ws_ok": False,
        "ping_count": 0,
        "session_ok": True,
        "is_gta6_cat": True,
        "cached_channels": []
    }

    user_data_dir = os.path.abspath("kick_profile")
    os.makedirs(user_data_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,  # Xvfb altında Manifest V3 uzantısı için zorunlu
            timezone_id="Europe/Istanbul",
            locale="tr-TR",
            viewport={"width": 1366, "height": 768},
            args=[
                f"--disable-extensions-except={EXT_DIR}",
                f"--load-extension={EXT_DIR}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--mute-audio",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        # Kick Çerezlerini Yükle
        cookies = parse_kick_cookies(COOKIES_RAW)
        if cookies:
            context.add_cookies(cookies)
            print(f"[OTURUM] {len(cookies)} adet Kick çerezi başarıyla yüklendi.")

        page = context.pages[0] if context.pages else context.new_page()

        # İlk GTA VI Kanalını Bul ve Gir
        switch_to_gta6_channel(page, bot_ctx)
        sync_extension_state(page, bot_ctx)
        bot_ctx["session_ok"] = check_session_logged_in(page)

        # İlk Drops Süresini Tara
        scan_and_claim_gta6_drops(context, bot_ctx)

        # Telegram'a Başlangıç Panelini Gönder
        send_status(bot_ctx)

        if not bot_ctx["session_ok"]:
            send_screenshot(page, "⚠️ <b>UYARI: Kick Oturumu Açık Görünmüyor!</b> Lütfen KICK_COOKIES_JSON verisini kontrol et.")

        # Eski Telegram mesajlarını atla
        last_update_id = 0
        try:
            init_u = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=5).json()
            if init_u.get("ok") and init_u.get("result"):
                last_update_id = init_u["result"][-1]["update_id"]
        except Exception:
            pass

        last_channel_check = time.time()
        last_drops_check = time.time()

        # ANA 7/24 İZLEME DÖNGÜSÜ
        while True:
            time.sleep(3)
            last_update_id = process_telegram_updates(page, context, bot_ctx, last_update_id)
            sync_extension_state(page, bot_ctx)

            elapsed_min = (time.time() - bot_ctx["start_time"]) / 60.0

            # 1. 330. Dakikada Temiz Bayrak Devri (GitHub 6 Saat Limitine Takılmamak İçin)
            if elapsed_min >= MAX_RUNTIME_MINUTES:
                print("[BAYRAK DEVRİ] 330 dakika doldu. Durum kaydediliyor ve yeni sunucuya devrediliyor...")
                scan_and_claim_gta6_drops(context, bot_ctx)
                tg_api("sendMessage", {
                    "chat_id": CHAT_ID,
                    "text": f"🔄 <b>5.5 Saatlik Vardiya Tamamlandı!</b>\nDurum kaydedildi (<code>{bot_ctx['state'].get('remaining_text')}</code>). Yeni sunucu bayrağı devralacak.",
                    "parse_mode": "HTML"
                })
                break

            # 2. Her 4 Dakikada Bir: Yayıncı Kapattı mı veya GTA VI'dan Çıktı mı Kontrolü
            if time.time() - last_channel_check >= 240:
                last_channel_check = time.time()
                is_live, is_gta6 = verify_channel_is_live_gta6(page, bot_ctx["current_channel"])
                bot_ctx["is_gta6_cat"] = is_gta6

                if not is_live or not is_gta6:
                    reason = "Yayını Kapattı (Offline)" if not is_live else "GTA VI Kategorisinden Çıktı"
                    old_ch = bot_ctx["current_channel"]
                    print(f"[OTOMATİK GEÇİŞ] {old_ch} -> {reason}. Yeni GTA VI kanalı aranıyor...")
                    switch_to_gta6_channel(page, bot_ctx, exclude_current=True)
                    tg_api("sendMessage", {
                        "chat_id": CHAT_ID,
                        "text": f"🔀 <b>Otomatik Kanal Değişimi:</b>\n<code>{old_ch}</code> ({reason}) ➡️ Yeni Kanal: <code>{bot_ctx['current_channel']}</code>",
                        "parse_mode": "HTML"
                    })

            # 3. Her 25 Dakikada Bir: Drops Sayfasından Kalan Süreyi Güncelle & Ödül Kontrolü Yap
            if time.time() - last_drops_check >= 1500:
                last_drops_check = time.time()
                scan_and_claim_gta6_drops(context, bot_ctx)
                if bot_ctx["state"].get("completed"):
                    print("🏆 250 Saatlik Maraton Tamamlandı! Sistem kapatılıyor.")
                    break

        context.close()

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        err_msg = traceback.format_exc()[-800:]
        print(f"[KRİTİK HATA]\n{err_msg}")
        if BOT_TOKEN and CHAT_ID:
            tg_api("sendMessage", {
                "chat_id": CHAT_ID,
                "text": f"❌ <b>Sunucu Hatası (Otomatik Yeniden Başlatılacak):</b>\n<pre>{err_msg}</pre>",
                "parse_mode": "HTML"
            })
        sys.exit(1)
