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
    "naver_blog_id": "",
    "naver_accounts": [],
    "lilys_api_key": "",
    "model_type": "gpt-4",
    "result_language": "ko",
    "check_interval_minutes": 30,
    "lilys_folder_name": "",
    "max_fetch_count": 10,
    "lilys_report_name": "",
    "lilys_summary_length": "기본",
    "transform_mode": "clean",
    "ai_provider": "off",
    "openai_key": "",
    "gpt_model": "gpt-4o",
    "gemini_key": "",
    "gemini_model": "gemini-2.5-flash",
    "ai_prompt": "",
    "paragraph_style": "para",
    "line_max_chars": 30,
    "use_quotes": "on",
    "quote_style": "line",
    "use_divider": "on",
    "text_align": "left",
    "image_enabled": "on",
    "image_max": 5,
    "chrome_profile_dir": os.path.join(BASE_DIR, "chrome_profile"),
    "publish_mode": "publish",  # "publish"(발행) / "draft"(임시저장) / "schedule"(예약발행)
    "schedule_time": "",        # 예약발행 시각 (예: 2026-07-15 09:00)
}

# 발행 방식 코드 ↔ 화면 표시 이름
PUBLISH_MODE_LABELS = {
    "publish": "📤 발행",
    "draft": "💾 임시저장",
    "schedule": "⏰ 예약발행",
}
PUBLISH_MODE_CODES = {v: k for k, v in PUBLISH_MODE_LABELS.items()}

# AI 재작성 제공자
AI_PROVIDER_LABELS = {
    "off": "🚫 사용 안 함 (Lilys 원본)",
    "gpt": "🟢 GPT (OpenAI)",
    "gemini": "🔵 Gemini (Google)",
}
AI_PROVIDER_CODES = {v: k for k, v in AI_PROVIDER_LABELS.items()}

# 본문 변형 방식
TRANSFORM_MODE_LABELS = {
    "clean": "✂️ 자동 정리 (각주 제거+인용구)",
    "raw": "📄 원문 그대로 (변형 없음)",
}
TRANSFORM_MODE_CODES = {v: k for k, v in TRANSFORM_MODE_LABELS.items()}

# Lilys 요약 길이 옵션 (노트 화면의 버튼 텍스트 그대로)
SUMMARY_LENGTHS = ["기본", "짧게", "길게", "쉽게"]

# 인용구 사용 여부
USE_QUOTES_LABELS = {
    "on": "💬 인용구 사용",
    "off": "🚫 인용구 사용 안 함",
}
USE_QUOTES_CODES = {v: k for k, v in USE_QUOTES_LABELS.items()}

# 인용구(소제목) 스타일 — SE 에디터 인용구 팝업의 몇 번째 스타일을 쓸지
QUOTE_STYLE_LABELS = {
    "line": "▎ 세로줄형",
    "bubble": "💬 말풍선형",
    "corner": "『』 따옴표형",
}
QUOTE_STYLE_CODES = {v: k for k, v in QUOTE_STYLE_LABELS.items()}
# 스타일 → 인용구 팝업에서 클릭할 버튼 인덱스(0부터)
QUOTE_STYLE_INDEX = {"line": 0, "bubble": 2, "corner": 1}

# 구분선 사용 여부
DIVIDER_LABELS = {
    "on": "➖ 구분선 넣기 (제목·소제목 구분)",
    "off": "🚫 구분선 안 씀",
}
DIVIDER_CODES = {v: k for k, v in DIVIDER_LABELS.items()}

# 본문 정렬
TEXT_ALIGN_LABELS = {
    "left": "⬅️ 왼쪽 정렬",
    "center": "🔳 가운데 정렬",
}
TEXT_ALIGN_CODES = {v: k for k, v in TEXT_ALIGN_LABELS.items()}

# 문단 나누기
PARAGRAPH_LABELS = {
    "para": "📑 문단 단위 (문장 유지 + 문단 사이 여백)",
    "airy": "✍️ 짧은 줄 + 여백 (모바일 가독성)",
    "none": "📄 그대로",
}
PARAGRAPH_CODES = {v: k for k, v in PARAGRAPH_LABELS.items()}

def para_format(text: str) -> str:
    """문단 단위 정리: 소제목(인용구) 줄은 그대로 두고,
    일반 문장들은 빈 줄 기준으로 한 문단씩 합쳐 문단 사이에 여백을 준다."""
    blocks, buf = [], []

    def _flush():
        if buf:
            blocks.append(" ".join(buf))
            buf.clear()

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            _flush()
            continue
        if looks_like_quote(s):     # 소제목/인용구는 독립 블록
            _flush()
            blocks.append(s)
        else:
            buf.append(s)
    _flush()
    return "\n\n".join(b for b in blocks if b)

def _split_long_line(text: str, max_len: int = 40) -> list[str]:
    """긴 줄을 문장 → 어절 단위로 잘라 짧은 줄 목록으로 만든다."""
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= max_len:
            out.append(p)
            continue
        words = p.split(" ")
        cur = ""
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= max_len:
                cur = cur + " " + w
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out

def airy_format(text: str, max_len: int = 30) -> str:
    """블로그 가독성용: 문장을 짧은 줄로 나누고 줄 사이에 여백을 넣는다."""
    blocks = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if looks_like_quote(line):
            blocks.append(line)  # 인용구는 자르지 않음
        else:
            blocks.extend(_split_long_line(line, max_len=max_len))
    return "\n\n".join(blocks)

def prepare_body(cfg: dict, body: str) -> str:
    """설정에 따라 본문을 변형하거나 원문 그대로 반환한다."""
    if cfg.get("transform_mode") == "raw":
        return body.strip()
    text = markdown_to_plain(body)
    style = cfg.get("paragraph_style", "para")
    if style == "airy":
        text = airy_format(text, max_len=_cfg_int(cfg, "line_max_chars", 30))
    elif style == "para":
        text = para_format(text)
    return text

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

# 크롤링한 노트 목록 캐시 (껐다 켜도 유지)
NOTES_CACHE_FILE = os.path.join(BASE_DIR, "notes_cache.json")

