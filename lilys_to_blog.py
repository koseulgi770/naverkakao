"""
Lilys AI → 네이버 블로그 자동 포스팅 도구

세 가지 방식으로 글을 가져와 네이버 블로그에 자동 발행합니다.
  1. Lilys 노트 링크 붙여넣기 → 내용 추출 → 발행 (API 키 불필요)
  2. Lilys AI 라이브러리(https://lilys.ai/library) 감시 → 새 노트 발견 시 자동 발행 (API 키 불필요)
  3. 유튜브 링크 입력 → Lilys AI 공식 API로 요약(blogPost 형식) → 발행 (API 키 필요)

네이버 블로그는 공식 글쓰기 API가 종료되어(2020년) Selenium 브라우저 자동화로 발행합니다.
전용 크롬 프로필(chrome_profile 폴더)을 사용하므로, [로그인용 브라우저 열기] 버튼으로
최초 1회 네이버와 Lilys에 직접 로그인해 두면 이후에는 자동으로 세션이 유지됩니다.
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import time
import json
import os
import re
import requests

# ──────────────────────────────────────────────
# 설정 / 상태 파일
# ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "lilys_config.json")
POSTED_FILE = os.path.join(BASE_DIR, "posted_notes.json")

DEFAULT_CONFIG = {
    "lilys_api_key": "",
    "model_type": "gpt-4",
    "result_language": "ko",
    "check_interval_minutes": 30,
    "lilys_folder_name": "",
    "chrome_profile_dir": os.path.join(BASE_DIR, "chrome_profile"),
    "publish_mode": "publish",  # "publish"(즉시 발행) 또는 "draft"(임시저장)
}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_posted() -> set:
    if os.path.exists(POSTED_FILE):
        try:
            with open(POSTED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_posted(posted: set):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(posted), f, ensure_ascii=False, indent=2)

# ──────────────────────────────────────────────
# Lilys AI 공식 API (https://reference.lilys.ai/)
# ──────────────────────────────────────────────
LILYS_API_BASE = "https://tool.lilys.ai"

def lilys_request_summary(api_key: str, youtube_url: str,
                          model_type: str, language: str) -> str:
    """유튜브 링크로 요약 생성을 요청하고 requestId를 반환한다."""
    resp = requests.post(
        f"{LILYS_API_BASE}/summaries",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "source": {
                "sourceType": "youtube_video",
                "sourceUrl": youtube_url,
            },
            "resultLanguage": language,
            "modelType": model_type,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    request_id = data.get("requestId") or (data.get("data") or {}).get("requestId")
    if not request_id:
        raise RuntimeError(f"requestId를 찾을 수 없습니다: {data}")
    return request_id

def _collect_long_strings(obj, out: list):
    """중첩 JSON에서 본문으로 보이는 긴 문자열들을 수집한다."""
    if isinstance(obj, str):
        if len(obj.strip()) >= 80:
            out.append(obj.strip())
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_long_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_long_strings(v, out)

def _extract_title(obj) -> str:
    """중첩 JSON에서 title 계열 키를 찾는다."""
    if isinstance(obj, dict):
        for key in ("title", "noteTitle", "videoTitle", "sourceTitle"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            found = _extract_title(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _extract_title(v)
            if found:
                return found
    return ""

def lilys_poll_result(api_key: str, request_id: str, log,
                      timeout_sec: int = 900) -> tuple[str, str]:
    """
    요약 완료를 폴링해서 (제목, 본문) 을 반환한다.
    blogPost 형식을 우선 시도하고, 없으면 summaryNote 로 대체한다.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + timeout_sec

    for result_type in ("blogPost", "summaryNote"):
        while time.time() < deadline:
            resp = requests.get(
                f"{LILYS_API_BASE}/summaries/{request_id}",
                headers=headers,
                params={"resultType": result_type},
                timeout=30,
            )
            if resp.status_code == 404:
                break  # 해당 resultType 미지원 → 다음 형식 시도
            resp.raise_for_status()
            data = resp.json()

            status = str(data.get("status", "")).lower()
            if status in ("pending", "processing", "in_progress", "running"):
                log(f"⏳ 요약 생성 중... ({result_type})")
                time.sleep(15)
                continue

            texts: list = []
            _collect_long_strings(data, texts)
            if texts:
                title = _extract_title(data)
                body = "\n\n".join(texts)
                return title, body

            # status 필드가 불명확한 경우도 잠시 후 재시도
            log(f"⏳ 결과 대기 중... ({result_type})")
            time.sleep(15)

    raise RuntimeError("제한 시간 내에 요약 결과를 받지 못했습니다.")

