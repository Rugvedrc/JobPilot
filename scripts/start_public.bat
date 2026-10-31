@echo off
REM Starts FresherPilot and the Cloudflare Tunnel (see docs\DEPLOY.md). Close this window to stop both.
cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8
start "FresherPilot" python main.py
timeout /t 5 /nobreak >nul
cloudflared tunnel run --url http://localhost:8000 fresherpilot
