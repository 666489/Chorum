@echo off
rem ClaudeLink hub clean restart -- waits DELAY seconds first so in-flight
rem messages land in the DB before the kill.
rem
rem Usage: restart-hub.bat [delay_seconds] [dry]
rem        delay defaults to 60; dry = walk the flow, kill/start nothing.
rem
rem NOTE 1: keep this file PURE ASCII. cmd.exe reads .bat in the console code
rem page (GBK here), so a UTF-8 or non-ASCII comment gets decoded into garbage
rem and cmd then tries to run that garbage as commands. That already happened
rem once (2026-09-26 16:06): a UTF-8 rewrite of this file turned `dry` into a
rem live relaunch. Rationale/notes live in the team report, not in here.
rem
rem NOTE 2: do NOT put a quoted interpreter path inside for /f ('...').
rem   for /f %%p in ('"C:\...\python.exe" "C:\...\hub_pid.py" 8787')  -> nothing
rem cmd fails to resolve the quoted command and the loop body never runs; with
rem 2^>nul the error is swallowed, HPIDS stays empty and the script "finds no
rem listener", skips the kill and starts a SECOND hub. That is exactly what
rem happened at 16:04 -- that trap is what created the duplicate instance.
rem Verified fix: cd into the script dir first and call the helper by bare name.
rem
rem Four traps, all already stepped in -- do not undo these:
rem  1) Never hard-code the PID. It used to be 30676; after a restart the hub
rem     became 13760, so the script killed nothing and started a SECOND hub ->
rem     port conflict. Always resolve the PID from the port via hub_pid.py.
rem  2) Do not use timeout.exe for the delay. It fails outright in a
rem     non-interactive console (stdin redirected), the delay vanishes and the
rem     hub is killed before in-flight messages are written. ping has no stdin
rem     dependency.
rem  3) The port can hold SEVERAL hub instances (Python http.server defaults to
rem     allow_reuse_address; on 2026-09-26 16:04 both 13760 and 30372 were
rem     listening). Killing only the first PID and starting a new one grows the
rem     instance count by one per restart (a ratchet). Always kill them ALL.
rem  4) Re-check the port after the kill and abort instead of relaunching if it
rem     is still held. Better to not restart than to create another coexisting
rem     instance: two hubs on one hub.db will settle each other's live turns as
rem     orphans.
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem Member CLI paths: env var -> config.json's %CLAUDELINK_*% placeholder -> hub.py default.
rem Safety net only; a variable already present in the system environment wins.
rem ASCII ONLY (see NOTE 1 above) -- do not add non-ASCII text to this file.
if not defined CLAUDELINK_NODE set "CLAUDELINK_NODE=D:\Node.js\node.exe"
if not defined CLAUDELINK_CLAUDE set "CLAUDELINK_CLAUDE=D:\Node.js\node_global\claude.cmd"
if not defined CLAUDELINK_CODEX set "CLAUDELINK_CODEX=codex"
if not defined CLAUDELINK_CODEBUDDY set "CLAUDELINK_CODEBUDDY=D:\Node.js\node_global\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy"
if not defined CLAUDELINK_DSH set "CLAUDELINK_DSH=D:\Node.js\node_global\node_modules\@deepseek-ai\dsh\lib\bin.js"
if not defined CLAUDELINK_CLAUDE_SETTINGS_DIR set "CLAUDELINK_CLAUDE_SETTINGS_DIR=%USERPROFILE%\.ccm\settings"

set LOG=%~dp0restart.log
set PY=D:\Anaconda3\envs\py11\python.exe
set PORT=8787
set DELAY=%~1
set DRY=%~2
if "%DELAY%"=="" set DELAY=60

echo [%DATE% %TIME%] restart requested (delay %DELAY%s dry=%DRY%) >> "%LOG%"

rem ping -n N waits roughly N-1 seconds; this delay is what lets in-flight
rem messages finish writing before the kill.
ping -n %DELAY% 127.0.0.1 >nul

set HPIDS=
for /f "delims=" %%p in ('"%PY%" hub_pid.py %PORT% 2^>nul') do set HPIDS=%%p
echo [%DATE% %TIME%] listeners on port !PORT! = [!HPIDS!] >> "%LOG%"

if /i "%DRY%"=="dry" (
  echo [%DATE% %TIME%] DRY: would kill [!HPIDS!] then relaunch - nothing done >> "%LOG%"
  exit /b 0
)

if not defined HPIDS (
  echo [%DATE% %TIME%] no listener on port !PORT! - skip kill >> "%LOG%"
) else (
  set KILLARGS=
  for %%q in (!HPIDS!) do set KILLARGS=!KILLARGS! /PID %%q
  taskkill /F !KILLARGS! >> "%LOG%" 2>&1
  echo [%DATE% %TIME%] killed pids=[!HPIDS!] rc=!ERRORLEVEL! >> "%LOG%"
)

ping -n 4 127.0.0.1 >nul

rem The port must really be free before starting anything. Check by CONNECTING,
rem not by parsing netstat: an empty netstat parse is exactly the failure that
rem silently skipped the kill, so a netstat-based re-check can fail the same way.
set STILL=
for /f "delims=" %%p in ('"%PY%" hub_pid.py %PORT% 2^>nul') do set STILL=%%p
"%PY%" hub_pid.py --probe %PORT%
if not errorlevel 1 (
  echo [%DATE% %TIME%] FATAL port !PORT! still answering (pids=[!STILL!]) - abort relaunch >> "%LOG%"
  exit /b 1
)

start "ClaudeLink Hub" /min "%PY%" "%~dp0hub.py"
echo [%DATE% %TIME%] relaunched py11 >> "%LOG%"
exit /b 0
