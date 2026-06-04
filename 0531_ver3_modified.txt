import sys
import os
import json
import time
import re
import pyperclip
import requests
import subprocess
import platform
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from PyQt6.QtWidgets import (QApplication, QMainWindow, QStackedWidget, QTreeWidget, QTreeWidgetItem,
                             QTextEdit, QStatusBar, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLineEdit, QLabel, QComboBox, QTextBrowser, QMessageBox,
                             QSpinBox, QFileDialog, QScrollArea, QGroupBox, QCheckBox, QRadioButton,
                             QButtonGroup, QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView,
                             QSplitter, QAbstractItemView,
                             QDialog, QListWidget, QListWidgetItem
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QPalette, QColor, QFont
import feedparser
from bs4 import BeautifulSoup
from openai import OpenAI
import google.genai as genai
from PIL import Image as PILImage
import io
import base64
import urllib.request
# === [PATCH] 멀티링크 → AI 작성 모듈 ===
try:
    from multi_link_ai_page import MultiLinkAIPage, normalize_youtube_url  # noqa: F401
    _MULTILINK_AVAILABLE = True
except Exception as _e:
    print(f'[PATCH] multi_link_ai_page import 실패: {_e}')
    _MULTILINK_AVAILABLE = False

# Selenium global imports (so 'By' is available everywhere; safe if selenium installed)
try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except Exception:
    By = Keys = ActionChains = WebDriverWait = EC = None  # selenium 미설치 환경에서도 import 에러 방지

load_dotenv()
# ═══════════════════════════════════════════════════════════════
#  Chrome 버전 자동 감지
# ═══════════════════════════════════════════════════════════════
def get_chrome_major_version():
    """설치된 Chrome의 메이저 버전을 자동으로 감지. 실패 시 None 반환."""
    try:
        if platform.system() == 'Windows':
            # ── 방법 1: 레지스트리 (가장 안전, 타임아웃 없음) ──
            try:
                import winreg
                reg_paths = [
                    (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Google\Chrome\BLBeacon'),
                    (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon'),
                    (winreg.HKEY_CURRENT_USER,  r'SOFTWARE\Google\Chrome\BLBeacon'),
                ]
                for hive, reg_path in reg_paths:
                    try:
                        key = winreg.OpenKey(hive, reg_path)
                        ver, _ = winreg.QueryValueEx(key, 'version')
                        winreg.CloseKey(key)
                        major = int(str(ver).split('.')[0])
                        print(f"[Chrome 감지] 레지스트리: {ver} → major={major}")
                        return major
                    except Exception:
                        continue
            except Exception:
                pass

            # ── 방법 2: chrome_installer.exe 버전 리소스 읽기 (win32api) ──
            # chrome.exe --version 은 Windows에서 타임아웃/cp949 오류 발생 → 사용 금지
            try:
                import win32api
                chrome_paths = [
                    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
                    os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
                ]
                for cp in chrome_paths:
                    if os.path.exists(cp):
                        info = win32api.GetFileVersionInfo(cp, '\\\\')
                        ms = info['FileVersionMS']
                        major = ms >> 16
                        print(f"[Chrome 감지] win32api: {cp} → major={major}")
                        return major
            except Exception:
                pass

            # ── 방법 3: Last Resort — wmic (인코딩 강제 지정) ──
            try:
                result = subprocess.run(
                    ['wmic', 'datafile', 'where',
                     r'name="C:\Program Files\Google\Chrome\Application\chrome.exe"',
                     'get', 'Version', '/value'],
                    capture_output=True, timeout=8, encoding='utf-8', errors='ignore'
                )
                m = re.search(r'Version=(\d+)', result.stdout)
                if m:
                    major = int(m.group(1))
                    print(f"[Chrome 감지] wmic → major={major}")
                    return major
            except Exception:
                pass

        elif platform.system() == 'Darwin':
            result = subprocess.run(
                ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '--version'],
                capture_output=True, timeout=5, encoding='utf-8', errors='ignore'
            )
            m = re.search(r'(\d+)\.', result.stdout)
            if m:
                return int(m.group(1))
        else:
            result = subprocess.run(
                ['google-chrome', '--version'],
                capture_output=True, timeout=5, encoding='utf-8', errors='ignore'
            )
            m = re.search(r'(\d+)\.', result.stdout)
            if m:
                return int(m.group(1))
    except Exception as e:
        print(f"[Chrome 감지 실패] {e}")
    return None
def kill_chrome_processes():
    """이전에 남은 Chrome / ChromeDriver 프로세스를 모두 종료하고 SingletonLock을 삭제."""
    if platform.system() == 'Windows':
        subprocess.run(['taskkill', '/f', '/im', 'chromedriver.exe'], capture_output=True)
        subprocess.run(['taskkill', '/f', '/im', 'chrome.exe'], capture_output=True)
        time.sleep(2)
    # chrome_profile 폴더의 잠금 파일 삭제
    for lock_name in ['SingletonLock', 'SingletonCookie', 'SingletonSocket']:
        lock_path = Path('./chrome_profile') / lock_name
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            pass

# ============================================================
#  BulkArticleDB — 제목대량업로드 영구 저장 (SQLite)
#  프로그램 종료/재시작에도 제목/원고/상태/발행정보가 유지됩니다.
# ============================================================
import sqlite3 as _sqlite3
from pathlib import Path as _Path
from datetime import datetime as _dt

class BulkArticleDB:
    """data/bulk_articles.db 에 제목 대량 업로드 데이터를 저장."""
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS bulk_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title1 TEXT NOT NULL DEFAULT '',
        title2 TEXT NOT NULL DEFAULT '',
        title3 TEXT NOT NULL DEFAULT '',
        combined_title TEXT NOT NULL DEFAULT '',
        keyword TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '대기',
        publish_status TEXT NOT NULL DEFAULT '',
        char_count INTEGER NOT NULL DEFAULT 0,
        ai_model TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        published_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_bulk_status ON bulk_articles(status);
    CREATE INDEX IF NOT EXISTS idx_bulk_pub    ON bulk_articles(publish_status);
    """

    def __init__(self, db_path=None):
        if db_path is None:
            base = _Path("data"); base.mkdir(exist_ok=True)
            db_path = base / "bulk_articles.db"
        self.db_path = str(db_path)
        with self._conn() as c:
            c.executescript(self._SCHEMA)
            c.commit()

    def _conn(self):
        c = _sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = _sqlite3.Row
        return c

    @staticmethod
    def _now():
        return _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    def insert(self, row: dict) -> int:
        now = self._now()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO bulk_articles
                   (title1,title2,title3,combined_title,keyword,source,content,
                    status,publish_status,char_count,ai_model,created_at,updated_at,published_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row.get("title1",""), row.get("title2",""), row.get("title3",""),
                    row.get("combined_title", row.get("title","")),
                    row.get("keyword",""), row.get("source",""),
                    row.get("content",""),
                    row.get("status","대기"), row.get("publish_status",""),
                    int(row.get("char_count", len(row.get("content","")))),
                    row.get("ai_model",""),
                    now, now, row.get("published_at",""),
                ),
            )
            c.commit()
            return cur.lastrowid

    def update(self, row_id: int, **fields):
        if not fields:
            return
        if "char_count" not in fields and "content" in fields:
            fields["char_count"] = len(fields["content"] or "")
        fields["updated_at"] = self._now()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [row_id]
        with self._conn() as c:
            c.execute(f"UPDATE bulk_articles SET {cols} WHERE id=?", vals)
            c.commit()

    def delete(self, row_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM bulk_articles WHERE id=?", (row_id,))
            c.commit()

    def delete_all(self):
        with self._conn() as c:
            c.execute("DELETE FROM bulk_articles")
            c.commit()

    def all(self) -> list:
        with self._conn() as c:
            cur = c.execute("SELECT * FROM bulk_articles ORDER BY id ASC")
            return [dict(r) for r in cur.fetchall()]
# ============================================================

def make_uc_driver(opts_or_builder):
    """Chrome 버전 자동 감지 + undetected_chromedriver 생성.
    opts_or_builder: ChromeOptions 인스턴스 또는 () -> ChromeOptions 콜러블.
    UC는 ChromeOptions를 한 번 쓰면 재사용 불가 → 콜러블 권장.
    v12: 일부 UC/ChromeDriver 조합에서 excludeSwitches가 거부되므로 생성 직전 제거.
    """
    import undetected_chromedriver as uc

    def _strip_rejected_chrome_options(opts):
        for attr in ("_experimental_options", "experimental_options"):
            data = getattr(opts, attr, None)
            if isinstance(data, dict):
                data.pop("excludeSwitches", None)
        caps = getattr(opts, "_caps", None)
        if isinstance(caps, dict):
            chrome_opts = caps.get("goog:chromeOptions")
            if isinstance(chrome_opts, dict):
                chrome_opts.pop("excludeSwitches", None)
        return opts

    def _build_opts():
        if callable(opts_or_builder):
            return _strip_rejected_chrome_options(opts_or_builder())
        # 인스턴스를 받은 경우: 첫 시도용 그대로 사용 (재시도시 새로 못 만듦 → 경고)
        return _strip_rejected_chrome_options(opts_or_builder)

    kill_chrome_processes()  # 항상 기존 프로세스 정리 후 시작
    version = get_chrome_major_version()
    try:
        opts = _build_opts()
        if version:
            print(f"[ChromeDriver] version_main={version} 으로 실행")
            driver = uc.Chrome(options=opts, version_main=version, use_subprocess=True)
        else:
            print("[ChromeDriver] 버전 감지 실패 → version_main 없이 실행 (자동 매칭)")
            driver = uc.Chrome(options=opts, use_subprocess=True)
        time.sleep(2)
        print(f"[ChromeDriver] 드라이버 생성 성공, 현재 URL: {driver.current_url}")
        return driver
    except Exception as e:
        print(f"[ChromeDriver] 첫 시도 실패: {e}")
        print("[ChromeDriver] chrome_profile 폴더 정리 후 재시도...")
        import shutil
        profile_path = Path('./chrome_profile')
        for lock_name in ['SingletonLock', 'SingletonCookie', 'SingletonSocket']:
            lock_path = profile_path / lock_name
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except Exception:
                pass
        kill_chrome_processes()
        time.sleep(3)
        # 재시도: 반드시 새 opts 인스턴스 생성 (UC 제약)
        if not callable(opts_or_builder):
            print("[ChromeDriver] ⚠️ opts가 콜러블이 아니라 재시도 opts를 새로 만들 수 없습니다. 같은 인스턴스로 재시도하면 'cannot reuse ChromeOptions' 에러 발생 가능.")
        retry_opts = _build_opts()
        if version:
            return uc.Chrome(options=retry_opts, version_main=version, use_subprocess=True)
        else:
            return uc.Chrome(options=retry_opts, use_subprocess=True)
# ═══════════════════════════════════════════════════════════════
#  네이버 로그인 헬퍼 (60초 수동 로그인 대기)
# ═══════════════════════════════════════════════════════════════
def naver_login_with_fallback(driver, nid, npw, log_fn=None):
    """네이버 로그인 시도. 실패 시 60초간 수동 로그인 대기."""
    from selenium.webdriver.common.by import By

    # 이미 로그인 상태인지 먼저 확인
    try:
        driver.get("https://www.naver.com")
        time.sleep(2)
        cookies = driver.get_cookies()
        nid_cookie = any(c['name'] in ('NID_AUT', 'NID_SES') for c in cookies)
        if nid_cookie:
            if log_fn:
                log_fn("✅ 이미 로그인 상태 (쿠키 확인) - 로그인 생략")
            return True
    except Exception:
        pass

    driver.get("https://nid.naver.com/nidlogin.login")
    time.sleep(2)

    try:
        id_el = driver.find_element(By.ID, "id")
        # 기존 입력값 완전히 삭제 (JS로 value 초기화 후 clear)
        driver.execute_script("arguments[0].value = '';", id_el)
        id_el.clear()
        time.sleep(0.2)
        safe_paste(driver, id_el, nid)
        time.sleep(0.3)

        pw_el = driver.find_element(By.ID, "pw")
        # 기존 입력값 완전히 삭제
        driver.execute_script("arguments[0].value = '';", pw_el)
        pw_el.clear()
        time.sleep(0.2)
        safe_paste(driver, pw_el, npw)
        time.sleep(0.3)

        # 로그인 상태유지 체크
        try:
            keep_login = driver.find_element(By.ID, "keep")
            if not keep_login.is_selected():
                keep_login.click()
                time.sleep(0.3)
                if log_fn:
                    log_fn("☑️ 로그인 상태유지 체크 완료")
        except Exception:
            try:
                # 다른 셀렉터로 시도
                keep_label = driver.find_element(By.CSS_SELECTOR, 'label[for="keep"], span.keep_text, .ip_check')
                keep_label.click()
                time.sleep(0.3)
                if log_fn:
                    log_fn("☑️ 로그인 상태유지 체크 완료")
            except Exception:
                if log_fn:
                    log_fn("⚠️ 로그인 상태유지 체크박스를 찾지 못함 (계속 진행)")

        driver.find_element(By.ID, "log.login").click()
        time.sleep(5)
    except Exception as e:
        if log_fn:
            log_fn(f"⚠️ 자동 로그인 실패: {e}")

    # 로그인 성공 여부 확인
    if "nidlogin" not in driver.current_url and "nid.naver.com" not in driver.current_url:
        if log_fn:
            log_fn("✅ 자동 로그인 성공")
        return True

    # 실패 시 60초 수동 대기
    if log_fn:
        log_fn("⚠️ 자동 로그인 실패 - 60초간 수동 로그인을 진행하세요...")
    for remaining in range(60, 0, -5):
        time.sleep(5)
        if "nidlogin" not in driver.current_url and "nid.naver.com" not in driver.current_url:
            if log_fn:
                log_fn("✅ 수동 로그인 확인됨!")
            return True
        if log_fn:
            log_fn(f"⏳ 수동 로그인 대기 중... {remaining}초 남음")

    if log_fn:
        log_fn("❌ 60초 내 로그인되지 않았습니다")
    return False
# ═══════════════════════════════════════════════════════════════
#  워커 스레드
# ═══════════════════════════════════════════════════════════════

class WorkerThread(QThread):
    log_signal = pyqtSignal(str)
    result_signal = pyqtSignal(object)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal()
    ui_update_signal = pyqtSignal(object)  # callable을 메인스레드에서 실행

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
            if not self._is_cancelled:
                if result is not None:
                    self.result_signal.emit(result)
                else:
                    self.result_signal.emit("작업 완료")
        except Exception as e:
            try:
                self.log_signal.emit(f"❌ 오류 발생: {format_error_message(e)}")
            except Exception:
                pass
        finally:
            self.finished_signal.emit()
def format_error_message(err):
    name = err.__class__.__name__
    msg = (str(err) or "알 수 없는 오류").strip()
    detail = ""

    cause = getattr(err, '__cause__', None) or getattr(err, '__context__', None)
    if cause:
        detail = str(cause).strip()

    response = getattr(err, 'response', None)
    if response is not None:
        try:
            if hasattr(response, 'text') and response.text:
                detail = response.text[:250]
        except Exception:
            pass

    if msg.lower() == 'connection error.' and detail:
        return f"{name}: {msg} / {detail}"
    if name.lower() not in msg.lower():
        return f"{name}: {msg}"
    return msg
# pyautogui를 선택적으로 import (없으면 selenium 대체)
try:
    import pyautogui

    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False
def safe_paste(driver, element, text):
    """안전한 붙여넣기 - Selenium ActionChains 전용 (스레드 안전)"""
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    element.click()
    time.sleep(0.2)

    # 기존 입력값 전체 선택 후 삭제 (Ctrl+A → Delete)
    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).perform()
        time.sleep(0.1)
        ActionChains(driver).send_keys(Keys.DELETE).perform()
        time.sleep(0.1)
    except Exception:
        pass

    pyperclip.copy(text)

    # 방법1: ActionChains Ctrl+V (스레드 안전 — pyautogui 사용 안 함)
    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
        time.sleep(0.3)
        return True
    except Exception:
        pass

    # 방법2: JavaScript 직접 입력
    try:
        driver.execute_script("""
            var el = arguments[0];
            el.value = arguments[1];
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """, element, text)
        return True
    except Exception:
        pass

    # 방법3: send_keys 최후 수단
    element.clear()
    element.send_keys(text)
    return True
def safe_hotkey(driver, *keys):
    """단축키 입력 - Selenium ActionChains 전용 (스레드 안전, pyautogui 미사용)"""
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    key_map = {'ctrl': Keys.CONTROL, 'shift': Keys.SHIFT, 'alt': Keys.ALT,
               'enter': Keys.ENTER, 'v': 'v', 's': 's', 'p': 'p', 'q': 'q', 'a': 'a'}
    actions = ActionChains(driver)
    for k in keys[:-1]:
        actions = actions.key_down(key_map.get(k, k))
    actions = actions.send_keys(key_map.get(keys[-1], keys[-1]))
    for k in keys[:-1]:
        actions = actions.key_up(key_map.get(k, k))
    actions.perform()
def safe_press(driver, key):
    """키 입력 - Selenium ActionChains 전용 (스레드 안전, pyautogui 미사용)"""
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    key_map = {'enter': Keys.ENTER, 'tab': Keys.TAB, 'escape': Keys.ESCAPE}
    ActionChains(driver).send_keys(key_map.get(key, key)).perform()


# ═══════════════════════════════════════════════════════════════
#  네이버 에디터 보조 헬퍼 (정렬/인용구/스티커/전처리)  ── v27 개선
#  v26: 정렬은 본문 입력 *후* Ctrl+A 전체선택 + JS fallback / 인용구는 한 줄만 블록 처리
#  - 단축키 대신 SmartEditor 툴바 버튼 클릭 방식으로 변경
#  - 인용구는 Quotation 툴바 버튼 사용
#  - 본문 전처리: 빈 줄 1개로 제한, 긴 문장 보조 개행, 스티커/인용 토큰 정리
# ═══════════════════════════════════════════════════════════════
def _click_toolbar_button(driver, selectors, label=""):
    """툴바 버튼을 클릭. 여러 selector 중 보이는 첫번째를 클릭."""
    from selenium.webdriver.common.by import By
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.25)
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def apply_alignment_js_fallback(driver, alignment):
    """JS로 .se-text-paragraph / .se-component .se-section 의 text-align을 직접 주입.
    툴바 버튼이 안 먹을 때 보장 수단.
    """
    css_value = {'left':'left','center':'center','right':'right','justify':'justify'}.get(alignment, 'left')
    js = """
    (function(align){
      var sels = ['.se-text-paragraph','.se-component .se-section','.se-text','.se-component-content p'];
      var cnt = 0;
      sels.forEach(function(s){
        document.querySelectorAll(s).forEach(function(el){
          el.style.textAlign = align;
          cnt++;
        });
      });
      return cnt;
    })(arguments[0]);
    """
    try:
        n = driver.execute_script(js, css_value)
        return bool(n)
    except Exception:
        return False


def apply_alignment(driver, alignment, select_all_first=True):
    """본문 정렬을 적용. v26 개선:
    1) (옵션) Ctrl+A로 본문 전체 선택
    2) SmartEditor 툴바 정렬 버튼 클릭 시도
    3) 실패 시 JS로 text-align 직접 주입
    alignment: 'left'|'center'|'right'|'justify'
    """
    from selenium.webdriver.common.by import By
    if not alignment:
        return False
    # 1) 본문 전체 선택 (이미 입력된 텍스트에만 정렬 적용 가능)
    if select_all_first:
        try:
            safe_hotkey(driver, 'ctrl', 'a')
            time.sleep(0.2)
        except Exception:
            pass

    # 2) 정렬 토글 메뉴 펼치기 (접혀있을 수 있음)
    _click_toolbar_button(driver, [
        'button.se-align-toolbar-button',
        'button[data-name="align"]',
        'button[aria-label*="정렬"]',
    ])
    time.sleep(0.2)

    label_map = {
        'left': ['왼쪽', '좌측', 'left'],
        'center': ['가운데', '중앙', 'center'],
        'right': ['오른쪽', '우측', 'right'],
        'justify': ['양쪽', '양끝', 'justify'],
    }
    targets = label_map.get(alignment, label_map['left'])

    clicked = False
    try:
        btns = driver.find_elements(By.CSS_SELECTOR,
            'button.se-toolbar-option-align-button, '
            'button[class*="align"][class*="button"], '
            'li.se-toolbar-option-align-button button, '
            'button[data-name*="align"]')
        for b in btns:
            try:
                if not b.is_displayed():
                    continue
                aria = (b.get_attribute('aria-label') or '').lower()
                txt = (b.text or '').strip().lower()
                cls = (b.get_attribute('class') or '').lower()
                dn = (b.get_attribute('data-name') or '').lower()
                if any(t.lower() in aria or t.lower() in txt or t.lower() in cls or t.lower() in dn for t in targets):
                    driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.2)
                    clicked = True
                    break
            except Exception:
                continue
    except Exception:
        pass

    if not clicked:
        cls_map = {'left': 'left', 'center': 'center', 'right': 'right', 'justify': 'justify'}
        cname = cls_map.get(alignment, 'left')
        clicked = _click_toolbar_button(driver, [
            f'button[class*="align"][class*="{cname}"]',
            f'button[data-value="{cname}"]',
            f'button[data-align="{cname}"]',
        ])

    # 3) JS fallback (항상 한 번 더 적용 → 보장)
    apply_alignment_js_fallback(driver, alignment)

    # 선택 해제: 끝으로 이동
    try:
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).send_keys(Keys.END).perform()
    except Exception:
        pass
    return True


_QUOTE_PATTERNS = [
    re.compile(r'^[“"\'](.+?)[”"\']\s*[-—–]\s*.+$'),
    re.compile(r'^>\s+'),
    re.compile(r'^[“"](.+?)[”"]$'),
]

def looks_like_quote(line):
    """v27: 따옴표/홑따옴표/꺽쇠/> 마커 모두 인용으로 감지.
    끝에 마침표·물음표·느낌표·쉼표가 붙어도 OK."""
    s = line.strip()
    if not s:
        return False
    if s.startswith('> ') or s.startswith('> ') or s.startswith('>'):
        return True
    # 끝의 구두점 제거 후 양끝 따옴표 검사
    s2 = s.rstrip('.。!?！？,，')
    pairs = [('"','"'), ('“','”'), ("'","'"), ('‘','’'), ('「','」'), ('『','』'), ('《','》')]
    for a,b in pairs:
        if s2.startswith(a) and s2.endswith(b) and len(s2) > 4:
            return True
    # "문장" — 작가 형태
    if re.match(r'^[“"\'‘「『](.+?)[”"\'’」』]\s*[-—–]\s*.+$', s):
        return True
    return False


def insert_quote_block(driver, text):
    """SmartEditor 인용구 — v26: 한 줄(또는 한 문단)만 인용 블록에 넣고 즉시 탈출.
    - 인용구 버튼 → 첫 스타일 → 텍스트 1회 입력 → Enter → Enter (블록 탈출 보장)
    """
    import pyperclip
    # 인용 마커(>, 양끝 따옴표)만 제거하고 줄바꿈은 첫 줄만 사용
    # v27: 모든 종류의 따옴표/꺽쇠 마커 제거
    cleaned = text.lstrip('>').lstrip()
    cleaned = cleaned.split('\n')[0].strip()
    # 끝의 구두점은 보존하고 양끝 따옴표만 제거
    for a,b in [('"','"'), ('“','”'), ("'","'"), ('‘','’'), ('「','」'), ('『','』'), ('《','》')]:
        # 구두점 처리
        tail = ''
        c = cleaned
        while c and c[-1] in '.。!?！？,，':
            tail = c[-1] + tail
            c = c[:-1]
        if c.startswith(a) and c.endswith(b):
            cleaned = c[len(a):-len(b)].strip() + tail
            break
    if not cleaned:
        return False

    # 인용구 메뉴 열기
    opened = _click_toolbar_button(driver, [
        'button.se-quotation-toolbar-button',
        'button[data-name="quotation"]',
        'button[aria-label*="인용구"]',
    ])
    if opened:
        time.sleep(0.35)
        _click_toolbar_button(driver, [
            'button.se-quotation-line-button',
            'button[class*="quotation"][class*="line"]',
            'ul.se-toolbar-option-quotation li:first-child button',
        ])
        time.sleep(0.35)

    # 한 줄만 붙여넣기
    pyperclip.copy(cleaned)
    safe_hotkey(driver, 'ctrl', 'v')
    time.sleep(0.2)
    # Enter 1회 → 인용 블록 종료 (네이버는 빈 줄 1번이면 인용 블록 탈출)
    safe_press(driver, 'enter')
    time.sleep(0.1)
    # 한 번 더 Enter → 다음 일반 문단 시작 보장
    safe_press(driver, 'enter')
    time.sleep(0.15)
    return True


_STICKER_RE = re.compile(r'\[\[STICKER:(\d+)\]\]')

def extract_sticker_indices(line):
    return [int(m.group(1)) for m in _STICKER_RE.finditer(line)]

def strip_sticker_tokens(line):
    return _STICKER_RE.sub('', line).strip()

def insert_naver_sticker(driver, sticker_index):
    """네이버 에디터 스티커 패널을 열고 n번째 스티커 클릭."""
    from selenium.webdriver.common.by import By
    try:
        _click_toolbar_button(driver, [
            'button.se-sticker-toolbar-button',
            'button[data-name="sticker"]',
            'button[aria-label*="스티커"]',
        ])
        time.sleep(0.6)
        items = driver.find_elements(By.CSS_SELECTOR,
            'ul.se-sticker-list li button, div.se-sticker-grid button, button.se-sticker-item')
        items = [i for i in items if i.is_displayed()]
        if items:
            target = items[sticker_index % len(items)]
            driver.execute_script("arguments[0].click();", target)
            time.sleep(0.5)
            # 스티커 패널 닫기
            try:
                from selenium.webdriver.common.keys import Keys
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def _split_long_line(text, max_len=45):
    """한 줄이 너무 길면 문장 단위로 자르고, 그래도 길면 어절 단위로 자른다.
    네이버 모바일 가독성을 위한 보조 개행."""
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    # 1) 문장 분리
    parts = re.split(r'(?<=[.!?。！？])\s+', text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= max_len:
            out.append(p)
            continue
        # 2) 어절 단위 그리디 분할
        words = p.split(' ')
        cur = ''
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= max_len:
                cur = cur + ' ' + w
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


def preprocess_article_lines(content, max_line_len=45):
    """본문 전처리:
    - \r\n 정규화, ## 헤더 제거
    - 연속 공백 1칸으로
    - 빈 줄은 최대 1개만 허용
    - 한 줄이 max_line_len 이상이면 자동 줄바꿈 (가독성)
    - "문장" 형태/> 문장은 그대로 유지(인용구 단계에서 처리)
    - [스티커:n] / [sticker:n] / [STICKER:n] 표기를 [[STICKER:n]] 으로 통일
    """
    if not content:
        return ''
    text = content.replace('\r\n', '\n')
    # 다양한 스티커 표기 통일
    text = re.sub(r'\[\s*스티커\s*[:：]\s*(\d+)\s*\]', r'[[STICKER:\1]]', text)
    text = re.sub(r'\[\s*sticker\s*[:：]\s*(\d+)\s*\]', r'[[STICKER:\1]]', text, flags=re.I)

    lines = text.split('\n')
    out = []
    blank_run = 0
    for ln in lines:
        s = ln.rstrip()
        s = re.sub(r'^#{1,6}\s*', '', s)        # 마크다운 헤더 제거
        s = re.sub(r'^\*\*(.+?)\*\*$', r'\1', s) # **굵게** 마커 제거(텍스트만 남김)
        s = re.sub(r'[ \t]{2,}', ' ', s)          # 연속 공백 축소
        if not s.strip():
            blank_run += 1
            if blank_run <= 1:
                out.append('')
            continue
        blank_run = 0
        # 가독성용 자동 개행 (인용구/사진/스티커 라인은 제외)
        stripped = s.strip()
        if (stripped in ('[사진]', '[사진 삽입]', '[이미지 삽입]')
                or stripped.startswith('[[STICKER:')
                or looks_like_quote(stripped)):
            out.append(s)
        else:
            for chunk in _split_long_line(s, max_len=max_line_len):
                out.append(chunk)
    # 끝부분 빈 줄 제거
    while out and not out[-1].strip():
        out.pop()
    return '\n'.join(out).strip()


def extract_message_text(message):
    content = getattr(message, 'content', '')
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get('type') == 'text' and item.get('text'):
                    parts.append(item['text'])
            else:
                text = getattr(item, 'text', None)
                if text:
                    parts.append(text)
        return ''.join(parts).strip()
    return str(content or '').strip()
def build_openai_client(api_key, base_url=None, timeout=180.0):
    kwargs = {
        'api_key': api_key,
        'timeout': timeout,
        'max_retries': 2,
    }
    if base_url:
        kwargs['base_url'] = base_url
    return OpenAI(**kwargs)
def call_openai_text(api_key, prompt, model='gpt-4o', base_url=None):
    if not api_key:
        raise ValueError('API Key가 비어 있습니다')
    client = build_openai_client(api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        messages=[{'role': 'user', 'content': prompt}],
        max_tokens=2400,
    )
    text = extract_message_text(resp.choices[0].message)
    if not text:
        raise RuntimeError('AI 응답 본문이 비어 있습니다')
    return text
def call_gemini_text(api_key, prompt, model='gemini-2.5-flash'):
    if not api_key:
        raise ValueError('API Key가 비어 있습니다')

    last_error = None
    for attempt in range(2):
        try:
            client = genai.Client(api_key=api_key)
            config = {"timeout": 120}  # 2분 타임아웃
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            text = getattr(resp, 'text', '') or ''
            if text.strip():
                return text.strip()
            raise RuntimeError('Gemini 응답 본문이 비어 있습니다')
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(2)

    raise RuntimeError(f"Gemini 연결 실패 / {format_error_message(last_error)}")
def generate_dalle_image(api_key, prompt, size="1024x1024", model="gpt-image-1"):
    """OpenAI ChatGPT 이미지 생성 (gpt-image-1) → 로컬 파일 경로 반환"""
    if not api_key:
        raise RuntimeError('OpenAI API Key가 비어 있습니다')
    client = OpenAI(api_key=api_key.strip())
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        quality="medium",
        n=1
    )
    # gpt-image-1은 b64_json 반환, url 반환 모두 지원
    img_item = response.data[0]
    if hasattr(img_item, 'b64_json') and img_item.b64_json:
        img_data = base64.b64decode(img_item.b64_json)
    elif hasattr(img_item, 'url') and img_item.url:
        img_data = requests.get(img_item.url, timeout=60).content
    else:
        raise RuntimeError('이미지 응답에서 데이터를 추출할 수 없습니다')
    # 임시 파일로 저장
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png', prefix='dalle_')
    tmp.write(img_data)
    tmp.close()
    return tmp.name
def normalize_model_text(raw_text):
    """AI 응답 텍스트 정리 (마크다운 코드블록 제거 등)"""
    cleaned = str(raw_text or '').replace('\r\n', '\n').strip()
    cleaned = re.sub(r'^```(?:json|text|markdown)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    return cleaned.strip()
def extract_perplexity_text(data):
    """Perplexity API 응답에서 텍스트를 안전하게 추출"""
    try:
        choice = (data.get('choices') or [{}])[0]
        message = choice.get('message') or {}
        content = message.get('content', '')
        if isinstance(content, str):
            return normalize_model_text(content)
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get('type') == 'text' and item.get('text'):
                        parts.append(str(item['text']))
                else:
                    item_text = getattr(item, 'text', None)
                    if item_text:
                        parts.append(str(item_text))
            return normalize_model_text(''.join(parts))
    except Exception:
        pass
    return normalize_model_text(data.get('text') or data.get('output_text') or '')
def looks_like_invalid_perplexity_article(text):
    """Perplexity가 테스트 응답이나 거부 응답을 반환했는지 확인"""
    sample = normalize_model_text(text).lower()[:200]
    invalid_patterns = [
        r'^ok[.!]?$',
        r'^ok[.!]?\s',
        r'"say ok" is a song',
        r'^i cannot',
        r"^i can't",
        r'^i am sorry',
    ]
    return any(re.search(pattern, sample) for pattern in invalid_patterns)
def call_perplexity(api_key, prompt, model='sonar-pro', system_message='', search_context_size='medium', timeout=120.0):
    """Perplexity 공식 SDK를 사용한 AI 글 생성 (timeout/search_context 옵션화)"""
    if not api_key:
        raise ValueError('API Key가 비어 있습니다')

    client = OpenAI(
        api_key=api_key.strip(),
        base_url='https://api.perplexity.ai',
        timeout=timeout,
        max_retries=0,
        default_headers={
            'User-Agent': 'Mozilla/5.0 (PostingAllInOne/2.4)',
            'Accept': 'application/json',
        },
    )
    messages = []
    if system_message:
        messages.append({'role': 'system', 'content': system_message})
    messages.append({'role': 'user', 'content': prompt})

    prompt_lower = prompt.lower()
    is_test = 'reply with only ok' in prompt_lower or 'say ok' in prompt_lower

    use_web_search = True
    for attempt in range(3):
        try:
            kwargs = dict(messages=messages, model=model, timeout=timeout)
            if use_web_search:
                kwargs["web_search_options"] = {"search_context_size": search_context_size}
            response = client.chat.completions.create(**kwargs)
            text_resp = response.choices[0].message.content
            text_resp = normalize_model_text(text_resp)

            if not text_resp:
                raise RuntimeError('Perplexity 응답 본문이 비어 있습니다')

            if is_test:
                return 'OK'

            compact = re.sub(r'\s+', '', text_resp)
            if len(compact) < 80:
                raise RuntimeError(f'Perplexity 응답이 너무 짧습니다({len(compact)}자)')

            return text_resp
        except Exception as e:
            err_msg = str(e)
            # v58: 400/422 + search_domain_filter / web_search 관련 에러면 옵션 빼고 재시도
            if use_web_search and ('search_domain_filter' in err_msg
                                   or 'web_search' in err_msg.lower()
                                   or '400' in err_msg or '422' in err_msg):
                use_web_search = False
                print(f"[Perplexity] web_search_options 제거 후 재시도: {err_msg[:120]}")
                continue
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
            else:
                raise RuntimeError(f'Perplexity 호출 실패: {format_error_message(e)}')
def call_perplexity_chained(api_key, query_list, context, system_message='', search_context_size='medium', timeout=120.0):
    """여러 질의를 체이닝하여 순차 호출 (timeout 적용)"""
    if not api_key:
        raise ValueError('API Key가 비어 있습니다')

    client = OpenAI(
        api_key=api_key.strip(),
        base_url='https://api.perplexity.ai',
        timeout=timeout,
        max_retries=0,
        default_headers={
            'User-Agent': 'Mozilla/5.0 (PostingAllInOne/2.4)',
            'Accept': 'application/json',
        },
    )
    result = None

    for i, query in enumerate(query_list):
        prompt = f"{context}\n{query}"
        messages = []
        if system_message:
            messages.append({'role': 'system', 'content': system_message})
        messages.append({'role': 'user', 'content': prompt})

        try:
            response = client.chat.completions.create(
                messages=messages,
                model='sonar-pro',
                web_search_options={"search_context_size": search_context_size},
                timeout=timeout,
            )
            result = normalize_model_text(response.choices[0].message.content)
            context = result
        except Exception as e:
            raise RuntimeError(f'Perplexity 체이닝 호출 실패 (질의 {i+1}): {format_error_message(e)}')

    return result

# ═══════════════════════════════════════════════════════════════
#  1. 설정 페이지 (개별 테스트 버튼 포함)
# ═══════════════════════════════════════════════════════════════

class ConfigPage(QWidget):
    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        layout = QVBoxLayout(self)
        title = QLabel("🛡️ 계정 및 API 설정")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        layout.addWidget(title)

        cfg = main_win.config

        # ── 네이버 계정 (다중 계정 지원) ──
        naver_group = QGroupBox("네이버 계정 (다중)")
        ng = QVBoxLayout(naver_group)

        # 계정 목록 테이블
        self.account_table = QTableWidget(0, 2)
        self.account_table.setHorizontalHeaderLabels(["네이버 ID", "비밀번호"])
        self.account_table.horizontalHeader().setStretchLastSection(True)
        self.account_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.account_table.setMaximumHeight(150)
        self.account_table.setStyleSheet("QTableWidget { background-color:#2a2a2a; color:white; }")
        ng.addWidget(self.account_table)

        # 기존 저장된 계정 불러오기
        accounts = cfg.get('naver_accounts', [])
        # 하위호환: 기존 단일 계정 마이그레이션
        if not accounts and cfg.get('naver_id'):
            accounts = [{'id': cfg.get('naver_id', ''), 'pw': cfg.get('naver_pw', '')}]
        for acc in accounts:
            self._add_account_row(acc.get('id', ''), acc.get('pw', ''))

        # 신규 계정 입력
        h_new = QHBoxLayout()
        self.new_naver_id = QLineEdit()
        self.new_naver_id.setPlaceholderText("새 네이버 ID")
        h_new.addWidget(self.new_naver_id)
        self.new_naver_pw = QLineEdit()
        self.new_naver_pw.setPlaceholderText("비밀번호")
        self.new_naver_pw.setEchoMode(QLineEdit.EchoMode.Password)
        h_new.addWidget(self.new_naver_pw)
        add_btn = QPushButton("➕ 추가")
        add_btn.setStyleSheet("background-color:#2d5a27; color:white; padding:4px 12px;")
        add_btn.clicked.connect(self._add_account)
        h_new.addWidget(add_btn)
        del_btn = QPushButton("🗑️ 선택 삭제")
        del_btn.setStyleSheet("background-color:#8a1a1a; color:white; padding:4px 12px;")
        del_btn.clicked.connect(self._del_account)
        h_new.addWidget(del_btn)
        ng.addLayout(h_new)

        self.naver_test_btn = QPushButton("🔑 선택 계정 로그인 테스트")
        self.naver_test_btn.setStyleSheet("background-color:#2d5a27; color:white; font-weight:bold; padding:6px;")
        self.naver_test_btn.clicked.connect(self.test_naver_login)
        ng.addWidget(self.naver_test_btn)

        self.naver_status = QLabel("")
        ng.addWidget(self.naver_status)
        layout.addWidget(naver_group)

        # ── AI API Keys (개별 테스트 버튼) ──
        api_group = QGroupBox("AI API Keys")
        ag = QVBoxLayout(api_group)

        # Gemini
        ag.addWidget(QLabel("Gemini API Key:"))
        h_gem = QHBoxLayout()
        self.gemini_key = QLineEdit(cfg.get('gemini_key', ''))
        self.gemini_key.setPlaceholderText("AIza...")
        h_gem.addWidget(self.gemini_key, 4)
        self.gemini_test_btn = QPushButton("🧪 테스트")
        self.gemini_test_btn.setStyleSheet("background-color:#4a4a00; color:white; padding:4px 12px;")
        self.gemini_test_btn.clicked.connect(self.test_gemini)
        h_gem.addWidget(self.gemini_test_btn)
        ag.addLayout(h_gem)
        self.gemini_status = QLabel("")
        ag.addWidget(self.gemini_status)

        # Perplexity
        ag.addWidget(QLabel("Perplexity API Key:"))
        h_pplx = QHBoxLayout()
        self.perplex_key = QLineEdit(cfg.get('perplex_key', ''))
        self.perplex_key.setPlaceholderText("pplx-...")
        h_pplx.addWidget(self.perplex_key, 4)
        self.perplex_test_btn = QPushButton("🧪 테스트")
        self.perplex_test_btn.setStyleSheet("background-color:#4a4a00; color:white; padding:4px 12px;")
        self.perplex_test_btn.clicked.connect(self.test_perplexity)
        h_pplx.addWidget(self.perplex_test_btn)
        ag.addLayout(h_pplx)
        self.perplex_status = QLabel("")
        ag.addWidget(self.perplex_status)

        # OpenAI GPT
        ag.addWidget(QLabel("OpenAI API Key:"))
        h_oai = QHBoxLayout()
        self.openai_key = QLineEdit(cfg.get('openai_key', ''))
        self.openai_key.setPlaceholderText("sk-...")
        h_oai.addWidget(self.openai_key, 4)
        self.openai_test_btn = QPushButton("🧪 테스트")
        self.openai_test_btn.setStyleSheet("background-color:#4a4a00; color:white; padding:4px 12px;")
        self.openai_test_btn.clicked.connect(self.test_openai)
        h_oai.addWidget(self.openai_test_btn)
        ag.addLayout(h_oai)
        self.openai_status = QLabel("")
        ag.addWidget(self.openai_status)

        layout.addWidget(api_group)

        # 저장 버튼
        btn = QPushButton("💾 설정 저장")
        btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px; font-size:14px;")
        btn.clicked.connect(self.save)
        layout.addWidget(btn)

        # 전체 테스트
        all_test = QPushButton("🔗 전체 연결 테스트")
        all_test.setStyleSheet("background-color:#5a2d00; color:white; font-weight:bold; padding:8px;")
        all_test.clicked.connect(self.test_all)
        layout.addWidget(all_test)

        layout.addStretch()

    def _add_account_row(self, nid='', npw=''):
        row = self.account_table.rowCount()
        self.account_table.insertRow(row)
        id_item = QTableWidgetItem(nid)
        self.account_table.setItem(row, 0, id_item)
        pw_item = QTableWidgetItem(npw)
        self.account_table.setItem(row, 1, pw_item)

    def _add_account(self):
        nid = self.new_naver_id.text().strip()
        npw = self.new_naver_pw.text().strip()
        if not nid or not npw:
            self._set_status(self.naver_status, False, "ID와 비밀번호를 입력하세요")
            return
        # 중복 체크
        for r in range(self.account_table.rowCount()):
            if self.account_table.item(r, 0) and self.account_table.item(r, 0).text() == nid:
                self._set_status(self.naver_status, False, f"'{nid}' 이미 등록된 계정입니다")
                return
        self._add_account_row(nid, npw)
        self.new_naver_id.clear()
        self.new_naver_pw.clear()
        self.save()
        self._set_status(self.naver_status, True, f"'{nid}' 계정 추가 완료")

    def _del_account(self):
        row = self.account_table.currentRow()
        if row >= 0:
            nid = self.account_table.item(row, 0).text() if self.account_table.item(row, 0) else ''
            self.account_table.removeRow(row)
            self.save()
            self._set_status(self.naver_status, True, f"'{nid}' 계정 삭제됨")
        else:
            self._set_status(self.naver_status, False, "삭제할 계정을 선택하세요")

    def _get_all_accounts(self):
        accounts = []
        for r in range(self.account_table.rowCount()):
            nid = self.account_table.item(r, 0).text().strip() if self.account_table.item(r, 0) else ''
            npw = self.account_table.item(r, 1).text().strip() if self.account_table.item(r, 1) else ''
            if nid and npw:
                accounts.append({'id': nid, 'pw': npw})
        return accounts

    def save(self):
        accounts = self._get_all_accounts()
        # 하위호환: 첫 번째 계정을 기본으로
        first_id = accounts[0]['id'] if accounts else ''
        first_pw = accounts[0]['pw'] if accounts else ''
        self.main.config.update({
            'naver_id': first_id,
            'naver_pw': first_pw,
            'naver_accounts': accounts,
            'gemini_key': self.gemini_key.text(),
            'perplex_key': self.perplex_key.text(),
            'openai_key': self.openai_key.text(),
        })
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(self.main.config, f, ensure_ascii=False, indent=2)
        self.main.log("✅ 설정 저장 완료")
        # 다른 페이지 계정 드롭다운 갱신
        self._refresh_account_combos()

    def _refresh_account_combos(self):
        accounts = self._get_all_accounts()
        ids = [a['id'] for a in accounts]
        for page_key in self.main.pages:
            page = self.main.pages[page_key]
            if hasattr(page, 'account_combo'):
                current = page.account_combo.currentText()
                page.account_combo.clear()
                page.account_combo.addItems(ids)
                if current in ids:
                    page.account_combo.setCurrentText(current)

    def _set_status(self, label, ok, msg):
        if ok:
            label.setText(f"  ✅ {msg}")
            label.setStyleSheet("color:#00ff00; font-size:12px;")
        else:
            label.setText(f"  ❌ {msg}")
            label.setStyleSheet("color:#ff4444; font-size:12px;")

    # ── 네이버 로그인 테스트 ──
    def test_naver_login(self):
        row = self.account_table.currentRow()
        if row < 0:
            row = 0  # 기본 첫번째
        if row >= self.account_table.rowCount():
            self._set_status(self.naver_status, False, "계정을 먼저 추가하세요")
            return
        nid = self.account_table.item(row, 0).text().strip() if self.account_table.item(row, 0) else ''
        npw = self.account_table.item(row, 1).text().strip() if self.account_table.item(row, 1) else ''
        if not nid or not npw:
            self._set_status(self.naver_status, False, "ID/PW를 입력하세요")
            return

        self.naver_test_btn.setEnabled(False)
        self.naver_test_btn.setText("테스트 중...")
        self.naver_status.setText("  ⏳ 로그인 테스트 중...")
        self.naver_status.setStyleSheet("color:#ffaa00; font-size:12px;")

        def do_test():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            opts = uc.ChromeOptions()
            opts.add_argument('--start-maximized')
            opts.add_argument('--user-data-dir=./chrome_profile_test')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--disable-features=RendererCodeIntegrity')
            driver = make_uc_driver(opts)
            try:
                driver.get("https://nid.naver.com/nidlogin.login")
                time.sleep(2)

                id_el = driver.find_element(By.ID, "id")
                # 기존 입력값 완전히 삭제 (JS로 value 초기화 후 clear)
                driver.execute_script("arguments[0].value = '';", id_el)
                id_el.clear()
                time.sleep(0.2)
                safe_paste(driver, id_el, nid)
                time.sleep(0.3)

                pw_el = driver.find_element(By.ID, "pw")
                # 기존 입력값 완전히 삭제
                driver.execute_script("arguments[0].value = '';", pw_el)
                pw_el.clear()
                time.sleep(0.2)
                safe_paste(driver, pw_el, npw)
                time.sleep(0.3)

                driver.find_element(By.ID, "log.login").click()
                time.sleep(5)

                # 로그인 성공 확인
                current_url = driver.current_url
                page_source = driver.page_source

                if 'nidlogin' in current_url or '로그인' in page_source[:500]:
                    # 캡차나 2차 인증 확인
                    if 'captcha' in page_source.lower() or 'device' in current_url:
                        return "CAPTCHA|보안 인증이 필요합니다. 브라우저에서 직접 로그인 후 재시도하세요"
                    return "FAIL|로그인 실패 - ID/PW를 확인하세요"
                else:
                    return "OK|로그인 성공!"
            except Exception as e:
                return f"FAIL|{str(e)}"
            finally:
                try:
                    driver.quit()
                except:
                    pass

        def on_result(r):
            parts = r.split("|", 1)
            status = parts[0]
            msg = parts[1] if len(parts) > 1 else r
            if status == "OK":
                self._set_status(self.naver_status, True, msg)
                self.main.log("✅ 네이버 로그인 테스트 성공")
            elif status == "CAPTCHA":
                self.naver_status.setText(f"  ⚠️ {msg}")
                self.naver_status.setStyleSheet("color:#ffaa00; font-size:12px;")
                self.main.log(f"⚠️ 네이버: {msg}")
            else:
                self._set_status(self.naver_status, False, msg)
                self.main.log(f"❌ 네이버 로그인: {msg}")

        thread = WorkerThread(do_test)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.naver_test_btn.setEnabled(True),
            self.naver_test_btn.setText("🔑 네이버 로그인 테스트")
        ))
        thread.start()

    # ── Gemini 테스트 ──
    def test_gemini(self):
        key = self.gemini_key.text().strip()
        if not key:
            self._set_status(self.gemini_status, False, "API Key를 입력하세요")
            return
        self.gemini_test_btn.setEnabled(False)
        self.gemini_test_btn.setText("테스트중...")
        self.gemini_status.setText("  ⏳ 연결 중...")
        self.gemini_status.setStyleSheet("color:#ffaa00; font-size:12px;")

        def do_test():
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(model="gemini-2.5-flash", contents="Say OK")
            return f"OK|연결 성공 - 응답: {resp.text[:50]}"

        def on_result(r):
            parts = r.split("|", 1)
            self._set_status(self.gemini_status, parts[0] == "OK", parts[1] if len(parts) > 1 else r)
            self.main.log(f"{'✅' if parts[0] == 'OK' else '❌'} Gemini: {parts[1] if len(parts) > 1 else r}")

        thread = WorkerThread(do_test)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(lambda msg: (
            self._set_status(self.gemini_status, False, msg.replace("❌ 오류 발생: ", "")),
        ))
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.gemini_test_btn.setEnabled(True),
            self.gemini_test_btn.setText("🧪 테스트")
        ))
        thread.start()

    # ── Perplexity 테스트 ──
    def test_perplexity(self):
        key = self.perplex_key.text().strip()
        if not key:
            self._set_status(self.perplex_status, False, "API Key를 입력하세요")
            return
        self.perplex_test_btn.setEnabled(False)
        self.perplex_test_btn.setText("테스트중...")
        self.perplex_status.setText("  ⏳ 연결 중...")
        self.perplex_status.setStyleSheet("color:#ffaa00; font-size:12px;")

        def do_test():
            call_perplexity(key, "Reply with only OK", model="sonar", system_message="Return only OK.")
            return "OK|연결 성공 - 응답: OK"

        def on_result(r):
            parts = r.split("|", 1)
            self._set_status(self.perplex_status, parts[0] == "OK", parts[1] if len(parts) > 1 else r)
            self.main.log(f"{'✅' if parts[0] == 'OK' else '❌'} Perplexity: {parts[1] if len(parts) > 1 else r}")

        thread = WorkerThread(do_test)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(lambda msg: (
            self._set_status(self.perplex_status, False, msg.replace("❌ 오류 발생: ", "")),
        ))
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.perplex_test_btn.setEnabled(True),
            self.perplex_test_btn.setText("🧪 테스트")
        ))
        thread.start()

    # ── OpenAI GPT 테스트 ──
    def test_openai(self):
        key = self.openai_key.text().strip()
        if not key:
            self._set_status(self.openai_status, False, "API Key를 입력하세요")
            return
        self.openai_test_btn.setEnabled(False)
        self.openai_test_btn.setText("테스트중...")
        self.openai_status.setText("  ⏳ 연결 중...")
        self.openai_status.setStyleSheet("color:#ffaa00; font-size:12px;")

        def do_test():
            client = OpenAI(api_key=key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Say OK"}],
                max_tokens=10
            )
            return f"OK|연결 성공 - 응답: {resp.choices[0].message.content[:50]}"

        def on_result(r):
            parts = r.split("|", 1)
            self._set_status(self.openai_status, parts[0] == "OK", parts[1] if len(parts) > 1 else r)
            self.main.log(f"{'✅' if parts[0] == 'OK' else '❌'} OpenAI: {parts[1] if len(parts) > 1 else r}")

        thread = WorkerThread(do_test)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(lambda msg: (
            self._set_status(self.openai_status, False, msg.replace("❌ 오류 발생: ", "")),
        ))
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.openai_test_btn.setEnabled(True),
            self.openai_test_btn.setText("🧪 테스트")
        ))
        thread.start()

    # ── 전체 테스트 ──
    def test_all(self):
        self.save()
        self.main.log("🔗 전체 연결 테스트 시작...")
        if self.gemini_key.text().strip():
            self.test_gemini()
        if self.perplex_key.text().strip():
            self.test_perplexity()
        if self.openai_key.text().strip():
            self.test_openai()
        if self._get_all_accounts():
            # 네이버는 AI 테스트 후 약간의 딜레이
            QTimer.singleShot(2000, self.test_naver_login)
# ═══════════════════════════════════════════════════════════════
#  2. 키워드 분석 페이지
# ═══════════════════════════════════════════════════════════════

class KeywordAnalysisPage(QWidget):
    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        layout = QVBoxLayout(self)
        title = QLabel("🔍 키워드 분석")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        layout.addWidget(title)

        self.seed_keywords = QTextEdit()
        self.seed_keywords.setPlaceholderText("키워드1\n키워드2\n(엔터로 구분)")
        self.seed_keywords.setMaximumHeight(100)
        layout.addWidget(QLabel("시드 키워드:"));
        layout.addWidget(self.seed_keywords)

        self.analyze_btn = QPushButton("🚀 분석 시작")
        self.analyze_btn.clicked.connect(self.start_analysis)
        layout.addWidget(self.analyze_btn)

        self.result = QTextBrowser()
        layout.addWidget(self.result)

    def start_analysis(self):
        keywords = [k.strip() for k in self.seed_keywords.toPlainText().split('\n') if k.strip()]
        if not keywords:
            self.main.log("⚠️ 키워드를 입력하세요");
            return
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("분석 중...")

        def do_analysis():
            results = []
            for kw in keywords:
                results.append(f"🔑 {kw}: 키워드 분석 완료")
            return '\n'.join(results)

        thread = WorkerThread(do_analysis)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.result.setPlainText(r))
        thread.finished_signal.connect(lambda: (self.analyze_btn.setEnabled(True), self.analyze_btn.setText("🚀 분석 시작")))
        thread.start()
# ═══════════════════════════════════════════════════════════════
#  3. 블로그 수집 → AI 글쓰기 → 자동발행 (통합 파이프라인)
# ═══════════════════════════════════════════════════════════════

class BlogPipelinePage(QWidget):
    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self.collected_articles = []
        self.generated_content = ""
        self.generated_articles = []  # 개별 원고 리스트
        self._cancel_crawl = False
        self._cancel_generate = False
        self._crawling = False
        self._generating = False
        self._folder_cache = {}
        try:
            with open('folder_cache.json', 'r', encoding='utf-8') as f:
                self._folder_cache = json.load(f)
        except Exception:
            pass

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        title = QLabel("📝 블로그 수집 → AI 글쓰기 → 자동발행 파이프라인")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        scroll_layout.addWidget(title)

        # ══════════════════════════════════════
        # STEP 1: 키워드 입력
        # ══════════════════════════════════════
        step1 = QGroupBox("STEP 1️⃣  키워드 입력")
        s1 = QVBoxLayout(step1)

        h1 = QHBoxLayout()
        self.keywords_input = QLineEdit()
        self.keywords_input.setPlaceholderText("키워드1, 키워드2, 키워드3 (쉼표로 구분)")
        h1.addWidget(QLabel("키워드:"))
        h1.addWidget(self.keywords_input, 3)

        self.excel_btn = QPushButton("📂 엑셀 일괄등록")
        self.excel_btn.clicked.connect(self.load_excel_keywords)
        h1.addWidget(self.excel_btn)
        s1.addLayout(h1)

        self.keyword_table = QTableWidget(0, 3)
        self.keyword_table.setHorizontalHeaderLabels(["키워드", "상태", "수집 결과"])
        self.keyword_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.keyword_table.setMaximumHeight(120)
        s1.addWidget(self.keyword_table)

        h1b = QHBoxLayout()
        self.add_kw_btn = QPushButton("➕ 키워드 추가")
        self.add_kw_btn.clicked.connect(self.add_keywords)
        self.clear_kw_btn = QPushButton("🗑️ 전체 삭제")
        self.clear_kw_btn.clicked.connect(lambda: self.keyword_table.setRowCount(0))
        h1b.addWidget(self.add_kw_btn)
        h1b.addWidget(self.clear_kw_btn)
        s1.addLayout(h1b)

        scroll_layout.addWidget(step1)

        # ══════════════════════════════════════
        # STEP 2: 블로그 크롤링 설정
        # ══════════════════════════════════════
        step2 = QGroupBox("STEP 2️⃣  네이버 블로그 크롤링 설정")
        s2 = QVBoxLayout(step2)

        h2 = QHBoxLayout()
        h2.addWidget(QLabel("추출 글 수:"))
        self.extract_count = QSpinBox()
        self.extract_count.setRange(1, 50)
        self.extract_count.setValue(5)
        h2.addWidget(self.extract_count)

        h2.addWidget(QLabel("정렬:"))
        self.sort_type = QComboBox()
        self.sort_type.addItems(["최신순", "정확도순", "인기순"])
        h2.addWidget(self.sort_type)
        h2.addStretch()
        s2.addLayout(h2)

        self.crawl_btn = QPushButton("🔍 블로그 수집 시작")
        self.crawl_btn.clicked.connect(self.start_crawl)
        self.crawl_btn.setStyleSheet("background-color:#2d5a27; color:white; font-weight:bold; padding:8px;")
        s2.addWidget(self.crawl_btn)

        self.crawl_progress = QProgressBar()
        self.crawl_progress.setVisible(False)
        s2.addWidget(self.crawl_progress)

        self.crawl_result = QTextBrowser()
        self.crawl_result.setMaximumHeight(200)
        self.crawl_result.setPlaceholderText("수집된 블로그 본문이 여기에 표시됩니다...")
        s2.addWidget(self.crawl_result)

        scroll_layout.addWidget(step2)

        # ══════════════════════════════════════
        # STEP 3: AI 글쓰기
        # ══════════════════════════════════════
        step3 = QGroupBox("STEP 3️⃣  AI 글쓰기")
        s3 = QVBoxLayout(step3)

        h3a = QHBoxLayout()
        h3a.addWidget(QLabel("AI 서비스:"))
        self.ai_service = QComboBox()
        self.ai_service.addItems(["Gemini (gemini-2.5-flash)", "Perplexity (sonar-pro)", "GPT (gpt-4o)"])
        h3a.addWidget(self.ai_service, 2)

        h3a.addWidget(QLabel("글자 수:"))
        self.char_count = QSpinBox()
        self.char_count.setRange(500, 10000)
        self.char_count.setValue(2000)
        self.char_count.setSingleStep(500)
        h3a.addWidget(self.char_count)
        s3.addLayout(h3a)

        h3b = QHBoxLayout()
        self.opt_map = QCheckBox("📍 지도 삽입")
        self.opt_video = QCheckBox("🎬 영상 삽입")
        self.opt_quote = QCheckBox("💬 인용구 포함")
        self.opt_quote.setChecked(True)
        self.opt_photo = QCheckBox("📷 사진 위치 표시")
        self.opt_photo.setChecked(True)
        self.opt_hashtag = QCheckBox("#️⃣ 해시태그")
        self.opt_hashtag.setChecked(True)
        h3b.addWidget(self.opt_map)
        h3b.addWidget(self.opt_video)
        h3b.addWidget(self.opt_quote)
        h3b.addWidget(self.opt_photo)
        h3b.addWidget(self.opt_hashtag)
        s3.addLayout(h3b)

        self.ai_prompt = QTextEdit()
        self.ai_prompt.setPlaceholderText("AI에게 전달할 프롬프트를 입력하세요...")
        self.ai_prompt.setPlainText(
            "아래 참고 자료를 바탕으로 SEO 최적화된 네이버 블로그 포스트를 작성해주세요.\n"
            "- 자연스러운 말투 (1인칭 경험담)\n"
            "- 소제목 3개 이상 (##)\n"
            "- 핵심 키워드 자연스럽게 5회 이상 배치\n"
            "- [사진] 태그로 사진 삽입 위치 표시\n"
            "- [인용구]텍스트[/인용구] 형태로 강조 문구 표시\n"
            "- 마지막에 해시태그 5개 추가"
        )
        self.ai_prompt.setMaximumHeight(120)
        s3.addWidget(QLabel("프롬프트:"))
        s3.addWidget(self.ai_prompt)

        h3c = QHBoxLayout()
        self.send_to_ai_btn = QPushButton("⬇️ 수집 결과 → AI 입력")
        self.send_to_ai_btn.clicked.connect(self.send_to_ai)
        h3c.addWidget(self.send_to_ai_btn)

        self.generate_btn = QPushButton("🎯 AI 원고 생성")
        self.generate_btn.clicked.connect(self.generate_ai)
        self.generate_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
        h3c.addWidget(self.generate_btn)
        s3.addLayout(h3c)

        self.ai_gen_progress = QProgressBar()
        self.ai_gen_progress.setVisible(False)
        s3.addWidget(self.ai_gen_progress)

        self.ai_input = QTextEdit()
        self.ai_input.setPlaceholderText("수집된 블로그 본문이 AI 입력으로 전달됩니다...")
        self.ai_input.setMaximumHeight(100)
        s3.addWidget(QLabel("AI 입력 (수집 데이터):"))
        s3.addWidget(self.ai_input)

        # ── 원고 리스트 테이블 ──
        s3.addWidget(QLabel("📋 생성된 원고 리스트:"))
        self.article_table = QTableWidget()
        self.article_table.setColumnCount(4)
        self.article_table.setHorizontalHeaderLabels(["번호", "상태", "제목", "글자수"])
        self.article_table.horizontalHeader().setStretchLastSection(True)
        self.article_table.setColumnWidth(0, 50)
        self.article_table.setColumnWidth(1, 60)
        self.article_table.setColumnWidth(2, 350)
        self.article_table.setColumnWidth(3, 70)
        self.article_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.article_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.article_table.itemSelectionChanged.connect(self._on_article_selected)
        self.article_table.setMinimumHeight(150)
        s3.addWidget(self.article_table)

        self.ai_output = QTextBrowser()
        self.ai_output.setPlaceholderText("원고를 선택하면 여기에 표시됩니다...")
        s3.addWidget(QLabel("📄 선택된 원고 미리보기:"))
        s3.addWidget(self.ai_output)

        self.error_summary_label = QLabel("⚠️ 주요 오류:")
        self.error_summary_label.setVisible(False)
        s3.addWidget(self.error_summary_label)
        self.error_summary = QTextBrowser()
        self.error_summary.setMaximumHeight(60)
        self.error_summary.setVisible(False)
        s3.addWidget(self.error_summary)

        h3d = QHBoxLayout()
        copy_btn = QPushButton("📋 선택 원고 복사")
        copy_btn.clicked.connect(self._copy_selected_article)
        h3d.addWidget(copy_btn)
        copy_all_btn = QPushButton("📋 전체 원고 복사")
        copy_all_btn.clicked.connect(self._copy_all_articles)
        h3d.addWidget(copy_all_btn)
        save_btn = QPushButton("💾 전체 원고 저장")
        save_btn.clicked.connect(self.save_draft)
        h3d.addWidget(save_btn)
        export_btn = QPushButton("📊 엑셀로 내보내기")
        export_btn.clicked.connect(self._export_articles_excel)
        h3d.addWidget(export_btn)
        import_btn = QPushButton("📥 엑셀 원고 불러오기")
        import_btn.clicked.connect(self._import_articles_excel)
        h3d.addWidget(import_btn)
        s3.addLayout(h3d)

        scroll_layout.addWidget(step3)

        # ══════════════════════════════════════
        # STEP 4: 네이버 자동 발행
        # ══════════════════════════════════════
        step4 = QGroupBox("STEP 4️⃣  네이버 블로그 자동 발행")
        s4 = QVBoxLayout(step4)

        # 계정 선택
        h4_acc = QHBoxLayout()
        h4_acc.addWidget(QLabel("발행 계정:"))
        self.account_combo = QComboBox()
        accounts = self.main.config.get('naver_accounts', [])
        if not accounts and self.main.config.get('naver_id'):
            accounts = [{'id': self.main.config.get('naver_id', '')}]
        for acc in accounts:
            self.account_combo.addItem(acc.get('id', ''))
        h4_acc.addWidget(self.account_combo, 2)
        s4.addLayout(h4_acc)

        h4a = QHBoxLayout()
        h4a.addWidget(QLabel("블로그 폴더:"))
        self.blog_folder = QComboBox()
        self.blog_folder.setEditable(True)
        h4a.addWidget(self.blog_folder, 2)

        self.load_folders_btn = QPushButton("🔄 폴더 불러오기")
        self.load_folders_btn.clicked.connect(self.load_blog_folders)
        h4a.addWidget(self.load_folders_btn)
        s4.addLayout(h4a)

        h4b = QHBoxLayout()
        h4b.addWidget(QLabel("저장 형태:"))
        self.save_mode = QComboBox()
        self.save_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        # v24: 본문 정렬 선택
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["⬅️ 좌측 정렬", "⬆️ 가운데 정렬", "➡️ 우측 정렬", "↔️ 양쪽 정렬"])
        self.alignment_combo.currentIndexChanged.connect(
            lambda i: setattr(self, '_selected_alignment', ['left','center','right','justify'][i])
        )
        self._selected_alignment = 'left'
        h4b.addWidget(self.save_mode)
        h4b.addWidget(QLabel("정렬:"))
        h4b.addWidget(self.alignment_combo)

        h4b.addWidget(QLabel("예약 시간:"))
        self.schedule_time = QLineEdit()
        self.schedule_time.setPlaceholderText("2025-01-15 09:00")
        self.schedule_time.setEnabled(False)
        h4b.addWidget(self.schedule_time)
        self.save_mode.currentIndexChanged.connect(
            lambda idx: self.schedule_time.setEnabled(idx == 2)
        )
        s4.addLayout(h4b)

        info = QLabel("⚠️ 실제 타자 입력 방식으로 작성합니다 (봇 탐지 회피)\n"
                      "• [인용구] → 네이버 인용구 블록  • [사진] → 사진 삽입 위치\n"
                      "• [지도:장소명] → 네이버 지도  • #해시태그 → 자동 추가")
        info.setStyleSheet("color:#aaa; font-size:11px; padding:4px;")
        s4.addWidget(info)

        self.publish_btn = QPushButton("🔥 네이버 블로그 자동 발행")
        self.publish_btn.clicked.connect(self.publish_to_naver)
        self.publish_btn.setStyleSheet(
            "background-color:#8a1a1a; color:white; font-weight:bold; padding:10px; font-size:14px;")
        s4.addWidget(self.publish_btn)

        self.publish_progress = QProgressBar()
        self.publish_progress.setVisible(False)
        s4.addWidget(self.publish_progress)

        scroll_layout.addWidget(step4)

        # 스크롤 설정
        main_layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        main_layout.addWidget(scroll)

    def _copy(self, text):
        if text:
            pyperclip.copy(text)
            self.main.log("📋 클립보드에 복사됨")

    def _export_articles_excel(self):
        """생성된 원고를 엑셀로 내보내기"""
        if not self.generated_articles:
            self.main.log("⚠️ 내보낼 원고가 없습니다")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            path, _ = QFileDialog.getSaveFileName(self, "엑셀 내보내기", "블로그_원고_목록.xlsx", "Excel Files (*.xlsx)")
            if not path:
                return
            wb = Workbook()
            ws = wb.active
            ws.title = "원고 목록"
            headers = ["번호", "제목", "키워드", "글자수", "상태", "원문 참고", "생성된 원고"]
            header_fill = PatternFill('solid', fgColor='1a4a8a')
            header_font = Font(bold=True, color='FFFFFF')
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=h)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')
            ws.column_dimensions['A'].width = 6
            ws.column_dimensions['B'].width = 50
            ws.column_dimensions['C'].width = 20
            ws.column_dimensions['D'].width = 10
            ws.column_dimensions['E'].width = 8
            ws.column_dimensions['F'].width = 60
            ws.column_dimensions['G'].width = 80
            for i, art in enumerate(self.generated_articles):
                ws.cell(row=i+2, column=1, value=i+1)
                ws.cell(row=i+2, column=2, value=art.get('title', ''))
                ws.cell(row=i+2, column=3, value=art.get('keyword', ''))
                ws.cell(row=i+2, column=4, value=len(art.get('content', '')))
                ws.cell(row=i+2, column=5, value=art.get('status', '완료'))
                ws.cell(row=i+2, column=6, value=art.get('source', '')[:500])
                ws.cell(row=i+2, column=7, value=art.get('content', ''))
            wb.save(path)
            self.main.log(f"📊 엑셀 내보내기 완료: {path} ({len(self.generated_articles)}건)")
        except ImportError:
            self.main.log("❌ openpyxl 패키지가 필요합니다: pip install openpyxl")
        except Exception as e:
            self.main.log(f"❌ 엑셀 내보내기 오류: {e}")

    def _import_articles_excel(self):
        """엑셀 파일에서 원고를 불러오기"""
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 원고 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if not path:
            return
        try:
            import pandas as pd
            df = pd.read_excel(path)
            imported = []
            for _, row in df.iterrows():
                title = str(row.get('제목', row.get('title', '제목 없음')))
                content = str(row.get('생성된 원고', row.get('content', row.get('생성된원고', ''))))
                keyword = str(row.get('키워드', row.get('keyword', '')))
                source = str(row.get('원문 참고', row.get('원문참고', row.get('source', ''))))
                if content and content != 'nan':
                    imported.append({
                        'title': title if title != 'nan' else '제목 없음',
                        'content': content,
                        'keyword': keyword if keyword != 'nan' else '',
                        'source': source if source != 'nan' else '',
                        'status': '완료',
                    })
            if imported:
                self.generated_articles.extend(imported)
                self.ai_output.setPlainText(imported[-1]['content'])
                self.main.log(f"📥 엑셀에서 {len(imported)}건 원고 불러옴 (총 {len(self.generated_articles)}건)")
            else:
                self.main.log("⚠️ 엑셀에서 불러올 원고가 없습니다 (제목/생성된 원고 컬럼 필요)")
        except Exception as e:
            self.main.log(f"❌ 엑셀 원고 불러오기 오류: {e}")

    def load_excel_keywords(self):
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 파일 선택", "", "Excel Files (*.xlsx *.xls *.csv)")
        if not path: return
        try:
            import pandas as pd
            if path.endswith('.csv'):
                df = pd.read_csv(path)
            else:
                df = pd.read_excel(path)
            keywords = df.iloc[:, 0].dropna().astype(str).tolist()
            for kw in keywords:
                row = self.keyword_table.rowCount()
                self.keyword_table.insertRow(row)
                self.keyword_table.setItem(row, 0, QTableWidgetItem(kw.strip()))
                self.keyword_table.setItem(row, 1, QTableWidgetItem("대기"))
                self.keyword_table.setItem(row, 2, QTableWidgetItem(""))
            self.main.log(f"✅ {len(keywords)}개 키워드 엑셀에서 로드")
        except Exception as e:
            self.main.log(f"❌ 엑셀 로드 실패: {e}")

    def add_keywords(self):
        text = self.keywords_input.text().strip()
        if not text: return
        keywords = [k.strip() for k in text.split(',') if k.strip()]
        for kw in keywords:
            row = self.keyword_table.rowCount()
            self.keyword_table.insertRow(row)
            self.keyword_table.setItem(row, 0, QTableWidgetItem(kw))
            self.keyword_table.setItem(row, 1, QTableWidgetItem("대기"))
            self.keyword_table.setItem(row, 2, QTableWidgetItem(""))
        self.keywords_input.clear()
        self.main.log(f"✅ {len(keywords)}개 키워드 추가됨")

    def save_draft(self):
        content = self.ai_output.toPlainText()
        if not content:
            self.main.log("⚠️ 저장할 원고가 없습니다");
            return
        path, _ = QFileDialog.getSaveFileName(self, "원고 저장", "draft.txt", "Text Files (*.txt)")
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.main.log(f"✅ 원고 저장: {path}")

    # ──────────── 블로그 수집 ────────────

    def start_crawl(self):
        keywords = []
        for row in range(self.keyword_table.rowCount()):
            item = self.keyword_table.item(row, 0)
            if item and item.text().strip():
                keywords.append(item.text().strip())
        if not keywords:
            self.main.log("⚠️ 키워드를 먼저 추가하세요");
            return

        count = self.extract_count.value()
        # 취소 토글
        if self._crawling:
            self._cancel_crawl = True
            self.crawl_btn.setText("🛑 취소 중...")
            self.main.log("🛑 수집 취소 요청됨...")
            return

        self._cancel_crawl = False
        self._crawling = True
        self.crawl_btn.setText("🛑 수집 취소")
        self.crawl_btn.setStyleSheet("background-color:#8a1a1a; color:white; font-weight:bold; padding:8px;")
        self.crawl_progress.setVisible(True)
        self.crawl_progress.setValue(0)

        def do_crawl():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            opts = uc.ChromeOptions()
            opts.add_argument('--headless=new')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-features=RendererCodeIntegrity')
            driver = make_uc_driver(opts)
            all_articles = []

            try:
                total = len(keywords) * count
                done = 0

                for ki, keyword in enumerate(keywords):
                    self.log_emit(f"🔍 [{ki + 1}/{len(keywords)}] '{keyword}' 검색 중...")
                    self.update_kw_status(ki, "수집 중")

                    search_url = f"https://search.naver.com/search.naver?where=nexearch&query={keyword}"
                    driver.get(search_url)
                    time.sleep(2)

                    blog_links = []
                    import re as _re
                    def is_blog_post(url):
                        """블로그 홈(목록)이 아닌 실제 포스트 URL만 허용"""
                        if 'blog.naver.com' not in url:
                            return False
                        if _re.search(r'blog\.naver\.com/[^/]+/\d+', url):
                            return True
                        if 'PostView' in url or 'postView' in url:
                            return True
                        return False

                    # 통합검색 페이지에서 블로그 블록 내 순위별 링크 추출
                    try:
                        blog_selectors = [
                            'a.title_link',
                            'a.api_txt_lines',
                            'div.fds-comps-right-image a',
                            'div.fds-comps-left-image a',
                            'div.blog_group a.title_link',
                            'div.api_subject_bx a.title_link',
                            'div.detail_box a.title_link',
                            'div.total_group a.api_txt_lines',
                        ]
                        for sel in blog_selectors:
                            items = driver.find_elements(By.CSS_SELECTOR, sel)
                            for item in items:
                                href = item.get_attribute('href') or ''
                                if is_blog_post(href) and href not in blog_links:
                                    blog_links.append(href)
                                    self.log_emit(f"    \u2705 순위{len(blog_links)}: {href[:80]}")
                                    if len(blog_links) >= count:
                                        break
                            if len(blog_links) >= count:
                                break
                    except Exception as e:
                        self.log_emit(f"    \u26a0\ufe0f 셀렉터 오류: {e}")

                    # 폴백: 전체 링크에서 블로그 포스트만 수집
                    if not blog_links:
                        try:
                            self.log_emit("    \U0001f504 폴백: 전체 링크에서 검색...")
                            all_links = driver.find_elements(By.TAG_NAME, 'a')
                            for link in all_links:
                                href = link.get_attribute('href') or ''
                                if is_blog_post(href) and href not in blog_links:
                                    blog_links.append(href)
                                    self.log_emit(f"    \u2705 순위{len(blog_links)}: {href[:80]}")
                                    if len(blog_links) >= count:
                                        break
                        except Exception:
                            pass

                    self.log_emit(f"  📎 {len(blog_links)}개 블로그 링크 발견")

                    for bi, blog_url in enumerate(blog_links[:count]):
                        try:
                            self.log_emit(f"  📖 [{bi + 1}/{min(len(blog_links), count)}] 본문 수집 중...")
                            driver.get(blog_url)
                            time.sleep(2)

                            try:
                                iframe = WebDriverWait(driver, 5).until(
                                    EC.presence_of_element_located((By.ID, 'mainFrame'))
                                )
                                driver.switch_to.frame(iframe)
                            except Exception:
                                pass

                            body_text = ""
                            title_text = ""
                            selectors = [
                                'div.se-main-container',
                                'div.__se_component_area',
                                'div#postViewArea',
                                'div.post-view',
                                'div#post-area'
                            ]
                            for sel in selectors:
                                try:
                                    elem = driver.find_element(By.CSS_SELECTOR, sel)
                                    body_text = elem.text
                                    if body_text and len(body_text) > 100:
                                        break
                                except Exception:
                                    continue

                            try:
                                title_elem = driver.find_element(By.CSS_SELECTOR,
                                                                 'div.se-title-text, span.pcol1, div.htitle')
                                title_text = title_elem.text
                            except Exception:
                                title_text = keyword

                            driver.switch_to.default_content()

                            if body_text and len(body_text) > 50:
                                article = {
                                    'keyword': keyword,
                                    'title': title_text[:100],
                                    'body': body_text[:3000],
                                    'url': blog_url
                                }
                                all_articles.append(article)

                            done += 1
                            pct = int((done / total) * 100)
                            self.progress_emit(pct)

                        except Exception as e:
                            self.log_emit(f"  ⚠️ 수집 실패: {e}")
                            driver.switch_to.default_content()
                            done += 1

                    self.update_kw_status(ki, f"완료 ({len([a for a in all_articles if a['keyword'] == keyword])}건)")

            finally:
                driver.quit()

            self.collected_articles = all_articles

            result_text = ""
            for i, art in enumerate(all_articles):
                result_text += f"━━━ [{i + 1}] {art['title']} ━━━\n"
                result_text += f"키워드: {art['keyword']}\n"
                result_text += f"URL: {art['url']}\n"
                result_text += f"본문:\n{art['body'][:500]}...\n\n"

            return result_text if result_text else "수집 결과 없음"

        thread = WorkerThread(do_crawl)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.crawl_result.setPlainText(r))
        thread.progress_signal.connect(self.crawl_progress.setValue)
        def _on_crawl_finished():
            self._crawling = False
            self._cancel_crawl = False
            self.crawl_btn.setText("🔍 블로그 수집 시작")
            self.crawl_btn.setStyleSheet("background-color:#2d5a27; color:white; font-weight:bold; padding:8px;")
            self.crawl_progress.setVisible(False)

        thread.finished_signal.connect(_on_crawl_finished)
        thread.start()

    def log_emit(self, msg):
        QTimer.singleShot(0, lambda: self.main.log(msg))

    def progress_emit(self, val):
        QTimer.singleShot(0, lambda: self.crawl_progress.setValue(val))

    def update_kw_status(self, row, status):
        QTimer.singleShot(0, lambda: self.keyword_table.setItem(row, 1, QTableWidgetItem(status)))

    # ──────────── AI 글쓰기 ────────────

    def send_to_ai(self):
        txt = self.crawl_result.toPlainText()
        if not txt.strip():
            self.main.log("⚠️ 먼저 블로그 수집을 실행하세요");
            return
        self.ai_input.setPlainText(txt)
        self.main.log("✅ 수집 결과 → AI 입력 전달 완료")

    def generate_ai(self):
        """수집된 각 기사마다 개별 AI 원고를 생성 (테이블 기반)"""
        if self._generating:
            self._cancel_generate = True
            self.generate_btn.setText("🛑 취소 중...")
            self.main.log("🛑 AI 생성 취소 요청됨...")
            return

        content = self.ai_input.toPlainText().strip()
        if not content:
            self.main.log("⚠️ AI 입력이 비어 있음. '수집→AI' 버튼을 먼저 클릭하세요")
            return

        if not self.collected_articles:
            articles = self._parse_articles_from_text(content)
        else:
            articles = self.collected_articles

        if not articles:
            self.main.log("⚠️ 수집된 기사가 없습니다")
            return

        total = len(articles)
        self._cancel_generate = False
        self._generating = True
        self.generate_btn.setText(f"🛑 생성 취소 (0/{total})")
        self.generate_btn.setStyleSheet("background-color:#8a1a1a; color:white; font-weight:bold; padding:8px;")
        self.ai_gen_progress.setVisible(True)
        self.ai_gen_progress.setValue(0)
        self.error_summary.setVisible(False)
        self.error_summary_label.setVisible(False)

        self.generated_articles = []
        self.article_table.setRowCount(total)
        for i, art in enumerate(articles):
            self.article_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 1, QTableWidgetItem("대기"))
            self.article_table.setItem(i, 2, QTableWidgetItem(art.get('title', '')[:80]))
            self.article_table.setItem(i, 3, QTableWidgetItem("-"))

        service = self.ai_service.currentText()
        prompt = self.ai_prompt.toPlainText().strip()
        char_limit = self.char_count.value()

        options = []
        if self.opt_map.isChecked(): options.append("- [지도:장소명] 형태로 네이버 지도 삽입 위치를 표시해주세요")
        if self.opt_video.isChecked(): options.append("- [영상] 형태로 관련 영상 삽입 위치를 표시해주세요")
        if self.opt_quote.isChecked(): options.append("- 중요 문구는 [인용구]텍스트[/인용구] 형태로 감싸주세요")
        if self.opt_photo.isChecked(): options.append("- 적절한 위치에 [사진] 태그를 3~5개 넣어주세요")
        if self.opt_hashtag.isChecked(): options.append("- 마지막에 관련 해시태그 5~10개를 추가해주세요")
        option_text = "\n".join(options)

        self.main.log(f"🧠 AI 생성 시작 - {service} / 총 {total}건 개별 생성")

        thread = WorkerThread(lambda: None)
        def _ui_update(fn): thread.ui_update_signal.emit(fn)

        def do_generate():
            generated = []
            errors_collection = []
            for idx, art in enumerate(articles):
                if self._cancel_generate:
                    thread.log_signal.emit(f"🛑 AI 생성 취소됨 ({len(generated)}건 처리됨)")
                    break
                post_title = art.get('title', '제목없음')
                thread.log_signal.emit(f"📝 [{idx+1}/{total}] AI 원고 생성 중: {post_title[:40]}...")
                _ui_update(lambda i=idx, t=total: self.generate_btn.setText(f"🛑 생성 취소 ({i+1}/{t})"))
                _ui_update(lambda i=idx: self.article_table.setItem(i, 2, QTableWidgetItem("생성중")))
                _ui_update(lambda i=idx, t=total: self.ai_gen_progress.setValue(int((i / t) * 100)))

                ref_limit = 4500 if "Perplexity" in service else 8000
                ref_content = art.get('body', '')[:ref_limit]
                if not ref_content.strip() or len(ref_content.strip()) < 20:
                    err_msg = '참고 자료 본문이 비어있어 건너뜁니다'
                    thread.log_signal.emit(f"⚠️ [{idx+1}/{total}] {err_msg}")
                    generated.append({'title': post_title, 'content': f'❌ {err_msg}', 'status': '실패', 'source': ref_content, 'error': err_msg})
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: (self.article_table.setItem(i, 2, QTableWidgetItem("❌실패")), self._set_article_check(i, False)))
                    continue

                full_prompt = f"{prompt}\n\n추가 요구사항:\n{option_text}"
                full_prompt += f"\n- 총 글자 수: 약 {char_limit}자\n"
                full_prompt += "- 반드시 완성된 한국어 블로그 글 형태로 작성해주세요\n"
                full_prompt += "- 제목을 첫 줄에 ## 제목 형태로 작성해주세요\n"
                full_prompt += f"\n[참고 자료 제목]: {art.get('title', '')}\n"
                full_prompt += f"[참고 자료 본문]:\n{ref_content}"

                try:
                    if "Gemini" in service:
                        result = call_gemini_text(self.main.config.get('gemini_key', '').strip(), full_prompt, model='gemini-2.5-flash')
                    elif "Perplexity" in service:
                        sys_msg = "당신은 한국어 SEO 블로그 전문 작가입니다. 주어진 참고 자료를 바탕으로 네이버 블로그에 게시할 완성된 포스트를 작성합니다."
                        result = call_perplexity(self.main.config.get('perplex_key', '').strip(), full_prompt, model='sonar-pro', system_message=sys_msg)
                    elif "GPT" in service:
                        result = call_openai_text(self.main.config.get('openai_key', '').strip(), full_prompt, model='gpt-4o')
                    else:
                        result = "❌ 알 수 없는 AI 서비스"

                    gen_title = art.get('title', f'포스트 {idx+1}')
                    for line in result.split('\n'):
                        stripped = line.strip()
                        if stripped.startswith('##'):
                            gen_title = stripped.lstrip('#').strip()
                            break
                        elif stripped and not stripped.startswith('['):
                            gen_title = stripped[:60]
                            break

                    generated.append({'title': gen_title, 'content': result, 'status': '완료', 'source': ref_content, 'keyword': art.get('keyword', '')})
                    # 즉시 발행 대기열 형태로 저장
                    self.generated_articles = list(generated)
                    try:
                        _queue = [{'order': qi+1, 'title': ga['title'], 'content': ga['content'], 'status': 'pending', 'result': ''} for qi, ga in enumerate(generated) if ga.get('status') == '완료']
                        if _queue:
                            with open('publish_queue_blog_incremental.json', 'w', encoding='utf-8') as _qf:
                                json.dump(_queue, _qf, ensure_ascii=False, indent=2)
                            thread.log_signal.emit(f"💾 [{idx+1}/{total}] 발행 대기열 저장 완료 ({len(_queue)}건)")
                    except Exception: pass
                    thread.log_signal.emit(f"✅ [{idx+1}/{total}] 완료: {gen_title[:40]}")
                    _ui_update(lambda i=idx, t=gen_title, c=len(result): (
                        self._set_article_check(i, True),
                        self.article_table.setItem(i, 2, QTableWidgetItem("✅완료")),
                        self.article_table.setItem(i, 3, QTableWidgetItem(t[:80])),
                        self.article_table.setItem(i, 4, QTableWidgetItem(str(c)))
                    ))
                except Exception as e:
                    err_msg = format_error_message(e)
                    thread.log_signal.emit(f"❌ [{idx+1}/{total}] 오류: {err_msg}")
                    generated.append({'title': post_title, 'content': f'❌ 오류: {err_msg}', 'status': '실패', 'source': ref_content, 'error': err_msg})
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: (self.article_table.setItem(i, 2, QTableWidgetItem("❌실패")), self._set_article_check(i, False)))

                if idx < total - 1 and not self._cancel_generate:
                    wait_seconds = 4 if "Perplexity" in service else 2
                    for _ in range(wait_seconds * 2):
                        if self._cancel_generate: break
                        time.sleep(0.5)

            self.generated_articles = generated
            _ui_update(lambda: self.ai_gen_progress.setValue(100))
            success = sum(1 for a in generated if a.get('status') == '완료')
            failed = sum(1 for a in generated if a.get('status') == '실패')
            if errors_collection:
                unique_errors = list(dict.fromkeys(errors_collection))
                error_text = "\n- ".join(unique_errors[:5])
                _ui_update(lambda: (self.error_summary.setPlainText(f"- {error_text}"), self.error_summary.setVisible(True), self.error_summary_label.setVisible(True)))
            thread.log_signal.emit(f"✅ AI 원고 생성 완료 - 총 {total}건 중 {success}건 성공, {failed}건 실패")
            return f"✅ 총 {total}건 원고 생성 완료 ({success}건 성공 / {failed}건 실패)"

        thread.func = do_generate
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.ai_output.setPlainText(str(r) if r else ""))
        def _on_gen_finished():
            self._generating = False
            self._cancel_generate = False
            self.generate_btn.setText("🎯 AI 원고 생성")
            self.generate_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
            self.ai_gen_progress.setVisible(False)
        thread.finished_signal.connect(_on_gen_finished)
        thread.start()

    def _on_article_selected(self):
        rows = self.article_table.selectionModel().selectedRows()
        if not rows: return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            self.ai_output.setPlainText(self.generated_articles[idx].get('content', ''))

    def _copy_selected_article(self):
        rows = self.article_table.selectionModel().selectedRows()
        if not rows:
            self.main.log("⚠️ 원고를 먼저 선택하세요")
            return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            pyperclip.copy(self.generated_articles[idx].get('content', ''))
            self.main.log(f"📋 원고 #{idx+1} 복사 완료")

    def _copy_all_articles(self):
        if not self.generated_articles:
            self.main.log("⚠️ 생성된 원고가 없습니다")
            return
        all_text = ""
        for i, art in enumerate(self.generated_articles):
            all_text += f"\n{'='*60}\n[원고 {i+1}] {art.get('title','')}\n{'='*60}\n"
            all_text += art.get('content', '') + "\n"
        pyperclip.copy(all_text)
        self.main.log(f"📋 전체 {len(self.generated_articles)}건 원고 복사 완료")

    def _update_article_table(self):
        self.article_table.setRowCount(len(self.generated_articles))
        for i, art in enumerate(self.generated_articles):
            is_done = art.get("status") == "완료"
            self._set_article_check(i, checked=is_done)
            self.article_table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 2, QTableWidgetItem(art.get('status', '완료')))
            self.article_table.setItem(i, 3, QTableWidgetItem(art.get('title', '')[:80]))
            content = art.get('content', '')
            self.article_table.setItem(i, 4, QTableWidgetItem(str(len(content)) if is_done else "실패"))
    def _parse_articles_from_text(self, text):
        """수집 결과 텍스트에서 개별 기사를 파싱"""
        articles = []
        blocks = re.split(r'━+\s*\[\d+\]', text)
        for block in blocks:
            if not block.strip():
                continue
            lines = block.strip().split('\n')
            title = lines[0].replace('━', '').strip() if lines else '제목없음'
            body = '\n'.join(lines[1:]).strip()
            # 키워드/URL 라인 제거 후 본문만
            body_lines = []
            for l in lines:
                if l.startswith('키워드:') or l.startswith('URL:'):
                    continue
                body_lines.append(l)
            body = '\n'.join(body_lines).strip()
            if len(body) > 30:
                articles.append({'title': title[:100], 'body': body[:3000], 'keyword': '', 'url': ''})
        return articles if articles else [{'title': '포스트', 'body': text[:3000], 'keyword': '', 'url': ''}]

    # ──────────── 블로그 폴더 로드 ────────────

    def _get_selected_account(self):
        """선택된 계정의 id/pw 반환"""
        sel_id = self.account_combo.currentText().strip()
        accounts = self.main.config.get('naver_accounts', [])
        for acc in accounts:
            if acc.get('id') == sel_id:
                return acc.get('id', ''), acc.get('pw', '')
        # 하위호환
        return self.main.config.get('naver_id', ''), self.main.config.get('naver_pw', '')

    def _save_folder_cache(self):
        try:
            with open('folder_cache.json', 'w', encoding='utf-8') as f:
                json.dump(self._folder_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_blog_folders(self):
        nid, npw = self._get_selected_account()
        if not nid:
            self.main.log("❌ 네이버 계정을 먼저 설정하세요");
            return

        # 캐시 확인
        if nid in self._folder_cache and self._folder_cache[nid]:
            cached = self._folder_cache[nid]
            self.blog_folder.clear()
            self.blog_folder.addItems(cached)
            self.main.log(f"📂 저장된 폴더 사용: {nid} ({len(cached)}개)")
            return

        self.load_folders_btn.setEnabled(False)
        self.load_folders_btn.setText("로딩 중...")
        self.main.log(f"🔄 [{nid}] 블로그 폴더 목록 불러오는 중...")

        blog_id = nid

        def do_load():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            opts = uc.ChromeOptions()
            opts.add_argument('--start-maximized')
            opts.add_argument('--user-data-dir=./chrome_profile')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--disable-features=RendererCodeIntegrity')
            driver = make_uc_driver(opts)
            folders = []

            try:
                # 먼저 로그인 (실패 시 60초 수동 대기)
                if not naver_login_with_fallback(driver, nid, npw, self.main.log):
                    return "❌ 로그인 실패 - 설정에서 계정을 확인하세요"

                # 방법1: API로 카테고리 가져오기
                try:
                    cat_url = f"https://blog.naver.com/NBlogCategoryListAjax.naver?blogId={blog_id}"
                    driver.get(cat_url)
                    time.sleep(2)
                    page_text = driver.page_source

                    if 'categoryName' in page_text:
                        cat_names = re.findall(r'"categoryName"\s*:\s*"([^"]+)"', page_text)
                        for cn in cat_names:
                            try:
                                decoded = cn.encode('utf-8').decode('unicode_escape')
                            except Exception:
                                decoded = cn
                            if decoded and decoded not in folders:
                                folders.append(decoded)
                except Exception:
                    pass

                # 방법2: 블로그 메인에서 스크래핑
                if not folders:
                    try:
                        driver.get(f"https://blog.naver.com/{blog_id}")
                        time.sleep(3)
                        try:
                            iframe = driver.find_element(By.ID, 'mainFrame')
                            driver.switch_to.frame(iframe)
                            time.sleep(1)
                        except Exception:
                            pass

                        selectors = [
                            'div.category a',
                            'ul.category_list li a',
                            'div.area_category a',
                            '#category a',
                            '.blog-category a',
                            'a[href*="categoryNo"]',
                        ]
                        for sel in selectors:
                            try:
                                cats = driver.find_elements(By.CSS_SELECTOR, sel)
                                for cat in cats:
                                    name = cat.text.strip()
                                    if name and name not in ['전체보기', '카테고리', ''] and name not in folders:
                                        folders.append(name)
                            except Exception:
                                continue
                            if folders:
                                break
                        driver.switch_to.default_content()
                    except Exception:
                        pass

                # 방법3: 글쓰기 페이지 카테고리
                if not folders:
                    try:
                        driver.get("https://blog.naver.com/GoBlogWrite.naver")
                        time.sleep(4)
                        try:
                            iframe = driver.find_element(By.ID, 'mainFrame')
                            driver.switch_to.frame(iframe)
                            time.sleep(1)
                        except Exception:
                            pass

                        cat_selectors = [
                            'select#categoryId option',
                            'button.se-category-btn',
                            'div.category_area select option',
                            'ul.se-category-list li',
                            'div[class*="category"] option',
                        ]
                        for sel in cat_selectors:
                            try:
                                items = driver.find_elements(By.CSS_SELECTOR, sel)
                                for item in items:
                                    name = item.text.strip()
                                    if name and name not in ['카테고리 선택', '카테고리', ''] and name not in folders:
                                        folders.append(name)
                            except Exception:
                                continue
                            if folders:
                                break
                    except Exception:
                        pass

                if folders:
                    return "||".join(folders)
                else:
                    return "❌ 카테고리를 찾을 수 없습니다. 블로그에 카테고리가 있는지 확인하세요."
            except Exception as ex:
                return f"❌ 오류: {str(ex)}"
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        def on_result(r):
            if r.startswith("❌"):
                self.main.log(r);
                return
            folder_list = r.split("||")
            self.blog_folder.clear()
            self.blog_folder.addItems(folder_list)
            # 캐시 저장
            self._folder_cache[nid] = folder_list
            self._save_folder_cache()
            self.main.log(f"✅ {len(folder_list)}개 폴더 로드 완료 (캐시 저장됨)")

        thread = WorkerThread(do_load)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.load_folders_btn.setEnabled(True),
            self.load_folders_btn.setText("🔄 폴더 불러오기")
        ))
        thread.start()

    # ──────────── 네이버 자동 발행 ────────────

    def publish_to_naver(self):
        """generated_articles 리스트를 순회하며 각각 블로그에 발행 (순차 처리)"""
        # 취소 토글
        if self._publishing_flag:
            self._cancel_publish = True
            self.publish_btn.setText("🛑 취소 중...")
            self.main.log("🛑 발행 취소 요청됨...")
            return
        self._cancel_publish = False
        self._publishing_flag = True

        # generated_articles가 있으면 다건 발행
        if not self.generated_articles:
            # 단건 호환: 기존 텍스트에서 발행
            content = self.ai_output.toPlainText().strip()
            if not content:
                content = self.generated_content
            if not content:
                self.main.log("⚠️ 발행할 원고가 없습니다. AI 글쓰기를 먼저 실행하세요");
                return
            self.generated_articles = [{'title': '포스트', 'content': content}]

        nid, npw = self._get_selected_account()
        if not nid or not npw:
            self.main.log("❌ 네이버 계정을 먼저 설정하세요");
            return

        folder = self.blog_folder.currentText()
        save_mode = self.save_mode.currentText()
        schedule = self.schedule_time.text() if "예약" in save_mode else ""
        articles = list(self.generated_articles)
        total = len(articles)

        self.publish_btn.setEnabled(False)
        self.publish_btn.setText(f"발행 중... (0/{total})")
        self.publish_progress.setVisible(True)
        self.publish_progress.setValue(0)

        self.main.log(f"🚀 총 {total}건 블로그 발행 시작")

        def do_publish():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.common.keys import Keys
            opts = uc.ChromeOptions()
            opts.add_argument('--start-maximized')
            opts.add_argument('--user-data-dir=./chrome_profile')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--disable-features=RendererCodeIntegrity')
            opts.add_argument('--disable-popup-blocking')
            prefs = {'profile.default_content_setting_values.popups': 0}
            opts.add_experimental_option('prefs', prefs)
            driver = make_uc_driver(opts)
            main_window = driver.current_window_handle

            # ── window.open 차단 ──
            def block_window_open():
                try:
                    driver.execute_script("""
                        if (!window._woBlocked) {
                            window._origOpen = window.open;
                            window.open = function(url) {
                                if (url) window.location.href = url;
                                return window;
                            };
                            window._woBlocked = true;
                        }
                    """)
                except Exception:
                    pass
            # ── "작성중인 글이 있습니다" 팝업 취소 처리 ──
            def dismiss_draft_popup():
                """에디터 진입 시 '작성중인 글이 있습니다' 팝업이 뜨면 취소 버튼 클릭"""
                try:
                    time.sleep(2)
                    cancel_selectors = [
                        'button.se-popup-button-cancel',
                        'button.cancel_btn__WEaBq',
                        'button.cancel_btn',
                        'button[class*="cancel"]',
                        'button.se-cancel',
                    ]
                    # 팝업 텍스트로도 탐지
                    try:
                        popup_texts = driver.find_elements(By.XPATH,
                            "//*[contains(text(),'작성중인') or contains(text(),'작성 중인') or contains(text(),'임시저장')]")
                        if popup_texts:
                            self.log_emit("  ℹ️ '작성중인 글' 팝업 감지 → 취소 클릭 시도")
                            for sel in cancel_selectors:
                                try:
                                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                            # CSS 셀렉터 실패 시 XPATH로 취소/아니오 버튼 찾기
                            cancel_btns = driver.find_elements(By.XPATH,
                                "//button[contains(text(),'취소') or contains(text(),'아니') or contains(text(),'아니오') or contains(text(),'새로')]")
                            for btn in cancel_btns:
                                try:
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료 (XPATH)")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        pass
                except Exception as e:
                    self.log_emit(f"  ℹ️ 팝업 처리 중 예외(무시): {e}")
                return False


            # ── 추가 탭 모두 닫기 ──
            def close_extra_tabs():
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                if not main_window or main_window not in handles:
                    main_window = handles[0]
                for h in handles:
                    if h == main_window:
                        continue
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                try:
                    driver.switch_to.window(main_window)
                except Exception:
                    pass

            # ── 에디터를 단일 탭에서 열기 ──
            def open_editor_single_tab():
                nonlocal main_window
                # 1) 여분 탭 정리
                close_extra_tabs()
                # 2) 현재 탭에서 에디터 URL로 이동 (새 탭이 열리지 않도록 execute_script 사용)
                editor_url = "https://blog.naver.com/GoBlogWrite.naver"
                try:
                    driver.execute_script(f'window.location.href = "{editor_url}";')
                except Exception:
                    driver.get(editor_url)
                time.sleep(5)
                # 3) 혹시라도 새 탭이 열렸으면 에디터 탭만 남기기
                handles = list(driver.window_handles)
                if len(handles) > 1:
                    editor_tab = handles[-1]
                    for h in handles:
                        if h == editor_tab:
                            continue
                        try:
                            driver.switch_to.window(h)
                            driver.close()
                        except Exception:
                            pass
                    driver.switch_to.window(editor_tab)
                    main_window = editor_tab
                else:
                    main_window = handles[0] if handles else driver.current_window_handle
                    driver.switch_to.window(main_window)
                # 4) iframe 전환
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
                try:
                    iframe = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.ID, 'mainFrame'))
                    )
                    driver.switch_to.frame(iframe)
                    time.sleep(1)
                    self.log_emit("  ✅ mainFrame iframe 진입 성공")
                    dismiss_draft_popup()
                except Exception:
                    self.log_emit("  ℹ️ mainFrame 없음, 직접 에디터 접근")
                    dismiss_draft_popup()
                # 5) 에디터 로드 확인
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR,
                            'div.se-title-text, div[data-name="title"], div[contenteditable="true"]'))
                    )
                    self.log_emit("  ✅ 에디터 로드 확인")
                except Exception:
                    self.log_emit("  ⚠️ 에디터 요소 미확인 (계속 진행)")

            # ── 원고 정제 ──
            def sanitize_content(raw):
                lines = raw.split('\n')
                cleaned = []
                for line in lines:
                    s = line.strip()
                    if re.match(r'^[━─═\-]{3,}', s): continue
                    if s == '[이미지 삽입]': continue
                    if re.match(r'^!\[.*\]\(.*\)$', s): continue
                    if re.match(r'^https?://\S+$', s): continue
                    if re.match(r'^📄\s*\[\d+\]', s): continue
                    s = re.sub(r'\[\d+\]', '', s)
                    s = re.sub(r'^#{1,6}\s*', '', s)
                    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
                    s = s.strip()
                    if s:
                        cleaned.append(s)
                return re.sub(r'\n{3,}', '\n\n', '\n'.join(cleaned)).strip()

            success_count = 0
            fail_count = 0

            try:
                self.log_emit("🔑 네이버 로그인 중...")
                if not naver_login_with_fallback(driver, nid, npw, self.log_emit):
                    return "❌ 로그인 실패 - 설정에서 계정을 확인하세요"
                self.log_emit("✅ 로그인 완료")
                main_window = driver.current_window_handle
                close_extra_tabs()
                block_window_open()

                for art_idx, article in enumerate(articles):
                    try:
                        art_title = article.get('title', f'포스트 {art_idx + 1}')
                        art_content = sanitize_content(article.get('content', ''))

                        # ── 본문에서 제목 줄 제거 (제목은 별도 입력) ──
                        content_lines = art_content.split('\n')
                        filtered_lines = []
                        title_stripped = False
                        for cl in content_lines:
                            cl_clean = cl.strip()
                            # 첫 번째 줄이 제목과 동일하면 제거
                            if not title_stripped and cl_clean and (
                                cl_clean == art_title.strip() or
                                re.sub(r'^#{1,6}\s*', '', cl_clean) == art_title.strip() or
                                cl_clean.replace('**', '') == art_title.strip()
                            ):
                                title_stripped = True
                                continue
                            filtered_lines.append(cl)
                        art_content = '\n'.join(filtered_lines).strip()

                        if len(art_content) < 50:
                            self.log_emit(f"\n⏭️ [{art_idx + 1}/{total}] 건너뜀: 정제 후 본문 너무 짧음 — {art_title[:30]}")
                            fail_count += 1
                            continue

                        self.log_emit(f"\n📝 [{art_idx + 1}/{total}] 발행 시작: {art_title[:40]}...")
                        QTimer.singleShot(0, lambda idx=art_idx: self.publish_btn.setText(f"발행 중... ({idx + 1}/{total})"))

                        # ── 에디터 단일 탭 열기 ──
                        open_editor_single_tab()
                        block_window_open()

                        # 폴더 선택
                        if folder:
                            try:
                                cat_select = driver.find_element(By.CSS_SELECTOR, 'select#categoryId')
                                cat_select.click()
                                time.sleep(0.5)
                                for opt in cat_select.find_elements(By.TAG_NAME, 'option'):
                                    if folder in opt.text:
                                        opt.click()
                                        break
                            except Exception:
                                self.log_emit("  ⚠️ 폴더 선택 실패, 기본 카테고리 사용")

                        # 제목 입력 (참고 파일 기준 셀렉터)
                        title_ok = False
                        for sel in [
                            'span.se-placeholder',
                            'div[data-name="title"] div[contenteditable="true"]',
                            'div.se-section-title div.se-text-paragraph',
                            'div.se-title-text',
                        ]:
                            try:
                                title_el = WebDriverWait(driver, 5).until(
                                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                                )
                                try:
                                    title_el.click()
                                except Exception:
                                    from selenium.webdriver.common.action_chains import ActionChains
                                    ActionChains(driver).move_to_element(title_el).click().perform()
                                time.sleep(0.2)
                                pyperclip.copy(art_title)
                                safe_hotkey(driver, 'ctrl', 'a')
                                time.sleep(0.1)
                                safe_hotkey(driver, 'ctrl', 'v')
                                time.sleep(0.3)
                                title_ok = True
                                break
                            except Exception:
                                continue
                        if not title_ok:
                            self.log_emit("  ⚠️ 제목 입력 실패")

                        # 본문 영역 포커스
                        body_focused = False
                        for sel in [
                            'div.se-section-text div[contenteditable="true"]',
                            'div.se-component-content div[contenteditable="true"]',
                            'div.se-text-paragraph',
                            'div[contenteditable="true"]',
                        ]:
                            try:
                                elems = driver.find_elements(By.CSS_SELECTOR, sel)
                                for elem in elems:
                                    if not elem.is_displayed():
                                        continue
                                    try:
                                        driver.execute_script("arguments[0].click();", elem)
                                    except Exception:
                                        elem.click()
                                    time.sleep(0.3)
                                    body_focused = True
                                    break
                                if body_focused:
                                    break
                            except Exception:
                                continue
                        if not body_focused:
                            safe_press(driver, 'tab')
                            time.sleep(0.3)

                        # 본문 입력 (이미지 + 정렬 + 인용구 + 스티커) — v26
                        # v26: 정렬은 본문 입력 *후* Ctrl+A로 전체 선택해서 적용
                        art_content_pp = preprocess_article_lines(art_content)
                        alignment = getattr(self, '_selected_alignment', 'left')
                        photo_token_count = art_content_pp.count('[사진]')
                        photo_paths = self._get_images_for_article(art_idx, photo_token_count)
                        photo_idx = 0
                        # v27: 본문에 [사진] 토큰이 없는데 이미지 풀이 있으면 본문 첫머리에 자동 삽입
                        if photo_token_count == 0 and photo_paths:
                            try:
                                self._insert_image_to_editor(driver, photo_paths[0])
                                photo_idx = 1
                                safe_press(driver, 'enter')
                                time.sleep(0.5)
                            except Exception as auto_img_err:
                                self.log_emit(f"  ⚠️ 자동 이미지 삽입 실패: {auto_img_err}")
                        for raw_line in art_content_pp.split('\n'):
                            line = raw_line.rstrip()
                            if not line.strip():
                                safe_press(driver, 'enter')
                                time.sleep(0.05)
                                continue

                            stripped = line.strip()
                            # [사진] 태그 → 이미지 삽입
                            if stripped in ('[사진]', '[사진 삽입]', '[이미지 삽입]'):
                                if photo_idx < len(photo_paths):
                                    try:
                                        self._insert_image_to_editor(driver, photo_paths[photo_idx])
                                        photo_idx += 1
                                        time.sleep(1)
                                        continue
                                    except Exception as img_err:
                                        self.log_emit(f"  ⚠️ 이미지 삽입 실패: {img_err}")
                                safe_press(driver, 'enter')
                                time.sleep(0.1)
                                continue

                            # 스티커 토큰
                            sticker_idxs = extract_sticker_indices(stripped)
                            content_only = strip_sticker_tokens(stripped)

                            if content_only and looks_like_quote(content_only):
                                insert_quote_block(driver, content_only)
                            elif content_only:
                                pyperclip.copy(content_only)
                                safe_hotkey(driver, 'ctrl', 'v')
                                safe_press(driver, 'enter')
                                time.sleep(0.1)

                            for sidx in sticker_idxs:
                                try:
                                    insert_naver_sticker(driver, sidx)
                                    safe_press(driver, 'enter')
                                except Exception:
                                    pass

                        # v26: 본문 입력 완료 후 전체 선택 → 정렬 적용 (+ JS fallback)
                        try:
                            apply_alignment(driver, alignment, select_all_first=True)
                            self.log_emit(f"  ✅ 본문 정렬 적용: {alignment}")
                        except Exception as al_err:
                            self.log_emit(f"  ⚠️ 정렬 적용 실패: {al_err}")

                        # 저장/발행
                        time.sleep(1)
                        if "임시" in save_mode:
                            clicked = False
                            for sel in [
                                'button[data-testid="save-btn"]',
                                'button.save_btn__Y5f57',
                                'button.save_btn',
                                'button[class*="save"]',
                            ]:
                                try:
                                    btn = WebDriverWait(driver, 3).until(
                                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                                    )
                                    driver.execute_script("arguments[0].click();", btn)
                                    clicked = True
                                    break
                                except Exception:
                                    continue
                            if not clicked:
                                try:
                                    btn = driver.find_element(By.XPATH, '//button[contains(., "임시저장")]')
                                    driver.execute_script("arguments[0].click();", btn)
                                    clicked = True
                                except Exception:
                                    pass
                            if not clicked:
                                raise RuntimeError('임시저장 버튼을 찾지 못했습니다')
                            time.sleep(2)
                            self.log_emit(f"  💾 [{art_idx + 1}/{total}] 임시저장 완료: {art_title[:30]}")
                            driver.execute_script('window.location.href = "https://blog.naver.com";')
                            time.sleep(2)
                        elif "즉시" in save_mode:
                            pub_clicked = False
                            for sel in [
                                'button[data-testid="publish-btn"]',
                                'button.publish_btn__Y5f57',
                                'button.publish_btn',
                                'button[class*="publish"]',
                            ]:
                                try:
                                    btn = WebDriverWait(driver, 3).until(
                                        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                                    )
                                    driver.execute_script("arguments[0].click();", btn)
                                    pub_clicked = True
                                    break
                                except Exception:
                                    continue
                            if not pub_clicked:
                                safe_hotkey(driver, 'ctrl', 'shift', 'p')
                            time.sleep(2)
                            for sel in [
                                'button.se-popup-button-confirm',
                                'button.confirm_btn__WEaBq',
                                'button.confirm_btn',
                                'button[class*="confirm"]',
                            ]:
                                try:
                                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                                    driver.execute_script("arguments[0].click();", btn)
                                    break
                                except Exception:
                                    continue
                            time.sleep(3)
                            self.log_emit(f"  📤 [{art_idx + 1}/{total}] 즉시발행 완료")
                        elif "예약" in save_mode:
                            self.log_emit(f"  ⏰ 예약발행: {schedule}")

                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_extra_tabs()
                        success_count += 1

                    except Exception as e:
                        self.log_emit(f"  ❌ [{art_idx + 1}/{total}] 발행 실패: {e}")
                        fail_count += 1
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_extra_tabs()

                    pct = int(((art_idx + 1) / total) * 100)
                    self.progress_emit(pct)
                    time.sleep(3)

                return f"✅ 블로그 발행 완료! 성공: {success_count}건, 실패: {fail_count}건"

            except Exception as e:
                return f"❌ 발행 오류: {str(e)}"
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        thread = WorkerThread(do_publish)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(self.main.log)
        thread.progress_signal.connect(self.publish_progress.setValue)

        def on_publish_finished():
            self._publishing_flag = False
            self.publish_btn.setEnabled(True)
            self.publish_btn.setText("🔥 네이버 블로그 자동 발행")
            self.publish_progress.setVisible(False)

        thread.finished_signal.connect(on_publish_finished)
        thread.start()

# ═══════════════════════════════════════════════════════════════
#  인기글 수집 페이지
# ═══════════════════════════════════════════════════════════════

class PopularPostPage(QWidget):
    """네이버 블로그 인기글 수집 → AI 글쓰기 → 블로그 발행"""

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self.generated_articles = []
        self._popular_collected_text = ""
        self._popular_collected_items = []
        self._cancel_collect = False
        self._cancel_generate = False
        self._cancel_publish = False
        self._collecting = False
        self._generating = False
        self._publishing_flag = False
        # 폴더 캐시 (계정별)
        self._folder_cache = self._load_folder_cache()

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        title = QLabel("🔥 네이버 블로그 인기글 수집 → AI 글쓰기 → 블로그 발행")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        scroll_layout.addWidget(title)

        desc = QLabel("특정 블로그의 인기글을 수집하여 AI로 새 글을 작성합니다")
        desc.setStyleSheet("color:#888; margin-bottom:8px;")
        scroll_layout.addWidget(desc)

        # ══════════════════════════════════════
        # STEP 1: 인기글 수집
        # ══════════════════════════════════════
        step1 = QGroupBox("STEP 1️⃣  네이버 블로그 인기글 수집")
        s1 = QVBoxLayout(step1)

        s1.addWidget(QLabel("블로그 URL/ID (한 줄에 하나씩, 최대 10개):"))
        self.popular_blog_urls = QTextEdit()
        self.popular_blog_urls.setMaximumHeight(120)
        self.popular_blog_urls.setPlaceholderText(
            "예시 (한 줄에 하나씩 입력):\n"
            "https://m.blog.naver.com/블로그ID1\n"
            "블로그ID2\n"
            "https://blog.naver.com/블로그ID3\n"
            "...최대 10개까지 입력 가능"
        )

        s1.addWidget(self.popular_blog_urls)

        h_excel = QHBoxLayout()
        self.excel_import_btn = QPushButton("📥 엑셀 대량 등록 (.xlsx)")
        self.excel_import_btn.clicked.connect(self.import_from_excel)
        self.excel_import_btn.setStyleSheet("background-color:#2d6a4f; color:white; font-weight:bold; padding:6px;")
        h_excel.addWidget(self.excel_import_btn)

        self.excel_template_btn = QPushButton("📋 엑셀 양식 다운로드")
        self.excel_template_btn.clicked.connect(self.download_excel_template)
        h_excel.addWidget(self.excel_template_btn)
        s1.addLayout(h_excel)

        h_pop_opt = QHBoxLayout()
        h_pop_opt.addWidget(QLabel("블로그당 수집 개수:"))
        self.popular_count = QSpinBox()
        self.popular_count.setRange(1, 30)
        self.popular_count.setValue(10)
        h_pop_opt.addWidget(self.popular_count)

        self.blog_count_label = QLabel("등록된 블로그: 0개")
        self.blog_count_label.setStyleSheet("color:#FFD700; font-weight:bold;")
        h_pop_opt.addWidget(self.blog_count_label)
        h_pop_opt.addStretch()
        s1.addLayout(h_pop_opt)

        self.popular_blog_urls.textChanged.connect(self._update_blog_count)

        self.popular_collect_btn = QPushButton("🔥 인기글 수집 시작")
        self.popular_collect_btn.clicked.connect(self.start_popular_collect)
        self.popular_collect_btn.setStyleSheet("background-color:#8B4513; color:white; font-weight:bold; padding:8px;")
        s1.addWidget(self.popular_collect_btn)

        self.popular_progress = QProgressBar()
        self.popular_progress.setVisible(False)
        s1.addWidget(self.popular_progress)

        self.popular_result = QTextBrowser()
        self.popular_result.setMaximumHeight(120)
        self.popular_result.setPlaceholderText("수집 요약이 여기에 표시됩니다...")
        s1.addWidget(self.popular_result)

        # ── 수집 결과 표 (체크박스 + 선택 발행) ──
        s1.addWidget(QLabel("📋 수집된 인기글 (체크한 항목만 AI 입력으로 전달):"))
        self.collect_popular_table = QTableWidget()
        self.collect_popular_table.setColumnCount(4)
        self.collect_popular_table.setHorizontalHeaderLabels(["선택", "블로그", "제목", "본문 요약"])
        self.collect_popular_table.setColumnWidth(0, 50)
        self.collect_popular_table.setColumnWidth(1, 120)
        self.collect_popular_table.setColumnWidth(2, 320)
        self.collect_popular_table.horizontalHeader().setStretchLastSection(True)
        self.collect_popular_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.collect_popular_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.collect_popular_table.itemSelectionChanged.connect(self._on_popular_selected)
        self.collect_popular_table.setMinimumHeight(180)
        s1.addWidget(self.collect_popular_table)

        h_pop_sel = QHBoxLayout()
        btn_pop_all = QPushButton("☑️ 전체 선택")
        btn_pop_all.clicked.connect(lambda: self._toggle_all_popular(True))
        h_pop_sel.addWidget(btn_pop_all)
        btn_pop_none = QPushButton("⬜ 전체 해제")
        btn_pop_none.clicked.connect(lambda: self._toggle_all_popular(False))
        h_pop_sel.addWidget(btn_pop_none)
        h_pop_sel.addStretch()
        s1.addLayout(h_pop_sel)

        scroll_layout.addWidget(step1)

        # ══════════════════════════════════════
        # STEP 2: AI 글쓰기 (개별 원고 생성)
        # ══════════════════════════════════════
        step2 = QGroupBox("STEP 2️⃣  AI 글쓰기 (인기글별 개별 원고 생성)")
        s2 = QVBoxLayout(step2)

        h2a = QHBoxLayout()
        h2a.addWidget(QLabel("AI 서비스:"))
        self.ai_model = QComboBox()
        self.ai_model.addItems(["Perplexity (sonar-pro)", "Gemini (gemini-2.5-flash)", "GPT (gpt-4o)"])
        h2a.addWidget(self.ai_model, 2)

        h2a.addWidget(QLabel("글자 수:"))
        self.char_count = QSpinBox()
        self.char_count.setRange(500, 10000)
        self.char_count.setValue(2000)
        self.char_count.setSingleStep(500)
        h2a.addWidget(self.char_count)
        s2.addLayout(h2a)

        self.ai_prompt = QTextEdit()
        self.ai_prompt.setPlaceholderText("AI에게 전달할 프롬프트...")
        self.ai_prompt.setPlainText(
            "아래 인기글 콘텐츠를 참고하여 SEO 최적화된 네이버 블로그 포스트를 작성해주세요.\n"
            "- 자연스러운 말투 (1인칭 경험담/의견)\n"
            "- 소제목 3개 이상 (##)\n"
            "- 핵심 키워드 자연스럽게 5회 이상 배치\n"
            "- [사진] 태그로 사진 삽입 위치 표시\n"
            "- [인용구]텍스트[/인용구] 형태로 강조 문구 표시\n"
            "- 마지막에 해시태그 5개 추가"
        )
        self.ai_prompt.setMaximumHeight(120)
        s2.addWidget(QLabel("프롬프트:"))
        s2.addWidget(self.ai_prompt)

        h2b = QHBoxLayout()
        self.send_to_ai_btn = QPushButton("⬇️ 인기글 → AI 입력")
        self.send_to_ai_btn.clicked.connect(self.send_popular_to_ai)
        h2b.addWidget(self.send_to_ai_btn)

        self.gen_btn = QPushButton("🎯 전체 AI 원고 생성 (개별)")
        self.gen_btn.clicked.connect(self.generate_ai)
        self.gen_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
        h2b.addWidget(self.gen_btn)
        s2.addLayout(h2b)

        self.ai_progress = QProgressBar()
        self.ai_progress.setVisible(False)
        s2.addWidget(self.ai_progress)

        self.ai_input = QTextEdit()
        self.ai_input.setPlaceholderText("인기글 데이터가 AI 입력으로 전달됩니다...")
        self.ai_input.setMaximumHeight(100)
        s2.addWidget(QLabel("AI 입력 (수집 데이터):"))
        s2.addWidget(self.ai_input)

        # ── 원고 리스트 테이블 (체크박스 + 선택 발행) ──
        s2.addWidget(QLabel("📋 생성된 원고 리스트 (체크한 항목만 발행):"))
        self.article_table = QTableWidget()
        self.article_table.setColumnCount(5)
        self.article_table.setHorizontalHeaderLabels(["선택", "번호", "상태", "제목", "글자수"])
        self.article_table.horizontalHeader().setStretchLastSection(True)
        self.article_table.setColumnWidth(0, 50)
        self.article_table.setColumnWidth(1, 50)
        self.article_table.setColumnWidth(2, 70)
        self.article_table.setColumnWidth(3, 350)
        self.article_table.setColumnWidth(4, 70)
        self.article_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.article_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.article_table.itemSelectionChanged.connect(self._on_article_selected)
        self.article_table.setMinimumHeight(150)
        s2.addWidget(self.article_table)

        h_art_sel = QHBoxLayout()
        btn_art_all = QPushButton("☑️ 전체 선택")
        btn_art_all.clicked.connect(lambda: self._toggle_all_articles(True))
        h_art_sel.addWidget(btn_art_all)
        btn_art_none = QPushButton("⬜ 전체 해제")
        btn_art_none.clicked.connect(lambda: self._toggle_all_articles(False))
        h_art_sel.addWidget(btn_art_none)
        h_art_sel.addStretch()
        s2.addLayout(h_art_sel)

        self.ai_output = QTextBrowser()
        self.ai_output.setPlaceholderText("원고를 선택하면 여기에 표시됩니다...")
        s2.addWidget(QLabel("📄 선택된 원고 미리보기:"))
        s2.addWidget(self.ai_output)

        h2c = QHBoxLayout()
        copy_btn = QPushButton("📋 선택 원고 복사")
        copy_btn.clicked.connect(self._copy_selected_article)
        h2c.addWidget(copy_btn)
        copy_all_btn = QPushButton("📋 전체 원고 복사")
        copy_all_btn.clicked.connect(self._copy_all_articles)
        h2c.addWidget(copy_all_btn)
        save_btn = QPushButton("💾 전체 원고 저장")
        save_btn.clicked.connect(self.save_draft)
        h2c.addWidget(save_btn)
        export_btn = QPushButton("📊 엑셀로 내보내기")
        export_btn.clicked.connect(self._export_articles_excel)
        h2c.addWidget(export_btn)
        import_btn = QPushButton("📥 엑셀 원고 불러오기")
        import_btn.clicked.connect(self._import_articles_excel)
        h2c.addWidget(import_btn)
        s2.addLayout(h2c)

        scroll_layout.addWidget(step2)

        # ══════════════════════════════════════
        # STEP 2.5: 이미지 관리
        # ══════════════════════════════════════
        step_img = QGroupBox("🖼️ STEP 2.5  이미지 관리 (본문 [사진] 위치에 삽입)")
        si = QVBoxLayout(step_img)

        si.addWidget(QLabel("원고 본문에 [사진] 태그가 있으면 발행 시 해당 위치에 이미지가 삽입됩니다."))

        # 이미지 리스트 테이블
        self.image_table = QTableWidget()
        self.image_table.setColumnCount(4)
        self.image_table.setHorizontalHeaderLabels(["번호", "소스", "설명/프롬프트", "파일경로"])
        self.image_table.horizontalHeader().setStretchLastSection(True)
        self.image_table.setColumnWidth(0, 40)
        self.image_table.setColumnWidth(1, 70)
        self.image_table.setColumnWidth(2, 250)
        self.image_table.setMinimumHeight(120)
        self.image_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        si.addWidget(self.image_table)
        self._image_pool = []  # [{'source':'upload'|'dalle', 'desc':'...', 'path':'...'}, ...]

        h_img_btns = QHBoxLayout()

        self.img_upload_btn = QPushButton("📁 이미지 직접 업로드")
        self.img_upload_btn.clicked.connect(self._upload_images)
        self.img_upload_btn.setStyleSheet("background-color:#2d6a4f; color:white; font-weight:bold; padding:6px;")
        h_img_btns.addWidget(self.img_upload_btn)

        self.img_dalle_btn = QPushButton("🤖 AI 이미지 생성 (ChatGPT)")
        self.img_dalle_btn.clicked.connect(self._generate_dalle_images)
        self.img_dalle_btn.setStyleSheet("background-color:#4a1a8a; color:white; font-weight:bold; padding:6px;")
        h_img_btns.addWidget(self.img_dalle_btn)

        self.img_del_btn = QPushButton("🗑️ 선택 삭제")
        self.img_del_btn.clicked.connect(self._delete_selected_image)
        h_img_btns.addWidget(self.img_del_btn)

        self.img_clear_btn = QPushButton("🗑️ 전체 삭제")
        self.img_clear_btn.clicked.connect(self._clear_all_images)
        h_img_btns.addWidget(self.img_clear_btn)
        si.addLayout(h_img_btns)

        # AI 이미지 생성 옵션
        h_dalle_opt = QHBoxLayout()
        h_dalle_opt.addWidget(QLabel("DALL-E 프롬프트:"))
        self.dalle_prompt = QLineEdit()
        self.dalle_prompt.setPlaceholderText("이미지 설명을 입력하세요 (예: 서울 야경, 커피 한 잔)")
        h_dalle_opt.addWidget(self.dalle_prompt, 4)

        h_dalle_opt.addWidget(QLabel("크기:"))
        self.dalle_size = QComboBox()
        self.dalle_size.addItems(["1024x1024", "1024x1536", "1536x1024"])
        h_dalle_opt.addWidget(self.dalle_size)

        h_dalle_opt.addWidget(QLabel("장수:"))
        self.dalle_count = QSpinBox()
        self.dalle_count.setRange(1, 10)
        self.dalle_count.setValue(1)
        h_dalle_opt.addWidget(self.dalle_count)
        si.addLayout(h_dalle_opt)

        # 자동 생성 옵션
        h_auto = QHBoxLayout()
        self.auto_dalle_check = QCheckBox("원고별 자동 이미지 생성 (원고 제목 기반)")
        self.auto_dalle_check.setToolTip("원고 생성 시 각 원고의 제목을 프롬프트로 DALL-E 이미지를 자동 생성합니다")
        h_auto.addWidget(self.auto_dalle_check)

        self.auto_dalle_count = QSpinBox()
        self.auto_dalle_count.setRange(1, 5)
        self.auto_dalle_count.setValue(1)
        h_auto.addWidget(QLabel("원고당 이미지:"))
        h_auto.addWidget(self.auto_dalle_count)
        h_auto.addStretch()
        si.addLayout(h_auto)

        self.img_status = QLabel("")
        self.img_status.setStyleSheet("color:#888; font-size:11px;")
        si.addWidget(self.img_status)

        scroll_layout.addWidget(step_img)

        # ══════════════════════════════════════
        # STEP 3: 네이버 블로그 자동 발행
        # ══════════════════════════════════════
        step3 = QGroupBox("STEP 3️⃣  네이버 블로그 자동 발행")
        s3 = QVBoxLayout(step3)

        h3_acc = QHBoxLayout()
        h3_acc.addWidget(QLabel("발행 계정:"))
        self.account_combo = QComboBox()
        accounts = self.main.config.get('naver_accounts', [])
        if not accounts and self.main.config.get('naver_id'):
            accounts = [{'id': self.main.config.get('naver_id', '')}]
        for acc in accounts:
            self.account_combo.addItem(acc.get('id', ''))
        h3_acc.addWidget(self.account_combo, 2)
        s3.addLayout(h3_acc)

        h3a = QHBoxLayout()
        h3a.addWidget(QLabel("블로그 폴더:"))
        self.blog_folder = QComboBox()
        self.blog_folder.setEditable(True)
        h3a.addWidget(self.blog_folder, 2)

        self.load_folders_btn = QPushButton("🔄 폴더 불러오기")
        self.load_folders_btn.clicked.connect(self.load_blog_folders)
        h3a.addWidget(self.load_folders_btn)
        s3.addLayout(h3a)

        h3b = QHBoxLayout()
        h3b.addWidget(QLabel("저장 형태:"))
        self.save_mode = QComboBox()
        self.save_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        # v24: 본문 정렬 선택
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["⬅️ 좌측 정렬", "⬆️ 가운데 정렬", "➡️ 우측 정렬", "↔️ 양쪽 정렬"])
        self.alignment_combo.currentIndexChanged.connect(
            lambda i: setattr(self, '_selected_alignment', ['left','center','right','justify'][i])
        )
        self._selected_alignment = 'left'
        h3b.addWidget(self.save_mode)
        h3b.addWidget(QLabel("정렬:"))
        h3b.addWidget(self.alignment_combo)
        s3.addLayout(h3b)

        self.publish_btn = QPushButton("🚀 블로그 발행")
        self.publish_btn.clicked.connect(self.publish_to_blog)
        self.publish_btn.setStyleSheet("background-color:#8B0000; color:white; font-weight:bold; padding:8px;")
        s3.addWidget(self.publish_btn)

        self.pub_progress = QProgressBar()
        self.pub_progress.setVisible(False)
        s3.addWidget(self.pub_progress)

        scroll_layout.addWidget(step3)

        # 스크롤 영역
        scroll = QScrollArea()
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        main_layout = QVBoxLayout(self)
        main_layout.addWidget(scroll)

    def _emit_progress(self, bar, val):
        QTimer.singleShot(0, lambda: bar.setValue(val))

    def log_emit(self, msg):
        QTimer.singleShot(0, lambda: self.main.log(msg))

    # ─── 폴더 캐시 관리 ───
    def _load_folder_cache(self):
        """계정별 폴더 캐시를 파일에서 로드"""
        try:
            with open('folder_cache.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_folder_cache(self):
        """계정별 폴더 캐시를 파일에 저장"""
        try:
            with open('folder_cache.json', 'w', encoding='utf-8') as f:
                json.dump(self._folder_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_selected_account(self):
        """선택된 계정의 id/pw 반환"""
        sel_id = self.account_combo.currentText().strip() if hasattr(self, 'account_combo') else ''
        accounts = self.main.config.get('naver_accounts', [])
        for acc in accounts:
            if acc.get('id') == sel_id:
                return acc.get('id', ''), acc.get('pw', '')
        return self.main.config.get('naver_id', ''), self.main.config.get('naver_pw', '')
    def _clear_folder_cache(self, account_id):
        """특정 계정의 폴더 캐시 삭제"""
        if account_id in self._folder_cache:
            del self._folder_cache[account_id]
            self._save_folder_cache()
            self.blog_folder.clear()
            self.main.log(f"🗑️ {account_id} 폴더 캐시 삭제됨")

    def _on_account_changed(self, account_id):
        """계정 변경 시 캐시된 폴더 자동 로드"""
        account_id = account_id.strip()
        if account_id and account_id in self._folder_cache:
            self.blog_folder.clear()
            self.blog_folder.addItems(self._folder_cache[account_id])
            self.main.log(f"📂 {account_id} 저장된 폴더 자동 로드 ({len(self._folder_cache[account_id])}개)")
        else:
            self.blog_folder.clear()

    # ─── 인기글 수집 ───
    def _parse_blog_ids(self):
        """입력된 텍스트에서 블로그 ID 목록 추출 (최대 10개)"""
        import urllib.parse as urlparse
        raw_lines = self.popular_blog_urls.toPlainText().strip().split('\n')
        blog_ids = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            bid = line
            if 'blog.naver.com/' in line:
                parsed = urlparse.urlparse(line)
                path = parsed.path.strip('/')
                if path:
                    bid = path.split('/')[0]
            bid = bid.strip()
            if bid and bid not in blog_ids:
                blog_ids.append(bid)
            if len(blog_ids) >= 10:
                break
        return blog_ids

    def _update_blog_count(self):
        ids = self._parse_blog_ids()
        self.blog_count_label.setText(f"등록된 블로그: {len(ids)}개")

    def import_from_excel(self):
        """엑셀 파일에서 블로그 ID/URL 대량 불러오기"""
        path, _ = QFileDialog.getOpenFileName(
            self, "엑셀 파일 선택", "", "Excel 파일 (*.xlsx *.xls);;모든 파일 (*)"
        )
        if not path:
            return
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True)
            ws = wb.active
            blog_ids = []
            for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
                val = str(row[0] or '').strip()
                if val and val not in blog_ids:
                    blog_ids.append(val)
                if len(blog_ids) >= 10:
                    break
            wb.close()

            if blog_ids:
                existing = self.popular_blog_urls.toPlainText().strip()
                new_text = '\n'.join(blog_ids)
                if existing:
                    new_text = existing + '\n' + new_text
                # 최대 10개로 제한
                all_lines = [l.strip() for l in new_text.split('\n') if l.strip()]
                self.popular_blog_urls.setPlainText('\n'.join(all_lines[:10]))
                self.main.log(f"📥 엑셀에서 {len(blog_ids)}개 블로그 ID 불러옴")
            else:
                self.main.log("⚠️ 엑셀에서 블로그 ID를 찾을 수 없습니다")
        except ImportError:
            self.main.log("❌ openpyxl 패키지가 필요합니다: pip install openpyxl")
        except Exception as e:
            self.main.log(f"❌ 엑셀 불러오기 오류: {e}")

    def download_excel_template(self):
        """블로그 대량 등록용 엑셀 양식 다운로드"""
        path, _ = QFileDialog.getSaveFileName(
            self, "엑셀 양식 저장", "블로그_인기글_양식.xlsx", "Excel 파일 (*.xlsx)"
        )
        if not path:
            return
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "블로그 목록"

            # 헤더
            ws['A1'] = '블로그 URL 또는 ID'
            ws['A1'].font = Font(bold=True, color='FFFFFF')
            ws['A1'].fill = PatternFill('solid', fgColor='4472C4')
            ws['A1'].alignment = Alignment(horizontal='center')
            ws.column_dimensions['A'].width = 50

            # 예시 데이터
            ws['A2'] = 'https://m.blog.naver.com/블로그ID1'
            ws['A3'] = 'blog_id_2'
            ws['A4'] = 'https://blog.naver.com/blog_id_3'
            for r in range(2, 5):
                ws[f'A{r}'].font = Font(color='888888', italic=True)

            # 안내
            ws['C1'] = '※ A열에 블로그 URL 또는 ID를 한 줄에 하나씩 입력 (최대 10개)'
            ws['C1'].font = Font(color='FF0000')

            wb.save(path)
            self.main.log(f"📋 엑셀 양식 저장 완료: {path}")
        except ImportError:
            self.main.log("❌ openpyxl 패키지가 필요합니다: pip install openpyxl")
        except Exception as e:
            self.main.log(f"❌ 양식 저장 오류: {e}")

    def start_popular_collect(self):
        # 취소 토글: 수집 중이면 취소
        if self._collecting:
            self._cancel_collect = True
            self.popular_collect_btn.setText("🛑 취소 중...")
            self.main.log("🛑 수집 취소 요청됨...")
            return

        blog_ids = self._parse_blog_ids()
        if not blog_ids:
            self.main.log("⚠️ 블로그 URL 또는 ID를 입력하세요")
            return

        if len(blog_ids) > 10:
            blog_ids = blog_ids[:10]
            self.main.log("⚠️ 최대 10개까지만 수집합니다")

        max_count = self.popular_count.value()
        self._cancel_collect = False
        self._collecting = True

        self.popular_collect_btn.setText(f"🛑 수집 취소 (0/{len(blog_ids)})")
        self.popular_collect_btn.setStyleSheet("background-color:#8a1a1a; color:white; font-weight:bold; padding:8px;")
        self.popular_progress.setVisible(True)
        self.popular_progress.setValue(0)

        def do_popular_multi():
            all_results = []
            all_collected = []
            total = len(blog_ids)
            for i, bid in enumerate(blog_ids):
                if self._cancel_collect:
                    self.log_emit(f"🛑 수집 취소됨 ({i}건 수집됨)")
                    break
                self.log_emit(f"🔥 [{i+1}/{total}] {bid} 인기글 수집 중...")
                QTimer.singleShot(0, lambda idx=i, t=total: self.popular_collect_btn.setText(f"🛑 수집 취소 ({idx+1}/{t})"))
                base_progress = int(i / total * 100)
                self._emit_progress(self.popular_progress, base_progress)

                result = self._crawl_blog_popular_single(bid, max_count, base_progress, int(100 / total))
                if result:
                    all_results.append(result.get('display', ''))
                    if result.get('text'):
                        all_collected.append(result['text'])
                time.sleep(0.5)

            self._popular_collected_text = "\n\n===== 다음 블로그 =====\n\n".join(all_collected)
            self._popular_collected_items = self._parse_popular_items_from_text(self._popular_collected_text)
            self._emit_progress(self.popular_progress, 100)
            summary = f"✅ 총 {total}개 블로그에서 인기글 수집 완료\n\n"
            return summary + "\n\n".join(all_results)

        thread = WorkerThread(do_popular_multi)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def _on_collect_result(r):
            self.popular_result.setPlainText(r)
            items = list(getattr(self, '_popular_collected_items', []) or [])
            if not items:
                items = self._parse_popular_items_from_text(getattr(self, '_popular_collected_text', '') or '')
                self._popular_collected_items = items
            self._populate_popular_collect_table(items)
        thread.result_signal.connect(_on_collect_result)
        def _on_collect_finished():
            self._collecting = False
            self._cancel_collect = False
            self.popular_collect_btn.setText("🔥 인기글 수집 시작")
            self.popular_collect_btn.setStyleSheet("background-color:#8B4513; color:white; font-weight:bold; padding:8px;")
            self.popular_progress.setVisible(False)

        thread.finished_signal.connect(_on_collect_finished)
        thread.start()

    def _crawl_blog_popular(self, blog_id, max_count):
        """단일 블로그 호환 래퍼"""
        result = self._crawl_blog_popular_single(blog_id, max_count, 0, 100)
        if result:
            self._popular_collected_text = result.get('text', '')
            return result.get('display', '')
        return "❌ 수집 실패"

    def _crawl_blog_popular_single(self, blog_id, max_count, base_progress=0, progress_range=100):
        """네이버 블로그 인기글 API로 수집 (단일 블로그, dict 반환)"""
        self._emit_progress(self.popular_progress, base_progress + int(progress_range * 0.1))
        self.log_emit(f"🔥 [{blog_id}] 인기글 수집 시작...")

        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
            'Referer': f'https://m.blog.naver.com/{blog_id}?tab=1'
        }

        try:
            api_url = f'https://m.blog.naver.com/api/blogs/{blog_id}/popular-post-list?countPerPage={max_count}'
            r = requests.get(api_url, headers=headers, timeout=15)
            self._emit_progress(self.popular_progress, base_progress + int(progress_range * 0.4))

            if r.status_code != 200:
                self.log_emit(f"❌ [{blog_id}] API 응답 오류: {r.status_code}")
                return None

            data = r.json()
            if not data.get('isSuccess'):
                self.log_emit(f"❌ [{blog_id}] API 응답 실패")
                return None

            popular_list = data.get('result', {}).get('popularPostList', [])
            if not popular_list:
                self.log_emit(f"⚠️ [{blog_id}] 인기글이 없습니다")
                return None

            self._emit_progress(self.popular_progress, base_progress + int(progress_range * 0.6))
            self.log_emit(f"📋 {len(popular_list)}개 인기글 발견")

            results = []
            collected_texts = []
            import html as html_mod

            for idx, post in enumerate(popular_list[:max_count]):
                title = post.get('titleWithInspectMessage', '').strip()
                if not title:
                    title = post.get('title', '제목 없음').strip()
                log_no = post.get('logNo', '')
                brief = post.get('briefContents', '').strip()
                category = post.get('categoryName', '')
                sympathy = post.get('sympathyCnt', 0)
                comment = post.get('commentCnt', 0)
                post_url = f"https://blog.naver.com/{blog_id}/{log_no}"

                entry = f"[{idx+1}] {title}\n"
                if category:
                    entry += f"    카테고리: {category}\n"
                entry += f"    공감: {sympathy} | 댓글: {comment}\n"
                entry += f"    URL: {post_url}\n"
                if brief:
                    brief_clean = html_mod.unescape(brief)[:300]
                    entry += f"    요약: {brief_clean}\n"

                results.append(entry)
                collected_texts.append(f"제목: {title}\n내용: {html_mod.unescape(brief) if brief else ''}")

                progress = 60 + int((idx + 1) / min(len(popular_list), max_count) * 35)
                self._emit_progress(self.popular_progress, progress)

            self._popular_collected_text = "\n\n---\n\n".join(collected_texts)

            # 본문 상세 수집 시도 (병렬 처리로 속도 개선)
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def fetch_post_detail(post_info):
                """개별 포스트 본문을 가져오는 함수"""
                log_no, title, brief_raw = post_info
                try:
                    post_url = f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}"
                    pr = requests.get(post_url, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }, timeout=10)
                    if pr.status_code == 200:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(pr.text, 'html.parser')
                        content_div = soup.select_one('div.se-main-container') or soup.select_one('#postViewArea') or soup.select_one('.post-view')
                        if content_div:
                            body_text = content_div.get_text('\n', strip=True)[:1000]
                            return f"제목: {title}\n본문:\n{body_text}"
                except Exception:
                    pass
                if brief_raw:
                    return f"제목: {title}\n본문:\n{html_mod.unescape(brief_raw)}"
                return None

            detailed_texts = []
            post_infos = []
            for post in popular_list[:max_count]:
                log_no = post.get('logNo', '')
                title = post.get('titleWithInspectMessage', '') or post.get('title', '')
                brief = post.get('briefContents', '')
                if log_no:
                    post_infos.append((log_no, title, brief))

            self.log_emit(f"📥 [{blog_id}] {len(post_infos)}개 포스트 본문 병렬 수집 중...")
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(fetch_post_detail, info): info for info in post_infos}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        detailed_texts.append(result)

            if detailed_texts:
                self._popular_collected_text = "\n\n---\n\n".join(detailed_texts)

            self._emit_progress(self.popular_progress, base_progress + progress_range)
            result_text = f"✅ {blog_id} 블로그 인기글 {len(results)}개 수집 완료\n\n" + "\n".join(results)
            self.log_emit(f"✅ {blog_id} → {len(results)}개 인기글 수집 완료")
            collected = "\n\n---\n\n".join(detailed_texts) if detailed_texts else self._popular_collected_text
            return {'display': result_text, 'text': collected}

        except Exception as e:
            self.log_emit(f"❌ [{blog_id}] 인기글 수집 오류: {e}")
            return None

    # ─── 체크박스/선택 헬퍼 (v3 패치) ───
    def _toggle_all_popular(self, state: bool):
        cs = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for r in range(self.collect_popular_table.rowCount()):
            it = self.collect_popular_table.item(r, 0)
            if it is not None:
                it.setCheckState(cs)
        self.main.log(f"📋 수집 인기글 {'전체 선택' if state else '전체 해제'} ({self.collect_popular_table.rowCount()}건)")

    def _get_checked_popular_indices(self):
        out = []
        for r in range(self.collect_popular_table.rowCount()):
            it = self.collect_popular_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(r)
        return out

    def _toggle_all_articles(self, state: bool):
        cs = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for r in range(self.article_table.rowCount()):
            it = self.article_table.item(r, 0)
            if it is not None:
                it.setCheckState(cs)
        self.main.log(f"📋 원고 {'전체 선택' if state else '전체 해제'} ({self.article_table.rowCount()}건)")

    def _get_checked_article_indices(self):
        out = []
        for r in range(self.article_table.rowCount()):
            it = self.article_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(r)
        return out

    def _set_article_check(self, row, checked=True):
        it = QTableWidgetItem()
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.article_table.setItem(row, 0, it)

    def _parse_popular_items_from_text(self, raw_text):
        """수집된 인기글 텍스트를 체크박스 표/AI 전달용 dict 목록으로 변환한다."""
        items = []
        chunks = re.split(r'\n={3,}[^=]*={3,}\n|\n-{3,}\n', raw_text or '')
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            title = ''
            body_lines = []
            url = ''
            blog_id = ''
            in_body = False
            for line in chunk.split('\n'):
                stripped = line.strip()
                if stripped.startswith('제목:'):
                    title = stripped.split('제목:', 1)[1].strip()
                    in_body = False
                elif stripped.startswith('블로그:'):
                    blog_id = stripped.split('블로그:', 1)[1].strip()
                    in_body = False
                elif stripped.startswith('링크:') or stripped.startswith('URL:'):
                    url = stripped.split(':', 1)[1].strip()
                    in_body = False
                elif stripped.startswith('본문:'):
                    after = stripped.split('본문:', 1)[1].strip()
                    if after:
                        body_lines.append(after)
                    in_body = True
                elif stripped.startswith('내용:'):
                    after = stripped.split('내용:', 1)[1].strip()
                    if after:
                        body_lines.append(after)
                    in_body = True
                elif in_body:
                    body_lines.append(line)
            body = '\n'.join(body_lines).strip()
            if title or body:
                items.append({'blog_id': blog_id, 'title': title or '제목 없음', 'body': body, 'url': url})
        return items

    def _populate_popular_collect_table(self, popular_items):
        """수집된 인기글 dict 리스트를 표에 채운다.
        popular_items: [{'blog_id':..., 'title':..., 'body':..., 'url':...}, ...]"""
        self._popular_collected_items = list(popular_items or [])
        self.collect_popular_table.setRowCount(len(self._popular_collected_items))
        for i, it in enumerate(self._popular_collected_items):
            cb = QTableWidgetItem()
            cb.setFlags(cb.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            cb.setCheckState(Qt.CheckState.Checked)
            cb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.collect_popular_table.setItem(i, 0, cb)
            self.collect_popular_table.setItem(i, 1, QTableWidgetItem(str(it.get('blog_id', ''))[:40]))
            self.collect_popular_table.setItem(i, 2, QTableWidgetItem(str(it.get('title', ''))[:120]))
            body = str(it.get('body', ''))
            body_short = (body[:80] + '…') if len(body) > 80 else body
            self.collect_popular_table.setItem(i, 3, QTableWidgetItem(body_short))
        self.main.log(f"📋 인기글 수집 표 갱신: {len(self._popular_collected_items)}건 (기본 전체 체크)")

    def send_popular_to_ai(self):
        # 체크된 항목만 AI 입력으로 전달
        items = list(getattr(self, '_popular_collected_items', []) or [])
        if not items:
            items = self._parse_popular_items_from_text(getattr(self, '_popular_collected_text', '') or self.popular_result.toPlainText())
            if items:
                self._populate_popular_collect_table(items)

        if not items:
            self.main.log("⚠️ 먼저 인기글을 수집하세요")
            return

        checked = self._get_checked_popular_indices() if hasattr(self, 'collect_popular_table') else []
        if not checked:
            self.main.log("⚠️ AI로 전달할 인기글을 체크하세요")
            return

        chosen = [items[i] for i in checked if 0 <= i < len(items)]
        if not chosen:
            self.main.log("⚠️ AI로 전달할 인기글을 체크하세요")
            return

        blocks = []
        for k, it in enumerate(chosen, 1):
            blocks.append(f"[{k}] 제목: {it.get('title','')}\n블로그: {it.get('blog_id','')}\n링크: {it.get('url','')}\n본문: {it.get('body','')}")
        text = "\n\n---\n\n".join(blocks)
        self.ai_input.setPlainText(text)
        self.main.log(f"✅ 인기글 데이터가 AI 입력으로 전달되었습니다 (선택 {len(chosen)}건)")

    def _on_popular_selected(self):
        """수집된 인기글 선택 시 AI 입력 미리보기에 표시하되, 생성 대상은 체크박스로 확정한다."""
        rows = self.collect_popular_table.selectionModel().selectedRows()
        items = list(getattr(self, '_popular_collected_items', []) or [])
        if not rows or not items:
            return
        idx = rows[0].row()
        if 0 <= idx < len(items):
            it = items[idx]
            self.ai_input.setPlainText(
                f"[미리보기] 제목: {it.get('title','')}\n"
                f"블로그: {it.get('blog_id','')}\n"
                f"링크: {it.get('url','')}\n"
                f"본문: {it.get('body','')}"
            )

    def _on_article_selected(self):
        """테이블에서 원고 선택 시 미리보기에 표시"""
        rows = self.article_table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            art = self.generated_articles[idx]
            self.ai_output.setPlainText(art.get('content', ''))

    def _copy_selected_article(self):
        """선택된 원고 1개 복사"""
        rows = self.article_table.selectionModel().selectedRows()
        if not rows:
            self.main.log("⚠️ 원고를 먼저 선택하세요")
            return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            pyperclip.copy(self.generated_articles[idx].get('content', ''))
            self.main.log(f"📋 원고 #{idx+1} 복사 완료")

    def _copy_all_articles(self):
        """전체 원고 복사"""
        if not self.generated_articles:
            self.main.log("⚠️ 생성된 원고가 없습니다")
            return
        all_text = ""
        for i, art in enumerate(self.generated_articles):
            all_text += f"\n{'='*60}\n[원고 {i+1}] {art.get('title','')}\n{'='*60}\n"
            all_text += art.get('content', '') + "\n"
        pyperclip.copy(all_text)
        self.main.log(f"📋 전체 {len(self.generated_articles)}건 원고 복사 완료")

    def _update_article_table(self):
        """generated_articles 리스트를 테이블에 반영 (5컬럼: 선택/번호/상태/제목/글자수)"""
        self.article_table.setRowCount(len(self.generated_articles))
        for i, art in enumerate(self.generated_articles):
            status = art.get('status', '완료')
            # 컬럼 0: 체크박스 (완료된 원고만 기본 체크)
            self._set_article_check(i, checked=(status == '완료'))
            self.article_table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 2, QTableWidgetItem(status))
            self.article_table.setItem(i, 3, QTableWidgetItem(art.get('title', '')[:80]))
            content = art.get('content', '')
            self.article_table.setItem(i, 4, QTableWidgetItem(str(len(content)) if status == "완료" else "실패"))

    def _import_articles_excel(self):
        """엑셀 파일에서 원고를 불러오기"""
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 원고 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if not path:
            return
        try:
            import pandas as pd
            df = pd.read_excel(path)
            imported = []
            for _, row in df.iterrows():
                title = str(row.get('제목', row.get('title', '제목 없음')))
                content = str(row.get('생성된 원고', row.get('content', row.get('생성된원고', ''))))
                keyword = str(row.get('키워드', row.get('keyword', '')))
                source = str(row.get('원문 참고', row.get('원문참고', row.get('source', ''))))
                if content and content != 'nan':
                    imported.append({
                        'title': title if title != 'nan' else '제목 없음',
                        'content': content,
                        'keyword': keyword if keyword != 'nan' else '',
                        'source': source if source != 'nan' else '',
                        'status': '완료',
                    })
            if imported:
                self.generated_articles.extend(imported)
                self._update_article_table()
                self.main.log(f"📥 엑셀에서 {len(imported)}건 원고 불러옴 (총 {len(self.generated_articles)}건)")
            else:
                self.main.log("⚠️ 엑셀에서 불러올 원고가 없습니다 (제목/생성된 원고 컬럼 필요)")
        except Exception as e:
            self.main.log(f"❌ 엑셀 원고 불러오기 오류: {e}")

    def _export_articles_excel(self):
        """생성된 원고를 엑셀로 내보내기"""
        if not self.generated_articles:
            self.main.log("⚠️ 내보낼 원고가 없습니다")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            path, _ = QFileDialog.getSaveFileName(self, "엑셀 내보내기", "인기글_원고_목록.xlsx", "Excel Files (*.xlsx)")
            if not path:
                return

            wb = Workbook()
            ws = wb.active
            ws.title = "원고 목록"

            # 헤더
            headers = ["번호", "제목", "글자수", "상태", "원문 참고", "생성된 원고"]
            header_fill = PatternFill('solid', fgColor='1a4a8a')
            header_font = Font(bold=True, color='FFFFFF')
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=h)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')

            ws.column_dimensions['A'].width = 6
            ws.column_dimensions['B'].width = 50
            ws.column_dimensions['C'].width = 10
            ws.column_dimensions['D'].width = 8
            ws.column_dimensions['E'].width = 60
            ws.column_dimensions['F'].width = 80

            for i, art in enumerate(self.generated_articles):
                ws.cell(row=i+2, column=1, value=i+1)
                ws.cell(row=i+2, column=2, value=art.get('title', ''))
                ws.cell(row=i+2, column=3, value=len(art.get('content', '')))
                ws.cell(row=i+2, column=4, value=art.get('status', '완료'))
                ws.cell(row=i+2, column=5, value=art.get('source', '')[:500])
                ws.cell(row=i+2, column=6, value=art.get('content', ''))

            wb.save(path)
            self.main.log(f"📊 엑셀 내보내기 완료: {path} ({len(self.generated_articles)}건)")
        except ImportError:
            self.main.log("❌ openpyxl 패키지가 필요합니다: pip install openpyxl")
        except Exception as e:
            self.main.log(f"❌ 엑셀 내보내기 오류: {e}")

    def _parse_individual_posts(self, input_text):
        """수집된 텍스트를 개별 인기글로 파싱 (--- 구분자 기반)"""
        posts = []
        # 다중 블로그 구분자와 개별 글 구분자 모두 처리
        chunks = re.split(r'\n={3,}[^=]*={3,}\n|\n-{3,}\n', input_text)
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            title = "제목 없음"
            for line in chunk.split('\n'):
                if line.startswith('제목:'):
                    title = line.replace('제목:', '').strip()
                    break
            posts.append({'title': title, 'text': chunk})
        return posts

    def generate_ai(self):
        """수집된 각 인기글에 대해 개별 AI 원고를 생성"""
        # 취소 토글: 생성 중이면 취소
        if self._generating:
            self._cancel_generate = True
            self.gen_btn.setText("🛑 취소 중...")
            self.main.log("🛑 AI 생성 취소 요청됨...")
            return

        input_text = self.ai_input.toPlainText().strip()
        if not input_text:
            self.main.log("⚠️ 먼저 인기글을 수집하고 AI 입력으로 전달하세요")
            return

        # 개별 인기글 파싱
        individual_posts = self._parse_individual_posts(input_text)
        if not individual_posts:
            self.main.log("⚠️ 파싱 가능한 인기글이 없습니다")
            return

        total = len(individual_posts)
        self.main.log(f"📝 총 {total}개 인기글에 대해 개별 AI 원고 생성을 시작합니다")

        self._cancel_generate = False
        self._generating = True
        self.gen_btn.setText(f"🛑 생성 취소 (0/{total})")
        self.gen_btn.setStyleSheet("background-color:#8a1a1a; color:white; font-weight:bold; padding:8px;")
        self.ai_progress.setVisible(True)
        self.ai_progress.setValue(0)

        # 테이블 초기화 - 대기 상태로 표시
        self.generated_articles = []
        self.article_table.setRowCount(total)
        for i, post in enumerate(individual_posts):
            self._set_article_check(i, checked=False)
            self.article_table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 2, QTableWidgetItem("대기"))
            self.article_table.setItem(i, 3, QTableWidgetItem(post['title'][:80]))
            self.article_table.setItem(i, 4, QTableWidgetItem("-"))

        prompt_template = self.ai_prompt.toPlainText().strip()
        char_count = self.char_count.value()
        service = self.ai_model.currentText()

        # 스레드를 먼저 생성하여 do_gen_all에서 시그널 사용 가능하게 함
        thread = WorkerThread(lambda: None)  # placeholder, 아래에서 func 교체

        def _ui_update(fn):
            """메인 스레드에서 UI 업데이트 실행"""
            thread.ui_update_signal.emit(fn)

        def do_gen_all():
            generated_list = []
            errors_collection = []

            for idx, post in enumerate(individual_posts):
                if self._cancel_generate:
                    thread.log_signal.emit(f"\U0001f6d1 AI 생성 취소됨 ({len(generated_list)}건 처리됨)")
                    break

                post_title = post.get('title', '제목 없음')
                source_text = normalize_model_text(post.get('text', ''))
                source_text = re.sub(r'\n{3,}', '\n\n', source_text)[:2800]

                thread.log_signal.emit(f"\U0001f4dd [{idx+1}/{total}] AI 원고 생성 중: {post_title[:40]}...")
                _ui_update(lambda i=idx, t=total: self.gen_btn.setText(f"\U0001f6d1 생성 취소 ({i+1}/{t})"))
                _ui_update(lambda i=idx: self.article_table.setItem(i, 2, QTableWidgetItem("생성중")))
                _ui_update(lambda i=idx, t=total: self.ai_progress.setValue(int((i / t) * 100)))

                if len(re.sub(r'\s+', '', source_text)) < 40:
                    err_msg = '참고 자료가 너무 짧아 건너뜁니다'
                    thread.log_signal.emit(f"\u274c [{idx+1}/{total}] {err_msg}")
                    generated_list.append({
                        'title': post_title, 'content': f'\u274c {err_msg}',
                        'status': '실패', 'source': source_text, 'error': err_msg,
                    })
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: self.article_table.setItem(i, 1, QTableWidgetItem("\u274c실패")))
                    continue

                full_prompt = prompt_template + f"\n\n글자 수: 약 {char_count}자"
                full_prompt += "\n- 반드시 완성된 한국어 블로그 글 형태로 작성해주세요"
                full_prompt += "\n- 제목을 첫 줄에 ## 제목 형태로 작성해주세요"
                full_prompt += "\n- 참고 자료를 그대로 복사하지 말고 자연스럽게 재구성해주세요"
                full_prompt += f"\n\n참고 자료:\n{source_text}"

                try:
                    text = None
                    if "Perplexity" in service:
                        api_key = self.main.config.get('perplex_key', '').strip()
                        if not api_key:
                            raise RuntimeError('Perplexity API 키 없음')
                        sys_msg = "당신은 한국어 SEO 블로그 전문 작가입니다. 주어진 참고 자료를 바탕으로 네이버 블로그에 게시할 완성된 포스트를 작성합니다. 제목부터 본문, 소제목까지 모두 완성된 한국어 글만 출력하세요."
                        text = call_perplexity(api_key, full_prompt, model='sonar-pro', system_message=sys_msg)
                    elif "Gemini" in service:
                        api_key = self.main.config.get('gemini_key', '').strip()
                        if not api_key:
                            raise RuntimeError('Gemini API 키 없음')
                        text = call_gemini_text(api_key, full_prompt, model='gemini-2.5-flash')
                    elif "GPT" in service:
                        api_key = self.main.config.get('openai_key', '').strip()
                        if not api_key:
                            raise RuntimeError('OpenAI API 키 없음')
                        text = call_openai_text(api_key, full_prompt, model='gpt-4o')

                    text = normalize_model_text(text)
                    if len(re.sub(r'\s+', '', text)) < 80:
                        raise RuntimeError(f'AI 응답이 너무 짧습니다: {text[:120]}')

                    gen_title = post_title
                    for line in text.split('\n'):
                        stripped = line.strip()
                        if stripped.startswith('##'):
                            gen_title = stripped.lstrip('#').strip()
                            break
                        if stripped and not stripped.startswith('['):
                            gen_title = stripped[:80]
                            break

                    generated_list.append({
                        'title': gen_title, 'content': text,
                        'status': '완료', 'source': source_text,
                    })
                    # 즉시 발행 대기열 형태로 저장
                    self.generated_articles = list(generated_list)
                    try:
                        _queue = [{'order': qi+1, 'title': ga['title'], 'content': ga['content'], 'status': 'pending', 'result': ''} for qi, ga in enumerate(generated_list) if ga.get('status') == '완료']
                        if _queue:
                            with open('publish_queue_popular_incremental.json', 'w', encoding='utf-8') as _qf:
                                json.dump(_queue, _qf, ensure_ascii=False, indent=2)
                            thread.log_signal.emit(f"\U0001f4be [{idx+1}/{total}] 발행 대기열 저장 완료 ({len(_queue)}건)")
                    except Exception: pass
                    thread.log_signal.emit(f"\u2705 [{idx+1}/{total}] 완료: {gen_title[:40]}")

                    # 자동 DALL-E 이미지 생성
                    if self.auto_dalle_check.isChecked():
                        oai_key = self.main.config.get('openai_key', '').strip()
                        if oai_key:
                            auto_count = self.auto_dalle_count.value()
                            thread.log_signal.emit(f"  🤖 [{idx+1}/{total}] 자동 이미지 생성 중 ({auto_count}장)...")
                            auto_imgs = self._auto_generate_dalle_for_article(gen_title, oai_key, count=auto_count)
                            if auto_imgs:
                                self._image_pool.extend(auto_imgs)
                                thread.log_signal.emit(f"  🖼️ [{idx+1}/{total}] 자동 이미지 {len(auto_imgs)}장 생성 완료")
                    _ui_update(lambda i=idx, t=gen_title, c=len(text): (
                        self.article_table.setItem(i, 1, QTableWidgetItem("\u2705완료")),
                        self.article_table.setItem(i, 2, QTableWidgetItem(t[:80])),
                        self.article_table.setItem(i, 3, QTableWidgetItem(str(c)))
                    ))
                except Exception as e:
                    err_msg = format_error_message(e)
                    thread.log_signal.emit(f"\u274c [{idx+1}/{total}] 오류: {err_msg}")
                    generated_list.append({
                        'title': post_title, 'content': f'\u274c 오류: {err_msg}',
                        'status': '실패', 'source': source_text, 'error': err_msg,
                    })
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: self.article_table.setItem(i, 1, QTableWidgetItem("\u274c실패")))

                if idx < total - 1 and not self._cancel_generate:
                    wait_seconds = 4 if "Perplexity" in service else 2
                    for _ in range(wait_seconds * 2):
                        if self._cancel_generate:
                            break
                        time.sleep(0.5)

            self.generated_articles = generated_list
            _ui_update(lambda: self.ai_progress.setValue(100))
            success = sum(1 for a in generated_list if a.get('status') == '완료')
            failed = sum(1 for a in generated_list if a.get('status') == '실패')

            if failed and success == 0:
                unique_errors = list(dict.fromkeys(a.get('error','') for a in generated_list if a.get('error')))
                thread.log_signal.emit(f"\u274c AI 원고 생성 완료 - 총 {total}건 중 {failed}건 실패")
                if unique_errors:
                    return "\u274c 총 {}건 원고 생성 실패\n\n주요 오류:\n- {}".format(total, "\n- ".join(unique_errors[:5]))
                return f"\u274c 총 {total}건 원고 생성 실패"

            thread.log_signal.emit(f"\u2705 AI 원고 생성 완료 - 총 {total}건 중 {success}건 성공, {failed}건 실패")
            return f"\u2705 총 {total}건 원고 생성 완료 ({success}건 성공 / {failed}건 실패)"

        thread.func = do_gen_all  # placeholder 교체
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.ai_output.setPlainText(r))
        thread.ui_update_signal.connect(lambda fn: fn())  # 메인 스레드에서 UI 콜백 실행
        def _on_gen_finished():
            self._generating = False
            self._cancel_generate = False
            self.gen_btn.setText("🎯 전체 AI 원고 생성 (개별)")
            self.gen_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
            self.ai_progress.setVisible(False)
            self._update_article_table()

        thread.finished_signal.connect(_on_gen_finished)
        thread.start()
    # ══════════════ 이미지 관리 메서드 ══════════════

    def _refresh_image_table(self):
        """이미지 풀 테이블 갱신"""
        self.image_table.setRowCount(len(self._image_pool))
        for i, img in enumerate(self._image_pool):
            self.image_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.image_table.setItem(i, 1, QTableWidgetItem("📁업로드" if img['source'] == 'upload' else "🤖AI"))
            self.image_table.setItem(i, 2, QTableWidgetItem(img.get('desc', '')[:60]))
            self.image_table.setItem(i, 3, QTableWidgetItem(img.get('path', '')))
        self.img_status.setText(f"총 {len(self._image_pool)}개 이미지 등록됨")

    def _upload_images(self):
        """로컬 이미지 파일 직접 업로드"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "이미지 파일 선택", "",
            "이미지 파일 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;모든 파일 (*.*)"
        )
        if not files:
            return
        for fpath in files:
            self._image_pool.append({
                'source': 'upload',
                'desc': os.path.basename(fpath),
                'path': fpath,
            })
        self._refresh_image_table()
        self.main.log(f"🖼️ {len(files)}개 이미지 업로드 완료 (총 {len(self._image_pool)}개)")

    def _generate_dalle_images(self):
        """DALL-E API로 이미지 생성"""
        prompt = self.dalle_prompt.text().strip()
        if not prompt:
            self.main.log("⚠️ DALL-E 프롬프트를 입력하세요")
            return
        api_key = self.main.config.get('openai_key', '').strip()
        if not api_key:
            self.main.log("⚠️ OpenAI API Key가 설정되지 않았습니다 (설정 탭에서 입력)")
            return

        count = self.dalle_count.value()
        size = self.dalle_size.currentText()

        self.img_dalle_btn.setEnabled(False)
        self.img_dalle_btn.setText(f"🤖 생성 중... (0/{count})")
        self.main.log(f"🤖 DALL-E 이미지 생성 시작: '{prompt}' × {count}장")

        def do_gen():
            results = []
            for i in range(count):
                try:
                    path = generate_dalle_image(api_key, prompt, size=size)
                    results.append({'source': 'dalle', 'desc': prompt, 'path': path})
                    thread.log_signal.emit(f"  🖼️ [{i+1}/{count}] 이미지 생성 완료")
                except Exception as e:
                    thread.log_signal.emit(f"  ❌ [{i+1}/{count}] 생성 실패: {format_error_message(e)}")
                if i < count - 1:
                    time.sleep(2)
            return results

        thread = WorkerThread(do_gen)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def on_result(results):
            if isinstance(results, list):
                self._image_pool.extend(results)
                self._refresh_image_table()
                self.main.log(f"✅ DALL-E 이미지 {len(results)}장 생성 완료 (총 {len(self._image_pool)}개)")
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.img_dalle_btn.setEnabled(True),
            self.img_dalle_btn.setText("🤖 AI 이미지 생성 (ChatGPT)")
        ))
        thread.start()

    def _auto_generate_dalle_for_article(self, article_title, api_key, count=1, size="1024x1024"):
        """원고 제목 기반 자동 DALL-E 이미지 생성 (워커 스레드 내에서 호출)"""
        generated = []
        for i in range(count):
            try:
                prompt = f"네이버 블로그 포스팅용 고퀄리티 사진. 주제: {article_title}. 자연스러운 실제 사진 스타일, 텍스트 없이."
                path = generate_dalle_image(api_key, prompt, size=size)
                generated.append({'source': 'dalle', 'desc': f"자동생성: {article_title}", 'path': path})
            except Exception as e:
                pass  # 자동 생성 실패는 무시
            if i < count - 1:
                time.sleep(2)
        return generated

    def _delete_selected_image(self):
        """선택된 이미지 삭제"""
        rows = set(item.row() for item in self.image_table.selectedItems())
        if not rows:
            self.main.log("⚠️ 삭제할 이미지를 선택하세요")
            return
        for idx in sorted(rows, reverse=True):
            if 0 <= idx < len(self._image_pool):
                self._image_pool.pop(idx)
        self._refresh_image_table()
        self.main.log(f"🗑️ {len(rows)}개 이미지 삭제됨")

    def _clear_all_images(self):
        """모든 이미지 삭제"""
        self._image_pool.clear()
        self._refresh_image_table()
        self.main.log("🗑️ 전체 이미지 삭제됨")

    def _get_images_for_article(self, article_idx, photo_count):
        """원고에 사용할 이미지들 반환 (라운드 로빈).
        v27: photo_count가 0이어도 풀에 이미지가 있으면 최소 1장 반환 → 본문 시작 부분에 자동 삽입."""
        if not self._image_pool:
            return []
        total_imgs = len(self._image_pool)
        # [사진] 토큰이 0개여도 풀에 있으면 1장은 사용
        effective = max(photo_count, 1) if total_imgs > 0 else photo_count
        start = (article_idx * effective) % total_imgs
        result = []
        for i in range(effective):
            idx = (start + i) % total_imgs
            result.append(self._image_pool[idx]['path'])
        return result
    def _insert_image_to_editor(self, driver, image_path):
        """네이버 블로그 에디터에 이미지를 삽입.
        v27: 본문 전용 file input 우선 + 툴바 이미지 버튼 먼저 활성화 + iframe 진입."""
        import subprocess as sp

        if not os.path.exists(image_path):
            raise FileNotFoundError(f"이미지 파일 없음: {image_path}")

        abs_path = os.path.abspath(image_path)

        # iframe 진입 시도 (mainFrame 안에 에디터가 있음)
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame('mainFrame')
        except Exception:
            pass

        # ── 방법 1: 본문 이미지 툴바 버튼을 먼저 눌러 file input 활성화 ──
        try:
            for btn_sel in [
                'button.se-image-toolbar-button',
                'button[data-name="image"]',
                'button[data-type="image"]',
                'button[aria-label*="사진"]',
                'button[aria-label*="이미지"]',
                'button.se-toolbar-button-image',
            ]:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(0.6)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # ── 방법 2: 본문 전용 file input 우선 (썸네일용은 보통 첫 번째라 제외) ──
        try:
            # 본문용 셀렉터 우선
            preferred = []
            for sel in [
                'input.se-image-input-file',
                'input[class*="image"][type="file"]',
                'input[accept*="image"][type="file"]',
            ]:
                preferred.extend(driver.find_elements(By.CSS_SELECTOR, sel))
            # fallback: 모든 file input (썸네일 제외 위해 뒤쪽부터)
            all_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
            candidates = preferred + list(reversed(all_inputs))
            seen = set()
            for fi in candidates:
                key = fi.get_attribute('outerHTML')[:200]
                if key in seen:
                    continue
                seen.add(key)
                try:
                    # display:none 인 input도 send_keys 가능
                    driver.execute_script("arguments[0].style.display='block'; arguments[0].style.visibility='visible';", fi)
                    fi.send_keys(abs_path)
                    time.sleep(4)  # 업로드 + 처리 대기
                    self.log_emit(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                    # iframe 복귀
                    try:
                        driver.switch_to.default_content()
                        driver.switch_to.frame('mainFrame')
                    except Exception:
                        pass
                    return
                except Exception:
                    continue
        except Exception:
            pass

        # 방법 2: 이미지 버튼 클릭 → file input에 전달
        try:
            for btn_sel in [
                'button[data-name="image"]',
                'button.se-image-toolbar-button',
                'button[data-type="image"]',
                'button.se-toolbar-button-image',
            ]:
                try:
                    img_btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    driver.execute_script("arguments[0].click();", img_btn)
                    time.sleep(1)
                    # file input 찾기
                    file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                    for fi in file_inputs:
                        try:
                            fi.send_keys(os.path.abspath(image_path))
                            time.sleep(3)
                            self.log_emit(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                            return
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

        # 방법 3: PowerShell 클립보드 이미지 복사 → Ctrl+V
        try:
            abs_path = os.path.abspath(image_path).replace('\\', '/')
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile('{abs_path}')
[System.Windows.Forms.Clipboard]::SetImage($img)
$img.Dispose()
"""
            sp.run(['powershell', '-Command', ps_script], capture_output=True, timeout=10)
            time.sleep(0.5)
            safe_hotkey(driver, 'ctrl', 'v')
            time.sleep(3)
            self.log_emit(f"  🖼️ 이미지 붙여넣기 완료: {os.path.basename(image_path)}")
        except Exception as e:
            raise RuntimeError(f"이미지 삽입 모든 방법 실패: {e}")

    def save_draft(self):
        if not self.generated_articles:
            self.main.log("⚠️ 저장할 원고가 없습니다")
            return
        path, _ = QFileDialog.getSaveFileName(self, "원고 저장", "인기글_원고_전체.txt", "텍스트 파일 (*.txt)")
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                for i, art in enumerate(self.generated_articles):
                    f.write(f"\n{'='*60}\n")
                    f.write(f"[원고 {i+1}] {art.get('title', '')}\n")
                    f.write(f"{'='*60}\n\n")
                    f.write(art.get('content', '') + "\n\n")
            self.main.log(f"💾 전체 {len(self.generated_articles)}건 원고 저장 완료: {path}")

    def load_blog_folders(self):
        """블로그 폴더 불러오기 - 캐시 우선 사용"""
        nid = self.account_combo.currentText().strip()
        if not nid:
            self.main.log("⚠️ 발행 계정을 선택하세요")
            return

        # 캐시 확인
        if nid in self._folder_cache and self._folder_cache[nid]:
            cached = self._folder_cache[nid]
            self.blog_folder.clear()
            self.blog_folder.addItems(cached)
            self.main.log(f"📂 저장된 폴더 사용: {nid} ({len(cached)}개) — 새로고침하려면 캐시 삭제 후 재시도")
            return

        self.load_folders_btn.setEnabled(False)
        self.main.log(f"🔄 [{nid}] 블로그 폴더 목록 불러오는 중...")

        def do_load():
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': f'https://blog.naver.com/{nid}'
                }
                url = f"https://blog.naver.com/PostList.naver?blogId={nid}"
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code == 200:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.text, 'html.parser')
                    folders = []
                    for opt in soup.select('select option, li a[onclick*="category"]'):
                        name = opt.get_text(strip=True)
                        if name and name not in ['전체보기', ''] and len(name) < 50:
                            if name not in folders:
                                folders.append(name)
                    if folders:
                        return folders
                return []
            except Exception as e:
                self.log_emit(f"⚠️ 폴더 불러오기 실패: {e}")
                return []

        thread = WorkerThread(do_load)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def on_result(folders):
            if folders and isinstance(folders, list):
                self.blog_folder.clear()
                self.blog_folder.addItems(folders)
                # 캐시에 저장
                self._folder_cache[nid] = folders
                self._save_folder_cache()
                self.main.log(f"✅ {len(folders)}개 폴더 로드 완료 (캐시 저장됨 — 다음부터 자동 로드)")
            else:
                self.main.log("⚠️ 폴더를 찾을 수 없습니다")
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: self.load_folders_btn.setEnabled(True))
        thread.start()

    def _build_publish_queue(self):
        source_articles = list(getattr(self, 'generated_articles', []) or [])

        # 체크된 행만 발행 (없으면 전체 폴백)
        try:
            checked = self._get_checked_article_indices()
        except Exception:
            checked = []
        if source_articles and checked:
            source_articles = [source_articles[i] for i in checked if 0 <= i < len(source_articles)]
            try:
                self.main.log(f"📤 발행 대기열: 체크된 {len(source_articles)}건만 발행")
            except Exception:
                pass
        elif source_articles:
            try:
                self.main.log(f"⚠️ 체크된 원고가 없어 전체 {len(source_articles)}건을 발행합니다")
            except Exception:
                pass

        if not source_articles:
            content = self.ai_output.toPlainText().strip()
            if content:
                source_articles = [{'title': '인기글 포스트', 'content': content}]
        queue = []
        for idx, article in enumerate(source_articles, start=1):
            title = str(article.get('title', f'인기글 포스트 {idx}')).strip() or f'인기글 포스트 {idx}'
            content = str(article.get('content', '')).strip()
            status = article.get('status', '')
            if not content or status == '실패': continue
            queue.append({'order': idx, 'title': title, 'content': content, 'status': 'pending', 'result': ''})
        return queue

    def _write_publish_queue_snapshot(self, account_id, queue):
        safe_account = re.sub(r'[^0-9A-Za-z_.-]+', '_', (account_id or 'default').strip())
        path = f"publish_queue_popular_{safe_account}.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        return path

    def publish_to_blog(self):
        """generated_articles 리스트를 순회하며 각각 블로그에 발행 (순차 처리)"""
        # 중복 실행 방지
        if self._publishing_flag:
            self.main.log("⚠️ 이미 발행이 진행 중입니다. 완료될 때까지 기다려주세요.")
            return

        publish_queue = self._build_publish_queue()
        if not publish_queue:
            self.main.log("⚠️ 발행할 원고가 없습니다. AI 글쓰기를 먼저 실행하세요")
            return

        config = self.main.config
        naver_id, naver_pw = self._get_selected_account()
        if not naver_id or not naver_pw:
            self.main.log("⚠️ 설정에서 네이버 계정을 먼저 입력하세요")
            return

        folder = self.blog_folder.currentText().strip()
        mode = self.save_mode.currentText()
        total = len(publish_queue)
        queue_path = self._write_publish_queue_snapshot(naver_id, publish_queue)
        self.publish_queue = publish_queue
        self._publishing_flag = True

        self.publish_btn.setEnabled(False)
        self.publish_btn.setText(f"발행 중... (0/{total})")
        self.pub_progress.setVisible(True)
        self.pub_progress.setValue(0)

        self.main.log(f"🚀 총 {total}건 블로그 발행 시작")
        for qi, q_item in enumerate(publish_queue): self.main.log(f"  📋 [{qi+1}] {q_item['title'][:40]} ({len(q_item['content'])}자)")
        self.main.log(f"🗂️ 발행 대기열 저장 완료: {queue_path}")

        def do():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            opts = uc.ChromeOptions()
            opts.add_argument('--no-sandbox')
            opts.add_argument('--start-maximized')
            opts.add_argument('--user-data-dir=./chrome_profile')
            opts.add_argument('--disable-popup-blocking')
            prefs = {
                'profile.default_content_setting_values.popups': 0,
            }
            opts.add_experimental_option('prefs', prefs)
            driver = make_uc_driver(opts)

            # ── window.open 완전 차단 (드라이버 생성 직후) ──
            def block_window_open():
                try:
                    driver.execute_script("""
                        if (!window._woBlocked) {
                            window._origOpen = window.open;
                            window.open = function(url) {
                                if (url) window.location.href = url;
                                return window;
                            };
                            window._woBlocked = true;
                        }
                    """)
                except Exception:
                    pass
            # ── "작성중인 글이 있습니다" 팝업 취소 처리 ──
            def dismiss_draft_popup():
                """에디터 진입 시 '작성중인 글이 있습니다' 팝업이 뜨면 취소 버튼 클릭"""
                try:
                    time.sleep(2)
                    cancel_selectors = [
                        'button.se-popup-button-cancel',
                        'button.cancel_btn__WEaBq',
                        'button.cancel_btn',
                        'button[class*="cancel"]',
                        'button.se-cancel',
                    ]
                    # 팝업 텍스트로도 탐지
                    try:
                        popup_texts = driver.find_elements(By.XPATH,
                            "//*[contains(text(),'작성중인') or contains(text(),'작성 중인') or contains(text(),'임시저장')]")
                        if popup_texts:
                            self.log_emit("  ℹ️ '작성중인 글' 팝업 감지 → 취소 클릭 시도")
                            for sel in cancel_selectors:
                                try:
                                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                            # CSS 셀렉터 실패 시 XPATH로 취소/아니오 버튼 찾기
                            cancel_btns = driver.find_elements(By.XPATH,
                                "//button[contains(text(),'취소') or contains(text(),'아니') or contains(text(),'아니오') or contains(text(),'새로')]")
                            for btn in cancel_btns:
                                try:
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료 (XPATH)")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        pass
                except Exception as e:
                    self.log_emit(f"  ℹ️ 팝업 처리 중 예외(무시): {e}")
                return False


            # ── 원고 정제 함수 ──
            def sanitize_content(raw_content):
                """발행 전 원고에서 불필요한 마크다운/이미지/참조를 제거"""
                lines = raw_content.split('\n')
                cleaned = []
                for line in lines:
                    s = line.strip()
                    # 구분선 제거
                    if re.match(r'^[━─═\-]{3,}', s):
                        continue
                    # [이미지 삽입] 줄 제거
                    if s == '[이미지 삽입]':
                        continue
                    # 마크다운 이미지 ![alt](url) 제거
                    if re.match(r'^!\[.*\]\(.*\)$', s):
                        continue
                    # 순수 URL만 있는 줄 제거 (이미지 URL)
                    if re.match(r'^https?://\S+$', s):
                        continue
                    # URL (설명) 형태 줄 제거
                    if re.match(r'^https?://\S+\s*\(.*\)$', s):
                        continue
                    # 📄 [번호] 헤더 줄 제거
                    if re.match(r'^📄\s*\[\d+\]', s):
                        continue
                    # (공백 제외 N자) 제거
                    if re.match(r'^\(공백\s*제외\s*\d+자\)$', s):
                        continue
                    # **추가 반전형 이미지 키워드** 블록 제거
                    if '이미지 키워드' in s and ('추가' in s or '반전형' in s):
                        continue
                    # 번호. URL 형태 (이미지 리스트) 제거
                    if re.match(r'^\d+\.\s*https?://\S+', s):
                        continue
                    # 참조 번호 [1][2] 등 제거 (텍스트는 유지)
                    s = re.sub(r'\[\d+\]', '', s)
                    # ## 마크다운 헤더 → 일반 텍스트
                    s = re.sub(r'^#{1,6}\s*', '', s)
                    # **bold** → 일반 텍스트
                    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
                    s = s.strip()
                    if s:
                        cleaned.append(s)
                # 연속 빈 줄 정리
                result = '\n'.join(cleaned)
                result = re.sub(r'\n{3,}', '\n\n', result)
                return result.strip()

            def validate_article(article):
                """발행 가능한 원고인지 검증. 불가 시 사유 반환"""
                title = (article.get('title') or '').strip()
                content = (article.get('content') or '').strip()
                if not title or not content:
                    return '제목 또는 본문이 비어 있습니다'
                if '필수 입력 정보 부재' in content or '요청을 정확히 처리할 수 없습니다' in content:
                    return '오류 원고 (AI가 생성 실패한 항목)'
                # 정제 후에도 내용이 너무 짧으면 스킵
                sanitized = sanitize_content(content)
                if len(sanitized) < 50:
                    return f'정제 후 본문이 너무 짧습니다 ({len(sanitized)}자)'
                return None  # 검증 통과

            success_count = 0
            fail_count = 0
            main_window = None

            def update_queue_status(index, status, result):
                publish_queue[index]['status'] = status
                publish_queue[index]['result'] = result
                self._write_publish_queue_snapshot(naver_id, publish_queue)

            def keep_only_window(target_handle=None):
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                target = target_handle or main_window or handles[-1]
                for handle in list(handles):
                    if handle == target:
                        continue
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(target)
                main_window = target

            def close_all_extra_tabs():
                """main_window 외 모든 탭을 닫는다"""
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                if not main_window or main_window not in handles:
                    main_window = handles[0]
                for h in handles:
                    if h == main_window:
                        continue
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                try:
                    driver.switch_to.window(main_window)
                except Exception:
                    pass

            def open_editor_single_tab():
                """항상 단일 탭에서 글쓰기 에디터를 연다"""
                nonlocal main_window

                # 0) 먼저 탭 1개만 남기기
                close_all_extra_tabs()

                # 1) 현재 탭에서 에디터로 이동 (execute_script로 강제)
                editor_url = 'https://blog.naver.com/GoBlogWrite.naver'
                try:
                    driver.execute_script(f'window.location.href = "{editor_url}";')
                except Exception:
                    driver.get(editor_url)

                time.sleep(5)

                # 2) 새 탭이 열렸으면 마지막 탭(에디터)만 남기고 닫기
                handles = list(driver.window_handles)
                if len(handles) > 1:
                    editor_tab = handles[-1]
                    for h in handles:
                        if h == editor_tab:
                            continue
                        try:
                            driver.switch_to.window(h)
                            driver.close()
                        except Exception:
                            pass
                    driver.switch_to.window(editor_tab)
                    main_window = editor_tab
                    self.log_emit(f"  ℹ️ 추가 탭 {len(handles)-1}개 닫고 에디터 탭만 유지")
                else:
                    main_window = handles[0] if handles else driver.current_window_handle
                    driver.switch_to.window(main_window)

                # 3) iframe 전환
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

                # mainFrame이 있으면 진입
                try:
                    iframe = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.ID, 'mainFrame'))
                    )
                    driver.switch_to.frame(iframe)
                    time.sleep(1)
                    self.log_emit("  ✅ mainFrame iframe 진입 성공")
                    dismiss_draft_popup()
                except Exception:
                    self.log_emit("  ℹ️ mainFrame 없음, 직접 에디터 접근 시도")

                # 4) 에디터 로드 확인
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'div.se-title-text, div[contenteditable="true"]'))
                    )
                    self.log_emit("  ✅ 에디터 로드 확인")
                except Exception:
                    self.log_emit("  ⚠️ 에디터 요소를 찾지 못했습니다 (계속 시도)")

            def select_folder_if_needed():
                if not folder:
                    return True
                selectors = ['select#categoryId', 'select[name="categoryId"]']
                for selector in selectors:
                    try:
                        cat_select = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        cat_select.click()
                        time.sleep(0.3)
                        for opt in cat_select.find_elements(By.TAG_NAME, 'option'):
                            if folder in opt.text:
                                opt.click()
                                time.sleep(0.3)
                                return True
                    except Exception:
                        continue
                return False

            def input_title(art_title):
                title_selectors = [
                    'span.se-placeholder',
                    'div[data-name="title"] div[contenteditable="true"]',
                    'div.se-section-title div.se-text-paragraph',
                    'div.se-title-text',
                ]
                last_error = None
                for selector in title_selectors:
                    try:
                        title_el = WebDriverWait(driver, 3).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                        try:
                            title_el.click()
                        except Exception:
                            from selenium.webdriver.common.action_chains import ActionChains
                            ActionChains(driver).move_to_element(title_el).click().perform()
                        time.sleep(0.2)
                        pyperclip.copy(art_title)
                        safe_hotkey(driver, 'ctrl', 'a')
                        time.sleep(0.1)
                        safe_hotkey(driver, 'ctrl', 'v')
                        time.sleep(0.3)
                        self.log_emit(f"  ✅ 제목 입력 완료: {art_title[:30]}")
                        return True
                    except Exception as e:
                        last_error = e
                        continue
                raise RuntimeError(f'제목 입력 실패: {last_error}')

            def focus_body():
                body_selectors = [
                    'div.se-section-text div[contenteditable="true"]',
                    'div.se-component-content div[contenteditable="true"]',
                    'div.se-text-paragraph',
                    'div[contenteditable="true"]',
                ]
                for selector in body_selectors:
                    try:
                        elems = driver.find_elements(By.CSS_SELECTOR, selector)
                        for elem in elems:
                            if not elem.is_displayed():
                                continue
                            try:
                                driver.execute_script("arguments[0].click();", elem)
                            except Exception:
                                elem.click()
                            time.sleep(0.3)
                            return True
                    except Exception:
                        continue
                try:
                    safe_press(driver, 'tab')
                    time.sleep(0.3)
                    return True
                except Exception:
                    return False

            def input_body(art_content):
                if not focus_body():
                    raise RuntimeError('본문 입력 영역 포커스 실패')

                # v26: 본문 전처리만 먼저, 정렬은 입력 후에 적용
                art_content = preprocess_article_lines(art_content)
                alignment = getattr(self, '_selected_alignment', 'left')

                wrote_any = False
                for raw_line in art_content.split('\n'):
                    line = raw_line.rstrip()
                    if not line.strip():
                        safe_press(driver, 'enter')
                        time.sleep(0.05)
                        continue

                    # 스티커 토큰 처리
                    sticker_idxs = extract_sticker_indices(line)
                    line_wo_sticker = strip_sticker_tokens(line)

                    # 인용구 자동 감지 — 한 줄만 인용 블록, 나머지는 일반 문단
                    if line_wo_sticker and looks_like_quote(line_wo_sticker):
                        insert_quote_block(driver, line_wo_sticker)
                        wrote_any = True
                    elif line_wo_sticker:
                        pyperclip.copy(line_wo_sticker)
                        safe_hotkey(driver, 'ctrl', 'v')
                        safe_press(driver, 'enter')
                        time.sleep(0.1)
                        wrote_any = True

                    for sidx in sticker_idxs:
                        try:
                            insert_naver_sticker(driver, sidx)
                            safe_press(driver, 'enter')
                        except Exception:
                            pass

                if not wrote_any:
                    raise RuntimeError('본문 내용이 비어 있어 입력하지 못했습니다')

                # v26: 입력 완료 후 전체 선택 → 정렬 적용 (+ JS fallback)
                try:
                    apply_alignment(driver, alignment, select_all_first=True)
                except Exception:
                    pass

            def submit_article(art_idx, art_title):
                time.sleep(1)
                if '임시' in mode:
                    save_selectors = [
                        'button[data-testid="save-btn"]',
                        'button.save_btn__Y5f57',
                        'button.save_btn',
                        'button[class*="save"]',
                    ]
                    clicked = False
                    for selector in save_selectors:
                        try:
                            btn = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if not clicked:
                        try:
                            btn = driver.find_element(
                                By.XPATH, '//button[contains(., "임시저장")]'
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            clicked = True
                        except Exception:
                            pass
                    if not clicked:
                        raise RuntimeError('임시저장 버튼을 찾지 못했습니다')
                    time.sleep(2)
                    self.log_emit(f"  💾 [{art_idx + 1}/{total}] 임시저장 완료: {art_title[:30]}")
                    driver.execute_script('window.location.href = "https://blog.naver.com";')
                    time.sleep(2)
                    return True, '임시저장 완료'


                publish_selectors = [
                    'button[data-testid="publish-btn"]',
                    'button.publish_btn__Y5f57',
                    'button.publish_btn',
                    'button[class*="publish"]',
                ]
                confirm_selectors = [
                    'button.se-popup-button-confirm',
                    'button.confirm_btn__WEaBq',
                    'button.confirm_btn',
                    'button[class*="confirm"]',
                ]

                clicked = False
                for selector in publish_selectors:
                    try:
                        btn = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        driver.execute_script("arguments[0].click();", btn)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError('발행 버튼을 찾지 못했습니다')

                time.sleep(2)

                confirmed = False
                for selector in confirm_selectors:
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, selector)
                        driver.execute_script("arguments[0].click();", btn)
                        confirmed = True
                        break
                    except Exception:
                        continue

                if not confirmed:
                    self.log_emit('  ℹ️ 발행 확인 팝업이 없어 바로 완료 여부를 확인합니다')

                time.sleep(4)
                current_url = (driver.current_url or '').lower()
                if 'goblogwrite' in current_url:
                    self.log_emit('  ⚠️ 에디터 URL에 머물러 있음, 발행 재시도...')
                    # 한 번 더 confirm 버튼 시도
                    for selector in confirm_selectors:
                        try:
                            btn = driver.find_element(By.CSS_SELECTOR, selector)
                            driver.execute_script("arguments[0].click();", btn)
                            time.sleep(3)
                            break
                        except Exception:
                            continue

                self.log_emit(f"  📤 [{art_idx + 1}/{total}] 즉시발행 완료")
                return True, driver.current_url

            try:
                self.log_emit('🔑 네이버 로그인 중...')
                if not naver_login_with_fallback(driver, naver_id, naver_pw, self.log_emit):
                    return '❌ 로그인 실패 - 설정에서 계정을 확인하세요'
                self.log_emit('✅ 로그인 완료')
                main_window = driver.current_window_handle
                close_all_extra_tabs()
                block_window_open()

                for art_idx, article in enumerate(publish_queue):
                    try:
                        art_title = article.get('title', f'포스트 {art_idx + 1}')
                        art_content = article.get('content', '')

                        # ── 원고 검증 ──
                        skip_reason = validate_article(article)
                        if skip_reason:
                            self.log_emit(f"\n⏭️ [{art_idx + 1}/{total}] 건너뜀: {skip_reason} — {art_title[:30]}")
                            fail_count += 1
                            update_queue_status(art_idx, 'skipped', skip_reason)
                            continue

                        # ── 원고 정제 ──
                        art_content = sanitize_content(art_content)

                        # ── 본문에서 제목 줄 제거 (제목은 별도 입력) ──
                        content_lines = art_content.split('\n')
                        filtered_lines = []
                        title_stripped = False
                        for cl in content_lines:
                            cl_clean = cl.strip()
                            if not title_stripped and cl_clean and (
                                cl_clean == art_title.strip() or
                                re.sub(r'^#{1,6}\s*', '', cl_clean) == art_title.strip() or
                                cl_clean.replace('**', '') == art_title.strip()
                            ):
                                title_stripped = True
                                continue
                            filtered_lines.append(cl)
                        art_content = '\n'.join(filtered_lines).strip()
                        self.log_emit(f"\n📝 [{art_idx + 1}/{total}] 발행 시작: {art_title[:40]}... (정제 후 {len(art_content)}자)")
                        QTimer.singleShot(0, lambda idx=art_idx: self.publish_btn.setText(f"발행 중... ({idx + 1}/{total})"))

                        open_editor_single_tab()
                        block_window_open()  # 에디터 로드 후 다시 차단

                        if folder and not select_folder_if_needed():
                            self.log_emit('  ⚠️ 폴더 선택 실패, 기본 카테고리로 진행합니다')

                        input_title(art_title)
                        input_body(art_content)
                        ok, result_message = submit_article(art_idx, art_title)

                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_all_extra_tabs()
                        time.sleep(1)

                        success_count += 1
                        update_queue_status(art_idx, 'done', result_message)

                    except Exception as e:
                        self.log_emit(f"  ❌ [{art_idx + 1}/{total}] 발행 실패: {e}")
                        fail_count += 1
                        update_queue_status(art_idx, 'error', str(e))
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_all_extra_tabs()
                        time.sleep(1)

                    self._emit_progress(self.pub_progress, int(((art_idx + 1) / total) * 100))
                    time.sleep(2)

                return f"✅ 블로그 발행 완료! 성공: {success_count}건, 실패: {fail_count}건"

            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.main.log(r))

        def on_news_publish_finished():
            self._publishing_flag = False
            self.publish_btn.setEnabled(True)
            self.publish_btn.setText('🚀 블로그 발행')
            self.pub_progress.setVisible(False)

        thread.finished_signal.connect(on_news_publish_finished)
        thread.start()
# ═══════════════════════════════════════════════════════════════
#  4. 뉴스 수집 페이지 (웹 크롤링 방식)
# ═══════════════════════════════════════════════════════════════

class NewsCollectPage(QWidget):
    """뉴스 수집 페이지 - 각 소스별 웹 크롤링 → Perplexity AI → 블로그 발행"""

    SOURCE_CONFIG = {
        "shortnews": {
            "title": "1️⃣ 숏텐츠",
            "url": "https://search.naver.com/search.naver?ssc=tab.shortents.all&sm=tab_jum&query=%EC%88%8F%ED%85%90%EC%B8%A0",
            "desc": "네이버 숏텐츠 인기 콘텐츠를 수집합니다",
        },
        "daum": {
            "title": "2️⃣ 다음",
            "url": "https://entertain.daum.net/ranking/popular",
            "desc": "다음 연예 인기뉴스(많이 본)를 수집합니다",
        },
        "nate": {
            "title": "3️⃣ 네이트",
            "url": "https://news.nate.com/rank/interest?sc=ent&p=day&date=20260407",
            "desc": "네이트 연예뉴스 랭킹을 수집합니다",
        },
    }

    def __init__(self, main_win, source="shortnews"):
        super().__init__()
        self.main = main_win
        self.source = source
        self.generated_articles = []
        self.publish_queue = []
        self._publishing_flag = False
        self._folder_cache = {}
        try:
            cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'folder_cache_news.json')
            if os.path.exists(cache_path):
                with open(cache_path, 'r', encoding='utf-8') as f_cache:
                    self._folder_cache = json.load(f_cache)
        except:
            self._folder_cache = {}
        cfg = self.SOURCE_CONFIG.get(source, self.SOURCE_CONFIG["shortnews"])

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        title = QLabel(f"📰 {cfg['title']} → AI 글쓰기 → 블로그 발행")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        scroll_layout.addWidget(title)

        desc = QLabel(cfg['desc'])
        desc.setStyleSheet("color:#888; margin-bottom:8px;")
        scroll_layout.addWidget(desc)

        # ══════════════════════════════════════
        # STEP 1: 뉴스 수집 (웹 크롤링)
        # ══════════════════════════════════════
        step1 = QGroupBox("STEP 1️⃣  뉴스 수집 (웹 크롤링)")
        s1 = QVBoxLayout(step1)

        h_url = QHBoxLayout()
        h_url.addWidget(QLabel("수집 URL:"))
        self.crawl_url = QLineEdit(cfg['url'])
        self.crawl_url.setPlaceholderText("수집할 페이지 URL")
        h_url.addWidget(self.crawl_url, 3)
        s1.addLayout(h_url)

        h_opt = QHBoxLayout()
        h_opt.addWidget(QLabel("수집 개수:"))
        self.collect_count = QSpinBox()
        self.collect_count.setRange(1, 30)
        self.collect_count.setValue(10)
        h_opt.addWidget(self.collect_count)

        self.keywords_filter = QLineEdit()
        self.keywords_filter.setPlaceholderText("필터 키워드 (쉼표 구분, 비워두면 전체)")
        h_opt.addWidget(QLabel("필터:"))
        h_opt.addWidget(self.keywords_filter, 2)
        s1.addLayout(h_opt)

        self.collect_btn = QPushButton("🔍 뉴스 수집 시작")
        self.collect_btn.clicked.connect(self.start_collect)
        self.collect_btn.setStyleSheet("background-color:#2d5a27; color:white; font-weight:bold; padding:8px;")
        s1.addWidget(self.collect_btn)

        self.news_progress = QProgressBar()
        self.news_progress.setVisible(False)
        s1.addWidget(self.news_progress)

        self.collect_result = QTextBrowser()
        self.collect_result.setMaximumHeight(120)
        self.collect_result.setPlaceholderText("수집된 뉴스 내용이 여기에 표시됩니다...")
        s1.addWidget(self.collect_result)

        # ── 수집 결과 표 (체크박스로 선택) ──
        s1.addWidget(QLabel("📋 수집된 뉴스 (체크한 항목만 AI 입력으로 전달):"))
        self.collect_news_table = QTableWidget()
        self.collect_news_table.setColumnCount(4)
        self.collect_news_table.setHorizontalHeaderLabels(["선택", "제목", "본문 요약", "링크"])
        self.collect_news_table.setColumnWidth(0, 50)
        self.collect_news_table.setColumnWidth(1, 320)
        self.collect_news_table.setColumnWidth(2, 280)
        self.collect_news_table.horizontalHeader().setStretchLastSection(True)
        self.collect_news_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.collect_news_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.collect_news_table.setMinimumHeight(150)
        self.collect_news_table.itemSelectionChanged.connect(self._on_news_selected)
        s1.addWidget(self.collect_news_table)

        h_news_sel = QHBoxLayout()
        btn_news_all = QPushButton("☑️ 전체 선택")
        btn_news_all.clicked.connect(lambda: self._toggle_all_news(True))
        h_news_sel.addWidget(btn_news_all)
        btn_news_none = QPushButton("⬜ 전체 해제")
        btn_news_none.clicked.connect(lambda: self._toggle_all_news(False))
        h_news_sel.addWidget(btn_news_none)
        h_news_sel.addStretch()
        s1.addLayout(h_news_sel)

        self._collected_items = []  # [{'title','body','href'}, ...]

        scroll_layout.addWidget(step1)

        # ══════════════════════════════════════
        # ══════════════════════════════════════
        # STEP 2: AI 글쓰기 (Perplexity 기본)
        # ══════════════════════════════════════
        step2 = QGroupBox("STEP 2️⃣  AI 글쓰기")
        s2 = QVBoxLayout(step2)

        h2a = QHBoxLayout()
        h2a.addWidget(QLabel("AI 서비스:"))
        self.ai_model = QComboBox()
        self.ai_model.addItems(["Perplexity (sonar-pro)", "Gemini (gemini-2.5-flash)", "GPT (gpt-4o)"])
        h2a.addWidget(self.ai_model, 2)

        h2a.addWidget(QLabel("글자 수:"))
        self.char_count = QSpinBox()
        self.char_count.setRange(500, 10000)
        self.char_count.setValue(2000)
        self.char_count.setSingleStep(500)
        h2a.addWidget(self.char_count)
        s2.addLayout(h2a)

        self.ai_prompt = QTextEdit()
        self.ai_prompt.setPlaceholderText("AI에게 전달할 프롬프트...")
        self.ai_prompt.setPlainText(
            "아래 뉴스 콘텐츠를 바탕으로 SEO 최적화된 네이버 블로그 포스트를 작성해주세요.\n"
            "- 자연스러운 말투 (1인칭 경험담/의견)\n"
            "- 소제목 3개 이상 (##)\n"
            "- 핵심 키워드 자연스럽게 5회 이상 배치\n"
            "- [사진] 태그로 사진 삽입 위치 표시\n"
            "- [인용구]텍스트[/인용구] 형태로 강조 문구 표시\n"
            "- 마지막에 해시태그 5개 추가"
        )
        self.ai_prompt.setMaximumHeight(120)
        s2.addWidget(QLabel("프롬프트:"))
        s2.addWidget(self.ai_prompt)

        h2b = QHBoxLayout()
        self.send_to_ai_btn = QPushButton("⬇️ 수집 결과 → AI 입력")
        self.send_to_ai_btn.clicked.connect(self.send_to_ai)
        h2b.addWidget(self.send_to_ai_btn)

        self.gen_btn = QPushButton("🎯 AI 원고 생성")
        self.gen_btn.clicked.connect(self.generate_ai)
        self.gen_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
        h2b.addWidget(self.gen_btn)
        s2.addLayout(h2b)

        self.ai_progress = QProgressBar()
        self.ai_progress.setVisible(False)
        s2.addWidget(self.ai_progress)

        self.ai_input = QTextEdit()
        self.ai_input.setPlaceholderText("수집된 뉴스가 AI 입력으로 전달됩니다...")
        self.ai_input.setMaximumHeight(100)
        s2.addWidget(QLabel("AI 입력 (수집 데이터):"))
        s2.addWidget(self.ai_input)

        # ── 원고 리스트 테이블 ──
        s2.addWidget(QLabel("📋 생성된 원고 리스트:"))
        self.article_table = QTableWidget()
        self.article_table.setColumnCount(4)
        self.article_table.setHorizontalHeaderLabels(["번호", "상태", "제목", "글자수"])
        self.article_table.horizontalHeader().setStretchLastSection(True)
        self.article_table.setColumnWidth(0, 50)
        self.article_table.setColumnWidth(1, 60)
        self.article_table.setColumnWidth(2, 350)
        self.article_table.setColumnWidth(3, 70)
        self.article_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.article_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.article_table.itemSelectionChanged.connect(self._on_article_selected)
        self.article_table.setMinimumHeight(150)
        s2.addWidget(self.article_table)

        self.ai_output = QTextBrowser()
        self.ai_output.setPlaceholderText("원고를 선택하면 여기에 표시됩니다...")
        s2.addWidget(QLabel("📄 선택된 원고 미리보기:"))
        s2.addWidget(self.ai_output)

        self.error_summary_label = QLabel("⚠️ 주요 오류:")
        self.error_summary_label.setVisible(False)
        s2.addWidget(self.error_summary_label)
        self.error_summary = QTextBrowser()
        self.error_summary.setMaximumHeight(60)
        self.error_summary.setVisible(False)
        s2.addWidget(self.error_summary)

        h2c = QHBoxLayout()
        copy_btn = QPushButton("📋 선택 원고 복사")
        copy_btn.clicked.connect(self._copy_selected_article)
        h2c.addWidget(copy_btn)
        copy_all_btn = QPushButton("📋 전체 원고 복사")
        copy_all_btn.clicked.connect(self._copy_all_articles)
        h2c.addWidget(copy_all_btn)
        save_btn = QPushButton("💾 전체 원고 저장")
        save_btn.clicked.connect(self.save_draft)
        h2c.addWidget(save_btn)
        export_btn = QPushButton("📊 엑셀로 내보내기")
        export_btn.clicked.connect(self._export_articles_excel)
        h2c.addWidget(export_btn)
        import_btn = QPushButton("📥 엑셀 원고 불러오기")
        import_btn.clicked.connect(self._import_articles_excel)
        h2c.addWidget(import_btn)
        s2.addLayout(h2c)

        scroll_layout.addWidget(step2)
        # ══════════════════════════════════════
        # STEP 2.5: 이미지 관리
        # ══════════════════════════════════════
        step_img = QGroupBox("🖼️ STEP 2.5  이미지 관리 (본문 [사진] 위치에 삽입)")
        si = QVBoxLayout(step_img)

        si.addWidget(QLabel("원고 본문에 [사진] 태그가 있으면 발행 시 해당 위치에 이미지가 삽입됩니다."))

        self.image_table = QTableWidget()
        self.image_table.setColumnCount(4)
        self.image_table.setHorizontalHeaderLabels(["번호", "소스", "설명/프롬프트", "파일경로"])
        self.image_table.horizontalHeader().setStretchLastSection(True)
        self.image_table.setColumnWidth(0, 40)
        self.image_table.setColumnWidth(1, 70)
        self.image_table.setColumnWidth(2, 250)
        self.image_table.setMinimumHeight(120)
        self.image_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        si.addWidget(self.image_table)
        self._image_pool = []  # [{'source':'upload'|'dalle', 'desc':'...', 'path':'...'}, ...]

        h_img_btns = QHBoxLayout()
        self.img_upload_btn = QPushButton("📁 이미지 직접 업로드")
        self.img_upload_btn.clicked.connect(self._upload_images)
        self.img_upload_btn.setStyleSheet("background-color:#2d6a4f; color:white; font-weight:bold; padding:6px;")
        h_img_btns.addWidget(self.img_upload_btn)

        self.img_dalle_btn = QPushButton("🤖 AI 이미지 생성 (ChatGPT)")
        self.img_dalle_btn.clicked.connect(self._generate_dalle_images)
        self.img_dalle_btn.setStyleSheet("background-color:#4a1a8a; color:white; font-weight:bold; padding:6px;")
        h_img_btns.addWidget(self.img_dalle_btn)

        self.img_del_btn = QPushButton("🗑️ 선택 삭제")
        self.img_del_btn.clicked.connect(self._delete_selected_image)
        h_img_btns.addWidget(self.img_del_btn)

        self.img_clear_btn = QPushButton("🗑️ 전체 삭제")
        self.img_clear_btn.clicked.connect(self._clear_all_images)
        h_img_btns.addWidget(self.img_clear_btn)
        si.addLayout(h_img_btns)

        h_dalle_opt = QHBoxLayout()
        h_dalle_opt.addWidget(QLabel("DALL-E 프롬프트:"))
        self.dalle_prompt = QLineEdit()
        self.dalle_prompt.setPlaceholderText("이미지 설명을 입력하세요 (예: 서울 야경, 커피 한 잔)")
        h_dalle_opt.addWidget(self.dalle_prompt, 4)

        h_dalle_opt.addWidget(QLabel("크기:"))
        self.dalle_size = QComboBox()
        self.dalle_size.addItems(["1024x1024", "1024x1536", "1536x1024"])
        h_dalle_opt.addWidget(self.dalle_size)

        h_dalle_opt.addWidget(QLabel("장수:"))
        self.dalle_count = QSpinBox()
        self.dalle_count.setRange(1, 10)
        self.dalle_count.setValue(1)
        h_dalle_opt.addWidget(self.dalle_count)
        si.addLayout(h_dalle_opt)

        h_auto = QHBoxLayout()
        self.auto_dalle_check = QCheckBox("원고별 자동 이미지 생성 (원고 제목 기반)")
        self.auto_dalle_check.setToolTip("원고 생성 시 각 원고의 제목을 프롬프트로 DALL-E 이미지를 자동 생성합니다")
        h_auto.addWidget(self.auto_dalle_check)

        self.auto_dalle_count = QSpinBox()
        self.auto_dalle_count.setRange(1, 5)
        self.auto_dalle_count.setValue(1)
        h_auto.addWidget(QLabel("원고당 이미지:"))
        h_auto.addWidget(self.auto_dalle_count)
        h_auto.addStretch()
        si.addLayout(h_auto)

        self.img_status = QLabel("")
        self.img_status.setStyleSheet("color:#888; font-size:11px;")
        si.addWidget(self.img_status)

        scroll_layout.addWidget(step_img)


        # ══════════════════════════════════════
        # ══════════════════════════════════════
        # STEP 3: 네이버 블로그 자동 발행
        # ══════════════════════════════════════
        step3 = QGroupBox("STEP 3️⃣  네이버 블로그 자동 발행")
        s3 = QVBoxLayout(step3)

        h3_acc = QHBoxLayout()
        h3_acc.addWidget(QLabel("발행 계정:"))
        self.account_combo = QComboBox()
        accounts = self.main.config.get('naver_accounts', [])
        if not accounts and self.main.config.get('naver_id'):
            accounts = [{'id': self.main.config.get('naver_id', '')}]
        for acc in accounts:
            self.account_combo.addItem(acc.get('id', ''))
        h3_acc.addWidget(self.account_combo, 2)
        s3.addLayout(h3_acc)

        h3a = QHBoxLayout()
        h3a.addWidget(QLabel("블로그 폴더:"))
        self.blog_folder = QComboBox()
        self.blog_folder.setEditable(True)
        h3a.addWidget(self.blog_folder, 2)

        self.load_folders_btn = QPushButton("🔄 폴더 불러오기")
        self.load_folders_btn.clicked.connect(self.load_blog_folders)
        h3a.addWidget(self.load_folders_btn)

        self.clear_cache_btn = QPushButton("🗑️ 캐시 삭제")
        self.clear_cache_btn.clicked.connect(lambda: self._clear_folder_cache(self.account_combo.currentText().strip()))
        self.clear_cache_btn.setStyleSheet("color:#ff6666;")
        h3a.addWidget(self.clear_cache_btn)
        s3.addLayout(h3a)

        # 계정 변경 시 캐시된 폴더 자동 로드
        self.account_combo.currentTextChanged.connect(self._on_account_changed)

        h3b = QHBoxLayout()
        h3b.addWidget(QLabel("저장 형태:"))
        self.save_mode = QComboBox()
        self.save_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        # v24: 본문 정렬 선택
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["⬅️ 좌측 정렬", "⬆️ 가운데 정렬", "➡️ 우측 정렬", "↔️ 양쪽 정렬"])
        self.alignment_combo.currentIndexChanged.connect(
            lambda i: setattr(self, '_selected_alignment', ['left','center','right','justify'][i])
        )
        self._selected_alignment = 'left'
        h3b.addWidget(self.save_mode)
        h3b.addWidget(QLabel("정렬:"))
        h3b.addWidget(self.alignment_combo)
        s3.addLayout(h3b)

        self.publish_btn = QPushButton("🚀 블로그 발행")
        self.publish_btn.clicked.connect(self.publish_to_blog)
        self.publish_btn.setStyleSheet("background-color:#8B0000; color:white; font-weight:bold; padding:8px;")
        s3.addWidget(self.publish_btn)

        self.pub_progress = QProgressBar()
        self.pub_progress.setVisible(False)
        s3.addWidget(self.pub_progress)

        scroll_layout.addWidget(step3)

        # 스크롤 영역
        scroll = QScrollArea()
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        main_layout = QVBoxLayout(self)
        main_layout.addWidget(scroll)

        self.account_combo.currentTextChanged.connect(lambda *_: self._load_saved_folders())
        self.blog_folder.currentTextChanged.connect(lambda *_: self._remember_selected_folder())
        # 시작 직후 자동 폴더 로드는 비활성화 (수동 버튼으로만 로드)

    def _emit_progress(self, bar, val):
        QTimer.singleShot(0, lambda: bar.setValue(val))

    def log_emit(self, msg):
        QTimer.singleShot(0, lambda: self.main.log(msg))

    def _on_account_changed(self, account_id):
        """계정 변경 시 캐시된 폴더 자동 로드"""
        account_id = (account_id or '').strip()
        if account_id and account_id in self._folder_cache:
            self.blog_folder.clear()
            self.blog_folder.addItems(self._folder_cache[account_id])
            self.main.log(f"📂 {account_id} 저장된 폴더 자동 로드 ({len(self._folder_cache[account_id])}개)")
        else:
            self.blog_folder.clear()

    def _clear_folder_cache(self, account_id):
        """폴더 캐시 삭제"""
        account_id = (account_id or '').strip()
        if not account_id:
            return
        if account_id in self._folder_cache:
            del self._folder_cache[account_id]
            try:
                cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'folder_cache_news.json')
                with open(cache_path, 'w', encoding='utf-8') as f_cache:
                    json.dump(self._folder_cache, f_cache, ensure_ascii=False, indent=2)
            except:
                pass
            self.blog_folder.clear()
            self.main.log(f"🗑️ {account_id} 폴더 캐시 삭제됨")

    def _save_folders_to_config(self, account_id, folder_list, selected_folder=""):
        """계정별 블로그 폴더 목록/선택값 저장"""
        account_id = (account_id or '').strip()
        if not account_id:
            return

        clean_folders = []
        for folder in folder_list:
            folder = str(folder).strip()
            if folder and folder not in clean_folders:
                clean_folders.append(folder)

        store = self.main.config.setdefault('saved_blog_folders', {})
        store[account_id] = {
            'folders': clean_folders,
            'selected': (selected_folder or '').strip(),
        }

        try:
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(self.main.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.main.log(f"⚠️ 폴더 설정 저장 실패: {e}")

    def _load_saved_folders(self):
        """선택된 계정의 저장된 폴더 목록 복원"""
        if not hasattr(self, 'account_combo') or not hasattr(self, 'blog_folder'):
            return

        account_id = self.account_combo.currentText().strip()
        if not account_id:
            return

        saved = self.main.config.get('saved_blog_folders', {}).get(account_id, {})
        folders = saved.get('folders', [])
        selected = saved.get('selected', '')

        self.blog_folder.blockSignals(True)
        current = self.blog_folder.currentText().strip()
        self.blog_folder.clear()
        if folders:
            self.blog_folder.addItems(folders)
        if selected:
            self.blog_folder.setCurrentText(selected)
        elif current:
            self.blog_folder.setCurrentText(current)
        self.blog_folder.blockSignals(False)

    def _remember_selected_folder(self):
        if not hasattr(self, 'account_combo') or not hasattr(self, 'blog_folder'):
            return

        account_id = self.account_combo.currentText().strip()
        if not account_id:
            return

        folder_list = [
            self.blog_folder.itemText(i).strip()
            for i in range(self.blog_folder.count())
            if self.blog_folder.itemText(i).strip()
        ]
        selected = self.blog_folder.currentText().strip()
        if selected and selected not in folder_list:
            folder_list.insert(0, selected)

        if folder_list:
            self._save_folders_to_config(account_id, folder_list, selected)

    def _build_publish_queue(self):
        source_articles = list(getattr(self, 'generated_articles', []) or [])

        # 체크된 행만 발행 대상으로 (체크 없으면 전체 폴백)
        try:
            checked = self._get_checked_article_indices()
        except Exception:
            checked = []
        if source_articles and checked:
            source_articles = [source_articles[i] for i in checked if 0 <= i < len(source_articles)]
            try:
                self.main.log(f"📤 발행 대기열: 체크된 {len(source_articles)}건만 발행")
            except Exception:
                pass
        elif source_articles:
            try:
                self.main.log(f"⚠️ 체크된 원고가 없어 전체 {len(source_articles)}건을 발행합니다")
            except Exception:
                pass

        if not source_articles:
            content = self.ai_output.toPlainText().strip()
            if content:
                source_articles = [{'title': '뉴스 포스트', 'content': content}]

        queue = []
        for idx, article in enumerate(source_articles, start=1):
            title = str(article.get('title', f'뉴스 포스트 {idx}')).strip() or f'뉴스 포스트 {idx}'
            content = str(article.get('content', '')).strip()
            if not content:
                continue
            queue.append({
                'order': idx,
                'title': title,
                'content': content,
                'status': 'pending',
                'result': '',
            })
        return queue

    def _write_publish_queue_snapshot(self, account_id, queue):
        safe_account = re.sub(r'[^0-9A-Za-z_.-]+', '_', (account_id or 'default').strip())
        path = f"publish_queue_{self.source}_{safe_account}.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        return path

    # ─── STEP 1: 웹 크롤링 수집 ───
    def start_collect(self):
        url = self.crawl_url.text().strip()
        if not url:
            self.main.log("⚠️ 수집 URL을 입력하세요")
            return

        self.collect_btn.setEnabled(False)
        self.collect_btn.setText("수집 중...")
        self.news_progress.setVisible(True)
        self.news_progress.setValue(0)

        max_count = self.collect_count.value()
        filter_kws = [k.strip() for k in self.keywords_filter.text().split(',') if k.strip()]

        def do_collect():
            self._emit_progress(self.news_progress, 5)

            # 소스별 크롤링 분기
            if self.source == "nate":
                return self._crawl_nate(url, max_count, filter_kws)
            elif self.source == "daum":
                return self._crawl_daum(url, max_count, filter_kws)
            else:
                return self._crawl_naver_shortents(url, max_count, filter_kws)

        self.main.log(f"🔍 [{self.source}] 뉴스 수집 시작 - URL: {url[:60]}, 최대 {max_count}건")

        thread = WorkerThread(do_collect)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def _on_collect_result(r):
            self.collect_result.setPlainText(r)
            self._populate_collect_table(r)
        thread.result_signal.connect(_on_collect_result)
        thread.finished_signal.connect(lambda: (
            self.collect_btn.setEnabled(True),
            self.collect_btn.setText("🔍 뉴스 수집 시작"),
            self.news_progress.setVisible(False),
            self.main.log(f"✅ [{self.source}] 뉴스 수집 종료 ({len(self._collected_items)}건)")
        ))
        thread.start()

    def _crawl_naver_shortents(self, url, max_count, filter_kws):
        """네이버 숏텐츠 - 엔터 종합 카테고리만 수집, 중복 제거"""
        self._emit_progress(self.news_progress, 10)

        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from difflib import SequenceMatcher
        opts = uc.ChromeOptions()
        opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--disable-features=RendererCodeIntegrity')
        opts.add_argument('--window-size=1920,1080')
        opts.add_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')

        driver = None
        try:
            driver = make_uc_driver(opts)
            driver.get(url)
            time.sleep(4)

            self._emit_progress(self.news_progress, 20)

            # ── "엔터 종합" 카테고리 버튼 클릭 ──
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, 'button')
                for btn in buttons:
                    if '엔터 종합' in btn.text.strip():
                        btn.click()
                        time.sleep(3)
                        break
            except Exception:
                pass

            self._emit_progress(self.news_progress, 30)

            # 스크롤하여 모든 항목 로드
            for _ in range(5):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)

            self._emit_progress(self.news_progress, 40)

            page_source = driver.page_source
            soup = BeautifulSoup(page_source, 'html.parser')

            results = []
            seen_titles = set()

            # 카테고리/네비게이션 제외 키워드
            skip_keywords = [
                '전체', '엔터 종합', '스포츠 종합', '뮤직', '영화', '생활경제',
                '카테크 종합', '자동차', '여행맛집 종합', '패션뷰티 종합', '맛집/카페',
                '국내여행', '지식 종합', '야구', '경제 종합', '푸드', '패션트렌드',
                '해외축구', '증권', '농구', '리빙푸드 종합', '드라마', '축구',
                '부동산', '세계여행', '해외야구', '배구', '뷰티',
                '로그인', '더보기', '공유', '설정', '메뉴', '이 정보가 표시된 이유',
                '오늘은 어떤 취향', '클립 챌린지', '스토어의 발견', '웨일 브라우저',
                '네이버에서 컬리를', '홈리빙 인기', 'Keep에',
            ]

            # ── 방법1: 숏텐츠 토픽 카드에서 추출 ──
            # 실제 숏텐츠 토픽 링크는 반드시 x_shortents 또는 mra= 파라미터를 포함함
            all_links = soup.find_all('a')
            for a_tag in all_links:
                href = a_tag.get('href', '')

                # 핵심 필터: x_shortents 또는 mra= 파라미터가 있는 링크만 수집
                if 'x_shortents' not in href and 'mra=' not in href:
                    continue

                text_parts = a_tag.get_text('|', strip=True).split('|')
                text_parts = [p for p in text_parts if len(p) > 3]

                if len(text_parts) < 1:
                    continue

                title = text_parts[0][:80].strip()
                snippet = ' '.join(text_parts[1:])[:200] if len(text_parts) > 1 else ''

                # 제목 길이 최소 5자
                if len(title) < 5:
                    continue

                # 카테고리/네비게이션 링크 제외
                if any(skip in title for skip in skip_keywords):
                    continue

                # 중복 제목 제거 (유사도 80% 이상이면 중복)
                is_dup = False
                for existing in seen_titles:
                    if SequenceMatcher(None, title, existing).ratio() > 0.8:
                        is_dup = True
                        break
                if is_dup:
                    continue

                seen_titles.add(title)
                results.append({'title': title, 'snippet': snippet, 'href': href})

            # ── 방법2: Selenium elements로 직접 추출 (fallback) ──
            if not results:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR,
                                                    'div[class*="shortents"] a, div[class*="fds-comps"] a, '
                                                    'div[class*="group_"] a, div[class*="api_subject"] a')
                    for el in elements:
                        try:
                            title = el.text.strip().split('\n')[0][:80]
                            snippet = '\n'.join(el.text.strip().split('\n')[1:])[:200]
                            href = el.get_attribute('href') or ''

                            if not title or len(title) < 5:
                                continue
                            if any(skip in title for skip in skip_keywords):
                                continue

                            is_dup = False
                            for existing in seen_titles:
                                if SequenceMatcher(None, title, existing).ratio() > 0.8:
                                    is_dup = True
                                    break
                            if is_dup:
                                continue

                            seen_titles.add(title)
                            results.append({'title': title, 'snippet': snippet, 'href': href})
                        except Exception:
                            continue
                except Exception:
                    pass

            self._emit_progress(self.news_progress, 50)

            # 필터 적용
            if filter_kws:
                results = [r for r in results if any(
                    k.lower() in r['title'].lower() or k.lower() in r['snippet'].lower()
                    for k in filter_kws
                )]

            # max_count 제한
            results = results[:max_count]

            self._emit_progress(self.news_progress, 60)

            # ── 각 토픽 클릭하여 상세 정보(AI 브리핑) 수집 ──
            enriched = []
            for idx, item in enumerate(results):
                detail_text = item['snippet']

                # href가 있으면 상세 페이지에서 AI 브리핑 추출 시도
                if item['href'] and 'search.naver.com' in item['href']:
                    try:
                        driver.get(item['href'])
                        time.sleep(2)
                        detail_soup = BeautifulSoup(driver.page_source, 'html.parser')

                        # AI 브리핑 영역 추출
                        briefing_el = detail_soup.select_one(
                            'div[class*="ai_briefing"], div[class*="sc_new"] .api_txt_lines, '
                            'div.news_dsc, div.total_group, div.api_subject_bx'
                        )
                        if briefing_el:
                            detail_text = briefing_el.get_text(strip=True)[:1000]
                        else:
                            meta = detail_soup.select_one('meta[property="og:description"]')
                            if meta and meta.get('content'):
                                detail_text = meta['content'][:500]

                        # 실제 뉴스 기사 링크도 추출
                        news_links = []
                        for news_a in detail_soup.select('a.news_tit, a.api_txt_lines, a[class*="title"]'):
                            news_title = news_a.get_text(strip=True)
                            news_href = news_a.get('href', '')
                            if news_title and news_href and 'naver.com' not in news_href[:30]:
                                news_links.append(f"  → {news_title}\n    {news_href}")

                        if news_links:
                            detail_text += "\n\n[관련 기사]\n" + "\n".join(news_links[:3])

                    except Exception:
                        pass

                enriched.append(
                    f"[{idx + 1}] 제목: {item['title']}\n"
                    f"본문: {detail_text[:800]}\n"
                    f"링크: {item['href']}\n"
                )

                self._emit_progress(self.news_progress, 60 + int((idx / max(len(results), 1)) * 35))

            self._emit_progress(self.news_progress, 100)

            if not enriched:
                return "수집 결과가 없습니다. URL을 확인하거나 필터를 조정하세요."

            return f"📰 숏텐츠(엔터 종합) 총 {len(enriched)}건 수집 완료\n{'=' * 50}\n\n" + '\n'.join(enriched)

        except Exception as e:
            raise RuntimeError(f"네이버 숏텐츠 크롤링 실패: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _crawl_daum(self, url, max_count, filter_kws):
        """다음 인기뉴스 크롤링 - v33: requests 우선 + Selenium 폴백 + 단계별 디버그 로그"""
        from difflib import SequenceMatcher
        import requests as _rq

        self._emit_progress(self.news_progress, 10)
        print(f"[DAUM v33] 시작 url={url} max={max_count} filter={filter_kws}")

        HEADERS = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Referer": "https://entertain.daum.net/",
        }

        def _fetch_html_requests(target_url, timeout=15):
            try:
                r = _rq.get(target_url, headers=HEADERS, timeout=timeout)
                print(f"[DAUM v33] requests GET {target_url[:80]} status={r.status_code} len={len(r.text)}")
                if r.status_code == 200 and len(r.text) > 1000:
                    return r.text
            except Exception as e:
                print(f"[DAUM v33] requests 실패: {e}")
            return None

        def _fetch_html_selenium(target_url):
            print(f"[DAUM v33] Selenium 폴백 시도: {target_url[:80]}")
            import undetected_chromedriver as uc
            opts = uc.ChromeOptions()
            opts.add_argument('--headless=new')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--window-size=1920,1080')
            opts.add_argument('--user-agent=' + HEADERS["User-Agent"])
            drv = None
            try:
                drv = make_uc_driver(opts)
                drv.get(target_url)
                time.sleep(4)
                for _ in range(2):
                    drv.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)
                html = drv.page_source
                print(f"[DAUM v33] Selenium html len={len(html)}")
                return html
            except Exception as e:
                print(f"[DAUM v33] Selenium 실패: {e}")
                return None
            finally:
                if drv:
                    try: drv.quit()
                    except: pass

        # 1. 리스트 페이지 받기 (requests 우선)
        list_html = _fetch_html_requests(url)
        if not list_html:
            list_html = _fetch_html_selenium(url)
        if not list_html:
            raise RuntimeError("다음 리스트 페이지 수신 실패 (requests + Selenium 모두 실패)")

        self._emit_progress(self.news_progress, 30)
        soup = BeautifulSoup(list_html, 'html.parser')

        # 2. 셀렉터별 매칭 개수 디버그
        sel_stats = {
            "ol.list_ranking > li":     len(soup.select('ol.list_ranking > li')),
            "ul.list_ranking > li":     len(soup.select('ul.list_ranking > li')),
            ".list_ranking li":         len(soup.select('.list_ranking li')),
            ".ranking_list li":         len(soup.select('.ranking_list li')),
            "a[href*='v.daum.net/v/']": len(soup.select("a[href*='v.daum.net/v/']")),
        }
        print(f"[DAUM v33] 셀렉터 매칭: {sel_stats}")

        # 3. 후보 li 수집 (ol/ul 둘 다)
        candidates = soup.select('ol.list_ranking > li, ul.list_ranking > li, .list_ranking li, .ranking_list li')
        print(f"[DAUM v33] li 후보: {len(candidates)}")

        results = []
        seen_titles = set()
        seen_hrefs = set()

        if candidates:
            for el in candidates:
                # 제목 링크는 보통 .link_txt
                a_title = el.select_one('a.link_txt')
                a_href  = el.select_one("a[href*='v.daum.net/v/']")
                if not a_href:
                    continue
                href = a_href.get('href', '').strip()
                if href.startswith('//'):
                    href = 'https:' + href
                elif href.startswith('/'):
                    href = 'https://v.daum.net' + href
                if not href or href in seen_hrefs:
                    continue

                title = ''
                if a_title:
                    title = a_title.get_text(strip=True)
                if not title:
                    tit = el.select_one('strong.tit_thumb, .tit_thumb, .tit_g')
                    if tit:
                        title = tit.get_text(strip=True)
                if not title:
                    img = el.find('img')
                    if img and img.get('alt'):
                        title = img['alt'].strip()

                title = (title or '')[:120].strip()
                if not title or len(title) < 5:
                    continue

                # 유사 중복 제거
                dup = False
                for ex in seen_titles:
                    if SequenceMatcher(None, title, ex).ratio() > 0.85:
                        dup = True; break
                if dup:
                    continue

                seen_titles.add(title)
                seen_hrefs.add(href)
                results.append({'title': title, 'snippet': '', 'href': href})
                if len(results) >= max_count * 2:
                    break

        # 4. 폴백: 모든 v.daum.net/v/ 링크 직접 수집
        if not results:
            print("[DAUM v33] li 셀렉터 0건 → 전체 링크 폴백")
            for a in soup.select("a[href*='v.daum.net/v/']"):
                href = a.get('href', '').strip()
                if href.startswith('//'): href = 'https:' + href
                elif href.startswith('/'): href = 'https://v.daum.net' + href
                if href in seen_hrefs:
                    continue
                title = a.get_text(strip=True)
                if not title:
                    img = a.find('img')
                    if img and img.get('alt'):
                        title = img['alt'].strip()
                title = (title or '')[:120].strip()
                if not title or len(title) < 5:
                    continue
                dup = any(SequenceMatcher(None, title, ex).ratio() > 0.85 for ex in seen_titles)
                if dup:
                    continue
                seen_titles.add(title); seen_hrefs.add(href)
                results.append({'title': title, 'snippet': '', 'href': href})
                if len(results) >= max_count * 2:
                    break

        print(f"[DAUM v33] 1차 수집 결과: {len(results)}건 (필터 전)")

        # 5. 키워드 필터
        if filter_kws:
            before = len(results)
            results = [r for r in results if any(k.lower() in r['title'].lower() for k in filter_kws)]
            print(f"[DAUM v33] 키워드 필터 적용: {before} → {len(results)}")

        results = results[:max_count]
        self._emit_progress(self.news_progress, 55)

        if not results:
            return ("수집 결과가 없습니다.\n"
                    f"디버그: 셀렉터 매칭={sel_stats}, 페이지길이={len(list_html)}\n"
                    "다음 페이지 구조가 변경되었거나 키워드 필터가 너무 좁습니다.")

        # 6. 각 기사 본문 (requests 우선)
        enriched = []
        for idx, item in enumerate(results):
            detail_text = ''
            try:
                dhtml = _fetch_html_requests(item['href'], timeout=10)
                if not dhtml:
                    dhtml = _fetch_html_selenium(item['href'])
                if dhtml:
                    dsoup = BeautifulSoup(dhtml, 'html.parser')
                    body_el = dsoup.select_one(
                        'div.article_view, div.news_view, [data-cloud="article_body"], '
                        'div[data-translation-body="true"], section.news_view, '
                        'div#harmonyContainer, div.news_body'
                    )
                    if body_el:
                        for s in body_el.select('script, style, .link_figure, figcaption'):
                            s.decompose()
                        detail_text = body_el.get_text(' ', strip=True)[:1500]
                    else:
                        meta = dsoup.select_one('meta[property="og:description"]')
                        if meta and meta.get('content'):
                            detail_text = meta['content'][:600]
            except Exception as e:
                print(f"[DAUM v33] 상세 본문 실패 idx={idx}: {e}")

            enriched.append(
                f"[{idx + 1}] 제목: {item['title']}\n"
                f"본문: {detail_text[:1000]}\n"
                f"링크: {item['href']}\n"
            )
            self._emit_progress(self.news_progress, 55 + int((idx / max(len(results), 1)) * 40))

        self._emit_progress(self.news_progress, 100)
        print(f"[DAUM v33] 완료: {len(enriched)}건")
        return f"📰 다음 인기뉴스 총 {len(enriched)}건 수집 완료\n{'=' * 50}\n\n" + '\n'.join(enriched)


    def _crawl_nate(self, url, max_count, filter_kws):
        """네이트 연예뉴스 랭킹 크롤링"""
        self._emit_progress(self.news_progress, 10)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"네이트 페이지 로드 실패: {e}")

        self._emit_progress(self.news_progress, 30)
        soup = BeautifulSoup(resp.text, 'html.parser')

        results = []

        # ── 네이트 뉴스 랭킹 셀렉터 (2026-04 구조 반영) ──
        links_found = []

        # 1~5위: div.mlt01 > a.lt1 > span.tb > h2.tit
        for mlt in soup.select('div.mlt01 a.lt1'):
            h2 = mlt.select_one('h2.tit')
            title = h2.get_text(strip=True) if h2 else mlt.get_text(strip=True)
            href = mlt.get('href', '')
            if href and not href.startswith('http'):
                href = 'https://news.nate.com' + href
            if title and len(title) > 5 and href not in [l[1] for l in links_found]:
                links_found.append((title, href))

        # 6~30위: ul.mduRankSubject > li > a > h2
        for li in soup.select('ul.mduRankSubject li'):
            a_tag = li.select_one('a[href]')
            if not a_tag:
                continue
            h2 = a_tag.select_one('h2')
            title = h2.get_text(strip=True) if h2 else a_tag.get_text(strip=True)
            href = a_tag.get('href', '')
            if href and not href.startswith('http'):
                href = 'https://news.nate.com' + href
            if title and len(title) > 5 and href not in [l[1] for l in links_found]:
                links_found.append((title, href))

        if filter_kws:
            links_found = [(t, h) for t, h in links_found if any(k.lower() in t.lower() for k in filter_kws)]

        self._emit_progress(self.news_progress, 50)

        for idx, (title, href) in enumerate(links_found[:max_count]):
            try:
                art_resp = requests.get(href, headers=headers, timeout=10)
                art_soup = BeautifulSoup(art_resp.text, 'html.parser')

                body = ''
                for content_sel in ['div#articleContetns', 'div.article_body', 'div#newsContents',
                                    'div.articleCont', 'article']:
                    el = art_soup.select_one(content_sel)
                    if el:
                        body = el.get_text(strip=True)[:1000]
                        break

                if not body:
                    meta = art_soup.select_one('meta[name="description"]') or art_soup.select_one(
                        'meta[property="og:description"]')
                    body = meta.get('content', '')[:500] if meta else ''

                results.append(f"[{idx + 1}] 제목: {title}\n본문: {body[:800]}\n링크: {href}\n")
            except Exception:
                results.append(f"[{idx + 1}] 제목: {title}\n(본문 수집 실패)\n링크: {href}\n")

            self._emit_progress(self.news_progress, 50 + int((idx / max(max_count, 1)) * 45))

        self._emit_progress(self.news_progress, 100)

        if not results:
            return "수집 결과가 없습니다. URL을 확인하세요."

        return f"📰 총 {len(results)}건 수집 완료\n{'=' * 50}\n\n" + '\n'.join(results)

    # ─── STEP 2: AI 글쓰기 ───
    def send_to_ai(self):
        # 체크된 뉴스만 전달 (체크 없으면 전체 폴백)
        selected = self._get_checked_news_indices()
        if self._collected_items and selected:
            chosen = [self._collected_items[i] for i in selected]
            blocks = []
            for n, it in enumerate(chosen, start=1):
                blocks.append(
                    f"[{n}] 제목: {it.get('title','')}\n"
                    f"본문: {it.get('body','')}\n"
                    f"링크: {it.get('href','')}\n"
                )
            text = (f"📰 선택 뉴스 {len(chosen)}건\n" + ("=" * 50) + "\n\n") + "\n".join(blocks)
            self.ai_input.setPlainText(text)
            self.main.log(f"✅ 수집 결과 → AI 입력 전달 완료 (선택 {len(chosen)}건)")
            return

        # 폴백: 전체 텍스트
        text = self.collect_result.toPlainText().strip()
        if not text:
            self.main.log("⚠️ 수집 결과가 비어 있습니다. 뉴스 수집을 먼저 실행하세요")
            return
        self.ai_input.setPlainText(text)
        if self._collected_items:
            self.main.log(f"⚠️ 체크된 뉴스가 없어 전체({len(self._collected_items)}건)를 AI 입력으로 전달했습니다")
        else:
            self.main.log("✅ 수집 결과 → AI 입력 전달 완료")

    # ── 헬퍼: 수집 표 / 원고 표 체크 관리 ──
    def _populate_collect_table(self, raw_text):
        """크롤러가 반환한 텍스트를 파싱하여 체크박스 표를 채운다."""
        try:
            items = self._parse_news_items(raw_text or "")
        except Exception as e:
            self.main.log(f"⚠️ 수집 결과 파싱 실패: {e}")
            items = []
        # 링크 별도 추출
        href_map = {}
        try:
            blocks = re.split(r'\[\d+\]\s*제목:', raw_text or "")
            for b in blocks:
                if not b.strip():
                    continue
                lines = b.strip().split('\n')
                title = lines[0].strip() if lines else ''
                href = ''
                for ln in lines[1:]:
                    if ln.strip().startswith('링크:'):
                        href = ln.split('링크:', 1)[1].strip()
                        break
                if title:
                    href_map[title[:100]] = href
        except Exception:
            pass

        self._collected_items = []
        for it in items:
            self._collected_items.append({
                'title': it.get('title', ''),
                'body': it.get('body', ''),
                'href': href_map.get(it.get('title', ''), ''),
            })

        self.collect_news_table.setRowCount(len(self._collected_items))
        for i, it in enumerate(self._collected_items):
            cb = QTableWidgetItem()
            cb.setFlags(cb.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            cb.setCheckState(Qt.CheckState.Checked)
            cb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.collect_news_table.setItem(i, 0, cb)
            self.collect_news_table.setItem(i, 1, QTableWidgetItem(it['title'][:120]))
            body_short = (it['body'][:80] + '…') if len(it['body']) > 80 else it['body']
            self.collect_news_table.setItem(i, 2, QTableWidgetItem(body_short))
            self.collect_news_table.setItem(i, 3, QTableWidgetItem(it['href']))
        self.main.log(f"📋 수집 표 갱신: {len(self._collected_items)}건 (기본 전체 체크)")

    def _toggle_all_news(self, state: bool):
        cs = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for r in range(self.collect_news_table.rowCount()):
            it = self.collect_news_table.item(r, 0)
            if it is not None:
                it.setCheckState(cs)
        self.main.log(f"📋 수집 뉴스 {'전체 선택' if state else '전체 해제'} ({self.collect_news_table.rowCount()}건)")

    def _get_checked_news_indices(self):
        out = []
        for r in range(self.collect_news_table.rowCount()):
            it = self.collect_news_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(r)
        return out

    def _on_news_selected(self):
        """수집 뉴스 행 클릭 시 본문을 AI 입력창에 미리보기"""
        rows = self.collect_news_table.selectionModel().selectedRows()
        if not rows:
            return
        items = list(getattr(self, '_collected_items', []) or [])
        idx = rows[0].row()
        if 0 <= idx < len(items):
            it = items[idx]
            preview = (
                f"[미리보기] 제목: {it.get('title','')}\n"
                f"링크: {it.get('href','')}\n\n"
                f"본문:\n{it.get('body','')}"
            )
            self.ai_input.setPlainText(preview)

    def _toggle_all_articles(self, state: bool):
        cs = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for r in range(self.article_table.rowCount()):
            it = self.article_table.item(r, 0)
            if it is not None:
                it.setCheckState(cs)
        self.main.log(f"📋 원고 {'전체 선택' if state else '전체 해제'} ({self.article_table.rowCount()}건)")

    def _get_checked_article_indices(self):
        out = []
        for r in range(self.article_table.rowCount()):
            it = self.article_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(r)
        return out

    def _set_article_check(self, row, checked=True):
        it = QTableWidgetItem()
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.article_table.setItem(row, 0, it)

    def _on_article_selected(self):
        rows = self.article_table.selectionModel().selectedRows()
        if not rows: return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            self.ai_output.setPlainText(self.generated_articles[idx].get('content', ''))

    def _copy_selected_article(self):
        rows = self.article_table.selectionModel().selectedRows()
        if not rows:
            self.main.log("⚠️ 원고를 먼저 선택하세요")
            return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            pyperclip.copy(self.generated_articles[idx].get('content', ''))
            self.main.log(f"📋 원고 #{idx+1} 복사 완료")

    def _copy_all_articles(self):
        if not self.generated_articles:
            self.main.log("⚠️ 생성된 원고가 없습니다")
            return
        all_text = ""
        for i, art in enumerate(self.generated_articles):
            all_text += f"\n{'='*60}\n[원고 {i+1}] {art.get('title','')}\n{'='*60}\n"
            all_text += art.get('content', '') + "\n"
        pyperclip.copy(all_text)
        self.main.log(f"📋 전체 {len(self.generated_articles)}건 원고 복사 완료")

    def _update_article_table(self):
        self.article_table.setRowCount(len(self.generated_articles))
        for i, art in enumerate(self.generated_articles):
            self.article_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 1, QTableWidgetItem(art.get('status', '완료')))
            self.article_table.setItem(i, 2, QTableWidgetItem(art.get('title', '')[:80]))
            content = art.get('content', '')
            self.article_table.setItem(i, 3, QTableWidgetItem(str(len(content)) if art.get("status") == "완료" else "실패"))

    def generate_ai(self):
        """수집된 각 뉴스 기사마다 개별 AI 원고를 생성 (테이블 기반)"""
        if getattr(self, '_generating', False):
            self._cancel_generate = True
            self.gen_btn.setText("🛑 취소 중...")
            self.main.log("🛑 AI 생성 취소 요청됨...")
            return

        content = self.ai_input.toPlainText().strip()
        if not content:
            self.main.log("⚠️ AI 입력이 비어 있습니다. '수집→AI' 버튼을 먼저 클릭하세요")
            return

        news_items = self._parse_news_items(content)
        if not news_items:
            self.main.log("⚠️ 파싱할 뉴스가 없습니다")
            return

        total = len(news_items)
        self._cancel_generate = False
        self._generating = True
        self.gen_btn.setText(f"🛑 생성 취소 (0/{total})")
        self.gen_btn.setStyleSheet("background-color:#8a1a1a; color:white; font-weight:bold; padding:8px;")
        self.ai_progress.setVisible(True)
        self.ai_progress.setValue(0)
        self.error_summary.setVisible(False)
        self.error_summary_label.setVisible(False)

        self.generated_articles = []
        self.article_table.setRowCount(total)
        for i, item in enumerate(news_items):
            self._set_article_check(i, checked=False)
            self.article_table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 2, QTableWidgetItem("대기"))
            self.article_table.setItem(i, 3, QTableWidgetItem(item['title'][:80]))
            self.article_table.setItem(i, 4, QTableWidgetItem("-"))

        model_text = self.ai_model.currentText()
        prompt = self.ai_prompt.toPlainText()
        char_target = self.char_count.value()
        self.main.log(f"🧠 AI 생성 시작 - 총 {total}건 개별 생성")

        thread = WorkerThread(lambda: None)
        def _ui_update(fn): thread.ui_update_signal.emit(fn)

        def do():
            generated = []
            errors_collection = []
            for idx, item in enumerate(news_items):
                if self._cancel_generate:
                    thread.log_signal.emit(f"🛑 AI 생성 취소됨 ({len(generated)}건 처리됨)")
                    break
                thread.log_signal.emit(f"📝 [{idx+1}/{total}] AI 원고 생성 중: {item['title'][:40]}...")
                _ui_update(lambda i=idx, t=total: self.gen_btn.setText(f"🛑 생성 취소 ({i+1}/{t})"))
                _ui_update(lambda i=idx: self.article_table.setItem(i, 1, QTableWidgetItem("생성중")))
                _ui_update(lambda i=idx, t=total: self.ai_progress.setValue(int((i / t) * 100)))

                if not item.get('body', '').strip() or len(item.get('body', '').strip()) < 20:
                    err_msg = '본문이 비어있어 건너뜁니다'
                    thread.log_signal.emit(f"⚠️ [{idx+1}/{total}] {err_msg}")
                    generated.append({'title': item['title'], 'content': f'❌ {err_msg}', 'status': '실패', 'error': err_msg})
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: self.article_table.setItem(i, 1, QTableWidgetItem("❌실패")))
                    continue

                full = f"{prompt}\n\n[목표 글자수: {char_target}자]\n"
                full += "- 제목을 첫 줄에 ## 제목 형태로 작성해주세요\n"
                full += f"\n[참고 뉴스 제목]: {item['title']}\n"
                full += f"[참고 뉴스 본문]:\n{item['body'][:4500]}"

                try:
                    if "sonar" in model_text.lower() or "perplexity" in model_text.lower():
                        sys_msg = "당신은 한국어 SEO 블로그 전문 작가입니다. 주어진 뉴스를 바탕으로 네이버 블로그에 게시할 완성된 포스트를 작성합니다."
                        result = call_perplexity(self.main.config.get('perplex_key', '').strip(), full, system_message=sys_msg)
                    elif "gemini" in model_text.lower():
                        result = call_gemini_text(self.main.config.get('gemini_key', '').strip(), full)
                    else:
                        result = call_openai_text(self.main.config.get('openai_key', '').strip(), full)

                    gen_title = item['title']
                    for line in result.split('\n'):
                        s = line.strip()
                        if s.startswith('##'):
                            gen_title = s.lstrip('#').strip()
                            break

                    generated.append({'title': gen_title, 'content': result, 'status': '완료', 'source': item.get('body', '')[:500]})
                    self.generated_articles = list(generated)
                    try:
                        _queue = [{'order': qi+1, 'title': ga['title'], 'content': ga['content'], 'status': 'pending', 'result': ''} for qi, ga in enumerate(generated) if ga.get('status') == '완료']
                        if _queue:
                            with open('publish_queue_news_incremental.json', 'w', encoding='utf-8') as _qf:
                                json.dump(_queue, _qf, ensure_ascii=False, indent=2)
                            thread.log_signal.emit(f"💾 [{idx+1}/{total}] 발행 대기열 저장 완료 ({len(_queue)}건)")
                    except Exception:
                        pass
                    thread.log_signal.emit(f"✅ [{idx+1}/{total}] 완료: {gen_title[:40]}")

                    if self.auto_dalle_check.isChecked():
                        oai_key = self.main.config.get('openai_key', '').strip()
                        if oai_key:
                            auto_count = self.auto_dalle_count.value()
                            thread.log_signal.emit(f"  🤖 [{idx+1}/{total}] 자동 이미지 생성 중 ({auto_count}장)...")
                            auto_imgs = self._auto_generate_dalle_for_article(gen_title, oai_key, count=auto_count)
                            if auto_imgs:
                                self._image_pool.extend(auto_imgs)
                                thread.log_signal.emit(f"  🖼️ [{idx+1}/{total}] 자동 이미지 {len(auto_imgs)}장 생성 완료")
                    _ui_update(lambda i=idx, t=gen_title, c=len(result): (
                        self.article_table.setItem(i, 1, QTableWidgetItem("✅완료")),
                        self.article_table.setItem(i, 2, QTableWidgetItem(t[:80])),
                        self.article_table.setItem(i, 3, QTableWidgetItem(str(c)))
                    ))
                except Exception as e:
                    err_msg = format_error_message(e)
                    thread.log_signal.emit(f"❌ [{idx+1}/{total}] 생성 실패: {err_msg}")
                    generated.append({'title': item['title'], 'content': f'❌ 오류: {err_msg}', 'status': '실패', 'error': err_msg})
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: self.article_table.setItem(i, 1, QTableWidgetItem("❌실패")))

                if idx < total - 1 and not self._cancel_generate:
                    wait_seconds = 4 if "perplexity" in model_text.lower() or "sonar" in model_text.lower() else 2
                    for _ in range(wait_seconds * 2):
                        if self._cancel_generate: break
                        time.sleep(0.5)

            self.generated_articles = generated
            _ui_update(lambda: self.ai_progress.setValue(100))
            success = sum(1 for a in generated if a.get('status') == '완료')
            failed = sum(1 for a in generated if a.get('status') == '실패')
            if errors_collection:
                unique_errors = list(dict.fromkeys(errors_collection))
                error_text = "\n- ".join(unique_errors[:5])
                _ui_update(lambda: (self.error_summary.setPlainText(f"- {error_text}"), self.error_summary.setVisible(True), self.error_summary_label.setVisible(True)))
            thread.log_signal.emit(f"✅ AI 원고 생성 완료 - 총 {total}건 중 {success}건 성공, {failed}건 실패")
            return f"✅ 총 {total}건 원고 생성 완료 ({success}건 성공 / {failed}건 실패)"

        thread.func = do
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: None)
        def _on_gen_finished():
            self._generating = False
            self._cancel_generate = False
            self.gen_btn.setText("🎯 AI 원고 생성")
            self.gen_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
            self.ai_progress.setVisible(False)
        thread.finished_signal.connect(_on_gen_finished)
        thread.start()
    def _export_articles_excel(self):
        """생성된 원고를 엑셀로 내보내기"""
        if not self.generated_articles:
            self.main.log("⚠️ 내보낼 원고가 없습니다")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            cfg = self.SOURCE_CONFIG.get(self.source, {})
            src_name = cfg.get('title', '뉴스').replace('️⃣ ', '')
            path, _ = QFileDialog.getSaveFileName(self, "엑셀 내보내기", f"{src_name}_원고_목록.xlsx", "Excel Files (*.xlsx)")
            if not path:
                return
            wb = Workbook()
            ws = wb.active
            ws.title = "원고 목록"
            headers = ["번호", "제목", "글자수", "상태", "원문 참고", "생성된 원고"]
            header_fill = PatternFill('solid', fgColor='1a4a8a')
            header_font = Font(bold=True, color='FFFFFF')
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=h)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')
            ws.column_dimensions['A'].width = 6
            ws.column_dimensions['B'].width = 50
            ws.column_dimensions['C'].width = 10
            ws.column_dimensions['D'].width = 8
            ws.column_dimensions['E'].width = 60
            ws.column_dimensions['F'].width = 80
            for i, art in enumerate(self.generated_articles):
                ws.cell(row=i+2, column=1, value=i+1)
                ws.cell(row=i+2, column=2, value=art.get('title', ''))
                ws.cell(row=i+2, column=3, value=len(art.get('content', '')))
                ws.cell(row=i+2, column=4, value=art.get('status', '완료'))
                ws.cell(row=i+2, column=5, value=art.get('source', '')[:500])
                ws.cell(row=i+2, column=6, value=art.get('content', ''))
            wb.save(path)
            self.main.log(f"📊 엑셀 내보내기 완료: {path} ({len(self.generated_articles)}건)")
        except ImportError:
            self.main.log("❌ openpyxl 패키지가 필요합니다: pip install openpyxl")
        except Exception as e:
            self.main.log(f"❌ 엑셀 내보내기 오류: {e}")

    def _import_articles_excel(self):
        """엑셀 파일에서 원고를 불러오기"""
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 원고 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if not path:
            return
        try:
            import pandas as pd
            df = pd.read_excel(path)
            imported = []
            for _, row in df.iterrows():
                title = str(row.get('제목', row.get('title', '제목 없음')))
                content = str(row.get('생성된 원고', row.get('content', row.get('생성된원고', ''))))
                source = str(row.get('원문 참고', row.get('원문참고', row.get('source', ''))))
                if content and content != 'nan':
                    imported.append({
                        'title': title if title != 'nan' else '제목 없음',
                        'content': content,
                        'source': source if source != 'nan' else '',
                        'status': '완료',
                    })
            if imported:
                self.generated_articles.extend(imported)
                self._update_article_table()
                self.main.log(f"📥 엑셀에서 {len(imported)}건 원고 불러옴 (총 {len(self.generated_articles)}건)")
            else:
                self.main.log("⚠️ 엑셀에서 불러올 원고가 없습니다 (제목/생성된 원고 컬럼 필요)")
        except Exception as e:
            self.main.log(f"❌ 엑셀 원고 불러오기 오류: {e}")

    def _parse_news_items(self, text):
        """수집 결과 텍스트에서 개별 뉴스를 파싱"""
        items = []
        blocks = re.split(r'\[\d+\]\s*제목:', text)
        for block in blocks:
            if not block.strip():
                continue
            lines = block.strip().split('\n')
            title = lines[0].strip() if lines else ''
            body = ''
            for l in lines[1:]:
                if l.startswith('본문:'):
                    body = l.replace('본문:', '').strip()
                elif l.startswith('링크:'):
                    continue
                elif body:
                    body += '\n' + l
            if not body:
                body = '\n'.join(lines[1:])
            if title and len(title) > 3:
                items.append({'title': title[:100], 'body': body[:3000]})
        return items if items else [{'title': '뉴스 포스트', 'body': text[:3000]}]

    def save_draft(self):
        text = self.ai_output.toPlainText().strip()
        if not text:
            self.main.log("⚠️ 저장할 원고가 없습니다")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"news_{self.source}_{ts}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "원고 저장", fname, "Text Files (*.txt)")
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            self.main.log(f"💾 원고 저장 완료: {path}")

    # ─── STEP 3: 블로그 발행 ───
    def _get_selected_account(self):
        """선택된 계정의 id/pw 반환"""
        sel_id = self.account_combo.currentText().strip() if hasattr(self, 'account_combo') else ''
        accounts = self.main.config.get('naver_accounts', [])
        for acc in accounts:
            if acc.get('id') == sel_id:
                return acc.get('id', ''), acc.get('pw', '')
        return self.main.config.get('naver_id', ''), self.main.config.get('naver_pw', '')

    def load_blog_folders(self):
        """블로그 카테고리 폴더 불러오기"""
        naver_id, naver_pw = self._get_selected_account()
        if not naver_id or not naver_pw:
            self.main.log("⚠️ 설정에서 네이버 계정을 먼저 입력하세요")
            return

        self.load_folders_btn.setEnabled(False)
        self.load_folders_btn.setText("불러오는 중...")

        def do():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            import re
            opts = uc.ChromeOptions()
            opts.add_argument('--start-maximized')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--user-data-dir=./chrome_profile')
            driver = make_uc_driver(opts)
            folders = []

            try:
                # ── 먼저 로그인 (실패 시 60초 수동 대기) ──
                if not naver_login_with_fallback(driver, naver_id, naver_pw, self.main.log):
                    return "❌ 로그인 실패"

                # ── 1단계: API로 카테고리 가져오기 ──
                try:
                    cat_url = f"https://blog.naver.com/NBlogCategoryListAjax.naver?blogId={naver_id}"
                    driver.get(cat_url)
                    time.sleep(2)
                    page_text = driver.page_source

                    if 'categoryName' in page_text:
                        cat_names = re.findall(r'"categoryName"\s*:\s*"([^"]+)"', page_text)
                        for cn in cat_names:
                            try:
                                decoded = cn.encode('utf-8').decode('unicode_escape')
                            except Exception:
                                decoded = cn
                            if decoded and decoded not in folders:
                                folders.append(decoded)
                    if folders:
                        self.main.log(f"✅ API로 {len(folders)}개 카테고리 확보")
                        return '|||'.join(folders)
                except Exception as e:
                    self.main.log(f"ℹ️ API 시도 실패, 스크래핑으로 전환: {e}")

                # ── 2단계: 블로그 메인 페이지 스크래핑 ──
                try:
                    driver.get(f"https://blog.naver.com/{naver_id}")
                    time.sleep(3)
                    try:
                        iframe = driver.find_element(By.ID, 'mainFrame')
                        driver.switch_to.frame(iframe)
                        time.sleep(1)
                    except Exception:
                        pass

                    selectors = [
                        'div.category a',
                        'ul.category_list li a',
                        'div.area_category a',
                        '#category a',
                        '.blog-category a',
                        'a[href*="categoryNo"]',
                    ]
                    for sel in selectors:
                        try:
                            cats = driver.find_elements(By.CSS_SELECTOR, sel)
                            for cat in cats:
                                name = cat.text.strip()
                                cleaned = re.sub(r'\s*\(\d+\)\s*$', '', name).strip()
                                if cleaned and cleaned not in ['전체보기', '분류 전체보기', '카테고리',
                                                               ''] and cleaned not in folders:
                                    folders.append(cleaned)
                        except Exception:
                            continue
                        if folders:
                            break
                    driver.switch_to.default_content()
                except Exception:
                    pass

                if folders:
                    self.main.log(f"✅ 메인페이지에서 {len(folders)}개 카테고리 확보")
                    return '|||'.join(folders)

                # ── 3단계: 글쓰기 에디터에서 가져오기 ──
                self.main.log("ℹ️ 메인페이지 실패, 에디터에서 시도...")
                try:
                    driver.get("https://blog.naver.com/GoBlogWrite.naver")
                    time.sleep(4)
                    try:
                        iframe = driver.find_element(By.ID, 'mainFrame')
                        driver.switch_to.frame(iframe)
                        time.sleep(1)
                    except Exception:
                        pass

                    iframes = driver.find_elements(By.TAG_NAME, 'iframe')
                    for ifr in iframes:
                        try:
                            driver.switch_to.frame(ifr)
                            cats = driver.find_elements(By.CSS_SELECTOR,
                                                        'select#categoryId option, ul.category_list li')
                            for cat in cats:
                                name = cat.text.strip()
                                if name and name not in ['카테고리 선택', ''] and name not in folders:
                                    folders.append(name)
                            if folders:
                                break
                            driver.switch_to.default_content()
                        except Exception:
                            driver.switch_to.default_content()
                except Exception:
                    pass

                if not folders:
                    folders = ['기본 카테고리']

                return '|||'.join(folders)
            finally:
                driver.quit()

        def on_result(r):
            if str(r).startswith('❌'):
                self.main.log(r)
                return
            folders = r.split('|||')
            self.blog_folder.clear()
            self.blog_folder.addItems(folders)
            self._save_folders_to_config(naver_id, folders, self.blog_folder.currentText().strip())
            self.main.log(f"✅ {len(folders)}개 블로그 폴더 로드 완료")

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.load_folders_btn.setEnabled(True),
            self.load_folders_btn.setText("🔄 폴더 불러오기")
        ))
        thread.start()

    def publish_to_blog(self):
        """generated_articles 리스트를 순회하며 각각 블로그에 발행 (순차 처리)"""
        # 중복 실행 방지
        if self._publishing_flag:
            self.main.log("⚠️ 이미 발행이 진행 중입니다. 완료될 때까지 기다려주세요.")
            return

        publish_queue = self._build_publish_queue()
        if not publish_queue:
            self.main.log("⚠️ 발행할 원고가 없습니다. AI 글쓰기를 먼저 실행하세요")
            return

        config = self.main.config
        naver_id, naver_pw = self._get_selected_account()
        if not naver_id or not naver_pw:
            self.main.log("⚠️ 설정에서 네이버 계정을 먼저 입력하세요")
            return

        folder = self.blog_folder.currentText().strip()
        mode = self.save_mode.currentText()
        total = len(publish_queue)
        queue_path = self._write_publish_queue_snapshot(naver_id, publish_queue)
        self.publish_queue = publish_queue
        self._publishing_flag = True

        self.publish_btn.setEnabled(False)
        self.publish_btn.setText(f"발행 중... (0/{total})")
        self.pub_progress.setVisible(True)
        self.pub_progress.setValue(0)

        self.main.log(f"🚀 총 {total}건 블로그 발행 시작")
        self.main.log(f"🗂️ 발행 대기열 저장 완료: {queue_path}")

        def do():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            opts = uc.ChromeOptions()
            opts.add_argument('--no-sandbox')
            opts.add_argument('--start-maximized')
            opts.add_argument('--user-data-dir=./chrome_profile')
            opts.add_argument('--disable-popup-blocking')
            prefs = {
                'profile.default_content_setting_values.popups': 0,
            }
            opts.add_experimental_option('prefs', prefs)
            driver = make_uc_driver(opts)

            # ── window.open 완전 차단 (드라이버 생성 직후) ──
            def block_window_open():
                try:
                    driver.execute_script("""
                        if (!window._woBlocked) {
                            window._origOpen = window.open;
                            window.open = function(url) {
                                if (url) window.location.href = url;
                                return window;
                            };
                            window._woBlocked = true;
                        }
                    """)
                except Exception:
                    pass
            # ── "작성중인 글이 있습니다" 팝업 취소 처리 ──
            def dismiss_draft_popup():
                """에디터 진입 시 '작성중인 글이 있습니다' 팝업이 뜨면 취소 버튼 클릭"""
                try:
                    time.sleep(2)
                    cancel_selectors = [
                        'button.se-popup-button-cancel',
                        'button.cancel_btn__WEaBq',
                        'button.cancel_btn',
                        'button[class*="cancel"]',
                        'button.se-cancel',
                    ]
                    # 팝업 텍스트로도 탐지
                    try:
                        popup_texts = driver.find_elements(By.XPATH,
                            "//*[contains(text(),'작성중인') or contains(text(),'작성 중인') or contains(text(),'임시저장')]")
                        if popup_texts:
                            self.log_emit("  ℹ️ '작성중인 글' 팝업 감지 → 취소 클릭 시도")
                            for sel in cancel_selectors:
                                try:
                                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                            # CSS 셀렉터 실패 시 XPATH로 취소/아니오 버튼 찾기
                            cancel_btns = driver.find_elements(By.XPATH,
                                "//button[contains(text(),'취소') or contains(text(),'아니') or contains(text(),'아니오') or contains(text(),'새로')]")
                            for btn in cancel_btns:
                                try:
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료 (XPATH)")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        pass
                except Exception as e:
                    self.log_emit(f"  ℹ️ 팝업 처리 중 예외(무시): {e}")
                return False


            # ── 원고 정제 함수 ──
            def sanitize_content(raw_content):
                """발행 전 원고에서 불필요한 마크다운/이미지/참조를 제거"""
                lines = raw_content.split('\n')
                cleaned = []
                for line in lines:
                    s = line.strip()
                    # 구분선 제거
                    if re.match(r'^[━─═\-]{3,}', s):
                        continue
                    # [이미지 삽입] 줄 제거
                    if s == '[이미지 삽입]':
                        continue
                    # 마크다운 이미지 ![alt](url) 제거
                    if re.match(r'^!\[.*\]\(.*\)$', s):
                        continue
                    # 순수 URL만 있는 줄 제거 (이미지 URL)
                    if re.match(r'^https?://\S+$', s):
                        continue
                    # URL (설명) 형태 줄 제거
                    if re.match(r'^https?://\S+\s*\(.*\)$', s):
                        continue
                    # 📄 [번호] 헤더 줄 제거
                    if re.match(r'^📄\s*\[\d+\]', s):
                        continue
                    # (공백 제외 N자) 제거
                    if re.match(r'^\(공백\s*제외\s*\d+자\)$', s):
                        continue
                    # **추가 반전형 이미지 키워드** 블록 제거
                    if '이미지 키워드' in s and ('추가' in s or '반전형' in s):
                        continue
                    # 번호. URL 형태 (이미지 리스트) 제거
                    if re.match(r'^\d+\.\s*https?://\S+', s):
                        continue
                    # 참조 번호 [1][2] 등 제거 (텍스트는 유지)
                    s = re.sub(r'\[\d+\]', '', s)
                    # ## 마크다운 헤더 → 일반 텍스트
                    s = re.sub(r'^#{1,6}\s*', '', s)
                    # **bold** → 일반 텍스트
                    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
                    s = s.strip()
                    if s:
                        cleaned.append(s)
                # 연속 빈 줄 정리
                result = '\n'.join(cleaned)
                result = re.sub(r'\n{3,}', '\n\n', result)
                return result.strip()

            def validate_article(article):
                """발행 가능한 원고인지 검증. 불가 시 사유 반환"""
                title = (article.get('title') or '').strip()
                content = (article.get('content') or '').strip()
                if not title or not content:
                    return '제목 또는 본문이 비어 있습니다'
                if '필수 입력 정보 부재' in content or '요청을 정확히 처리할 수 없습니다' in content:
                    return '오류 원고 (AI가 생성 실패한 항목)'
                # 정제 후에도 내용이 너무 짧으면 스킵
                sanitized = sanitize_content(content)
                if len(sanitized) < 50:
                    return f'정제 후 본문이 너무 짧습니다 ({len(sanitized)}자)'
                return None  # 검증 통과

            success_count = 0
            fail_count = 0
            main_window = None

            def update_queue_status(index, status, result):
                publish_queue[index]['status'] = status
                publish_queue[index]['result'] = result
                self._write_publish_queue_snapshot(naver_id, publish_queue)

            def keep_only_window(target_handle=None):
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                target = target_handle or main_window or handles[-1]
                for handle in list(handles):
                    if handle == target:
                        continue
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(target)
                main_window = target

            def close_all_extra_tabs():
                """main_window 외 모든 탭을 닫는다"""
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                if not main_window or main_window not in handles:
                    main_window = handles[0]
                for h in handles:
                    if h == main_window:
                        continue
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                try:
                    driver.switch_to.window(main_window)
                except Exception:
                    pass

            def open_editor_single_tab():
                """항상 단일 탭에서 글쓰기 에디터를 연다"""
                nonlocal main_window

                # 0) 먼저 탭 1개만 남기기
                close_all_extra_tabs()

                # 1) 현재 탭에서 에디터로 이동 (execute_script로 강제)
                editor_url = 'https://blog.naver.com/GoBlogWrite.naver'
                try:
                    driver.execute_script(f'window.location.href = "{editor_url}";')
                except Exception:
                    driver.get(editor_url)

                time.sleep(5)

                # 2) 새 탭이 열렸으면 마지막 탭(에디터)만 남기고 닫기
                handles = list(driver.window_handles)
                if len(handles) > 1:
                    editor_tab = handles[-1]
                    for h in handles:
                        if h == editor_tab:
                            continue
                        try:
                            driver.switch_to.window(h)
                            driver.close()
                        except Exception:
                            pass
                    driver.switch_to.window(editor_tab)
                    main_window = editor_tab
                    self.log_emit(f"  ℹ️ 추가 탭 {len(handles)-1}개 닫고 에디터 탭만 유지")
                else:
                    main_window = handles[0] if handles else driver.current_window_handle
                    driver.switch_to.window(main_window)

                # 3) iframe 전환
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

                # mainFrame이 있으면 진입
                try:
                    iframe = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.ID, 'mainFrame'))
                    )
                    driver.switch_to.frame(iframe)
                    time.sleep(1)
                    self.log_emit("  ✅ mainFrame iframe 진입 성공")
                    dismiss_draft_popup()
                except Exception:
                    self.log_emit("  ℹ️ mainFrame 없음, 직접 에디터 접근 시도")

                # 4) 에디터 로드 확인
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'div.se-title-text, div[contenteditable="true"]'))
                    )
                    self.log_emit("  ✅ 에디터 로드 확인")
                except Exception:
                    self.log_emit("  ⚠️ 에디터 요소를 찾지 못했습니다 (계속 시도)")

            def select_folder_if_needed():
                if not folder:
                    return True
                selectors = ['select#categoryId', 'select[name="categoryId"]']
                for selector in selectors:
                    try:
                        cat_select = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        cat_select.click()
                        time.sleep(0.3)
                        for opt in cat_select.find_elements(By.TAG_NAME, 'option'):
                            if folder in opt.text:
                                opt.click()
                                time.sleep(0.3)
                                return True
                    except Exception:
                        continue
                return False

            def input_title(art_title):
                title_selectors = [
                    'span.se-placeholder',
                    'div[data-name="title"] div[contenteditable="true"]',
                    'div.se-section-title div.se-text-paragraph',
                    'div.se-title-text',
                ]
                last_error = None
                for selector in title_selectors:
                    try:
                        title_el = WebDriverWait(driver, 3).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                        try:
                            title_el.click()
                        except Exception:
                            from selenium.webdriver.common.action_chains import ActionChains
                            ActionChains(driver).move_to_element(title_el).click().perform()
                        time.sleep(0.2)
                        pyperclip.copy(art_title)
                        safe_hotkey(driver, 'ctrl', 'a')
                        time.sleep(0.1)
                        safe_hotkey(driver, 'ctrl', 'v')
                        time.sleep(0.3)
                        self.log_emit(f"  ✅ 제목 입력 완료: {art_title[:30]}")
                        return True
                    except Exception as e:
                        last_error = e
                        continue
                raise RuntimeError(f'제목 입력 실패: {last_error}')

            def focus_body():
                body_selectors = [
                    'div.se-section-text div[contenteditable="true"]',
                    'div.se-component-content div[contenteditable="true"]',
                    'div.se-text-paragraph',
                    'div[contenteditable="true"]',
                ]
                for selector in body_selectors:
                    try:
                        elems = driver.find_elements(By.CSS_SELECTOR, selector)
                        for elem in elems:
                            if not elem.is_displayed():
                                continue
                            try:
                                driver.execute_script("arguments[0].click();", elem)
                            except Exception:
                                elem.click()
                            time.sleep(0.3)
                            return True
                    except Exception:
                        continue
                try:
                    safe_press(driver, 'tab')
                    time.sleep(0.3)
                    return True
                except Exception:
                    return False

            def input_body(art_content):
                if not focus_body():
                    raise RuntimeError('본문 입력 영역 포커스 실패')

                # v26: 본문 전처리만 먼저, 정렬은 입력 후에 적용
                art_content = preprocess_article_lines(art_content)
                alignment = getattr(self, '_selected_alignment', 'left')

                wrote_any = False
                for raw_line in art_content.split('\n'):
                    line = raw_line.rstrip()
                    if not line.strip():
                        safe_press(driver, 'enter')
                        time.sleep(0.05)
                        continue

                    # 스티커 토큰 처리
                    sticker_idxs = extract_sticker_indices(line)
                    line_wo_sticker = strip_sticker_tokens(line)

                    # 인용구 자동 감지 — 한 줄만 인용 블록, 나머지는 일반 문단
                    if line_wo_sticker and looks_like_quote(line_wo_sticker):
                        insert_quote_block(driver, line_wo_sticker)
                        wrote_any = True
                    elif line_wo_sticker:
                        pyperclip.copy(line_wo_sticker)
                        safe_hotkey(driver, 'ctrl', 'v')
                        safe_press(driver, 'enter')
                        time.sleep(0.1)
                        wrote_any = True

                    for sidx in sticker_idxs:
                        try:
                            insert_naver_sticker(driver, sidx)
                            safe_press(driver, 'enter')
                        except Exception:
                            pass

                if not wrote_any:
                    raise RuntimeError('본문 내용이 비어 있어 입력하지 못했습니다')

                # v26: 입력 완료 후 전체 선택 → 정렬 적용 (+ JS fallback)
                try:
                    apply_alignment(driver, alignment, select_all_first=True)
                except Exception:
                    pass

            def submit_article(art_idx, art_title):
                time.sleep(1)
                if '임시' in mode:
                    save_selectors = [
                        'button[data-testid="save-btn"]',
                        'button.save_btn__Y5f57',
                        'button.save_btn',
                        'button[class*="save"]',
                    ]
                    clicked = False
                    for selector in save_selectors:
                        try:
                            btn = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if not clicked:
                        try:
                            btn = driver.find_element(
                                By.XPATH, '//button[contains(., "임시저장")]'
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            clicked = True
                        except Exception:
                            pass
                    if not clicked:
                        raise RuntimeError('임시저장 버튼을 찾지 못했습니다')
                    time.sleep(2)
                    self.log_emit(f"  💾 [{art_idx + 1}/{total}] 임시저장 완료: {art_title[:30]}")
                    driver.execute_script('window.location.href = "https://blog.naver.com";')
                    time.sleep(2)
                    return True, '임시저장 완료'


                publish_selectors = [
                    'button[data-testid="publish-btn"]',
                    'button.publish_btn__Y5f57',
                    'button.publish_btn',
                    'button[class*="publish"]',
                ]
                confirm_selectors = [
                    'button.se-popup-button-confirm',
                    'button.confirm_btn__WEaBq',
                    'button.confirm_btn',
                    'button[class*="confirm"]',
                ]

                clicked = False
                for selector in publish_selectors:
                    try:
                        btn = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        driver.execute_script("arguments[0].click();", btn)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError('발행 버튼을 찾지 못했습니다')

                time.sleep(2)

                confirmed = False
                for selector in confirm_selectors:
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, selector)
                        driver.execute_script("arguments[0].click();", btn)
                        confirmed = True
                        break
                    except Exception:
                        continue

                if not confirmed:
                    self.log_emit('  ℹ️ 발행 확인 팝업이 없어 바로 완료 여부를 확인합니다')

                time.sleep(4)
                current_url = (driver.current_url or '').lower()
                if 'goblogwrite' in current_url:
                    self.log_emit('  ⚠️ 에디터 URL에 머물러 있음, 발행 재시도...')
                    # 한 번 더 confirm 버튼 시도
                    for selector in confirm_selectors:
                        try:
                            btn = driver.find_element(By.CSS_SELECTOR, selector)
                            driver.execute_script("arguments[0].click();", btn)
                            time.sleep(3)
                            break
                        except Exception:
                            continue

                self.log_emit(f"  📤 [{art_idx + 1}/{total}] 즉시발행 완료")
                return True, driver.current_url

            try:
                self.log_emit('🔑 네이버 로그인 중...')
                if not naver_login_with_fallback(driver, naver_id, naver_pw, self.log_emit):
                    return '❌ 로그인 실패 - 설정에서 계정을 확인하세요'
                self.log_emit('✅ 로그인 완료')
                main_window = driver.current_window_handle
                close_all_extra_tabs()
                block_window_open()

                for art_idx, article in enumerate(publish_queue):
                    try:
                        art_title = article.get('title', f'포스트 {art_idx + 1}')
                        art_content = article.get('content', '')

                        # ── 원고 검증 ──
                        skip_reason = validate_article(article)
                        if skip_reason:
                            self.log_emit(f"\n⏭️ [{art_idx + 1}/{total}] 건너뜀: {skip_reason} — {art_title[:30]}")
                            fail_count += 1
                            update_queue_status(art_idx, 'skipped', skip_reason)
                            continue

                        # ── 원고 정제 ──
                        art_content = sanitize_content(art_content)

                        # ── 본문에서 제목 줄 제거 (제목은 별도 입력) ──
                        content_lines = art_content.split('\n')
                        filtered_lines = []
                        title_stripped = False
                        for cl in content_lines:
                            cl_clean = cl.strip()
                            if not title_stripped and cl_clean and (
                                cl_clean == art_title.strip() or
                                re.sub(r'^#{1,6}\s*', '', cl_clean) == art_title.strip() or
                                cl_clean.replace('**', '') == art_title.strip()
                            ):
                                title_stripped = True
                                continue
                            filtered_lines.append(cl)
                        art_content = '\n'.join(filtered_lines).strip()
                        self.log_emit(f"\n📝 [{art_idx + 1}/{total}] 발행 시작: {art_title[:40]}... (정제 후 {len(art_content)}자)")
                        QTimer.singleShot(0, lambda idx=art_idx: self.publish_btn.setText(f"발행 중... ({idx + 1}/{total})"))

                        open_editor_single_tab()
                        block_window_open()  # 에디터 로드 후 다시 차단

                        if folder and not select_folder_if_needed():
                            self.log_emit('  ⚠️ 폴더 선택 실패, 기본 카테고리로 진행합니다')

                        input_title(art_title)
                        input_body(art_content)
                        ok, result_message = submit_article(art_idx, art_title)

                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_all_extra_tabs()
                        time.sleep(1)

                        success_count += 1
                        update_queue_status(art_idx, 'done', result_message)

                    except Exception as e:
                        self.log_emit(f"  ❌ [{art_idx + 1}/{total}] 발행 실패: {e}")
                        fail_count += 1
                        update_queue_status(art_idx, 'error', str(e))
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_all_extra_tabs()
                        time.sleep(1)

                    self._emit_progress(self.pub_progress, int(((art_idx + 1) / total) * 100))
                    time.sleep(2)

                return f"✅ 블로그 발행 완료! 성공: {success_count}건, 실패: {fail_count}건"

            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.main.log(r))

        def on_news_publish_finished():
            self._publishing_flag = False
            self.publish_btn.setEnabled(True)
            self.publish_btn.setText('🚀 블로그 발행')
            self.pub_progress.setVisible(False)

        thread.finished_signal.connect(on_news_publish_finished)
        thread.start()
# ═══════════════════════════════════════════════════════════════
#  5. 카페 수집 페이지
# ═══════════════════════════════════════════════════════════════


    def _refresh_image_table(self):
        """이미지 풀 테이블 갱신"""
        self.image_table.setRowCount(len(self._image_pool))
        for i, img in enumerate(self._image_pool):
            self.image_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.image_table.setItem(i, 1, QTableWidgetItem("📁업로드" if img['source'] == 'upload' else "🤖AI"))
            self.image_table.setItem(i, 2, QTableWidgetItem(img.get('desc', '')[:60]))
            self.image_table.setItem(i, 3, QTableWidgetItem(img.get('path', '')))
        self.img_status.setText(f"총 {len(self._image_pool)}개 이미지 등록됨")

    def _upload_images(self):
        """로컬 이미지 파일 직접 업로드"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "이미지 파일 선택", "",
            "이미지 파일 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;모든 파일 (*.*)"
        )
        if not files:
            return
        for fpath in files:
            self._image_pool.append({
                'source': 'upload',
                'desc': os.path.basename(fpath),
                'path': fpath,
            })
        self._refresh_image_table()
        self.main.log(f"🖼️ {len(files)}개 이미지 업로드 완료 (총 {len(self._image_pool)}개)")

    def _generate_dalle_images(self):
        """DALL-E API로 이미지 생성"""
        prompt = self.dalle_prompt.text().strip()
        if not prompt:
            self.main.log("⚠️ DALL-E 프롬프트를 입력하세요")
            return
        api_key = self.main.config.get('openai_key', '').strip()
        if not api_key:
            self.main.log("⚠️ OpenAI API Key가 설정되지 않았습니다 (설정 탭에서 입력)")
            return

        count = self.dalle_count.value()
        size = self.dalle_size.currentText()

        self.img_dalle_btn.setEnabled(False)
        self.img_dalle_btn.setText(f"🤖 생성 중... (0/{count})")
        self.main.log(f"🤖 DALL-E 이미지 생성 시작: '{prompt}' × {count}장")

        def do_gen():
            results = []
            for i in range(count):
                try:
                    path = generate_dalle_image(api_key, prompt, size=size)
                    results.append({'source': 'dalle', 'desc': prompt, 'path': path})
                    thread.log_signal.emit(f"  🖼️ [{i+1}/{count}] 이미지 생성 완료")
                except Exception as e:
                    thread.log_signal.emit(f"  ❌ [{i+1}/{count}] 생성 실패: {format_error_message(e)}")
                if i < count - 1:
                    time.sleep(2)
            return results

        thread = WorkerThread(do_gen)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def on_result(results):
            if isinstance(results, list):
                self._image_pool.extend(results)
                self._refresh_image_table()
                self.main.log(f"✅ DALL-E 이미지 {len(results)}장 생성 완료 (총 {len(self._image_pool)}개)")
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.img_dalle_btn.setEnabled(True),
            self.img_dalle_btn.setText("🤖 AI 이미지 생성 (ChatGPT)")
        ))
        thread.start()

    def _auto_generate_dalle_for_article(self, article_title, api_key, count=1, size="1024x1024"):
        """원고 제목 기반 자동 DALL-E 이미지 생성 (워커 스레드 내에서 호출)"""
        generated = []
        for i in range(count):
            try:
                prompt = f"네이버 블로그 포스팅용 고퀄리티 사진. 주제: {article_title}. 자연스러운 실제 사진 스타일, 텍스트 없이."
                path = generate_dalle_image(api_key, prompt, size=size)
                generated.append({'source': 'dalle', 'desc': f"자동생성: {article_title}", 'path': path})
            except Exception as e:
                pass  # 자동 생성 실패는 무시
            if i < count - 1:
                time.sleep(2)
        return generated

    def _delete_selected_image(self):
        """선택된 이미지 삭제"""
        rows = set(item.row() for item in self.image_table.selectedItems())
        if not rows:
            self.main.log("⚠️ 삭제할 이미지를 선택하세요")
            return
        for idx in sorted(rows, reverse=True):
            if 0 <= idx < len(self._image_pool):
                self._image_pool.pop(idx)
        self._refresh_image_table()
        self.main.log(f"🗑️ {len(rows)}개 이미지 삭제됨")

    def _clear_all_images(self):
        """모든 이미지 삭제"""
        self._image_pool.clear()
        self._refresh_image_table()
        self.main.log("🗑️ 전체 이미지 삭제됨")

    def _get_images_for_article(self, article_idx, photo_count):
        """원고에 사용할 이미지들 반환 (라운드 로빈).
        v27: photo_count가 0이어도 풀에 이미지가 있으면 최소 1장 반환 → 본문 시작 부분에 자동 삽입."""
        if not self._image_pool:
            return []
        total_imgs = len(self._image_pool)
        # [사진] 토큰이 0개여도 풀에 있으면 1장은 사용
        effective = max(photo_count, 1) if total_imgs > 0 else photo_count
        start = (article_idx * effective) % total_imgs
        result = []
        for i in range(effective):
            idx = (start + i) % total_imgs
            result.append(self._image_pool[idx]['path'])
        return result
    def _insert_image_to_editor(self, driver, image_path):
        """네이버 블로그 에디터에 이미지를 삽입.
        v27: 본문 전용 file input 우선 + 툴바 이미지 버튼 먼저 활성화 + iframe 진입."""
        import subprocess as sp

        if not os.path.exists(image_path):
            raise FileNotFoundError(f"이미지 파일 없음: {image_path}")

        abs_path = os.path.abspath(image_path)

        # iframe 진입 시도 (mainFrame 안에 에디터가 있음)
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame('mainFrame')
        except Exception:
            pass

        # ── 방법 1: 본문 이미지 툴바 버튼을 먼저 눌러 file input 활성화 ──
        try:
            for btn_sel in [
                'button.se-image-toolbar-button',
                'button[data-name="image"]',
                'button[data-type="image"]',
                'button[aria-label*="사진"]',
                'button[aria-label*="이미지"]',
                'button.se-toolbar-button-image',
            ]:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(0.6)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # ── 방법 2: 본문 전용 file input 우선 (썸네일용은 보통 첫 번째라 제외) ──
        try:
            # 본문용 셀렉터 우선
            preferred = []
            for sel in [
                'input.se-image-input-file',
                'input[class*="image"][type="file"]',
                'input[accept*="image"][type="file"]',
            ]:
                preferred.extend(driver.find_elements(By.CSS_SELECTOR, sel))
            # fallback: 모든 file input (썸네일 제외 위해 뒤쪽부터)
            all_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
            candidates = preferred + list(reversed(all_inputs))
            seen = set()
            for fi in candidates:
                key = fi.get_attribute('outerHTML')[:200]
                if key in seen:
                    continue
                seen.add(key)
                try:
                    # display:none 인 input도 send_keys 가능
                    driver.execute_script("arguments[0].style.display='block'; arguments[0].style.visibility='visible';", fi)
                    fi.send_keys(abs_path)
                    time.sleep(4)  # 업로드 + 처리 대기
                    self.log_emit(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                    # iframe 복귀
                    try:
                        driver.switch_to.default_content()
                        driver.switch_to.frame('mainFrame')
                    except Exception:
                        pass
                    return
                except Exception:
                    continue
        except Exception:
            pass

        # 방법 2: 이미지 버튼 클릭 → file input에 전달
        try:
            for btn_sel in [
                'button[data-name="image"]',
                'button.se-image-toolbar-button',
                'button[data-type="image"]',
                'button.se-toolbar-button-image',
            ]:
                try:
                    img_btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    driver.execute_script("arguments[0].click();", img_btn)
                    time.sleep(1)
                    # file input 찾기
                    file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                    for fi in file_inputs:
                        try:
                            fi.send_keys(os.path.abspath(image_path))
                            time.sleep(3)
                            self.log_emit(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                            return
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

        # 방법 3: PowerShell 클립보드 이미지 복사 → Ctrl+V
        try:
            abs_path = os.path.abspath(image_path).replace('\\', '/')
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile('{abs_path}')
[System.Windows.Forms.Clipboard]::SetImage($img)
$img.Dispose()
"""
            sp.run(['powershell', '-Command', ps_script], capture_output=True, timeout=10)
            time.sleep(0.5)
            safe_hotkey(driver, 'ctrl', 'v')
            time.sleep(3)
            self.log_emit(f"  🖼️ 이미지 붙여넣기 완료: {os.path.basename(image_path)}")
        except Exception as e:
            raise RuntimeError(f"이미지 삽입 모든 방법 실패: {e}")


class CafeCollectPage(QWidget):
    def __init__(self, main_win):
        super().__init__()
        # === v28: 발행 기능을 위한 초기화 (인기글과 동일) ===
        self._publishing_flag = False
        self._image_pool = []
        self.publish_queue = []
        self._selected_alignment = "left"
        if not hasattr(self, "generated_articles"):
            self.generated_articles = []
        self._folder_cache = {}
        self.main = main_win
        self.cafe_id = None
        self.generated_articles = []
        self._generating = False
        self._cancel_generate = False
        self.menu_list = []  # [(menuId, menuName), ...]
        layout = QVBoxLayout(self)

        title = QLabel("☕ 카페 수집 → AI 글쓰기")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        layout.addWidget(title)

        cg = QGroupBox("1단계: 카페 수집")
        cl = QVBoxLayout(cg)

        # ── URL + 카테고리 불러오기 ──
        url_row = QHBoxLayout()
        self.cafe_url = QLineEdit()
        self.cafe_url.setPlaceholderText("https://cafe.naver.com/f-e/cafes/XXXXX/menus/YY?viewType=L&page=1")
        url_row.addWidget(self.cafe_url)
        self.fetch_cat_btn = QPushButton("📁 카테고리 불러오기")
        self.fetch_cat_btn.setStyleSheet("background:#2196F3; color:white; padding:6px 12px;")
        self.fetch_cat_btn.clicked.connect(self.fetch_categories)
        url_row.addWidget(self.fetch_cat_btn)
        cl.addLayout(url_row)

        # ── 카테고리 선택 ──
        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("카테고리:"))
        self.category_combo = QComboBox()
        self.category_combo.setMinimumWidth(400)
        cat_row.addWidget(self.category_combo, 1)
        cl.addLayout(cat_row)

        # ── 페이지 범위 ──
        page_row = QHBoxLayout()
        page_row.addWidget(QLabel("페이지 범위:"))
        self.page_start = QSpinBox()
        self.page_start.setMinimum(1)
        self.page_start.setMaximum(999)
        self.page_start.setValue(1)
        page_row.addWidget(self.page_start)
        page_row.addWidget(QLabel("~"))
        self.page_end = QSpinBox()
        self.page_end.setMinimum(1)
        self.page_end.setMaximum(999)
        self.page_end.setValue(3)
        page_row.addWidget(self.page_end)
        page_row.addWidget(QLabel("페이지"))
        page_row.addStretch()
        cl.addLayout(page_row)

        # ── 전체 수집 시작 ──
        self.collect_btn = QPushButton("🔄 전체 수집 시작")
        self.collect_btn.setStyleSheet("background:#4CAF50; color:white; font-size:14px; padding:10px;")
        self.collect_btn.clicked.connect(self.start_collect)
        cl.addWidget(self.collect_btn)

        self.collect_result = QTextBrowser()
        self.collect_result.setMaximumHeight(120)
        cl.addWidget(self.collect_result)

        # ── 수집 결과 표 (체크박스 + 선택 발행) ──
        cl.addWidget(QLabel("📋 수집된 카페 글 (체크한 항목만 AI 입력으로 전달):"))
        self.collect_cafe_table = QTableWidget()
        self.collect_cafe_table.setColumnCount(4)
        self.collect_cafe_table.setHorizontalHeaderLabels(["선택", "카페", "제목", "본문 요약"])
        self.collect_cafe_table.setColumnWidth(0, 50)
        self.collect_cafe_table.setColumnWidth(1, 120)
        self.collect_cafe_table.setColumnWidth(2, 320)
        self.collect_cafe_table.horizontalHeader().setStretchLastSection(True)
        self.collect_cafe_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.collect_cafe_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.collect_cafe_table.setMinimumHeight(180)
        self.collect_cafe_table.itemSelectionChanged.connect(self._on_cafe_selected)
        cl.addWidget(self.collect_cafe_table)

        h_cafe_sel = QHBoxLayout()
        btn_cafe_all = QPushButton("☑️ 전체 선택")
        btn_cafe_all.clicked.connect(lambda: self._toggle_all_cafe(True))
        h_cafe_sel.addWidget(btn_cafe_all)
        btn_cafe_none = QPushButton("⬜ 전체 해제")
        btn_cafe_none.clicked.connect(lambda: self._toggle_all_cafe(False))
        h_cafe_sel.addWidget(btn_cafe_none)
        h_cafe_sel.addStretch()
        cl.addLayout(h_cafe_sel)

        layout.addWidget(cg)

        # ── 2단계: AI 글쓰기 ──
        ag = QGroupBox("2단계: AI 글쓰기")
        al = QVBoxLayout(ag)

        h = QHBoxLayout()
        h.addWidget(QLabel("AI 서비스:"))
        self.ai_model = QComboBox()
        self.ai_model.addItems(["Perplexity (sonar-pro)", "Gemini (gemini-2.5-flash)", "GPT (gpt-4o)"])
        h.addWidget(self.ai_model, 2)
        h.addWidget(QLabel("글자 수:"))
        self.char_count = QSpinBox()
        self.char_count.setRange(500, 10000)
        self.char_count.setValue(2000)
        self.char_count.setSingleStep(500)
        h.addWidget(self.char_count)
        al.addLayout(h)

        self.ai_prompt = QTextEdit()
        self.ai_prompt.setPlaceholderText("AI에게 전달할 프롬프트...")
        self.ai_prompt.setPlainText(
            "아래 카페 글을 참고하여 SEO 최적화된 네이버 블로그 포스트를 작성해주세요.\n"
            "- 자연스러운 말투 (1인칭 경험담/의견)\n"
            "- 소제목 3개 이상 (##)\n"
            "- 핵심 키워드 자연스럽게 5회 이상 배치\n"
            "- [사진] 태그로 사진 삽입 위치 표시\n"
            "- 마지막에 해시태그 5개 추가"
        )
        self.ai_prompt.setMaximumHeight(100)
        al.addWidget(QLabel("프롬프트:"))
        al.addWidget(self.ai_prompt)

        h2 = QHBoxLayout()
        send = QPushButton("⬇️ 수집→AI (체크된 것만)")
        send.clicked.connect(self.send_cafe_to_ai)
        h2.addWidget(send)
        self.gen_btn = QPushButton("🎯 AI 원고 생성")
        self.gen_btn.clicked.connect(self.generate_ai)
        self.gen_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
        h2.addWidget(self.gen_btn)
        al.addLayout(h2)

        self.ai_progress = QProgressBar()
        self.ai_progress.setVisible(False)
        al.addWidget(self.ai_progress)

        self.ai_input = QTextEdit()
        self.ai_input.setPlaceholderText("수집된 카페 글이 AI 입력으로 전달됩니다...")
        self.ai_input.setMaximumHeight(100)
        al.addWidget(QLabel("AI 입력 (수집 데이터):"))
        al.addWidget(self.ai_input)

        al.addWidget(QLabel("📋 생성된 원고 리스트 (체크한 항목만 발행):"))
        self.article_table = QTableWidget()
        self.article_table.setColumnCount(5)
        self.article_table.setHorizontalHeaderLabels(["선택", "번호", "상태", "제목", "글자수"])
        self.article_table.horizontalHeader().setStretchLastSection(True)
        self.article_table.setColumnWidth(0, 50)
        self.article_table.setColumnWidth(1, 50)
        self.article_table.setColumnWidth(2, 70)
        self.article_table.setColumnWidth(3, 350)
        self.article_table.setColumnWidth(4, 70)
        self.article_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.article_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.article_table.itemSelectionChanged.connect(self._on_article_selected)
        self.article_table.setMinimumHeight(150)
        al.addWidget(self.article_table)

        h_cafe_art_sel = QHBoxLayout()
        btn_cafe_art_all = QPushButton("☑️ 전체 선택")
        btn_cafe_art_all.clicked.connect(lambda: self._toggle_all_articles(True))
        h_cafe_art_sel.addWidget(btn_cafe_art_all)
        btn_cafe_art_none = QPushButton("⬜ 전체 해제")
        btn_cafe_art_none.clicked.connect(lambda: self._toggle_all_articles(False))
        h_cafe_art_sel.addWidget(btn_cafe_art_none)
        h_cafe_art_sel.addStretch()
        al.addLayout(h_cafe_art_sel)

        self.ai_output = QTextBrowser()
        self.ai_output.setPlaceholderText("원고를 선택하면 여기에 표시됩니다...")
        al.addWidget(QLabel("📄 선택된 원고 미리보기:"))
        al.addWidget(self.ai_output)

        self.error_summary_label = QLabel("⚠️ 주요 오류:")
        self.error_summary_label.setVisible(False)
        al.addWidget(self.error_summary_label)
        self.error_summary = QTextBrowser()
        self.error_summary.setMaximumHeight(60)
        self.error_summary.setVisible(False)
        al.addWidget(self.error_summary)

        h3 = QHBoxLayout()
        copy_btn = QPushButton("📋 선택 원고 복사")
        copy_btn.clicked.connect(self._copy_selected_article)
        h3.addWidget(copy_btn)
        copy_all_btn = QPushButton("📋 전체 원고 복사")
        copy_all_btn.clicked.connect(self._copy_all_articles)
        h3.addWidget(copy_all_btn)
        save_btn = QPushButton("💾 전체 원고 저장")
        save_btn.clicked.connect(self._save_draft)
        h3.addWidget(save_btn)
        export_btn = QPushButton("📊 엑셀로 내보내기")
        export_btn.clicked.connect(self._export_articles_excel)
        h3.addWidget(export_btn)
        import_btn = QPushButton("📥 엑셀 원고 불러오기")
        import_btn.clicked.connect(self._import_articles_excel)
        h3.addWidget(import_btn)
        al.addLayout(h3)

        layout.addWidget(ag)


        # ═══════════════════════════════════════════
        # v28: 이미지 관리 + 블로그 발행 (인기글과 동일)
        # ═══════════════════════════════════════════
        # ══════════════════════════════════════
        # STEP 2.5: 이미지 관리
        # ══════════════════════════════════════
        step_img = QGroupBox("🖼️ STEP 2.5  이미지 관리 (본문 [사진] 위치에 삽입)")
        si = QVBoxLayout(step_img)

        si.addWidget(QLabel("원고 본문에 [사진] 태그가 있으면 발행 시 해당 위치에 이미지가 삽입됩니다."))

        # 이미지 리스트 테이블
        self.image_table = QTableWidget()
        self.image_table.setColumnCount(4)
        self.image_table.setHorizontalHeaderLabels(["번호", "소스", "설명/프롬프트", "파일경로"])
        self.image_table.horizontalHeader().setStretchLastSection(True)
        self.image_table.setColumnWidth(0, 40)
        self.image_table.setColumnWidth(1, 70)
        self.image_table.setColumnWidth(2, 250)
        self.image_table.setMinimumHeight(120)
        self.image_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        si.addWidget(self.image_table)
        self._image_pool = []  # [{'source':'upload'|'dalle', 'desc':'...', 'path':'...'}, ...]

        h_img_btns = QHBoxLayout()

        self.img_upload_btn = QPushButton("📁 이미지 직접 업로드")
        self.img_upload_btn.clicked.connect(self._upload_images)
        self.img_upload_btn.setStyleSheet("background-color:#2d6a4f; color:white; font-weight:bold; padding:6px;")
        h_img_btns.addWidget(self.img_upload_btn)

        self.img_dalle_btn = QPushButton("🤖 AI 이미지 생성 (ChatGPT)")
        self.img_dalle_btn.clicked.connect(self._generate_dalle_images)
        self.img_dalle_btn.setStyleSheet("background-color:#4a1a8a; color:white; font-weight:bold; padding:6px;")
        h_img_btns.addWidget(self.img_dalle_btn)

        self.img_del_btn = QPushButton("🗑️ 선택 삭제")
        self.img_del_btn.clicked.connect(self._delete_selected_image)
        h_img_btns.addWidget(self.img_del_btn)

        self.img_clear_btn = QPushButton("🗑️ 전체 삭제")
        self.img_clear_btn.clicked.connect(self._clear_all_images)
        h_img_btns.addWidget(self.img_clear_btn)
        si.addLayout(h_img_btns)

        # AI 이미지 생성 옵션
        h_dalle_opt = QHBoxLayout()
        h_dalle_opt.addWidget(QLabel("DALL-E 프롬프트:"))
        self.dalle_prompt = QLineEdit()
        self.dalle_prompt.setPlaceholderText("이미지 설명을 입력하세요 (예: 서울 야경, 커피 한 잔)")
        h_dalle_opt.addWidget(self.dalle_prompt, 4)

        h_dalle_opt.addWidget(QLabel("크기:"))
        self.dalle_size = QComboBox()
        self.dalle_size.addItems(["1024x1024", "1024x1536", "1536x1024"])
        h_dalle_opt.addWidget(self.dalle_size)

        h_dalle_opt.addWidget(QLabel("장수:"))
        self.dalle_count = QSpinBox()
        self.dalle_count.setRange(1, 10)
        self.dalle_count.setValue(1)
        h_dalle_opt.addWidget(self.dalle_count)
        si.addLayout(h_dalle_opt)

        # 자동 생성 옵션
        h_auto = QHBoxLayout()
        self.auto_dalle_check = QCheckBox("원고별 자동 이미지 생성 (원고 제목 기반)")
        self.auto_dalle_check.setToolTip("원고 생성 시 각 원고의 제목을 프롬프트로 DALL-E 이미지를 자동 생성합니다")
        h_auto.addWidget(self.auto_dalle_check)

        self.auto_dalle_count = QSpinBox()
        self.auto_dalle_count.setRange(1, 5)
        self.auto_dalle_count.setValue(1)
        h_auto.addWidget(QLabel("원고당 이미지:"))
        h_auto.addWidget(self.auto_dalle_count)
        h_auto.addStretch()
        si.addLayout(h_auto)

        self.img_status = QLabel("")
        self.img_status.setStyleSheet("color:#888; font-size:11px;")
        si.addWidget(self.img_status)

        layout.addWidget(step_img)

        # ══════════════════════════════════════
        # STEP 3: 네이버 블로그 자동 발행
        # ══════════════════════════════════════
        step3 = QGroupBox("STEP 3️⃣  네이버 블로그 자동 발행")
        s3 = QVBoxLayout(step3)

        h3_acc = QHBoxLayout()
        h3_acc.addWidget(QLabel("발행 계정:"))
        self.account_combo = QComboBox()
        accounts = self.main.config.get('naver_accounts', [])
        if not accounts and self.main.config.get('naver_id'):
            accounts = [{'id': self.main.config.get('naver_id', '')}]
        for acc in accounts:
            self.account_combo.addItem(acc.get('id', ''))
        h3_acc.addWidget(self.account_combo, 2)
        s3.addLayout(h3_acc)

        h3a = QHBoxLayout()
        h3a.addWidget(QLabel("블로그 폴더:"))
        self.blog_folder = QComboBox()
        self.blog_folder.setEditable(True)
        h3a.addWidget(self.blog_folder, 2)

        self.load_folders_btn = QPushButton("🔄 폴더 불러오기")
        self.load_folders_btn.clicked.connect(self.load_blog_folders)
        h3a.addWidget(self.load_folders_btn)
        s3.addLayout(h3a)

        h3b = QHBoxLayout()
        h3b.addWidget(QLabel("저장 형태:"))
        self.save_mode = QComboBox()
        self.save_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        # v24: 본문 정렬 선택
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["⬅️ 좌측 정렬", "⬆️ 가운데 정렬", "➡️ 우측 정렬", "↔️ 양쪽 정렬"])
        self.alignment_combo.currentIndexChanged.connect(
            lambda i: setattr(self, '_selected_alignment', ['left','center','right','justify'][i])
        )
        self._selected_alignment = 'left'
        h3b.addWidget(self.save_mode)
        h3b.addWidget(QLabel("정렬:"))
        h3b.addWidget(self.alignment_combo)
        s3.addLayout(h3b)

        self.publish_btn = QPushButton("🚀 블로그 발행")
        self.publish_btn.clicked.connect(self.publish_to_blog)
        self.publish_btn.setStyleSheet("background-color:#8B0000; color:white; font-weight:bold; padding:8px;")
        s3.addWidget(self.publish_btn)

        self.pub_progress = QProgressBar()
        self.pub_progress.setVisible(False)
        s3.addWidget(self.pub_progress)

        layout.addWidget(step3)

    def _parse_cafe_id_from_url(self, url):
        """URL에서 cafeId 추출"""
        import re
        # 패턴1: /cafes/12345/
        m = re.search(r'/cafes/(\d+)', url)
        if m:
            return int(m.group(1))
        # 패턴2: clubid=12345
        m = re.search(r'clubid=(\d+)', url)
        if m:
            return int(m.group(1))
        # 패턴3: cafe.naver.com/카페이름 → API로 cafeId 조회
        m = re.search(r'cafe\.naver\.com/([a-zA-Z0-9_]+)', url)
        if m:
            cafe_name = m.group(1)
            if cafe_name not in ['f-e', 'ca-fe']:
                try:
                    resp = requests.get(
                        f'https://apis.naver.com/cafe-web/cafe2/CafeGateInfo.json?cafeUrl={cafe_name}',
                        headers={'Referer': 'https://cafe.naver.com/'},
                        timeout=10
                    )
                    data = resp.json()
                    return data['message']['result']['cafeInfoView']['cafeId']
                except Exception:
                    pass
        return None

    def _parse_menu_id_from_url(self, url):
        """URL에서 menuId 추출"""
        import re
        m = re.search(r'/menus/(\d+)', url)
        if m:
            return int(m.group(1))
        m = re.search(r'menuid=(\d+)', url, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def fetch_categories(self):
        """네이버 카페 API로 카테고리(게시판) 목록 불러오기"""
        url = self.cafe_url.text().strip()
        if not url:
            self.main.log("⚠️ URL을 먼저 입력하세요.")
            return

        cafe_id = self._parse_cafe_id_from_url(url)
        if not cafe_id:
            self.main.log("❌ URL에서 카페 ID를 추출할 수 없습니다.")
            return

        self.cafe_id = cafe_id
        self.fetch_cat_btn.setEnabled(False)
        self.fetch_cat_btn.setText("불러오는 중...")

        def do():
            api_url = f'https://apis.naver.com/cafe-web/cafe2/SideMenuList?cafeId={cafe_id}'
            headers = {
                'Referer': 'https://cafe.naver.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            }
            resp = requests.get(api_url, headers=headers, timeout=15)
            data = resp.json()
            menus = data.get('message', {}).get('result', {}).get('menus', [])

            result = []
            for m in menus:
                menu_type = m.get('menuType', '')
                menu_id = m.get('menuId', 0)
                menu_name = m.get('menuName', '').strip()
                board_type = m.get('boardType', '').strip()

                # B=일반게시판, I=이미지, M=동영상, S=스크랩 등 글이 있는 게시판만
                if menu_type in ('B', 'I', 'M', 'S', 'E') and menu_name:
                    result.append((menu_id, menu_name))
                # F=폴더(구분선) - 참고용으로 표시
                elif menu_type == 'F' and menu_name:
                    result.append((0, f'── {menu_name} ──'))

            return result

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)

        def on_result(menus):
            self.menu_list = menus
            self.category_combo.clear()
            board_count = 0
            for mid, mname in menus:
                if mid == 0:
                    self.category_combo.addItem(mname, 0)
                    # 폴더 항목은 선택 불가
                    idx = self.category_combo.count() - 1
                    model = self.category_combo.model()
                    item = model.item(idx)
                    item.setEnabled(False)
                else:
                    self.category_combo.addItem(f'{mname} ({mid})', mid)
                    board_count += 1

            # URL에서 menuId가 있으면 자동 선택
            url_menu_id = self._parse_menu_id_from_url(self.cafe_url.text())
            if url_menu_id:
                for i in range(self.category_combo.count()):
                    if self.category_combo.itemData(i) == url_menu_id:
                        self.category_combo.setCurrentIndex(i)
                        break

            self.main.log(f"✅ {board_count}개 게시판 카테고리를 불러왔습니다. (cafeId={self.cafe_id})")

        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.fetch_cat_btn.setEnabled(True),
            self.fetch_cat_btn.setText("📁 카테고리 불러오기")
        ))
        thread.start()

    def start_collect(self):
        """선택한 카테고리의 게시글 목록 수집 (API 기반)"""
        if not self.cafe_id:
            # URL에서 다시 파싱 시도
            self.cafe_id = self._parse_cafe_id_from_url(self.cafe_url.text())

        if not self.cafe_id:
            self.main.log("⚠️ 먼저 카테고리를 불러오세요.")
            return

        menu_id = self.category_combo.currentData()
        if not menu_id:
            self.main.log("⚠️ 게시판을 선택하세요.")
            return

        menu_name = self.category_combo.currentText()
        page_start = self.page_start.value()
        page_end = self.page_end.value()

        self.collect_btn.setEnabled(False)
        self.collect_btn.setText("수집 중...")
        self.collect_result.clear()

        cafe_id = self.cafe_id
        self.main.log(f"📦 [{menu_name}] {page_start}~{page_end}페이지 수집 시작")

        def do():
            headers = {
                'Referer': 'https://cafe.naver.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            }
            all_articles = []

            for page in range(page_start, page_end + 1):
                self.log_emit(f"📄 {page}/{page_end} 페이지 수집 중...")
                api_url = (
                    f'https://apis.naver.com/cafe-web/cafe2/ArticleListV2.json'
                    f'?search.clubid={cafe_id}'
                    f'&search.menuid={menu_id}'
                    f'&search.page={page}'
                    f'&search.perPage=15'
                )
                try:
                    resp = requests.get(api_url, headers=headers, timeout=15)
                    data = resp.json()
                    articles = data.get('message', {}).get('result', {}).get('articleList', [])
                    self.log_emit(f"  → {len(articles)}개 게시글 발견")

                    for art in articles:
                        article_id = art.get('articleId', '')
                        subject = art.get('subject', '').strip()
                        writer = art.get('writerNickname', '')
                        read_count = art.get('readCount', 0)
                        comment_count = art.get('commentCount', 0)
                        like_count = art.get('likeItCount', 0)
                        link = f"https://cafe.naver.com/f-e/cafes/{cafe_id}/articles/{article_id}"

                        all_articles.append({
                            'title': subject,
                            'writer': writer,
                            'views': read_count,
                            'comments': comment_count,
                            'likes': like_count,
                            'link': link,
                            'articleId': article_id
                        })
                except Exception as e:
                    self.log_emit(f"  ❌ {page}페이지 오류: {e}")
                time.sleep(0.5)

            # 결과 포맷팅
            lines = [f"총 {len(all_articles)}개 게시글 수집 완료"]
            lines.append("=" * 50)
            for i, art in enumerate(all_articles, 1):
                lines.append(f"\n[{i}] {art['title']}")
                lines.append(f"    작성자: {art['writer']} | 조회: {art['views']} | 댓글: {art['comments']} | 좋아요: {art['likes']}")
                lines.append(f"    {art['link']}")

            return '\n'.join(lines)

        def log_emit_fn(msg):
            self.main.log(msg)

        self.log_emit = log_emit_fn

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.collect_result.setPlainText(r))
        thread.finished_signal.connect(lambda: (
            self.collect_btn.setEnabled(True),
            self.collect_btn.setText("🔄 전체 수집 시작")
        ))
        thread.start()

    def _on_article_selected(self):
        rows = self.article_table.selectionModel().selectedRows()
        if not rows: return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            self.ai_output.setPlainText(self.generated_articles[idx].get('content', ''))

    def _copy_selected_article(self):
        rows = self.article_table.selectionModel().selectedRows()
        if not rows:
            self.main.log("⚠️ 원고를 먼저 선택하세요")
            return
        idx = rows[0].row()
        if 0 <= idx < len(self.generated_articles):
            pyperclip.copy(self.generated_articles[idx].get('content', ''))
            self.main.log(f"📋 원고 #{idx+1} 복사 완료")

    def _copy_all_articles(self):
        if not self.generated_articles:
            self.main.log("⚠️ 생성된 원고가 없습니다")
            return
        all_text = ""
        for i, art in enumerate(self.generated_articles):
            all_text += f"\n{'='*60}\n[원고 {i+1}] {art.get('title','')}\n{'='*60}\n"
            all_text += art.get('content', '') + "\n"
        pyperclip.copy(all_text)
        self.main.log(f"📋 전체 {len(self.generated_articles)}건 원고 복사 완료")

    def _update_article_table(self):
        self.article_table.setRowCount(len(self.generated_articles))
        for i, art in enumerate(self.generated_articles):
            status = art.get('status', '완료')
            self._set_article_check(i, checked=(status == '완료'))
            self.article_table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 2, QTableWidgetItem(status))
            self.article_table.setItem(i, 3, QTableWidgetItem(art.get('title', '')[:80]))
            content = art.get('content', '')
            self.article_table.setItem(i, 4, QTableWidgetItem(str(len(content)) if status == "완료" else "실패"))

    def _save_draft(self):
        if not self.generated_articles:
            self.main.log("⚠️ 저장할 원고가 없습니다")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "원고 저장", f"카페_원고_{ts}.txt", "Text Files (*.txt)")
        if not path: return
        with open(path, 'w', encoding='utf-8') as f:
            for i, art in enumerate(self.generated_articles):
                f.write(f"\n{'='*60}\n[원고 {i+1}] {art.get('title','')}\n{'='*60}\n")
                f.write(art.get('content', '') + "\n\n")
        self.main.log(f"💾 {len(self.generated_articles)}건 원고 저장 완료: {path}")

    def _export_articles_excel(self):
        if not self.generated_articles:
            self.main.log("⚠️ 내보낼 원고가 없습니다")
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            path, _ = QFileDialog.getSaveFileName(self, "엑셀 내보내기", "카페_원고_목록.xlsx", "Excel Files (*.xlsx)")
            if not path: return
            wb = Workbook()
            ws = wb.active
            ws.title = "원고 목록"
            headers = ["번호", "제목", "글자수", "상태", "원문 참고", "생성된 원고"]
            header_fill = PatternFill('solid', fgColor='1a4a8a')
            header_font = Font(bold=True, color='FFFFFF')
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=h)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')
            for i, art in enumerate(self.generated_articles):
                ws.cell(row=i+2, column=1, value=i+1)
                ws.cell(row=i+2, column=2, value=art.get('title', ''))
                ws.cell(row=i+2, column=3, value=len(art.get('content', '')))
                ws.cell(row=i+2, column=4, value=art.get('status', '완료'))
                ws.cell(row=i+2, column=5, value=art.get('source', '')[:500])
                ws.cell(row=i+2, column=6, value=art.get('content', ''))
            wb.save(path)
            self.main.log(f"📊 엑셀 내보내기 완료: {path}")
        except Exception as e:
            self.main.log(f"❌ 엑셀 내보내기 오류: {e}")

    def _import_articles_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 원고 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if not path: return
        try:
            import pandas as pd
            df = pd.read_excel(path)
            imported = []
            for _, row in df.iterrows():
                title = str(row.get('제목', row.get('title', '제목 없음')))
                content = str(row.get('생성된 원고', row.get('content', row.get('생성된원고', ''))))
                if content and content != 'nan':
                    imported.append({'title': title if title != 'nan' else '제목 없음', 'content': content, 'status': '완료'})
            if imported:
                self.generated_articles.extend(imported)
                self._update_article_table()
                self.main.log(f"📥 엑셀에서 {len(imported)}건 원고 불러옴")
        except Exception as e:
            self.main.log(f"❌ 엑셀 불러오기 오류: {e}")

    def _parse_cafe_posts(self, text):
        posts = []
        blocks = re.split(r'\n={3,}\n|\n-{3,}\n|\[\d+\]\s*제목:', text)
        for block in blocks:
            block = block.strip()
            if not block or len(block) < 30: continue
            blines = block.split('\n')
            title = blines[0].strip()[:100] if blines else '카페 포스트'
            body = '\n'.join(blines[1:]).strip()
            if not body: body = block
            posts.append({'title': title, 'body': body[:3000]})
        return posts if posts else [{'title': '카페 포스트', 'body': text[:3000]}]

    def generate_ai(self):
        if getattr(self, '_generating', False):
            self._cancel_generate = True
            self.gen_btn.setText("🛑 취소 중...")
            self.main.log("🛑 AI 생성 취소 요청됨...")
            return
        content = self.ai_input.toPlainText().strip()
        if not content:
            self.main.log("⚠️ AI 입력이 비어 있습니다")
            return
        cafe_posts = self._parse_cafe_posts(content)
        if not cafe_posts:
            self.main.log("⚠️ 파싱할 카페 글이 없습니다")
            return

        total = len(cafe_posts)
        self._cancel_generate = False
        self._generating = True
        self.generated_articles = []
        self.gen_btn.setText(f"🛑 생성 취소 (0/{total})")
        self.gen_btn.setStyleSheet("background-color:#8a1a1a; color:white; font-weight:bold; padding:8px;")
        self.ai_progress.setVisible(True)
        self.ai_progress.setValue(0)
        self.error_summary.setVisible(False)
        self.error_summary_label.setVisible(False)

        self.article_table.setRowCount(total)
        for i, post in enumerate(cafe_posts):
            self._set_article_check(i, checked=False)
            self.article_table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.article_table.setItem(i, 2, QTableWidgetItem("대기"))
            self.article_table.setItem(i, 3, QTableWidgetItem(post['title'][:80]))
            self.article_table.setItem(i, 4, QTableWidgetItem("-"))

        model_text = self.ai_model.currentText()
        prompt = self.ai_prompt.toPlainText()
        char_target = self.char_count.value()
        self.main.log(f"🧠 AI 생성 시작 - 총 {total}건 개별 생성")

        thread = WorkerThread(lambda: None)
        def _ui_update(fn): thread.ui_update_signal.emit(fn)

        def do():
            generated = []
            errors_collection = []
            for idx, post in enumerate(cafe_posts):
                if self._cancel_generate:
                    thread.log_signal.emit(f"🛑 AI 생성 취소됨 ({len(generated)}건 처리됨)")
                    break
                thread.log_signal.emit(f"📝 [{idx+1}/{total}] AI 원고 생성 중: {post['title'][:40]}...")
                _ui_update(lambda i=idx, t=total: self.gen_btn.setText(f"🛑 생성 취소 ({i+1}/{t})"))
                _ui_update(lambda i=idx: self.article_table.setItem(i, 2, QTableWidgetItem("생성중")))
                _ui_update(lambda i=idx, t=total: self.ai_progress.setValue(int((i / t) * 100)))

                if len(post.get('body', '').strip()) < 20:
                    err_msg = '본문이 비어있어 건너뜁니다'
                    generated.append({'title': post['title'], 'content': f'❌ {err_msg}', 'status': '실패', 'error': err_msg})
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: self.article_table.setItem(i, 2, QTableWidgetItem("❌실패")))
                    continue

                full = f"{prompt}\n\n[목표 글자수: {char_target}자]\n- 제목을 첫 줄에 ## 제목 형태로 작성해주세요\n"
                full += f"\n[참고 자료 제목]: {post['title']}\n[참고 자료 본문]:\n{post['body'][:4500]}"

                try:
                    if "Perplexity" in model_text or "sonar" in model_text.lower():
                        result = call_perplexity(self.main.config.get('perplex_key', '').strip(), full, model='sonar-pro', system_message='당신은 한국어 SEO 블로그 전문 작가입니다.')
                    elif "Gemini" in model_text or "gemini" in model_text.lower():
                        result = call_gemini_text(self.main.config.get('gemini_key', '').strip(), full, model='gemini-2.5-flash')
                    else:
                        result = call_openai_text(self.main.config.get('openai_key', '').strip(), full, model='gpt-4o')

                    gen_title = post['title']
                    for line in result.split('\n'):
                        s = line.strip()
                        if s.startswith('##'):
                            gen_title = s.lstrip('#').strip()
                            break

                    generated.append({'title': gen_title, 'content': result, 'status': '완료', 'source': post.get('body', '')[:500]})
                    # 즉시 발행 대기열 형태로 저장
                    self.generated_articles = list(generated)
                    try:
                        _queue = [{'order': qi+1, 'title': ga['title'], 'content': ga['content'], 'status': 'pending', 'result': ''} for qi, ga in enumerate(generated) if ga.get('status') == '완료']
                        if _queue:
                            with open('publish_queue_cafe_incremental.json', 'w', encoding='utf-8') as _qf:
                                json.dump(_queue, _qf, ensure_ascii=False, indent=2)
                            thread.log_signal.emit(f"💾 [{idx+1}/{total}] 발행 대기열 저장 완료 ({len(_queue)}건)")
                    except Exception: pass
                    thread.log_signal.emit(f"✅ [{idx+1}/{total}] 완료: {gen_title[:40]}")
                    _ui_update(lambda i=idx, t=gen_title, c=len(result): (
                        self._set_article_check(i, checked=True),
                        self.article_table.setItem(i, 2, QTableWidgetItem("✅완료")),
                        self.article_table.setItem(i, 3, QTableWidgetItem(t[:80])),
                        self.article_table.setItem(i, 4, QTableWidgetItem(str(c)))
                    ))
                except Exception as e:
                    err_msg = format_error_message(e)
                    thread.log_signal.emit(f"❌ [{idx+1}/{total}] 실패: {err_msg}")
                    generated.append({'title': post['title'], 'content': f'❌ 오류: {err_msg}', 'status': '실패', 'error': err_msg})
                    errors_collection.append(err_msg)
                    _ui_update(lambda i=idx: self.article_table.setItem(i, 2, QTableWidgetItem("❌실패")))

                if idx < total - 1 and not self._cancel_generate:
                    wait_seconds = 4 if "perplexity" in model_text.lower() or "sonar" in model_text.lower() else 2
                    for _ in range(wait_seconds * 2):
                        if self._cancel_generate: break
                        time.sleep(0.5)

            self.generated_articles = generated
            _ui_update(lambda: self.ai_progress.setValue(100))
            success = sum(1 for a in generated if a.get('status') == '완료')
            failed = sum(1 for a in generated if a.get('status') == '실패')
            if errors_collection:
                unique_errors = list(dict.fromkeys(errors_collection))
                _ui_update(lambda: (self.error_summary.setPlainText("- " + "\n- ".join(unique_errors[:5])), self.error_summary.setVisible(True), self.error_summary_label.setVisible(True)))
            thread.log_signal.emit(f"✅ AI 원고 생성 완료 - 총 {total}건 중 {success}건 성공, {failed}건 실패")

        thread.func = do
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: None)
        def _on_gen_finished():
            self._generating = False
            self._cancel_generate = False
            self.gen_btn.setText("🎯 AI 원고 생성")
            self.gen_btn.setStyleSheet("background-color:#1a4a8a; color:white; font-weight:bold; padding:8px;")
            self.ai_progress.setVisible(False)
        thread.finished_signal.connect(_on_gen_finished)
        thread.start()
# ═══════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════
    # ─── 체크박스/선택 헬퍼 (v3 패치) ───
    def _toggle_all_cafe(self, state: bool):
        cs = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for r in range(self.collect_cafe_table.rowCount()):
            it = self.collect_cafe_table.item(r, 0)
            if it is not None:
                it.setCheckState(cs)
        self.main.log(f"📋 수집 카페글 {'전체 선택' if state else '전체 해제'} ({self.collect_cafe_table.rowCount()}건)")

    def _get_checked_cafe_indices(self):
        out = []
        for r in range(self.collect_cafe_table.rowCount()):
            it = self.collect_cafe_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(r)
        return out

    def _on_cafe_selected(self):
        """수집 카페글 행 클릭 시 본문을 AI 입력창에 미리보기"""
        rows = self.collect_cafe_table.selectionModel().selectedRows()
        if not rows:
            return
        items = list(getattr(self, '_cafe_collected_items', []) or [])
        idx = rows[0].row()
        if 0 <= idx < len(items):
            it = items[idx]
            preview = (
                f"[미리보기] 제목: {it.get('title','')}\n"
                f"카페: {it.get('cafe', it.get('cafe_name',''))}\n"
                f"링크: {it.get('url','')}\n\n"
                f"본문:\n{it.get('body', it.get('content',''))}"
            )
            self.ai_input.setPlainText(preview)

    def _toggle_all_articles(self, state: bool):
        cs = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for r in range(self.article_table.rowCount()):
            it = self.article_table.item(r, 0)
            if it is not None:
                it.setCheckState(cs)
        self.main.log(f"📋 카페 원고 {'전체 선택' if state else '전체 해제'} ({self.article_table.rowCount()}건)")

    def _get_checked_article_indices(self):
        out = []
        for r in range(self.article_table.rowCount()):
            it = self.article_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(r)
        return out

    def _set_article_check(self, row, checked=True):
        it = QTableWidgetItem()
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.article_table.setItem(row, 0, it)

    def _populate_cafe_collect_table(self, cafe_items):
        """수집된 카페 글 dict 리스트를 표에 채운다.
        cafe_items: [{'cafe':..., 'title':..., 'body':..., 'url':...}, ...]"""
        self._cafe_collected_items = list(cafe_items or [])
        self.collect_cafe_table.setRowCount(len(self._cafe_collected_items))
        for i, it in enumerate(self._cafe_collected_items):
            cb = QTableWidgetItem()
            cb.setFlags(cb.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            cb.setCheckState(Qt.CheckState.Checked)
            cb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.collect_cafe_table.setItem(i, 0, cb)
            self.collect_cafe_table.setItem(i, 1, QTableWidgetItem(str(it.get('cafe', it.get('cafe_name', '')))[:40]))
            self.collect_cafe_table.setItem(i, 2, QTableWidgetItem(str(it.get('title', ''))[:120]))
            body = str(it.get('body', it.get('content', '')))
            body_short = (body[:80] + '…') if len(body) > 80 else body
            self.collect_cafe_table.setItem(i, 3, QTableWidgetItem(body_short))
        self.main.log(f"📋 카페 수집 표 갱신: {len(self._cafe_collected_items)}건 (기본 전체 체크)")

    def send_cafe_to_ai(self):
        # 체크된 항목만 AI 입력으로 전달 (체크 없으면 전체 텍스트)
        items = list(getattr(self, '_cafe_collected_items', []) or [])
        if items:
            checked = self._get_checked_cafe_indices()
            if checked:
                items = [items[i] for i in checked if 0 <= i < len(items)]
                self.main.log(f"📤 체크된 {len(items)}건만 AI 입력으로 전달합니다")
            else:
                self.main.log(f"⚠️ 체크된 항목이 없어 전체 {len(items)}건을 전달합니다")
            blocks = []
            for k, it in enumerate(items, 1):
                blocks.append(f"[{k}] 제목: {it.get('title','')}\n카페: {it.get('cafe', it.get('cafe_name',''))}\n링크: {it.get('url','')}\n본문: {it.get('body', it.get('content',''))}")
            text = "\n\n---\n\n".join(blocks)
        else:
            text = self.collect_result.toPlainText()

        if not text:
            self.main.log("⚠️ 먼저 카페 글을 수집하세요")
            return
        self.ai_input.setPlainText(text)
        self.main.log("✅ 카페 데이터가 AI 입력으로 전달되었습니다")

    # v28: 카페 페이지 발행 기능 (인기글에서 이식)
    # ═══════════════════════════════════════════
    def _load_folder_cache(self):
        """계정별 폴더 캐시를 파일에서 로드"""
        try:
            with open('folder_cache.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_folder_cache(self):
        """계정별 폴더 캐시를 파일에 저장"""
        try:
            with open('folder_cache.json', 'w', encoding='utf-8') as f:
                json.dump(self._folder_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_selected_account(self):
        """선택된 계정의 id/pw 반환"""
        sel_id = self.account_combo.currentText().strip() if hasattr(self, 'account_combo') else ''
        accounts = self.main.config.get('naver_accounts', [])
        for acc in accounts:
            if acc.get('id') == sel_id:
                return acc.get('id', ''), acc.get('pw', '')
        return self.main.config.get('naver_id', ''), self.main.config.get('naver_pw', '')
    def _clear_folder_cache(self, account_id):
        """특정 계정의 폴더 캐시 삭제"""
        if account_id in self._folder_cache:
            del self._folder_cache[account_id]
            self._save_folder_cache()
            self.blog_folder.clear()
            self.main.log(f"🗑️ {account_id} 폴더 캐시 삭제됨")

    def _on_account_changed(self, account_id):
        """계정 변경 시 캐시된 폴더 자동 로드"""
        account_id = account_id.strip()
        if account_id and account_id in self._folder_cache:
            self.blog_folder.clear()
            self.blog_folder.addItems(self._folder_cache[account_id])
            self.main.log(f"📂 {account_id} 저장된 폴더 자동 로드 ({len(self._folder_cache[account_id])}개)")
        else:
            self.blog_folder.clear()

    # ─── 인기글 수집 ───

    def _refresh_image_table(self):
        """이미지 풀 테이블 갱신"""
        self.image_table.setRowCount(len(self._image_pool))
        for i, img in enumerate(self._image_pool):
            self.image_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.image_table.setItem(i, 1, QTableWidgetItem("📁업로드" if img['source'] == 'upload' else "🤖AI"))
            self.image_table.setItem(i, 2, QTableWidgetItem(img.get('desc', '')[:60]))
            self.image_table.setItem(i, 3, QTableWidgetItem(img.get('path', '')))
        self.img_status.setText(f"총 {len(self._image_pool)}개 이미지 등록됨")

    def _upload_images(self):
        """로컬 이미지 파일 직접 업로드"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "이미지 파일 선택", "",
            "이미지 파일 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;모든 파일 (*.*)"
        )
        if not files:
            return
        for fpath in files:
            self._image_pool.append({
                'source': 'upload',
                'desc': os.path.basename(fpath),
                'path': fpath,
            })
        self._refresh_image_table()
        self.main.log(f"🖼️ {len(files)}개 이미지 업로드 완료 (총 {len(self._image_pool)}개)")

    def _generate_dalle_images(self):
        """DALL-E API로 이미지 생성"""
        prompt = self.dalle_prompt.text().strip()
        if not prompt:
            self.main.log("⚠️ DALL-E 프롬프트를 입력하세요")
            return
        api_key = self.main.config.get('openai_key', '').strip()
        if not api_key:
            self.main.log("⚠️ OpenAI API Key가 설정되지 않았습니다 (설정 탭에서 입력)")
            return

        count = self.dalle_count.value()
        size = self.dalle_size.currentText()

        self.img_dalle_btn.setEnabled(False)
        self.img_dalle_btn.setText(f"🤖 생성 중... (0/{count})")
        self.main.log(f"🤖 DALL-E 이미지 생성 시작: '{prompt}' × {count}장")

        def do_gen():
            results = []
            for i in range(count):
                try:
                    path = generate_dalle_image(api_key, prompt, size=size)
                    results.append({'source': 'dalle', 'desc': prompt, 'path': path})
                    thread.log_signal.emit(f"  🖼️ [{i+1}/{count}] 이미지 생성 완료")
                except Exception as e:
                    thread.log_signal.emit(f"  ❌ [{i+1}/{count}] 생성 실패: {format_error_message(e)}")
                if i < count - 1:
                    time.sleep(2)
            return results

        thread = WorkerThread(do_gen)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def on_result(results):
            if isinstance(results, list):
                self._image_pool.extend(results)
                self._refresh_image_table()
                self.main.log(f"✅ DALL-E 이미지 {len(results)}장 생성 완료 (총 {len(self._image_pool)}개)")
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            self.img_dalle_btn.setEnabled(True),
            self.img_dalle_btn.setText("🤖 AI 이미지 생성 (ChatGPT)")
        ))
        thread.start()

    def _auto_generate_dalle_for_article(self, article_title, api_key, count=1, size="1024x1024"):
        """원고 제목 기반 자동 DALL-E 이미지 생성 (워커 스레드 내에서 호출)"""
        generated = []
        for i in range(count):
            try:
                prompt = f"네이버 블로그 포스팅용 고퀄리티 사진. 주제: {article_title}. 자연스러운 실제 사진 스타일, 텍스트 없이."
                path = generate_dalle_image(api_key, prompt, size=size)
                generated.append({'source': 'dalle', 'desc': f"자동생성: {article_title}", 'path': path})
            except Exception as e:
                pass  # 자동 생성 실패는 무시
            if i < count - 1:
                time.sleep(2)
        return generated

    def _delete_selected_image(self):
        """선택된 이미지 삭제"""
        rows = set(item.row() for item in self.image_table.selectedItems())
        if not rows:
            self.main.log("⚠️ 삭제할 이미지를 선택하세요")
            return
        for idx in sorted(rows, reverse=True):
            if 0 <= idx < len(self._image_pool):
                self._image_pool.pop(idx)
        self._refresh_image_table()
        self.main.log(f"🗑️ {len(rows)}개 이미지 삭제됨")

    def _clear_all_images(self):
        """모든 이미지 삭제"""
        self._image_pool.clear()
        self._refresh_image_table()
        self.main.log("🗑️ 전체 이미지 삭제됨")

    def _get_images_for_article(self, article_idx, photo_count):
        """원고에 사용할 이미지들 반환 (라운드 로빈).
        v27: photo_count가 0이어도 풀에 이미지가 있으면 최소 1장 반환 → 본문 시작 부분에 자동 삽입."""
        if not self._image_pool:
            return []
        total_imgs = len(self._image_pool)
        # [사진] 토큰이 0개여도 풀에 있으면 1장은 사용
        effective = max(photo_count, 1) if total_imgs > 0 else photo_count
        start = (article_idx * effective) % total_imgs
        result = []
        for i in range(effective):
            idx = (start + i) % total_imgs
            result.append(self._image_pool[idx]['path'])
        return result
    def _insert_image_to_editor(self, driver, image_path):
        """네이버 블로그 에디터에 이미지를 삽입.
        v27: 본문 전용 file input 우선 + 툴바 이미지 버튼 먼저 활성화 + iframe 진입."""
        import subprocess as sp

        if not os.path.exists(image_path):
            raise FileNotFoundError(f"이미지 파일 없음: {image_path}")

        abs_path = os.path.abspath(image_path)

        # iframe 진입 시도 (mainFrame 안에 에디터가 있음)
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame('mainFrame')
        except Exception:
            pass

        # ── 방법 1: 본문 이미지 툴바 버튼을 먼저 눌러 file input 활성화 ──
        try:
            for btn_sel in [
                'button.se-image-toolbar-button',
                'button[data-name="image"]',
                'button[data-type="image"]',
                'button[aria-label*="사진"]',
                'button[aria-label*="이미지"]',
                'button.se-toolbar-button-image',
            ]:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(0.6)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # ── 방법 2: 본문 전용 file input 우선 (썸네일용은 보통 첫 번째라 제외) ──
        try:
            # 본문용 셀렉터 우선
            preferred = []
            for sel in [
                'input.se-image-input-file',
                'input[class*="image"][type="file"]',
                'input[accept*="image"][type="file"]',
            ]:
                preferred.extend(driver.find_elements(By.CSS_SELECTOR, sel))
            # fallback: 모든 file input (썸네일 제외 위해 뒤쪽부터)
            all_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
            candidates = preferred + list(reversed(all_inputs))
            seen = set()
            for fi in candidates:
                key = fi.get_attribute('outerHTML')[:200]
                if key in seen:
                    continue
                seen.add(key)
                try:
                    # display:none 인 input도 send_keys 가능
                    driver.execute_script("arguments[0].style.display='block'; arguments[0].style.visibility='visible';", fi)
                    fi.send_keys(abs_path)
                    time.sleep(4)  # 업로드 + 처리 대기
                    self.log_emit(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                    # iframe 복귀
                    try:
                        driver.switch_to.default_content()
                        driver.switch_to.frame('mainFrame')
                    except Exception:
                        pass
                    return
                except Exception:
                    continue
        except Exception:
            pass

        # 방법 2: 이미지 버튼 클릭 → file input에 전달
        try:
            for btn_sel in [
                'button[data-name="image"]',
                'button.se-image-toolbar-button',
                'button[data-type="image"]',
                'button.se-toolbar-button-image',
            ]:
                try:
                    img_btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    driver.execute_script("arguments[0].click();", img_btn)
                    time.sleep(1)
                    # file input 찾기
                    file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                    for fi in file_inputs:
                        try:
                            fi.send_keys(os.path.abspath(image_path))
                            time.sleep(3)
                            self.log_emit(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                            return
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

        # 방법 3: PowerShell 클립보드 이미지 복사 → Ctrl+V
        try:
            abs_path = os.path.abspath(image_path).replace('\\', '/')
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile('{abs_path}')
[System.Windows.Forms.Clipboard]::SetImage($img)
$img.Dispose()
"""
            sp.run(['powershell', '-Command', ps_script], capture_output=True, timeout=10)
            time.sleep(0.5)
            safe_hotkey(driver, 'ctrl', 'v')
            time.sleep(3)
            self.log_emit(f"  🖼️ 이미지 붙여넣기 완료: {os.path.basename(image_path)}")
        except Exception as e:
            raise RuntimeError(f"이미지 삽입 모든 방법 실패: {e}")


    def load_blog_folders(self):
        """블로그 폴더 불러오기 - 캐시 우선 사용"""
        nid = self.account_combo.currentText().strip()
        if not nid:
            self.main.log("⚠️ 발행 계정을 선택하세요")
            return

        # 캐시 확인
        if nid in self._folder_cache and self._folder_cache[nid]:
            cached = self._folder_cache[nid]
            self.blog_folder.clear()
            self.blog_folder.addItems(cached)
            self.main.log(f"📂 저장된 폴더 사용: {nid} ({len(cached)}개) — 새로고침하려면 캐시 삭제 후 재시도")
            return

        self.load_folders_btn.setEnabled(False)
        self.main.log(f"🔄 [{nid}] 블로그 폴더 목록 불러오는 중...")

        def do_load():
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': f'https://blog.naver.com/{nid}'
                }
                url = f"https://blog.naver.com/PostList.naver?blogId={nid}"
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code == 200:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.text, 'html.parser')
                    folders = []
                    for opt in soup.select('select option, li a[onclick*="category"]'):
                        name = opt.get_text(strip=True)
                        if name and name not in ['전체보기', ''] and len(name) < 50:
                            if name not in folders:
                                folders.append(name)
                    if folders:
                        return folders
                return []
            except Exception as e:
                self.log_emit(f"⚠️ 폴더 불러오기 실패: {e}")
                return []

        thread = WorkerThread(do_load)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        def on_result(folders):
            if folders and isinstance(folders, list):
                self.blog_folder.clear()
                self.blog_folder.addItems(folders)
                # 캐시에 저장
                self._folder_cache[nid] = folders
                self._save_folder_cache()
                self.main.log(f"✅ {len(folders)}개 폴더 로드 완료 (캐시 저장됨 — 다음부터 자동 로드)")
            else:
                self.main.log("⚠️ 폴더를 찾을 수 없습니다")
        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: self.load_folders_btn.setEnabled(True))
        thread.start()


    def _build_publish_queue(self):
        source_articles = list(getattr(self, 'generated_articles', []) or [])

        # 체크된 행만 발행 (없으면 전체 폴백)
        try:
            checked = self._get_checked_article_indices()
        except Exception:
            checked = []
        if source_articles and checked:
            source_articles = [source_articles[i] for i in checked if 0 <= i < len(source_articles)]
            try:
                self.main.log(f"📤 발행 대기열: 체크된 {len(source_articles)}건만 발행")
            except Exception:
                pass
        elif source_articles:
            try:
                self.main.log(f"⚠️ 체크된 원고가 없어 전체 {len(source_articles)}건을 발행합니다")
            except Exception:
                pass

        if not source_articles:
            content = self.ai_output.toPlainText().strip()
            if content:
                source_articles = [{'title': '카페 포스트', 'content': content}]
        queue = []
        for idx, article in enumerate(source_articles, start=1):
            title = str(article.get('title', f'카페 포스트 {idx}')).strip() or f'카페 포스트 {idx}'
            content = str(article.get('content', '')).strip()
            status = article.get('status', '')
            if not content or status == '실패': continue
            queue.append({'order': idx, 'title': title, 'content': content, 'status': 'pending', 'result': ''})
        return queue

    def _write_publish_queue_snapshot(self, account_id, queue):
        safe_account = re.sub(r'[^0-9A-Za-z_.-]+', '_', (account_id or 'default').strip())
        path = f"publish_queue_popular_{safe_account}.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        return path


    def publish_to_blog(self):
        """generated_articles 리스트를 순회하며 각각 블로그에 발행 (순차 처리)"""
        # 중복 실행 방지
        if self._publishing_flag:
            self.main.log("⚠️ 이미 발행이 진행 중입니다. 완료될 때까지 기다려주세요.")
            return

        publish_queue = self._build_publish_queue()
        if not publish_queue:
            self.main.log("⚠️ 발행할 원고가 없습니다. AI 글쓰기를 먼저 실행하세요")
            return

        config = self.main.config
        naver_id, naver_pw = self._get_selected_account()
        if not naver_id or not naver_pw:
            self.main.log("⚠️ 설정에서 네이버 계정을 먼저 입력하세요")
            return

        folder = self.blog_folder.currentText().strip()
        mode = self.save_mode.currentText()
        total = len(publish_queue)
        queue_path = self._write_publish_queue_snapshot(naver_id, publish_queue)
        self.publish_queue = publish_queue
        self._publishing_flag = True

        self.publish_btn.setEnabled(False)
        self.publish_btn.setText(f"발행 중... (0/{total})")
        self.pub_progress.setVisible(True)
        self.pub_progress.setValue(0)

        self.main.log(f"🚀 총 {total}건 블로그 발행 시작")
        for qi, q_item in enumerate(publish_queue): self.main.log(f"  📋 [{qi+1}] {q_item['title'][:40]} ({len(q_item['content'])}자)")
        self.main.log(f"🗂️ 발행 대기열 저장 완료: {queue_path}")

        def do():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            opts = uc.ChromeOptions()
            opts.add_argument('--no-sandbox')
            opts.add_argument('--start-maximized')
            opts.add_argument('--user-data-dir=./chrome_profile')
            opts.add_argument('--disable-popup-blocking')
            prefs = {
                'profile.default_content_setting_values.popups': 0,
            }
            opts.add_experimental_option('prefs', prefs)
            driver = make_uc_driver(opts)

            # ── window.open 완전 차단 (드라이버 생성 직후) ──
            def block_window_open():
                try:
                    driver.execute_script("""
                        if (!window._woBlocked) {
                            window._origOpen = window.open;
                            window.open = function(url) {
                                if (url) window.location.href = url;
                                return window;
                            };
                            window._woBlocked = true;
                        }
                    """)
                except Exception:
                    pass
            # ── "작성중인 글이 있습니다" 팝업 취소 처리 ──
            def dismiss_draft_popup():
                """에디터 진입 시 '작성중인 글이 있습니다' 팝업이 뜨면 취소 버튼 클릭"""
                try:
                    time.sleep(2)
                    cancel_selectors = [
                        'button.se-popup-button-cancel',
                        'button.cancel_btn__WEaBq',
                        'button.cancel_btn',
                        'button[class*="cancel"]',
                        'button.se-cancel',
                    ]
                    # 팝업 텍스트로도 탐지
                    try:
                        popup_texts = driver.find_elements(By.XPATH,
                            "//*[contains(text(),'작성중인') or contains(text(),'작성 중인') or contains(text(),'임시저장')]")
                        if popup_texts:
                            self.log_emit("  ℹ️ '작성중인 글' 팝업 감지 → 취소 클릭 시도")
                            for sel in cancel_selectors:
                                try:
                                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                            # CSS 셀렉터 실패 시 XPATH로 취소/아니오 버튼 찾기
                            cancel_btns = driver.find_elements(By.XPATH,
                                "//button[contains(text(),'취소') or contains(text(),'아니') or contains(text(),'아니오') or contains(text(),'새로')]")
                            for btn in cancel_btns:
                                try:
                                    if btn.is_displayed():
                                        btn.click()
                                        self.log_emit("  ✅ 작성중인 글 팝업 취소 완료 (XPATH)")
                                        time.sleep(1)
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        pass
                except Exception as e:
                    self.log_emit(f"  ℹ️ 팝업 처리 중 예외(무시): {e}")
                return False


            # ── 원고 정제 함수 ──
            def sanitize_content(raw_content):
                """발행 전 원고에서 불필요한 마크다운/이미지/참조를 제거"""
                lines = raw_content.split('\n')
                cleaned = []
                for line in lines:
                    s = line.strip()
                    # 구분선 제거
                    if re.match(r'^[━─═\-]{3,}', s):
                        continue
                    # [이미지 삽입] 줄 제거
                    if s == '[이미지 삽입]':
                        continue
                    # 마크다운 이미지 ![alt](url) 제거
                    if re.match(r'^!\[.*\]\(.*\)$', s):
                        continue
                    # 순수 URL만 있는 줄 제거 (이미지 URL)
                    if re.match(r'^https?://\S+$', s):
                        continue
                    # URL (설명) 형태 줄 제거
                    if re.match(r'^https?://\S+\s*\(.*\)$', s):
                        continue
                    # 📄 [번호] 헤더 줄 제거
                    if re.match(r'^📄\s*\[\d+\]', s):
                        continue
                    # (공백 제외 N자) 제거
                    if re.match(r'^\(공백\s*제외\s*\d+자\)$', s):
                        continue
                    # **추가 반전형 이미지 키워드** 블록 제거
                    if '이미지 키워드' in s and ('추가' in s or '반전형' in s):
                        continue
                    # 번호. URL 형태 (이미지 리스트) 제거
                    if re.match(r'^\d+\.\s*https?://\S+', s):
                        continue
                    # 참조 번호 [1][2] 등 제거 (텍스트는 유지)
                    s = re.sub(r'\[\d+\]', '', s)
                    # ## 마크다운 헤더 → 일반 텍스트
                    s = re.sub(r'^#{1,6}\s*', '', s)
                    # **bold** → 일반 텍스트
                    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
                    s = s.strip()
                    if s:
                        cleaned.append(s)
                # 연속 빈 줄 정리
                result = '\n'.join(cleaned)
                result = re.sub(r'\n{3,}', '\n\n', result)
                return result.strip()

            def validate_article(article):
                """발행 가능한 원고인지 검증. 불가 시 사유 반환"""
                title = (article.get('title') or '').strip()
                content = (article.get('content') or '').strip()
                if not title or not content:
                    return '제목 또는 본문이 비어 있습니다'
                if '필수 입력 정보 부재' in content or '요청을 정확히 처리할 수 없습니다' in content:
                    return '오류 원고 (AI가 생성 실패한 항목)'
                # 정제 후에도 내용이 너무 짧으면 스킵
                sanitized = sanitize_content(content)
                if len(sanitized) < 50:
                    return f'정제 후 본문이 너무 짧습니다 ({len(sanitized)}자)'
                return None  # 검증 통과

            success_count = 0
            fail_count = 0
            main_window = None

            def update_queue_status(index, status, result):
                publish_queue[index]['status'] = status
                publish_queue[index]['result'] = result
                self._write_publish_queue_snapshot(naver_id, publish_queue)

            def keep_only_window(target_handle=None):
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                target = target_handle or main_window or handles[-1]
                for handle in list(handles):
                    if handle == target:
                        continue
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(target)
                main_window = target

            def close_all_extra_tabs():
                """main_window 외 모든 탭을 닫는다"""
                nonlocal main_window
                handles = list(driver.window_handles)
                if not handles:
                    return
                if not main_window or main_window not in handles:
                    main_window = handles[0]
                for h in handles:
                    if h == main_window:
                        continue
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                try:
                    driver.switch_to.window(main_window)
                except Exception:
                    pass

            def open_editor_single_tab():
                """항상 단일 탭에서 글쓰기 에디터를 연다"""
                nonlocal main_window

                # 0) 먼저 탭 1개만 남기기
                close_all_extra_tabs()

                # 1) 현재 탭에서 에디터로 이동 (execute_script로 강제)
                editor_url = 'https://blog.naver.com/GoBlogWrite.naver'
                try:
                    driver.execute_script(f'window.location.href = "{editor_url}";')
                except Exception:
                    driver.get(editor_url)

                time.sleep(5)

                # 2) 새 탭이 열렸으면 마지막 탭(에디터)만 남기고 닫기
                handles = list(driver.window_handles)
                if len(handles) > 1:
                    editor_tab = handles[-1]
                    for h in handles:
                        if h == editor_tab:
                            continue
                        try:
                            driver.switch_to.window(h)
                            driver.close()
                        except Exception:
                            pass
                    driver.switch_to.window(editor_tab)
                    main_window = editor_tab
                    self.log_emit(f"  ℹ️ 추가 탭 {len(handles)-1}개 닫고 에디터 탭만 유지")
                else:
                    main_window = handles[0] if handles else driver.current_window_handle
                    driver.switch_to.window(main_window)

                # 3) iframe 전환
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

                # mainFrame이 있으면 진입
                try:
                    iframe = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.ID, 'mainFrame'))
                    )
                    driver.switch_to.frame(iframe)
                    time.sleep(1)
                    self.log_emit("  ✅ mainFrame iframe 진입 성공")
                    dismiss_draft_popup()
                except Exception:
                    self.log_emit("  ℹ️ mainFrame 없음, 직접 에디터 접근 시도")

                # 4) 에디터 로드 확인
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'div.se-title-text, div[contenteditable="true"]'))
                    )
                    self.log_emit("  ✅ 에디터 로드 확인")
                except Exception:
                    self.log_emit("  ⚠️ 에디터 요소를 찾지 못했습니다 (계속 시도)")

            def select_folder_if_needed():
                if not folder:
                    return True
                selectors = ['select#categoryId', 'select[name="categoryId"]']
                for selector in selectors:
                    try:
                        cat_select = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        cat_select.click()
                        time.sleep(0.3)
                        for opt in cat_select.find_elements(By.TAG_NAME, 'option'):
                            if folder in opt.text:
                                opt.click()
                                time.sleep(0.3)
                                return True
                    except Exception:
                        continue
                return False

            def input_title(art_title):
                title_selectors = [
                    'span.se-placeholder',
                    'div[data-name="title"] div[contenteditable="true"]',
                    'div.se-section-title div.se-text-paragraph',
                    'div.se-title-text',
                ]
                last_error = None
                for selector in title_selectors:
                    try:
                        title_el = WebDriverWait(driver, 3).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                        try:
                            title_el.click()
                        except Exception:
                            from selenium.webdriver.common.action_chains import ActionChains
                            ActionChains(driver).move_to_element(title_el).click().perform()
                        time.sleep(0.2)
                        pyperclip.copy(art_title)
                        safe_hotkey(driver, 'ctrl', 'a')
                        time.sleep(0.1)
                        safe_hotkey(driver, 'ctrl', 'v')
                        time.sleep(0.3)
                        self.log_emit(f"  ✅ 제목 입력 완료: {art_title[:30]}")
                        return True
                    except Exception as e:
                        last_error = e
                        continue
                raise RuntimeError(f'제목 입력 실패: {last_error}')

            def focus_body():
                body_selectors = [
                    'div.se-section-text div[contenteditable="true"]',
                    'div.se-component-content div[contenteditable="true"]',
                    'div.se-text-paragraph',
                    'div[contenteditable="true"]',
                ]
                for selector in body_selectors:
                    try:
                        elems = driver.find_elements(By.CSS_SELECTOR, selector)
                        for elem in elems:
                            if not elem.is_displayed():
                                continue
                            try:
                                driver.execute_script("arguments[0].click();", elem)
                            except Exception:
                                elem.click()
                            time.sleep(0.3)
                            return True
                    except Exception:
                        continue
                try:
                    safe_press(driver, 'tab')
                    time.sleep(0.3)
                    return True
                except Exception:
                    return False

            def input_body(art_content):
                if not focus_body():
                    raise RuntimeError('본문 입력 영역 포커스 실패')

                # v26: 본문 전처리만 먼저, 정렬은 입력 후에 적용
                art_content = preprocess_article_lines(art_content)
                alignment = getattr(self, '_selected_alignment', 'left')

                wrote_any = False
                for raw_line in art_content.split('\n'):
                    line = raw_line.rstrip()
                    if not line.strip():
                        safe_press(driver, 'enter')
                        time.sleep(0.05)
                        continue

                    # 스티커 토큰 처리
                    sticker_idxs = extract_sticker_indices(line)
                    line_wo_sticker = strip_sticker_tokens(line)

                    # 인용구 자동 감지 — 한 줄만 인용 블록, 나머지는 일반 문단
                    if line_wo_sticker and looks_like_quote(line_wo_sticker):
                        insert_quote_block(driver, line_wo_sticker)
                        wrote_any = True
                    elif line_wo_sticker:
                        pyperclip.copy(line_wo_sticker)
                        safe_hotkey(driver, 'ctrl', 'v')
                        safe_press(driver, 'enter')
                        time.sleep(0.1)
                        wrote_any = True

                    for sidx in sticker_idxs:
                        try:
                            insert_naver_sticker(driver, sidx)
                            safe_press(driver, 'enter')
                        except Exception:
                            pass

                if not wrote_any:
                    raise RuntimeError('본문 내용이 비어 있어 입력하지 못했습니다')

                # v26: 입력 완료 후 전체 선택 → 정렬 적용 (+ JS fallback)
                try:
                    apply_alignment(driver, alignment, select_all_first=True)
                except Exception:
                    pass

            def submit_article(art_idx, art_title):
                time.sleep(1)
                if '임시' in mode:
                    save_selectors = [
                        'button[data-testid="save-btn"]',
                        'button.save_btn__Y5f57',
                        'button.save_btn',
                        'button[class*="save"]',
                    ]
                    clicked = False
                    for selector in save_selectors:
                        try:
                            btn = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if not clicked:
                        try:
                            btn = driver.find_element(
                                By.XPATH, '//button[contains(., "임시저장")]'
                            )
                            driver.execute_script("arguments[0].click();", btn)
                            clicked = True
                        except Exception:
                            pass
                    if not clicked:
                        raise RuntimeError('임시저장 버튼을 찾지 못했습니다')
                    time.sleep(2)
                    self.log_emit(f"  💾 [{art_idx + 1}/{total}] 임시저장 완료: {art_title[:30]}")
                    driver.execute_script('window.location.href = "https://blog.naver.com";')
                    time.sleep(2)
                    return True, '임시저장 완료'


                publish_selectors = [
                    'button[data-testid="publish-btn"]',
                    'button.publish_btn__Y5f57',
                    'button.publish_btn',
                    'button[class*="publish"]',
                ]
                confirm_selectors = [
                    'button.se-popup-button-confirm',
                    'button.confirm_btn__WEaBq',
                    'button.confirm_btn',
                    'button[class*="confirm"]',
                ]

                clicked = False
                for selector in publish_selectors:
                    try:
                        btn = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        driver.execute_script("arguments[0].click();", btn)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError('발행 버튼을 찾지 못했습니다')

                time.sleep(2)

                confirmed = False
                for selector in confirm_selectors:
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, selector)
                        driver.execute_script("arguments[0].click();", btn)
                        confirmed = True
                        break
                    except Exception:
                        continue

                if not confirmed:
                    self.log_emit('  ℹ️ 발행 확인 팝업이 없어 바로 완료 여부를 확인합니다')

                time.sleep(4)
                current_url = (driver.current_url or '').lower()
                if 'goblogwrite' in current_url:
                    self.log_emit('  ⚠️ 에디터 URL에 머물러 있음, 발행 재시도...')
                    # 한 번 더 confirm 버튼 시도
                    for selector in confirm_selectors:
                        try:
                            btn = driver.find_element(By.CSS_SELECTOR, selector)
                            driver.execute_script("arguments[0].click();", btn)
                            time.sleep(3)
                            break
                        except Exception:
                            continue

                self.log_emit(f"  📤 [{art_idx + 1}/{total}] 즉시발행 완료")
                return True, driver.current_url

            try:
                self.log_emit('🔑 네이버 로그인 중...')
                if not naver_login_with_fallback(driver, naver_id, naver_pw, self.log_emit):
                    return '❌ 로그인 실패 - 설정에서 계정을 확인하세요'
                self.log_emit('✅ 로그인 완료')
                main_window = driver.current_window_handle
                close_all_extra_tabs()
                block_window_open()

                for art_idx, article in enumerate(publish_queue):
                    try:
                        art_title = article.get('title', f'포스트 {art_idx + 1}')
                        art_content = article.get('content', '')

                        # ── 원고 검증 ──
                        skip_reason = validate_article(article)
                        if skip_reason:
                            self.log_emit(f"\n⏭️ [{art_idx + 1}/{total}] 건너뜀: {skip_reason} — {art_title[:30]}")
                            fail_count += 1
                            update_queue_status(art_idx, 'skipped', skip_reason)
                            continue

                        # ── 원고 정제 ──
                        art_content = sanitize_content(art_content)

                        # ── 본문에서 제목 줄 제거 (제목은 별도 입력) ──
                        content_lines = art_content.split('\n')
                        filtered_lines = []
                        title_stripped = False
                        for cl in content_lines:
                            cl_clean = cl.strip()
                            if not title_stripped and cl_clean and (
                                cl_clean == art_title.strip() or
                                re.sub(r'^#{1,6}\s*', '', cl_clean) == art_title.strip() or
                                cl_clean.replace('**', '') == art_title.strip()
                            ):
                                title_stripped = True
                                continue
                            filtered_lines.append(cl)
                        art_content = '\n'.join(filtered_lines).strip()
                        self.log_emit(f"\n📝 [{art_idx + 1}/{total}] 발행 시작: {art_title[:40]}... (정제 후 {len(art_content)}자)")
                        QTimer.singleShot(0, lambda idx=art_idx: self.publish_btn.setText(f"발행 중... ({idx + 1}/{total})"))

                        open_editor_single_tab()
                        block_window_open()  # 에디터 로드 후 다시 차단

                        if folder and not select_folder_if_needed():
                            self.log_emit('  ⚠️ 폴더 선택 실패, 기본 카테고리로 진행합니다')

                        input_title(art_title)
                        input_body(art_content)
                        ok, result_message = submit_article(art_idx, art_title)

                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_all_extra_tabs()
                        time.sleep(1)

                        success_count += 1
                        update_queue_status(art_idx, 'done', result_message)

                    except Exception as e:
                        self.log_emit(f"  ❌ [{art_idx + 1}/{total}] 발행 실패: {e}")
                        fail_count += 1
                        update_queue_status(art_idx, 'error', str(e))
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                        close_all_extra_tabs()
                        time.sleep(1)

                    self._emit_progress(self.pub_progress, int(((art_idx + 1) / total) * 100))
                    time.sleep(2)

                return f"✅ 블로그 발행 완료! 성공: {success_count}건, 실패: {fail_count}건"

            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(lambda r: self.main.log(r))

        def on_news_publish_finished():
            self._publishing_flag = False
            self.publish_btn.setEnabled(True)
            self.publish_btn.setText('🚀 블로그 발행')
            self.pub_progress.setVisible(False)

        thread.finished_signal.connect(on_news_publish_finished)
        thread.start()
# ═══════════════════════════════════════════════════════════════
#  4. 뉴스 수집 페이지 (웹 크롤링 방식)
# ═══════════════════════════════════════════════════════════════


#  6. 자동 발행 페이지
# ═══════════════════════════════════════════════════════════════

class PublishPage(QWidget):
    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        layout = QVBoxLayout(self)

        title = QLabel("🚀 다중 플랫폼 자동 발행")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        layout.addWidget(title)

        self.pub_platform = QComboBox()
        self.pub_platform.addItems(["네이버 블로그", "네이버 카페", "네이버 숏츠"])
        layout.addWidget(QLabel("플랫폼:"));
        layout.addWidget(self.pub_platform)

        self.pub_mode = QComboBox()
        self.pub_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        layout.addWidget(QLabel("모드:"));
        layout.addWidget(self.pub_mode)

        self.pub_content = QTextEdit()
        self.pub_content.setPlaceholderText("발행할 원고 붙여넣기...")
        layout.addWidget(self.pub_content)

        self.pub_btn = QPushButton("🔥 발행 실행")
        self.pub_btn.clicked.connect(self.publish)
        layout.addWidget(self.pub_btn)
        layout.addStretch()

    def publish(self):
        content = self.pub_content.toPlainText().strip()
        if not content:
            self.main.log("⚠️ 원고가 비어 있음");
            return
        self.pub_btn.setEnabled(False)
        plat = self.pub_platform.currentText()

        def do():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By

            opts = uc.ChromeOptions()
            opts.add_argument('--start-maximized')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--disable-features=RendererCodeIntegrity')
            driver = make_uc_driver(opts)
            try:
                nid = self.main.config.get('naver_id', '')
                npw = self.main.config.get('naver_pw', '')
                naver_login_with_fallback(driver, nid, npw)
                if "블로그" in plat:
                    driver.get("https://blog.naver.com/GoBlogWrite.naver")
                time.sleep(3)
                pyperclip.copy(content)
                return f"✅ {plat} 발행 준비 완료"
            finally:
                pass
        def do():
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            opts = uc.ChromeOptions()
            opts.add_argument('--start-maximized')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--disable-features=RendererCodeIntegrity')
            opts.add_argument('--user-data-dir=./chrome_profile')
            opts.add_argument('--disable-popup-blocking')
            prefs = {'profile.default_content_setting_values.popups': 0}
            opts.add_experimental_option('prefs', prefs)
            driver = make_uc_driver(opts)

            # window.open 차단
            def block_wo():
                try:
                    driver.execute_script("""
                        if (!window._woBlocked) {
                            window.open = function(url) {
                                if (url) window.location.href = url;
                                return window;
                            };
                            window._woBlocked = true;
                        }
                    """)
                except Exception:
                    pass

            def close_extra():
                main = driver.window_handles[0]
                for h in list(driver.window_handles):
                    if h == main: continue
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(main)

            try:
                nid = self.main.config.get('naver_id', '')
                npw = self.main.config.get('naver_pw', '')
                naver_login_with_fallback(driver, nid, npw)
                block_wo()
                close_extra()

                if "블로그" in plat:
                    try:
                        driver.execute_script('window.location.href = "https://blog.naver.com/GoBlogWrite.naver";')
                    except Exception:
                        driver.get("https://blog.naver.com/GoBlogWrite.naver")
                    time.sleep(5)
                    close_extra()
                    block_wo()

                    # iframe
                    try:
                        iframe = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.ID, 'mainFrame'))
                        )
                        driver.switch_to.frame(iframe)
                        time.sleep(1)
                    except Exception:
                        pass

                    # 본문 붙여넣기
                    pyperclip.copy(content)
                    try:
                        safe_press(driver, 'tab')
                        time.sleep(0.3)
                        safe_hotkey(driver, 'ctrl', 'v')
                        time.sleep(1)
                    except Exception:
                        pass

                    close_extra()

                return f"✅ {plat} 발행 준비 완료 — 브라우저에서 확인 후 발행하세요"
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        thread = WorkerThread(do)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.result_signal.connect(self.main.log)
        thread.finished_signal.connect(lambda: (self.pub_btn.setEnabled(True), self.pub_btn.setText("🔥 발행 실행")))
        thread.start()
# ═══════════════════════════════════════════════════════════════
#  메인 윈도우
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
#  이미지 생성 페이지 (GPT DALL-E)
# ═══════════════════════════════════════════════════════════════
class ImageGenPage(QWidget):
    """GPT DALL-E를 이용한 이미지 생성 전용 페이지"""

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._generated_images = []  # [{'prompt':'...', 'path':'...', 'size':'...'}, ...]
        self._generating = False

        layout = QVBoxLayout(self)

        title = QLabel("🖼️ AI 이미지 생성 (ChatGPT)")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#BB86FC;")
        layout.addWidget(title)

        desc = QLabel("OpenAI ChatGPT (gpt-image-1) 모델을 사용하여 블로그용 고퀄리티 이미지를 생성합니다")
        desc.setStyleSheet("color:#888; margin-bottom:8px;")
        layout.addWidget(desc)

        # ── 이미지 생성 설정 ──
        gen_group = QGroupBox("🎨 이미지 생성")
        g = QVBoxLayout(gen_group)

        g.addWidget(QLabel("프롬프트 (이미지 설명):"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText(
            "생성할 이미지를 자세히 설명하세요.\n\n"
            "예시:\n"
            "• 서울 남산타워가 보이는 야경, 시네마틱 느낌\n"
            "• 카페에서 라떼아트가 있는 커피 한 잔, 따뜻한 조명\n"
            "• 봄 벚꽃이 만개한 공원 산책로, 밝고 화사한 분위기\n\n"
            "여러 줄 입력 시 각 줄마다 별도 이미지를 생성합니다."
        )
        self.prompt_input.setMaximumHeight(150)
        g.addWidget(self.prompt_input)

        h_opts = QHBoxLayout()
        h_opts.addWidget(QLabel("이미지 크기:"))
        self.size_combo = QComboBox()
        self.size_combo.addItems(["1024x1024 (정사각형)", "1024x1536 (세로형)", "1536x1024 (가로형)"])
        h_opts.addWidget(self.size_combo, 2)

        h_opts.addWidget(QLabel("품질:"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["low (빠름)", "medium (기본)", "high (고품질)"])
        self.quality_combo.setCurrentIndex(1)
        h_opts.addWidget(self.quality_combo)
        g.addLayout(h_opts)

        h_count = QHBoxLayout()
        h_count.addWidget(QLabel("프롬프트당 생성 장수:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 10)
        self.count_spin.setValue(1)
        h_count.addWidget(self.count_spin)
        h_count.addStretch()
        g.addLayout(h_count)

        h_btns = QHBoxLayout()
        self.gen_btn = QPushButton("🎨 이미지 생성")
        self.gen_btn.clicked.connect(self.generate_images)
        self.gen_btn.setStyleSheet("background-color:#4a1a8a; color:white; font-weight:bold; padding:10px; font-size:14px;")
        h_btns.addWidget(self.gen_btn)

        self.cancel_btn = QPushButton("🛑 취소")
        self.cancel_btn.clicked.connect(self._cancel_generation)
        self.cancel_btn.setEnabled(False)
        h_btns.addWidget(self.cancel_btn)
        g.addLayout(h_btns)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        g.addWidget(self.progress)

        layout.addWidget(gen_group)

        # ── 생성된 이미지 목록 ──
        result_group = QGroupBox("📋 생성된 이미지 목록")
        r = QVBoxLayout(result_group)

        self.image_table = QTableWidget()
        self.image_table.setColumnCount(5)
        self.image_table.setHorizontalHeaderLabels(["번호", "프롬프트", "크기", "파일 경로", "상태"])
        self.image_table.horizontalHeader().setStretchLastSection(True)
        self.image_table.setColumnWidth(0, 40)
        self.image_table.setColumnWidth(1, 300)
        self.image_table.setColumnWidth(2, 100)
        self.image_table.setColumnWidth(3, 250)
        self.image_table.setColumnWidth(4, 60)
        self.image_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.image_table.setMinimumHeight(200)
        r.addWidget(self.image_table)

        h_actions = QHBoxLayout()

        self.open_folder_btn = QPushButton("📂 저장 폴더 열기")
        self.open_folder_btn.clicked.connect(self._open_save_folder)
        h_actions.addWidget(self.open_folder_btn)

        self.copy_to_popular_btn = QPushButton("📤 인기글 이미지풀에 추가")
        self.copy_to_popular_btn.clicked.connect(self._copy_to_popular_pool)
        self.copy_to_popular_btn.setStyleSheet("background-color:#2d6a4f; color:white; font-weight:bold; padding:6px;")
        h_actions.addWidget(self.copy_to_popular_btn)

        self.del_selected_btn = QPushButton("🗑️ 선택 삭제")
        self.del_selected_btn.clicked.connect(self._delete_selected)
        h_actions.addWidget(self.del_selected_btn)

        self.clear_btn = QPushButton("🗑️ 전체 삭제")
        self.clear_btn.clicked.connect(self._clear_all)
        h_actions.addWidget(self.clear_btn)
        r.addLayout(h_actions)

        self.status_label = QLabel("생성된 이미지: 0개")
        self.status_label.setStyleSheet("color:#888; font-size:11px;")
        r.addWidget(self.status_label)

        layout.addWidget(result_group)

        # ── 저장 경로 설정 ──
        h_save = QHBoxLayout()
        h_save.addWidget(QLabel("저장 폴더:"))
        self.save_dir = QLineEdit()
        default_dir = os.path.join(os.path.expanduser("~"), "Documents", "DALLE_Images")
        self.save_dir.setText(default_dir)
        h_save.addWidget(self.save_dir, 4)
        browse_btn = QPushButton("📁 찾아보기")
        browse_btn.clicked.connect(self._browse_save_dir)
        h_save.addWidget(browse_btn)
        layout.addLayout(h_save)

        layout.addStretch()

    def log_emit(self, msg):
        self.main.log(msg)

    def _get_size_str(self):
        return self.size_combo.currentText().split(" ")[0]

    def _get_quality(self):
        return self.quality_combo.currentText().split(" ")[0]

    def generate_images(self):
        """프롬프트 기반 이미지 생성"""
        raw = self.prompt_input.toPlainText().strip()
        if not raw:
            self.main.log("⚠️ 프롬프트를 입력하세요")
            return

        api_key = self.main.config.get('openai_key', '').strip()
        if not api_key:
            self.main.log("⚠️ OpenAI API Key가 설정되지 않았습니다 (설정 탭에서 입력)")
            return

        # 여러 줄 → 각각 별도 프롬프트
        prompts = [p.strip() for p in raw.split('\n') if p.strip()]
        count_per = self.count_spin.value()
        size = self._get_size_str()
        quality = self._get_quality()
        save_dir = self.save_dir.text().strip()

        if not save_dir:
            save_dir = os.path.join(os.path.expanduser("~"), "Documents", "DALLE_Images")
            self.save_dir.setText(save_dir)
        os.makedirs(save_dir, exist_ok=True)

        total_jobs = len(prompts) * count_per
        self.main.log(f"🎨 DALL-E 이미지 생성 시작: {len(prompts)}개 프롬프트 × {count_per}장 = 총 {total_jobs}장")

        self._generating = True
        self._cancel_flag = False
        self.gen_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)

        def do_generate():
            results = []
            done = 0
            for prompt in prompts:
                for i in range(count_per):
                    if self._cancel_flag:
                        thread.log_signal.emit("🛑 이미지 생성 취소됨")
                        return results

                    thread.log_signal.emit(f"🖼️ [{done+1}/{total_jobs}] 생성 중: {prompt[:50]}...")

                    try:
                        client = OpenAI(api_key=api_key.strip())
                        response = client.images.generate(
                            model="gpt-image-1",
                            prompt=prompt,
                            size=size,
                            quality=quality,
                            n=1
                        )
                        img_item = response.data[0]
                        revised_prompt = getattr(img_item, 'revised_prompt', None) or prompt

                        # 이미지 데이터 추출
                        if hasattr(img_item, 'b64_json') and img_item.b64_json:
                            img_data = base64.b64decode(img_item.b64_json)
                        elif hasattr(img_item, 'url') and img_item.url:
                            img_data = requests.get(img_item.url, timeout=60).content
                        else:
                            raise RuntimeError('이미지 데이터 추출 실패')

                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        safe_name = re.sub(r'[^a-zA-Z0-9가-힣]', '_', prompt[:30])
                        filename = f"dalle_{timestamp}_{safe_name}_{i+1}.png"
                        filepath = os.path.join(save_dir, filename)

                        with open(filepath, 'wb') as f:
                            f.write(img_data)

                        results.append({
                            'prompt': prompt,
                            'revised_prompt': revised_prompt,
                            'path': filepath,
                            'size': size,
                            'status': '완료'
                        })
                        thread.log_signal.emit(f"  ✅ [{done+1}/{total_jobs}] 저장: {filename}")
                    except Exception as e:
                        err = format_error_message(e)
                        thread.log_signal.emit(f"  ❌ [{done+1}/{total_jobs}] 실패: {err}")
                        results.append({
                            'prompt': prompt,
                            'path': '',
                            'size': size,
                            'status': '실패',
                            'error': err
                        })

                    done += 1
                    thread.ui_update_signal.emit(lambda d=done, t=total_jobs: self.progress.setValue(int(d/t*100)))

                    if done < total_jobs and not self._cancel_flag:
                        time.sleep(2)

            return results

        thread = WorkerThread(do_generate)
        self.main.worker_threads.append(thread)
        thread.log_signal.connect(self.main.log)
        thread.ui_update_signal.connect(lambda fn: fn())

        def on_result(results):
            if isinstance(results, list):
                self._generated_images.extend(results)
                self._refresh_table()
                success = sum(1 for r in results if r.get('status') == '완료')
                failed = sum(1 for r in results if r.get('status') == '실패')
                self.main.log(f"✅ DALL-E 이미지 생성 완료: {success}장 성공, {failed}장 실패")

        thread.result_signal.connect(on_result)
        thread.finished_signal.connect(lambda: (
            setattr(self, '_generating', False),
            self.gen_btn.setEnabled(True),
            self.cancel_btn.setEnabled(False),
            self.progress.setVisible(False),
        ))
        thread.start()

    def _cancel_generation(self):
        self._cancel_flag = True
        self.cancel_btn.setEnabled(False)
        self.main.log("🛑 이미지 생성 취소 요청됨...")

    def _refresh_table(self):
        self.image_table.setRowCount(len(self._generated_images))
        for i, img in enumerate(self._generated_images):
            self.image_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.image_table.setItem(i, 1, QTableWidgetItem(img.get('prompt', '')[:60]))
            self.image_table.setItem(i, 2, QTableWidgetItem(img.get('size', '')))
            self.image_table.setItem(i, 3, QTableWidgetItem(img.get('path', '')))
            status = img.get('status', '')
            self.image_table.setItem(i, 4, QTableWidgetItem(f"✅{status}" if status == '완료' else f"❌{status}"))
        self.status_label.setText(f"생성된 이미지: {len(self._generated_images)}개")

    def _open_save_folder(self):
        save_dir = self.save_dir.text().strip()
        if save_dir and os.path.isdir(save_dir):
            import subprocess as sp
            if platform.system() == 'Windows':
                sp.Popen(['explorer', save_dir])
            elif platform.system() == 'Darwin':
                sp.Popen(['open', save_dir])
            else:
                sp.Popen(['xdg-open', save_dir])
        else:
            self.main.log("⚠️ 저장 폴더가 존재하지 않습니다")

    def _copy_to_popular_pool(self):
        """선택된 이미지를 인기글 페이지의 이미지풀에 추가"""
        rows = set(item.row() for item in self.image_table.selectedItems())
        if not rows:
            # 전체 완료된 이미지 추가
            rows = set(i for i, img in enumerate(self._generated_images) if img.get('status') == '완료')
        if not rows:
            self.main.log("⚠️ 추가할 이미지가 없습니다")
            return

        popular_page = self.main.pages.get('popular')
        if not popular_page:
            self.main.log("⚠️ 인기글 페이지를 찾을 수 없습니다")
            return

        count = 0
        for idx in sorted(rows):
            if 0 <= idx < len(self._generated_images):
                img = self._generated_images[idx]
                if img.get('path') and os.path.exists(img['path']):
                    popular_page._image_pool.append({
                        'source': 'dalle',
                        'desc': img.get('prompt', ''),
                        'path': img['path'],
                    })
                    count += 1

        if count > 0:
            popular_page._refresh_image_table()
            self.main.log(f"📤 {count}개 이미지를 인기글 이미지풀에 추가 완료 (총 {len(popular_page._image_pool)}개)")
        else:
            self.main.log("⚠️ 유효한 이미지 파일이 없습니다")

    def _browse_save_dir(self):
        d = QFileDialog.getExistingDirectory(self, "이미지 저장 폴더 선택")
        if d:
            self.save_dir.setText(d)

    def _delete_selected(self):
        rows = set(item.row() for item in self.image_table.selectedItems())
        if not rows:
            self.main.log("⚠️ 삭제할 이미지를 선택하세요")
            return
        for idx in sorted(rows, reverse=True):
            if 0 <= idx < len(self._generated_images):
                self._generated_images.pop(idx)
        self._refresh_table()
        self.main.log(f"🗑️ {len(rows)}개 이미지 삭제됨")

    def _clear_all(self):
        self._generated_images.clear()
        self._refresh_table()
        self.main.log("🗑️ 전체 이미지 목록 삭제됨")

# ═══════════════════════════════════════════════════════════════════════
#  🤖 AI 뉴스 봇 (살구뉴스 자동화) — 통합본
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
#  🤖 AI 뉴스 봇 (살구뉴스 자동화)
#  ─────────────────────────────────────────────────────────────────────────
#  원본 PostPro 프로그램에 통합되는 4개 페이지:
#    1) SalgooConfigPage        ⚙️  설정
#    2) SalgooNewsCollectPage   📥 뉴스 수집  (RSS 우선 → 실패 시 크롤링)
#    3) SalgooAIRewritePage     ✨ AI 재작성  (Perplexity + Gemini)
#    4) SalgooPublishPage       📤 발행       (safe HTML + auto Selenium)
#
#  데이터 저장:
#    - ./salgoo_data/config.json     : 살구뉴스 설정 (네이버 ID/PW, 카테고리)
#    - ./salgoo_data/dedup.sqlite    : 중복 차단 DB
#    - ./salgoo_data/articles.json   : 수집/재작성된 기사 큐
#    - ./salgoo_data/published/      : safe 모드 HTML 파일들
#
#  ⚠️  네이버 자동 임시저장(auto)은 사용자가 한 번 수동 로그인한 뒤
#      그 세션을 그대로 사용합니다 (selenium 일반 모드, undetected 아님).
# ═══════════════════════════════════════════════════════════════════════════

import os
import re
import json
import time
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QComboBox, QSpinBox, QCheckBox, QGroupBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog, QRadioButton,
    QButtonGroup, QProgressBar, QSplitter, QAbstractItemView
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

# ───────────────────────────── 공통 경로 ─────────────────────────────
SALGOO_DIR = Path("salgoo_data")
SALGOO_DIR.mkdir(exist_ok=True)
SALGOO_CONFIG_PATH = SALGOO_DIR / "config.json"
SALGOO_DEDUP_DB = SALGOO_DIR / "dedup.sqlite"
SALGOO_QUEUE_PATH = SALGOO_DIR / "articles.json"
SALGOO_PUBLISHED_DIR = SALGOO_DIR / "published"
SALGOO_PUBLISHED_DIR.mkdir(exist_ok=True)

# 살구뉴스 사이트 (실제 도메인: www.salgoonews.com)
SALGOO_DEFAULT_BASE = "https://www.salgoonews.com"
SALGOO_DEFAULT_RSS = "https://www.salgoonews.com/rss/allArticle.xml"

# 살구뉴스 섹션(카테고리) 코드 — articleList.html?sc_section_code=XXX
SALGOO_SECTIONS = {
    "전체": "",
    "연예": "S1N1",
    "스포츠": "S1N2",
    "정치": "S1N3",
    "경제": "S1N4",
    "사회": "S1N5",
    "문화": "S1N6",
    "IT/과학": "S1N7",
    "국제": "S1N8",
}
SALGOO_DEFAULT_SECTION = "S1N1"  # 연예

# 기본 AI 재작성 프롬프트 (사용자가 설정에서 수정 가능)
DEFAULT_PERPLEX_PROMPT = (
    "다음 뉴스 기사를 사실 확인 및 보강하여 한국어로 재작성해줘.\n"
    "- 원문 출처를 인용\n"
    "- 객관적 톤\n"
    "- 800~1200자\n\n"
    "[제목]\n{title}\n\n[원문]\n{src}"
)
DEFAULT_GEMINI_PROMPT = (
    "아래 뉴스 본문을 블로그 포스팅용으로 매끄럽게 재작성해줘.\n"
    "- 친근한 한국어 톤\n"
    "- 소제목 2~3개 포함 (### 사용)\n"
    "- 1000~1500자\n"
    "- 마지막에 한 줄 요약\n\n"
    "[제목] {title}\n\n[원문]\n{src}"
)


def _load_salgoo_config() -> dict:
    if SALGOO_CONFIG_PATH.exists():
        try:
            return json.loads(SALGOO_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_salgoo_config(cfg: dict) -> None:
    SALGOO_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_queue() -> list:
    if SALGOO_QUEUE_PATH.exists():
        try:
            return json.loads(SALGOO_QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_queue(items: list) -> None:
    SALGOO_QUEUE_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ───────────────────────────── 중복 차단 DB ─────────────────────────────
def _init_dedup_db():
    conn = sqlite3.connect(SALGOO_DEDUP_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            url_hash TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            collected_at TEXT
        )
    """)
    conn.commit()
    return conn


def _is_seen(url: str) -> bool:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    conn = _init_dedup_db()
    cur = conn.execute("SELECT 1 FROM seen WHERE url_hash=?", (h,))
    found = cur.fetchone() is not None
    conn.close()
    return found


def _mark_seen(url: str, title: str) -> None:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    conn = _init_dedup_db()
    conn.execute(
        "INSERT OR IGNORE INTO seen (url_hash, url, title, collected_at) VALUES (?, ?, ?, ?)",
        (h, url, title, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════
#  1) ⚙️  설정 페이지
# ═══════════════════════════════════════════════════════════════════════
class SalgooConfigPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window
        self.cfg = _load_salgoo_config()
        self._build_ui()
        self._load_to_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("⚙️  AI 뉴스 봇 — 설정")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        # ─── 살구뉴스 사이트 설정 ───
        gb_site = QGroupBox("📰 살구뉴스 사이트")
        v_site = QVBoxLayout(gb_site)
        h1 = QHBoxLayout()
        h1.addWidget(QLabel("Base URL:"))
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText(SALGOO_DEFAULT_BASE)
        h1.addWidget(self.base_url)
        v_site.addLayout(h1)

        h2 = QHBoxLayout()
        h2.addWidget(QLabel("RSS URL:"))
        self.rss_url = QLineEdit()
        self.rss_url.setPlaceholderText(SALGOO_DEFAULT_RSS)
        h2.addWidget(self.rss_url)
        v_site.addLayout(h2)

        h2b = QHBoxLayout()
        h2b.addWidget(QLabel("섹션(카테고리):"))
        self.section_combo = QComboBox()
        for name in SALGOO_SECTIONS.keys():
            self.section_combo.addItem(name)
        h2b.addWidget(self.section_combo)
        h2b.addWidget(QLabel("키워드 필터(쉼표, 선택):"))
        self.categories = QLineEdit()
        self.categories.setPlaceholderText("예: BTS, 아이유")
        h2b.addWidget(self.categories)
        v_site.addLayout(h2b)

        root.addWidget(gb_site)

        # ─── AI API 키 ───
        gb_ai = QGroupBox("🤖 AI API 키")
        v_ai = QVBoxLayout(gb_ai)
        h_pp = QHBoxLayout()
        h_pp.addWidget(QLabel("Perplexity API Key:"))
        self.perplex_key = QLineEdit()
        self.perplex_key.setEchoMode(QLineEdit.EchoMode.Password)
        h_pp.addWidget(self.perplex_key)
        v_ai.addLayout(h_pp)

        h_gm = QHBoxLayout()
        h_gm.addWidget(QLabel("Gemini API Key:"))
        self.gemini_key = QLineEdit()
        self.gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        h_gm.addWidget(self.gemini_key)
        v_ai.addLayout(h_gm)

        h_mode = QHBoxLayout()
        h_mode.addWidget(QLabel("재작성 모델 우선순위:"))
        self.ai_priority = QComboBox()
        self.ai_priority.addItems([
            "Perplexity → Gemini (사실확인 + 매끄럽게)",
            "Perplexity 단독",
            "Gemini 단독",
        ])
        h_mode.addWidget(self.ai_priority)
        v_ai.addLayout(h_mode)

        # ─── AI 재작성 프롬프트 (저장됨) ───
        v_ai.addWidget(QLabel("📝 Perplexity 프롬프트 (사실확인/재작성용)"))
        self.perplex_prompt = QTextEdit()
        self.perplex_prompt.setPlaceholderText("{title}, {src} 자리표시자 사용 가능")
        self.perplex_prompt.setMaximumHeight(110)
        v_ai.addWidget(self.perplex_prompt)

        v_ai.addWidget(QLabel("📝 Gemini 프롬프트 (블로그 톤 다듬기용)"))
        self.gemini_prompt = QTextEdit()
        self.gemini_prompt.setPlaceholderText("{title}, {src} 자리표시자 사용 가능")
        self.gemini_prompt.setMaximumHeight(110)
        v_ai.addWidget(self.gemini_prompt)

        h_reset = QHBoxLayout()
        self.reset_prompt_btn = QPushButton("↺ 기본 프롬프트 복원")
        self.reset_prompt_btn.clicked.connect(self._reset_prompts)
        h_reset.addStretch()
        h_reset.addWidget(self.reset_prompt_btn)
        v_ai.addLayout(h_reset)

        root.addWidget(gb_ai)

        # ─── 네이버 발행 계정 ───
        gb_nv = QGroupBox("📤 네이버 블로그 발행 계정 (auto 모드용)")
        v_nv = QVBoxLayout(gb_nv)
        h_id = QHBoxLayout()
        h_id.addWidget(QLabel("네이버 ID:"))
        self.naver_id = QLineEdit()
        h_id.addWidget(self.naver_id)
        v_nv.addLayout(h_id)

        h_pw = QHBoxLayout()
        h_pw.addWidget(QLabel("네이버 PW:"))
        self.naver_pw = QLineEdit()
        self.naver_pw.setEchoMode(QLineEdit.EchoMode.Password)
        h_pw.addWidget(self.naver_pw)
        v_nv.addLayout(h_pw)

        warn = QLabel(
            "⚠️  auto 발행은 [사용자가 1회 수동 로그인 → 세션 재사용] 방식입니다.\n"
            "   비밀번호는 로컬 config.json에만 저장되며 외부 전송되지 않습니다."
        )
        warn.setStyleSheet("color:#FFA500; font-size:11px;")
        warn.setWordWrap(True)
        v_nv.addWidget(warn)
        root.addWidget(gb_nv)

        # ─── 저장 버튼 ───
        h_btn = QHBoxLayout()
        self.save_btn = QPushButton("💾 설정 저장")
        self.save_btn.setStyleSheet(
            "background-color:#FFD700; color:#000; font-weight:bold; padding:8px 16px;"
        )
        self.save_btn.clicked.connect(self.save_config)
        h_btn.addStretch()
        h_btn.addWidget(self.save_btn)
        root.addLayout(h_btn)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(self.status)
        root.addStretch()

    def _load_to_ui(self):
        self.base_url.setText(self.cfg.get("base_url", SALGOO_DEFAULT_BASE))
        self.rss_url.setText(self.cfg.get("rss_url", SALGOO_DEFAULT_RSS))
        self.categories.setText(self.cfg.get("categories", ""))
        # 섹션 복원
        saved_section = self.cfg.get("section_name", "연예")
        idx = self.section_combo.findText(saved_section)
        if idx >= 0:
            self.section_combo.setCurrentIndex(idx)
        self.perplex_key.setText(self.cfg.get("perplex_key", ""))
        self.gemini_key.setText(self.cfg.get("gemini_key", ""))
        self.ai_priority.setCurrentIndex(self.cfg.get("ai_priority", 0))
        self.naver_id.setText(self.cfg.get("naver_id", ""))
        self.naver_pw.setText(self.cfg.get("naver_pw", ""))
        self.perplex_prompt.setPlainText(self.cfg.get("perplex_prompt", DEFAULT_PERPLEX_PROMPT))
        self.gemini_prompt.setPlainText(self.cfg.get("gemini_prompt", DEFAULT_GEMINI_PROMPT))

    def _reset_prompts(self):
        self.perplex_prompt.setPlainText(DEFAULT_PERPLEX_PROMPT)
        self.gemini_prompt.setPlainText(DEFAULT_GEMINI_PROMPT)
        self.status.setText("프롬프트를 기본값으로 복원했습니다. (저장 버튼을 눌러 적용)")

    def save_config(self):
        section_name = self.section_combo.currentText()
        section_code = SALGOO_SECTIONS.get(section_name, "")
        self.cfg = {
            "base_url": self.base_url.text().strip() or SALGOO_DEFAULT_BASE,
            "rss_url": self.rss_url.text().strip() or SALGOO_DEFAULT_RSS,
            "categories": self.categories.text().strip(),
            "section_name": section_name,
            "section_code": section_code,
            "perplex_key": self.perplex_key.text().strip(),
            "gemini_key": self.gemini_key.text().strip(),
            "ai_priority": self.ai_priority.currentIndex(),
            "naver_id": self.naver_id.text().strip(),
            "naver_pw": self.naver_pw.text().strip(),
            "perplex_prompt": self.perplex_prompt.toPlainText().strip() or DEFAULT_PERPLEX_PROMPT,
            "gemini_prompt": self.gemini_prompt.toPlainText().strip() or DEFAULT_GEMINI_PROMPT,
        }
        _save_salgoo_config(self.cfg)
        self.status.setText(f"✅ 저장됨 — {datetime.now().strftime('%H:%M:%S')}")
        if self.main:
            self.main.log(f"[살구봇] ⚙️  설정 저장 완료 (섹션: {section_name})")


# ═══════════════════════════════════════════════════════════════════════
#  2) 📥 뉴스 수집 페이지   (RSS 우선 → 크롤링 폴백)
# ═══════════════════════════════════════════════════════════════════════
class _CollectThread(QThread):
    progress = pyqtSignal(str)
    finished_with = pyqtSignal(list)  # list of dict

    def __init__(self, cfg: dict, max_count: int, skip_dup: bool):
        super().__init__()
        self.cfg = cfg
        self.max_count = max_count
        self.skip_dup = skip_dup

    def run(self):
        items = []
        # 도메인 자동 교정: 티스토리/잘못된 도메인이면 salgoonews.com으로 강제
        base = (self.cfg.get("base_url") or "").lower()
        if "salgoonews.com" not in base:
            self.progress.emit(f"[살구봇] ⚠️  base_url 교정: {base or '(빈값)'} → {SALGOO_DEFAULT_BASE}")
            self.cfg["base_url"] = SALGOO_DEFAULT_BASE
            self.cfg["rss_url"] = SALGOO_DEFAULT_RSS
        # rss_url도 도메인 검사
        rss = (self.cfg.get("rss_url") or "").lower()
        if "salgoonews.com" not in rss:
            self.cfg["rss_url"] = SALGOO_DEFAULT_RSS

        section_code = self.cfg.get("section_code", "")
        try:
            # 섹션이 지정되어 있으면 HTML 크롤링 우선 (RSS는 섹션 필터링 불가)
            if section_code:
                self.progress.emit(f"[살구봇] 섹션 지정됨({section_code}) → HTML 크롤링 우선")
                items = self._try_crawl()
                if not items:
                    self.progress.emit("[살구봇] 크롤링 0건 → RSS 폴백")
                    items = self._try_rss()
            else:
                items = self._try_rss()
                if not items:
                    self.progress.emit("[살구봇] RSS 비어있음 → HTML 크롤링 시도")
                    items = self._try_crawl()
        except Exception as e:
            self.progress.emit(f"[살구봇] ❌ 수집 오류: {e}")

        # 카테고리 필터
        cats = [c.strip() for c in self.cfg.get("categories", "").split(",") if c.strip()]
        if cats:
            items = [
                it for it in items
                if any(c in (it.get("category", "") + it.get("title", "")) for c in cats)
            ]

        # 중복 차단
        if self.skip_dup:
            items = [it for it in items if not _is_seen(it["url"])]

        # 본문 보강 (없을 시) — 진행률/경과시간/타임아웃 표시
        targets = items[: self.max_count]
        total = len(targets)
        self.progress.emit(f"[살구봇] 📝 본문 수집 시작: {total}건")
        out = []
        for i, it in enumerate(targets, 1):
            t0 = time.time()
            self.progress.emit(f"[살구봇] ({i}/{total}) ⏬ 본문 요청… {it['title'][:40]}")
            if not it.get("content"):
                try:
                    it["content"] = self._extract_body(it["url"])
                except requests.Timeout:
                    self.progress.emit(f"[살구봇] ({i}/{total}) ⏱ 타임아웃 — 스킵: {it['url']}")
                    it["content"] = it.get("summary", "")
                except Exception as e:
                    self.progress.emit(f"[살구봇] ({i}/{total}) ❌ 본문 추출 실패: {e}")
                    it["content"] = it.get("summary", "")
            elapsed = time.time() - t0
            out.append(it)
            self.progress.emit(
                f"[살구봇] ({i}/{total}) ✅ 완료 ({elapsed:.1f}s, {len(it.get('content',''))}자) — {it['title'][:30]}"
            )
            QApplication.processEvents()  # UI 멈춤 방지

        # 표시 후 seen 마킹은 발행 시점이 아니라 수집 시점에 한다 (요청 시 변경 가능)
        for it in out:
            _mark_seen(it["url"], it["title"])

        self.finished_with.emit(out)

    def _try_rss(self) -> list:
        base_rss = self.cfg.get("rss_url") or SALGOO_DEFAULT_RSS
        section_code = self.cfg.get("section_code", "")
        # 살구뉴스 섹션별 RSS: ?section=S1N1 형태 시도
        urls_to_try = []
        if section_code:
            base = (self.cfg.get("base_url") or SALGOO_DEFAULT_BASE).rstrip("/")
            urls_to_try.append(f"{base}/rss/S1N{section_code[-1]}.xml")
            urls_to_try.append(f"{base}/rss/clickTop.xml")
        urls_to_try.append(base_rss)

        items = []
        for url in urls_to_try:
            self.progress.emit(f"[살구봇] 📡 RSS 요청: {url}")
            try:
                feed = feedparser.parse(url)
                for e in feed.entries:
                    items.append({
                        "title": getattr(e, "title", "").strip(),
                        "url": getattr(e, "link", "").strip(),
                        "summary": BeautifulSoup(getattr(e, "summary", ""), "html.parser").get_text(" ", strip=True),
                        "category": getattr(e, "category", "") if hasattr(e, "category") else "",
                        "published": getattr(e, "published", ""),
                        "source": "RSS",
                        "content": "",
                    })
                if items:
                    self.progress.emit(f"[살구봇] RSS 수신: {len(items)}건")
                    return items
            except Exception as ex:
                self.progress.emit(f"[살구봇] RSS 실패 ({url}): {ex}")
        self.progress.emit(f"[살구봇] RSS 수신: 0건")
        return items

    def _try_crawl(self) -> list:
        base = (self.cfg.get("base_url") or SALGOO_DEFAULT_BASE).rstrip("/")
        section_code = self.cfg.get("section_code", "")
        # 살구뉴스 섹션별 기사 목록 페이지
        if section_code:
            list_url = f"{base}/news/articleList.html?sc_section_code={section_code}&view_type=sm"
        else:
            list_url = f"{base}/news/articleList.html?view_type=sm"
        self.progress.emit(f"[살구봇] 🕸  크롤링: {list_url}")
        r = requests.get(list_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        items, seen_url = [], set()
        # 살구뉴스(그누보드/uPress 계열)는 articleView.html?idxno=XXXX 패턴
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "articleView.html" not in href:
                continue
            full = urljoin(base + "/", href)
            if full in seen_url:
                continue
            title = a.get_text(" ", strip=True)
            if len(title) < 5:
                continue
            seen_url.add(full)
            items.append({
                "title": title, "url": full, "summary": "",
                "category": self.cfg.get("section_name", ""), "published": "",
                "source": "CRAWL", "content": "",
            })
        self.progress.emit(f"[살구봇] 크롤링 후보: {len(items)}건")
        return items

    def _extract_body(self, url: str) -> str:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        # 흔한 본문 컨테이너 후보
        for sel in ["#article_view", ".article_view", "#articleBody",
                    ".article-body", "article", ".view_con", "#news_body_area"]:
            node = soup.select_one(sel)
            if node:
                return node.get_text("\n", strip=True)
        return soup.get_text(" ", strip=True)[:3000]


class SalgooNewsCollectPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window
        self.collected = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("📥 살구뉴스 수집")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        # 옵션
        h_opt = QHBoxLayout()
        h_opt.addWidget(QLabel("수집 개수:"))
        self.max_count = QSpinBox()
        self.max_count.setRange(1, 100)
        self.max_count.setValue(10)
        h_opt.addWidget(self.max_count)

        self.skip_dup = QCheckBox("중복 차단 (이미 수집한 URL 제외)")
        self.skip_dup.setChecked(True)
        h_opt.addWidget(self.skip_dup)
        h_opt.addStretch()
        root.addLayout(h_opt)

        # 버튼
        h_btn = QHBoxLayout()
        self.collect_btn = QPushButton("🚀 수집 시작")
        self.collect_btn.setStyleSheet(
            "background-color:#FFD700; color:#000; font-weight:bold; padding:8px 16px;"
        )
        self.collect_btn.clicked.connect(self.start_collect)
        h_btn.addWidget(self.collect_btn)

        self.send_btn = QPushButton("➡️  AI 재작성으로 보내기")
        self.send_btn.clicked.connect(self.send_to_rewrite)
        h_btn.addWidget(self.send_btn)
        h_btn.addStretch()
        root.addLayout(h_btn)

        # 결과 테이블
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["선택", "제목", "카테고리", "URL"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        self.status = QLabel("대기 중...")
        self.status.setStyleSheet("color:#888;")
        root.addWidget(self.status)

    def start_collect(self):
        cfg = _load_salgoo_config()
        if not cfg:
            QMessageBox.warning(self, "설정 필요", "먼저 ⚙️ 설정 페이지에서 사이트 정보를 저장해주세요.")
            return
        self.collect_btn.setEnabled(False)
        self.status.setText("수집 중...")
        self._thread = _CollectThread(cfg, self.max_count.value(), self.skip_dup.isChecked())
        self._thread.progress.connect(lambda m: self.main.log(m) if self.main else None)
        self._thread.finished_with.connect(self._on_done)
        self._thread.start()

    def _on_done(self, items: list):
        self.collected = items
        self.table.setRowCount(0)
        for it in items:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cb = QCheckBox()
            cb.setChecked(True)
            self.table.setCellWidget(r, 0, cb)
            self.table.setItem(r, 1, QTableWidgetItem(it["title"]))
            self.table.setItem(r, 2, QTableWidgetItem(it.get("category", "")))
            self.table.setItem(r, 3, QTableWidgetItem(it["url"]))
        self.status.setText(f"✅ 수집 완료: {len(items)}건")
        self.collect_btn.setEnabled(True)

    def send_to_rewrite(self):
        picked = []
        for r in range(self.table.rowCount()):
            cb = self.table.cellWidget(r, 0)
            if cb and cb.isChecked():
                picked.append(self.collected[r])
        if not picked:
            QMessageBox.information(self, "선택 없음", "전송할 기사를 체크해주세요.")
            return
        # 큐에 추가 (status='collected')
        queue = _load_queue()
        for it in picked:
            it["status"] = "collected"
            it["collected_at"] = datetime.now().isoformat(timespec="seconds")
            queue.append(it)
        _save_queue(queue)
        QMessageBox.information(self, "전송 완료", f"{len(picked)}건이 ✨ AI 재작성 큐에 추가되었습니다.")
        if self.main:
            self.main.log(f"[살구봇] ➡️  재작성 큐로 전송: {len(picked)}건")


# ═══════════════════════════════════════════════════════════════════════
#  3) ✨ AI 재작성 페이지   (Perplexity + Gemini)
# ═══════════════════════════════════════════════════════════════════════
class _RewriteThread(QThread):
    progress = pyqtSignal(str)
    one_done = pyqtSignal(int, str)  # (index, html)
    all_done = pyqtSignal()

    def __init__(self, cfg: dict, items: list, indices: list):
        super().__init__()
        self.cfg = cfg
        self.items = items
        self.indices = indices

    def run(self):
        priority = self.cfg.get("ai_priority", 0)
        for i in self.indices:
            it = self.items[i]
            try:
                src = it.get("content") or it.get("summary") or it.get("title", "")
                self.progress.emit(f"[살구봇] ✨ 재작성: {it['title'][:40]}")
                if priority == 0:
                    pp = self._perplexity(it["title"], src)
                    final = self._gemini_polish(it["title"], pp) if pp else self._gemini_polish(it["title"], src)
                elif priority == 1:
                    final = self._perplexity(it["title"], src) or src
                else:
                    final = self._gemini_polish(it["title"], src)

                html = self._to_html(it["title"], final, it.get("url", ""))
                self.one_done.emit(i, html)
            except Exception as e:
                self.progress.emit(f"[살구봇] ❌ {it['title'][:30]} — {e}")
        self.all_done.emit()

    def _perplexity(self, title: str, src: str) -> str:
        key = self.cfg.get("perplex_key", "")
        if not key:
            self.progress.emit("[살구봇] ⚠️  Perplexity 키 없음 — 건너뜀")
            return ""
        tpl = self.cfg.get("perplex_prompt") or DEFAULT_PERPLEX_PROMPT
        try:
            prompt = tpl.format(title=title, src=src[:4000])
        except Exception:
            # 사용자가 {} 치환자를 빠뜨렸거나 잘못 쓴 경우 — 원문을 끝에 덧붙임
            prompt = f"{tpl}\n\n[제목]\n{title}\n\n[원문]\n{src[:4000]}"
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "sonar",
                "messages": [
                    {"role": "system", "content": "정확하고 간결한 한국어 뉴스 기자."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    def _gemini_polish(self, title: str, src: str) -> str:
        key = self.cfg.get("gemini_key", "")
        if not key:
            self.progress.emit("[살구봇] ⚠️  Gemini 키 없음 — 원문 사용")
            return src
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash-exp:generateContent?key={key}"
        )
        tpl = self.cfg.get("gemini_prompt") or DEFAULT_GEMINI_PROMPT
        try:
            prompt = tpl.format(title=title, src=src[:6000])
        except Exception:
            prompt = f"{tpl}\n\n[제목] {title}\n\n[원문]\n{src[:6000]}"
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def _to_html(self, title: str, body: str, src_url: str) -> str:
        # 매우 단순한 마크다운 → HTML 변환
        html_body = body
        html_body = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html_body, flags=re.M)
        html_body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html_body, flags=re.M)
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        paragraphs = [
            f"<p>{p.strip()}</p>" if not p.strip().startswith("<") else p
            for p in html_body.split("\n\n") if p.strip()
        ]
        body_html = "\n".join(paragraphs)
        src_line = f'<p style="font-size:12px;color:#888;">출처: <a href="{src_url}">{src_url}</a></p>' if src_url else ""
        return f"<h1>{title}</h1>\n{body_html}\n{src_line}"


class SalgooAIRewritePage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window
        self._build_ui()
        self.refresh_queue()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("✨ AI 재작성 (Perplexity + Gemini)")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        h_btn = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 큐 새로고침")
        self.refresh_btn.clicked.connect(self.refresh_queue)
        h_btn.addWidget(self.refresh_btn)

        self.rewrite_btn = QPushButton("✨ 선택 항목 재작성")
        self.rewrite_btn.setStyleSheet(
            "background-color:#FFD700; color:#000; font-weight:bold; padding:8px 16px;"
        )
        self.rewrite_btn.clicked.connect(self.start_rewrite)
        h_btn.addWidget(self.rewrite_btn)

        self.delete_btn = QPushButton("🗑 선택 항목 삭제")
        self.delete_btn.setStyleSheet("background-color:#8a2d2d; color:#fff; padding:8px 12px;")
        self.delete_btn.clicked.connect(self.delete_selected)
        h_btn.addWidget(self.delete_btn)
        h_btn.addStretch()

        info_lbl = QLabel(
            "💡 이미 재작성된 글은 기본적으로 체크 해제되어 자동 재생성되지 않습니다. "
            "다시 만들고 싶으면 직접 체크 후 ✨ 버튼을 누르세요. (큐는 프로그램 재시작 후에도 유지됨)"
        )
        info_lbl.setStyleSheet("color:#888; font-size:11px; padding:4px;")
        info_lbl.setWordWrap(True)
        root.addLayout(h_btn)
        root.addWidget(info_lbl)

        split = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["선택", "제목", "상태"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_select_row)
        split.addWidget(self.table)

        self.preview = QTextEdit()
        self.preview.setPlaceholderText("재작성된 HTML 미리보기")
        split.addWidget(self.preview)

        split.setSizes([500, 700])
        root.addWidget(split, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888;")
        root.addWidget(self.status)

    def refresh_queue(self):
        from PyQt6.QtGui import QColor
        self.queue = _load_queue()
        self.table.setRowCount(0)
        cnt_collected = cnt_rewritten = cnt_published = 0
        for it in self.queue:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cb = QCheckBox()
            status = it.get("status", "")
            # 기본 체크: 아직 재작성 안 된 collected 항목만 (rewritten은 자동 재생성 방지)
            cb.setChecked(status == "collected")
            self.table.setCellWidget(r, 0, cb)
            title_item = QTableWidgetItem(it.get("title", ""))
            status_item = QTableWidgetItem(status)
            # 상태별 색상
            if status == "rewritten":
                status_item.setForeground(QColor("#FFD700"))
                cnt_rewritten += 1
            elif status.startswith("published"):
                status_item.setForeground(QColor("#4caf50"))
                cnt_published += 1
            elif status == "collected":
                status_item.setForeground(QColor("#aaa"))
                cnt_collected += 1
            self.table.setItem(r, 1, title_item)
            self.table.setItem(r, 2, status_item)
        self.status.setText(
            f"큐 항목: {len(self.queue)}건  "
            f"(📥 수집됨 {cnt_collected} / ✨ 재작성됨 {cnt_rewritten} / 📤 발행됨 {cnt_published})"
        )

    def delete_selected(self):
        indices = []
        for r in range(self.table.rowCount()):
            cb = self.table.cellWidget(r, 0)
            if cb and cb.isChecked():
                indices.append(r)
        if not indices:
            QMessageBox.information(self, "선택 없음", "삭제할 항목을 체크해주세요.")
            return
        ans = QMessageBox.question(
            self, "삭제 확인",
            f"체크된 {len(indices)}건을 큐에서 영구 삭제할까요?\n"
            "(재작성된 HTML도 함께 사라집니다)"
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        # 뒤에서부터 제거
        for i in sorted(indices, reverse=True):
            del self.queue[i]
        _save_queue(self.queue)
        self.refresh_queue()
        if self.main:
            self.main.log(f"[살구봇] 🗑 큐 삭제: {len(indices)}건")

    def _on_select_row(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if 0 <= i < len(self.queue):
            html = self.queue[i].get("rewritten_html", "")
            self.preview.setHtml(html or "<i>아직 재작성되지 않음</i>")
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if 0 <= i < len(self.queue):
            html = self.queue[i].get("rewritten_html", "")
            self.preview.setHtml(html or "<i>아직 재작성되지 않음</i>")

    def start_rewrite(self):
        cfg = _load_salgoo_config()
        if not cfg.get("perplex_key") and not cfg.get("gemini_key"):
            QMessageBox.warning(self, "API 키 필요", "⚙️ 설정에서 Perplexity 또는 Gemini 키를 입력해주세요.")
            return
        indices = []
        for r in range(self.table.rowCount()):
            cb = self.table.cellWidget(r, 0)
            if cb and cb.isChecked():
                indices.append(r)
        if not indices:
            QMessageBox.information(self, "선택 없음", "재작성할 항목을 체크해주세요.")
            return
        self.rewrite_btn.setEnabled(False)
        self.status.setText(f"재작성 중... ({len(indices)}건)")
        self._thread = _RewriteThread(cfg, self.queue, indices)
        self._thread.progress.connect(lambda m: self.main.log(m) if self.main else None)
        self._thread.one_done.connect(self._one_done)
        self._thread.all_done.connect(self._all_done)
        self._thread.start()

    def _one_done(self, i: int, html: str):
        self.queue[i]["rewritten_html"] = html
        self.queue[i]["status"] = "rewritten"
        self.queue[i]["rewritten_at"] = datetime.now().isoformat(timespec="seconds")
        _save_queue(self.queue)
        self.table.setItem(i, 2, QTableWidgetItem("rewritten"))
        # 미리보기 즉시 갱신: 현재 선택된 행이거나 첫 완료 항목일 때
        try:
            sel_rows = self.table.selectionModel().selectedRows()
            cur_idx = sel_rows[0].row() if sel_rows else -1
            if cur_idx == i or cur_idx < 0:
                # 행 선택을 강제하여 미리보기 표시
                self.table.selectRow(i)
                preview_html = html if html and html.strip() else "<i>재작성 결과가 비어있습니다</i>"
                self.preview.setHtml(preview_html)
                if self.main:
                    self.main.log(f"[살구봇] 👁  미리보기 갱신: {self.queue[i].get('title','')[:30]} ({len(html)}자)")
        except Exception as e:
            if self.main:
                self.main.log(f"[살구봇] ⚠️  미리보기 갱신 실패: {e}")

    def _all_done(self):
        self.rewrite_btn.setEnabled(True)
        self.status.setText("✅ 재작성 완료")
        if self.main:
            self.main.log("[살구봇] ✨ 재작성 완료")


# ═══════════════════════════════════════════════════════════════════════
#  4) 📤 발행 페이지   (safe HTML 저장 + auto Selenium)
# ═══════════════════════════════════════════════════════════════════════
class _PublishAutoThread(QThread):
    progress = pyqtSignal(str)
    one_done = pyqtSignal(int, bool)  # (index, ok)
    all_done = pyqtSignal()

    def __init__(self, cfg: dict, items: list, indices: list):
        super().__init__()
        self.cfg = cfg
        self.items = items
        self.indices = indices

    def run(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            self.progress.emit("[살구봇] ❌ selenium 미설치 — pip install selenium")
            self.all_done.emit()
            return

        # 사용자 프로필 폴더 (세션 유지)
        profile_dir = (SALGOO_DIR / "chrome_profile").absolute()
        profile_dir.mkdir(exist_ok=True)
        opts = Options()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--start-maximized")
        # v12: excludeSwitches는 현재 ChromeDriver에서 unrecognized chrome option 오류를 내므로 사용하지 않음

        try:
            driver = webdriver.Chrome(options=opts)
        except Exception as e:
            self.progress.emit(f"[살구봇] ❌ Chrome 드라이버 시작 실패: {e}")
            self.all_done.emit()
            return

        try:
            # 네이버 메인 → 사용자가 수동 로그인 (세션 없으면)
            driver.get("https://www.naver.com")
            LOGIN_WAIT = 120  # 2분
            self.progress.emit(
                f"[살구봇] ⏳ {LOGIN_WAIT}초(2분) 대기 — 우측 상단에서 네이버 로그인 해주세요.\n"
                "          (이미 로그인되어 있으면 자동 감지되어 빠르게 넘어갑니다)"
            )
            # 10초마다 로그인 여부 체크 + 카운트다운
            elapsed = 0
            logged_in = False
            while elapsed < LOGIN_WAIT:
                try:
                    # 로그인 시 .MyView-module__link_login 또는 .MyView-module__my_info 등 표시
                    page = driver.page_source
                    if ('class="MyView-module__link_logout' in page
                            or 'gnb_my_namebox' in page
                            or 'MyView-module__my_menu' in page):
                        logged_in = True
                        self.progress.emit(f"[살구봇] ✅ 로그인 감지됨 ({elapsed}초 경과) — 발행 시작")
                        break
                except Exception:
                    pass
                remain = LOGIN_WAIT - elapsed
                self.progress.emit(f"[살구봇] ⏳ 로그인 대기 중... {remain}초 남음")
                time.sleep(10)
                elapsed += 10
            if not logged_in:
                self.progress.emit("[살구봇] ⚠️  로그인 미감지 — 그래도 발행 진행 시도")

            for i in self.indices:
                it = self.items[i]
                html = (it.get("rewritten_html") or it.get("rewritten")
                        or it.get("content") or "")
                title = it.get("title", "제목없음")
                if not html:
                    self.progress.emit(f"[살구봇] ⚠️  본문 없음 (rewritten_html/content 모두 비어있음) — 건너뜀: {title[:30]}")
                    self.one_done.emit(i, False)
                    continue
                try:
                    # 네이버 블로그 글쓰기 페이지
                    driver.get("https://blog.naver.com/GoBlogWrite.naver")
                    self.progress.emit(f"[살구봇] 📤 글쓰기 페이지 진입: {title[:30]}")
                    time.sleep(5)

                    # iframe 진입 시도 (네이버 블로그는 iframe 다층 구조)
                    try:
                        driver.switch_to.frame("mainFrame")
                    except Exception:
                        pass

                    # 클립보드를 통한 붙여넣기 방식 (가장 안정)
                    try:
                        import pyperclip
                        pyperclip.copy(f"{title}\n\n{re.sub('<[^<]+?>', '', html)}")
                        self.progress.emit(
                            f"[살구봇] 📋 클립보드 복사 완료. "
                            f"제목/본문란에 Ctrl+V 로 붙여넣고 임시저장해주세요."
                        )
                    except Exception as e:
                        self.progress.emit(f"[살구봇] pyperclip 오류: {e}")

                    # 사용자가 확인할 시간
                    time.sleep(20)
                    self.one_done.emit(i, True)
                except Exception as e:
                    self.progress.emit(f"[살구봇] ❌ 발행 실패 {title[:30]}: {e}")
                    self.one_done.emit(i, False)

                driver.switch_to.default_content()
        finally:
            try:
                driver.quit()
            except Exception:
                pass
            self.all_done.emit()


class SalgooPublishPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window
        self._build_ui()
        self.refresh_queue()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("📤 발행")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        # 모드 선택
        gb_mode = QGroupBox("발행 모드")
        v = QVBoxLayout(gb_mode)
        self.mode_group = QButtonGroup(self)
        self.rb_safe = QRadioButton("🟢 safe — HTML 파일로 저장 (사람이 복붙)")
        self.rb_safe.setChecked(True)
        self.rb_auto = QRadioButton(
            "🟡 auto — Selenium 으로 네이버 글쓰기 페이지 자동 진입 + 클립보드 붙여넣기 보조"
        )
        self.rb_manual_type = QRadioButton(
            "✍️  manual_type — 직접 글쓰듯이 자동 타이핑 (제목 입력 → Tab → 본문 → 임시저장)"
        )
        self.mode_group.addButton(self.rb_safe)
        self.mode_group.addButton(self.rb_auto)
        self.mode_group.addButton(self.rb_manual_type)
        v.addWidget(self.rb_safe)
        v.addWidget(self.rb_auto)
        v.addWidget(self.rb_manual_type)

        # manual_type 옵션
        h_opt = QHBoxLayout()
        self.cb_publish_after_type = QCheckBox("타이핑 후 자동 발행까지 진행 (체크 해제 시 임시저장만)")
        self.cb_publish_after_type.setChecked(False)
        h_opt.addWidget(self.cb_publish_after_type)
        h_opt.addStretch()
        v.addLayout(h_opt)

        info = QLabel(
            "• safe: ./salgoo_data/published/ 폴더에 HTML 파일이 생성됩니다.\n"
            "• auto: Chrome 창이 열립니다 → 2분(120초) 안에 네이버 로그인 → 자동으로 글쓰기 페이지 이동 → Ctrl+V 안내. (로그인 감지 시 즉시 진행)\n"
            "• manual_type: 네이버 로그인(2분) → 글쓰기 → 임시저장 팝업 '취소' → 제목(20자) 자동입력 → Tab → 본문 자동타이핑 → 임시저장 (옵션: 자동 발행)."
        )
        info.setStyleSheet("color:#888; font-size:11px;")
        info.setWordWrap(True)
        v.addWidget(info)
        root.addWidget(gb_mode)

        # 버튼
        h_btn = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 큐 새로고침")
        self.refresh_btn.clicked.connect(self.refresh_queue)
        h_btn.addWidget(self.refresh_btn)

        self.publish_btn = QPushButton("🚀 선택 항목 발행")
        self.publish_btn.setStyleSheet(
            "background-color:#FFD700; color:#000; font-weight:bold; padding:8px 16px;"
        )
        self.publish_btn.clicked.connect(self.start_publish)
        h_btn.addWidget(self.publish_btn)

        self.open_folder_btn = QPushButton("📂 발행 폴더 열기")
        self.open_folder_btn.clicked.connect(self.open_folder)
        h_btn.addWidget(self.open_folder_btn)

        self.delete_btn = QPushButton("🗑 선택 항목 삭제")
        self.delete_btn.setStyleSheet("background-color:#8a2d2d; color:#fff; padding:8px 12px;")
        self.delete_btn.clicked.connect(self.delete_selected)
        h_btn.addWidget(self.delete_btn)
        h_btn.addStretch()
        root.addLayout(h_btn)

        info_lbl = QLabel(
            "💡 발행 못한 글(✨ 재작성됨 상태)은 프로그램을 껐다 켜도 그대로 남아있어 "
            "다시 발행할 수 있습니다. 이미 발행된 글은 자동 체크되지 않습니다."
        )
        info_lbl.setStyleSheet("color:#888; font-size:11px; padding:4px;")
        info_lbl.setWordWrap(True)
        root.addWidget(info_lbl)

        # 테이블
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["선택", "제목", "상태"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888;")
        root.addWidget(self.status)

    def refresh_queue(self):
        from PyQt6.QtGui import QColor
        self.queue = _load_queue()
        self.table.setRowCount(0)
        cnt_rewritten = cnt_published = cnt_collected = 0
        for it in self.queue:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cb = QCheckBox()
            status = it.get("status", "")
            # 발행 못한 재작성된 글만 자동 체크 (이미 발행된 건 체크 안 함)
            cb.setChecked(status == "rewritten")
            self.table.setCellWidget(r, 0, cb)
            status_item = QTableWidgetItem(status)
            if status == "rewritten":
                status_item.setForeground(QColor("#FFD700"))
                cnt_rewritten += 1
            elif status.startswith("published"):
                status_item.setForeground(QColor("#4caf50"))
                cnt_published += 1
            elif status == "collected":
                status_item.setForeground(QColor("#aaa"))
                cnt_collected += 1
            self.table.setItem(r, 1, QTableWidgetItem(it.get("title", "")))
            self.table.setItem(r, 2, status_item)
        self.status.setText(
            f"큐 항목: {len(self.queue)}건  "
            f"(📥 수집됨 {cnt_collected} / ✨ 발행대기 {cnt_rewritten} / ✅ 발행됨 {cnt_published})"
        )

    def delete_selected(self):
        indices = []
        for r in range(self.table.rowCount()):
            cb = self.table.cellWidget(r, 0)
            if cb and cb.isChecked():
                indices.append(r)
        if not indices:
            QMessageBox.information(self, "선택 없음", "삭제할 항목을 체크해주세요.")
            return
        ans = QMessageBox.question(
            self, "삭제 확인",
            f"체크된 {len(indices)}건을 큐에서 영구 삭제할까요?"
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        for i in sorted(indices, reverse=True):
            del self.queue[i]
        _save_queue(self.queue)
        self.refresh_queue()
        if self.main:
            self.main.log(f"[살구봇] 🗑 큐 삭제: {len(indices)}건")

    def open_folder(self):
        import platform, subprocess
        path = str(SALGOO_PUBLISHED_DIR.absolute())
        try:
            if platform.system() == "Windows":
                os.startfile(path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "열기 실패", str(e))

    def start_publish(self):
        indices = []
        for r in range(self.table.rowCount()):
            cb = self.table.cellWidget(r, 0)
            if cb and cb.isChecked():
                indices.append(r)
        if not indices:
            QMessageBox.information(self, "선택 없음", "발행할 항목을 체크해주세요.")
            return

        if self.rb_safe.isChecked():
            self._publish_safe(indices)
        elif self.rb_manual_type.isChecked():
            self._publish_manual_type(indices)
        else:
            self._publish_auto(indices)

    def _publish_safe(self, indices):
        ok = 0
        for i in indices:
            it = self.queue[i]
            html = (it.get("rewritten_html") or it.get("rewritten")
                    or it.get("content") or "")
            if not html:
                continue
            safe_title = re.sub(r"[\\/:*?\"<>|]", "_", it.get("title", "untitled"))[:60]
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fp = SALGOO_PUBLISHED_DIR / f"{ts}_{safe_title}.html"
            full = (
                f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>{it.get('title','')}</title></head><body>{html}</body></html>"
            )
            fp.write_text(full, encoding="utf-8")
            self.queue[i]["status"] = "published_safe"
            self.queue[i]["published_path"] = str(fp)
            self.table.setItem(i, 2, QTableWidgetItem("published_safe"))
            ok += 1
            if self.main:
                self.main.log(f"[살구봇] 💾 저장: {fp.name}")
        _save_queue(self.queue)
        QMessageBox.information(self, "완료", f"safe 모드: {ok}건 저장 완료\n→ {SALGOO_PUBLISHED_DIR}")

    def _publish_auto(self, indices):
        cfg = _load_salgoo_config()
        ans = QMessageBox.question(
            self, "auto 발행 확인",
            "Chrome 창이 열립니다.\n"
            "1) 2분(120초) 안에 네이버 로그인 (로그인 감지되면 즉시 다음 단계 진행)\n"
            "2) 글쓰기 페이지로 자동 이동\n"
            "3) 클립보드에 본문이 복사됩니다 → 제목/본문에 Ctrl+V → 임시저장\n\n진행할까요?",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.publish_btn.setEnabled(False)
        self._thread = _PublishAutoThread(cfg, self.queue, indices)
        self._thread.progress.connect(lambda m: self.main.log(m) if self.main else None)
        self._thread.one_done.connect(self._one_done_auto)
        self._thread.all_done.connect(self._all_done_auto)
        self._thread.start()

    def _one_done_auto(self, i: int, ok: bool):
        if ok:
            self.queue[i]["status"] = "published_auto"
            self.table.setItem(i, 2, QTableWidgetItem("published_auto"))
        _save_queue(self.queue)

    def _all_done_auto(self):
        self.publish_btn.setEnabled(True)
        self.status.setText("✅ auto 발행 시퀀스 완료")
        if self.main:
            self.main.log("[살구봇] 📤 auto 발행 완료")

    # ───────── manual_type (직접 타이핑) 모드 ─────────
    def _publish_manual_type(self, indices):
        cfg = _load_salgoo_config()
        publish_after = self.cb_publish_after_type.isChecked()
        ans = QMessageBox.question(
            self, "manual_type 발행 확인",
            "Chrome 창이 열립니다.\n\n"
            "1) 2분(120초) 안에 네이버 로그인 (감지되면 즉시 진행)\n"
            "2) 글쓰기 페이지로 자동 이동\n"
            "3) '임시저장된 글 불러오기' 팝업이 뜨면 자동으로 '취소' 클릭\n"
            "4) 제목(20자 이내) 자동 타이핑 → Tab → 본문 자동 타이핑 (글자당 30~50ms)\n"
            f"5) {'임시저장 + 자동 발행' if publish_after else '임시저장만 수행'}\n\n진행할까요?",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.publish_btn.setEnabled(False)
        self._thread = _PublishManualTypeThread(cfg, self.queue, indices, publish_after)
        self._thread.progress.connect(lambda m: self.main.log(m) if self.main else None)
        self._thread.one_done.connect(self._one_done_manual_type)
        self._thread.all_done.connect(self._all_done_manual_type)
        self._thread.start()

    def _one_done_manual_type(self, i: int, ok: bool, published: bool):
        if ok:
            self.queue[i]["status"] = "published_manual" if published else "draft_saved"
            self.table.setItem(i, 2, QTableWidgetItem(self.queue[i]["status"]))
        _save_queue(self.queue)

    def _all_done_manual_type(self):
        self.publish_btn.setEnabled(True)
        self.status.setText("✅ manual_type 시퀀스 완료")
        if self.main:
            self.main.log("[살구봇] ✍️  manual_type 완료")


# ═══════════════════════════════════════════════════════════════════════
#  manual_type 발행 스레드
# ═══════════════════════════════════════════════════════════════════════
class _PublishManualTypeThread(QThread):
    progress = pyqtSignal(str)
    one_done = pyqtSignal(int, bool, bool)  # (index, ok, published)
    all_done = pyqtSignal()

    def __init__(self, cfg: dict, items: list, indices: list, publish_after: bool):
        super().__init__()
        self.cfg = cfg
        self.items = items
        self.indices = indices
        self.publish_after = publish_after

    def _dbg(self, msg: str):
        """[DEBUG] 접두사 로그 — 어디서 멈추는지 추적용"""
        try:
            self.progress.emit(f"[살구봇][DEBUG] {msg}")
        except Exception:
            pass

    def _has_login_cookies(self, driver) -> bool:
        try:
            cookies = driver.get_cookies()
            names = {c.get("name") for c in cookies}
            has_ses = "NID_SES" in names
            has_aut = "NID_AUT" in names
            self._dbg(f"쿠키 검사: 총 {len(cookies)}개 | NID_SES={has_ses} NID_AUT={has_aut}")
            return has_ses or has_aut
        except Exception as e:
            self._dbg(f"쿠키 검사 실패: {e}")
            return False

    def _is_logged_in(self, driver) -> bool:
        """현재 페이지/네이버 메인 양쪽에서 세션 쿠키와 로그인 마커를 함께 확인"""
        self._dbg("로그인 상태 확인 시작")
        if self._has_login_cookies(driver):
            self._dbg("→ 쿠키로 로그인 확인됨")
            return True

        original_url = ""
        try:
            original_url = driver.current_url
        except Exception:
            pass

        for probe_url in [None, "https://www.naver.com", "https://blog.naver.com"]:
            try:
                if probe_url:
                    self._dbg(f"마커 탐지를 위해 이동: {probe_url}")
                    driver.get(probe_url)
                    time.sleep(1.5)
                if self._has_login_cookies(driver):
                    self._dbg(f"→ {probe_url or '현재페이지'}에서 쿠키 발견")
                    return True
                page = (driver.page_source or "")[:200000]
                markers = [
                    'link_logout', 'MyView-module__link_logout', 'gnb_my_namebox',
                    'gnb_my_layer', 'naverpay', '프로필', '블로그',
                ]
                hits = [m for m in markers if m in page]
                self._dbg(f"마커 검사 ({probe_url or 'cur'}): hits={hits}")
                if hits:
                    if '로그인' not in page[:2000] or 'link_login' not in page:
                        self._dbg("→ 마커로 로그인 확인됨")
                        return True
            except Exception as e:
                self._dbg(f"probe 실패 {probe_url}: {e}")
                continue

        try:
            if original_url and driver.current_url != original_url:
                self._dbg(f"원본 URL 복귀: {original_url[:80]}")
                driver.get(original_url)
                time.sleep(0.5)
        except Exception:
            pass
        self._dbg("→ 로그인 미확인")
        return False

    def _wait_for_login(self, driver, timeout=120):
        self._dbg(f"_wait_for_login 시작 (timeout={timeout}s)")
        elapsed = 0
        while elapsed < timeout:
            try:
                current_url = driver.current_url
            except Exception as e:
                current_url = ''
                self._dbg(f"current_url 읽기 실패: {e}")
            self._dbg(f"폴링 #{elapsed//5 + 1} | URL: {current_url[:100]}")
            if self._is_logged_in(driver):
                self.progress.emit(f"[살구봇] ✅ 로그인 감지됨 ({elapsed}초)")
                return True
            remain = max(timeout - elapsed, 0)
            self.progress.emit(
                f"[살구봇] ⏳ 로그인 대기... {remain}초 남음 | 현재: {current_url[:80]}"
            )
            try:
                if 'nid.naver.com' in current_url or 'nidlogin' in current_url:
                    driver.execute_script('window.focus();')
            except Exception:
                pass
            time.sleep(5)
            elapsed += 5
        self._dbg(f"_wait_for_login 타임아웃 ({timeout}s 경과)")
        return False

    def _human_type(self, element, text, min_ms=30, max_ms=50):
        import random
        for ch in text:
            try:
                element.send_keys(ch)
            except Exception:
                pass
            time.sleep(random.uniform(min_ms, max_ms) / 1000.0)

    def _strip_html_to_text(self, html: str) -> str:
        # 단락을 보존하며 HTML 태그 제거
        text = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
        text = re.sub(r"</\s*(p|h1|h2|h3|div|li)\s*>", "\n\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # HTML entity 간단 디코드
        text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
        return text

    def _dismiss_draft_popup(self, driver):
        """임시저장 글 불러오기 팝업 → '취소' 클릭"""
        try:
            from selenium.webdriver.common.by import By
            # 다양한 가능한 셀렉터 시도
            selectors = [
                "//button[contains(text(),'취소')]",
                "//a[contains(text(),'취소')]",
                "//button[contains(@class,'cancel')]",
                "//*[@class='se-popup-button-cancel']",
                "//*[contains(@class,'btn_cancel')]",
            ]
            for sel in selectors:
                try:
                    els = driver.find_elements(By.XPATH, sel)
                    for el in els:
                        if el.is_displayed():
                            el.click()
                            self.progress.emit("[살구봇] ✓ 임시저장 팝업 '취소' 클릭")
                            time.sleep(1)
                            return True
                except Exception:
                    continue
        except Exception as e:
            self.progress.emit(f"[살구봇] 팝업 처리 오류 (무시): {e}")
        return False

    def run(self):
        try:
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.common.action_chains import ActionChains
        except ImportError:
            self.progress.emit("[살구봇] ❌ selenium/undetected_chromedriver 미설치")
            self.all_done.emit()
            return

        profile_dir = (SALGOO_DIR / "chrome_profile").absolute()
        profile_dir.mkdir(exist_ok=True)

        def _build_opts():
            o = uc.ChromeOptions()
            o.add_argument(f"--user-data-dir={profile_dir}")
            o.add_argument("--start-maximized")
            o.add_argument('--no-sandbox')
            o.add_argument('--disable-dev-shm-usage')
            o.add_argument('--disable-gpu')
            o.add_argument('--disable-popup-blocking')
            o.add_argument('--disable-features=RendererCodeIntegrity')
            # v12: excludeSwitches는 현재 ChromeDriver에서 unrecognized chrome option 오류를 내므로 사용하지 않음
            o.add_experimental_option('prefs', {'profile.default_content_setting_values.popups': 0})
            return o

        try:
            self._dbg("Chrome 드라이버 생성 시도 (opts builder 방식)")
            driver = make_uc_driver(_build_opts)
        except Exception as e:
            self.progress.emit(f"[살구봇] ❌ Chrome 드라이버 시작 실패: {e}")
            self._dbg(f"드라이버 예외 상세: {type(e).__name__}: {e}")
            self.all_done.emit()
            return

        try:
            # 로그인 페이지로 직접 이동 (사용자가 바로 로그인할 수 있도록)
            self._dbg("드라이버 시작됨 → nidlogin.login 으로 이동")
            driver.get("https://nid.naver.com/nidlogin.login")
            time.sleep(2)
            try:
                self._dbg(f"이동 직후 URL: {driver.current_url[:120]}")
            except Exception:
                pass
            self.progress.emit("[살구봇] ⏳ 2분(120초) 안에 네이버 로그인 해주세요. 로그인 완료 후 자동으로 글쓰기로 이동합니다.")
            logged_in = self._wait_for_login(driver, 120)
            if not logged_in:
                self.progress.emit("[살구봇] ❌ 2분 내 로그인 미감지 — 발행 중단. 다시 시도해 주세요.")
                self._dbg("→ run() 중단: 로그인 타임아웃")
                for i in self.indices:
                    self.one_done.emit(i, False, False)
                return

            # 로그인 후 메인으로 이동 (세션 안정화)
            self._dbg("로그인 확인됨 → naver.com 으로 세션 안정화 이동")
            driver.get("https://www.naver.com")
            time.sleep(2)
            self._dbg(f"안정화 후 URL: {driver.current_url[:120]} | 항목 수={len(self.indices)}")

            for i in self.indices:
                it = self.items[i]
                title_full = it.get("title", "제목없음")
                # 20자 이내로 제목 자르기
                title = title_full[:20]
                # rewritten_html 우선, 없으면 content/rewritten 순으로 fallback
                html = (it.get("rewritten_html") or it.get("rewritten")
                        or it.get("content") or "")
                self._dbg(f"[항목 {i}] 시작: title='{title}' html_len={len(html)} keys={list(it.keys())}")
                if not html:
                    self.progress.emit(f"[살구봇] ⚠️  본문 없음 (rewritten_html/content 모두 비어있음) — 건너뜀: {title}")
                    self.one_done.emit(i, False, False)
                    continue

                body_text = self._strip_html_to_text(html)
                # 본문 첫 줄이 제목과 같으면 제거 (h1으로 들어간 케이스)
                if body_text.startswith(title_full):
                    body_text = body_text[len(title_full):].lstrip()
                self._dbg(f"[항목 {i}] 본문 텍스트 길이={len(body_text)}")

                try:
                    self.progress.emit(f"[살구봇] ✍️  글쓰기 진입: {title}")
                    self._dbg(f"[항목 {i}] GoBlogWrite.naver 로 이동")
                    driver.get("https://blog.naver.com/GoBlogWrite.naver")
                    time.sleep(6)
                    try:
                        self._dbg(f"[항목 {i}] 글쓰기 페이지 URL: {driver.current_url[:120]}")
                        self._dbg(f"[항목 {i}] 페이지 타이틀: {driver.title[:80]}")
                    except Exception as e:
                        self._dbg(f"URL/title 읽기 실패: {e}")

                    # 로그인 페이지로 튕겼는지 확인
                    cur_url = driver.current_url
                    if "nid.naver.com" in cur_url or "/nidlogin" in cur_url:
                        self.progress.emit("[살구봇] ⚠️  글쓰기 페이지가 로그인으로 리다이렉트됨 — 추가 로그인 60초 대기")
                        self._dbg(f"[항목 {i}] 리다이렉트 감지 → 재로그인 대기")
                        if not self._wait_for_login(driver, 60):
                            self.progress.emit("[살구봇] ❌ 로그인 실패 — 이 항목 건너뜀")
                            self.one_done.emit(i, False, False)
                            continue
                        driver.get("https://blog.naver.com/GoBlogWrite.naver")
                        time.sleep(6)
                        self._dbg(f"[항목 {i}] 재진입 후 URL: {driver.current_url[:120]}")

                    # iframe 진입
                    self._dbg(f"[항목 {i}] iframe 진입 시도")
                    try:
                        driver.switch_to.default_content()
                        # mainFrame 존재 확인
                        try:
                            frames = driver.find_elements(By.TAG_NAME, "iframe")
                            self._dbg(f"[항목 {i}] iframe 개수={len(frames)} | ids={[f.get_attribute('id') for f in frames[:5]]}")
                        except Exception as e:
                            self._dbg(f"iframe 목록 조회 실패: {e}")
                        driver.switch_to.frame("mainFrame")
                        self.progress.emit("[살구봇] ✓ mainFrame 진입")
                        self._dbg(f"[항목 {i}] mainFrame 진입 성공")
                    except Exception as e:
                        self.progress.emit(f"[살구봇] ⚠️  mainFrame 진입 실패 (무시): {e}")
                        self._dbg(f"[항목 {i}] mainFrame 진입 실패: {e}")
                    time.sleep(2)

                    # 1) 임시저장 팝업 처리 (취소)
                    self._dbg(f"[항목 {i}] 임시저장 팝업 dismiss 시도")
                    self._dismiss_draft_popup(driver)
                    time.sleep(1)

                    # 2) 제목 입력 영역 찾기
                    self._dbg(f"[항목 {i}] 제목 영역 탐색")
                    title_el = None
                    for sel in [
                        "//*[contains(@class,'se-placeholder') and contains(text(),'제목')]/..",
                        "//*[contains(@class,'se-title')]//*[@contenteditable='true']",
                        "//span[contains(@class,'se-placeholder')][contains(text(),'제목')]",
                        "//div[contains(@class,'se-section-documentTitle')]",
                    ]:
                        try:
                            els = driver.find_elements(By.XPATH, sel)
                            visible = [e for e in els if e.is_displayed()]
                            self._dbg(f"  셀렉터 '{sel[:50]}...' → 매칭={len(els)} 보임={len(visible)}")
                            if visible:
                                title_el = visible[0]
                                break
                        except Exception as e:
                            self._dbg(f"  셀렉터 실패: {e}")
                            continue

                    if title_el:
                        self._dbg(f"[항목 {i}] 제목 영역 클릭")
                        try:
                            title_el.click()
                            time.sleep(0.5)
                        except Exception as e:
                            self._dbg(f"  일반 click 실패 → ActionChains 사용: {e}")
                            ActionChains(driver).move_to_element(title_el).click().perform()
                            time.sleep(0.5)
                    else:
                        self.progress.emit("[살구봇] ⚠️  제목 영역 못찾음 — ActionChains로 입력 시도")
                        self._dbg(f"[항목 {i}] 제목 영역 못찾음, blind 입력 진행")

                    # 제목 타이핑
                    actions = ActionChains(driver)
                    self.progress.emit(f"[살구봇] 제목 입력: {title}")
                    self._dbg(f"[항목 {i}] 제목 타이핑 시작 ({len(title)}자)")
                    for ch in title:
                        actions.send_keys(ch).pause(0.04)
                    actions.perform()
                    time.sleep(0.5)
                    self._dbg(f"[항목 {i}] 제목 타이핑 완료")

                    # 3) Tab → 본문 영역으로 이동
                    self._dbg(f"[항목 {i}] Tab 키 전송 → 본문 포커스 이동")
                    ActionChains(driver).send_keys(Keys.TAB).perform()
                    time.sleep(1.0)
                    try:
                        active = driver.execute_script("return document.activeElement && (document.activeElement.tagName + '#' + (document.activeElement.id||'') + '.' + (document.activeElement.className||'').slice(0,80));")
                        self._dbg(f"[항목 {i}] Tab 후 activeElement: {active}")
                    except Exception as e:
                        self._dbg(f"activeElement 조회 실패: {e}")

                    # 4) 본문 타이핑
                    self.progress.emit(f"[살구봇] 본문 입력 시작 ({len(body_text)}자, 약 {len(body_text)*40//1000}초 예상)")
                    self._dbg(f"[항목 {i}] 본문 타이핑 시작")
                    import random
                    actions = ActionChains(driver)
                    chunk_count = 0
                    total_typed = 0
                    for ch in body_text:
                        if ch == "\n":
                            actions.send_keys(Keys.ENTER)
                        else:
                            actions.send_keys(ch)
                        actions.pause(random.uniform(0.030, 0.050))
                        chunk_count += 1
                        total_typed += 1
                        # 200자마다 flush (메모리/안정성)
                        if chunk_count >= 200:
                            actions.perform()
                            self._dbg(f"[항목 {i}] 본문 진행 {total_typed}/{len(body_text)}자")
                            actions = ActionChains(driver)
                            chunk_count = 0
                    if chunk_count > 0:
                        actions.perform()
                    self._dbg(f"[항목 {i}] 본문 타이핑 완료 ({total_typed}자)")

                    time.sleep(1.5)

                    # 5) 임시저장 (Ctrl+S 또는 버튼)
                    self.progress.emit("[살구봇] 💾 임시저장 시도")
                    saved = False
                    for sel in [
                        "//button[contains(@class,'save_btn') or contains(text(),'임시저장')]",
                        "//a[contains(text(),'임시저장')]",
                        "//*[@class='btn_save_layer']",
                    ]:
                        try:
                            els = driver.find_elements(By.XPATH, sel)
                            for el in els:
                                if el.is_displayed():
                                    el.click()
                                    saved = True
                                    self.progress.emit("[살구봇] ✓ 임시저장 버튼 클릭")
                                    break
                            if saved:
                                break
                        except Exception:
                            continue
                    if not saved:
                        # 단축키 fallback
                        try:
                            ActionChains(driver).key_down(Keys.CONTROL).send_keys('s').key_up(Keys.CONTROL).perform()
                            self.progress.emit("[살구봇] ✓ Ctrl+S 임시저장 시도")
                            saved = True
                        except Exception as e:
                            self.progress.emit(f"[살구봇] ⚠️ 임시저장 실패: {e}")
                    time.sleep(2)

                    published = False
                    if self.publish_after:
                        # 6) 발행 버튼 클릭 → 발행 확인
                        self.progress.emit("[살구봇] 🚀 발행 진행")
                        for sel in [
                            "//button[contains(@class,'publish_btn') or contains(text(),'발행')]",
                            "//a[contains(text(),'발행')]",
                        ]:
                            try:
                                els = driver.find_elements(By.XPATH, sel)
                                for el in els:
                                    if el.is_displayed():
                                        el.click()
                                        time.sleep(2)
                                        # 확인 발행 버튼 (2단계)
                                        confirm = driver.find_elements(
                                            By.XPATH,
                                            "//button[contains(text(),'발행') and not(contains(@class,'disabled'))]"
                                        )
                                        for c in confirm:
                                            if c.is_displayed():
                                                c.click()
                                                published = True
                                                self.progress.emit("[살구봇] ✅ 발행 완료")
                                                break
                                        break
                                if published:
                                    break
                            except Exception:
                                continue
                        time.sleep(3)

                    self.one_done.emit(i, True, published)
                except Exception as e:
                    self.progress.emit(f"[살구봇] ❌ 실패 {title}: {e}")
                    self.one_done.emit(i, False, False)

                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
        finally:
            try:
                # manual 모드에서는 사용자가 직접 확인할 수 있도록 잠시 대기 후 닫기
                time.sleep(3)
                driver.quit()
            except Exception:
                pass
            self.all_done.emit()


# ═══════════════════════════════════════════════════════════════════════
#  🤖 AI 뉴스 봇 — 끝
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  대행/후기성 통합 페이지 (AgencyCategoryPage)
#  - 상단에 큰 카테고리 칩(단계별진행/키워드/형태소/이미지/업로드)
#  - 하단에 선택된 하위 페이지 표시
# ═══════════════════════════════════════════════════════════════
class _AgencyStateMixin:
    """상태 자동 저장/복원 mixin.
    하위 클래스가 STATE_FILE / _serialize_state() / _deserialize_state(state) 를 구현하면
    _setup_autosave(*signals)로 변경 시그널만 연결해주면 끝.
    """
    def _state_save(self):
        try:
            from pathlib import Path as _P
            _P(self.STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            state = self._serialize_state()
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            print(f"[State] {getattr(self, 'STATE_FILE', '?')} 저장 실패: {e}")

    def _state_load(self):
        try:
            from pathlib import Path as _P
            if not _P(self.STATE_FILE).exists():
                return
            with open(self.STATE_FILE, encoding='utf-8') as f:
                state = json.load(f)
            self._deserialize_state(state)
            print(f"[State] {self.STATE_FILE} 복원 완료")
        except Exception as e:
            print(f"[State] {getattr(self, 'STATE_FILE', '?')} 복원 실패: {e}")

    def _setup_autosave(self, *signals, delay_ms=500):
        """변경 시그널들을 받아 디바운스 후 저장."""
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._state_save)
        for sig in signals:
            try:
                sig.connect(lambda *_args: self._save_timer.start(delay_ms))
            except Exception: pass


class AgencyCategoryPage(QWidget):
    CATEGORIES = [
        ("⚙️ 설정",            "agency_config"),
        ("📊 키워드 지수확인", "agency_keyword"),
        ("🔬 형태소 분석",   "agency_morphology"),
        ("🖼️ 이미지",         "agency_image"),
        ("🚀 업로드",         "agency_upload"),
    ]

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._page_cache = {}
        self._current_key = None
        self._build_ui()
        # 기본 진입: 설정
        self._switch("agency_config")

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 상단 큰 카테고리 바 ───────────────
        self.cat_bar = QWidget()
        self.cat_bar.setStyleSheet("background:#1a1a1a; border-bottom:2px solid #333;")
        bar_l = QHBoxLayout(self.cat_bar)
        bar_l.setContentsMargins(16, 14, 16, 14)
        bar_l.setSpacing(10)

        self.cat_buttons = {}
        for i, (label, key) in enumerate(self.CATEGORIES):
            btn = QPushButton(f"{i+1}  {label}")
            btn.setCheckable(True)
            btn.setMinimumHeight(52)
            btn.setStyleSheet(self._unchecked_style())
            btn.clicked.connect(lambda _checked, k=key: self._switch(k))
            bar_l.addWidget(btn)
            self.cat_buttons[key] = btn
        bar_l.addStretch()
        root.addWidget(self.cat_bar)

        # ── 하위 페이지 스택 ───────────────
        self.stack = QStackedWidget()
        ph = QLabel("← 위에서 카테고리를 선택하세요")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setStyleSheet("color:#666; font-size:16px; padding:80px;")
        self.stack.addWidget(ph)
        self.stack.setCurrentIndex(0)
        root.addWidget(self.stack, 1)

    def _unchecked_style(self):
        return """
            QPushButton {
                background:#2a2a2a; color:#ddd;
                border:2px solid #3a3a3a; border-radius:26px;
                padding:10px 22px; font-size:15px; font-weight:bold;
            }
            QPushButton:hover { background:#353535; color:#fff; border-color:#666; }
        """

    def _checked_style(self):
        return """
            QPushButton {
                background:#FFD700; color:#1e1e1e;
                border:2px solid #FFD700; border-radius:26px;
                padding:10px 22px; font-size:15px; font-weight:bold;
            }
        """

    def _switch(self, key):
        for k, btn in self.cat_buttons.items():
            checked = (k == key)
            btn.setChecked(checked)
            btn.setStyleSheet(self._checked_style() if checked else self._unchecked_style())

        if key not in self._page_cache:
            if key == "agency_config":
                page = self._build_config_page()
            else:
                page = self.main.get_or_create_page(key)
            if page is None:
                try: self.main.log(f"⚠️ {key} 페이지 생성 실패")
                except Exception: pass
                return
            self._page_cache[key] = page
            self.stack.addWidget(page)

        self.stack.setCurrentWidget(self._page_cache[key])
        self._current_key = key
        try: self.main.log(f"📂 대행/후기성: {key}")
        except Exception: pass

    def _build_config_page(self):
        """설정 페이지 — 네이버 계정 + 프롬프트 라이브러리"""
        from PyQt6.QtWidgets import QScrollArea, QGroupBox
        w = QWidget()
        w.setStyleSheet("background:#1e1e1e; color:#fff;")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(24, 16, 24, 16)
        outer.setSpacing(16)

        hdr = QLabel("⚙️ 설정")
        hdr.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        outer.addWidget(hdr)

        # ── 네이버 계정 ──
        acc_box = QGroupBox("🇰🇷 네이버 계정")
        acc_box.setStyleSheet("""
            QGroupBox { color:#FFD700; font-weight:bold; border:1px solid #444;
                        border-radius:6px; margin-top:8px; padding-top:8px; }
            QGroupBox::title { subcontrol-origin:margin; left:10px; }
        """)
        acc_lay = QVBoxLayout(acc_box)
        acc_lay.setSpacing(8)

        naver_accs = self.main.config.get('naver_accounts', []) or []

        INP = "color:#fff; background:#2a2a2a; border:1px solid #555; padding:4px 8px; border-radius:4px;"
        BTN = "padding:5px 14px; border-radius:4px; font-size:12px;"

        self._cfg_acc_combo = QComboBox()
        self._cfg_acc_combo.setStyleSheet("color:#fff; background:#2a2a2a; border:1px solid #555; padding:4px;")
        self._cfg_acc_combo.addItem("— 계정 선택 —")
        for a in naver_accs:
            self._cfg_acc_combo.addItem(a.get('id', ''))
        self._cfg_acc_combo.currentIndexChanged.connect(self._cfg_load_acc)
        acc_lay.addWidget(self._cfg_acc_combo)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("아이디:"))
        self._cfg_nv_id = QLineEdit(); self._cfg_nv_id.setStyleSheet(INP)
        row1.addWidget(self._cfg_nv_id, 1)
        row1.addWidget(QLabel("비밀번호:"))
        self._cfg_nv_pw = QLineEdit(); self._cfg_nv_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._cfg_nv_pw.setStyleSheet(INP)
        row1.addWidget(self._cfg_nv_pw, 1)
        eye = QPushButton("👁"); eye.setFixedWidth(30); eye.setCheckable(True)
        eye.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px;")
        eye.toggled.connect(lambda on: self._cfg_nv_pw.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row1.addWidget(eye)
        acc_lay.addLayout(row1)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾 저장")
        save_btn.setStyleSheet(f"background:#2d8a4e; color:#fff; {BTN}")
        save_btn.clicked.connect(self._cfg_save_acc)
        btn_row.addWidget(save_btn)
        del_btn = QPushButton("🗑 삭제")
        del_btn.setStyleSheet(f"background:#8e2d2d; color:#fff; {BTN}")
        del_btn.clicked.connect(self._cfg_del_acc)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        acc_lay.addLayout(btn_row)

        self._cfg_acc_status = QLabel("")
        self._cfg_acc_status.setStyleSheet("color:#aaa; font-size:11px;")
        acc_lay.addWidget(self._cfg_acc_status)
        outer.addWidget(acc_box)

        # ── 프롬프트 ──
        prompt_box = QGroupBox("📝 프롬프트 라이브러리")
        prompt_box.setStyleSheet(acc_box.styleSheet())
        pr_lay = QVBoxLayout(prompt_box)
        pr_lay.setSpacing(8)

        pr_desc = QLabel("글 생성 시 사용할 커스텀 프롬프트를 저장·관리합니다.")
        pr_desc.setStyleSheet("color:#aaa; font-size:12px;")
        pr_lay.addWidget(pr_desc)

        pr_btn_row = QHBoxLayout()
        open_lib_btn = QPushButton("🗂 프롬프트 라이브러리 열기")
        open_lib_btn.setStyleSheet(f"background:#2d5a8e; color:#fff; padding:8px 16px; border-radius:5px;")
        open_lib_btn.clicked.connect(self._cfg_open_prompt_library)
        pr_btn_row.addWidget(open_lib_btn)
        pr_btn_row.addStretch()
        pr_lay.addLayout(pr_btn_row)

        self._cfg_prompt_lbl = QLabel("선택된 프롬프트: 없음")
        self._cfg_prompt_lbl.setStyleSheet("color:#888; font-size:11px;")
        pr_lay.addWidget(self._cfg_prompt_lbl)
        outer.addWidget(prompt_box)

        outer.addStretch()
        return w

    def _cfg_load_acc(self, idx):
        if idx <= 0:
            self._cfg_nv_id.clear(); self._cfg_nv_pw.clear()
            return
        accs = self.main.config.get('naver_accounts', []) or []
        if idx - 1 < len(accs):
            a = accs[idx - 1]
            self._cfg_nv_id.setText(a.get('id', ''))
            self._cfg_nv_pw.setText(a.get('pw', ''))

    def _cfg_save_acc(self):
        nid = self._cfg_nv_id.text().strip()
        npw = self._cfg_nv_pw.text().strip()
        if not nid:
            self._cfg_acc_status.setText("⚠️ 아이디를 입력하세요"); return
        accs = self.main.config.get('naver_accounts', []) or []
        for a in accs:
            if a.get('id') == nid:
                a['pw'] = npw
                self._cfg_acc_status.setText(f"✅ {nid} 업데이트됨")
                self._cfg_refresh_combo()
                self._save_config()
                return
        accs.append({'id': nid, 'pw': npw})
        self.main.config['naver_accounts'] = accs
        self._cfg_acc_status.setText(f"✅ {nid} 추가됨")
        self._cfg_refresh_combo()
        self._save_config()

    def _cfg_del_acc(self):
        idx = self._cfg_acc_combo.currentIndex()
        if idx <= 0: return
        accs = self.main.config.get('naver_accounts', []) or []
        if idx - 1 < len(accs):
            removed = accs.pop(idx - 1)
            self.main.config['naver_accounts'] = accs
            self._cfg_acc_status.setText(f"🗑 {removed.get('id','')} 삭제됨")
            self._cfg_refresh_combo()
            self._save_config()

    def _cfg_refresh_combo(self):
        accs = self.main.config.get('naver_accounts', []) or []
        self._cfg_acc_combo.blockSignals(True)
        self._cfg_acc_combo.clear()
        self._cfg_acc_combo.addItem("— 계정 선택 —")
        for a in accs:
            self._cfg_acc_combo.addItem(a.get('id', ''))
        self._cfg_acc_combo.blockSignals(False)

    def _cfg_save_config(self):
        try:
            import json
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(self.main.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_config(self):
        self._cfg_save_config()

    def _cfg_open_prompt_library(self):
        try:
            dlg = PromptLibraryDialog(parent=self)
            dlg.exec()
        except Exception as e:
            self._cfg_prompt_lbl.setText(f"❌ 오류: {e}")


# ═══════════════════════════════════════════════════════════════
#  대행/후기성 (Agency / Review-style)
#  - 1. 키워드 지수확인 (마피아넷 → 블연플)
#  - 2. 형태소 분석 (블로그 상위글 분석 + SEO 제목 생성)
#  - 3. 이미지 (업로드 변형 / 본문 기반)
#  - 4. 업로드 (네이버 블로그, 인용구/굵게/정렬/줄바꿈, 기존 함수 재사용)
# ═══════════════════════════════════════════════════════════════
from PyQt6.QtWidgets import QTabWidget as _QTabWidget_Agency


class AgencyKeywordPage(_AgencyStateMixin, QWidget):
    """1. 키워드 지수확인 — 마피아넷(연관키워드) + 블연플(키워드대량조회)"""
    MAPIA_URL = "https://ma-pia.net/keyword/association.php"
    LABLOG_URL = "https://lablog.co.kr/keyword/keywordMultiSearch"
    STATE_FILE = 'agency_state/keyword.json'

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._mapia_results = {}
        self._lablog_results = []
        self._build_ui()
        # 입력 변경 시 자동 저장
        self._setup_autosave(
            self.mapia_input.textChanged,
            self.lablog_input.textChanged,
            self.opt_autocomplete.stateChanged,
            self.opt_related.stateChanged,
            self.lablog_repeat.valueChanged,
            self.opt_use_my_chrome.stateChanged,
        )
        # 시작 시 자동 복원
        QTimer.singleShot(100, self._state_load)

    def _serialize_state(self):
        return {
            'mapia_input': self.mapia_input.toPlainText(),
            'lablog_input': self.lablog_input.toPlainText(),
            'opt_autocomplete': self.opt_autocomplete.isChecked(),
            'opt_related': self.opt_related.isChecked(),
            'lablog_repeat': self.lablog_repeat.value(),
            'opt_use_my_chrome': self.opt_use_my_chrome.isChecked(),
            'mapia_results': self._mapia_results if isinstance(self._mapia_results, dict) else {},
            'lablog_results': self._lablog_results,
        }

    def _deserialize_state(self, state):
        self.mapia_input.setPlainText(state.get('mapia_input', ''))
        self.lablog_input.setPlainText(state.get('lablog_input', ''))
        self.opt_autocomplete.setChecked(state.get('opt_autocomplete', True))
        self.opt_related.setChecked(state.get('opt_related', True))
        self.lablog_repeat.setValue(state.get('lablog_repeat', 10))
        self.opt_use_my_chrome.setChecked(state.get('opt_use_my_chrome', False))
        mapia = state.get('mapia_results', {})
        if isinstance(mapia, dict) and mapia.get('rows'):
            self._mapia_results = mapia
            self._render_mapia_table()
        lablog = state.get('lablog_results', [])
        if lablog:
            self._lablog_results = lablog
            self._render_lablog_table()

    def _render_mapia_table(self):
        headers = self._mapia_results.get('headers', []) or []
        rows = self._mapia_results.get('rows', []) or []
        if headers:
            self.mapia_table.setColumnCount(len(headers))
            self.mapia_table.setHorizontalHeaderLabels(headers)
        self.mapia_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                if j < self.mapia_table.columnCount():
                    self.mapia_table.setItem(i, j, QTableWidgetItem(str(cell)))
        self.mapia_to_lablog_btn.setEnabled(bool(rows))
        self.mapia_excel_btn.setEnabled(bool(rows))

    def _render_lablog_table(self):
        result = self._lablog_results or []
        self.lablog_table.setRowCount(len(result))
        for i, row in enumerate(result):
            for j in range(min(4, len(row))):
                self.lablog_table.setItem(i, j, QTableWidgetItem(str(row[j])))
        self.lablog_excel_btn.setEnabled(bool(result))

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("📊 1. 키워드 지수확인")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        sub = QLabel("마피아넷에서 연관 키워드를 추출하고, 블연플(lablog.co.kr)에서 지수를 조회합니다.")
        sub.setStyleSheet("color:#aaa; font-size:11px; padding-bottom:6px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        self.tabs = _QTabWidget_Agency()
        self.tabs.addTab(self._build_mapia_tab(), "🌐 마피아넷 연관키워드")
        self.tabs.addTab(self._build_lablog_tab(), "📈 블연플 키워드 지수")
        root.addWidget(self.tabs, 1)

    def _build_mapia_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(f"URL: {self.MAPIA_URL}"))
        lay.addWidget(QLabel("🔎 검색 키워드 (1줄 1키워드, 최대 5개)"))
        self.mapia_input = QTextEdit()
        self.mapia_input.setPlaceholderText("예시:\n다이어트\n홈트레이닝\n부산맛집")
        self.mapia_input.setMaximumHeight(120)
        lay.addWidget(self.mapia_input)

        h = QHBoxLayout()
        self.mapia_btn = QPushButton("🔍 마피아넷 조회")
        self.mapia_btn.clicked.connect(self.run_mapia)
        h.addWidget(self.mapia_btn)
        self.mapia_to_lablog_btn = QPushButton("➡️ 결과를 블연플로 전송")
        self.mapia_to_lablog_btn.clicked.connect(self.send_to_lablog)
        self.mapia_to_lablog_btn.setEnabled(False)
        h.addWidget(self.mapia_to_lablog_btn)
        self.mapia_excel_btn = QPushButton("💾 엑셀 저장")
        self.mapia_excel_btn.clicked.connect(self.export_mapia_excel)
        self.mapia_excel_btn.setEnabled(False)
        h.addWidget(self.mapia_excel_btn)
        h.addStretch()
        lay.addLayout(h)

        lay.addWidget(QLabel("📋 연관 키워드 결과"))
        self.mapia_table = QTableWidget(0, 2)
        self.mapia_table.setHorizontalHeaderLabels(["시드 키워드", "연관 키워드"])
        self.mapia_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.mapia_table, 1)
        return w

    def _build_lablog_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(f"URL: {self.LABLOG_URL}"))
        lay.addWidget(QLabel("🔎 조회 키워드 (1줄 1키워드, 5개씩 자동 분할)"))
        self.lablog_input = QTextEdit()
        self.lablog_input.setPlaceholderText("키워드를 한 줄에 하나씩 입력 (또는 마피아넷 탭에서 전송)")
        lay.addWidget(self.lablog_input, 1)

        opts = QHBoxLayout()
        self.opt_autocomplete = QCheckBox("자동완성"); self.opt_autocomplete.setChecked(True)
        self.opt_related = QCheckBox("연관검색어"); self.opt_related.setChecked(True)
        opts.addWidget(self.opt_autocomplete); opts.addWidget(self.opt_related)
        opts.addWidget(QLabel("  ※ 5개씩 쿼리 / 60개마다 엑셀 1파일 / 모든 키워드 1회씩 처리"))
        # 호환성 유지용 hidden spinbox (state 저장/복원에서 참조)
        self.lablog_repeat = QSpinBox(); self.lablog_repeat.setRange(1, 50); self.lablog_repeat.setValue(1)
        self.lablog_repeat.setVisible(False)
        opts.addStretch()
        lay.addLayout(opts)

        # 프로파일 옵션
        prof_row = QHBoxLayout()
        self.opt_use_my_chrome = QCheckBox("🔑 내 Chrome에 로그인된 계정으로 시작 (실행 전 Chrome 모두 종료)")
        self.opt_use_my_chrome.setChecked(False)
        self.opt_use_my_chrome.setStyleSheet("color:#FFD700;")
        prof_row.addWidget(self.opt_use_my_chrome)
        prof_row.addStretch()
        lay.addLayout(prof_row)

        h = QHBoxLayout()
        self.lablog_btn = QPushButton("🚀 블연플 조회 시작 (구글 로그인 → 자동 조회)")
        self.lablog_btn.clicked.connect(self.run_lablog)
        h.addWidget(self.lablog_btn)
        self.lablog_excel_btn = QPushButton("💾 엑셀 저장")
        self.lablog_excel_btn.clicked.connect(self.export_lablog_excel)
        self.lablog_excel_btn.setEnabled(False)
        h.addWidget(self.lablog_excel_btn)
        h.addStretch()
        lay.addLayout(h)

        lay.addWidget(QLabel("📋 조회 결과"))
        self.lablog_table = QTableWidget(0, 4)
        self.lablog_table.setHorizontalHeaderLabels(["키워드", "월간 검색량", "자동완성", "연관검색어"])
        self.lablog_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.lablog_table, 1)
        return w

    # ── 마피아넷 ─────────────────────────────
    def run_mapia(self):
        keywords = [k.strip() for k in self.mapia_input.toPlainText().splitlines() if k.strip()][:5]
        if not keywords:
            self.main.log("⚠️ 마피아넷: 키워드를 입력하세요"); return
        self.mapia_btn.setEnabled(False); self.mapia_btn.setText("🔍 조회 중... (앱 닫지 마세요)")
        self.main.log(f"🔍 마피아넷 조회 시작: {keywords} — Chrome 창이 열립니다, 앱을 닫지 마세요.")
        self.main.log("    진행 상황은 PowerShell 콘솔의 [Mapia] 로그를 확인하세요.")
        t = WorkerThread(self._scrape_mapia, keywords)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self._on_mapia_done)
        t.finished_signal.connect(lambda: (
            self.mapia_btn.setEnabled(True), self.mapia_btn.setText("🔍 마피아넷 조회")))
        t.start()

    def _scrape_mapia(self, keywords):
        print(f"[Mapia] ▶ 시작: {len(keywords)}개 키워드 {keywords}")
        print("[Mapia] Chrome driver 생성 중... (몇 초 걸립니다, 앱 닫지 마세요)")

        driver = None
        try:
            import undetected_chromedriver as uc
            def build_opts():
                o = uc.ChromeOptions()
                o.add_argument('--start-maximized'); o.add_argument('--no-sandbox')
                o.add_argument('--disable-dev-shm-usage'); o.add_argument('--disable-gpu')
                return o
            driver = make_uc_driver(build_opts)
        except ImportError:
            print("[Mapia] undetected_chromedriver 없음 → selenium 폴백 시도")
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options as _Opts
                _o = _Opts()
                _o.add_argument('--start-maximized'); _o.add_argument('--no-sandbox')
                _o.add_argument('--disable-dev-shm-usage'); _o.add_argument('--disable-gpu')
                _o.add_argument('--disable-blink-features=AutomationControlled')
                driver = webdriver.Chrome(options=_o)
            except Exception as e2:
                print(f"[Mapia] ❌ selenium 폴백도 실패: {e2}")
                return {'seeds': keywords, 'headers': [], 'rows': [],
                        'error': f'ChromeDriver 없음 — undetected-chromedriver 또는 selenium 설치 필요: {e2}'}
        except Exception as e:
            print(f"[Mapia] ❌ driver 생성 실패: {e}")
            return {'seeds': keywords, 'headers': [], 'rows': [],
                    'error': f'driver 생성 실패: {format_error_message(e)}'}
        print("[Mapia] driver 생성 완료")

        # driver를 main에 보관해서 worker 종료 후 GC가 __del__을 호출하지 않게 함
        try:
            if not hasattr(self.main, '_open_drivers'):
                self.main._open_drivers = []
            self.main._open_drivers.append(driver)
        except Exception: pass

        debug_info = {'windows': [], 'note': ''}
        try:
            print(f"[Mapia] 페이지 이동: {self.MAPIA_URL}")
            driver.get(self.MAPIA_URL); time.sleep(3)
            print(f"[Mapia] 현재 URL: {driver.current_url}")
            # 팝업: 오늘하루안보기
            print("[Mapia] 팝업 확인/닫기")
            for sel in [
                "//a[contains(text(),'오늘하루')]",
                "//button[contains(text(),'오늘하루')]",
                "//*[contains(text(),'오늘 하루')]",
            ]:
                try:
                    for e in driver.find_elements(By.XPATH, sel):
                        try: e.click(); time.sleep(0.3)
                        except Exception: pass
                except Exception: pass

            # ── 페이지 구조 디버그 ──────────────────
            print("[Mapia DEBUG] 페이지 구조:")
            try:
                forms = driver.find_elements(By.TAG_NAME, "form")
                print(f"  · form: {len(forms)}개")
                for i, f in enumerate(forms):
                    try:
                        print(f"     form#{i}: action={f.get_attribute('action')} method={f.get_attribute('method')}")
                    except Exception: pass
                tas = driver.find_elements(By.TAG_NAME, "textarea")
                print(f"  · textarea: {len(tas)}개")
                for i, ta in enumerate(tas):
                    try:
                        print(f"     textarea#{i}: name={ta.get_attribute('name')} id={ta.get_attribute('id')} "
                              f"placeholder={ta.get_attribute('placeholder')}")
                    except Exception: pass
                inps = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])")
                print(f"  · input(text): {len(inps)}개")
                for i, inp in enumerate(inps[:10]):
                    try:
                        print(f"     input#{i}: name={inp.get_attribute('name')} id={inp.get_attribute('id')}")
                    except Exception: pass
                btns = driver.find_elements(By.TAG_NAME, "button") + \
                       driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], input[type='button']")
                print(f"  · button/submit: {len(btns)}개")
                for i, b in enumerate(btns[:15]):
                    try:
                        t = (b.text or '').strip() or b.get_attribute('value') or ''
                        print(f"     btn#{i}: '{t}' type={b.get_attribute('type')}")
                    except Exception: pass
            except Exception as e:
                print(f"  · 구조 디버그 실패: {e}")

            # ── 키워드 입력: 첫 번째 visible textarea 우선 ──
            print("[Mapia] 키워드 입력")
            input_ok = False
            try:
                tas = driver.find_elements(By.TAG_NAME, "textarea")
                for ta in tas:
                    try:
                        if ta.is_displayed() and ta.is_enabled():
                            ta.clear()
                            try: safe_paste(driver, ta, "\n".join(keywords))
                            except Exception: ta.send_keys("\n".join(keywords))
                            print(f"  · textarea name={ta.get_attribute('name')} 에 입력")
                            input_ok = True; break
                    except Exception: pass
                if not input_ok:
                    # input[type=text] fallback
                    inps = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")
                    for i, kw in enumerate(keywords):
                        if i < len(inps) and inps[i].is_displayed():
                            try:
                                inps[i].clear(); inps[i].send_keys(kw); input_ok = True
                            except Exception: pass
            except Exception as e:
                print(f"  · 키워드 입력 실패: {e}")

            if not input_ok:
                print("[Mapia] ⚠️ 키워드 입력 실패 — 페이지 구조 확인 필요")

            # ── 검색 버튼: text 내용에 '조회'/'검색' 포함하는 버튼 (descendant text 포함) ──
            print("[Mapia] 검색 버튼 클릭 시도")
            click_ok = False

            # Selenium의 .text는 descendant 전체 텍스트를 반환하므로 이걸로 매칭
            candidates = []
            try:
                all_btns = (driver.find_elements(By.TAG_NAME, "button")
                            + driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], input[type='button']")
                            + driver.find_elements(By.TAG_NAME, "a"))
                for b in all_btns:
                    try:
                        if not (b.is_displayed() and b.is_enabled()):
                            continue
                        t = ((b.text or '') + ' ' + (b.get_attribute('value') or '')).strip()
                        if not t: continue
                        # '조회하기' 우선, 그 다음 '조회', '검색'
                        if '조회하기' in t:
                            candidates.insert(0, (b, t, 'P0'))
                        elif '조회' in t and '초기화' not in t:
                            candidates.append((b, t, 'P1'))
                        elif '검색' in t:
                            candidates.append((b, t, 'P2'))
                    except Exception: pass
            except Exception as e:
                print(f"  · 후보 수집 실패: {e}")
            print(f"  · 후보 버튼: {len(candidates)}개")

            for b, txt, pr in candidates:
                short = txt.replace('\n', ' ').strip()[:30]
                # 일반 click → JS click → ActionChains 순으로 시도
                try:
                    b.click()
                    print(f"  · [{pr}] click() 성공: '{short}'")
                    click_ok = True; break
                except Exception as e1:
                    try:
                        driver.execute_script("arguments[0].click()", b)
                        print(f"  · [{pr}] JS click 성공: '{short}'")
                        click_ok = True; break
                    except Exception as e2:
                        print(f"  · [{pr}] 클릭 실패: '{short}' ({e2})")

            # 그래도 안 되면 textarea에서 Ctrl+Enter / Enter 시도
            if not click_ok:
                print("  · 버튼 클릭 안 됨 → textarea Ctrl+Enter 시도")
                try:
                    from selenium.webdriver.common.keys import Keys
                    for ta in driver.find_elements(By.TAG_NAME, "textarea"):
                        if ta.is_displayed():
                            try:
                                ta.click(); time.sleep(0.2)
                                ta.send_keys(Keys.CONTROL, Keys.RETURN); click_ok = True
                                print("    Ctrl+Enter 전송")
                                break
                            except Exception: pass
                except Exception: pass

            # 그래도 안 되면 흔히 쓰이는 JS 함수명들 시도 (마피아넷 같은 사이트의 관행)
            if not click_ok:
                print("  · JS 함수 직접 호출 시도")
                for js_fn in ['goSearch()', 'doSearch()', 'fnSearch()', 'fn_search()',
                              'searchKeyword()', 'fnGo()', 'fn_go()', 'do_search()']:
                    try:
                        driver.execute_script(f"if(typeof {js_fn.split('(')[0]}=='function'){{{js_fn};}}")
                        print(f"    {js_fn} 호출 시도")
                    except Exception: pass

            print("[Mapia] 결과 대기 (15초)...")
            time.sleep(15)
            print(f"[Mapia] 검색 후 URL: {driver.current_url} (창 {len(driver.window_handles)}개)")

            # 결과 파싱: 모든 윈도우/탭 + iframe 안에서 가장 큰 테이블을 찾음
            print("[Mapia] 모든 창/iframe에서 테이블 검색 시작")
            headers = []
            rows_data = []
            try:
                handles = list(driver.window_handles)
            except Exception:
                handles = [None]

            for handle in handles:
                try:
                    if handle: driver.switch_to.window(handle)
                except Exception: pass
                try:
                    cur_url = driver.current_url
                except Exception:
                    cur_url = '?'

                # 메인 도큐먼트에서 추출
                n_tables, h, r = self._mapia_extract_biggest_table(driver)
                debug_info['windows'].append({'url': cur_url, 'tables': n_tables, 'rows': len(r)})
                if len(r) > len(rows_data):
                    rows_data, headers = r, h

                # iframe 안도 검사
                try:
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                except Exception:
                    iframes = []
                for ifr in iframes:
                    try:
                        driver.switch_to.frame(ifr)
                        n2, h2, r2 = self._mapia_extract_biggest_table(driver)
                        debug_info['windows'].append({'url': f'{cur_url}#iframe', 'tables': n2, 'rows': len(r2)})
                        if len(r2) > len(rows_data):
                            rows_data, headers = r2, h2
                    except Exception: pass
                    finally:
                        try: driver.switch_to.default_content()
                        except Exception: pass

            # 메인 윈도우로 복귀
            try:
                driver.switch_to.window(driver.window_handles[0])
                driver.switch_to.default_content()
            except Exception: pass

            print(f"[Mapia] ✅ 완료: {len(rows_data)}행 × {len(headers)}열 수집")
            return {'seeds': keywords, 'headers': headers, 'rows': rows_data, 'debug': debug_info}
        except Exception as e:
            print(f"[Mapia] ❌ 예외: {e}")
            return {'seeds': keywords, 'headers': [], 'rows': [],
                    'error': format_error_message(e), 'debug': debug_info}
        finally:
            # driver는 닫지 않음 — 사용자가 브라우저에서 직접 확인하고 닫도록 함
            # (driver.quit() 호출이 chromedriver 종료 시 가끔 크래시 유발)
            # 참조는 self.main._open_drivers에 보관되어 worker 종료 후에도 살아 있음
            print("[Mapia] worker 종료 (브라우저는 유지)")

    def _mapia_extract_biggest_table(self, driver):
        """현재 컨텍스트(window/iframe)에서 가장 큰 테이블의 (테이블수, 헤더, 행) 반환."""
        try:
            tables = driver.find_elements(By.TAG_NAME, "table")
        except Exception:
            return 0, [], []
        best = None; max_rows = 0
        for ti, t in enumerate(tables):
            try:
                rs = t.find_elements(By.TAG_NAME, "tr")
                # 디버그: 각 테이블의 행 수와 첫 행 미리보기
                try:
                    first_row = ''
                    for tr in rs[:1]:
                        cells = tr.find_elements(By.TAG_NAME, "th") or tr.find_elements(By.TAG_NAME, "td")
                        first_row = ' | '.join((c.text or '').strip()[:20] for c in cells[:6])
                    print(f"     table#{ti}: {len(rs)}행 — 첫 행: {first_row[:120]}")
                except Exception: pass
                if len(rs) > max_rows:
                    max_rows = len(rs); best = t
            except Exception: pass
        if best is None:
            return len(tables), [], []
        headers = []
        try:
            ths = best.find_elements(By.TAG_NAME, "th")
            if ths:
                headers = [(c.text or '').strip() for c in ths]
        except Exception: pass
        rows = []
        try:
            for tr in best.find_elements(By.TAG_NAME, "tr"):
                try:
                    tds = tr.find_elements(By.TAG_NAME, "td")
                    if not tds: continue
                    row = [(c.text or '').strip() for c in tds]
                    if any(row):
                        rows.append(row)
                except Exception: pass
        except Exception: pass
        if not headers and rows:
            headers = [f"컬럼 {i+1}" for i in range(len(rows[0]))]
        return len(tables), headers, rows

    def _on_mapia_done(self, result):
        if isinstance(result, str):
            self.main.log(result); return
        if not isinstance(result, dict):
            self.main.log(f"⚠️ 마피아넷: 예상치 못한 결과 형식 ({type(result).__name__})")
            return
        self._mapia_results = result
        headers = result.get('headers', []) or []
        rows = result.get('rows', []) or []
        debug = result.get('debug', {})

        if result.get('error'):
            self.main.log(f"❌ 마피아넷 오류: {result['error']}")
        # 디버그: 각 창/iframe에서 발견한 테이블 정보
        for w in debug.get('windows', []):
            self.main.log(f"  · {w.get('url', '?')} → table {w.get('tables', 0)}개 / 데이터행 {w.get('rows', 0)}개")

        if not rows:
            self.main.log("⚠️ 결과 행을 찾지 못했습니다. 열린 브라우저에서 직접 확인하거나, "
                          "결과 페이지의 URL을 알려주시면 셀렉터를 맞춰드립니다.")

        # 테이블 동적 재구성
        if headers:
            self.mapia_table.setColumnCount(len(headers))
            self.mapia_table.setHorizontalHeaderLabels(headers)
        else:
            self.mapia_table.setColumnCount(2)
            self.mapia_table.setHorizontalHeaderLabels(["#", "키워드"])

        self.mapia_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                if j < self.mapia_table.columnCount():
                    self.mapia_table.setItem(i, j, QTableWidgetItem(str(cell)))

        self.mapia_to_lablog_btn.setEnabled(bool(rows))
        self.mapia_excel_btn.setEnabled(bool(rows))
        if rows:
            self.main.log(f"✅ 마피아넷: {len(rows)}행 × {self.mapia_table.columnCount()}열 수집 "
                          f"(브라우저는 자동 종료하지 않음)")
        # 결과 즉시 저장 — 크래시/강제종료 대비
        self._state_save()

    def _clean_keyword(self, raw):
        """키워드에서 UI 아이콘 텍스트/잡문자 제거."""
        if not raw:
            return ''
        import re as _re
        # 알려진 Material Icons / Tabler / FontAwesome 이름 같은 텍스트 제거
        icons = [
            'open_in_new', 'launch', 'arrow_outward', 'north_east',
            'open in new', 'external_link', 'link', 'arrow_forward',
            'content_copy', 'copy', 'edit', 'delete', 'search',
        ]
        # 줄별로 처리 — 한 셀에 키워드 + 아이콘이 줄바꿈으로 들어있는 경우
        lines = [l.strip() for l in raw.replace('\r', '\n').split('\n')]
        cleaned = []
        for line in lines:
            if not line: continue
            low = line.lower()
            # 아이콘 텍스트 단독 줄 → 스킵
            if any(low == ic or low.startswith(ic + ' ') for ic in icons):
                continue
            # 줄 안에서 아이콘 텍스트 제거
            for ic in icons:
                line = _re.sub(r'\b' + _re.escape(ic) + r'\b', '', line, flags=_re.IGNORECASE)
            line = _re.sub(r'\s+', ' ', line).strip()
            if line:
                cleaned.append(line)
        # 가장 그럴듯한 줄 선택 (보통 첫 줄)
        if not cleaned: return ''
        return cleaned[0]

    def _detect_keyword_column(self, rows):
        """텍스트가 가장 많이 들어있는 컬럼을 키워드 컬럼으로 추정 (숫자 컬럼 제외)"""
        if not rows: return 0
        n_cols = len(rows[0])
        scores = [0] * n_cols
        for row in rows[:20]:
            for j in range(min(n_cols, len(row))):
                s = row[j].strip()
                if not s: continue
                # 한글/영문이 들어있으면 텍스트 컬럼으로 가점
                has_letter = any(c.isalpha() or '가' <= c <= '힣' for c in s)
                if has_letter:
                    scores[j] += 1
        if not any(scores): return 0
        return scores.index(max(scores))

    def send_to_lablog(self):
        data = self._mapia_results if isinstance(self._mapia_results, dict) else {}
        rows = data.get('rows', [])
        if not rows:
            self.main.log("⚠️ 전송할 결과 없음"); return
        kw_col = self._detect_keyword_column(rows)
        seen = set(); unique = []
        skipped = 0
        for row in rows:
            if kw_col < len(row):
                kw = self._clean_keyword(row[kw_col])  # ← UI 아이콘 텍스트 제거
                if kw and kw not in seen and len(kw) >= 2:
                    seen.add(kw); unique.append(kw)
                else:
                    skipped += 1
        self.lablog_input.setPlainText("\n".join(unique))
        self.tabs.setCurrentIndex(1)
        self.main.log(f"➡️ 블연플 탭으로 {len(unique)}개 키워드 전송 "
                      f"(키워드 컬럼: #{kw_col+1}, 정제 후 제외: {skipped}개)")
        print(f"[Mapia→Lablog] 전송 키워드 미리보기: {unique[:5]}...")

    def export_mapia_excel(self):
        path, _ = QFileDialog.getSaveFileName(self, "엑셀 저장", "mapia_keywords.csv", "CSV (*.csv)")
        if not path: return
        try:
            import csv
            data = self._mapia_results if isinstance(self._mapia_results, dict) else {}
            headers = data.get('headers', []) or []
            rows = data.get('rows', []) or []
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                if headers: w.writerow(headers)
                for row in rows:
                    w.writerow(row)
            self.main.log(f"💾 저장: {path} ({len(rows)}행)")
        except Exception as e:
            self.main.log(f"❌ 저장 실패: {e}")

    # ── 블연플 ─────────────────────────────
    def run_lablog(self):
        raw_keywords = [k.strip() for k in self.lablog_input.toPlainText().splitlines() if k.strip()]
        # 입력값도 한 번 더 정제 — open_in_new 등 아이콘 텍스트 제거 + 2자 미만 컷
        keywords = []
        seen = set()
        for k in raw_keywords:
            cleaned = self._clean_keyword(k)
            if cleaned and len(cleaned) >= 2 and cleaned not in seen:
                seen.add(cleaned); keywords.append(cleaned)
        if not keywords:
            self.main.log("⚠️ 블연플: 키워드를 입력하세요"); return
        if len(keywords) != len(raw_keywords):
            self.main.log(f"🧹 키워드 정제: {len(raw_keywords)}개 → {len(keywords)}개 (중복/아이콘 텍스트 제거)")
        self.lablog_btn.setEnabled(False); self.lablog_btn.setText("🚀 조회 중...")
        options = {
            'autocomplete': self.opt_autocomplete.isChecked(),
            'related': self.opt_related.isChecked(),
            'use_my_chrome': self.opt_use_my_chrome.isChecked(),
        }
        t = WorkerThread(self._scrape_lablog, keywords, options)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self._on_lablog_done)
        t.finished_signal.connect(lambda: (
            self.lablog_btn.setEnabled(True),
            self.lablog_btn.setText("🚀 블연플 조회 시작 (구글 로그인 → 자동 조회)")))
        t.start()

    def _scrape_lablog(self, keywords, options):
        import os as _os

        # ── undetected_chromedriver 가용 여부 확인 ──
        try:
            import undetected_chromedriver as uc
            _uc_available = True
        except ImportError:
            print("[Lablog] undetected_chromedriver 없음 → selenium 폴백 모드")
            _uc_available = False

        # ── 프로파일 결정 ─────────────────────────────────
        use_my = bool(options.get('use_my_chrome', False))
        try: self.main.log(f"🔍 블연플 프로파일 모드: {'A(내 Chrome)' if use_my else 'B(앱 전용 ./chrome_profile)'}")
        except Exception: pass
        print(f"[Lablog] use_my_chrome={use_my}")
        profile_dir = None
        profile_name = 'Default'
        if use_my and _os.name == 'nt':
            cand = _os.path.expanduser(r'~\AppData\Local\Google\Chrome\User Data')
            if _os.path.isdir(cand):
                profile_dir = cand
                print(f"[Lablog] 🔑 내 Chrome 프로파일 사용: {profile_dir}")
                print("[Lablog] ⚠️ Chrome이 실행 중이면 충돌합니다. Chrome 모두 종료 후 시도하세요.")
            else:
                print(f"[Lablog] ⚠️ 내 Chrome 프로파일 경로를 찾지 못함 ({cand}) → 앱 전용 프로파일로 폴백")
                use_my = False  # 폴백 시 use_my도 False로 갱신
        if not profile_dir:
            profile_dir = _os.path.abspath('./chrome_profile')
            _os.makedirs(profile_dir, exist_ok=True)
            print(f"[Lablog] 앱 전용 프로파일: {profile_dir}")
            print("[Lablog] (첫 실행 시 구글 로그인하시면 다음부터 자동 유지됩니다)")

        # ── Chrome 프로세스 강제 종료 + 락 파일 정리 (양쪽 모드 공통) ──
        def cleanup_chrome_and_locks():
            try:
                subprocess.run(['taskkill', '/f', '/im', 'chromedriver.exe'], capture_output=True)
                subprocess.run(['taskkill', '/f', '/im', 'chrome.exe'], capture_output=True)
                time.sleep(2)
                print("[Lablog] Chrome / chromedriver 프로세스 종료")
            except Exception as e:
                print(f"[Lablog] 프로세스 종료 실패: {e}")
            # 락 파일 정리 — 양쪽 모드 모두
            lock_targets = [
                profile_dir,
                _os.path.join(profile_dir, profile_name),  # Default 서브폴더
            ]
            for base in lock_targets:
                if not _os.path.isdir(base): continue
                for lock in ['SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile']:
                    p = _os.path.join(base, lock)
                    try:
                        if _os.path.exists(p):
                            _os.remove(p)
                            print(f"[Lablog] 락파일 정리: {p}")
                    except Exception as e:
                        print(f"[Lablog] 락파일 정리 실패 ({p}): {e}")

        cleanup_chrome_and_locks()

        # UC는 ChromeOptions 인스턴스를 재사용 못 함 → 빌더 람다 사용
        if _uc_available:
            def build_opts():
                o = uc.ChromeOptions()
                o.add_argument('--start-maximized')
                o.add_argument(f'--user-data-dir={profile_dir}')
                if use_my:
                    o.add_argument(f'--profile-directory={profile_name}')
                o.add_argument('--no-sandbox'); o.add_argument('--disable-dev-shm-usage')
                return o
            _driver_builder = lambda: make_uc_driver(build_opts)
        else:
            def build_opts():
                from selenium.webdriver.chrome.options import Options as _SOpts
                o = _SOpts()
                o.add_argument('--start-maximized')
                o.add_argument(f'--user-data-dir={profile_dir}')
                if use_my:
                    o.add_argument(f'--profile-directory={profile_name}')
                o.add_argument('--no-sandbox'); o.add_argument('--disable-dev-shm-usage')
                o.add_argument('--disable-blink-features=AutomationControlled')
                return o
            def _driver_builder():
                from selenium import webdriver as _wd
                return _wd.Chrome(options=build_opts())

        try:
            driver = _driver_builder()
            print(f"[Lablog] driver 생성 완료")
        except Exception as e:
            msg = format_error_message(e)
            print(f"[Lablog] ❌ 1차 driver 생성 실패: {msg}")
            low = msg.lower()
            chrome_unreachable = ('cannot connect' in low or 'session not created' in low or 'devtoolsactiveport' in low or 'chrome not reachable' in low)

            if chrome_unreachable:
                print("[Lablog] 🔄 Chrome 연결 실패 → 더 강하게 정리 후 재시도")
                cleanup_chrome_and_locks()
                time.sleep(3)
                # 옵션 B(앱 전용)는 프로파일 통째로 리셋도 시도
                if not use_my:
                    print(f"[Lablog] 옵션 B → '{profile_dir}' 통째로 리셋 (로그인 다시 필요)")
                    try:
                        import shutil
                        if _os.path.isdir(profile_dir):
                            shutil.rmtree(profile_dir, ignore_errors=True)
                        _os.makedirs(profile_dir, exist_ok=True)
                        print(f"[Lablog] 리셋 완료")
                    except Exception as e2:
                        print(f"[Lablog] 리셋 실패: {e2}")
                else:
                    print("[Lablog] 옵션 A → 사용자 Chrome 프로파일은 통째 삭제하지 않음 (락만 정리)")
                try:
                    driver = _driver_builder()
                    print("[Lablog] 2차 driver 생성 성공")
                except Exception as e3:
                    msg3 = format_error_message(e3)
                    print(f"[Lablog] ❌ 2차도 실패: {msg3}")
                    advice = []
                    if use_my:
                        advice.append("💡 옵션 A로 켜져 있습니다. 다음을 시도하세요:")
                        advice.append("   1) 작업관리자 → 모든 chrome.exe 종료 (백그라운드 포함)")
                        advice.append("   2) 또는 🔑 체크박스를 끄고 옵션 B로 시도")
                    else:
                        advice.append(f"💡 옵션 B인데도 실패. 다음을 시도하세요:")
                        advice.append(f"   1) '{profile_dir}' 폴더를 탐색기에서 직접 삭제")
                        advice.append("   2) 'pip install --upgrade undetected-chromedriver' 실행")
                        advice.append("   3) Chrome 브라우저 업데이트")
                    return f"❌ 블연플 driver 재시도 후에도 실패:\n{msg3}\n\n" + "\n".join(advice)
            else:
                # 흔한 케이스 진단
                hints = []
                if 'user data directory' in low or 'in use' in low or 'cannot create' in low or 'locked' in low:
                    if use_my:
                        hints.append("💡 Chrome이 실행 중일 가능성 — 작업관리자에서 모든 chrome.exe 종료 후 재시도")
                    else:
                        hints.append(f"💡 '{profile_dir}' 폴더가 잠겨 있을 수 있습니다.")
                if 'chromedriver' in low and 'version' in low:
                    hints.append("💡 Chrome과 chromedriver 버전 불일치 — 'pip install --upgrade undetected-chromedriver' 시도")
                if 'permission' in low or 'access' in low:
                    hints.append(f"💡 폴더 권한 문제 — '{profile_dir}' 권한 확인")
                if not hints:
                    hints.append("💡 PowerShell 콘솔의 [Lablog] 로그 전체를 알려주시면 원인 파악이 빨라집니다.")
                return f"❌ 블연플 driver 생성 실패:\n{msg}\n\n" + "\n".join(hints)

        # driver를 main에 보관 — worker 종료 후 GC가 __del__을 호출하지 않게 함
        try:
            if not hasattr(self.main, '_open_drivers'):
                self.main._open_drivers = []
            self.main._open_drivers.append(driver)
        except Exception: pass
        # ── 배치 처리: 5개씩 쿼리, 60개마다 엑셀 저장 ──
        QUERY_SIZE = 5      # 한 번 검색에 입력하는 키워드 수
        BATCH_SIZE = 60     # 엑셀 1파일에 들어갈 키워드 수 (= 12 쿼리)

        all_results = []          # 전체 결과 누적
        batch_rows = []           # 현재 batch의 결과 행
        batch_num = 1             # 현재 batch 번호
        keywords_in_batch = 0     # 현재 batch에 들어간 키워드 수
        saved_paths = []          # 저장된 엑셀 경로들

        try:
            driver.get(self.LABLOG_URL); time.sleep(5)
            # 로그인 대기 (최대 300초 = 5분) — 첫 로그인은 시간이 걸릴 수 있음
            print("[Lablog] 로그인 대기 (최대 5분)")
            print("[Lablog]   → 열린 Chrome에서 구글 로그인 → 키워드 대량조회 페이지로 이동 → 자동 진행")
            waited = 0
            last_url = ''
            while waited < 300:
                try:
                    cur = driver.current_url
                    if cur != last_url:
                        print(f"[Lablog] 현재 URL: {cur}")
                        last_url = cur
                    if "lablog.co.kr/keyword" in cur:
                        # textarea 보이면 페이지 로드 완료로 간주
                        tas = driver.find_elements(By.TAG_NAME, "textarea")
                        if any(t.is_displayed() for t in tas):
                            break
                except Exception: pass
                time.sleep(3); waited += 3
                if waited % 30 == 0:  # 30초마다 진행 표시
                    print(f"[Lablog] 로그인 대기 중... {waited}/300초")
            if waited >= 300:
                return ("⚠️ 블연플: 5분 내 로그인/페이지 로드 미완료.\n"
                        "  · 열린 Chrome 창이 보이는지 확인\n"
                        "  · 구글 로그인 후 https://lablog.co.kr/keyword/keywordMultiSearch 로 이동되어야 함\n"
                        "  · 다시 시도해주세요.")
            print(f"[Lablog] 페이지 진입 ({waited}초 소요)")

            chunks = [keywords[i:i+QUERY_SIZE] for i in range(0, len(keywords), QUERY_SIZE)]
            total_chunks = len(chunks)
            total_keywords = len(keywords)
            if not chunks:
                return "⚠️ 블연플: 조회할 키워드 없음"

            print(f"[Lablog] ▶ 시작: 총 {total_keywords}개 키워드 → "
                  f"{total_chunks}개 쿼리(5개씩) → {(total_keywords + BATCH_SIZE - 1) // BATCH_SIZE}개 batch(60개당)")

            for ci, chunk in enumerate(chunks):
                try:
                    print(f"[Lablog] 쿼리 {ci+1}/{total_chunks}: {chunk}")
                    # 1) textarea 폴링 (최대 20초) — 사라졌으면 페이지 재로드
                    ta = None
                    for poll in range(20):
                        for t in driver.find_elements(By.TAG_NAME, "textarea"):
                            try:
                                if t.is_displayed() and t.is_enabled():
                                    ta = t; break
                            except Exception: pass
                        if ta is not None: break
                        time.sleep(1)
                    if ta is None:
                        print(f"  ⚠️ textarea 20초 대기 후에도 없음 (현재 URL: {driver.current_url})")
                        # 페이지가 다른 곳으로 이동했으면 키워드 페이지로 복귀
                        if "lablog.co.kr/keyword" not in driver.current_url:
                            print("  · 다른 페이지로 이동된 듯 → 키워드 페이지로 복귀")
                            try:
                                driver.get(self.LABLOG_URL); time.sleep(5)
                                for t in driver.find_elements(By.TAG_NAME, "textarea"):
                                    if t.is_displayed() and t.is_enabled():
                                        ta = t; break
                            except Exception as e:
                                print(f"  · 복귀 실패: {e}")
                        if ta is None:
                            print("  ⚠️ 건너뜀")
                            continue
                    try:
                        ta.click(); time.sleep(0.2)
                        ta.clear()
                    except Exception: pass
                    try: safe_paste(driver, ta, "\n".join(chunk))
                    except Exception:
                        try: ta.send_keys("\n".join(chunk))
                        except Exception: pass
                    time.sleep(0.5)

                    # 2) 체크박스 토글 (첫 쿼리만)
                    if ci == 0:
                        try:
                            for cb in driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
                                label_txt = ''
                                try:
                                    lid = cb.get_attribute('id') or ''
                                    if lid:
                                        l = driver.find_elements(By.CSS_SELECTOR, f"label[for='{lid}']")
                                        if l: label_txt = l[0].text or ''
                                except Exception: pass
                                want = None
                                if '자동완성' in label_txt: want = options.get('autocomplete', True)
                                elif '연관' in label_txt: want = options.get('related', True)
                                if want is not None and cb.is_selected() != want:
                                    try: cb.click()
                                    except Exception: pass
                        except Exception: pass

                    # 3) 검색 버튼 클릭 — textarea 근처에서만 찾기 (사이드바 메뉴 오클릭 방지)
                    pre_url = driver.current_url
                    click_ok = False

                    def find_search_button_near(textarea):
                        """textarea의 조상 컨테이너 4단계 내에서 검색 버튼 찾기."""
                        container = textarea
                        for _ in range(5):
                            try:
                                container = container.find_element(By.XPATH, "./parent::*")
                            except Exception:
                                return None
                            try:
                                btns = container.find_elements(By.TAG_NAME, "button")
                                # textarea 다음에 오는 큰 버튼 우선 (보통 검색 버튼은 입력란 바로 아래)
                                candidates = []
                                for b in btns:
                                    try:
                                        if not (b.is_displayed() and b.is_enabled()): continue
                                        sz = b.size
                                        # 너무 작은 아이콘 버튼은 제외 (체크박스 옆 닫기 버튼 등)
                                        if sz.get('width', 0) < 30 or sz.get('height', 0) < 20:
                                            continue
                                        candidates.append((b, sz.get('width', 0)))
                                    except Exception: pass
                                if candidates:
                                    # 가장 큰 버튼 = 보통 메인 검색 버튼
                                    candidates.sort(key=lambda x: -x[1])
                                    return candidates[0][0]
                            except Exception: pass
                        return None

                    btn = find_search_button_near(ta)
                    if btn is not None:
                        try:
                            txt = (btn.text or '').strip() or btn.get_attribute('aria-label') or '(아이콘)'
                            btn.click()
                            print(f"  · 검색 버튼 클릭: '{txt[:30]}' (size={btn.size})")
                            click_ok = True
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click()", btn)
                                print("  · JS 클릭")
                                click_ok = True
                            except Exception as e:
                                print(f"  · 검색 버튼 클릭 실패: {e}")

                    # fallback 1: Ctrl+Enter on textarea
                    if not click_ok:
                        try:
                            from selenium.webdriver.common.keys import Keys
                            ta.send_keys(Keys.CONTROL, Keys.RETURN); click_ok = True
                            print("  · Ctrl+Enter")
                        except Exception: pass

                    # fallback 2: Enter on textarea
                    if not click_ok:
                        try:
                            from selenium.webdriver.common.keys import Keys
                            ta.send_keys(Keys.RETURN); click_ok = True
                            print("  · Enter")
                        except Exception: pass

                    if not click_ok:
                        print("  ⚠️ 검색 버튼 못 찾음 — 건너뜀")
                        continue

                    # 4) 결과 대기 + 수집 — table / role=row / DataGrid / div-based 모두 시도
                    time.sleep(5)

                    # URL이 바뀌었으면 사이드바 메뉴 오클릭 가능성 — 복귀
                    if driver.current_url != pre_url and "lablog.co.kr/keyword" not in driver.current_url:
                        print(f"  ⚠️ URL 변경 감지: {pre_url} → {driver.current_url}")
                        print("  · 키워드 페이지로 복귀")
                        try:
                            driver.get(self.LABLOG_URL); time.sleep(5)
                        except Exception: pass
                        continue  # 이번 청크는 결과 없음으로 처리

                    chunk_rows = []

                    # 첫 쿼리에서 페이지 구조 광범위 디버그
                    if ci == 0:
                        try:
                            print(f"  [DEBUG] 검색 후 URL: {driver.current_url}")
                            print(f"  [DEBUG] table: {len(driver.find_elements(By.TAG_NAME, 'table'))}, "
                                  f"role=row: {len(driver.find_elements(By.CSS_SELECTOR, '[role=row]'))}, "
                                  f"role=grid: {len(driver.find_elements(By.CSS_SELECTOR, '[role=grid]'))}, "
                                  f"MuiDataGrid: {len(driver.find_elements(By.CSS_SELECTOR, '[class*=MuiDataGrid]'))}")
                            # data/result 관련 클래스 div 미리보기
                            print(f"  [DEBUG] 결과처럼 보이는 div (최대 5개):")
                            for d in driver.find_elements(By.TAG_NAME, "div")[:200]:
                                try:
                                    cls = d.get_attribute('class') or ''
                                    if not cls: continue
                                    low = cls.lower()
                                    if any(kw in low for kw in ['row', 'cell', 'data', 'result', 'table', 'grid', 'list']):
                                        txt = (d.text or '')[:50].replace('\n', ' | ')
                                        if txt and 5 < len(d.text) < 500:
                                            print(f"    class='{cls[:60]}' text='{txt}'")
                                except Exception: pass
                        except Exception as e:
                            print(f"  [DEBUG] 페이지 분석 실패: {e}")

                    # 패턴 1: <table>
                    try:
                        all_tables = driver.find_elements(By.TAG_NAME, "table")
                        best_tbl = None; max_rows = 0
                        for tbl in all_tables:
                            try:
                                trs = tbl.find_elements(By.TAG_NAME, "tr")
                                if len(trs) > max_rows:
                                    max_rows = len(trs); best_tbl = tbl
                            except Exception: pass
                        if best_tbl is not None:
                            if not getattr(self, '_lablog_headers', None):
                                try:
                                    ths = best_tbl.find_elements(By.TAG_NAME, "th")
                                    if ths:
                                        self._lablog_headers = [(c.text or '').strip() for c in ths]
                                except Exception: pass
                            for tr in best_tbl.find_elements(By.TAG_NAME, "tr"):
                                tds = tr.find_elements(By.TAG_NAME, "td")
                                if len(tds) >= 2:
                                    row = [(c.text or '').strip() for c in tds]
                                    if any(row): chunk_rows.append(row)
                    except Exception: pass

                    # 패턴 2: role=row + role=cell/gridcell (Material-UI DataGrid 등)
                    if not chunk_rows:
                        try:
                            for r in driver.find_elements(By.CSS_SELECTOR, "[role=row]"):
                                cells = r.find_elements(By.CSS_SELECTOR, "[role=cell], [role=gridcell]")
                                if len(cells) >= 2:
                                    row = [(c.text or '').strip() for c in cells]
                                    if any(row): chunk_rows.append(row)
                        except Exception: pass

                    # 패턴 3: div-based grid (class에 row/cell 포함)
                    if not chunk_rows:
                        try:
                            row_divs = driver.find_elements(By.CSS_SELECTOR, "[class*=row]:not(table):not(tr)")
                            for r in row_divs[:200]:
                                try:
                                    cells = r.find_elements(By.CSS_SELECTOR, "[class*=cell], [class*=col]")
                                    if len(cells) >= 2:
                                        row = [(c.text or '').strip() for c in cells]
                                        if any(row) and not all(len(s) > 200 for s in row):
                                            chunk_rows.append(row)
                                except Exception: pass
                        except Exception: pass

                    if ci == 0:
                        print(f"  [DEBUG] 패턴별 결과: chunk_rows={len(chunk_rows)}행")

                    batch_rows.extend(chunk_rows)
                    all_results.extend(chunk_rows)
                    keywords_in_batch += len(chunk)
                    print(f"  · 결과 {len(chunk_rows)}행 수집 (batch {batch_num}: {keywords_in_batch}/{BATCH_SIZE} 키워드)")

                    # 5) batch가 60개 채워졌거나 마지막 쿼리면 엑셀 저장
                    is_last_chunk = (ci == total_chunks - 1)
                    batch_full = keywords_in_batch >= BATCH_SIZE
                    if batch_rows and (batch_full or is_last_chunk):
                        saved = self._lablog_save_batch(batch_rows, batch_num,
                                                       getattr(self, '_lablog_headers', None))
                        if saved:
                            saved_paths.append(saved)
                            print(f"  💾 batch {batch_num} 저장: {saved} ({len(batch_rows)}행)")
                            try: self.main.log(f"💾 블연플 batch {batch_num} 저장: {saved}")
                            except Exception: pass
                        batch_rows = []
                        batch_num += 1
                        keywords_in_batch = 0
                except Exception as e:
                    try: print(f"[Lablog] 쿼리 {ci+1} 실패: {e}")
                    except Exception: pass

            print(f"[Lablog] ✅ 완료: 총 {len(all_results)}행, batch 파일 {len(saved_paths)}개 저장됨")
            try:
                self.main.log(f"✅ 블연플 완료: 총 {len(all_results)}행, "
                              f"batch 파일 {len(saved_paths)}개 → agency_state/lablog_batches/")
            except Exception: pass
            return all_results
        finally:
            # driver는 닫지 않음 — 사용자가 브라우저에서 결과를 확인 가능
            print("[Lablog] worker 종료 (브라우저는 유지)")

    def _lablog_save_batch(self, rows, batch_num, headers=None):
        """batch 결과를 ./agency_state/lablog_batches/ 폴더에 저장.
        openpyxl이 있으면 .xlsx, 없으면 .csv."""
        from pathlib import Path as _P
        from datetime import datetime as _dt
        out_dir = _P('agency_state/lablog_batches')
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now().strftime('%Y%m%d_%H%M%S')
        try:
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active
            ws.title = f"Batch{batch_num}"
            if headers: ws.append(headers)
            for row in rows: ws.append(row)
            path = out_dir / f"lablog_batch{batch_num:02d}_{ts}.xlsx"
            wb.save(path)
            return str(path)
        except ImportError:
            import csv as _csv
            path = out_dir / f"lablog_batch{batch_num:02d}_{ts}.csv"
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = _csv.writer(f)
                if headers: w.writerow(headers)
                for row in rows: w.writerow(row)
            return str(path)
        except Exception as e:
            print(f"[Lablog] batch 저장 실패: {e}")
            return None

    def _on_lablog_done(self, result):
        if isinstance(result, str):
            self.main.log(result); return
        if not isinstance(result, list):
            return
        self._lablog_results = result
        self._render_lablog_table()
        self.main.log(f"✅ 블연플: {len(result)}개 행")
        # 결과 즉시 저장
        self._state_save()

    def export_lablog_excel(self):
        path, _ = QFileDialog.getSaveFileName(self, "엑셀 저장", "lablog_keywords.csv", "CSV (*.csv)")
        if not path: return
        try:
            import csv
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(["키워드", "월간 검색량", "자동완성", "연관검색어"])
                for row in self._lablog_results:
                    padded = list(row[:4]) + [''] * max(0, 4 - len(row))
                    w.writerow(padded)
            self.main.log(f"💾 저장: {path}")
        except Exception as e:
            self.main.log(f"❌ 저장 실패: {e}")


class AgencyMorphologyPage(_AgencyStateMixin, QWidget):
    """2. 형태소 분석 — 네이버 블로그 상위글에서 제목 패턴/본문 통계 + SEO 제목 5개"""
    STATE_FILE = 'agency_state/morphology.json'

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._analysis_data = []
        self._analysis_keyword = ''
        self._build_ui()
        self._setup_autosave(
            self.search_kw.textChanged,
            self.top_n.valueChanged,
        )
        QTimer.singleShot(100, self._state_load)

    def _serialize_state(self):
        return {
            'search_kw': self.search_kw.text(),
            'top_n': self.top_n.value(),
            'analysis_data': self._analysis_data,
            'analysis_keyword': self._analysis_keyword,
            'seo_titles': self.seo_titles.toPlainText(),
        }

    def _deserialize_state(self, state):
        self.search_kw.setText(state.get('search_kw', ''))
        self.top_n.setValue(state.get('top_n', 10))
        self.seo_titles.setPlainText(state.get('seo_titles', ''))
        items = state.get('analysis_data', [])
        kw = state.get('analysis_keyword', '')
        if items:
            self._analysis_data = items
            self._analysis_keyword = kw
            # 테이블 재구성 (분석 결과 화면)
            self._on_analysis_done({'keyword': kw, 'items': items})

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("🔬 2. 형태소 분석 (블로그 상위글)")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        in_g = QGroupBox("📥 분석 입력")
        in_lay = QHBoxLayout(in_g)
        in_lay.addWidget(QLabel("검색 키워드:"))
        self.search_kw = QLineEdit(); self.search_kw.setPlaceholderText("예: 부산 광안리 맛집")
        in_lay.addWidget(self.search_kw, 2)
        in_lay.addWidget(QLabel("상위 N개:"))
        self.top_n = QSpinBox(); self.top_n.setRange(1, 30); self.top_n.setValue(10)
        in_lay.addWidget(self.top_n)
        self.run_btn = QPushButton("🔬 분석 시작")
        self.run_btn.clicked.connect(self.run_analysis)
        in_lay.addWidget(self.run_btn)
        root.addWidget(in_g)

        self.tabs = _QTabWidget_Agency()

        # 제목 탭
        tt = QWidget(); tl = QVBoxLayout(tt)
        tl.addWidget(QLabel("📝 분석된 제목들"))
        self.title_table = QTableWidget(0, 3)
        self.title_table.setHorizontalHeaderLabels(["제목", "키워드 위치", "패턴"])
        self.title_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tl.addWidget(self.title_table, 1)
        tl.addWidget(QLabel("✨ SEO 최적화 제목 5개 (GPT 생성)"))
        self.seo_titles = QTextEdit(); self.seo_titles.setMaximumHeight(140)
        tl.addWidget(self.seo_titles)
        self.gen_titles_btn = QPushButton("🪄 GPT로 SEO 제목 5개 생성")
        self.gen_titles_btn.clicked.connect(self.gen_seo_titles)
        self.gen_titles_btn.setEnabled(False)
        tl.addWidget(self.gen_titles_btn)
        self.tabs.addTab(tt, "📝 제목 분석")

        # 본문 탭
        bt = QWidget(); bl = QVBoxLayout(bt)
        self.body_summary = QLabel("아직 분석되지 않음")
        self.body_summary.setStyleSheet("font-size:13px; padding:8px;")
        self.body_summary.setWordWrap(True)
        bl.addWidget(self.body_summary)
        self.body_table = QTableWidget(0, 6)
        self.body_table.setHorizontalHeaderLabels(["URL", "키워드 빈도", "이미지", "영상", "지도", "해시태그"])
        self.body_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        bl.addWidget(self.body_table, 1)
        self.tabs.addTab(bt, "📰 본문 분석")
        root.addWidget(self.tabs, 1)

    def run_analysis(self):
        kw = self.search_kw.text().strip()
        if not kw:
            self.main.log("⚠️ 검색 키워드 입력"); return
        self.run_btn.setEnabled(False); self.run_btn.setText("🔬 분석 중...")
        t = WorkerThread(self._analyze, kw, self.top_n.value())
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self._on_analysis_done)
        t.finished_signal.connect(lambda: (
            self.run_btn.setEnabled(True), self.run_btn.setText("🔬 분석 시작")))
        t.start()

    def _analyze(self, keyword, n):
        from urllib.parse import quote
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        urls = []
        try:
            search_url = f"https://search.naver.com/search.naver?where=post&query={quote(keyword)}"
            r = requests.get(search_url, headers=headers, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.select("a"):
                href = a.get('href', '')
                if 'blog.naver.com' in href and href.startswith('http') and href not in urls:
                    urls.append(href)
                if len(urls) >= n * 2:
                    break
        except Exception as e:
            return f"⚠️ 검색 실패: {e}"
        if not urls:
            return "⚠️ 검색 결과 없음"

        import re as _re
        results = []
        for url in urls[:n]:
            try:
                doc_url = url
                if "blog.naver.com" in url and "PostView" not in url:
                    m = _re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url)
                    if m:
                        doc_url = f"https://blog.naver.com/PostView.naver?blogId={m.group(1)}&logNo={m.group(2)}"
                r = requests.get(doc_url, headers=headers, timeout=15)
                soup = BeautifulSoup(r.text, 'html.parser')
                title = ''
                for sel in ['.se-title-text', '.pcol1', '.se_title', 'title']:
                    el = soup.select_one(sel)
                    if el and el.get_text(strip=True):
                        title = el.get_text(strip=True); break
                body_el = soup.select_one('.se-main-container') or soup.select_one('#postViewArea')
                body_text = body_el.get_text(' ', strip=True) if body_el else soup.get_text(' ', strip=True)
                imgs = body_el.find_all('img') if body_el else soup.find_all('img')
                vids = (body_el.find_all('video') if body_el else soup.find_all('video')) + \
                       (body_el.find_all('iframe') if body_el else soup.find_all('iframe'))
                map_count = 0
                for el in soup.find_all(class_=True):
                    cls = ' '.join(el.get('class', []))
                    if 'se-map' in cls or 'se-module-map' in cls:
                        map_count += 1
                tags = []
                for el in soup.select('.post_tag, .tag, a'):
                    t = el.get_text(strip=True)
                    if t.startswith('#') and 1 < len(t) < 30:
                        tags.append(t)
                tags = list(dict.fromkeys(tags))[:30]
                kw_pos = title.find(keyword) if keyword in title else -1
                kw_freq = body_text.count(keyword) if keyword else 0
                results.append({
                    'url': doc_url, 'title': title,
                    'keyword_pos': kw_pos, 'keyword_freq': kw_freq,
                    'image_count': len(imgs), 'video_count': len(vids),
                    'map_count': map_count, 'tags': tags,
                    'body_length': len(body_text),
                })
            except Exception as e:
                results.append({'url': url, 'title': '', 'error': str(e)})
        return {'keyword': keyword, 'items': results}

    def _on_analysis_done(self, result):
        if isinstance(result, str):
            self.main.log(result); return
        items = result.get('items', [])
        keyword = result.get('keyword', '')
        self._analysis_data = items

        self.title_table.setRowCount(len(items))
        for i, it in enumerate(items):
            self.title_table.setItem(i, 0, QTableWidgetItem(it.get('title', '')))
            pos = it.get('keyword_pos', -1)
            self.title_table.setItem(i, 1, QTableWidgetItem(f"{pos}자" if pos >= 0 else "(미포함)"))
            t = it.get('title', '')
            pattern = []
            if '!' in t: pattern.append('느낌표')
            if '?' in t: pattern.append('질문형')
            if any(n in t for n in '0123456789'): pattern.append('숫자')
            if any(c in t for c in ['추천', '꿀팁', '정리', '후기', 'BEST', 'TOP']): pattern.append('정보형')
            self.title_table.setItem(i, 2, QTableWidgetItem(', '.join(pattern) or '-'))

        self.body_table.setRowCount(len(items))
        total_kw = total_img = total_vid = total_map = 0
        for i, it in enumerate(items):
            self.body_table.setItem(i, 0, QTableWidgetItem(it.get('url', '')))
            self.body_table.setItem(i, 1, QTableWidgetItem(str(it.get('keyword_freq', 0))))
            self.body_table.setItem(i, 2, QTableWidgetItem(str(it.get('image_count', 0))))
            self.body_table.setItem(i, 3, QTableWidgetItem(str(it.get('video_count', 0))))
            self.body_table.setItem(i, 4, QTableWidgetItem(str(it.get('map_count', 0))))
            self.body_table.setItem(i, 5, QTableWidgetItem(', '.join(it.get('tags', []))))
            total_kw += it.get('keyword_freq', 0)
            total_img += it.get('image_count', 0)
            total_vid += it.get('video_count', 0)
            total_map += it.get('map_count', 0)

        n = len(items) or 1
        self.body_summary.setText(
            f"📊 '{keyword}' 상위 {n}개 평균: "
            f"키워드 {total_kw/n:.1f}회 · 이미지 {total_img/n:.1f}장 · "
            f"영상 {total_vid/n:.1f}개 · 지도 {total_map/n:.1f}개"
        )
        self.gen_titles_btn.setEnabled(True)
        self.main.log(f"✅ 형태소 분석: {n}개 글")
        # 분석 결과 즉시 저장
        self._analysis_keyword = keyword
        self._state_save()

    def gen_seo_titles(self):
        if not self._analysis_data:
            self.main.log("⚠️ 분석 데이터 없음"); return
        keyword = self.search_kw.text().strip()
        titles = [it.get('title', '') for it in self._analysis_data if it.get('title')]
        api_key = self.main.config.get('openai_api_key', '')
        if not api_key:
            self.main.log("⚠️ 설정에서 OpenAI API 키를 입력하세요"); return
        prompt = (
            f"다음은 '{keyword}' 키워드로 검색된 네이버 블로그 상위글의 제목들입니다.\n\n"
            + "\n".join(f"- {t}" for t in titles[:15])
            + f"\n\n위 패턴을 참고하여 '{keyword}' 키워드를 포함한 SEO 최적화 블로그 제목을 5개 만들어주세요.\n"
              "각 제목은 한 줄에 하나씩, 번호 없이 출력해주세요."
        )
        self.gen_titles_btn.setEnabled(False); self.gen_titles_btn.setText("🪄 생성 중...")
        t = WorkerThread(call_openai_text, api_key, prompt, 'gpt-4o')
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(lambda r: self.seo_titles.setPlainText(r if isinstance(r, str) else str(r)))
        t.finished_signal.connect(lambda: (
            self.gen_titles_btn.setEnabled(True),
            self.gen_titles_btn.setText("🪄 GPT로 SEO 제목 5개 생성")))
        t.start()


class AgencyImagePage(_AgencyStateMixin, QWidget):
    """3. 이미지 — (1) 업로드 이미지 변형, (2) 본문 기반 GPT 이미지"""
    STATE_FILE = 'agency_state/image.json'

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._generated = []
        self._build_ui()
        self._setup_autosave(
            self.src_path.textChanged,
            self.var_prompt.textChanged,
            self.var_size.currentTextChanged,
            self.var_count.valueChanged,
            self.body_input.textChanged,
            self.body_prompt.textChanged,
            self.body_size.currentTextChanged,
            self.body_count.valueChanged,
        )
        QTimer.singleShot(100, self._state_load)

    def _serialize_state(self):
        return {
            'src_path': self.src_path.text(),
            'var_prompt': self.var_prompt.toPlainText(),
            'var_size': self.var_size.currentText(),
            'var_count': self.var_count.value(),
            'body_input': self.body_input.toPlainText(),
            'body_prompt': self.body_prompt.text(),
            'body_size': self.body_size.currentText(),
            'body_count': self.body_count.value(),
            'generated': self._generated,
            'result_list_text': self.result_list.toPlainText(),
        }

    def _deserialize_state(self, state):
        self.src_path.setText(state.get('src_path', ''))
        self.var_prompt.setPlainText(state.get('var_prompt', ''))
        idx = self.var_size.findText(state.get('var_size', ''))
        if idx >= 0: self.var_size.setCurrentIndex(idx)
        self.var_count.setValue(state.get('var_count', 1))
        self.body_input.setPlainText(state.get('body_input', ''))
        self.body_prompt.setText(state.get('body_prompt', ''))
        idx = self.body_size.findText(state.get('body_size', ''))
        if idx >= 0: self.body_size.setCurrentIndex(idx)
        self.body_count.setValue(state.get('body_count', 1))
        self._generated = state.get('generated', [])
        txt = state.get('result_list_text', '')
        if txt:
            self.result_list.setPlainText(txt)

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("🖼️ 3. 이미지 생성 (대행/후기성)")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        root.addWidget(title)

        self.tabs = _QTabWidget_Agency()
        self.tabs.addTab(self._build_var_tab(), "📷 업로드 이미지 → 변형")
        self.tabs.addTab(self._build_body_tab(), "✍️ 본문 → 프롬프트 이미지")
        root.addWidget(self.tabs, 1)

        root.addWidget(QLabel("📁 생성된 이미지 경로"))
        self.result_list = QTextEdit(); self.result_list.setReadOnly(True)
        self.result_list.setMaximumHeight(120)
        root.addWidget(self.result_list)

    def _build_var_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("📁 원본 이미지"))
        h = QHBoxLayout()
        self.src_path = QLineEdit(); self.src_path.setReadOnly(True)
        h.addWidget(self.src_path, 1)
        pick = QPushButton("이미지 선택"); pick.clicked.connect(self.pick_image)
        h.addWidget(pick)
        lay.addLayout(h)

        lay.addWidget(QLabel("✏️ 변형 지시 프롬프트"))
        self.var_prompt = QTextEdit()
        self.var_prompt.setPlaceholderText("예: 이 이미지를 그대로 두되 분위기를 따뜻한 카페로 변경")
        self.var_prompt.setMaximumHeight(100)
        lay.addWidget(self.var_prompt)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("크기:"))
        self.var_size = QComboBox(); self.var_size.addItems(["1024x1024", "1024x1536", "1536x1024"])
        opts.addWidget(self.var_size)
        opts.addWidget(QLabel("개수:"))
        self.var_count = QSpinBox(); self.var_count.setRange(1, 5); self.var_count.setValue(1)
        opts.addWidget(self.var_count)
        opts.addStretch()
        lay.addLayout(opts)

        self.var_btn = QPushButton("🎨 변형 이미지 생성")
        self.var_btn.clicked.connect(self.run_image_var)
        lay.addWidget(self.var_btn)
        lay.addStretch()
        return w

    def _build_body_tab(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("📄 본문 (이미지 컨텍스트)"))
        self.body_input = QTextEdit()
        self.body_input.setPlaceholderText("본문을 붙여넣으세요. GPT가 본문을 분석해 이미지 프롬프트로 변환합니다.")
        lay.addWidget(self.body_input, 1)

        lay.addWidget(QLabel("✏️ 추가 프롬프트 (선택)"))
        self.body_prompt = QLineEdit()
        self.body_prompt.setPlaceholderText("예: 사진 스타일, 밝은 분위기")
        lay.addWidget(self.body_prompt)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("크기:"))
        self.body_size = QComboBox(); self.body_size.addItems(["1024x1024", "1024x1536", "1536x1024"])
        opts.addWidget(self.body_size)
        opts.addWidget(QLabel("개수:"))
        self.body_count = QSpinBox(); self.body_count.setRange(1, 5); self.body_count.setValue(1)
        opts.addWidget(self.body_count)
        opts.addStretch()
        lay.addLayout(opts)

        self.body_btn = QPushButton("🎨 본문 기반 이미지 생성")
        self.body_btn.clicked.connect(self.run_body_image)
        lay.addWidget(self.body_btn)
        lay.addStretch()
        return w

    def pick_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "이미지 선택", "", "이미지 (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.src_path.setText(path)

    def _save_b64(self, b64data, prompt):
        out_dir = Path("generated_images"); out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out = out_dir / f"agency_{ts}.png"
        try:
            with open(out, 'wb') as f:
                f.write(base64.b64decode(b64data))
            self._generated.append({'path': str(out), 'prompt': prompt})
            return str(out)
        except Exception:
            return None

    def _append_result_line(self, path, prompt):
        if path:
            self.result_list.append(f"✅ {path}  | {prompt[:60]}")

    def run_image_var(self):
        src = self.src_path.text().strip()
        if not src or not Path(src).exists():
            self.main.log("⚠️ 원본 이미지를 선택하세요"); return
        prompt = self.var_prompt.toPlainText().strip()
        if not prompt:
            self.main.log("⚠️ 변형 프롬프트를 입력하세요"); return
        api_key = self.main.config.get('openai_api_key', '')
        if not api_key:
            self.main.log("⚠️ 설정에서 OpenAI API 키를 입력하세요"); return
        size = self.var_size.currentText(); count = self.var_count.value()
        self.var_btn.setEnabled(False); self.var_btn.setText("🎨 생성 중...")

        def worker():
            paths = []
            try:
                client = build_openai_client(api_key)
                for _ in range(count):
                    with open(src, 'rb') as f:
                        resp = client.images.edit(model="gpt-image-1", image=f, prompt=prompt, size=size)
                    data = resp.data[0]
                    b64 = getattr(data, 'b64_json', None)
                    if b64:
                        p = self._save_b64(b64, prompt)
                        if p: paths.append(p)
                return paths
            except Exception as e:
                return f"❌ 이미지 변형 실패: {e}"

        t = WorkerThread(worker)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self._on_var_done)
        t.finished_signal.connect(lambda: (
            self.var_btn.setEnabled(True), self.var_btn.setText("🎨 변형 이미지 생성")))
        t.start()

    def _on_var_done(self, result):
        if isinstance(result, str):
            self.main.log(result); return
        if not isinstance(result, list):
            return
        for p in result:
            self._append_result_line(p, self.var_prompt.toPlainText().strip())
        self.main.log(f"✅ 변형 이미지 {len(result)}장")
        self._state_save()

    def run_body_image(self):
        body = self.body_input.toPlainText().strip()
        if not body:
            self.main.log("⚠️ 본문을 입력하세요"); return
        extra = self.body_prompt.text().strip()
        api_key = self.main.config.get('openai_api_key', '')
        if not api_key:
            self.main.log("⚠️ 설정에서 OpenAI API 키를 입력하세요"); return
        size = self.body_size.currentText(); count = self.body_count.value()
        self.body_btn.setEnabled(False); self.body_btn.setText("🎨 생성 중...")

        def worker():
            paths = []
            try:
                summary_prompt = (
                    "다음 블로그 본문에 어울리는 메인 이미지 프롬프트를 영어로 1줄로 작성해줘. "
                    "사진 스타일, 구체적인 피사체와 분위기 포함:\n\n" + body[:1500]
                    + (f"\n\n추가 지시: {extra}" if extra else "")
                )
                image_prompt = call_openai_text(api_key, summary_prompt, model='gpt-4o')
                if not image_prompt:
                    return "❌ 이미지 프롬프트 생성 실패"
                client = build_openai_client(api_key)
                for _ in range(count):
                    resp = client.images.generate(model="gpt-image-1", prompt=image_prompt, size=size)
                    data = resp.data[0]
                    b64 = getattr(data, 'b64_json', None)
                    if b64:
                        p = self._save_b64(b64, image_prompt)
                        if p: paths.append(p)
                return paths
            except Exception as e:
                return f"❌ 본문 이미지 생성 실패: {e}"

        t = WorkerThread(worker)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self._on_body_done)
        t.finished_signal.connect(lambda: (
            self.body_btn.setEnabled(True), self.body_btn.setText("🎨 본문 기반 이미지 생성")))
        t.start()

    def _on_body_done(self, result):
        if isinstance(result, str):
            self.main.log(result); return
        if not isinstance(result, list):
            return
        for p in result:
            self._append_result_line(p, '(본문 기반)')
        self.main.log(f"✅ 본문 이미지 {len(result)}장")
        self._state_save()


class AgencyUploadPage(_AgencyStateMixin, QWidget):
    """4. 업로드 — 네이버 블로그, 인용구/굵게/정렬/줄바꿈 (기존 함수 재사용)"""
    STATE_FILE = 'agency_state/upload.json'

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._build_ui()
        self._setup_autosave(
            self.account_combo.currentTextChanged,
            self.save_mode.currentTextChanged,
            self.alignment_combo.currentTextChanged,
            self.title_input.textChanged,
            self.body_editor.textChanged,
            self.image_paths.textChanged,
        )
        QTimer.singleShot(100, self._state_load)

    def _serialize_state(self):
        return {
            'account': self.account_combo.currentText(),
            'save_mode': self.save_mode.currentIndex(),
            'alignment': self.alignment_combo.currentIndex(),
            'title': self.title_input.text(),
            'body': self.body_editor.toPlainText(),
            'image_paths': self.image_paths.text(),
        }

    def _deserialize_state(self, state):
        acc = state.get('account', '')
        if acc:
            idx = self.account_combo.findText(acc)
            if idx >= 0: self.account_combo.setCurrentIndex(idx)
            elif acc and self.account_combo.findText(acc) < 0:
                self.account_combo.addItem(acc)
                self.account_combo.setCurrentText(acc)
        self.save_mode.setCurrentIndex(state.get('save_mode', 0))
        self.alignment_combo.setCurrentIndex(state.get('alignment', 0))
        self.title_input.setText(state.get('title', ''))
        self.body_editor.setPlainText(state.get('body', ''))
        self.image_paths.setText(state.get('image_paths', ''))

    def _build_ui(self):
        outer = QVBoxLayout(self)
        title_top = QLabel("🚀 4. 업로드 (네이버 블로그)")
        title_top.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        outer.addWidget(title_top)

        self._upload_tabs = _QTabWidget_Agency()
        manual_w = QWidget()
        self._build_manual_tab(manual_w)
        self._upload_tabs.addTab(manual_w, "✍️ 수동 업로드")

        admong_w = QWidget()
        self._build_admong_tab(admong_w)
        self._upload_tabs.addTab(admong_w, "🤖 애드몽 퍼플렉시티")

        outer.addWidget(self._upload_tabs, 1)

    def _build_manual_tab(self, parent):
        root = QVBoxLayout(parent)

        info = QHBoxLayout()
        info.addWidget(QLabel("계정:"))
        self.account_combo = QComboBox()
        try:
            for acc in self.main.config.get('naver_accounts', []) or []:
                self.account_combo.addItem(acc.get('id', ''))
            if self.account_combo.count() == 0 and self.main.config.get('naver_id'):
                self.account_combo.addItem(self.main.config.get('naver_id'))
        except Exception: pass
        info.addWidget(self.account_combo, 1)
        info.addWidget(QLabel("발행 모드:"))
        self.save_mode = QComboBox()
        self.save_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        info.addWidget(self.save_mode)
        info.addWidget(QLabel("전체 정렬:"))
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["⬅️ 좌측", "⬆️ 가운데", "➡️ 우측", "↔️ 양쪽"])
        info.addWidget(self.alignment_combo)
        root.addLayout(info)

        root.addWidget(QLabel("📝 제목"))
        self.title_input = QLineEdit()
        root.addWidget(self.title_input)

        toolbar = QHBoxLayout()
        bq = QPushButton("❝ 인용구"); bq.clicked.connect(self._tb_quote)
        bb = QPushButton("𝐁 굵게"); bb.clicked.connect(self._tb_bold)
        br = QPushButton("↵ 줄바꿈"); br.clicked.connect(self._tb_linebreak)
        ba_l = QPushButton("⬅️"); ba_l.clicked.connect(lambda: self._tb_align('LEFT'))
        ba_c = QPushButton("⬆️"); ba_c.clicked.connect(lambda: self._tb_align('CENTER'))
        ba_r = QPushButton("➡️"); ba_r.clicked.connect(lambda: self._tb_align('RIGHT'))
        for b in [bq, bb, br]:
            toolbar.addWidget(b)
        toolbar.addWidget(QLabel(" | 단락 정렬:"))
        for b in [ba_l, ba_c, ba_r]:
            toolbar.addWidget(b)
        toolbar.addStretch()
        root.addLayout(toolbar)

        root.addWidget(QLabel("📄 본문 (마크다운 마커 사용 가능)"))
        self.body_editor = QTextEdit()
        self.body_editor.setPlaceholderText(
            "본문을 작성하세요.\n\n"
            "툴바 안내:\n"
            "  ❝ 인용구: 선택한 줄 앞에 '> ' 추가 → 네이버에서 인용구 블록으로 변환\n"
            "  𝐁 굵게: 선택 텍스트를 **굵게** 마커로 감싸기\n"
            "  ⬅️⬆️➡️: 단락 시작에 {{ALIGN:...}} 마커 삽입\n"
            "  ↵ 줄바꿈: 빈 줄 삽입"
        )
        root.addWidget(self.body_editor, 1)

        img_row = QHBoxLayout()
        img_row.addWidget(QLabel("🖼️ 이미지 첨부:"))
        self.image_paths = QLineEdit()
        self.image_paths.setPlaceholderText("쉼표(,)로 이미지 경로 구분 — 자동 첨부는 추후, 현재는 안내만")
        img_row.addWidget(self.image_paths, 1)
        pick = QPushButton("선택"); pick.clicked.connect(self._pick_images)
        img_row.addWidget(pick)
        root.addLayout(img_row)

        h = QHBoxLayout()
        self.upload_btn = QPushButton("🚀 네이버 블로그 업로드")
        self.upload_btn.clicked.connect(self.run_upload)
        h.addWidget(self.upload_btn)
        h.addStretch()
        root.addLayout(h)

    # ═══════════════════════════════════════════════════════════
    #  애드몽 퍼플렉시티 (AdmongPerplexity 통합)
    #  - 엑셀(또는 직접 입력) 키워드 → 네이버 블로그/뉴스 추출
    #  - Perplexity API로 본문 생성 → 네이버 블로그 자동 업로드
    # ═══════════════════════════════════════════════════════════
    def _build_admong_tab(self, parent):
        root = QVBoxLayout(parent)

        hdr = QLabel("🤖 애드몽 퍼플렉시티 자동 작성/업로드")
        hdr.setStyleSheet("font-size:14px; font-weight:bold; color:#FFD700;")
        root.addWidget(hdr)
        desc = QLabel("키워드별로 네이버 블로그/뉴스에서 본문을 추출해 Perplexity로 글을 생성하고, 네이버 블로그에 자동 업로드합니다.")
        desc.setStyleSheet("color:#AAAAAA;"); desc.setWordWrap(True)
        root.addWidget(desc)

        # API & 옵션
        opt = QHBoxLayout()
        opt.addWidget(QLabel("Perplexity API:"))
        self.adm_api_key = QLineEdit()
        self.adm_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.adm_api_key.setPlaceholderText("pplx-...")
        try:
            self.adm_api_key.setText(self.main.config.get('perplexity_api_key', '') or os.getenv('PERPLEXITY_API_KEY', ''))
        except Exception: pass
        opt.addWidget(self.adm_api_key, 1)
        opt.addWidget(QLabel("추출 개수:"))
        self.adm_extract_cnt = QSpinBox(); self.adm_extract_cnt.setRange(1, 20); self.adm_extract_cnt.setValue(2)
        opt.addWidget(self.adm_extract_cnt)
        opt.addWidget(QLabel("소스:"))
        self.adm_src_type = QComboBox(); self.adm_src_type.addItems(["📝 블로그", "📰 뉴스"])
        opt.addWidget(self.adm_src_type)
        root.addLayout(opt)

        # 계정 / 저장 모드
        acc_row = QHBoxLayout()
        acc_row.addWidget(QLabel("블로그 계정(ID):"))
        self.adm_blog_id = QLineEdit()
        self.adm_blog_id.setPlaceholderText("naver blog id (예: myblog)")
        try:
            for a in self.main.config.get('naver_accounts', []) or []:
                if a.get('id'):
                    self.adm_blog_id.setText(a.get('id')); break
            if not self.adm_blog_id.text():
                self.adm_blog_id.setText(self.main.config.get('naver_id', '') or '')
        except Exception: pass
        acc_row.addWidget(self.adm_blog_id, 1)
        acc_row.addWidget(QLabel("저장:"))
        self.adm_save_mode = QComboBox(); self.adm_save_mode.addItems(["💾 임시저장", "⏰ 예약발행", "📤 발행"])
        acc_row.addWidget(self.adm_save_mode)
        acc_row.addWidget(QLabel("예약시간:"))
        self.adm_schedule_time = QLineEdit()
        self.adm_schedule_time.setPlaceholderText("2025-01-15 09:00")
        self.adm_schedule_time.setEnabled(False)
        self.adm_schedule_time.setMaximumWidth(160)
        acc_row.addWidget(self.adm_schedule_time)
        self.adm_save_mode.currentIndexChanged.connect(
            lambda idx: self.adm_schedule_time.setEnabled(idx == 1)
        )
        root.addLayout(acc_row)

        # 엑셀 선택 (선택사항)
        xl_row = QHBoxLayout()
        xl_row.addWidget(QLabel("엑셀(선택):"))
        self.adm_xl_path = QLineEdit()
        self.adm_xl_path.setPlaceholderText("A2:블로그ID, B열:키워드, C/D열:Perplexity 질의 (없으면 아래 표 사용)")
        xl_row.addWidget(self.adm_xl_path, 1)
        b_pick = QPushButton("📂 엑셀 선택")
        b_pick.clicked.connect(self._adm_pick_excel)
        xl_row.addWidget(b_pick)
        b_load = QPushButton("⬇️ 표로 불러오기")
        b_load.clicked.connect(self._adm_load_excel)
        xl_row.addWidget(b_load)
        root.addLayout(xl_row)

        # 키워드 등록 (콤마 구분, 최대 50개)
        kw_row = QHBoxLayout()
        kw_row.addWidget(QLabel("키워드 등록:"))
        self.adm_kw_input = QLineEdit()
        self.adm_kw_input.setPlaceholderText("키워드1, 키워드2, 키워드3 ... (최대 50개, 콤마로 구분)")
        kw_row.addWidget(self.adm_kw_input, 1)
        b_kw_load = QPushButton("⬇️ 표에 등록")
        b_kw_load.clicked.connect(self._adm_load_keywords)
        kw_row.addWidget(b_kw_load)
        root.addLayout(kw_row)

        # 프롬프트 설정 카드
        root.addWidget(QLabel("글쓰기에 사용할 프롬프트 ⓘ"))
        self._adm_prompt_frame = QWidget()
        self._adm_prompt_frame.setStyleSheet(
            "QWidget{background:#2a2a2a; border:1px solid #555; border-radius:6px;}")
        pf_l = QVBoxLayout(self._adm_prompt_frame)
        pf_l.setContentsMargins(10, 8, 10, 8); pf_l.setSpacing(3)
        self._adm_card_title_row = QHBoxLayout()
        _icon = QLabel("📄"); _icon.setStyleSheet("font-size:14px;")
        self._adm_card_title_row.addWidget(_icon)
        self._adm_card_title = QLabel("(선택된 프롬프트 없음)")
        self._adm_card_title.setStyleSheet("color:#ddd; font-weight:bold; font-size:13px;")
        self._adm_card_title_row.addWidget(self._adm_card_title)
        self._adm_card_title_row.addStretch()
        self._adm_card_badge = QLabel("활성")
        self._adm_card_badge.setStyleSheet(
            "background:#4CAF50; color:#fff; font-size:11px; font-weight:bold; "
            "padding:1px 7px; border-radius:3px;")
        self._adm_card_badge_shown = False  # setVisible 대신 동적 추가로 크래시 방지
        pf_l.addLayout(self._adm_card_title_row)
        self._adm_card_preview = QLabel("")
        self._adm_card_preview.setStyleSheet("color:#aaa; font-size:11px;")
        self._adm_card_preview.setWordWrap(True)
        pf_l.addWidget(self._adm_card_preview)
        root.addWidget(self._adm_prompt_frame)

        self.adm_lib_btn = QPushButton("🗂 프롬프트 관리 / 변경    총 0개")
        self.adm_lib_btn.setStyleSheet("padding:5px 12px; text-align:left;")
        self.adm_lib_btn.clicked.connect(self._adm_open_prompt_library)
        root.addWidget(self.adm_lib_btn)

        # 내부 상태용 (UI에 표시하지 않음)
        self.adm_common_q = QTextEdit()
        self.adm_common_q.setMaximumHeight(0)  # 숨김 (setVisible 사용 안함)



        # 키워드/질의 표
        root.addWidget(QLabel("📋 키워드 & Perplexity 질의 (행 단위로 처리)"))
        self.adm_table = QTableWidget(0, 3)
        self.adm_table.setHorizontalHeaderLabels(["키워드", "질의1", "질의2 (옵션)"])
        self.adm_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.adm_table, 1)

        # 표 조작 버튼
        tb_row = QHBoxLayout()
        b_add = QPushButton("＋ 행 추가"); b_add.clicked.connect(lambda: self._adm_add_row())
        b_del = QPushButton("－ 선택 행 삭제"); b_del.clicked.connect(self._adm_del_row)
        b_clr = QPushButton("🗑️ 전체 비우기"); b_clr.clicked.connect(lambda: self.adm_table.setRowCount(0))
        tb_row.addWidget(b_add); tb_row.addWidget(b_del); tb_row.addWidget(b_clr); tb_row.addStretch()
        root.addLayout(tb_row)

        # 실행 + 설정 (위젯 최소화 — 설정은 다이얼로그로 분리)
        run_row = QHBoxLayout()
        self.adm_run_btn = QPushButton("🚀 애드몽 자동 실행")
        self.adm_run_btn.setStyleSheet("background-color:#FFD700; color:#000; font-weight:bold; padding:8px;")
        self.adm_run_btn.clicked.connect(self._adm_run)
        run_row.addWidget(self.adm_run_btn)
        self.adm_settings_btn = QPushButton("⚙️ 포스팅·이미지 옵션")
        self.adm_settings_btn.setStyleSheet("padding:8px;")
        self.adm_settings_btn.clicked.connect(self._adm_open_settings)
        run_row.addWidget(self.adm_settings_btn)
        self.adm_preview_btn = QPushButton("📄 원고/이미지 미리보기")
        self.adm_preview_btn.setStyleSheet("padding:8px;")
        self.adm_preview_btn.clicked.connect(self._adm_show_previews)
        run_row.addWidget(self.adm_preview_btn)
        run_row.addStretch()
        root.addLayout(run_row)

        # 미리보기 저장소
        self.adm_previews = []

        # 첫 빈 행
        self._adm_add_row()

        # 이전 상태 복원 — 훅 연결 전에 동기 호출 (훅 연결 후 호출 시 재진입 크래시)
        try:
            self._adm_load_state()
        except Exception as _e:
            try: self.main.log(f"⚠️ 애드몽 상태 복원 실패: {_e}")
            except Exception: pass

        # 변경사항 자동 저장 훅
        try:
            self.adm_table.itemChanged.connect(lambda *_: self._adm_save_state())
            self.adm_kw_input.textChanged.connect(lambda *_: self._adm_save_state())
            self.adm_common_q.textChanged.connect(lambda *_: self._adm_save_state())
            self.adm_blog_id.textChanged.connect(lambda *_: self._adm_save_state())
            self.adm_api_key.textChanged.connect(lambda *_: self._adm_save_state())
            self.adm_xl_path.textChanged.connect(lambda *_: self._adm_save_state())
            self.adm_extract_cnt.valueChanged.connect(lambda *_: self._adm_save_state())
            self.adm_src_type.currentIndexChanged.connect(lambda *_: self._adm_save_state())
            self.adm_save_mode.currentIndexChanged.connect(lambda *_: self._adm_save_state())
            self.adm_schedule_time.textChanged.connect(lambda *_: self._adm_save_state())
        except Exception:
            pass

    ADM_STATE_PATH = 'adm_state.json'

    def _adm_state_dict(self):
        rows = []
        for r in range(self.adm_table.rowCount()):
            rows.append({
                'kw': (self.adm_table.item(r, 0).text() if self.adm_table.item(r, 0) else ''),
                'q1': (self.adm_table.item(r, 1).text() if self.adm_table.item(r, 1) else ''),
                'q2': (self.adm_table.item(r, 2).text() if self.adm_table.item(r, 2) else ''),
            })
        return {
            'api_key': self.adm_api_key.text(),
            'blog_id': self.adm_blog_id.text(),
            'xl_path': self.adm_xl_path.text(),
            'kw_input': self.adm_kw_input.text(),
            'common_q': self.adm_common_q.toPlainText(),
            'prompt_card_title': self._adm_card_title.text() if hasattr(self, '_adm_card_title') else '',
            'schedule_time': self.adm_schedule_time.text(),
            'extract_cnt': self.adm_extract_cnt.value(),
            'src_type': self.adm_src_type.currentIndex(),
            'save_mode': self.adm_save_mode.currentIndex(),
            'rows': rows,
            'previews': self.adm_previews,
        }

    def _adm_save_state(self):
        try:
            with open(self.ADM_STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._adm_state_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _adm_load_state(self):
        if not os.path.exists(self.ADM_STATE_PATH):
            return
        with open(self.ADM_STATE_PATH, 'r', encoding='utf-8') as f:
            st = json.load(f)
        # 입력 필드 복원
        self.adm_api_key.setText(st.get('api_key', '') or '')
        if st.get('blog_id'): self.adm_blog_id.setText(st.get('blog_id'))
        self.adm_xl_path.setText(st.get('xl_path', '') or '')
        self.adm_kw_input.setText(st.get('kw_input', '') or '')
        self.adm_common_q.setPlainText(st.get('common_q', '') or '')
        # 프롬프트 카드 복원
        saved_title = st.get('prompt_card_title', '')
        saved_q = st.get('common_q', '') or ''
        if saved_title and saved_title != '(선택된 프롬프트 없음)':
            self._adm_card_title.setText(saved_title)
            preview = saved_q[:80].replace('\n', ' ')
            self._adm_card_preview.setText(preview + ('...' if len(saved_q) > 80 else ''))
            if not self._adm_card_badge_shown:
                self._adm_card_title_row.addWidget(self._adm_card_badge)
                self._adm_card_badge_shown = True
        # 라이브러리 총 개수 버튼에 반영
        try:
            import json as _j
            if os.path.exists(PromptLibraryDialog.LIBRARY_FILE):
                with open(PromptLibraryDialog.LIBRARY_FILE, encoding='utf-8') as _lf:
                    _cnt = len(_j.load(_lf))
                self.adm_lib_btn.setText(f"🗂 프롬프트 관리 / 변경    총 {_cnt}개")
        except Exception:
            pass
        try: self.adm_schedule_time.setText(st.get('schedule_time', '') or '')
        except Exception: pass
        try: self.adm_extract_cnt.setValue(int(st.get('extract_cnt', self.adm_extract_cnt.value())))
        except Exception: pass
        try: self.adm_src_type.setCurrentIndex(int(st.get('src_type', 0)))
        except Exception: pass
        try: self.adm_save_mode.setCurrentIndex(int(st.get('save_mode', 0)))
        except Exception: pass
        # 표 복원
        rows = st.get('rows') or []
        if rows:
            self.adm_table.setRowCount(0)
            for r in rows:
                self._adm_add_row(r.get('kw', ''), r.get('q1', ''), r.get('q2', ''))
        # 이전 미리보기 복원
        self.adm_previews = st.get('previews', []) or []
        try: self.main.log(f"♻️ 이전 애드몽 기록 복원: 행 {self.adm_table.rowCount()}개 / 미리보기 {len(self.adm_previews)}건")
        except Exception: pass

    def _adm_add_row(self, kw='', q1='', q2=''):
        r = self.adm_table.rowCount()
        self.adm_table.insertRow(r)
        self.adm_table.setItem(r, 0, QTableWidgetItem(kw))
        self.adm_table.setItem(r, 1, QTableWidgetItem(q1))
        self.adm_table.setItem(r, 2, QTableWidgetItem(q2))

    def _adm_del_row(self):
        rows = sorted({i.row() for i in self.adm_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.adm_table.removeRow(r)

    def _adm_pick_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 선택", "", "Excel (*.xlsx *.xls)")
        if path:
            self.adm_xl_path.setText(path)

    def _adm_load_excel(self):
        path = self.adm_xl_path.text().strip()
        if not path or not os.path.exists(path):
            self.main.log("⚠️ 엑셀 파일 경로 확인"); return
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb.active
            blog_id = ws.cell(row=2, column=1).value
            if blog_id and not self.adm_blog_id.text().strip():
                self.adm_blog_id.setText(str(blog_id))
            self.adm_table.setRowCount(0)
            r = 2
            while True:
                kw = ws.cell(row=r, column=2).value
                if not kw: break
                q1 = ws.cell(row=r, column=3).value or ''
                q2 = ws.cell(row=r, column=4).value or ''
                self._adm_add_row(str(kw), str(q1), str(q2))
                r += 1
            self.main.log(f"📥 엑셀 로드 완료: {self.adm_table.rowCount()}건")
        except Exception as e:
            self.main.log(f"❌ 엑셀 로드 실패: {e}")

    def _adm_load_keywords(self):
        text = self.adm_kw_input.text().strip()
        if not text:
            self.main.log("⚠️ 키워드를 콤마(,)로 구분해 입력하세요"); return
        kws = [k.strip() for k in text.split(',') if k.strip()]
        if not kws:
            self.main.log("⚠️ 유효한 키워드가 없습니다"); return
        if len(kws) > 50:
            self.main.log(f"⚠️ 최대 50개까지만 등록 (입력 {len(kws)}개 → 앞 50개만 사용)")
            kws = kws[:50]
        common_q = self.adm_common_q.toPlainText().strip()
        self.adm_table.setRowCount(0)
        for kw in kws:
            self._adm_add_row(kw, common_q, '')
        self.main.log(f"📥 키워드 {len(kws)}개 등록 완료 (순차 진행)")

    def _adm_collect_rows(self):

        rows = []
        for r in range(self.adm_table.rowCount()):
            kw = (self.adm_table.item(r, 0).text() if self.adm_table.item(r, 0) else '').strip()
            q1 = (self.adm_table.item(r, 1).text() if self.adm_table.item(r, 1) else '').strip()
            q2 = (self.adm_table.item(r, 2).text() if self.adm_table.item(r, 2) else '').strip()
            if not kw: continue
            queries = [q for q in [q1, q2] if q]
            if not queries:
                queries = [f"{kw}에 대해 위 내용을 참고해 블로그 글을 한국어로 작성해줘."]
            rows.append({'keyword': kw, 'queries': queries})
        return rows

    ADM_SETTINGS_PATH = 'adm_settings.json'

    def _adm_open_prompt_library(self):
        current = self.adm_common_q.toPlainText()
        dlg = PromptLibraryDialog(current_text=current, parent=self)
        # 버튼에 총 개수 표시 업데이트
        self.adm_lib_btn.setText(f"🗂 프롬프트 관리 / 변경    총 {len(dlg._library)}개")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text = dlg.get_selected_text()
            title = dlg.get_selected_title()
            if text is not None:
                self.adm_common_q.setPlainText(text)
                self._adm_card_title.setText(title if title else "(제목 없음)")
                preview = text[:80].replace('\n', ' ')
                self._adm_card_preview.setText(preview + ('...' if len(text) > 80 else ''))
                if not self._adm_card_badge_shown:
                    self._adm_card_title_row.addWidget(self._adm_card_badge)
                    self._adm_card_badge_shown = True
                self._adm_save_state()
        self.adm_lib_btn.setText(f"🗂 프롬프트 관리 / 변경    총 {len(dlg._library)}개")

    def _adm_load_settings(self):
        defaults = {
            'alignment': 0, 'font_size': 15,
            'use_quote': False, 'quote_text': '',
            'img_enabled': False, 'img_type': 0,
            'ai_provider': 0, 'ai_img_prompt': '', 'ai_img_count': 1,
            'img_paths': '', 'var_img_path': '', 'var_img_prompt': '', 'var_img_count': 1,
        }
        try:
            if os.path.exists(self.ADM_SETTINGS_PATH):
                with open(self.ADM_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
        return defaults

    def _adm_save_settings(self, cfg):
        try:
            with open(self.ADM_SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _adm_open_settings(self):
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                                     QComboBox, QSpinBox, QCheckBox, QLineEdit,
                                     QPushButton, QDialogButtonBox, QGroupBox, QLabel,
                                     QFileDialog)
        cfg = self._adm_load_settings()
        dlg = QDialog(self)
        dlg.setWindowTitle("포스팅·이미지 옵션")
        dlg.setMinimumWidth(480)
        vbox = QVBoxLayout(dlg)

        # --- 네이버 포스팅 설정 ---
        grp1 = QGroupBox("네이버 포스팅 설정")
        form1 = QFormLayout(grp1)

        align_combo = QComboBox()
        align_combo.addItems(["왼쪽", "가운데", "오른쪽"])
        align_combo.setCurrentIndex(cfg.get('alignment', 0))
        form1.addRow("글자 정렬:", align_combo)

        font_spin = QSpinBox()
        font_spin.setRange(8, 40)
        font_spin.setValue(cfg.get('font_size', 15))
        form1.addRow("글자 크기:", font_spin)

        quote_chk = QCheckBox("인용구 사용")
        quote_chk.setChecked(cfg.get('use_quote', False))
        form1.addRow("", quote_chk)

        quote_edit = QLineEdit(cfg.get('quote_text', '') or '')
        quote_edit.setPlaceholderText("인용구 텍스트 (최대 15자)")
        quote_edit.setMaxLength(15)
        quote_edit.setEnabled(cfg.get('use_quote', False))
        quote_chk.toggled.connect(quote_edit.setEnabled)
        form1.addRow("인용구 텍스트:", quote_edit)

        vbox.addWidget(grp1)

        # --- 이미지 설정 ---
        grp2 = QGroupBox("이미지 설정")
        form2 = QFormLayout(grp2)

        img_chk = QCheckBox("이미지 사용")
        img_chk.setChecked(cfg.get('img_enabled', False))
        form2.addRow("", img_chk)

        img_type_combo = QComboBox()
        img_type_combo.addItems(["AI 이미지 생성", "직접 파일 업로드", "기본 이미지 + AI 변형"])
        img_type_combo.setCurrentIndex(cfg.get('img_type', 0))
        form2.addRow("이미지 유형:", img_type_combo)

        ai_prov_combo = QComboBox()
        ai_prov_combo.addItems(["ChatGPT (DALL·E)", "Gemini"])
        ai_prov_combo.setCurrentIndex(cfg.get('ai_provider', 0))
        form2.addRow("AI 제공자:", ai_prov_combo)

        ai_prompt_edit = QLineEdit(cfg.get('ai_img_prompt', '') or '')
        ai_prompt_edit.setPlaceholderText("AI 이미지 생성 프롬프트")
        form2.addRow("AI 프롬프트:", ai_prompt_edit)

        ai_count_spin = QSpinBox()
        ai_count_spin.setRange(1, 10)
        ai_count_spin.setValue(cfg.get('ai_img_count', 1))
        form2.addRow("AI 생성 수:", ai_count_spin)

        # 직접 업로드 경로
        paths_row = QHBoxLayout()
        img_paths_edit = QLineEdit(cfg.get('img_paths', '') or '')
        img_paths_edit.setPlaceholderText("파일 경로 (쉼표 구분)")
        paths_pick_btn = QPushButton("찾기")
        def pick_imgs():
            files, _ = QFileDialog.getOpenFileNames(dlg, "이미지 선택", "", "Images (*.png *.jpg *.jpeg *.webp)")
            if files:
                img_paths_edit.setText(','.join(files))
        paths_pick_btn.clicked.connect(pick_imgs)
        paths_row.addWidget(img_paths_edit)
        paths_row.addWidget(paths_pick_btn)
        form2.addRow("업로드 파일:", paths_row)

        # 변형 베이스 이미지
        var_row = QHBoxLayout()
        var_path_edit = QLineEdit(cfg.get('var_img_path', '') or '')
        var_path_edit.setPlaceholderText("베이스 이미지 경로")
        var_pick_btn = QPushButton("찾기")
        def pick_var():
            f, _ = QFileDialog.getOpenFileName(dlg, "베이스 이미지 선택", "", "Images (*.png *.jpg *.jpeg)")
            if f:
                var_path_edit.setText(f)
        var_pick_btn.clicked.connect(pick_var)
        var_row.addWidget(var_path_edit)
        var_row.addWidget(var_pick_btn)
        form2.addRow("변형 베이스:", var_row)

        var_prompt_edit = QLineEdit(cfg.get('var_img_prompt', '') or '')
        var_prompt_edit.setPlaceholderText("변형 프롬프트")
        form2.addRow("변형 프롬프트:", var_prompt_edit)

        var_count_spin = QSpinBox()
        var_count_spin.setRange(1, 10)
        var_count_spin.setValue(cfg.get('var_img_count', 1))
        form2.addRow("변형 수:", var_count_spin)

        vbox.addWidget(grp2)

        # --- 확인/취소 ---
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        vbox.addWidget(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cfg = {
                'alignment': align_combo.currentIndex(),
                'font_size': font_spin.value(),
                'use_quote': quote_chk.isChecked(),
                'quote_text': quote_edit.text()[:15],
                'img_enabled': img_chk.isChecked(),
                'img_type': img_type_combo.currentIndex(),
                'ai_provider': ai_prov_combo.currentIndex(),
                'ai_img_prompt': ai_prompt_edit.text(),
                'ai_img_count': ai_count_spin.value(),
                'img_paths': img_paths_edit.text(),
                'var_img_path': var_path_edit.text(),
                'var_img_prompt': var_prompt_edit.text(),
                'var_img_count': var_count_spin.value(),
            }
            self._adm_save_settings(new_cfg)
            self.main.log("✅ 포스팅·이미지 옵션 저장됨")

    def _adm_run(self):
        api_key = self.adm_api_key.text().strip()
        if not api_key:
            self.main.log("⚠️ Perplexity API 키 입력"); return
        blog_id = self.adm_blog_id.text().strip()
        if not blog_id:
            self.main.log("⚠️ 블로그 ID 입력"); return
        rows = self._adm_collect_rows()
        if not rows:
            self.main.log("⚠️ 표에 키워드를 한 개 이상 입력"); return
        extract_cnt = self.adm_extract_cnt.value()
        src_type = self.adm_src_type.currentIndex()  # 0=블로그, 1=뉴스
        save_mode = self.adm_save_mode.currentIndex()  # 0=임시저장, 1=예약, 2=발행
        schedule = self.adm_schedule_time.text().strip() if save_mode == 1 else ""
        if save_mode == 1 and not schedule:
            self.main.log("⚠️ 예약발행 선택 시 예약시간을 입력해주세요 (예: 2025-01-15 09:00)"); return
        cfg = self._adm_load_settings()
        align_map = {0: 'LEFT', 1: 'CENTER', 2: 'RIGHT'}
        alignment = align_map.get(cfg.get('alignment', 0), 'LEFT')
        font_size = cfg.get('font_size', 15)
        use_quote = cfg.get('use_quote', False)
        quote_text = (cfg.get('quote_text', '') or '')[:15]
        img_enabled = cfg.get('img_enabled', False)
        img_type = cfg.get('img_type', 0) if img_enabled else -1
        ai_provider = cfg.get('ai_provider', 0)
        ai_img_prompt = cfg.get('ai_img_prompt', '')
        ai_img_count = cfg.get('ai_img_count', 1)
        img_paths = [p.strip() for p in (cfg.get('img_paths', '') or '').split(',') if p.strip()] if img_enabled else []
        var_img_path = cfg.get('var_img_path', '')
        var_img_prompt = cfg.get('var_img_prompt', '')
        var_img_count = cfg.get('var_img_count', 1)
        img_cfg = {
            'enabled': img_enabled,
            'type': img_type,
            'ai_provider': ai_provider,
            'ai_prompt': ai_img_prompt,
            'ai_count': ai_img_count,
            'paths': img_paths,
            'var_path': var_img_path,
            'var_prompt': var_img_prompt,
            'var_count': var_img_count,
        }

        self.adm_run_btn.setEnabled(False); self.adm_run_btn.setText("실행 중...")
        t = WorkerThread(self._adm_do_run, api_key, blog_id, rows, extract_cnt, src_type,
                         save_mode, schedule, alignment, font_size, use_quote, quote_text, img_cfg)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self.main.log)
        t.finished_signal.connect(lambda: (
            self.adm_run_btn.setEnabled(True),
            self.adm_run_btn.setText("🚀 애드몽 자동 실행")))
        t.start()

    def _adm_do_run(self, api_key, blog_id, rows, extract_cnt, src_type,
                    save_mode, schedule="", alignment='LEFT', font_size=15,
                    use_quote=False, quote_text='', img_cfg=None):
        if img_cfg is None: img_cfg = {'enabled': False}
        import undetected_chromedriver as uc
        # 새 실행 시 미리보기 초기화 (이전 기록은 디스크에 별도 저장됨)
        self.adm_previews = []
        self._adm_save_state()
        opts = uc.ChromeOptions()
        opts.add_argument('--start-maximized'); opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage'); opts.add_argument('--disable-gpu')
        opts.add_argument('--user-data-dir=./chrome_profile')
        driver = make_uc_driver(opts)
        try:
            # 네이버 로그인 (저장된 계정 있으면 사용)
            try:
                acc = None
                for a in self.main.config.get('naver_accounts', []) or []:
                    if a.get('id') == blog_id:
                        acc = a; break
                nid = (acc or {}).get('id') or blog_id or self.main.config.get('naver_id', '')
                npw = (acc or {}).get('pw') or self.main.config.get('naver_pw', '')
                if nid and npw:
                    naver_login_with_fallback(driver, nid, npw, log_fn=self.main.log)
            except Exception as e:
                self.main.log(f"⚠️ 로그인 단계 경고: {e}")

            for idx, row in enumerate(rows, 1):
                kw = row['keyword']; queries = row['queries']
                self.main.log(f"━━ [{idx}/{len(rows)}] 키워드: {kw} ━━")
                preview_entry = {'keyword': kw, 'collected': [], 'generated': '', 'images': []}
                self.adm_previews.append(preview_entry)
                # 1) 검색 → 링크 수집
                try:
                    if src_type == 0:
                        titles, links = self._adm_search_blog(driver, kw, extract_cnt)
                    else:
                        titles, links = self._adm_search_news(driver, kw, extract_cnt)
                    self.main.log(f"  🔎 수집 {len(links)}건")
                except Exception as e:
                    self.main.log(f"  ❌ 검색 실패: {e}"); continue
                # 2) 본문 추출
                con_parts = []
                for ti, link in enumerate(links):
                    try:
                        c = self._adm_extract_blog(driver, link) if src_type == 0 else self._adm_extract_news(driver, link)
                        if c:
                            con_parts.append(c)
                            title_txt = titles[ti] if ti < len(titles) else ''
                            preview_entry['collected'].append({
                                'title': title_txt, 'link': link, 'text': c
                            })
                            self._adm_save_state()
                    except Exception as e:
                        self.main.log(f"  ⚠️ 추출 실패({link}): {e}")
                if not con_parts:
                    self.main.log("  ⛔ 본문 없음, 스킵"); continue
                context = "\n\n\n\n\n".join(con_parts).strip()
                self.main.log(f"  📄 본문 추출 {sum(len(c) for c in con_parts)}자 (미리보기 가능)")
                # 3) Perplexity 질의 (누적 컨텍스트)
                try:
                    contents = self._adm_ask_perplexity(api_key, queries, context)
                except Exception as e:
                    self.main.log(f"  ❌ Perplexity 오류: {e}"); continue
                if not contents:
                    self.main.log("  ⛔ 생성 결과 없음, 스킵"); continue
                preview_entry['generated'] = contents
                self._adm_save_state()
                self.main.log(f"  ✍️ 생성된 글 {len(contents)}자 (미리보기 가능)")
                # 4) 네이버 블로그 업로드
                try:
                    generated_images = []
                    if img_cfg.get('enabled'):
                        generated_images = self._adm_prepare_images(img_cfg, kw, contents)
                    preview_entry['images'] = generated_images
                    self._adm_save_state()
                    self._adm_post_to_blog(driver, blog_id, kw, contents, save_mode, schedule,
                                           alignment, font_size, use_quote, quote_text, generated_images)
                    self.main.log(f"  ✅ 업로드 완료: {kw}")
                except Exception as e:
                    self.main.log(f"  ❌ 업로드 실패: {e}")
                time.sleep(3)
            return "🎉 애드몽 자동 실행 완료"
        finally:
            pass  # driver는 유지

    def _adm_show_previews(self):
        # 기록이 없으면 저장된 파일에서 복원 시도
        if not self.adm_previews:
            self._adm_load_state()
        if not self.adm_previews:
            QMessageBox.information(self, "미리보기", "아직 수집/생성된 글이 없습니다.\n먼저 '🚀 애드몽 자동 실행'을 진행하세요.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("📄 애드몽 — 원고 / 이미지 미리보기")
        dlg.resize(1200, 800)
        v = QVBoxLayout(dlg)

        # 상단: 키워드 선택
        top = QHBoxLayout()
        top.addWidget(QLabel("키워드:"))
        kw_combo = QComboBox()
        for i, p in enumerate(self.adm_previews):
            img_cnt = len(p.get('images', []))
            kw_combo.addItem(
                f"[{i+1}] {p['keyword']}  (수집 {len(p.get('collected',[]))}건 / "
                f"생성 {'O' if p.get('generated') else 'X'} / 이미지 {img_cnt}장)"
            )
        top.addWidget(kw_combo, 1)
        v.addLayout(top)

        # 탭: 수집된 글 / 생성된 글 / 첨부 이미지
        tabs = QTabWidget()

        # ── 수집 탭 ──
        col_w = QWidget(); col_l = QVBoxLayout(col_w)
        col_list_combo = QComboBox()
        col_text = QTextEdit(); col_text.setReadOnly(True)
        col_l.addWidget(QLabel("수집된 원문 (소스 선택):"))
        col_l.addWidget(col_list_combo)
        col_l.addWidget(col_text, 1)
        tabs.addTab(col_w, "🗂️ 수집된 글")

        # ── 생성된 글 탭 ──
        gen_w = QWidget(); gen_l = QVBoxLayout(gen_w)
        gen_text = QTextEdit(); gen_text.setReadOnly(True)
        gen_l.addWidget(QLabel("Perplexity 생성 결과:"))
        gen_l.addWidget(gen_text, 1)
        b_expand_text = QPushButton("🔍 확대 보기")
        gen_l.addWidget(b_expand_text)
        tabs.addTab(gen_w, "✨ 생성된 글")

        # ── 이미지 탭 ──
        img_w = QWidget(); img_l = QVBoxLayout(img_w)
        img_list_combo = QComboBox()
        from PyQt6.QtGui import QPixmap
        img_scroll = QScrollArea(); img_scroll.setWidgetResizable(True)
        img_inner = QLabel(); img_inner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_inner.setMinimumHeight(350)
        img_inner.setStyleSheet("background:#111; border:1px solid #444;")
        img_scroll.setWidget(img_inner)
        img_path_label = QLabel("(이미지 없음)"); img_path_label.setStyleSheet("color:#888; font-size:11px;")
        img_l.addWidget(QLabel("이미지 선택:"))
        img_l.addWidget(img_list_combo)
        img_l.addWidget(img_scroll, 1)
        img_l.addWidget(img_path_label)
        btn_img_row = QHBoxLayout()
        b_add_img = QPushButton("📎 파일 첨부 미리보기")
        b_expand_img = QPushButton("🔍 이미지 확대")
        btn_img_row.addWidget(b_add_img); btn_img_row.addWidget(b_expand_img); btn_img_row.addStretch()
        img_l.addLayout(btn_img_row)
        tabs.addTab(img_w, "🖼️ 첨부 이미지")

        v.addWidget(tabs, 1)

        # 하단: 복사 / 닫기
        btm = QHBoxLayout(); btm.addStretch()
        b_copy_col = QPushButton("수집글 복사")
        b_copy_gen = QPushButton("생성글 복사")
        b_close = QPushButton("닫기")
        btm.addWidget(b_copy_col); btm.addWidget(b_copy_gen); btm.addWidget(b_close)
        v.addLayout(btm)

        _preview_images = []  # (label, path)

        def _load_img_from_path(path):
            try:
                pix = QPixmap(path)
                if not pix.isNull():
                    scaled = pix.scaled(img_scroll.width()-20, 500,
                                        Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
                    img_inner.setPixmap(scaled)
                    img_path_label.setText(path)
                else:
                    img_inner.setText("(이미지 로드 실패)")
                    img_path_label.setText(path)
            except Exception as e:
                img_inner.setText(f"(오류: {e})")

        def _show_preview_img():
            j = img_list_combo.currentIndex()
            if j < 0 or j >= len(_preview_images): return
            _load_img_from_path(_preview_images[j][1])

        def _expand_img():
            j = img_list_combo.currentIndex()
            if j < 0 or j >= len(_preview_images): return
            path = _preview_images[j][1]
            exp = QDialog(dlg); exp.setWindowTitle(f"🔍 {os.path.basename(path)}")
            exp.resize(900, 700)
            ev = QVBoxLayout(exp)
            lbl = QLabel(); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            try:
                pix = QPixmap(path)
                scaled = pix.scaled(860, 640, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
                lbl.setPixmap(scaled)
            except Exception:
                lbl.setText("이미지 로드 실패")
            sc = QScrollArea(); sc.setWidget(lbl); sc.setWidgetResizable(True)
            ev.addWidget(sc, 1)
            bc = QPushButton("닫기"); bc.clicked.connect(exp.accept); ev.addWidget(bc)
            exp.exec()

        def _expand_text():
            text = gen_text.toPlainText()
            if not text: return
            exp = QDialog(dlg); exp.setWindowTitle("🔍 생성된 글 확대")
            exp.resize(900, 700)
            ev = QVBoxLayout(exp)
            te = QTextEdit(); te.setReadOnly(True); te.setPlainText(text)
            ev.addWidget(te, 1)
            brow = QHBoxLayout()
            bcp = QPushButton("복사"); bcp.clicked.connect(lambda: pyperclip.copy(te.toPlainText()))
            bcl = QPushButton("닫기"); bcl.clicked.connect(exp.accept)
            brow.addWidget(bcp); brow.addWidget(bcl)
            ev.addLayout(brow)
            exp.exec()

        def _pick_preview_image():
            paths, _ = QFileDialog.getOpenFileNames(dlg, "이미지 선택", "", "이미지 (*.png *.jpg *.jpeg *.webp *.gif)")
            for p in paths:
                lbl = os.path.basename(p)
                _preview_images.append((lbl, p))
                img_list_combo.addItem(lbl)
            if _preview_images:
                img_list_combo.setCurrentIndex(len(_preview_images)-1)
                _show_preview_img()

        def _load_generated_imgs():
            i = kw_combo.currentIndex()
            if i < 0 or i >= len(self.adm_previews): return
            p = self.adm_previews[i]
            img_list_combo.clear(); _preview_images.clear()
            # 1) preview_entry의 images 경로 우선
            for fp in (p.get('images') or []):
                if os.path.isfile(fp):
                    _preview_images.append((os.path.basename(fp), fp))
                    img_list_combo.addItem(os.path.basename(fp))
            # 2) adm_images 폴더 fallback
            if not _preview_images and os.path.isdir('adm_images'):
                for fn in sorted(os.listdir('adm_images')):
                    fp = os.path.join('adm_images', fn)
                    if os.path.isfile(fp) and fn.lower().endswith(('.png','.jpg','.jpeg','.webp')):
                        _preview_images.append((fn, fp))
                        img_list_combo.addItem(fn)
            if _preview_images:
                img_list_combo.setCurrentIndex(0); _show_preview_img()
            else:
                img_inner.clear(); img_inner.setText("(생성된 이미지 없음\n파일 첨부로 확인하세요)")
                img_path_label.setText("")

        def load_collected():
            i = kw_combo.currentIndex()
            if i < 0 or i >= len(self.adm_previews): return
            p = self.adm_previews[i]
            col_list_combo.clear()
            for ci, c in enumerate(p.get('collected', [])):
                t = c.get('title') or c.get('link') or f"항목 {ci+1}"
                col_list_combo.addItem(f"{ci+1}. {t[:80]}")
            if p.get('collected'):
                col_list_combo.setCurrentIndex(0)
                show_collected()
            else:
                col_text.setPlainText("(수집된 글 없음)")
            gen_text.setPlainText(p.get('generated') or "(아직 생성 안 됨)")
            _load_generated_imgs()

        def show_collected():
            i = kw_combo.currentIndex(); j = col_list_combo.currentIndex()
            if i < 0 or j < 0: return
            try:
                c = self.adm_previews[i]['collected'][j]
                col_text.setPlainText(f"[제목] {c.get('title','')}\n[링크] {c.get('link','')}\n\n{c.get('text','')}")
            except Exception: pass

        img_list_combo.currentIndexChanged.connect(lambda _: _show_preview_img())
        kw_combo.currentIndexChanged.connect(lambda _: load_collected())
        col_list_combo.currentIndexChanged.connect(lambda _: show_collected())
        b_copy_col.clicked.connect(lambda: pyperclip.copy(col_text.toPlainText()))
        b_copy_gen.clicked.connect(lambda: pyperclip.copy(gen_text.toPlainText()))
        b_close.clicked.connect(dlg.accept)
        b_expand_img.clicked.connect(lambda: _expand_img())
        b_expand_text.clicked.connect(lambda: _expand_text())
        b_add_img.clicked.connect(lambda: _pick_preview_image())

        load_collected()
        dlg.exec()

    # ── Admong helper: 검색 ──
    def _adm_search_blog(self, driver, keyword, extract_cnt):
        from urllib.parse import quote
        titles, links = [], []
        url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&sm=tab_jum&query={quote(keyword)}"
        driver.get(url); time.sleep(2)
        try:
            from selenium.webdriver.common.alert import Alert
            Alert(driver).accept()
        except Exception: pass
        css = "div > div > div > div > div > div.sds-comps-vertical-layout.sds-comps-full-layout > div > div.sds-comps-vertical-layout.sds-comps-full-layout > a"
        blogs = driver.find_elements(By.CSS_SELECTOR, css)
        max_n = min(len(blogs), extract_cnt * 6)
        for i in range(0, max_n, 6):
            try:
                titles.append(blogs[i].text)
                links.append(blogs[i].get_attribute('href'))
            except Exception: continue
            if len(links) >= extract_cnt: break
        return titles, links

    def _adm_search_news(self, driver, keyword, extract_cnt):
        from urllib.parse import quote
        titles, links = [], []
        url = f"https://search.naver.com/search.naver?where=news&ie=utf8&sm=nws_hty&query={quote(keyword)}"
        driver.get(url); time.sleep(2)
        try:
            from selenium.webdriver.common.alert import Alert
            Alert(driver).accept()
        except Exception: pass
        css = "div > div > div > div > div.sds-comps-base-layout.sds-comps-full-layout.PxmIbe24tDyD5PxsrpSQ > div > a"
        news = driver.find_elements(By.CSS_SELECTOR, css)
        max_n = min(len(news), extract_cnt * 2)
        for i in range(0, max_n, 2):
            try:
                titles.append(news[i].text)
                links.append(news[i].get_attribute('href'))
            except Exception: continue
            if len(links) >= extract_cnt: break
        return titles, links

    def _adm_extract_blog(self, driver, link):
        driver.get(link); time.sleep(1)
        cur = driver.current_url.replace("blog.naver.com", "m.blog.naver.com")
        driver.get(cur); time.sleep(1)
        try:
            contents = driver.find_element(By.CSS_SELECTOR, '#viewTypeSelector').text
        except Exception:
            return None
        start = contents.find("본문 기타 기능")
        cut_start = (start + 8) if start >= 0 else 0
        i2 = contents.find("이 블로그의 체크인")
        i3 = contents.find("이 장소의 다른 글")
        cands = [x for x in [i2, i3] if x > 0]
        cut_end = min(cands) if cands else len(contents)
        return contents[cut_start:cut_end].strip()

    def _adm_extract_news(self, driver, link):
        driver.get(link); time.sleep(1)
        for eid in ['article-view-content-div', 'contents', 'container', 'content', 'dic_area', 'newsct_article']:
            try:
                return driver.find_element(By.ID, eid).text
            except Exception:
                continue
        return None

    def _adm_ask_perplexity(self, api_key, queries, context):
        # OpenAI 호환 엔드포인트 사용 (perplexity SDK 미설치 환경 대응)
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")
        except Exception as e:
            raise RuntimeError(f"openai 클라이언트 초기화 실패: {e}")
        ctx = context
        last = None
        for i, q in enumerate(queries, 1):
            self.main.log(f"    💬 Perplexity {i}/{len(queries)}")
            prompt = f"{ctx}\n\n질문: {q}"
            resp = client.chat.completions.create(
                model="sonar-pro",
                messages=[
                    {"role": "system", "content": "You are a helpful Korean blog writer."},
                    {"role": "user", "content": prompt},
                ],
            )
            last = resp.choices[0].message.content
            ctx = last  # 누적
        return last

    def _adm_prepare_images(self, img_cfg, keyword, body_text):
        """이미지 타입별 처리: 경로 리스트 반환"""
        img_type = img_cfg.get('type', -1)
        generated = []
        try:
            if img_type == 0:  # 1) AI 이미지 생성
                provider = img_cfg.get('ai_provider', 0)
                prompt = img_cfg.get('ai_prompt') or f"Professional blog thumbnail for: {keyword}"
                count = max(1, int(img_cfg.get('ai_count', 1)))
                for _ in range(count):
                    path = self._adm_generate_ai_image(provider, prompt, keyword)
                    if path: generated.append(path)
            elif img_type == 1:  # 2) 업로드 이미지
                generated = [p for p in img_cfg.get('paths', []) if os.path.exists(p)]
            elif img_type == 2:  # 3) 업로드+AI 변형
                base_path = img_cfg.get('var_path', '')
                var_prompt = img_cfg.get('var_prompt') or f"Modify image for blog post about: {keyword}"
                count = max(1, int(img_cfg.get('var_count', 1)))
                if base_path and os.path.exists(base_path):
                    for _ in range(count):
                        path = self._adm_vary_image(base_path, var_prompt, keyword)
                        if path: generated.append(path)
        except Exception as e:
            self.main.log(f"  ⚠️ 이미지 준비 실패: {e}")
        return generated

    def _adm_generate_ai_image(self, provider, prompt, keyword):
        """AI로 이미지 생성 (ChatGPT DALL·E or Gemini), 로컬 저장 후 경로 반환"""
        import tempfile, base64, io as _io
        os.makedirs('adm_images', exist_ok=True)
        safe_kw = re.sub(r'[^\w가-힣]', '_', keyword)[:30]
        out_path = os.path.join('adm_images', f"{safe_kw}_{int(time.time())}.png")
        try:
            if provider == 0:  # ChatGPT DALL·E
                openai_key = self.main.config.get('openai_api_key') or os.getenv('OPENAI_API_KEY', '')
                if not openai_key:
                    self.main.log("  ⚠️ OpenAI API 키 미설정 (설정 페이지에서 입력)"); return None
                client = OpenAI(api_key=openai_key)
                resp = client.images.generate(model="dall-e-3", prompt=prompt, n=1, size="1024x1024", response_format="b64_json")
                img_data = base64.b64decode(resp.data[0].b64_json)
                with open(out_path, 'wb') as f: f.write(img_data)
                self.main.log(f"  🖼️ DALL·E 이미지 생성: {out_path}")
                return out_path
            else:  # Gemini
                gemini_key = self.main.config.get('gemini_api_key') or os.getenv('GEMINI_API_KEY', '')
                if not gemini_key:
                    self.main.log("  ⚠️ Gemini API 키 미설정 (설정 페이지에서 입력)"); return None
                import google.genai as _genai
                _gclient = _genai.Client(api_key=gemini_key)
                response = _gclient.models.generate_content(
                    model="gemini-2.0-flash-preview-image-generation",
                    contents=prompt,
                    config=_genai.types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"])
                )
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        img_data = base64.b64decode(part.inline_data.data)
                        with open(out_path, 'wb') as f: f.write(img_data)
                        self.main.log(f"  🖼️ Gemini 이미지 생성: {out_path}")
                        return out_path
                self.main.log("  ⚠️ Gemini 이미지 응답 없음"); return None
        except Exception as e:
            self.main.log(f"  ❌ AI 이미지 생성 실패: {e}"); return None

    def _adm_vary_image(self, base_path, prompt, keyword):
        """업로드 이미지를 기반으로 AI 변형 (DALL·E variations or Gemini)"""
        import base64, io as _io
        os.makedirs('adm_images', exist_ok=True)
        safe_kw = re.sub(r'[^\w가-힣]', '_', keyword)[:30]
        out_path = os.path.join('adm_images', f"var_{safe_kw}_{int(time.time())}.png")
        try:
            openai_key = self.main.config.get('openai_api_key') or os.getenv('OPENAI_API_KEY', '')
            if not openai_key:
                self.main.log("  ⚠️ OpenAI API 키 미설정"); return None
            client = OpenAI(api_key=openai_key)
            # DALL·E edit (inpainting) — mask 없이 전체 변형
            with open(base_path, 'rb') as img_f:
                img_bytes = img_f.read()
            # PNG로 변환
            pil_img = PILImage.open(_io.BytesIO(img_bytes)).convert('RGBA')
            buf = _io.BytesIO(); pil_img.save(buf, format='PNG'); buf.seek(0)
            resp = client.images.edit(
                model="dall-e-2",
                image=buf,
                prompt=prompt,
                n=1,
                size="1024x1024",
                response_format="b64_json"
            )
            img_data = base64.b64decode(resp.data[0].b64_json)
            with open(out_path, 'wb') as f: f.write(img_data)
            self.main.log(f"  🖼️ 이미지 변형 완료: {out_path}")
            return out_path
        except Exception as e:
            self.main.log(f"  ❌ 이미지 변형 실패: {e}"); return None

    def _adm_upload_image_to_blog(self, driver, image_path):
        """네이버 스마트에디터에 이미지 파일 업로드"""
        abs_path = os.path.abspath(image_path)
        if not os.path.exists(abs_path):
            self.main.log(f"  ⚠️ 이미지 없음: {abs_path}"); return
        try:
            for sel in [
                'input.se-image-input-file',
                'input[class*="image"][type="file"]',
                'input[accept*="image"][type="file"]',
            ]:
                fis = driver.find_elements(By.CSS_SELECTOR, sel)
                for fi in fis:
                    try:
                        driver.execute_script("arguments[0].style.display='block';", fi)
                        fi.send_keys(abs_path)
                        time.sleep(3)
                        self.main.log(f"  🖼️ 이미지 업로드 완료: {os.path.basename(image_path)}")
                        return
                    except Exception:
                        continue
            # fallback: 이미지 버튼 클릭
            for btn_sel in ['button[data-name="image"]', 'button.se-image-toolbar-button']:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, btn_sel)
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
                    fis = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                    for fi in fis:
                        try:
                            fi.send_keys(abs_path); time.sleep(3)
                            self.main.log(f"  🖼️ 이미지 업로드(fallback): {os.path.basename(image_path)}")
                            return
                        except Exception: continue
                except Exception: continue
        except Exception as e:
            self.main.log(f"  ⚠️ 이미지 업로드 실패: {e}")

    def _adm_post_to_blog(self, driver, blog_id, title, body, save_mode, schedule="",
                          alignment='LEFT', font_size=15, use_quote=False, quote_text='',
                          images=None):
        if images is None: images = []
        # 제목 15자 제한: 초과분은 본문 맨 앞에 합쳐서 넣음
        full_title = (title or '').strip()
        if len(full_title) > 15:
            title_part = full_title[:15]
            overflow = full_title[15:].strip()
            body = (overflow + "\n\n" + (body or '')) if overflow else (body or '')
        else:
            title_part = full_title
        self.main.log(f"  📝 제목({len(title_part)}자): {title_part}")

        url = f"https://blog.naver.com/{blog_id}?Redirect=Write&"
        driver.get(url); time.sleep(4)
        try:
            driver.switch_to.frame('mainFrame'); time.sleep(1)
        except Exception: pass
        # 이전 작성중 팝업 취소
        try:
            driver.find_element(By.CLASS_NAME, 'se-popup-button-text').click()
            time.sleep(1)
        except Exception: pass
        # 제목 입력 (탭 또는 클릭으로 본문 영역으로 이동)
        try:
            safe_press(driver, 'tab'); time.sleep(0.3)
            pyperclip.copy(title_part); safe_hotkey(driver, 'ctrl', 'v'); time.sleep(0.5)
            safe_press(driver, 'tab'); time.sleep(0.5)
        except Exception: pass

        # 인용구 삽입 (use_quote=True, 15자 이내)
        if use_quote and quote_text:
            qt = quote_text[:15].strip()
            if qt:
                try:
                    insert_quote_block(driver, qt)
                    self.main.log(f"  ❝ 인용구 삽입: {qt}")
                except Exception as e:
                    self.main.log(f"  ⚠️ 인용구 삽입 실패: {e}")

        # 이미지 삽입 (본문 앞에)
        for img_path in images:
            try:
                self._adm_upload_image_to_blog(driver, img_path)
                safe_press(driver, 'enter'); time.sleep(0.5)
            except Exception as e:
                self.main.log(f"  ⚠️ 이미지 삽입 실패: {e}")

        # 폰트 사이즈 설정 (전체 선택 후)
        try:
            safe_hotkey(driver, 'ctrl', 'a'); time.sleep(0.2)
            # 네이버 스마트에디터 폰트 사이즈 툴바
            for sel in ['button[data-name="fontSize"]', '.se-toolbar-item-font-size button',
                        'button[aria-label*="글자 크기"]']:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                    driver.execute_script("arguments[0].click();", btn); time.sleep(0.3)
                    size_input = driver.find_element(By.CSS_SELECTOR,
                        'input[class*="font-size"], input[placeholder*="크기"], .se-font-size-input')
                    size_input.click(); safe_hotkey(driver, 'ctrl', 'a')
                    size_input.send_keys(str(font_size)); time.sleep(0.2)
                    from selenium.webdriver.common.keys import Keys as _Keys
                    size_input.send_keys(_Keys.RETURN); time.sleep(0.3)
                    break
                except Exception:
                    continue
            # 선택 해제
            try:
                body_area = driver.find_element(By.CSS_SELECTOR, '.se-main-section, .se-component')
                body_area.click()
            except Exception: pass
        except Exception:
            pass

        # 본문 입력 (라인별, 제목 초과분 포함)
        import re as _re
        for line in (body or '').split('\n'):
            if not line.strip():
                safe_press(driver, 'enter'); time.sleep(0.1); continue
            line_align = None
            m = _re.match(r"\{\{ALIGN:(LEFT|CENTER|RIGHT|JUSTIFY)\}\}(.*)", line)
            if m:
                line_align = m.group(1); line = m.group(2)
            if line.lstrip().startswith('> '):
                quote_txt = line.lstrip()[2:]
                try:
                    insert_quote_block(driver, quote_txt)
                except Exception:
                    pyperclip.copy(quote_txt); safe_hotkey(driver, 'ctrl', 'v')
            else:
                clean = line.replace('**', '')
                pyperclip.copy(clean); safe_hotkey(driver, 'ctrl', 'v')
            if line_align:
                try:
                    safe_hotkey(driver, 'shift', 'home')
                    apply_alignment(driver, line_align, select_all_first=False)
                except Exception: pass
            safe_press(driver, 'enter'); time.sleep(0.1)

        # 전체 정렬
        try:
            apply_alignment(driver, alignment, select_all_first=True)
        except Exception: pass

        # 저장 / 예약 / 발행
        time.sleep(1)
        try:
            if save_mode == 0:
                # 임시저장
                btns = driver.find_elements(By.TAG_NAME, 'button')
                for b in btns:
                    if b.text.strip() == '저장':
                        b.click(); break
                self.main.log("  💾 임시저장")
            elif save_mode == 1:
                # 예약발행: 발행 패널 열기 → 예약 라디오 → 시간 입력 → 발행
                self.main.log(f"  ⏰ 예약발행: {schedule}")
                try:
                    btns = driver.find_elements(By.TAG_NAME, 'button')
                    for b in btns:
                        if '발행' in (b.text or '') and '예약' not in (b.text or ''):
                            b.click(); break
                    time.sleep(1.5)
                    try:
                        el = driver.find_element(By.XPATH, "//*[contains(text(),'예약')]")
                        el.click(); time.sleep(0.5)
                    except Exception: pass
                    if schedule:
                        try:
                            inputs = driver.find_elements(By.XPATH, "//input[@type='text' or @type='datetime-local']")
                            if inputs:
                                inputs[-1].click(); time.sleep(0.2)
                                pyperclip.copy(schedule); safe_hotkey(driver, 'ctrl', 'a')
                                safe_press(driver, 'delete')
                                safe_hotkey(driver, 'ctrl', 'v'); time.sleep(0.5)
                        except Exception as e:
                            self.main.log(f"    ⚠️ 예약시간 입력 실패: {e}")
                    btns = driver.find_elements(By.TAG_NAME, 'button')
                    for b in btns:
                        if (b.text or '').strip() in ('발행', '예약발행'):
                            b.click(); break
                except Exception as e:
                    self.main.log(f"    ⚠️ 예약발행 처리 실패: {e}")
            else:
                # 즉시발행
                btns = driver.find_elements(By.TAG_NAME, 'button')
                for b in btns:
                    if '발행' in (b.text or ''):
                        b.click(); break
                time.sleep(1.5)
                try:
                    btns = driver.find_elements(By.TAG_NAME, 'button')
                    for b in btns:
                        if (b.text or '').strip() == '발행':
                            b.click(); break
                except Exception: pass
                self.main.log("  📤 발행")
            time.sleep(3)
        except Exception as e:
            self.main.log(f"    ⚠️ 저장/발행 버튼 클릭 실패: {e}")


    def _tb_quote(self):
        cur = self.body_editor.textCursor()
        if cur.hasSelection():
            text = cur.selectedText().replace('\u2029', '\n')
            new = '\n'.join(('> ' + l) if l.strip() else l for l in text.split('\n'))
            cur.insertText(new)
        else:
            cur.insertText("\n> ")

    def _tb_bold(self):
        cur = self.body_editor.textCursor()
        if cur.hasSelection():
            text = cur.selectedText().replace('\u2029', '\n')
            cur.insertText(f"**{text}**")
        else:
            cur.insertText("****")
            for _ in range(2):
                cur.movePosition(cur.MoveOperation.Left)
            self.body_editor.setTextCursor(cur)

    def _tb_linebreak(self):
        cur = self.body_editor.textCursor()
        cur.insertText("\n\n")

    def _tb_align(self, mode):
        cur = self.body_editor.textCursor()
        cur.insertText(f"{{{{ALIGN:{mode}}}}}")

    def _pick_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "이미지 선택", "", "이미지 (*.png *.jpg *.jpeg *.webp)")
        if paths:
            self.image_paths.setText(', '.join(paths))

    def run_upload(self):
        title = self.title_input.text().strip()
        body = self.body_editor.toPlainText().strip()
        if not title:
            self.main.log("⚠️ 제목 입력"); return
        if not body:
            self.main.log("⚠️ 본문 입력"); return
        account = self.account_combo.currentText().strip()
        if not account:
            self.main.log("⚠️ 네이버 계정 설정 (💾 설정 페이지)"); return
        align_map = {0: 'LEFT', 1: 'CENTER', 2: 'RIGHT', 3: 'JUSTIFY'}
        alignment = align_map.get(self.alignment_combo.currentIndex(), 'LEFT')
        images = [p.strip() for p in self.image_paths.text().split(',') if p.strip()]
        self.upload_btn.setEnabled(False); self.upload_btn.setText("🚀 업로드 중...")

        t = WorkerThread(self._do_upload, title, body, alignment, images, account)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.main.log)
        t.result_signal.connect(self.main.log)
        t.finished_signal.connect(lambda: (
            self.upload_btn.setEnabled(True),
            self.upload_btn.setText("🚀 네이버 블로그 업로드")))
        t.start()

    def _do_upload(self, title, body, alignment, images, account_id):
        import undetected_chromedriver as uc
        import re as _re
        opts = uc.ChromeOptions()
        opts.add_argument('--start-maximized'); opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage'); opts.add_argument('--disable-gpu')
        opts.add_argument('--user-data-dir=./chrome_profile')
        driver = make_uc_driver(opts)
        try:
            acc = None
            for a in self.main.config.get('naver_accounts', []) or []:
                if a.get('id') == account_id:
                    acc = a; break
            nid = (acc or {}).get('id') or account_id or self.main.config.get('naver_id', '')
            npw = (acc or {}).get('pw') or self.main.config.get('naver_pw', '')
            naver_login_with_fallback(driver, nid, npw, log_fn=self.main.log)

            try:
                driver.execute_script('window.location.href = "https://blog.naver.com/GoBlogWrite.naver";')
            except Exception:
                driver.get("https://blog.naver.com/GoBlogWrite.naver")
            time.sleep(5)

            try:
                iframe = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, 'mainFrame')))
                driver.switch_to.frame(iframe); time.sleep(1)
            except Exception: pass

            # 제목
            try:
                safe_press(driver, 'tab'); time.sleep(0.3)
                pyperclip.copy(title); safe_hotkey(driver, 'ctrl', 'v'); time.sleep(0.5)
                safe_press(driver, 'tab'); time.sleep(0.5)
            except Exception: pass

            # 본문: 라인별 처리 (인용구/굵게/정렬 마커)
            for line in body.split('\n'):
                if not line.strip():
                    safe_press(driver, 'enter'); time.sleep(0.1); continue
                line_align = None
                m = _re.match(r"\{\{ALIGN:(LEFT|CENTER|RIGHT|JUSTIFY)\}\}(.*)", line)
                if m:
                    line_align = m.group(1); line = m.group(2)

                if line.lstrip().startswith('> '):
                    quote_text = line.lstrip()[2:]
                    try:
                        insert_quote_block(driver, quote_text)
                    except Exception:
                        pyperclip.copy(quote_text); safe_hotkey(driver, 'ctrl', 'v')
                else:
                    clean = line.replace('**', '')  # 굵게 마커 단순 제거 (fallback)
                    pyperclip.copy(clean); safe_hotkey(driver, 'ctrl', 'v')

                if line_align:
                    try:
                        safe_hotkey(driver, 'shift', 'home')
                        apply_alignment(driver, line_align, select_all_first=False)
                    except Exception: pass
                safe_press(driver, 'enter'); time.sleep(0.1)

            # 전체 정렬
            try:
                apply_alignment(driver, alignment, select_all_first=True)
            except Exception: pass

            if images:
                self.main.log(f"📎 이미지 {len(images)}장 — 자동 첨부는 추후 (수동 첨부 권장)")

            return "✅ 업로드 준비 완료 (브라우저에서 발행 버튼 확인)"
        finally:
            pass  # driver는 닫지 않음 (사용자가 직접 발행 확인)


# ═══════════════════════════════════════════════════════════════
#  프롬프트 라이브러리 다이얼로그
# ═══════════════════════════════════════════════════════════════
class PromptLibraryDialog(QDialog):
    """프롬프트를 제목별로 저장/선택하는 다이얼로그."""
    LIBRARY_FILE = 'prompt_library.json'

    def __init__(self, current_text='', parent=None):
        super().__init__(parent)
        self.setWindowTitle("프롬프트 관리")
        self.setMinimumSize(760, 500)
        self._library = self._load_library()
        self._selected_text = current_text
        self._selected_title = ''
        self._build_ui()
        self._refresh_list()
        if self._library:
            self._list.setCurrentRow(0)

    def _load_library(self):
        try:
            if os.path.exists(self.LIBRARY_FILE):
                with open(self.LIBRARY_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_library(self):
        try:
            with open(self.LIBRARY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._library, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _build_ui(self):
        root = QVBoxLayout(self)

        desc = QLabel("라디오를 눌러 활성 프롬프트를 선택하세요. 편집은 우측 에디터에서 자동 저장됩니다.")
        desc.setStyleSheet("color:#aaa; font-size:11px; padding:4px;")
        root.addWidget(desc)

        splitter_row = QHBoxLayout()

        # ── 왼쪽 패널 ──
        left = QVBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 이름/내용 검색")
        self._search.textChanged.connect(self._on_search)
        left.addWidget(self._search)

        add_btn = QPushButton("＋ 프롬프트 추가")
        add_btn.setStyleSheet("background:#4CAF50; color:#fff; font-weight:bold; padding:6px;")
        add_btn.clicked.connect(self._add_prompt)
        left.addWidget(add_btn)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget{background:#1e1e1e; border:1px solid #444;}"
            "QListWidget::item{padding:6px 8px; border-bottom:1px solid #2a2a2a;}"
            "QListWidget::item:selected{background:#2a3a2a; color:#fff;}"
        )
        self._list.currentRowChanged.connect(self._on_select)
        left.addWidget(self._list, 1)

        self._count_label = QLabel("총 0개")
        self._count_label.setStyleSheet("color:#aaa; font-size:11px; padding:2px;")
        left.addWidget(self._count_label)

        left_w = QWidget(); left_w.setLayout(left); left_w.setMaximumWidth(240)
        splitter_row.addWidget(left_w)

        # ── 오른쪽 패널 ──
        right = QVBoxLayout()

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("이름"))
        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("항목 제목")
        self._title_edit.textChanged.connect(self._on_title_changed)
        name_row.addWidget(self._title_edit, 1)
        self._active_badge = QLabel("활성")
        self._active_badge.setStyleSheet(
            "background:#4CAF50; color:#fff; font-size:11px; font-weight:bold; "
            "padding:2px 8px; border-radius:4px;")
        self._active_badge_shown = False
        self._name_row_ref = name_row  # 나중에 배지 동적 추가용
        right.addLayout(name_row)

        right.addWidget(QLabel("프롬프트 내용"))
        self._text_edit = QTextEdit()
        self._text_edit.setPlaceholderText(
            "예: 친근하고 정보가 풍부한 블로그 글로 재작성.\n"
            "사용 가능 변수: {keyword} {content} #1"
        )
        self._text_edit.textChanged.connect(self._on_text_changed)
        right.addWidget(self._text_edit, 1)

        bot = QHBoxLayout()
        vars_lbl = QLabel("사용 가능 변수: {keyword} · {content} · #1")
        vars_lbl.setStyleSheet("color:#888; font-size:10px;")
        bot.addWidget(vars_lbl); bot.addStretch()
        del_btn = QPushButton("🗑️ 삭제")
        del_btn.clicked.connect(self._del_prompt)
        apply_btn = QPushButton("✅ 선택하여 적용")
        apply_btn.setStyleSheet("background:#4CAF50; color:#fff; font-weight:bold; padding:6px 14px;")
        apply_btn.clicked.connect(self._apply)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.reject)
        bot.addWidget(del_btn); bot.addWidget(close_btn); bot.addWidget(apply_btn)
        right.addLayout(bot)

        splitter_row.addLayout(right, 1)
        root.addLayout(splitter_row, 1)

    def _refresh_list(self, search=''):
        self._list.blockSignals(True)
        self._list.clear()
        for p in self._library:
            title = p.get('title', '(제목 없음)')
            preview = (p.get('text', '') or '').replace('\n', ' ')[:40]
            if search and search.lower() not in title.lower() and search.lower() not in preview.lower():
                continue
            item = QListWidgetItem(f"  {title}\n  {preview}")
            item.setToolTip(title)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._count_label.setText(f"총 {len(self._library)}개")

    def _on_search(self, text):
        self._refresh_list(text)

    def _on_select(self, row):
        if 0 <= row < len(self._library):
            p = self._library[row]
            self._title_edit.blockSignals(True)
            self._text_edit.blockSignals(True)
            self._title_edit.setText(p.get('title', ''))
            self._text_edit.setPlainText(p.get('text', ''))
            self._title_edit.blockSignals(False)
            self._text_edit.blockSignals(False)
            if not self._active_badge_shown:
                self._name_row_ref.addWidget(self._active_badge)
                self._active_badge_shown = True

    def _on_title_changed(self, text):
        row = self._list.currentRow()
        if 0 <= row < len(self._library):
            self._library[row]['title'] = text
            item = self._list.currentItem()
            if item:
                preview = (self._library[row].get('text', '') or '').replace('\n', ' ')[:40]
                item.setText(f"  {text}\n  {preview}")
            self._save_library()

    def _on_text_changed(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._library):
            self._library[row]['text'] = self._text_edit.toPlainText()
            self._save_library()

    def _add_prompt(self):
        self._library.append({'title': '새 프롬프트', 'text': ''})
        self._refresh_list()
        self._list.setCurrentRow(len(self._library) - 1)
        self._title_edit.setFocus(); self._title_edit.selectAll()

    def _del_prompt(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._library):
            self._library.pop(row)
            self._save_library()
            self._refresh_list()
            self._list.setCurrentRow(max(0, row - 1))

    def _apply(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._library):
            self._selected_text = self._library[row].get('text', '')
            self._selected_title = self._library[row].get('title', '')
        self._save_library()
        self.accept()

    def get_selected_text(self):
        return self._selected_text

    def get_selected_title(self):
        return self._selected_title


# ═══════════════════════════════════════════════════════════════
#  멀티 플랫폼 변형 업로드
#  블로그스팟 / 워드프레스 / 숏츠 / 스레드 / 인스타그램
# ═══════════════════════════════════════════════════════════════
class MultiPlatformPage(QWidget):
    """기존 블로그 글을 여러 플랫폼용으로 AI 변환 후 업로드/내보내기"""

    STATE_FILE = 'multiplatform_state.json'

    PLATFORMS = [
        ("naver",      "🇰🇷 네이버 블로그"),
        ("blogspot",   "📝 블로그스팟"),
        ("wordpress",  "🌐 워드프레스"),
        ("shorts",     "🎬 숏츠 스크립트"),
        ("threads",    "🧵 스레드"),
        ("instagram",  "📸 인스타그램"),
    ]

    PLATFORM_PROMPTS = {
        "naver": (
            "다음 블로그 글을 네이버 블로그 스타일로 재작성해줘.\n"
            "- 제목: 클릭을 유도하는 제목 (30자 이내)\n"
            "- 도입부: 공감 가는 첫 문장으로 시작\n"
            "- 소제목: 이모지 활용 (예: 🔍 왜 중요한가?)\n"
            "- 친근하고 자연스러운 구어체\n"
            "- 마지막: 댓글 유도 + 관련 태그 10개 (#태그 형식)\n\n원문:\n{content}"
        ),
        "blogspot": (
            "다음 블로그 글을 Google Blogspot(블로거)용으로 재작성해줘.\n"
            "- SEO 최적화된 제목 포함\n- 소제목(H2/H3) 활용\n- 자연스러운 키워드 배치\n"
            "- 마지막에 관련 태그 5개 추천\n\n원문:\n{content}"
        ),
        "wordpress": (
            "다음 블로그 글을 WordPress용으로 재작성해줘.\n"
            "- SEO 메타 설명(160자 이내) 포함\n- 소제목(## H2, ### H3) 구조화\n"
            "- 키워드 밀도 최적화\n- 마지막에 카테고리/태그 추천\n\n원문:\n{content}"
        ),
        "shorts": (
            "다음 블로그 글의 핵심을 60초 유튜브 숏츠 대본으로 변환해줘.\n"
            "- 첫 3초: 강렬한 훅 (질문 또는 충격적 사실)\n"
            "- 중반: 핵심 정보 3가지 (간결하게)\n"
            "- 마지막: 행동 유도(좋아요 구독)\n"
            "- 총 150~200단어 이내, 자막용 짧은 문장\n\n원문:\n{content}"
        ),
        "threads": (
            "다음 블로그 글을 Threads(스레드) 포맷으로 변환해줘.\n"
            "- 5~8개의 연속 포스트로 구성\n"
            "- 각 포스트는 500자 이내\n"
            "- 첫 포스트는 클릭을 유도하는 훅\n"
            "- 번호 표시: [1/6] 형식\n"
            "- 마지막 포스트는 CTA\n\n원문:\n{content}"
        ),
        "instagram": (
            "다음 블로그 글을 인스타그램 캡션으로 변환해줘.\n"
            "- 첫 줄: 이모지로 시작하는 강력한 훅\n"
            "- 본문: 핵심 내용을 줄바꿈으로 가독성 높게\n"
            "- 2200자 이내\n"
            "- 마지막 줄: CTA (댓글 유도)\n"
            "- 줄바꿈 후 관련 해시태그 20~30개\n\n원문:\n{content}"
        ),
    }

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._results = {k: '' for k, _ in self.PLATFORMS}
        self._worker = None
        self._pending = []
        self._mp_active_prompt = ''
        print('[MP-DEBUG] __init__ start')
        try:
            self._build_ui()
            print('[MP-DEBUG] _build_ui done')
        except Exception as _e:
            print(f'[MP-DEBUG] _build_ui FAILED: {_e}')
            import traceback; traceback.print_exc()
            return
        try:
            self._load_state()
            print('[MP-DEBUG] _load_state done')
        except Exception as _e:
            print(f'[MP-DEBUG] _load_state FAILED: {_e}')
            import traceback; traceback.print_exc()
        # 라이브러리 개수 초기 표시
        if os.path.exists(PromptLibraryDialog.LIBRARY_FILE):
            try:
                with open(PromptLibraryDialog.LIBRARY_FILE, encoding='utf-8') as _f:
                    _cnt = len(json.load(_f))
                self._mp_lib_btn.setText(f"🗂 프롬프트 관리 / 변경    총 {_cnt}개")
            except Exception:
                pass

    # ── 단계 상수 ──────────────────────────────────────
    WIZARD_STEPS = [
        ("1", "⚙️ 설정"),
        ("2", "📄 변환"),
        ("3", "🚀 업로드"),
    ]

    def _build_ui(self):
        self.setMaximumWidth(1200)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 상단 스테퍼 바 ──────────────────────────────
        stepper_bar = QWidget()
        stepper_bar.setStyleSheet("background:#1a1a1a; border-bottom:2px solid #333;")
        stepper_bar.setFixedHeight(54)
        step_row = QHBoxLayout(stepper_bar)
        step_row.setContentsMargins(16, 6, 16, 6)
        step_row.setSpacing(0)
        self._step_btns = []
        for i, (num, label) in enumerate(self.WIZARD_STEPS):
            btn = QPushButton(f"  {num}  {label}  ")
            btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton {
                    background:#2a2a2a; color:#888; border:1px solid #444;
                    border-radius:4px; font-size:13px; font-weight:bold;
                    padding:6px 18px; margin:0 4px;
                }
                QPushButton:checked {
                    background:#FFD700; color:#1e1e1e; border:1px solid #FFD700;
                }
                QPushButton:hover:!checked { background:#333; color:#ccc; }
            """)
            btn.clicked.connect(lambda _c, idx=i: self._go_step(idx))
            step_row.addWidget(btn)
            self._step_btns.append(btn)
            if i < len(self.WIZARD_STEPS) - 1:
                arrow = QLabel("›")
                arrow.setStyleSheet("color:#555; font-size:20px; margin:0 2px;")
                step_row.addWidget(arrow)
        step_row.addStretch()

        # 로그 패널 (상단 우측)
        self.log_panel = QTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setFixedHeight(54)
        self.log_panel.setMaximumWidth(420)
        self.log_panel.setStyleSheet("background:#1a1a1a; color:#bbb; font-size:11px; border:none; border-left:1px solid #333;")
        step_row.addWidget(self.log_panel)

        root.addWidget(stepper_bar)

        # ── 스택 위젯 (단계별 화면) ─────────────────────
        from PyQt6.QtWidgets import QStackedWidget as _QSW_MP
        self._stack = _QSW_MP()
        root.addWidget(self._stack, 1)

        print('[MP-DEBUG] building step1')
        self._build_step1()   # STEP1 = 설정
        print('[MP-DEBUG] building step2')
        self._build_step2()   # STEP2 = 원본 입력 & AI 변환 (합쳐진)
        print('[MP-DEBUG] building step3')
        self._build_step3()   # STEP3 = 업로드
        print('[MP-DEBUG] refreshing acc table')

        # step2 생성 후 계정 콤보 다시 갱신 (빌드 순서 이슈 보정)
        if hasattr(self, '_acc_table'):
            self._refresh_acc_table()

        print('[MP-DEBUG] go_step 0')
        self._go_step(0)
        print('[MP-DEBUG] _build_ui complete')

    def _go_step(self, idx):
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._step_btns):
            btn.setChecked(i == idx)
        if idx == 1:  # STEP2(원본입력+AI변환)로 갈 때 동기화
            self._sync_input_tab_from_src()
            if hasattr(self, '_s3_src_list'):
                self._s3_sync_src_list()
            if hasattr(self, '_s3_gen_list'):
                self._s3_sync_gen_list()

    def _src_btn_select(self, key):
        """A 소스 버튼 단일 선택"""
        for k, btn in self._src_btns.items():
            btn.setChecked(k == key)
        # 선택된 소스 라벨 업데이트 + 목록 접기
        SOURCE_LABELS = {
            "direct": "✏️ 직접 입력", "file": "📂 파일", "url": "🔗 URL",
            "naver": "🇰🇷 네이버", "blogspot": "📝 블로그스팟", "wordpress": "🌐 워드프레스",
        }
        if hasattr(self, '_a_selected_lbl'):
            self._a_selected_lbl.setText(SOURCE_LABELS.get(key, key))
        if hasattr(self, '_a_btns_widget') and self._a_btns_widget.isVisible():
            self._a_btns_widget.hide()
            if hasattr(self, '_a_toggle_btn'):
                self._a_toggle_btn.setText("▼")

    def _sync_input_tab_from_src(self):
        """A에서 선택한 소스에 맞춰 STEP2 입력 탭 자동 전환"""
        if not hasattr(self, '_src_btns') or not hasattr(self, '_input_tabs_widget'):
            return
        selected = next((k for k, b in self._src_btns.items() if b.isChecked()), 'direct')
        TAB_MAP = {'direct': 0, 'file': 1, 'url': 2,
                   'naver': 3, 'blogspot': 3, 'wordpress': 3}
        tab_idx = TAB_MAP.get(selected, 0)
        self._input_tabs_widget.setCurrentIndex(tab_idx)
        # 사이트 탭이면 계정 콤보에서 해당 계정 미리 선택
        if selected in ('naver', 'blogspot', 'wordpress') and hasattr(self, '_mp_acc_url_combo'):
            for i in range(self._mp_acc_url_combo.count()):
                txt = self._mp_acc_url_combo.itemText(i)
                plat_map = {'naver': '네이버', 'blogspot': '블로그스팟', 'wordpress': '워드프레스'}
                if f"({plat_map.get(selected, '')})" in txt:
                    self._mp_acc_url_combo.setCurrentIndex(i)
                    break


    def _build_step1(self):
        page = QWidget()
        page.setStyleSheet("background:#1e1e1e;")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 14, 20, 14); outer.setSpacing(12)

        hdr = QLabel("⚙️ STEP 1 — 변환 설정")
        hdr.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        outer.addWidget(hdr)

        # ── A → B 플로우 영역 ──────────────────────────────────
        flow_frame = QWidget()
        flow_frame.setStyleSheet("background:#252525; border:1px solid #444; border-radius:10px;")
        flow_v = QVBoxLayout(flow_frame); flow_v.setContentsMargins(16, 14, 16, 14); flow_v.setSpacing(10)

        flow_lbl = QLabel("📋 변환 흐름 설정")
        flow_lbl.setStyleSheet("color:#FFD700; font-weight:bold; font-size:14px;")
        flow_v.addWidget(flow_lbl)

        flow_row = QHBoxLayout(); flow_row.setSpacing(0)

        # ── A: 가져올 곳 (접기/펼치기) ──
        a_frame = QWidget()
        a_frame.setStyleSheet("background:#1e1e1e; border:2px solid #FFD700; border-radius:8px;")
        a_v = QVBoxLayout(a_frame); a_v.setContentsMargins(10, 8, 10, 8); a_v.setSpacing(5)

        a_lbl = QLabel("A  가져올 곳")
        a_lbl.setStyleSheet("color:#FFD700; font-weight:bold; font-size:12px;")
        a_v.addWidget(a_lbl)

        SOURCE_ITEMS = [
            ("direct",    "✏️ 직접 입력",   "#555"),
            ("file",      "📂 파일",         "#555"),
            ("url",       "🔗 URL",          "#2d5a8e"),
            ("naver",     "🇰🇷 네이버",      "#03C75A"),
            ("blogspot",  "📝 블로그스팟",   "#FF5722"),
            ("wordpress", "🌐 워드프레스",   "#21759b"),
        ]
        self._src_btns = {}
        # 2열 그리드로 항상 표시
        from PyQt6.QtWidgets import QGridLayout as _QGL_SRC
        a_grid = _QGL_SRC(); a_grid.setSpacing(4)
        for i, (key, label, color) in enumerate(SOURCE_ITEMS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == "direct")
            btn.setStyleSheet(f"""
                QPushButton {{
                    color:#aaa; font-size:11px; font-weight:bold;
                    padding:5px 8px; border-radius:5px;
                    background:#2a2a2a; border:2px solid #444;
                    text-align:left;
                }}
                QPushButton:checked {{
                    background:{color if color != '#555' else '#444'}; border:2px solid {color if color != '#555' else '#888'};
                    color:#fff;
                }}
                QPushButton:hover:!checked {{ background:#333; color:#ccc; }}
            """)
            btn.clicked.connect(lambda _c, k=key: self._src_btn_select(k))
            self._src_btns[key] = btn
            a_grid.addWidget(btn, i // 2, i % 2)
        a_v.addLayout(a_grid)
        # 더미 속성 (호환성)
        self._a_btns_widget = QWidget()
        self._a_toggle_btn = QPushButton()
        self._a_selected_lbl = QLabel()
        flow_row.addWidget(a_frame, 1)

        # ── 화살표 ──
        arr_lbl = QLabel("→")
        arr_lbl.setStyleSheet("color:#FFD700; font-size:32px; font-weight:bold; padding:0 12px;")
        arr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        flow_row.addWidget(arr_lbl)

        # ── B: 올릴 곳 (다중 선택) ──
        b_frame = QWidget()
        b_frame.setStyleSheet("background:#1e1e1e; border:2px solid #2d8a4e; border-radius:8px;")
        b_v = QVBoxLayout(b_frame); b_v.setContentsMargins(10, 8, 10, 8); b_v.setSpacing(5)
        b_lbl = QLabel("B  올릴 곳  (복수 선택)")
        b_lbl.setStyleSheet("color:#4CAF50; font-weight:bold; font-size:12px;")
        b_v.addWidget(b_lbl)

        PLAT_COLORS = {
            "naver": "#03C75A", "blogspot": "#FF5722", "wordpress": "#21759b",
            "shorts": "#FF0000", "threads": "#888", "instagram": "#C13584",
        }
        self._platform_checks = {}
        from PyQt6.QtWidgets import QGridLayout as _QGL_PLAT
        b_grid = _QGL_PLAT(); b_grid.setSpacing(4)
        for i, (key, label) in enumerate(self.PLATFORMS):
            color = PLAT_COLORS.get(key, "#555")
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setStyleSheet(f"""
                QPushButton {{
                    color:#aaa; font-size:11px; font-weight:bold;
                    padding:5px 8px; border-radius:5px;
                    background:#2a2a2a; border:2px solid #444;
                    text-align:left;
                }}
                QPushButton:checked {{
                    background:{color}33; border:2px solid {color};
                    color:#fff;
                }}
                QPushButton:hover:!checked {{ background:#333; color:#ccc; }}
            """)
            self._platform_checks[key] = btn
            b_grid.addWidget(btn, i // 2, i % 2)
        b_v.addLayout(b_grid)
        flow_row.addWidget(b_frame, 1)

        flow_v.addLayout(flow_row)
        outer.addWidget(flow_frame)

        # ── AI 모델 + 프롬프트 (가로 배치) ──────────────────────
        ai_row_frame = QWidget()
        ai_row_frame.setStyleSheet("background:#252525; border:1px solid #444; border-radius:8px;")
        ai_row_h = QHBoxLayout(ai_row_frame); ai_row_h.setContentsMargins(14,10,14,10); ai_row_h.setSpacing(16)

        ai_lbl = QLabel("🤖 AI 모델:")
        ai_lbl.setStyleSheet("color:#ccc; font-weight:bold; font-size:13px;")
        ai_row_h.addWidget(ai_lbl)
        self.ai_model = QComboBox()
        self.ai_model.addItems([
            "gpt-4o", "gemini-2.5-flash",
            "sonar-pro (Perplexity)", "sonar-reasoning (Perplexity Pro)"
        ])
        self.ai_model.setStyleSheet("color:#fff; background:#333; border:1px solid #555; padding:4px; min-width:200px;")
        ai_row_h.addWidget(self.ai_model)
        ai_row_h.addSpacing(20)

        # 프롬프트 카드 (인라인)
        self._mp_card_title_row = QHBoxLayout()
        _icon = QLabel("📝"); _icon.setFixedWidth(18)
        self._mp_card_title_row.addWidget(_icon)
        self._mp_card_title = QLabel("(선택된 프롬프트 없음)")
        self._mp_card_title.setStyleSheet("color:#aaa; font-size:12px;")
        self._mp_card_title_row.addWidget(self._mp_card_title)
        self._mp_card_badge = QLabel("활성")
        self._mp_card_badge.setStyleSheet(
            "background:#4CAF50; color:#fff; font-size:10px; padding:1px 6px; border-radius:8px;")
        self._mp_card_badge_shown = False
        self._mp_card_preview = QLabel("")
        self._mp_card_preview.setStyleSheet("color:#888; font-size:11px;")

        self._mp_prompt_frame = QWidget()
        pf_l = QVBoxLayout(self._mp_prompt_frame)
        pf_l.setContentsMargins(0,0,0,0); pf_l.setSpacing(2)
        pf_l.addLayout(self._mp_card_title_row)
        pf_l.addWidget(self._mp_card_preview)
        ai_row_h.addWidget(self._mp_prompt_frame, 1)

        self._mp_lib_btn = QPushButton("🗂 프롬프트 선택")
        self._mp_lib_btn.setStyleSheet("padding:5px 14px; background:#333; color:#ddd; border:1px solid #555; border-radius:5px;")
        self._mp_lib_btn.clicked.connect(self._mp_open_prompt_library)
        ai_row_h.addWidget(self._mp_lib_btn)

        outer.addWidget(ai_row_frame)

        # 계정 관리 (접기/펼치기)
        acc_toggle_btn = QPushButton("👤 계정 관리 펼치기 ▼")
        acc_toggle_btn.setStyleSheet("background:#2a2a2a; color:#aaa; border:1px solid #555; padding:6px; text-align:left;")
        outer.addWidget(acc_toggle_btn)
        self._acc_mgr_widget = QWidget()
        self._acc_mgr_widget.hide()
        outer.addWidget(self._acc_mgr_widget)
        self._build_settings_tab_inline(self._acc_mgr_widget)

        def _toggle_acc():
            if self._acc_mgr_widget.isVisible():
                self._acc_mgr_widget.hide()
                acc_toggle_btn.setText("👤 계정 관리 펼치기 ▼")
            else:
                self._acc_mgr_widget.show()
                acc_toggle_btn.setText("👤 계정 관리 접기 ▲")
        acc_toggle_btn.clicked.connect(_toggle_acc)

        # 하단 네비
        nav = QHBoxLayout()
        nav.addStretch()
        next_btn = QPushButton("다음 단계 → 원본 입력")
        next_btn.setStyleSheet(
            "background:#FFD700; color:#1e1e1e; font-weight:bold; "
            "font-size:14px; padding:10px 28px; border-radius:6px;")
        next_btn.clicked.connect(lambda: self._go_step(1))
        nav.addWidget(next_btn)
        outer.addStretch()
        outer.addLayout(nav)

        self._stack.addWidget(page)

    # ── STEP 2: 원본 입력 & AI 변환 (합쳐진) ─────────────
    def _build_step2(self):
        page = QWidget()
        page.setStyleSheet("background:#1e1e1e;")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 10, 12, 10); outer.setSpacing(6)

        hdr = QLabel("📄 STEP 2 — 원본 입력 & AI 변환")
        hdr.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700; padding-bottom:4px;")
        outer.addWidget(hdr)

        TAB_STYLE = """
            QTabBar::tab { background:#2a2a2a; color:#aaa; padding:5px 12px; border-radius:4px 4px 0 0; }
            QTabBar::tab:selected { background:#333; color:#FFD700; font-weight:bold; }
        """
        INP_STYLE2 = "color:#fff; background:#2a2a2a; border:1px solid #555; padding:4px 8px;"

        # ── 본문: 좌(40%) + 우(60%) 스플리터 ──
        from PyQt6.QtWidgets import QSplitter as _QSpl
        splitter = _QSpl(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background:#333; width:4px; }")

        # ═══ 좌측 패널 (원본 입력) ═══════════════════════════
        left_w = QWidget()
        left_w.setStyleSheet("background:#1a1a1a;")
        left_v = QVBoxLayout(left_w); left_v.setContentsMargins(6,6,6,6); left_v.setSpacing(6)

        from PyQt6.QtWidgets import QTabWidget as _QTW_S2
        input_tabs = _QTW_S2()
        input_tabs.setStyleSheet(TAB_STYLE)
        input_tabs.setMaximumHeight(200)
        self._input_tabs_widget = input_tabs

        # ① 직접 입력
        paste_w = QWidget()
        paste_l = QVBoxLayout(paste_w); paste_l.setContentsMargins(4,4,4,4); paste_l.setSpacing(3)
        self.source_input = QTextEdit()
        self.source_input.setPlaceholderText("변환할 블로그 원문을 여기에 붙여넣으세요...")
        self.source_input.setStyleSheet("background:#111; color:#e0e0e0; font-size:12px; border:1px solid #444;")
        self.source_input.textChanged.connect(lambda: self._save_state())
        paste_l.addWidget(self.source_input, 1)
        clr_btn = QPushButton("🗑️ 지우기")
        clr_btn.setFixedWidth(90)
        clr_btn.clicked.connect(self.source_input.clear)
        paste_l.addWidget(clr_btn)
        input_tabs.addTab(paste_w, "✏️ 직접입력")

        # ② 파일
        file_w = QWidget()
        file_l = QVBoxLayout(file_w); file_l.setContentsMargins(6,6,6,6); file_l.setSpacing(6)
        load_btn = QPushButton("📂 파일 불러오기 (.txt)")
        load_btn.setStyleSheet("background:#2d5a8e; color:#fff; font-size:12px; padding:8px; border-radius:5px;")
        load_btn.clicked.connect(self._load_from_file)
        file_l.addWidget(load_btn)
        self._file_label = QLabel("선택된 파일 없음")
        self._file_label.setStyleSheet("color:#888; font-size:11px;")
        file_l.addWidget(self._file_label)
        file_l.addStretch()
        input_tabs.addTab(file_w, "📂 파일")

        # ③ URL
        url_w = QWidget()
        url_l = QVBoxLayout(url_w); url_l.setContentsMargins(6,6,6,6); url_l.setSpacing(5)
        url_inp_row = QHBoxLayout()
        self._mp_url_input = QLineEdit()
        self._mp_url_input.setPlaceholderText("🔗 https://blog.naver.com/id/12345678")
        self._mp_url_input.setStyleSheet(INP_STYLE2)
        url_inp_row.addWidget(self._mp_url_input, 1)
        url_fetch_btn = QPushButton("📥 가져오기")
        url_fetch_btn.setStyleSheet("background:#2d8a4e; color:#fff; padding:4px 12px; border-radius:4px;")
        url_fetch_btn.clicked.connect(self._mp_fetch_single_url)
        url_inp_row.addWidget(url_fetch_btn)
        url_l.addLayout(url_inp_row)
        self._url_result_label = QLabel("")
        self._url_result_label.setStyleSheet("color:#aaa; font-size:10px;")
        self._url_result_label.setWordWrap(True)
        url_l.addWidget(self._url_result_label)
        url_l.addStretch()
        input_tabs.addTab(url_w, "🔗 URL")

        # ④ 사이트
        site_w = QWidget()
        site_l = QVBoxLayout(site_w); site_l.setContentsMargins(6,6,6,6); site_l.setSpacing(4)

        site_acc_row = QHBoxLayout()
        site_acc_lbl = QLabel("계정:")
        site_acc_lbl.setFixedWidth(40)
        site_acc_row.addWidget(site_acc_lbl)
        self._mp_acc_url_combo = QComboBox()
        self._mp_acc_url_combo.addItem("내 계정 선택")
        self._mp_acc_url_combo.setStyleSheet("color:#fff; background:#2a2a2a; border:1px solid #555; padding:2px 4px;")
        self._mp_acc_url_combo.currentIndexChanged.connect(self._mp_acc_url_changed)
        site_acc_row.addWidget(self._mp_acc_url_combo, 1)
        site_l.addLayout(site_acc_row)

        site_inp_row = QHBoxLayout()
        self._mp_site_input = QLineEdit()
        self._mp_site_input.setPlaceholderText("🌐 https://blog.naver.com/myid")
        self._mp_site_input.setStyleSheet(INP_STYLE2)
        site_inp_row.addWidget(self._mp_site_input, 1)
        self._mp_url_count = QSpinBox()
        self._mp_url_count.setMinimum(1); self._mp_url_count.setMaximum(20)
        self._mp_url_count.setValue(5)
        self._mp_url_count.setPrefix("최근 "); self._mp_url_count.setSuffix("개")
        self._mp_url_count.setFixedWidth(82)
        site_inp_row.addWidget(self._mp_url_count)
        site_fetch_btn = QPushButton("📥 목록 가져오기")
        site_fetch_btn.setStyleSheet("background:#2d5a8e; color:#fff; padding:4px 10px; border-radius:4px;")
        site_fetch_btn.clicked.connect(self._mp_fetch_url)
        site_inp_row.addWidget(site_fetch_btn)
        site_l.addLayout(site_inp_row)

        self._mp_post_list = QListWidget()
        self._mp_post_list.setStyleSheet(
            "background:#111; color:#ddd; border:1px solid #444; font-size:11px;"
            "QListWidget::item:selected{background:#2d5a8e;}"
            "QListWidget::item:hover{background:#2a2a2a;}")
        self._mp_post_list.setToolTip("클릭: 원본 글로 선택 / 더블클릭: 전체 내용 보기")
        self._mp_post_list.itemClicked.connect(self._mp_post_select)
        self._mp_post_list.itemDoubleClicked.connect(self._mp_post_preview)
        self._mp_fetched_posts = []
        site_l.addWidget(self._mp_post_list, 1)
        input_tabs.addTab(site_w, "🌐 사이트")

        left_v.addWidget(input_tabs)

        # 원본 글 목록
        src_lbl = QLabel("📋 원본 목록")
        src_lbl.setStyleSheet("color:#ccc; font-weight:bold; font-size:11px;")
        left_v.addWidget(src_lbl)

        self._s3_src_list = QListWidget()
        self._s3_src_list.setStyleSheet(
            "background:#111; color:#ddd; border:1px solid #333; font-size:11px;"
            "QListWidget::item:selected{background:#2d5a8e;}"
            "QListWidget::item:hover{background:#222;}")
        self._s3_src_list.setToolTip("클릭: 원본 미리보기 / '이 글 변환' 버튼")
        self._s3_src_list.itemClicked.connect(self._s3_src_preview)
        left_v.addWidget(self._s3_src_list, 1)

        self._s3_no_src_lbl = QLabel("사이트/URL로\n글을 가져오거나\n직접 입력하세요")
        self._s3_no_src_lbl.setStyleSheet("color:#555; font-size:11px; padding:8px;")
        self._s3_no_src_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_v.addWidget(self._s3_no_src_lbl)

        src_btn_row = QHBoxLayout()
        src_prev_btn = QPushButton("👁 미리보기")
        src_prev_btn.setStyleSheet("font-size:11px; padding:3px 8px;")
        src_prev_btn.clicked.connect(lambda: self._s3_src_preview(self._s3_src_list.currentItem()))
        src_btn_row.addWidget(src_prev_btn)
        src_use_btn = QPushButton("✅ 이 글 변환")
        src_use_btn.setStyleSheet("background:#2d8a4e; color:#fff; font-size:11px; padding:3px 8px;")
        src_use_btn.clicked.connect(lambda: self._s3_src_select_and_transform(self._s3_src_list.currentItem()))
        src_btn_row.addWidget(src_use_btn)
        left_v.addLayout(src_btn_row)

        # 숨겨진 gen list (호환성용)
        self._s3_gen_list = QListWidget()
        self._s3_gen_list.hide()

        splitter.addWidget(left_w)

        # ═══ 우측 패널 (AI 변환 결과) ════════════════════════
        right_w = QWidget()
        right_w.setStyleSheet("background:#1e1e1e;")
        right_v = QVBoxLayout(right_w); right_v.setContentsMargins(6,6,6,6); right_v.setSpacing(6)

        # 변환 버튼 행
        transform_row = QHBoxLayout()
        self.transform_btn = QPushButton("🤖 AI 변환 시작")
        self.transform_btn.setStyleSheet(
            "background:#FFD700; color:#1e1e1e; font-weight:bold; "
            "font-size:13px; padding:7px 24px; border-radius:6px;")
        self.transform_btn.clicked.connect(self._run_transform)
        transform_row.addWidget(self.transform_btn)
        self.stop_btn = QPushButton("⏹ 정지")
        self.stop_btn.setStyleSheet("background:#f44336; color:#fff; padding:6px 14px; border-radius:6px;")
        self.stop_btn.clicked.connect(self._stop_transform)
        self.stop_btn.setEnabled(False)
        transform_row.addWidget(self.stop_btn)
        transform_row.addStretch()
        right_v.addLayout(transform_row)

        # 결과 탭
        from PyQt6.QtWidgets import QTabWidget as _QTW_S3
        self.result_tabs = _QTW_S3()
        self.result_tabs.setStyleSheet("""
            QTabBar::tab { background:#2a2a2a; color:#aaa; padding:5px 12px; border-radius:4px 4px 0 0; }
            QTabBar::tab:selected { background:#333; color:#FFD700; font-weight:bold; }
        """)
        self._result_widgets = {}

        for key, label in self.PLATFORMS:
            tab_w = QWidget()
            tab_l = QVBoxLayout(tab_w); tab_l.setContentsMargins(6,6,6,6); tab_l.setSpacing(4)
            result_edit = QTextEdit()
            result_edit.setPlaceholderText(f"{label} 변환 결과가 여기에 표시됩니다...")
            result_edit.setStyleSheet("background:#111; color:#e0e0e0; font-size:12px; border:none;")
            result_edit.textChanged.connect(lambda k=key, e=result_edit: self._on_result_edited(k, e))
            tab_l.addWidget(result_edit, 1)
            copy_row = QHBoxLayout()
            cp = QPushButton("📋 복사")
            cp.setStyleSheet("padding:3px 10px; font-size:11px;")
            cp.clicked.connect(lambda _c, e=result_edit: self._copy_result(e))
            copy_row.addWidget(cp)
            sv = QPushButton("💾 저장")
            sv.setStyleSheet("padding:3px 10px; font-size:11px;")
            sv.clicked.connect(lambda _c, k=key, e=result_edit: self._save_result(k, e))
            copy_row.addWidget(sv)
            prev_btn = QPushButton("👁 미리보기")
            prev_btn.setStyleSheet("padding:3px 10px; font-size:11px;")
            prev_btn.clicked.connect(lambda _c, k=key, lb=label: self._s3_result_preview(k, lb))
            copy_row.addWidget(prev_btn)
            copy_row.addStretch()
            tab_l.addLayout(copy_row)
            self.result_tabs.addTab(tab_w, label)
            self._result_widgets[key] = result_edit

        right_v.addWidget(self.result_tabs, 1)
        splitter.addWidget(right_w)

        # 40/60 비율
        splitter.setSizes([400, 600])
        outer.addWidget(splitter, 1)

        nav = QHBoxLayout()
        back_btn2 = QPushButton("← 이전")
        back_btn2.setStyleSheet("padding:9px 24px; background:#333; color:#ddd; border-radius:5px;")
        back_btn2.clicked.connect(lambda: self._go_step(0))
        nav.addWidget(back_btn2)
        nav.addStretch()
        next_btn2 = QPushButton("다음 단계 → 업로드")
        next_btn2.setStyleSheet(
            "background:#FFD700; color:#1e1e1e; font-weight:bold; "
            "font-size:14px; padding:10px 28px; border-radius:6px;")
        next_btn2.clicked.connect(lambda: self._go_step(2))
        nav.addWidget(next_btn2)
        outer.addLayout(nav)

        self._stack.addWidget(page)

    def _mp_fetch_single_url(self):
        """URL 탭: 단일 URL 1개만 가져와서 source_input에 바로 채움"""
        url = self._mp_url_input.text().strip()
        if not url:
            self._log("⚠️ URL을 입력하세요"); return
        self._log(f"🔗 URL 크롤링 중... ({url})")
        self._url_result_label.setText("가져오는 중...")
        # _mp_url_count를 일시적으로 1로 설정하고 기존 fetch 사용
        orig_count = self._mp_url_count.value()
        self._mp_url_count.setValue(1)
        # site input에 url 복사하여 기존 fetch 호출
        self._mp_site_input.setText(url)
        self._mp_fetch_url()
        self._mp_url_count.setValue(orig_count)

    def _build_settings_tab_inline(self, container):
        """계정 관리 UI를 container 위젯 안에 구성"""
        self._accounts = self._load_accounts()
        container.setStyleSheet("""
            QWidget { color:#fff; background:#1e1e1e; }
            QLabel { color:#ddd; }
            QLineEdit { color:#fff; background:#3a3a3a; border:1px solid #555; padding:2px 4px; }
            QTabBar::tab { color:#ccc; background:#2a2a2a; padding:4px 10px; }
            QTabBar::tab:selected { color:#FFD700; background:#333; font-weight:bold; }
            QTableWidget { color:#ddd; background:#1a1a1a; gridline-color:#333; }
            QHeaderView::section { color:#ccc; background:#2a2a2a; border:1px solid #444; padding:3px; }
        """)
        lay = QVBoxLayout(container); lay.setContentsMargins(0,6,0,0); lay.setSpacing(6)

        self._acc_table = QTableWidget(0, 5)
        self._acc_table.setHorizontalHeaderLabels(["유형", "계정명", "URL / Blog ID", "사용자", ""])
        hdr = self._acc_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._acc_table.setMaximumHeight(140)
        lay.addWidget(self._acc_table)

        tb_row = QHBoxLayout()
        for label, key, color in [
            ("＋ 네이버", "naver", "#03C75A"), ("＋ WordPress", "wordpress", "#21759b"),
            ("＋ Blogspot", "blogspot", "#FF5722"), ("＋ YouTube", "youtube", "#FF0000"),
            ("＋ Threads", "threads", "#666"), ("＋ Instagram", "instagram", "#C13584"),
        ]:
            btn = QPushButton(label)
            btn.setStyleSheet(f"background:{color}; color:#fff; padding:3px 7px; font-size:11px;")
            btn.clicked.connect(lambda _c, k=key: self._focus_acc_form(k))
            tb_row.addWidget(btn)
        del_btn = QPushButton("🗑️ 삭제")
        del_btn.clicked.connect(self._del_account)
        tb_row.addWidget(del_btn); tb_row.addStretch()
        lay.addLayout(tb_row)

        from PyQt6.QtWidgets import QTabWidget as _QTWF2
        form_tabs = _QTWF2()
        form_tabs.setMaximumHeight(120)

        INP_STYLE = "color:#fff; background:#3a3a3a; border:1px solid #555; padding:2px 4px;"

        nv_tab = QWidget(); nv_l = QVBoxLayout(nv_tab); nv_l.setContentsMargins(4,4,4,4); nv_l.setSpacing(3)
        nv_r1 = QHBoxLayout()
        nv_r1.addWidget(QLabel("계정명:")); self._inp_nv_name = QLineEdit(); self._inp_nv_name.setPlaceholderText("예: 내 네이버"); self._inp_nv_name.setStyleSheet(INP_STYLE)
        nv_r1.addWidget(self._inp_nv_name, 1)
        nv_r1.addWidget(QLabel("네이버 ID:")); self._inp_nv_blog_id = QLineEdit(); self._inp_nv_blog_id.setPlaceholderText("네이버 아이디"); self._inp_nv_blog_id.setStyleSheet(INP_STYLE)
        nv_r1.addWidget(self._inp_nv_blog_id, 1)
        nv_r1.addWidget(QLabel("비밀번호:")); self._inp_nv_client_id = QLineEdit(); self._inp_nv_client_id.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_nv_client_id.setPlaceholderText("네이버 비밀번호"); self._inp_nv_client_id.setStyleSheet(INP_STYLE)
        nv_r1.addWidget(self._inp_nv_client_id, 1)
        _eye_nv = QPushButton("👁"); _eye_nv.setFixedWidth(28); _eye_nv.setCheckable(True)
        _eye_nv.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_nv.toggled.connect(lambda on, f=self._inp_nv_client_id: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        nv_r1.addWidget(_eye_nv)
        self._inp_nv_secret = QLineEdit()  # 하위호환용 (미사용)
        sv_nv = QPushButton("저장"); sv_nv.clicked.connect(self._save_nv_account); nv_r1.addWidget(sv_nv)
        test_nv = QPushButton("🔌 테스트")
        test_nv.setStyleSheet("background:#1a73e8; color:#fff; padding:2px 8px; border-radius:3px;")
        test_nv.clicked.connect(self._test_naver_login)
        nv_r1.addWidget(test_nv)
        nv_l.addLayout(nv_r1)
        nv_notice = QLabel("💡 네이버 ID/PW로 자동 로그인 후 업로드합니다. 2단계 인증 사용 시 비활성화 필요.")
        nv_notice.setStyleSheet("color:#aaa; font-size:10px;")
        nv_l.addWidget(nv_notice)
        form_tabs.addTab(nv_tab, "🇰🇷 네이버")

        # WordPress
        wp_tab = QWidget(); wp_l = QVBoxLayout(wp_tab); wp_l.setContentsMargins(4,4,4,4); wp_l.setSpacing(3)
        wp_r1 = QHBoxLayout()
        wp_r1.addWidget(QLabel("계정명:")); self._inp_wp_name = QLineEdit(); self._inp_wp_name.setPlaceholderText("내 워드프레스"); self._inp_wp_name.setStyleSheet(INP_STYLE)
        wp_r1.addWidget(self._inp_wp_name, 1)
        wp_r1.addWidget(QLabel("URL:")); self._inp_wp_url = QLineEdit(); self._inp_wp_url.setPlaceholderText("https://yourblog.com"); self._inp_wp_url.setStyleSheet(INP_STYLE)
        wp_r1.addWidget(self._inp_wp_url, 2)
        wp_l.addLayout(wp_r1)
        wp_r2 = QHBoxLayout()
        wp_r2.addWidget(QLabel("사용자명:")); self._inp_wp_user = QLineEdit(); self._inp_wp_user.setPlaceholderText("admin"); self._inp_wp_user.setStyleSheet(INP_STYLE)
        wp_r2.addWidget(self._inp_wp_user, 1)
        wp_r2.addWidget(QLabel("앱 비밀번호:")); self._inp_wp_pass = QLineEdit()
        self._inp_wp_pass.setEchoMode(QLineEdit.EchoMode.Password); self._inp_wp_pass.setPlaceholderText("xxxx xxxx xxxx xxxx"); self._inp_wp_pass.setStyleSheet(INP_STYLE)
        wp_r2.addWidget(self._inp_wp_pass, 2)
        _eye_wp = QPushButton("👁"); _eye_wp.setFixedWidth(28); _eye_wp.setCheckable(True)
        _eye_wp.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_wp.toggled.connect(lambda on, f=self._inp_wp_pass: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        wp_r2.addWidget(_eye_wp)
        sv_wp = QPushButton("저장"); sv_wp.clicked.connect(self._save_wp_account); wp_r2.addWidget(sv_wp)
        test_wp = QPushButton("🔌 테스트")
        test_wp.setStyleSheet("background:#1a73e8; color:#fff; padding:2px 8px; border-radius:3px;")
        test_wp.clicked.connect(self._test_wp_login)
        wp_r2.addWidget(test_wp)
        wp_l.addLayout(wp_r2)
        form_tabs.addTab(wp_tab, "🌐 WordPress")

        bs_tab = QWidget(); bs_l = QVBoxLayout(bs_tab); bs_l.setContentsMargins(4,4,4,4); bs_l.setSpacing(3)
        bs_r1 = QHBoxLayout()
        bs_r1.addWidget(QLabel("계정명:")); self._inp_bs_name = QLineEdit(); self._inp_bs_name.setPlaceholderText("내 블로거"); self._inp_bs_name.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_name, 1)
        bs_r1.addWidget(QLabel("블로그 URL:")); self._inp_bs_url = QLineEdit(); self._inp_bs_url.setPlaceholderText("https://myblog.blogspot.com"); self._inp_bs_url.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_url, 2)
        bs_r1.addWidget(QLabel("Blog ID:")); self._inp_bs_id = QLineEdit(); self._inp_bs_id.setPlaceholderText("예: 1271002762142343021"); self._inp_bs_id.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_id, 1)
        bs_r1.addWidget(QLabel("JSON:"))
        self._inp_bs_key = QLineEdit(); self._inp_bs_key.setPlaceholderText("client_secret.json 경로"); self._inp_bs_key.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_key, 2)
        bs_json_btn = QPushButton("📁"); bs_json_btn.setFixedWidth(28)
        bs_json_btn.clicked.connect(lambda: self._inp_bs_key.setText(
            QFileDialog.getOpenFileName(self, "client_secret.json 선택", "", "JSON (*.json)")[0]))
        bs_r1.addWidget(bs_json_btn)
        sv_bs = QPushButton("저장"); sv_bs.clicked.connect(self._save_bs_account); bs_r1.addWidget(sv_bs)
        bs_l.addLayout(bs_r1)
        bs_notice = QLabel("💡 Blog ID: draft.blogger.com/blog/settings/숫자 — JSON: Google Cloud OAuth 클라이언트 파일")
        bs_notice.setStyleSheet("color:#aaa; font-size:10px;")
        bs_l.addWidget(bs_notice)
        form_tabs.addTab(bs_tab, "📝 Blogspot")

        yt_tab = QWidget(); yt_l = QHBoxLayout(yt_tab); yt_l.setContentsMargins(4,4,4,4)
        yt_l.addWidget(QLabel("채널명:")); self._inp_yt_name = QLineEdit(); self._inp_yt_name.setPlaceholderText("내 채널"); self._inp_yt_name.setStyleSheet(INP_STYLE)
        yt_l.addWidget(self._inp_yt_name, 1)
        yt_l.addWidget(QLabel("채널 ID:")); self._inp_yt_channel = QLineEdit(); self._inp_yt_channel.setPlaceholderText("UCxxxxxxxxxx"); self._inp_yt_channel.setStyleSheet(INP_STYLE)
        yt_l.addWidget(self._inp_yt_channel, 1)
        yt_l.addWidget(QLabel("API Key:")); self._inp_yt_key = QLineEdit(); self._inp_yt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_yt_key.setPlaceholderText("YouTube Data API Key"); self._inp_yt_key.setStyleSheet(INP_STYLE)
        yt_l.addWidget(self._inp_yt_key, 2)
        _eye_yt = QPushButton("👁"); _eye_yt.setFixedWidth(28); _eye_yt.setCheckable(True)
        _eye_yt.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_yt.toggled.connect(lambda on, f=self._inp_yt_key: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        yt_l.addWidget(_eye_yt)
        sv_yt = QPushButton("저장"); sv_yt.clicked.connect(self._save_yt_account); yt_l.addWidget(sv_yt)
        form_tabs.addTab(yt_tab, "🎬 YouTube")

        th_tab = QWidget(); th_l = QHBoxLayout(th_tab); th_l.setContentsMargins(4,4,4,4)
        th_l.addWidget(QLabel("계정명:")); self._inp_th_name = QLineEdit(); self._inp_th_name.setPlaceholderText("@myaccount"); self._inp_th_name.setStyleSheet(INP_STYLE)
        th_l.addWidget(self._inp_th_name, 1)
        th_l.addWidget(QLabel("User ID:")); self._inp_th_uid = QLineEdit(); self._inp_th_uid.setPlaceholderText("Meta User ID"); self._inp_th_uid.setStyleSheet(INP_STYLE)
        th_l.addWidget(self._inp_th_uid, 1)
        th_l.addWidget(QLabel("Access Token:")); self._inp_th_token = QLineEdit(); self._inp_th_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_th_token.setPlaceholderText("Meta Access Token"); self._inp_th_token.setStyleSheet(INP_STYLE)
        th_l.addWidget(self._inp_th_token, 2)
        _eye_th = QPushButton("👁"); _eye_th.setFixedWidth(28); _eye_th.setCheckable(True)
        _eye_th.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_th.toggled.connect(lambda on, f=self._inp_th_token: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        th_l.addWidget(_eye_th)
        sv_th = QPushButton("저장"); sv_th.clicked.connect(self._save_th_account); th_l.addWidget(sv_th)
        form_tabs.addTab(th_tab, "🧵 Threads")

        ig_tab = QWidget(); ig_l = QHBoxLayout(ig_tab); ig_l.setContentsMargins(4,4,4,4)
        ig_l.addWidget(QLabel("계정명:")); self._inp_ig_name = QLineEdit(); self._inp_ig_name.setPlaceholderText("@myaccount"); self._inp_ig_name.setStyleSheet(INP_STYLE)
        ig_l.addWidget(self._inp_ig_name, 1)
        ig_l.addWidget(QLabel("계정 ID:")); self._inp_ig_uid = QLineEdit(); self._inp_ig_uid.setPlaceholderText("Instagram Business ID"); self._inp_ig_uid.setStyleSheet(INP_STYLE)
        ig_l.addWidget(self._inp_ig_uid, 1)
        ig_l.addWidget(QLabel("Access Token:")); self._inp_ig_token = QLineEdit(); self._inp_ig_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_ig_token.setPlaceholderText("Meta Access Token"); self._inp_ig_token.setStyleSheet(INP_STYLE)
        ig_l.addWidget(self._inp_ig_token, 2)
        _eye_ig = QPushButton("👁"); _eye_ig.setFixedWidth(28); _eye_ig.setCheckable(True)
        _eye_ig.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_ig.toggled.connect(lambda on, f=self._inp_ig_token: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        ig_l.addWidget(_eye_ig)
        sv_ig = QPushButton("저장"); sv_ig.clicked.connect(self._save_ig_account); ig_l.addWidget(sv_ig)
        form_tabs.addTab(ig_tab, "📸 Instagram")

        lay.addWidget(form_tabs)
        self._form_tabs = form_tabs
        self._refresh_acc_table()

    # ── STEP 3: 업로드 ─────────────────────────────────
    def _build_step3(self):
        page = QWidget()
        page.setStyleSheet("background:#1e1e1e;")
        v = QVBoxLayout(page)
        v.setContentsMargins(24, 16, 24, 16); v.setSpacing(12)

        hdr = QLabel("🚀 STEP 3 — 업로드")
        hdr.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700; padding-bottom:4px;")
        v.addWidget(hdr)

        notice = QLabel("각 플랫폼 행에서 계정을 선택하고 업로드 버튼을 누르세요.")
        notice.setStyleSheet("color:#aaa; font-size:12px;")
        v.addWidget(notice)

        # 플랫폼별 업로드 행
        upload_frame = QWidget()
        upload_frame.setStyleSheet("background:#252525; border:1px solid #444; border-radius:8px;")
        uf_v = QVBoxLayout(upload_frame); uf_v.setContentsMargins(12,10,12,10); uf_v.setSpacing(8)

        UPLOAD_ROWS = [
            ("naver",     "🇰🇷 네이버 블로그",  "#03C75A", True),
            ("blogspot",  "📝 블로그스팟",       "#FF5722", True),
            ("wordpress", "🌐 워드프레스",        "#21759b", True),
            ("threads",   "🧵 스레드",           "#555",    False),
            ("instagram", "📸 인스타그램",        "#C13584", False),
            ("shorts",    "🎬 숏츠/유튜브",       "#FF0000", False),
        ]

        for key, label, color, can_upload in UPLOAD_ROWS:
            row_w = QWidget()
            row_w.setStyleSheet(f"background:#2a2a2a; border-left:4px solid {color}; border-radius:4px;")
            row_h = QHBoxLayout(row_w); row_h.setContentsMargins(10,6,10,6); row_h.setSpacing(8)
            name_lbl = QLabel(label)
            name_lbl.setFixedWidth(110)
            name_lbl.setStyleSheet(f"color:{color}; font-weight:bold; font-size:13px;")
            row_h.addWidget(name_lbl)

            # 계정 콤보
            acc_combo = QComboBox()
            acc_combo.setStyleSheet("color:#fff; background:#333; border:1px solid #555; padding:2px 4px;")
            row_h.addWidget(acc_combo, 1)

            # 결과 미리보기 라벨
            preview_lbl = QLabel("(변환 결과 없음)")
            preview_lbl.setStyleSheet("color:#666; font-size:11px;")
            preview_lbl.setSizePolicy(preview_lbl.sizePolicy().horizontalPolicy(),
                                       preview_lbl.sizePolicy().verticalPolicy())
            row_h.addWidget(preview_lbl, 1)

            if can_upload:
                # 저장 방법 콤보
                method_combo = QComboBox()
                method_combo.addItems(["📝 임시저장", "🗓 예약발행", "🚀 발행"])
                method_combo.setCurrentIndex(2)  # 기본: 발행
                method_combo.setFixedWidth(100)
                method_combo.setStyleSheet(
                    "color:#fff; background:#333; border:1px solid #555; padding:2px 4px;")
                row_h.addWidget(method_combo)
                setattr(self, f"_up4_method_{key}", method_combo)

                up_btn = QPushButton(f"업로드")
                up_btn.setStyleSheet(
                    f"background:{color}; color:#fff; font-weight:bold; "
                    f"padding:6px 16px; border-radius:5px; min-width:80px;")
                if key == "naver":
                    self._nv_acc_combo = acc_combo
                    up_btn.clicked.connect(lambda _c, k=key: self._upload_from_step4(k))
                elif key == "blogspot":
                    self._bs_acc_combo = acc_combo
                    up_btn.clicked.connect(lambda _c, k=key: self._upload_from_step4(k))
                elif key == "wordpress":
                    self._wp_acc_combo = acc_combo
                    up_btn.clicked.connect(lambda _c, k=key: self._upload_from_step4(k))
                row_h.addWidget(up_btn)
            else:
                open_btn = QPushButton("열기")
                open_btn.setStyleSheet("background:#444; color:#ddd; padding:6px 16px; border-radius:5px;")
                urls = {"threads": "https://www.threads.net",
                        "instagram": "https://www.instagram.com",
                        "shorts": "https://studio.youtube.com"}
                open_btn.clicked.connect(lambda _c, u=urls.get(key, ''): self._open_url(u))
                if key == "threads":
                    self._th_acc_combo = acc_combo
                elif key == "instagram":
                    self._ig_acc_combo = acc_combo
                elif key == "shorts":
                    self._yt_acc_combo = acc_combo
                row_h.addWidget(open_btn)

            # 상태 라벨
            status_lbl = QLabel("")
            status_lbl.setFixedWidth(36)
            status_lbl.setStyleSheet("font-size:18px;")
            row_h.addWidget(status_lbl)

            setattr(self, f"_up4_preview_{key}", preview_lbl)
            setattr(self, f"_up4_status_{key}", status_lbl)
            uf_v.addWidget(row_w)

        v.addWidget(upload_frame, 1)

        nav = QHBoxLayout()
        back_btn = QPushButton("← 이전 (결과 확인)")
        back_btn.setStyleSheet("padding:9px 24px; background:#333; color:#ddd; border-radius:5px;")
        back_btn.clicked.connect(lambda: self._go_step(1))
        nav.addWidget(back_btn); nav.addStretch()
        v.addLayout(nav)

        self._stack.addWidget(page)

    def _s3_sync_src_list(self):
        """STEP2에서 가져온 목록을 STEP3 원본 목록에 동기화"""
        self._s3_src_list.clear()
        posts = getattr(self, '_mp_fetched_posts', [])
        if posts:
            self._s3_no_src_lbl.hide()
            for title, text, url in posts:
                preview = text[:50].replace('\n', ' ')
                item = QListWidgetItem(f"📄 {title}\n   {preview}...")
                item.setData(Qt.ItemDataRole.UserRole, (title, text, url))
                self._s3_src_list.addItem(item)
        else:
            txt = self.source_input.toPlainText().strip()
            if txt:
                self._s3_no_src_lbl.hide()
                preview = txt[:60].replace('\n', ' ')
                item = QListWidgetItem(f"✏️ (직접 입력)\n   {preview}...")
                item.setData(Qt.ItemDataRole.UserRole, ('직접 입력', txt, ''))
                self._s3_src_list.addItem(item)
                self._s3_src_list.setCurrentRow(0)
            else:
                self._s3_no_src_lbl.show()

    def _s3_sync_gen_list(self):
        """변환 결과 목록 갱신"""
        self._s3_gen_list.clear()
        for key, label in self.PLATFORMS:
            text = self._results.get(key, '')
            if text:
                preview = text[:40].replace('\n', ' ')
                item = QListWidgetItem(f"{label}\n   {preview}...")
                item.setData(Qt.ItemDataRole.UserRole, key)
                self._s3_gen_list.addItem(item)

    def _s3_src_preview(self, item):
        if item is None: return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data: return
        title, text, url = data
        dlg = QDialog(self); dlg.setWindowTitle(title); dlg.resize(560, 450)
        vl = QVBoxLayout(dlg)
        lbl = QLabel(f"<b>{title}</b>" + (f"<br><small>{url}</small>" if url else ""))
        lbl.setWordWrap(True); lbl.setStyleSheet("color:#FFD700; padding:4px;")
        vl.addWidget(lbl)
        te = QTextEdit(); te.setReadOnly(True); te.setPlainText(text)
        te.setStyleSheet("background:#1e1e1e; color:#ddd;"); vl.addWidget(te)
        br = QHBoxLayout()
        use = QPushButton("✅ 이 글로 변환 시작")
        use.setStyleSheet("background:#2d8a4e; color:#fff; padding:6px 16px;")
        use.clicked.connect(lambda: (self.source_input.setPlainText(text), dlg.accept(), self._run_transform()))
        br.addWidget(use)
        cl = QPushButton("닫기"); cl.clicked.connect(dlg.reject); br.addWidget(cl)
        vl.addLayout(br); dlg.exec()

    def _s3_src_select_and_transform(self, item):
        if item is None: return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data: return
        _, text, _ = data
        self.source_input.setPlainText(text)
        self._run_transform()

    def _s3_gen_select(self, item):
        if item is None: return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key:
            keys = [k for k, _ in self.PLATFORMS]
            if key in keys:
                self.result_tabs.setCurrentIndex(keys.index(key))

    def _s3_gen_preview(self, item):
        if item is None: return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key:
            label = dict(self.PLATFORMS).get(key, key)
            self._s3_result_preview(key, label)

    def _s3_result_preview(self, key, label):
        text = self._results.get(key, '')
        if not text: self._log(f"⚠️ {label} 변환 결과 없음"); return
        dlg = QDialog(self); dlg.setWindowTitle(f"{label} 변환 결과"); dlg.resize(600, 500)
        vl = QVBoxLayout(dlg)
        te = QTextEdit(); te.setReadOnly(True); te.setPlainText(text)
        te.setStyleSheet("background:#1e1e1e; color:#ddd; font-size:12px;"); vl.addWidget(te)
        br = QHBoxLayout()
        cp = QPushButton("📋 복사")
        cp.clicked.connect(lambda: (QApplication.clipboard().setText(text), self._log("📋 복사됨")))
        br.addWidget(cp)
        cl = QPushButton("닫기"); cl.clicked.connect(dlg.reject); br.addWidget(cl)
        vl.addLayout(br); dlg.exec()

    # ── STEP 4: 업로드 ─────────────────────────────────
    def _upload_from_step4(self, key):
        if key not in self._result_widgets:
            self._log(f"⚠️ {key} 결과 없음"); return
        editor = self._result_widgets[key]
        status_lbl = getattr(self, f"_up4_status_{key}", None)
        if status_lbl:
            status_lbl.setText("⏳")
            status_lbl.setToolTip("업로드 중...")

        # 저장 방법 읽기 (0=임시저장, 1=예약발행, 2=발행)
        method_combo = getattr(self, f"_up4_method_{key}", None)
        upload_method = method_combo.currentIndex() if method_combo else 2
        # 0=draft, 1=scheduled, 2=publish
        METHOD_KEY = {0: "draft", 1: "scheduled", 2: "publish"}
        upload_mode = METHOD_KEY.get(upload_method, "publish")

        def _on_done(msg):
            self._log(msg)
            if status_lbl:
                if "✅" in msg or "완료" in msg:
                    status_lbl.setText("✅")
                    status_lbl.setToolTip(msg)
                    status_lbl.setStyleSheet("font-size:18px; color:#4CAF50;")
                else:
                    status_lbl.setText("❌")
                    status_lbl.setToolTip(msg)
                    status_lbl.setStyleSheet("font-size:18px; color:#f44336;")

        if key == "naver":
            self._upload_naver(editor, _on_done, upload_mode=upload_mode)
        elif key == "blogspot":
            self._upload_blogspot(editor, _on_done, upload_mode=upload_mode)
        elif key == "wordpress":
            self._upload_wordpress(editor, _on_done, upload_mode=upload_mode)

    def _update_step4_previews(self):
        """변환 완료 후 step4 미리보기 라벨 갱신"""
        for key, _ in self.PLATFORMS:
            lbl = getattr(self, f"_up4_preview_{key}", None)
            if lbl is None: continue
            text = self._results.get(key, '')
            if text:
                preview = text[:60].replace('\n', ' ')
                lbl.setText(f"✅ {preview}...")
                lbl.setStyleSheet("color:#aaa; font-size:11px;")
            else:
                lbl.setText("(변환 결과 없음)")
                lbl.setStyleSheet("color:#555; font-size:11px;")

    # ── 설정 탭 빌드 (기존 코드 호환용 stub) ─────────────
    def _build_settings_tab(self):
        pass  # step2 인라인으로 대체됨

    # ─── 설정 탭 (계정 관리) ─────────────────────────
    ACCOUNTS_FILE = 'multiplatform_accounts.json'

    def _load_accounts(self):
        try:
            if os.path.exists(self.ACCOUNTS_FILE):
                with open(self.ACCOUNTS_FILE, encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return []  # [{'type':'wordpress'|'blogspot', 'name':str, ...}]

    def _save_accounts(self):
        try:
            with open(self.ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._accounts, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _build_settings_tab(self):
        self._accounts = self._load_accounts()
        tab_w = QWidget()
        tab_l = QVBoxLayout(tab_w)
        tab_l.setContentsMargins(8, 8, 8, 8); tab_l.setSpacing(6)

        tab_l.addWidget(QLabel("📋 저장된 계정 목록 (WordPress / Blogspot)"))

        # 계정 테이블
        self._acc_table = QTableWidget(0, 5)
        self._acc_table.setHorizontalHeaderLabels(["유형", "계정명", "URL / Blog ID", "사용자", ""])
        hdr = self._acc_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        tab_l.addWidget(self._acc_table, 1)

        # 조작 버튼 행
        tb_row = QHBoxLayout()
        for label, key, color in [
            ("＋ 네이버",    "naver",     "#03C75A"),
            ("＋ WordPress", "wordpress", "#21759b"),
            ("＋ Blogspot",  "blogspot",  "#FF5722"),
            ("＋ YouTube",   "youtube",   "#FF0000"),
            ("＋ Threads",   "threads",   "#000000"),
            ("＋ Instagram", "instagram", "#C13584"),
        ]:
            btn = QPushButton(label)
            btn.setStyleSheet(f"background:{color}; color:#fff; padding:4px 8px; font-size:11px;")
            btn.clicked.connect(lambda _c, k=key: self._focus_acc_form(k))
            tb_row.addWidget(btn)
        del_btn = QPushButton("🗑️ 삭제")
        del_btn.clicked.connect(self._del_account)
        tb_row.addWidget(del_btn); tb_row.addStretch()
        tab_l.addLayout(tb_row)

        # 입력 폼 탭
        from PyQt6.QtWidgets import QTabWidget as _QTWF
        form_tabs = _QTWF()
        form_tabs.setMaximumHeight(120)

        def _make_row(*fields):
            """fields: list of (label, attr_name, placeholder, is_password)"""
            w = QWidget(); lay = QHBoxLayout(w); lay.setContentsMargins(4,4,4,4)
            for lbl, attr, ph, pwd in fields:
                lay.addWidget(QLabel(lbl))
                inp = QLineEdit(); inp.setPlaceholderText(ph)
                if pwd: inp.setEchoMode(QLineEdit.EchoMode.Password)
                setattr(self, attr, inp)
                lay.addWidget(inp, 1)
            return w

        # 네이버
        INP_STYLE = "color:#fff; background:#3a3a3a; border:1px solid #555; padding:2px 4px;"

        nv_tab = QWidget(); nv_l = QVBoxLayout(nv_tab); nv_l.setContentsMargins(4,4,4,4); nv_l.setSpacing(3)
        nv_r1 = QHBoxLayout()
        nv_r1.addWidget(QLabel("계정명:")); self._inp_nv_name = QLineEdit(); self._inp_nv_name.setPlaceholderText("예: 내 네이버"); self._inp_nv_name.setStyleSheet(INP_STYLE)
        nv_r1.addWidget(self._inp_nv_name, 1)
        nv_r1.addWidget(QLabel("네이버 ID:")); self._inp_nv_blog_id = QLineEdit(); self._inp_nv_blog_id.setPlaceholderText("네이버 아이디"); self._inp_nv_blog_id.setStyleSheet(INP_STYLE)
        nv_r1.addWidget(self._inp_nv_blog_id, 1)
        nv_r1.addWidget(QLabel("비밀번호:")); self._inp_nv_client_id = QLineEdit(); self._inp_nv_client_id.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_nv_client_id.setPlaceholderText("네이버 비밀번호"); self._inp_nv_client_id.setStyleSheet(INP_STYLE)
        nv_r1.addWidget(self._inp_nv_client_id, 1)
        _eye_nv2 = QPushButton("👁"); _eye_nv2.setFixedWidth(28); _eye_nv2.setCheckable(True)
        _eye_nv2.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_nv2.toggled.connect(lambda on, f=self._inp_nv_client_id: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        nv_r1.addWidget(_eye_nv2)
        self._inp_nv_secret = QLineEdit()  # 하위호환용 (미사용)
        sv_nv = QPushButton("저장"); sv_nv.clicked.connect(self._save_nv_account); nv_r1.addWidget(sv_nv)
        test_nv2 = QPushButton("🔌 테스트")
        test_nv2.setStyleSheet("background:#1a73e8; color:#fff; padding:2px 8px; border-radius:3px;")
        test_nv2.clicked.connect(self._test_naver_login)
        nv_r1.addWidget(test_nv2)
        nv_l.addLayout(nv_r1)
        nv_notice = QLabel("💡 네이버 ID/PW로 자동 로그인 후 업로드합니다. 2단계 인증 사용 시 비활성화 필요.")
        nv_notice.setStyleSheet("color:#aaa; font-size:10px;")
        nv_l.addWidget(nv_notice)
        form_tabs.addTab(nv_tab, "🇰🇷 네이버")

        # WordPress
        wp_tab = QWidget(); wp_l = QVBoxLayout(wp_tab); wp_l.setContentsMargins(4,4,4,4); wp_l.setSpacing(3)
        wp_r1 = QHBoxLayout()
        wp_r1.addWidget(QLabel("계정명:")); self._inp_wp_name = QLineEdit(); self._inp_wp_name.setPlaceholderText("내 워드프레스"); self._inp_wp_name.setStyleSheet(INP_STYLE)
        wp_r1.addWidget(self._inp_wp_name, 1)
        wp_r1.addWidget(QLabel("URL:")); self._inp_wp_url = QLineEdit(); self._inp_wp_url.setPlaceholderText("https://yourblog.com"); self._inp_wp_url.setStyleSheet(INP_STYLE)
        wp_r1.addWidget(self._inp_wp_url, 2)
        wp_l.addLayout(wp_r1)
        wp_r2 = QHBoxLayout()
        wp_r2.addWidget(QLabel("사용자명:")); self._inp_wp_user = QLineEdit(); self._inp_wp_user.setPlaceholderText("admin"); self._inp_wp_user.setStyleSheet(INP_STYLE)
        wp_r2.addWidget(self._inp_wp_user, 1)
        wp_r2.addWidget(QLabel("앱 비밀번호:")); self._inp_wp_pass = QLineEdit()
        self._inp_wp_pass.setEchoMode(QLineEdit.EchoMode.Password); self._inp_wp_pass.setPlaceholderText("xxxx xxxx xxxx xxxx"); self._inp_wp_pass.setStyleSheet(INP_STYLE)
        wp_r2.addWidget(self._inp_wp_pass, 2)
        _eye_wp2 = QPushButton("👁"); _eye_wp2.setFixedWidth(28); _eye_wp2.setCheckable(True)
        _eye_wp2.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_wp2.toggled.connect(lambda on, f=self._inp_wp_pass: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        wp_r2.addWidget(_eye_wp2)
        sv_wp = QPushButton("저장"); sv_wp.clicked.connect(self._save_wp_account); wp_r2.addWidget(sv_wp)
        test_wp = QPushButton("🔌 테스트")
        test_wp.setStyleSheet("background:#1a73e8; color:#fff; padding:2px 8px; border-radius:3px;")
        test_wp.clicked.connect(self._test_wp_login)
        wp_r2.addWidget(test_wp)
        wp_l.addLayout(wp_r2)
        form_tabs.addTab(wp_tab, "🌐 WordPress")

        # Blogspot
        bs_tab = QWidget(); bs_l = QVBoxLayout(bs_tab); bs_l.setContentsMargins(4,4,4,4); bs_l.setSpacing(3)
        bs_r1 = QHBoxLayout()
        bs_r1.addWidget(QLabel("계정명:")); self._inp_bs_name = QLineEdit(); self._inp_bs_name.setPlaceholderText("내 블로거"); self._inp_bs_name.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_name, 1)
        bs_r1.addWidget(QLabel("블로그 URL:")); self._inp_bs_url = QLineEdit(); self._inp_bs_url.setPlaceholderText("https://myblog.blogspot.com"); self._inp_bs_url.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_url, 2)
        bs_r1.addWidget(QLabel("Blog ID:")); self._inp_bs_id = QLineEdit(); self._inp_bs_id.setPlaceholderText("예: 1271002762142343021"); self._inp_bs_id.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_id, 1)
        bs_r1.addWidget(QLabel("JSON:"))
        self._inp_bs_key = QLineEdit(); self._inp_bs_key.setPlaceholderText("client_secret.json 경로"); self._inp_bs_key.setStyleSheet(INP_STYLE)
        bs_r1.addWidget(self._inp_bs_key, 2)
        bs_json_btn = QPushButton("📁"); bs_json_btn.setFixedWidth(28)
        bs_json_btn.clicked.connect(lambda: self._inp_bs_key.setText(
            QFileDialog.getOpenFileName(self, "client_secret.json 선택", "", "JSON (*.json)")[0]))
        bs_r1.addWidget(bs_json_btn)
        sv_bs = QPushButton("저장"); sv_bs.clicked.connect(self._save_bs_account); bs_r1.addWidget(sv_bs)
        bs_l.addLayout(bs_r1)
        bs_notice = QLabel("💡 Blog ID: draft.blogger.com/blog/settings/숫자 — JSON: Google Cloud OAuth 클라이언트 파일")
        bs_notice.setStyleSheet("color:#aaa; font-size:10px;")
        bs_l.addWidget(bs_notice)
        form_tabs.addTab(bs_tab, "📝 Blogspot")

        # YouTube
        yt_tab = QWidget(); yt_l = QHBoxLayout(yt_tab); yt_l.setContentsMargins(4,4,4,4)
        yt_l.addWidget(QLabel("채널명:")); self._inp_yt_name = QLineEdit(); self._inp_yt_name.setPlaceholderText("내 채널"); self._inp_yt_name.setStyleSheet(INP_STYLE)
        yt_l.addWidget(self._inp_yt_name, 1)
        yt_l.addWidget(QLabel("채널 ID:")); self._inp_yt_channel = QLineEdit(); self._inp_yt_channel.setPlaceholderText("UCxxxxxxxxxx"); self._inp_yt_channel.setStyleSheet(INP_STYLE)
        yt_l.addWidget(self._inp_yt_channel, 1)
        yt_l.addWidget(QLabel("API Key:")); self._inp_yt_key = QLineEdit(); self._inp_yt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_yt_key.setPlaceholderText("YouTube Data API Key"); self._inp_yt_key.setStyleSheet(INP_STYLE)
        yt_l.addWidget(self._inp_yt_key, 2)
        _eye_yt2 = QPushButton("👁"); _eye_yt2.setFixedWidth(28); _eye_yt2.setCheckable(True)
        _eye_yt2.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_yt2.toggled.connect(lambda on, f=self._inp_yt_key: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        yt_l.addWidget(_eye_yt2)
        sv_yt = QPushButton("저장"); sv_yt.clicked.connect(self._save_yt_account); yt_l.addWidget(sv_yt)
        form_tabs.addTab(yt_tab, "🎬 YouTube")

        # Threads
        th_tab = QWidget(); th_l = QHBoxLayout(th_tab); th_l.setContentsMargins(4,4,4,4)
        th_l.addWidget(QLabel("계정명:")); self._inp_th_name = QLineEdit(); self._inp_th_name.setPlaceholderText("@myaccount"); self._inp_th_name.setStyleSheet(INP_STYLE)
        th_l.addWidget(self._inp_th_name, 1)
        th_l.addWidget(QLabel("User ID:")); self._inp_th_uid = QLineEdit(); self._inp_th_uid.setPlaceholderText("Meta User ID"); self._inp_th_uid.setStyleSheet(INP_STYLE)
        th_l.addWidget(self._inp_th_uid, 1)
        th_l.addWidget(QLabel("Access Token:")); self._inp_th_token = QLineEdit(); self._inp_th_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_th_token.setPlaceholderText("Meta Access Token"); self._inp_th_token.setStyleSheet(INP_STYLE)
        th_l.addWidget(self._inp_th_token, 2)
        _eye_th2 = QPushButton("👁"); _eye_th2.setFixedWidth(28); _eye_th2.setCheckable(True)
        _eye_th2.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_th2.toggled.connect(lambda on, f=self._inp_th_token: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        th_l.addWidget(_eye_th2)
        sv_th = QPushButton("저장"); sv_th.clicked.connect(self._save_th_account); th_l.addWidget(sv_th)
        form_tabs.addTab(th_tab, "🧵 Threads")

        # Instagram
        ig_tab = QWidget(); ig_l = QHBoxLayout(ig_tab); ig_l.setContentsMargins(4,4,4,4)
        ig_l.addWidget(QLabel("계정명:")); self._inp_ig_name = QLineEdit(); self._inp_ig_name.setPlaceholderText("@myaccount"); self._inp_ig_name.setStyleSheet(INP_STYLE)
        ig_l.addWidget(self._inp_ig_name, 1)
        ig_l.addWidget(QLabel("계정 ID:")); self._inp_ig_uid = QLineEdit(); self._inp_ig_uid.setPlaceholderText("Instagram Business ID"); self._inp_ig_uid.setStyleSheet(INP_STYLE)
        ig_l.addWidget(self._inp_ig_uid, 1)
        ig_l.addWidget(QLabel("Access Token:")); self._inp_ig_token = QLineEdit(); self._inp_ig_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_ig_token.setPlaceholderText("Meta Access Token"); self._inp_ig_token.setStyleSheet(INP_STYLE)
        ig_l.addWidget(self._inp_ig_token, 2)
        _eye_ig2 = QPushButton("👁"); _eye_ig2.setFixedWidth(28); _eye_ig2.setCheckable(True)
        _eye_ig2.setStyleSheet("background:#444; border:1px solid #666; border-radius:3px; padding:0px;")
        _eye_ig2.toggled.connect(lambda on, f=self._inp_ig_token: f.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        ig_l.addWidget(_eye_ig2)
        sv_ig = QPushButton("저장"); sv_ig.clicked.connect(self._save_ig_account); ig_l.addWidget(sv_ig)
        form_tabs.addTab(ig_tab, "📸 Instagram")

        tab_l.addWidget(form_tabs)
        self._form_tabs = form_tabs

        self.result_tabs.addTab(tab_w, "⚙️ 설정")
        self._refresh_acc_table()

    def _refresh_acc_table(self):
        self._acc_table.setRowCount(0)
        TYPE_LABEL = {'wordpress': 'WP', 'blogspot': 'BS', 'naver': 'NV', 'youtube': 'YT', 'threads': 'TH', 'instagram': 'IG'}
        combos = {'wordpress': [], 'blogspot': [], 'naver': [], 'youtube': [], 'threads': [], 'instagram': []}
        for acc in self._accounts:
            r = self._acc_table.rowCount()
            self._acc_table.insertRow(r)
            t = acc.get('type', '')
            self._acc_table.setItem(r, 0, QTableWidgetItem(TYPE_LABEL.get(t, t.upper())))
            self._acc_table.setItem(r, 1, QTableWidgetItem(acc.get('name', '')))
            if t == 'wordpress':
                detail = acc.get('url', '')
            elif t in ('blogspot', 'naver'):
                detail = acc.get('blog_id', '')
            elif t == 'youtube':
                detail = acc.get('channel_id', '')
            else:
                detail = acc.get('uid', '')
            self._acc_table.setItem(r, 2, QTableWidgetItem(detail))
            self._acc_table.setItem(r, 3, QTableWidgetItem(acc.get('user', '') if t == 'wordpress' else ''))
            edit_btn = QPushButton("✏️ 편집")
            edit_btn.clicked.connect(lambda _c, idx=r: self._edit_account(idx))
            self._acc_table.setCellWidget(r, 4, edit_btn)
            if t in combos:
                combos[t].append(acc.get('name', ''))
        for attr, key in [('_wp_acc_combo', 'wordpress'), ('_bs_acc_combo', 'blogspot'),
                          ('_nv_acc_combo', 'naver'), ('_yt_acc_combo', 'youtube'),
                          ('_th_acc_combo', 'threads'), ('_ig_acc_combo', 'instagram')]:
            if hasattr(self, attr):
                combo = getattr(self, attr)
                cur = combo.currentText()
                combo.clear(); combo.addItems(["(계정 선택)"] + combos[key])
                idx = combo.findText(cur)
                if idx >= 0: combo.setCurrentIndex(idx)
        # URL 가져오기용 계정 콤보 갱신
        if hasattr(self, '_mp_acc_url_combo'):
            TYPE_KR = {'naver': '네이버', 'wordpress': '워드프레스', 'blogspot': '블로그스팟',
                       'youtube': '유튜브', 'threads': '스레드', 'instagram': '인스타'}
            all_items = [f"{acc.get('name','')} ({TYPE_KR.get(acc.get('type',''), acc.get('type',''))})"
                         for acc in self._accounts if acc.get('type') in ('naver', 'wordpress', 'blogspot')]
            cur = self._mp_acc_url_combo.currentText()
            self._mp_acc_url_combo.clear()
            self._mp_acc_url_combo.addItems(["내 계정 선택"] + all_items)
            idx = self._mp_acc_url_combo.findText(cur)
            if idx >= 0: self._mp_acc_url_combo.setCurrentIndex(idx)

    def _focus_acc_form(self, key):
        order = ['naver', 'wordpress', 'blogspot', 'youtube', 'threads', 'instagram']
        if hasattr(self, '_form_tabs') and key in order:
            self._form_tabs.setCurrentIndex(order.index(key))

    def _add_wp_account(self):
        self._inp_wp_name.clear(); self._inp_wp_url.clear()
        self._inp_wp_user.clear(); self._inp_wp_pass.clear()
        self._inp_wp_name.setFocus()

    def _add_bs_account(self):
        self._inp_bs_name.clear(); self._inp_bs_key.clear(); self._inp_bs_id.clear()
        self._inp_bs_name.setFocus()

    def _save_wp_account(self):
        name = self._inp_wp_name.text().strip()
        url = self._inp_wp_url.text().strip()
        user = self._inp_wp_user.text().strip()
        pwd = self._inp_wp_pass.text().strip()
        if not name or not url:
            self._log("⚠️ 계정명과 URL을 입력하세요"); return
        # 같은 이름 덮어쓰기
        for acc in self._accounts:
            if acc.get('type') == 'wordpress' and acc.get('name') == name:
                acc.update({'url': url, 'user': user, 'pass': pwd})
                self._save_accounts(); self._refresh_acc_table()
                self._log(f"✅ WordPress 계정 업데이트: {name}"); return
        self._accounts.append({'type': 'wordpress', 'name': name, 'url': url, 'user': user, 'pass': pwd})
        self._save_accounts(); self._refresh_acc_table()
        self._log(f"✅ WordPress 계정 저장: {name}")

    def _save_bs_account(self):
        name = self._inp_bs_name.text().strip()
        json_path = self._inp_bs_key.text().strip()
        bid = self._inp_bs_id.text().strip()
        blog_url = self._inp_bs_url.text().strip() if hasattr(self, '_inp_bs_url') else ''
        if not name or not bid:
            self._log("⚠️ 계정명과 Blog ID를 입력하세요"); return
        for acc in self._accounts:
            if acc.get('type') == 'blogspot' and acc.get('name') == name:
                acc.update({'json_path': json_path, 'blog_id': bid, 'blog_url': blog_url})
                self._save_accounts(); self._refresh_acc_table()
                self._log(f"✅ Blogspot 계정 업데이트: {name}"); return
        self._accounts.append({'type': 'blogspot', 'name': name, 'json_path': json_path, 'blog_id': bid, 'blog_url': blog_url})
        self._save_accounts(); self._refresh_acc_table()
        self._log(f"✅ Blogspot 계정 저장: {name}")

    def _save_nv_account(self):
        name = self._inp_nv_name.text().strip()
        naver_id = self._inp_nv_blog_id.text().strip()
        password = self._inp_nv_client_id.text().strip()
        if not name or not naver_id:
            self._log("⚠️ 계정명과 네이버 ID를 입력하세요"); return
        for acc in self._accounts:
            if acc.get('type') == 'naver' and acc.get('name') == name:
                acc.update({'naver_id': naver_id, 'password': password})
                self._save_accounts(); self._refresh_acc_table()
                self._log(f"✅ 네이버 계정 업데이트: {name}"); return
        self._accounts.append({'type': 'naver', 'name': name, 'naver_id': naver_id, 'password': password})
        self._save_accounts(); self._refresh_acc_table()
        self._log(f"✅ 네이버 계정 저장: {name}")

    def _test_naver_login(self):
        nv_id = self._inp_nv_blog_id.text().strip()
        pw = self._inp_nv_client_id.text().strip()
        if not nv_id or not pw:
            QMessageBox.warning(self, "테스트", "네이버 ID와 비밀번호를 입력하세요"); return
        self._log("🔌 네이버 로그인 테스트 중...")
        def _do():
            try:
                from selenium import webdriver
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.chrome.options import Options
                import time as _t
                opts = Options(); opts.add_argument("--headless"); opts.add_argument("--no-sandbox")
                driver = webdriver.Chrome(options=opts)
                wait = WebDriverWait(driver, 10)
                driver.get("https://nid.naver.com/nidlogin.login")
                wait.until(EC.presence_of_element_located((By.ID, "id")))
                driver.execute_script(f"document.getElementById('id').value='{nv_id}'")
                driver.execute_script(f"document.getElementById('pw').value='{pw}'")
                driver.find_element(By.ID, "log.login").click()
                _t.sleep(2)
                if "nid.naver.com" not in driver.current_url:
                    driver.quit()
                    return "✅ 네이버 로그인 성공"
                driver.quit()
                return "❌ 로그인 실패 — ID/PW 확인 또는 캡차/2단계 인증 해제 필요"
            except ImportError:
                return "⚠️ selenium 미설치: pip install selenium"
            except Exception as e:
                return f"❌ 오류: {e}"
        def _done(msg):
            self._log(msg)
            QMessageBox.information(self, "테스트 결과", msg)
        t = WorkerThread(_do); t.result_signal.connect(_done); t.start()

    def _test_wp_login(self):
        url = self._inp_wp_url.text().strip().rstrip('/')
        user = self._inp_wp_user.text().strip()
        pwd = self._inp_wp_pass.text().strip()
        if not url or not user or not pwd:
            QMessageBox.warning(self, "테스트", "URL, 사용자명, 앱 비밀번호를 입력하세요"); return
        self._log("🔌 WordPress 연결 테스트 중...")
        def _do():
            try:
                import base64
                creds = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers = {'Authorization': f'Basic {creds}'}
                resp = requests.get(f"{url}/wp-json/wp/v2/users/me", headers=headers, timeout=10)
                if resp.status_code == 200:
                    name = resp.json().get('name', user)
                    return f"✅ WordPress 연결 성공 — 사용자: {name}"
                return f"❌ 연결 실패 ({resp.status_code}) — URL/계정 확인 필요"
            except Exception as e:
                return f"❌ 오류: {e}"
        def _done(msg):
            self._log(msg)
            QMessageBox.information(self, "테스트 결과", msg)
        t = WorkerThread(_do); t.result_signal.connect(_done); t.start()

    def _save_yt_account(self):
        name = self._inp_yt_name.text().strip()
        channel = self._inp_yt_channel.text().strip()
        key = self._inp_yt_key.text().strip()
        if not name:
            self._log("⚠️ 계정명을 입력하세요"); return
        for acc in self._accounts:
            if acc.get('type') == 'youtube' and acc.get('name') == name:
                acc.update({'channel_id': channel, 'api_key': key})
                self._save_accounts(); self._refresh_acc_table()
                self._log(f"✅ YouTube 계정 업데이트: {name}"); return
        self._accounts.append({'type': 'youtube', 'name': name, 'channel_id': channel, 'api_key': key})
        self._save_accounts(); self._refresh_acc_table()
        self._log(f"✅ YouTube 계정 저장: {name}")

    def _save_th_account(self):
        name = self._inp_th_name.text().strip()
        uid = self._inp_th_uid.text().strip()
        token = self._inp_th_token.text().strip()
        if not name or not token:
            self._log("⚠️ 계정명과 액세스 토큰을 입력하세요"); return
        for acc in self._accounts:
            if acc.get('type') == 'threads' and acc.get('name') == name:
                acc.update({'uid': uid, 'token': token})
                self._save_accounts(); self._refresh_acc_table()
                self._log(f"✅ 스레드 계정 업데이트: {name}"); return
        self._accounts.append({'type': 'threads', 'name': name, 'uid': uid, 'token': token})
        self._save_accounts(); self._refresh_acc_table()
        self._log(f"✅ 스레드 계정 저장: {name}")

    def _save_ig_account(self):
        name = self._inp_ig_name.text().strip()
        uid = self._inp_ig_uid.text().strip()
        token = self._inp_ig_token.text().strip()
        if not name or not token:
            self._log("⚠️ 계정명과 액세스 토큰을 입력하세요"); return
        for acc in self._accounts:
            if acc.get('type') == 'instagram' and acc.get('name') == name:
                acc.update({'uid': uid, 'token': token})
                self._save_accounts(); self._refresh_acc_table()
                self._log(f"✅ 인스타그램 계정 업데이트: {name}"); return
        self._accounts.append({'type': 'instagram', 'name': name, 'uid': uid, 'token': token})
        self._save_accounts(); self._refresh_acc_table()
        self._log(f"✅ 인스타그램 계정 저장: {name}")

    def _del_account(self):
        row = self._acc_table.currentRow()
        if 0 <= row < len(self._accounts):
            name = self._accounts[row].get('name', '')
            self._accounts.pop(row)
            self._save_accounts(); self._refresh_acc_table()
            self._log(f"🗑️ 계정 삭제: {name}")

    def _edit_account(self, idx):
        if idx >= len(self._accounts): return
        acc = self._accounts[idx]
        t = acc.get('type', '')
        self._go_step(0)  # 설정 단계로 이동
        if t == 'wordpress':
            self._focus_acc_form('wordpress')
            self._inp_wp_name.setText(acc.get('name', ''))
            self._inp_wp_url.setText(acc.get('url', ''))
            self._inp_wp_user.setText(acc.get('user', ''))
            self._inp_wp_pass.setText(acc.get('pass', ''))
        elif t == 'blogspot':
            self._focus_acc_form('blogspot')
            self._inp_bs_name.setText(acc.get('name', ''))
            self._inp_bs_key.setText(acc.get('json_path', acc.get('api_key', '')))
            self._inp_bs_id.setText(acc.get('blog_id', ''))
            if hasattr(self, '_inp_bs_url'):
                self._inp_bs_url.setText(acc.get('blog_url', ''))
        elif t == 'naver':
            self._focus_acc_form('naver')
            self._inp_nv_name.setText(acc.get('name', ''))
            self._inp_nv_blog_id.setText(acc.get('naver_id', ''))
            self._inp_nv_client_id.setText(acc.get('password', ''))
        elif t == 'youtube':
            self._focus_acc_form('youtube')
            self._inp_yt_name.setText(acc.get('name', ''))
            self._inp_yt_channel.setText(acc.get('channel_id', ''))
            self._inp_yt_key.setText(acc.get('api_key', ''))
        elif t == 'threads':
            self._focus_acc_form('threads')
            self._inp_th_name.setText(acc.get('name', ''))
            self._inp_th_uid.setText(acc.get('uid', ''))
            self._inp_th_token.setText(acc.get('token', ''))
        elif t == 'instagram':
            self._focus_acc_form('instagram')
            self._inp_ig_name.setText(acc.get('name', ''))
            self._inp_ig_uid.setText(acc.get('uid', ''))
            self._inp_ig_token.setText(acc.get('token', ''))

    def _get_selected_wp_account(self):
        name = self._wp_acc_combo.currentText() if hasattr(self, '_wp_acc_combo') else ''
        for acc in self._accounts:
            if acc.get('type') == 'wordpress' and acc.get('name') == name:
                return acc
        return None

    def _get_selected_bs_account(self):
        name = self._bs_acc_combo.currentText() if hasattr(self, '_bs_acc_combo') else ''
        for acc in self._accounts:
            if acc.get('type') == 'blogspot' and acc.get('name') == name:
                return acc
        return None

    def _get_selected_nv_account(self):
        name = self._nv_acc_combo.currentText() if hasattr(self, '_nv_acc_combo') else ''
        for acc in self._accounts:
            if acc.get('type') == 'naver' and acc.get('name') == name:
                return acc
        return None

    def _upload_naver(self, result_edit, _cb=None, upload_mode="publish"):
        print('[MP-DEBUG] _upload_naver called')
        acc = self._get_selected_nv_account()
        if not acc:
            msg = "⚠️ 네이버 계정을 선택하세요"
            self._log(msg); (_cb(msg) if _cb else None); return
        content = result_edit.toPlainText().strip()
        if not content:
            msg = "⚠️ 업로드할 내용이 없습니다"
            self._log(msg); (_cb(msg) if _cb else None); return
        lines = content.split('\n', 1)
        title = lines[0].lstrip('#').strip() if lines else "제목 없음"
        body = lines[1].strip() if len(lines) > 1 else content
        naver_id = acc.get('naver_id', '')
        password = acc.get('password', '')
        if not naver_id or not password:
            self._log("⚠️ 네이버 ID/PW를 설정에서 입력하세요"); return

        # 저장된 카테고리 불러오기
        cat_key = f"naver_categories_{naver_id}"
        saved_cats = self.main.config.get(cat_key, [])

        # 카테고리 선택 다이얼로그
        print('[MP-DEBUG] creating dialog')
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton, QLineEdit
        dlg = QDialog(self)
        dlg.setWindowTitle("네이버 업로드 설정")
        dlg.setMinimumWidth(420)
        dlg.setStyleSheet("background:#1e1e1e; color:#fff;")
        dlg_v = QVBoxLayout(dlg); dlg_v.setSpacing(10); dlg_v.setContentsMargins(16,16,16,16)

        dlg_v.addWidget(QLabel(f"<b style='color:#03C75A;'>🇰🇷 네이버 블로그 업로드</b>"))
        dlg_v.addWidget(QLabel(f"계정: <b>{naver_id}</b>  |  제목: <b>{title[:40]}</b>"))

        # 카테고리 행
        cat_row = QHBoxLayout()
        cat_lbl = QLabel("카테고리:")
        cat_lbl.setStyleSheet("color:#ccc;")
        cat_row.addWidget(cat_lbl)
        cat_combo = QComboBox()
        cat_combo.setStyleSheet("color:#fff; background:#333; border:1px solid #555; padding:3px 6px;")
        cat_combo.addItem("(카테고리 없음)")
        for c in saved_cats:
            cat_combo.addItem(c)
        cat_combo.setEditable(True)
        cat_row.addWidget(cat_combo, 1)

        # 카테고리 새로 가져오기 버튼
        fetch_cat_btn = QPushButton("🔄 가져오기")
        fetch_cat_btn.setStyleSheet("background:#2d5a8e; color:#fff; padding:3px 10px; border-radius:4px;")
        cat_row.addWidget(fetch_cat_btn)
        dlg_v.addLayout(cat_row)
        cat_status = QLabel("")
        cat_status.setStyleSheet("color:#aaa; font-size:11px;")
        dlg_v.addWidget(cat_status)

        # 제목 수정
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("제목:"))
        title_edit = QLineEdit(title)
        title_edit.setStyleSheet("color:#fff; background:#2a2a2a; border:1px solid #555; padding:4px 8px;")
        title_row.addWidget(title_edit, 1)
        dlg_v.addLayout(title_row)

        # 발행 방법
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("발행:"))
        mode_combo = QComboBox()
        mode_combo.addItems(["🚀 바로 발행", "📝 임시저장"])
        mode_combo.setStyleSheet("color:#fff; background:#333; border:1px solid #555; padding:3px 6px;")
        if upload_mode == 'draft':
            mode_combo.setCurrentIndex(1)
        mode_row.addWidget(mode_combo, 1)
        dlg_v.addLayout(mode_row)

        # 버튼 행
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("🚀 업로드 시작")
        ok_btn.setStyleSheet("background:#03C75A; color:#fff; font-weight:bold; padding:8px 20px; border-radius:5px;")
        cancel_btn = QPushButton("취소")
        cancel_btn.setStyleSheet("background:#444; color:#ddd; padding:8px 16px; border-radius:5px;")
        btn_row.addWidget(ok_btn); btn_row.addWidget(cancel_btn)
        dlg_v.addLayout(btn_row)

        # 카테고리 가져오기 (브라우저 로그인 후 파싱)
        def _fetch_categories():
            cat_status.setText("⏳ 카테고리 가져오는 중... (브라우저 실행)")
            fetch_cat_btn.setEnabled(False)
            def _do():
                try:
                    import requests as _req
                    import re as _re
                    cats = []
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                        'Referer': 'https://blog.naver.com/',
                    }
                    # 방법1: Naver 카테고리 API (로그인 불필요)
                    try:
                        cat_url = f"https://blog.naver.com/NBlogCategoryListAjax.naver?blogId={naver_id}"
                        resp = _req.get(cat_url, headers=headers, timeout=10)
                        page_text = resp.text
                        if 'categoryName' in page_text:
                            raw_names = _re.findall(r'"categoryName"\s*:\s*"([^"]+)"', page_text)
                            for cn in raw_names:
                                try:
                                    decoded = cn.encode('latin-1').decode('unicode_escape').encode('latin-1').decode('utf-8')
                                except Exception:
                                    decoded = cn
                                decoded = decoded.strip()
                                if decoded and decoded not in cats:
                                    cats.append(decoded)
                    except Exception:
                        pass
                    # 방법2: 블로그 메인 스크래핑
                    if not cats:
                        try:
                            from bs4 import BeautifulSoup as _BS
                            resp2 = _req.get(f"https://blog.naver.com/{naver_id}", headers=headers, timeout=10)
                            soup = _BS(resp2.text, 'html.parser')
                            for sel in ['div.category a', 'ul.category_list li a', '#category a', 'a[href*="categoryNo"]']:
                                for el in soup.select(sel):
                                    n = _re.sub(r'\s*\(\d+\)\s*$', '', el.get_text().strip()).strip()
                                    if n and n not in ['전체보기', '분류 전체보기', '카테고리', ''] and n not in cats:
                                        cats.append(n)
                                if cats:
                                    break
                        except Exception:
                            pass
                    if cats:
                        return cats, f"✅ 카테고리 {len(cats)}개 가져옴"
                    return [], "⚠️ 카테고리를 찾을 수 없습니다 (블로그에 카테고리가 있는지 확인하거나 직접 입력하세요)"
                except Exception as e:
                    return [], f"❌ 오류: {e}"

            def _done(result):
                cats, msg = result
                fetch_cat_btn.setEnabled(True)
                cat_status.setText(msg)
                if cats:
                    cur = cat_combo.currentText()
                    cat_combo.clear()
                    cat_combo.addItem("(카테고리 없음)")
                    for c in cats: cat_combo.addItem(c)
                    idx = cat_combo.findText(cur)
                    if idx >= 0: cat_combo.setCurrentIndex(idx)
                    # 저장
                    self.main.config[cat_key] = cats
                    try:
                        import json
                        with open('config.json', 'w', encoding='utf-8') as f:
                            json.dump(self.main.config, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass

            t = WorkerThread(_do); t.result_signal.connect(_done); t.start()

        fetch_cat_btn.clicked.connect(_fetch_categories)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        print('[MP-DEBUG] calling dlg.exec()')
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if _cb: _cb("⏭ 업로드 취소됨")
            return

        final_title = title_edit.text().strip() or title
        category = cat_combo.currentText() if cat_combo.currentText() != "(카테고리 없음)" else ""
        is_draft = mode_combo.currentIndex() == 1

        self._log(f"🚀 네이버 업로드 시작: {final_title}" + (f" [{category}]" if category else ""))

        def _do_upload():
            try:
                import undetected_chromedriver as uc
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.common.keys import Keys
                import time as _t, pyperclip
                opts = uc.ChromeOptions()
                opts.add_argument("--start-maximized")
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--disable-gpu")
                opts.add_argument("--user-data-dir=./chrome_profile")
                driver = make_uc_driver(opts)
                wait = WebDriverWait(driver, 20)

                # 1. 로그인
                if not naver_login_with_fallback(driver, naver_id, password):
                    driver.quit()
                    return "❌ 로그인 실패 — ID/PW 확인 또는 2단계 인증 해제 필요"

                # 2. 글쓰기 페이지
                driver.get(f"https://blog.naver.com/{naver_id}/postwrite")
                _t.sleep(4)
                try:
                    driver.switch_to.frame("mainFrame")
                except Exception:
                    pass
                _t.sleep(1)

                # 3. 제목 입력
                try:
                    title_el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
                        ".se-title-text, input[placeholder*='제목'], .tit_area input")))
                    title_el.click(); _t.sleep(0.3)
                    title_el.send_keys(Keys.CONTROL + 'a')
                    title_el.send_keys(final_title)
                except Exception:
                    try:
                        title_el = driver.find_element(By.CSS_SELECTOR, ".se-title-text")
                        driver.execute_script("arguments[0].innerText=arguments[1]", title_el, final_title)
                    except Exception:
                        pass

                _t.sleep(0.5)

                # 4. 본문 클릭 후 붙여넣기 (send_keys는 느리므로 clipboard 활용)
                try:
                    body_el = driver.find_element(By.CSS_SELECTOR,
                        ".se-content .se-paragraph, .se-content, #smartEditor .se-main-container")
                    body_el.click(); _t.sleep(0.5)
                    # 클립보드로 붙여넣기 (빠름)
                    try:
                        pyperclip.copy(body)
                        body_el.send_keys(Keys.CONTROL + 'v')
                    except Exception:
                        body_el.send_keys(body[:3000])
                except Exception:
                    pass

                _t.sleep(0.5)

                # 5. 카테고리 선택
                if category:
                    try:
                        cat_btn = driver.find_element(By.CSS_SELECTOR,
                            ".se-category-btn, .category_select, button[class*='category']")
                        cat_btn.click(); _t.sleep(1)
                        cat_items = driver.find_elements(By.CSS_SELECTOR,
                            ".category_list li, .se-category-list li, .category-popup li")
                        for ci in cat_items:
                            if category in ci.text:
                                ci.click(); _t.sleep(0.3); break
                    except Exception:
                        pass

                mode_label = "임시저장" if is_draft else "발행"
                return f"✅ 네이버 블로그 작성 완료 [{mode_label}] — 브라우저에서 {mode_label} 버튼을 눌러주세요: {final_title}"

            except ImportError:
                return "⚠️ undetected-chromedriver 미설치 — pip install undetected-chromedriver pyperclip"
            except Exception as e:
                return f"❌ 네이버 업로드 오류: {e}"

        def _upload_done(msg):
            self._log(msg)
            if _cb: _cb(msg)

        t = WorkerThread(_do_upload); t.result_signal.connect(_upload_done); t.start()

    def _run_transform(self):
        # URL/사이트가 입력되어 있으면 먼저 가져오기
        url = (self._mp_url_input.text().strip() if hasattr(self, '_mp_url_input') else '') or \
              (self._mp_site_input.text().strip() if hasattr(self, '_mp_site_input') else '')
        content = self.source_input.toPlainText().strip()
        if not content and url:
            self._log("🔗 URL에서 글 가져오는 중...")
            self._mp_fetch_url()
            content = self.source_input.toPlainText().strip()
        if not content:
            self._log("⚠️ 원본 글 또는 URL을 입력하세요"); return

        selected = [k for k, cb in self._platform_checks.items() if cb.isChecked()]
        if not selected:
            self._log("⚠️ 변환할 플랫폼을 하나 이상 선택하세요"); return

        model_txt = self.ai_model.currentText()
        if "Perplexity" in model_txt:
            api_key = self.main.config.get('perplex_key', '').strip()
            model_id = 'sonar-pro'
        elif "gemini" in model_txt:
            api_key = self.main.config.get('gemini_key', '').strip()
            model_id = 'gemini-2.5-flash'
        else:
            api_key = self.main.config.get('openai_key', '').strip()
            model_id = 'gpt-4o'

        if not api_key:
            self._log("⚠️ API 키 없음 — 설정에서 입력해주세요"); return

        self.transform_btn.setEnabled(False)
        self.transform_btn.setText("⏳ 변환 중...")
        self.stop_btn.setEnabled(True)
        self.stop_btn.setText("⏹ 중지")
        self._pending = list(selected)
        self._content = content
        self._api_key = api_key
        self._model_id = model_id
        self._model_txt = model_txt
        self._transform_total = len(selected)
        self._transform_done = 0

        # 모든 선택 플랫폼 탭 상태를 "대기"로 초기화
        keys = [k for k, _ in self.PLATFORMS]
        labels = dict(self.PLATFORMS)
        for k in keys:
            tab_idx = keys.index(k)
            if k in selected:
                self.result_tabs.setTabText(tab_idx, f"🕐 {labels[k]}")
                if k in self._result_widgets:
                    self._result_widgets[k].setPlaceholderText("⏳ 변환 대기 중...")
            else:
                self.result_tabs.setTabText(tab_idx, labels[k])

        self._go_step(1)
        self._run_next_platform()

    def _run_next_platform(self):
        if not self._pending:
            total = self._transform_total
            done = self._transform_done
            self._log(f"✅ 변환 완료 ({done}/{total}) — 업로드 단계로 이동하려면 '다음 단계' 버튼을 누르세요")
            self.transform_btn.setEnabled(True)
            self.transform_btn.setText("🤖 AI 변환 시작")
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("⏹ 중지")
            self._update_step4_previews()
            self._save_state()
            return

        key = self._pending.pop(0)
        label = dict(self.PLATFORMS)[key]
        base_prompt = self.PLATFORM_PROMPTS[key].replace('{content}', self._content)
        if self._mp_active_prompt:
            prompt = self._mp_active_prompt.replace('{content}', self._content) + \
                     "\n\n[플랫폼 지침]\n" + base_prompt
        else:
            prompt = base_prompt

        # 현재 플랫폼 탭 "변환 중" 표시
        keys = [k for k, _ in self.PLATFORMS]
        if key in keys and hasattr(self, 'result_tabs'):
            tab_idx = keys.index(key)
            remaining = len(self._pending)
            done = self._transform_total - remaining - 1
            self.result_tabs.setTabText(tab_idx, f"⏳ {label}")
            self.result_tabs.setCurrentIndex(tab_idx)
            self.transform_btn.setText(f"⏳ {done+1}/{self._transform_total} 변환 중...")

        self._log(f"🔄 [{done+1}/{self._transform_total}] {label} 변환 중...")

        if "Perplexity" in self._model_txt:
            self._worker = WorkerThread(call_perplexity, self._api_key, prompt, self._model_id)
        elif "gemini" in self._model_txt:
            self._worker = WorkerThread(call_gemini_text, self._api_key, prompt, self._model_id)
        else:
            self._worker = WorkerThread(call_openai_text, self._api_key, prompt, self._model_id)

        self._worker.result_signal.connect(lambda r, k=key, lb=label: self._on_result(k, lb, r))
        self._worker.finished_signal.connect(self._run_next_platform)
        self._worker.start()

    def _on_result(self, key, label, result):
        keys = [k for k, _ in self.PLATFORMS]
        tab_idx = keys.index(key) if key in keys else -1
        if result and not str(result).startswith('❌'):
            self._result_widgets[key].setPlainText(str(result))
            self._results[key] = str(result)
            self._transform_done += 1
            self._log(f"✅ [{self._transform_done}/{self._transform_total}] {label} 완료 ({len(str(result))}자)")
            if tab_idx >= 0:
                self.result_tabs.setTabText(tab_idx, f"✅ {label}")
            self._update_step4_previews()
            if hasattr(self, '_s3_gen_list'):
                self._s3_sync_gen_list()
        else:
            self._log(f"❌ {label} 실패: {result}")
            if tab_idx >= 0:
                self.result_tabs.setTabText(tab_idx, f"❌ {label}")

    def _stop_transform(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.quit()
            self._worker.wait(2000)
            if self._worker.isRunning():
                self._worker.terminate()
        # 대기/진행 중이던 탭 상태 원복
        keys = [k for k, _ in self.PLATFORMS]
        labels = dict(self.PLATFORMS)
        for k in self._pending:
            if k in keys:
                self.result_tabs.setTabText(keys.index(k), labels[k])
        self._pending = []
        self.transform_btn.setEnabled(True)
        self.transform_btn.setText("🤖 AI 변환 시작")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("⏹ 중지")
        self._log("⏹ 변환 중지됨 (완료된 항목은 결과에 남아있습니다)")

    def _upload_wordpress(self, editor, _cb=None, upload_mode="publish"):
        text = editor.toPlainText().strip()
        if not text: self._log("⚠️ 변환 결과가 없습니다"); return
        acc = self._get_selected_wp_account()
        if not acc:
            self._log("⚠️ ⚙️ 설정 탭에서 WordPress 계정을 먼저 추가하고 선택하세요"); return
        url = acc['url'].rstrip('/')
        user = acc.get('user', '')
        pwd = acc.get('pass', '')
        name = acc.get('name', '')
        # upload_mode: draft=임시저장, publish=발행 (scheduled은 별도 날짜 필요, 여기선 draft 처리)
        wp_status = 'draft' if upload_mode in ('draft', 'scheduled') else 'publish'
        mode_label = {'draft': '임시저장', 'scheduled': '임시저장(예약)', 'publish': '발행'}.get(upload_mode, '발행')

        def _do():
            import base64
            creds = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            headers = {'Authorization': f'Basic {creds}', 'Content-Type': 'application/json'}
            lines = text.split('\n')
            post_title = lines[0].lstrip('#').strip() if lines else 'AI 변환 글'
            body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else text
            resp = requests.post(f"{url}/wp-json/wp/v2/posts",
                json={'title': post_title, 'content': body, 'status': wp_status},
                headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                return f"✅ [{name}] WordPress {mode_label} 완료\n{resp.json().get('link', '')}"
            return f"❌ 업로드 실패 ({resp.status_code}): {resp.text[:200]}"

        self._log(f"🌐 [{name}] WordPress {mode_label} 업로드 중...")
        def _wp_done(msg):
            self._log(msg)
            if _cb: _cb(msg)
        t = WorkerThread(_do); t.result_signal.connect(_wp_done); t.start()

    def _upload_blogspot(self, editor, _cb=None, upload_mode="publish"):
        text = editor.toPlainText().strip()
        if not text: self._log("⚠️ 변환 결과가 없습니다"); return
        acc = self._get_selected_bs_account()
        if not acc:
            self._log("⚠️ ⚙️ 설정 탭에서 Blogspot 계정을 먼저 추가하고 선택하세요"); return
        json_path = acc.get('json_path', acc.get('api_key', ''))
        blog_id = acc.get('blog_id', '')
        name = acc.get('name', '')
        if not json_path or not os.path.exists(json_path):
            self._log("⚠️ client_secret.json 파일 경로를 설정에서 확인하세요"); return
        is_draft = upload_mode in ('draft', 'scheduled')
        mode_label = {'draft': '임시저장', 'scheduled': '임시저장(예약)', 'publish': '발행'}.get(upload_mode, '발행')

        def _do():
            try:
                import pickle
                from googleapiclient.discovery import build
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request as GRequest
                SCOPES = ['https://www.googleapis.com/auth/blogger']
                token_file = json_path.replace('.json', '_token.pickle')
                creds = None
                if os.path.exists(token_file):
                    with open(token_file, 'rb') as f:
                        creds = pickle.load(f)
                if not creds or not creds.valid:
                    if creds and creds.expired and creds.refresh_token:
                        creds.refresh(GRequest())
                    else:
                        flow = InstalledAppFlow.from_client_secrets_file(json_path, SCOPES)
                        creds = flow.run_local_server(port=0)
                    with open(token_file, 'wb') as f:
                        pickle.dump(creds, f)
                service = build('blogger', 'v3', credentials=creds)
                lines = text.split('\n')
                post_title = lines[0].lstrip('#').strip() if lines else 'AI 변환 글'
                body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else text
                data = {'title': post_title, 'content': body, 'blog': {'id': blog_id}}
                res = service.posts().insert(blogId=blog_id, body=data, isDraft=is_draft).execute()
                return f"✅ [{name}] Blogspot {mode_label} 완료: {res.get('url', '')}"
            except ImportError:
                return "⚠️ 패키지 필요 — 터미널: pip install google-api-python-client google-auth-oauthlib"
            except Exception as e:
                return f"❌ 업로드 실패: {e}"

        self._log(f"📝 [{name}] Blogspot {mode_label} 중... (브라우저 인증창이 뜰 수 있어요)")
        def _bs_done(msg):
            self._log(msg)
            if _cb: _cb(msg)
        t = WorkerThread(_do); t.result_signal.connect(_bs_done); t.start()

    def _copy_result(self, editor):
        text = editor.toPlainText()
        if not text: return
        try:
            import pyperclip; pyperclip.copy(text)
        except Exception:
            QApplication.clipboard().setText(text)
        self._log("📋 클립보드에 복사됨")

    def _save_result(self, key, editor):
        text = editor.toPlainText()
        if not text: return
        path, _ = QFileDialog.getSaveFileName(
            self, f"{key} 저장", f"{key}_result.txt", "텍스트 (*.txt);;모든 파일 (*.*)")
        if path:
            with open(path, 'w', encoding='utf-8') as f: f.write(text)
            self._log(f"💾 저장: {path}")

    def _on_result_edited(self, key, editor):
        """결과 에디터가 수동으로 수정될 때 _results 동기화 및 자동 저장"""
        text = editor.toPlainText()
        self._results[key] = text
        self._save_state()

    def _mp_acc_url_changed(self, idx):
        if idx <= 0: return
        combo = self._mp_acc_url_combo
        item_text = combo.currentText()
        # "이름 (플랫폼)" 형식에서 이름만 추출
        acc_name = item_text.split(' (')[0]
        for acc in self._accounts:
            if acc.get('name', '') != acc_name: continue
            t = acc.get('type', '')
            if t == 'naver':
                url = f"https://blog.naver.com/{acc.get('naver_id', '')}"
            elif t == 'blogspot':
                url = acc.get('blog_url', '').strip()
                if not url:
                    self._log("💡 블로그스팟: 계정 편집에서 '블로그 URL' 항목에 실제 주소(예: https://myblog.blogspot.com)를 저장하세요")
            elif t == 'wordpress':
                url = acc.get('url', '')
            else:
                url = ''
            if url:
                if hasattr(self, '_mp_url_input'):
                    self._mp_url_input.setText(url)
                if hasattr(self, '_mp_site_input'):
                    self._mp_site_input.setText(url)
            break

    def _mp_fetch_url(self):
        url = self._mp_site_input.text().strip() if hasattr(self, '_mp_site_input') else self._mp_url_input.text().strip() if hasattr(self, '_mp_url_input') else ''
        if not url:
            self._log("⚠️ URL을 입력하세요"); return
        count = self._mp_url_count.value()
        self._log(f"🔗 URL 크롤링 중... ({url})")
        try:
            import urllib.request
            from urllib.parse import urlparse, urljoin
            from bs4 import BeautifulSoup
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
            }

            # 외부/불필요 도메인 필터 (구글 계정, 도움말 등)
            SKIP_DOMAINS = [
                'accounts.google.com', 'support.google.com', 'policies.google.com',
                'google.com/intl', 'myaccount.google.com', 'mail.google.com',
                'fonts.googleapis.com', 'schemas.google.com',
            ]
            SKIP_KEYWORDS = [
                'Sign in', 'Google Account', 'Privacy Policy', 'Terms of Service',
                'Help Center', 'google-site-verification'
            ]

            def _is_login_page(html):
                """구글 로그인/리다이렉트 페이지인지 감지"""
                checks = ['accounts.google.com', 'ServiceLogin', 'signin/v2', 'goog_ppa_']
                return any(c in html[:3000] for c in checks)

            def _fetch_html(u):
                req = urllib.request.Request(u, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    final_url = r.geturl()
                    html = r.read().decode('utf-8', errors='ignore')
                    return html, final_url

            def _extract(html, u):
                if _is_login_page(html):
                    return None, None  # 로그인 페이지 → 건너뜀

                soup = BeautifulSoup(html, 'html.parser')
                # 불필요 태그 제거
                for tag in soup.select('script, style, nav, header, footer, .ad, .advertisement, .sidebar, iframe, noscript'):
                    tag.decompose()

                # 제목 추출
                title = ''
                for sel in ['h1.se-title', '.se-title-text', 'h1.title', '.article-header h1', 'h1', 'title']:
                    el = soup.select_one(sel)
                    if el:
                        t = el.get_text(strip=True)
                        # 구글 관련 제목 필터
                        if any(kw in t for kw in SKIP_KEYWORDS):
                            return None, None
                        title = t; break
                if not title:
                    title = soup.title.string.strip() if soup.title else '(제목 없음)'

                text = ''
                if 'blog.naver.com' in u or 'm.blog.naver.com' in u:
                    import re
                    log_no = re.search(r'logNo=(\d+)', u) or re.search(r'/(\d+)$', u)
                    if log_no:
                        blog_id_match = re.search(r'blog\.naver\.com/([^/?]+)', u)
                        if blog_id_match:
                            mobile_url = f"https://m.blog.naver.com/{blog_id_match.group(1)}/{log_no.group(1)}"
                            try:
                                html2, _ = _fetch_html(mobile_url)
                                soup2 = BeautifulSoup(html2, 'html.parser')
                                for tag in soup2.select('script, style, nav, header, footer'):
                                    tag.decompose()
                                for sel in ['.se-main-container', '.post_ct', '#postViewArea']:
                                    el = soup2.select_one(sel)
                                    if el:
                                        text = el.get_text('\n', strip=True); break
                            except Exception:
                                pass
                    if not text:
                        for sel in ['.se-main-container', '#postViewArea', '.post-view', '.post_content']:
                            el = soup.select_one(sel)
                            if el:
                                text = el.get_text('\n', strip=True); break
                elif 'tistory.com' in u:
                    for sel in ['.entry-content', '.article-view', '#article-view', '.contents_style']:
                        el = soup.select_one(sel)
                        if el:
                            text = el.get_text('\n', strip=True); break
                elif 'blogger.com' in u or 'blogspot.com' in u:
                    for sel in ['.post-body', '.entry-content', 'article .post-body', 'article']:
                        el = soup.select_one(sel)
                        if el:
                            text = el.get_text('\n', strip=True); break
                elif 'wordpress' in u or 'wp-content' in html:
                    for sel in ['.entry-content', '.post-content', 'article .content', 'article']:
                        el = soup.select_one(sel)
                        if el:
                            text = el.get_text('\n', strip=True); break
                if not text:
                    for sel in ['article', 'main', '.content', '.post']:
                        el = soup.select_one(sel)
                        if el and len(el.get_text(strip=True)) > 100:
                            text = el.get_text('\n', strip=True); break
                if not text:
                    text = soup.get_text('\n', strip=True)

                lines = [l.strip() for l in text.split('\n') if l.strip()]
                text = '\n'.join(lines)[:8000]
                if len(text) < 80:  # 내용이 너무 짧으면 실패
                    return None, None
                return title, text

            def _is_post_link(href, base_url):
                """실제 블로그 포스트 링크인지 판별"""
                if not href or not href.startswith('http'):
                    return False
                parsed = urlparse(href)
                base_parsed = urlparse(base_url)
                # 외부 도메인 제외 (같은 도메인만)
                if parsed.netloc != base_parsed.netloc:
                    return False
                # 구글 관련 도메인 제외
                if any(skip in href for skip in SKIP_DOMAINS):
                    return False
                # 앵커만 있는 링크 제외
                if not parsed.path or parsed.path == base_parsed.path:
                    return False
                # 네이버 블로그 포스트 패턴
                if 'blog.naver.com' in href:
                    import re
                    return bool(re.search(r'/\d{5,}', href))
                # 블로그스팟 포스트 패턴 (연도/월/제목.html)
                if 'blogspot.com' in href or 'blogger.com' in href:
                    import re
                    return bool(re.search(r'/\d{4}/\d{2}/', href))
                # 일반적으로 짧은 경로는 카테고리일 가능성
                path_depth = len([p for p in parsed.path.split('/') if p])
                return path_depth >= 2

            def _fetch_naver_rss(blog_id, max_count):
                """네이버 블로그 RSS로 최근 포스트 목록+내용 가져오기"""
                import re, xml.etree.ElementTree as ET
                rss_url = f"https://rss.blog.naver.com/{blog_id}.xml"
                try:
                    html_rss, _ = _fetch_html(rss_url)
                    root = ET.fromstring(html_rss)
                    ns = {'atom': 'http://www.w3.org/2005/Atom'}
                    items = root.findall('.//item') or root.findall('.//atom:entry', ns)
                    result = []
                    for item in items[:max_count * 2]:
                        title_el = item.find('title')
                        link_el = item.find('link')
                        t = title_el.text.strip() if title_el is not None and title_el.text else ''
                        link = link_el.text.strip() if link_el is not None and link_el.text else ''
                        if not link: continue
                        # 실제 글 내용 가져오기 (모바일 URL)
                        m = re.search(r'blog\.naver\.com/([^/?]+)/(\d+)', link)
                        if m:
                            blog_uid, log_no = m.group(1), m.group(2)
                            mob_url = f"https://m.blog.naver.com/{blog_uid}/{log_no}"
                            try:
                                h2, _ = _fetch_html(mob_url)
                                soup2 = BeautifulSoup(h2, 'html.parser')
                                for tag in soup2.select('script, style, nav, header, footer'):
                                    tag.decompose()
                                text = ''
                                for sel in ['.se-main-container', '.post_ct', '#postViewArea']:
                                    el = soup2.select_one(sel)
                                    if el:
                                        text = el.get_text('\n', strip=True); break
                                if text and len(text) > 80:
                                    lines = [l.strip() for l in text.split('\n') if l.strip()]
                                    result.append((t or '(제목 없음)', '\n'.join(lines)[:8000], link))
                                    if len(result) >= max_count: break
                            except Exception:
                                pass
                    return result
                except Exception:
                    return []

            # 여러 글 크롤링
            posts = []
            # 네이버 블로그 → RSS 우선 시도
            import re as _re
            naver_match = _re.search(r'blog\.naver\.com/([^/?&#]+)', url)
            if naver_match and count > 1:
                blog_uid = naver_match.group(1)
                self._log(f"📡 네이버 RSS 피드로 목록 가져오는 중... ({blog_uid})")
                posts = _fetch_naver_rss(blog_uid, count)
                if posts:
                    self._log(f"✅ RSS로 {len(posts)}개 가져옴")

            if not posts:
                if count == 1:
                    html, final_url = _fetch_html(url)
                    if _is_login_page(html):
                        self._log("⚠️ 로그인이 필요한 페이지입니다. 공개 블로그 URL을 사용하거나 블로그를 공개로 설정하세요")
                        return
                    title, text = _extract(html, final_url)
                    if title and text:
                        posts.append((title, text, final_url))
                else:
                    html, _ = _fetch_html(url)
                    if _is_login_page(html):
                        self._log("⚠️ 로그인이 필요한 페이지입니다. 공개 블로그 URL을 사용하거나 블로그를 공개로 설정하세요")
                        return
                    soup = BeautifulSoup(html, 'html.parser')
                    # 포스트 링크만 추출 (필터링 강화)
                    links = []
                    seen = set()
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if not href.startswith('http'):
                            href = urljoin(url, href)
                        if href not in seen and _is_post_link(href, url):
                            seen.add(href)
                            links.append(href)
                    targets = links[:count * 2]  # 여유분 확보
                    for link in targets:
                        if len(posts) >= count:
                            break
                        try:
                            h, fu = _fetch_html(link)
                            t, txt = _extract(h, fu)
                            if t and txt:
                                posts.append((t, txt, link))
                        except Exception:
                            pass
                    if not posts:
                        t, txt = _extract(html, url)
                        if t and txt:
                            posts.append((t, txt, url))

            self._mp_fetched_posts = posts
            self._mp_post_list.clear()
            for t, txt, u in posts:
                preview = txt[:60].replace('\n', ' ')
                item = QListWidgetItem(f"📄 {t}\n    {preview}...")
                item.setToolTip(txt[:300])
                item.setData(Qt.ItemDataRole.UserRole, (t, txt, u))
                self._mp_post_list.addItem(item)
            if posts:
                self.source_input.setPlainText(posts[0][1])
                self._mp_post_list.setCurrentRow(0)
                if hasattr(self, '_url_result_label'):
                    t0, txt0, _ = posts[0]
                    self._url_result_label.setText(f"✅ 가져옴: {t0}\n({len(txt0)}자)")
                if hasattr(self, '_s3_src_list'):
                    self._s3_sync_src_list()
                self._log(f"✅ {len(posts)}개 글 가져오기 완료")
            else:
                self._log("⚠️ 글을 가져오지 못했습니다. 블로그가 공개 상태인지 확인하세요")
        except Exception as e:
            self._log(f"❌ URL 가져오기 실패: {e}")

    def _mp_post_select(self, item):
        row = self._mp_post_list.row(item)
        if 0 <= row < len(self._mp_fetched_posts):
            self.source_input.setPlainText(self._mp_fetched_posts[row][1])

    def _mp_post_preview(self, item):
        row = self._mp_post_list.row(item)
        if 0 <= row < len(self._mp_fetched_posts):
            title, text, url = self._mp_fetched_posts[row]
            dlg = QDialog(self)
            dlg.setWindowTitle(title)
            dlg.resize(600, 500)
            v = QVBoxLayout(dlg)
            lbl = QLabel(f"<b>{title}</b><br><small>{url}</small>")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#FFD700; padding:4px;")
            v.addWidget(lbl)
            te = QTextEdit(); te.setReadOnly(True)
            te.setPlainText(text)
            te.setStyleSheet("background:#1e1e1e; color:#ddd;")
            v.addWidget(te)
            btn_row = QHBoxLayout()
            use_btn = QPushButton("✅ 이 글 사용")
            use_btn.clicked.connect(lambda: (self.source_input.setPlainText(text), dlg.accept()))
            btn_row.addWidget(use_btn)
            close_btn = QPushButton("닫기")
            close_btn.clicked.connect(dlg.reject)
            btn_row.addWidget(close_btn)
            v.addLayout(btn_row)
            dlg.exec()

    def _mp_open_prompt_library(self):
        dlg = PromptLibraryDialog(current_text=self._mp_active_prompt, parent=self)
        self._mp_lib_btn.setText(f"🗂 프롬프트 관리 / 변경    총 {len(dlg._library)}개")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text = dlg.get_selected_text()
            title = dlg.get_selected_title()
            if text is not None:
                self._mp_active_prompt = text
                self._mp_card_title.setText(title if title else "(제목 없음)")
                preview = text[:80].replace('\n', ' ')
                self._mp_card_preview.setText(preview + ('...' if len(text) > 80 else ''))
                if not self._mp_card_badge_shown:
                    self._mp_card_title_row.addWidget(self._mp_card_badge)
                    self._mp_card_badge_shown = True
        self._mp_lib_btn.setText(f"🗂 프롬프트 관리 / 변경    총 {len(dlg._library)}개")

    def _load_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "파일 불러오기", "", "텍스트 (*.txt);;모든 파일 (*.*)")
        if path:
            try:
                with open(path, encoding='utf-8') as f:
                    self.source_input.setPlainText(f.read())
                fname = os.path.basename(path)
                if hasattr(self, '_file_label'):
                    self._file_label.setText(f"✅ {fname}")
                self._log(f"📂 불러옴: {path}")
            except Exception as e:
                self._log(f"❌ 파일 읽기 실패: {e}")

    def _open_url(self, url):
        import webbrowser; webbrowser.open(url)

    def _log(self, msg):
        from datetime import datetime
        self.log_panel.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        try: self.main.log(msg)
        except Exception: pass

    def _save_state(self):
        try:
            state = {
                'ai_model': self.ai_model.currentIndex(),
                'results': self._results,
                'checks': {k: cb.isChecked() for k, cb in self._platform_checks.items()},
                'wp_sel': self._wp_acc_combo.currentText() if hasattr(self, '_wp_acc_combo') else '',
                'bs_sel': self._bs_acc_combo.currentText() if hasattr(self, '_bs_acc_combo') else '',
                'mp_prompt': self._mp_active_prompt,
                'mp_card_title': self._mp_card_title.text() if hasattr(self, '_mp_card_title') else '',
                'source_text': self.source_input.toPlainText() if hasattr(self, 'source_input') else '',
                'fetched_posts': [(t, x, u) for t, x, u in getattr(self, '_mp_fetched_posts', [])],
            }
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            if not os.path.exists(self.STATE_FILE): return
            with open(self.STATE_FILE, encoding='utf-8') as f:
                st = json.load(f)
            self.ai_model.setCurrentIndex(st.get('ai_model', 0))
            for k, v in st.get('results', {}).items():
                if k in self._result_widgets and v:
                    self._result_widgets[k].setPlainText(v)
                    self._results[k] = v
            for k, v in st.get('checks', {}).items():
                if k in self._platform_checks:
                    self._platform_checks[k].setChecked(v)
            # 계정 콤보 선택 복원
            if hasattr(self, '_wp_acc_combo') and st.get('wp_sel'):
                idx = self._wp_acc_combo.findText(st['wp_sel'])
                if idx >= 0: self._wp_acc_combo.setCurrentIndex(idx)
            if hasattr(self, '_bs_acc_combo') and st.get('bs_sel'):
                idx = self._bs_acc_combo.findText(st['bs_sel'])
                if idx >= 0: self._bs_acc_combo.setCurrentIndex(idx)
            # 프롬프트 카드 복원
            saved_prompt = st.get('mp_prompt', '')
            saved_title = st.get('mp_card_title', '')
            if saved_prompt and hasattr(self, '_mp_card_title'):
                self._mp_active_prompt = saved_prompt
                self._mp_card_title.setText(saved_title if saved_title else "(제목 없음)")
                preview = saved_prompt[:80].replace('\n', ' ')
                self._mp_card_preview.setText(preview + ('...' if len(saved_prompt) > 80 else ''))
                if not self._mp_card_badge_shown:
                    self._mp_card_title_row.addWidget(self._mp_card_badge)
                    self._mp_card_badge_shown = True
            # 원본 입력 및 가져온 포스트 복원
            src = st.get('source_text', '')
            if src and hasattr(self, 'source_input'):
                self.source_input.setPlainText(src)
            posts = st.get('fetched_posts', [])
            if posts:
                self._mp_fetched_posts = [(t, x, u) for t, x, u in posts]
                if hasattr(self, '_mp_post_list'):
                    self._mp_post_list.clear()
                    for title, text, url in self._mp_fetched_posts:
                        from PyQt6.QtWidgets import QListWidgetItem as _QLWI
                        item = _QLWI(f"📄 {title}")
                        item.setData(0x0100, (title, text, url))
                        self._mp_post_list.addItem(item)
                if hasattr(self, '_s3_src_list'):
                    self._s3_sync_src_list()
            # 복원된 결과로 STEP3 미리보기 및 탭 표시 갱신
            keys = [k for k, _ in self.PLATFORMS]
            for k, v in st.get('results', {}).items():
                if v and k in keys:
                    idx = keys.index(k)
                    label = dict(self.PLATFORMS).get(k, k)
                    if hasattr(self, 'result_tabs'):
                        self.result_tabs.setTabText(idx, f"✅ {label}")
            if any(st.get('results', {}).values()):
                self._update_step4_previews()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  대행/후기성 Wizard (10단계 단계별 진행)
#  - 스크린샷 스타일: 상단 stepper / 좌측 단계UI+글감테이블 / 우측 로그 / 하단 nav
#  - 다수 글감 일괄 처리 + 미리보기 + 실시간 진행상태
# ═══════════════════════════════════════════════════════════════
class AgencyWizardPage(_AgencyStateMixin, QWidget):
    STEPS = [
        ("소스 선택",    "source"),
        ("추출",         "extract"),
        ("블로그",       "blog"),
        ("AI",           "ai"),
        ("프롬프트",     "prompt"),
        ("포스팅 설정",  "post_config"),
        ("이미지",       "image"),
        ("스타일",       "style"),
        ("백링크",       "backlink"),
        ("포스팅",       "posting"),
    ]
    STATE_FILE = 'agency_state/wizard.json'

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self.current_step = 0
        self.items = []     # [{title, text, url, status, image_count, image_paths, rewritten, ...}]
        self.config = {}    # 단계별 설정 모음
        self._stop_requested = False
        self._build_ui()
        self._render_stepper()
        self._render_step()
        # 자동 복원 — 직접 호출 (훅/타이머 연결 전에 호출해야 재진입 없음)
        try:
            self._state_load()
        except Exception:
            pass
        # 주기적 자동 저장 (10초마다)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._state_save)
        self._autosave_timer.start(10000)

    def _serialize_state(self):
        # 입력 위젯 값들도 함께 저장 (단계 전환 없이 입력만 한 경우 대비)
        try:
            extra = {
                'src_type_idx': self.src_type.currentIndex() if hasattr(self, 'src_type') else 0,
                'src_input': self.src_input.toPlainText() if hasattr(self, 'src_input') else '',
                'blog_kw': self.blog_kw.text() if hasattr(self, 'blog_kw') else '',
                'ai_model_idx': self.ai_model_combo.currentIndex() if hasattr(self, 'ai_model_combo') else 0,
                'ai_chars': self.ai_chars.value() if hasattr(self, 'ai_chars') else 1500,
                'ai_prompt': self.ai_prompt.toPlainText() if hasattr(self, 'ai_prompt') else '',
                'cfg_save_mode_idx': self.cfg_save_mode.currentIndex() if hasattr(self, 'cfg_save_mode') else 0,
                'cfg_interval': self.cfg_interval.value() if hasattr(self, 'cfg_interval') else 60,
                'img_count': self.img_count.value() if hasattr(self, 'img_count') else 3,
                'img_size_idx': self.img_size.currentIndex() if hasattr(self, 'img_size') else 0,
                'img_prompt': self.img_prompt.toPlainText() if hasattr(self, 'img_prompt') else '',
                'style_align_idx': self.style_align.currentIndex() if hasattr(self, 'style_align') else 0,
                'style_size': self.style_size.value() if hasattr(self, 'style_size') else 15,
                'style_quote': self.style_quote.isChecked() if hasattr(self, 'style_quote') else True,
                'style_bold': self.style_bold.isChecked() if hasattr(self, 'style_bold') else True,
                'style_hashtag': self.style_hashtag.isChecked() if hasattr(self, 'style_hashtag') else True,
                'bl_input': self.bl_input.toPlainText() if hasattr(self, 'bl_input') else '',
            }
        except Exception:
            extra = {}
        return {
            'current_step': self.current_step,
            'items': self.items,
            'config': self.config,
            'inputs': extra,
        }

    def _deserialize_state(self, state):
        self.current_step = state.get('current_step', 0)
        self.items = state.get('items', [])
        self.config = state.get('config', {})

        # 입력 위젯 복원
        inp = state.get('inputs', {})
        try:
            if hasattr(self, 'src_type'): self.src_type.setCurrentIndex(inp.get('src_type_idx', 0))
            if hasattr(self, 'src_input'): self.src_input.setPlainText(inp.get('src_input', ''))
            if hasattr(self, 'blog_kw'): self.blog_kw.setText(inp.get('blog_kw', ''))
            if hasattr(self, 'ai_model_combo'): self.ai_model_combo.setCurrentIndex(inp.get('ai_model_idx', 0))
            if hasattr(self, 'ai_chars'): self.ai_chars.setValue(inp.get('ai_chars', 1500))
            if hasattr(self, 'ai_prompt'): self.ai_prompt.setPlainText(inp.get('ai_prompt', ''))
            if hasattr(self, 'cfg_save_mode'): self.cfg_save_mode.setCurrentIndex(inp.get('cfg_save_mode_idx', 0))
            if hasattr(self, 'cfg_interval'): self.cfg_interval.setValue(inp.get('cfg_interval', 60))
            if hasattr(self, 'img_count'): self.img_count.setValue(inp.get('img_count', 3))
            if hasattr(self, 'img_size'): self.img_size.setCurrentIndex(inp.get('img_size_idx', 0))
            if hasattr(self, 'img_prompt'): self.img_prompt.setPlainText(inp.get('img_prompt', ''))
            if hasattr(self, 'style_align'): self.style_align.setCurrentIndex(inp.get('style_align_idx', 0))
            if hasattr(self, 'style_size'): self.style_size.setValue(inp.get('style_size', 15))
            if hasattr(self, 'style_quote'): self.style_quote.setChecked(inp.get('style_quote', True))
            if hasattr(self, 'style_bold'): self.style_bold.setChecked(inp.get('style_bold', True))
            if hasattr(self, 'style_hashtag'): self.style_hashtag.setChecked(inp.get('style_hashtag', True))
            if hasattr(self, 'bl_input'): self.bl_input.setPlainText(inp.get('bl_input', ''))
        except Exception as e:
            print(f"[Wizard] 입력 복원 일부 실패: {e}")

        self._render_stepper(); self._render_step(); self._render_items_table()

    # ─── 공통 UI ─────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Stepper (위쪽 큰 단계 표시 바)
        self.stepper_widget = QWidget()
        self.stepper_widget.setStyleSheet("background:#1a1a1a; border-bottom:2px solid #333;")
        self.stepper_layout = QHBoxLayout(self.stepper_widget)
        self.stepper_layout.setContentsMargins(14, 12, 14, 12)
        self.stepper_layout.setSpacing(4)
        root.addWidget(self.stepper_widget)

        # Center: left content + right log
        center = QHBoxLayout()
        center.setContentsMargins(8, 4, 8, 4)

        left = QVBoxLayout()
        self.stat_label = QLabel("글감 0개 | 완료 0 | 실패 0 | 대기 0")
        self.stat_label.setStyleSheet("color:#fff; font-size:13px; padding:4px;")
        left.addWidget(self.stat_label)

        self.step_stack = QStackedWidget()
        for _, key in self.STEPS:
            self.step_stack.addWidget(self._build_step_widget(key))
        left.addWidget(self.step_stack, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100); self.progress_bar.setValue(0)
        left.addWidget(self.progress_bar)

        left.addWidget(QLabel("📋 글감 (AI 입력) — ↗ 클릭 시 미리보기"))
        self.items_table = QTableWidget(0, 6)
        self.items_table.setHorizontalHeaderLabels(["#", "글감 (AI 입력)", "이미지", "상태", "확대", "링크"])
        hdr = self.items_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for c in [0, 2, 3, 4]:
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.items_table.setMaximumHeight(220)
        left.addWidget(self.items_table)

        center.addLayout(left, 4)

        right = QVBoxLayout()
        right.addWidget(QLabel("📋 로그"))
        self.log_panel = QTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setStyleSheet("background:#1a1a1a; color:#bbb; font-size:11px;")
        right.addWidget(self.log_panel)
        center.addLayout(right, 2)

        root.addLayout(center, 1)

        # Bottom nav
        bot = QHBoxLayout()
        self.prev_btn = QPushButton("← 이전"); self.prev_btn.clicked.connect(self.prev_step)
        bot.addWidget(self.prev_btn); bot.addStretch()
        self.step_label = QLabel("(1/10)"); self.step_label.setStyleSheet("color:#aaa;")
        bot.addWidget(self.step_label); bot.addStretch()
        self.save_btn = QPushButton("💾 상태 저장"); self.save_btn.clicked.connect(self.save_state)
        bot.addWidget(self.save_btn)
        self.load_btn = QPushButton("📂 상태 불러오기"); self.load_btn.clicked.connect(self.load_state)
        bot.addWidget(self.load_btn)
        self.final_btn = QPushButton("최종단계 →")
        self.final_btn.setStyleSheet("padding:8px 14px; color:#aaa;")
        self.final_btn.clicked.connect(lambda: self._jump_to_step(len(self.STEPS) - 1))
        bot.addWidget(self.final_btn)
        self.next_btn = QPushButton("다음 →")
        self.next_btn.setStyleSheet("background:#FFD700; color:#1e1e1e; font-weight:bold; padding:8px 20px;")
        self.next_btn.clicked.connect(self.next_step)
        bot.addWidget(self.next_btn)
        root.addLayout(bot)

    def log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_panel.append(f"[{ts}] {msg}")
        try: self.main.log(msg)
        except Exception: pass

    def _update_stats(self):
        done = sum(1 for it in self.items if it.get('status') == 'done')
        fail = sum(1 for it in self.items if it.get('status') == 'fail')
        run  = sum(1 for it in self.items if it.get('status') == 'running')
        wait = sum(1 for it in self.items if it.get('status') in ('wait', None))
        total = len(self.items)
        self.stat_label.setText(f"글감 {total}개 | 완료 {done} | 실패 {fail} | 대기 {wait + run}")
        self.progress_bar.setMaximum(max(1, total))
        self.progress_bar.setValue(done + fail)

    def _render_items_table(self):
        self.items_table.setRowCount(len(self.items))
        for i, it in enumerate(self.items):
            self.items_table.setItem(i, 0, QTableWidgetItem(str(i+1)))
            title = it.get('title', '') or ''
            text = (it.get('rewritten') or it.get('text') or '')[:60]
            disp = f"[제목] {title[:60]} · [본문] {text}" if (title or text) else (it.get('url', '') or '(빈 글감)')
            self.items_table.setItem(i, 1, QTableWidgetItem(disp[:160]))
            self.items_table.setItem(i, 2, QTableWidgetItem(str(it.get('image_count', 0))))
            status_text = {'done': '완료', 'fail': '실패', 'running': '진행중', 'wait': '대기'}.get(
                it.get('status', 'wait'), '대기')
            self.items_table.setItem(i, 3, QTableWidgetItem(status_text))
            zoom = QPushButton("↗")
            zoom.clicked.connect(lambda _checked, idx=i: self.preview_item(idx))
            self.items_table.setCellWidget(i, 4, zoom)
            link = it.get('url', '') or it.get('blog_url', '') or '-'
            self.items_table.setItem(i, 5, QTableWidgetItem(link[:40]))
        self._update_stats()

    def preview_item(self, idx):
        if idx >= len(self.items): return
        it = self.items[idx]
        dlg = QDialog(self)
        dlg.setWindowTitle(f"글감 미리보기 #{idx+1}")
        dlg.resize(800, 600)
        lay = QVBoxLayout(dlg)

        info = QLabel(
            f"<b>제목:</b> {it.get('title', '(없음)')}<br>"
            f"<b>상태:</b> {it.get('status', 'wait')}<br>"
            f"<b>링크:</b> {it.get('url', '-') or '-'}<br>"
            f"<b>이미지:</b> {it.get('image_count', 0)}장"
        )
        info.setWordWrap(True); info.setStyleSheet("padding:6px;")
        lay.addWidget(info)

        tabs = _QTabWidget_Agency()
        # 원본 본문
        orig = QTextEdit(); orig.setReadOnly(True)
        orig.setPlainText(it.get('text', '') or '(원본 없음)')
        tabs.addTab(orig, "📄 원본")
        # AI 재작성본
        rew = QTextEdit(); rew.setReadOnly(True)
        rew.setPlainText(it.get('rewritten', '') or '(AI 재작성 안 됨)')
        tabs.addTab(rew, "✨ AI 재작성")
        # 이미지 경로
        img_list = QTextEdit(); img_list.setReadOnly(True)
        img_list.setPlainText('\n'.join(it.get('image_paths', []) or ['(없음)']))
        tabs.addTab(img_list, "🖼️ 이미지")
        lay.addWidget(tabs, 1)

        close = QPushButton("닫기"); close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        dlg.exec()

    def _render_stepper(self):
        for i in reversed(range(self.stepper_layout.count())):
            it = self.stepper_layout.itemAt(i)
            w = it.widget() if it else None
            if w: w.setParent(None)
            else: self.stepper_layout.removeItem(it)
        for i, (label, _) in enumerate(self.STEPS):
            chip = QPushButton()
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setMinimumHeight(40)
            if i < self.current_step:
                chip.setText(f"✓ {label}")
                chip.setStyleSheet(
                    "QPushButton{color:#4CAF50; background:transparent; border:none; "
                    "font-size:14px; font-weight:bold; padding:6px 10px;} "
                    "QPushButton:hover{color:#7be087;}")
            elif i == self.current_step:
                chip.setText(f"●{i+1} {label}")
                chip.setStyleSheet(
                    "QPushButton{color:#1e1e1e; background:#FFD700; "
                    "font-weight:bold; font-size:15px; padding:8px 14px; "
                    "border:2px solid #FFD700; border-radius:20px;} "
                    "QPushButton:hover{background:#ffe566;}")
            else:
                chip.setText(f"{i+1} {label}")
                chip.setStyleSheet(
                    "QPushButton{color:#aaa; background:transparent; border:none; "
                    "font-size:14px; padding:6px 10px;} "
                    "QPushButton:hover{color:#fff;}")
            # 클릭으로 해당 단계로 점프
            chip.clicked.connect(lambda _checked, idx=i: self._jump_to_step(idx))
            self.stepper_layout.addWidget(chip)
            if i < len(self.STEPS) - 1:
                sep = QLabel("─")
                sep.setStyleSheet("color:#555; font-size:14px; padding:0 2px;")
                self.stepper_layout.addWidget(sep)
        self.stepper_layout.addStretch()

    def _jump_to_step(self, idx):
        """stepper 칩 클릭 시 해당 단계로 이동 (이전 단계는 자유, 다음 단계는 검증 통과 시)"""
        if idx == self.current_step:
            return
        if idx < self.current_step:
            self.current_step = idx
            self._render_stepper(); self._render_step()
        else:
            # 앞으로 이동 — 중간 단계의 검증 거치기
            while self.current_step < idx:
                key = self.STEPS[self.current_step][1]
                on_exit = getattr(self, f'_on_exit_{key}', None)
                if on_exit:
                    try:
                        if on_exit() is False: return
                    except Exception as e:
                        self.log(f"⚠️ {key} 검증 실패: {e}"); return
                self.current_step += 1
            self._render_stepper(); self._render_step()

    def _render_step(self):
        self.step_stack.setCurrentIndex(self.current_step)
        self.step_label.setText(f"({self.current_step + 1}/{len(self.STEPS)})")
        self.prev_btn.setEnabled(self.current_step > 0)
        is_last = self.current_step == len(self.STEPS) - 1
        self.next_btn.setText("✓ 완료" if is_last else "다음 →")
        self.final_btn.setVisible(self.current_step < len(self.STEPS) - 1)
        key = self.STEPS[self.current_step][1]
        on_enter = getattr(self, f'_on_enter_{key}', None)
        if on_enter:
            try: on_enter()
            except Exception as e: self.log(f"⚠️ {key} 진입 오류: {e}")

    def next_step(self):
        key = self.STEPS[self.current_step][1]
        on_exit = getattr(self, f'_on_exit_{key}', None)
        if on_exit:
            try:
                if on_exit() is False: return
            except Exception as e:
                self.log(f"⚠️ {key} 검증 실패: {e}"); return
        if self.current_step < len(self.STEPS) - 1:
            self.current_step += 1
            self._render_stepper(); self._render_step()
        else:
            self.log("✅ 모든 단계 완료")
        self._state_save()

    def prev_step(self):
        if self.current_step > 0:
            self.current_step -= 1
            self._render_stepper(); self._render_step()
            self._state_save()

    def save_state(self):
        path, _ = QFileDialog.getSaveFileName(self, "상태 저장", "wizard_state.json", "JSON (*.json)")
        if not path: return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'current_step': self.current_step, 'items': self.items,
                           'config': self.config}, f, ensure_ascii=False, indent=2)
            self.log(f"💾 저장: {path}")
        except Exception as e:
            self.log(f"❌ 저장 실패: {e}")

    def load_state(self):
        path, _ = QFileDialog.getOpenFileName(self, "상태 불러오기", "", "JSON (*.json)")
        if not path: return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.current_step = data.get('current_step', 0)
            self.items = data.get('items', [])
            self.config = data.get('config', {})
            self._render_stepper(); self._render_step(); self._render_items_table()
            self.log(f"📂 불러옴: {path}")
        except Exception as e:
            self.log(f"❌ 불러오기 실패: {e}")

    def _build_step_widget(self, key):
        builder = getattr(self, f'_build_{key}', None)
        if builder: return builder()
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel(f"[{key}] 구현 예정")); l.addStretch()
        return w

    # ─── 1. 소스 선택 ─────────────────────────
    def _build_source(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("① 소스 선택"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("글감을 어디서 가져올지 선택하세요."))
        self.src_type = QComboBox()
        self.src_type.addItems([
            "📝 URL 직접 입력 (여러 줄)",
            "📰 네이버 블로그 검색 (키워드)",
            "📺 유튜브 URL (자막)",
            "☕ 네이버 카페 URL",
            "📥 엑셀/CSV (제목 대량)",
        ])
        gl.addWidget(self.src_type)
        gl.addWidget(QLabel("입력 (URL/키워드/파일경로 — 한 줄에 하나):"))
        self.src_input = QTextEdit()
        self.src_input.setPlaceholderText(
            "URL 모드 예: https://blog.naver.com/...\n"
            "키워드 모드 예: 부산 광안리 맛집\n"
            "엑셀 모드 예: C:/data/titles.csv"
        )
        gl.addWidget(self.src_input, 1)
        h = QHBoxLayout()
        pick = QPushButton("파일 선택"); pick.clicked.connect(self._src_pick_file)
        h.addWidget(pick); h.addStretch()
        gl.addLayout(h)
        l.addWidget(g); l.addStretch()
        return w

    def _src_pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "파일 선택", "",
                                              "엑셀/CSV (*.xlsx *.xls *.csv);;모든 파일 (*.*)")
        if path:
            cur = self.src_input.toPlainText().strip()
            self.src_input.setPlainText((cur + "\n" if cur else "") + path)

    def _on_exit_source(self):
        raw = self.src_input.toPlainText().strip()
        if not raw:
            self.log("⚠️ 소스 입력 비어있음"); return False
        self.config['source_type'] = self.src_type.currentText()
        self.config['source_input'] = raw
        self.log(f"✅ 소스: {self.config['source_type']}")
        return True

    # ─── 2. 추출 ─────────────────────────
    def _build_extract(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("② 추출"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("선택한 소스에서 글감을 추출합니다."))
        self.extract_btn = QPushButton("🔍 추출 시작")
        self.extract_btn.clicked.connect(self.run_extract)
        gl.addWidget(self.extract_btn)
        gl.addWidget(QLabel("추출 결과는 아래 글감 테이블에 표시됩니다."))
        l.addWidget(g); l.addStretch()
        return w

    def run_extract(self):
        src_type = self.config.get('source_type', '')
        raw = self.config.get('source_input', '')
        self.items = []
        try:
            if "URL" in src_type:
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        self.items.append({'url': line, 'title': '', 'text': '', 'status': 'wait'})
            elif "키워드" in src_type:
                from urllib.parse import quote
                keyword = raw.splitlines()[0].strip()
                self.config['keyword'] = keyword
                r = requests.get(
                    f"https://search.naver.com/search.naver?where=post&query={quote(keyword)}",
                    headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
                soup = BeautifulSoup(r.text, 'html.parser')
                urls = []
                for a in soup.select("a"):
                    h = a.get('href', '')
                    if 'blog.naver.com' in h and h.startswith('http') and h not in urls:
                        urls.append(h)
                    if len(urls) >= 10: break
                for u in urls:
                    self.items.append({'url': u, 'title': '', 'text': '', 'status': 'wait'})
            elif "유튜브" in src_type:
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        self.items.append({'url': line, 'title': '(유튜브)', 'text': '', 'status': 'wait'})
            elif "카페" in src_type:
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        self.items.append({'url': line, 'title': '(카페)', 'text': '', 'status': 'wait'})
            elif "엑셀" in src_type or "CSV" in src_type:
                import csv
                for p in [x.strip() for x in raw.splitlines() if x.strip()]:
                    try:
                        if p.lower().endswith('.csv'):
                            with open(p, encoding='utf-8-sig') as f:
                                for row in list(csv.reader(f))[1:]:
                                    if row:
                                        self.items.append({
                                            'title': row[0] if row else '',
                                            'text': row[1] if len(row) > 1 else '',
                                            'status': 'wait',
                                        })
                        else:
                            self.log(f"⚠️ xlsx는 openpyxl 필요 (스킵): {p}")
                    except Exception as e:
                        self.log(f"⚠️ {p}: {e}")
            self._render_items_table()
            self.log(f"✅ 추출 완료: {len(self.items)}개")
            self._state_save()
        except Exception as e:
            self.log(f"❌ 추출 실패: {e}")
            self._state_save()

    # ─── 3. 블로그 (형태소 간이) ───
    def _build_blog(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("③ 블로그 (상위글 패턴 학습)"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("키워드의 상위 블로그를 분석해 SEO 패턴을 학습합니다."))
        self.blog_kw = QLineEdit(); self.blog_kw.setPlaceholderText("키워드 (자동 채움)")
        gl.addWidget(self.blog_kw)
        self.blog_result = QTextEdit(); self.blog_result.setReadOnly(True)
        self.blog_result.setMaximumHeight(120)
        gl.addWidget(QLabel("분석 결과:")); gl.addWidget(self.blog_result)
        self.blog_btn = QPushButton("🔬 분석 실행")
        self.blog_btn.clicked.connect(self.run_blog_analysis)
        gl.addWidget(self.blog_btn)
        l.addWidget(g); l.addStretch()
        return w

    def _on_enter_blog(self):
        if self.config.get('keyword'):
            self.blog_kw.setText(self.config['keyword'])

    def run_blog_analysis(self):
        kw = self.blog_kw.text().strip()
        if not kw:
            self.log("⚠️ 키워드 입력"); return
        self.config['keyword'] = kw
        self.blog_result.setText(
            f"분석 키워드: {kw}\n"
            f"  · 권장 제목 길이: 20~30자\n"
            f"  · 권장 본문 키워드 빈도: 5회 이상\n"
            f"  · 권장 이미지: 5장 이상\n"
            f"(상세 분석은 '🔬 형태소 분석' 페이지에서)"
        )
        self.log(f"🔬 블로그 분석: {kw}")

    # ─── 4. AI ───
    def _build_ai(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("④ AI 모델"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("AI 모델:"))
        self.ai_model_combo = QComboBox()
        self.ai_model_combo.addItems(["gpt-4o", "gemini-2.5-flash", "sonar-pro"])
        gl.addWidget(self.ai_model_combo)
        gl.addWidget(QLabel("목표 글자 수:"))
        self.ai_chars = QSpinBox(); self.ai_chars.setRange(300, 8000); self.ai_chars.setValue(1500)
        gl.addWidget(self.ai_chars)
        l.addWidget(g); l.addStretch()
        return w

    def _on_exit_ai(self):
        self.config['ai_model'] = self.ai_model_combo.currentText()
        self.config['ai_chars'] = self.ai_chars.value()
        return True

    # ─── 5. 프롬프트 ───
    def _build_prompt(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("⑤ AI 프롬프트"); gl = QVBoxLayout(g)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("글 재작성 지시 프롬프트:"))
        hdr.addStretch()
        lib_btn = QPushButton("📚 프롬프트 라이브러리")
        lib_btn.setStyleSheet("padding:4px 10px;")
        lib_btn.clicked.connect(self._open_prompt_library)
        hdr.addWidget(lib_btn)
        gl.addLayout(hdr)

        self._prompt_title_label = QLabel("(선택된 프롬프트 없음)")
        self._prompt_title_label.setStyleSheet("color:#aaa; font-style:italic; padding:2px;")
        gl.addWidget(self._prompt_title_label)

        self.ai_prompt = QTextEdit()
        self.ai_prompt.setPlaceholderText(
            "예: 친근하고 정보가 풍부한 블로그 글로 재작성. 키워드를 자연스럽게 5번 이상 포함. 해시태그 5개 추가."
        )
        gl.addWidget(self.ai_prompt, 1)
        l.addWidget(g); l.addStretch()
        return w

    def _open_prompt_library(self):
        current = self.ai_prompt.toPlainText() if hasattr(self, 'ai_prompt') else ''
        dlg = PromptLibraryDialog(current_text=current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text = dlg.get_selected_text()
            if text:
                self.ai_prompt.setPlainText(text)
                # 제목 표시 업데이트
                row = dlg._list.currentRow()
                if 0 <= row < len(dlg._library):
                    title = dlg._library[row].get('title', '')
                    self._prompt_title_label.setText(f"✅ 선택됨: {title}")
                    self._prompt_title_label.setStyleSheet("color:#4CAF50; font-weight:bold; padding:2px;")

    def _on_exit_prompt(self):
        self.config['ai_prompt'] = self.ai_prompt.toPlainText().strip()
        return True

    # ─── 6. 포스팅 설정 ───
    def _build_post_config(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("⑥ 포스팅 설정"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("📋 계정 선택 (체크박스로 선택)"))
        self.account_table = QTableWidget(0, 5)
        self.account_table.setHorizontalHeaderLabels(["선택", "계정명", "블로그 ID", "카테고리", "포스팅 수"])
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.account_table.setMaximumHeight(160)
        gl.addWidget(self.account_table)
        load = QPushButton("📥 설정에서 계정 불러오기"); load.clicked.connect(self._load_accounts)
        gl.addWidget(load)
        h = QHBoxLayout()
        h.addWidget(QLabel("발행 모드:"))
        self.cfg_save_mode = QComboBox()
        self.cfg_save_mode.addItems(["💾 임시저장", "📤 즉시발행", "⏰ 예약발행"])
        h.addWidget(self.cfg_save_mode)
        h.addWidget(QLabel("글 간격(초):"))
        self.cfg_interval = QSpinBox(); self.cfg_interval.setRange(0, 600); self.cfg_interval.setValue(60)
        h.addWidget(self.cfg_interval); h.addStretch()
        gl.addLayout(h)
        l.addWidget(g); l.addStretch()
        return w

    def _load_accounts(self):
        accounts = self.main.config.get('naver_accounts', []) or []
        if not accounts and self.main.config.get('naver_id'):
            accounts = [{'id': self.main.config['naver_id'],
                         'blog_id': self.main.config.get('naver_id', '')}]
        self.account_table.setRowCount(len(accounts))
        for i, a in enumerate(accounts):
            cb = QCheckBox(); cb.setChecked(i == 0)
            self.account_table.setCellWidget(i, 0, cb)
            self.account_table.setItem(i, 1, QTableWidgetItem(a.get('id', '')))
            self.account_table.setItem(i, 2, QTableWidgetItem(a.get('blog_id', a.get('id', ''))))
            self.account_table.setItem(i, 3, QTableWidgetItem(a.get('category', '')))
            cnt = QSpinBox(); cnt.setRange(0, 999); cnt.setValue(len(self.items))
            self.account_table.setCellWidget(i, 4, cnt)

    def _on_exit_post_config(self):
        selected = []
        for i in range(self.account_table.rowCount()):
            cb = self.account_table.cellWidget(i, 0)
            if cb and cb.isChecked():
                selected.append({
                    'id': self.account_table.item(i, 1).text() if self.account_table.item(i, 1) else '',
                    'blog_id': self.account_table.item(i, 2).text() if self.account_table.item(i, 2) else '',
                    'category': self.account_table.item(i, 3).text() if self.account_table.item(i, 3) else '',
                    'count': self.account_table.cellWidget(i, 4).value() if self.account_table.cellWidget(i, 4) else 0,
                })
        if not selected:
            self.log("⚠️ 계정을 1개 이상 선택하세요"); return False
        self.config['accounts'] = selected
        self.config['save_mode'] = self.cfg_save_mode.currentText()
        self.config['interval'] = self.cfg_interval.value()
        return True

    # ─── 7. 이미지 ───
    def _build_image(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("⑦ 이미지 생성"); gl = QVBoxLayout(g)
        h = QHBoxLayout()
        h.addWidget(QLabel("글당 이미지:"))
        self.img_count = QSpinBox(); self.img_count.setRange(0, 10); self.img_count.setValue(3)
        h.addWidget(self.img_count)
        h.addWidget(QLabel("크기:"))
        self.img_size = QComboBox(); self.img_size.addItems(["1024x1024", "1024x1536", "1536x1024"])
        h.addWidget(self.img_size); h.addStretch()
        gl.addLayout(h)
        gl.addWidget(QLabel("이미지 스타일 프롬프트:"))
        self.img_prompt = QTextEdit()
        self.img_prompt.setPlaceholderText("예: 밝고 따뜻한 분위기, 사진 스타일, 한국적 정서")
        self.img_prompt.setMaximumHeight(80)
        gl.addWidget(self.img_prompt)
        l.addWidget(g); l.addStretch()
        return w

    def _on_exit_image(self):
        self.config['image_count_per_post'] = self.img_count.value()
        self.config['image_size'] = self.img_size.currentText()
        self.config['image_prompt_style'] = self.img_prompt.toPlainText().strip()
        return True

    # ─── 8. 스타일 ───
    def _build_style(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("⑧ 글 스타일"); gl = QVBoxLayout(g)
        h = QHBoxLayout()
        h.addWidget(QLabel("전체 정렬:"))
        self.style_align = QComboBox(); self.style_align.addItems(["좌측", "가운데", "우측", "양쪽"])
        h.addWidget(self.style_align)
        h.addWidget(QLabel("글꼴 크기:"))
        self.style_size = QSpinBox(); self.style_size.setRange(10, 24); self.style_size.setValue(15)
        h.addWidget(self.style_size); h.addStretch()
        gl.addLayout(h)
        self.style_quote = QCheckBox("인용구 자동 감지 (> 로 시작 줄)"); self.style_quote.setChecked(True)
        self.style_bold = QCheckBox("**굵게** 마커 처리"); self.style_bold.setChecked(True)
        self.style_hashtag = QCheckBox("해시태그 자동 추가"); self.style_hashtag.setChecked(True)
        gl.addWidget(self.style_quote); gl.addWidget(self.style_bold); gl.addWidget(self.style_hashtag)
        l.addWidget(g); l.addStretch()
        return w

    def _on_exit_style(self):
        align_map = {0: 'LEFT', 1: 'CENTER', 2: 'RIGHT', 3: 'JUSTIFY'}
        self.config['alignment'] = align_map.get(self.style_align.currentIndex(), 'LEFT')
        self.config['style_quote'] = self.style_quote.isChecked()
        self.config['style_bold'] = self.style_bold.isChecked()
        self.config['style_hashtag'] = self.style_hashtag.isChecked()
        return True

    # ─── 9. 백링크 ───
    def _build_backlink(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("⑨ 백링크 (글 끝에 삽입)"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("한 줄에 한 링크. 비워두면 없음."))
        self.bl_input = QTextEdit()
        self.bl_input.setPlaceholderText("https://example.com\nhttps://other-site.com")
        gl.addWidget(self.bl_input, 1)
        l.addWidget(g); l.addStretch()
        return w

    def _on_exit_backlink(self):
        self.config['backlinks'] = [l.strip() for l in self.bl_input.toPlainText().splitlines() if l.strip()]
        return True

    # ─── 10. 포스팅 ───
    def _build_posting(self):
        w = QWidget(); l = QVBoxLayout(w)
        g = QGroupBox("⑩ 포스팅 실행"); gl = QVBoxLayout(g)
        gl.addWidget(QLabel("모든 글감을 일괄로 처리합니다. (추출→AI 재작성→이미지 생성→업로드)"))
        h = QHBoxLayout()
        self.start_btn = QPushButton("▶ 포스팅 시작")
        self.start_btn.setStyleSheet("background:#4CAF50; color:#fff; font-weight:bold; padding:8px 16px;")
        self.start_btn.clicked.connect(self.run_posting)
        h.addWidget(self.start_btn)
        self.stop_btn = QPushButton("⏹ 정지")
        self.stop_btn.setStyleSheet("background:#f44336; color:#fff; font-weight:bold; padding:8px 16px;")
        self.stop_btn.clicked.connect(self.stop_posting); self.stop_btn.setEnabled(False)
        h.addWidget(self.stop_btn); h.addStretch()
        gl.addLayout(h)
        gl.addWidget(QLabel("실시간 진행 상태는 아래 글감 테이블 + 우측 로그에서 확인."))
        l.addWidget(g); l.addStretch()
        return w

    def stop_posting(self):
        self._stop_requested = True
        self.log("⏹ 정지 요청")

    def run_posting(self):
        if not self.items:
            self.log("⚠️ 글감이 없습니다 (② 추출 단계에서 가져오세요)"); return
        self._stop_requested = False
        self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)

        def worker():
            interval = self.config.get('interval', 60)
            for i, it in enumerate(self.items):
                if self._stop_requested:
                    self.log(f"⏹ 정지: {i}/{len(self.items)}"); break
                it['status'] = 'running'
                self._update_stats()
                self.items_table.setItem(i, 3, QTableWidgetItem('진행중'))
                try:
                    self._process_one_item(i, it)
                    it['status'] = 'done'
                except Exception as e:
                    it['status'] = 'fail'
                    self.log(f"❌ #{i+1}: {e}")
                self.items_table.setItem(i, 3, QTableWidgetItem(
                    {'done': '완료', 'fail': '실패'}.get(it['status'], '대기')))
                self._update_stats()
                self._state_save()  # 글감 하나 처리할 때마다 저장 — 크래시 대비
                if interval > 0 and i < len(self.items) - 1 and not self._stop_requested:
                    self.log(f"⏱ 다음까지 {interval}초 대기...")
                    time.sleep(interval)
            return "✅ 포스팅 작업 완료"

        t = WorkerThread(worker)
        self.main.worker_threads.append(t)
        t.log_signal.connect(self.log)
        t.result_signal.connect(self.log)
        t.finished_signal.connect(lambda: (
            self.start_btn.setEnabled(True), self.stop_btn.setEnabled(False)))
        t.start()

    def _process_one_item(self, idx, item):
        """글감 1개 처리: 본문 추출 → AI 재작성 → 이미지 → 업로드"""
        api_key = self.main.config.get('openai_api_key', '')

        # 1) URL이 있으면 본문 추출
        if item.get('url') and not item.get('text'):
            try:
                r = requests.get(item['url'], headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
                soup = BeautifulSoup(r.text, 'html.parser')
                body_el = soup.select_one('.se-main-container') or soup.select_one('#postViewArea')
                item['text'] = (body_el.get_text(' ', strip=True) if body_el
                                else soup.get_text(' ', strip=True))[:3000]
                if not item.get('title'):
                    title_el = soup.select_one('.se-title-text') or soup.select_one('title')
                    if title_el: item['title'] = title_el.get_text(strip=True)
            except Exception as e:
                self.log(f"⚠️ #{idx+1} 본문 추출 실패: {e}")

        # 2) AI 재작성
        if api_key and item.get('text'):
            prompt = (self.config.get('ai_prompt', '') or '다음 본문을 블로그 글로 재작성:') \
                     + '\n\n' + item['text']
            try:
                rewritten = call_openai_text(api_key, prompt,
                                             model=self.config.get('ai_model', 'gpt-4o'))
                if rewritten:
                    item['rewritten'] = rewritten
                    self.log(f"✏️ #{idx+1} AI 재작성 ({len(rewritten)}자)")
            except Exception as e:
                self.log(f"⚠️ #{idx+1} AI 실패: {e}")

        # 3) 이미지 생성
        n_imgs = self.config.get('image_count_per_post', 0)
        if api_key and n_imgs > 0:
            try:
                client = build_openai_client(api_key)
                style = self.config.get('image_prompt_style', '')
                base_prompt = (item.get('title', '') or (item.get('text', '') or '')[:200])
                if style: base_prompt += ' | ' + style
                paths = []
                for _ in range(n_imgs):
                    if self._stop_requested: break
                    resp = client.images.generate(
                        model="gpt-image-1", prompt=base_prompt[:1000],
                        size=self.config.get('image_size', '1024x1024'))
                    b64 = getattr(resp.data[0], 'b64_json', None)
                    if b64:
                        out_dir = Path("generated_images"); out_dir.mkdir(exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        out = out_dir / f"wizard_{idx}_{ts}.png"
                        with open(out, 'wb') as f:
                            f.write(base64.b64decode(b64))
                        paths.append(str(out))
                item['image_paths'] = paths
                item['image_count'] = len(paths)
                self.items_table.setItem(idx, 2, QTableWidgetItem(str(len(paths))))
                self.log(f"🖼️ #{idx+1} 이미지 {len(paths)}장")
            except Exception as e:
                self.log(f"⚠️ #{idx+1} 이미지 실패: {e}")

        # 4) 백링크 추가
        backlinks = self.config.get('backlinks', [])
        if backlinks and item.get('rewritten'):
            item['rewritten'] += '\n\n' + '\n'.join(backlinks)

        # 5) 업로드 (실제: 첫 계정 사용, 시뮬레이션 + 추후 활성화 가능)
        accounts = self.config.get('accounts', [])
        if accounts:
            self.log(f"📤 #{idx+1} 업로드: {accounts[0].get('id', '?')} → "
                     f"{self.config.get('save_mode', '임시저장')} (시뮬레이션)")
            # 실제 업로드 코드는 AgencyUploadPage._do_upload와 유사하게 동작
            # 다음 버전에서 실제 호출 활성화 — 현재는 안전을 위해 시뮬레이션
        time.sleep(0.5)


# ═══════════════════════════════════════════════════════════════
#  통합 수집/발행 페이지 (UnifiedCollectPage)
#  - 기존 글수집 하위 7~12개 페이지를 하나의 페이지로 통합
#  - 상단: 소스 선택 버튼 (수집 소스 / AI봇 워크플로우)
#  - 하단: 선택된 페이지의 전체 UI (기존 페이지 인스턴스 재사용)
#  - 기존 페이지 코드는 그대로 유지되며, 사이드바에서만 숨겨짐
# ═══════════════════════════════════════════════════════════════
class UnifiedCollectPage(QWidget):
    SOURCE_GROUPS = [
        ("📥 수집 소스", [
            ("📝 블로그",      "blog"),
            ("1️⃣ 숏텐츠",     "news_short"),
            ("2️⃣ 다음",       "news_daum"),
            ("3️⃣ 네이트",     "news_nate"),
            ("☕ 카페",        "cafe"),
            ("🔥 인기글",      "popular"),
            ("📺 유튜브",      "youtube"),
            ("📥 엑셀(대량)",  "bulk_title"),
        ]),
        ("🤖 AI봇 워크플로우", [
            ("⚙️ 설정",        "salgoo_config"),
            ("📥 뉴스 수집",   "salgoo_news"),
            ("✨ AI 재작성",   "salgoo_rewrite"),
            ("📤 발행",        "salgoo_publish"),
        ]),
    ]

    def __init__(self, main_win):
        super().__init__()
        self.main_win = main_win
        self.source_pages = {}     # key -> 페이지 인스턴스 (캐시)
        self.source_buttons = {}   # key -> QPushButton
        self._current_key = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 헤더
        header = QLabel("📥 통합 수집/발행 페이지")
        header.setStyleSheet("color:#FFD700; font-weight:bold; font-size:16px; padding:4px 2px;")
        root.addWidget(header)

        sub = QLabel("위에서 소스를 선택하면 해당 페이지가 아래에 표시됩니다. "
                     "기존 페이지의 모든 기능(수집·AI 재작성·이미지 생성·업로드)이 그대로 동작합니다.")
        sub.setStyleSheet("color:#aaa; font-size:11px; padding:0 2px 4px 2px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # 소스 선택 버튼 영역 (그룹별로 한 줄씩)
        btn_style_unchecked = """
            QPushButton {
                background-color:#2a2a2a; color:#ddd;
                border:1px solid #3a3a3a; border-radius:4px;
                padding:6px 10px; font-size:12px;
            }
            QPushButton:hover { background-color:#3a3a3a; color:#fff; }
        """
        btn_style_checked = """
            QPushButton {
                background-color:#FFD700; color:#1e1e1e;
                border:1px solid #FFD700; border-radius:4px;
                padding:6px 10px; font-size:12px; font-weight:bold;
            }
        """
        self._btn_style_unchecked = btn_style_unchecked
        self._btn_style_checked = btn_style_checked

        for group_label, items in self.SOURCE_GROUPS:
            grp_lbl = QLabel(group_label)
            grp_lbl.setStyleSheet("color:#888; font-size:11px; padding:4px 2px 2px 2px;")
            root.addWidget(grp_lbl)

            row = QHBoxLayout()
            row.setSpacing(4)
            for label, key in items:
                # 조건부 — multilink가 없으면 스킵
                if key == "multilink_ai" and not _MULTILINK_AVAILABLE:
                    continue
                btn = QPushButton(label)
                btn.setCheckable(True)
                btn.setStyleSheet(btn_style_unchecked)
                btn.clicked.connect(lambda _checked, k=key: self._switch_source(k))
                row.addWidget(btn)
                self.source_buttons[key] = btn
            row.addStretch()
            root.addLayout(row)

        # 멀티링크는 조건부로 별도 추가
        if _MULTILINK_AVAILABLE:
            extra_lbl = QLabel("🌐 확장")
            extra_lbl.setStyleSheet("color:#888; font-size:11px; padding:4px 2px 2px 2px;")
            root.addWidget(extra_lbl)
            extra_row = QHBoxLayout()
            extra_row.setSpacing(4)
            btn = QPushButton("🌐 멀티링크 → AI")
            btn.setCheckable(True)
            btn.setStyleSheet(btn_style_unchecked)
            btn.clicked.connect(lambda _checked, k="multilink_ai": self._switch_source(k))
            extra_row.addWidget(btn)
            extra_row.addStretch()
            root.addLayout(extra_row)
            self.source_buttons["multilink_ai"] = btn

        # 구분선
        line = QLabel()
        line.setStyleSheet("background-color:#3a3a3a; min-height:1px; max-height:1px; margin:6px 0;")
        root.addWidget(line)

        # 선택된 소스 페이지를 표시할 스택
        self.stack = QStackedWidget()
        placeholder = QLabel("← 위에서 소스를 선택하세요")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet("color:#666; font-size:14px; padding:60px;")
        self.stack.addWidget(placeholder)
        self.stack.setCurrentIndex(0)
        root.addWidget(self.stack, 1)

    def _switch_source(self, key):
        # 버튼 상태 갱신 (단일 선택 토글)
        for k, btn in self.source_buttons.items():
            checked = (k == key)
            btn.setChecked(checked)
            btn.setStyleSheet(self._btn_style_checked if checked else self._btn_style_unchecked)

        # 페이지 lazy load + 캐시
        if key not in self.source_pages:
            page = self.main_win.get_or_create_page(key)
            if page is None:
                try:
                    self.main_win.log(f"⚠️ 통합: '{key}' 페이지 생성 실패")
                except Exception:
                    pass
                return
            self.source_pages[key] = page
            # addWidget가 부모를 자동 재설정 (reparent)
            self.stack.addWidget(page)

        self.stack.setCurrentWidget(self.source_pages[key])
        self._current_key = key
        try:
            self.main_win.log(f"📂 통합: {key} 페이지 활성화")
        except Exception:
            pass


# ============================================================
# 🧩 확장 (Extension) — Naver 수집 + Blogger/WordPress 초안 발행
# ============================================================
import base64 as _ext_base64
import html as _ext_html_lib
from dataclasses import dataclass as _ext_dataclass, field as _ext_field, asdict as _ext_asdict
from urllib.parse import urljoin as _ext_urljoin


@_ext_dataclass
class ExtSourcePost:
    source_url: 'Optional[str]'
    title: str
    html: str
    text: str
    tags: list = _ext_field(default_factory=list)
    images: list = _ext_field(default_factory=list)
    author: 'Optional[str]' = None
    published_at: 'Optional[str]' = None


class ExtNaverBlogImporter:
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout=20):
        import requests as _rq
        self.timeout = timeout
        self.session = _rq.Session()
        self.session.headers.update({"User-Agent": self.USER_AGENT})

    def fetch(self, url):
        from bs4 import BeautifulSoup
        outer_html = self._get(url)
        iframe_url = self._extract_iframe_url(url, outer_html)
        if iframe_url:
            inner_html = self._get(iframe_url)
            base_url = iframe_url
        else:
            inner_html = outer_html
            base_url = url
        soup = BeautifulSoup(inner_html, "html.parser")
        title = self._extract_title(soup)
        author = self._extract_author(soup)
        published_at = self._extract_published_at(soup)
        content_node = self._extract_content_node(soup)
        if content_node is None:
            raise ValueError("네이버 본문 영역을 찾지 못했습니다.")
        self._remove_unwanted(content_node)
        self._normalize_images(content_node, base_url)
        images = [img.get("src", "").strip() for img in content_node.select("img[src]") if img.get("src")]
        images = list(dict.fromkeys(images))
        return ExtSourcePost(
            source_url=url, title=title, html=str(content_node),
            text=content_node.get_text("\n", strip=True), images=images,
            author=author, published_at=published_at,
        )

    def _get(self, url):
        res = self.session.get(url, timeout=self.timeout)
        res.raise_for_status()
        return res.text

    def _extract_iframe_url(self, page_url, html):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        iframe = soup.find("iframe", id="mainFrame")
        if iframe and iframe.get("src"):
            return _ext_urljoin(page_url, iframe["src"])
        return None

    def _extract_title(self, soup):
        for sel in ["meta[property='og:title']", ".se-title-text span", ".pcol1 .title_1", ".se_textarea", "title"]:
            node = soup.select_one(sel)
            if not node:
                continue
            val = (node.get("content") or "").strip() if node.name == "meta" else node.get_text(" ", strip=True)
            if val:
                return val
        return "제목 없음"

    def _extract_author(self, soup):
        for sel in ["meta[property='naverblog:nickname']", ".nick .ell", ".blog2_series .nick"]:
            node = soup.select_one(sel)
            if not node:
                continue
            val = (node.get("content") or "").strip() if node.name == "meta" else node.get_text(" ", strip=True)
            if val:
                return val
        return None

    def _extract_published_at(self, soup):
        for sel in ["meta[property='article:published_time']", ".se_publishDate", ".date"]:
            node = soup.select_one(sel)
            if not node:
                continue
            val = (node.get("content") or "").strip() if node.name == "meta" else node.get_text(" ", strip=True)
            if val:
                return val
        return None

    def _extract_content_node(self, soup):
        for sel in ["div.se-main-container", "div#postViewArea", "div.post-view", "div.view"]:
            node = soup.select_one(sel)
            if node:
                return node
        return None

    def _remove_unwanted(self, node):
        for bad in node.select("script, style, iframe, noscript, button"):
            bad.decompose()

    def _normalize_images(self, node, base_url):
        for img in node.select("img"):
            src = img.get("src") or img.get("data-lazy-src") or img.get("data-src") or img.get("data-linkdata")
            if src:
                img["src"] = _ext_urljoin(base_url, src)
            alt = (img.get("alt") or "").strip()
            img.attrs = {"src": img.get("src", ""), "alt": alt}


class ExtPostTransformer:
    ALLOWED_TAGS = {"p","br","h1","h2","h3","h4","ul","ol","li","blockquote",
                    "strong","em","b","i","a","img","figure","figcaption"}

    def clean_html(self, html):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.select("script, style, iframe, noscript, form, input, button"):
            tag.decompose()
        for tag in soup.find_all(True):
            if tag.name not in self.ALLOWED_TAGS:
                tag.unwrap()
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            a.attrs = {"href": href, "target": "_blank", "rel": "noopener noreferrer"}
        for img in soup.select("img"):
            img.attrs = {"src": img.get("src", "").strip(), "alt": (img.get("alt") or "").strip()}
        cleaned = str(soup)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def text_to_html(self, text):
        chunks = [x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()]
        return "\n".join(f"<p>{_ext_html_lib.escape(c)}</p>" for c in chunks)

    def make_excerpt(self, text, limit=140):
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit].rstrip() + ("..." if len(text) > limit else "")

    def slugify(self, title):
        v = title.lower().strip()
        v = re.sub(r"[^\w\s-]", "", v, flags=re.UNICODE)
        v = re.sub(r"[\s_-]+", "-", v)
        return v.strip("-")


class ExtBloggerPublisher:
    def __init__(self, access_token, blog_id, timeout=20):
        self.access_token = access_token
        self.blog_id = blog_id
        self.timeout = timeout

    def create_post(self, title, html, is_draft=True):
        import requests as _rq
        url = f"https://www.googleapis.com/blogger/v3/blogs/{self.blog_id}/posts"
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        params = {"isDraft": str(is_draft).lower()}
        payload = {"title": title, "content": html}
        res = _rq.post(url, headers=headers, params=params, json=payload, timeout=self.timeout)
        if not res.ok:
            raise RuntimeError(f"Blogger 업로드 실패: {res.status_code} {res.text}")
        return res.json()


class ExtWordPressPublisher:
    def __init__(self, site_url, username, app_password, timeout=20):
        self.site_url = site_url.rstrip("/")
        self.username = username
        self.app_password = app_password
        self.timeout = timeout

    def create_post(self, title, html, status="draft", slug=None, excerpt=None):
        import requests as _rq
        url = f"{self.site_url}/wp-json/wp/v2/posts"
        raw = f"{self.username}:{self.app_password}".encode("utf-8")
        token = _ext_base64.b64encode(raw).decode("utf-8")
        headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        payload = {"title": title, "content": html, "status": status}
        if slug:
            payload["slug"] = slug
        if excerpt:
            payload["excerpt"] = excerpt
        res = _rq.post(url, headers=headers, json=payload, timeout=self.timeout)
        if not res.ok:
            raise RuntimeError(f"WordPress 업로드 실패: {res.status_code} {res.text}")
        return res.json()


EXT_STATE_PATH = 'ext_state.json'


class ExtensionPage(QWidget):
    """🧩 확장: 네이버 원본 수집 + Blogger / WordPress 초안 발행"""

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self._build_ui()
        self._load_state()

    def _log(self, msg):
        try:
            self.log_view.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        except Exception:
            pass
        try:
            self.main.log(msg)
        except Exception:
            pass

    def _build_ui(self):
        from PyQt6.QtWidgets import (
            QFormLayout, QTabWidget, QGroupBox, QCheckBox, QLineEdit,
            QTextEdit, QPushButton, QLabel, QVBoxLayout, QHBoxLayout, QWidget,
            QTextBrowser,
        )
        self.setStyleSheet("""
            QWidget { background-color:#2b2b2b; color:#e8e8e8; }
            QGroupBox { border:1px solid #444; border-radius:6px; margin-top:14px; padding-top:10px;
                        background-color:#333; color:#FFD700; font-weight:bold; }
            QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 6px; }
            QLineEdit, QTextEdit { background-color:#3a3a3a; color:#fff; border:1px solid #555;
                                   border-radius:4px; padding:6px; selection-background-color:#555; }
            QLineEdit:focus, QTextEdit:focus { border:1px solid #FFD700; }
            QLineEdit::placeholder { color:#888; }
            QLabel { color:#ddd; background:transparent; }
            QCheckBox { color:#ddd; background:transparent; }
            QTabWidget::pane { border:1px solid #444; background:#333; }
            QTabBar::tab { background:#3a3a3a; color:#ddd; padding:6px 14px; border:1px solid #444; }
            QTabBar::tab:selected { background:#555; color:#FFD700; }
            QPushButton { background:#FFD700; color:#000; font-weight:bold; padding:8px 16px;
                          border:none; border-radius:4px; }
            QPushButton:hover { background:#ffe34d; }
            QPushButton:disabled { background:#666; color:#aaa; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("🧩 확장 — 네이버 수집 + Blogger / WordPress 초안 업로드")
        title.setStyleSheet("color:#FFD700; font-size:18px; font-weight:bold;")
        root.addWidget(title)

        # ── 플랫폼 인증 ──
        auth_box = QGroupBox("🔐 플랫폼 인증")
        auth_box.setStyleSheet("QGroupBox{color:#FFD700;font-weight:bold;}")
        af = QFormLayout(auth_box)
        self.blogger_token = QLineEdit(); self.blogger_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.blogger_token.setPlaceholderText("구글 블로거 OAuth 액세스 토큰")
        self.blogger_blog_id = QLineEdit(); self.blogger_blog_id.setPlaceholderText("구글 블로거 블로그 ID")
        self.wp_site = QLineEdit(); self.wp_site.setPlaceholderText("https://내-사이트.com")
        self.wp_user = QLineEdit(); self.wp_user.setPlaceholderText("워드프레스 사용자명")
        self.wp_pass = QLineEdit(); self.wp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.wp_pass.setPlaceholderText("워드프레스 애플리케이션 비밀번호")
        af.addRow("구글 블로거 액세스 토큰:", self.blogger_token)
        af.addRow("구글 블로거 블로그 ID:", self.blogger_blog_id)
        af.addRow("워드프레스 사이트 주소:", self.wp_site)
        af.addRow("워드프레스 사용자명:", self.wp_user)
        af.addRow("워드프레스 앱 비밀번호:", self.wp_pass)
        root.addWidget(auth_box)

        # ── 입력 소스 ──
        src_box = QGroupBox("📥 원본 입력")
        src_box.setStyleSheet("QGroupBox{color:#FFD700;font-weight:bold;}")
        sv = QVBoxLayout(src_box)
        self.src_tabs = QTabWidget()

        # 탭1: 네이버 링크
        w_url = QWidget(); lu = QFormLayout(w_url)
        self.naver_url = QLineEdit(); self.naver_url.setPlaceholderText("https://blog.naver.com/yourblog/123456789")
        lu.addRow("네이버 블로그 URL:", self.naver_url)
        self.src_tabs.addTab(w_url, "🔗 네이버 링크")

        # 탭2: 직접 입력
        w_man = QWidget(); lm = QFormLayout(w_man)
        self.manual_title = QLineEdit(); self.manual_title.setPlaceholderText("제목")
        self.manual_body = QTextEdit(); self.manual_body.setMinimumHeight(180)
        self.manual_body.setPlaceholderText("본문 (HTML 또는 텍스트)")
        self.manual_is_html = QCheckBox("본문이 HTML 형식")
        lm.addRow("제목:", self.manual_title)
        lm.addRow("본문:", self.manual_body)
        lm.addRow("", self.manual_is_html)
        self.src_tabs.addTab(w_man, "✍️ 직접 입력")

        sv.addWidget(self.src_tabs)
        root.addWidget(src_box)

        # ── 옵션 ──
        opt_box = QGroupBox("⚙️ 발행 옵션")
        opt_box.setStyleSheet("QGroupBox{color:#FFD700;font-weight:bold;}")
        of = QFormLayout(opt_box)
        self.chk_blogger = QCheckBox("구글 블로거 초안 업로드"); self.chk_blogger.setChecked(True)
        self.chk_wordpress = QCheckBox("워드프레스 초안 업로드"); self.chk_wordpress.setChecked(True)
        self.blogger_suffix = QLineEdit(); self.blogger_suffix.setPlaceholderText(" | Blogger")
        self.wp_suffix = QLineEdit(); self.wp_suffix.setPlaceholderText(" | WordPress")
        self.wp_include_source = QCheckBox("워드프레스 본문에 원문 출처 링크 추가")
        of.addRow("", self.chk_blogger)
        of.addRow("구글 블로거 제목 접미:", self.blogger_suffix)
        of.addRow("", self.chk_wordpress)
        of.addRow("워드프레스 제목 접미:", self.wp_suffix)
        of.addRow("", self.wp_include_source)
        root.addWidget(opt_box)

        # ── 실행 버튼 ──
        bh = QHBoxLayout()
        self.btn_preview = QPushButton("🔍 미리보기")
        self.btn_preview.setStyleSheet("background:#5a5a5a;color:#fff;font-weight:bold;padding:10px 18px;")
        self.btn_preview.clicked.connect(self._on_preview)
        bh.addWidget(self.btn_preview)
        self.btn_run = QPushButton("🚀 초안 업로드")
        self.btn_run.setStyleSheet("background:#FFD700;color:#000;font-weight:bold;padding:10px 18px;")
        self.btn_run.clicked.connect(self._on_run)
        bh.addWidget(self.btn_run)
        bh.addStretch()
        root.addLayout(bh)

        # ── 미리보기 (생성 전 / 생성 후, 화면 하단 항상 표시) ──
        prev_box = QGroupBox("👀 미리보기 (생성 전 / 생성 후)")
        prev_box.setStyleSheet("QGroupBox{color:#FFD700;font-weight:bold;}")
        ph = QHBoxLayout(prev_box)

        before_col = QVBoxLayout()
        before_col.addWidget(QLabel("📄 생성 전 (원본)"))
        self.preview_before = QTextBrowser()
        self.preview_before.setOpenExternalLinks(True)
        self.preview_before.setStyleSheet("background:#1e1e1e;color:#eee;border:1px solid #555;")
        self.preview_before.setMinimumHeight(220)
        before_col.addWidget(self.preview_before)
        ph.addLayout(before_col)

        after_col = QVBoxLayout()
        after_col.addWidget(QLabel("✨ 생성 후 (발행 변환본)"))
        self.preview_after = QTextBrowser()
        self.preview_after.setOpenExternalLinks(True)
        self.preview_after.setStyleSheet("background:#1e1e1e;color:#eee;border:1px solid #555;")
        self.preview_after.setMinimumHeight(220)
        after_col.addWidget(self.preview_after)
        ph.addLayout(after_col)

        root.addWidget(prev_box, 1)

        # ── 결과/로그 ──
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background:#1a1a1a;color:#9fd;font-size:12px;")
        self.log_view.setMinimumHeight(140)
        root.addWidget(self.log_view, 1)

        # 자동 저장 훅
        for w in [self.blogger_token, self.blogger_blog_id, self.wp_site, self.wp_user, self.wp_pass,
                  self.naver_url, self.manual_title, self.blogger_suffix, self.wp_suffix]:
            w.textChanged.connect(self._save_state)
        self.manual_body.textChanged.connect(self._save_state)
        for c in [self.chk_blogger, self.chk_wordpress, self.wp_include_source, self.manual_is_html]:
            c.stateChanged.connect(self._save_state)
        self.src_tabs.currentChanged.connect(self._save_state)

    def _save_state(self):
        try:
            data = {
                "blogger_token": self.blogger_token.text(),
                "blogger_blog_id": self.blogger_blog_id.text(),
                "wp_site": self.wp_site.text(),
                "wp_user": self.wp_user.text(),
                "wp_pass": self.wp_pass.text(),
                "naver_url": self.naver_url.text(),
                "manual_title": self.manual_title.text(),
                "manual_body": self.manual_body.toPlainText(),
                "manual_is_html": self.manual_is_html.isChecked(),
                "chk_blogger": self.chk_blogger.isChecked(),
                "chk_wordpress": self.chk_wordpress.isChecked(),
                "blogger_suffix": self.blogger_suffix.text(),
                "wp_suffix": self.wp_suffix.text(),
                "wp_include_source": self.wp_include_source.isChecked(),
                "src_tab": self.src_tabs.currentIndex(),
            }
            with open(EXT_STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            if not os.path.exists(EXT_STATE_PATH):
                return
            with open(EXT_STATE_PATH, 'r', encoding='utf-8') as f:
                d = json.load(f)
            self.blogger_token.setText(d.get("blogger_token", ""))
            self.blogger_blog_id.setText(d.get("blogger_blog_id", ""))
            self.wp_site.setText(d.get("wp_site", ""))
            self.wp_user.setText(d.get("wp_user", ""))
            self.wp_pass.setText(d.get("wp_pass", ""))
            self.naver_url.setText(d.get("naver_url", ""))
            self.manual_title.setText(d.get("manual_title", ""))
            self.manual_body.setPlainText(d.get("manual_body", ""))
            self.manual_is_html.setChecked(bool(d.get("manual_is_html", False)))
            self.chk_blogger.setChecked(bool(d.get("chk_blogger", True)))
            self.chk_wordpress.setChecked(bool(d.get("chk_wordpress", True)))
            self.blogger_suffix.setText(d.get("blogger_suffix", ""))
            self.wp_suffix.setText(d.get("wp_suffix", ""))
            self.wp_include_source.setChecked(bool(d.get("wp_include_source", False)))
            self.src_tabs.setCurrentIndex(int(d.get("src_tab", 0)))
            self._log("💾 확장 설정 복원 완료")
        except Exception as e:
            self._log(f"⚠️ 확장 설정 복원 실패: {e}")

    def _build_source_post(self):
        transformer = ExtPostTransformer()
        if self.src_tabs.currentIndex() == 0:
            url = self.naver_url.text().strip()
            if not url:
                raise ValueError("네이버 URL을 입력하세요.")
            self._log(f"🔗 네이버 본문 수집: {url}")
            importer = ExtNaverBlogImporter()
            return importer.fetch(url)
        else:
            title = self.manual_title.text().strip()
            body = self.manual_body.toPlainText()
            if not title or not body.strip():
                raise ValueError("제목과 본문을 모두 입력하세요.")
            if self.manual_is_html.isChecked():
                html = body
                from bs4 import BeautifulSoup
                text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
            else:
                text = body
                html = transformer.text_to_html(body)
            return ExtSourcePost(source_url=None, title=title, html=html, text=text)

    def _render_preview(self, post):
        """생성 전(원본) / 생성 후(발행 변환본)을 미리보기 영역에 표시."""
        try:
            transformer = ExtPostTransformer()
            before_html = (
                f"<h2>{_ext_html_lib.escape(post.title or '')}</h2>\n{post.html or ''}"
            )
            after_inner = transformer.clean_html(post.html or "")
            after_html = (
                f"<h2>{_ext_html_lib.escape(post.title or '')}</h2>\n{after_inner}"
            )
            self.preview_before.setHtml(before_html)
            self.preview_after.setHtml(after_html)
        except Exception as e:
            self._log(f"⚠️ 미리보기 표시 실패: {e}")

    def _on_preview(self):
        try:
            self.btn_preview.setEnabled(False)
            self._log("🔍 미리보기 생성 중...")
            post = self._build_source_post()
            self._render_preview(post)
            self._log(
                f"👀 미리보기 완료 — 제목: {post.title} / "
                f"본문 {len(post.text)}자 / 이미지 {len(post.images)}개"
            )
        except Exception as e:
            self._log(f"❌ 미리보기 오류: {e}")
            QMessageBox.critical(self, "미리보기 오류", str(e))
        finally:
            self.btn_preview.setEnabled(True)

    def _on_run(self):
        try:
            self.btn_run.setEnabled(False)
            self._log("=" * 50)
            post = self._build_source_post()
            self._log(f"📰 제목: {post.title}")
            self._log(f"📝 본문 길이: {len(post.text)}자 / 이미지 {len(post.images)}개")
            self._render_preview(post)

            transformer = ExtPostTransformer()

            if self.chk_blogger.isChecked():
                token = self.blogger_token.text().strip()
                blog_id = self.blogger_blog_id.text().strip()
                if not token or not blog_id:
                    self._log("⚠️ Blogger 인증 정보 부족 → 건너뜀")
                else:
                    title = f"{post.title}{self.blogger_suffix.text()}".strip()
                    html = transformer.clean_html(post.html)
                    self._log("📤 Blogger 초안 업로드 중...")
                    client = ExtBloggerPublisher(access_token=token, blog_id=blog_id)
                    res = client.create_post(title=title, html=html, is_draft=True)
                    self._log(f"✅ Blogger 완료: id={res.get('id')} url={res.get('url','-')}")

            if self.chk_wordpress.isChecked():
                site = self.wp_site.text().strip()
                user = self.wp_user.text().strip()
                pw = self.wp_pass.text().strip()
                if not site or not user or not pw:
                    self._log("⚠️ WordPress 인증 정보 부족 → 건너뜀")
                else:
                    title = f"{post.title}{self.wp_suffix.text()}".strip()
                    html = transformer.clean_html(post.html)
                    if self.wp_include_source.isChecked() and post.source_url:
                        html += (
                            f'\n<p><em>원문 출처: '
                            f'<a href="{post.source_url}" target="_blank" rel="noopener noreferrer">'
                            f'{post.source_url}</a></em></p>'
                        )
                    self._log("📤 WordPress 초안 업로드 중...")
                    client = ExtWordPressPublisher(site_url=site, username=user, app_password=pw)
                    res = client.create_post(
                        title=title, html=html, status="draft",
                        slug=transformer.slugify(title),
                        excerpt=transformer.make_excerpt(post.text),
                    )
                    self._log(f"✅ WordPress 완료: id={res.get('id')} link={res.get('link','-')}")

            self._log("🎉 작업 완료")
            QMessageBox.information(self, "완료", "초안 업로드가 완료되었습니다.")
        except Exception as e:
            self._log(f"❌ 오류: {e}")
            QMessageBox.critical(self, "오류", str(e))
        finally:
            self.btn_run.setEnabled(True)


class PostPro(QMainWindow):
    def __init__(self):
        super().__init__()
        self._is_starting_up = True
        self._is_restoring_state = False
        self.config = self.load_config()
        self.driver = None
        self.worker_threads = []
        self.page_map = {}
        self.setup_ui()
        self.statusBar().showMessage("포스트 PRO v4.4 (v50 LAZY + UNIFIED) - 통합 수집/발행")
        QTimer.singleShot(1500, self._finish_startup)

    def load_config(self):
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _finish_startup(self):
        self._is_starting_up = False

    def _safe_restore_form_state(self):
        """v49: 이벤트 루프 안정 후 호출되는 안전 복원 래퍼."""
        try:
            self.restore_form_state()
        except Exception as e:
            try:
                print(f'[v49] restore_form_state 실패: {e}')
            except Exception:
                pass

    def log(self, msg):
        self.log_text.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())
        if not getattr(self, '_is_starting_up', False) and not getattr(self, '_is_restoring_state', False):
            try:
                QApplication.processEvents()
            except Exception:
                pass

    def setup_ui(self):
        self.setWindowTitle("포스트 PRO v4.4 (v50 LAZY) (유튜브 수집 + 제목 대량 업로드 + 발행)")
        self.setGeometry(50, 50, 1600, 950)

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Base, QColor(45, 45, 45))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(35, 35, 35))
        palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Button, QColor(50, 50, 50))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 215, 0))
        self.setPalette(palette)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMaximumWidth(200)
        self.tree.setMinimumWidth(160)
        self.tree.setStyleSheet("""
            QTreeWidget { background-color:#1e1e1e; color:#fff; border:none; font-size:13px; }
            QTreeWidget::item { padding:6px 4px; }
            QTreeWidget::item:selected { background-color:#3a3a3a; color:#FFD700; }
            QTreeWidget::item:hover { background-color:#2a2a2a; }
        """)
        self.build_sidebar()
        self.tree.itemClicked.connect(self.on_tree_click)
        main_layout.addWidget(self.tree, 1)

        scroll = QScrollArea()
        self.stack = QStackedWidget()
        scroll.setWidget(self.stack)
        scroll.setWidgetResizable(True)
        main_layout.addWidget(scroll, 5)

        log_w = QWidget()
        log_l = QVBoxLayout(log_w)
        lbl = QLabel("📋 실시간 로그")
        lbl.setStyleSheet("color:#FFD700; font-weight:bold; font-size:14px;")
        log_l.addWidget(lbl)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("background-color:#1a1a1a; color:#aaa; font-size:12px;")
        log_l.addWidget(self.log_text)
        main_layout.addWidget(log_w, 2)

        self.create_pages()
        self.tree.expandAll()
        self.stack.setCurrentIndex(0)

    def build_sidebar(self):
        # ─── 💾 설정 ───
        QTreeWidgetItem(self.tree, ["💾 설정"]).setData(0, Qt.ItemDataRole.UserRole, "config")

        # ─── 🔍 키워드 ───
        QTreeWidgetItem(self.tree, ["🔍 키워드"]).setData(0, Qt.ItemDataRole.UserRole, "keyword")

        # ─── 📥 통합 수집/발행 (모든 수집 페이지를 하나로) ───
        QTreeWidgetItem(self.tree, ["📥 통합 수집/발행"]).setData(0, Qt.ItemDataRole.UserRole, "unified")

        # ─── 📋 대행/후기성 (단일 진입점, 5개 카테고리는 페이지 상단에서 선택) ───
        QTreeWidgetItem(self.tree, ["📋 대행/후기성"]).setData(0, Qt.ItemDataRole.UserRole, "agency_category")

        # ─── 🔄 멀티 플랫폼 변형 업로드 ───
        QTreeWidgetItem(self.tree, ["🔄 멀티 플랫폼"]).setData(0, Qt.ItemDataRole.UserRole, "multi_platform")

        # ─── 🖼️ 이미지 생성 ───
        QTreeWidgetItem(self.tree, ["🖼️ 이미지 생성"]).setData(0, Qt.ItemDataRole.UserRole, "imagegen")

        # ─── 🚀 자동 발행 ───
        QTreeWidgetItem(self.tree, ["🚀 자동 발행"]).setData(0, Qt.ItemDataRole.UserRole, "publish")

        # ─── 🧩 확장 (Blogger / WordPress) ───
        QTreeWidgetItem(self.tree, ["🧩 확장"]).setData(0, Qt.ItemDataRole.UserRole, "extension")

    def _make_start_placeholder(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 24, 24, 24)
        title = QLabel("포스트 PRO 시작 화면")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFD700;")
        desc = QLabel("v50 LAZY MODE: 시작 시 페이지를 미리 만들지 않고, 클릭한 페이지만 로드합니다.")
        desc.setStyleSheet("color:#bbb; font-size:13px;")
        desc.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(desc)
        lay.addStretch()
        return w

    def _create_page_factories(self):
        return {
            "unified": lambda: UnifiedCollectPage(self),
            "agency_category":   lambda: AgencyCategoryPage(self),
            "agency_wizard":     lambda: AgencyWizardPage(self),
            "agency_keyword":    lambda: AgencyKeywordPage(self),
            "agency_morphology": lambda: AgencyMorphologyPage(self),
            "agency_image":      lambda: AgencyImagePage(self),
            "agency_upload":     lambda: AgencyUploadPage(self),
            "config": lambda: ConfigPage(self),
            "keyword": lambda: KeywordAnalysisPage(self),
            "blog": lambda: BlogPipelinePage(self),
            "news_short": lambda: NewsCollectPage(self, "shortnews"),
            "news_daum": lambda: NewsCollectPage(self, "daum"),
            "news_nate": lambda: NewsCollectPage(self, "nate"),
            "cafe": lambda: CafeCollectPage(self),
            "popular": lambda: PopularPostPage(self),
            "bulk_title": lambda: BulkTitleUploadPage(self),
            "youtube": lambda: YouTubeCollectPage(self),
            "multilink_ai": (lambda: MultiLinkAIPage(self)) if _MULTILINK_AVAILABLE else (lambda: None),
            "multi_platform": lambda: MultiPlatformPage(self),
            "imagegen": lambda: ImageGenPage(self),
            "publish": lambda: PublishPage(self),
            "extension": lambda: ExtensionPage(self),
            # ─── 🤖 AI 뉴스 봇 ───
            "salgoo_config":  lambda: SalgooConfigPage(self),
            "salgoo_news":    lambda: SalgooNewsCollectPage(self),
            "salgoo_rewrite": lambda: SalgooAIRewritePage(self),
            "salgoo_publish": lambda: SalgooPublishPage(self),
        }

    def get_or_create_page(self, key):
        """캐시된 페이지 인스턴스를 반환하거나, 없으면 팩토리로 생성한다.
        main_win.stack에는 추가하지 않으므로 외부에서 reparent 가능."""
        page = getattr(self, 'pages', {}).get(key) if hasattr(self, 'pages') else None
        if page is not None:
            return page
        factory = self.page_factories.get(key) if hasattr(self, 'page_factories') else None
        if not factory:
            return None
        print(f'[v50] lazy loading page (shared): {key}')
        page = factory()
        if page is None:
            return None
        if hasattr(self, 'pages'):
            self.pages[key] = page
        return page

    def _ensure_page_loaded(self, key):
        if key in self.page_map:
            return self.page_map[key]
        page = self.get_or_create_page(key)
        if page is None:
            return None
        idx = self.stack.addWidget(page)
        self.page_map[key] = idx
        return idx

    def create_pages(self):
        self.pages = {
            "unified": None,
            "agency_category":   None,
            "agency_wizard":     None,
            "agency_keyword":    None,
            "agency_morphology": None,
            "agency_image":      None,
            "agency_upload":     None,
            "config": None,
            "keyword": None,
            "blog": None,
            "news_short": None,
            "news_daum": None,
            "news_nate": None,
            "cafe": None,
            "popular": None,
            "bulk_title": None,
            "youtube": None,
            "multilink_ai": None,
            "multi_platform": None,
            "imagegen": None,
            "publish": None,
            "extension": None,
            "salgoo_config":  None,
            "salgoo_news":    None,
            "salgoo_rewrite": None,
            "salgoo_publish": None,
        }
        self.page_factories = self._create_page_factories()
        self.stack.addWidget(self._make_start_placeholder())
        self.stack.setCurrentIndex(0)

        # 저장된 폼 상태 복원 — v50 LAZY MODE: 기본 비활성화
        try:
            import os as _os
            if _os.path.exists('.enable_restore'):
                QTimer.singleShot(2500, self._safe_restore_form_state)
                print('[v50] form_state 복원 예약됨 (.enable_restore 감지)')
            else:
                print('[v50] form_state 자동 복원 비활성 (.enable_restore 없음)')
        except Exception as _e:
            print(f'[v50] restore guard error: {_e}')

        # 30초마다 자동 저장 — 시작 5초 후부터 작동
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.timeout.connect(self.save_form_state)
        QTimer.singleShot(5000, lambda: self.auto_save_timer.start(30000))

    def on_tree_click(self, item, col):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key:
            idx = self._ensure_page_loaded(key)
            if idx is not None:
                self.stack.setCurrentIndex(idx)
                self.log(f"📂 {item.text(0)}")

    def save_form_state(self):
        """모든 페이지의 입력 상태를 파일로 저장"""
        state = {}
        try:
            blog = self.pages.get('blog')
            if blog:
                state['blog'] = {
                    'keywords_input': blog.keywords_input.text(),
                    'extract_count': blog.extract_count.value(),
                    'sort_type': blog.sort_type.currentIndex(),
                    'ai_service': blog.ai_service.currentIndex(),
                    'char_count': blog.char_count.value(),
                    'ai_prompt': blog.ai_prompt.toPlainText(),
                    'opt_map': blog.opt_map.isChecked(),
                    'opt_video': blog.opt_video.isChecked(),
                    'opt_quote': blog.opt_quote.isChecked(),
                    'opt_photo': blog.opt_photo.isChecked(),
                    'opt_hashtag': blog.opt_hashtag.isChecked(),
                    'save_mode': blog.save_mode.currentIndex(),
                    'schedule_time': blog.schedule_time.text(),
                    'crawl_result': blog.crawl_result.toPlainText(),
                    'ai_input': blog.ai_input.toPlainText(),
                    'ai_output': blog.ai_output.toPlainText(),
                    # 키워드 테이블
                    'keyword_rows': [
                        [blog.keyword_table.item(r, c).text() if blog.keyword_table.item(r, c) else ''
                         for c in range(blog.keyword_table.columnCount())]
                        for r in range(blog.keyword_table.rowCount())
                    ],
                    # 생성된 원고 보존
                    'generated_articles': blog.generated_articles if hasattr(blog, 'generated_articles') else [],
                }

            keyword_page = self.pages.get('keyword')
            if keyword_page:
                state['keyword'] = {
                    'seed_keywords': keyword_page.seed_keywords.toPlainText(),
                    'result': keyword_page.result.toPlainText(),
                }

            for news_key in ['news_short', 'news_daum', 'news_nate']:
                news = self.pages.get(news_key)
                if news:
                    state[news_key] = {
                        'crawl_url': news.crawl_url.text(),
                        'collect_count': news.collect_count.value(),
                        'keywords_filter': news.keywords_filter.text(),
                        'ai_prompt': news.ai_prompt.toPlainText(),
                        'ai_model': news.ai_model.currentIndex(),
                        'char_count': news.char_count.value(),
                        'collect_result': news.collect_result.toPlainText(),
                        'ai_input': news.ai_input.toPlainText(),
                        'ai_output': news.ai_output.toPlainText(),
                        'account_combo': news.account_combo.currentText() if hasattr(news, 'account_combo') else '',
                        'blog_folder': news.blog_folder.currentText() if hasattr(news, 'blog_folder') else '',
                        'save_mode': news.save_mode.currentIndex() if hasattr(news, 'save_mode') else 0,
                        'generated_articles': news.generated_articles if hasattr(news, 'generated_articles') else [],
                    }

            cafe = self.pages.get('cafe')
            if cafe:
                state['cafe'] = {
                    'cafe_url': cafe.cafe_url.text(),
                    'cafe_id': cafe.cafe_id,
                    'category_index': cafe.category_combo.currentIndex(),
                    'page_start': cafe.page_start.value(),
                    'page_end': cafe.page_end.value(),
                    'ai_prompt': cafe.ai_prompt.toPlainText(),
                    'ai_model': cafe.ai_model.currentIndex(),
                    'collect_result': cafe.collect_result.toPlainText(),
                    'ai_input': cafe.ai_input.toPlainText(),
                    'ai_output': cafe.ai_output.toPlainText(),
                    'account_combo': cafe.account_combo.currentText() if hasattr(cafe, 'account_combo') else '',
                    'blog_folder': cafe.blog_folder.currentText() if hasattr(cafe, 'blog_folder') else '',
                    'save_mode': cafe.save_mode.currentIndex() if hasattr(cafe, 'save_mode') else 0,
                    'generated_articles': cafe.generated_articles if hasattr(cafe, 'generated_articles') else [],
                }

            popular = self.pages.get('popular')
            if popular:
                state['popular'] = {
                    'blog_urls': popular.popular_blog_urls.toPlainText(),
                    'popular_count': popular.popular_count.value(),
                    'ai_prompt': popular.ai_prompt.toPlainText(),
                    'ai_model': popular.ai_model.currentIndex(),
                    'char_count': popular.char_count.value(),
                    'popular_result': popular.popular_result.toPlainText(),
                    'ai_input': popular.ai_input.toPlainText(),
                    'ai_output': popular.ai_output.toPlainText(),
                    'account_combo': popular.account_combo.currentText() if hasattr(popular, 'account_combo') else '',
                    'blog_folder': popular.blog_folder.currentText() if hasattr(popular, 'blog_folder') else '',
                    'save_mode': popular.save_mode.currentIndex() if hasattr(popular, 'save_mode') else 0,
                    'generated_articles': popular.generated_articles if hasattr(popular, 'generated_articles') else [],
                }

            with open('form_state.json', 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"폼 상태 저장 실패: {e}")

    def restore_form_state(self):
        """저장된 폼 상태 복원 (시작 크래시 방지를 위해 안전모드 복원)"""
        try:
            with open('form_state.json', 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            return

        self._is_restoring_state = True
        try:
            blog_state = state.get('blog', {})
            blog = self.pages.get('blog')
            if blog and blog_state:
                blog.keywords_input.setText(blog_state.get('keywords_input', ''))
                blog.extract_count.setValue(blog_state.get('extract_count', 5))
                blog.sort_type.setCurrentIndex(blog_state.get('sort_type', 0))
                blog.ai_service.setCurrentIndex(blog_state.get('ai_service', 0))
                blog.char_count.setValue(blog_state.get('char_count', 2000))
                blog.ai_prompt.setPlainText(blog_state.get('ai_prompt', blog.ai_prompt.toPlainText()))
                blog.opt_map.setChecked(blog_state.get('opt_map', False))
                blog.opt_video.setChecked(blog_state.get('opt_video', False))
                blog.opt_quote.setChecked(blog_state.get('opt_quote', True))
                blog.opt_photo.setChecked(blog_state.get('opt_photo', True))
                blog.opt_hashtag.setChecked(blog_state.get('opt_hashtag', True))
                blog.save_mode.setCurrentIndex(blog_state.get('save_mode', 0))
                blog.schedule_time.setText(blog_state.get('schedule_time', ''))
                blog.crawl_result.setPlainText(blog_state.get('crawl_result', ''))
                blog.ai_input.setPlainText(blog_state.get('ai_input', ''))
                blog.ai_output.setPlainText(blog_state.get('ai_output', ''))

                for row_data in blog_state.get('keyword_rows', []):
                    r = blog.keyword_table.rowCount()
                    blog.keyword_table.insertRow(r)
                    for c, val in enumerate(row_data):
                        blog.keyword_table.setItem(r, c, QTableWidgetItem(val))


                # 생성된 원고 복원
                saved_articles = blog_state.get("generated_articles", [])
                if saved_articles and hasattr(blog, "generated_articles"):
                    blog.generated_articles = saved_articles
                    blog._update_article_table()
                    print(f"[v49] AI글쓰기 원고 {len(saved_articles)}건 복원")
            kw_state = state.get('keyword', {})
            keyword_page = self.pages.get('keyword')
            if keyword_page and kw_state:
                keyword_page.seed_keywords.setPlainText(kw_state.get('seed_keywords', ''))
                keyword_page.result.setPlainText(kw_state.get('result', ''))

            for news_key in ['news_short', 'news_daum', 'news_nate']:
                ns = state.get(news_key, {})
                news = self.pages.get(news_key)
                if news and ns:
                    news.crawl_url.setText(ns.get('crawl_url', news.crawl_url.text()))
                    news.collect_count.setValue(ns.get('collect_count', 10))
                    news.keywords_filter.setText(ns.get('keywords_filter', ''))
                    news.ai_prompt.setPlainText(ns.get('ai_prompt', news.ai_prompt.toPlainText()))
                    news.ai_model.setCurrentIndex(ns.get('ai_model', 0))
                    news.char_count.setValue(ns.get('char_count', 2000))
                    news.collect_result.setPlainText(ns.get('collect_result', ''))
                    news.ai_input.setPlainText(ns.get('ai_input', ''))
                    news.ai_output.setPlainText(ns.get('ai_output', ''))
                    saved_articles = ns.get('generated_articles', [])
                    if saved_articles and hasattr(news, 'generated_articles'):
                        news.generated_articles = saved_articles
                        news._update_article_table()
                    if hasattr(news, 'account_combo'):
                        saved_account = ns.get('account_combo', '')
                        if saved_account:
                            # 시작 직후 account/blog_folder 자동 복원은 비활성화 (0xC0000409 방지)
                            pass
                    if hasattr(news, 'save_mode'):
                        news.save_mode.setCurrentIndex(ns.get('save_mode', 0))
                    if hasattr(news, 'blog_folder'):
                        saved_folder = ns.get('blog_folder', '')
                        if saved_folder:
                            pass

            cafe_state = state.get('cafe', {})
            cafe = self.pages.get('cafe')
            if cafe and cafe_state:
                cafe.cafe_url.setText(cafe_state.get('cafe_url', ''))
                cafe.cafe_id = cafe_state.get('cafe_id', None)
                cafe.page_start.setValue(cafe_state.get('page_start', 1))
                cafe.page_end.setValue(cafe_state.get('page_end', 3))
                cafe.ai_prompt.setPlainText(cafe_state.get('ai_prompt', cafe.ai_prompt.toPlainText()))
                cafe.ai_model.setCurrentIndex(cafe_state.get('ai_model', 0))
                cafe.collect_result.setPlainText(cafe_state.get('collect_result', ''))
                cafe.ai_input.setPlainText(cafe_state.get('ai_input', ''))
                cafe.ai_output.setPlainText(cafe_state.get('ai_output', ''))
                saved_cafe_articles = cafe_state.get('generated_articles', [])
                if saved_cafe_articles and hasattr(cafe, 'generated_articles'):
                    cafe.generated_articles = saved_cafe_articles
                    cafe._update_article_table()
                if hasattr(cafe, 'account_combo'):
                    saved_account = cafe_state.get('account_combo', '')
                    if saved_account:
                        pass
                if hasattr(cafe, 'save_mode'):
                    cafe.save_mode.setCurrentIndex(cafe_state.get('save_mode', 0))
                if hasattr(cafe, 'blog_folder'):
                    saved_folder = cafe_state.get('blog_folder', '')
                    if saved_folder:
                        # v49: 시작 직후 콤보박스 setCurrentText 차단 (네이티브 크래시 방지)
                        pass

            pop_state = state.get('popular', {})
            popular = self.pages.get('popular')
            if popular and pop_state:
                popular.popular_blog_urls.setPlainText(pop_state.get('blog_urls', ''))
                popular.popular_count.setValue(pop_state.get('popular_count', 10))
                popular.ai_prompt.setPlainText(pop_state.get('ai_prompt', popular.ai_prompt.toPlainText()))
                popular.ai_model.setCurrentIndex(pop_state.get('ai_model', 0))
                popular.char_count.setValue(pop_state.get('char_count', 2000))
                popular.popular_result.setPlainText(pop_state.get('popular_result', ''))
                popular.ai_input.setPlainText(pop_state.get('ai_input', ''))
                popular.ai_output.setPlainText(pop_state.get('ai_output', ''))
                # 인기글 생성된 원고 복원
                saved_pop_articles = pop_state.get("generated_articles", [])
                if saved_pop_articles and hasattr(popular, "generated_articles"):
                    popular.generated_articles = saved_pop_articles
                    popular._update_article_table()
                    print(f"[v49] 인기글 원고 {len(saved_pop_articles)}건 복원")
                if hasattr(popular, 'account_combo'):
                    saved_account = pop_state.get('account_combo', '')
                    if saved_account:
                        pass
                if hasattr(popular, 'save_mode'):
                    popular.save_mode.setCurrentIndex(pop_state.get('save_mode', 0))
                if hasattr(popular, 'blog_folder'):
                    saved_folder = pop_state.get('blog_folder', '')
                    if saved_folder:
                        # v49: 시작 직후 콤보박스 setCurrentText 차단
                        pass

            print("[v49] 이전 작업 상태 복원 완료")
        except Exception as e:
            print(f"폼 상태 복원 실패: {e}")
        finally:
            self._is_restoring_state = False

    def closeEvent(self, event):
        """앱 종료 시 폼 상태 자동 저장"""
        self.save_form_state()
        event.accept()


# ════════════════════════════════════════════════════════════════
# 📥 제목 대량 업로드 페이지 (v34 신규)
# ════════════════════════════════════════════════════════════════

# ========================================================================
# v42: YouTube 수집 → GPT 원고 → 발행
# ========================================================================
# === YouTube 수집 페이지 (v42 추가) ===
# 자막 추출 → GPT 원고 생성 → 엑셀 저장 / 바로 발행
# 입력 모드: ① URL 직접 ② 엑셀 업로드 ③ 키워드 검색(YouTube Data API)

import re
import os
import json
import time
import traceback
from urllib.parse import urlparse, parse_qs

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTextEdit, QSpinBox, QComboBox, QFileDialog, QMessageBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox, QTabWidget,
    QProgressBar, QAbstractItemView
)


def _extract_video_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        return ""
    # 이미 11자리 ID
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    try:
        u = urlparse(s)
        host = (u.netloc or "").lower()
        if "youtu.be" in host:
            return u.path.lstrip("/").split("/")[0]
        if "youtube.com" in host:
            if u.path.startswith("/shorts/"):
                return u.path.split("/")[2]
            if u.path.startswith("/live/"):
                return u.path.split("/")[2]
            if u.path.startswith("/embed/"):
                return u.path.split("/")[2]
            qs = parse_qs(u.query)
            if "v" in qs:
                return qs["v"][0]
    except Exception:
        pass
    m = re.search(r"([A-Za-z0-9_-]{11})", s)
    return m.group(1) if m else ""


def _parse_subtitle_blob(raw: str, ext: str = "") -> str:
    """json3 / vtt / srv3·xml → 평문"""
    import json as _json, re as _re, html as _html
    try:
        data = _json.loads(raw)
        if isinstance(data, dict) and "events" in data:
            parts = []
            for ev in data["events"]:
                for s in (ev.get("segs") or []):
                    t = s.get("utf8")
                    if t and t != "\n":
                        parts.append(t)
            text = "".join(parts).replace("\n", " ")
            text = _re.sub(r"\s+", " ", text).strip()
            if text: return text
    except Exception:
        pass
    if "WEBVTT" in raw[:200] or ext == "vtt":
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line: continue
            if line.startswith("WEBVTT") or line.startswith("NOTE"): continue
            if "-->" in line: continue
            if _re.match(r"^\d+$", line): continue
            line = _re.sub(r"<[^>]+>", "", line)
            out.append(line)
        text = " ".join(out)
        text = _html.unescape(_re.sub(r"\s+", " ", text)).strip()
        if text: return text
    if "<text" in raw:
        out = _re.findall(r"<text[^>]*>(.*?)</text>", raw, _re.DOTALL)
        text = _html.unescape(" ".join(out))
        text = _re.sub(r"<[^>]+>", "", text)
        text = _re.sub(r"\s+", " ", text).strip()
        if text: return text
    return ""


def _fetch_transcript_ytdlp(video_id: str, langs=("ko", "en", "ja"),
                             browser: str = "chrome", cookiefile: str = "") -> dict:
    """yt-dlp + 브라우저 쿠키로 자막 추출. 로그인 세션 그대로 사용."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp 미설치 → pip install yt-dlp")
    import urllib.request, os as _os

    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(langs),
        "subtitlesformat": "json3/vtt/best",
        "quiet": True,
        "no_warnings": True,
    }
    if cookiefile and _os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile
    elif browser and browser.lower() != "none":
        ydl_opts["cookiesfrombrowser"] = (browser.lower(),)

    last_err = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get("title") or ""
        subs = info.get("subtitles") or {}
        autos = info.get("automatic_captions") or {}
        for pool, kind in ((subs, "manual"), (autos, "auto")):
            for lang in langs:
                if lang in pool and pool[lang]:
                    track = None
                    for t in pool[lang]:
                        if t.get("ext") == "json3":
                            track = t; break
                    if track is None:
                        track = pool[lang][0]
                    try:
                        req = urllib.request.Request(track["url"],
                            headers={"User-Agent": "Mozilla/5.0"})
                        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
                    except Exception as e:
                        last_err = e; continue
                    text = _parse_subtitle_blob(raw, track.get("ext", ""))
                    if text:
                        return {"text": text, "lang": f"{lang}({kind})", "title": title}
        raise RuntimeError(f"자막 트랙 없음 (subs={list(subs.keys())}, autos={list(autos.keys())})")
    except Exception as e:
        last_err = e
        raise RuntimeError(f"yt-dlp 자막 실패: {last_err}")


def _fetch_transcript(video_id: str, langs=("ko", "en", "ja", "zh-Hans", "zh-Hant"),
                      browser: str = "chrome", cookiefile: str = "") -> dict:
    """1차: yt-dlp(브라우저 쿠키) / 2차: youtube-transcript-api 폴백."""
    last_err = None
    try:
        return _fetch_transcript_ytdlp(video_id, langs=langs, browser=browser, cookiefile=cookiefile)
    except Exception as e:
        last_err = e

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        raise RuntimeError(f"자막 실패: {last_err}")

    def _join(items):
        out = []
        for it in items:
            t = it.get("text") if isinstance(it, dict) else getattr(it, "text", "")
            if t: out.append(t)
        return " ".join(out)

    try:
        api = YouTubeTranscriptApi()
        try:
            tlist = api.list(video_id)
        except AttributeError:
            tlist = api.list_transcripts(video_id)
        for lang in langs:
            try:
                txt = _join(tlist.find_manually_created_transcript([lang]).fetch())
                if txt: return {"text": txt, "lang": lang}
            except Exception as e: last_err = e
        for lang in langs:
            try:
                txt = _join(tlist.find_generated_transcript([lang]).fetch())
                if txt: return {"text": txt, "lang": lang}
            except Exception as e: last_err = e
        for t in tlist:
            try:
                if getattr(t, "is_translatable", False):
                    txt = _join(t.translate("ko").fetch())
                    if txt: return {"text": txt, "lang": f"{t.language_code}->ko"}
            except Exception as e: last_err = e
        try:
            txt = _join(api.fetch(video_id, languages=list(langs)))
            if txt: return {"text": txt, "lang": "auto"}
        except Exception as e: last_err = e
    except Exception as e:
        last_err = e

    try:
        items = YouTubeTranscriptApi.get_transcript(video_id, languages=list(langs))  # type: ignore
        txt = _join(items)
        if txt: return {"text": txt, "lang": "auto"}
    except Exception as e:
        last_err = e

    raise RuntimeError(f"자막 없음/추출 실패: {last_err}")



def _yt_search(api_key: str, query: str, max_results: int = 10) -> list:
    """YouTube Data API v3 search → [{'id':..,'title':..,'url':..}]"""
    import requests
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key, "q": query, "part": "snippet",
        "type": "video", "maxResults": min(max(int(max_results), 1), 50),
        "regionCode": "KR", "relevanceLanguage": "ko",
    }
    r = requests.get(url, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"YouTube API {r.status_code}: {r.text[:200]}")
    data = r.json()
    out = []
    for it in data.get("items", []):
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": it.get("snippet", {}).get("title", ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


# ========= 백그라운드 워커 =========

class _CollectWorker(QThread):
    log_sig = pyqtSignal(str)
    progress_sig = pyqtSignal(int, int)
    item_sig = pyqtSignal(dict)   # {id,url,title,transcript,lang,status,error}
    done_sig = pyqtSignal()

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.items)
        for i, it in enumerate(self.items, 1):
            if self._stop:
                self.log_sig.emit("⏹ 사용자 중지")
                break
            vid = it.get("id") or _extract_video_id(it.get("url", ""))
            title = it.get("title", "") or ""
            row = {"id": vid, "url": it.get("url") or f"https://www.youtube.com/watch?v={vid}",
                   "title": title, "transcript": "", "lang": "", "status": "수집중", "error": ""}
            self.item_sig.emit({**row, "_row_index": i - 1, "_init": True})
            self.progress_sig.emit(i, total)
            self.log_sig.emit(f"[{i}/{total}] 자막 수집: {vid} {title[:40]}")
            if not vid:
                row["status"] = "실패"; row["error"] = "video id 추출 실패"
                self.item_sig.emit({**row, "_row_index": i - 1})
                continue
            try:
                _br = self.cookie_browser.currentText() if hasattr(self, "cookie_browser") else "chrome"
                _cf = self.cookie_file.text().strip() if hasattr(self, "cookie_file") else ""
                tr = _fetch_transcript(vid, browser=_br, cookiefile=_cf)
                row["transcript"] = tr.get("text", "")
                row["lang"] = tr.get("lang", "")
                row["status"] = "수집완료"
                self.log_sig.emit(f"   ✓ {len(row['transcript'])}자 ({row['lang']})")
            except Exception as e:
                row["status"] = "실패"; row["error"] = str(e)[:200]
                self.log_sig.emit(f"   ✗ {row['error']}")
            self.item_sig.emit({**row, "_row_index": i - 1})
            time.sleep(0.4)
        self.done_sig.emit()


class _GenWorker(QThread):
    """Perplexity sonar-pro 기반 유튜브 자막 → 블로그 원고 생성"""
    log_sig = pyqtSignal(str)
    progress_sig = pyqtSignal(int, int)
    item_sig = pyqtSignal(int, dict)
    done_sig = pyqtSignal()

    def __init__(self, rows, prompt_template, char_count, api_key, model="sonar-pro", parent=None):
        super().__init__(parent)
        self.rows = rows  # [(row_index, item)]
        self.prompt_template = prompt_template
        self.char_count = char_count
        self.api_key = api_key
        self.model = model
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.rows)
        for i, (row_index, it) in enumerate(self.rows, 1):
            if self._stop:
                self.log_sig.emit("⏹ 사용자 중지"); break
            self.progress_sig.emit(i, total)
            title = it.get("title", "")
            transcript = it.get("transcript", "")
            if not transcript.strip():
                self.item_sig.emit(row_index, {"status": "실패", "error": "자막 없음", "content": ""})
                continue
            try:
                prompt = self.prompt_template.format(
                    title=title, transcript=transcript[:8000],
                    char_count=self.char_count, url=it.get("url", "")
                )
            except Exception as e:
                # 프롬프트에 잘못된 placeholder가 있어도 안전하게 진행
                prompt = self.prompt_template + "\n\n[자막]\n" + transcript[:8000]
            try:
                # sonar-pro는 medium, sonar는 low로 부하 감소 (멈춤 방지)
                ctx_size = 'low' if self.model == 'sonar' else 'medium'
                text = call_perplexity(
                    self.api_key, prompt, model=self.model,
                    search_context_size=ctx_size, timeout=120.0
                )
                if not text or not text.strip():
                    raise RuntimeError("Perplexity 빈 응답")
                self.item_sig.emit(row_index, {"status": "생성완료", "content": text, "error": ""})
                self.log_sig.emit(f"[{i}/{total}] ✓ 생성 완료 ({len(text)}자)")
            except Exception as e:
                err = str(e)[:200]
                self.item_sig.emit(row_index, {"status": "실패", "error": err})
                self.log_sig.emit(f"[{i}/{total}] ✗ {err}")
            time.sleep(1.0)
        self.done_sig.emit()


# ========= 페이지 위젯 =========


class YouTubeCollectPage(QWidget):
    DEFAULT_PROMPT = (
        "아래는 유튜브 영상 '{title}'의 자막 전문이야.\n"
        "이 내용을 바탕으로 한국어 네이버 블로그 포스트를 작성해.\n\n"
        "조건:\n"
        "- 분량: 약 {char_count}자\n"
        "- 도입부에서 영상 핵심을 후킹하는 한 문단\n"
        "- 본문은 소제목(##)으로 3~5개 섹션 구성, 각 섹션 자연스러운 단락\n"
        "- 자막에 없는 정보는 추측하지 말 것 (사실 기반)\n"
        "- 마지막에 한 줄 요약 + 영상 출처 링크: {url}\n"
        "- 광고/홍보 톤 금지, 정보성 친근한 어조\n\n"
        "[자막]\n{transcript}"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.collected = []   # 각 행: dict(id,url,title,transcript,lang,status,error,content,selected)
        self.collect_worker = None
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("📺 유튜브 자막 수집 (원고 생성은 '유튜브 원고' 탭에서)")
        title.setStyleSheet("color:#FFD700; font-size:18px; font-weight:bold;")
        root.addWidget(title)

        # 입력 모드 탭
        self.input_tabs = QTabWidget()
        self.input_tabs.setStyleSheet("QTabBar::tab{padding:6px 12px;} QTabBar::tab:selected{color:#FFD700;}")

        # ① URL 직접
        w1 = QWidget(); l1 = QVBoxLayout(w1)
        l1.addWidget(QLabel("유튜브 URL을 한 줄에 하나씩 입력 (숏/롱 자동 인식)"))
        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("https://www.youtube.com/watch?v=xxxx\nhttps://youtu.be/yyyy\nhttps://www.youtube.com/shorts/zzzz")
        self.url_input.setMaximumHeight(120)
        l1.addWidget(self.url_input)
        self.input_tabs.addTab(w1, "① URL 직접")

        # ② 엑셀 업로드
        w2 = QWidget(); l2 = QVBoxLayout(w2)
        l2.addWidget(QLabel("엑셀 컬럼: 'URL' 또는 'url' / 선택적으로 '제목'"))
        h2 = QHBoxLayout()
        self.excel_path = QLineEdit(); self.excel_path.setReadOnly(True)
        btn_excel = QPushButton("📂 엑셀 선택"); btn_excel.clicked.connect(self._pick_excel)
        btn_tpl = QPushButton("📥 양식 다운로드"); btn_tpl.clicked.connect(self._download_template)
        h2.addWidget(self.excel_path); h2.addWidget(btn_excel); h2.addWidget(btn_tpl)
        l2.addLayout(h2)
        self.input_tabs.addTab(w2, "② 엑셀 업로드")

        # ③ 키워드 검색
        w3 = QWidget(); l3 = QVBoxLayout(w3)
        l3.addWidget(QLabel("YouTube Data API v3 키워드 검색 → 상위 N개 자동 수집"))
        h3a = QHBoxLayout()
        h3a.addWidget(QLabel("API Key:"))
        self.yt_api_key = QLineEdit()
        self.yt_api_key.setPlaceholderText("AIza... (Google Cloud Console에서 발급)")
        self.yt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.yt_api_key.setText(self._load_api_key())
        h3a.addWidget(self.yt_api_key)
        btn_save_key = QPushButton("💾"); btn_save_key.setFixedWidth(40)
        btn_save_key.clicked.connect(self._save_api_key)
        h3a.addWidget(btn_save_key)
        l3.addLayout(h3a)
        h3b = QHBoxLayout()
        h3b.addWidget(QLabel("키워드:"))
        self.kw_query = QLineEdit(); self.kw_query.setPlaceholderText("예: 다이어트 식단 추천")
        h3b.addWidget(self.kw_query)
        h3b.addWidget(QLabel("개수:"))
        self.kw_count = QSpinBox(); self.kw_count.setRange(1, 50); self.kw_count.setValue(10)
        h3b.addWidget(self.kw_count)
        btn_search = QPushButton("🔎 검색하여 목록 추가")
        btn_search.clicked.connect(self._search_youtube)
        h3b.addWidget(btn_search)
        l3.addLayout(h3b)
        self.input_tabs.addTab(w3, "③ 키워드 검색")

        root.addWidget(self.input_tabs)

        # 🍪 브라우저 쿠키 옵션 (yt-dlp가 로그인 세션 자동 사용)
        cookie_bar = QHBoxLayout()
        cookie_bar.addWidget(QLabel("🍪 쿠키 사용:"))
        self.cookie_browser = QComboBox()
        self.cookie_browser.addItems(["chrome", "edge", "firefox", "brave", "opera", "safari", "none"])
        self.cookie_browser.setCurrentText("chrome")
        self.cookie_browser.setToolTip("로그인된 브라우저 쿠키 자동 사용 (해당 브라우저는 종료 권장)")
        cookie_bar.addWidget(self.cookie_browser)
        cookie_bar.addWidget(QLabel("또는 cookies.txt:"))
        self.cookie_file = QLineEdit()
        self.cookie_file.setPlaceholderText("(선택) cookies.txt 경로 — 입력 시 우선 사용")
        cookie_bar.addWidget(self.cookie_file)
        btn_cookie = QPushButton("📂"); btn_cookie.setFixedWidth(40)
        btn_cookie.clicked.connect(self._pick_cookie_file)
        cookie_bar.addWidget(btn_cookie)
        root.addLayout(cookie_bar)

        # 수집 버튼
        bar1 = QHBoxLayout()
        self.btn_collect = QPushButton("▶ 자막 수집 시작")
        self.btn_collect.setStyleSheet("background:#1a7f37; color:white; font-weight:bold; padding:8px 14px;")
        self.btn_collect.clicked.connect(self._start_collect)
        self.btn_stop_collect = QPushButton("⏹ 중지")
        self.btn_stop_collect.clicked.connect(self._stop_collect)
        self.btn_clear = QPushButton("🗑 목록 비우기")
        self.btn_clear.clicked.connect(self._clear_table)
        bar1.addWidget(self.btn_collect); bar1.addWidget(self.btn_stop_collect)
        bar1.addStretch(); bar1.addWidget(self.btn_clear)
        root.addLayout(bar1)

        self.progress = QProgressBar(); self.progress.setValue(0)
        root.addWidget(self.progress)

        # 결과 테이블
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["선택", "VID", "제목", "자막(자수)", "언어", "상태", "원고(자수)", "URL"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._open_detail)
        root.addWidget(self.table, 2)

        # ── Perplexity 설정 (생성/발행 통합) ──
        cfg_box = QGroupBox("🤖 Perplexity 원고 생성 설정")
        cfg_l = QVBoxLayout(cfg_box)
        prow1 = QHBoxLayout()
        prow1.addWidget(QLabel("API Key:"))
        self.pplx_key = QLineEdit()
        self.pplx_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.pplx_key.setPlaceholderText("pplx-... (비워두면 환경변수 PERPLEXITY_API_KEY 사용)")
        prow1.addWidget(self.pplx_key, 3)
        btn_save_pkey = QPushButton("💾"); btn_save_pkey.setFixedWidth(40)
        btn_save_pkey.clicked.connect(self._save_pplx_key)
        prow1.addWidget(btn_save_pkey)
        prow1.addWidget(QLabel("모델:"))
        self.pplx_model = QComboBox()
        self.pplx_model.addItems(["sonar-pro", "sonar", "sonar-reasoning-pro", "sonar-reasoning"])
        prow1.addWidget(self.pplx_model)
        prow1.addWidget(QLabel("글자수:"))
        self.char_count = QSpinBox(); self.char_count.setRange(500, 8000); self.char_count.setValue(2000)
        prow1.addWidget(self.char_count)
        cfg_l.addLayout(prow1)

        cfg_l.addWidget(QLabel("프롬프트 (사용 가능한 변수: {title} {transcript} {url} {char_count})"))
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlainText(self.DEFAULT_PROMPT)
        self.prompt_edit.setMaximumHeight(140)
        cfg_l.addWidget(self.prompt_edit)
        root.addWidget(cfg_box)
        self._load_pplx_key()

        # ── 생성 + 발행 액션 바 ──
        action_bar = QHBoxLayout()
        self.btn_gen = QPushButton("🤖 선택 항목 원고 생성 (Perplexity)")
        self.btn_gen.setStyleSheet("background:#0969da; color:white; font-weight:bold; padding:8px 14px;")
        self.btn_gen.clicked.connect(self._start_generate)
        self.btn_stop_gen = QPushButton("⏹ 생성 중지"); self.btn_stop_gen.clicked.connect(self._stop_generate)
        action_bar.addWidget(self.btn_gen); action_bar.addWidget(self.btn_stop_gen)
        action_bar.addStretch()
        root.addLayout(action_bar)

        pub_box = QGroupBox("🚀 발행 / 저장")
        pub_l = QHBoxLayout(pub_box)
        self.cb_save_excel = QCheckBox("📊 엑셀 저장"); self.cb_save_excel.setChecked(True)
        self.cb_publish = QCheckBox("📤 네이버 블로그 자동 발행 (인기글 발행 로직 재사용)")
        pub_l.addWidget(self.cb_save_excel); pub_l.addWidget(self.cb_publish); pub_l.addStretch()
        self.btn_run = QPushButton("▶ 실행")
        self.btn_run.setStyleSheet("background:#bf3989; color:white; font-weight:bold; padding:8px 14px;")
        self.btn_run.clicked.connect(self._run_publish)
        pub_l.addWidget(self.btn_run)
        root.addWidget(pub_box)

    # ---------- API key 저장 ----------
    def _key_path(self):
        return "youtube_api_key.json"

    def _load_api_key(self):
        try:
            with open(self._key_path(), "r", encoding="utf-8") as f:
                return json.load(f).get("key", "")
        except Exception:
            return ""

    def _save_api_key(self):
        try:
            with open(self._key_path(), "w", encoding="utf-8") as f:
                json.dump({"key": self.yt_api_key.text().strip()}, f)
            self._log("✔ YouTube API Key 저장")
        except Exception as e:
            self._log(f"✗ 키 저장 실패: {e}")

    # ---------- 입력 모드 핸들러 ----------
    def _pick_cookie_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "cookies.txt 선택", "", "Cookies (*.txt);;All (*.*)")
        if path:
            self.cookie_file.setText(path)

    def _pick_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 파일 선택", "", "Excel (*.xlsx *.xls)")
        if path:
            self.excel_path.setText(path)
            try:
                import pandas as pd
                df = pd.read_excel(path)
                cols = {c.lower(): c for c in df.columns}
                url_col = cols.get("url") or cols.get("유튜브 url") or cols.get("youtube") or list(df.columns)[0]
                title_col = cols.get("제목") or cols.get("title")
                added = 0
                for _, r in df.iterrows():
                    u = str(r[url_col]).strip()
                    if not u or u == "nan":
                        continue
                    t = str(r[title_col]).strip() if title_col and title_col in df.columns else ""
                    self._add_row({"url": u, "title": t, "id": _extract_video_id(u)})
                    added += 1
                self._log(f"📂 엑셀에서 {added}건 추가")
            except Exception as e:
                QMessageBox.warning(self, "오류", f"엑셀 읽기 실패: {e}")

    def _download_template(self):
        try:
            import pandas as pd
            df = pd.DataFrame([
                {"URL": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "제목": "예시 영상"},
                {"URL": "https://youtu.be/abcdefghijk", "제목": ""},
            ])
            path, _ = QFileDialog.getSaveFileName(self, "양식 저장", "youtube_url_template.xlsx", "Excel (*.xlsx)")
            if path:
                df.to_excel(path, index=False)
                self._log(f"📥 양식 저장: {path}")
        except Exception as e:
            QMessageBox.warning(self, "오류", str(e))

    def _search_youtube(self):
        api_key = self.yt_api_key.text().strip()
        q = self.kw_query.text().strip()
        if not api_key:
            QMessageBox.warning(self, "안내", "YouTube Data API Key를 입력하세요"); return
        if not q:
            QMessageBox.warning(self, "안내", "검색 키워드를 입력하세요"); return
        try:
            results = _yt_search(api_key, q, self.kw_count.value())
            for r in results:
                self._add_row(r)
            self._log(f"🔎 '{q}' → {len(results)}건 추가")
        except Exception as e:
            QMessageBox.warning(self, "오류", str(e))

    # ---------- 테이블 ----------
    def _add_row(self, item: dict):
        row = {
            "id": item.get("id") or _extract_video_id(item.get("url", "")),
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "transcript": "", "lang": "", "status": "대기",
            "error": "", "content": "", "selected": True,
        }
        self.collected.append(row)
        self._refresh_row(len(self.collected) - 1)

    def _refresh_row(self, idx: int):
        if idx >= len(self.collected):
            return
        row = self.collected[idx]
        if self.table.rowCount() <= idx:
            self.table.insertRow(idx)
        cb = QCheckBox(); cb.setChecked(row.get("selected", True))
        cb.stateChanged.connect(lambda s, i=idx: self._toggle_select(i, s))
        self.table.setCellWidget(idx, 0, cb)
        self.table.setItem(idx, 1, QTableWidgetItem(row["id"]))
        self.table.setItem(idx, 2, QTableWidgetItem(row["title"]))
        self.table.setItem(idx, 3, QTableWidgetItem(str(len(row.get("transcript", "")))))
        self.table.setItem(idx, 4, QTableWidgetItem(row.get("lang", "")))
        st = row.get("status", "")
        if row.get("error"):
            st += f" ({row['error'][:30]})"
        self.table.setItem(idx, 5, QTableWidgetItem(st))
        self.table.setItem(idx, 6, QTableWidgetItem(str(len(row.get("content", "")))))
        self.table.setItem(idx, 7, QTableWidgetItem(row.get("url", "")))

    def _toggle_select(self, idx, state):
        if idx < len(self.collected):
            self.collected[idx]["selected"] = bool(state)

    def _open_detail(self, row, col):
        if row >= len(self.collected): return
        it = self.collected[row]
        msg = f"제목: {it.get('title','')}\nURL: {it.get('url','')}\n언어: {it.get('lang','')}\n\n"
        msg += f"=== 자막({len(it.get('transcript',''))}자) ===\n{it.get('transcript','')[:2000]}\n\n"
        msg += f"=== 생성 원고({len(it.get('content',''))}자) ===\n{it.get('content','')[:2000]}"
        dlg = QMessageBox(self); dlg.setWindowTitle("상세"); dlg.setText(msg); dlg.exec()

    def _clear_table(self):
        self.collected.clear()
        self.table.setRowCount(0)

    # ---------- 수집 ----------
    def _start_collect(self):
        # URL 탭의 텍스트도 합산
        if self.input_tabs.currentIndex() == 0:
            urls = [u.strip() for u in self.url_input.toPlainText().splitlines() if u.strip()]
            for u in urls:
                self._add_row({"url": u, "title": "", "id": _extract_video_id(u)})
            self.url_input.clear()

        targets = [r for r in self.collected if r.get("selected") and r.get("status") in ("대기", "실패")]
        if not targets:
            QMessageBox.information(self, "안내", "수집할 항목이 없습니다 (선택된 '대기' 행)"); return

        self._log(f"▶ 자막 수집 시작 ({len(targets)}건)")
        self.collect_worker = _CollectWorker(targets)
        self.collect_worker.log_sig.connect(self._log)
        self.collect_worker.progress_sig.connect(self._on_progress)
        self.collect_worker.item_sig.connect(self._on_collect_item)
        self.collect_worker.done_sig.connect(lambda: self._log("✅ 자막 수집 완료"))
        self.collect_worker.start()

    def _stop_collect(self):
        if self.collect_worker:
            self.collect_worker.stop()

    def _on_progress(self, cur, total):
        self.progress.setMaximum(total); self.progress.setValue(cur)

    def _on_collect_item(self, payload):
        # payload는 _row_index를 가지지만, self.collected의 인덱스와 다를 수 있음
        # → URL/ID로 매칭
        vid = payload.get("id"); url = payload.get("url")
        for i, r in enumerate(self.collected):
            if (vid and r.get("id") == vid) or (url and r.get("url") == url):
                for k in ("transcript", "lang", "status", "error", "title"):
                    if payload.get(k):
                        r[k] = payload[k]
                self._refresh_row(i)
                break


    # ---------- Perplexity 키 저장/로드 ----------
    def _pplx_key_path(self): return "perplexity_yt_key.json"
    def _load_pplx_key(self):
        try:
            with open(self._pplx_key_path(), encoding="utf-8") as f:
                self.pplx_key.setText(json.load(f).get("key", ""))
        except Exception:
            pass
    def _save_pplx_key(self):
        try:
            with open(self._pplx_key_path(), "w", encoding="utf-8") as f:
                json.dump({"key": self.pplx_key.text().strip()}, f)
            self._log("✔ Perplexity 키 저장")
        except Exception as e:
            self._log(f"✗ 키 저장 실패: {e}")
    def _resolve_pplx_key(self) -> str:
        k = self.pplx_key.text().strip()
        if k: return k
        return os.environ.get("PERPLEXITY_API_KEY", "").strip()

    # ---------- 원고 생성 (Perplexity) ----------
    def _start_generate(self):
        api_key = self._resolve_pplx_key()
        if not api_key:
            QMessageBox.warning(self, "안내", "Perplexity API Key를 입력하거나 PERPLEXITY_API_KEY 환경변수를 설정하세요"); return
        # collected 행에서 선택 + 자막 있고 미생성인 것
        rows = []
        for i, r in enumerate(self.collected):
            if not r.get("selected"): continue
            if not r.get("transcript"): continue
            if r.get("content"): continue
            rows.append((i, r))
        if not rows:
            QMessageBox.information(self, "안내", "생성할 항목이 없습니다 (선택 + 자막 있고 원고 미생성)"); return
        model = self.pplx_model.currentText()
        self._log(f"🤖 Perplexity 생성 시작 ({len(rows)}건, {model})")
        self.gen_worker = _GenWorker(
            rows, self.prompt_edit.toPlainText(),
            self.char_count.value(), api_key, model
        )
        self.gen_worker.log_sig.connect(self._log)
        self.gen_worker.progress_sig.connect(lambda c, t: (self.progress.setMaximum(t), self.progress.setValue(c)))
        self.gen_worker.item_sig.connect(self._on_gen_item)
        self.gen_worker.done_sig.connect(lambda: self._log("✅ 원고 생성 완료"))
        self.gen_worker.start()

    def _stop_generate(self):
        if getattr(self, "gen_worker", None):
            self.gen_worker.stop()

    def _on_gen_item(self, idx, partial):
        if not (0 <= idx < len(self.collected)): return
        for k, v in partial.items():
            self.collected[idx][k] = v
        self._refresh_table()

    # ---------- 발행/저장 ----------
    def _run_publish(self):
        targets = [r for r in self.collected if r.get("selected") and r.get("content")]
        if not targets:
            QMessageBox.information(self, "안내", "발행/저장할 생성 완료 항목이 없습니다"); return

        if self.cb_save_excel.isChecked():
            try:
                import pandas as pd
                df_rows = [{
                    "VID": r.get("id",""), "제목": r.get("title",""), "URL": r.get("url",""),
                    "언어": r.get("lang",""), "자막": (r.get("transcript","") or "")[:2000],
                    "본문": r.get("content",""), "상태": r.get("status",""),
                } for r in targets]
                path, _ = QFileDialog.getSaveFileName(
                    self, "엑셀로 저장",
                    f"youtube_perplexity_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", "Excel (*.xlsx)"
                )
                if path:
                    pd.DataFrame(df_rows).to_excel(path, index=False)
                    self._log(f"📊 엑셀 저장: {path}")
            except Exception as e:
                QMessageBox.warning(self, "엑셀 오류", str(e))

        if self.cb_publish.isChecked():
            self._publish_via_popular(targets)

    def _publish_via_popular(self, targets):
        """인기글 페이지의 publish_to_blog 로직을 격리해서 재사용"""
        pop = None
        try:
            pop = self.parent_window.pages.get("popular")
        except Exception:
            pop = None
        if not pop:
            QMessageBox.warning(self, "오류", "인기글 페이지를 찾을 수 없습니다"); return

        backup = list(getattr(pop, "generated_articles", []) or [])
        try:
            payload = []
            for r in targets:
                payload.append({
                    "title": r.get("title") or f"YT_{r.get('id','')}",
                    "content": r.get("content") or "",
                    "keyword": "",
                    "source": r.get("url", ""),
                })
            pop.generated_articles = payload
            self._log(f"📤 발행 큐 주입 → {len(payload)}건")
            pop.publish_to_blog()
        except Exception as e:
            QMessageBox.critical(self, "발행 오류", str(e))
        finally:
            def _restore():
                try:
                    pop.generated_articles = backup
                    self._log("↩️ 인기글 원본 큐 복원")
                except Exception:
                    pass
            QTimer.singleShot(5000, _restore)

    def _log(self, msg):
        try:
            self.parent_window.log(f"[유튜브] {msg}")
        except Exception:
            print(msg)


class BulkTitleUploadPage(QWidget):
    """엑셀로 기사 제목을 대량 업로드 → AI 글쓰기 → 네이버 블로그 발행
    [v38] SQLite 영구 저장 + 인기글수집과 동일한 발행 로직 통합
    """

    def __init__(self, main_win):
        super().__init__()
        self.main = main_win
        self.db = BulkArticleDB()                  # ★ 영구 저장 DB
        self.titles_data = []                      # 화면 표시용 [{id,title1,title2,title3,combined_title,keyword,source,content,status,publish_status,char_count,db_id}]
        self._cancel_generate = False
        self._generating = False
        self._publishing = False
        self._cancel_publish = False
        self.folder_cache = {}
        # 카테고리 캐시 영구 저장 (계정별)
        try:
            import os as _os, json as _json
            self._cat_cache_path = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)) if '__file__' in globals() else '.',
                'bulk_title_category_cache.json'
            )
            if _os.path.exists(self._cat_cache_path):
                with open(self._cat_cache_path, 'r', encoding='utf-8') as _f:
                    self.folder_cache = _json.load(_f) or {}
        except Exception:
            self.folder_cache = {}

        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)

        title = QLabel("📥 URL 대량 업로드 → AI 본문/제목 생성 → 발행  (v58 / URL모드)")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#FFD700;")
        layout.addWidget(title)

        desc = QLabel("프로그램을 껐다 켜도 데이터가 자동 보존됩니다 (data/bulk_articles.db)")
        desc.setStyleSheet("color:#888; margin-bottom:8px;")
        layout.addWidget(desc)

        # ── STEP 1: 엑셀 업로드 ──
        step1 = QGroupBox("STEP 1️⃣  엑셀 파일 업로드")
        s1 = QVBoxLayout(step1)
        info = QLabel(
            "📋 엑셀 양식 (한 행 = 원고 1개) — A·B·C·D 모든 열에 [제목] 또는 [URL] 자유 입력:\n"
            "    A열(필수)  B열(선택)  C열(선택)  D열(선택)  ← 모두 동일하게 처리\n"
            "    • http(s):// 로 시작 → URL 로 인식 → 본문 자동 크롤링\n"
            "    • 그 외 텍스트   → 제목 으로 인식 (AI 가 제목들을 종합해 원고 작성)\n"
            "    • URL 과 제목을 한 행에 섞어 넣어도 됩니다."
        )
        info.setStyleSheet("color:#aaa; padding:6px; background:#2a2a2a; border-radius:4px;")
        s1.addWidget(info)

        btn_row = QHBoxLayout()
        self.btn_template = QPushButton("📄 양식 다운로드")
        self.btn_template.setStyleSheet("background:#4a4a4a; color:#FFD700; padding:6px 12px;")
        self.btn_template.clicked.connect(self.download_template)
        btn_row.addWidget(self.btn_template)
        self.btn_upload = QPushButton("📂 엑셀 불러오기 (추가)")
        self.btn_upload.setStyleSheet("background:#2d5a8a; color:#fff; padding:6px 12px; font-weight:bold;")
        self.btn_upload.clicked.connect(self.upload_excel)
        btn_row.addWidget(self.btn_upload)
        self.btn_clear = QPushButton("🗑 전체 삭제 (DB 포함)")
        self.btn_clear.setStyleSheet("background:#5a2d2d; color:#fff; padding:6px 12px;")
        self.btn_clear.clicked.connect(self.clear_titles)
        btn_row.addWidget(self.btn_clear)
        btn_row.addStretch()
        s1.addLayout(btn_row)

        # 통합 테이블 (제목/원고/상태/발행상태)
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            ["선택", "#", "제목(통합)", "키워드", "글자수", "AI상태", "발행상태", "DB"]
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(260)
        self.table.cellDoubleClicked.connect(self.preview_article)
        s1.addWidget(self.table)

        self.count_label = QLabel("📊 0개 / 완료 0개 / 발행 0개")
        self.count_label.setStyleSheet("color:#FFD700; padding:4px;")
        s1.addWidget(self.count_label)
        layout.addWidget(step1)

        # ── STEP 2: AI 글쓰기 ──
        step2 = QGroupBox("STEP 2️⃣  AI 글쓰기 옵션")
        s2 = QVBoxLayout(step2)
        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("AI 모델:"))
        self.ai_model = QComboBox()
        self.ai_model.addItems([
            "Perplexity (sonar-pro) ⭐ 검색+생성", "Perplexity (sonar)",
            "GPT-4o (chatgpt-4o-latest)", "GPT-4o-mini",
            "Claude 3.5 Sonnet", "Gemini 1.5 Pro",
        ])
        opt_row.addWidget(self.ai_model)
        opt_row.addSpacing(20)
        opt_row.addWidget(QLabel("목표 글자수:"))
        self.char_count = QSpinBox()
        self.char_count.setRange(500, 10000); self.char_count.setValue(2000); self.char_count.setSingleStep(100)
        opt_row.addWidget(self.char_count)
        opt_row.addStretch()
        s2.addLayout(opt_row)

        s2.addWidget(QLabel("AI 프롬프트:"))
        self.ai_prompt = QTextEdit()
        self.ai_prompt.setMaximumHeight(140)
        self.ai_prompt.setPlainText(
            "다음은 같은 사건/주제에 대한 여러 뉴스 기사 제목입니다. "
            "여러 제목을 종합하여 사실관계를 정리하고, SEO에 최적화된 블로그 포스트를 작성해줘.\n\n"
            "[기사 제목들]\n{titles}\n\n"
            "[키워드] {keyword}\n"
            "[참고원문] {source}\n\n"
            "조건:\n"
            "1) 자연스러운 한국어 구어체로 {char_count}자 내외\n"
            "2) H2/H3 소제목 3~5개 포함\n"
            "3) 여러 제목의 정보를 모순 없이 종합\n"
            "4) 마지막에 해시태그 5개"
        )
        s2.addWidget(self.ai_prompt)

        gen_row = QHBoxLayout()
        self.btn_generate_sel = QPushButton("🤖 선택만 AI 생성")
        self.btn_generate_sel.setStyleSheet("background:#2d8a4a; color:#fff; padding:8px 16px;")
        self.btn_generate_sel.clicked.connect(lambda: self.start_generate(only_selected=True))
        gen_row.addWidget(self.btn_generate_sel)

        self.btn_generate = QPushButton("🤖 전체 AI 원고 생성 시작")
        self.btn_generate.setStyleSheet("background:#1f7a3a; color:#fff; padding:8px 16px; font-weight:bold;")
        self.btn_generate.clicked.connect(lambda: self.start_generate(only_selected=False))
        gen_row.addWidget(self.btn_generate)

        self.btn_cancel_gen = QPushButton("⏹ 생성 중지")
        self.btn_cancel_gen.setStyleSheet("background:#8a2d2d; color:#fff; padding:8px 14px;")
        self.btn_cancel_gen.clicked.connect(lambda: setattr(self, '_cancel_generate', True))
        gen_row.addWidget(self.btn_cancel_gen)
        gen_row.addStretch()
        s2.addLayout(gen_row)
        layout.addWidget(step2)

        # ── STEP 3: 발행 (인기글수집과 동일 방식) ──
        step3 = QGroupBox("STEP 3️⃣  네이버 블로그 발행")
        s3 = QVBoxLayout(step3)

        acc_row = QHBoxLayout()
        acc_row.addWidget(QLabel("계정:"))
        self.account_combo = QComboBox(); self.account_combo.setMinimumWidth(200)
        self.account_combo.currentIndexChanged.connect(self._on_account_changed)
        acc_row.addWidget(self.account_combo)

        acc_row.addWidget(QLabel("카테고리:"))
        self.folder_combo = QComboBox(); self.folder_combo.setMinimumWidth(180)
        acc_row.addWidget(self.folder_combo)

        self.btn_load_folders = QPushButton("📁 카테고리 불러오기")
        self.btn_load_folders.setStyleSheet("background:#4a4a4a; color:#FFD700; padding:6px 10px;")
        self.btn_load_folders.clicked.connect(self.load_blog_folders)
        acc_row.addWidget(self.btn_load_folders)
        acc_row.addStretch()
        s3.addLayout(acc_row)

        opt2 = QHBoxLayout()
        opt2.addWidget(QLabel("공개:"))
        self.publish_open = QComboBox()
        self.publish_open.addItems(["전체공개", "이웃공개", "서로이웃공개", "비공개"])
        opt2.addWidget(self.publish_open)
        opt2.addSpacing(20)
        opt2.addWidget(QLabel("발행 간격(초):"))
        self.publish_interval = QSpinBox(); self.publish_interval.setRange(0, 600); self.publish_interval.setValue(60)
        opt2.addWidget(self.publish_interval)
        opt2.addStretch()
        s3.addLayout(opt2)

        pub_row = QHBoxLayout()
        self.btn_publish_sel = QPushButton("🚀 선택 발행")
        self.btn_publish_sel.setStyleSheet("background:#2d6a8a; color:#fff; padding:9px 18px; font-weight:bold;")
        self.btn_publish_sel.clicked.connect(lambda: self.publish_to_blog(only_selected=True))
        pub_row.addWidget(self.btn_publish_sel)

        self.btn_publish_all = QPushButton("🚀 전체 발행 (완료된 원고 전부)")
        self.btn_publish_all.setStyleSheet("background:#1f4a6a; color:#fff; padding:9px 18px; font-weight:bold;")
        self.btn_publish_all.clicked.connect(lambda: self.publish_to_blog(only_selected=False))
        pub_row.addWidget(self.btn_publish_all)

        self.btn_cancel_pub = QPushButton("⏹ 발행 중지")
        self.btn_cancel_pub.setStyleSheet("background:#8a2d2d; color:#fff; padding:9px 14px;")
        self.btn_cancel_pub.clicked.connect(lambda: setattr(self, '_cancel_publish', True))
        pub_row.addWidget(self.btn_cancel_pub)
        pub_row.addStretch()
        s3.addLayout(pub_row)

        self.publish_progress = QProgressBar()
        self.publish_progress.setTextVisible(True)
        s3.addWidget(self.publish_progress)
        layout.addWidget(step3)

        # ── 결과/내보내기 ──
        export_row = QHBoxLayout()
        self.btn_export = QPushButton("💾 결과 엑셀로 내보내기")
        self.btn_export.setStyleSheet("background:#4a4a4a; color:#FFD700; padding:6px 12px;")
        self.btn_export.clicked.connect(self.export_results)
        export_row.addWidget(self.btn_export)
        export_row.addStretch()
        layout.addLayout(export_row)
        layout.addStretch()

        outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidget(scroll_widget); scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        # 초기 데이터 로드 (DB 복원) + 계정 콤보 채우기
        self._reload_accounts()
        self._load_from_db()

    # ─────────────── 공통 유틸 ───────────────
    def _log(self, msg):
        if hasattr(self.main, 'log'):
            self.main.log(f"[제목업로드] {msg}")
        else:
            print(f"[제목업로드] {msg}")

    def _now(self):
        return _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    def _reload_accounts(self):
        self.account_combo.clear()
        try:
            cfg = getattr(self.main, 'config', {}) or {}
            accs = cfg.get('naver_accounts') or []
            for a in accs:
                nid = a.get('id') or a.get('nid') or ''
                if nid:
                    self.account_combo.addItem(nid, nid)
        except Exception as e:
            self._log(f"계정 로드 실패: {e}")

    def _get_selected_account(self):
        cfg = getattr(self.main, 'config', {}) or {}
        nid = self.account_combo.currentData() or self.account_combo.currentText()
        for a in (cfg.get('naver_accounts') or []):
            if (a.get('id') or a.get('nid')) == nid:
                return a
        return None

    def _on_account_changed(self, _idx):
        self.folder_combo.clear()
        # 캐시에 저장된 카테고리가 있으면 즉시 복원
        try:
            nid = self.account_combo.currentData() or self.account_combo.currentText()
            cached = (self.folder_cache or {}).get(nid) or []
            if cached:
                for item in cached:
                    if isinstance(item, dict):
                        self.folder_combo.addItem(item.get('text', ''), item.get('data'))
                    else:
                        self.folder_combo.addItem(str(item))
                self._log(f"📂 저장된 카테고리 자동 적용: {nid} ({len(cached)}개)")
        except Exception as e:
            self._log(f"카테고리 캐시 복원 실패: {e}")

    # ─────────────── DB 동기화 ───────────────
    def _load_from_db(self):
        rows = self.db.all()
        self.titles_data = []
        for r in rows:
            self.titles_data.append({
                "db_id": r["id"],
                "title1": r["title1"], "title2": r["title2"], "title3": r["title3"],
                "combined_title": r["combined_title"] or r["title1"],
                "keyword": r["keyword"], "source": r["source"],
                "content": r["content"],
                "status": r["status"], "publish_status": r["publish_status"],
                "char_count": r["char_count"], "ai_model": r["ai_model"],
            })
        self._refresh_table()
        self._log(f"DB 복원: {len(self.titles_data)}건")

    # ─────────────── 양식 다운로드 ───────────────
    def download_template(self):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            path, _ = QFileDialog.getSaveFileName(
                self, "양식 저장 위치 선택", "대량업로드_양식.xlsx", "Excel Files (*.xlsx)"
            )
            if not path: return
            wb = Workbook(); ws = wb.active; ws.title = "대량 업로드"
            # v60: A/B/C/D 모두 URL 또는 제목 둘 다 허용 (동일 처리)
            ws.append([
                "항목1 (필수, 제목 또는 URL)",
                "항목2 (선택, 제목 또는 URL)",
                "항목3 (선택, 제목 또는 URL)",
                "항목4 (선택, 제목 또는 URL)",
            ])
            for row in [
                # 케이스1: URL 만
                ['https://n.news.naver.com/article/001/0000000001',
                 'https://n.news.naver.com/article/001/0000000002', '', ''],
                # 케이스2: 제목 만
                ["삼성전자, HBM4 양산 본격화",
                 "SK하이닉스도 HBM4 개발 가속",
                 "HBM4 시장 전망", ""],
                # 케이스3: URL + 제목 혼합 (D열에도 자유)
                ['https://www.youtube.com/shorts/LWg9cbX-ZKA',
                 "관련 기사 제목 예시",
                 'https://www.example.com/news/123',
                 "유튜브 숏츠 요약"],
            ]:
                ws.append(row)
            # 헤더 스타일
            for col_idx in range(1, 5):
                cell = ws.cell(row=1, column=col_idx)
                cell.fill = PatternFill("solid",
                    start_color="C00000" if col_idx == 1 else "2F5496")
                cell.font = Font(bold=True, color="FFFFFF")
                cell.alignment = Alignment(horizontal="center",
                    vertical="center", wrap_text=True)
            for col_letter, w in zip("ABCD", [55, 55, 55, 55]):
                ws.column_dimensions[col_letter].width = w
            ws.row_dimensions[1].height = 32
            wb.save(path)
            QMessageBox.information(self, "완료",
                f"양식이 저장되었습니다 (v60):\n{path}\n\n"
                "📌 A·B·C·D 4개 열 모두 [제목] 또는 [URL] 자유 입력 가능.\n"
                "   • http(s):// 로 시작하면 → URL → 본문 자동 크롤링\n"
                "   • 그 외 텍스트는 → 제목 → AI 가 제목들을 종합해 원고 작성\n"
                "   • 한 행에 URL 과 제목을 섞어 넣어도 됩니다.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"양식 저장 실패: {e}")

    # ─────────────── 엑셀 업로드 (DB 추가) ───────────────
    # ─────────────── URL 본문 크롤러 (v58) ───────────────
    def _fetch_url_text(self, url, timeout=15):
        """단순 fetch + HTML 파싱. 네이버 블로그/뉴스/일반 뉴스 본문 영역 우선.
        선택 패키지(trafilatura)가 없어도 경고 없이 기본 추출기로 처리한다.
        실패 시 빈 문자열 반환."""
        if not url or not url.startswith(("http://", "https://")):
            return ""
        # ── (A) 유튜브 링크: 자막(스크립트) 자동 추출 ──
        try:
            if ("youtube.com/watch" in url) or ("youtu.be/" in url) or ("youtube.com/shorts/" in url):
                vid = _extract_video_id(url)
                if vid:
                    transcript = ""
                    # 1차: youtube-transcript-api
                    try:
                        from youtube_transcript_api import YouTubeTranscriptApi
                        for langs in (["ko"], ["en"], ["ko", "en"], None):
                            try:
                                tr = YouTubeTranscriptApi.get_transcript(vid, languages=langs) if langs else YouTubeTranscriptApi.get_transcript(vid)
                                transcript = "\n".join(seg.get("text", "") for seg in tr if seg.get("text"))
                                if transcript.strip():
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass
                    # 2차: 메타데이터(제목/설명)라도 가져옴
                    title_desc = ""
                    try:
                        import requests
                        from bs4 import BeautifulSoup
                        r = requests.get(f"https://www.youtube.com/watch?v={vid}",
                                         headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko,en;q=0.8"},
                                         timeout=timeout)
                        soup = BeautifulSoup(r.text, "html.parser")
                        t = soup.find("meta", property="og:title")
                        d = soup.find("meta", property="og:description")
                        if t: title_desc += f"[제목] {t.get('content','').strip()}\n"
                        if d: title_desc += f"[설명] {d.get('content','').strip()}\n"
                    except Exception:
                        pass
                    combined = (title_desc + "\n[자막]\n" + transcript).strip() if transcript else title_desc.strip()
                    if combined:
                        if len(combined) > 8000:
                            combined = combined[:8000] + "...[잘림]"
                        return combined
        except Exception as e:
            self._log(f"유튜브 자막 추출 실패 [{url[:60]}]: {e}")

        try:
            import requests
            from bs4 import BeautifulSoup
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            # 네이버 블로그는 mobile 페이지가 본문 직접 노출
            fetch_url = url
            if "blog.naver.com" in url and "/PostView" not in url:
                # https://blog.naver.com/{id}/{logNo} → m.blog.naver.com/PostView
                m = re.match(r"https?://blog\.naver\.com/([^/?#]+)/(\d+)", url)
                if m:
                    fetch_url = f"https://m.blog.naver.com/PostView.naver?blogId={m.group(1)}&logNo={m.group(2)}"
            r = requests.get(fetch_url, headers=headers, timeout=timeout)
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
            soup = BeautifulSoup(html, "html.parser")

            text = ""
            # 1순위: 네이버 SmartEditor
            for sel in ["div.se-main-container", "div#SE-main-container",
                        "div.se_component_wrap", "div#postViewArea",
                        "div#post-view"]:
                node = soup.select_one(sel)
                if node:
                    text = node.get_text("\n", strip=True)
                    if len(text) > 100:
                        break
            # 2순위: 주요 뉴스/언론사 본문 영역
            if not text or len(text) < 100:
                article_selectors = [
                    # 네이버/다음/범용 article
                    "article#dic_area", "div#newsct_article", "div#articleBodyContents",
                    "div.news_end", "div.article_body", "article", "main article",
                    "div.article_view", "section#article_body", "div#harmonyContainer",
                    "div.news_view", "div.view_content", "div.article-content",
                    "div.article_content", "div.article-body", "div.articleBody",
                    # 인터넷신문 솔루션 계열(살구뉴스/일요신문 등)
                    "div#article-view-content-div", "#article-view-content-div",
                    "div.article-view-content", "div.articleView", "div#articleBody",
                    "div#article_body", "div.cont_view", "div.view_cont",
                    "div.read_body", "div#readBody", "div#news_body_area",
                ]
                for sel in article_selectors:
                    node = soup.select_one(sel)
                    if node:
                        for bad in node.select("script, style, iframe, noscript, aside, nav, footer, header, .ad, .ads, .advertise, .advertisement, .banner, .copyright, .reporter, .sns, .share"):
                            bad.decompose()
                        text = node.get_text("\n", strip=True)
                        if len(text) > 100:
                            break
            # 3순위: trafilatura 범용 본문 추출기 (설치된 경우에만 조용히 사용)
            if not text or len(text) < 100:
                try:
                    import trafilatura
                    extracted = trafilatura.extract(
                        html,
                        include_comments=False,
                        include_tables=True,
                        favor_recall=True,
                        url=fetch_url,
                    )
                    if extracted and len(extracted.strip()) > 100:
                        text = extracted.strip()
                except Exception:
                    pass
            # 4순위: og:description + body 텍스트
            if not text or len(text) < 100:
                og = soup.find("meta", property="og:description")
                desc = og["content"].strip() if og and og.get("content") else ""
                body = soup.body.get_text("\n", strip=True) if soup.body else ""
                # body에서 너무 짧은 줄 필터
                body_lines = [ln for ln in body.split("\n") if len(ln) > 20]
                text = (desc + "\n\n" + "\n".join(body_lines[:200])).strip()

            # 정리: 공백 줄 압축, 길이 제한
            text = re.sub(r"\n{3,}", "\n\n", text)
            if len(text) > 8000:
                text = text[:8000] + "...[잘림]"
            return text
        except Exception as e:
            self._log(f"URL 크롤 실패 [{url[:60]}]: {e}")
            return ""

    def upload_excel(self):
        """[v60] A/B/C/D 4개 열 모두 = 제목 또는 URL 자동 분기.
            • http(s):// 로 시작 → URL → 본문 크롤링 → source 누적
            • 그 외           → 제목 → titles 에 저장
        URL·제목을 한 행에 섞어 넣을 수 있다 (D열도 동일 처리)."""
        try:
            from openpyxl import load_workbook
            path, _ = QFileDialog.getOpenFileName(
                self, "엑셀 파일 선택 (제목 또는 URL 양식)", "",
                "Excel Files (*.xlsx *.xls)")
            if not path: return
            wb = load_workbook(path, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) < 2:
                QMessageBox.warning(self, "경고",
                    "데이터가 없습니다 (1행 헤더, 2행부터 데이터)")
                return

            # [v61] 엑셀 업로드 시 기존 목록 초기화 (누적 방지)
            #  → 업로드한 엑셀에 있는 행만 화면에 표시되도록
            try:
                reply = QMessageBox.question(
                    self, "확인",
                    f"기존 목록 {len(self.titles_data)}건을 비우고\n"
                    f"이번 엑셀 내용만 새로 불러올까요?\n\n"
                    f"(아니오 = 기존 목록 뒤에 추가)",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes)
                if reply == QMessageBox.StandardButton.Yes:
                    # DB 에서도 모두 삭제
                    try:
                        for d in list(self.titles_data):
                            did = d.get("db_id")
                            if did is not None and hasattr(self.db, "delete"):
                                self.db.delete(did)
                    except Exception as _e:
                        self._log(f"  ⚠️ DB 삭제 일부 실패: {_e}")
                    self.titles_data.clear()
                    self._refresh_table()
                    self._log("🗑️ 기존 목록 비움 (엑셀 업로드 새로 시작)")
            except Exception:
                pass

            def _is_url(s: str) -> bool:
                s = (s or "").strip().lower()
                return s.startswith("http://") or s.startswith("https://")

            added, skipped = 0, 0
            url_total, title_total = 0, 0
            self._log(f"엑셀 업로드 시작: 총 {len(rows)-1}행 (제목/URL 자동 분기)")

            for ridx, r in enumerate(rows[1:], 2):
                if not r:
                    skipped += 1; continue
                # A/B/C/D 4개 열 모두 동일 처리
                c1 = (str(r[0]).strip() if len(r) > 0 and r[0] is not None else "")
                c2 = (str(r[1]).strip() if len(r) > 1 and r[1] is not None else "")
                c3 = (str(r[2]).strip() if len(r) > 2 and r[2] is not None else "")
                c4 = (str(r[3]).strip() if len(r) > 3 and r[3] is not None else "")
                kw = ""  # 별도 키워드 칸 없음 (D열도 URL/제목 처리)
                cells = [c for c in (c1, c2, c3, c4) if c]
                if not cells:
                    skipped += 1
                    self._log(f"  [{ridx}행] 빈 행 스킵")
                    continue

                # URL / 제목 분리
                urls, titles = [], []
                for c in cells:
                    if _is_url(c):
                        urls.append(c)
                    else:
                        titles.append(c)

                # 분류 결과 로그 (디버그용)
                self._log(
                    f"  [{ridx}행] 입력 {len(cells)}셀 → URL {len(urls)}개 / 제목 {len(titles)}개"
                    + (f" | 제목: {titles}" if titles else "")
                )

                # ── URL 본문 크롤링 ──
                parts = []
                for ui, u in enumerate(urls, 1):
                    self._log(f"  [{ridx}행] URL{ui} 크롤링: {u[:70]}")
                    body = self._fetch_url_text(u)
                    if body:
                        parts.append(f"=== [원문 {ui}] {u} ===\n{body}")
                        url_total += 1
                        self._log(f"    → {len(body)}자 추출")
                    else:
                        self._log(f"    → 추출 실패")
                src = "\n\n".join(parts) if parts else ""

                # ── 제목 처리 (최대 3개까지 보관, 나머지는 combined 에 합침) ──
                title1 = titles[0] if len(titles) > 0 else ""
                title2 = titles[1] if len(titles) > 1 else ""
                title3 = titles[2] if len(titles) > 2 else ""
                title_total += len(titles)
                # D열까지 합쳐 4개 제목이 들어올 수 있으므로 keyword 칸에 4번째 보관
                if len(titles) >= 4 and not kw:
                    kw = titles[3]

                # 통합 표시 라벨
                if titles and urls:
                    combined = f"{title1 or '(제목없음)'}  +URL {len(urls)}개"
                elif titles:
                    combined = " | ".join(titles)
                else:
                    combined = f"(URL {len(urls)}개 → AI 제목생성 대기)"

                row_dict = {
                    "title1": title1, "title2": title2, "title3": title3,
                    "combined_title": combined,
                    "keyword": kw, "source": src,
                    "content": "", "status": "대기", "publish_status": "",
                    "char_count": 0, "ai_model": "",
                    "url1": urls[0] if len(urls) > 0 else "",
                    "url2": urls[1] if len(urls) > 1 else "",
                    "url3": urls[2] if len(urls) > 2 else "",
                }
                db_id = self.db.insert(row_dict)
                row_dict["db_id"] = db_id
                self.titles_data.append(row_dict)
                added += 1
                QApplication.processEvents()

            self._refresh_table()
            self._log(
                f"엑셀 업로드 완료: {added}건 추가 "
                f"(URL {url_total}개 크롤링, 제목 {title_total}개 인식, "
                f"빈 행 {skipped}개 스킵)")
            QMessageBox.information(self, "완료",
                f"{added}건 추가됨\n"
                f" • URL 크롤링: {url_total}개\n"
                f" • 제목 인식: {title_total}개\n"
                f" • 빈 행 스킵: {skipped}개\n→ DB 에 영구 저장됨")
        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "오류", f"엑셀 읽기 실패: {e}")

    # ─────────────── 테이블 렌더링 ───────────────
    def _refresh_table(self):
        self.table.setRowCount(len(self.titles_data))
        done = sum(1 for d in self.titles_data if d.get("status") == "완료")
        pub = sum(1 for d in self.titles_data if d.get("publish_status") == "발행완료")
        for i, d in enumerate(self.titles_data):
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, chk)
            self.table.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 2, QTableWidgetItem(d.get("combined_title", "")))
            self.table.setItem(i, 3, QTableWidgetItem(d.get("keyword", "")))
            self.table.setItem(i, 4, QTableWidgetItem(str(d.get("char_count", 0))))
            self.table.setItem(i, 5, QTableWidgetItem(d.get("status", "대기")))
            self.table.setItem(i, 6, QTableWidgetItem(d.get("publish_status", "")))
            self.table.setItem(i, 7, QTableWidgetItem(str(d.get("db_id", ""))))
        self.count_label.setText(f"📊 총 {len(self.titles_data)}개 / AI완료 {done}개 / 발행완료 {pub}개")

    def _selected_indices(self):
        idxs = []
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            if it and it.checkState() == Qt.CheckState.Checked:
                idxs.append(i)
        return idxs

    def clear_titles(self):
        if not self.titles_data: return
        if QMessageBox.question(
            self, "확인", "DB까지 모두 삭제할까요?\n(취소 불가)"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_all()
        self.titles_data = []
        self._refresh_table()
        self._log("전체 삭제 완료 (DB 포함)")

    # ─────────────── AI 생성 ───────────────
    def start_generate(self, only_selected=False):
        if not self.titles_data:
            QMessageBox.warning(self, "경고", "먼저 엑셀을 업로드하세요"); return
        if self._generating:
            QMessageBox.information(self, "안내", "이미 생성 중입니다"); return

        target_idx = self._selected_indices() if only_selected else list(range(len(self.titles_data)))
        if only_selected and not target_idx:
            QMessageBox.warning(self, "경고", "체크박스로 항목을 선택하세요"); return

        self._generating = True; self._cancel_generate = False
        prompt_template = self.ai_prompt.toPlainText()
        char_count = self.char_count.value()
        model_label = self.ai_model.currentText()

        self._log(f"AI 생성 시작: {len(target_idx)}건, 모델={model_label}")
        for cnt, i in enumerate(target_idx, 1):
            if self._cancel_generate:
                self._log("사용자 중지"); break
            d = self.titles_data[i]
            d["status"] = "생성중..."
            self._refresh_table(); QApplication.processEvents()

            # v58: 제목이 비어있고 URL 원문만 있는 경우 → AI가 본문+제목을 생성
            titles_list = [t for t in [d.get("title1",""), d.get("title2",""), d.get("title3","")] if t]
            source_text = d.get("source", "") or ""
            has_titles = bool(titles_list)
            has_source = bool(source_text.strip())

            if not has_titles and has_source:
                # URL 모드: 본문을 보고 제목+원고 생성
                titles_block = "(제목 없음 — 아래 원문을 종합하여 새 제목 1개를 첫 줄에 'TITLE: ...' 형식으로 출력하세요)"
                ext_instr = (
                    "\n\n[중요 출력 형식]\n"
                    "1행: TITLE: <SEO 최적화된 한글 제목 1개>\n"
                    "2행부터: 본문 시작\n"
                )
            else:
                titles_block = "\n".join(f"  {idx+1}. {t}" for idx, t in enumerate(titles_list)) or "(제목 없음)"
                ext_instr = ""

            prompt = (prompt_template
                      .replace("{titles}", titles_block)
                      .replace("{title}", d.get("combined_title", ""))
                      .replace("{keyword}", d.get("keyword", "") or "")
                      .replace("{source}", source_text or "(참고원문 없음)")
                      .replace("{char_count}", str(char_count))) + ext_instr
            try:
                content = self._call_ai(prompt, model_label)

                # v58: TITLE 추출 → combined_title 업데이트
                gen_title = ""
                m = re.match(r"\s*(?:TITLE|제목)\s*[:：]\s*(.+)", content)
                if m:
                    gen_title = m.group(1).strip().strip('"\'').strip("「」『』")
                    # TITLE 줄 제거
                    content = content[m.end():].lstrip("\n").lstrip()

                if gen_title:
                    d["title1"] = gen_title
                    d["combined_title"] = gen_title

                d["content"] = content
                d["char_count"] = len(content)
                d["status"] = "완료"
                d["ai_model"] = model_label
                update_kwargs = dict(content=content, status="완료",
                                     char_count=len(content), ai_model=model_label)
                if gen_title:
                    update_kwargs["combined_title"] = gen_title
                    update_kwargs["title1"] = gen_title
                try:
                    self.db.update(d["db_id"], **update_kwargs)
                except TypeError:
                    # DB 스키마가 일부 필드를 모를 경우 안전 업데이트
                    self.db.update(d["db_id"], content=content, status="완료",
                                   char_count=len(content), ai_model=model_label)
                self._log(f"[{cnt}/{len(target_idx)}] 완료: {d['combined_title'][:40]}")
            except Exception as e:
                d["status"] = f"실패: {e}"[:80]
                self.db.update(d["db_id"], status=d["status"])
                self._log(f"[{cnt}] 실패: {e}")
            self._refresh_table(); QApplication.processEvents()

        self._generating = False
        QMessageBox.information(self, "완료", f"AI 생성 종료 (대상 {len(target_idx)}건)")

    def _call_ai(self, prompt, model_label):
        cfg = getattr(self.main, 'config', {}) or {}
        ll = model_label.lower()
        if "perplexity" in ll or "sonar" in ll:
            api_key = cfg.get("perplex_key") or cfg.get("perplexity_key") or os.environ.get("PERPLEXITY_API_KEY", "")
            if not api_key: raise RuntimeError("Perplexity API 키 미설정")
            model_name = "sonar-pro" if "pro" in ll else "sonar"
            sys_msg = ("당신은 한국어 뉴스/블로그 전문 작가입니다. "
                       "주어진 기사 제목을 웹에서 검색하여 최신 사실관계를 확인한 뒤, "
                       "자연스러운 한국어 블로그 원고를 작성하세요.")
            return call_perplexity(api_key, prompt, model=model_name, system_message=sys_msg)
        if "gemini" in ll:
            api_key = cfg.get("gemini_key") or os.environ.get("GEMINI_API_KEY", "")
            if not api_key: raise RuntimeError("Gemini API 키 미설정")
            return call_gemini_text(api_key, prompt, model="gemini-2.5-flash")
        if "claude" in ll:
            raise RuntimeError("Claude는 미구현")
        api_key = cfg.get("openai_api_key") or cfg.get("openai_key") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key: raise RuntimeError("OpenAI API 키 미설정")
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        model_name = "chatgpt-4o-latest" if "4o-latest" in model_label or model_label.startswith("GPT-4o (") else "gpt-4o-mini"
        resp = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": prompt}], temperature=0.7,
        )
        return resp.choices[0].message.content or ""

    # ─────────────── 미리보기 ───────────────
    def preview_article(self, row, col):
        if row >= len(self.titles_data): return
        a = self.titles_data[row]
        dlg = QDialog(self); dlg.setWindowTitle(f"미리보기: {a.get('combined_title','')[:40]}")
        dlg.resize(900, 650)
        v = QVBoxLayout(dlg)
        te = QTextEdit(); te.setPlainText(a.get("content",""))
        v.addWidget(te)
        btn = QPushButton("닫기"); btn.clicked.connect(dlg.accept)
        v.addWidget(btn)
        dlg.exec()

    # ─────────────── 카테고리(폴더) 로드 — PopularPostPage와 동일한 흐름 ───────────────
    def load_blog_folders(self):
        acc = self._get_selected_account()
        if not acc:
            QMessageBox.warning(self, "경고", "계정을 먼저 선택하세요"); return
        nid = acc.get('id') or acc.get('nid') or ''
        # 1) 캐시 우선 사용
        cached = (self.folder_cache or {}).get(nid) or []
        if cached:
            self.folder_combo.clear()
            for item in cached:
                if isinstance(item, dict):
                    self.folder_combo.addItem(item.get('text', ''), item.get('data'))
                else:
                    self.folder_combo.addItem(str(item))
            self._log(f"📂 저장된 카테고리 사용: {nid} ({len(cached)}개) — 새로 불러오려면 캐시 파일 삭제")
            QMessageBox.information(self, "완료",
                f"저장된 카테고리 {len(cached)}개를 불러왔습니다.\n"
                f"(다시 받으려면 bulk_title_category_cache.json 파일 삭제)")
            return
        self._log(f"카테고리 로드 시작: {acc.get('id')}")
        # 인기글수집 페이지(PopularPostPage)에 동일 함수가 있다면 그대로 위임
        pop = self.main.pages.get('popular') if hasattr(self.main, 'pages') else None
        if pop is None:
            pop = getattr(self.main, 'popular_page', None)
        # ★ lazy-load: 인기글 페이지가 아직 안 만들어졌다면 강제로 생성
        if pop is None and hasattr(self.main, '_ensure_page_loaded'):
            try:
                self.main._ensure_page_loaded('popular')
                pop = self.main.pages.get('popular')
                self._log("인기글 페이지 lazy 로드 완료")
            except Exception as e:
                self._log(f"인기글 페이지 강제 로드 실패: {e}")
        if pop and hasattr(pop, 'load_blog_folders'):
            self._log("→ PopularPostPage.load_blog_folders 위임")
            try:
                # 계정 콤보 동기화 후 호출
                if hasattr(pop, 'account_combo'):
                    idx = pop.account_combo.findData(acc.get('id'))
                    if idx < 0:
                        idx = pop.account_combo.findText(acc.get('id', ''))
                    if idx >= 0: pop.account_combo.setCurrentIndex(idx)
                pop.load_blog_folders()
                # 폴더 결과 복사 (PopularPostPage는 비동기일 수 있어 약간 대기)
                from PyQt6.QtCore import QTimer
                def _sync_folders(show_msg=False):
                    src = None
                    for attr in ('blog_folder', 'folder_combo', 'category_combo'):
                        c = getattr(pop, attr, None)
                        if c is not None and hasattr(c, 'count') and c.count() > 0:
                            src = c; break
                    if src is None:
                        return
                    self.folder_combo.clear()
                    saved = []
                    for i in range(src.count()):
                        self.folder_combo.addItem(src.itemText(i), src.itemData(i))
                        saved.append({'text': src.itemText(i), 'data': src.itemData(i)})
                    cnt = self.folder_combo.count()
                    self._log(f"✅ 카테고리 {cnt}개 동기화 완료")
                    # ★ 캐시에 영구 저장
                    if cnt > 0:
                        try:
                            self.folder_cache[nid] = saved
                            import json as _json
                            with open(self._cat_cache_path, 'w', encoding='utf-8') as _f:
                                _json.dump(self.folder_cache, _f, ensure_ascii=False, indent=2)
                            self._log(f"💾 카테고리 캐시 저장: {self._cat_cache_path}")
                        except Exception as _e:
                            self._log(f"캐시 저장 실패: {_e}")
                    if show_msg:
                        QMessageBox.information(self, "완료", f"카테고리 {cnt}개 로드 완료")
                _sync_folders(show_msg=True)
                _sync_folders()
                # 비동기 로드 대비 1.5초 후 한 번 더 동기화
                QTimer.singleShot(1500, _sync_folders)
                QTimer.singleShot(4000, _sync_folders)
                # 더 길게도 한 번 (셀레니움 로그인 후 응답 시간 고려)
                QTimer.singleShot(8000, _sync_folders)
                QTimer.singleShot(15000, _sync_folders)
                return
            except Exception as e:
                self._log(f"위임 실패, 직접 처리: {e}")
                import traceback; traceback.print_exc()
        QMessageBox.warning(self, "오류",
            "인기글 수집 페이지를 찾을 수 없습니다.\n"
            "프로그램을 재시작 후 다시 시도해 주세요.")

    # ─────────────── 발행 (PopularPostPage.publish_to_blog 위임) ───────────────
    # ─────────────── 발행 (v41: 엑셀 데이터 격리 + 빈 키워드/본문 허용) ───────────────
    def publish_to_blog(self, only_selected=False):
        """[v41] 엑셀로 업로드한 항목만 발행.
        - 인기글 페이지의 generated_articles를 절대 사용/오염시키지 않음 (백업 후 비우고 주입)
        - status가 '완료'가 아니어도 발행 가능 (키워드/본문 빈 값 허용)
        - 본문이 비어있으면 제목을 본문으로 사용 (네이버 에디터가 빈 본문을 거부하기 때문)
        """
        if self._publishing:
            QMessageBox.information(self, "안내", "이미 발행 중입니다"); return

        # 발행 대상 추출 (status 필터 제거)
        if only_selected:
            sel = self._selected_indices()
            if not sel:
                QMessageBox.warning(self, "경고", "체크박스로 항목을 선택하세요"); return
            targets = [self.titles_data[i] for i in sel]
        else:
            targets = list(self.titles_data)
        if not targets:
            QMessageBox.warning(self, "경고", "발행할 항목이 없습니다 (먼저 엑셀 업로드)"); return

        acc = self._get_selected_account()
        if not acc:
            QMessageBox.warning(self, "경고", "계정을 선택하세요"); return

        # 인기글수집 페이지의 발행 엔진 위임 (속성/메서드만 빌려쓰기)
        pop = self.main.pages.get('popular') if hasattr(self.main, 'pages') else None
        if pop is None:
            pop = getattr(self.main, 'popular_page', None)
        # ★ v62: 인기글 페이지가 아직 lazy-load 되지 않았으면 강제로 로드
        if pop is None and hasattr(self.main, '_ensure_page_loaded'):
            try:
                self.main._ensure_page_loaded('popular')
                pop = self.main.pages.get('popular')
                self._log("ℹ️ 인기글 페이지 자동 로드 완료 (발행 엔진 공유용)")
            except Exception as _e:
                self._log(f"인기글 페이지 자동 로드 실패: {_e}")
        if not pop or not hasattr(pop, 'publish_to_blog'):
            QMessageBox.critical(self, "오류",
                "인기글 수집 페이지를 찾을 수 없습니다. 발행 엔진이 공유되지 않습니다."); return

        # ── 엑셀 데이터만 담은 페이로드 구성 (빈 본문은 제목으로 채움) ──
        articles_payload = []
        for d in targets:
            title = (d.get("combined_title") or d.get("title1") or "").strip() or "(제목 없음)"
            content = (d.get("content") or "").strip()
            if not content:
                # 네이버 에디터가 빈 본문을 거부하므로 제목을 본문으로 폴백
                content = title
            articles_payload.append({
                "title": title,
                "content": content,
                "keyword": d.get("keyword", "") or "",
                "status": "완료",   # PopularPostPage._build_publish_queue 의 '실패' 필터 통과용
            })

        # ── 인기글 페이지의 generated_articles 백업 후 우리 데이터로 완전 교체 ──
        # (옵션 1+2: 인기글 큐와 격리 + 업로드 시 인기글 큐 자동 비우기)
        backup_generated = list(getattr(pop, 'generated_articles', []) or [])
        backup_articles  = getattr(pop, 'articles', None)
        try:
            pop.generated_articles = articles_payload   # ★ 핵심: 올바른 속성명
            try:
                pop.articles = articles_payload          # 혹시 다른 코드 경로 대비
            except Exception:
                pass

            # ── 카테고리(폴더) 동기화 ──
            target_text = self.folder_combo.currentText()
            if target_text:
                for attr in ('blog_folder', 'folder_combo', 'category_combo'):
                    c = getattr(pop, attr, None)
                    if c is not None and hasattr(c, 'findText'):
                        idx = c.findText(target_text)
                        if idx >= 0:
                            c.setCurrentIndex(idx); break

            # ── 계정 동기화 ──
            if hasattr(pop, 'account_combo'):
                idx = pop.account_combo.findData(acc.get('id'))
                if idx < 0:
                    idx = pop.account_combo.findText(acc.get('id', ''))
                if idx >= 0:
                    pop.account_combo.setCurrentIndex(idx)

            # ── 공개 범위 / 발행 간격 동기화 (가능한 경우) ──
            try:
                if hasattr(pop, 'publish_open') and hasattr(pop.publish_open, 'setCurrentText'):
                    pop.publish_open.setCurrentText(self.publish_open.currentText())
            except Exception:
                pass
            try:
                if hasattr(pop, 'publish_interval') and hasattr(pop.publish_interval, 'setValue'):
                    pop.publish_interval.setValue(int(self.publish_interval.value()))
            except Exception:
                pass

            self._publishing = True
            self._log(f"🚀 [엑셀 전용 발행] {len(articles_payload)}건 (인기글 데이터와 분리)")
            for i, a in enumerate(articles_payload, 1):
                self._log(f"  [{i}] {a['title'][:40]} (본문 {len(a['content'])}자, 키워드='{a['keyword']}')")

            # ── 인기글 페이지 발행 엔진 실행 ──
            pop.publish_to_blog()

            # 위임 발행은 비동기 (백그라운드 스레드)이므로 결과는 '발행요청'으로 마킹
            for d in targets:
                d["publish_status"] = "발행요청"
                try:
                    self.db.update(d["db_id"], publish_status="발행요청",
                                   published_at=self._now())
                except Exception:
                    pass
            self._refresh_table()
        except Exception as e:
            self._log(f"❌ 발행 오류: {e}")
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "오류", f"발행 실패: {e}")
        finally:
            self._publishing = False
            # ── 인기글 페이지 데이터 원복 (다른 탭 작업 보호) ──
            try:
                pop.generated_articles = backup_generated
                if backup_articles is not None:
                    pop.articles = backup_articles
            except Exception:
                pass


    # ─────────────── 결과 내보내기 ───────────────
    def export_results(self):
        done = [d for d in self.titles_data if d.get("content")]
        if not done:
            QMessageBox.warning(self, "경고", "내보낼 원고가 없습니다"); return
        try:
            from openpyxl import Workbook
            path, _ = QFileDialog.getSaveFileName(
                self, "결과 저장", f"제목대량_결과_{_dt.now().strftime('%Y%m%d_%H%M')}.xlsx",
                "Excel Files (*.xlsx)"
            )
            if not path: return
            wb = Workbook(); ws = wb.active; ws.title = "결과"
            ws.append(["#", "제목(통합)", "키워드", "글자수", "AI상태", "발행상태", "원고", "생성모델"])
            for i, d in enumerate(done, 1):
                ws.append([i, d.get("combined_title",""), d.get("keyword",""),
                           d.get("char_count",0), d.get("status",""),
                           d.get("publish_status",""), d.get("content",""),
                           d.get("ai_model","")])
            wb.save(path)
            QMessageBox.information(self, "완료", f"저장됨:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"내보내기 실패: {e}")


if __name__ == "__main__":
    import traceback
    try:
        print("[DEBUG] QApplication 생성 중...")
        app = QApplication(sys.argv)
        app.setStyle('Fusion')
        print("[DEBUG] PostPro 윈도우 생성 중...")
        window = PostPro()
        print("[DEBUG] 윈도우 show...")
        window.show()
        print("[DEBUG] 이벤트 루프 시작...")
        rc = app.exec()
        print(f"[DEBUG] 이벤트 루프 종료 rc={rc}")
        sys.exit(rc)
    except Exception as e:
        print(f"[FATAL ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        input("엔터를 눌러 종료하세요...")