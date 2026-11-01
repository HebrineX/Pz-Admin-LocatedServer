@echo off
cd /d "%~dp0"
rem Abre la app en su propia ventana (sin consola y sin puertos locales).
if exist "%~dp0PZManager.exe" (
  start "" "%~dp0PZManager.exe"
  exit /b 0
)
where pyw >nul 2>nul
if errorlevel 1 (echo Instala Python 3.10 o superior, o genera PZManager.exe con crear_exe.bat. & pause & exit /b 1)
start "" pyw -3 "%~dp0manager.py" app
