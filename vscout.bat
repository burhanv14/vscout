@echo off
:: vscout.bat — Windows launcher for the vscout MCP server.
:: Double-click to open a terminal that starts the MCP server.
:: Close the window or press Ctrl+C to stop the server.

setlocal
cd /d "%~dp0.."

if not exist "%~dp0..\.env" (
    echo [vscout] No .env found. You will be prompted for a GitHub token on first run.
)

python -m oss_agent.terminal_launcher
if %ERRORLEVEL% neq 0 (
    echo.
    echo [vscout] The MCP server exited with an error.
    pause
)
