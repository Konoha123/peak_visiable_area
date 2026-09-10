#!/usr/bin/env bash
# 前端页面自检（/?selftest=1）：临时启动后端 + 无头浏览器 dump-dom，
# 校验全部检查项为 PASS；出现 FAIL、结果缺失或环境缺失时以非零码退出。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PVA_SELFTEST_PORT:-8365}"
DOM="$(mktemp)"
PYBIN="$(conda run -n "$(cat conda_env_name)" which python)"
CHROME="$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || true)"

cleanup() { kill "$SERVER" 2>/dev/null || true; rm -f "$DOM"; }
trap cleanup EXIT

if [ -z "$CHROME" ]; then
  echo "错误：未找到 Chrome/Chromium，无法执行无头自检" >&2
  exit 1
fi

"$PYBIN" -m uvicorn app.main:app --port "$PORT" >/dev/null 2>&1 &
SERVER=$!

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/" >/dev/null && break
  sleep 0.2
done
curl -sf "http://127.0.0.1:$PORT/" >/dev/null || { echo "错误：后端启动超时" >&2; exit 1; }

"$CHROME" --headless=new --disable-gpu --no-sandbox --virtual-time-budget=20000 \
  --dump-dom "http://127.0.0.1:$PORT/?selftest=1" >"$DOM" 2>/dev/null

LINES="$(tr -d '\r' <"$DOM" | sed -n '/id="selftest-results"/,/<\/pre>/p' \
  | sed -e 's/<[^>]*>//g' | sed '/^[[:space:]]*$/d')"

if [ -z "$LINES" ]; then
  echo "错误：页面自检结果未写入 DOM（#selftest-results 缺失）" >&2
  exit 1
fi
echo "$LINES"

TOTAL="$(wc -l <<<"$LINES")"
PASSED="$(grep -c '^PASS ' <<<"$LINES" || true)"
if grep -q '^FAIL ' <<<"$LINES" || [ "$PASSED" != "$TOTAL" ]; then
  echo "自检失败：PASS=$PASSED/$TOTAL" >&2
  exit 1
fi
echo "自检通过：PASS=$PASSED/$TOTAL"