def load_notes_cache() -> tuple[list, str]:
    """저장해 둔 노트 목록과 마지막 갱신 시각을 반환한다. 없으면 ([], "")."""
    if os.path.exists(NOTES_CACHE_FILE):
        try:
            with open(NOTES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            notes = [(n[0], n[1]) for n in data.get("notes", [])]
            return notes, data.get("updated_at", "")
        except Exception:
            pass
    return [], ""

def save_notes_cache(notes: list):
    from datetime import datetime
    try:
        with open(NOTES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "notes": [[u, t] for u, t in notes],
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

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
    """블로그 붙여넣기용으로 원문을 정리·변형한다.
    - [1], [2, 3] 같은 각주 번호와 [12:34] 타임스탬프 제거
    - 마크다운 헤딩(소제목)은 '> 제목' 으로 바꿔 발행 시 인용구 블록으로 삽입
    """
    text = re.sub(r"```.*?```", "", text, flags=re.S)              # 코드블록 제거
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", text)              # 링크는 텍스트만
    text = re.sub(r"\[\d+(?:[,\s]+\d+)*\]", "", text)              # [1], [2, 3] 각주 제거
    text = re.sub(r"\[?\b\d{1,2}:\d{2}(?::\d{2})?\]?", "", text)   # 12:34 타임스탬프 제거
    text = re.sub(r"^#{1,6}\s*(.+)$", r"> \1", text, flags=re.M)   # 헤딩 → 인용구 후보
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                   # 굵게
    text = re.sub(r"\*(.+?)\*", r"\1", text)                       # 기울임
    text = re.sub(r"^[-*]\s+", "· ", text, flags=re.M)             # 리스트 불릿
    text = re.sub(r"[ \t]{2,}", " ", text)                         # 연속 공백 정리
    text = re.sub(r"[ \t]+([.,!?。，])", r"\1", text)              # 구두점 앞 공백 제거
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def looks_like_quote(line: str) -> bool:
    """인용구 블록으로 넣을 줄인지 감지 (> 마커 또는 양끝 따옴표)."""
    s = line.strip()
    if not s:
        return False
    if s.startswith(">"):
        return True
    s2 = s.rstrip(".。!?！？,，")
    pairs = [('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’'),
             ('「', '」'), ('『', '』'), ('《', '》')]
    for a, b in pairs:
        if s2.startswith(a) and s2.endswith(b) and len(s2) > 4:
            return True
    return False

def _strip_quote_markers(text: str) -> str:
    """인용 마커(>, 양끝 따옴표)를 제거한 본문만 남긴다."""
    cleaned = text.lstrip(">").strip().split("\n")[0].strip()
    tail = ""
    c = cleaned
    while c and c[-1] in ".。!?！？,，":
        tail = c[-1] + tail
        c = c[:-1]
    for a, b in [('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’'),
                 ('「', '」'), ('『', '』'), ('《', '》')]:
        if c.startswith(a) and c.endswith(b):
            return c[len(a):-len(b)].strip() + tail
    return cleaned + tail if c != cleaned else cleaned

# ──────────────────────────────────────────────
# AI 재작성 (GPT / Gemini)
# ──────────────────────────────────────────────
DEFAULT_REWRITE_PROMPT = (
    "너는 블로그 글을 잘 쓰는 전문 작가야. 아래 원문을 참고해서 "
    "네이버 블로그에 올릴 글을 새로 써줘.\n"
    "규칙:\n"
    "- 자연스러운 한국어 존댓말, 친근한 블로거 말투\n"
    "- 첫 줄은 클릭을 부르는 제목 한 줄 (제목: 접두어 없이 제목만)\n"
    "- 소제목은 줄 앞에 '## ' 를 붙여 구분\n"
    "- 핵심 문장은 따옴표로 감싸 인용구로 강조\n"
    "- 원문의 각주 번호[1], 타임스탬프는 넣지 마\n"
    "- 마크다운 표/코드블록/링크문법은 쓰지 마\n"
    "- 사실을 지어내지 말고 원문 범위 안에서만 써\n\n"
    "원문:\n{content}"
)

def call_openai_text(api_key: str, prompt: str, model: str = "gpt-4o") -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, timeout=180.0, max_retries=2)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2400,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("AI 응답이 비어 있습니다")
    return text

def call_gemini_text(api_key: str, prompt: str, model: str = "gemini-2.5-flash") -> str:
    try:
        from google import genai
    except Exception:
        try:
            import google.genai as genai
        except Exception as e:
            raise RuntimeError(
                "Gemini 라이브러리가 설치되지 않았습니다. 터미널에서 "
                "'pip install google-genai' 를 실행해 주세요."
            ) from e
    fallback = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    chain = fallback[fallback.index(model):] if model in fallback else [model]
    last_error = None
    for try_model in chain:
        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(model=try_model, contents=prompt)
            text = (getattr(resp, "text", "") or "").strip()
            if text:
                return text
            raise RuntimeError("Gemini 응답이 비어 있습니다")
        except Exception as e:
            last_error = e
            s = str(e)
            if ("503" in s or "429" in s or "overloaded" in s.lower()
                    or "RESOURCE_EXHAUSTED" in s) and try_model != chain[-1]:
                time.sleep(1)
                continue
            time.sleep(2)
    raise RuntimeError(f"Gemini 연결 실패: {last_error}")

def ai_rewrite(cfg: dict, title: str, body: str, log) -> tuple[str, str]:
    """AI로 글을 재작성해 (제목, 본문)을 반환한다. 실패 시 원본을 그대로 돌려준다."""
    provider = cfg.get("ai_provider", "off")
    if provider == "off":
        return title, body

    prompt_tmpl = cfg.get("ai_prompt", "").strip() or DEFAULT_REWRITE_PROMPT
    if "{content}" not in prompt_tmpl:
        prompt_tmpl += "\n\n원문:\n{content}"
    source = f"제목: {title}\n\n{body}" if title else body
    prompt = prompt_tmpl.replace("{content}", source[:12000])

    try:
        if provider == "gpt":
            key = (cfg.get("openai_key") or "").strip()
            if not key:
                log("⚠️ OpenAI API 키가 없어 AI 재작성을 건너뜁니다")
                return title, body
            log("🤖 GPT로 글을 새로 생성하는 중...")
            out = call_openai_text(key, prompt, cfg.get("gpt_model", "gpt-4o"))
        elif provider == "gemini":
            key = (cfg.get("gemini_key") or "").strip()
            if not key:
                log("⚠️ Gemini API 키가 없어 AI 재작성을 건너뜁니다")
                return title, body
            log("🤖 Gemini로 글을 새로 생성하는 중...")
            out = call_gemini_text(key, prompt, cfg.get("gemini_model", "gemini-2.5-flash"))
        else:
            return title, body
    except Exception as e:
        log(f"⚠️ AI 재작성 실패({e}). 원본 내용으로 발행합니다.")
        return title, body

    # 결과의 첫 줄을 제목으로, 나머지를 본문으로 분리
    lines = [ln for ln in out.splitlines()]
    new_title, new_body = title, out
    for i, ln in enumerate(lines):
        if ln.strip():
            new_title = re.sub(r"^(제목|title)\s*[:：]\s*", "", ln.strip(), flags=re.I)
            new_body = "\n".join(lines[i + 1:]).strip() or out
            break
    log("✅ AI 재작성 완료")
    return new_title, new_body

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

def safe_get(driver, url) -> bool:
    """페이지 이동. 로드가 시간제한을 넘겨 멈추면 로딩을 중단하고 현재 DOM으로 계속 진행."""
    try:
        driver.get(url)
        return True
    except Exception:
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return False

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
        opts.page_load_strategy = "eager"  # DOM 준비되면 진행 (모든 리소스 대기 안 함)
        driver = webdriver.Chrome(options=opts)
        # 페이지 로드/스크립트가 무한정 멈추지 않도록 시간제한
        try:
            driver.set_page_load_timeout(60)
            driver.set_script_timeout(30)
        except Exception:
            pass
        return driver

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

def _click_toolbar_button(driver, selectors) -> bool:
    """에디터 툴바 버튼을 클릭. 여러 selector 중 보이는 첫 번째."""
    from selenium.webdriver.common.by import By
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            continue
    return False

def insert_naver_divider(driver) -> bool:
    """본문에 구분선(수평선)을 삽입한다."""
    ok = _click_toolbar_button(driver, [
        "button.se-horizontalLine-toolbar-button",
        'button[data-name="horizontalLine"]',
        'button[aria-label*="구분선"]',
        "button.se-toolbar-button-horizontalLine",
    ])
    if ok:
        time.sleep(0.3)
        # 구분선 스타일 팝업이 뜨면 첫 번째 선택
        _click_toolbar_button(driver, [
            "ul.se-toolbar-option-horizontalLine li:first-child button",
            'button[class*="horizontalLine"][class*="line"]',
        ])
        time.sleep(0.3)
    return ok

def insert_quote_block(driver, text: str, style: str = "line") -> bool:
    """SmartEditor 인용구 블록에 한 줄을 넣고 블록을 빠져나온다.
    style: line(세로줄) / bubble(말풍선) / corner(따옴표)"""
    import pyperclip
    from selenium.webdriver.common.by import By

    cleaned = _strip_quote_markers(text)
    if not cleaned:
        return False

    opened = _click_toolbar_button(driver, [
        "button.se-quotation-toolbar-button",
        'button[data-name="quotation"]',
        'button[aria-label*="인용구"]',
    ])
    if opened:
        time.sleep(0.35)
        # 인용구 스타일 팝업에서 원하는 스타일 버튼을 인덱스로 선택
        idx = QUOTE_STYLE_INDEX.get(style, 0)
        picked = False
        try:
            btns = driver.find_elements(
                By.CSS_SELECTOR,
                "ul.se-toolbar-option-quotation li button, "
                'button[class*="quotation"][class*="button"]')
            vis = [b for b in btns if b.is_displayed()]
            if vis:
                target = vis[idx] if idx < len(vis) else vis[0]
                driver.execute_script("arguments[0].click();", target)
                picked = True
        except Exception:
            pass
        if not picked:
            _click_toolbar_button(driver, [
                "button.se-quotation-line-button",
                'button[class*="quotation"][class*="line"]',
                "ul.se-toolbar-option-quotation li:first-child button",
            ])
        time.sleep(0.35)

    pyperclip.copy(cleaned)
    safe_hotkey(driver, "ctrl", "v")
    time.sleep(0.2)
    safe_press(driver, "enter")   # 인용 블록 종료 (다음은 호출부에서 본문 재포커스)
    time.sleep(0.15)
    return True

def apply_alignment(driver, alignment: str) -> bool:
    """본문 전체 선택 후 정렬을 적용한다 (툴바 버튼 + JS 폴백)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    if not alignment or alignment == "left":
        return False
    try:
        safe_hotkey(driver, "ctrl", "a")
        time.sleep(0.2)
    except Exception:
        pass

    # 정렬 토글 메뉴 펼치기
    _click_toolbar_button(driver, [
        "button.se-align-toolbar-button",
        'button[data-name="align"]',
        'button[aria-label*="정렬"]',
    ])
    time.sleep(0.2)

    targets = {"center": ["가운데", "중앙", "center"],
               "right": ["오른쪽", "우측", "right"]}.get(alignment, [])
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
                attrs = " ".join([(b.get_attribute("aria-label") or ""),
                                  (b.text or ""), (b.get_attribute("class") or ""),
                                  (b.get_attribute("data-name") or "")]).lower()
                if any(t.lower() in attrs for t in targets):
                    driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.2)
                    clicked = True
                    break
            except Exception:
                continue
    except Exception:
        pass
    if not clicked:
        _click_toolbar_button(driver, [
            f'button[class*="align"][class*="{alignment}"]',
            f'button[data-value="{alignment}"]',
            f'button[data-align="{alignment}"]',
        ])

    # JS 폴백 (항상 한 번 더 적용해 보장)
    try:
        driver.execute_script("""
            (function(align){
              var sels = ['.se-text-paragraph','.se-component .se-section',
                          '.se-text','.se-component-content p'];
              sels.forEach(function(s){
                document.querySelectorAll(s).forEach(function(el){
                  el.style.textAlign = align;
                });
              });
            })(arguments[0]);
        """, alignment)
    except Exception:
        pass

    try:
        ActionChains(driver).send_keys(Keys.END).perform()
    except Exception:
        pass
    return True

def _set_schedule_and_publish(driver, schedule_str: str, log) -> bool:
    """발행 레이어에서 '예약'을 선택하고 시간을 입력한다.
    schedule_str 형식: 'YYYY-MM-DD HH:MM' (분은 10분 단위로 반올림됨)"""
    import pyperclip
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})",
                 schedule_str.strip())
    if not m:
        log("⚠️ 예약 시간 형식이 올바르지 않습니다 (예: 2026-07-15 09:00)")
        return False
    yy, mo, dd, hh, mi = (int(g) for g in m.groups())
    mi = min(50, round(mi / 10) * 10)  # 네이버는 10분 단위

    # '예약' 라디오/라벨 클릭
    clicked = False
    for el in driver.find_elements(By.XPATH, "//*[contains(text(), '예약')]"):
        try:
            if el.is_displayed():
                el.click()
                time.sleep(1.5)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        log("⚠️ 발행 레이어에서 '예약' 옵션을 찾지 못했습니다")
        return False

    # 날짜 입력 (보이는 마지막 input 에 yyyy.MM.dd)
    try:
        inputs = [i for i in driver.find_elements(By.CSS_SELECTOR, "input")
                  if i.is_displayed()]
        if inputs:
            di = inputs[-1]
            di.click()
            time.sleep(0.3)
            pyperclip.copy(f"{yy}.{mo:02d}.{dd:02d}")
            safe_hotkey(driver, "ctrl", "a")
            safe_hotkey(driver, "ctrl", "v")
            time.sleep(0.5)
            safe_press(driver, "escape")  # 달력 팝업 닫기
            time.sleep(0.3)
    except Exception as e:
        log(f"⚠️ 예약 날짜 입력 실패(계속 진행): {e}")

    # 시/분 select 설정
    try:
        sels = [s for s in driver.find_elements(By.TAG_NAME, "select")
                if s.is_displayed()]
        if len(sels) >= 2:
            for sel_el, val in ((sels[0], hh), (sels[1], mi)):
                sel = Select(sel_el)
                for cand in (f"{val:02d}", str(val)):
                    try:
                        sel.select_by_visible_text(cand)
                        break
                    except Exception:
                        try:
                            sel.select_by_value(cand)
                            break
                        except Exception:
                            continue
            time.sleep(0.3)
    except Exception as e:
        log(f"⚠️ 예약 시각 선택 실패(계속 진행): {e}")

    log(f"⏰ 예약 시간 설정: {yy}-{mo:02d}-{dd:02d} {hh:02d}:{mi:02d}")
    return True

def _dismiss_editor_popups(driver):
    """작성 중이던 글 팝업 / 도움말 패널 등을 닫는다."""
    for css in ("button.se-popup-button-cancel", ".se-popup-button-cancel",
                "button.se-cancel", "button.se-help-panel-close-button"):
        _click_if_exists(driver, css)

def _dump_naver_debug(driver, log):
    """에디터를 못 열었을 때 화면 상태를 파일로 저장해 원인 분석을 돕는다."""
    from selenium.webdriver.common.by import By
    try:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        lines = [f"URL: {driver.current_url}", f"TITLE: {driver.title}", ""]
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            lines.append("[iframe 목록]")
            for f in frames:
                lines.append(f"- id={f.get_attribute('id')} name={f.get_attribute('name')} "
                             f"src={(f.get_attribute('src') or '')[:100]}")
        except Exception:
            pass
        try:
            lines.append("")
            lines.append("[보이는 버튼/링크 텍스트]")
            texts = set()
            for el in driver.find_elements(By.CSS_SELECTOR, "button, a"):
                t = (el.text or "").strip().replace("\n", " / ")[:50]
                if t and t not in texts and el.is_displayed():
                    texts.add(t)
                    lines.append(f"- {t}")
                if len(texts) > 60:
                    break
        except Exception:
            pass
        try:
            body_text = driver.execute_script("return document.body.innerText") or ""
            lines += ["", "[화면 텍스트 앞부분]", body_text[:2000]]
        except Exception:
            pass
        txt_path = os.path.join(BASE_DIR, "naver_debug.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        with open(os.path.join(BASE_DIR, "naver_debug.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log(f"🛠 네이버 진단 파일을 저장했습니다: {txt_path}")
        log("   (이 파일 내용을 보여주시면 화면 구조에 맞춰 수정할 수 있습니다)")
    except Exception as e:
        log(f"⚠️ 진단 파일 저장 실패: {e}")

def ensure_naver_login(driver, cfg, log) -> bool:
    """
    네이버 로그인 상태를 확인하고, 필요하면 설정의 ID/PW로 자동 로그인한다.
    자동 로그인이 실패하면 60초간 수동 로그인을 기다린다.
    """
    from selenium.webdriver.common.by import By

    # 이미 로그인 상태인지 쿠키로 확인
    try:
        safe_get(driver, "https://www.naver.com")
        time.sleep(2)
        if any(c["name"] in ("NID_AUT", "NID_SES") for c in driver.get_cookies()):
            log("✅ 네이버 로그인 상태 확인됨")
            return True
    except Exception:
        pass

    safe_get(driver, "https://nid.naver.com/nidlogin.login")
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
    try:
        driver.maximize_window()
    except Exception:
        pass
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

def insert_naver_image(driver, image_path: str, log) -> bool:
    """네이버 에디터 본문에 이미지 파일 하나를 삽입한다."""
    from selenium.webdriver.common.by import By

    abs_path = os.path.abspath(image_path)

    def _find_file_input():
        preferred = []
        for sel in ("input.se-image-input-file",
                    'input[class*="image"][type="file"]',
                    'input[accept*="image"][type="file"]'):
            preferred.extend(driver.find_elements(By.CSS_SELECTOR, sel))
        all_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
        return preferred + list(reversed(all_inputs))

    # 최대 2회: 이미지 툴바 버튼 클릭 → file input 대기 → 전송
    for attempt in range(2):
        # 이미지 툴바 버튼 클릭 (매 시도마다 다시)
        for sel in ("button.se-image-toolbar-button",
                    'button[data-name="image"]',
                    'button[data-type="image"]',
                    'button[aria-label*="사진"]',
                    'button[aria-label*="이미지"]',
                    "button.se-toolbar-button-image"):
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.8)
                    break
            except Exception:
                continue

        # file input 이 나타날 때까지 잠깐 대기하며 재시도
        deadline = time.time() + 5
        while time.time() < deadline:
            for fi in _find_file_input():
                try:
                    driver.execute_script(
                        "arguments[0].style.display='block';"
                        "arguments[0].style.visibility='visible';"
                        "arguments[0].removeAttribute('disabled');", fi)
                    fi.send_keys(abs_path)
                    time.sleep(4)  # 업로드 처리 대기
                    return True
                except Exception:
                    continue
            time.sleep(0.5)

    log("⚠️ 이미지 입력창을 찾지 못했습니다")
    return False

def post_to_naver_blog(browser: Browser, cfg: dict, title: str, content: str,
                       log, step=None, images=None) -> bool:
    step = step or (lambda key: None)
    images = images or []
    """네이버 블로그 스마트에디터 ONE 에 글을 작성하고 발행/임시저장한다."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = browser.get_driver()

    step("login")
    if not ensure_naver_login(driver, cfg, log):
        return False

    log("🌐 네이버 블로그 글쓰기 페이지 이동 중...")

    def _focus_naver_tab():
        """여러 탭 중 네이버 블로그(에디터)를 보고 있는 탭으로 전환한다.
        (로그인용으로 열린 Lilys 탭 등을 에디터로 착각하지 않도록 URL로 판별)"""
        try:
            cur = driver.current_window_handle
        except Exception:
            cur = None
        fallback = None
        for h in list(driver.window_handles):
            try:
                driver.switch_to.window(h)
                u = driver.current_url or ""
            except Exception:
                continue
            if "blog.naver.com" in u:
                fallback = h
                if any(k in u.lower() for k in ("postwrite", "goblogwrite", "postwriteform")):
                    return  # 에디터 탭 확정
        if fallback:
            driver.switch_to.window(fallback)
        elif cur:
            try:
                driver.switch_to.window(cur)
            except Exception:
                pass

    def _editor_loaded() -> bool:
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    'div.se-title-text, span.se-placeholder, '
                    'div.se-section-documentTitle, div[contenteditable="true"]')))
            return True
        except Exception:
            return False

    # 에디터 접근 경로 후보: 블로그ID가 있으면 새 에디터 주소 우선
    blog_id = (cfg.get("naver_blog_id") or "").strip().strip("/")
    candidates = []
    if blog_id:
        candidates.append(f"https://blog.naver.com/{blog_id}/postwrite")
    candidates.append("https://blog.naver.com/GoBlogWrite.naver")

    editor_ready = False
    tried = set()
    while candidates:
        url = candidates.pop(0)
        if url in tried:
            continue
        tried.add(url)
        safe_get(driver, url)
        time.sleep(5)
        _focus_naver_tab()

        # 글쓰기 화면이 mainFrame iframe 안에 있는 구형 구조 대응
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        try:
            iframe = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.ID, "mainFrame")))
            driver.switch_to.frame(iframe)
            time.sleep(1)
        except Exception:
            pass

        _dismiss_editor_popups(driver)
        if _editor_loaded():
            editor_ready = True
            break

        # 에디터가 아니면 현재 URL에서 블로그 ID를 추출해 새 에디터 주소로 재시도
        cur = ""
        try:
            driver.switch_to.default_content()
            cur = driver.current_url or ""
        except Exception:
            pass
        m = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)", cur)
        if m and m.group(1) not in ("GoBlogWrite.naver", "PostWriteForm.naver",
                                    "gnb", "post", "section"):
            next_url = f"https://blog.naver.com/{m.group(1)}/postwrite"
            if next_url not in tried:
                candidates.append(next_url)
        log(f"ℹ️ 에디터가 아닌 화면입니다 ({cur[:80]}), 다른 경로로 재시도합니다...")

    if not editor_ready:
        log("❌ 글쓰기 에디터를 열지 못했습니다")
        _dump_naver_debug(driver, log)
        return False

    step("write")
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
        _dump_naver_debug(driver, log)
        driver.switch_to.default_content()
        return False
    log(f"✏️ 제목 입력 완료: {title[:30]}")

    # ── 본문 영역에 포커스를 주는 헬퍼 (인용구 뒤 재포커스에 재사용) ──
    body_selectors = [
        'div.se-section-text div[contenteditable="true"]',
        'div.se-component-content div[contenteditable="true"]',
        'div[contenteditable="true"]',
        "div.se-text-paragraph",
    ]

    def _focus_body() -> bool:
        # 인용구(se-quotation) 안이 아닌, 일반 본문 문단 중 '마지막'을 클릭한다
        for sel in body_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                continue
            for el in reversed(els):
                try:
                    if not el.is_displayed():
                        continue
                    in_quote = driver.execute_script(
                        "return !!arguments[0].closest('.se-quotation, "
                        "[class*=quotation]');", el)
                    if in_quote:
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el)
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(0.25)
                    return True
                except Exception:
                    continue
        return False

    if not _focus_body():
        try:
            safe_press(driver, "tab")
            time.sleep(0.3)
        except Exception:
            log("❌ 본문 영역 포커스 실패")
            driver.switch_to.default_content()
            return False

    # ── 본문 입력 ──
    # 규칙: 소제목/따옴표 문장은 인용구 블록으로, 나머지는 일반 문단으로.
    # 인용구를 넣은 뒤에는 반드시 본문 영역을 다시 클릭(_focus_body)해서
    # 그다음 내용이 인용구 안에 딸려 들어가지 않도록 한다.
    import pyperclip
    use_quotes = (cfg.get("transform_mode") != "raw"
                  and cfg.get("use_quotes", "on") != "off")
    quote_style = cfg.get("quote_style", "line")
    use_divider = (cfg.get("transform_mode") != "raw"
                   and cfg.get("use_divider", "on") != "off")
    wrote_any = False

    # 본문 맨 앞(제목 아래) 구분선
    if use_divider:
        try:
            insert_naver_divider(driver)
            _focus_body()
        except Exception:
            pass

    for raw_line in content.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue  # 빈 줄은 건너뜀 (문단은 아래에서 Enter로 구분)

        is_quote = use_quotes and looks_like_quote(line)
        if is_quote:
            subtitle = _strip_quote_markers(line)
            # 인용구는 너무 길면 소제목이 아니므로 일반 문단으로 처리
            if len(subtitle) <= 40:
                try:
                    if use_divider:
                        insert_naver_divider(driver)   # 소제목 앞 구분선
                        _focus_body()
                    insert_quote_block(driver, subtitle, style=quote_style)
                    _focus_body()          # ★ 인용구 뒤 본문 영역 재클릭
                    wrote_any = True
                    continue
                except Exception:
                    pass  # 실패하면 일반 문단으로 폴백
            line = subtitle  # 마커 제거하고 일반 문단으로

        # 일반 문단 입력
        text = _strip_quote_markers(line) if line.startswith(">") else line
        pyperclip.copy(text)
        safe_hotkey(driver, "ctrl", "v")
        safe_press(driver, "enter")
        time.sleep(0.1)
        wrote_any = True

    if not wrote_any:
        log("❌ 본문 내용이 비어 있습니다")
        driver.switch_to.default_content()
        return False
    log("✏️ 본문 입력 완료")

    # 이미지 삽입 (본문 끝에 순서대로)
    if images:
        ok_n = 0
        for p in images:
            try:
                if insert_naver_image(driver, p, log):
                    ok_n += 1
                    time.sleep(1)
            except Exception as e:
                log(f"⚠️ 이미지 삽입 실패(계속 진행): {str(e)[:60]}")
        if ok_n:
            log(f"🖼️ 이미지 {ok_n}개 삽입 완료")

    # 정렬 적용 (가운데 정렬 등)
    align = cfg.get("text_align", "left")
    if align and align != "left":
        try:
            apply_alignment(driver, align)
            log(f"📐 {TEXT_ALIGN_LABELS.get(align, align)} 적용")
        except Exception as e:
            log(f"⚠️ 정렬 적용 실패(계속 진행): {e}")

    time.sleep(1)

    step("publish")
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

            # 예약발행이면 발행 레이어에서 예약 옵션 + 시간 설정
            if cfg.get("publish_mode") == "schedule":
                _set_schedule_and_publish(
                    driver, cfg.get("schedule_time", ""), log)

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
            if cfg.get("publish_mode") == "schedule":
                log("⏰ 예약발행 완료!")
            else:
                log("🚀 발행 완료!")
    except Exception as e:
        log(f"❌ 발행/저장 실패: {e}")
        driver.switch_to.default_content()
        return False

    step("done")
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
    실패한 카드는 한 번 더 시도하고, 최종 수집 결과를 로그로 알린다.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    base_url = driver.current_url
    # 지연 로딩/무한스크롤 라이브러리 대비: 카드 수가 안 늘 때까지 스크롤
    _scroll_page(driver)
    prev = -1
    for _ in range(8):
        cards_now = [t for t in _mark_cards(driver) if _looks_like_note_card(t)]
        if len(cards_now) <= prev:
            break
        prev = len(cards_now)
        _scroll_page(driver)
        if len(cards_now) >= max_notes:
            break

    card_texts = _mark_cards(driver)
    targets = [t for t in card_texts if _looks_like_note_card(t)][:max_notes]
    if not targets:
        return []
    log(f"🃏 노트 카드 {len(targets)}개를 발견했습니다. 하나씩 열어 주소를 수집합니다...")

    def _open_card(card_text):
        """카드를 클릭해 이동한 주소를 반환. 실패하면 None."""
        texts_now = _mark_cards(driver)
        idx = next((i for i, t in enumerate(texts_now) if t == card_text), None)
        if idx is None:
            # 시간 표기 등이 바뀌었을 수 있으니 앞부분만 매칭
            head = card_text[:25]
            idx = next((i for i, t in enumerate(texts_now) if t[:25] == head), None)
        if idx is None:
            return None
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[data-lilys-card='{idx}']")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.5)
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
        except Exception:
            return None

        deadline = time.time() + 10
        while time.time() < deadline:
            if driver.current_url != base_url:
                new_url = driver.current_url
                try:
                    driver.back()
                except Exception:
                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass
                time.sleep(3)
                if driver.current_url != base_url:
                    safe_get(driver, base_url)
                    time.sleep(4)
                return new_url
            time.sleep(0.5)

        # 주소가 안 바뀌었으면 모달이 열렸을 수 있으니 ESC로 닫기
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(1)
        except Exception:
            pass
        return None

    collected, seen_urls = [], set()
    pending = list(targets)
    for attempt in (1, 2):
        still_failed = []
        for card_text in pending:
            url = _open_card(card_text)
            if url and url not in seen_urls:
                seen_urls.add(url)
                lines = [ln.strip() for ln in card_text.splitlines() if ln.strip()]
                title = max(lines, key=len) if lines else card_text[:60]
                collected.append((url, title))
                log(f"  ✔ 수집 {len(collected)}/{len(targets)}: {title[:40]}")
            elif not url:
                still_failed.append(card_text)
        pending = still_failed
        if not pending:
            break
        if attempt == 1:
            log(f"⚠️ 카드 {len(pending)}개가 열리지 않아 한 번 더 시도합니다...")
            safe_get(driver, base_url)
            time.sleep(4)

    if pending:
        for t in pending:
            log(f"  ✖ 열기 실패로 건너뜀: {t.splitlines()[0][:40]}")
    log(f"🃏 카드 수집 완료: {len(collected)}/{len(targets)}개")
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
        safe_get(driver, url)
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

