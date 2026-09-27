@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m bps_proxy %*
) else (
    python -m bps_proxy %*
)
