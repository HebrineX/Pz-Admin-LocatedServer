# Project Zomboid Dedicated Manager para Windows — v3.1.0

App de escritorio para instalar, configurar y administrar un servidor dedicado de Project Zomboid (B42) en Windows.

## Contenido
- **`PZManager.exe`**: la app, con su propia ventana. No necesita permisos de administrador y no abre ningún puerto local. Se actualiza sola desde este repositorio (ver *Versiones*).
- `version.json`: versión publicada y huella SHA-256 de `PZManager.exe`; la app lo usa para ofrecer actualizaciones.
- **`instalar.bat`** (+ `instalar.ps1`): instala todo lo que la app necesita para funcionar.
- `manager.py`: el código de la app. También funciona desde la línea de comandos.
- **79 entradas** de mods con Workshop ID numérico (`initial_mods.json`) y **20 entradas excluidas** sin Workshop ID (`mods_excluidos_sin_workshop_id.json`). Se pueden recuperar desde la pestaña Mods o buscando su Workshop ID con *Agregar desde Workshop*.
- El programa no garantiza que los mods sean compatibles con B42 ni que funcionen en multijugador. La pestaña Mods verifica cada Workshop ID contra el `mod.info` descargado; después hay que probar el servidor.

## Instalación
No hace falta Python ni permisos de administrador. Se necesita Windows 10/11 de 64 bits y unos 15 GB libres.

1. Copiá la carpeta del paquete donde quieras tener el servidor, por ejemplo `D:\PZServer`. No uses Program Files.
2. Ejecutá **`instalar.bat`**. El instalador:
   - comprueba Windows, el espacio libre y .NET Framework 4.7.2+;
   - instala WebView2 si falta, verificando la firma de Microsoft;
   - descarga SteamCMD, verificando la firma de Valve;
   - instala el servidor dedicado (AppID 380870, unos 7 GB) con 3 reintentos;
   - crea accesos directos en el Escritorio y el menú Inicio, y abre la app.
3. En la app, **Inicio > Preparación** indica lo que falta. La primera vez, iniciá el servidor para que cree el INI; si pide la contraseña del usuario `admin`, escribila en **Consola**.
4. En **Mods**: *Verificar archivos locales* → *Guardar y aplicar al INI* → *Iniciar servidor*.

Opciones del instalador:

| Comando | Qué hace |
|---|---|
| `instalar.bat mis-mods.json` | Además carga ese catálogo de mods. Respalda el anterior y acepta Workshop IDs como número o URL. |
| `instalar.bat -Verificar` | Solo revisa y muestra qué falta, sin descargar ni cambiar nada. |
| `instalar.bat -OmitirServidor` | No descarga ni actualiza el servidor. |
| `instalar.bat -SinAccesos -NoAbrir` | Sin accesos directos y sin abrir la app al final. |

