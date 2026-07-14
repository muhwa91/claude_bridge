@echo off
REM Python 3.12.3+ 필요(그 미만이면 bridge.py 가 시작 시 종료).
REM 절전 방지: 제어판 > 전원 옵션에서 "절전: 안 함", "덮개를 닫을 때: 아무 동작 안 함"으로 설정.
REM 노트북이 슬립에 들어가면 폴링이 멈춰 원격 지시를 받지 못합니다(상시 켜둔 채 실행).
cd /d "%~dp0"
python bridge.py
pause
