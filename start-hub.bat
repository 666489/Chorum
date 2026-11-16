@echo off
setlocal
set HERE=%~dp0
>>"%TEMP%\claudelink-hub-start.log" echo [%DATE% %TIME%] start from %HERE%

rem Interpreter: py11 only. Older versions had anaconda3/where-python fallbacks,
rem but those belong to a different standalone install (sqlite 3.51.2; the local
rem base env is broken). Silent downgrade would flip WAL/length() criteria
rem without any error. Better to refuse to start.
set PY=
if exist "%CLAUDELINK_PYTHON%" set PY=%CLAUDELINK_PYTHON%
if not defined PY if exist "D:\Anaconda3\envs\py11\python.exe" set PY=D:\Anaconda3\envs\py11\python.exe
if not defined PY (
  echo [ERROR] py11 not found. Expected:
  echo         D:\Anaconda3\envs\py11\python.exe
  echo         ^(verification criteria assume Python 3.11 / sqlite 3.53.2^)
  echo         Install it, or set CLAUDELINK_PYTHON to a python.exe path.
  pause
  exit /b 1
)

rem --- Member CLI paths (env-var first) -------------------------------------
rem Every member CLI is located via an env var: environment -> config.json's
rem agents.<name>.exe (%%CLAUDELINK_*%% placeholder) -> the built-in default in hub.py.
rem These "if not defined" lines are only a safety net so the hub still boots if
rem the user-level variables are missing. A variable already set in the system
rem environment WINS -- edit it there (or via setx) to point at a new CLI path,
rem then restart the hub. Keep everything in this block ASCII.
if not defined CLAUDELINK_NODE set "CLAUDELINK_NODE=D:\Node.js\node.exe"
if not defined CLAUDELINK_CLAUDE set "CLAUDELINK_CLAUDE=D:\Node.js\node_global\claude.cmd"
if not defined CLAUDELINK_CODEX set "CLAUDELINK_CODEX=codex"
if not defined CLAUDELINK_CODEBUDDY set "CLAUDELINK_CODEBUDDY=D:\Node.js\node_global\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy"
if not defined CLAUDELINK_DSH set "CLAUDELINK_DSH=D:\Node.js\node_global\node_modules\@deepseek-ai\dsh\lib\bin.js"
if not defined CLAUDELINK_CLAUDE_SETTINGS_DIR set "CLAUDELINK_CLAUDE_SETTINGS_DIR=%USERPROFILE%\.ccm\settings"

echo Python : %PY%
echo Hub    : http://127.0.0.1:8787
echo Log    : %HERE%hub.log
start "ClaudeLink Hub" "%PY%" "%HERE%hub.py"
ping -n 4 127.0.0.1 >nul
start "" http://127.0.0.1:8787
echo Hub window started. Close that window to stop the hub.
ping -n 6 127.0.0.1 >nul
