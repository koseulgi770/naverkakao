"""
구글 시트 연동 (n8n '1. 구글 시트 연동' + 'Mark Row as Processed' 대체).

폴링 방식이라 트리거 대신, 시트를 읽어 아직 처리되지 않은 url 행을 찾는다.
서비스 계정(gspread) 방식을 사용한다.

시트 형식(첫 행이 헤더):
  | url | status | videoUrl | completedAt |
  - url         : (필수) 영상으로 만들 기사/글 주소
  - status      : 비어있으면 '미처리'. 완료 시 'completed' 로 기록
  - videoUrl    : 완성 영상 다운로드 링크 기록
  - completedAt : 완료 시각(ISO) 기록
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("videostew.sheets")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COL_URL = "url"
COL_STATUS = "status"
COL_VIDEO_URL = "videoUrl"
COL_COMPLETED_AT = "completedAt"


@dataclass
class SheetRow:
    row_number: int  # 1-based, 헤더 포함(gspread 좌표계)
    url: str


class SheetClient:
    def __init__(self, service_account_file: str, spreadsheet_url: str,
                 worksheet: str | int = 0):
        creds = Credentials.from_service_account_file(service_account_file, scopes=_SCOPES)
        gc = gspread.authorize(creds)
        sh = gc.open_by_url(spreadsheet_url)
        self.ws = sh.get_worksheet(worksheet) if isinstance(worksheet, int) \
            else sh.worksheet(worksheet)
        self._header = self.ws.row_values(1)
        self._col_idx = {name: i + 1 for i, name in enumerate(self._header)}
        if COL_URL not in self._col_idx:
            raise ValueError(f"시트 첫 행에 '{COL_URL}' 컬럼이 필요합니다. 현재 헤더: {self._header}")

    def fetch_unprocessed(self) -> list[SheetRow]:
        """status 가 비어있는(아직 처리 안 된) url 행들을 반환."""
        records = self.ws.get_all_records()  # 헤더 제외한 dict 리스트
        pending: list[SheetRow] = []
        for i, rec in enumerate(records):
            url = str(rec.get(COL_URL, "")).strip()
            status = str(rec.get(COL_STATUS, "")).strip()
            if url and not status:
                pending.append(SheetRow(row_number=i + 2, url=url))  # +2: 헤더(1) + 0-based
        log.info("미처리 행 %d개 발견", len(pending))
        return pending

    def mark_processed(self, row: SheetRow, video_url: str,
                       status: str = "completed") -> None:
        """해당 행에 완료 상태/영상링크/완료시각을 기록 (n8n Mark Row as Processed)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        updates = {
            COL_STATUS: status,
            COL_VIDEO_URL: video_url,
            COL_COMPLETED_AT: now_iso,
        }
        for col_name, value in updates.items():
            col = self._col_idx.get(col_name)
            if col:  # 시트에 해당 컬럼이 있을 때만 기록
                self.ws.update_cell(row.row_number, col, value)
        log.info("행 %d 처리 완료 표시", row.row_number)
