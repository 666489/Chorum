@echo off
rem 常驻把 ClaudeLink 完整会话镜像到 logs\conversation_full.md 与 logs\conversation_cache.md
cd /d "%~dp0"
"D:\Anaconda3\envs\py11\python.exe" "%~dp0tools\conv_feed.py" --pump --interval 1.5
pause