# 확장 리포트 즐겨찾기 프리셋 (드롭다운에서 선택)
REPORT_PRESETS = ["", "블로그_글+제목 (트렌드)", "유튜브 숏츠", "스크립트", "카툰"]

def _dump_report_debug(driver, log):
    """확장 리포트를 못 찾았을 때 노트 화면의 버튼/탭 텍스트를 파일로 남긴다."""
    from selenium.webdriver.common.by import By
    try:
        texts = []
        for el in driver.find_elements(By.CSS_SELECTOR, "button, a, [role='tab'], span, div"):
            try:
                t = (el.text or "").strip()
                if t and 1 <= len(t) <= 25 and el.is_displayed():
                    texts.append(t)
            except Exception:
                continue
        seen, uniq = set(), []
        for t in texts:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        path = os.path.join(BASE_DIR, "report_debug.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"URL: {driver.current_url}\n\n[노트 화면의 짧은 텍스트들]\n")
            f.write("\n".join(uniq[:120]))
        log(f"🛠 확장 진단 파일 저장: {path} (이 내용을 보여주시면 버튼을 맞춰드립니다)")
    except Exception:
        pass

def _click_report_tab(driver, report_name: str, log) -> bool:
    """
    노트 페이지에서 확장 리포트를 선택해 연다.
    1) 이미 열려 있는 탭이면 바로 클릭
    2) 없으면 '확장' 버튼 → '확장 리포트 추가' 팝업에서 즐겨찾기 카드 선택 → '추가'
    report_name 예: '블로그_글+제목 (트렌드)', '유튜브 숏츠', '스크립트', '카툰'
    """
    from selenium.webdriver.common.by import By

    # 이름 매칭용 키워드 (괄호/공백 앞부분만 써도 매칭)
    key = re.split(r"[\s(]", report_name.strip())[0] if report_name else ""

    def _find_clickable(keyword: str):
        """텍스트에 keyword를 포함하는, 클릭 가능한 가장 안쪽 요소를 찾는다."""
        found = []
        for el in driver.find_elements(
                By.XPATH, f"//*[contains(normalize-space(.), '{keyword}')]"):
            try:
                if el.is_displayed():
                    found.append(el)
            except Exception:
                continue
        # 가장 안쪽(자식이 keyword를 안 가진) 요소 우선
        for el in reversed(found):
            return el
        return None

    # 1) 이미 열린 탭이면 바로 클릭
    tab = _find_clickable(key) if key else None
    if tab:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
            time.sleep(0.3)
            tab.click()
            time.sleep(4)
            log(f"📑 '{report_name}' 리포트를 열었습니다")
            return True
        except Exception:
            pass

    # 2) '확장' 버튼 클릭 → 팝업 열기
    opened = False
    for label in ("확장", "Expand", "리포트"):
        for el in driver.find_elements(
                By.XPATH, f"//*[normalize-space(text())='{label}']"):
            try:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(2)
                    opened = True
                    log(f"📑 '{label}' 버튼을 눌러 확장 리포트 목록을 열었습니다")
                    break
            except Exception:
                continue
        if opened:
            break
    if not opened:
        log("⚠️ 노트 화면에서 '확장' 버튼을 찾지 못했습니다")
        _dump_report_debug(driver, log)

    if opened:
        # 팝업(확장 리포트 추가)에서 즐겨찾기 카드 선택
        card = _find_clickable(key) if key else None
        if card:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                time.sleep(0.3)
                card.click()
                time.sleep(1)
                # '추가' 버튼이 있으면 눌러 생성/열기 진행
                for btn in driver.find_elements(
                        By.XPATH, "//button[normalize-space(text())='추가']"):
                    try:
                        if btn.is_displayed() and btn.is_enabled():
                            btn.click()
                            break
                    except Exception:
                        continue
                log(f"📑 확장에서 '{report_name}' 리포트를 선택했습니다 (생성 대기)")
                time.sleep(8)  # 리포트 생성/로딩 대기
                return True
            except Exception as e:
                log(f"⚠️ 리포트 선택 중 오류: {e}")
        else:
            log(f"⚠️ 팝업에서 '{key}' 카드를 찾지 못했습니다")
            _dump_report_debug(driver, log)
        # 못 찾았으면 팝업 닫기
        for el in driver.find_elements(
                By.XPATH, "//*[normalize-space(text())='취소']"):
            try:
                if el.is_displayed():
                    el.click()
                    break
            except Exception:
                continue

    log(f"⚠️ '{report_name}' 리포트를 찾지 못해 기본 요약을 가져옵니다.")
    return False

def _click_summary_length(driver, length: str, log):
    """요약 길이 버튼(짧게/기본/길게/쉽게)을 클릭한다."""
    from selenium.webdriver.common.by import By
    for el in driver.find_elements(
            By.XPATH, f"//*[normalize-space(text())='{length}']"):
        try:
            if el.is_displayed():
                el.click()
                time.sleep(5)  # 길이 변경 후 내용 갱신 대기
                log(f"📏 요약 길이 '{length}' 적용")
                return True
        except Exception:
            continue
    log(f"⚠️ 요약 길이 '{length}' 버튼을 찾지 못해 기본 길이로 가져옵니다")
    return False

def _scroll_page(driver):
    """지연 로딩(lazy-load) 이미지를 띄우기 위해 페이지를 천천히 스크롤한다."""
    try:
        h = driver.execute_script("return document.body.scrollHeight") or 0
        pos = 0
        while pos < h:
            driver.execute_script(f"window.scrollTo(0, {pos});")
            time.sleep(0.4)
            pos += 600
            h = driver.execute_script("return document.body.scrollHeight") or h
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
    except Exception:
        pass

def _collect_note_images(driver, max_count: int, log=None) -> list[str]:
    """노트 본문의 이미지/인포그래픽 URL을 수집한다 (아이콘·아바타 제외).
    <img> 의 src/srcset 과 CSS background-image 를 모두 훑고,
    지연 로딩을 위해 먼저 페이지를 스크롤한다."""
    from selenium.webdriver.common.by import By

    _scroll_page(driver)

    # 1) <img> 태그에서 실제 로딩된 주소 수집 (JS로 currentSrc 까지)
    raw = driver.execute_script("""
        const out = [];
        document.querySelectorAll('img').forEach(im => {
            const s = im.currentSrc || im.src || '';
            const w = im.naturalWidth || 0, h = im.naturalHeight || 0;
            if (s) out.push([s, w, h]);
        });
        // CSS background-image 도 수집
        document.querySelectorAll('*').forEach(el => {
            const bg = getComputedStyle(el).backgroundImage || '';
            const m = bg.match(/url\\(["']?(.*?)["']?\\)/);
            if (m && m[1] && m[1].startsWith('http')) out.push([m[1], 0, 0]);
        });
        return out;
    """) or []

    total = len(raw)
    urls, seen = [], set()
    for item in raw:
        try:
            src, w, h = item[0], int(item[1] or 0), int(item[2] or 0)
            if not src or src in seen or src.startswith("data:"):
                continue
            low = src.lower()
            if any(k in low for k in ("logo", "icon", "avatar", "profile",
                                      "favicon", "sprite", "emoji", ".svg")):
                continue
            # 크기 정보가 있을 때만 소형 이미지 제외 (없으면 통과)
            if (w and w < 150) or (h and h < 150):
                continue
            seen.add(src)
            urls.append(src)
            if len(urls) >= max_count:
                break
        except Exception:
            continue

    if log:
        log(f"🖼️ (진단) 화면의 이미지 후보 {total}개 중 콘텐츠 이미지 {len(urls)}개 선별")
        if total and not urls:
            samples = [str(x[0])[:70] for x in raw[:5]]
            log("🖼️ (진단) 걸러진 예시: " + " | ".join(samples))
    return urls

def _collect_note_images_old(driver, max_count: int) -> list[str]:
    """(미사용) 이전 방식."""
    from selenium.webdriver.common.by import By

    urls, seen = [], set()
    for img in driver.find_elements(By.TAG_NAME, "img"):
        try:
            src = img.get_attribute("src") or ""
            if not src or src in seen or src.startswith("data:"):
                continue
            low = src.lower()
            if any(k in low for k in ("logo", "icon", "avatar", "profile",
                                      "favicon", "sprite", "emoji")):
                continue
            try:
                w = int(img.get_attribute("naturalWidth") or 0)
                h = int(img.get_attribute("naturalHeight") or 0)
            except Exception:
                w = h = 0
            if (w and w < 200) or (h and h < 200):
                continue
            seen.add(src)
            urls.append(src)
            if len(urls) >= max_count:
                break
        except Exception:
            continue
    return urls

def download_images(urls: list[str], log) -> list[str]:
    """이미지 URL들을 임시 폴더에 내려받아 로컬 경로 목록을 반환한다."""
    import tempfile
    out_dir = os.path.join(tempfile.gettempdir(), "lilys_blog_imgs")
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://lilys.ai/"}
    for i, url in enumerate(urls):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            ext = ".png"
            ct = r.headers.get("Content-Type", "")
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
            elif "webp" in ct:
                ext = ".webp"
            elif "gif" in ct:
                ext = ".gif"
            p = os.path.join(out_dir, f"img_{int(time.time())}_{i}{ext}")
            with open(p, "wb") as f:
                f.write(r.content)
            paths.append(p)
        except Exception as e:
            log(f"⚠️ 이미지 내려받기 실패({i + 1}): {str(e)[:60]}")
    return paths

def _img_max(cfg: dict) -> int:
    if cfg.get("image_enabled", "on") == "off":
        return 0
    return _cfg_int(cfg, "image_max", 5)

# 화면 스크래핑 시 섞여 들어오는 Lilys UI 문구 (이 줄들은 제거)
_UI_NOISE = {
    "요약", "확장", "짧게", "기본", "길게", "쉽게", "공유", "고급", "고급모델",
    "고급 모델", "하이라이트", "구독", "즐겨찾기", "더보기", "확장 리포트 추가",
    "만들기", "취소", "추가", "복사", "복사하기", "다운로드", "NOW", "홈",
    "라이브러리", "미분류", "휴지통", "새로 추가하기",
}
_UI_NOISE_PREFIX = (
    "블로그_글", "유튜브 숏츠", "스크립트", "카툰", "주요", "핵심", "캡처",
    "관련 배경지식", "반대 시각", "내 액션아이템", "댓글분석",
    "Gemini", "LILY", "조회수", "개월 전", "주 전", "일 전", "시간 전",
    "나만의 템플릿", "더 깊이 이해하기",
)

# 이 문구가 나오면 그 아래는 본문이 아니라 하단 UI/관련영상/채팅이므로 잘라낸다
_FOOTER_MARKERS = (
    "다시 보고 싶은", "아카이브로 이동", "자료를 바탕으로 추가 리서치",
    "정리해드릴까요", "정리해줘", "별로야", "시각자료", "마인드맵",
    "인포그래픽", "애니메이션", "이해 점검하기", "플래시카드", "팟캐스트",
    "1개 추가", "추가 리서치 수행",
)

def _truncate_footer(text: str) -> str:
    """하단 UI/관련영상/채팅 영역을 통째로 잘라낸다."""
    if not text:
        return ""
    lines_all = text.splitlines()
    cut = len(lines_all)
    for i, ln in enumerate(lines_all):
        s = ln.strip()
        if any(s.startswith(mk) for mk in _FOOTER_MARKERS):
            cut = i
            break
    return "\n".join(lines_all[:cut]).strip()

def _clean_scraped_text(raw: str) -> str:
    """화면에서 긁은 텍스트에서 Lilys UI 메뉴/버튼 문구 줄을 제거한다."""
    if not raw:
        return ""
    raw = _truncate_footer(raw)
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        if s in _UI_NOISE:
            continue
        # 줄 전체가 메뉴 단어들로만 이뤄진 경우 제거 (예: "짧게 기본 길게 쉽게")
        words = s.split()
        if len(words) >= 2 and all(w in _UI_NOISE for w in words):
            continue
        if any(s.startswith(p) for p in _UI_NOISE_PREFIX):
            continue
        # 아주 짧은 메뉴성 한 단어 줄(2자 이하)도 제거
        if len(s) <= 2 and not any(ch.isdigit() for ch in s):
            continue
        out.append(s)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _copy_note_body(driver, log) -> str:
    """
    노트 하단의 '복사하기' 버튼을 눌러 클립보드로 깔끔한 본문만 가져온다.
    성공하면 복사된 텍스트, 실패하면 빈 문자열.
    """
    import pyperclip
    from selenium.webdriver.common.by import By

    try:
        pyperclip.copy("__LILYS_EMPTY__")  # 이전 내용 초기화(변화 감지용)
    except Exception:
        pass

    # 복사 버튼 후보: aria-label/title 에 '복사', class 에 copy, 또는 복사 아이콘 버튼
    candidates = []
    xpaths = [
        "//button[contains(@aria-label,'복사') or contains(@title,'복사')]",
        "//*[@role='button'][contains(@aria-label,'복사') or contains(@title,'복사')]",
        "//button[contains(@aria-label,'opy') or contains(@title,'opy')]",
        "//button[normalize-space(text())='복사' or normalize-space(text())='복사하기']",
    ]
    for xp in xpaths:
        try:
            candidates += driver.find_elements(By.XPATH, xp)
        except Exception:
            continue
    # 클래스/데이터 속성에 copy 가 든 버튼도 후보에 추가
    try:
        candidates += driver.find_elements(
            By.CSS_SELECTOR, "button[class*='copy'], [data-action*='copy'], [class*='Copy']")
    except Exception:
        pass

    for el in candidates:
        try:
            if not el.is_displayed():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.3)
            driver.execute_script("arguments[0].click();", el)
            time.sleep(1.2)
            txt = ""
            try:
                txt = pyperclip.paste() or ""
            except Exception:
                txt = ""
            if txt and txt != "__LILYS_EMPTY__" and len(txt.strip()) > 100:
                log("📋 복사하기 버튼으로 본문을 가져왔습니다")
                return _truncate_footer(txt.strip())
        except Exception:
            continue
    return ""

