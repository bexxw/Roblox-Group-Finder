import threading
import time
import random
import queue
import requests
from flask import Flask, request, jsonify, Response, send_from_directory, stream_with_context

# =========================
# Config / Globals
# =========================
app = Flask(__name__)

RUNNING = False
WORKERS = []
STOP_EVENT = threading.Event()
LOG_QUEUE = queue.Queue(maxsize=2000)

# Çalışma parametreleri (run-time set ediliyor)
CURRENT_WEBHOOK = None
CURRENT_THREADS = 0

# Basit bir logger: hem print eder hem SSE kuyruğuna iletir
def log(msg):
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        LOG_QUEUE.put_nowait(line)
    except queue.Full:
        # Kuyruk dolarsa en eskileri at
        _ = LOG_QUEUE.get_nowait()
        LOG_QUEUE.put_nowait(line)

# =========================
# Çekirdek iş: Roblox Group Finder
# =========================
def send_discord_message(webhook_url, content):
    try:
        r = requests.post(
            webhook_url,
            json={"content": content},
            timeout=15
        )
        # Discord başarılı ise 204 döner
        if r.status_code == 204:
            return True
        else:
            log(f"[-] Webhook Hatası: HTTP {r.status_code}")
            return False
    except Exception as e:
        log(f"[-] Webhook İstisnası: {e}")
        return False

def grup_bulucu_worker(webhook_url):
    # Orijinal mantığı temel alarak güvenli ve kararlı hâle getirdik
    session = requests.Session()
    while not STOP_EVENT.is_set():
        try:
            gid = random.randint(1000000, 1150000)

            sayfa = session.get(
                f"https://www.roblox.com/groups/group.aspx?gid={gid}",
                timeout=30
            )
            if 'owned' not in sayfa.text:
                veri = session.get(
                    f"https://groups.roblox.com/v1/groups/{gid}",
                    timeout=30
                )

                if veri.status_code == 429:
                    log("[-] API Limit Aşıldı (429), kısa uyku...")
                    time.sleep(1.5)
                    continue

                data = {}
                try:
                    data = veri.json()
                except Exception:
                    log(f"[-] JSON Çözümlenemedi: {gid}")
                    continue

                txt = veri.text
                # Orijinal kontroller korunuyor
                if ('errors' not in data) and ('isLocked' not in txt and 'owner' in txt):
                    if data.get('publicEntryAllowed') is True and data.get('owner') is None:
                        url = f"https://www.roblox.com/groups/group.aspx?gid={gid}"
                        ok = send_discord_message(webhook_url, f"Bulundu: {url}")
                        if ok:
                            log(f"[+] Bulundu ve Webhook'a Gönderildi: {gid}")
                        else:
                            log(f"[?] Bulundu ama Webhook Gönderimi Başarısız: {gid}")
                    else:
                        log(f"[-] Giriş İzinli Değil veya Sahip Var/Kilitli: {gid}")
                else:
                    log(f"[-] Grup Kilitli veya Hatalı: {gid}")
            else:
                log(f"[-] Grup Zaten Sahiplenilmiş: {gid}")

            # Aşırı yüklenmeyi önlemek için minik uyku
            time.sleep(0.05)

        except requests.RequestException as e:
            # Ağ hatası: kısa bekleyip devam
            log(f"[-] İstek Hatası: {e}")
            time.sleep(0.5)
        except Exception as e:
            # Beklenmeyen durumlar yutulmasın, loglansın
            log(f"[-] İstisna: {e}")

# =========================
# SSE (Server-Sent Events) ile canlı log akışı
# =========================
@app.route("/logs/stream")
def logs_stream():
    @stream_with_context
    def event_stream():
        # Başlangıçta küçük bir hoş geldin
        yield f"data: { 'Log akışı başladı.' }\n\n"
        last_heartbeat = time.time()
        while True:
            # Heartbeat (bazı proxy’ler için bağlantıyı canlı tutar)
            now = time.time()
            if now - last_heartbeat > 15:
                yield "event: ping\ndata: {}\n\n"
                last_heartbeat = now

            try:
                line = LOG_QUEUE.get(timeout=1.0)
                yield f"data: {line}\n\n"
            except queue.Empty:
                # yeni log yoksa döngü devam
                continue

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(event_stream(), headers=headers)

# =========================
# Kontrol uçları
# =========================
@app.route("/start", methods=["POST"])
def start():
    global RUNNING, WORKERS, STOP_EVENT, CURRENT_WEBHOOK, CURRENT_THREADS

    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    threads = int(data.get("threads", 0))
    webhook = data.get("webhook", "")

    if key != "ravenxx11":
        return jsonify({"ok": False, "error": "Geçersiz key."}), 401
    if not webhook or not webhook.startswith("https://"):
        return jsonify({"ok": False, "error": "Geçerli bir Discord webhook URL giriniz."}), 400
    if threads < 1 or threads > 200:
        return jsonify({"ok": False, "error": "Threads 1-200 aralığında olmalı."}), 400

    # Zaten çalışıyorsa önce durdur
    if RUNNING:
        _stop_workers(join=True)

    CURRENT_WEBHOOK = webhook
    CURRENT_THREADS = threads
    STOP_EVENT.clear()
    WORKERS = []

    for _ in range(threads):
        t = threading.Thread(target=grup_bulucu_worker, args=(CURRENT_WEBHOOK,), daemon=True)
        WORKERS.append(t)
        t.start()

    RUNNING = True
    log(f"[✓] Başlatıldı. Threads: {threads}")
    return jsonify({"ok": True, "status": "running", "threads": threads})

@app.route("/stop", methods=["POST"])
def stop():
    global RUNNING
    if not RUNNING:
        return jsonify({"ok": True, "status": "already_stopped"})
    _stop_workers(join=True)
    log("[✓] Durduruldu.")
    return jsonify({"ok": True, "status": "stopped"})

def _stop_workers(join=False):
    global RUNNING, STOP_EVENT, WORKERS
    STOP_EVENT.set()
    if join:
        for t in WORKERS:
            try:
                t.join(timeout=3)
            except Exception:
                pass
    WORKERS = []
    RUNNING = False
    STOP_EVENT.clear()

@app.route("/status")
def status():
    return jsonify({
        "running": RUNNING,
        "threads": CURRENT_THREADS if RUNNING else 0
    })

# (Opsiyonel) Aynı klasördeki index.html'yi servis etmek istersen:
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    # Flask’ı çalıştır
    # Prod’da bir reverse proxy arkasında (gunicorn/uvicorn) çalıştırmanız önerilir.
    app.run(host="0.0.0.0", port=5000, threaded=True)