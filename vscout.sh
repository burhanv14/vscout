#!/bin/bash
# vscout.sh — Linux launcher for the vscout MCP server.
# Run in a terminal: ./vscout.sh
# Press Ctrl+C to stop the server.

cd "$(dirname "$0")/.."

if [ ! -f ".env" ]; then
    echo "[vscout] No .env found. You will be prompted for a GitHub token on first run."
fi

python3 -m oss_agent.terminal_launcher