def fetch_note_content(browser: Browser, note_url: str, log,
                       report_name: str = "",
                       summary_length: str = "",
                       image_max: int = 0) -> tuple[str, str, list]:
    """
    노트 페이지에서 (제목, 본문 텍스트, 이미지경로목록)을 추출한다.
    report_name 이 지정되면 해당 탭(확장 리포트)을 클릭한 뒤 내용을 가져온다.
    summary_length(짧게/길게/쉽게)가 지정되면 요약 길이를 바꾼 뒤 가져온다.
    image_max > 0 이면 그만큼 본문 이미지를 내려받아 경로로 반환한다.
    """
    from selenium.webdriver.common.by import By

    driver = browser.get_driver()
    safe_get(driver, note_url)
    time.sleep(6)

    title = ""
    try:
        title = driver.find_element(By.CSS_SELECTOR, "h1").text.strip()
    except Exception:
        pass

    if report_name:
        log(f"📑 확장 리포트 '{report_name}' 적용을 시도합니다...")
        ok = _click_report_tab(driver, report_name, log)
        # 리포트가 실제로 열렸는지 확인용 진단은 항상 남겨 둠
        _dump_report_debug(driver, log)
        if not ok:
            log("⚠️ 리포트를 확실히 열지 못했을 수 있습니다. "
                "report_debug.txt 의 버튼/탭 목록을 보내주시면 정확히 맞추겠습니다.")
    elif summary_length and summary_length != "기본":
        _click_summary_length(driver, summary_length, log)

    # 1) '복사하기' 버튼으로 깔끔한 본문 확보 (UI 잡문구 없이)
    body = _copy_note_body(driver, log)

    # 2) 복사가 안 되면 화면 텍스트를 긁되, UI 잡문구를 걸러낸다
    if not body or len(body) < 100:
        raw = ""
        for css in ("article", "main", "body"):
            try:
                el = driver.find_element(By.CSS_SELECTOR, css)
                raw = el.text.strip()
                if len(raw) > 200:
                    break
            except Exception:
                continue
        body = _clean_scraped_text(raw)
        if raw:
            log("📄 복사 버튼을 못 찾아 화면 텍스트에서 UI 문구를 걸러 가져왔습니다")

    images = []
    if image_max > 0:
        try:
            urls = _collect_note_images(driver, image_max, log)
            if urls:
                log(f"🖼️ 본문 이미지 {len(urls)}개 발견, 내려받는 중...")
                images = download_images(urls, log)
                log(f"🖼️ 이미지 {len(images)}개 준비 완료")
            else:
                log("🖼️ 가져올 본문 이미지를 찾지 못했습니다 "
                    "(이미지가 캔버스로 그려지거나 접근이 막힌 경우)")
        except Exception as e:
            log(f"⚠️ 이미지 수집 실패: {e}")

    return title, body, images

