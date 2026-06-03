import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import time
import json
import os
import hmac
import hashlib
import base64
import requests
import pyautogui
import pyperclip
from datetime import datetime, timedelta

# ──────────────────────────────────────────────
# 설정 로드
# ──────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
SENT_ORDERS_FILE = os.path.join(os.path.dirname(__file__), "sent_orders.json")

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_sent_orders():
    if os.path.exists(SENT_ORDERS_FILE):
        with open(SENT_ORDERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_sent_orders(sent: set):
    with open(SENT_ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent), f, ensure_ascii=False)

# ──────────────────────────────────────────────
# 네이버 커머스 API
# ──────────────────────────────────────────────
NAVER_API_BASE = "https://api.commerce.naver.com/external"

def _make_signature(client_id: str, client_secret: str, timestamp: int) -> str:
    message = f"{client_id}_{timestamp}"
    secret_bytes = client_secret.encode("utf-8")
    message_bytes = message.encode("utf-8")
    sig = hmac.new(secret_bytes, message_bytes, hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")

def get_naver_token(client_id: str, client_secret: str) -> str:
    timestamp = int(time.time() * 1000)
    signature = _make_signature(client_id, client_secret, timestamp)
    payload = {
        "client_id": client_id,
        "timestamp": timestamp,
        "client_secret_sign": signature,
        "grant_type": "client_credentials",
        "type": "SELF",
    }
    resp = requests.post(
        f"{NAVER_API_BASE}/v1/oauth2/token",
        data=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def fetch_new_orders(token: str) -> list[dict]:
    """최근 20분 이내 신규 주문(결제완료) 목록 반환."""
    now = datetime.utcnow()
    from_dt = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_dt = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "orderStatusType": "PAYED",
        "paymentDateFrom": from_dt,
        "paymentDateTo": to_dt,
        "pageNum": 1,
        "pageSize": 100,
    }
    resp = requests.get(
        f"{NAVER_API_BASE}/v1/pay-order/seller/orders",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("contents", [])

def format_order_message(order: dict) -> str:
    order_id   = order.get("orderId", "N/A")
    order_date = order.get("paymentDate", "")[:19].replace("T", " ")
    buyer      = order.get("ordererName", "N/A")
    total      = order.get("generalPaymentAmount", 0)
    products   = order.get("productOrderList", [])

    lines = [
        "📦 새 주문 알림",
        f"주문번호: {order_id}",
        f"주문일시: {order_date}",
        f"주문자: {buyer}",
        f"결제금액: {total:,}원",
        "상품:",
    ]
    for p in products:
        name = p.get("productName", "N/A")
        qty  = p.get("quantity", 1)
        lines.append(f"  - {name} x{qty}")
    return "\n".join(lines)

# ──────────────────────────────────────────────
# 카카오톡 PyAutoGUI 전송
# ──────────────────────────────────────────────
def find_and_send_kakao(room_name: str, message: str) -> bool:
    """
    PC 카카오톡에서 room_name 오픈채팅방을 찾아 메시지를 전송한다.
    성공하면 True, 실패하면 False 반환.
    """
    import pyautogui, pyperclip, time

    # 카카오톡 창 활성화 (Windows: ahk 없이 pyautogui 이미지 매칭 또는 창 제목 탐색)
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle("카카오톡")
        if not windows:
            return False
        win = windows[0]
        win.activate()
        time.sleep(0.5)
    except Exception:
        pass  # pygetwindow 없으면 포커스 없이 시도

    # Ctrl+F 로 채팅방 검색
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)
    pyperclip.copy(room_name)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.6)
    pyautogui.press("enter")
    time.sleep(0.8)

    # 메시지 입력창에 포커스 후 전송 (Enter)
    pyperclip.copy(message)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.3)
    return True

# ──────────────────────────────────────────────
# 백그라운드 워커
# ──────────────────────────────────────────────
class OrderWatcher:
    def __init__(self, log_fn):
        self._running = False
        self._thread = None
        self.log = log_fn

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        cfg = load_config()
        sent = load_sent_orders()
        interval = cfg.get("check_interval_minutes", 10) * 60

        self.log("▶ 모니터링 시작")
        while self._running:
            try:
                self.log("🔍 신규 주문 확인 중...")
                token = get_naver_token(cfg["naver_client_id"], cfg["naver_client_secret"])
                orders = fetch_new_orders(token)
                new_count = 0
                for order in orders:
                    oid = order.get("orderId")
                    if oid and oid not in sent:
                        msg = format_order_message(order)
                        ok = find_and_send_kakao(cfg["kakao_room_name"], msg)
                        if ok:
                            sent.add(oid)
                            save_sent_orders(sent)
                            self.log(f"✅ 전송 완료: {oid}")
                            new_count += 1
                        else:
                            self.log(f"⚠️ 카카오톡 전송 실패 (창을 찾을 수 없음): {oid}")
                if new_count == 0:
                    self.log("ℹ️ 신규 주문 없음")
            except Exception as e:
                self.log(f"❌ 오류 발생: {e}")

            # 인터벌 대기 (중지 신호 감지)
            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

        self.log("⏹ 모니터링 중지")

