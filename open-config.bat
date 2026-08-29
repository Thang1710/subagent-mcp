@echo off
setlocal

where uv >nul 2>&1
if errorlevel 1 (
  echo [Subagent MCP] uv was not found. Install uv, then run this file again.
  pause
  exit /b 1
)

uv run --project "%~dp0." --frozen subagent-harness-mcp ui --open >nul 2>&1
if not errorlevel 1 exit /b 0

uv run --project "%~dp0." --frozen subagent-harness-mcp ui --background
if errorlevel 1 (
  echo.
  echo [Subagent MCP] Could not open the config UI at http://127.0.0.1:8765
  pause
  exit /b 1
)
