import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
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

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

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
    import pyautogui, pyperclip, time
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle("카카오톡")
        if not windows:
            return False
        win = windows[0]
        win.activate()
        time.sleep(0.5)
    except Exception:
        pass
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)
    pyperclip.copy(room_name)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.6)
    pyautogui.press("enter")
    time.sleep(0.8)
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
            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)
        self.log("⏹ 모니터링 중지")

# ──────────────────────────────────────────────
# 색상 테마
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
FONT_S   = ("맑은 고딕", 9)


# ──────────────────────────────────────────────
# 탭1: 주문 알리미
# ──────────────────────────────────────────────
class OrderTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._watcher = OrderWatcher(log_fn=self._append_log)
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="네이버 스마트스토어 → 카카오톡 알리미",
                 bg=BG, fg=FG, font=FONT_T).pack(pady=(16, 4))
        tk.Label(self, text="신규 주문을 감지하여 PC 카카오톡 오픈채팅방에 자동 전송합니다",
                 bg=BG, fg=FG_DIM, font=FONT_M).pack(pady=(0, 12))

        card = tk.Frame(self, bg=SURFACE)
        card.pack(fill="x", padx=24, pady=4)

        self._cfg_vars = {}
        fields = [
            ("naver_client_id",        "Naver Client ID",     False),
            ("naver_client_secret",    "Naver Client Secret", True),
            ("kakao_room_name",        "카카오톡 오픈채팅방 이름", False),
            ("check_interval_minutes", "체크 주기 (분)",       False),
        ]
        for i, (key, label, secret) in enumerate(fields):
            tk.Label(card, text=label, bg=SURFACE, fg=FG_DIM,
                     font=FONT_M, width=22, anchor="w").grid(
                row=i, column=0, padx=(12, 4), pady=5, sticky="w")
            show = "*" if secret else ""
            var = tk.StringVar()
            self._cfg_vars[key] = var
            tk.Entry(card, textvariable=var, show=show,
                     bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", font=FONT_M, width=36).grid(
                row=i, column=1, padx=(4, 12), pady=5)

        self._load_cfg_to_ui()

        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=10)

        self._btn_start = tk.Button(
            btn_frame, text="▶  시작", font=FONT_B,
            bg=ACCENT, fg="white", activebackground=ACCENT_H,
            activeforeground="white", relief="flat",
            padx=20, pady=6, cursor="hand2", command=self._on_start)
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

        tk.Label(self, text="로그", bg=BG, fg=FG_DIM,
                 font=FONT_M, anchor="w").pack(fill="x", padx=28)
        self._log_box = scrolledtext.ScrolledText(
            self, state="disabled", bg="#0f0f1a", fg=FG,
            font=("Consolas", 9), relief="flat", wrap="word", height=10)
        self._log_box.pack(fill="both", expand=True, padx=24, pady=(2, 16))
        self._log_box.tag_config("ok",   foreground=SUCCESS)
        self._log_box.tag_config("err",  foreground=ERROR)
        self._log_box.tag_config("warn", foreground=WARN)
        self._log_box.tag_config("info", foreground=FG_DIM)

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
        save_config(cfg)
        self._append_log("💾 설정이 저장되었습니다")

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

    def _append_log(self, msg: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            tag = ("ok"   if "✅" in msg else
                   "err"  if "❌" in msg else
                   "warn" if "⚠️" in msg else "info")
            self._log_box.config(state="normal")
            self._log_box.insert("end", f"[{ts}] {msg}\n", tag)
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self.after(0, _do)


# ──────────────────────────────────────────────
# 탭2: 블로그 업로더
# ──────────────────────────────────────────────
class BlogUploaderTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._extracted: dict | None = None
        self._html_content: str = ""
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="블로그 자동 업로더",
                 bg=BG, fg=FG, font=FONT_T).pack(pady=(16, 2))
        tk.Label(self,
                 text="URL을 입력하면 콘텐츠를 추출해 블로그 포스트로 자동 업로드합니다 (Lilys AI 링크 지원)",
                 bg=BG, fg=FG_DIM, font=FONT_S).pack(pady=(0, 10))

        # ── URL 입력 ──────────────────────────
        url_frame = tk.Frame(self, bg=BG)
        url_frame.pack(fill="x", padx=24, pady=2)
        tk.Label(url_frame, text="URL", bg=BG, fg=FG_DIM,
                 font=FONT_M, width=8, anchor="w").pack(side="left")
        self._url_var = tk.StringVar()
        tk.Entry(url_frame, textvariable=self._url_var,
                 bg=SURFACE, fg=FG, insertbackground=FG,
                 relief="flat", font=FONT_M).pack(side="left", fill="x", expand=True, padx=(4, 8))
        tk.Button(url_frame, text="콘텐츠 추출", font=FONT_B,
                  bg=ACCENT, fg="white", activebackground=ACCENT_H,
                  activeforeground="white", relief="flat",
                  padx=12, pady=4, cursor="hand2",
                  command=self._on_extract).pack(side="left")

        # ── 플랫폼 선택 ───────────────────────
        plat_frame = tk.LabelFrame(self, text="업로드 플랫폼", bg=BG, fg=FG_DIM,
                                   font=FONT_M, bd=1)
        plat_frame.pack(fill="x", padx=24, pady=8)

        self._use_naver   = tk.BooleanVar(value=True)
        self._use_blogger = tk.BooleanVar(value=True)
        self._use_wp      = tk.BooleanVar(value=True)

        tk.Checkbutton(plat_frame, text="네이버 블로그", variable=self._use_naver,
                       bg=BG, fg=FG, selectcolor=SURFACE, font=FONT_M,
                       activebackground=BG, activeforeground=FG).pack(side="left", padx=16, pady=4)
        tk.Checkbutton(plat_frame, text="블로그스팟 (Blogger)", variable=self._use_blogger,
                       bg=BG, fg=FG, selectcolor=SURFACE, font=FONT_M,
                       activebackground=BG, activeforeground=FG).pack(side="left", padx=16)
        tk.Checkbutton(plat_frame, text="워드프레스", variable=self._use_wp,
                       bg=BG, fg=FG, selectcolor=SURFACE, font=FONT_M,
                       activebackground=BG, activeforeground=FG).pack(side="left", padx=16)

        # ── 설정 카드 ─────────────────────────
        cfg_frame = tk.LabelFrame(self, text="플랫폼 설정", bg=BG, fg=FG_DIM,
                                  font=FONT_M, bd=1)
        cfg_frame.pack(fill="x", padx=24, pady=4)

        self._blog_vars: dict[str, tk.StringVar] = {}
        blog_fields = [
            # (key, label, show_as_password)
            ("claude_api_key",      "Claude API Key (선택)",         True),
            ("naver_blog_client_id",     "네이버 Blog Client ID",    False),
            ("naver_blog_client_secret", "네이버 Blog Client Secret",True),
            ("blogger_client_id",        "Blogger Client ID",        False),
            ("blogger_client_secret",    "Blogger Client Secret",    True),
            ("blogger_blog_id",          "Blogger Blog ID",          False),
            ("wp_site_url",              "WordPress 사이트 URL",      False),
            ("wp_username",              "WordPress 사용자명",        False),
            ("wp_app_password",          "WordPress 앱 비밀번호",     True),
        ]
        for i, (key, label, secret) in enumerate(blog_fields):
            row, col = divmod(i, 2)
            tk.Label(cfg_frame, text=label, bg=BG, fg=FG_DIM,
                     font=FONT_S, width=24, anchor="w").grid(
                row=row, column=col * 2, padx=(8, 2), pady=3, sticky="w")
            var = tk.StringVar()
            self._blog_vars[key] = var
            tk.Entry(cfg_frame, textvariable=var, show="*" if secret else "",
                     bg=SURFACE, fg=FG, insertbackground=FG,
                     relief="flat", font=FONT_S, width=28).grid(
                row=row, column=col * 2 + 1, padx=(2, 12), pady=3)

        self._load_blog_cfg()

        tk.Button(cfg_frame, text="💾 설정 저장", font=FONT_S,
                  bg="#0f766e", fg="white", relief="flat",
                  padx=10, pady=3, cursor="hand2",
                  command=self._save_blog_cfg).grid(
            row=(len(blog_fields) + 1) // 2, column=0, columnspan=4,
            pady=6)

        # ── 미리보기 / 결과 ───────────────────
        mid_frame = tk.Frame(self, bg=BG)
        mid_frame.pack(fill="both", expand=True, padx=24, pady=4)

        tk.Label(mid_frame, text="추출된 제목", bg=BG, fg=FG_DIM, font=FONT_S).pack(anchor="w")
        self._title_var = tk.StringVar()
        tk.Entry(mid_frame, textvariable=self._title_var,
                 bg=SURFACE, fg=FG, insertbackground=FG,
                 relief="flat", font=FONT_M).pack(fill="x", pady=2)

        tk.Label(mid_frame, text="HTML 미리보기 (수정 가능)", bg=BG, fg=FG_DIM, font=FONT_S).pack(anchor="w")
        self._preview_box = scrolledtext.ScrolledText(
            mid_frame, bg="#0f0f1a", fg=FG,
            font=("Consolas", 8), relief="flat", wrap="word", height=8)
        self._preview_box.pack(fill="both", expand=True, pady=2)

        # ── 업로드 버튼 ───────────────────────
        upload_frame = tk.Frame(self, bg=BG)
        upload_frame.pack(pady=6)
        tk.Button(upload_frame, text="🚀  선택 플랫폼에 업로드", font=FONT_B,
                  bg="#0284c7", fg="white", activebackground="#0369a1",
                  activeforeground="white", relief="flat",
                  padx=20, pady=6, cursor="hand2",
                  command=self._on_upload).pack(side="left", padx=6)

        # ── 로그 ──────────────────────────────
        tk.Label(self, text="로그", bg=BG, fg=FG_DIM, font=FONT_M, anchor="w").pack(
            fill="x", padx=28)
        self._log_box = scrolledtext.ScrolledText(
            self, state="disabled", bg="#0f0f1a", fg=FG,
            font=("Consolas", 8), relief="flat", wrap="word", height=5)
        self._log_box.pack(fill="x", padx=24, pady=(2, 10))
        self._log_box.tag_config("ok",   foreground=SUCCESS)
        self._log_box.tag_config("err",  foreground=ERROR)
        self._log_box.tag_config("warn", foreground=WARN)
        self._log_box.tag_config("info", foreground=FG_DIM)

    # ── 설정 ────────────────────────────────────
    def _load_blog_cfg(self):
        try:
            cfg = load_config()
            for k, var in self._blog_vars.items():
                var.set(str(cfg.get(k, "")))
        except Exception:
            pass

    def _save_blog_cfg(self):
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        for k, var in self._blog_vars.items():
            cfg[k] = var.get().strip()
        save_config(cfg)
        self._log("💾 블로그 설정이 저장되었습니다")

    # ── 콘텐츠 추출 ────────────────────────────
    def _on_extract(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "URL을 입력해 주세요.")
            return
        self._log("🔍 콘텐츠 추출 중...")
        threading.Thread(target=self._do_extract, args=(url,), daemon=True).start()

    def _do_extract(self, url: str):
        try:
            from content_extractor import extract_from_url
            data = extract_from_url(url)
            self._extracted = data

            cfg = load_config()
            api_key = cfg.get("claude_api_key", "")

            from ai_formatter import format_as_blog_post
            html = format_as_blog_post(
                data["title"], data["text"], data["images"], api_key=api_key)

            self._html_content = html
            self.after(0, lambda: self._title_var.set(data["title"]))
            self.after(0, self._update_preview)
            self._log(f"✅ 추출 완료 — 이미지 {len(data['images'])}개, 텍스트 {len(data['text'])}자")
            if api_key:
                self._log("✅ Claude AI로 블로그 포스트 생성 완료")
            else:
                self._log("ℹ️ Claude API Key 없음 — 기본 HTML 포맷으로 생성 (Key 입력 시 AI 작성)")
        except Exception as e:
            self._log(f"❌ 추출 오류: {e}")

    def _update_preview(self):
        self._preview_box.delete("1.0", "end")
        self._preview_box.insert("end", self._html_content)

    # ── 업로드 ──────────────────────────────────
    def _on_upload(self):
        if not self._html_content and not self._preview_box.get("1.0", "end").strip():
            messagebox.showwarning("콘텐츠 없음", "먼저 URL에서 콘텐츠를 추출해 주세요.")
            return
        html = self._preview_box.get("1.0", "end").strip() or self._html_content
        title = self._title_var.get().strip() or "블로그 포스트"
        if not (self._use_naver.get() or self._use_blogger.get() or self._use_wp.get()):
            messagebox.showwarning("플랫폼 선택", "업로드할 플랫폼을 하나 이상 선택해 주세요.")
            return
        threading.Thread(target=self._do_upload, args=(title, html), daemon=True).start()

    def _do_upload(self, title: str, html: str):
        cfg = load_config()
        results = []

        if self._use_naver.get():
            try:
                from blog_publishers import naver_get_access_token, post_to_naver_blog
                self._log("🔐 네이버 로그인 중 (브라우저 열림)...")
                token = naver_get_access_token(
                    cfg.get("naver_blog_client_id", ""),
                    cfg.get("naver_blog_client_secret", ""))
                url = post_to_naver_blog(token, title, html)
                self._log(f"✅ 네이버 블로그 업로드 완료: {url}")
                results.append(("네이버", True, url))
            except Exception as e:
                self._log(f"❌ 네이버 블로그 오류: {e}")
                results.append(("네이버", False, str(e)))

        if self._use_blogger.get():
            try:
                from blog_publishers import blogger_get_access_token, post_to_blogger
                self._log("🔐 Google 로그인 중 (브라우저 열림)...")
                token = blogger_get_access_token(
                    cfg.get("blogger_client_id", ""),
                    cfg.get("blogger_client_secret", ""))
                url = post_to_blogger(token, cfg.get("blogger_blog_id", ""), title, html)
                self._log(f"✅ 블로그스팟 업로드 완료: {url}")
                results.append(("블로그스팟", True, url))
            except Exception as e:
                self._log(f"❌ 블로그스팟 오류: {e}")
                results.append(("블로그스팟", False, str(e)))

        if self._use_wp.get():
            try:
                from blog_publishers import post_to_wordpress
                self._log("🚀 워드프레스 업로드 중...")
                url = post_to_wordpress(
                    cfg.get("wp_site_url", ""),
                    cfg.get("wp_username", ""),
                    cfg.get("wp_app_password", ""),
                    title, html)
                self._log(f"✅ 워드프레스 업로드 완료: {url}")
                results.append(("워드프레스", True, url))
            except Exception as e:
                self._log(f"❌ 워드프레스 오류: {e}")
                results.append(("워드프레스", False, str(e)))

        success = [r for r in results if r[1]]
        failed  = [r for r in results if not r[1]]
        summary = f"업로드 결과: 성공 {len(success)}개"
        if failed:
            summary += f", 실패 {len(failed)}개"
        self._log(summary)

    # ── 로그 ────────────────────────────────────
    def _log(self, msg: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            tag = ("ok"   if "✅" in msg else
                   "err"  if "❌" in msg else
                   "warn" if "⚠️" in msg else "info")
            self._log_box.config(state="normal")
            self._log_box.insert("end", f"[{ts}] {msg}\n", tag)
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self.after(0, _do)


# ──────────────────────────────────────────────
# 메인 앱
# ──────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("네이버 스마트스토어 & 블로그 자동화 툴")
        self.geometry("780x680")
        self.resizable(True, True)
        self.configure(bg=BG)
        self._build_ui()

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("TNotebook",
                         background=BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                         background=SURFACE, foreground=FG_DIM,
                         padding=[14, 6], font=FONT_B)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "white")])

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        order_tab = OrderTab(notebook)
        blog_tab  = BlogUploaderTab(notebook)

        notebook.add(order_tab, text="📦  주문 알리미")
        notebook.add(blog_tab,  text="✍️  블로그 업로더")


if __name__ == "__main__":
    app = App()
    app.mainloop()
