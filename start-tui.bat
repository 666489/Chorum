@echo off
setlocal
set HERE=%~dp0
set PY=
if exist "%CLAUDELINK_PYTHON%" set PY=%CLAUDELINK_PYTHON%
if not defined PY if exist "D:\Anaconda3\envs\py11\python.exe" set PY=D:\Anaconda3\envs\py11\python.exe
if not defined PY (
  echo [ERROR] py11 not found. Expected:
  echo         D:\Anaconda3\envs\py11\python.exe
  echo         Set CLAUDELINK_PYTHON to a python.exe path, or install py11.
  pause
  exit /b 1
)
"%PY%" "%HERE%hub_tui.py" %*
