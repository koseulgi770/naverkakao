@echo off
REM ============================================================
REM  비디오스튜 자동화 - 윈도우 원클릭 실행기
REM  더블클릭하면 파이썬 패키지를 설치하고 파이프라인을 실행합니다.
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   비디오스튜 자동화 파이프라인
echo ============================================
echo.

REM --- 파이썬 확인 -------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] 파이썬이 설치되어 있지 않습니다.
    echo         https://www.python.org/downloads/ 에서 설치 후
    echo         설치 화면에서 "Add Python to PATH" 를 반드시 체크하세요.
    echo.
    pause
    exit /b 1
)

REM --- 최초 실행 시 가상환경 생성 ----------------------------
if not exist ".venv\" (
    echo [1/3] 가상환경을 만드는 중... (최초 1회)
    python -m venv .venv
)

echo [2/3] 필요한 패키지를 설치하는 중...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt

REM --- 설정 파일 확인 ----------------------------------------
if not exist "config.json" (
    copy /y config.example.json config.json >nul
    echo.
    echo [안내] config.json 을 새로 만들었습니다.
    echo        메모장으로 열어 API 키 / 구글시트 주소 등을 채운 뒤 다시 실행하세요.
    echo.
    notepad config.json
    pause
    exit /b 0
)

echo [3/3] 파이프라인 실행!
echo.
call ".venv\Scripts\python.exe" pipeline.py --debug %*

echo.
echo ============================================
echo   작업이 끝났습니다.
echo ============================================
pause
