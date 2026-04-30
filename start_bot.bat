@echo off
cd /d C:\Users\9BCF~1\Documents\GitHub\freel_bot

start "ngrok" /min C:\Users\9BCF~1\Documents\GitHub\freel_bot\ngrok.exe http 8081

timeout /t 3 /nobreak > nul

start "freel_bot" Z:\freel_venv\Scripts\python.exe -m app
