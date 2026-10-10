@echo off
rem Daily task-scheduler entry. Output appends to runtime\scheduler.log
cd /d %~dp0
if not exist runtime mkdir runtime
%~dp0.venv\Scripts\python.exe main.py >> runtime\scheduler.log 2>&1