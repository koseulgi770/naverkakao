"""
유튜브 업로드 (n8n '8. 유튜브로 업로드' 대체).

OAuth 2.0 설치형 앱 흐름을 사용한다. 최초 1회 브라우저 인증 후
token.json 에 토큰을 캐시하며, 이후에는 자동 갱신된다.

사전 준비:
  1) Google Cloud Console 에서 'YouTube Data API v3' 활성화
  2) OAuth 클라이언트(데스크톱 앱) 생성 -> client_secret.json 다운로드
     문서: https://docs.n8n.io/integrations/builtin/credentials/google/oauth-single-service/
"""

from __future__ import annotations

import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("videostew.youtube")

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _get_credentials(client_secret_file: str, token_file: str) -> Credentials:
    creds: Credentials | None = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, _SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def upload_video(
    file_path: str,
    title: str,
    description: str,
    *,
    client_secret_file: str,
    token_file: str = "youtube_token.json",
    category_id: str = "25",           # 25 = News & Politics
    privacy_status: str = "unlisted",  # 테스트 단계 권장값
    default_language: str = "ko",
    made_for_kids: bool = False,
) -> str:
    """영상을 업로드하고 videoId 를 반환한다."""
    creds = _get_credentials(client_secret_file, token_file)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": (title or "무제")[:100],
            "description": description or "",
            "categoryId": category_id,
            "defaultLanguage": default_language,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("업로드 진행률 %d%%", int(status.progress() * 100))

    video_id = response["id"]
    log.info("유튜브 업로드 완료: https://youtu.be/%s", video_id)
    return video_id
