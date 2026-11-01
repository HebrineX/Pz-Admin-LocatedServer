@echo off
title PZ Dedicated Manager (navegador)
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (echo Instala Python 3.10 o superior. & pause & exit /b 1)
rem Modo de respaldo: abre el panel en el navegador y escucha en 127.0.0.1:8765 mientras esta ventana siga abierta.
py -3 manager.py web
if errorlevel 1 pause
