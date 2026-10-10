@echo off
setlocal
cd /d "%~dp0"
set "LOGIN_PYTHON=%~dp0.venv-login-service\Scripts\python.exe"
if exist "%LOGIN_PYTHON%" goto run
set "LOGIN_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%LOGIN_PYTHON%" goto run
echo Python environment not found. Install the login-service dependencies first.
pause
exit /b 1
:run
"%LOGIN_PYTHON%" -u -m naruto_daily.login_service %*
set "LOGIN_EXIT_CODE=%ERRORLEVEL%"
if not "%LOGIN_EXIT_CODE%"=="0" pause
exit /b %LOGIN_EXIT_CODE%
