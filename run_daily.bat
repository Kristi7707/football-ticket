@echo off
cd /d %~dp0
python main.py >> run_log.txt 2>&1
