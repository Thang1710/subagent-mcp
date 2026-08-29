@echo off
setlocal

where uvx >nul 2>&1
if errorlevel 1 (
  echo [Subagent MCP] uvx was not found. Install uv, then run this file again.
  pause
  exit /b 1
)

uvx --isolated --from subagent-harness-mcp==1.0.24 subagent-harness-mcp ui --open >nul 2>&1
if not errorlevel 1 exit /b 0

uvx --isolated --from subagent-harness-mcp==1.0.24 subagent-harness-mcp ui --background
if errorlevel 1 (
  echo.
  echo [Subagent MCP] Could not open the config UI at http://127.0.0.1:8765
  pause
  exit /b 1
)
