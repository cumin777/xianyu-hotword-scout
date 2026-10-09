@echo off
rem 任务计划程序每日调这个批处理。日志追加进 runtime\scheduler.log
cd /d %~dp0
if not exist runtime mkdir runtime
python main.py >> runtime\scheduler.log 2>&1
