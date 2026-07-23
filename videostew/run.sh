#!/usr/bin/env bash
# ============================================================
#  비디오스튜 자동화 - macOS / Linux 실행기
#  터미널에서:  bash run.sh
# ============================================================
set -e
cd "$(dirname "$0")"

echo
echo "============================================"
echo "  비디오스튜 자동화 파이프라인"
echo "============================================"
echo

# --- 파이썬 확인 -------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "[오류] python3 가 설치되어 있지 않습니다."
  echo "        https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요."
  exit 1
fi

# --- 최초 실행 시 가상환경 생성 ----------------------------
if [ ! -d ".venv" ]; then
  echo "[1/3] 가상환경을 만드는 중... (최초 1회)"
  python3 -m venv .venv
fi

echo "[2/3] 필요한 패키지를 설치하는 중..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

# --- 설정 파일 확인 ----------------------------------------
if [ ! -f "config.json" ]; then
  cp config.example.json config.json
  echo
  echo "[안내] config.json 을 새로 만들었습니다."
  echo "       편집기로 열어 API 키 / 구글시트 주소 등을 채운 뒤 다시 실행하세요."
  echo
  exit 0
fi

echo "[3/3] 파이프라인 실행!"
echo
.venv/bin/python pipeline.py --debug "$@"
