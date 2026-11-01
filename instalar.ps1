<#
Instalador de PZ Dedicated Manager.
Deja listas, en esta misma carpeta, las dependencias de PZManager.exe:
.NET Framework, WebView2, catálogo de mods, SteamCMD y el servidor dedicado de Project Zomboid.
No requiere administrador. Volver a ejecutarlo repara o actualiza sin tocar mundos ni configuración.

Uso (desde instalar.bat):
  instalar.bat                      instala o actualiza todo
  instalar.bat mis-mods.json        además carga ese catálogo de mods
  instalar.bat -Verificar           solo revisa y muestra qué falta (no cambia nada)
  instalar.bat -OmitirServidor      no descarga ni actualiza el servidor
  instalar.bat -SinAccesos -NoAbrir sin accesos directos y sin abrir la app al final
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Mods = '',
  [switch]$Verificar,
  [switch]$OmitirServidor,
  [switch]$SinAccesos,
  [switch]$NoAbrir
)

$ErrorActionPreference = 'Stop'
# Invoke-WebRequest es mucho más lento con la barra de progreso de PowerShell 5.1.
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root = $PSScriptRoot
$Exe = Join-Path $Root 'PZManager.exe'
$Seed = Join-Path $Root 'initial_mods.json'
$DataDir = Join-Path $Root 'data'
$Catalog = Join-Path $DataDir 'mods.json'
$SettingsFile = Join-Path $DataDir 'settings.json'
$SteamDir = Join-Path $Root 'steamcmd'
$SteamExe = Join-Path $SteamDir 'steamcmd.exe'
$ServerDir = Join-Path $Root 'server'
$ServerBat = Join-Path $ServerDir 'StartServer64.bat'
$AppId = '380870'
$SteamCmdUrl = 'https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip'
$WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
$WebView2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$NetFxUrl = 'https://dotnet.microsoft.com/download/dotnet-framework/net48'
$Utf8 = New-Object System.Text.UTF8Encoding $false
$script:Problemas = New-Object System.Collections.Generic.List[string]

function Paso([string]$Texto) { Write-Host ''; Write-Host "== $Texto" -ForegroundColor Cyan }
function Ok([string]$Texto) { Write-Host "   [OK] $Texto" -ForegroundColor Green }
function Info([string]$Texto) { Write-Host "   $Texto" }
function Aviso([string]$Texto) { Write-Host "   [!]  $Texto" -ForegroundColor Yellow }
function Falta([string]$Texto) { Write-Host "   [X]  $Texto" -ForegroundColor Red; $script:Problemas.Add($Texto) }
function Detener([string]$Texto) { Falta $Texto; throw $Texto }

function Nueva-CarpetaTemporal {
  # Cada descarga va a una carpeta nueva y vacía.
  $dir = Join-Path ([IO.Path]::GetTempPath()) ('pzmanager-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
  New-Item -ItemType Directory -Path $dir | Out-Null
  return $dir
}

function Test-Firma([string]$Archivo, [string[]]$Organizaciones) {
  # Solo se ejecuta lo descargado si la firma digital es válida y de alguno de los fabricantes esperados.
  $firma = Get-AuthenticodeSignature -LiteralPath $Archivo
  if ($firma.Status -ne 'Valid') { return $false }
  foreach ($org in $Organizaciones) {
    if ($firma.SignerCertificate.Subject -match ('(^|, )O=' + [regex]::Escape($org) + '(,|$)')) { return $true }
  }
  return $false
}
# El steamcmd.exe del zip oficial está firmado como "Valve"; al autoactualizarse pasa a "Valve Corp.".
$FirmasValve = @('Valve Corp.', 'Valve')

function Get-WebView2Version {
  $claves = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$WebView2Guid",
              "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$WebView2Guid",
              "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$WebView2Guid")
  foreach ($clave in $claves) {
    $v = (Get-ItemProperty -Path $clave -ErrorAction SilentlyContinue).pv
    if ($v -and $v -ne '0.0.0.0') { return $v }
  }
  return $null
}

