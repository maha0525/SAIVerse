#!/bin/bash
# Start SAIVerse server with test environment
#
# Usage:
#   ./test_fixtures/start_test_server.sh           # Start test server
#   ./test_fixtures/start_test_server.sh --setup   # Setup and start
#   ./test_fixtures/start_test_server.sh --clean   # Clean setup and start

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Test environment paths
export SAIVERSE_HOME="$PROJECT_ROOT/test_data/.saiverse"
export SAIVERSE_USER_DATA_DIR="$PROJECT_ROOT/test_data/user_data"
TEST_DB_PATH="$SAIVERSE_USER_DATA_DIR/database/saiverse.db"

# City name from test_data.json
CITY_NAME="test_city"

echo "=============================================="
echo "SAIVerse Test Server"
echo "=============================================="
echo "SAIVERSE_HOME: $SAIVERSE_HOME"
echo "SAIVERSE_USER_DATA_DIR: $SAIVERSE_USER_DATA_DIR"
echo "Database: $TEST_DB_PATH"
echo "=============================================="

# Handle arguments
case "$1" in
    --setup)
        echo "Setting up test environment..."
        python "$SCRIPT_DIR/setup_test_env.py"
        ;;
    --clean)
        echo "Clean setup of test environment..."
        python "$SCRIPT_DIR/setup_test_env.py" --clean
        ;;
    *)
        # Check if test environment exists
        if [ ! -f "$TEST_DB_PATH" ]; then
            echo "Test environment not found. Running setup..."
            python "$SCRIPT_DIR/setup_test_env.py"
        fi
        ;;
esac

echo ""
echo "Starting test server..."
echo "UI will be available at: http://127.0.0.1:18000"
echo "API will be available at: http://127.0.0.1:18001"
echo ""
echo "Press Ctrl+C to stop"
echo ""

# Start the server
cd "$PROJECT_ROOT"
python main.py "$CITY_NAME" --db-file "$TEST_DB_PATH"
