@echo off
cd /d "%~dp0"
py -3 manager.py update
if errorlevel 1 (echo ERROR: Revisa el log de SteamCMD. & pause & exit /b 1)
echo Actualizacion finalizada. Verifica la version instalada.
pause
