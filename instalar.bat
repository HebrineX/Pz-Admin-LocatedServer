@echo off
setlocal
title Instalador de PZ Dedicated Manager
cd /d "%~dp0"
rem Toda la logica esta en instalar.ps1. Ejemplos:
rem   instalar.bat                  instala o actualiza todo
rem   instalar.bat mis-mods.json    ademas carga ese catalogo de mods
rem   instalar.bat -Verificar       solo revisa que falta, sin cambiar nada
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1" %*
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo El instalador termino con problemas (codigo %CODE%). El detalle queda en la carpeta logs.
pause
exit /b %CODE%