function Get-ServidorEnEjecucion {
  # java.exe de ESTE servidor (zombie.network.GameServer desde la carpeta server).
  $procesos = Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue
  foreach ($p in $procesos) {
    if ($p.CommandLine -like '*zombie.network.GameServer*' -and $p.ExecutablePath -and
        $p.ExecutablePath.StartsWith($ServerDir, [StringComparison]::OrdinalIgnoreCase)) { return $p.ProcessId }
  }
  return $null
}

function Invoke-SteamCmd([string[]]$Argumentos) {
  # La salida de SteamCMD se muestra y queda en el log; devuelve el texto para revisar el resultado.
  $lineas = New-Object System.Collections.Generic.List[string]
  # Con 'Stop', PowerShell 5.1 cortaría el script ante cualquier línea que SteamCMD escriba en stderr.
  $anterior = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & $SteamExe @Argumentos 2>&1 | ForEach-Object {
      $texto = ($_.ToString() -split "`r")[-1]
      if ($texto.Trim()) { Write-Host "     $texto"; $lineas.Add($texto) }
    }
    $codigo = $LASTEXITCODE
  }
  finally { $ErrorActionPreference = $anterior }
  return @{ Codigo = $codigo; Texto = ($lineas -join "`n") }
}

function Convertir-WorkshopId($Valor) {
  $texto = [string]$Valor
  $texto = $texto.Trim()
  if (-not $texto) { return '' }
  if ($texto -match '^\d{1,20}$') { return $texto }
  if ($texto -match '[?&]id=(\d{1,20})') { return $Matches[1] }
  throw "Workshop ID inválido: $texto"
}

function Leer-Catalogo([string]$Archivo) {
  # Acepta la lista de la app o una más simple; la normaliza a {id, enabled, workshop_id, notes}.
  try {
    # En PowerShell 5.1 ConvertFrom-Json no enumera arreglos: se asigna primero y después se envuelve.
    $json = Get-Content -LiteralPath $Archivo -Raw -Encoding UTF8 | ConvertFrom-Json
    $datos = @($json)
  } catch {
    throw "El archivo de mods no es un JSON válido: $Archivo"
  }
  $vistos = @{}
  $salida = New-Object System.Collections.Generic.List[object]
  foreach ($m in $datos) {
    $id = [string]$m.id
    $id = $id.Trim()
    if (-not $id -or $id -match '[;\\\r\n]' -or $id.Length -gt 150) { throw "Mod ID inválido en el catálogo: '$id'" }
    if ($vistos.ContainsKey($id)) { throw "Mod ID duplicado en el catálogo: $id" }
    $vistos[$id] = $true
    $activo = $true
    if ($null -ne $m.enabled) { $activo = [bool]$m.enabled }
    $notas = ''
    if ($null -ne $m.notes) { $notas = ([string]$m.notes).Substring(0, [Math]::Min(500, ([string]$m.notes).Length)) }
    $salida.Add([ordered]@{ id = $id; enabled = $activo; workshop_id = (Convertir-WorkshopId $m.workshop_id); notes = $notas })
  }
  if ($salida.Count -eq 0) { throw 'El catálogo de mods está vacío.' }
  return ,$salida
}

