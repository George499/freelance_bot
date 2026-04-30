@echo off
cd /d C:\Users\Исаев\Documents\GitHub\freel_bot

start "ngrok" /min ngrok.exe http 8081

timeout /t 3 /nobreak > nul

start "freel_bot — логи" Z:\freel_venv\Scripts\python.exe -m app
