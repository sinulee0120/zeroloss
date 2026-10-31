@echo off
chcp 65001 >nul <nul
setlocal
set "APP=%~dp01_Windows_PC\ZeroLoss"
rem installed: this file is in the program folder (Start menu "ZeroLoss 리눅스 PC 등록")
if exist "%~dp0ZeroLoss.exe" set "APP=%~dp0."
echo ============================================================
echo   ZeroLoss - 리눅스 PC 등록 (송신 PC, 수신 PC 한 대씩)
echo ============================================================
curl -s -m 3 http://127.0.0.1:10994/v1/controller/status >nul 2>&1
if errorlevel 1 (
  echo ZeroLoss 프로그램을 켭니다...
  start "" "%APP%\ZeroLoss.exe"
  for /l %%i in (1,1,30) do (
    timeout /t 2 >nul
    curl -s -m 3 http://127.0.0.1:10994/v1/controller/status >nul 2>&1 && goto ready
  )
  echo ZeroLoss 프로그램이 켜지지 않았습니다. 시작 메뉴의 ZeroLoss Controller를 직접 켜고 다시 실행하세요.
  pause
  exit /b 1
)
:ready
set TRIES=0
echo.
:ask_host
set "HOST="
set /p "HOST=리눅스 PC의 IP 주소 (예: 192.168.1.20): "
set /a TRIES+=1
if "%HOST%"=="" if %TRIES% lss 5 goto ask_host
if "%HOST%"=="" exit /b 1
:ask_user
set "LUSER="
set /p "LUSER=리눅스 PC의 아이디 (로그인 이름): "
set /a TRIES+=1
if "%LUSER%"=="" if %TRIES% lss 5 goto ask_user
if "%LUSER%"=="" exit /b 1
:ask_name
set "NAME="
set /p "NAME=이 PC의 이름 (송신 PC면 sender, 수신 PC면 receiver): "
set /a TRIES+=1
if "%NAME%"=="" if %TRIES% lss 5 goto ask_name
if "%NAME%"=="" exit /b 1
set /p "AUTO=수신 PC입니까? 화면 자동 로그인을 켤까요 [y/n]: "
set "AL="
if /i "%AUTO%"=="y" set "AL=--auto-login"
echo.
echo 다음에 리눅스 PC의 비밀번호를 물어봅니다. 입력해도 화면에 안 보이는 것이 정상입니다.
"%APP%\cli\zeroloss.exe" install linux --host %HOST% --user %LUSER% --name %NAME% %AL% --yes
echo.
if errorlevel 1 (
  echo 실패했습니다. 위의 영어 메시지를 사진으로 찍어 보내 주세요.
) else (
  echo 완료! ZeroLoss 화면의 Devices 에 "%NAME%" 이 보입니다.
)
pause
