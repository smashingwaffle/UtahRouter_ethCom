@echo off
rem Utah eth2RS422 launcher - opens the web UI at http://localhost:8422
cd /d "%~dp0"
python -c "import serial" 2>nul || python -m pip install -r requirements.txt
python run_eth2rs422.py %*
if errorlevel 1 pause
