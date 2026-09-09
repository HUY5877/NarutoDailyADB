@echo off
chcp 65001 >nul
setlocal
title 火影忍者 - 启动秘境
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
set "LAUNCH_EXIT_CODE=2"

if not exist ".venv\Scripts\python.exe" (
    echo 未找到项目虚拟环境，请先按 README 的安装步骤创建 .venv 并安装依赖。
    goto finish
)
if not exist "config.local.json" (
    echo 未找到 config.local.json，请按 README 的安装步骤准备配置文件和 .env。
    goto finish
)

echo 正在启动独立秘境流程，请先打开模拟器并停留在第一张登录页面。
echo 登录后从忍者挑战进入秘境探险。按 Ctrl+C 可停止。
echo.
".venv\Scripts\python.exe" -u -m naruto_daily.cli run secret_realm_farm --config "config.local.json" %*
set "LAUNCH_EXIT_CODE=%errorlevel%"

:finish
echo.
if "%LAUNCH_EXIT_CODE%"=="0" (
    echo 秘境流程已结束。
) else (
    echo 秘境流程已停止，退出码：%LAUNCH_EXIT_CODE%。请查看上方错误信息。
)
pause
popd
exit /b %LAUNCH_EXIT_CODE%
