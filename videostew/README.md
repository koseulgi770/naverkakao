# 비디오스튜 자동화 파이프라인 (Python)

n8n 워크플로우 `비디오스튜 자동화 시나리오 샘플`을 파이썬으로 옮긴 것입니다.
**웹훅 대신 폴링 방식**으로 렌더링 완료를 감지하며, 서버 없이 로컬 스크립트로 동작합니다.

```
구글시트(url 행)  →  비디오스튜 API로 영상 생성  →  완료까지 폴링
                 →  mp4 다운로드  →  유튜브 업로드(unlisted)  →  시트에 완료 표시
```

## 구성 파일

| 파일 | 역할 | 대응되는 n8n 노드 |
|------|------|------------------|
| `pipeline.py` | 전체 오케스트레이션 (진입점) | 워크플로우 전체 |
| `sheets.py` | 구글시트 읽기/완료표시 | 1, Mark Row as Processed |
| `injector.py` | wizard/injector JSON 구성 | 3. 대본·이미지 설정 |
| `videostew_client.py` | 오토메이션 생성·폴링·다운로드 | 4, 5(대체), 6, 7 |
| `youtube_upload.py` | 유튜브 업로드 | 8 |

## 설치

```bash
cd videostew
pip install -r requirements.txt
cp config.example.json config.json   # 값 채우기
```

## 준비물

1. **비디오스튜 API 키/토큰** — https://videostew.com/ko/dev/apps 에서 발급 → `apiKey`, `apiToken`
2. **구글시트**
   - 첫 행 헤더: `url | status | videoUrl | completedAt`
   - `url` 열에 영상으로 만들 기사/글 주소를 추가하면 됨 (나머지 열은 자동 기록)
   - Google Cloud 서비스 계정 생성 → JSON 키를 `service_account.json` 으로 저장
   - **시트를 서비스 계정 이메일과 공유(편집자)** 해야 읽기/쓰기가 됩니다
3. **유튜브** (`youtube.enabled: true` 일 때)
   - Google Cloud Console 에서 `YouTube Data API v3` 활성화
   - OAuth 클라이언트(데스크톱 앱) → `client_secret.json` 저장
   - 최초 1회 브라우저 인증 → `youtube_token.json` 자동 생성
   - 참고: https://docs.n8n.io/integrations/builtin/credentials/google/oauth-single-service/

## 실행

```bash
python -m videostew.pipeline           # 1회 실행 (미처리 행 모두 처리)
python -m videostew.pipeline --loop    # check_interval_minutes 마다 반복
python -m videostew.pipeline --debug   # API 원본 응답까지 로그 출력
```

## ⚠️ 중요: API 응답 필드명 확인 필요

비디오스튜 공식 API 문서 페이지가 봇 접근을 차단(403)하여, 아래 응답의 **정확한 필드명을
소스만으로 확정하지 못했습니다.** 그래서 `videostew_client.py`의 파서는 흔한 후보 키
(`projectId`/`id`/`result.projectId`, `link`/`downloadUrl`/`result.link` 등)를
방어적으로 탐색합니다.

**최초 실행은 반드시 `--debug` 로 하세요.** 원본 응답이 로그에 출력됩니다. 만약
`projectId를 찾지 못했습니다` 또는 폴링이 계속 완료되지 않으면, 로그의 실제 응답 구조를
보고 다음 두 지점의 후보 키만 조정하면 됩니다:

- `VideoStewClient.extract_project_id()` — 오토메이션 응답에서 projectId 추출
- `VideoStewClient.wait_until_ready()` 의 `_pick(...)` — 완료 링크/상태 추출

가능하면 [API 플레이그라운드](https://videostew.com/ko/dev/docs)(로그인 후)에서 실제
응답 스키마를 먼저 확인하는 것을 권장합니다.

## n8n 대비 차이점

- **웹훅 → 폴링**: n8n은 비디오스튜가 완료 시 webhookUrl로 콜백했지만, 여기서는 외부에서
  접근 가능한 서버가 필요 없도록 `GET /api/projects/{id}` 를 주기적으로 조회합니다.
  (외부 서버/도메인이 있다면 `webhookUrl` 을 채우고 웹훅 수신기를 붙이는 방식으로 바꿀 수 있음)
- **트리거 → 조회**: 구글시트 실시간 트리거 대신, 실행 시점에 `status` 가 빈 행을 찾아 처리합니다.
