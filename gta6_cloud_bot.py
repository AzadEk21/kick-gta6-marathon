import os
import re
import sys
import json
import time
import random
import warnings
import requests

warnings.filterwarnings("ignore")

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    from playwright.sync_api import sync_playwright

# ==========================================
# 1. ORTAM DEĞİŞKENLERİ & MATRIX WORKER AYARI
# ==========================================
KICK_COOKIES_RAW = (
    os.environ.get("KICK_COOKIES", "").strip()
    or os.environ.get("KICK_COOKIE", "").strip()
    or os.environ.get("KICK_SESSION_TOKEN", "").strip()
    or os.environ.get("SESSION_TOKEN", "").strip()
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
WORKFLOW_FILE = "gta6_drops.yml"

WORKER_ID = str(os.environ.get("WORKER_ID", "1")).strip()
IS_LEADER = (WORKER_ID == "1")

STATE_FILE = f"gta6_state_w{WORKER_ID}.json"
MAX_RUN_SECONDS = int(5.4 * 3600)

GTA6_CATEGORY_URL = "https://kick.com/category/grand-theft-auto-vi"
GTA6_DROPS_URL = "https://kick.com/category/grand-theft-auto-vi/drops"

PRIORITY_SLUGS = [
    "rraenee", "eray", "mithrain", "hype", "xqc",
    "adinross", "trainwreckstv", "elraenn", "cordiseps", "kendinemuzisyen"
]

# ==========================================
# 2. MİLİSANİYELİK SENKRON (RACE CONDITION) JS ENJEKTÖRÜ
# ==========================================
STEALTH_AND_RACE_JS = r"""
(() => {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    document.hasFocus = () => true;

    window.__GET_VISIBLE_TEXT__ = () => document.body ? document.body.innerText : "";

    window.__CAPTURED_WS__ = null;
    window.__ACTIVE_CHANNEL_ID__ = null;
    window.__ACTIVE_LIVESTREAM_ID__ = null;
    window.__RACE_SYNC_STARTED__ = false;

    const origSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function(data) {
        if (this.url && this.url.includes("websockets.kick.com")) {
            window.__CAPTURED_WS__ = this;
            try {
                const parsed = JSON.parse(data);
                if (parsed?.data?.channel_id) window.__ACTIVE_CHANNEL_ID__ = parsed.data.channel_id;
                if (parsed?.data?.livestream_id) window.__ACTIVE_LIVESTREAM_ID__ = parsed.data.livestream_id;
                if (parsed?.data?.message?.channelId) window.__ACTIVE_CHANNEL_ID__ = parsed.data.message.channelId;
            } catch (e) {}

            if (!window.__RACE_SYNC_STARTED__) {
                window.__RACE_SYNC_STARTED__ = true;
                startMillisecondSyncLoop();
            }
        }
        return origSend.apply(this, arguments);
    };

    function fireSyncWatchPacket() {
        const ws = window.__CAPTURED_WS__;
        if (!ws || ws.readyState !== 1) return;

        const chId = window.__ACTIVE_CHANNEL_ID__;
        const lsId = window.__ACTIVE_LIVESTREAM_ID__;

        try {
            if (chId) {
                origSend.call(ws, JSON.stringify({
                    type: "channel_handshake",
                    data: { message: { channelId: chId } }
                }));
            }
            if (chId && lsId) {
                origSend.call(ws, JSON.stringify({
                    type: "tracking.user.watch.livestream",
                    data: { channel_id: chId, livestream_id: lsId }
                }));
            }
            origSend.call(ws, JSON.stringify({ type: "ping" }));
        } catch (e) {}
    }

    function startMillisecondSyncLoop() {
        const scheduleNextZeroMs = () => {
            const now = Date.now();
            const delay = 20000 - (now % 20000);
            setTimeout(() => {
                fireSyncWatchPacket();
                scheduleNextZeroMs();
            }, delay);
        };
        scheduleNextZeroMs();
    }
})();
"""

# ==========================================
# 3. CLOUDFLARE TURNSTILE OTOMATİK GEÇİCİ
# ==========================================
def bypass_cloudflare(page, max_wait=20):
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

            print(f"🛡️ [Worker #{WORKER_ID}] Cloudflare tespit edildi (Deneme {attempt + 1}/4), kutucuk aranıyor...")
            page.wait_for_timeout(3000)

            # Turnstile iframe veya widget konumunu bul
            box = None
            for frame in page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    el = frame.frame_element()
                    box = el.bounding_box()
                    if box:
                        break

            if box:
                # Kutucuk genelde iframe'in sol tarafındadır (x + 28, y + height/2)
                tx = box["x"] + 28 + random.uniform(-3, 3)
                ty = box["y"] + (box["height"] / 2) + random.uniform(-3, 3)
                page.mouse.move(tx - 60, ty + 20, steps=10)
                page.wait_for_timeout(random.randint(150, 350))
                page.mouse.move(tx, ty, steps=12)
                page.wait_for_timeout(random.randint(100, 250))
                page.mouse.click(tx, ty)
                print(f"🖱️ [Worker #{WORKER_ID}] Turnstile kutusuna tıklandı ({int(tx)}, {int(ty)}).")
            else:
                # Fallback: ekran görüntüsündeki standart Turnstile konumu
                page.mouse.move(215, 295, steps=12)
                page.mouse.click(215, 295)

            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"[CF BYPASS UYARI] {e}")
            page.wait_for_timeout(2000)
    return False

