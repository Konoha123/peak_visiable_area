@echo off
rem Peak Visible Area 启动脚本（Windows）
rem 用法：在发行包根目录双击本文件。
rem 停止服务：关闭本窗口，或按 Ctrl+C 后结束批处理。
rem 环境变量：PVA_NO_BROWSER=1 时启动后不自动打开浏览器。
rem 本文件为 UTF-8 编码，靠 chcp 65001 保证中文正常显示。
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"

set "PY=%~dp0env\python.exe"

if not exist "%PY%" (
    echo [错误] 未找到运行环境 env\python.exe，请确认压缩包已完整解压且 env 目录存在。
    pause
    exit /b 1
)
if not exist app (
    echo [错误] 缺少 app 目录，发行包不完整。
    pause
    exit /b 1
)
if not exist static (
    echo [错误] 缺少 static 目录，发行包不完整。
    pause
    exit /b 1
)

rem 环境二进制目录置前；env\Library\bin 含 GDAL 运行所需 DLL
set "PATH=%~dp0env\Library\mingw-w64\bin;%~dp0env\Library\usr\bin;%~dp0env\Library\bin;%~dp0env\Scripts;%~dp0env;%PATH%"
if exist "%~dp0env\Library\share\gdal" set "GDAL_DATA=%~dp0env\Library\share\gdal"
if exist "%~dp0env\Library\share\proj" set "PROJ_LIB=%~dp0env\Library\share\proj"

rem 首次运行：conda-unpack 修复打包机器的路径前缀（写标记避免重复执行）
if exist "%~dp0env\.pva-unpacked" goto :unpacked
echo [首次运行] 正在初始化运行环境（conda-unpack），约需数十秒……
set UNPACK_OK=0
if exist "%~dp0env\Scripts\conda-unpack.exe" (
    "%~dp0env\Scripts\conda-unpack.exe" && set UNPACK_OK=1
)
if "%UNPACK_OK%"=="0" if exist "%~dp0env\Scripts\conda-unpack-script.py" (
    "%PY%" "%~dp0env\Scripts\conda-unpack-script.py" && set UNPACK_OK=1
)
if "%UNPACK_OK%"=="0" (
    "%PY%" -m conda_unpack && set UNPACK_OK=1
)
if "%UNPACK_OK%"=="1" (
    type nul > "%~dp0env\.pva-unpacked"
) else (
    echo [警告] conda-unpack 未成功，仍尝试启动（若启动失败请反馈此信息）。
)
:unpacked

rem 从 8000 起寻找可用端口
set "PORT="
for /L %%P in (8000,1,8009) do (
    if not defined PORT (
        "%PY%" -c "import socket,sys; s=socket.socket(); sys.exit(1 if s.connect_ex(('127.0.0.1',%%P))==0 else 0)" >nul 2>&1 && set "PORT=%%P"
    )
)
if not defined PORT (
    echo [错误] 8000-8009 端口均被占用，请关闭占用程序后重试。
    pause
    exit /b 1
)

echo [启动] 正在启动服务（端口 %PORT%）……
start "" /b "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

rem 等待 /api/health 就绪（最多 60 秒）
set /a TRIES=0
:waitloop
"%PY%" -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%PORT%/api/health', timeout=2).status==200 else 1)" >nul 2>&1 && goto :ready
set /a TRIES+=1
if %TRIES% geq 60 goto :notready
timeout /t 1 /nobreak >nul
goto :waitloop

:notready
echo [错误] 服务 60 秒内未就绪，请截图本窗口反馈。
pause
exit /b 1

:ready
if /i not "%PVA_NO_BROWSER%"=="1" start "" "http://127.0.0.1:%PORT%"
echo.
echo 服务已就绪：http://127.0.0.1:%PORT%
echo 关闭本窗口（或按 Ctrl+C 后结束批处理）即可停止服务。
echo.
pause >nul
