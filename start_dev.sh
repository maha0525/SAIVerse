#!/bin/bash
echo "Starting SAIVerse..."

# Function to kill processes on exit
cleanup() {
    echo "Shutting down..."
    kill $(jobs -p)
}
trap cleanup EXIT

# Start Backend
echo "Starting Backend (FastAPI + Gradio)..."
python3 main.py &
BACKEND_PID=$!

# Wait a bit for backend to initialize
sleep 5

# Start Frontend
echo "Starting Frontend (Next.js)..."
cd frontend
# Check if next is installed locally, if not try global or npx
if [ -f "node_modules/.bin/next" ]; then
    ./node_modules/.bin/next dev -p 3000 &
else
    npm run dev -- -p 3000 &
fi

echo "Backend running on port 7860 (Gradio at /gradio)"
echo "Frontend running on port 3000"

wait