# ==========================================
# 4. DURUM (STATE) & TELEGRAM YARDIMCILARI
# ==========================================
def load_state():
    default_state = {
        "remaining_text": "Henüz taranmadı",
        "percent": 0.0,
        "completed": False,
        "current_streamer": "Yok",
        "last_update_offset": 0
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
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_control_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Anlık Durum", "callback_data": "/durum"},
                {"text": "📸 Ekran Görüntüsü", "callback_data": "/ekran"}
            ],
            [
                {"text": "🎁 Drops Süresini Tara", "callback_data": "/drops"},
                {"text": "🔄 Yayıncı Değiştir", "callback_data": "/degistir"}
            ]
        ]
    }

def tg_send(text, reply_markup=None, force=False):
    if not IS_LEADER and not force:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
        print(f"[TG HATA] {e}")

def send_screenshot(page, caption=""):
    if not IS_LEADER:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    path = f"screen_w{WORKER_ID}.png"
    try:
        page.screenshot(path=path, full_page=False)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(path, "rb") as photo:
            requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption[:1000],
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(get_control_keyboard())
                },
                files={"photo": photo},
                timeout=25
            )
    except Exception as e:
        print(f"[SCREENSHOT HATA] {e}")

# ==========================================
# 5. ÇEREZ (COOKIE) YÜKLEYİCİ
# ==========================================
def parse_and_load_cookies(context):
    if not KICK_COOKIES_RAW:
        print("❌ KICK_COOKIES_JSON secret bulunamadı!")
        return False

    cookies_to_add = []
    try:
        raw_list = json.loads(KICK_COOKIES_RAW)
        if isinstance(raw_list, dict) and "cookies" in raw_list:
            raw_list = raw_list["cookies"]
        if isinstance(raw_list, list):
            for c in raw_list:
                name = c.get("name")
                value = c.get("value")
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

    if not cookies_to_add and len(KICK_COOKIES_RAW) > 10:
        cookies_to_add.append({
            "name": "session_token",
            "value": KICK_COOKIES_RAW,
            "domain": ".kick.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax"
        })

    if cookies_to_add:
        context.add_cookies(cookies_to_add)
        print(f"✅ [Worker #{WORKER_ID}] {len(cookies_to_add)} adet Kick çerezi yüklendi.")
        return True
    return False

