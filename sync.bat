@echo off
REM ============================================================
REM 双向同步脚本 (Windows 批处理版本)
REM 使用方式（在 CMD 或 PowerShell 中运行）：
REM   sync pull    从远程服务器拉取到本地
REM   sync push    从本地推送到远程服务器
REM   sync dry-run 试运行
REM ============================================================

if "%1"=="" (
    echo 用法: sync {pull^|push^|dry-run}
    exit /b 1
)

wsl -d Ubuntu-24.04 -u root -- bash -c "cd \"$(wslpath '%CD%')\" && ./tools/sync.sh %1"
if %ERRORLEVEL% NEQ 0 (
    echo 同步失败，请确保 WSL Ubuntu-24.04 已安装并运行
    exit /b 1
)
