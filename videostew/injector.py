"""
n8n '3. 대본 및 이미지 생성 디테일 설정' 노드의 injector JSON을 파이썬으로 구성.

wizard 파라미터 문서: https://videostew.com/ko/dev/docs#wizard
아래는 워크플로우 샘플과 동일한 기본값(1분 news 스타일, 빈 슬라이드는 AI 실사 이미지).
"""

from __future__ import annotations

from typing import Any


def build_injector(
    source_url: str,
    *,
    language: str = "ko",
    duration: str = "1m",
    style: str = "news",
    visual: str = "ai-image",
    visual_style: str = "photo",
    tts_dictionary: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    구글시트에서 받은 기사 URL(source_url)을 headless 위저드 입력으로 변환.

    tts_dictionary: TTS 발음 교정 사전. 예) [{"i": "KAIST", "o": "카이스트"}]
    """
    if tts_dictionary is None:
        tts_dictionary = [{"i": "KAIST", "o": "카이스트"}]

    return {
        "dictionary": {
            "tts": tts_dictionary,
        },
        "wizard": {
            "mode": "headless",
            "source": "url",
            "sourceContent": source_url,
            "language": language,
            "opts": {
                "visual": visual,
                "visualStyle": visual_style,
                "autoLineBreak": "y",
                "replace": "all",
                "minLibraryShareUnit": "1",
            },
            "adjust": {
                "duration": duration,
                "style": style,
            },
        },
    }
