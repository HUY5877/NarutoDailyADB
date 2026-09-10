@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Follow the installation steps in README.md.
    pause
    popd
    exit /b 2
)
if not exist "config.local.json" (
    echo Missing config.local.json. Follow README.md to configure the project.
    pause
    popd
    exit /b 2
)

rem Python owns its console so Ctrl+C does not prompt to terminate a batch job.
start "NarutoDailyDaily" ".venv\Scripts\python.exe" -u -m naruto_daily.launcher run all --config "config.local.json" %*
set "LAUNCH_EXIT_CODE=%errorlevel%"
if not "%LAUNCH_EXIT_CODE%"=="0" (
    echo Unable to start Python. Exit code: %LAUNCH_EXIT_CODE%.
    pause
)
popd
exit /b %LAUNCH_EXIT_CODE%