def markdown_to_plain(text: str) -> str:
    """블로그 붙여넣기용으로 마크다운 표기를 가볍게 정리한다."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)      # 코드블록 제거
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)     # 헤딩 기호
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)           # 굵게
    text = re.sub(r"\*(.+?)\*", r"\1", text)               # 기울임
    text = re.sub(r"^[-*]\s+", "· ", text, flags=re.M)     # 리스트 불릿
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 (\2)", text) # 링크
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ──────────────────────────────────────────────
# Selenium 브라우저 (네이버 발행 + Lilys 라이브러리 감시 공용)
# ──────────────────────────────────────────────
def _kill_profile_chrome(profile_dir: str):
    """이 전용 프로필을 사용 중인 크롬 프로세스를 종료한다 (Windows)."""
    if os.name != "nt":
        return
    try:
        import subprocess
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{profile_dir}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=30)
    except Exception:
        pass

class Browser:
    def __init__(self, profile_dir: str, log):
        self.profile_dir = profile_dir
        self.log = log
        self.driver = None

    def _launch(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        os.makedirs(self.profile_dir, exist_ok=True)
        opts = Options()
        opts.add_argument(f"--user-data-dir={self.profile_dir}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        return webdriver.Chrome(options=opts)

    def get_driver(self):
        if self.driver:
            try:
                _ = self.driver.current_url  # 살아있는지 확인
                return self.driver
            except Exception:
                self.driver = None

        try:
            self.driver = self._launch()
        except Exception:
            # 같은 프로필을 쓰는 크롬이 이미 떠 있으면 실행에 실패한다.
            # 남아있는 크롬을 정리하고 한 번 더 시도한다.
            self.log("⚠️ 크롬 실행 실패. 프로필을 사용 중인 기존 크롬 창을 정리하고 재시도합니다...")
            _kill_profile_chrome(self.profile_dir)
            time.sleep(3)
            try:
                self.driver = self._launch()
            except Exception as e:
                raise RuntimeError(
                    "크롬을 시작하지 못했습니다. 다음을 확인해 주세요:\n"
                    "  1) 열려 있는 크롬 창을 모두 닫고 다시 시도\n"
                    "  2) 크롬(Chrome)이 설치되어 있는지 확인\n"
                    "  3) 크롬을 최신 버전으로 업데이트\n"
                    f"  (원본 오류: {str(e).splitlines()[0]})"
                ) from e
        return self.driver

    def quit(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

def paste_text(driver, element, text: str):
    """클립보드 붙여넣기로 입력한다 (이모지 등 non-BMP 문자, 보안 입력 대응)."""
    import pyperclip
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys

    element.click()
    time.sleep(0.5)
    pyperclip.copy(text)
    ActionChains(driver).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
    time.sleep(0.8)

def _click_if_exists(driver, css: str) -> bool:
    from selenium.webdriver.common.by import By
    try:
        els = driver.find_elements(By.CSS_SELECTOR, css)
        if els and els[0].is_displayed():
            els[0].click()
            time.sleep(0.6)
            return True
    except Exception:
        pass
    return False

def post_to_naver_blog(browser: Browser, title: str, content: str,
                       publish_mode: str, log) -> bool:
    """네이버 블로그 스마트에디터 ONE 에 글을 작성하고 발행/임시저장한다."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = browser.get_driver()
    log("🌐 네이버 블로그 글쓰기 페이지 이동 중...")
    driver.get("https://blog.naver.com/GoBlogWrite.naver")
    time.sleep(5)

    if "nid.naver.com" in driver.current_url:
        log("❌ 네이버 로그인이 필요합니다. [로그인용 브라우저 열기]로 먼저 로그인해 주세요.")
        return False

    # 글쓰기 화면은 mainFrame iframe 안에 있음
    try:
        WebDriverWait(driver, 15).until(
            EC.frame_to_be_available_and_switch_to_it((By.ID, "mainFrame")))
    except Exception:
        pass  # 일부 환경은 iframe 없이 에디터가 바로 뜸

    time.sleep(3)
    # 작성 중이던 글 팝업 / 도움말 패널 닫기
    _click_if_exists(driver, ".se-popup-button-cancel")
    _click_if_exists(driver, "button.se-help-panel-close-button")

    try:
        # 제목 입력
        title_el = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".se-section-documentTitle .se-text-paragraph")))
        paste_text(driver, title_el, title)
        log("✏️ 제목 입력 완료")

        # 본문 입력
        body_el = driver.find_element(
            By.CSS_SELECTOR, ".se-section-text .se-text-paragraph")
        paste_text(driver, body_el, content)
        log("✏️ 본문 입력 완료")
    except Exception as e:
        log(f"❌ 에디터 입력 실패 (에디터 구조가 변경되었을 수 있음): {e}")
        driver.switch_to.default_content()
        return False

    time.sleep(1)
    try:
        if publish_mode == "draft":
            # 임시저장
            save_btn = driver.find_element(
                By.CSS_SELECTOR, "button[class*='save_btn']")
            save_btn.click()
            time.sleep(2)
            log("💾 임시저장 완료")
        else:
            # 발행 버튼 → 발행 레이어의 확인 버튼
            publish_btn = driver.find_element(
                By.CSS_SELECTOR, "button[class*='publish_btn']")
            publish_btn.click()
            time.sleep(2)
            confirm_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button[class*='confirm_btn']")))
            confirm_btn.click()
            time.sleep(3)
            log("🚀 발행 완료!")
    except Exception as e:
        log(f"❌ 발행 버튼 클릭 실패 (에디터 구조가 변경되었을 수 있음): {e}")
        driver.switch_to.default_content()
        return False

    driver.switch_to.default_content()
    return True

