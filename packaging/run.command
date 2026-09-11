#!/bin/bash
# Peak Visible Area 启动脚本（macOS）
# 用法：在发行包根目录双击本文件（Terminal 自动打开并运行）。
# 停止服务：关闭本 Terminal 窗口，或按 Ctrl+C。
# 环境变量：
#   PVA_NO_BROWSER=1  启动后不自动打开浏览器

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

PY="$ROOT/env/bin/python"

fail() { echo "[错误] $1"; exit 1; }

[ -x "$PY" ] || fail "未找到运行环境 env/bin/python，请确认压缩包已完整解压且 env 目录存在。"
[ -d "$ROOT/app" ] && [ -d "$ROOT/static" ] || fail "app/ 或 static/ 缺失，发行包不完整。"

export PATH="$ROOT/env/bin:$PATH"

[ -d "$ROOT/env/share/gdal" ] && export GDAL_DATA="$ROOT/env/share/gdal"
[ -d "$ROOT/env/share/proj" ] && export PROJ_LIB="$ROOT/env/share/proj"

if [ ! -f "$ROOT/env/.pva-unpacked" ]; then
    echo "[首次运行] 正在初始化运行环境（conda-unpack），约需数十秒……"
    UNPACK_OK=0
    if [ -f "$ROOT/env/bin/conda-unpack" ]; then
        "$PY" "$ROOT/env/bin/conda-unpack" && UNPACK_OK=1
    fi
    if [ "$UNPACK_OK" -eq 0 ]; then
        "$PY" -m conda_unpack && UNPACK_OK=1
    fi
    if [ "$UNPACK_OK" -eq 0 ]; then
        echo "[警告] conda-unpack 未成功，仍尝试启动（若启动失败请反馈此信息）。"
    else
        touch "$ROOT/env/.pva-unpacked"
    fi
fi

PORT=""
for p in $(seq 8000 8009); do
    if "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(1 if s.connect_ex(('127.0.0.1', $p)) == 0 else 0)"; then
        PORT="$p"
        break
    fi
done
[ -n "$PORT" ] || fail "8000-8009 端口均被占用，请关闭占用程序后重试。"

echo "[启动] 正在启动服务（端口 $PORT）……"
LOG="$ROOT/pva-server.log"
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
SRV_PID=$!

cleanup() { kill "$SRV_PID" 2>/dev/null; exit 0; }
trap cleanup INT TERM HUP

READY=0
for _ in $(seq 1 60); do
    if "$PY" -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=2).status == 200 else 1)" 2>/dev/null; then
        READY=1
        break
    fi
    if ! kill -0 "$SRV_PID" 2>/dev/null; then
        echo "[错误] 服务进程启动失败，最近日志如下（完整日志见 $LOG）："
        tail -n 20 "$LOG"
        exit 1
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "[错误] 服务 60 秒内未就绪，最近日志如下（完整日志见 $LOG）："
    tail -n 20 "$LOG"
    kill "$SRV_PID" 2>/dev/null
    exit 1
fi

if [ "${PVA_NO_BROWSER:-0}" != "1" ]; then
    (open "http://127.0.0.1:$PORT" >/dev/null 2>&1) &
fi

echo
echo "服务已就绪：http://127.0.0.1:$PORT"
echo "关闭本窗口（或按 Ctrl+C）即可停止服务。"
echo "运行日志：$LOG"
echo

wait "$SRV_PID"
