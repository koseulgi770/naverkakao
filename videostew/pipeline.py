"""
비디오스튜 자동화 파이프라인 (폴링 방식 · 전체 파이프라인).

n8n 워크플로우 '비디오스튜 자동화 시나리오 샘플'을 파이썬으로 옮긴 것.

흐름:
  1. 구글시트에서 미처리 url 행 조회
  2. injector(wizard) JSON 구성
  3. POST /api/automations 로 영상 생성 요청
  4. GET /api/projects/{id} 폴링으로 렌더링 완료 대기 (웹훅 대체)
  5. 완성 mp4 다운로드
  6. 유튜브 업로드(unlisted)
  7. 시트에 완료 표시

실행:
  python -m videostew.pipeline            # config.json 사용, 1회 실행
  python -m videostew.pipeline --loop     # check_interval_minutes 마다 반복
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time

# 패키지(-m)로도, 스크립트로도 실행 가능하게 임포트 처리
try:
    from .injector import build_injector
    from .sheets import SheetClient, SheetRow
    from .videostew_client import VideoStewClient, download_file
    from .youtube_upload import upload_video
except ImportError:  # python videostew/pipeline.py 로 직접 실행한 경우
    sys.path.insert(0, os.path.dirname(__file__))
    from injector import build_injector
    from sheets import SheetClient, SheetRow
    from videostew_client import VideoStewClient, download_file
    from youtube_upload import upload_video

log = logging.getLogger("videostew.pipeline")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(path: str = CONFIG_FILE) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"설정 파일이 없습니다: {path}\n"
            "config.example.json 을 config.json 으로 복사한 뒤 값을 채워주세요."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def process_row(row: SheetRow, cfg: dict, vs: VideoStewClient, sheet: SheetClient) -> None:
    log.info("▶ 처리 시작: row %d, url=%s", row.row_number, row.url)

    wcfg = cfg.get("wizard", {})
    injector = build_injector(
        row.url,
        language=wcfg.get("language", "ko"),
        duration=wcfg.get("duration", "1m"),
        style=wcfg.get("style", "news"),
        visual=wcfg.get("visual", "ai-image"),
        visual_style=wcfg.get("visualStyle", "photo"),
    )

    # 3. 오토메이션 생성 -> projectId 추출
    resp = vs.create_automation(cfg["baseProjectId"], injector,
                                webhook_url=cfg.get("webhookUrl", ""))
    project_id = VideoStewClient.extract_project_id(resp)
    log.info("오토메이션 생성됨. projectId=%s", project_id)

    # 4. 폴링으로 완료 대기
    result = vs.wait_until_ready(
        project_id,
        poll_interval=cfg.get("poll_interval_seconds", 15),
        timeout=cfg.get("render_timeout_seconds", 1800),
    )
    link = result["link"]
    title = result.get("title") or "무제"
    desc = result.get("desc") or ""

    # 5. 다운로드 -> 6. 유튜브 업로드
    with tempfile.TemporaryDirectory() as tmp:
        mp4 = os.path.join(tmp, f"{project_id}.mp4")
        download_file(link, mp4)

        if cfg.get("youtube", {}).get("enabled", True):
            upload_video(
                mp4,
                title=title,
                description=desc,
                client_secret_file=cfg["youtube"]["client_secret_file"],
                token_file=cfg["youtube"].get("token_file", "youtube_token.json"),
                privacy_status=cfg["youtube"].get("privacy_status", "unlisted"),
            )
        else:
            log.info("youtube.enabled=false → 업로드 건너뜀 (다운로드만 완료)")

    # 7. 시트에 완료 표시
    sheet.mark_processed(row, video_url=link)
    log.info("✅ 완료: row %d", row.row_number)


def run_once(cfg: dict) -> int:
    sheet = SheetClient(
        service_account_file=cfg["google"]["service_account_file"],
        spreadsheet_url=cfg["google"]["spreadsheet_url"],
        worksheet=cfg["google"].get("worksheet", 0),
    )
    vs = VideoStewClient(cfg["apiKey"], cfg["apiToken"])

    rows = sheet.fetch_unprocessed()
    if not rows:
        log.info("ℹ️ 미처리 행 없음")
        return 0

    done = 0
    for row in rows:
        try:
            process_row(row, cfg, vs, sheet)
            done += 1
        except Exception as e:  # 한 행 실패가 전체를 멈추지 않도록
            log.error("❌ row %d 처리 실패: %s", row.row_number, e)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description="비디오스튜 자동화 파이프라인")
    parser.add_argument("--loop", action="store_true",
                        help="check_interval_minutes 마다 반복 실행")
    parser.add_argument("--config", default=CONFIG_FILE, help="설정 파일 경로")
    parser.add_argument("--debug", action="store_true", help="원본 API 응답까지 로그 출력")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)

    if not args.loop:
        run_once(cfg)
        return

    interval = cfg.get("check_interval_minutes", 10) * 60
    log.info("🔁 반복 모드 시작 (매 %d분)", cfg.get("check_interval_minutes", 10))
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            log.error("실행 중 오류: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