# ──────────────────────────────────────────────
# Lilys 라이브러리(콜렉션) 감시
# ──────────────────────────────────────────────
def _collect_note_links(driver) -> list[tuple[str, str]]:
    """현재 페이지에서 (노트URL, 제목) 링크들을 수집한다."""
    from selenium.webdriver.common.by import By

    notes, seen = [], set()
    for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='/notes/'], a[href*='/digest/']"):
        try:
            href = a.get_attribute("href") or ""
            text = (a.text or "").strip().split("\n")
            title = max(text, key=len) if text else ""
            if href and href not in seen and title:
                seen.add(href)
                notes.append((href, title))
        except Exception:
            continue
    return notes

def fetch_collection_notes(browser: Browser, log,
                           folder_name: str = "") -> list[tuple[str, str]]:
    """
    라이브러리(또는 보관함) 페이지에서 (노트URL, 제목) 목록을 수집한다.
    folder_name 이 지정되면 사이드바에서 해당 폴더를 클릭한 뒤 수집한다.
    """
    from selenium.webdriver.common.by import By

    driver = browser.get_driver()
    notes = []
    # 라이브러리 → 보관함 순으로 시도 (Lilys 화면 구성에 따라 다름)
    for url in ("https://lilys.ai/library", "https://lilys.ai/collections"):
        driver.get(url)
        time.sleep(6)

        if "signin" in driver.current_url or "login" in driver.current_url:
            log("❌ Lilys AI 로그인이 필요합니다. [로그인용 브라우저 열기]로 먼저 로그인해 주세요.")
            return []

        # 특정 폴더만 가져오도록 설정한 경우 사이드바에서 폴더 클릭
        if folder_name:
            clicked = False
            for el in driver.find_elements(
                    By.XPATH, f"//*[contains(normalize-space(text()), '{folder_name}')]"):
                try:
                    if el.is_displayed():
                        el.click()
                        time.sleep(5)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                log(f"📂 '{folder_name}' 폴더를 열었습니다")
            else:
                log(f"⚠️ '{folder_name}' 폴더를 찾지 못했습니다. 전체 목록에서 수집합니다.")

        notes = _collect_note_links(driver)
        if notes:
            break
    return notes

def fetch_note_content(browser: Browser, note_url: str, log) -> tuple[str, str]:
    """노트 페이지에서 (제목, 본문 텍스트)를 추출한다."""
    from selenium.webdriver.common.by import By

    driver = browser.get_driver()
    driver.get(note_url)
    time.sleep(6)

    title = ""
    try:
        title = driver.find_element(By.CSS_SELECTOR, "h1").text.strip()
    except Exception:
        pass

    body = ""
    for css in ("article", "main", "body"):
        try:
            el = driver.find_element(By.CSS_SELECTOR, css)
            body = el.text.strip()
            if len(body) > 200:
                break
        except Exception:
            continue

    return title, body

