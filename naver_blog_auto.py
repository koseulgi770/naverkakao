# -*- coding: utf-8 -*-
"""
Naver Blog Automation Tool
- Login via Selenium, then use requests session with cookies
- Search blog posts, perform comment / neighbor / like actions
- Tkinter dark-theme GUI with tabbed settings
"""

import json
import os
import random
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants / theme
# ---------------------------------------------------------------------------
BG = "#1e1e2e"
SURFACE = "#2a2a3d"
ACCENT = "#7c3aed"
FG = "#e2e8f0"
FG_DIM = "#94a3b8"

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config_blog.json")
PROCESSED_FILE = os.path.join(os.path.dirname(__file__), "processed_posts.json")

DEFAULT_CONFIG = {
    "naver_id": "",
    "naver_pw": "",
    "use_comment": True,
    "use_neighbor": True,
    "use_like": True,
    "neighbor_group": "서로이웃",
    "neighbor_message": "좋은 글 잘 봤습니다! 이웃해요~!",
    "comment_mode": "manual",
    "manual_comments": ["좋은 글 잘보고 갑니다! 즐거운 하루 되세요~"],
    "openai_api_key": "",
    "ai_prompt": "상냥한 말투 / 10~20자내외로 생성 / 이모티콘 없이 / 자연스러운 댓글",
    "ai_fallback": "좋은 글 감사합니다~",
    "keyword": "",
    "sort_order": "latest",
    "collect_count": 5,
    "start_time": "09:00",
    "end_time": "22:00",
    "min_interval": 30,
    "max_interval": 90,
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_processed(processed: set):
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(processed), f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Naver automation core
# ---------------------------------------------------------------------------

class NaverBlogBot:
    """Handles login, search and per-post actions."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def __init__(self, log_callback=None):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.logged_in = False
        self._log = log_callback or print

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self, naver_id: str, naver_pw: str) -> bool:
        """Login using Selenium, transfer cookies to requests session."""
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
            from webdriver_manager.chrome import ChromeDriverManager
        except ImportError as e:
            self._log(f"[오류] 필수 패키지 미설치: {e}")
            return False

        self._log("[로그인] Chrome 브라우저 시작 중...")
        options = Options()
        # Run headed so Naver's bot detection is less likely to trigger
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--window-size=1200,800")

        try:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            self._log(f"[오류] ChromeDriver 실행 실패: {e}")
            return False

        try:
            driver.get("https://nid.naver.com/nidlogin.login")
            wait = WebDriverWait(driver, 20)

            # Use JavaScript to set values to avoid Naver's keyboard monitoring
            id_field = wait.until(EC.presence_of_element_located((By.ID, "id")))
            pw_field = driver.find_element(By.ID, "pw")

            driver.execute_script(
                "arguments[0].value = arguments[1];", id_field, naver_id
            )
            driver.execute_script(
                "arguments[0].value = arguments[1];", pw_field, naver_pw
            )

            # Click login button
            login_btn = driver.find_element(By.ID, "log.login")
            login_btn.click()

            # Wait for redirect away from login page
            try:
                wait.until(EC.url_contains("naver.com"))
                time.sleep(2)
                current_url = driver.current_url
                if "nidlogin" in current_url or "loginform" in current_url:
                    self._log("[오류] 로그인 실패: 아이디/비밀번호를 확인하세요.")
                    return False
            except Exception:
                pass

            # Check for captcha / 2FA page
            if "nid.naver.com" in driver.current_url and "login" in driver.current_url:
                self._log("[경고] 추가 인증이 필요합니다. 브라우저에서 직접 완료해주세요. (30초 대기)")
                time.sleep(30)

            # Transfer cookies
            for cookie in driver.get_cookies():
                self.session.cookies.set(
                    cookie["name"], cookie["value"], domain=cookie.get("domain", "")
                )

            self.logged_in = True
            self._log("[로그인] 로그인 성공!")
            return True

        except Exception as e:
            self._log(f"[오류] 로그인 중 예외: {e}")
            return False
        finally:
            driver.quit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_blog_posts(self, keyword: str, sort_order: str, count: int) -> list:
        """Return list of blog post URLs from Naver Blog search."""
        results = []
        start = 1
        per_page = 10

        if sort_order == "latest":
            base_url = (
                "https://search.naver.com/search.naver"
                "?where=blog&query={kw}&sm=tab_opt&nso=so:dd,p:all&start={s}"
            )
        else:
            base_url = (
                "https://search.naver.com/search.naver"
                "?where=blog&query={kw}&sm=tab_opt&start={s}"
            )

        while len(results) < count:
            url = base_url.format(kw=quote(keyword), s=start)
            try:
                resp = self.session.get(url, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                self._log(f"[검색] 요청 오류: {e}")
                break

            soup = BeautifulSoup(resp.text, "lxml")

            # Naver search result links for blog posts
            links = []
            # Try new Naver search result selectors
            for a in soup.select("a.title_link"):
                href = a.get("href", "")
                if "blog.naver.com" in href:
                    links.append(href)

            # Fallback: any link containing blog.naver.com
            if not links:
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "blog.naver.com" in href and re.search(r"/\d+$", href):
                        links.append(href)

            if not links:
                self._log("[검색] 더 이상 결과가 없습니다.")
                break

            for link in links:
                if len(results) >= count:
                    break
                # Normalise URL
                clean = link.split("?")[0]
                if clean not in results:
                    results.append(clean)

            start += per_page
            time.sleep(random.uniform(0.5, 1.5))

        return results

    # ------------------------------------------------------------------
    # Parse blog post
    # ------------------------------------------------------------------

    def _parse_blog_id_logno(self, url: str):
        """Extract (blogId, logNo) from a blog.naver.com URL."""
        m = re.match(r"https?://blog\.naver\.com/([^/]+)/(\d+)", url)
        if m:
            return m.group(1), m.group(2)
        # Handle PostView.nhn?blogId=...&logNo=...
        m2 = re.search(r"blogId=([^&]+)&(?:amp;)?logNo=(\d+)", url)
        if m2:
            return m2.group(1), m2.group(2)
        return None, None

    def _get_post_content(self, url: str) -> str:
        """Fetch post content text for AI comment generation."""
        try:
            resp = self.session.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, "lxml")
            # Try to get main post text
            for selector in [".se-main-container", ".post-view", "#postViewArea"]:
                el = soup.select_one(selector)
                if el:
                    return el.get_text(separator=" ", strip=True)[:500]
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Like
    # ------------------------------------------------------------------

    def do_like(self, url: str) -> bool:
        """Attempt to like a post via the Naver sympathy API."""
        blog_id, log_no = self._parse_blog_id_logno(url)
        if not blog_id or not log_no:
            self._log(f"[좋아요] URL 파싱 실패: {url}")
            return False

        # Get post page first to set referer and get necessary tokens
        try:
            # Naver blog like API
            api_url = "https://blog.naver.com/SympathyWriteAjax.naver"
            data = {
                "blogId": blog_id,
                "logNo": log_no,
            }
            headers = {
                "Referer": url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            resp = self.session.post(api_url, data=data, headers=headers, timeout=10)
            if resp.status_code == 200:
                self._log(f"[좋아요] 완료: {url}")
                return True
            else:
                self._log(f"[좋아요] 실패 (HTTP {resp.status_code}): {url}")
                return False
        except Exception as e:
            self._log(f"[좋아요] 오류: {e}")
            return False

    # ------------------------------------------------------------------
    # Neighbor (서이추)
    # ------------------------------------------------------------------

    def do_neighbor(self, url: str, group_name: str, message: str) -> bool:
        """Send 서로이웃 request."""
        blog_id, _ = self._parse_blog_id_logno(url)
        if not blog_id:
            self._log(f"[서이추] URL 파싱 실패: {url}")
            return False

        try:
            # Get buddy add page to extract form token
            buddy_url = f"https://blog.naver.com/BuddyListDirect.naver?blogId={blog_id}"
            resp = self.session.get(buddy_url, timeout=15)
            soup = BeautifulSoup(resp.text, "lxml")

            # Extract hidden form fields
            form = soup.find("form", id="buddyAddForm") or soup.find("form")
            params = {}
            if form:
                for inp in form.find_all("input", type="hidden"):
                    if inp.get("name"):
                        params[inp["name"]] = inp.get("value", "")

            # Naver buddy add API
            api_url = "https://blog.naver.com/BuddyAddProc.naver"
            params.update(
                {
                    "blogId": blog_id,
                    "addType": "BothBuddy",
                    "groupName": group_name,
                    "memo": message,
                }
            )
            headers = {"Referer": buddy_url}
            resp2 = self.session.post(api_url, data=params, headers=headers, timeout=10)
            if resp2.status_code == 200:
                self._log(f"[서이추] 요청 완료: {blog_id}")
                return True
            else:
                self._log(f"[서이추] 실패 (HTTP {resp2.status_code}): {blog_id}")
                return False
        except Exception as e:
            self._log(f"[서이추] 오류: {e}")
            return False

    # ------------------------------------------------------------------
    # Comment
    # ------------------------------------------------------------------

    def _generate_ai_comment(self, api_key: str, prompt: str, post_content: str) -> str:
        """Generate comment using OpenAI API."""
        try:
            import openai

            client = openai.OpenAI(api_key=api_key)
            system_msg = prompt
            user_msg = f"다음 블로그 글에 댓글을 작성해주세요:\n{post_content}" if post_content else "블로그 글에 간단한 댓글을 작성해주세요."
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=100,
                temperature=0.8,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            self._log(f"[AI댓글] API 오류: {e}")
            return ""

    def do_comment(
        self,
        url: str,
        comment_mode: str,
        manual_comments: list,
        api_key: str,
        ai_prompt: str,
        ai_fallback: str,
    ) -> bool:
        """Post a comment on a blog post."""
        blog_id, log_no = self._parse_blog_id_logno(url)
        if not blog_id or not log_no:
            self._log(f"[댓글] URL 파싱 실패: {url}")
            return False

        # Determine comment text
        if comment_mode == "ai":
            post_content = self._get_post_content(url)
            comment_text = self._generate_ai_comment(api_key, ai_prompt, post_content)
            if not comment_text:
                comment_text = ai_fallback
                self._log("[댓글] AI 실패 → 대체 메시지 사용")
        else:
            comment_text = random.choice(manual_comments) if manual_comments else "좋은 글 감사합니다~"

        try:
            # First load the post page to get session context
            self.session.get(url, timeout=15)

            # Naver comment write API
            api_url = "https://blog.naver.com/CommentWrite.naver"
            data = {
                "blogId": blog_id,
                "logNo": log_no,
                "contents": comment_text,
                "validateKey": "",
            }
            headers = {
                "Referer": url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            resp = self.session.post(api_url, data=data, headers=headers, timeout=10)
            if resp.status_code == 200:
                self._log(f"[댓글] 완료 ({comment_text[:20]}...): {url}")
                return True
            else:
                self._log(f"[댓글] 실패 (HTTP {resp.status_code}): {url}")
                return False
        except Exception as e:
            self._log(f"[댓글] 오류: {e}")
            return False


# ---------------------------------------------------------------------------
# Automation runner (background thread)
# ---------------------------------------------------------------------------

class AutomationRunner:
    def __init__(self, cfg: dict, log_callback, status_callback):
        self.cfg = cfg
        self._log = log_callback
        self._status = status_callback
        self._stop_event = threading.Event()
        self._thread = None
        self.bot = NaverBlogBot(log_callback=log_callback)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._status("중지됨")

    def _in_time_window(self) -> bool:
        now = datetime.now().strftime("%H:%M")
        return self.cfg["start_time"] <= now <= self.cfg["end_time"]

    def _run(self):
        cfg = self.cfg
        self._log("[자동화] 시작")

        # Login
        if not self.bot.login(cfg["naver_id"], cfg["naver_pw"]):
            self._log("[자동화] 로그인 실패로 중단")
            self._status("로그인 실패")
            return

        processed = load_processed()

        while not self._stop_event.is_set():
            if not self._in_time_window():
                self._log(f"[자동화] 운영 시간 외 ({cfg['start_time']}~{cfg['end_time']}). 60초 대기.")
                self._stop_event.wait(60)
                continue

            # Search for posts
            self._log(f"[검색] 키워드: {cfg['keyword']} | 수집: {cfg['collect_count']}개")
            posts = self.bot.search_blog_posts(
                cfg["keyword"], cfg["sort_order"], cfg["collect_count"]
            )
            self._log(f"[검색] 총 {len(posts)}개 URL 수집")

            new_posts = [p for p in posts if p not in processed]
            if not new_posts:
                self._log("[자동화] 새 게시물 없음. 다음 주기 대기.")

            for url in new_posts:
                if self._stop_event.is_set():
                    break
                self._log(f"[처리] {url}")

                if cfg.get("use_like"):
                    self.bot.do_like(url)
                    time.sleep(random.uniform(1, 3))

                if cfg.get("use_neighbor"):
                    self.bot.do_neighbor(
                        url, cfg["neighbor_group"], cfg["neighbor_message"]
                    )
                    time.sleep(random.uniform(1, 3))

                if cfg.get("use_comment"):
                    self.bot.do_comment(
                        url,
                        cfg["comment_mode"],
                        cfg["manual_comments"],
                        cfg["openai_api_key"],
                        cfg["ai_prompt"],
                        cfg["ai_fallback"],
                    )

                processed.add(url)
                save_processed(processed)

                # Wait random interval
                interval = random.randint(
                    int(cfg["min_interval"]), int(cfg["max_interval"])
                )
                next_time = datetime.fromtimestamp(time.time() + interval).strftime("%H:%M:%S")
                self._log(f"[대기] 다음 작업: {next_time} ({interval}초 후)")
                self._status(f"다음 작업: {next_time}")
                self._stop_event.wait(interval)

            if not self._stop_event.is_set():
                # After processing batch, wait min_interval before next search
                wait = int(cfg["min_interval"])
                self._stop_event.wait(wait)

        self._log("[자동화] 종료")
        self._status("종료")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.runner = None

        self.title("네이버 블로그 자동화")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.geometry("780x700")

        self._build_style()
        self._build_ui()
        self._load_cfg_to_ui()

    # ------------------------------------------------------------------
    # Style
    # ------------------------------------------------------------------

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, fieldbackground=SURFACE, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
        style.configure(
            "TButton",
            background=ACCENT,
            foreground=FG,
            relief="flat",
            padding=(10, 6),
            font=("Segoe UI", 10, "bold"),
        )
        style.map("TButton", background=[("active", "#6d28d9")])
        style.configure(
            "TNotebook",
            background=BG,
            tabmargins=[0, 0, 0, 0],
            borderwidth=0,
        )
        style.configure(
            "TNotebook.Tab",
            background=SURFACE,
            foreground=FG_DIM,
            padding=[14, 6],
            font=("Segoe UI", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT)],
            foreground=[("selected", FG)],
        )
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TEntry", fieldbackground=SURFACE, foreground=FG, insertcolor=FG)
        style.configure("TCombobox", fieldbackground=SURFACE, foreground=FG, background=SURFACE)
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE)])
        style.configure("TSpinbox", fieldbackground=SURFACE, foreground=FG)

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _entry(self, parent, **kwargs) -> ttk.Entry:
        e = ttk.Entry(parent, **kwargs)
        return e

    def _label(self, parent, text, dim=False, **kwargs) -> ttk.Label:
        fg = FG_DIM if dim else FG
        return ttk.Label(parent, text=text, foreground=fg, **kwargs)

    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=SURFACE, pady=10)
        top.pack(fill="x")
        tk.Label(
            top, text="네이버 블로그 자동화", bg=SURFACE, fg=FG,
            font=("Segoe UI", 16, "bold")
        ).pack(side="left", padx=20)

        # Notebook
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self._tab_login = ttk.Frame(nb)
        self._tab_options = ttk.Frame(nb)
        self._tab_comment = ttk.Frame(nb)
        self._tab_search = ttk.Frame(nb)
        self._tab_time = ttk.Frame(nb)

        nb.add(self._tab_login, text="  로그인  ")
        nb.add(self._tab_options, text="  동작 설정  ")
        nb.add(self._tab_comment, text="  댓글 설정  ")
        nb.add(self._tab_search, text="  검색 설정  ")
        nb.add(self._tab_time, text="  시간 설정  ")

        self._build_tab_login()
        self._build_tab_options()
        self._build_tab_comment()
        self._build_tab_search()
        self._build_tab_time()

        # Bottom controls
        self._build_bottom()

    def _build_tab_login(self):
        f = self._tab_login
        pad = {"padx": 20, "pady": 8}

        self._label(f, "네이버 아이디").grid(row=0, column=0, sticky="w", **pad)
        self.var_id = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_id, width=30).grid(row=0, column=1, sticky="w", **pad)

        self._label(f, "비밀번호").grid(row=1, column=0, sticky="w", **pad)
        self.var_pw = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_pw, show="*", width=30).grid(row=1, column=1, sticky="w", **pad)

        btn_frame = tk.Frame(f, bg=BG)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=10, padx=20, sticky="w")
        ttk.Button(btn_frame, text="로그인 테스트", command=self._test_login).pack(side="left")
        self.lbl_login_status = self._label(btn_frame, "  미로그인", dim=True)
        self.lbl_login_status.pack(side="left", padx=10)

        note = (
            "※ Chrome 브라우저가 열리고 로그인 후 자동으로 닫힙니다.\n"
            "  캡차/2FA가 나타나면 브라우저에서 직접 완료하세요."
        )
        self._label(f, note, dim=True).grid(row=3, column=0, columnspan=2, sticky="w", padx=20)

    def _build_tab_options(self):
        f = self._tab_options
        pad = {"padx": 20, "pady": 8}

        self.var_use_comment = tk.BooleanVar()
        self.var_use_neighbor = tk.BooleanVar()
        self.var_use_like = tk.BooleanVar()

        ttk.Checkbutton(f, text="댓글 달기", variable=self.var_use_comment).grid(
            row=0, column=0, sticky="w", **pad
        )
        ttk.Checkbutton(f, text="서로이웃 신청 (서이추)", variable=self.var_use_neighbor).grid(
            row=1, column=0, sticky="w", **pad
        )
        ttk.Checkbutton(f, text="좋아요 (공감)", variable=self.var_use_like).grid(
            row=2, column=0, sticky="w", **pad
        )

        # Neighbor settings
        lf = ttk.LabelFrame(f, text="서이추 설정", padding=10)
        lf.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=10)

        self._label(lf, "이웃 그룹").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        self.var_neighbor_group = tk.StringVar()
        ttk.Combobox(
            lf, textvariable=self.var_neighbor_group,
            values=["서로이웃", "이웃"], width=20, state="readonly"
        ).grid(row=0, column=1, sticky="w", padx=5, pady=4)

        self._label(lf, "신청 메시지").grid(row=1, column=0, sticky="nw", padx=5, pady=4)
        self.var_neighbor_msg = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_neighbor_msg, width=45).grid(
            row=1, column=1, sticky="w", padx=5, pady=4
        )

    def _build_tab_comment(self):
        f = self._tab_comment
        pad = {"padx": 20, "pady": 6}

        self.var_comment_mode = tk.StringVar(value="manual")

        ttk.Radiobutton(
            f, text="수동 댓글 (랜덤 선택)", variable=self.var_comment_mode,
            value="manual", command=self._toggle_comment_mode
        ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(
            f, text="AI 댓글 (OpenAI GPT)", variable=self.var_comment_mode,
            value="ai", command=self._toggle_comment_mode
        ).grid(row=0, column=1, sticky="w", **pad)

        # Manual
        self._lf_manual = ttk.LabelFrame(f, text="수동 댓글 목록 (한 줄에 하나씩)", padding=10)
        self._lf_manual.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=6)
        self.txt_manual_comments = tk.Text(
            self._lf_manual, width=60, height=6, bg=SURFACE, fg=FG,
            insertbackground=FG, relief="flat", font=("Segoe UI", 10)
        )
        self.txt_manual_comments.pack(fill="both", expand=True)

        # AI
        self._lf_ai = ttk.LabelFrame(f, text="AI 댓글 설정", padding=10)
        self._lf_ai.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=6)

        self._label(self._lf_ai, "OpenAI API Key").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        self.var_api_key = tk.StringVar()
        key_frame = tk.Frame(self._lf_ai, bg=BG)
        key_frame.grid(row=0, column=1, sticky="w")
        ttk.Entry(key_frame, textvariable=self.var_api_key, width=35, show="*").pack(side="left")
        ttk.Button(key_frame, text="검증", command=self._validate_api_key, width=6).pack(side="left", padx=5)

        self._label(self._lf_ai, "AI 프롬프트").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        self.var_ai_prompt = tk.StringVar()
        ttk.Entry(self._lf_ai, textvariable=self.var_ai_prompt, width=45).grid(
            row=1, column=1, sticky="w", padx=5, pady=4
        )

        self._label(self._lf_ai, "실패 시 대체 메시지").grid(row=2, column=0, sticky="w", padx=5, pady=4)
        self.var_ai_fallback = tk.StringVar()
        ttk.Entry(self._lf_ai, textvariable=self.var_ai_fallback, width=45).grid(
            row=2, column=1, sticky="w", padx=5, pady=4
        )

    def _build_tab_search(self):
        f = self._tab_search
        pad = {"padx": 20, "pady": 8}

        self._label(f, "검색 키워드").grid(row=0, column=0, sticky="w", **pad)
        self.var_keyword = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_keyword, width=35).grid(row=0, column=1, sticky="w", **pad)

        self._label(f, "게시물 유형").grid(row=1, column=0, sticky="w", **pad)
        self._label(f, "블로그 (고정)").grid(row=1, column=1, sticky="w", **pad)

        self._label(f, "정렬 순서").grid(row=2, column=0, sticky="w", **pad)
        self.var_sort = tk.StringVar()
        ttk.Combobox(
            f, textvariable=self.var_sort,
            values=["최신순", "정확도순"], width=15, state="readonly"
        ).grid(row=2, column=1, sticky="w", **pad)

        self._label(f, "수집할 게시물 수").grid(row=3, column=0, sticky="w", **pad)
        self.var_collect_count = tk.IntVar(value=5)
        ttk.Spinbox(f, from_=1, to=100, textvariable=self.var_collect_count, width=8).grid(
            row=3, column=1, sticky="w", **pad
        )

    def _build_tab_time(self):
        f = self._tab_time
        pad = {"padx": 20, "pady": 8}

        self._label(f, "시작 시간 (HH:MM)").grid(row=0, column=0, sticky="w", **pad)
        self.var_start_time = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_start_time, width=10).grid(row=0, column=1, sticky="w", **pad)

        self._label(f, "종료 시간 (HH:MM)").grid(row=1, column=0, sticky="w", **pad)
        self.var_end_time = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_end_time, width=10).grid(row=1, column=1, sticky="w", **pad)

        self._label(f, "최소 간격 (초)").grid(row=2, column=0, sticky="w", **pad)
        self.var_min_interval = tk.IntVar(value=30)
        ttk.Spinbox(f, from_=5, to=3600, textvariable=self.var_min_interval, width=8).grid(
            row=2, column=1, sticky="w", **pad
        )

        self._label(f, "최대 간격 (초)").grid(row=3, column=0, sticky="w", **pad)
        self.var_max_interval = tk.IntVar(value=90)
        ttk.Spinbox(f, from_=5, to=3600, textvariable=self.var_max_interval, width=8).grid(
            row=3, column=1, sticky="w", **pad
        )

        note = (
            "※ 각 게시물 처리 후 최소/최대 간격 사이의\n"
            "  랜덤한 시간(초) 동안 대기합니다."
        )
        self._label(f, note, dim=True).grid(row=4, column=0, columnspan=2, sticky="w", padx=20, pady=10)

    def _build_bottom(self):
        # Status bar + buttons
        ctrl = tk.Frame(self, bg=SURFACE, pady=8)
        ctrl.pack(fill="x", padx=10, pady=(5, 0))

        ttk.Button(ctrl, text="설정 저장", command=self._save).pack(side="left", padx=8)
        ttk.Button(ctrl, text="▶ 실행", command=self._start).pack(side="left", padx=4)

        stop_btn = ttk.Button(ctrl, text="■ 중지", command=self._stop)
        stop_btn.pack(side="left", padx=4)

        self.lbl_status = tk.Label(
            ctrl, text="대기 중", bg=SURFACE, fg=FG_DIM,
            font=("Segoe UI", 10)
        )
        self.lbl_status.pack(side="right", padx=12)

        # Log area
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        tk.Label(
            log_frame, text="실행 로그", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 9)
        ).pack(anchor="w")

        self.log_area = scrolledtext.ScrolledText(
            log_frame,
            bg=SURFACE, fg=FG, insertbackground=FG,
            relief="flat", font=("Consolas", 9),
            height=10, wrap="word",
        )
        self.log_area.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _toggle_comment_mode(self):
        mode = self.var_comment_mode.get()
        # Just visual feedback; both frames stay visible
        if mode == "manual":
            self._lf_manual.configure(labelanchor="nw")
        else:
            self._lf_ai.configure(labelanchor="nw")

    def _load_cfg_to_ui(self):
        cfg = self.cfg
        self.var_id.set(cfg.get("naver_id", ""))
        self.var_pw.set(cfg.get("naver_pw", ""))
        self.var_use_comment.set(cfg.get("use_comment", True))
        self.var_use_neighbor.set(cfg.get("use_neighbor", True))
        self.var_use_like.set(cfg.get("use_like", True))
        self.var_neighbor_group.set(cfg.get("neighbor_group", "서로이웃"))
        self.var_neighbor_msg.set(cfg.get("neighbor_message", ""))
        self.var_comment_mode.set(cfg.get("comment_mode", "manual"))
        comments = cfg.get("manual_comments", [])
        self.txt_manual_comments.delete("1.0", "end")
        self.txt_manual_comments.insert("1.0", "\n".join(comments))
        self.var_api_key.set(cfg.get("openai_api_key", ""))
        self.var_ai_prompt.set(cfg.get("ai_prompt", ""))
        self.var_ai_fallback.set(cfg.get("ai_fallback", ""))
        self.var_keyword.set(cfg.get("keyword", ""))
        sort_map = {"latest": "최신순", "accuracy": "정확도순"}
        self.var_sort.set(sort_map.get(cfg.get("sort_order", "latest"), "최신순"))
        self.var_collect_count.set(cfg.get("collect_count", 5))
        self.var_start_time.set(cfg.get("start_time", "09:00"))
        self.var_end_time.set(cfg.get("end_time", "22:00"))
        self.var_min_interval.set(cfg.get("min_interval", 30))
        self.var_max_interval.set(cfg.get("max_interval", 90))

    def _ui_to_cfg(self) -> dict:
        sort_map = {"최신순": "latest", "정확도순": "accuracy"}
        comments_raw = self.txt_manual_comments.get("1.0", "end").strip()
        comments = [c.strip() for c in comments_raw.split("\n") if c.strip()]
        return {
            "naver_id": self.var_id.get().strip(),
            "naver_pw": self.var_pw.get(),
            "use_comment": self.var_use_comment.get(),
            "use_neighbor": self.var_use_neighbor.get(),
            "use_like": self.var_use_like.get(),
            "neighbor_group": self.var_neighbor_group.get(),
            "neighbor_message": self.var_neighbor_msg.get(),
            "comment_mode": self.var_comment_mode.get(),
            "manual_comments": comments or ["좋은 글 잘보고 갑니다!"],
            "openai_api_key": self.var_api_key.get().strip(),
            "ai_prompt": self.var_ai_prompt.get().strip(),
            "ai_fallback": self.var_ai_fallback.get().strip(),
            "keyword": self.var_keyword.get().strip(),
            "sort_order": sort_map.get(self.var_sort.get(), "latest"),
            "collect_count": self.var_collect_count.get(),
            "start_time": self.var_start_time.get().strip(),
            "end_time": self.var_end_time.get().strip(),
            "min_interval": self.var_min_interval.get(),
            "max_interval": self.var_max_interval.get(),
        }

    def _log(self, msg: str):
        def _insert():
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_area.insert("end", f"[{ts}] {msg}\n")
            self.log_area.see("end")
        self.after(0, _insert)

    def _set_status(self, msg: str):
        self.after(0, lambda: self.lbl_status.configure(text=msg))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _save(self):
        self.cfg = self._ui_to_cfg()
        save_config(self.cfg)
        self._log("[설정] 저장 완료")

    def _test_login(self):
        cfg = self._ui_to_cfg()
        if not cfg["naver_id"] or not cfg["naver_pw"]:
            messagebox.showwarning("입력 오류", "아이디와 비밀번호를 입력하세요.")
            return

        def _do():
            self.lbl_login_status.configure(text="  로그인 중...", foreground=FG_DIM)
            bot = NaverBlogBot(log_callback=self._log)
            ok = bot.login(cfg["naver_id"], cfg["naver_pw"])
            if ok:
                self.lbl_login_status.configure(text="  ✓ 로그인 성공", foreground="#22c55e")
            else:
                self.lbl_login_status.configure(text="  ✗ 로그인 실패", foreground="#ef4444")

        threading.Thread(target=_do, daemon=True).start()

    def _validate_api_key(self):
        key = self.var_api_key.get().strip()
        if not key:
            messagebox.showwarning("입력 오류", "API Key를 입력하세요.")
            return

        def _do():
            self._log("[API] OpenAI API Key 검증 중...")
            try:
                import openai
                client = openai.OpenAI(api_key=key)
                client.models.list()
                self._log("[API] API Key 유효!")
                messagebox.showinfo("성공", "API Key가 유효합니다.")
            except Exception as e:
                self._log(f"[API] 유효하지 않음: {e}")
                messagebox.showerror("실패", f"API Key 오류:\n{e}")

        threading.Thread(target=_do, daemon=True).start()

    def _start(self):
        self._save()
        cfg = self.cfg

        if not cfg["naver_id"] or not cfg["naver_pw"]:
            messagebox.showwarning("입력 오류", "로그인 탭에서 아이디/비밀번호를 입력하세요.")
            return
        if not cfg["keyword"]:
            messagebox.showwarning("입력 오류", "검색 탭에서 키워드를 입력하세요.")
            return
        if cfg["min_interval"] > cfg["max_interval"]:
            messagebox.showwarning("입력 오류", "최소 간격이 최대 간격보다 클 수 없습니다.")
            return

        if self.runner and self.runner._thread and self.runner._thread.is_alive():
            messagebox.showinfo("알림", "이미 실행 중입니다.")
            return

        self.runner = AutomationRunner(cfg, self._log, self._set_status)
        self.runner.start()
        self._set_status("실행 중")
        self._log("[자동화] 실행 시작")

    def _stop(self):
        if self.runner:
            self.runner.stop()
            self._log("[자동화] 중지 요청")
        self._set_status("중지됨")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
