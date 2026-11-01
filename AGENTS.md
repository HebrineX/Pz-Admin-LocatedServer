# AGENTS.md — PZ Dedicated Manager

App de escritorio para Windows que instala, configura y administra un servidor dedicado de Project Zomboid (B42).
Este proyecto lo gobierna **Hebri-AI-Harness 0.17.3**, vinculado (`bound`) en `.hebrinex/`. Cualquier agente (Codex, Claude Code, Copilot, Cursor, Gemini u otro) sigue este archivo.

## 1. Harness: la autoridad es `.hebrinex/`

- Si existe `.hebrinex/`, es la autoridad operativa del proyecto. No usar nunca un harness externo como autoridad.
- `.hebrinex/` es local y **no se sube a git** (está en `.gitignore`). En un clon nuevo no existe: no operar; proponer instalarlo desde https://github.com/HebrineX/Hebri-AI-Harness (v0.17.3) con preflight y `SI`.
- `.hebrinex/` contiene el producto compartido y no se edita en el trabajo del proyecto. El estado del proyecto vive en `.hebrinex/instance/` (ver `instance_path_map` en `.hebrinex/SHARED_MANIFEST.yaml`).

| Qué | Ruta |
|---|---|
| Contrato kernel del harness | `.hebrinex/AGENTS.md` |
| Binding del proyecto | `.hebrinex/instance/PROJECT_BINDING.yaml` |
| Pin de sesión | `.hebrinex/orquestador/memory/local/session-pin.md` |
| Registro y ruteo de memoria | `.hebrinex/instance/memory/memory-registry.yaml`, `.hebrinex/orquestador/memory/memory-routing.yaml` |
| Presupuesto de contexto | `.hebrinex/orquestador/context-budget.yaml` |
| Entrypoints (`first_message`, `reentry_light`…) | `.hebrinex/orquestador/entrypoints/` |
| Foco y contrato vivo | `.hebrinex/instance/memory/local/current-focus.md`, `active-contract.md` |
| Memoria del proyecto | `.hebrinex/instance/memory/project/` (`project-facts.md`, `architecture-summary.md`, `decisions.md`) |
| Evidencia | `.hebrinex/instance/sdd/progress/evidence/` |
| Detractor senior | `.hebrinex/agents/detractor-senior.md` |

Problemas conocidos del harness (no se arreglan desde este proyecto):
- **H-01**: `.hebrinex/PROJECT_BINDING.yaml` es la plantilla del producto. El binding real es el de `instance/`. Por eso `validate-release` falla en este proyecto.
- **H-02**: `instance/sdd/progress/state.yaml` heredó el ciclo del harness (`C-010/P06`). No es un ciclo de este proyecto.
- **H-03**: `memory-registry.yaml` cita rutas legacy `orquestador/memory/...`. Resolverlas con `instance_path_map` (`local`, `project`, `daily`, `cycle` y `complete` viven en `instance/memory/`; `session-pin.md` sigue en `orquestador/memory/local/`).

## 2. Inicio de sesión

1. Cargar solo el kernel de la tabla anterior (binding, pin, registro y ruteo de memoria, presupuesto y entrypoint), dentro de `context-budget.yaml`.
2. Declarar el contrato de sesión con el formato de `.hebrinex/AGENTS.md` y hechos observados, no supuestos.
3. Leer `current-focus.md` y `decisions.md` antes de proponer cambios.

No cargar por defecto: README ni CHANGELOG del harness, `init.sh`, `prompts/`, memoria `complete/` ni `infoHebri.md` (denegado).

## 3. Aprobaciones

- **Sin preflight**: lectura dentro del proyecto (leer archivos, buscar, `git status`/`log`/`diff`).
- **Con preflight y `SI` del operador**:
  - escribir archivos;
  - comandos con efecto, incluidos compilar e instalar paquetes;
  - red, incluidas la API de Steam y las descargas de SteamCMD;
  - git con efecto (`commit`, `push`, ramas, tags);
  - rutas fuera del proyecto;
  - cambios de estado SDD.
- El `SI` aprueba el alcance agrupado que declara el preflight, nada más. Si el alcance cambia, hace falta un preflight nuevo.
- IDs de aprobación: `PZ-<ÁREA>-NNN` (usados: `PZ-HBX-001`, `PZ-INST-001`, `PZ-WS-001`).
- Antes de implementar, hacer la pasada del **detractor senior** (`.hebrinex/agents/detractor-senior.md`) y mostrar su salida en el preflight.

Formato del preflight (el del kernel):
```text
Approval ID:
Accion propuesta:
CWD:
Read-set:
Write-set:
Comando/tool:
Red/git/externo:
Riesgo:
Verificacion:
Evidencia esperada:
Requiere SI: SI
```

## 4. Mapa del proyecto

