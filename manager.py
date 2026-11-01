#!/usr/bin/env python3
"""Administrador local de Project Zomboid: solo biblioteca estándar Python."""
import argparse
import base64
import ctypes
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

# Compilado con PyInstaller, los datos viven junto a PZManager.exe y no en la carpeta temporal del ejecutable.
FROZEN = getattr(sys, 'frozen', False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DATA = ROOT / 'data'
CATALOG = DATA / 'mods.json'
SETTINGS = DATA / 'settings.json'
EXCLUDED = ROOT / 'mods_excluidos_sin_workshop_id.json'
LOGS = ROOT / 'logs'
LOCK = threading.RLock()
INSPECT_LOCK = threading.Lock()
PROCESS = None
PROC_INFO = {}
EXTERNAL = {'checked': 0.0, 'pid': None}
TOKEN = secrets.token_urlsafe(32)
PORT = 8765
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
# Mods y WorkshopItems solo se escriben desde el catálogo (apply_config).
PROTECTED_INI_KEYS = {'Mods', 'WorkshopItems'}
# Ventana propia: sin servidor HTTP; la página llama a dispatch() por el puente de pywebview.
APP = {'active': False, 'window': None, 'dirty': False, 'allow_close': False, 'mutex': None}
FW_GROUP = 'PZ Dedicated Manager'
ROLES = ('none', 'priority', 'observer', 'gm', 'moderator', 'admin')
# API pública de Steam Workshop: sin clave, solo este host y con límites de tamaño y cantidad.
STEAM_API = 'https://api.steampowered.com/ISteamRemoteStorage/'
STEAM_MAX_BYTES = 4 * 1024 * 1024
STEAM_MAX_ITEMS = 1000
WS_CHECK = {}
RESTART = {'active': False, 'phase': '', 'at': 0, 'error': '', 'cancel': None}
RESTART_NOTICES = (15, 10, 5, 2, 1, 0)

class NotFound(Exception):
    """Ruta de la API inexistente."""

DEFAULT = {
 'server_name': 'servertest',
 'server_dir': str(ROOT / 'server'),
 'steamcmd_path': str(ROOT / 'steamcmd' / 'steamcmd.exe'),
 'pz_home': str(Path.home() / 'Zomboid'),
 'branch': '',
 'port': 8765,
 'strict': True,
 'steam_workshop_appid': '108600',
 'server_appid': '380870',
 'extra_server_args': '',
 'ram_gb': 0,
}

def ensure():
    """Crear carpetas y archivos iniciales sin sobrescribir cambios del usuario."""
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if not CATALOG.exists():
        seed = ROOT / 'initial_mods.json'
        if seed.is_file():
            shutil.copyfile(seed, CATALOG)
        else:
            # Sin catálogo semilla la app arranca con la lista vacía en vez de cerrarse.
            save_json(CATALOG, [])
    if not SETTINGS.exists():
        SETTINGS.write_text(json.dumps(DEFAULT, indent=2, ensure_ascii=False), encoding='utf-8')

def read_json(path):
    """Leer JSON y devolver un objeto Python."""
    return json.loads(path.read_text(encoding='utf-8'))

def save_json(path, data):
    """Escribir JSON atómicamente para evitar archivos truncados."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)

def settings():
    """Obtener ajustes persistentes (completando claves nuevas con sus valores por defecto)."""
    return {**DEFAULT, **read_json(SETTINGS)}

def mods():
    """Obtener catálogo de mods."""
    return read_json(CATALOG)

def safe_name(name):
    """Validar nombre de perfil de servidor sin rutas arbitrarias."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', name):
        raise ValueError('Nombre de servidor no válido (letras, números, _ y -).')
    return name

def ini_path(s):
    """Ruta del INI del perfil de servidor configurado."""
    return Path(s['pz_home']) / 'Server' / (safe_name(s['server_name']) + '.ini')

def workshop_id(raw):
    """Normalizar ID numérico o enlace Steam Workshop con parámetro id."""
    raw = str(raw or '').strip()
    if not raw:
        return ''
    if raw.isdigit() and len(raw) <= 20:
        return raw
    parsed = urlparse(raw)
    if parsed.hostname in ('steamcommunity.com', 'www.steamcommunity.com'):
        value = parse_qs(parsed.query).get('id', [''])[0]
        if value.isdigit():
            return value
    raise ValueError('Workshop ID inválido. Pegá un número o URL de Steam Workshop.')

def mod_issues(items=None):
    """Detectar duplicados, Mod IDs inválidos y Workshop IDs ausentes en los mods activos."""
    items = mods() if items is None else items
    seen = set()
    issues = []
    enabled = [m for m in items if m.get('enabled', False)]
    for m in enabled:
        mid = str(m.get('id',''))
        if not mid or ';' in mid or '\n' in mid or '\r' in mid:
            issues.append(f'Mod ID inválido: {mid!r}')
        if mid in seen:
            issues.append(f'Mod duplicado: {mid}')
        seen.add(mid)
        wid = m.get('workshop_id','')
        if not wid:
            issues.append(f'Falta Workshop ID: {mid}')
        elif not str(wid).isdigit():
            issues.append(f'Workshop ID inválido: {mid}')
    return enabled, issues

def validate():
    """Analizar duplicados, asociaciones Workshop y presencia del INI."""
    items = mods()
    enabled, issues = mod_issues(items)
    ini = ini_path(settings())
    if not ini.exists():
        issues.append(f'No existe configuración inicial: {ini}. Iniciá el servidor una vez antes de aplicar.')
    return {'enabled':len(enabled),'total':len(items),'issues':issues,'ini':str(ini)}

def steam_workshop_dirs():
    """Ubicar las carpetas Workshop de PZ en las bibliotecas del cliente Steam, si está instalado."""
    if os.name != 'nt':
        return []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            steam = Path(winreg.QueryValueEx(key, 'SteamPath')[0])
    except OSError:
        return []
    libraries = [steam]
    try:
        vdf = (steam / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
        libraries += [Path(p.replace('\\\\', '\\')) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    return [lib / 'steamapps' / 'workshop' / 'content' / '108600' for lib in libraries]

def workshop_dirs(s):
    """Carpetas donde buscar descargas Workshop: servidor, Zomboid, Steam y rutas adicionales."""
    candidates = [Path(s['server_dir'])/'steamapps'/'workshop'/'content'/'108600',
                  Path(s['pz_home'])/'Workshop',
                  Path.home()/'Zomboid'/'mods']
    candidates += steam_workshop_dirs()
    # También admite rutas Steam Workshop adicionales configuradas por el usuario.
    candidates += [Path(p) for p in s.get('extra_workshop_paths',[]) if isinstance(p,str)]
    unique = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique

def scan_local():
    """Leer los mod.info locales: Mod ID -> Workshop IDs, sus dependencias y qué Workshop IDs están descargados."""
    mapping = {}
    requires = {}
    downloaded = set()
    scanned = []
    for base in workshop_dirs(settings()):
        if not base.is_dir():
            continue
        scanned.append(str(base))
        downloaded.update(p.name for p in base.iterdir() if p.is_dir() and p.name.isdigit())
        for info in base.glob('*/mods/**/mod.info'):
            if len(info.parts) > len(base.parts) + 9:
                continue
            relative = info.relative_to(base)
            wid = relative.parts[0]
            if not wid.isdigit():
                continue
            try:
                content = info.read_text(encoding='utf-8-sig', errors='replace')
            except OSError:
                continue
            match = re.search(r'^\s*id\s*=\s*(.*?)\s*$', content, re.M)
            if match:
                mapping.setdefault(match.group(1),set()).add(wid)
                # require= admite los dos formatos que usan los mods: "\ModA,\ModB" (B42) y "ModA,ModB".
                for line in re.findall(r'^\s*require\s*=\s*(.*?)\s*$', content, re.M):
                    deps = (r.strip().lstrip('\\').strip() for r in line.split(','))
                    requires.setdefault(match.group(1),set()).update(d for d in deps if d)
    return {'found':{k:sorted(v) for k,v in mapping.items()},'requires':{k:sorted(v) for k,v in requires.items()},
            'downloaded':sorted(downloaded),'scanned':scanned}

def locate_mod_ids():
    """Encontrar correspondencias Mod ID -> Workshop ID en instalaciones locales de Steam."""
    return scan_local()['found']

def autodetect():
    """Asociar automáticamente Workshop IDs cuando existe una coincidencia local inequívoca."""
    with LOCK:
        scan = scan_local()
        found = scan['found']
        items = mods()
        assigned = []
        ambiguous = []
        for m in items:
            ids = found.get(m['id'],[])
            if not m.get('workshop_id') and len(ids)==1:
                m['workshop_id'] = ids[0]
                assigned.append(m['id'])
            elif not m.get('workshop_id') and len(ids)>1:
                ambiguous.append({'id':m['id'],'candidates':ids})
        save_json(CATALOG,items)
        return {'assigned':assigned,'ambiguous':ambiguous,'scanned_paths':scan['scanned']}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Steam responde directo: una redirección se rechaza en vez de seguirla a otro sitio."""
    def redirect_request(self, *args, **kwargs):
        return None

def steam_post(method, params):
    """Consultar la API pública de Steam Workshop (sin clave), con TLS verificado y límites de tiempo y tamaño."""
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(STEAM_API + method + '/v1/', data=urlencode(params).encode(),
                                 headers={'User-Agent':'PZDedicatedManager'})
    try:
        with opener.open(req, timeout=15) as r:
            raw = r.read(STEAM_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f'Steam respondió con el error {e.code}. Probá de nuevo en unos minutos.')
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise ValueError(f'No se pudo conectar con Steam: {getattr(e, "reason", e)}')
    if len(raw) > STEAM_MAX_BYTES:
        raise ValueError('La respuesta de Steam es demasiado grande.')
    try:
        data = json.loads(raw)['response']
    except (ValueError, KeyError, TypeError):
        data = None
    if not isinstance(data, dict):
        raise ValueError('Steam devolvió una respuesta inválida.')
    return data

def id_params(ids):
    """Parámetros publishedfileids[n] que espera la API de Steam."""
    return {f'publishedfileids[{n}]': wid for n, wid in enumerate(ids)}

def to_int(value):
    """Número entero de un dato externo, o 0 si no lo es."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

def valid_mod_id(mid):
    """Mod ID aceptable para el INI (las mismas reglas que al guardar el catálogo)."""
    return bool(mid) and len(mid) <= 150 and not any(x in mid for x in ';\r\n\\')

def description_mod_ids(text):
    """Mod IDs que la descripción declara con "Mod ID: X", el formato habitual en el Workshop de PZ."""
    text = re.sub(r'\[/?[a-z0-9*]+(?:=[^\]\n]*)?\]', '', str(text)[:100_000], flags=re.I)
    found = []
    for line in re.findall(r'^\s*mod\s*ids?\s*:\s*(.+?)\s*$', text, re.I | re.M):
        for part in line.split(','):
            mid = part.strip().lstrip('\\').strip()
            if valid_mod_id(mid) and mid not in found:
                found.append(mid)
    return found[:20]

def workshop_item(it, appid):
    """Normalizar un ítem de Steam y explicar por qué el servidor no podría descargarlo."""
    result = it.get('result')
    if result == 9:
        problem = 'No existe o fue retirado de Workshop.'
    elif result != 1:
        problem = f'Steam no lo devuelve (código {result}).'
    elif to_int(it.get('consumer_app_id')) != appid:
        problem = 'No es un mod de Project Zomboid.'
    elif it.get('banned'):
        problem = 'Steam lo bloqueó.'
    elif it.get('visibility') in (1, 2):
        problem = 'No es público: el servidor no puede descargarlo.'
    else:
        problem = ''
    tags = [str(t.get('tag', ''))[:40] for t in it.get('tags') or [] if isinstance(t, dict)]
    return {'workshop_id':str(it.get('publishedfileid', '')),'problem':problem,'title':str(it.get('title') or '')[:200],
            'updated':to_int(it.get('time_updated')),'tags':tags[:15],
            'mod_ids':[] if problem else description_mod_ids(it.get('description', ''))}

def workshop_items(ids):
    """Datos públicos de Workshop por ID, en lotes de 100."""
    appid = to_int(settings().get('steam_workshop_appid')) or 108600
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        resp = steam_post('GetPublishedFileDetails', {'itemcount':len(chunk), **id_params(chunk)})
        for it in resp.get('publishedfiledetails') or []:
            if isinstance(it, dict) and str(it.get('publishedfileid', '')) in chunk:
                out[str(it['publishedfileid'])] = workshop_item(it, appid)
    return out

def expand_collections(ids):
    """Separar colecciones de mods sueltos, en orden; las subcolecciones se abren un nivel y las más profundas se ignoran."""
    items, collections, pending = [], {}, ids
    for depth in range(2):
        if not pending:
            break
        resp = steam_post('GetCollectionDetails', {'collectioncount':len(pending), **id_params(pending)})
        children = {str(c.get('publishedfileid', '')): c['children'] for c in resp.get('collectiondetails') or []
                    if isinstance(c, dict) and c.get('result') == 1 and isinstance(c.get('children'), list)}
        nested = []
        for wid in pending:
            if wid not in children:
                items.append(wid)
                continue
            kids = sorted((k for k in children[wid] if isinstance(k, dict)), key=lambda k: to_int(k.get('sortorder')))
            collections[wid] = len(kids)
            for kid in kids:
                kid_id = str(kid.get('publishedfileid', ''))
                if not (kid_id.isdigit() and len(kid_id) <= 20):
                    continue
                if kid.get('filetype') == 2:
                    if depth == 0:
                        nested.append(kid_id)
                else:
                    items.append(kid_id)
        pending = [x for x in dict.fromkeys(nested) if x not in collections]
    return collections, list(dict.fromkeys(items))

def workshop_lookup(data):
    """Resolver los mods y colecciones que pegó el usuario (URL o ID) para agregarlos al catálogo."""
    ids = []
    for token in re.split(r'[\s,;]+', str(data.get('text', ''))[:20_000]):
        wid = workshop_id(token)
        if wid and wid not in ids:
            ids.append(wid)
    if not ids:
        raise ValueError('Pegá la URL o el ID de un mod o de una colección de Steam Workshop.')
    if len(ids) > 100:
        raise ValueError('Pegá como máximo 100 enlaces por búsqueda.')
    collections, items = expand_collections(ids)
    truncated = len(items) > STEAM_MAX_ITEMS
    items = items[:STEAM_MAX_ITEMS]
    details = workshop_items(items + [c for c in collections if c not in items])
    missing = {'problem':'Steam no devolvió datos de este ítem.','title':'','updated':0,'tags':[],'mod_ids':[]}
    return {'items':[details.get(wid) or {'workshop_id':wid, **missing} for wid in items],
            'collections':[{'workshop_id':wid,'title':(details.get(wid) or {}).get('title', ''),'count':count}
                           for wid, count in collections.items()],
            'truncated':truncated}

def server_workshop_manifest(s):
    """Fecha de cada mod que bajó el servidor, leída de su appworkshop_<appid>.acf (None si todavía no bajó ninguno)."""
    appid = to_int(s.get('steam_workshop_appid')) or 108600
    acf = Path(s['server_dir']) / 'steamapps' / 'workshop' / f'appworkshop_{appid}.acf'
    try:
        text = acf.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    marker = text.find('"WorkshopItemsInstalled"')
    start = text.find('{', marker) if marker >= 0 else -1
    if start < 0:
        return {}
    depth = 0
    for end in range(start, len(text)):
        depth += {'{':1, '}':-1}.get(text[end], 0)
        if depth == 0:
            break
    block = text[start:end + 1]
    return {wid: to_int(t) for wid, t in re.findall(r'"(\d+)"\s*\{[^{}]*?"timeupdated"\s*"(\d+)"', block)}

def workshop_updates(data=None):
    """Comparar la fecha de cada mod del catálogo guardado en Steam con la de la copia que bajó el servidor."""
    s = settings()
    ids = list(dict.fromkeys(str(m.get('workshop_id', '')) for m in mods() if str(m.get('workshop_id', '')).isdigit()))
    if not ids:
        raise ValueError('El catálogo no tiene Workshop IDs para revisar.')
    remote = workshop_items(ids[:STEAM_MAX_ITEMS])
    local = server_workshop_manifest(s)
    items = {}
    for wid in ids:
        r = remote.get(wid) or {'problem':'Steam no devolvió datos de este ítem.','title':'','updated':0}
        here = (local or {}).get(wid)
        if r['problem']:
            state = 'missing'
        elif here is None:
            state = 'not_downloaded'
        else:
            state = 'outdated' if r['updated'] > here else 'ok'
        items[wid] = {'state':state,'title':r['title'],'updated':r['updated'],'local':here,'problem':r['problem']}
    result = {'checked':time.time(),'downloaded':local is not None,'items':items,
              'outdated':[w for w, x in items.items() if x['state'] == 'outdated'],
              'missing':[w for w, x in items.items() if x['state'] == 'missing']}
    WS_CHECK.clear()
    WS_CHECK.update(result)
    return result

def read_ini(path):
    """Leer las claves del INI junto con la descripción (# comentarios) que las precede."""
    entries = []
    seen = set()
    comments = []
    for line in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        text = line.strip()
        if not text:
            comments = []
            continue
        if text.startswith('#'):
            comments.append(text[1:].strip())
            continue
        key, sep, value = line.partition('=')
        key = key.strip()
        if sep and key and key not in seen:
            seen.add(key)
            entries.append({'key':key,'value':value,'help':' '.join(comments)})
        comments = []
    return entries

def write_ini_keys(ini, replacements, append_missing=False):
    """Crear copia de seguridad y reemplazar solo las claves indicadas, conservando el resto del INI."""
    original = ini.read_text(encoding='utf-8-sig')
    backup = ini.with_name(ini.name + '.' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    shutil.copy2(ini,backup)
    lines = original.splitlines()
    for key,value in replacements.items():
        matches = [i for i,line in enumerate(lines) if re.match(r'^\s*'+re.escape(key)+r'\s*=',line)]
        if matches:
            lines[matches[0]] = key + '=' + value
            for i in reversed(matches[1:]):
                del lines[i]
        elif append_missing:
            lines.append(key + '=' + value)
    ini.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return backup

def apply_config():
    """Crear copia de seguridad y actualizar exclusivamente Mods y WorkshopItems en INI existente."""
    with LOCK:
        result = validate()
        s = settings()
        if s.get('strict') and result['issues']:
            raise ValueError('Validación estricta: ' + '; '.join(result['issues'][:8]) + (' ...' if len(result['issues'])>8 else ''))
        ini = Path(result['ini'])
        if not ini.is_file():
            raise ValueError('No existe el INI; iniciá el servidor para crearlo.')
        active = [m for m in mods() if m.get('enabled')]
        # En B42, la forma habitual para Mods es \ModId; el servidor debe validarlo en sus logs.
        modline = ';'.join('\\'+m['id'] for m in active)
        ids = list(dict.fromkeys(str(m.get('workshop_id','')) for m in active if m.get('workshop_id')))
        backup = write_ini_keys(ini, {'Mods':modline,'WorkshopItems':';'.join(ids)}, append_missing=True)
        return {'backup':str(backup),'ini':str(ini),'workshop_count':len(ids),'mod_count':len(active),'warnings':result['issues']}

def ini_state():
    """Comparar Mods/WorkshopItems del INI con los mods activos del catálogo (orden incluido)."""
    ini = ini_path(settings())
    active = [m for m in mods() if m.get('enabled')]
    want_mods = [str(m['id']) for m in active]
    want_ws = list(dict.fromkeys(str(m.get('workshop_id','')) for m in active if m.get('workshop_id')))
    state = {'ini':str(ini),'exists':ini.is_file(),'in_sync':False,'want_mods':len(want_mods),'want_workshop':len(want_ws)}
    if not state['exists']:
        return state
    values = {e['key']:e['value'] for e in read_ini(ini)}
    have_mods = [x.strip().lstrip('\\') for x in values.get('Mods','').split(';') if x.strip()]
    have_ws = [x.strip() for x in values.get('WorkshopItems','').split(';') if x.strip()]
    state.update(
        ini_mods=len(have_mods), ini_workshop=len(have_ws),
        missing=[m for m in want_mods if m not in have_mods], extra=[m for m in have_mods if m not in want_mods],
        workshop_missing=[w for w in want_ws if w not in have_ws], workshop_extra=[w for w in have_ws if w not in want_ws],
        order_differs=set(have_mods)==set(want_mods) and have_mods!=want_mods,
        in_sync=have_mods==want_mods and have_ws==want_ws)
    return state

def ini_entries():
    """Ajustes del INI del servidor para el editor del panel."""
    ini = ini_path(settings())
    return {'ini':str(ini),'exists':ini.is_file(),'entries':read_ini(ini) if ini.is_file() else [],'protected':sorted(PROTECTED_INI_KEYS)}

def update_ini(changes):
    """Guardar en el INI los valores editados en el panel (solo claves existentes, con copia de seguridad)."""
    if not isinstance(changes, dict) or not changes or len(changes) > 500:
        raise ValueError('No hay cambios válidos para guardar.')
    ini = ini_path(settings())
    if not ini.is_file():
        raise ValueError('No existe el INI; iniciá el servidor una vez para crearlo.')
    known = {e['key'] for e in read_ini(ini)}
    clean = {}
    for key, value in changes.items():
        key, value = str(key), str(value)
        if key in PROTECTED_INI_KEYS:
            raise ValueError(f'{key} se administra desde la pestaña Mods.')
        if key not in known:
            raise ValueError(f'La clave {key} no existe en el INI.')
        if '\n' in value or '\r' in value or len(value) > 10000:
            raise ValueError(f'Valor inválido para {key}.')
        clean[key] = value
    with LOCK:
        backup = write_ini_keys(ini, clean)
    return {'backup':str(backup),'changed':sorted(clean)}

def excluded_mods():
    """Mods apartados por no tener Workshop ID (para poder recuperarlos desde el panel)."""
    try:
        data = read_json(EXCLUDED)
    except (OSError, ValueError):
        return []
    return [m for m in data if isinstance(m, dict) and m.get('id')] if isinstance(data, list) else []

def bat_memory(s):
    """RAM (GB) que asigna StartServer64.bat con -Xmx, o None si no se puede leer."""
    try:
        match = re.search(rb'-Xmx(\d+)([gGmM])', (Path(s['server_dir']) / 'StartServer64.bat').read_bytes())
    except OSError:
        return None
    if not match:
        return None
    value = int(match.group(1))
    return value if match.group(2) in b'gG' else round(value / 1024, 1)

def server_script(s):
    """Generar StartServer64_panel.bat: copia del original sin PAUSE (para detectar el cierre) y con la RAM elegida."""
    src = Path(s['server_dir']) / 'StartServer64.bat'
    if not src.is_file():
        raise ValueError(f'No se encontró {src}. Instalá el servidor primero.')
    lines = [line for line in src.read_bytes().splitlines() if not re.fullmatch(rb'\s*@?pause\s*', line, re.I)]
    data = b'\r\n'.join(lines) + b'\r\n'
    ram = int(s.get('ram_gb') or 0)
    if ram:
        data = re.sub(rb'-Xms\d+[kKmMgG]?', b'-Xms%dg' % ram, data)
        data = re.sub(rb'-Xmx\d+[kKmMgG]?', b'-Xmx%dg' % ram, data)
    dst = src.with_name('StartServer64_panel.bat')
    dst.write_bytes(data)
    return dst

def log_file(label):
    """Generar ruta para el registro de una operación."""
    return LOGS / (datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-'+label+'.log')

def external_server(fresh=False):
    """PID de un servidor PZ iniciado fuera del panel (java.exe con zombie.network.GameServer), o None."""
    if os.name != 'nt':
        return None
    now = time.monotonic()
    if not fresh and now - EXTERNAL['checked'] < 10:
        return EXTERNAL['pid']
    pid = None
    try:
        tasks = subprocess.run(['tasklist','/FI','IMAGENAME eq java.exe','/FO','CSV','/NH'],capture_output=True,text=True,errors='replace',timeout=15,creationflags=NO_WINDOW).stdout
        if 'java.exe' in tasks.lower():
            query = ("Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
                     "Where-Object { $_.CommandLine -like '*zombie.network.GameServer*' } | "
                     "Select-Object -First 1 -ExpandProperty ProcessId")
            out = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,errors='replace',timeout=30,creationflags=NO_WINDOW).stdout.strip()
            pid = int(out) if out.isdigit() else None
    except (OSError, subprocess.SubprocessError):
        pid = None
    EXTERNAL.update(checked=now, pid=pid)
    return pid

def launch(kind):
    """Iniciar un proceso controlado y redirigir salida a un archivo de log."""
    global PROCESS
    with LOCK:
        if PROCESS and PROCESS.poll() is None:
            raise ValueError('Ya existe un proceso iniciado desde este panel.')
        other = external_server(fresh=True)
        if other:
            raise ValueError(f'Hay un servidor de Project Zomboid abierto fuera del panel (PID {other}). Cerralo antes de continuar.')
        s = settings()
        stdin = subprocess.DEVNULL
        if kind == 'server':
            exe = server_script(s)
            cmd = ['cmd.exe','/d','/c',str(exe),'-servername',safe_name(s['server_name'])]
            cwd = exe.parent
            # La entrada queda abierta para enviar comandos de consola (save, quit, contraseña admin...).
            stdin = subprocess.PIPE
        elif kind == 'update':
            exe = Path(s['steamcmd_path'])
            if not exe.is_file():
                raise ValueError('No existe steamcmd.exe en la ruta configurada.')
            cmd = [str(exe),'+force_install_dir',str(Path(s['server_dir']).resolve()),'+login','anonymous',
                   '+app_update',str(s['server_appid'])]
            branch = s.get('branch','').strip()
            if branch:
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,50}',branch):
                    raise ValueError('Rama Steam inválida.')
                cmd += ['-beta',branch]
            cmd += ['validate','+quit']
            cwd = exe.parent
        else:
            raise ValueError('Operación desconocida.')
        log = log_file(kind)
        f = log.open('w',encoding='utf-8',errors='replace')
        try:
            # En la ventana propia no hay consola: NO_WINDOW evita que aparezcan ventanas negras de cmd/java.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | (NO_WINDOW if APP['active'] else 0) if os.name=='nt' else 0
            PROCESS = subprocess.Popen(cmd,cwd=str(cwd),stdin=stdin,stdout=f,stderr=subprocess.STDOUT,creationflags=flags)
        finally:
            f.close()
        PROC_INFO.clear()
        PROC_INFO.update(kind=kind, started=time.time(), log=log.name, stdin=stdin == subprocess.PIPE)
        return {'pid':PROCESS.pid,'log':str(log),'operation':kind}

def send_command(text):
    """Escribir un comando en la consola del servidor iniciado desde el panel."""
    text = str(text or '').strip()
    if not text or len(text) > 500 or '\n' in text or '\r' in text:
        raise ValueError('Comando inválido.')
    with LOCK:
        running = PROCESS and PROCESS.poll() is None
        if not (running and PROC_INFO.get('kind') == 'server' and PROC_INFO.get('stdin')):
            raise ValueError('No hay un servidor iniciado desde este panel que acepte comandos.')
        try:
            PROCESS.stdin.write((text + '\n').encode('utf-8'))
            PROCESS.stdin.flush()
        except OSError:
            raise ValueError('El servidor ya no acepta comandos.')
        if text.lower() == 'quit':
            PROC_INFO['stopping'] = True
    return {'sent':text}

def kill_tree(pid):
    """Terminar un proceso y sus hijos (cmd.exe -> java.exe)."""
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,timeout=30,creationflags=NO_WINDOW)
    elif PROCESS and PROCESS.pid == pid:
        PROCESS.kill()

def stop(force=False):
    """Detener: 'quit' guarda el mundo y cierra; forzar termina el proceso sin guardar."""
    # Detener a mano gana sobre un reinicio programado: no se vuelve a iniciar.
    cancel_restart()
    with LOCK:
        if PROCESS and PROCESS.poll() is None:
            if not force and PROC_INFO.get('kind') == 'server' and PROC_INFO.get('stdin'):
                send_command('quit')
                return {'stopping':True,'message':'Se envió "quit": el servidor guarda el mundo y se cierra.'}
            kill_tree(PROCESS.pid)
            return {'killed':PROCESS.pid,'message':'Proceso terminado.'}
        pid = external_server(fresh=True)
        if pid and force:
            kill_tree(pid)
            EXTERNAL['checked'] = 0
            return {'killed':pid,'message':f'Servidor externo (PID {pid}) terminado.'}
        if pid:
            raise ValueError('El servidor se inició fuera del panel: solo se puede forzar el cierre.')
        raise ValueError('No hay ningún proceso en ejecución.')

def inspect_log():
    """Detectar en el log del proceso actual si el servidor arrancó, si pide la contraseña admin o si SteamCMD terminó bien."""
    name = PROC_INFO.get('log')
    if not name:
        return
    with INSPECT_LOCK:
        try:
            with (LOGS / name).open('rb') as f:
                f.seek(max(0, PROC_INFO.get('scanned', 0) - 64))
                chunk = f.read()
                PROC_INFO['scanned'] = f.tell()
        except OSError:
            return
        if b'*** SERVER STARTED' in chunk:
            PROC_INFO['ready'] = True
        if b'Success! App' in chunk:
            PROC_INFO['update_ok'] = True
        tail = chunk[-200:]
        PROC_INFO['prompt'] = b'administrator password' in tail or b'Confirm the password' in tail

def status():
    """Informar estado del último proceso iniciado (y de un servidor externo si lo hay)."""
    running = bool(PROCESS and PROCESS.poll() is None)
    inspect_log()
    return {'running':running,'pid':PROCESS.pid if PROCESS else None,'returncode':PROCESS.poll() if PROCESS else None,
            'kind':PROC_INFO.get('kind'),'started':PROC_INFO.get('started'),'log':PROC_INFO.get('log'),
            'ready':PROC_INFO.get('ready',False),'prompt':running and PROC_INFO.get('prompt',False),
            'stopping':running and PROC_INFO.get('stopping',False),'update_ok':PROC_INFO.get('update_ok',False),
            'can_command':running and PROC_INFO.get('kind')=='server' and PROC_INFO.get('stdin',False),
            'external':None if running else external_server(),'restart':restart_status()}

def overview():
    """Resumen para la pantalla de inicio: estado, pasos de preparación y sincronización de mods."""
    s = settings()
    items = mods()
    enabled, issues = mod_issues(items)
    sync = ini_state()
    checks = [
        {'id':'steamcmd','ok':Path(s['steamcmd_path']).is_file(),'title':'SteamCMD instalado',
         'hint':'Ejecutá instalar.bat para descargar SteamCMD y el servidor.'},
        {'id':'server','ok':(Path(s['server_dir'])/'StartServer64.bat').is_file(),'title':'Servidor dedicado instalado',
         'hint':'Descargalo con SteamCMD (AppID 380870).','action':'update'},
        {'id':'ini','ok':sync['exists'],'title':f'Archivo {s["server_name"]}.ini creado',
         'hint':'Iniciá el servidor una vez (sin mods) para que lo genere. Si pide contraseña de admin, escribila en la Consola.','action':'start'},
        {'id':'mods','ok':not issues,'title':'Mods activos sin advertencias',
         'hint':f'{len(issues)} advertencia(s) en el catálogo.','tab':'mods'},
        {'id':'sync','ok':sync['in_sync'],'title':'Mods activos aplicados al INI',
         'hint':'El INI no coincide con los mods activos del catálogo.','action':'apply'},
    ]
    return {'server_name':s['server_name'],'checks':checks,'sync':sync,'status':status(),
            'mods':{'enabled':len(enabled),'total':len(items),'issues':issues},
            'paths':{k:Path(s[k]).exists() for k in ('server_dir','steamcmd_path','pz_home')},
            'memory':bat_memory(s),'ram_gb':s.get('ram_gb',0),'workshop':dict(WS_CHECK),
            'is_admin':is_admin(),'mode':'app' if APP['active'] else 'web'}

def latest_logs():
    """Listar los últimos registros sin permitir rutas proporcionadas por el navegador."""
    files = sorted(LOGS.glob('*.log'),key=lambda p:p.stat().st_mtime,reverse=True)[:30]
    return [{'name':p.name,'size':p.stat().st_size,'mtime':p.stat().st_mtime} for p in files]

def log_source(name):
    """Resolver un log por nombre (solo archivos de logs/ o server-console.txt de Zomboid)."""
    if name == 'pz-console':
        return Path(settings()['pz_home']) / 'server-console.txt'
    if not re.fullmatch(r'[\w.-]{1,120}\.log', name):
        raise ValueError('Nombre de log inválido')
    return LOGS / name

def read_log(name, offset=None):
    """Leer un log desde offset (o su final) para mostrarlo en vivo en la consola."""
    path = log_source(name)
    if not path.is_file():
        return {'text':'','offset':0,'reset':True,'missing':True}
    size = path.stat().st_size
    reset = offset is None or offset > size or size - offset > 1_000_000
    start = max(0, size - 200_000) if reset else offset
    with path.open('rb') as f:
        f.seek(start)
        data = f.read(size - start)
    return {'text':data.decode('utf-8',errors='replace'),'offset':start+len(data),'reset':reset,'truncated':start>0 and reset}

def open_folder(target):
    """Abrir en el Explorador una de las carpetas conocidas del servidor."""
    s = settings()
    folders = {'server':Path(s['server_dir']),'zomboid':Path(s['pz_home']),'ini':Path(s['pz_home'])/'Server','logs':LOGS,'root':ROOT}
    if target not in folders:
        raise ValueError('Carpeta desconocida.')
    path = folders[target]
    if not path.is_dir():
        raise ValueError(f'No existe la carpeta {path}')
    if os.name != 'nt':
        raise ValueError('Solo disponible en Windows.')
    os.startfile(str(path))
    return {'opened':str(path)}

def is_admin():
    """True si el panel corre con privilegios de administrador (no hace falta y conviene evitarlo)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False

def server_running():
    """Hay un servidor iniciado desde el panel que acepta comandos."""
    return bool(PROCESS and PROCESS.poll() is None and PROC_INFO.get('kind') == 'server')

def ini_values():
    """Valores actuales del INI del servidor ({} si todavía no existe)."""
    ini = ini_path(settings())
    return {e['key']:e['value'] for e in read_ini(ini)} if ini.is_file() else {}

def quoted(value, label):
    """Argumento entre comillas para la consola del servidor; rechaza comillas y saltos de línea."""
    value = str(value or '').strip()
    if not value or len(value) > 100 or any(c in value for c in '"\r\n'):
        raise ValueError(f'{label}: no puede estar vacío ni tener comillas.')
    return '"' + value + '"'

def players():
    """Pedir 'players' a la consola del servidor y leer la respuesta en el log."""
    if not server_running():
        return {'running':False,'count':0,'players':[]}
    path = LOGS / PROC_INFO['log']
    start = path.stat().st_size
    send_command('players')
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        time.sleep(0.3)
        with path.open('rb') as f:
            f.seek(start)
            text = f.read().decode('utf-8', errors='replace')
        match = re.search(r'Players connected \((\d+)\):(.*)', text, re.S)
        if not match:
            continue
        count = int(match.group(1))
        lines = [line.strip() for line in match.group(2).replace('<LINE>', '\n').splitlines()]
        names = [line[1:].strip() for line in lines if line.startswith('-')][:count]
        if len(names) >= count or time.monotonic() > deadline - 2:
            return {'running':True,'count':count,'players':names,'checked':time.time()}
    raise ValueError('El servidor no respondió al comando players (puede estar cargando todavía).')

def player_action(data):
    """Expulsar, banear, cambiar rol o editar la whitelist con comandos de consola del servidor."""
    if not server_running():
        raise ValueError('Iniciá el servidor desde el panel para gestionar jugadores.')
    action = str(data.get('action',''))
    user = quoted(data.get('username'), 'Usuario')
    reason = re.sub(r'[\r\n"]', ' ', str(data.get('reason') or '')).strip()[:200]
    suffix = f' -r "{reason}"' if reason else ''
    if action == 'kick':
        cmd = f'kickuser {user}{suffix}'
    elif action == 'ban':
        cmd = f'banuser {user}' + (' -ip' if data.get('ip') else '') + suffix
    elif action == 'unban':
        cmd = f'unbanuser {user}'
    elif action == 'role':
        role = str(data.get('role',''))
        if role not in ROLES:
            raise ValueError('Rol inválido.')
        cmd = f'setaccesslevel {user} {role}'
    elif action == 'whitelist_add':
        cmd = f'adduser {user} {quoted(data.get("password"), "Contraseña")}'
    elif action == 'whitelist_connected':
        cmd = f'addusertowhitelist {user}'
    elif action == 'whitelist_remove':
        cmd = f'removeuserfromwhitelist {user}'
    else:
        raise ValueError('Acción desconocida.')
    send_command(cmd)
    # Solo se devuelve el comando base: la contraseña de adduser no vuelve a la interfaz.
    return {'sent':cmd.split(' ', 1)[0]}

def whitelist():
    """Cuentas, roles y baneos de la base del servidor, en solo lectura y sin contraseñas."""
    s = settings()
    db = Path(s['pz_home']) / 'db' / (safe_name(s['server_name']) + '.db')
    values = ini_values()
    result = {'db':str(db),'exists':db.is_file(),'running':server_running(),'users':[],'banned_ips':[],'banned_ids':[],
              'access':{'Open':values.get('Open'),'AutoCreateUserInWhiteList':values.get('AutoCreateUserInWhiteList'),
                        'Public':values.get('Public'),'MaxPlayers':values.get('MaxPlayers'),'password':bool(values.get('Password'))}}
    if not db.is_file():
        return result
    truthy = lambda v: str(v).lower() in ('1', 'true')
    try:
        con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=3)
    except sqlite3.Error as e:
        raise ValueError(f'No se pudo abrir {db.name}: {e}')
    try:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        roles = dict(con.execute('select id, name from role').fetchall()) if 'role' in tables else {}
        for row in con.execute('select * from whitelist'):
            keys = row.keys()
            if 'role' in keys:
                role = roles.get(row['role'], str(row['role']))
            elif 'accesslevel' in keys and row['accesslevel']:
                role = row['accesslevel']
            else:
                role = 'admin' if 'admin' in keys and truthy(row['admin']) else 'moderator' if 'moderator' in keys and truthy(row['moderator']) else 'user'
            result['users'].append({'username':row['username'],'role':role,'last':row['lastConnection'],
                                    'steamid':row['steamid'] if 'steamid' in keys else None,
                                    'banned':role == 'banned' or ('banned' in keys and truthy(row['banned']))})
        if 'bannedip' in tables:
            result['banned_ips'] = [dict(r) for r in con.execute('select ip, username, reason from bannedip')]
        if 'bannedid' in tables:
            result['banned_ids'] = [dict(r) for r in con.execute('select steamid, reason from bannedid')]
    except sqlite3.Error as e:
        raise ValueError(f'No se pudo leer {db.name}: {e}')
    finally:
        con.close()
    result['users'].sort(key=lambda u: (u['username'] or '').lower())
    return result

def server_ports(values):
    """Puertos que usa el servidor según el INI (RCON solo cuenta si tiene contraseña)."""
    ports = []
    def add(key, proto, use, default=None):
        raw = str(values.get(key, default) or '').strip()
        if raw.isdigit() and int(raw) > 0:
            ports.append({'key':key,'port':int(raw),'proto':proto,'use':use})
    add('DefaultPort', 'UDP', 'Conexión de jugadores (puerto principal)', '16261')
    add('UDPPort', 'UDP', 'Tráfico de juego')
    add('SteamPort1', 'UDP', 'Steam')
    add('SteamPort2', 'UDP', 'Steam')
    if values.get('RCONPassword'):
        add('RCONPort', 'TCP', 'RCON (administración remota)', '27015')
    return ports

def listening_ports():
    """Puertos locales ocupados: {('UDP', 16261): pid} según netstat."""
    used = {}
    for proto in ('UDP', 'TCP'):
        try:
            out = subprocess.run(['netstat','-ano','-p',proto],capture_output=True,text=True,errors='replace',timeout=20,creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0] != proto:
                continue
            # En TCP solo interesan los puertos en escucha (dirección remota 0.0.0.0:0); el estado viene traducido.
            if proto == 'TCP' and parts[2] not in ('0.0.0.0:0', '[::]:0'):
                continue
            port, pid = parts[1].rsplit(':', 1)[-1], parts[-1]
            if port.isdigit() and pid.isdigit():
                used.setdefault((proto, int(port)), int(pid))
    return used

def process_names(pids):
    """Nombre del ejecutable de cada PID (para mostrar quién ocupa un puerto)."""
    names = {}
    for pid in pids:
        try:
            out = subprocess.run(['tasklist','/FI',f'PID eq {int(pid)}','/FO','CSV','/NH'],capture_output=True,text=True,errors='replace',timeout=15,creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.match(r'"([^"]+)"', out.strip())
        if match:
            names[pid] = match.group(1)
    return names

def ps_str(value):
    """Literal de PowerShell entre comillas simples (escapa comillas para evitar inyección)."""
    return "'" + str(value).replace("'", "''") + "'"

def server_java(s):
    """java.exe del servidor dedicado: las reglas de firewall se limitan a este programa."""
    return Path(s['server_dir']) / 'jre64' / 'bin' / 'java.exe'

def firewall_query():
    """Reglas del panel, otras reglas para el java.exe del servidor y categoría de la red actual."""
    java = server_java(settings())
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8\n$ErrorActionPreference='SilentlyContinue'\n"
              f"$g={ps_str(FW_GROUP)}\n$java={ps_str(java)}\n"
              "function Info($r){ $p=$r|Get-NetFirewallPortFilter; $a=$r|Get-NetFirewallApplicationFilter; $ad=$r|Get-NetFirewallAddressFilter\n"
              " [pscustomobject]@{name=[string]$r.DisplayName; enabled=[string]$r.Enabled; action=[string]$r.Action; direction=[string]$r.Direction;"
              " profile=[string]$r.Profile; protocol=[string]$p.Protocol; ports=[string]($p.LocalPort -join ','); program=[string]$a.Program; remote=[string]($ad.RemoteAddress -join ',')} }\n"
              "$own=@(Get-NetFirewallRule -Group $g | ForEach-Object { Info $_ })\n"
              "$other=@(Get-NetFirewallApplicationFilter -Program $java | Get-NetFirewallRule | Where-Object { $_.Group -ne $g } | ForEach-Object { Info $_ })\n"
              "$prof=@(Get-NetConnectionProfile | ForEach-Object { [pscustomobject]@{name=[string]$_.Name; category=[string]$_.NetworkCategory} })\n"
              "[pscustomobject]@{rules=$own; other=$other; profiles=$prof} | ConvertTo-Json -Depth 4 -Compress\n")
    try:
        out = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,timeout=60,creationflags=NO_WINDOW).stdout
        data = json.loads(out.decode('utf-8', errors='replace').strip() or '{}')
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'rules':[],'other':[],'profiles':[],'error':'No se pudo consultar el firewall de Windows.'}
    return {'rules':data.get('rules') or [],'other':data.get('other') or [],'profiles':data.get('profiles') or []}

def elevated_powershell(script):
    """Ejecutar un script de PowerShell como administrador: Windows muestra el aviso UAC."""
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    launcher = ("try { $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
                "-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','" + encoded + "'; exit $p.ExitCode } "
                "catch { exit 1223 }")
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',launcher],capture_output=True,timeout=600,creationflags=NO_WINDOW)
    if result.returncode == 1223:
        raise ValueError('Se canceló el permiso de administrador: el firewall no se modificó.')
    if result.returncode != 0:
        raise ValueError(f'Windows no pudo aplicar el cambio en el firewall (código {result.returncode}).')

def port_list(ports):
    """Puertos como lista de PowerShell (solo números)."""
    return ','.join(str(int(p)) for p in ports)

def firewall_change(data):
    """Crear/quitar las reglas del panel o deshabilitar reglas amplias para java.exe (requiere UAC)."""
    action = str(data.get('action',''))
    s = settings()
    java = server_java(s)
    group = ps_str(FW_GROUP)
    if action in ('lan', 'internet'):
        if not java.is_file():
            raise ValueError(f'No se encontró {java}. Instalá el servidor primero.')
        udp = sorted({p['port'] for p in server_ports(ini_values()) if p['proto'] == 'UDP'})
        if not udp:
            raise ValueError('No se encontraron puertos UDP en el INI.')
        category = {'Private':'Private','Public':'Public','DomainAuthenticated':'Domain'}
        profiles = sorted({category[p['category']] for p in firewall_query()['profiles'] if p.get('category') in category}) or ['Private']
        remote = 'LocalSubnet' if action == 'lan' else 'Any'
        body = (f"Remove-NetFirewallRule -Group {group} -ErrorAction SilentlyContinue\n"
                f"New-NetFirewallRule -DisplayName {ps_str(FW_GROUP + ' - UDP')} -Group {group} -Direction Inbound -Action Allow "
                f"-Protocol UDP -LocalPort {port_list(udp)} -Program {ps_str(java)} -Profile {','.join(profiles)} -RemoteAddress {remote} | Out-Null\n")
        result = {'ports':udp,'profiles':profiles,'remote':remote}
    elif action == 'remove':
        body = f"Remove-NetFirewallRule -Group {group} -ErrorAction SilentlyContinue\n"
        result = {'removed':True}
    elif action == 'disable_others':
        body = (f"Get-NetFirewallApplicationFilter -Program {ps_str(java)} | Get-NetFirewallRule | "
                f"Where-Object {{ $_.Group -ne {group} }} | Disable-NetFirewallRule\n")
        result = {'disabled':True}
    else:
        raise ValueError('Acción de firewall desconocida.')
    elevated_powershell("$ErrorActionPreference='Stop'\ntry {\n" + body + "exit 0\n} catch { exit 2 }\n")
    return result

def lan_ip():
    """IP de esta PC en la red local (no envía tráfico: solo elige la interfaz de salida)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            return sock.getsockname()[0]
    except OSError:
        return None

def port_in(port, spec):
    """True si el puerto está incluido en una lista de PowerShell como '16261,16262' o '16261-16270'."""
    for token in str(spec or '').split(','):
        token = token.strip()
        if token.lower() == 'any':
            return True
        low, _, high = token.partition('-')
        if low.isdigit() and (int(low) <= port <= int(high) if high.isdigit() else int(low) == port):
            return True
    return False

def network_info():
    """Puertos del servidor, quién los ocupa, reglas de firewall y datos para exponerlo."""
    s = settings()
    values = ini_values()
    ports = server_ports(values)
    used = listening_ports()
    fw = firewall_query()
    names = process_names({used[(p['proto'], p['port'])] for p in ports if (p['proto'], p['port']) in used})
    for p in ports:
        pid = used.get((p['proto'], p['port']))
        p['pid'], p['process'] = pid, names.get(pid)
        p['firewall'] = any(r.get('enabled') == 'True' and r.get('action') == 'Allow' and r.get('protocol') == p['proto']
                            and port_in(p['port'], r.get('ports')) for r in fw['rules'])
    java = server_java(s)
    return {'ports':ports,'rules':fw['rules'],'other':fw['other'],'profiles':fw['profiles'],'fw_error':fw.get('error'),
            'lan_ip':lan_ip(),'java':str(java),'java_exists':java.is_file(),'is_admin':is_admin(),
            'mode':'app' if APP['active'] else 'web','ini_exists':bool(values),
            'exposure':{'Public':values.get('Public'),'Open':values.get('Open'),'password':bool(values.get('Password')),
                        'UPnP':values.get('UPnP'),'rcon':bool(values.get('RCONPassword')),'RCONPort':values.get('RCONPort')}}

def save_mods(data):
    """Validar y guardar el catálogo enviado por la interfaz."""
    if not isinstance(data,list) or len(data)>5000: raise ValueError('Catálogo inválido')
    cleaned=[]
    for m in data:
        mid=str(m.get('id','')).strip()
        if not valid_mod_id(mid): raise ValueError('Mod ID inválido: '+mid)
        cleaned.append({'id':mid,'enabled':bool(m.get('enabled',False)),'workshop_id':workshop_id(m.get('workshop_id','')),'notes':str(m.get('notes',''))[:500]})
    if len(set(m['id'] for m in cleaned))!=len(cleaned): raise ValueError('Mod IDs duplicados')
    save_json(CATALOG,cleaned)
    return {'mods':cleaned}

def save_settings(data):
    """Validar y guardar la configuración del panel."""
    s=settings()
    for key in ('server_name','server_dir','steamcmd_path','pz_home','branch'):
        s[key]=str(data.get(key,s[key])).strip()
    safe_name(s['server_name'])
    if s['branch'] and not re.fullmatch(r'[A-Za-z0-9_-]{1,50}',s['branch']): raise ValueError('Rama inválida')
    s['strict']=bool(data.get('strict',True))
    try: ram=int(data.get('ram_gb',s.get('ram_gb',0)) or 0)
    except (TypeError,ValueError): raise ValueError('RAM inválida')
    if not 0<=ram<=256: raise ValueError('RAM inválida (0 a 256 GB).')
    s['ram_gb']=ram
    paths=data.get('extra_workshop_paths',[])
    if not isinstance(paths,list) or len(paths)>30: raise ValueError('Rutas adicionales inválidas')
    s['extra_workshop_paths']=[str(x) for x in paths]
    save_json(SETTINGS,s)
    return s

def start_server(data):
    """Iniciar el servidor respetando el modo estricto."""
    s=settings()
    # Sin INI todavía (primer arranque) no se bloquea: es justamente lo que lo crea.
    if s.get('strict') and ini_path(s).is_file() and mod_issues()[1]:
        raise ValueError('Validación estricta fallida: resolvé las advertencias de mods antes de iniciar.')
    return launch('server')

def restart_status():
    """Estado del reinicio programado, o el error del último, para la interfaz."""
    if RESTART['active']:
        return {'phase':RESTART['phase'],'at':RESTART['at']}
    return {'error':RESTART['error']} if RESTART['error'] else None

def cancel_restart():
    """Anular un reinicio programado: el servidor no se vuelve a iniciar."""
    if RESTART['cancel']:
        RESTART['cancel'].set()

def restart_server(data):
    """Programar un reinicio avisando a los jugadores: servermsg, save, quit y volver a iniciar."""
    minutes = to_int(data.get('minutes')) if str(data.get('minutes', '')).isdigit() else -1
    if minutes not in (0, 5, 10, 15):
        raise ValueError('Elegí reiniciar ahora o en 5, 10 o 15 minutos.')
    with LOCK:
        if RESTART['active']:
            raise ValueError('Ya hay un reinicio programado.')
        if not (server_running() and PROC_INFO.get('stdin')):
            raise ValueError('El reinicio funciona solo con un servidor iniciado desde la app.')
        cancel = threading.Event()
        RESTART.update(active=True, phase='countdown', at=time.time() + minutes * 60, error='', cancel=cancel)
    threading.Thread(target=restart_worker, args=(minutes, cancel), daemon=True).start()
    return restart_status()

def restart_worker(minutes, cancel):
    """Cuenta regresiva con avisos; después guarda, cierra y vuelve a iniciar el servidor."""
    try:
        for left in RESTART_NOTICES:
            if left > minutes:
                continue
            if cancel.wait(max(0, RESTART['at'] - left * 60 - time.time())):
                return
            if not server_running():
                raise ValueError('El servidor se detuvo antes del reinicio.')
            text = f'El servidor se reinicia en {left} minuto(s) para actualizar mods.' if left else 'Reiniciando el servidor ahora. Volvé a entrar en unos minutos.'
            send_command(f'servermsg "{text}"')
        RESTART['phase'] = 'stopping'
        proc = PROCESS
        send_command('save')
        if cancel.wait(5):
            return
        send_command('quit')
        try:
            proc.wait(300)
        except subprocess.TimeoutExpired:
            raise ValueError('El servidor no se cerró en 5 minutos. Revisá la Consola.')
        if cancel.is_set():
            return
        RESTART['phase'] = 'starting'
        with LOCK:
            start_server({})
    except ValueError as e:
        RESTART['error'] = str(e)
    finally:
        RESTART['active'] = False

def restart_cancel(data):
    """Cancelar el reinicio durante la cuenta regresiva y avisar a los jugadores."""
    if not RESTART['active']:
        raise ValueError('No hay un reinicio programado.')
    if RESTART['phase'] != 'countdown':
        raise ValueError('El servidor ya se está reiniciando.')
    cancel_restart()
    try:
        send_command('servermsg "Se canceló el reinicio del servidor."')
    except ValueError:
        pass
    return {'cancelled':True}

def ui_state(data):
    """La interfaz avisa si hay cambios sin guardar (para preguntar antes de cerrar la ventana)."""
    APP['dirty'] = bool(data.get('dirty'))
    return {}

def quit_app(data):
    """Cerrar la ventana propia; si el servidor corre, primero lo guarda y lo detiene."""
    window = APP['window']
    if not window:
        raise ValueError('Solo disponible en la ventana de la app.')
    cancel_restart()
    def worker():
        if server_running():
            try:
                send_command('quit')
                PROCESS.wait(300)
            except ValueError:
                pass
            except subprocess.TimeoutExpired:
                window.evaluate_js('closeFailed()')
                return
        elif PROCESS and PROCESS.poll() is None:
            kill_tree(PROCESS.pid)
        APP['allow_close'] = True
        window.destroy()
    threading.Thread(target=worker, daemon=True).start()
    return {'closing':True}

def export_mods(data):
    """Guardar el catálogo en un archivo elegido con el diálogo de Windows."""
    import webview
    if not APP['window']: raise ValueError('Solo disponible en la ventana de la app.')
    chosen = APP['window'].create_file_dialog(webview.FileDialog.SAVE, save_filename='pz-mods.json', file_types=('JSON (*.json)',))
    if not chosen:
        return {'cancelled':True}
    path = Path(chosen if isinstance(chosen, str) else chosen[0])
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    return {'path':str(path)}

def import_mods(data):
    """Leer una lista de mods desde un JSON elegido con el diálogo de Windows."""
    import webview
    if not APP['window']: raise ValueError('Solo disponible en la ventana de la app.')
    chosen = APP['window'].create_file_dialog(webview.FileDialog.OPEN, file_types=('JSON (*.json)',))
    if not chosen:
        return {'cancelled':True}
    try:
        items = json.loads(Path(chosen[0]).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise ValueError('El archivo no es un JSON válido.')
    if not isinstance(items, list):
        raise ValueError('El JSON debe ser una lista de mods.')
    return {'mods':items}

GET_ROUTES = {'mods':mods,'settings':settings,'status':status,'overview':overview,'logs':latest_logs,'ini':ini_entries,
              'excluded':excluded_mods,'scan':scan_local,'whitelist':whitelist,'network':network_info}
# (función, si se ejecuta bajo LOCK). Las que esperan al usuario o al servidor no bloquean al resto.
POST_ROUTES = {
    'mods':(save_mods,True),'settings':(save_settings,True),'validate':(lambda d:validate(),True),
    'detect':(lambda d:autodetect(),True),'apply':(lambda d:apply_config(),True),'update':(lambda d:launch('update'),True),
    'start':(start_server,True),'stop':(lambda d:stop(bool(d.get('force'))),True),
    'command':(lambda d:send_command(d.get('command','')),True),'ini':(lambda d:update_ini(d.get('changes')),True),
    'open':(lambda d:open_folder(str(d.get('target',''))),False),'players':(lambda d:players(),False),
    'player':(player_action,False),'firewall':(firewall_change,False),'ui_state':(ui_state,False),
    'quit_app':(quit_app,False),'export_mods':(export_mods,False),'import_mods':(import_mods,False),
    'ws_lookup':(workshop_lookup,False),'ws_updates':(workshop_updates,False),
    'restart':(restart_server,False),'restart_cancel':(restart_cancel,False),
}

def dispatch(method, path, data=None):
    """Ejecutar una acción de la interfaz; la usan la ventana propia y el modo navegador."""
    route, _, qs = path.partition('?')
    if method == 'GET':
        if route in ('tail', 'log'):
            query = parse_qs(qs)
            offset = query.get('offset', [None])[0]
            result = read_log(query.get('name', [''])[0], int(offset) if offset and offset.isdigit() else None)
            return result if route == 'tail' else {'text':result['text']}
        if route not in GET_ROUTES:
            raise NotFound('Ruta no encontrada')
        return GET_ROUTES[route]()
    if route not in POST_ROUTES:
        raise NotFound('Ruta no encontrada')
    func, locked = POST_ROUTES[route]
    data = {} if data is None else data
    if locked:
        with LOCK:
            return func(data)
    return func(data)

class Bridge:
    """Puente de la ventana propia: la página llama a Python directo, sin abrir ningún puerto."""
    def call(self, method, path, body=None):
        try:
            return {'ok':True,'data':dispatch(str(method).upper(), str(path), body)}
        except Exception as e:
            return {'ok':False,'error':str(e) or e.__class__.__name__}

PAGE = r'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="x-dns-prefetch-control" content="off">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'">
<title>PZ Dedicated Manager</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23a8332f'/%3E%3Ctext x='16' y='21' font-size='13' text-anchor='middle' fill='white' font-family='Segoe UI,Arial' font-weight='700'%3EPZ%3C/text%3E%3C/svg%3E">
<style>
:root{--bg:#0a0f17;--panel:#111926;--panel2:#162132;--hover:#1c293d;--line:#233248;--line2:#2f4260;--text:#e4e9f1;--muted:#8d9bb0;--dim:#5f6d82;--blue:#4c8dff;--ok:#38c98a;--warn:#f2b14c;--err:#f26b6b;--r:10px;color-scheme:dark;font:14px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text)}
a{color:var(--blue)}h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 4px}
h3{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:24px 0 10px}h3:first-child{margin-top:0}
p{margin:4px 0}.hint{color:var(--muted);font-size:13px}
.mono,code,.console{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace}
code{background:var(--panel2);padding:1px 6px;border-radius:5px;font-size:12.5px}
.top{position:sticky;top:0;z-index:30;height:58px;display:flex;align-items:center;gap:12px;padding:0 18px;background:rgba(10,15,23,.93);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;margin-right:auto;min-width:0}
.logo{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,#c0413c,#6e1b1b);display:grid;place-items:center;font-weight:800;font-size:13px;flex:none}
.brand b{display:block;font-size:15px}.brand small{display:block;font-size:12px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);font-size:13px;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.s-on .dot{background:var(--ok);animation:pulse 2s infinite}.s-busy .dot{background:var(--warn);animation:blink 1s infinite}
.s-warn .dot{background:var(--warn)}.s-err .dot{background:var(--err)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(56,201,138,.55)}70%,100%{box-shadow:0 0 0 7px rgba(56,201,138,0)}}
@keyframes blink{50%{opacity:.35}}
.uptime{color:var(--muted);font-variant-numeric:tabular-nums}
.layout{display:grid;grid-template-columns:210px minmax(0,1fr)}
nav{position:sticky;top:58px;height:calc(100vh - 58px);padding:14px 10px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:3px}
nav button{display:flex;align-items:center;justify-content:flex-start;gap:10px;width:100%;padding:9px 12px;border:0;border-radius:8px;background:none;color:var(--muted);font:inherit;text-align:left;cursor:pointer}
nav button:hover{background:var(--hover);color:var(--text)}
nav button.active{background:var(--panel2);color:var(--text);box-shadow:inset 3px 0 0 var(--blue)}
nav svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
nav .count{margin-left:auto}
.navfoot{margin-top:auto;padding:10px 12px;font-size:12px;color:var(--dim);line-height:1.6}
main{padding:22px 26px 110px;min-width:0;max-width:1340px}
.tab{display:none}.tab.active{display:block;animation:fade .15s}
@keyframes fade{from{opacity:.5}}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr));gap:16px;align-items:start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin-bottom:16px}
.grid2>.card{margin:0}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:12px}
button{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 13px;border-radius:8px;border:1px solid var(--line2);background:var(--panel2);color:var(--text);font:inherit;font-size:13.5px;cursor:pointer;white-space:nowrap;transition:background .12s,border-color .12s}
button:hover:not(:disabled){background:var(--hover);border-color:#3c5378}
button:disabled{opacity:.42;cursor:not-allowed}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button.primary:hover:not(:disabled){background:#619cff}
button.go{background:#1d8a5b;border-color:#1d8a5b;color:#fff}button.go:hover:not(:disabled){background:#23a46c}
button.stop{background:#a33f3a;border-color:#a33f3a;color:#fff}button.stop:hover:not(:disabled){background:#bb4a44}
button.danger{background:transparent;border-color:#6b2c2c;color:#ff9d9d}button.danger:hover:not(:disabled){background:#3a1a1d}
button.sm{padding:5px 10px;font-size:12.5px}
button.icon{padding:5px 8px;background:none;border-color:transparent;color:var(--muted)}
button.icon:hover:not(:disabled){color:var(--text);background:var(--hover)}
button.busy{position:relative;color:transparent!important}
button.busy::after{content:"";position:absolute;width:14px;height:14px;border:2px solid var(--text);border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
input,select,textarea{font:inherit;color:var(--text);background:#0c131e;border:1px solid var(--line2);border-radius:8px;padding:8px 10px;min-width:0}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(76,141,255,.18)}
textarea{resize:vertical;width:100%}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.grow{flex:1 1 240px}
.seg{display:inline-flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;background:transparent;color:var(--muted)}
.seg button+button{border-left:1px solid var(--line2)}
.seg button.on{background:var(--panel2);color:var(--text)}
.seg b{font-weight:600;color:var(--dim);font-size:12px}.seg button.on b{color:var(--muted)}
.chk{display:inline-flex;align-items:center;gap:8px;cursor:pointer;color:var(--muted);user-select:none}
.switch{position:relative;display:inline-block;width:38px;height:22px;flex:none}
.switch input{position:absolute;inset:0;opacity:0;width:100%;height:100%;margin:0;cursor:pointer;z-index:1}
.switch span{position:absolute;inset:0;border-radius:999px;background:#2a3850;transition:.15s}
.switch span::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#c8d1de;transition:.15s}
.switch input:checked+span{background:var(--ok)}.switch input:checked+span::after{transform:translateX(16px);background:#fff}
.switch input:focus-visible+span{outline:2px solid var(--blue);outline-offset:2px}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;font-size:12px;white-space:nowrap;border:1px solid transparent}
.b-ok{background:rgba(56,201,138,.12);color:#72e0ae;border-color:rgba(56,201,138,.25)}
.b-warn{background:rgba(242,177,76,.12);color:#f5c372;border-color:rgba(242,177,76,.26)}
.b-err{background:rgba(242,107,107,.12);color:#ff9696;border-color:rgba(242,107,107,.28)}
.b-muted{background:var(--panel2);color:var(--muted);border-color:var(--line)}
.count{min-width:20px;padding:0 6px;border-radius:999px;font-size:11.5px;font-weight:600;text-align:center;line-height:18px}
.count:empty{display:none}
.count.warn{background:rgba(242,177,76,.18);color:var(--warn)}.count.dirty{background:rgba(76,141,255,.22);color:#9cbfff}
.banner{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:10px 14px;border-radius:8px;margin-bottom:14px;border:1px solid}
.banner.warn{background:rgba(242,177,76,.08);border-color:rgba(242,177,76,.35);color:#f7d08f}
.banner.info{background:rgba(76,141,255,.08);border-color:rgba(76,141,255,.3);color:#bcd2ff}
.state{display:flex;gap:14px;align-items:center;margin-bottom:16px}
.state .dot{width:14px;height:14px}.state h2{font-size:20px;margin:0}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px;margin:14px 0 0}
.kv dt{color:var(--muted)}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.note{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.checks{list-style:none;margin:0;padding:0}
.checks li{display:flex;gap:12px;align-items:center;padding:10px 4px}
.checks li+li{border-top:1px solid var(--line)}
.ck{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;flex:none}
.checks .ok .ck{background:rgba(56,201,138,.15);color:var(--ok)}.checks .todo .ck{background:rgba(242,177,76,.15);color:var(--warn)}
.checks li>div{flex:1;min-width:0}.checks .ok b{font-weight:500;color:var(--muted)}
.progress{height:6px;border-radius:99px;background:var(--panel2);overflow:hidden;margin:4px 0 8px}
.progress i{display:block;height:100%;background:var(--ok);transition:width .3s}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:4px 0 12px}
.stats div{background:var(--panel2);border-radius:8px;padding:10px 12px}
.stats b{display:block;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}.stats span{color:var(--muted);font-size:12.5px}
.difflist{display:flex;flex-wrap:wrap;gap:4px;max-height:120px;overflow:auto;margin:6px 0 10px}
.tablewrap{overflow:auto;border:1px solid var(--line);border-radius:8px;max-height:calc(100vh - 340px);min-height:260px}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;z-index:2;background:var(--panel2);text-align:left;font-weight:600;font-size:12px;color:var(--muted);padding:8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:hover{background:rgba(255,255,255,.025)}
tr.off td:not(.tog){opacity:.5}
tr.lvl-err td:first-child{box-shadow:inset 3px 0 0 var(--err)}tr.lvl-warn td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
td input[type=text]{width:100%;padding:6px 8px;background:transparent;border-color:transparent}
td input[type=text]:hover{border-color:var(--line2)}td input[type=text]:focus{background:#0c131e;border-color:var(--blue)}
.handle{cursor:grab;color:var(--dim);user-select:none;width:24px;text-align:center}
.pos{width:56px;padding:4px 6px;text-align:right;background:transparent;border-color:transparent;color:var(--muted);font-variant-numeric:tabular-nums}
.pos:hover{border-color:var(--line2)}
tr.dragging{opacity:.35}
tr.drop-before td{box-shadow:inset 0 2px 0 var(--blue)}tr.drop-after td{box-shadow:inset 0 -2px 0 var(--blue)}
tr.flash{animation:flash 1.6s}@keyframes flash{0%{background:rgba(76,141,255,.28)}}
.widcell{display:flex;align-items:center;gap:2px}
.widcell a{color:var(--muted);text-decoration:none;padding:3px 6px;border-radius:5px;font-size:13px}
.widcell a:hover{color:var(--blue);background:var(--hover)}
.st{display:flex;gap:4px;flex-wrap:wrap}
table.mods{table-layout:fixed;min-width:980px}
table.mods th:nth-child(1){width:30px}table.mods th:nth-child(2){width:68px}table.mods th:nth-child(3){width:64px}
table.mods th:nth-child(4){width:26%}table.mods th:nth-child(5){width:150px}table.mods th:nth-child(6){width:150px}table.mods th:nth-child(8){width:44px}
.widcell input{flex:1}
.addrow{display:grid;grid-template-columns:minmax(160px,1fr) minmax(160px,1fr) auto;gap:8px;margin-bottom:14px}
.empty{text-align:center;color:var(--muted);padding:30px}
.exrow{display:grid;grid-template-columns:minmax(150px,230px) 1fr auto;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
details>summary{cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:8px}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"\25B8";color:var(--muted);transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}details[open]>summary{margin-bottom:10px}
.console{height:calc(100vh - 360px);min-height:280px;overflow:auto;background:#05080d;border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:12.5px;line-height:1.5;white-space:pre}
.console.wrap{white-space:pre-wrap;overflow-wrap:anywhere}
.ln.err{color:#ff8f8f}.ln.warn{color:#f5c372}.ln.ok{color:#72e0ae;font-weight:600}.ln.cmd{color:#9cbfff}.ln.dim{color:var(--dim);font-style:italic}
.cmdbar{display:flex;gap:8px;margin-top:10px;align-items:center}
.cmdbar .prompt{font-family:Consolas,monospace;color:var(--blue);font-weight:700}
.cmdbar input{flex:1;font-family:"Cascadia Mono",Consolas,monospace}
.quick{margin-top:10px}
.inigrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,330px),1fr));gap:10px}
.iniitem{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;min-width:0}
.iniitem.changed{border-color:var(--blue);box-shadow:inset 0 0 0 1px var(--blue)}
.inihead{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:24px}
.inihead label{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis}
.iniitem input[type=text],.iniitem input[type=password]{width:100%}
.help{font-size:12px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer;margin:0}
.help.open{display:block}
.secret{display:flex;gap:6px}.secret input{flex:1}
.form{display:grid;gap:16px;margin-top:12px}
.field>label{display:flex;justify-content:space-between;gap:8px;font-weight:600;font-size:13px;margin-bottom:6px}
.field input,.field textarea{width:100%}.field .hint{font-size:12px;margin-top:5px}
#toasts{position:fixed;right:18px;bottom:18px;z-index:60;display:flex;flex-direction:column;gap:8px;max-width:min(460px,calc(100vw - 36px))}
.toast{display:flex;align-items:flex-start;gap:10px;padding:11px 12px 11px 14px;border-radius:9px;background:#1a2538;border:1px solid var(--line2);border-left:4px solid var(--ok);box-shadow:0 10px 30px rgba(0,0,0,.45);animation:up .18s}
.toast.err{border-left-color:var(--err)}.toast.warn{border-left-color:var(--warn)}.toast.info{border-left-color:var(--blue)}
.toast span{flex:1;white-space:pre-wrap;overflow-wrap:anywhere}
.toast button{padding:2px 8px;font-size:12.5px}.toast .x{border:0;background:none;color:var(--muted);font-size:17px;line-height:1;padding:0 2px}
@keyframes up{from{transform:translateY(8px);opacity:0}}
.savebar{position:fixed;left:calc(50% + 105px);bottom:18px;transform:translateX(-50%);width:max-content;z-index:50;display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center;padding:10px 12px 10px 18px;border-radius:12px;background:#1a2538;border:1px solid #3c5378;box-shadow:0 12px 34px rgba(0,0,0,.5);animation:up .18s;max-width:calc(100vw - 32px)}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line2);border-radius:12px;padding:22px;width:min(500px,calc(100vw - 32px));box-shadow:0 20px 60px rgba(0,0,0,.6)}
dialog::backdrop{background:rgba(0,0,0,.55)}
dialog p{color:var(--muted);white-space:pre-wrap;margin-top:8px}
dialog input{width:100%;margin-top:12px}
dialog .actions{justify-content:flex-end;margin-top:20px}
dialog.wide{width:min(1000px,calc(100vw - 32px))}
.wsform{display:flex;gap:8px;margin-top:12px}.wsform input{flex:1;margin-top:0}
table.wsres{table-layout:fixed;min-width:760px}
table.wsres th:nth-child(1){width:36px}table.wsres th:nth-child(2){width:24%}table.wsres th:nth-child(4){width:150px}table.wsres th:nth-child(5){width:30%}
table.wsres td{padding:6px 8px}table.wsres input{margin-top:0}table.wsres input[type=checkbox],#wsAll{width:auto;margin:0}
.wstitle a{color:var(--text);text-decoration:none;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.wstitle a:hover{color:var(--blue)}
.wstitle small{display:block;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#restartBox{margin-top:12px}#restartInfo{margin:12px 0 0}
.tablewrap.short{max-height:440px;min-height:0}.tablewrap.auto{max-height:none;min-height:0}
table.users td{padding:7px 8px}table.users select{padding:4px 8px}
.fwbtns{margin:14px 0 8px}
.plrow{display:flex;align-items:center;gap:8px;padding:8px 2px;border-top:1px solid var(--line)}
.plrow:first-child{border-top:0}.plrow b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.plrow select{padding:4px 8px}
.setrow{display:flex;align-items:center;justify-content:space-between;gap:12px}
.setrow>div{min-width:0}.setrow b{display:block;font-weight:600;font-size:13px}
.overlay{position:fixed;inset:0;z-index:90;display:grid;place-items:center;background:rgba(5,8,13,.85);text-align:center}
.spinner{width:34px;height:34px;margin:0 auto 14px;border:3px solid var(--line2);border-top-color:var(--blue);border-radius:50%;animation:spin .8s linear infinite}
.topbanner{position:fixed;top:0;left:0;right:0;z-index:70;display:flex;gap:12px;align-items:center;justify-content:center;background:#a33f3a;color:#fff;padding:8px;font-weight:600}
@media(max-width:860px){
 .layout{grid-template-columns:1fr}
 nav{top:58px;z-index:20;height:auto;flex-direction:row;overflow-x:auto;border-right:0;border-bottom:1px solid var(--line);background:var(--bg);padding:8px}
 nav button{width:auto;white-space:nowrap}nav button.active{box-shadow:inset 0 -2px 0 var(--blue)}
 .navfoot,.uptime,.brand small{display:none}
 main{padding:16px 16px 130px}
 .addrow,.exrow{grid-template-columns:1fr}
 .console{height:55vh}.tablewrap{max-height:none}
 .savebar{left:50%}
}
</style></head><body>
<div id="offline" class="topbanner" hidden>Sin conexión con el panel: comprobá que la ventana de manager.py siga abierta.</div>
<div id="stale" class="topbanner" hidden>El panel se reinició y esta página quedó vieja. <button class="sm" id="reloadPage">Recargar</button></div>

<header class="top">
 <div class="brand"><div class="logo">PZ</div><div><b>Dedicated Manager</b><small>Servidor <span id="hdrName" class="mono">…</span></small></div></div>
 <div id="hdrPill" class="pill s-off"><span class="dot"></span><span id="hdrState">Cargando…</span><span id="hdrUptime" class="uptime"></span></div>
 <button id="hdrStart" class="go" data-act="start">&#9654; Iniciar</button>
 <button id="hdrStop" class="stop" data-act="stop" hidden>&#9632; Detener</button>
</header>

<div class="layout">
<nav>
 <button data-tab="home"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>Inicio</button>
 <button data-tab="mods"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Mods<span id="navMods" class="count"></span></button>
 <button data-tab="console"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/></svg>Consola</button>
 <button data-tab="players"><svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5"/></svg>Jugadores<span id="navPlayers" class="count dirty"></span></button>
 <button data-tab="network"><svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/></svg>Red y puertos<span id="navNet" class="count warn"></span></button>
 <button data-tab="ini"><svg viewBox="0 0 24 24"><path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg>Ajustes del servidor<span id="navIni" class="count dirty"></span></button>
 <button data-tab="settings"><svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Configuración</button>
 <div class="navfoot"><span id="navMode">Solo en 127.0.0.1</span><br>Ctrl+S guardar · Ctrl+K buscar</div>
</nav>
<main>

<section id="tab-home" class="tab">
 <div class="pagehead"><h1>Inicio</h1><div class="actions"><button data-open="server">Carpeta del servidor</button><button data-open="zomboid">Carpeta Zomboid</button><button data-open="logs">Logs</button></div></div>
 <div id="adminWarn" class="banner warn" hidden><span>La app se está ejecutando <b>como administrador</b>. No hace falta: cerrala y abrila con tu usuario normal para reducir riesgos.</span></div>
 <div id="homePrompt" class="banner warn" hidden><span>El servidor está esperando que crees la contraseña del usuario <b>admin</b>.</span><button class="sm" data-goto="console">Ir a la consola</button></div>
 <div class="grid2">
  <div class="card">
   <div class="state s-off" id="homeState"><span class="dot"></span><div><h2 id="stTitle">…</h2><p class="hint" id="stSub"></p></div></div>
   <div class="actions">
    <button class="go" data-act="start">&#9654; Iniciar servidor</button>
    <button class="stop" data-act="stop">&#9632; Detener y guardar</button>
    <button data-cmd="save">Guardar mundo</button>
    <button data-act="update">Actualizar servidor</button>
    <button class="danger" data-act="kill">Forzar cierre</button>
   </div>
   <dl class="kv" id="stInfo"></dl>
   <p class="hint note" id="closeNote">Si cerrás la ventana negra del panel, el servidor se cierra con ella. Para salir seguro usá <b>Detener y guardar</b> o Ctrl+C en esa ventana.</p>
  </div>
  <div class="card">
   <h2>Preparación</h2><p class="hint" id="checksSummary"></p>
   <div class="progress"><i id="checksBar"></i></div>
   <ul class="checks" id="checks"></ul>
  </div>
  <div class="card">
   <div class="cardhead"><div><h2>Mods</h2><p class="hint" id="syncText"></p></div><div class="actions"><button data-goto="mods">Gestionar</button><button class="primary" data-act="apply">Aplicar al INI</button></div></div>
   <div class="stats"><div><b id="kEnabled">–</b><span>activos</span></div><div><b id="kTotal">–</b><span>en catálogo</span></div><div><b id="kIssues">–</b><span>advertencias</span></div></div>
   <div id="syncDiff"></div>
  </div>
  <div class="card">
   <div class="cardhead"><div><h2>Actualizaciones de Workshop</h2><p class="hint" id="wsText"></p></div><div class="actions"><button data-act="wscheck">Buscar actualizaciones</button></div></div>
   <div id="wsDiff"></div>
   <div class="actions" id="restartBox"><select id="restartMin" aria-label="Cuándo reiniciar"><option value="0">Ahora</option><option value="5" selected>En 5 minutos</option><option value="10">En 10 minutos</option><option value="15">En 15 minutos</option></select><button class="primary" data-act="restart" id="btnRestart">Reiniciar para actualizar</button></div>
   <div class="banner info" id="restartInfo" hidden><span id="restartText"></span><button class="sm" data-act="restartCancel" id="btnRestartCancel">Cancelar reinicio</button></div>
   <p class="hint">El reinicio avisa a los jugadores con <code>servermsg</code>, guarda, cierra y vuelve a iniciar el servidor, que descarga las actualizaciones al arrancar.</p>
  </div>
 </div>
</section>

<section id="tab-mods" class="tab">
 <div class="pagehead"><div><h1>Mods</h1><p class="hint">El orden de la lista es el orden de carga: arrastrá &#8942;&#8942; o cambiá el número para moverlos.</p></div>
  <div class="actions"><button class="primary" id="btnWsOpen">Agregar desde Workshop</button><button data-act="wscheck">Buscar actualizaciones</button><button id="btnScan">Verificar archivos locales</button><button id="btnDetect">Completar Workshop IDs</button><button id="btnImport">Importar</button><button id="btnExport">Exportar</button><input type="file" id="importFile" accept=".json,application/json" hidden></div></div>
 <div class="card">
  <form id="addForm" class="addrow"><input id="newId" class="mono" placeholder="Mod ID nuevo (ej. Brita)" autocomplete="off"><input id="newWid" placeholder="Workshop ID o URL de Steam (opcional)" autocomplete="off"><button class="primary" type="submit">+ Agregar mod</button></form>
  <div class="toolbar">
   <input id="modSearch" class="grow" type="search" placeholder="Buscar por Mod ID, Workshop ID o nota… (Ctrl+K)">
   <div class="seg" id="modFilter"><button data-f="all" class="on">Todos <b id="f-all"></b></button><button data-f="on">Activos <b id="f-on"></b></button><button data-f="off">Inactivos <b id="f-off"></b></button><button data-f="warn">Con avisos <b id="f-warn"></b></button></div>
   <button id="bulkOn" class="sm">Activar visibles</button><button id="bulkOff" class="sm">Desactivar visibles</button>
  </div>
  <p class="hint" id="scanInfo"></p>
  <div class="tablewrap"><table class="mods"><thead><tr><th></th><th>#</th><th>Activo</th><th>Mod ID</th><th>Workshop ID</th><th>Estado</th><th>Notas</th><th></th></tr></thead><tbody id="modRows"></tbody></table><p id="modEmpty" class="empty" hidden>Ningún mod coincide con la búsqueda o el filtro.</p></div>
 </div>
 <details class="card" id="exBox"><summary>Mods excluidos sin Workshop ID <span class="badge b-muted" id="exCount">0</span></summary>
  <p class="hint">Vienen de <code>mods_excluidos_sin_workshop_id.json</code>. Se agregan desactivados; activalos cuando confirmes su Workshop ID.</p><div id="exList"></div></details>
</section>

<section id="tab-console" class="tab">
 <div class="pagehead"><h1>Consola</h1><div class="actions"><select id="logSource" aria-label="Log a mostrar"></select><button id="btnLogsRefresh" class="icon" title="Actualizar lista de logs">&#10227;</button><button data-open="logs">Abrir carpeta</button></div></div>
 <div id="adminPrompt" class="banner warn" hidden><span>El servidor pide crear la contraseña del usuario <b>admin</b>. Escribila abajo y pulsá Enter (la pide dos veces).</span></div>
 <div class="card">
  <div class="toolbar"><input id="logFind" class="grow" type="search" placeholder="Filtrar líneas (ej. ERROR, Workshop, nombre de un mod)…"><label class="chk"><input type="checkbox" id="follow" checked> Seguir</label><label class="chk"><input type="checkbox" id="wrap"> Ajustar líneas</label><button id="btnClearView" class="sm">Limpiar vista</button></div>
  <div id="out" class="console" tabindex="0"></div>
  <form id="cmdForm" class="cmdbar"><span class="prompt">&gt;</span><input id="cmd" autocomplete="off" spellcheck="false"><button class="primary" type="submit" id="cmdSend">Enviar</button></form>
  <div class="actions quick"><span class="hint">Rápidos:</span><button class="sm" data-cmd="save">save</button><button class="sm" data-cmd="players">players</button><button class="sm" id="btnBroadcast">Mensaje a todos…</button><button class="sm" data-cmd="help">help</button><button class="sm" data-act="stop">quit (detener)</button></div>
 </div>
</section>

<section id="tab-players" class="tab">
 <div class="pagehead"><div><h1>Jugadores</h1><p class="hint">Conectados, whitelist y roles. Las acciones se envían como comandos al servidor en ejecución.</p></div>
  <div class="actions"><label class="chk"><input type="checkbox" id="plAuto" checked> Actualizar cada 30 s</label><button id="plRefresh">Actualizar</button></div></div>
 <div id="plOffline" class="banner info" hidden>El servidor no está en ejecución desde la app: podés ver las cuentas guardadas, pero para expulsar, banear, cambiar roles o agregar cuentas hay que iniciarlo.</div>
 <div class="grid2">
  <div class="card"><div class="cardhead"><div><h2>Conectados ahora <span id="plCount" class="badge b-muted">–</span></h2><p class="hint" id="plChecked"></p></div><button class="sm" id="btnBroadcast2">Mensaje a todos…</button></div>
   <div id="plList"></div></div>
  <div class="card"><h2>Acceso al servidor</h2><p class="hint">Se guarda en el INI y se aplica al reiniciar el servidor.</p><div id="accessBox" class="form"></div></div>
 </div>
 <div class="card">
  <div class="cardhead"><div><h2>Cuentas y whitelist <span id="wlCount" class="badge b-muted">0</span></h2><p class="hint mono" id="wlDb"></p></div><input id="wlSearch" type="search" placeholder="Buscar usuario o SteamID…"></div>
  <form id="wlAdd" class="addrow"><input id="wlUser" placeholder="Usuario nuevo" autocomplete="off" spellcheck="false"><input id="wlPass" type="password" placeholder="Contraseña de esa cuenta" autocomplete="new-password"><button class="primary" type="submit" id="wlAddBtn">+ Agregar a la whitelist</button></form>
  <div class="tablewrap short"><table class="users"><thead><tr><th>Usuario</th><th>Rol</th><th>Último ingreso</th><th>SteamID</th><th>Estado</th><th></th></tr></thead><tbody id="wlRows"></tbody></table><p id="wlEmpty" class="empty" hidden>No hay cuentas para mostrar.</p></div>
  <div id="wlBans"></div>
 </div>
</section>

<section id="tab-network" class="tab">
 <div class="pagehead"><div><h1>Red y puertos</h1><p class="hint">Qué puertos usa el servidor, si el firewall los permite y cómo exponerlo con la menor superficie posible.</p></div><div class="actions"><button id="netRefresh">Actualizar</button></div></div>
 <div class="grid2">
  <div class="card"><h2>Revisión de seguridad</h2><p class="hint" id="secSummary"></p><ul class="checks" id="secChecks"></ul></div>
  <div class="card"><h2>Puertos del servidor</h2><p class="hint">Según el INI (DefaultPort, UDPPort…). Se cambian en Ajustes del servidor.</p>
   <div class="tablewrap auto"><table><thead><tr><th>Puerto</th><th>Proto.</th><th>Uso</th><th>En esta PC</th><th>Firewall</th></tr></thead><tbody id="portRows"></tbody></table></div></div>
  <div class="card"><h2>Firewall de Windows</h2><p class="hint" id="fwInfo"></p><div id="fwRules"></div>
   <div class="actions fwbtns"><button data-fw="lan">Permitir solo red local</button><button data-fw="internet" class="primary">Permitir desde Internet</button><button data-fw="remove" class="danger">Quitar reglas</button></div>
   <p class="hint">Las reglas se limitan al <code>java.exe</code> del servidor y a sus puertos UDP, en el perfil de red actual. Windows pide permiso de administrador (UAC) solo para este paso.</p>
   <div id="fwOthers"></div></div>
  <div class="card"><h2>Exponer a Internet</h2><div id="exposeBox" class="form"></div></div>
 </div>
</section>

<section id="tab-ini" class="tab">
 <div class="pagehead"><div><h1>Ajustes del servidor</h1><p class="hint mono" id="iniPath"></p></div><div class="actions"><button data-open="ini">Abrir carpeta</button><button id="iniReload">Recargar</button></div></div>
 <div id="iniRunning" class="banner info" hidden>El servidor está en ejecución: los cambios se aplican la próxima vez que lo inicies.</div>
 <div id="iniMissing" class="banner warn" hidden>Todavía no existe el INI de este servidor. Inicialo una vez desde Inicio para que Project Zomboid lo genere.</div>
 <div class="card" id="iniCard">
  <div class="toolbar"><input id="iniSearch" class="grow" type="search" placeholder="Buscar ajuste por nombre o descripción…"><label class="chk"><input type="checkbox" id="iniOnly"> Solo modificados</label></div>
  <h3 id="iniCommonH">Principales</h3><div id="iniCommon" class="inigrid"></div>
  <h3 id="iniAllH">Todos los demás <span id="iniCount"></span></h3><div id="iniAll" class="inigrid"></div>
  <p id="iniEmpty" class="empty" hidden>Ningún ajuste coincide.</p>
 </div>
</section>

<section id="tab-settings" class="tab">
 <div class="pagehead"><h1>Configuración del panel</h1><div class="actions"><button class="primary" id="setSave">Guardar configuración</button></div></div>
 <div class="grid2">
  <div class="card"><h2>Rutas</h2><div class="form">
   <div class="field"><label for="s-server_name">Nombre del servidor</label><input id="s-server_name" class="mono"><p class="hint">Define <code>Zomboid/Server/&lt;nombre&gt;.ini</code> y el mundo guardado. Letras, números, _ y -.</p></div>
   <div class="field"><label for="s-server_dir">Carpeta del servidor dedicado <span data-path="server_dir"></span></label><input id="s-server_dir" class="mono"></div>
   <div class="field"><label for="s-steamcmd_path">steamcmd.exe <span data-path="steamcmd_path"></span></label><input id="s-steamcmd_path" class="mono"></div>
   <div class="field"><label for="s-pz_home">Carpeta Zomboid del usuario <span data-path="pz_home"></span></label><input id="s-pz_home" class="mono"></div>
  </div></div>
  <div class="card"><h2>Servidor y mods</h2><div class="form">
   <div class="field"><label for="s-ram_gb">Memoria RAM (GB)</label><input id="s-ram_gb" type="number" min="0" max="256" placeholder="Vacío = la de StartServer64.bat"><p class="hint" id="ramHint"></p></div>
   <div class="field"><label for="s-branch">Rama de Steam</label><input id="s-branch" class="mono" placeholder="Vacío = rama predeterminada"><p class="hint">Dejala vacía salvo que necesites una rama beta válida para el dedicado.</p></div>
   <label class="chk"><span class="switch"><input type="checkbox" id="s-strict"><span></span></span>Modo estricto: no aplicar al INI ni iniciar si hay mods activos con advertencias</label>
   <div class="field"><label for="s-extra">Carpetas Workshop adicionales</label><textarea id="s-extra" rows="3" class="mono" placeholder="Una ruta por línea, ej. D:\SteamLibrary\steamapps\workshop\content\108600"></textarea><p class="hint">Se usan para verificar mods y completar Workshop IDs. Las bibliotecas del cliente Steam se detectan solas.</p></div>
  </div></div>
 </div>
</section>

</main>
</div>

<div id="modsBar" class="savebar" hidden><span>Cambios sin guardar en <b>Mods</b></span><button id="modsDiscard">Descartar</button><button id="modsSave">Guardar</button><button id="modsSaveApply" class="primary">Guardar y aplicar al INI</button></div>
<div id="iniBar" class="savebar" hidden><span id="iniBarText"></span><button id="iniDiscard">Descartar</button><button id="iniSave" class="primary">Guardar en el INI</button></div>
<div id="toasts" aria-live="polite"></div>
<div id="closing" class="overlay" hidden><div><div class="spinner"></div><h2>Guardando y cerrando…</h2><p class="hint">El servidor guarda el mundo antes de detenerse. No apagues la PC.</p></div></div>
<dialog id="dlg"><h2 id="dlgTitle"></h2><p id="dlgText"></p><input id="dlgInput" hidden autocomplete="off"><div class="actions" id="dlgBtns"></div></dialog>
<dialog id="wsDlg" class="wide"><h2>Agregar desde Steam Workshop</h2>
 <p>Pegá la URL o el ID de un mod o de una colección. Podés pegar varios, separados por espacios o comas.</p>
 <form id="wsForm" class="wsform"><input id="wsInput" placeholder="https://steamcommunity.com/sharedfiles/filedetails/?id=…" autocomplete="off" spellcheck="false"><button class="primary" type="submit" id="wsFind">Buscar</button></form>
 <p class="hint" id="wsInfo"></p>
 <div class="tablewrap short" id="wsWrap" hidden><table class="wsres"><thead><tr><th><input type="checkbox" id="wsAll" aria-label="Seleccionar todos"></th><th>Mod ID</th><th>Mod en Workshop</th><th>Actualizado</th><th>Estado</th></tr></thead><tbody id="wsRows"></tbody></table></div>
 <div class="actions"><button class="primary" type="button" id="wsAdd" disabled>Agregar seleccionados</button><button type="button" id="wsClose">Cerrar</button></div>
</dialog>

<script>
'use strict';
const TOKEN='__TOKEN__',APP_MODE='__MODE__'==='app';
// En la ventana propia no hay HTTP: pywebview inyecta window.pywebview.api y avisa con 'pywebviewready'.
const bridgeReady=APP_MODE?new Promise(r=>{if(window.pywebview&&window.pywebview.api)r();else window.addEventListener('pywebviewready',()=>r(),{once:true})}):Promise.resolve();
const ROLE_LABEL={none:'Usuario',user:'Usuario',priority:'Prioridad',observer:'Observador',gm:'GM',moderator:'Moderador',admin:'Admin',banned:'Baneado'};
const ROLES=['none','priority','observer','gm','moderator','admin'];
const COMMON_INI=['PublicName','PublicDescription','Public','Open','Password','MaxPlayers','DefaultPort','UDPPort','PVP','PauseEmpty','SaveWorldEveryMinutes','BackupsCount','BackupsOnStart','Map','ServerWelcomeMessage','SpawnPoint','AllowCoop','SleepAllowed','VoiceEnable','RCONPort','RCONPassword'];
const SECRET_RE=/password|token/i;
const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const S={tab:'home',status:{},prev:null,overview:null,settings:{},mods:[],saved:'[]',scan:null,ws:{},excluded:[],filter:'all',ini:null,iniChanges:{},logs:[],hist:[],hpos:0};

function el(tag,props={},...kids){
  const e=document.createElement(tag);
  for(const [k,v] of Object.entries(props)){
    if(v==null||v===false)continue;
    if(k==='class')e.className=v;
    else if(k==='text')e.textContent=v;
    else if(k.startsWith('on')&&typeof v==='function')e.addEventListener(k.slice(2),v);
    else if(k.includes('-')||!(k in e))e.setAttribute(k,v===true?'':v);
    else e[k]=v;
  }
  for(const c of kids.flat())if(c!=null&&c!==false)e.append(c);
  return e;
}
function switchEl(checked,onchange,label){
  const input=el('input',{type:'checkbox',checked,onchange:e=>onchange(e.target.checked)});
  if(label)input.setAttribute('aria-label',label);
  return el('span',{class:'switch'},input,el('span'));
}
const fmtDur=s=>{s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return(h?h+'h ':'')+(h?String(m).padStart(2,'0'):m)+'m '+String(x).padStart(2,'0')+'s'};
const fmtTime=t=>new Date(t*1000).toLocaleString('es-AR',{dateStyle:'short',timeStyle:'short'});
const fmtSize=b=>b<1024?b+' B':b<1048576?(b/1024).toFixed(1)+' KB':(b/1048576).toFixed(1)+' MB';
const basename=p=>String(p||'').split(/[\\/]/).pop();

/* ---------- API, avisos y diálogos ---------- */
async function api(path,method='GET',body){
  if(APP_MODE){
    await bridgeReady;
    const res=await window.pywebview.api.call(method,path,body===undefined?null:body);
    if(!res||!res.ok)throw Error(res&&res.error||'Error interno de la app');
    return res.data;
  }
  let r;
  try{r=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-PZ-Token':TOKEN},body:body===undefined?undefined:JSON.stringify(body)})}
  catch(e){$('#offline').hidden=false;throw Error('No se pudo conectar con el panel.')}
  $('#offline').hidden=true;
  const j=await r.json().catch(()=>({error:'Respuesta inválida del panel ('+r.status+')'}));
  if(!r.ok){if(j.error==='Token inválido')$('#stale').hidden=false;throw Error(j.error||('Error '+r.status))}
  return j;
}
function toast(msg,type='ok',opt={}){
  const t=el('div',{class:'toast '+type,role:type==='err'?'alert':'status'},el('span',{text:msg}));
  if(opt.action)t.append(el('button',{text:opt.action,onclick:()=>{t.remove();opt.onAction()}}));
  t.append(el('button',{class:'x',text:'×','aria-label':'Cerrar',onclick:()=>t.remove()}));
  $('#toasts').append(t);
  setTimeout(()=>t.remove(),opt.timeout||(type==='err'?10000:opt.action?8000:4500));
}
async function run(fn,btn){
  if(btn){btn.disabled=true;btn.classList.add('busy')}
  try{return await fn()}catch(e){toast(e.message,'err')}
  finally{if(btn){btn.classList.remove('busy');btn.disabled=false;updateButtons()}}
}
function ask(title,text,buttons,input){
  const d=$('#dlg'),inp=$('#dlgInput');
  $('#dlgTitle').textContent=title;$('#dlgText').textContent=text||'';
  inp.hidden=!input;inp.value='';if(input)inp.placeholder=input;
  return new Promise(res=>{
    const done=k=>{res(input?(k?inp.value:null):k);d.close()};
    $('#dlgBtns').replaceChildren(...buttons.map(b=>el('button',{class:b.cls||'',text:b.t,type:'button',onclick:()=>done(b.k)})));
    inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();done(buttons[0].k)}};
    d.oncancel=()=>res(null);
    d.showModal();
    (input?inp:$('#dlgBtns button')).focus();
  });
}
const confirmBox=(title,text,ok='Continuar',cls='primary')=>ask(title,text,[{k:'ok',t:ok,cls},{k:null,t:'Cancelar'}]);

/* ---------- Navegación ---------- */
function showTab(name){
  if(!$('#tab-'+name))name='home';
  S.tab=name;
  $$('.tab').forEach(t=>t.classList.toggle('active',t.id==='tab-'+name));
  $$('nav [data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  try{history.replaceState(null,'','#'+name)}catch(e){}
  if(name==='console'){loadLogs();consoleStart()}else consoleStop();
  if(name==='players'){run(loadPlayersTab);playersTimer(true)}else playersTimer(false);
  if(name==='network'&&!S.net)run(loadNetwork);
  if(name==='home'&&S.overview)loadOverview().catch(()=>{});
  if(name==='mods'&&!S.scan)scanLocal(true);
  if(name==='ini'&&!S.ini)run(loadIni);
  updateBars();
}

/* ---------- Estado del servidor ---------- */
function stateInfo(st){
  if(st.running&&st.kind==='update')return{cls:'busy',title:'Actualizando servidor',sub:'SteamCMD está descargando y validando archivos. Seguilo en la Consola.'};
  if(st.running&&st.stopping)return{cls:'busy',title:'Deteniendo…',sub:'Guardando el mundo y cerrando el servidor.'};
  if(st.running&&st.prompt)return{cls:'warn',title:'Esperando contraseña admin',sub:'Escribí la contraseña del usuario admin en la Consola.'};
  if(st.running&&st.ready)return{cls:'on',title:'En línea',sub:'El servidor está listo para recibir jugadores.'};
  if(st.running)return{cls:'busy',title:'Iniciando…',sub:'Cargando el mundo y los mods. Puede tardar varios minutos.'};
  if(st.external)return{cls:'on',title:'En ejecución (fuera del panel)',sub:'Se inició desde otra ventana (PID '+st.external+'). Desde acá solo se puede forzar el cierre.'};
  if(st.kind==='update')return st.update_ok?{cls:'off',title:'Detenido',sub:'La última actualización terminó correctamente.'}:{cls:'err',title:'Detenido',sub:'La última actualización no confirmó éxito. Revisá el log en la Consola.'};
  if(st.kind==='server'&&st.returncode)return{cls:'err',title:'Detenido',sub:'El servidor terminó con código '+st.returncode+'. Revisá el log en la Consola.'};
  return{cls:'off',title:'Detenido',sub:st.kind?'El servidor se cerró correctamente.':'No hay procesos en ejecución.'};
}
function setState(node,cls){node.className=node.className.replace(/\bs-\w+/g,'').trim()+' s-'+cls}
function renderStatus(){
  const st=S.status||{},info=stateInfo(st);
  setState($('#hdrPill'),info.cls);setState($('#homeState'),info.cls);
  $('#hdrState').textContent=info.title;$('#stTitle').textContent=info.title;$('#stSub').textContent=info.sub;
  const prompt=!!(st.running&&st.prompt);
  $('#homePrompt').hidden=!prompt;$('#adminPrompt').hidden=!prompt;
  $('#iniRunning').hidden=!(st.running&&st.kind==='server'||st.external);
  const rows=[];
  if(st.running){rows.push(['Proceso',st.kind==='update'?'SteamCMD':'Servidor dedicado'],['PID',String(st.pid)],['Inicio',fmtTime(st.started)],['Activo',el('span',{class:'uptime-live'})],['Log',st.log||'–'])}
  else if(st.external)rows.push(['PID',String(st.external)]);
  const o=S.overview;
  if(o)rows.push(['RAM',o.ram_gb?o.ram_gb+' GB (configurada en el panel)':o.memory?o.memory+' GB (StartServer64.bat)':'desconocida']);
  $('#stInfo').replaceChildren(...rows.flatMap(([k,v])=>[el('dt',{text:k}),el('dd',{},v)]));
  updateButtons();tick();
  const p=S.prev;
  if(p&&p.running&&(!st.running||p.pid!==st.pid)){
    if(p.kind==='update')toast(st.update_ok?'Actualización del servidor completada.':'La actualización terminó sin confirmar éxito. Revisá el log.',st.update_ok?'ok':'err');
    else toast(st.returncode?'El servidor se cerró con código '+st.returncode+'.':'Servidor detenido.',st.returncode?'warn':'info');
    loadOverview().catch(()=>{});loadLogs();
  }
  if(p&&!p.ready&&st.ready&&st.running)toast('Servidor en línea.');
  if(p&&!p.prompt&&prompt)toast('El servidor pide la contraseña de admin: escribila en la Consola.','warn');
  if(p&&!!p.can_command!==!!st.can_command&&S.tab==='players')run(loadPlayersTab);
  const err=st.restart&&st.restart.error;
  if(p&&err&&err!==(p.restart&&p.restart.error))toast('No se pudo completar el reinicio: '+err,'err');
  S.prev=st;
}
function renderRestart(){
  const st=S.status||{},r=st.restart,active=!!(r&&r.phase);
  $('#restartBox').hidden=active;$('#restartInfo').hidden=!active;
  $('#btnRestart').disabled=!st.can_command||!!st.stopping;
  if(!active)return;
  $('#btnRestartCancel').hidden=r.phase!=='countdown';
  $('#restartText').textContent=r.phase==='countdown'?'Reinicio en '+fmtDur(r.at-Date.now()/1000)+'. Los jugadores reciben avisos antes del cierre.':r.phase==='stopping'?'Guardando y cerrando el servidor para reiniciarlo…':'Iniciando el servidor de nuevo…';
}
function updateButtons(){
  const st=S.status||{},srv=st.running&&st.kind==='server',upd=st.running&&st.kind==='update',busy=st.running||!!st.external;
  $$('[data-act=start]').forEach(b=>b.disabled=busy);
  $$('[data-act=stop]').forEach(b=>{b.disabled=!(srv&&!st.stopping)&&!upd;if(b.id==='hdrStop')b.textContent=upd?'\u25A0 Cancelar':'\u25A0 Detener'});
  $$('[data-act=kill]').forEach(b=>b.disabled=!busy);
  $$('[data-act=update]').forEach(b=>b.disabled=busy);
  $$('[data-cmd],#btnBroadcast,#btnBroadcast2').forEach(b=>b.disabled=!st.can_command);
  $('#hdrStart').hidden=busy;$('#hdrStop').hidden=!(st.running);
  const c=$('#cmd');c.disabled=!st.can_command;$('#cmdSend').disabled=!st.can_command;
  c.placeholder=st.can_command?'Comando del servidor: help, save, players, kickuser "nombre"…':'Iniciá el servidor desde el panel para enviar comandos';
}
function tick(){
  const st=S.status||{},up=st.running&&st.started?fmtDur(Date.now()/1000-st.started):'';
  $('#hdrUptime').textContent=up;
  $$('.uptime-live').forEach(n=>n.textContent=up);
  renderRestart();
}
async function pollStatus(){
  try{S.status=await api('status');renderStatus()}catch(e){}
  setTimeout(pollStatus,2000);
}

/* ---------- Inicio ---------- */
const ACTION_LABEL={update:'Descargar',start:'Iniciar',apply:'Aplicar'};
async function loadOverview(){
  const o=await api('overview');S.overview=o;S.status=o.status;
  $('#hdrName').textContent=o.server_name;
  const done=o.checks.filter(c=>c.ok).length;
  $('#checksSummary').textContent=done===o.checks.length?'Todo listo para jugar.':done+' de '+o.checks.length+' pasos completos.';
  $('#checksBar').style.width=(100*done/o.checks.length)+'%';
  $('#checks').replaceChildren(...o.checks.map(c=>el('li',{class:c.ok?'ok':'todo'},
    el('span',{class:'ck',text:c.ok?'\u2713':'!'}),
    el('div',{},el('b',{text:c.title}),c.ok?null:el('p',{class:'hint',text:c.hint})),
    c.ok?null:c.action?el('button',{class:'sm','data-act':c.action,text:ACTION_LABEL[c.action]}):c.tab?el('button',{class:'sm','data-goto':c.tab,text:'Revisar'}):null)));
  $('#kEnabled').textContent=o.mods.enabled;$('#kTotal').textContent=o.mods.total;$('#kIssues').textContent=o.mods.issues.length;
  S.ws=o.workshop||{};
  renderSync(o);renderWorkshop();renderStatus();renderPaths(o.paths);
  $('#adminWarn').hidden=!o.is_admin;
  $('#ramHint').textContent=o.memory?'StartServer64.bat asigna '+o.memory+' GB. Si ponés un valor, el panel inicia el servidor con esa RAM (-Xms/-Xmx).':'Si ponés un valor, el panel inicia el servidor con esa RAM (-Xms/-Xmx).';
  const nm=$('#navMods');
  if(!modsDirty()){nm.textContent=o.mods.issues.length||'';nm.className='count warn'}
  return o;
}
function renderSync(o){
  const s=o.sync,box=$('#syncDiff');box.replaceChildren();
  if(!s.exists){$('#syncText').textContent='Todavía no existe el INI: iniciá el servidor una vez.';return}
  if(s.in_sync){$('#syncText').textContent='\u2713 El INI tiene exactamente los mods activos, en el mismo orden.';return}
  $('#syncText').textContent='El INI no coincide con el catálogo: tiene '+s.ini_mods+' mods y '+s.ini_workshop+' Workshop IDs; el catálogo activa '+s.want_mods+' y '+s.want_workshop+'.';
  const group=(title,list,cls)=>list&&list.length?[el('p',{class:'hint',text:title+' ('+list.length+')'}),el('div',{class:'difflist'},...list.slice(0,60).map(x=>el('span',{class:'badge '+cls,text:x})),list.length>60?el('span',{class:'hint',text:'…'}):null)]:[];
  box.append(...group('Faltan en el INI',s.missing,'b-warn'),...group('Sobran en el INI',s.extra,'b-muted'),...group('Workshop IDs faltantes',s.workshop_missing,'b-warn'),...group('Workshop IDs que sobran',s.workshop_extra,'b-muted'));
  if(s.order_differs)box.append(el('p',{class:'hint',text:'Los mismos mods, pero en otro orden de carga.'}));
}
function renderWorkshop(){
  const w=S.ws||{},box=$('#wsDiff');box.replaceChildren();
  if(!w.checked){$('#wsText').textContent='Todavía no se revisó si los mods del catálogo tienen actualizaciones en Steam.';return}
  const out=w.outdated||[],miss=w.missing||[],name=id=>(w.items[id]&&w.items[id].title)||id;
  const parts=[w.downloaded?(out.length?out.length+' mod(s) con actualización disponible.':'Los mods que bajó el servidor están al día.'):'El servidor todavía no descargó mods (los baja al iniciar): no hay fechas para comparar.'];
  if(miss.length)parts.push(miss.length+' mod(s) retirado(s) de Workshop.');
  parts.push('Última revisión: '+fmtTime(w.checked));
  $('#wsText').textContent=parts.join(' ');
  const group=(title,list,cls,tip)=>list.length?[el('p',{class:'hint',text:title}),el('div',{class:'difflist'},...list.slice(0,60).map(id=>el('span',{class:'badge '+cls,text:name(id),title:tip(id)})))]:[];
  box.append(...group('Retirados (pueden impedir que el servidor arranque):',miss,'b-err',id=>id+': '+w.items[id].problem),
    ...group('Con actualización:',out,'b-warn',id=>'Workshop '+id+' · Steam: '+fmtTime(w.items[id].updated)+' · servidor: '+fmtTime(w.items[id].local)));
}
function renderPaths(paths){
  $$('[data-path]').forEach(n=>{const ok=paths[n.dataset.path];n.replaceChildren(el('span',{class:'badge '+(ok?'b-ok':'b-err'),text:ok?'\u2713 encontrado':'\u2717 no existe'}))});
}

/* ---------- Acciones ---------- */
const ACTIONS={
  async start(){
    const o=S.overview;
    if(modsDirty()&&!await confirmBox('Mods sin guardar','Tenés cambios sin guardar en Mods. El servidor usará lo que ya está en el INI.','Iniciar igual'))return;
    if(o&&o.sync.exists&&!o.sync.in_sync&&!modsDirty()){
      const k=await ask('Mods no aplicados','El INI no tiene los mismos mods que el catálogo. ¿Aplicarlos antes de iniciar?',[{k:'apply',t:'Aplicar e iniciar',cls:'primary'},{k:'start',t:'Iniciar sin aplicar'},{k:null,t:'Cancelar'}]);
      if(!k)return;
      if(k==='apply'){const j=await api('apply','POST',{});toast('INI actualizado: '+j.mod_count+' mods.')}
    }
    await api('start','POST',{});
    toast('Iniciando servidor… podés seguirlo en la Consola.','info');
    S.status=await api('status');renderStatus();loadLogs();
  },
  async stop(){
    const st=S.status;
    if(st.running&&st.kind==='update'){
      if(!await confirmBox('Cancelar actualización','SteamCMD se cerrará a mitad de la descarga. Podés volver a ejecutarla después.','Cancelar actualización','stop'))return;
      await api('stop','POST',{force:true});toast('Actualización cancelada.','warn');
    }else{const j=await api('stop','POST',{});toast(j.message,'info')}
    S.status=await api('status');renderStatus();
  },
  async kill(){
    if(!await confirmBox('Forzar cierre','Se termina el proceso sin guardar: se pierde lo ocurrido desde el último guardado. Usalo solo si el servidor no responde.','Forzar cierre','stop'))return;
    const j=await api('stop','POST',{force:true});toast(j.message,'warn');
    S.status=await api('status');renderStatus();
  },
  async update(){
    if(!await confirmBox('Actualizar servidor','Se ejecuta SteamCMD para descargar y validar el servidor dedicado (AppID 380870). Puede tardar varios minutos.','Actualizar'))return;
    await api('update','POST',{});toast('Actualización iniciada.','info');
    S.status=await api('status');renderStatus();showTab('console');
  },
  async apply(){
    if(modsDirty()){
      const k=await ask('Mods sin guardar','Tenés cambios sin guardar. Para aplicarlos al INI primero hay que guardarlos.',[{k:'save',t:'Guardar y aplicar',cls:'primary'},{k:'saved',t:'Aplicar lo guardado'},{k:null,t:'Cancelar'}]);
      if(!k)return;
      if(k==='save')await saveMods(true);
    }
    const j=await api('apply','POST',{});
    toast('INI actualizado: '+j.mod_count+' mods y '+j.workshop_count+' Workshop IDs.\nCopia de seguridad: '+basename(j.backup)+(j.warnings.length?'\n'+j.warnings.length+' advertencia(s).':''),j.warnings.length?'warn':'ok');
    loadOverview();if(S.ini)loadIni();
  },
  async wscheck(){
    if(modsDirty())toast('Se revisa el catálogo guardado: los cambios sin guardar no se incluyen.','info');
    S.ws=await api('ws_updates','POST',{});
    renderWorkshop();renderMods();
    const n=S.ws.outdated.length,m=S.ws.missing.length;
    toast(n||m?(n?n+' mod(s) con actualización disponible. ':'')+(m?m+' retirado(s) de Workshop.':''):S.ws.downloaded?'Los mods que bajó el servidor están al día.':'Revisión hecha: el servidor todavía no descargó mods para comparar.',n||m?'warn':'ok');
  },
  async restart(){
    const min=+$('#restartMin').value;
    const text=(min?'Los jugadores reciben un aviso ahora y otros antes del cierre. En '+min+' minutos':'Los jugadores reciben un aviso y')+' el servidor guarda, se cierra y vuelve a iniciar. Al arrancar descarga las actualizaciones de los mods.';
    if(!await confirmBox('Reiniciar para actualizar',text,min?'Programar reinicio':'Reiniciar ahora'))return;
    await api('restart','POST',{minutes:min});
    toast(min?'Reinicio programado en '+min+' minutos.':'Reiniciando el servidor…','info');
    S.status=await api('status');renderStatus();
  },
  async restartCancel(){
    await api('restart_cancel','POST',{});
    toast('Reinicio cancelado. Se avisó a los jugadores.','info');
    S.status=await api('status');renderStatus();
  },
};
async function sendCmd(cmd){
  cmd=String(cmd||'').trim();if(!cmd)return;
  if(cmd.toLowerCase()==='quit')return ACTIONS.stop();
  const secret=!!(S.status.prompt||!$('#adminPrompt').hidden);
  await api('command','POST',{command:cmd});
  echo('> '+(secret?'********':cmd));
  if(!secret&&S.hist[S.hist.length-1]!==cmd)S.hist.push(cmd);
  S.hpos=S.hist.length;
  if(cmd==='save')toast('Guardando el mundo…','info');
}

/* ---------- Mods ---------- */
function normWid(v){
  v=String(v||'').trim();if(!v||/^\d+$/.test(v))return v;
  try{const id=new URL(v).searchParams.get('id');if(id&&/^\d+$/.test(id))return id}catch(e){}
  return v;
}
const modsDirty=()=>JSON.stringify(S.mods)!==S.saved;
function dups(){const c={};for(const m of S.mods){const id=(m.id||'').trim();c[id]=(c[id]||0)+1}return new Set(Object.keys(c).filter(k=>c[k]>1))}
function modStatus(m,dup){
  const out=[],id=(m.id||'').trim(),wid=normWid(m.workshop_id),on=!!m.enabled;
  if(!id||/[;\\\r\n]/.test(id))out.push({t:'Mod ID inválido',c:'err'});
  if(dup.has(id))out.push({t:'Duplicado',c:'err'});
  if(!wid)out.push({t:'Sin Workshop ID',c:on?'err':'muted'});
  else if(!/^\d+$/.test(wid))out.push({t:'Workshop ID inválido',c:'err'});
  else if(S.scan){
    const found=S.scan.found[id]||[];
    if(found.includes(wid))out.push({t:'\u2713 Verificado',c:'ok',title:'Un mod.info de ese Workshop declara este Mod ID.'});
    else if(found.length)out.push({t:'Está en '+found.join(', '),c:'warn',title:'El mod.info con este Mod ID está en otro Workshop ID.'});
    else if(S.scan.downloaded.includes(wid))out.push({t:'ID no coincide',c:on?'warn':'muted',title:'El Workshop está descargado pero ningún mod.info declara este Mod ID.'});
    else out.push({t:'No descargado',c:'muted',title:'El servidor lo descarga al iniciar. Volvé a verificar después.'});
  }
  const ws=S.ws.items&&S.ws.items[wid];
  if(ws&&ws.state==='missing')out.push({t:'Retirado de Workshop',c:on?'err':'muted',title:ws.problem});
  else if(ws&&ws.state==='outdated')out.push({t:'Actualización disponible',c:'warn',title:'Steam: '+fmtTime(ws.updated)+' · servidor: '+fmtTime(ws.local)+'. Reiniciá el servidor para descargarla.'});
  const req=on&&S.scan&&S.scan.requires&&S.scan.requires[id];
  if(req){
    const active=new Set(S.mods.filter(x=>x.enabled).map(x=>(x.id||'').trim()));
    const miss=req.filter(r=>!active.has(r));
    if(miss.length)out.push({t:'Requiere '+miss.slice(0,2).join(', ')+(miss.length>2?'…':''),c:'warn',title:'Su mod.info pide estos mods y no están activos: '+miss.join(', ')});
  }
  return out;
}
const level=st=>st.some(x=>x.c==='err')?'err':st.some(x=>x.c==='warn')?'warn':'';
function badges(st){return st.map(x=>el('span',{class:'badge b-'+x.c,text:x.t,title:x.title||null}))}
function matches(m,q){return !q||[m.id,m.workshop_id,m.notes].join(' ').toLowerCase().includes(q)}
function renderMods(){
  const q=$('#modSearch').value.trim().toLowerCase(),dup=dups(),frag=document.createDocumentFragment();
  let shown=0;
  S.mods.forEach((m,i)=>{
    const st=modStatus(m,dup),lvl=level(st);
    if(S.filter==='on'&&!m.enabled||S.filter==='off'&&m.enabled||S.filter==='warn'&&!lvl||!matches(m,q))return;
    frag.append(modRow(m,i,st,lvl));shown++;
  });
  $('#modRows').replaceChildren(frag);
  $('#modEmpty').hidden=shown>0;
  refreshCounts(dup);renderExcluded();
}
function refreshCounts(dup){
  dup=dup||dups();let on=0,warn=0;
  for(const m of S.mods){if(m.enabled)on++;if(level(modStatus(m,dup)))warn++}
  $('#f-all').textContent=S.mods.length;$('#f-on').textContent=on;$('#f-off').textContent=S.mods.length-on;$('#f-warn').textContent=warn;
  const nm=$('#navMods');
  if(modsDirty()){nm.textContent='\u25CF';nm.className='count dirty';nm.title='Cambios sin guardar'}else{nm.textContent=warn||'';nm.className='count warn';nm.title=warn?warn+' mods con avisos':''}
}
function refreshRows(){
  const dup=dups();
  for(const tr of $('#modRows').children){
    const m=S.mods[+tr.dataset.i];if(!m)continue;
    const st=modStatus(m,dup),lvl=level(st);
    tr.className=(m.enabled?'':'off ')+(lvl?'lvl-'+lvl:'');
    tr.querySelector('.st').replaceChildren(...badges(st));
    const a=tr.querySelector('.widcell a'),w=normWid(m.workshop_id);
    a.hidden=!/^\d+$/.test(w);a.href='https://steamcommunity.com/sharedfiles/filedetails/?id='+w;
  }
  refreshCounts(dup);updateBars();
}
let dragFrom=null;
function modRow(m,i,st,lvl){
  const tr=el('tr',{class:(m.enabled?'':'off ')+(lvl?'lvl-'+lvl:'')});tr.dataset.i=i;
  const handle=el('td',{class:'handle',title:'Arrastrá para cambiar el orden de carga',text:'\u22EE\u22EE'});
  handle.addEventListener('mousedown',()=>tr.draggable=true);
  handle.addEventListener('mouseup',()=>tr.draggable=false);
  tr.addEventListener('dragstart',e=>{dragFrom=i;tr.classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',m.id)});
  tr.addEventListener('dragend',()=>{tr.draggable=false;dragFrom=null;$$('.dragging,.drop-before,.drop-after').forEach(x=>x.classList.remove('dragging','drop-before','drop-after'))});
  tr.addEventListener('dragover',e=>{if(dragFrom==null)return;e.preventDefault();const r=tr.getBoundingClientRect(),after=e.clientY>r.top+r.height/2;tr.classList.toggle('drop-after',after);tr.classList.toggle('drop-before',!after)});
  tr.addEventListener('dragleave',()=>tr.classList.remove('drop-before','drop-after'));
  tr.addEventListener('drop',e=>{e.preventDefault();if(dragFrom==null)return;let to=i+(tr.classList.contains('drop-after')?1:0);if(dragFrom<to)to--;moveMod(dragFrom,to)});
  const w=normWid(m.workshop_id);
  const link=el('a',{href:'https://steamcommunity.com/sharedfiles/filedetails/?id='+w,target:'_blank',rel:'noopener noreferrer',title:'Abrir en Steam Workshop',text:'\u2197',hidden:!/^\d+$/.test(w)});
  const edit=(field,cls,ph)=>el('input',{type:'text',value:m[field]||'',class:cls,placeholder:ph,spellcheck:false,title:field==='notes'&&m.notes||null,oninput:e=>{m[field]=e.target.value;refreshRows()},
    onchange:field==='workshop_id'?e=>{const v=normWid(e.target.value);if(v!==e.target.value){e.target.value=v;m.workshop_id=v;refreshRows()}}:null});
  tr.append(
    handle,
    el('td',{},el('input',{type:'number',class:'pos',min:1,max:S.mods.length,value:i+1,title:'Posición en el orden de carga',onchange:e=>moveMod(i,(+e.target.value||1)-1)})),
    el('td',{class:'tog'},switchEl(!!m.enabled,v=>{m.enabled=v;refreshRows()},'Activar '+m.id)),
    el('td',{},edit('id','mono','Mod ID')),
    el('td',{},el('div',{class:'widcell'},edit('workshop_id','mono','Workshop ID'),link)),
    el('td',{},el('div',{class:'st'},...badges(st))),
    el('td',{},edit('notes','','Notas')),
    el('td',{},el('button',{class:'icon',title:'Quitar del catálogo','aria-label':'Quitar '+m.id,text:'\u2715',onclick:()=>removeMod(i)})));
  return tr;
}
function moveMod(from,to){
  to=Math.max(0,Math.min(S.mods.length-1,to));
  if(from===to){renderMods();return}
  const [m]=S.mods.splice(from,1);S.mods.splice(to,0,m);
  renderMods();updateBars();flashRow(to);
}
function flashRow(i){const tr=$('#modRows tr[data-i="'+i+'"]');if(tr){tr.classList.add('flash');tr.scrollIntoView({block:'nearest'})}}
function removeMod(i){
  const [m]=S.mods.splice(i,1);renderMods();updateBars();
  toast('Se quitó '+m.id+' del catálogo.','info',{action:'Deshacer',onAction:()=>{S.mods.splice(Math.min(i,S.mods.length),0,m);renderMods();updateBars();flashRow(i)}});
}
async function loadMods(){S.mods=await api('mods');S.saved=JSON.stringify(S.mods);renderMods();updateBars()}
async function saveMods(quiet){
  for(const m of S.mods){m.id=(m.id||'').trim();m.workshop_id=normWid(m.workshop_id)}
  const j=await api('mods','POST',S.mods);
  S.mods=j.mods;S.saved=JSON.stringify(S.mods);renderMods();updateBars();
  if(!quiet)toast('Catálogo guardado: '+S.mods.filter(m=>m.enabled).length+' mods activos. Aplicalo al INI para que el servidor lo use.');
  loadOverview().catch(()=>{});
}
async function scanLocal(quiet){
  try{
    $('#scanInfo').textContent='Verificando los mod.info locales…';
    S.scan=await api('scan');
    const n=Object.keys(S.scan.found).length;
    $('#scanInfo').textContent=S.scan.scanned.length?'Verificado contra '+S.scan.scanned.length+' carpeta(s) Workshop: '+S.scan.downloaded.length+' descargas y '+n+' Mod IDs encontrados.':'No hay descargas Workshop locales todavía: el servidor las baja al iniciar. Volvé a verificar después.';
    if(!quiet)toast('Verificación terminada.');
    refreshRows();
  }catch(e){if(!quiet)toast(e.message,'err')}
}
async function detectIds(){
  if(modsDirty()){if(!await confirmBox('Mods sin guardar','Para completar IDs hay que guardar primero el catálogo.','Guardar y continuar'))return;await saveMods(true)}
  const j=await api('detect','POST',{});
  await loadMods();await scanLocal(true);
  toast(j.assigned.length?'Se completaron '+j.assigned.length+' Workshop IDs: '+j.assigned.slice(0,6).join(', ')+(j.assigned.length>6?'…':''):'No había Workshop IDs vacíos que se pudieran completar con archivos locales.',j.assigned.length?'ok':'info');
  if(j.ambiguous.length)toast(j.ambiguous.length+' mod(s) tienen varios Workshop candidatos: '+j.ambiguous.map(a=>a.id).join(', '),'warn');
}
async function exportMods(){
  if(APP_MODE){const j=await api('export_mods','POST',S.mods);if(j.path)toast('Catálogo exportado a '+j.path);return}
  const a=el('a',{href:URL.createObjectURL(new Blob([JSON.stringify(S.mods,null,2)],{type:'application/json'})),download:'pz-mods.json'});
  a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);
}
async function importMods(file){
  let data;
  if(APP_MODE){const j=await api('import_mods','POST',{});if(!j.mods)return;data=j.mods}
  else{
    try{data=JSON.parse(await file.text())}catch(e){throw Error('El archivo no es un JSON válido.')}
    if(!Array.isArray(data))throw Error('El JSON debe ser una lista de mods.');
  }
  const ids=new Set(S.mods.map(m=>m.id));let added=0,skipped=0;
  for(const x of data){
    const id=String(x&&x.id||'').trim();
    if(!id||ids.has(id)){skipped++;continue}
    S.mods.push({id,enabled:x.enabled!==false,workshop_id:normWid(x.workshop_id),notes:String(x.notes||'')});ids.add(id);added++;
  }
  renderMods();updateBars();
  toast('Importación: '+added+' mods nuevos agregados al final'+(skipped?', '+skipped+' omitidos (ya existían o sin ID)':'')+'. Guardá para conservarlos.',added?'ok':'info');
}
function renderExcluded(){
  const ids=new Set(S.mods.map(m=>m.id));
  $('#exCount').textContent=S.excluded.length;
  $('#exList').replaceChildren(...S.excluded.map(x=>{
    const cand=((x.notes||'').match(/\b(\d{8,12})\b/)||[])[1]||'';
    return el('div',{class:'exrow'},el('code',{text:x.id}),el('span',{class:'hint',text:x.notes||'Sin notas.'}),
      ids.has(x.id)?el('span',{class:'badge b-muted',text:'Ya en el catálogo'}):
      el('button',{class:'sm',text:cand?'Agregar con '+cand:'Agregar',onclick:()=>{S.mods.push({id:x.id,enabled:false,workshop_id:cand,notes:x.notes||''});renderMods();updateBars();flashRow(S.mods.length-1);toast(x.id+' agregado desactivado al final de la lista.','info')}}));
  }));
}

/* ---------- Agregar desde Workshop ---------- */
const WS={rows:[]};
function openWorkshop(){
  $('#wsInput').value='';$('#wsInfo').textContent='';$('#wsWrap').hidden=true;WS.rows=[];wsButtons();
  $('#wsDlg').showModal();$('#wsInput').focus();
}
function wsState(r){
  const id=r.mid.trim(),out=[];
  if(r.problem)return{can:false,st:[{t:r.problem,c:'err'}]};
  const same=id&&S.mods.find(m=>(m.id||'').trim()===id);
  if(same&&normWid(same.workshop_id)===r.wid)return{can:false,st:[{t:'Ya en el catálogo',c:'muted'}]};
  if(same)out.push({t:'Mod ID ya usado por otro Workshop',c:'err',title:'En el catálogo, '+id+' tiene el Workshop ID '+(same.workshop_id||'(vacío)')+'.'});
  if(!id)out.push({t:'Escribí el Mod ID',c:'warn',title:'La descripción no lo indica. Está en el mod.info o en la página del mod.'});
  else if(id.length>150||/[;\\\r\n]/.test(id))out.push({t:'Mod ID inválido',c:'err'});
  else if(!same)out.push({t:'Mod ID sin confirmar',c:'muted',title:'Sale de la descripción. Se confirma con "Verificar archivos locales" cuando el servidor descargue el mod.'});
  if(r.multi>1)out.push({t:'Uno de '+r.multi+' Mod IDs',c:'muted',title:'Este Workshop trae varios mods: activá solo los que quieras.'});
  if(!r.tags.includes('Build 42'))out.push({t:'Sin etiqueta Build 42',c:'warn',title:'El autor no lo marcó como compatible con B42.'});
  return{can:!!id&&!out.some(x=>x.c==='err'),st:out};
}
function wsButtons(){
  const can=WS.rows.filter(r=>wsState(r).can),sel=can.filter(r=>r.checked).length;
  $('#wsAdd').disabled=!sel;$('#wsAdd').textContent=sel?'Agregar '+sel+' mod(s)':'Agregar seleccionados';
  const all=$('#wsAll');all.disabled=!can.length;all.checked=!!can.length&&sel===can.length;all.indeterminate=sel>0&&sel<can.length;
}
function wsRow(r){
  const tr=el('tr'),box=el('input',{type:'checkbox','aria-label':'Agregar '+(r.title||r.wid)});
  const badgeCell=el('div',{class:'st'});
  const refresh=()=>{const s=wsState(r);if(!s.can)r.checked=false;box.disabled=!s.can;box.checked=r.checked;badgeCell.replaceChildren(...badges(s.st));tr.className=s.can?'':'off';wsButtons()};
  box.onchange=()=>{r.checked=box.checked;wsButtons()};
  const locked=!!r.problem||wsState(r).st.some(x=>x.t==='Ya en el catálogo');
  const input=el('input',{type:'text',class:'mono',value:r.mid,placeholder:'Mod ID',spellcheck:false,disabled:locked,
    oninput:e=>{const before=wsState(r).can;r.mid=e.target.value;if(!before&&wsState(r).can)r.checked=true;refresh()}});
  tr.append(el('td',{},box),el('td',{},input),
    el('td',{class:'wstitle'},el('a',{href:'https://steamcommunity.com/sharedfiles/filedetails/?id='+r.wid,target:'_blank',rel:'noopener noreferrer',text:r.title||'(sin título)',title:'Abrir en Steam Workshop: '+r.wid}),el('small',{text:r.wid+(r.tags.length?' · '+r.tags.join(', '):'')})),
    el('td',{text:r.updated?fmtTime(r.updated):'–'}),el('td',{},badgeCell));
  refresh();
  return tr;
}
async function wsLookup(){
  const text=$('#wsInput').value.trim();if(!text)return;
  $('#wsInfo').textContent='Consultando Steam…';
  const j=await api('ws_lookup','POST',{text});
  WS.rows=j.items.flatMap(it=>{
    const ids=it.mod_ids.length?it.mod_ids:[''];
    return ids.map(mid=>({wid:it.workshop_id,mid,multi:ids.length,title:it.title,updated:it.updated,tags:it.tags,problem:it.problem,checked:ids.length===1}));
  });
  for(const r of WS.rows)if(!wsState(r).can)r.checked=false;
  $('#wsRows').replaceChildren(...WS.rows.map(wsRow));
  $('#wsWrap').hidden=!WS.rows.length;
  const cols=j.collections.map(c=>'"'+(c.title||c.workshop_id)+'" ('+c.count+' ítems)');
  const ok=WS.rows.filter(r=>wsState(r).can).length;
  $('#wsInfo').textContent=(cols.length?'Colección '+cols.join(', ')+'. ':'')+j.items.length+' mod(s) en Workshop; '+ok+' se pueden agregar.'+(j.truncated?' Se muestran los primeros 1000.':'')+(ok?' Los nuevos se agregan activos al final del orden de carga.':'');
  wsButtons();
}
function wsAdd(){
  const ids=new Set(S.mods.map(m=>(m.id||'').trim()));let added=0,repeated=0;
  for(const r of WS.rows){
    if(!r.checked||!wsState(r).can)continue;
    const id=r.mid.trim();
    if(ids.has(id)){repeated++;continue}
    S.mods.push({id,enabled:true,workshop_id:r.wid,notes:('Workshop: '+r.title).slice(0,500)});ids.add(id);added++;
  }
  $('#wsDlg').close();renderMods();updateBars();
  if(added)flashRow(S.mods.length-1);
  toast(added+' mod(s) agregados activos al final de la lista'+(repeated?' ('+repeated+' con Mod ID repetido, omitidos)':'')+'. Guardá y aplicá al INI; cuando el servidor los descargue, "Verificar archivos locales" confirma los Mod IDs.',added?'ok':'info',{timeout:10000});
}

/* ---------- Consola ---------- */
const CON={timer:null,name:null,offset:null,lines:[],partial:'',max:5000,busy:false};
function consoleStart(){if(!CON.timer){consoleTick();CON.timer=setInterval(consoleTick,1000)}}
function consoleStop(){clearInterval(CON.timer);CON.timer=null}
function sourceName(){const v=$('#logSource').value;return v&&v!=='auto'?v:(S.status.log||(S.logs[0]&&S.logs[0].name)||null)}
function lineClass(l){
  if(/^> /.test(l))return'cmd';
  if(/SERVER STARTED|Success! App/.test(l))return'ok';
  if(/\bERROR\b|SEVERE|Exception|Failed|FAILED/.test(l))return'err';
  if(/\bWARN/.test(l))return'warn';
  return'';
}
const lineEl=l=>el('div',{class:'ln '+lineClass(l),text:l||' '});
function clearConsole(){CON.lines=[];CON.partial='';$('#out').replaceChildren()}
function renderConsole(){
  const q=$('#logFind').value.trim().toLowerCase(),out=$('#out');
  const lines=q?CON.lines.filter(l=>l.toLowerCase().includes(q)):CON.lines;
  out.replaceChildren(...lines.map(lineEl));
  if(CON.partial&&(!q||CON.partial.toLowerCase().includes(q)))out.append(el('div',{class:'ln partial',text:CON.partial}));
  if(!CON.lines.length&&!CON.partial)out.append(el('div',{class:'ln dim',text:CON.empty||'(sin salida todavía)'}));
  if($('#follow').checked)out.scrollTop=out.scrollHeight;
}
function appendConsole(text){
  const parts=(CON.partial+text).replace(/\r\n/g,'\n').split('\n');
  CON.partial=parts.pop().split('\r').pop();
  const fresh=parts.map(l=>l.includes('\r')?l.split('\r').filter(Boolean).pop()||'':l);
  CON.lines.push(...fresh);
  const over=CON.lines.length-CON.max;
  if(over>0)CON.lines.splice(0,over);
  const q=$('#logFind').value.trim().toLowerCase(),out=$('#out');
  if(over>0||q||out.querySelector('.dim')){renderConsole();return}
  const old=out.querySelector('.partial');if(old)old.remove();
  out.append(...fresh.map(lineEl));
  if(CON.partial)out.append(el('div',{class:'ln partial',text:CON.partial}));
  if($('#follow').checked)out.scrollTop=out.scrollHeight;
}
function echo(line){CON.lines.push(line);const p=$('#out .partial');const n=lineEl(line);p?p.before(n):$('#out').append(n);if($('#follow').checked)$('#out').scrollTop=$('#out').scrollHeight}
async function consoleTick(){
  if(CON.busy)return;CON.busy=true;
  try{
    const name=sourceName();
    if(!name){CON.empty='No hay logs todavía. Iniciá o actualizá el servidor para ver su salida acá.';if(CON.name!==null||!$('#out').childElementCount){CON.name=null;clearConsole();renderConsole()}return}
    if(name!==CON.name){CON.name=name;CON.offset=null;clearConsole()}
    const j=await api('tail?name='+encodeURIComponent(name)+(CON.offset==null?'':'&offset='+CON.offset));
    if(j.reset&&CON.offset!=null)clearConsole();
    CON.offset=j.offset;
    CON.empty=j.missing?'El archivo todavía no existe.':'(sin salida todavía)';
    if(j.reset){if(j.truncated)CON.lines.push('… (se muestran solo los últimos 200 KB del archivo)');appendConsole(j.text);renderConsole()}
    else if(j.text)appendConsole(j.text);
  }catch(e){}finally{CON.busy=false}
}
const LOG_RE=/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}-\d+-(server|update)\.log$/;
function logLabel(x){
  const m=x.name.match(LOG_RE);
  return (m?(m[6]==='server'?'Servidor':'Actualización')+' · '+m[3]+'/'+m[2]+'/'+m[1]+' '+m[4]+':'+m[5]:x.name)+' ('+fmtSize(x.size)+')';
}
async function loadLogs(){
  try{S.logs=await api('logs')}catch(e){return}
  const sel=$('#logSource'),cur=sel.value||'auto';
  sel.replaceChildren(el('option',{value:'auto',text:'En vivo: proceso actual o último log'}),
    ...S.logs.map(x=>el('option',{value:x.name,text:logLabel(x)})),
    el('option',{value:'pz-console',text:'Zomboid · server-console.txt'}));
  sel.value=[...sel.options].some(o=>o.value===cur)?cur:'auto';
}

/* ---------- Ajustes del servidor (INI) ---------- */
async function loadIni(){S.ini=await api('ini');S.iniChanges={};renderIni();updateBars()}
function renderIni(){
  const d=S.ini;if(!d)return;
  $('#iniPath').textContent=d.ini;$('#iniMissing').hidden=d.exists;$('#iniCard').hidden=!d.exists;
  if(!d.exists)return;
  const q=$('#iniSearch').value.trim().toLowerCase(),only=$('#iniOnly').checked,common=[],rest=[];
  for(const e of d.entries){
    if(q&&!(e.key.toLowerCase().includes(q)||e.help.toLowerCase().includes(q)))continue;
    if(only&&!(e.key in S.iniChanges))continue;
    (COMMON_INI.includes(e.key)?common:rest).push(e);
  }
  common.sort((a,b)=>COMMON_INI.indexOf(a.key)-COMMON_INI.indexOf(b.key));
  $('#iniCommon').replaceChildren(...common.map(iniItem));$('#iniAll').replaceChildren(...rest.map(iniItem));
  $('#iniCommonH').hidden=!common.length;$('#iniCommon').hidden=!common.length;
  $('#iniAllH').hidden=!rest.length;$('#iniAll').hidden=!rest.length;
  $('#iniCount').textContent='('+rest.length+')';
  $('#iniEmpty').hidden=common.length+rest.length>0;
}
function iniItem(e){
  const changed=e.key in S.iniChanges,val=changed?S.iniChanges[e.key]:e.value,id='ini-'+e.key;
  const box=el('div',{class:'iniitem'+(changed?' changed':'')});
  const set=v=>{if(v===e.value)delete S.iniChanges[e.key];else S.iniChanges[e.key]=v;box.classList.toggle('changed',e.key in S.iniChanges);updateBars()};
  let ctrl,headCtrl=null;
  if(S.ini.protected.includes(e.key)){
    const n=e.value.split(';').filter(x=>x.trim()).length;
    ctrl=el('p',{class:'hint',text:n+' entradas. Se administra desde la pestaña Mods (Aplicar al INI).'});
  }else if(/^(true|false)$/i.test(e.value)){
    headCtrl=switchEl(val.toLowerCase()==='true',v=>set(v?'true':'false'),e.key);
  }else if(SECRET_RE.test(e.key)){
    const inp=el('input',{type:'password',id,value:val,autocomplete:'new-password',oninput:ev=>set(ev.target.value)});
    ctrl=el('div',{class:'secret'},inp,el('button',{class:'sm',type:'button',text:'Ver',onclick:ev=>{const show=inp.type==='password';inp.type=show?'text':'password';ev.target.textContent=show?'Ocultar':'Ver'}}));
  }else{
    ctrl=el('input',{type:'text',id,value:val,class:/^-?\d+(\.\d+)?$/.test(e.value)?'mono':'',spellcheck:false,oninput:ev=>set(ev.target.value)});
  }
  box.append(el('div',{class:'inihead'},el('label',{for:id,text:e.key,title:e.key}),headCtrl));
  if(ctrl)box.append(ctrl);
  if(e.help)box.append(el('p',{class:'help',text:e.help,title:'Clic para ver completo',onclick:ev=>ev.target.classList.toggle('open')}));
  return box;
}
async function saveIni(){
  const n=Object.keys(S.iniChanges).length;if(!n)return;
  const j=await api('ini','POST',{changes:S.iniChanges});
  toast(n+' ajuste(s) guardados en el INI.\nCopia de seguridad: '+basename(j.backup)+(S.status.running?'\nReiniciá el servidor para aplicarlos.':''));
  await loadIni();loadOverview().catch(()=>{});
}

/* ---------- Jugadores y whitelist ---------- */
let PL_TIMER=null;
function playersTimer(on){clearInterval(PL_TIMER);PL_TIMER=null;if(on)PL_TIMER=setInterval(()=>{if($('#plAuto').checked&&S.status.can_command)loadPlayers(true)},30000)}
async function loadPlayersTab(){await Promise.all([loadPlayers(true),loadWhitelist()])}
async function loadPlayers(quiet){
  const box=$('#plList'),can=!!S.status.can_command;
  $('#plOffline').hidden=can;
  if(!can){$('#plCount').textContent='–';$('#plChecked').textContent='';$('#navPlayers').textContent='';box.replaceChildren(el('p',{class:'empty',text:'Iniciá el servidor para ver quién está conectado.'}));return}
  let p;
  try{p=await api('players','POST',{})}catch(e){if(!quiet)toast(e.message,'err');box.replaceChildren(el('p',{class:'hint',text:e.message}));return}
  $('#plCount').textContent=p.count;$('#navPlayers').textContent=p.count||'';
  $('#plChecked').textContent='Consultado a las '+new Date(p.checked*1000).toLocaleTimeString('es-AR');
  if(!p.count){box.replaceChildren(el('p',{class:'empty',text:'No hay nadie conectado.'}));return}
  box.replaceChildren(...p.players.map(playerRow),p.players.length<p.count?el('p',{class:'hint',text:'No se pudieron leer todos los nombres; revisá la Consola.'}):null);
}
function roleSelect(current,onchange,disabled){
  const sel=el('select',{disabled,title:'Rol en el servidor (setaccesslevel)','aria-label':'Rol',onchange:e=>onchange(e.target.value)},...ROLES.map(r=>el('option',{value:r,text:ROLE_LABEL[r]})));
  sel.value=ROLES.includes(current)?current:'none';
  return sel;
}
async function playerAct(action,username,extra={}){
  if(action==='kick'||action==='ban'){
    const verb=action==='ban'?'Banear':'Expulsar';
    const reason=await ask(verb+' a '+username,action==='ban'?'No va a poder volver a entrar hasta que lo desbanees.':'Se desconecta del servidor; puede volver a entrar.',[{k:'ok',t:verb,cls:'stop'},{k:null,t:'Cancelar'}],'Motivo (opcional)');
    if(reason===null)return;
    extra.reason=reason;
  }
  if(action==='whitelist_remove'&&!await confirmBox('Quitar de la whitelist','Se quita la cuenta de '+username+' de la whitelist del servidor.','Quitar','stop'))return;
  const j=await api('player','POST',{action,username,...extra});
  toast('Comando '+j.sent+' enviado para '+username+'.');
  setTimeout(()=>{loadWhitelist().catch(()=>{});if(action==='kick'||action==='ban')loadPlayers(true)},1500);
}
function playerRow(name){
  const dis=!S.status.can_command,user=((S.wl&&S.wl.users)||[]).find(u=>u.username===name);
  return el('div',{class:'plrow'},el('b',{text:name,title:name}),
    user?null:el('button',{class:'sm',text:'+ Whitelist',disabled:dis,title:'Agregar a la whitelist (addusertowhitelist)',onclick:e=>run(()=>playerAct('whitelist_connected',name),e.currentTarget)}),
    roleSelect(user?user.role:'none',v=>run(()=>playerAct('role',name,{role:v})),dis),
    el('button',{class:'sm',text:'Expulsar',disabled:dis,onclick:e=>run(()=>playerAct('kick',name),e.currentTarget)}),
    el('button',{class:'sm danger',text:'Banear',disabled:dis,onclick:e=>run(()=>playerAct('ban',name),e.currentTarget)}));
}
async function loadWhitelist(){S.wl=await api('whitelist');renderAccess();renderWhitelist()}
function renderWhitelist(){
  const w=S.wl;if(!w)return;
  const can=!!S.status.can_command,q=$('#wlSearch').value.trim().toLowerCase();
  $('#wlDb').textContent=w.exists?'Base: '+basename(w.db):'Todavía no existe '+basename(w.db)+' (se crea al iniciar el servidor).';
  $('#wlDb').title=w.db;
  $('#wlCount').textContent=w.users.length;
  $$('#wlAdd input,#wlAddBtn').forEach(x=>x.disabled=!can);
  const rows=w.users.filter(u=>!q||(u.username||'').toLowerCase().includes(q)||(u.steamid||'').includes(q));
  $('#wlRows').replaceChildren(...rows.map(u=>el('tr',{},
    el('td',{},el('b',{text:u.username||'(sin nombre)'})),
    el('td',{},u.role==='banned'?el('span',{class:'badge b-err',text:'Baneado'}):roleSelect(u.role,v=>run(()=>playerAct('role',u.username,{role:v})),!can)),
    el('td',{class:'hint',text:u.last||'nunca'}),
    el('td',{class:'mono hint',text:u.steamid||'–'}),
    el('td',{},el('span',{class:'badge '+(u.banned?'b-err':'b-ok'),text:u.banned?'Baneado':'Habilitado'})),
    el('td',{},el('div',{class:'actions'},
      u.banned?el('button',{class:'sm',text:'Desbanear',disabled:!can,onclick:e=>run(()=>playerAct('unban',u.username),e.currentTarget)})
              :el('button',{class:'sm danger',text:'Banear',disabled:!can,onclick:e=>run(()=>playerAct('ban',u.username),e.currentTarget)}),
      el('button',{class:'icon',text:'✕',title:'Quitar de la whitelist','aria-label':'Quitar '+u.username,disabled:!can,onclick:e=>run(()=>playerAct('whitelist_remove',u.username),e.currentTarget)}))))));
  $('#wlEmpty').hidden=rows.length>0;
  const bans=[...w.banned_ips.map(b=>'IP '+b.ip+(b.username?' ('+b.username+')':'')+(b.reason?' · '+b.reason:'')),...w.banned_ids.map(b=>'SteamID '+b.steamid+(b.reason?' · '+b.reason:''))];
  $('#wlBans').replaceChildren(...(bans.length?[el('h3',{text:'Baneos por IP o SteamID ('+bans.length+')'}),el('div',{class:'difflist'},...bans.map(t=>el('span',{class:'badge b-err',text:t})))]:[]));
}
const settingRow=(title,hint,ctrl)=>el('div',{class:'setrow'},el('div',{},el('b',{text:title}),hint?el('p',{class:'hint',text:hint}):null),ctrl);
const isTrue=v=>String(v).toLowerCase()==='true';
function iniSwitch(key,value,invert,msgOn,msgOff,after){
  const on=isTrue(value);
  return switchEl(invert?!on:on,v=>run(async()=>{
    await api('ini','POST',{changes:{[key]:(invert?!v:v)?'true':'false'}});S.ini=null;
    toast((v?msgOn:msgOff)+(S.status.running?' Reiniciá el servidor para aplicarlo.':''));
    await after();
  }),key);
}
function renderAccess(){
  const a=S.wl.access,box=$('#accessBox');
  if(a.Open==null){box.replaceChildren(el('p',{class:'hint',text:'Todavía no existe el INI del servidor.'}));return}
  box.replaceChildren(
    settingRow('Solo cuentas de la whitelist','Nadie puede crear una cuenta al entrar: las agregás vos (Open=false).',iniSwitch('Open',a.Open,true,'Ahora solo entran cuentas de la whitelist.','Ahora cualquiera puede crear una cuenta al entrar.',loadWhitelist)),
    settingRow('Contraseña del servidor',a.password?'Configurada.':'Sin contraseña: con el servidor abierto, cualquiera que tenga tu IP puede entrar.',el('button',{class:'sm',type:'button',text:a.password?'Cambiar':'Configurar',onclick:()=>{$('#iniSearch').value='Password';showTab('ini');renderIni()}})),
    settingRow('Visible en la lista pública','Aparece en el navegador de servidores del juego (Public).',iniSwitch('Public',a.Public,false,'El servidor aparecerá en la lista pública.','El servidor ya no aparecerá en la lista pública.',loadWhitelist)),
    settingRow('Máximo de jugadores',null,el('span',{class:'badge b-muted',text:a.MaxPlayers||'–'})));
}

/* ---------- Red, puertos y firewall ---------- */
async function loadNetwork(){$('#secSummary').textContent='Consultando puertos y firewall…';S.net=await api('network');renderNetwork()}
function renderNetwork(){
  const n=S.net;if(!n)return;
  const e=n.exposure,udp=n.ports.filter(p=>p.proto==='UDP').map(p=>p.port),ip=n.lan_ip||'tu IP local';
  const foreign=n.ports.filter(p=>p.pid&&!/java/i.test(p.process||''));
  $('#portRows').replaceChildren(...n.ports.map(p=>el('tr',{},
    el('td',{class:'mono',text:String(p.port)}),el('td',{text:p.proto}),el('td',{class:'hint',text:p.use}),
    el('td',{},p.pid?el('span',{class:'badge '+(/java/i.test(p.process||'')?'b-ok':'b-warn'),text:'En uso: '+(p.process||'PID '+p.pid),title:'PID '+p.pid}):el('span',{class:'badge b-muted',text:'Libre'})),
    el('td',{},p.proto==='TCP'?el('span',{class:'badge b-warn',text:'No exponer'}):el('span',{class:'badge '+(p.firewall?'b-ok':'b-warn'),text:p.firewall?'✓ Permitido':'Sin regla'})))));
  const prof=n.profiles.map(p=>p.name+' ('+({Private:'privada',Public:'pública',DomainAuthenticated:'dominio'}[p.category]||p.category)+')').join(', ');
  $('#fwInfo').textContent=n.fw_error||('Red actual: '+(prof||'desconocida')+'.'+(n.java_exists?'':' No se encontró java.exe del servidor.'));
  $('#fwRules').replaceChildren(...(n.rules.length?n.rules.map(r=>el('div',{class:'plrow'},el('b',{text:r.name}),
    el('span',{class:'hint',text:r.protocol+' '+r.ports+' · '+(r.remote==='LocalSubnet'?'solo red local':'desde Internet')+' · '+r.profile}),
    el('span',{class:'badge '+(r.enabled==='True'?'b-ok':'b-muted'),text:r.enabled==='True'?'Activa':'Desactivada'}))):[el('p',{class:'hint',text:'Todavía no hay reglas del panel.'})]));
  const othersOn=n.other.filter(r=>r.enabled==='True'&&r.action==='Allow');
  $('#fwOthers').replaceChildren(...(othersOn.length?[el('div',{class:'banner warn'},el('span',{text:othersOn.length+' regla(s) más permiten el java.exe del servidor en cualquier puerto (Windows las crea al primer inicio).'}),el('button',{class:'sm','data-fw':'disable_others',text:'Deshabilitarlas'}))]:[]));
  $('#exposeBox').replaceChildren(
    settingRow('IP de esta PC en tu red',null,el('code',{text:n.lan_ip||'desconocida'})),
    el('div',{},el('b',{text:'Redirección de puertos en el router'}),el('p',{class:'hint',text:'Creá una regla de port forwarding para estos puertos UDP hacia '+ip+' (conviene fijar esa IP en el router):'}),el('p',{},el('code',{text:'UDP '+(udp.join(', ')||'–')+' → '+ip}))),
    settingRow('Abrir puertos automáticamente (UPnP)','Cómodo, pero abre puertos en el router sin control. Recomendado: desactivado y redirección manual.',iniSwitch('UPnP',e.UPnP,false,'UPnP activado.','UPnP desactivado.',loadNetwork)),
    settingRow('Visible en la lista pública','Si está desactivado, solo entra quien conozca tu IP.',iniSwitch('Public',e.Public,false,'El servidor aparecerá en la lista pública.','El servidor ya no aparecerá en la lista pública.',loadNetwork)),
    el('p',{class:'hint',text:'Los jugadores se conectan a tu IP pública (la ves en el router). Si tu proveedor usa CG-NAT, la redirección no funciona: pedí una IP pública o usá una VPN tipo Tailscale o ZeroTier.'}));
  const checks=[
    [n.mode==='app','App sin puertos abiertos',n.mode==='app'?'La interfaz no escucha en ningún puerto de red.':'Modo navegador: el panel escucha en 127.0.0.1. Usá PZManager para evitarlo.'],
    [!n.is_admin,'Ejecutando sin administrador',n.is_admin?'Cerrá la app y abrila con tu usuario normal.':'La app corre con permisos de usuario estándar.'],
    [!n.ini_exists||!isTrue(e.Open)||e.password,'Acceso controlado',!isTrue(e.Open)?'Solo entran cuentas de la whitelist.':e.password?'Servidor abierto, pero con contraseña.':'Cualquiera con tu IP puede crear una cuenta: activá la whitelist o una contraseña.'],
    [!e.rcon,'RCON desactivado',e.rcon?'RCON está activo en TCP '+(e.RCONPort||'27015')+': no lo redirijas en el router.':'No hay administración remota expuesta.'],
    [!isTrue(e.UPnP),'UPnP desactivado',isTrue(e.UPnP)?'El servidor puede abrir puertos en tu router por su cuenta.':'Los puertos del router los controlás vos.'],
    [n.rules.length>0&&!othersOn.length,'Firewall limitado al servidor',!n.rules.length?'Todavía no hay reglas del panel para los puertos del juego.':othersOn.length?'Hay reglas amplias para java.exe además de las del panel.':'Solo se permiten los puertos UDP del juego para java.exe.'],
    [!foreign.length,'Puertos libres',foreign.length?'Otro programa ocupa el puerto '+foreign.map(p=>p.port).join(', ')+'.':'Ningún otro programa ocupa los puertos del servidor.'],
  ];
  const bad=checks.filter(c=>!c[0]).length;
  $('#secSummary').textContent=bad?bad+' punto(s) para revisar.':'Todo en orden.';
  $('#navNet').textContent=bad||'';
  $('#secChecks').replaceChildren(...checks.map(([ok,t,h])=>el('li',{class:ok?'ok':'todo'},el('span',{class:'ck',text:ok?'✓':'!'}),el('div',{},el('b',{text:t}),el('p',{class:'hint',text:h})))));
}
async function firewallAction(action){
  const text={lan:'Se permite el tráfico UDP del juego hacia el java.exe del servidor solo desde tu red local.',
    internet:'Se permite el tráfico UDP del juego hacia el java.exe del servidor desde cualquier IP (necesario para jugar por Internet). Además tenés que redirigir los puertos en el router.',
    remove:'Se quitan las reglas creadas por el panel.',
    disable_others:'Se deshabilitan (no se borran) las otras reglas que permiten el java.exe del servidor en cualquier puerto.'}[action];
  if(!await confirmBox('Firewall de Windows',text+'\n\nWindows va a pedir permiso de administrador solo para este cambio.','Continuar'))return;
  toast('Esperando el permiso de administrador de Windows…','info');
  await api('firewall','POST',{action});
  toast('Firewall actualizado.');
  await loadNetwork();
}

/* ---------- Cierre de la ventana propia ---------- */
window.requestClose=async function(){
  const st=S.status||{},lines=[];
  if(st.running&&st.kind==='server')lines.push('El servidor está en ejecución: se guarda el mundo y se detiene antes de cerrar.');
  if(st.running&&st.kind==='update')lines.push('Se cancela la actualización en curso.');
  if(modsDirty()||Object.keys(S.iniChanges).length)lines.push('Hay cambios sin guardar que se van a perder.');
  const k=await ask('Cerrar PZ Dedicated Manager',lines.join('\n')||'¿Cerrar la app?',[{k:'ok',t:st.running&&st.kind==='server'?'Guardar, detener y cerrar':'Cerrar',cls:'stop'},{k:null,t:'Cancelar'}]);
  if(!k)return;
  $('#closing').hidden=false;
  try{await api('quit_app','POST',{})}catch(e){$('#closing').hidden=true;toast(e.message,'err')}
};
window.closeFailed=function(){$('#closing').hidden=true;toast('El servidor no terminó de cerrarse a tiempo. Revisá la Consola antes de cerrar la app.','err')};

/* ---------- Configuración ---------- */
const SET_KEYS=['server_name','server_dir','steamcmd_path','pz_home','branch'];
async function loadSettings(){
  const s=S.settings=await api('settings');
  for(const k of SET_KEYS)$('#s-'+k).value=s[k]||'';
  $('#s-strict').checked=!!s.strict;$('#s-ram_gb').value=s.ram_gb||'';
  $('#s-extra').value=(s.extra_workshop_paths||[]).join('\n');
  $('#hdrName').textContent=s.server_name;
}
async function saveSettings(){
  const body={...S.settings};
  for(const k of SET_KEYS)body[k]=$('#s-'+k).value.trim();
  body.strict=$('#s-strict').checked;body.ram_gb=+$('#s-ram_gb').value||0;
  body.extra_workshop_paths=$('#s-extra').value.split('\n').map(x=>x.trim()).filter(Boolean);
  S.settings=await api('settings','POST',body);
  toast('Configuración guardada.');
  await loadSettings();await loadOverview();S.ini=null;S.scan=null;
}

/* ---------- Barras de guardado ---------- */
function updateBars(){
  const md=modsDirty(),n=Object.keys(S.iniChanges).length;
  $('#modsBar').hidden=!(md&&S.tab==='mods');
  $('#iniBar').hidden=!(n&&S.tab==='ini');
  $('#iniBarText').textContent=n+(n===1?' ajuste modificado':' ajustes modificados');
  $('#navIni').textContent=n||'';
  if(md){const nm=$('#navMods');nm.textContent='\u25CF';nm.className='count dirty';nm.title='Cambios sin guardar'}
  else refreshCounts();
  // La ventana propia pregunta antes de cerrarse si hay cambios sin guardar.
  const dirty=md||n>0;
  if(APP_MODE&&dirty!==S.lastDirty){S.lastDirty=dirty;api('ui_state','POST',{dirty}).catch(()=>{})}
}

/* ---------- Eventos ---------- */
document.addEventListener('click',e=>{
  const t=e.target.closest('button,[data-goto]');if(!t||t.disabled)return;
  if(t.dataset.tab)return showTab(t.dataset.tab);
  if(t.dataset.goto)return showTab(t.dataset.goto);
  if(t.dataset.act)return run(()=>ACTIONS[t.dataset.act](),t);
  if(t.dataset.cmd)return run(()=>sendCmd(t.dataset.cmd),t);
  if(t.dataset.open)return run(()=>api('open','POST',{target:t.dataset.open}),t);
  if(t.dataset.fw)return run(()=>firewallAction(t.dataset.fw),t);
});
$('#reloadPage').onclick=()=>location.reload();
$('#modSearch').addEventListener('input',renderMods);
$$('#modFilter button').forEach(b=>b.addEventListener('click',()=>{S.filter=b.dataset.f;$$('#modFilter button').forEach(x=>x.classList.toggle('on',x===b));renderMods()}));
const bulk=v=>{for(const tr of $('#modRows').children)S.mods[+tr.dataset.i].enabled=v;renderMods();updateBars()};
$('#bulkOn').onclick=()=>bulk(true);$('#bulkOff').onclick=()=>bulk(false);
$('#btnScan').onclick=e=>run(()=>scanLocal(false),e.currentTarget);
$('#btnWsOpen').onclick=openWorkshop;
$('#wsForm').onsubmit=e=>{e.preventDefault();run(wsLookup,$('#wsFind'))};
$('#wsAll').onchange=e=>{for(const r of WS.rows)if(wsState(r).can)r.checked=e.target.checked;$$('#wsRows input[type=checkbox]').forEach(b=>{if(!b.disabled)b.checked=e.target.checked});wsButtons()};
$('#wsAdd').onclick=wsAdd;
$('#wsClose').onclick=()=>$('#wsDlg').close();
$('#btnDetect').onclick=e=>run(detectIds,e.currentTarget);
$('#btnExport').onclick=e=>run(exportMods,e.currentTarget);
$('#btnImport').onclick=e=>APP_MODE?run(()=>importMods(null),e.currentTarget):$('#importFile').click();
$('#importFile').onchange=e=>{const f=e.target.files[0];e.target.value='';if(f)run(()=>importMods(f))};
$('#addForm').onsubmit=e=>{
  e.preventDefault();
  const id=$('#newId').value.trim(),wid=normWid($('#newWid').value);
  if(!id)return toast('Escribí el Mod ID (aparece como id= en el mod.info).','err');
  if(/[;\\]/.test(id))return toast('El Mod ID no puede contener ; ni \\.','err');
  if(S.mods.some(m=>m.id===id))return toast('Ese Mod ID ya está en el catálogo.','err');
  if(wid&&!/^\d+$/.test(wid))return toast('Workshop ID inválido: pegá el número o la URL de Steam.','err');
  S.mods.push({id,enabled:true,workshop_id:wid,notes:''});
  $('#newId').value='';$('#newWid').value='';$('#modSearch').value='';
  S.filter='all';$$('#modFilter button').forEach(x=>x.classList.toggle('on',x.dataset.f==='all'));
  renderMods();updateBars();flashRow(S.mods.length-1);$('#newId').focus();
};
$('#modsDiscard').onclick=()=>{S.mods=JSON.parse(S.saved);renderMods();updateBars();toast('Cambios descartados.','info')};
$('#modsSave').onclick=e=>run(()=>saveMods(false),e.currentTarget);
$('#modsSaveApply').onclick=e=>run(async()=>{await saveMods(true);await ACTIONS.apply()},e.currentTarget);
$('#logSource').onchange=()=>{CON.name=null;consoleTick()};
$('#btnLogsRefresh').onclick=loadLogs;
$('#logFind').addEventListener('input',renderConsole);
$('#wrap').onchange=e=>$('#out').classList.toggle('wrap',e.target.checked);
$('#btnClearView').onclick=()=>{CON.lines=[];CON.partial='';$('#out').replaceChildren()};
$('#out').addEventListener('scroll',()=>{const o=$('#out');$('#follow').checked=o.scrollTop+o.clientHeight>=o.scrollHeight-24});
$('#cmdForm').onsubmit=e=>{e.preventDefault();const c=$('#cmd');const v=c.value;c.value='';run(()=>sendCmd(v)).then(()=>c.focus())};
$('#cmd').addEventListener('keydown',e=>{
  if(e.key==='ArrowUp'&&S.hpos>0){e.preventDefault();S.hpos--;e.target.value=S.hist[S.hpos]}
  else if(e.key==='ArrowDown'){e.preventDefault();S.hpos=Math.min(S.hist.length,S.hpos+1);e.target.value=S.hist[S.hpos]||''}
});
const broadcast=e=>run(async()=>{
  const msg=await ask('Mensaje a todos','Se muestra a todos los jugadores conectados.',[{k:'ok',t:'Enviar',cls:'primary'},{k:null,t:'Cancelar'}],'Ej.: Reinicio en 5 minutos');
  if(msg&&msg.trim())await sendCmd('servermsg "'+msg.trim().replace(/"/g,"'")+'"');
},e.currentTarget);
$('#btnBroadcast').onclick=broadcast;$('#btnBroadcast2').onclick=broadcast;
$('#plRefresh').onclick=e=>run(loadPlayersTab,e.currentTarget);
$('#wlSearch').addEventListener('input',renderWhitelist);
$('#wlAdd').onsubmit=e=>{
  e.preventDefault();
  const u=$('#wlUser').value.trim(),p=$('#wlPass').value;
  if(!u||!p)return toast('Completá usuario y contraseña.','err');
  run(async()=>{await playerAct('whitelist_add',u,{password:p});$('#wlUser').value='';$('#wlPass').value=''},$('#wlAddBtn'));
};
$('#netRefresh').onclick=e=>run(loadNetwork,e.currentTarget);
$('#iniSearch').addEventListener('input',renderIni);$('#iniOnly').onchange=renderIni;
$('#iniReload').onclick=e=>run(async()=>{if(Object.keys(S.iniChanges).length&&!await confirmBox('Descartar cambios','Se perderán los ajustes que no guardaste.','Recargar'))return;await loadIni()},e.currentTarget);
$('#iniDiscard').onclick=()=>{S.iniChanges={};renderIni();updateBars()};
$('#iniSave').onclick=e=>run(saveIni,e.currentTarget);
$('#setSave').onclick=e=>run(saveSettings,e.currentTarget);
$$('nav [data-tab]').forEach(b=>b.setAttribute('type','button'));
document.addEventListener('keydown',e=>{
  const mod=e.ctrlKey||e.metaKey;
  if(mod&&e.key.toLowerCase()==='s'){
    e.preventDefault();
    if(S.tab==='mods'&&modsDirty())run(()=>saveMods(false));
    else if(S.tab==='ini')run(saveIni);
    else if(S.tab==='settings')run(saveSettings);
  }
  if(mod&&e.key.toLowerCase()==='k'){
    const box={mods:'#modSearch',console:'#logFind',ini:'#iniSearch',players:'#wlSearch'}[S.tab];
    if(box){e.preventDefault();$(box).focus();$(box).select()}
  }
});
if(!APP_MODE)window.addEventListener('beforeunload',e=>{if(modsDirty()||Object.keys(S.iniChanges).length){e.preventDefault();e.returnValue=''}});
$('#navMode').textContent=APP_MODE?'App local · sin puertos abiertos':'Navegador · solo 127.0.0.1';
if(APP_MODE)$('#closeNote').textContent='Si cerrás la app con el servidor encendido, primero se guarda el mundo y se detiene el servidor.';
window.addEventListener('hashchange',()=>showTab(location.hash.slice(1)));
setInterval(tick,1000);

(async()=>{
  showTab(location.hash.slice(1)||'home');
  try{
    await Promise.all([loadSettings(),loadMods(),loadOverview(),api('excluded').then(x=>{S.excluded=x;renderExcluded()}),loadLogs()]);
  }catch(e){toast(e.message,'err')}
  setTimeout(pollStatus,2000);
})();
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    """API HTTP local, con validación de host, origen y token contra solicitudes externas."""
    def respond(self,obj,status=200):
        payload=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def local_host(self):
        """Rechazar nombres de host ajenos (protección contra DNS rebinding)."""
        return self.headers.get('Host','') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def do_GET(self):
        try:
            if not self.local_host(): return self.respond({'error':'Host no autorizado'},403)
            p=urlparse(self.path)
            if p.path=='/':
                page=PAGE.replace('__TOKEN__',TOKEN).replace('__MODE__','web').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header('Content-Length',str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif p.path.startswith('/api/'):
                # También las lecturas exigen el token: una web ajena no puede dispararlas con <img> o enlaces.
                if self.headers.get('X-PZ-Token')!=TOKEN: raise ValueError('Token inválido')
                self.respond(dispatch('GET',p.path[5:]+('?'+p.query if p.query else '')))
            else: self.respond({'error':'Ruta no encontrada'},404)
        except NotFound as e: self.respond({'error':str(e)},404)
        except Exception as e: self.respond({'error':str(e)},400)
    def do_POST(self):
        try:
            if not self.local_host(): raise ValueError('Host no autorizado')
            origin=self.headers.get('Origin','')
            if origin and origin not in (f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'): raise ValueError('Origen no autorizado')
            if self.headers.get('X-PZ-Token')!=TOKEN: raise ValueError('Token inválido')
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>2_000_000: raise ValueError('Tamaño inválido')
            data=json.loads(self.rfile.read(size) or b'{}')
            path=urlparse(self.path).path
            if not path.startswith('/api/'): raise NotFound('Ruta no encontrada')
            self.respond(dispatch('POST',path[5:],data))
        except NotFound as e: self.respond({'error':str(e)},404)
        except Exception as e: self.respond({'error':str(e)},400)
    def log_message(self,fmt,*args):
        """Evitar registrar tokens o datos personales del navegador."""
        pass

def shutdown_server(timeout=120):
    """Al cerrar el panel con Ctrl+C, pedir al servidor que guarde y se cierre antes de salir."""
    if not (PROCESS and PROCESS.poll() is None and PROC_INFO.get('kind') == 'server'):
        return
    print('Deteniendo el servidor (quit) para guardar el mundo... Ctrl+C otra vez para salir sin esperar.', flush=True)
    try:
        send_command('quit')
        PROCESS.wait(timeout)
    except ValueError:
        pass
    except subprocess.TimeoutExpired:
        print('El servidor no terminó a tiempo y sigue en ejecución.', flush=True)

def forward_input():
    """Reenviar al servidor lo que se escriba en esta ventana (comandos o contraseña admin)."""
    for line in sys.stdin:
        if line.strip():
            try:
                send_command(line.strip())
            except ValueError as e:
                print(e, flush=True)

def follow(log, proc):
    """Mostrar el log en vivo hasta que termine el proceso; Ctrl+C envía quit para guardar."""
    pos, stopping = 0, False
    while True:
        try:
            with log.open('rb') as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                sys.stdout.write(chunk.decode('utf-8', errors='replace'))
                sys.stdout.flush()
            if proc.poll() is not None and not chunk:
                return proc.returncode
            time.sleep(0.5)
        except KeyboardInterrupt:
            if stopping:
                raise
            stopping = True
            print('\nCtrl+C: enviando quit (el servidor guarda y se cierra). Ctrl+C otra vez para salir sin esperar.', flush=True)
            try:
                send_command('quit')
            except ValueError:
                pass

def message_box(text):
    """Aviso nativo de Windows (la app no tiene consola donde imprimir)."""
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, text, 'PZ Dedicated Manager', 0x40)
    else:
        print(text)

def single_instance():
    """Evitar dos ventanas controlando el mismo servidor (mutex con nombre de Windows)."""
    if os.name != 'nt':
        return True
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    APP['mutex'] = kernel32.CreateMutexW(None, False, 'Local\\PZDedicatedManager')
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

def on_closing():
    """Al cerrar la ventana: si hay un proceso en curso o cambios sin guardar, preguntar primero en la página."""
    if APP['allow_close']:
        return True
    if (PROCESS and PROCESS.poll() is None) or APP['dirty']:
        threading.Thread(target=APP['window'].evaluate_js, args=('requestClose()',), daemon=True).start()
        return False
    return True

def run_app():
    """Abrir la ventana propia (pywebview + WebView2): sin servidor HTTP ni puertos abiertos."""
    try:
        import webview
    except ImportError:
        message_box('Falta el paquete pywebview.\n\nInstalalo con:\n    py -3 -m pip install pywebview\n\nMientras tanto podés usar el modo navegador:\n    py -3 manager.py web')
        return 1
    if not single_instance():
        message_box('PZ Dedicated Manager ya está abierto.')
        return 0
    APP['active'] = True
    # La página es local y los enlaces externos se abren en el navegador: sin tráfico de fondo del motor WebView2.
    os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
                          '--disable-background-networking --disable-component-update --disable-domain-reliability '
                          '--no-pings --disable-features=msSmartScreenProtection,ElasticOverscroll')
    page = PAGE.replace('__TOKEN__', '').replace('__MODE__', 'app')
    window = webview.create_window('PZ Dedicated Manager', html=page, js_api=Bridge(), width=1360, height=880,
                                   min_size=(960, 620), background_color='#0a0f17', text_select=True)
    APP['window'] = window
    window.events.closing += on_closing
    icon = ROOT / 'pz.ico'
    # private_mode: WebView2 no guarda cookies ni almacenamiento entre sesiones.
    webview.start(private_mode=True, icon=str(icon) if icon.is_file() else None)
    return 0

def main():
    """Abrir la ventana propia (por defecto), el panel en el navegador o ejecutar comandos CLI."""
    global PORT
    ensure()
    parser=argparse.ArgumentParser(description='Administrador local Project Zomboid')
    parser.add_argument('command',nargs='?',choices=['app','web','validate','detect','apply','start','update'],default='app')
    parser.add_argument('--no-browser',action='store_true',help='No abrir el navegador al iniciar el panel')
    parser.add_argument('--port',type=int,help='Puerto del panel (por defecto, el de data/settings.json)')
    args=parser.parse_args()
    if args.command=='app':
        raise SystemExit(run_app())
    if args.command=='web':
        PORT=args.port or int(settings()['port'])
        url=f'http://127.0.0.1:{PORT}'
        try:
            httpd=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
        except OSError:
            print(f'El puerto {PORT} ya está en uso: el panel probablemente ya está abierto. Abriendo {url}')
            if not args.no_browser:
                webbrowser.open(url)
            return
        print(f'Panel local: {url}')
        print('Dejá esta ventana abierta mientras uses el panel. Ctrl+C para cerrarlo (si el servidor está corriendo, primero lo guarda y lo detiene).', flush=True)
        if not args.no_browser:
            threading.Timer(0.6, webbrowser.open, (url,)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nCerrando el panel...', flush=True)
            try:
                shutdown_server()
            except KeyboardInterrupt:
                pass
        finally:
            httpd.server_close()
    else:
        commands={'validate':validate,'detect':autodetect,'apply':apply_config,'start':lambda:launch('server'),'update':lambda:launch('update')}
        result = commands[args.command]()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.command == 'start':
            print('Servidor iniciado. Podés escribir comandos acá (save, players, quit...). Ctrl+C lo guarda y lo detiene.', flush=True)
            threading.Thread(target=forward_input, daemon=True).start()
            code = follow(Path(result['log']), PROCESS)
            print('\nEl servidor terminó. Código:', code)
        if args.command == 'update':
            print('Esperando a SteamCMD. El registro queda en:', result['log'], flush=True)
            code = PROCESS.wait()
            log = Path(result['log'])
            tail = log.read_text(encoding='utf-8',errors='replace')[-7000:] if log.exists() else ''
            print(tail)
            if code != 0 or 'Success! App' not in tail:
                raise SystemExit('SteamCMD no confirmó una instalación correcta. Revisá el log. Código: '+str(code))
            print('SteamCMD finalizó correctamente. Verificá la versión de Build 42 antes de aplicar mods.')

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        # El .exe (y pyw) no tienen consola: sin este aviso un error de arranque cerraría la app en silencio.
        if FROZEN or sys.stdout is None:
            message_box(f'No se pudo iniciar PZ Dedicated Manager:\n\n{e}\n\nCarpeta: {ROOT}')
            # Salir sin relanzar: PyInstaller mostraría además su propio diálogo con el traceback.
            raise SystemExit(1)
        raise