# ──────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────
BG       = "#1e1e2e"
SURFACE  = "#2a2a3d"
ACCENT   = "#7c3aed"
ACCENT_H = "#6d28d9"
FG       = "#e2e8f0"
FG_DIM   = "#94a3b8"
SUCCESS  = "#22c55e"
ERROR    = "#ef4444"
WARN     = "#f59e0b"
FONT_M   = ("맑은 고딕", 10)
FONT_B   = ("맑은 고딕", 10, "bold")
FONT_T   = ("맑은 고딕", 13, "bold")

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("네이버 주문 → 카카오톡 알리미")
        self.geometry("680x520")
        self.resizable(False, False)
        self.configure(bg=BG)

        self._watcher = OrderWatcher(log_fn=self._append_log)
        self._build_ui()

    # ── UI 구성 ──────────────────────────────
    def _build_ui(self):
        # 제목
        tk.Label(self, text="네이버 스마트스토어 → 카카오톡 알리미",
                 bg=BG, fg=FG, font=FONT_T).pack(pady=(16, 4))
        tk.Label(self, text="신규 주문을 감지하여 PC 카카오톡 오픈채팅방에 자동 전송합니다",
                 bg=BG, fg=FG_DIM, font=FONT_M).pack(pady=(0, 12))

        # 설정 카드
        card = tk.Frame(self, bg=SURFACE, bd=0)
        card.pack(fill="x", padx=24, pady=4)

        self._cfg_vars = {}
        fields = [
            ("naver_client_id",      "Naver Client ID",    False),
            ("naver_client_secret",  "Naver Client Secret", True),
            ("kakao_room_name",      "카카오톡 오픈채팅방 이름", False),
            ("check_interval_minutes", "체크 주기 (분)",    False),
        ]
        for i, (key, label, secret) in enumerate(fields):
            tk.Label(card, text=label, bg=SURFACE, fg=FG_DIM,
                     font=FONT_M, width=22, anchor="w").grid(
                row=i, column=0, padx=(12, 4), pady=5, sticky="w")
            show = "*" if secret else ""
            var = tk.StringVar()
            self._cfg_vars[key] = var
            entry = tk.Entry(card, textvariable=var, show=show,
                             bg=BG, fg=FG, insertbackground=FG,
                             relief="flat", font=FONT_M, width=36)
            entry.grid(row=i, column=1, padx=(4, 12), pady=5)

        self._load_cfg_to_ui()

        # 버튼 행
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=10)

        self._btn_start = tk.Button(
            btn_frame, text="▶  시작", font=FONT_B,
            bg=ACCENT, fg="white", activebackground=ACCENT_H,
            activeforeground="white", relief="flat",
            padx=20, pady=6, cursor="hand2",
            command=self._on_start)
        self._btn_start.pack(side="left", padx=6)

        self._btn_stop = tk.Button(
            btn_frame, text="⏹  중지", font=FONT_B,
            bg="#475569", fg="white", activebackground="#334155",
            activeforeground="white", relief="flat",
            padx=20, pady=6, cursor="hand2",
            state="disabled", command=self._on_stop)
        self._btn_stop.pack(side="left", padx=6)

        tk.Button(
            btn_frame, text="💾  설정 저장", font=FONT_B,
            bg="#0f766e", fg="white", activebackground="#0d6060",
            activeforeground="white", relief="flat",
            padx=16, pady=6, cursor="hand2",
            command=self._save_cfg).pack(side="left", padx=6)

        # 로그창
        tk.Label(self, text="로그", bg=BG, fg=FG_DIM, font=FONT_M,
                 anchor="w").pack(fill="x", padx=28)
        self._log_box = scrolledtext.ScrolledText(
            self, state="disabled", bg="#0f0f1a", fg=FG,
            font=("Consolas", 9), relief="flat",
            wrap="word", height=10)
        self._log_box.pack(fill="both", expand=True, padx=24, pady=(2, 16))
        self._log_box.tag_config("ok",   foreground=SUCCESS)
        self._log_box.tag_config("err",  foreground=ERROR)
        self._log_box.tag_config("warn", foreground=WARN)
        self._log_box.tag_config("info", foreground=FG_DIM)

    # ── 설정 ─────────────────────────────────
    def _load_cfg_to_ui(self):
        try:
            cfg = load_config()
            for k, var in self._cfg_vars.items():
                var.set(str(cfg.get(k, "")))
        except Exception:
            pass

    def _save_cfg(self):
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        for k, var in self._cfg_vars.items():
            val = var.get().strip()
            cfg[k] = int(val) if k == "check_interval_minutes" and val.isdigit() else val
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self._append_log("💾 설정이 저장되었습니다")

    # ── 버튼 핸들러 ──────────────────────────
    def _on_start(self):
        self._save_cfg()
        cfg = load_config()
        if cfg.get("naver_client_id", "").startswith("여기에"):
            messagebox.showwarning("설정 필요", "Naver Client ID / Secret을 먼저 입력해 주세요.")
            return
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._watcher.start()

    def _on_stop(self):
        self._watcher.stop()
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")

    # ── 로그 ─────────────────────────────────
    def _append_log(self, msg: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            tag = "ok" if "✅" in msg else \
                  "err" if "❌" in msg else \
                  "warn" if "⚠️" in msg else "info"
            self._log_box.config(state="normal")
            self._log_box.insert("end", f"[{ts}] {msg}\n", tag)
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self.after(0, _do)

# ──────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
