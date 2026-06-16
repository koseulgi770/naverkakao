"""
URL에서 제목, 본문, 이미지를 추출합니다.
Lilys AI URL인 경우 요약 내용을 우선 추출합니다.
"""
import re
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def is_lilys_url(url: str) -> bool:
    return "lilys.ai" in urlparse(url).netloc


def extract_from_lilys(url: str) -> dict:
    """Lilys AI 요약 페이지에서 콘텐츠 추출."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title = ""
    title_tag = soup.find("h1") or soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    # Lilys AI는 주로 <article> 또는 요약 카드 구조 사용
    content_parts = []
    for tag in soup.select("article, .summary, .content, .note-content, [class*='summary'], [class*='content']"):
        text = tag.get_text(separator="\n", strip=True)
        if len(text) > 100:
            content_parts.append(text)
            break

    if not content_parts:
        # fallback: body 전체 텍스트에서 script/style 제거
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        content_parts.append(soup.get_text(separator="\n", strip=True))

    images = _extract_images(soup, url)
    return {
        "title": title,
        "text": "\n".join(content_parts)[:8000],
        "images": images,
        "source_url": url,
    }


def extract_from_url(url: str) -> dict:
    """일반 URL에서 콘텐츠 추출."""
    if is_lilys_url(url):
        return extract_from_lilys(url)

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # 제목
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"]
    elif soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)
    elif soup.find("title"):
        title = soup.find("title").get_text(strip=True)

    # 본문 (article > main > div 순으로 시도)
    body_text = ""
    for selector in ["article", "main", ".post-content", ".entry-content",
                     ".article-body", "#content", ".content"]:
        tag = soup.select_one(selector)
        if tag:
            for s in tag(["script", "style"]):
                s.decompose()
            body_text = tag.get_text(separator="\n", strip=True)
            if len(body_text) > 200:
                break

    if not body_text:
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        body_text = soup.get_text(separator="\n", strip=True)

    # 빈 줄 정리
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    body_text = "\n".join(lines)

    images = _extract_images(soup, url)

    return {
        "title": title,
        "text": body_text[:8000],
        "images": images,
        "source_url": url,
    }


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[str]:
    """본문 이미지 URL 목록 반환 (최대 10개)."""
    # og:image 우선
    og_img = soup.find("meta", property="og:image")
    imgs = []
    if og_img and og_img.get("content"):
        imgs.append(og_img["content"])

    for img in soup.find_all("img", src=True):
        src = img["src"]
        if not src or src.startswith("data:"):
            continue
        full = urljoin(base_url, src)
        if full not in imgs and _is_content_image(img):
            imgs.append(full)
        if len(imgs) >= 10:
            break
    return imgs


def _is_content_image(img_tag) -> bool:
    """작은 아이콘/광고 이미지 제외."""
    w = img_tag.get("width", "")
    h = img_tag.get("height", "")
    try:
        if int(w) < 100 or int(h) < 100:
            return False
    except (ValueError, TypeError):
        pass
    src = img_tag.get("src", "").lower()
    skip_keywords = ["icon", "logo", "banner", "ad", "pixel", "tracking", "1x1"]
    return not any(kw in src for kw in skip_keywords)


def download_image(url: str) -> tuple[bytes, str]:
    """이미지 다운로드 → (bytes, mime_type)."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return resp.content, ct
