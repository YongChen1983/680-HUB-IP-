@echo off
chcp 65001 >nul
setlocal
echo ========================================
echo   V680-CHUD 读写工具 - 快速安装
echo ========================================
echo.

REM 切换到脚本所在目录
cd /d "%~dp0"

REM 查找 Python（优先 py launcher，再 python）
set PYEXE=
where py >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('py -3 -c "import sys; print(sys.executable)" 2nul') do set PYEXE=%%i
)
if not defined PYEXE (
    where python >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "tokens=*" %%i in ('python -c "import sys; print(sys.executable)" 2nul') do set PYEXE=%%i
    )
)
if not defined PYEXE (
    echo [错误] 未找到 Python。请先安装 Python 3.8 或更高版本：
    echo   https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

echo [OK] 使用 Python: %PYEXE%
echo.

REM 升级 pip（可选，减少告警）
echo 正在升级 pip ...
"%PYEXE%" -m pip install --upgrade pip -q
if %errorlevel% neq 0 (
    echo [警告] pip 升级失败，继续尝试安装依赖...
)

REM 安装依赖
echo 正在安装依赖 (pyserial) ...
"%PYEXE%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败。
    pause
    exit /b 1
)

echo.
echo ========================================
echo   安装完成。运行方式：
echo   双击 run.bat  或 在命令行执行：
echo   "%PYEXE%" v680_chud_app.py
echo ========================================
pause