| Archivo | Qué es |
|---|---|
| `manager.py` | Toda la app: backend con biblioteca estándar, UI HTML/JS embebida en `PAGE` y CLI (`app`, `web`, `validate`, `detect`, `apply`, `start`, `update`). |
| `PZManager.exe` | `manager.py` compilado con PyInstaller (onefile, ventana propia). Está versionado: recompilarlo si cambia `manager.py`. |
| `instalar.bat` + `instalar.ps1` | Instalador portable: .NET, WebView2, SteamCMD, servidor, catálogo y accesos directos. |
| `crear_exe.bat` / `empaquetar.bat` | Compilar el exe / armar una copia distribuible en `dist\PZManager`. |
| `iniciar_*.bat`, `actualizar_servidor.bat`, `validar_mods.bat` | Atajos de desarrollo que necesitan Python. |
| `initial_mods.json` | Catálogo semilla. Se copia a `data\mods.json` en el primer inicio. |
| `mods_excluidos_sin_workshop_id.json` | Mods apartados por no tener Workshop ID. Se recuperan desde la pestaña Mods. |
| `pz.ico` | Ícono de la app y de los accesos directos. |

La app y el instalador crean al usarse `data/`, `logs/`, `server/` y `steamcmd/`; la compilación crea `build/` y `dist/`. Todo eso está en `.gitignore`.

## 5. Reglas del producto (no negociables)

- **Usuario estándar**: la app no requiere administrador. UAC aparece solo al cambiar el firewall.
- **Sin puertos en escucha** en modo app: la UI habla con Python por el puente de pywebview (`Bridge.call` → `dispatch`). El modo web (`127.0.0.1`, con validación de Host, Origin y token) es solo de respaldo.
- **La ventana no sale a Internet**: la CSP de `PAGE` mantiene `connect-src 'self'`. Cualquier acceso a la red lo hace Python, con host fijo, TLS verificado, timeout y límite de tamaño.
- **Datos no confiables** (Steam, logs, DB, INI): se muestran con `textContent`. Nunca usar `innerHTML`: un XSS tendría acceso al puente.
- **Dependencias**: backend solo con la biblioteca estándar. pywebview es la única dependencia en tiempo de ejecución y PyInstaller solo compila. Una dependencia nueva necesita la pasada del detractor y `SI`.
- **Jugadores y whitelist**: la DB de PZ se abre solo en lectura (`mode=ro`). Los cambios se hacen como comandos de consola.
- **Firewall**: una regla de entrada solo UDP, solo para el `java.exe` del servidor y solo en el perfil actual. RCON nunca se abre.
- **Descargas**: un binario descargado se ejecuta solo con firma Authenticode válida de Microsoft (WebView2) o Valve (`O=Valve` / `O=Valve Corp.`).
- **Escrituras del usuario**: el INI se modifica con `write_ini_keys` (deja un `.bak`); `Mods` y `WorkshopItems` solo con *Aplicar*. El catálogo cambia solo con *Guardar*.
- **Pruebas**: nunca usar el servidor real del operador, sus mundos (`%USERPROFILE%\Zomboid`), su INI ni su DB. Probar en una carpeta temporal con `settings.json` propio y un servidor falso cuando haga falta.

## 6. Convenciones de código

- Textos de UI, mensajes, documentación y comentarios en español rioplatense (voseo).
- En `manager.py`: un docstring de una línea por función y comentarios escasos que expliquen el porqué.
- Rutas de la API en `GET_ROUTES` / `POST_ROUTES` como `(función, toma_LOCK)`. Lo que espera al usuario o al servidor no toma `LOCK`.
- `instalar.ps1` debe funcionar en Windows PowerShell 5.1: UTF-8 con BOM, y CRLF en `.ps1` y `.bat`.
- Mantener el README al día con cada cambio visible para el usuario.

## 7. Verificación

```bash
py -3 -m py_compile manager.py
py -3 manager.py validate
py -3 manager.py web --no-browser --port 8765
crear_exe.bat
instalar.bat -Verificar
```

- El tercer comando sirve para revisar la UI en un navegador. `crear_exe.bat` recompila `PZManager.exe` y `instalar.bat -Verificar` solo lee.
- Después de cambios de seguridad: comprobar que el árbol de procesos de `PZManager.exe` no tenga puertos TCP en escucha y que no aparezca `innerHTML`.
- Registrar lo hecho en `.hebrinex/instance/sdd/progress/evidence/AAAA-MM-DD-<tema>.md`, las decisiones en `decisions.md` (`D-NNN`) y el siguiente paso en `current-focus.md`. Separar lo verificado de lo no verificado.

## 8. Git

- Remoto `origin`: https://github.com/HebrineX/Pz-Admin-LocatedServer.git, rama `master`.
- Commit y push solo cuando el operador lo pide, con preflight y `SI`.
- `.hebrinex/` y los datos de ejecución nunca se suben.
