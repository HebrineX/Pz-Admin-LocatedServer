@echo off
title Crear PZManager.exe
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (echo Instala Python 3.10 o superior. & pause & exit /b 1)
echo Instalando/actualizando dependencias (pywebview, PyInstaller) en tu usuario...
py -3 -m pip install --user --disable-pip-version-check --quiet pywebview pyinstaller
if errorlevel 1 (echo ERROR: no se pudieron instalar las dependencias. & pause & exit /b 1)
echo Compilando PZManager.exe...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name PZManager --icon "%~dp0pz.ico" --distpath "%~dp0." --workpath "%~dp0build\work" --specpath "%~dp0build" "%~dp0manager.py"
if errorlevel 1 (echo ERROR: fallo la compilacion. & pause & exit /b 1)
rem version.json lleva la version y la SHA-256 del exe: la app lo compara para ofrecer actualizaciones.
py -3 "%~dp0manager.py" release >nul
if errorlevel 1 (echo ERROR: no se pudo generar version.json. & pause & exit /b 1)
echo.
echo Listo: %~dp0PZManager.exe y version.json
echo Para publicar la version, subi PZManager.exe y version.json a master (subi VERSION en manager.py en cada version nueva).
echo Si modificas manager.py, volve a ejecutar este archivo para regenerar el .exe.
pause
