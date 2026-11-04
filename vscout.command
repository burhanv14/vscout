#!/bin/bash
# vscout.command — macOS launcher for the vscout MCP server.
# Double-click to open Terminal.app and start the MCP server.
# Close the window or press Ctrl+C to stop the server.

cd "$(dirname "$0")/.."

if [ ! -f ".env" ]; then
    echo "[vscout] No .env found. You will be prompted for a GitHub token on first run."
fi

python3 -m oss_agent.terminal_launcher
