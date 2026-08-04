@echo off
REM job-radar entry point for Windows Task Scheduler. Output is appended to run.log (UTF-8).
REM Uses whichever python is on PATH; replace "python" with a full path if the
REM scheduled task runs under an account whose PATH doesn't include it.
cd /d "%~dp0"
echo. >> run.log
echo ===== %DATE% %TIME% ===== >> run.log
python jobradar.py >> run.log 2>&1
