@echo off
REM job-radar entry point for Windows Task Scheduler. Output is appended to run.log (UTF-8).
cd /d "%~dp0"
echo. >> run.log
echo ===== %DATE% %TIME% ===== >> run.log
"C:\Users\huhon\AppData\Local\Python\pythoncore-3.14-64\python.exe" jobradar.py >> run.log 2>&1
