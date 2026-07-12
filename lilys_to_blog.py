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
    "naver_id": "",
    "naver_pw": "",
    "lilys_api_key": "",
    "model_type": "gpt-4",
    "result_language": "ko",
    "check_interval_minutes": 30,
    "lilys_folder_name": "",
    "max_fetch_count": 10,
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

def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """설정값을 안전하게 정수로 읽는다."""
    try:
        n = int(str(cfg.get(key, default)).strip())
        return n if n > 0 else default
    except Exception:
        return default

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
        self._launch_lock = threading.Lock()

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
        with self._launch_lock:  # 동시에 두 개가 뜨지 않도록
            return self._get_driver_locked()

    def _get_driver_locked(self):
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

def safe_hotkey(driver, *keys):
    """단축키 입력 (ActionChains 전용, 스레드 안전)."""
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    key_map = {"ctrl": Keys.CONTROL, "shift": Keys.SHIFT, "alt": Keys.ALT,
               "enter": Keys.ENTER}
    actions = ActionChains(driver)
    for k in keys[:-1]:
        actions = actions.key_down(key_map.get(k, k))
    actions = actions.send_keys(key_map.get(keys[-1], keys[-1]))
    for k in keys[:-1]:
        actions = actions.key_up(key_map.get(k, k))
    actions.perform()

def safe_press(driver, key):
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    key_map = {"enter": Keys.ENTER, "tab": Keys.TAB, "escape": Keys.ESCAPE}
    ActionChains(driver).send_keys(key_map.get(key, key)).perform()

def paste_text(driver, element, text: str, clear: bool = False):
    """클립보드 붙여넣기로 입력한다 (이모지 등 non-BMP 문자, 보안 입력 대응)."""
    import pyperclip

    try:
        element.click()
    except Exception:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(element).click().perform()
    time.sleep(0.3)
    if clear:
        safe_hotkey(driver, "ctrl", "a")
        time.sleep(0.1)
    pyperclip.copy(text)
    safe_hotkey(driver, "ctrl", "v")
    time.sleep(0.4)

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

def _dismiss_editor_popups(driver):
    """작성 중이던 글 팝업 / 도움말 패널 등을 닫는다."""
    for css in ("button.se-popup-button-cancel", ".se-popup-button-cancel",
                "button.se-cancel", "button.se-help-panel-close-button"):
        _click_if_exists(driver, css)

def ensure_naver_login(driver, cfg, log) -> bool:
    """
    네이버 로그인 상태를 확인하고, 필요하면 설정의 ID/PW로 자동 로그인한다.
    자동 로그인이 실패하면 60초간 수동 로그인을 기다린다.
    """
    from selenium.webdriver.common.by import By

    # 이미 로그인 상태인지 쿠키로 확인
    try:
        driver.get("https://www.naver.com")
        time.sleep(2)
        if any(c["name"] in ("NID_AUT", "NID_SES") for c in driver.get_cookies()):
            log("✅ 네이버 로그인 상태 확인됨")
            return True
    except Exception:
        pass

    driver.get("https://nid.naver.com/nidlogin.login")
    time.sleep(2)

    nid = (cfg.get("naver_id") or "").strip()
    npw = (cfg.get("naver_pw") or "").strip()
    if nid and npw:
        try:
            id_el = driver.find_element(By.ID, "id")
            driver.execute_script("arguments[0].value = '';", id_el)
            paste_text(driver, id_el, nid, clear=True)
            pw_el = driver.find_element(By.ID, "pw")
            driver.execute_script("arguments[0].value = '';", pw_el)
            paste_text(driver, pw_el, npw, clear=True)
            # 로그인 상태유지 체크
            try:
                keep = driver.find_element(By.ID, "keep")
                if not keep.is_selected():
                    keep.click()
                    time.sleep(0.3)
            except Exception:
                _click_if_exists(driver, 'label[for="keep"], span.keep_text, .ip_check')
            driver.find_element(By.ID, "log.login").click()
            time.sleep(5)
        except Exception as e:
            log(f"⚠️ 자동 로그인 시도 실패: {e}")
    else:
        log("ℹ️ 설정에 네이버 ID/PW가 없어 수동 로그인을 기다립니다")

    # 새 기기 등록 확인 화면이 나오면 '등록안함' 클릭
    try:
        for el in driver.find_elements(
                By.XPATH, "//*[contains(text(), '등록안함') or contains(text(), '등록 안함')]"):
            if el.is_displayed():
                el.click()
                time.sleep(3)
                break
    except Exception:
        pass

    if "nid.naver.com" not in driver.current_url:
        log("✅ 네이버 로그인 성공")
        return True

    # 캡차가 뜬 경우 안내
    try:
        if driver.find_elements(By.CSS_SELECTOR,
                                "#captcha, img#captchaimg, #rcapt, .captcha"):
            log("⚠️ 캡차(보안문자)가 나타났습니다 — 크롬 창에서 캡차를 입력하고 직접 로그인해 주세요")
    except Exception:
        pass

    # 실패 시 120초간 수동 로그인 대기 (캡차/2단계 인증 대응)
    log("⚠️ 자동 로그인 미완료 — 열린 크롬 창에서 120초 안에 직접 로그인해 주세요...")
    for remaining in range(120, 0, -5):
        time.sleep(5)
        if "nid.naver.com" not in driver.current_url:
            log("✅ 수동 로그인 확인됨!")
            return True
        if remaining % 15 == 0:
            log(f"⏳ 수동 로그인 대기 중... {remaining}초 남음")

    log("❌ 시간 내에 로그인되지 않았습니다")
    return False