# ──────────────────────────────────────────────
# 백그라운드 워커
# ──────────────────────────────────────────────
class Worker:
    """유튜브 단건 처리 / 라이브러리 감시 루프를 담당."""

    def __init__(self, log_fn, step_fn=None):
        self.log = log_fn
        self.step = step_fn or (lambda key: None)
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
                safe_get(driver, "https://nid.naver.com/nidlogin.login")
                driver.execute_script("window.open('https://lilys.ai', '_blank');")
                self.log("🔑 브라우저가 열렸습니다. 네이버와 Lilys AI에 로그인해 주세요.")
                self.log("   로그인 후 창을 닫지 말고 그대로 두면 세션이 프로필에 저장됩니다.")
            except Exception as e:
                self.log(f"❌ 브라우저 실행 실패: {e}")
        threading.Thread(target=_run, daemon=True).start()

    # ── 네이버 로그인 테스트 ──
    def test_naver_login(self, cfg):
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                self.log("🔐 네이버 로그인 테스트를 시작합니다...")
                browser = self._get_browser(cfg)
                driver = browser.get_driver()
                if ensure_naver_login(driver, cfg, self.log):
                    self.log("✅ 로그인 테스트 성공! 이제 [포스팅 시작]을 누르면 바로 발행됩니다.")
                else:
                    self.log("❌ 로그인 테스트 실패. 크롬 창에서 직접 로그인한 뒤 다시 테스트해 주세요.")
            except Exception as e:
                self.log(f"❌ 로그인 테스트 오류: {e}")
            finally:
                self._busy.release()
        threading.Thread(target=_run, daemon=True).start()

    # ── 방식 0: Lilys 노트 링크 → 블로그 (API 불필요) ──
    def post_note_link(self, cfg, note_url: str):
        def _run():
            if not self._busy.acquire(blocking=False):
                self.log("⚠️ 이미 작업이 진행 중입니다.")
                return
            try:
                self.log(f"▶ Lilys 노트 가져오기: {note_url}")
                self.step("extract")
                browser = self._get_browser(cfg)
                title, body, images = fetch_note_content(browser, note_url, self.log,
                        report_name=cfg.get("lilys_report_name", ""),
                        summary_length=cfg.get("lilys_summary_length", ""),
                        image_max=_img_max(cfg))
                if not body or len(body) < 100:
                    self.log("❌ 노트 본문을 가져오지 못했습니다. Lilys 로그인 상태와 링크를 확인해 주세요.")
                    return
                if not title:
                    title = body.strip().splitlines()[0][:80]
                self.step("transform")
                title, body = ai_rewrite(cfg, title, body, self.log)
                body = prepare_body(cfg, body) + f"\n\n원본 노트: {note_url}"
                self.log(f"📄 노트 내용 추출 완료: {title}")

                ok = post_to_naver_blog(
                    browser, cfg, title, body, self.log, step=self.step, images=images)
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
                self.step("source")
                self.log("📥 라이브러리 목록을 불러오는 중...")
                browser = self._get_browser(cfg)
                notes = fetch_collection_notes(
                    browser, self.log, cfg.get("lilys_folder_name", ""),
                    max_notes=_fetch_count(cfg))
                if notes:
                    save_notes_cache(notes)
                    self.log(f"🔍 라이브러리에서 노트 {len(notes)}개를 찾아 저장했습니다")
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
                    self.step("extract")
                    title, body, images = fetch_note_content(browser, url, self.log,
                        report_name=cfg.get("lilys_report_name", ""),
                        summary_length=cfg.get("lilys_summary_length", ""),
                        image_max=_img_max(cfg))
                    if not body or len(body) < 100:
                        self.log(f"⚠️ 본문 추출 실패, 건너뜁니다: {list_title}")
                        _set(url, "실패")
                        continue
                    title = title or list_title
                    self.step("transform")
                    title, body = ai_rewrite(cfg, title, body, self.log)
                    body = prepare_body(cfg, body) + f"\n\n원본 노트: {url}"
                    ok = post_to_naver_blog(
                        browser, cfg, title, body, self.log, step=self.step, images=images)
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
                title, body, _imgs = fetch_note_content(browser, url, self.log,
                        report_name=cfg.get("lilys_report_name", ""),
                        summary_length=cfg.get("lilys_summary_length", ""),
                        image_max=0)
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
                self.step("extract")
                request_id = lilys_request_summary(
                    cfg["lilys_api_key"], youtube_url,
                    cfg["model_type"], cfg["result_language"])
                self.log(f"📨 요약 요청 완료 (requestId: {request_id})")

                title, body = lilys_poll_result(
                    cfg["lilys_api_key"], request_id, self.log)
                if not title:
                    title = body.strip().splitlines()[0][:80]
                self.step("transform")
                title, body = ai_rewrite(cfg, title, body, self.log)
                body = prepare_body(cfg, body)
                body += f"\n\n출처 영상: {youtube_url}"
                self.log(f"📄 요약 완료: {title}")

                browser = self._get_browser(cfg)
                ok = post_to_naver_blog(
                    browser, cfg, title, body, self.log, step=self.step)
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
                        max_notes=_fetch_count(cfg))
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
                            title, body, images = fetch_note_content(browser, url, self.log,
                        report_name=cfg.get("lilys_report_name", ""),
                        summary_length=cfg.get("lilys_summary_length", ""),
                        image_max=_img_max(cfg))
                            if not body or len(body) < 100:
                                self.log("⚠️ 본문 추출 실패, 다음 주기에 다시 시도합니다.")
                                continue
                            title = title or list_title
                            title, body = ai_rewrite(cfg, title, body, self.log)
                            body = prepare_body(cfg, body) + f"\n\n원본 노트: {url}"
                            ok = post_to_naver_blog(
                                browser, cfg, title, body, self.log, step=self.step, images=images)
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
# 포스팅 진행 단계 (스텝바)
POST_STEPS = [
    ("source",    "소스 선택"),
    ("extract",   "추출"),
    ("transform", "변형"),
    ("login",     "로그인"),
    ("write",     "작성"),
    ("publish",   "포스팅"),
]

