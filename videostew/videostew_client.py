"""
비디오스튜(VideoStew) Automation API 클라이언트.

n8n 워크플로우의 다음 노드들을 파이썬으로 옮긴 것입니다.
  - 4. 비디오스튜 API 호출        -> create_automation()
  - 5. 웹훅 응답 받기 (대체)      -> wait_until_ready()  ※ 웹훅 대신 폴링
  - 6. 프로젝트 제목/설명 호출    -> get_project()

주의: 공식 API 문서(https://videostew.com/ko/dev/docs)가 봇 접근을 차단하고 있어
응답 JSON의 정확한 필드명은 확정하지 못했습니다. 아래 파서는 흔히 쓰이는 후보 키를
방어적으로 탐색하며, 실제 응답과 다르면 _pick() 호출부의 후보 키만 수정하면 됩니다.
처음 실행 시 DEBUG 로그로 원본 응답 전체를 출력하니 그것을 보고 맞추세요.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

log = logging.getLogger("videostew")

BASE_URL = "https://videostew.com/api"
# 비디오스튜 API는 브라우저형 요청을 기대하므로 n8n 워크플로우와 동일하게 UA를 붙인다.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class VideoStewError(RuntimeError):
    pass


class VideoStewClient:
    def __init__(self, api_key: str, api_token: str, timeout: int = 30):
        if not api_key or not api_token:
            raise ValueError("api_key / api_token 이 필요합니다. (videostew.com/ko/dev/apps 에서 발급)")
        self.api_key = api_key
        self.api_token = api_token
        self.timeout = timeout
        self._session = requests.Session()

    # ── 공통 ──────────────────────────────────────────────
    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {
            "x-api-key": self.api_key,
            "x-token": self.api_token,
            # x-nounce: n8n 워크플로우와 동일하게 매 요청 랜덤값
            "x-nounce": str(random.randint(0, 999_999)),
            "User-Agent": _USER_AGENT,
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _pick(data: dict[str, Any], *candidate_keys: str) -> Optional[Any]:
        """중첩('result.title') 및 평면 키 후보들 중 처음 발견되는 값을 반환."""
        for key in candidate_keys:
            cur: Any = data
            ok = True
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
        return None

    # ── 4. 오토메이션 생성 ────────────────────────────────
    def create_automation(self, base_project_id: str, injector: dict[str, Any],
                          webhook_url: str = "") -> dict[str, Any]:
        """
        POST /api/automations 로 영상 생성을 요청한다.
        webhook_url 은 폴링 방식에서는 비워도 되지만, 값이 있으면 그대로 전달한다.
        반환: 원본 응답 JSON (projectId 추출용)
        """
        body: dict[str, Any] = {
            "baseProjectId": base_project_id,
            "injector": injector,
        }
        if webhook_url:
            body["webhookUrl"] = webhook_url

        resp = self._session.post(
            f"{BASE_URL}/automations",
            headers=self._headers(json_body=True),
            json=body,
            timeout=self.timeout,
        )
        self._raise_for_status(resp, "automations 생성")
        data = resp.json()
        log.debug("create_automation 원본 응답: %s", data)
        return data

    @staticmethod
    def extract_project_id(automation_response: dict[str, Any]) -> str:
        pid = VideoStewClient._pick(
            automation_response,
            "projectId", "project_id", "id",
            "result.projectId", "result.project_id", "result.id",
            "data.projectId", "data.id",
        )
        if not pid:
            raise VideoStewError(
                "오토메이션 응답에서 projectId를 찾지 못했습니다. "
                "원본 응답을 확인하고 extract_project_id()의 후보 키를 조정하세요.\n"
                f"응답: {automation_response}"
            )
        return str(pid)

    # ── 6. 프로젝트 조회 ──────────────────────────────────
    def get_project(self, project_id: str) -> dict[str, Any]:
        resp = self._session.get(
            f"{BASE_URL}/projects/{project_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._raise_for_status(resp, f"프로젝트({project_id}) 조회")
        data = resp.json()
        log.debug("get_project 원본 응답: %s", data)
        return data

    # ── 5. 웹훅 대체: 완료까지 폴링 ───────────────────────
    def wait_until_ready(self, project_id: str, poll_interval: int = 15,
                         timeout: int = 1800) -> dict[str, Any]:
        """
        렌더링 완료까지 GET /api/projects/{id} 를 주기적으로 폴링한다.
        완료로 판단되면 {'projectId', 'link', 'title', 'desc', 'raw'} 를 반환.

        완료 판정: 다운로드 링크(link)가 응답에 나타나면 완료로 본다.
        (상태 필드명이 확인되면 _is_ready()를 상태 기반으로 바꾸는 것이 더 정확)
        """
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            project = self.get_project(project_id)
            link = self._pick(
                project,
                "link", "result.link", "downloadUrl", "result.downloadUrl",
                "videoUrl", "result.videoUrl", "export.url", "result.export.url",
            )
            status = self._pick(project, "status", "result.status", "state", "result.state")

            if self._is_failed(status):
                raise VideoStewError(f"렌더링 실패 (status={status}). 응답: {project}")

            if link:
                log.info("렌더링 완료 (%d회 폴링): %s", attempt, link)
                return {
                    "projectId": project_id,
                    "link": str(link),
                    "title": self._pick(project, "result.title", "title", "name", "result.name"),
                    "desc": self._pick(project, "result.desc", "desc", "description", "result.description"),
                    "raw": project,
                }

            log.info("아직 렌더링 중... (%d회, status=%s) %ds 후 재확인",
                     attempt, status, poll_interval)
            time.sleep(poll_interval)

        raise VideoStewError(f"렌더링 완료 대기 시간 초과 ({timeout}s). projectId={project_id}")

    @staticmethod
    def _is_failed(status: Any) -> bool:
        if not status:
            return False
        return str(status).lower() in {"failed", "error", "fail", "canceled", "cancelled"}

    # ── HTTP 오류 처리 ────────────────────────────────────
    @staticmethod
    def _raise_for_status(resp: requests.Response, what: str) -> None:
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise VideoStewError(f"{what} 실패: HTTP {resp.status_code} - {body}")


def download_file(url: str, dest_path: str, timeout: int = 300) -> str:
    """7. 비디오 파일 다운로드. 완성된 mp4 링크를 dest_path 로 저장."""
    headers = {"User-Agent": _USER_AGENT}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    log.info("영상 다운로드 완료: %s", dest_path)
    return dest_path
