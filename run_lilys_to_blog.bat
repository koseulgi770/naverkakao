@echo off
REM Lilys AI -> 네이버 블로그 자동 포스팅 실행기
REM 이 파일과 lilys_to_blog.py가 같은 폴더에 있어야 합니다.

cd /d "%~dp0"
pythonw lilys_to_blog.py

REM pythonw로 실행하면 검은 콘솔창 없이 프로그램 창만 뜹니다.
REM 만약 pythonw가 없다는 오류가 나면 아래 줄의 REM을 지우고 위 줄 앞에 REM을 붙여주세요.
REM python lilys_to_blog.py
