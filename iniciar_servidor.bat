@echo off
title Servidor Project Zomboid
cd /d "%~dp0"
rem Muestra la salida en vivo; lo que escribas se envia a la consola del servidor. Ctrl+C guarda y detiene.
py -3 manager.py start
if errorlevel 1 pause