# ==========================================
# 6. AKTİF GTA VI YAYINCISI BULUCU
# ==========================================
def find_live_gta6_streamers(page):
    live_list = []
    try:
        page.goto(GTA6_CATEGORY_URL, wait_until="domcontentloaded", timeout=35000)
        bypass_cloudflare(page)
        page.wait_for_timeout(3000)
        dom_slugs = page.evaluate(r"""() => {
            const ignore = new Set(['categories', 'category', 'drops', 'browse', 'following', 'search', 'dashboard', 'settings', 'terms', 'privacy']);
            const found = [];
            document.querySelectorAll('a[href^="/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/^\/([a-zA-Z0-9_]{3,25})$/);
                if (m && !ignore.has(m[1].toLowerCase())) {
                    const s = m[1].toLowerCase();
                    if (!found.includes(s)) found.push(s);
                }
            });
            return found.slice(0, 15);
        }""")
        for s in dom_slugs:
            if s not in live_list:
                live_list.append(s)
    except Exception as e:
        print(f"[KATEGORİ TARAMA UYARISI] {e}")

    if len(live_list) < 5:
        for slug in PRIORITY_SLUGS:
            if slug in live_list:
                continue
            try:
                res = page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('https://kick.com/api/v2/channels/{slug}');
                        if (!r.ok) return null;
                        const d = await r.json();
                        return (d && d.livestream && d.livestream.is_live) ? true : null;
                    }} catch(e) {{ return null; }}
                }}""")
                if res:
                    live_list.append(slug)
            except Exception:
                pass

    if not live_list:
        live_list = PRIORITY_SLUGS[:5]

    return live_list

def pick_streamer_for_worker(live_list, offset=0):
    w_idx = max(0, int(WORKER_ID) - 1)
    target_idx = (w_idx + offset) % len(live_list)
    return live_list[target_idx]

# ==========================================
# 7. GTA VI DROPS SAYFASI TARAYICI
# ==========================================
def scan_and_claim_gta6_drops(context, bot_ctx):
    state = bot_ctx["state"]
    drop_page = None
    result_summary = "Drops sayfası taranamadı."
    try:
        drop_page = context.new_page()
        drop_page.goto(GTA6_DROPS_URL, wait_until="domcontentloaded", timeout=35000)
        bypass_cloudflare(drop_page)
        drop_page.wait_for_timeout(4000)

        vis_text = drop_page.evaluate("window.__GET_VISIBLE_TEXT__ ? window.__GET_VISIBLE_TEXT__() : document.body.innerText")

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

        matches_tr = re.findall(r"(\d+)\s*sa\.?\s*(?:(\d+)\s*dk\.?)?", vis_text, re.IGNORECASE)
        matches_en = re.findall(r"(\d+)\s*h(?:r|ours?)?\s*(?:(\d+)\s*m(?:in)?)?", vis_text, re.IGNORECASE)
        all_matches = matches_tr if matches_tr else matches_en

        best_rem_min = None
        rem_hours, rem_mins = None, 0

        for h_str, m_str in all_matches:
            h = int(h_str)
            m = int(m_str) if m_str else 0
            if h <= 250 and not (h == 250 and m == 0 and len(all_matches) > 1):
                tot = h * 60 + m
                if best_rem_min is None or tot > best_rem_min:
                    best_rem_min = tot
                    rem_hours, rem_mins = h, m

        if rem_hours is not None:
            watched_min = max(0, 15000 - best_rem_min)
            pct = round((watched_min / 15000.0) * 100, 2)
            state["remaining_text"] = f"{rem_hours} sa. {rem_mins} dk. (İzlenen: {watched_min // 60} sa {watched_min % 60} dk)"
            state["percent"] = pct
            save_state(state)
            result_summary = f"Kalan: {state['remaining_text']} (%{pct})"

        drop_page.close()
    except Exception as e:
        print(f"[DROPS TARAMA HATASI] {e}")
        if drop_page:
            try:
                drop_page.close()
            except Exception:
                pass
    return result_summary

# ==========================================
# 8. YAYIN OYNATICI & SENKRON KANAL BESLEYİCİ
# ==========================================
def open_stream_and_arm_sync(page, slug, state):
    url = f"https://kick.com/{slug}"
    print(f"📺 [Worker #{WORKER_ID}] Yayın açılıyor: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=35000)
    bypass_cloudflare(page)
    page.wait_for_timeout(4000)

    page.evaluate(r"""() => {
        document.querySelectorAll('button').forEach(b => {
            const t = (b.innerText || '').toLowerCase();
            if (t.includes('start watching') || t.includes('izlemeye başla') || t.includes('18+') || t.includes('kabul')) {
                b.click();
            }
        });
        const v = document.querySelector('video');
        if (v) {
            v.muted = true;
            v.play().catch(() => {});
        }
    }""")

    ch_info = page.evaluate(f"""async () => {{
        try {{
            const r = await fetch('https://kick.com/api/v2/channels/{slug}');
            if (!r.ok) return null;
            const d = await r.json();
            if (d && d.livestream && d.livestream.is_live) {{
                window.__ACTIVE_CHANNEL_ID__ = d.id;
                window.__ACTIVE_LIVESTREAM_ID__ = d.livestream.id;
                return {{ channel_id: d.id, livestream_id: d.livestream.id, viewers: d.livestream.viewer_count }};
            }}
            return null;
        }} catch(e) {{ return null; }}
    }}""")

    state["current_streamer"] = slug
    save_state(state)
    return ch_info

# ==========================================
# 9. TELEGRAM KOMUT DİNLEYİCİSİ (SADECE WORKER #1)
# ==========================================
def poll_telegram_commands(page, context, bot_ctx):
    if not IS_LEADER or not TELEGRAM_BOT_TOKEN:
        return
    state = bot_ctx["state"]
    offset = state.get("last_update_offset", 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset + 1}&timeout=1"
    try:
        res = requests.get(url, timeout=5)
        if not res.ok:
            return
        updates = res.json().get("result", [])
        for upd in updates:
            offset = upd["update_id"]
            state["last_update_offset"] = offset
            save_state(state)

            cmd = ""
            if "message" in upd and "text" in upd["message"]:
                cmd = upd["message"]["text"].strip().lower()
            elif "callback_query" in upd:
                cmd = upd["callback_query"].get("data", "").strip().lower()
                cb_id = upd["callback_query"]["id"]
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", data={"callback_query_id": cb_id}, timeout=5)

            if not cmd:
                continue

            if cmd.startswith("/durum"):
                elapsed_min = int((time.time() - bot_ctx["start_time"]) // 60)
                msg = (
                    f"⚡ <b>5'Lİ MATRIX SENKRON BULUT ORDUSU</b>\n\n"
                    f"👑 <b>Lider Sunucu (W#1):</b> <code>{state['current_streamer']}</code>\n"
                    f"🛰️ <b>Aktif Paralel Sunucu:</b> <code>5 Ayrı IP (00.000 ms Senkron)</code>\n"
                    f"🎁 <b>SunsetK Badge:</b> <code>{state['remaining_text']}</code>\n"
                    f"📈 <b>Tamamlanan:</b> <code>%{state['percent']}</code>\n"
                    f"⏱️ <b>Bu Oturum Süresi:</b> <code>{elapsed_min} dk</code>"
                )
                tg_send(msg, reply_markup=get_control_keyboard())

            elif cmd.startswith("/ekran"):
                send_screenshot(page, f"📸 <b>Worker #1 Canlı Yayın Ekranı:</b> <code>{state['current_streamer']}</code>")

            elif cmd.startswith("/drops"):
                tg_send("🔍 <i>GTA VI SunsetK Badge ilerlemesi taranıyor...</i>")
                summary = scan_and_claim_gta6_drops(context, bot_ctx)
                tg_send(f"🎁 <b>Güncel Drops Durumu:</b>\n<code>{summary}</code>", reply_markup=get_control_keyboard())

            elif cmd.startswith("/degistir"):
                bot_ctx["stream_offset"] += 1
                live_list = find_live_gta6_streamers(page)
                new_slug = pick_streamer_for_worker(live_list, bot_ctx["stream_offset"])
                open_stream_and_arm_sync(page, new_slug, state)
                tg_send(f"🔄 <b>Yayıncı Değiştirildi!</b> Yeni kanal: <code>{new_slug}</code>", reply_markup=get_control_keyboard())
    except Exception:
        pass

# ==========================================
# 10. OTOMATİK BAYRAK DEVRİ (SADECE WORKER #1)
# ==========================================
def trigger_next_workflow():
    if not IS_LEADER or not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    try:
        r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
        if r.status_code in (200, 204):
            tg_send("🔄 <b>5.5 Saatlik Oturum Tamamlandı!</b> Bayrak yeni 5'li bulut sunucu ordusuna devredildi.")
    except Exception as e:
        print(f"[WORKFLOW TETİKLEME HATASI] {e}")

# ==========================================
# 11. ANA ÇALIŞMA DÖNGÜSÜ
# ==========================================
def run_bot(context):
    context.add_init_script(STEALTH_AND_RACE_JS)

    if not parse_and_load_cookies(context):
        tg_send("❌ <b>HATA:</b> Kick çerezleri (KICK_COOKIES_JSON) okunamadı!")
        return

    state = load_state()
    bot_ctx = {
        "state": state,
        "start_time": time.time(),
        "stream_offset": 0
    }

    page = context.pages[0] if context.pages else context.new_page()

    live_list = find_live_gta6_streamers(page)
    my_slug = pick_streamer_for_worker(live_list, 0)
    open_stream_and_arm_sync(page, my_slug, state)

    if IS_LEADER:
        scan_and_claim_gta6_drops(context, bot_ctx)
        start_msg = (
            f"🚀 <b>5'Lİ MATRIX SENKRON ORDUSU BAŞLATILDI!</b>\n\n"
            f"👑 <b>Worker #1 Kanalı:</b> <code>{my_slug}</code>\n"
            f"🛰️ <b>Dağıtılan Havuz:</b> <code>{', '.join(live_list[:5])}</code>\n"
            f"⚡ <b>Race Condition Modu:</b> <code>Her 20.000 ms sınırında 5 IP'den senkron vuruş</code>\n"
            f"🎁 <b>SunsetK Badge:</b> <code>{state['remaining_text']}</code>"
        )
        send_screenshot(page, start_msg)

    last_health_check = time.time()
    last_drops_scan = time.time()

    while (time.time() - bot_ctx["start_time"]) < MAX_RUN_SECONDS:
        if state.get("completed"):
            tg_send("🏆 <b>250 Saatlik GTA VI Ödülü Tamamlandı!</b> Bot durduruluyor.")
            break

        if IS_LEADER:
            poll_telegram_commands(page, context, bot_ctx)

        if (time.time() - last_health_check) > 180:
            last_health_check = time.time()
            try:
                is_still_live = page.evaluate(f"""async () => {{
                    const v = document.querySelector('video');
                    if (v && v.paused) v.play().catch(() => {{}});
                    try {{
                        const r = await fetch('https://kick.com/api/v2/channels/{state["current_streamer"]}');
                        const d = await r.json();
                        if (d && d.livestream && d.livestream.is_live) {{
                            window.__ACTIVE_CHANNEL_ID__ = d.id;
                            window.__ACTIVE_LIVESTREAM_ID__ = d.livestream.id;
                            return true;
                        }}
                        return false;
                    }} catch(e) {{ return true; }}
                }}""")

                if not is_still_live:
                    print(f"⚠️ [Worker #{WORKER_ID}] {state['current_streamer']} yayını kapattı, yeni yayıncıya geçiliyor...")
                    bot_ctx["stream_offset"] += 1
                    live_list = find_live_gta6_streamers(page)
                    new_slug = pick_streamer_for_worker(live_list, bot_ctx["stream_offset"])
                    open_stream_and_arm_sync(page, new_slug, state)
            except Exception as e:
                print(f"[SAĞLIK KONTROLÜ UYARISI] {e}")

        if IS_LEADER and (time.time() - last_drops_scan) > 1200:
            last_drops_scan = time.time()
            scan_and_claim_gta6_drops(context, bot_ctx)

        time.sleep(4)

    if IS_LEADER and not state.get("completed"):
        trigger_next_workflow()

def main():
    print(f"🔥 [GTA 6 CLOUD BOT - WORKER #{WORKER_ID}] Patchright Chrome ile başlatılıyor (Lider: {IS_LEADER})...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=f"/tmp/chrome_profile_w{WORKER_ID}",
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
