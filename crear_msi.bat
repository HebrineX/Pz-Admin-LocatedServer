@echo off
setlocal
title Crear instalador MSI de PZ Dedicated Manager
cd /d "%~dp0"
rem Arma dist\PZManager-<version>.msi: instalacion por usuario, sin administrador (WiX 5).
rem Usa el PZManager.exe y el version.json actuales: si cambiaste manager.py, ejecuta antes crear_exe.bat.
where wix >nul 2>nul
if errorlevel 1 (echo Falta WiX 5: dotnet tool install --global wix --version 5.0.2 & pause & exit /b 1)
wix extension list -g | findstr /i "WixToolset.UI.wixext" >nul
if errorlevel 1 (
  echo Agregando la extension de interfaz de WiX...
  wix extension add -g WixToolset.UI.wixext/5.0.2
  if errorlevel 1 (echo ERROR: no se pudo agregar WixToolset.UI.wixext. & pause & exit /b 1)
)
set "VERSION="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "$v = Get-Content -Raw 'version.json' | ConvertFrom-Json; $h = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([IO.File]::ReadAllBytes((Resolve-Path 'PZManager.exe')))).Replace('-', '').ToLower(); if ($h -eq $v.files.'PZManager.exe'.sha256) { $v.version }"`) do set "VERSION=%%V"
if "%VERSION%"=="" (echo ERROR: PZManager.exe no coincide con version.json. Ejecuta crear_exe.bat primero. & pause & exit /b 1)
set "ROOTDIR=%~dp0"
set "ROOTDIR=%ROOTDIR:~0,-1%"
if not exist "%~dp0dist" mkdir "%~dp0dist"
echo Armando PZManager-%VERSION%.msi...
wix build "%~dp0instalador\PZManager.wxs" -arch x64 -culture es-ES -ext WixToolset.UI.wixext -d Version=%VERSION% -d "Root=%ROOTDIR%" -o "%~dp0dist\PZManager-%VERSION%.msi"
if errorlevel 1 (echo ERROR: fallo la compilacion del MSI. & pause & exit /b 1)
echo.
echo Listo: %~dp0dist\PZManager-%VERSION%.msi
echo Para compartirlo, subilo a un Release de GitHub o copialo. No tiene firma: SmartScreen avisa al abrirlo descargado.
pause