# 이미지 사용 여부
IMAGE_ENABLED_LABELS = {
    "on": "🖼️ 이미지 가져오기",
    "off": "🚫 이미지 안 씀",
}
IMAGE_ENABLED_CODES = {v: k for k, v in IMAGE_ENABLED_LABELS.items()}

# 단계별 설정 그룹 (탭 네비게이션용) — (설정키, 라벨, 비밀번호여부)
STEP_FIELDS = {
    "source": [
        ("lilys_folder_name",      "라이브러리 폴더 이름 (비우면 전체)", False),
        ("max_fetch_count",        "가져올 노트 개수 (최대)", False),
        ("lilys_report_name",      "확장 리포트 선택 (비우면 요약)", False),
        ("lilys_api_key",          "Lilys API Key (선택, 유튜브 직접 요약용)", True),
        ("model_type",             "요약 모델 (gpt-3.5 / gpt-4)", False),
        ("result_language",        "요약 언어 (ko / en)",   False),
    ],
    "extract": [
        ("lilys_summary_length",   "요약 길이",             False),
        ("image_enabled",          "이미지",                False),
        ("image_max",              "가져올 이미지 개수 (최대)", False),
    ],
    "transform": [
        ("transform_mode",         "본문 변형",             False),
        ("ai_provider",            "AI 글 새로 생성",       False),
        ("openai_key",             "OpenAI API Key",        True),
        ("gpt_model",              "GPT 모델",              False),
        ("gemini_key",             "Gemini API Key",        True),
        ("gemini_model",           "Gemini 모델",           False),
        ("paragraph_style",        "문단 나누기",           False),
        ("line_max_chars",         "한 줄 글자 수 (줄바꿈 기준)", False),
        ("use_quotes",             "인용구",                False),
        ("quote_style",            "인용구 스타일",         False),
        ("use_divider",            "구분선",                False),
        ("text_align",             "본문 정렬",             False),
    ],
    "login": [
        ("naver_id",               "네이버 ID (자동 로그인용)", False),
        ("naver_pw",               "네이버 비밀번호",       True),
        ("naver_blog_id",          "블로그 ID (blog.naver.com/여기)", False),
        ("chrome_profile_dir",     "크롬 프로필 폴더",      False),
    ],
    "write": [],  # 작성 단계는 별도 설정 없음 (안내만 표시)
    "publish": [
        ("publish_mode",           "발행 방식",             False),
        ("schedule_time",          "예약 시간 (예: 2026-07-15 09:00)", False),
        ("check_interval_minutes", "라이브러리 체크 주기 (분)", False),
    ],
}

def _combo_values_for(key):
    return {
        "publish_mode": list(PUBLISH_MODE_LABELS.values()),
        "transform_mode": list(TRANSFORM_MODE_LABELS.values()),
        "ai_provider": list(AI_PROVIDER_LABELS.values()),
        "gpt_model": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "gemini_model": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
        "paragraph_style": list(PARAGRAPH_LABELS.values()),
        "line_max_chars": ["20", "25", "30", "35", "40", "50"],
        "use_quotes": list(USE_QUOTES_LABELS.values()),
        "quote_style": list(QUOTE_STYLE_LABELS.values()),
        "use_divider": list(DIVIDER_LABELS.values()),
        "text_align": list(TEXT_ALIGN_LABELS.values()),
        "lilys_summary_length": SUMMARY_LENGTHS,
        "image_enabled": list(IMAGE_ENABLED_LABELS.values()),
        "image_max": ["3", "5", "8", "10"],
        "max_fetch_count": ["전체", "3", "5", "10", "20", "30", "50"],
    }.get(key)

def _fetch_count(cfg) -> int:
    """가져올 노트 개수. '전체'면 사실상 제한 없음(9999)."""
    v = str(cfg.get("max_fetch_count", 10)).strip()
    if v in ("전체", "all", ""):
        return 9999
    return _cfg_int(cfg, "max_fetch_count", 10)
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

