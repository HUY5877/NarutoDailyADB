@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "LOCAL_QR_PYTHON=%~dp0.venv-login-service\Scripts\python.exe"
if exist "%LOCAL_QR_PYTHON%" goto configured
set "LOCAL_QR_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%LOCAL_QR_PYTHON%" goto configured
echo Python environment not found. Follow the installation steps in README.md.
pause
popd
exit /b 2

:configured
set "LOCAL_QR_CONFIG=%~dp0config.local.json"
if not exist "%LOCAL_QR_CONFIG%" set "LOCAL_QR_CONFIG=%~dp0config.example.json"
rem Python owns its console; Ctrl+C exits without a batch termination prompt.
start "NarutoLocalQrSecret" "%LOCAL_QR_PYTHON%" -u -m naruto_daily.launcher run secret_realm_farm --qr-login --config "%LOCAL_QR_CONFIG%" %*
set "LAUNCH_EXIT_CODE=%errorlevel%"
if not "%LAUNCH_EXIT_CODE%"=="0" (
    echo Unable to start Python. Exit code: %LAUNCH_EXIT_CODE%.
    pause
)
popd
exit /b %LAUNCH_EXIT_CODE%
