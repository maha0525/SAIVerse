#!/usr/bin/env bash
set -euo pipefail

# SAIVerse + SearXNG 同時起動スクリプト
# Usage: ./start.sh [city_name]
#   city_name: 起動する都市名 (default: city_a)

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

echo "[INFO] Starting SearXNG server..."
./scripts/run_searxng_server.sh &
SEARXNG_PID=$!

# SearXNG の起動を少し待つ
sleep 3

echo "[INFO] Starting SAIVerse Backend (${CITY_NAME})..."
python main.py "$CITY_NAME" &
SAIVERSE_PID=$!

# Wait a bit for backend to initialize
sleep 2

echo "[INFO] Starting SAIVerse Frontend..."
(cd frontend && npm run dev) &
FRONTEND_PID=$!

echo ""
echo "========================================"
echo "  SearXNG PID:  $SEARXNG_PID"
echo "  Backend PID:  $SAIVERSE_PID"
echo "  Frontend PID: $FRONTEND_PID"
echo "  Press Ctrl+C to stop all services"
echo "========================================"
echo ""

# 3つのうちどれかが終了するまで待機
wait -n 2>/dev/null || wait
