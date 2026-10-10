@echo off
rem Daily task-scheduler entry. Artifacts dir overridable via HOTWORD_SCOUT_RUNTIME
rem (db/reports/hot-words/cookies/log all live there; default = repo\runtime)
cd /d %~dp0
if "%HOTWORD_SCOUT_RUNTIME%"=="" set HOTWORD_SCOUT_RUNTIME=%~dp0runtime
if not exist "%HOTWORD_SCOUT_RUNTIME%" mkdir "%HOTWORD_SCOUT_RUNTIME%"
%~dp0.venv\Scripts\python.exe main.py >> "%HOTWORD_SCOUT_RUNTIME%\scheduler.log" 2>&1