def post_to_naver_blog(browser: Browser, cfg: dict, title: str, content: str,
                       log) -> bool:
    """네이버 블로그 스마트에디터 ONE 에 글을 작성하고 발행/임시저장한다."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = browser.get_driver()

    if not ensure_naver_login(driver, cfg, log):
        return False

    log("🌐 네이버 블로그 글쓰기 페이지 이동 중...")
    driver.get("https://blog.naver.com/GoBlogWrite.naver")
    time.sleep(5)

    # 새 탭이 열렸으면 에디터 탭만 남기기
    handles = list(driver.window_handles)
    if len(handles) > 1:
        editor_tab = handles[-1]
        for h in handles:
            if h != editor_tab:
                try:
                    driver.switch_to.window(h)
                    driver.close()
                except Exception:
                    pass
        driver.switch_to.window(editor_tab)

    # 글쓰기 화면은 mainFrame iframe 안에 있음 (없는 환경도 있음)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "mainFrame")))
        driver.switch_to.frame(iframe)
        time.sleep(1)
    except Exception:
        log("ℹ️ mainFrame 없음, 에디터에 직접 접근합니다")

    _dismiss_editor_popups(driver)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'div.se-title-text, div[contenteditable="true"]')))
    except Exception:
        log("⚠️ 에디터 로드가 늦습니다 (계속 시도)")

    # ── 제목 입력 (여러 셀렉터 순차 시도) ──
    title_selectors = [
        "span.se-placeholder",
        'div[data-name="title"] div[contenteditable="true"]',
        "div.se-section-title div.se-text-paragraph",
        ".se-section-documentTitle .se-text-paragraph",
        "div.se-title-text",
    ]
    title_ok = False
    for sel in title_selectors:
        try:
            el = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            paste_text(driver, el, title, clear=True)
            title_ok = True
            break
        except Exception:
            continue
    if not title_ok:
        log("❌ 제목 입력 실패 (에디터 구조가 변경되었을 수 있음)")
        driver.switch_to.default_content()
        return False
    log(f"✏️ 제목 입력 완료: {title[:30]}")

    # ── 본문 포커스 ──
    body_selectors = [
        'div.se-section-text div[contenteditable="true"]',
        'div.se-component-content div[contenteditable="true"]',
        "div.se-text-paragraph",
        'div[contenteditable="true"]',
    ]
    focused = False
    for sel in body_selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    try:
                        driver.execute_script("arguments[0].click();", el)
                    except Exception:
                        el.click()
                    time.sleep(0.3)
                    focused = True
                    break
        except Exception:
            continue
        if focused:
            break
    if not focused:
        try:
            safe_press(driver, "tab")
            time.sleep(0.3)
            focused = True
        except Exception:
            pass
    if not focused:
        log("❌ 본문 영역 포커스 실패")
        driver.switch_to.default_content()
        return False

    # ── 본문 입력 (한 줄씩 붙여넣기 → 문단 유지) ──
    import pyperclip
    wrote_any = False
    for raw_line in content.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            safe_press(driver, "enter")
            time.sleep(0.05)
            continue
        pyperclip.copy(line)
        safe_hotkey(driver, "ctrl", "v")
        safe_press(driver, "enter")
        time.sleep(0.1)
        wrote_any = True
    if not wrote_any:
        log("❌ 본문 내용이 비어 있습니다")
        driver.switch_to.default_content()
        return False
    log("✏️ 본문 입력 완료")

    time.sleep(1)

    # ── 발행 / 임시저장 ──
    def _click_first(selectors, timeout=3):
        for sel in selectors:
            try:
                btn = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                driver.execute_script("arguments[0].click();", btn)
                return True
            except Exception:
                continue
        return False

    try:
        if cfg.get("publish_mode") == "draft":
            clicked = _click_first([
                'button[data-testid="save-btn"]',
                "button.save_btn__Y5f57",
                "button.save_btn",
                'button[class*="save"]',
            ])
            if not clicked:
                try:
                    btn = driver.find_element(
                        By.XPATH, '//button[contains(., "임시저장")]')
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                raise RuntimeError("임시저장 버튼을 찾지 못했습니다")
            time.sleep(2)
            log("💾 임시저장 완료")
        else:
            if not _click_first([
                'button[data-testid="publish-btn"]',
                "button.publish_btn__Y5f57",
                "button.publish_btn",
                'button[class*="publish"]',
            ]):
                raise RuntimeError("발행 버튼을 찾지 못했습니다")
            time.sleep(2)

            confirm_selectors = [
                "button.se-popup-button-confirm",
                "button.confirm_btn__WEaBq",
                "button.confirm_btn",
                'button[class*="confirm"]',
            ]
            if not _click_first(confirm_selectors):
                log("ℹ️ 발행 확인 팝업이 없어 바로 완료 여부를 확인합니다")

            time.sleep(4)
            if "goblogwrite" in (driver.current_url or "").lower():
                log("⚠️ 에디터에 머물러 있어 발행을 재시도합니다...")
                _click_first(confirm_selectors)
                time.sleep(3)
            log("🚀 발행 완료!")
    except Exception as e:
        log(f"❌ 발행/저장 실패: {e}")
        driver.switch_to.default_content()
        return False

    driver.switch_to.default_content()
    return True

# ──────────────────────────────────────────────
# Lilys 라이브러리(콜렉션) 감시
# ──────────────────────────────────────────────
# 노트로 보이는 링크 경로 패턴 (넓게 잡고 메뉴성 경로는 제외)
_NOTE_HREF_PAT = re.compile(r"/(notes?|digest|summar\w*|videos?|contents?)(/|\?)", re.I)
_EXCLUDE_PATHS = ("/library", "/collections", "/pricing", "/api", "/signin",
                  "/login", "/subscribe", "/now", "/highlight", "/home")

def _collect_note_links(driver) -> list[tuple[str, str]]:
    """현재 페이지에서 (노트URL, 제목) 링크들을 수집한다."""
    from selenium.webdriver.common.by import By

    notes, seen = [], set()
    for a in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = a.get_attribute("href") or ""
            if not href or "lilys.ai" not in href or href in seen:
                continue
            path = href.split("lilys.ai", 1)[1]
            if not _NOTE_HREF_PAT.search(path):
                continue
            if any(path.rstrip("/").endswith(p) for p in _EXCLUDE_PATHS):
                continue
            lines = [ln.strip() for ln in (a.text or "").splitlines() if ln.strip()]
            title = max(lines, key=len) if lines else ""
            if len(title) < 5:
                continue
            seen.add(href)
            notes.append((href, title))
        except Exception:
            continue
    return notes

def _dump_debug(driver, log):
    """노트를 못 찾았을 때 화면 구조를 파일로 저장해 원인 분석을 돕는다."""
    from selenium.webdriver.common.by import By
    try:
        lines = [f"URL: {driver.current_url}", f"TITLE: {driver.title}", "", "[페이지의 모든 링크]"]
        seen = set()
        for a in driver.find_elements(By.TAG_NAME, "a"):
            try:
                href = a.get_attribute("href") or ""
                text = (a.text or "").strip().replace("\n", " / ")[:80]
                if href and href not in seen:
                    seen.add(href)
                    lines.append(f"{href}  |  {text}")
            except Exception:
                continue
        lines += ["", "[클릭 가능한 카드 후보]"]
        try:
            for t in _mark_cards(driver):
                lines.append("- " + t.replace("\n", " / ")[:120])
        except Exception:
            pass
        lines += ["", "[화면 텍스트 앞부분]"]
        try:
            body_text = driver.execute_script("return document.body.innerText") or ""
            lines.append(body_text[:3000])
        except Exception:
            pass
        txt_path = os.path.join(BASE_DIR, "lilys_debug.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        with open(os.path.join(BASE_DIR, "lilys_debug.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log(f"🛠 진단 파일을 저장했습니다: {txt_path}")
        log("   (노트를 계속 못 찾으면 이 파일 내용을 보여주세요. 화면 구조에 맞춰 수정할 수 있습니다)")
    except Exception as e:
        log(f"⚠️ 진단 파일 저장 실패: {e}")

# 카드(클릭으로 열리는 노트) 탐지용 JS:
# 마우스 커서가 pointer 이고 적당한 길이의 텍스트를 가진 '가장 바깥' 요소를 찾아
# data-lilys-card 번호를 붙이고 텍스트 목록을 반환한다.
_MARK_CARDS_JS = """
const cands = [...document.querySelectorAll('div, li, article, section')].filter(el => {
    if (!el.offsetParent) return false;
    const t = (el.innerText || '').trim();
    if (t.length < 20 || t.length > 400) return false;
    if (getComputedStyle(el).cursor !== 'pointer') return false;
    return true;
});
const outer = cands.filter(el => !cands.some(o => o !== el && o.contains(el)));
document.querySelectorAll('[data-lilys-card]').forEach(el => el.removeAttribute('data-lilys-card'));
outer.forEach((el, i) => el.setAttribute('data-lilys-card', i));
return outer.map(el => (el.innerText || '').trim());
"""

def _mark_cards(driver) -> list[str]:
    return driver.execute_script(_MARK_CARDS_JS) or []

def _looks_like_note_card(text: str) -> bool:
    """카드 텍스트가 노트(요약 글)로 보이는지 판별한다."""
    # 날짜(2026.07.12 형태)나 '유튜브' 표기가 있는, 어느 정도 긴 텍스트만 노트로 취급
    return bool(re.search(r"20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}", text)
                or "유튜브" in text or "YouTube" in text.lower())

def _click_collect_notes(driver, log, max_notes: int = 30) -> list[tuple[str, str]]:
    """
    노트 카드가 링크(<a>)가 아닌 화면에서, 카드를 하나씩 클릭해
    이동한 주소를 수집하고 뒤로가기로 돌아온다.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    collected, done_titles = [], set()
    base_url = driver.current_url
    card_texts = _mark_cards(driver)
    note_idxs = [i for i, t in enumerate(card_texts) if _looks_like_note_card(t)]
    if not note_idxs:
        return []
    log(f"🃏 노트 카드 {len(note_idxs)}개를 발견했습니다. 하나씩 열어 주소를 수집합니다...")

    for n, _ in enumerate(note_idxs[:max_notes]):
        # 목록 화면으로 돌아올 때마다 DOM이 새로 그려지므로 카드를 다시 표시
        texts_now = _mark_cards(driver)
        idxs_now = [i for i, t in enumerate(texts_now)
                    if _looks_like_note_card(t) and t not in done_titles]
        if not idxs_now:
            break
        idx = idxs_now[0]
        card_text = texts_now[idx]
        done_titles.add(card_text)
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[data-lilys-card='{idx}']")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.5)
            el.click()
        except Exception:
            continue

        # 주소가 바뀔 때까지 대기
        new_url = None
        deadline = time.time() + 8
        while time.time() < deadline:
            if driver.current_url != base_url:
                new_url = driver.current_url
                break
            time.sleep(0.5)

        if new_url:
            lines = [ln.strip() for ln in card_text.splitlines() if ln.strip()]
            title = max(lines, key=len) if lines else card_text[:60]
            collected.append((new_url, title))
            driver.back()
            time.sleep(3)
            # 뒤로가기 후 목록 화면이 아니면 다시 이동
            if driver.current_url != base_url:
                driver.get(base_url)
                time.sleep(4)
        else:
            # 모달이 열렸을 수 있으니 ESC로 닫기
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(1)
            except Exception:
                pass
    return collected

