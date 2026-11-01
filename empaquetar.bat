@echo off
setlocal
title Empaquetar PZ Dedicated Manager
cd /d "%~dp0"
rem Genera una version limpia (sin servidor, logs ni datos) lista para instalar con instalar.bat.
rem Uso: empaquetar.bat [carpeta_destino] [catalogo.json]
rem   Destino por defecto: dist\PZManager. Catalogo por defecto: data\mods.json (o initial_mods.json).
rem   No borra nada: si el destino ya existe, solo actualiza estos archivos.
set "DEST=%~1"
if "%DEST%"=="" set "DEST=%~dp0dist\PZManager"
set "CAT=%~2"
if "%CAT%"=="" if exist "%~dp0data\mods.json" set "CAT=%~dp0data\mods.json"
if "%CAT%"=="" set "CAT=%~dp0initial_mods.json"
if not exist "%CAT%" (echo No existe el catalogo: %CAT% & exit /b 1)

echo Compilando PZManager.exe...
call "%~dp0crear_exe.bat" < nul
if errorlevel 1 (echo ERROR: no se pudo compilar PZManager.exe. & exit /b 1)

if not exist "%DEST%" mkdir "%DEST%"
for %%F in (PZManager.exe pz.ico mods_excluidos_sin_workshop_id.json instalar.bat instalar.ps1 README.md) do (
  copy /y "%~dp0%%F" "%DEST%\" >nul || (echo ERROR copiando %%F & exit /b 1)
)
copy /y "%CAT%" "%DEST%\initial_mods.json" >nul || (echo ERROR copiando el catalogo & exit /b 1)
echo.
echo Version limpia lista en: %DEST%
echo Copiala a la PC del servidor y ejecuta instalar.bat
exit /b 0
