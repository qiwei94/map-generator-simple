@echo off
REM ========================================
REM  CLI Pipeline: osmium + ogr2ogr → 3MF
REM ========================================
REM
REM 前置要求：
REM   1. 在 Anaconda Prompt 中运行此脚本
REM   2. 安装必要工具：conda install -c conda-forge osmium-tool gdal
REM
REM ========================================

echo.
echo ========================================
echo  CLI Pipeline: osmium + ogr2ogr → 3MF
echo ========================================
echo.

REM 切换到项目目录
cd /d "%~dp0"

echo [检查] 确认在 Anaconda 环境中...
echo 当前 Python: %PYTHON%
echo.

REM 运行 Python 脚本
python generate_westlake_cli.py

echo.
if %ERRORLEVEL% EQU 0 (
    echo ========================================
    echo  成功！输出文件在 output/westlake_cli/ 目录
    echo ========================================
) else (
    echo ========================================
    echo  失败！请确保：
    echo    1. 在 Anaconda Prompt 中运行
    echo    2. 已安装: conda install -c conda-forge osmium-tool gdal
    echo ========================================
)

pause