def _wait_and_collect(driver, timeout_sec: int = 20) -> list[tuple[str, str]]:
    """페이지 로딩/무한스크롤을 고려해 노트 링크가 나올 때까지 기다리며 수집한다."""
    deadline = time.time() + timeout_sec
    notes = []
    while time.time() < deadline:
        notes = _collect_note_links(driver)
        if notes:
            break
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        time.sleep(2)
    return notes

def fetch_collection_notes(browser: Browser, log,
                           folder_name: str = "",
                           max_notes: int = 30) -> list[tuple[str, str]]:
    """
    라이브러리(또는 보관함) 페이지에서 (노트URL, 제목) 목록을 수집한다.
    folder_name 이 지정되면 사이드바에서 해당 폴더를 클릭한 뒤 수집한다.
    max_notes 개수만큼만 수집한다.
    """
    from selenium.webdriver.common.by import By

    driver = browser.get_driver()
    notes = []
    # 라이브러리 → 보관함 순으로 시도 (Lilys 화면 구성에 따라 다름)
    for url in ("https://lilys.ai/library", "https://lilys.ai/collections", "https://lilys.ai/"):
        driver.get(url)
        time.sleep(6)

        cur = driver.current_url
        if "signin" in cur or "login" in cur or "auth" in cur:
            log("❌ Lilys AI 로그인이 필요합니다. [로그인용 브라우저 열기]로 먼저 로그인해 주세요.")
            return []

        # 홈으로 온 경우 사이드바의 '라이브러리' 메뉴 클릭 시도
        if url.rstrip("/").endswith("lilys.ai"):
            for el in driver.find_elements(
                    By.XPATH, "//*[normalize-space(text())='라이브러리' or normalize-space(text())='Library']"):
                try:
                    if el.is_displayed():
                        el.click()
                        time.sleep(4)
                        break
                except Exception:
                    continue

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

        notes = _wait_and_collect(driver, timeout_sec=10)
        if not notes:
            # 링크가 전혀 없는 화면(클릭 카드 방식)이면 카드를 눌러가며 주소 수집
            notes = _click_collect_notes(driver, log, max_notes=max_notes)
        if notes:
            notes = notes[:max_notes]
            break
        log(f"ℹ️ {driver.current_url} 에서 노트를 찾지 못해 다음 경로를 시도합니다...")

    if not notes:
        _dump_debug(driver, log)
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
                    browser, cfg, title, body, self.log)
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
                    browser, self.log, cfg.get("lilys_folder_name", ""),
                    max_notes=_cfg_int(cfg, "max_fetch_count", 10))
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

    def post_selected_notes(self, cfg, notes: list, on_status=None):
        """선택한 노트들을 순서대로 블로그에 발행한다. on_status(url, 상태) 콜백으로 진행을 알린다."""
        def _set(url, status):
            if on_status:
                try:
                    on_status(url, status)
                except Exception:
                    pass

        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                posted = load_posted()
                browser = self._get_browser(cfg)

                # 발행 전에 네이버 로그인을 먼저 확보 (실패 시 전체 중단)
                driver = browser.get_driver()
                if not ensure_naver_login(driver, cfg, self.log):
                    self.log("❌ 네이버 로그인에 실패해 발행을 중단합니다. 로그인 후 다시 [포스팅 시작]을 눌러주세요.")
                    for url, _t in notes:
                        _set(url, "대기")
                    return

                for url, list_title in notes:
                    self.log(f"▶ 노트 발행 시작: {list_title}")
                    _set(url, "진행중")
                    title, body = fetch_note_content(browser, url, self.log)
                    if not body or len(body) < 100:
                        self.log(f"⚠️ 본문 추출 실패, 건너뜁니다: {list_title}")
                        _set(url, "실패")
                        continue
                    title = title or list_title
                    body = markdown_to_plain(body) + f"\n\n원본 노트: {url}"
                    ok = post_to_naver_blog(
                        browser, cfg, title, body, self.log)
                    if ok:
                        posted.add(url)
                        save_posted(posted)
                        self.log(f"✅ 블로그 포스팅 완료: {title}")
                        _set(url, "완료")
                    else:
                        _set(url, "실패")
                    time.sleep(3)
                self.log("🏁 선택한 노트 발행 작업이 끝났습니다")
            except Exception as e:
                self.log(f"❌ 오류 발생: {e}")
            finally:
                self._busy.release()
        threading.Thread(target=_run, daemon=True).start()

    def preview_note(self, cfg, url: str, fallback_title: str, on_ready):
        """노트 내용을 가져와 on_ready(제목, 본문) 콜백으로 전달한다 (미리보기용)."""
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요.")
                return
            try:
                self.log(f"🔎 미리보기 불러오는 중: {fallback_title}")
                browser = self._get_browser(cfg)
                title, body = fetch_note_content(browser, url, self.log)
                on_ready(title or fallback_title,
                         body or "(본문을 가져오지 못했습니다)")
            except Exception as e:
                self.log(f"❌ 미리보기 실패: {e}")
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
                    browser, cfg, title, body, self.log)
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
                        browser, self.log, cfg.get("lilys_folder_name", ""),
                        max_notes=_cfg_int(cfg, "max_fetch_count", 10))
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
                                browser, cfg, title, body, self.log)
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
        self.geometry("720x740")
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
            ("naver_id",               "네이버 ID (자동 로그인용)", False),
            ("naver_pw",               "네이버 비밀번호",       True),
            ("lilys_api_key",          "Lilys API Key (선택, 유튜브 직접 요약용)", True),
            ("model_type",             "요약 모델 (gpt-3.5 / gpt-4)", False),
            ("result_language",        "요약 언어 (ko / en)",   False),
            ("check_interval_minutes", "라이브러리 체크 주기 (분)", False),
            ("lilys_folder_name",      "라이브러리 폴더 이름 (비우면 전체)", False),
            ("max_fetch_count",        "가져올 노트 개수 (최대)", False),
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
            cfg[k] = int(val) if k in ("check_interval_minutes", "max_fetch_count") and val.isdigit() else val
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
        """라이브러리 노트 목록: 체크로 선택, 미리보기, 상태 표시가 있는 창."""
        from tkinter import ttk

        win = tk.Toplevel(self)
        win.title("라이브러리에서 발행할 노트 선택")
        win.geometry("820x560")
        win.configure(bg=BG)

        posted = load_posted()

        # 상단: 요약/전체선택
        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=16, pady=(12, 6))
        count_var = tk.StringVar(value=f"글감 {len(notes)}개 · 선택 0개")
        tk.Label(top, textvariable=count_var, bg=BG, fg=FG,
                 font=FONT_B).pack(side="left")
        tk.Label(top, text="  (제목 클릭=체크, 🔍=미리보기)", bg=BG, fg=FG_DIM,
                 font=FONT_M).pack(side="left")

        # 표 스타일 (다크)
        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Notes.Treeview", background=SURFACE, foreground=FG,
                        fieldbackground=SURFACE, rowheight=30,
                        font=FONT_M, borderwidth=0)
        style.configure("Notes.Treeview.Heading", background="#1b1b2b",
                        foreground=FG_DIM, font=FONT_B, borderwidth=0)
        style.map("Notes.Treeview", background=[("selected", "#3b2d63")])

        table_frame = tk.Frame(win, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=16)
        cols = ("check", "num", "title", "status", "preview")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                            style="Notes.Treeview", selectmode="none")
        tree.heading("check", text="선택")
        tree.heading("num", text="#")
        tree.heading("title", text="글감 (노트 제목)")
        tree.heading("status", text="상태")
        tree.heading("preview", text="미리보기")
        tree.column("check", width=50, anchor="center", stretch=False)
        tree.column("num", width=40, anchor="center", stretch=False)
        tree.column("title", width=520, anchor="w")
        tree.column("status", width=70, anchor="center", stretch=False)
        tree.column("preview", width=70, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        checked: dict = {}      # iid -> bool
        url_by_iid: dict = {}   # iid -> (url, title)
        for i, (url, title) in enumerate(notes):
            iid = str(i)
            status = "발행됨" if url in posted else "대기"
            tree.insert("", "end", iid=iid,
                        values=("☐", i + 1, title, status, "🔍"))
            checked[iid] = False
            url_by_iid[iid] = (url, title)

        def _update_count():
            n = sum(1 for v in checked.values() if v)
            count_var.set(f"글감 {len(notes)}개 · 선택 {n}개")

        def _toggle(iid):
            checked[iid] = not checked[iid]
            tree.set(iid, "check", "☑" if checked[iid] else "☐")
            _update_count()

        def _show_preview(iid):
            url, title = url_by_iid[iid]

            def _on_ready(t, body):
                def _do():
                    pv = tk.Toplevel(win)
                    pv.title(f"미리보기 — {t[:40]}")
                    pv.geometry("640x560")
                    pv.configure(bg=BG)
                    tk.Label(pv, text=t, bg=BG, fg=FG, font=FONT_B,
                             wraplength=600, justify="left").pack(
                        fill="x", padx=16, pady=(12, 6))
                    box = scrolledtext.ScrolledText(
                        pv, bg=SURFACE, fg=FG, font=FONT_M,
                        relief="flat", wrap="word")
                    box.pack(fill="both", expand=True, padx=16, pady=(0, 12))
                    box.insert("1.0", markdown_to_plain(body))
                    box.config(state="disabled")
                self.after(0, _do)

            self._worker.preview_note(cfg, url, title, _on_ready)

        def _on_click(event):
            iid = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not iid:
                return
            if col == "#5":          # 미리보기
                _show_preview(iid)
            else:                    # 나머지 영역은 체크 토글
                _toggle(iid)

        tree.bind("<Button-1>", _on_click)

        # 하단 버튼
        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", pady=12, padx=16)

        def _select_all():
            all_on = all(checked.values())
            for iid in checked:
                checked[iid] = not all_on
                tree.set(iid, "check", "☑" if checked[iid] else "☐")
            _update_count()

        tk.Button(btns, text="전체 선택/해제", font=FONT_B,
                  bg="#475569", fg="white", activebackground="#334155",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=_select_all).pack(side="left")

        btn_start = tk.Button(
            btns, text="▷ 포스팅 시작", font=FONT_B,
            bg="#16a34a", fg="white", activebackground="#15803d",
            activeforeground="white", relief="flat",
            padx=20, pady=6, cursor="hand2")
        btn_start.pack(side="right")

        def _on_status(url, status):
            def _do():
                for iid, (u, _) in url_by_iid.items():
                    if u == url:
                        try:
                            tree.set(iid, "status", status)
                        except Exception:
                            pass
                        break
                if status in ("완료", "실패"):
                    remaining = any(
                        tree.set(i, "status") == "진행중" for i in url_by_iid)
                    if not remaining:
                        try:
                            btn_start.config(state="normal", text="▷ 포스팅 시작")
                        except Exception:
                            pass
            self.after(0, _do)

        def _start():
            selected = [url_by_iid[iid] for iid, on in checked.items() if on]
            if not selected:
                messagebox.showwarning("선택 필요", "발행할 노트를 체크해 주세요.", parent=win)
                return
            btn_start.config(state="disabled", text="포스팅 중...")
            self._worker.post_selected_notes(cfg, selected, on_status=_on_status)

        btn_start.config(command=_start)

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
