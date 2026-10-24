#!/usr/bin/env bash
set -euo pipefail

# SAIVerse 起動スクリプト (Dev Mode - hot reload enabled)
# Usage: ./start-dev.sh [city_name]
#   city_name: 起動する都市名 (default: city_a)
#
# Environment variables:
#   SAIVERSE_SEARXNG=1  SearXNG サーバーも同時起動する
#
# NOTE: Dev mode uses HMR which may cause page refreshes over Tailscale.
# Use start.sh for stable remote access.

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
#     12 = 古い更新処理が入れ替えの一部を行っていなかったので、パッケージの
#          入れ直しを一度試したが失敗した。警告を出して起動し、失敗の内容は
#          SAIVerse の画面のバナーに出る (docs/intent/update_completes_with_new_code.md)。
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
    elif [ "$UPDATE_CHECK_RC" -eq 12 ]; then
        echo "[WARN] The packages for this version could not be installed once more."
        echo "[WARN] Starting anyway. The SAIVerse screen shows what happened and what to do."
        echo "[WARN] If the SAIVerse screen does not open, its packages are missing too: check the"
        echo "[WARN] internet connection, stop SAIVerse (Ctrl+C), then run start.sh again."
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

echo "[INFO] Starting SAIVerse Frontend (dev mode)..."
(cd frontend && npm run dev) &
FRONTEND_PID=$!

sleep 3

# The frontend listens on PORT when it is set (next start / next dev read it),
# else on 3000: open the browser on the same port.
UI_PORT="${PORT:-3000}"

# Open browser (macOS or Linux)
if command -v open &>/dev/null; then
    open http://localhost:${UI_PORT}
elif command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:${UI_PORT}
fi

echo ""
echo "========================================"
echo "  SAIVerse is running (Dev Mode)"
echo ""
echo "  Web UI:  http://localhost:${UI_PORT}"
echo "  Backend: PID $SAIVERSE_PID"
echo "  Frontend: PID $FRONTEND_PID"
echo ""
echo "  NOTE: HMR enabled - may cause refreshes over Tailscale"
echo "  Use start.sh for stable remote access"
echo ""
echo "  Press Ctrl+C to stop all services"
echo "========================================"
echo ""

# どれかが終了するまで待機
wait -n 2>/dev/null || wait
