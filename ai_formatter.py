"""
Claude API를 사용해 스크래핑한 원문을 블로그 포스트 HTML로 변환합니다.
API 키가 없으면 간단한 HTML 포맷팅만 수행합니다.
"""
import re
import textwrap


def format_as_blog_post(title: str, raw_text: str, images: list[str],
                        api_key: str = "", model: str = "claude-haiku-4-5-20251001") -> str:
    """원문 텍스트 → 블로그용 HTML 반환."""
    if api_key:
        return _format_with_claude(title, raw_text, images, api_key, model)
    return _format_simple(title, raw_text, images)


def _format_with_claude(title: str, raw_text: str, images: list[str],
                        api_key: str, model: str) -> str:
    import anthropic

    img_note = ""
    if images:
        img_list = "\n".join(f"- {u}" for u in images[:5])
        img_note = f"\n\n본문에 삽입할 이미지 URL 목록:\n{img_list}"

    prompt = textwrap.dedent(f"""
        다음 웹 콘텐츠를 한국어 블로그 포스트로 작성해 주세요.
        출력 형식은 순수 HTML입니다 (html/head/body 태그 없이 본문 HTML만).

        요구사항:
        - 핵심 내용을 명확하고 읽기 좋게 정리
        - 소제목(<h2>)으로 섹션 구분
        - 중요 키워드는 <strong>으로 강조
        - 이미지 URL이 있으면 적절한 위치에 <img src="URL" style="max-width:100%"> 삽입
        - 마지막에 출처 정보 포함
        - 전체 분량: 800~1500자

        제목: {title}
        {img_note}

        원문 내용:
        {raw_text[:4000]}
    """).strip()

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def _format_simple(title: str, raw_text: str, images: list[str]) -> str:
    """Claude 없이 기본 HTML 구성."""
    lines = [l for l in raw_text.splitlines() if l.strip()]
    paragraphs = []
    for line in lines:
        if len(line) > 150:
            paragraphs.append(f"<p>{_escape(line)}</p>")
        elif len(line) > 30:
            paragraphs.append(f"<h2>{_escape(line)}</h2>")

    body = "\n".join(paragraphs[:30])

    img_html = ""
    if images:
        img_html = "\n".join(
            f'<img src="{u}" alt="이미지" style="max-width:100%;margin:12px 0">'
            for u in images[:3]
        )

    return f"<h1>{_escape(title)}</h1>\n{img_html}\n{body}"


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))