# ──────────────────────────────────────────────
# 백그라운드 워커
# ──────────────────────────────────────────────
class Worker:
    """유튜브 단건 처리 / 라이브러리 감시 루프를 담당."""

    def __init__(self, log_fn):
        self.log = log_fn
        self.browser: Browser | None = None
        self._watching = False
        self._watch_thread = None
        self._busy = threading.Lock()

    def _get_browser(self, cfg) -> Browser:
        if self.browser is None or self.browser.profile_dir != cfg["chrome_profile_dir"]:
            if self.browser:
                self.browser.quit()
            self.browser = Browser(cfg["chrome_profile_dir"], self.log)
        return self.browser

    # ── 로그인용 브라우저 ──
    def open_login_browser(self, cfg):
        def _run():
            try:
                browser = self._get_browser(cfg)
                driver = browser.get_driver()
                driver.get("https://nid.naver.com/nidlogin.login")
                driver.execute_script("window.open('https://lilys.ai', '_blank');")
                self.log("🔑 브라우저가 열렸습니다. 네이버와 Lilys AI에 로그인해 주세요.")
                self.log("   로그인 후 창을 닫지 말고 그대로 두면 세션이 프로필에 저장됩니다.")
            except Exception as e:
                self.log(f"❌ 브라우저 실행 실패: {e}")
        threading.Thread(target=_run, daemon=True).start()

    # ── 방식 0: Lilys 노트 링크 → 블로그 (API 불필요) ──
    def post_note_link(self, cfg, note_url: str):
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                self.log(f"▶ Lilys 노트 가져오기: {note_url}")
                browser = self._get_browser(cfg)
                title, body = fetch_note_content(browser, note_url, self.log)
                if not body or len(body) < 100:
                    self.log("❌ 노트 본문을 가져오지 못했습니다. Lilys 로그인 상태와 링크를 확인해 주세요.")
                    return
                if not title:
                    title = body.strip().splitlines()[0][:80]
                body = markdown_to_plain(body) + f"\n\n원본 노트: {note_url}"
                self.log(f"📄 노트 내용 추출 완료: {title}")

                ok = post_to_naver_blog(
                    browser, title, body, cfg["publish_mode"], self.log)
                if ok:
                    self.log("✅ 블로그 포스팅 완료")
            except Exception as e:
                self.log(f"❌ 오류 발생: {e}")
            finally:
                self._busy.release()
        threading.Thread(target=_run, daemon=True).start()

    # ── 라이브러리 목록 불러오기 / 선택 발행 ──
    def fetch_notes_async(self, cfg, on_done):
        """라이브러리의 (URL, 제목) 목록을 가져와 on_done(notes) 콜백으로 전달한다."""
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                self.log("📥 라이브러리 목록을 불러오는 중...")
                browser = self._get_browser(cfg)
                notes = fetch_collection_notes(
                    browser, self.log, cfg.get("lilys_folder_name", ""))
                if notes:
                    self.log(f"🔍 라이브러리에서 노트 {len(notes)}개를 찾았습니다")
                    on_done(notes)
                else:
                    self.log("⚠️ 라이브러리에서 노트를 찾지 못했습니다. Lilys 로그인 상태를 확인해 주세요.")
            except Exception as e:
                self.log(f"❌ 라이브러리 불러오기 실패: {e}")
            finally:
                self._busy.release()
        threading.Thread(target=_run, daemon=True).start()

    def post_selected_notes(self, cfg, notes: list):
        """선택한 노트들을 순서대로 블로그에 발행한다."""
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                posted = load_posted()
                browser = self._get_browser(cfg)
                for url, list_title in notes:
                    self.log(f"▶ 노트 발행 시작: {list_title}")
                    title, body = fetch_note_content(browser, url, self.log)
                    if not body or len(body) < 100:
                        self.log(f"⚠️ 본문 추출 실패, 건너뜁니다: {list_title}")
                        continue
                    title = title or list_title
                    body = markdown_to_plain(body) + f"\n\n원본 노트: {url}"
                    ok = post_to_naver_blog(
                        browser, title, body, cfg["publish_mode"], self.log)
                    if ok:
                        posted.add(url)
                        save_posted(posted)
                        self.log(f"✅ 블로그 포스팅 완료: {title}")
                    time.sleep(3)
                self.log("🏁 선택한 노트 발행 작업이 끝났습니다")
            except Exception as e:
                self.log(f"❌ 오류 발생: {e}")
            finally:
                self._busy.release()
        threading.Thread(target=_run, daemon=True).start()

    # ── 방식 1: 유튜브 링크 → Lilys API → 블로그 ──
    def summarize_and_post(self, cfg, youtube_url: str):
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                self.log(f"▶ 유튜브 요약 시작: {youtube_url}")
                request_id = lilys_request_summary(
                    cfg["lilys_api_key"], youtube_url,
                    cfg["model_type"], cfg["result_language"])
                self.log(f"📨 요약 요청 완료 (requestId: {request_id})")

                title, body = lilys_poll_result(
                    cfg["lilys_api_key"], request_id, self.log)
                if not title:
                    title = body.strip().splitlines()[0][:80]
                body = markdown_to_plain(body)
                body += f"\n\n출처 영상: {youtube_url}"
                self.log(f"📄 요약 완료: {title}")

                browser = self._get_browser(cfg)
                ok = post_to_naver_blog(
                    browser, title, body, cfg["publish_mode"], self.log)
                if ok:
                    self.log("✅ 블로그 포스팅 완료")
            except Exception as e:
                self.log(f"❌ 오류 발생: {e}")
            finally:
                self._busy.release()
        threading.Thread(target=_run, daemon=True).start()

    # ── 방식 2: 라이브러리 감시 ──
    def start_watching(self, cfg):
        if self._watching:
            return
        self._watching = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(cfg,), daemon=True)
        self._watch_thread.start()

    def stop_watching(self):
        self._watching = False

    def _watch_loop(self, cfg):
        posted = load_posted()
        interval = max(int(cfg.get("check_interval_minutes", 30)), 5) * 60
        first_scan = len(posted) == 0

        self.log("▶ 라이브러리 감시 시작")
        while self._watching:
            if self._busy.acquire(blocking=False):
                try:
                    browser = self._get_browser(cfg)
                    notes = fetch_collection_notes(
                        browser, self.log, cfg.get("lilys_folder_name", ""))
                    self.log(f"🔍 라이브러리 노트 {len(notes)}개 확인")

                    if first_scan and notes:
                        # 최초 실행 시 기존 노트는 발행하지 않고 '본 것'으로만 기록
                        for url, _ in notes:
                            posted.add(url)
                        save_posted(posted)
                        first_scan = False
                        self.log(f"ℹ️ 기존 노트 {len(notes)}개는 건너뜁니다. 이후 새로 추가되는 노트만 발행합니다.")
                    else:
                        for url, list_title in notes:
                            if not self._watching:
                                break
                            if url in posted:
                                continue
                            self.log(f"🆕 새 노트 발견: {list_title}")
                            title, body = fetch_note_content(browser, url, self.log)
                            if not body or len(body) < 100:
                                self.log("⚠️ 본문 추출 실패, 다음 주기에 다시 시도합니다.")
                                continue
                            title = title or list_title
                            body = markdown_to_plain(body) + f"\n\n원본 노트: {url}"
                            ok = post_to_naver_blog(
                                browser, title, body, cfg["publish_mode"], self.log)
                            if ok:
                                posted.add(url)
                                save_posted(posted)
                                self.log(f"✅ 블로그 포스팅 완료: {title}")
                except Exception as e:
                    self.log(f"❌ 감시 중 오류: {e}")
                finally:
                    self._busy.release()
            else:
                self.log("⚠️ 다른 작업이 진행 중이라 이번 주기를 건너뜁니다.")

            for _ in range(interval):
                if not self._watching:
                    break
                time.sleep(1)

        self.log("⏹ 라이브러리 감시 중지")

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
        self.title("Lilys AI → 네이버 블로그 자동 포스팅")
        self.geometry("720x680")
        self.resizable(False, False)
        self.configure(bg=BG)

        self._worker = Worker(log_fn=self._append_log)
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="Lilys AI → 네이버 블로그 자동 포스팅",
                 bg=BG, fg=FG, font=FONT_T).pack(pady=(16, 4))
        tk.Label(self, text="유튜브 링크를 요약하거나, Lilys 라이브러리의 새 노트를 감지해 블로그에 자동 발행합니다",
                 bg=BG, fg=FG_DIM, font=FONT_M).pack(pady=(0, 12))

        # 설정 카드
        card = tk.Frame(self, bg=SURFACE)
        card.pack(fill="x", padx=24, pady=4)

        self._cfg_vars = {}
        fields = [
            ("lilys_api_key",          "Lilys API Key (선택, 유튜브 직접 요약용)", True),
            ("model_type",             "요약 모델 (gpt-3.5 / gpt-4)", False),
            ("result_language",        "요약 언어 (ko / en)",   False),
            ("check_interval_minutes", "라이브러리 체크 주기 (분)", False),
            ("lilys_folder_name",      "라이브러리 폴더 이름 (비우면 전체)", False),
            ("chrome_profile_dir",     "크롬 프로필 폴더",      False),
            ("publish_mode",           "발행 방식 (publish / draft)", False),
        ]
        for i, (key, label, secret) in enumerate(fields):
            tk.Label(card, text=label, bg=SURFACE, fg=FG_DIM,
                     font=FONT_M, width=26, anchor="w").grid(
                row=i, column=0, padx=(12, 4), pady=4, sticky="w")
            var = tk.StringVar()
            self._cfg_vars[key] = var
            tk.Entry(card, textvariable=var, show="*" if secret else "",
                     bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", font=FONT_M, width=40).grid(
                row=i, column=1, padx=(4, 12), pady=4)

        self._load_cfg_to_ui()

        # 유튜브 링크 입력 행
        yt_frame = tk.Frame(self, bg=BG)
        yt_frame.pack(fill="x", padx=24, pady=(12, 4))
        tk.Label(yt_frame, text="유튜브 링크 또는 Lilys 노트 링크", bg=BG, fg=FG_DIM,
                 font=FONT_M).pack(side="left", padx=(0, 8))
        self._yt_var = tk.StringVar()
        tk.Entry(yt_frame, textvariable=self._yt_var,
                 bg=SURFACE, fg=FG, insertbackground=FG,
                 relief="flat", font=FONT_M).pack(
            side="left", fill="x", expand=True, ipady=4)

        # 버튼 행
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="🔑 로그인용 브라우저 열기", font=FONT_B,
                  bg="#0f766e", fg="white", activebackground="#0d6060",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_open_login).pack(side="left", padx=5)

        tk.Button(btn_frame, text="▶ 요약 → 블로그 발행", font=FONT_B,
                  bg=ACCENT, fg="white", activebackground=ACCENT_H,
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_summarize).pack(side="left", padx=5)

        tk.Button(btn_frame, text="📥 라이브러리에서 골라 발행", font=FONT_B,
                  bg="#1d4ed8", fg="white", activebackground="#1e40af",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_pick_from_library).pack(side="left", padx=5)

        self._btn_watch = tk.Button(
            btn_frame, text="👀 라이브러리 감시 시작", font=FONT_B,
            bg="#b45309", fg="white", activebackground="#92400e",
            activeforeground="white", relief="flat",
            padx=12, pady=6, cursor="hand2",
            command=self._on_toggle_watch)
        self._btn_watch.pack(side="left", padx=5)

        tk.Button(btn_frame, text="💾 설정 저장", font=FONT_B,
                  bg="#475569", fg="white", activebackground="#334155",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._save_cfg).pack(side="left", padx=5)

        # 로그창
        tk.Label(self, text="로그", bg=BG, fg=FG_DIM, font=FONT_M,
                 anchor="w").pack(fill="x", padx=28)
        self._log_box = scrolledtext.ScrolledText(
            self, state="disabled", bg="#0f0f1a", fg=FG,
            font=("Consolas", 9), relief="flat",
            wrap="word", height=14)
        self._log_box.pack(fill="both", expand=True, padx=24, pady=(2, 16))
        self._log_box.tag_config("ok",   foreground=SUCCESS)
        self._log_box.tag_config("err",  foreground=ERROR)
        self._log_box.tag_config("warn", foreground=WARN)
        self._log_box.tag_config("info", foreground=FG_DIM)

    # ── 설정 ─────────────────────────────────
    def _load_cfg_to_ui(self):
        cfg = load_config()
        for k, var in self._cfg_vars.items():
            var.set(str(cfg.get(k, "")))

    def _save_cfg(self) -> dict:
        cfg = load_config()
        for k, var in self._cfg_vars.items():
            val = var.get().strip()
            cfg[k] = int(val) if k == "check_interval_minutes" and val.isdigit() else val
        save_config(cfg)
        self._append_log("💾 설정이 저장되었습니다")
        return cfg

    # ── 버튼 핸들러 ──────────────────────────
    def _on_open_login(self):
        cfg = self._save_cfg()
        self._worker.open_login_browser(cfg)

    def _on_summarize(self):
        cfg = self._save_cfg()
        url = self._yt_var.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "유튜브 링크 또는 Lilys 노트 링크를 입력해 주세요.")
            return
        if "lilys.ai" in url:
            # Lilys 노트 링크 → API 없이 브라우저로 내용을 가져와 발행
            self._worker.post_note_link(cfg, url)
        elif "youtu" in url:
            if not cfg.get("lilys_api_key"):
                messagebox.showwarning(
                    "API Key 필요",
                    "유튜브 링크를 직접 요약하려면 Lilys API Key가 필요합니다.\n\n"
                    "API 없이 쓰시려면:\n"
                    "1) Lilys 앱에서 영상을 요약한 뒤 노트 링크를 여기에 붙여넣거나\n"
                    "2) [라이브러리 감시 시작]을 켜 두세요.")
                return
            self._worker.summarize_and_post(cfg, url)
        else:
            messagebox.showwarning("입력 확인", "유튜브 링크 또는 lilys.ai 노트 링크만 지원합니다.")

    def _on_pick_from_library(self):
        cfg = self._save_cfg()
        self._worker.fetch_notes_async(
            cfg, lambda notes: self.after(0, self._show_note_picker, cfg, notes))

    def _show_note_picker(self, cfg, notes):
        """라이브러리 노트 목록에서 발행할 노트를 고르는 창."""
        win = tk.Toplevel(self)
        win.title("라이브러리에서 발행할 노트 선택")
        win.geometry("560x460")
        win.configure(bg=BG)

        posted = load_posted()
        tk.Label(win, text="발행할 노트를 선택하세요 (Ctrl/Shift 클릭으로 여러 개 선택 가능)",
                 bg=BG, fg=FG_DIM, font=FONT_M).pack(pady=(12, 6))

        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True, padx=16)
        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(
            frame, selectmode="extended", font=FONT_M,
            bg=SURFACE, fg=FG, selectbackground=ACCENT,
            relief="flat", yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        for url, title in notes:
            mark = "✅ " if url in posted else ""
            listbox.insert("end", f"{mark}{title}")

        def _publish():
            selected = [notes[i] for i in listbox.curselection()]
            if not selected:
                messagebox.showwarning("선택 필요", "발행할 노트를 선택해 주세요.", parent=win)
                return
            win.destroy()
            self._worker.post_selected_notes(cfg, selected)

        btns = tk.Frame(win, bg=BG)
        btns.pack(pady=12)
        tk.Button(btns, text="🚀 선택한 노트 발행", font=FONT_B,
                  bg=ACCENT, fg="white", activebackground=ACCENT_H,
                  activeforeground="white", relief="flat",
                  padx=16, pady=6, cursor="hand2",
                  command=_publish).pack(side="left", padx=6)
        tk.Button(btns, text="닫기", font=FONT_B,
                  bg="#475569", fg="white", activebackground="#334155",
                  activeforeground="white", relief="flat",
                  padx=16, pady=6, cursor="hand2",
                  command=win.destroy).pack(side="left", padx=6)

    def _on_toggle_watch(self):
        if self._worker._watching:
            self._worker.stop_watching()
            self._btn_watch.config(text="👀 라이브러리 감시 시작", bg="#b45309")
        else:
            cfg = self._save_cfg()
            self._worker.start_watching(cfg)
            self._btn_watch.config(text="⏹ 라이브러리 감시 중지", bg="#7f1d1d")

    # ── 로그 ─────────────────────────────────
    def _append_log(self, msg: str):
        def _do():
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            tag = "ok" if "✅" in msg or "🚀" in msg else \
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