Volver a ejecutar `instalar.bat` repara o actualiza el servidor sin tocar `data\`, los mundos ni el INI. Cada ejecución deja un log en `logs\instalacion-*.log`.

Formato del catálogo (`id` es el Mod ID del `mod.info`; `enabled` y `notes` son opcionales):
```json
[
  {"id": "Brita", "workshop_id": "2200148440"},
  {"id": "OtroMod", "workshop_id": "https://steamcommunity.com/sharedfiles/filedetails/?id=123", "enabled": false}
]
```

## Archivos para desarrollo
| Archivo | Para qué sirve |
|---|---|
| `empaquetar.bat [destino] [catalogo.json]` | Compila el `.exe` y arma una versión limpia (por defecto en `dist\PZManager`) con `PZManager.exe`, `instalar.bat`, `instalar.ps1`, el catálogo y este README. Usa `data\mods.json` como catálogo si existe. |
| `crear_exe.bat` | Instala pywebview y PyInstaller en tu usuario, genera `PZManager.exe` y escribe `version.json`. Volvé a ejecutarlo si modificás `manager.py`. |
| `iniciar_panel.bat` | Abre la app (`PZManager.exe`, o `manager.py` si todavía no generaste el .exe). |
| `iniciar_servidor.bat` | Inicia el servidor en una consola: muestra la salida en vivo y lo que escribas se envía al servidor. Ctrl+C guarda y lo detiene. Requiere Python. |
| `actualizar_servidor.bat` / `validar_mods.bat` | Actualizar con SteamCMD / validar el catálogo desde la consola. Requieren Python. |
| `iniciar_panel_navegador.bat` | Modo de respaldo en el navegador (escucha en 127.0.0.1:8765). Requiere Python. |

### Versiones
1. Subí `VERSION` en `manager.py` (por ejemplo, de `3.1.0` a `3.1.1`).
2. Ejecutá `crear_exe.bat`: compila `PZManager.exe` y actualiza `version.json`.
3. Subí a `master` los dos archivos (y `manager.py`).

Al abrirse (como mucho cada 12 horas) o con *Buscar actualización* en Configuración, cada servidor lee `version.json` de `master`. Si la versión es mayor, ofrece *Actualizar*: descarga el exe, verifica su tamaño y su SHA-256, la app se cierra, se reemplaza el exe (el anterior queda como `PZManager.exe.old` hasta el próximo inicio) y vuelve a abrirse. Hace falta tener el servidor detenido. Solo se actualiza `PZManager.exe`; `instalar.*` y el README se copian a mano si cambian. GitHub guarda en caché los archivos unos 5 minutos: si justo después de subir una versión la descarga no coincide, probá de nuevo en un rato.

La primera vez hay que copiar a mano un exe 3.1.0 o posterior: las versiones anteriores no traen el actualizador.

Antes de cambiar de versión de Project Zomboid, respaldá `%USERPROFILE%\Zomboid`. No borres los saves.

La rama predeterminada de Steam no garantiza Build 42. Comprobá la versión instalada; si necesitás una rama beta, configurala en **Configuración** y actualizá de nuevo.

## Pestañas de la app
- **Inicio**: estado del servidor (en línea, iniciando, pide contraseña admin…), iniciar, *Detener y guardar*, guardar mundo, actualizar y forzar cierre. También muestra:
  - la lista de preparación y si el INI coincide con los mods activos;
  - **Actualizaciones de Workshop**: qué mods tienen una versión nueva en Steam y cuáles fueron retirados (un mod retirado puede impedir que el servidor arranque);
  - **Reiniciar para actualizar**: ahora o en 5, 10 o 15 minutos. La app avisa a los jugadores con `servermsg` y repite el aviso cuando falta poco. Después guarda, cierra y vuelve a iniciar el servidor, que descarga las actualizaciones al arrancar. Se puede cancelar durante la cuenta regresiva; *Detener* también la cancela.
- **Mods**: búsqueda, filtros (activos, inactivos, con avisos), activar o desactivar en bloque y orden de carga (arrastrando o con el número). También importa y exporta, y recupera los mods excluidos. Los cambios quedan pendientes hasta *Guardar*; Ctrl+S guarda.
  - *Agregar desde Workshop*: pegás la URL o el ID de uno o varios mods, o de una **colección**. La app muestra título, fecha de actualización, etiquetas (por ejemplo, Build 42) y el Mod ID que declara la descripción. Avisa si un mod fue retirado, es privado, es de otro juego o ya está en tu catálogo. Los que elegís se agregan activos al final del orden de carga.
  - El Mod ID que sale de la descripción queda *sin confirmar* hasta que *Verificar archivos locales* lo encuentre en el `mod.info`, una vez que el servidor descargue el mod. Si la descripción no lo trae, lo escribís vos.
  - *Buscar actualizaciones*: compara la fecha de cada mod en Steam con la de la copia que bajó el servidor.
  - *Verificar archivos locales*: confirma cada Workshop ID contra los `mod.info` locales, incluida tu biblioteca de Steam, y avisa si un mod activo **requiere** otro que no está activo (`require=` del `mod.info`).
  - *Limpiar descargas*: lista los mods que bajó el servidor y que el INI ya no pide, con su tamaño, y borra los que elijas: la carpeta y su registro en `appworkshop_108600.acf` (deja un `.bak`), así Steam no los da por instalados. Al *Guardar y aplicar al INI* después de quitar mods del catálogo, la app ofrece borrarlos en el mismo paso. Necesita el servidor detenido y nunca toca tu biblioteca de Steam ni `Zomboid\mods`.
- **Consola**: salida en vivo con filtro y colores, y envío de comandos (`save`, `players`, `servermsg`…). Las contraseñas que escribas ahí no se muestran.
- **Jugadores**: conectados ahora (expulsar, banear, cambiar rol, agregar a la whitelist), cuentas guardadas en `Zomboid\db\<servidor>.db` (solo lectura, nunca muestra contraseñas) y acceso: solo whitelist (`Open=false`), contraseña del servidor y visibilidad pública. Las acciones se envían como comandos de consola, así que el servidor tiene que estar iniciado desde la app.
  - **Personajes guardados**: los personajes del mundo (`Saves\Multiplayer\<servidor>\players.db`), vivos o muertos. *Borrar* lo saca del mundo: al volver a entrar, el jugador crea uno nuevo y conserva su cuenta, su rol y la whitelist. Quitar a alguien de la whitelist no borra su personaje; esto sí. Necesita el servidor detenido y antes guarda una copia en `backups\personajes`.
- **Red y puertos**: puertos del INI y quién los usa, reglas del firewall de Windows, IP local, la redirección necesaria en el router, UPnP y una revisión de seguridad.
- **Ajustes del servidor**: editor del `<servidor>.ini` con buscador y la descripción de cada clave. Cada guardado crea una copia `.bak`.
- **Configuración**: rutas, RAM del servidor (reemplaza `-Xms/-Xmx` sin tocar `StartServer64.bat`), rama de Steam, modo estricto y carpetas Workshop extra. Además:
  - **Reinstalar servidor**: borra los archivos del juego de la carpeta del servidor y los vuelve a descargar con SteamCMD, sin tocar mundos, INI, cuentas ni la configuración de la app. Por defecto conserva los mods descargados. Solo actúa si la carpeta tiene un servidor de PZ y no contiene la app, `Zomboid`, SteamCMD ni tu carpeta de usuario. Para solo reparar archivos alcanza con *Actualizar servidor*.
  - **Borrar mundo**: borra `Saves\Multiplayer\<servidor>` (y `<servidor>_player` si existe) para que el servidor genere un mundo nuevo al iniciar. Conserva el INI, el sandbox, las zonas de aparición, las cuentas y los baneos, y pone un `ResetID` nuevo en el INI para que los jugadores descarten la copia del mapa viejo que guarda su juego. Pide escribir el nombre del servidor y, por defecto, guarda una copia en `backups\mundos` (*Abrir copias*).
  - **Versión de la app**: versión instalada, *Buscar actualización*, *Actualizar* y la opción de buscar al abrir.

## Seguridad y permisos
- **Sin administrador**: la app funciona con un usuario estándar de Windows. Si la abrís como administrador, Inicio te avisa.
- **Sin puertos locales**: la ventana se comunica con Python directamente (pywebview/WebView2), sin servidor HTTP. El motor WebView2 corre sin herramientas de desarrollo, sin menú contextual, sin almacenamiento persistente y con el tráfico de fondo reducido. Los enlaces externos se abren en tu navegador.
- **Conexión con Steam**: solo cuando usás *Agregar desde Workshop* o *Buscar actualizaciones*. La app hace una consulta HTTPS saliente a la API pública de Steam (`api.steampowered.com`):
  - sin cuenta ni clave, con el certificado verificado y sin seguir redirecciones;
  - con una espera máxima de 15 s, respuestas de hasta 4 MB y colecciones de hasta 1000 mods.
  
  La ventana en sí no accede a Internet. Lo que devuelve Steam se muestra solo como texto y pasa por la misma validación que el catálogo.
- **Actualizaciones de la app**: la app lee `version.json` de `raw.githubusercontent.com/HebrineX/Pz-Admin-LocatedServer` con las mismas reglas (TLS verificado, sin redirecciones, tamaño máximo). El exe descargado no tiene firma digital: se instala solo si su tamaño y su SHA-256 coinciden con `version.json`. La garantía es la cuenta de GitHub que publica, así que conviene tenerla protegida con verificación en dos pasos. Se puede desactivar la búsqueda automática en Configuración.
- **Acciones que borran datos** (limpiar mods, reinstalar, borrar personajes o el mundo): piden el servidor detenido y una confirmación, y no siguen enlaces ni junctions. La única base de PZ que la app modifica es `players.db`, y solo al borrar un personaje, con copia previa; la base de cuentas sigue en solo lectura.
- **Firewall acotado**: *Red y puertos* crea una única regla de entrada **solo UDP**, solo para el `java.exe` del servidor y solo en el perfil de red actual. Podés elegir *solo red local* o *desde Internet*. Windows pide permiso de administrador (UAC) únicamente en ese paso. RCON nunca se abre. Si Windows ya había creado reglas amplias para `java.exe`, la app las detecta y ofrece deshabilitarlas.
- **Exposición recomendada**: whitelist activada (o contraseña), UPnP desactivado con redirección manual en el router solo de los puertos UDP del juego, y RCON vacío.
- **Una sola instancia**: no se pueden abrir dos ventanas que controlen el mismo servidor.
- **Cuenta dedicada (opcional)**: para aislar más, creá un usuario estándar de Windows solo para el servidor y ejecutá la app con esa cuenta. El servidor usa la carpeta `Zomboid` **del usuario que lo ejecuta** (`C:\Users\<cuenta>\Zomboid`): ajustá la ruta en *Configuración* y copiá ahí los saves y la configuración que quieras conservar.
- **Cierre seguro**: al cerrar la ventana con el servidor encendido, la app envía `quit`, espera a que guarde y recién después se cierra.

## Rutas y archivos
Las rutas son relativas a esta carpeta. `data/settings.json` y `data/mods.json` se crean en el primer inicio. El INI del servidor está en `%USERPROFILE%\Zomboid\Server\<servidor>.ini`. La app genera `server\StartServer64_panel.bat` (una copia de `StartServer64.bat` sin `PAUSE` y con la RAM elegida) cada vez que inicia el servidor. Los logs quedan en `logs\` y las copias de mundos y personajes borrados en `backups\` (se pueden borrar a mano cuando ya no hagan falta).

## Línea de comandos
```
py -3 manager.py app|web|validate|detect|apply|start|update
```
`app` es el comando por defecto. `web` acepta `--port` y `--no-browser`.

## Solución de problemas
- `Failed to set beta unstable`: dejá la rama vacía en Configuración (rama predeterminada).
- `StartServer64.bat no encontrado`: ejecutá `instalar.bat` (o *Actualizar servidor* en la app) y confirmá `Success! App '380870' fully installed.`
- `Missing configuration` al instalar el servidor: es un fallo intermitente de SteamCMD; el instalador reintenta 3 veces y, si persiste, volvé a ejecutar `instalar.bat`.
- La app no abre: aparece un aviso con el error. Si no aparece nada, ejecutá `instalar.bat -Verificar` para ver si falta WebView2 o .NET Framework.
- `No existe configuración inicial`: iniciá el servidor una vez para crear el INI; después aplicá los mods.
- `Falta el paquete pywebview`: ejecutá `crear_exe.bat`, o `py -3 -m pip install pywebview`.
- Mods que no cargan: revisá en Mods el estado de verificación y los avisos *Requiere…*. Algunos mods son solo de cliente.
- `No se pudo conectar con Steam`: revisá la conexión a Internet o el firewall. El resto de la app funciona sin conexión.
- *Buscar actualizaciones* dice que no hay fechas para comparar: el servidor todavía no descargó mods. Los baja al iniciar con los mods aplicados al INI.
- El servidor quedó abierto fuera de la app (por ejemplo, tras un cierre inesperado): Inicio lo muestra como "fuera del panel" y solo permite forzar el cierre.
- `Detené el servidor antes de…`: limpiar mods, reinstalar, borrar personajes o el mundo y actualizar la app necesitan el servidor y SteamCMD detenidos.
- `El PZManager.exe descargado no coincide con version.json`: suele ser la caché de GitHub justo después de publicar; probá en unos minutos. No se instala nada mientras no coincida.
- La actualización no se aplicó: al volver a abrirse, la app muestra el motivo en un aviso y sigue con la versión anterior.
# Pz-Admin-LocatedServer
