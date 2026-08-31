#!/usr/bin/env bash
set -euo pipefail

# SAIVerse 起動スクリプト
# Usage: ./start.sh [city_name]
#   city_name: 起動する都市名 (default: city_a)
#
# Environment variables:
#   SAIVERSE_SEARXNG=1  SearXNG サーバーも同時起動する

CITY_NAME=${1:-city_a}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
    echo ""
    echo "[INFO] Shutting down..."
    # バックグラウンドジョブを終了
    jobs -p | xargs -r kill 2>/dev/null || true
    wait 2>/dev/null || true
    echo "[INFO] Shutdown complete"
}

trap cleanup EXIT INT TERM

cd "$SCRIPT_DIR"

# Activate venv if available
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# 中断された更新を仕上げてから起動する (start.bat と同じ意味論)。
#   update_engine.py --check-complete の終了コード:
#     0  = コードと導入済みパッケージが一致。そのまま起動。
#     10 = 更新が途中で死んでいる。--manual で仕上げてから起動。
#     11 = 判定できなかった。警告だけ出して起動。
# 仕上げに失敗したら起動しない (中途半端な状態で立ち上げない)。
# docs/issues/v0229_update_bat_truncates_after_git_pull.md
if [ -x ".venv/bin/python" ]; then
    UPDATE_CHECK_RC=0
    .venv/bin/python scripts/update_engine.py --check-complete || UPDATE_CHECK_RC=$?
    if [ "$UPDATE_CHECK_RC" -eq 10 ]; then
        echo "[INFO] Update was interrupted; finishing it now..."
        if ! .venv/bin/python scripts/update_engine.py --manual; then
            echo "[ERROR] The interrupted update could not be finished." >&2
            echo "[ERROR] SAIVerse was NOT started, because the code and the installed" >&2
            echo "[ERROR] packages do not match. See self_update.log for details." >&2
            exit 1
        fi
        echo "[OK] Update finished."
    elif [ "$UPDATE_CHECK_RC" -ne 0 ]; then
        echo "[WARN] Could not verify the update state. Starting anyway."
    fi
fi

# SearXNG (optional - set SAIVERSE_SEARXNG=1 to enable)
if [ "${SAIVERSE_SEARXNG:-0}" = "1" ] && [ -f "./scripts/run_searxng_server.sh" ]; then
    echo "[INFO] Starting SearXNG server..."
    ./scripts/run_searxng_server.sh &
    sleep 3
fi

echo "[INFO] Starting SAIVerse Backend (${CITY_NAME})..."
python main.py "$CITY_NAME" &
SAIVERSE_PID=$!

# Wait a bit for backend to initialize
sleep 3

echo "[INFO] Building SAIVerse Frontend..."
(cd frontend && npm run build)

echo "[INFO] Starting SAIVerse Frontend..."
(cd frontend && npm start) &
FRONTEND_PID=$!

sleep 3

# Open browser (macOS or Linux)
if command -v open &>/dev/null; then
    open http://localhost:3000
elif command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:3000
fi

echo ""
echo "========================================"
echo "  SAIVerse is running"
echo ""
echo "  Web UI:  http://localhost:3000"
echo "  Backend: PID $SAIVERSE_PID"
echo "  Frontend: PID $FRONTEND_PID"
echo ""
echo "  Press Ctrl+C to stop all services"
echo "========================================"
echo ""

# どれかが終了するまで待機
wait -n 2>/dev/null || wait