class StepBar(tk.Frame):
    """포스팅 진행 단계 표시줄: ① 소스 선택 ─ ② 추출 ─ ... ─ ⑥ 포스팅"""

    C_DONE    = "#22c55e"   # 완료(초록)
    C_ACTIVE  = "#16a34a"   # 진행 중(진초록 배경)
    C_PENDING = "#475569"   # 대기(회색)

    def __init__(self, parent, on_click=None):
        super().__init__(parent, bg=BG)
        self._labels = {}
        self._on_click = on_click
        for i, (key, name) in enumerate(POST_STEPS):
            if i:
                tk.Label(self, text="─", bg=BG, fg="#334155",
                         font=FONT_M).pack(side="left")
            lbl = tk.Label(self, text=f"{i + 1} {name}", bg=BG,
                           fg=self.C_PENDING, font=FONT_M, padx=6, pady=2,
                           cursor="hand2")
            lbl.pack(side="left")
            if on_click:
                lbl.bind("<Button-1>", lambda e, k=key: on_click(k))
            self._labels[key] = lbl
        self.reset()

    def highlight_tab(self, active_key: str):
        """탭 클릭 네비게이션용: 현재 보고 있는 페이지만 강조 (진행 상태와 별개)."""
        for key, name in POST_STEPS:
            lbl = self._labels[key]
            if key == active_key:
                lbl.config(fg="white", bg=self.C_ACTIVE, font=FONT_B)
            else:
                lbl.config(fg=self.C_PENDING, bg=BG, font=FONT_M)

    def reset(self):
        for i, (key, name) in enumerate(POST_STEPS):
            self._labels[key].config(text=f"{i + 1} {name}",
                                     fg=self.C_PENDING, bg=BG,
                                     font=FONT_M)

    def set_active(self, active_key: str):
        """active_key 단계를 진행 중으로, 그 이전 단계는 완료로 표시."""
        keys = [k for k, _ in POST_STEPS]
        if active_key not in keys:
            return
        idx = keys.index(active_key)
        for i, (key, name) in enumerate(POST_STEPS):
            lbl = self._labels[key]
            if i < idx:
                lbl.config(text=f"✔ {name}", fg=self.C_DONE, bg=BG, font=FONT_M)
            elif i == idx:
                lbl.config(text=f"{i + 1} {name}", fg="white",
                           bg=self.C_ACTIVE, font=FONT_B)
            else:
                lbl.config(text=f"{i + 1} {name}", fg=self.C_PENDING,
                           bg=BG, font=FONT_M)

    def all_done(self):
        for key, name in POST_STEPS:
            self._labels[key].config(text=f"✔ {name}", fg=self.C_DONE,
                                     bg=BG, font=FONT_M)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lilys AI → 네이버 블로그 자동 포스팅")
        self.geometry("760x860")
        self.resizable(False, False)
        self.configure(bg=BG)

        self._worker = Worker(log_fn=self._append_log, step_fn=self._on_step)
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="Lilys AI → 네이버 블로그 자동 포스팅",
                 bg=BG, fg=FG, font=FONT_T).pack(pady=(16, 4))
        tk.Label(self, text="유튜브 링크를 요약하거나, Lilys 라이브러리의 새 노트를 감지해 블로그에 자동 발행합니다",
                 bg=BG, fg=FG_DIM, font=FONT_M).pack(pady=(0, 6))

        self._stepbar = StepBar(self, on_click=self._show_page)
        self._stepbar.pack(pady=(0, 4))
        tk.Label(self, text="위 단계를 눌러 각 설정으로 이동하세요",
                 bg=BG, fg="#64748b", font=("맑은 고딕", 9)).pack(pady=(0, 8))

        from tkinter import ttk
        self._cfg_vars = {}
        self._yt_var = tk.StringVar()

        # 단계별 설정 페이지를 한 자리(카드)에 겹쳐두고 하나만 보여준다
        card = tk.Frame(self, bg=SURFACE, height=440)
        card.pack(fill="x", padx=24, pady=4)
        card.pack_propagate(False)
        self._pages = {}

        def _build_field(parent, key, label, secret, row):
            tk.Label(parent, text=label, bg=SURFACE, fg=FG_DIM,
                     font=FONT_M, width=26, anchor="w").grid(
                row=row, column=0, padx=(12, 4), pady=4, sticky="w")
            var = tk.StringVar()
            self._cfg_vars[key] = var
            values = _combo_values_for(key)
            if key == "lilys_report_name":
                # 확장 리포트: 즐겨찾기 프리셋에서 고르되 직접 입력도 가능
                ttk.Combobox(parent, textvariable=var, state="normal",
                             values=REPORT_PRESETS, font=FONT_M, width=38).grid(
                    row=row, column=1, padx=(4, 12), pady=4)
            elif values:
                ttk.Combobox(parent, textvariable=var, state="readonly",
                             values=values, font=FONT_M, width=38).grid(
                    row=row, column=1, padx=(4, 12), pady=4)
            else:
                tk.Entry(parent, textvariable=var, show="*" if secret else "",
                         bg=BG, fg=FG, insertbackground=FG,
                         relief="flat", font=FONT_M, width=40).grid(
                    row=row, column=1, padx=(4, 12), pady=4)

        for step_key, _name in POST_STEPS:
            page = tk.Frame(card, bg=SURFACE)
            self._pages[step_key] = page
            row = 0
            # 소스 선택 페이지에는 유튜브/노트 링크 입력을 함께 배치
            if step_key == "source":
                tk.Label(page, text="유튜브 링크 또는 Lilys 노트 링크",
                         bg=SURFACE, fg=FG_DIM, font=FONT_M, width=26,
                         anchor="w").grid(row=row, column=0, padx=(12, 4),
                                          pady=4, sticky="w")
                tk.Entry(page, textvariable=self._yt_var, bg=BG, fg=FG,
                         insertbackground=FG, relief="flat", font=FONT_M,
                         width=40).grid(row=row, column=1, padx=(4, 12), pady=4)
                row += 1
            # 로그인 페이지: 저장된 네이버 계정 선택/저장/삭제 줄
            if step_key == "login":
                tk.Label(page, text="저장된 네이버 계정", bg=SURFACE, fg=FG_DIM,
                         font=FONT_M, width=26, anchor="w").grid(
                    row=row, column=0, padx=(12, 4), pady=4, sticky="w")
                acc_wrap = tk.Frame(page, bg=SURFACE)
                acc_wrap.grid(row=row, column=1, padx=(4, 12), pady=4, sticky="w")
                self._account_var = tk.StringVar()
                self._account_combo = ttk.Combobox(
                    acc_wrap, textvariable=self._account_var, state="readonly",
                    values=[], font=FONT_M, width=22)
                self._account_combo.pack(side="left")
                self._account_combo.bind(
                    "<<ComboboxSelected>>", lambda e: self._on_account_select())
                tk.Button(acc_wrap, text="저장", font=("맑은 고딕", 9),
                          bg="#0f766e", fg="white", relief="flat",
                          padx=8, pady=2, cursor="hand2",
                          command=self._save_account).pack(side="left", padx=3)
                tk.Button(acc_wrap, text="삭제", font=("맑은 고딕", 9),
                          bg="#7f1d1d", fg="white", relief="flat",
                          padx=8, pady=2, cursor="hand2",
                          command=self._delete_account).pack(side="left")
                row += 1
            for key, label, secret in STEP_FIELDS[step_key]:
                _build_field(page, key, label, secret, row)
                row += 1
            if step_key == "login":
                tk.Label(page, text="↑ 아래 칸에 ID/비밀번호/블로그ID를 넣고 [저장]하면 계정으로 추가됩니다",
                         bg=SURFACE, fg="#64748b", font=("맑은 고딕", 9),
                         justify="left").grid(row=row, column=0, columnspan=2,
                                              padx=12, pady=(2, 0), sticky="w")
                row += 1
            if step_key == "write":
                tk.Label(page, text="작성 단계는 네이버 에디터에 제목·본문·이미지를\n"
                                    "자동으로 입력하는 과정입니다. 별도 설정은 없습니다.",
                         bg=SURFACE, fg=FG_DIM, font=FONT_M, justify="left").grid(
                    row=row, column=0, columnspan=2, padx=12, pady=16, sticky="w")

        self._load_cfg_to_ui()
        self._refresh_accounts_combo()
        self._show_page("source")

        # 버튼 행 1: 설정/로그인
        btn_row1 = tk.Frame(self, bg=BG)
        btn_row1.pack(pady=(10, 3))

        tk.Button(btn_row1, text="💾 설정 저장", font=FONT_B,
                  bg="#475569", fg="white", activebackground="#334155",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._save_cfg).pack(side="left", padx=5)

        tk.Button(btn_row1, text="🔑 로그인용 브라우저 열기", font=FONT_B,
                  bg="#0f766e", fg="white", activebackground="#0d6060",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_open_login).pack(side="left", padx=5)

        tk.Button(btn_row1, text="🔐 네이버 로그인 테스트", font=FONT_B,
                  bg="#9333ea", fg="white", activebackground="#7e22ce",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_test_login).pack(side="left", padx=5)

        # 버튼 행 2: 발행 작업
        btn_row2 = tk.Frame(self, bg=BG)
        btn_row2.pack(pady=(3, 10))

        tk.Button(btn_row2, text="▶ 요약 → 블로그 발행", font=FONT_B,
                  bg=ACCENT, fg="white", activebackground=ACCENT_H,
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_summarize).pack(side="left", padx=5)

        tk.Button(btn_row2, text="📥 라이브러리에서 골라 발행", font=FONT_B,
                  bg="#1d4ed8", fg="white", activebackground="#1e40af",
                  activeforeground="white", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._on_pick_from_library).pack(side="left", padx=5)

        self._btn_watch = tk.Button(
            btn_row2, text="👀 라이브러리 감시 시작", font=FONT_B,
            bg="#b45309", fg="white", activebackground="#92400e",
            activeforeground="white", relief="flat",
            padx=12, pady=6, cursor="hand2",
            command=self._on_toggle_watch)
        self._btn_watch.pack(side="left", padx=5)

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

    # ── 네이버 다중 계정 관리 ────────────────
    def _refresh_accounts_combo(self):
        cfg = load_config()
        accts = cfg.get("naver_accounts", []) or []
        labels = [a.get("id", "") for a in accts if a.get("id")]
        self._account_combo["values"] = labels
        # 현재 활성 ID가 목록에 있으면 콤보에 표시
        cur = self._cfg_vars["naver_id"].get().strip()
        if cur in labels:
            self._account_var.set(cur)

    def _on_account_select(self):
        cfg = load_config()
        sel = self._account_var.get().strip()
        for a in cfg.get("naver_accounts", []) or []:
            if a.get("id") == sel:
                self._cfg_vars["naver_id"].set(a.get("id", ""))
                self._cfg_vars["naver_pw"].set(a.get("pw", ""))
                self._cfg_vars["naver_blog_id"].set(a.get("blog_id", ""))
                self._append_log(f"👤 계정 '{sel}' 선택됨")
                self._save_cfg()
                break

    def _save_account(self):
        nid = self._cfg_vars["naver_id"].get().strip()
        npw = self._cfg_vars["naver_pw"].get().strip()
        bid = self._cfg_vars["naver_blog_id"].get().strip()
        if not nid or not npw:
            messagebox.showwarning("입력 필요", "네이버 ID와 비밀번호를 먼저 입력해 주세요.")
            return
        cfg = load_config()
        accts = cfg.get("naver_accounts", []) or []
        # 같은 ID면 갱신, 없으면 추가
        for a in accts:
            if a.get("id") == nid:
                a["pw"], a["blog_id"] = npw, bid
                break
        else:
            accts.append({"id": nid, "pw": npw, "blog_id": bid})
        cfg["naver_accounts"] = accts
        save_config(cfg)
        self._save_cfg()          # 현재 활성 계정도 저장
        self._refresh_accounts_combo()
        self._account_var.set(nid)
        self._append_log(f"👤 계정 '{nid}' 저장됨 (총 {len(accts)}개)")

    def _delete_account(self):
        sel = self._account_var.get().strip()
        if not sel:
            return
        cfg = load_config()
        accts = [a for a in (cfg.get("naver_accounts", []) or [])
                 if a.get("id") != sel]
        cfg["naver_accounts"] = accts
        save_config(cfg)
        self._refresh_accounts_combo()
        self._account_var.set("")
        self._append_log(f"👤 계정 '{sel}' 삭제됨")

    # ── 단계별 설정 페이지 전환 ──────────────
    def _show_page(self, step_key: str):
        for k, page in self._pages.items():
            page.pack_forget()
        self._pages.get(step_key, self._pages["source"]).pack(
            fill="x", padx=4, pady=6)
        self._stepbar.highlight_tab(step_key)

    # ── 설정 ─────────────────────────────────
    def _load_cfg_to_ui(self):
        cfg = load_config()
        for k, var in self._cfg_vars.items():
            if k == "publish_mode":
                var.set(PUBLISH_MODE_LABELS.get(cfg.get(k, "publish"),
                                                PUBLISH_MODE_LABELS["publish"]))
            elif k == "transform_mode":
                var.set(TRANSFORM_MODE_LABELS.get(cfg.get(k, "clean"),
                                                  TRANSFORM_MODE_LABELS["clean"]))
            elif k == "ai_provider":
                var.set(AI_PROVIDER_LABELS.get(cfg.get(k, "off"),
                                               AI_PROVIDER_LABELS["off"]))
            elif k == "paragraph_style":
                var.set(PARAGRAPH_LABELS.get(cfg.get(k, "airy"),
                                             PARAGRAPH_LABELS["airy"]))
            elif k == "text_align":
                var.set(TEXT_ALIGN_LABELS.get(cfg.get(k, "left"),
                                              TEXT_ALIGN_LABELS["left"]))
            elif k == "use_quotes":
                var.set(USE_QUOTES_LABELS.get(cfg.get(k, "on"),
                                              USE_QUOTES_LABELS["on"]))
            elif k == "quote_style":
                var.set(QUOTE_STYLE_LABELS.get(cfg.get(k, "line"),
                                               QUOTE_STYLE_LABELS["line"]))
            elif k == "use_divider":
                var.set(DIVIDER_LABELS.get(cfg.get(k, "on"),
                                           DIVIDER_LABELS["on"]))
            elif k == "image_enabled":
                var.set(IMAGE_ENABLED_LABELS.get(cfg.get(k, "on"),
                                                 IMAGE_ENABLED_LABELS["on"]))
            elif k == "lilys_summary_length":
                v = cfg.get(k, "기본")
                var.set(v if v in SUMMARY_LENGTHS else "기본")
            else:
                var.set(str(cfg.get(k, "")))

    def _save_cfg(self) -> dict:
        cfg = load_config()
        for k, var in self._cfg_vars.items():
            val = var.get().strip()
            if k == "publish_mode":
                cfg[k] = PUBLISH_MODE_CODES.get(val, "publish")
            elif k == "transform_mode":
                cfg[k] = TRANSFORM_MODE_CODES.get(val, "clean")
            elif k == "ai_provider":
                cfg[k] = AI_PROVIDER_CODES.get(val, "off")
            elif k == "paragraph_style":
                cfg[k] = PARAGRAPH_CODES.get(val, "airy")
            elif k == "text_align":
                cfg[k] = TEXT_ALIGN_CODES.get(val, "left")
            elif k == "use_quotes":
                cfg[k] = USE_QUOTES_CODES.get(val, "on")
            elif k == "quote_style":
                cfg[k] = QUOTE_STYLE_CODES.get(val, "line")
            elif k == "use_divider":
                cfg[k] = DIVIDER_CODES.get(val, "on")
            elif k == "image_enabled":
                cfg[k] = IMAGE_ENABLED_CODES.get(val, "on")
            elif k in ("check_interval_minutes", "max_fetch_count",
                       "line_max_chars", "image_max") and val.isdigit():
                cfg[k] = int(val)
            else:
                cfg[k] = val
        save_config(cfg)
        self._append_log("💾 설정이 저장되었습니다")
        return cfg

    # ── 버튼 핸들러 ──────────────────────────
    def _on_open_login(self):
        cfg = self._save_cfg()
        self._worker.open_login_browser(cfg)

    def _on_test_login(self):
        cfg = self._save_cfg()
        self._worker.test_naver_login(cfg)

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
        cached, updated_at = load_notes_cache()
        if cached:
            # 저장된 목록이 있으면 크롤링 없이 바로 표시 (창 안에서 새로고침 가능)
            self.after(0, self._show_note_picker, cfg, cached, updated_at)
        else:
            self._worker.fetch_notes_async(
                cfg, lambda notes: self.after(0, self._show_note_picker, cfg, notes, ""))

    def _refresh_library(self, cfg, old_win):
        old_win.destroy()
        self._worker.fetch_notes_async(
            cfg, lambda notes: self.after(0, self._show_note_picker, cfg, notes, ""))

    def _show_note_picker(self, cfg, notes, updated_at=""):
        """라이브러리 노트 목록: 체크로 선택, 미리보기, 상태 표시가 있는 창."""
        from tkinter import ttk

        win = tk.Toplevel(self)
        win.title("라이브러리에서 발행할 노트 선택")
        win.geometry("1180x600")
        win.configure(bg=BG)

        posted = load_posted()

        # 상단: 요약/전체선택/새로고침
        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=16, pady=(12, 6))
        count_var = tk.StringVar(value=f"글감 {len(notes)}개 · 선택 0개")
        tk.Label(top, textvariable=count_var, bg=BG, fg=FG,
                 font=FONT_B).pack(side="left")
        cache_note = f"  (저장된 목록 · {updated_at} 기준)" if updated_at else \
                     "  (제목 클릭=체크, 🔍=오른쪽에 미리보기)"
        tk.Label(top, text=cache_note, bg=BG, fg=FG_DIM,
                 font=FONT_M).pack(side="left")
        tk.Button(top, text="🔄 새로고침(다시 크롤링)", font=FONT_M,
                  bg="#334155", fg="white", activebackground="#1e293b",
                  activeforeground="white", relief="flat",
                  padx=10, pady=3, cursor="hand2",
                  command=lambda: self._refresh_library(cfg, win)).pack(side="right")

        # 리포트 선택 줄: 발행 직전에 어떤 확장 리포트로 가져올지 미리 고른다
        rep_row = tk.Frame(win, bg=BG)
        rep_row.pack(fill="x", padx=16, pady=(0, 6))
        tk.Label(rep_row, text="가져올 내용:", bg=BG, fg=FG_DIM,
                 font=FONT_M).pack(side="left", padx=(0, 6))
        report_var = tk.StringVar(value=cfg.get("lilys_report_name", "") or "")
        ttk.Combobox(rep_row, textvariable=report_var, state="normal",
                     values=REPORT_PRESETS, font=FONT_M, width=24).pack(side="left")
        tk.Label(rep_row, text="(비우면 기본 요약)", bg=BG, fg="#64748b",
                 font=("맑은 고딕", 9)).pack(side="left", padx=(6, 0))

        length_var = tk.StringVar(value=cfg.get("lilys_summary_length", "기본"))
        tk.Label(rep_row, text="   요약 길이:", bg=BG, fg=FG_DIM,
                 font=FONT_M).pack(side="left", padx=(12, 6))
        ttk.Combobox(rep_row, textvariable=length_var, state="readonly",
                     values=SUMMARY_LENGTHS, font=FONT_M, width=8).pack(side="left")

        # 표 스타일 (다크)
        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Notes.Treeview", background=SURFACE, foreground=FG,
                        fieldbackground=SURFACE, rowheight=30,
                        font=FONT_M, borderwidth=0)
        style.configure("Notes.Treeview.Heading", background="#1b1b2b",
                        foreground=FG_DIM, font=FONT_B, borderwidth=0)
        style.map("Notes.Treeview", background=[("selected", "#3b2d63")])

        # 본체: 왼쪽 표 + 오른쪽 미리보기 패널
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=16)

        table_frame = tk.Frame(body, bg=BG)
        table_frame.pack(side="left", fill="both", expand=True)
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
        tree.column("title", width=430, anchor="w")
        tree.column("status", width=70, anchor="center", stretch=False)
        tree.column("preview", width=70, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 오른쪽 미리보기 패널
        preview_frame = tk.Frame(body, bg="#16162a", width=420)
        preview_frame.pack(side="right", fill="both", padx=(12, 0))
        preview_frame.pack_propagate(False)
        pv_title_var = tk.StringVar(value="미리보기")
        tk.Label(preview_frame, textvariable=pv_title_var, bg="#16162a", fg=FG,
                 font=FONT_B, wraplength=390, justify="left").pack(
            fill="x", padx=12, pady=(12, 6))
        pv_box = scrolledtext.ScrolledText(
            preview_frame, bg="#0f0f1a", fg=FG, font=FONT_M,
            relief="flat", wrap="word", state="disabled")
        pv_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        def _set_preview(t, body_text):
            pv_title_var.set(t[:120] if t else "미리보기")
            pv_box.config(state="normal")
            pv_box.delete("1.0", "end")
            pv_box.insert("1.0", body_text)
            pv_box.config(state="disabled")

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

        preview_cache: dict = {}  # url -> (제목, 본문)

        def _apply_choice():
            """창에서 고른 리포트/요약 길이를 cfg와 저장 설정에 반영한다."""
            cfg["lilys_report_name"] = report_var.get().strip()
            cfg["lilys_summary_length"] = length_var.get().strip() or "기본"
            # 메인 설정 화면에도 반영 + 파일 저장
            if "lilys_report_name" in self._cfg_vars:
                self._cfg_vars["lilys_report_name"].set(cfg["lilys_report_name"])
            if "lilys_summary_length" in self._cfg_vars:
                self._cfg_vars["lilys_summary_length"].set(cfg["lilys_summary_length"])
            try:
                save_config(cfg)
            except Exception:
                pass

        def _show_preview(iid):
            url, title = url_by_iid[iid]
            _apply_choice()
            preview_cache.pop(url, None)  # 리포트가 바뀌었을 수 있으니 새로 불러옴
            _set_preview(title, "⏳ 미리보기를 불러오는 중입니다...")

            def _on_ready(t, body):
                def _do():
                    plain = prepare_body(cfg, body)
                    preview_cache[url] = (t, plain)
                    _set_preview(t, plain)
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
            _apply_choice()
            rep = cfg.get("lilys_report_name") or "기본 요약"
            self._append_log(f"📑 이번 발행은 '{rep}' 내용으로 가져옵니다")
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

    # ── 진행 단계 표시 ───────────────────────
    def _on_step(self, key: str):
        def _do():
            if key == "done":
                self._stepbar.all_done()
            elif key == "reset":
                self._stepbar.reset()
            else:
                self._stepbar.set_active(key)
        self.after(0, _do)

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