$transcripcion = $false
try {
  Write-Host 'PZ Dedicated Manager - Instalador' -ForegroundColor White
  Write-Host "Carpeta: $Root"
  if ($Verificar) { Write-Host 'Modo verificación: no se descarga ni se modifica nada.' -ForegroundColor Yellow }
  else {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
    $log = Join-Path $Root ('logs\instalacion-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Start-Transcript -LiteralPath $log | Out-Null
    $transcripcion = $true
  }

  Paso 'Sistema'
  if (-not [Environment]::Is64BitOperatingSystem) { Detener 'Se necesita Windows de 64 bits.' }
  $so = [Environment]::OSVersion.Version
  if ($so.Major -lt 10) { Detener "Se necesita Windows 10 u 11 (detectado $so)." }
  # Windows 11 se reporta como 10.0: se distingue por el número de build.
  $nombreSo = 'Windows 10'
  if ($so.Build -ge 22000) { $nombreSo = 'Windows 11' }
  Ok "$nombreSo de 64 bits (build $($so.Build))"
  if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Aviso 'Estás ejecutando el instalador como administrador. No hace falta: con tu usuario normal alcanza.'
  }

  Paso 'Carpeta de instalación'
  foreach ($pf in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:windir)) {
    if ($pf -and $Root.StartsWith($pf, [StringComparison]::OrdinalIgnoreCase)) {
      Detener "La carpeta está dentro de $pf, donde tu usuario no puede escribir. Mové la carpeta (por ejemplo a D:\PZServer) y volvé a ejecutar."
    }
  }
  if ($Root -match '[^\x20-\x7E]') { Aviso 'La ruta tiene acentos u otros caracteres especiales; SteamCMD y Java pueden fallar. Mejor una ruta simple como D:\PZServer.' }
  if (-not $Verificar) {
    $prueba = Join-Path $Root ('.escritura-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    try { [IO.File]::WriteAllText($prueba, 'ok'); Remove-Item -LiteralPath $prueba -Force }
    catch { Detener "No se puede escribir en $Root. Elegí una carpeta propia de tu usuario." }
  }
  $necesarioGB = 2
  if (-not (Test-Path -LiteralPath $ServerBat)) { $necesarioGB = 15 }
  $libreGB = [Math]::Round((New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($Root))).AvailableFreeSpace / 1GB, 1)
  if ($libreGB -lt $necesarioGB) { Falta "Hay $libreGB GB libres y se necesitan al menos $necesarioGB GB (servidor y mods)." }
  else { Ok "$libreGB GB libres (se necesitan $necesarioGB GB)" }

  Paso '.NET Framework'
  $release = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction SilentlyContinue).Release
  if ($release -ge 461808) { Ok ".NET Framework 4.7.2 o superior (release $release)" }
  else { Falta ".NET Framework 4.7.2 o superior no está instalado. Instalalo desde $NetFxUrl (requiere administrador y reinicio)." }

  Paso 'Microsoft Edge WebView2 Runtime'
  $wv = Get-WebView2Version
  if ($wv) { Ok "WebView2 $wv" }
  elseif ($Verificar) { Falta 'WebView2 Runtime no está instalado (el instalador lo descarga de Microsoft).' }
  else {
    Info 'No está instalado: descargando el instalador oficial de Microsoft...'
    $tmp = Nueva-CarpetaTemporal
    $setup = Join-Path $tmp 'MicrosoftEdgeWebview2Setup.exe'
    Invoke-WebRequest -Uri $WebView2Url -OutFile $setup -UseBasicParsing
    if (-not (Test-Firma $setup 'Microsoft Corporation')) { Detener 'El instalador de WebView2 descargado no tiene una firma válida de Microsoft. No se ejecutó.' }
    $proc = Start-Process -FilePath $setup -ArgumentList '/silent', '/install' -Wait -PassThru
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    $wv = Get-WebView2Version
    if ($wv) { Ok "WebView2 $wv instalado" }
    else { Falta "No se pudo instalar WebView2 (código $($proc.ExitCode)). Instalalo a mano desde https://developer.microsoft.com/microsoft-edge/webview2/" }
  }

  Paso 'PZ Dedicated Manager'
  if (Test-Path -LiteralPath $Exe) { Ok 'PZManager.exe presente' }
  else { Falta 'Falta PZManager.exe en esta carpeta (generalo con crear_exe.bat o usá el paquete de empaquetar.bat).' }
  if ($Mods) {
    $origen = $Mods
    if (-not [IO.Path]::IsPathRooted($origen)) { $origen = Join-Path (Get-Location).Path $origen }
    if (-not (Test-Path -LiteralPath $origen)) { Detener "No existe el archivo de mods: $Mods" }
    $lista = Leer-Catalogo $origen
    $activos = @($lista | Where-Object { $_.enabled }).Count
    if ($Verificar) { Ok "Catálogo válido: $($lista.Count) mods ($activos activos). Se cargaría al instalar." }
    else {
      New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
      if (Test-Path -LiteralPath $Catalog) {
        $copia = "$Catalog.$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
        Copy-Item -LiteralPath $Catalog -Destination $copia
        Info "Catálogo anterior respaldado en $(Split-Path -Leaf $copia)"
      }
      [IO.File]::WriteAllText($Catalog, (ConvertTo-Json -InputObject $lista.ToArray() -Depth 3), $Utf8)
      Ok "Catálogo cargado: $($lista.Count) mods ($activos activos). Aplicalo al INI desde la app."
    }
  }
  elseif (Test-Path -LiteralPath $Catalog) { Ok 'Se conserva el catálogo actual (data\mods.json)' }
  elseif (Test-Path -LiteralPath $Seed) { Ok 'Catálogo inicial: initial_mods.json (la app lo carga al abrir)' }
  else { Aviso 'Sin catálogo de mods: la app arranca con la lista vacía.' }

  # Si la carpeta se movió, settings.json puede seguir apuntando a la ubicación anterior.
  if (Test-Path -LiteralPath $SettingsFile) {
    $ajustes = Get-Content -LiteralPath $SettingsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $cambios = @()
    if ($ajustes.server_dir -and -not (Test-Path -LiteralPath $ajustes.server_dir) -and $ajustes.server_dir -ne $ServerDir) { $cambios += 'server_dir' }
    if ($ajustes.steamcmd_path -and -not (Test-Path -LiteralPath $ajustes.steamcmd_path) -and $ajustes.steamcmd_path -ne $SteamExe) { $cambios += 'steamcmd_path' }
    if ($cambios.Count -and $Verificar) { Aviso "data\settings.json apunta a rutas que no existen ($($cambios -join ', ')); el instalador las corregiría a esta carpeta." }
    elseif ($cambios.Count) {
      Copy-Item -LiteralPath $SettingsFile -Destination "$SettingsFile.$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
      if ($cambios -contains 'server_dir') { $ajustes.server_dir = $ServerDir }
      if ($cambios -contains 'steamcmd_path') { $ajustes.steamcmd_path = $SteamExe }
      [IO.File]::WriteAllText($SettingsFile, ($ajustes | ConvertTo-Json -Depth 5), $Utf8)
      Ok "Rutas de data\settings.json corregidas a esta carpeta ($($cambios -join ', '))"
    }
  }

  Paso 'SteamCMD'
  if (Test-Path -LiteralPath $SteamExe) { Ok 'SteamCMD presente' }
  elseif ($Verificar) { Falta 'SteamCMD no está instalado (el instalador lo descarga de Valve).' }
  else {
    Info 'Descargando SteamCMD de Valve...'
    $tmp = Nueva-CarpetaTemporal
    $zip = Join-Path $tmp 'steamcmd.zip'
    Invoke-WebRequest -Uri $SteamCmdUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $nuevo = Join-Path $tmp 'steamcmd.exe'
    if (-not (Test-Path -LiteralPath $nuevo) -or -not (Test-Firma $nuevo $FirmasValve)) {
      Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
      Detener 'steamcmd.exe descargado no tiene una firma válida de Valve. No se instaló.'
    }
    New-Item -ItemType Directory -Force -Path $SteamDir | Out-Null
    Move-Item -LiteralPath $nuevo -Destination $SteamExe
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok 'SteamCMD instalado'
  }
  if (-not $Verificar -and (Test-Path -LiteralPath $SteamExe)) {
    Info 'Actualizando SteamCMD (la primera vez descarga unos 40 MB)...'
    $r = Invoke-SteamCmd @('+quit')
    if ($r.Codigo -ne 0) { Aviso "SteamCMD terminó con código $($r.Codigo); se continúa igual." }
    if (-not (Test-Firma $SteamExe $FirmasValve)) { Detener 'steamcmd.exe actualizado no tiene una firma válida de Valve.' }
  }

  Paso 'Servidor dedicado de Project Zomboid'
  $pidServidor = Get-ServidorEnEjecucion
  if ($OmitirServidor) {
    if (Test-Path -LiteralPath $ServerBat) { Ok 'Servidor presente (no se actualiza: -OmitirServidor)' }
    else { Aviso 'Servidor no instalado (se omitió con -OmitirServidor).' }
  }
  elseif ($Verificar) {
    if (Test-Path -LiteralPath $ServerBat) { Ok 'Servidor presente (el instalador lo actualizaría con SteamCMD)' }
    else { Falta "Servidor no instalado (el instalador lo descarga: unos 7 GB, AppID $AppId)." }
    if ($pidServidor) { Aviso "El servidor está en ejecución (PID $pidServidor): cerralo antes de instalar." }
  }
  elseif ($pidServidor) { Detener "El servidor está en ejecución (PID $pidServidor). Detenelo desde la app antes de actualizarlo." }
  elseif (-not (Test-Path -LiteralPath $SteamExe)) { Falta 'No se puede instalar el servidor sin SteamCMD.' }
  else {
    New-Item -ItemType Directory -Force -Path $ServerDir | Out-Null
    $listo = $false
    for ($intento = 1; $intento -le 3 -and -not $listo; $intento++) {
      Info "Instalando/actualizando con SteamCMD (intento $intento de 3). La primera vez tarda varios minutos..."
      $r = Invoke-SteamCmd @('+force_install_dir', $ServerDir, '+login', 'anonymous', '+app_update', $AppId, 'validate', '+quit')
      $listo = ($r.Texto -match "Success! App '$AppId'") -and (Test-Path -LiteralPath $ServerBat)
      if (-not $listo -and $intento -lt 3) { Aviso 'SteamCMD no confirmó la instalación; se reintenta en 10 segundos.'; Start-Sleep -Seconds 10 }
    }
    if ($listo) { Ok 'Servidor instalado y actualizado' }
    else { Falta 'SteamCMD no pudo instalar el servidor tras 3 intentos. Revisá el log; si aparece "Missing configuration", volvé a ejecutar el instalador.' }
  }

  if (-not $Verificar -and -not $SinAccesos -and (Test-Path -LiteralPath $Exe)) {
    Paso 'Accesos directos'
    $shell = New-Object -ComObject WScript.Shell
    foreach ($carpeta in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
      $acceso = $shell.CreateShortcut((Join-Path $carpeta 'PZ Dedicated Manager.lnk'))
      $acceso.TargetPath = $Exe
      $acceso.WorkingDirectory = $Root
      $acceso.IconLocation = "$Exe,0"
      $acceso.Description = 'Administrar el servidor dedicado de Project Zomboid'
      $acceso.Save()
    }
    Ok 'Escritorio y menú Inicio'
  }

  Write-Host ''
  if ($script:Problemas.Count) {
    Write-Host "Quedan $($script:Problemas.Count) problema(s):" -ForegroundColor Red
    foreach ($p in $script:Problemas) { Write-Host " - $p" -ForegroundColor Red }
    $codigo = 1
  }
  else {
    if ($Verificar) { Write-Host 'Todo listo: no falta ninguna dependencia.' -ForegroundColor Green }
    else {
      Write-Host 'Instalación completa.' -ForegroundColor Green
      Write-Host 'En la app: Inicio > Preparación te indica los pasos que faltan (primer inicio, contraseña de admin, aplicar mods).'
    }
    $codigo = 0
  }
  if ($codigo -eq 0 -and -not $Verificar -and -not $NoAbrir -and (Test-Path -LiteralPath $Exe)) {
    Start-Process -FilePath $Exe -WorkingDirectory $Root
  }
}
catch {
  Write-Host ''
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  $codigo = 1
}
finally {
  if ($transcripcion) { Stop-Transcript | Out-Null }
}
exit $codigo
