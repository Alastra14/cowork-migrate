#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cowork-migrate — Migrador de proyectos de Claude Cowork entre Teams (misma Mac).

Aprovecha que Cowork guarda todo localmente (JSON + archivos) bajo:
  ~/Library/Application Support/Claude/local-agent-mode-sessions/{accountId}/{orgId}/

Un proyecto = un "space" (entrada en spaces.json) + su carpeta física + su memoria.
Este programa copia un proyecto de un Team (org) a otro de forma SEGURA y REVERSIBLE:
backups con timestamp, escritura atómica y rollback.

⚠️  No es una función oficial de Anthropic. Cierra la app Claude antes de migrar.
    Solo usa la biblioteca estándar de Python (>=3.9). macOS y Windows.

Subcomandos:
  list-teams                         Lista los Teams (orgs) detectados en la Mac.
  list-projects --team T             Lista los proyectos de un Team.
  inspect --team T --project P       Pre-vuelo read-only de un proyecto.
  migrate --from S --to D --project P [--dry-run]   Migra un proyecto S -> D.
  rollback --manifest path           Deshace una migración desde su manifest.
  verify --team T --project P        Verifica integridad de un proyecto.

Selector de Team (T/S/D): orgId, prefijo de orgId, índice de `list-teams`, o nombre.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------------

def default_base():
    """Ruta del almacén local de Claude según el sistema operativo."""
    if sys.platform == "darwin":                       # macOS
        return Path.home() / "Library" / "Application Support" / "Claude"
    if os.name == "nt":                                # Windows
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) / "Claude") if appdata else \
            Path.home() / "AppData" / "Roaming" / "Claude"
    return Path.home() / ".config" / "Claude"          # Linux (fallback)

DEFAULT_BASE = default_base()
SESSIONS_DIRNAME = "local-agent-mode-sessions"
ACTIVE_ACCOUNT_FILE = "cowork-enabled-cli-ops.json"
LOGS_DIRNAME = "cowork-migrate-logs"

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Archivos/dirs que NUNCA se copian ni se loguean (seguridad).
DENYLIST_NAMES = {".audit-key", "audit.jsonl", "config.json",
                  ACTIVE_ACCOUNT_FILE, ".DS_Store"}
SECRET_KEY_RE = re.compile(r"token|secret|authoriz|api[-_]?key|password|cookie|bearer",
                           re.IGNORECASE)

# ----------------------------------------------------------------------------
# Salida / colores
# ----------------------------------------------------------------------------

class C:
    R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GRN = "\033[32m"; YLW = "\033[33m"
    BLU = "\033[34m"; CYA = "\033[36m"

_USE_COLOR = sys.stdout.isatty()
def _c(s, color):
    return f"{color}{s}{C.R}" if _USE_COLOR else s

def info(msg):  print(msg)
def ok(msg):    print(_c("✓ ", C.GRN) + msg)
def warn(msg):  print(_c("⚠ ", C.YLW) + msg)
def err(msg):   print(_c("✗ ", C.RED) + msg, file=sys.stderr)
def head(msg):  print("\n" + _c(msg, C.B + C.CYA))

class MigrateError(Exception):
    pass

# ----------------------------------------------------------------------------
# Utilidades generales
# ----------------------------------------------------------------------------

def now_stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

def is_uuid(name):
    return bool(UUID_RE.match(name))

def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0

def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total

def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def redact(obj):
    """Devuelve una copia con valores de claves sensibles enmascarados."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and SECRET_KEY_RE.search(k):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj

def sha256_file(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

# ----------------------------------------------------------------------------
# Manifest (auditoría + rollback)
# ----------------------------------------------------------------------------

class Manifest:
    """Registra cada acción para poder deshacerla. Una accion es:
       {"op": "create", "path": ...}            -> undo: borrar path
       {"op": "backup", "path": ..., "backup": ...} -> undo: restaurar backup
    """
    def __init__(self, meta):
        self.meta = meta
        self.actions = []

    def record_create(self, path):
        self.actions.append({"op": "create", "path": str(path)})

    def record_backup(self, path, backup):
        self.actions.append({"op": "backup", "path": str(path), "backup": str(backup)})

    def to_dict(self):
        return {"meta": self.meta, "actions": self.actions}

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return path

    def rollback(self):
        """Deshace acciones en orden inverso."""
        errors = []
        for act in reversed(self.actions):
            try:
                if act["op"] == "create":
                    p = Path(act["path"])
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p, ignore_errors=True)
                    elif p.exists() or p.is_symlink():
                        p.unlink()
                elif act["op"] == "backup":
                    p = Path(act["path"]); bak = Path(act["backup"])
                    if bak.exists():
                        shutil.copy2(bak, p)
            except Exception as e:  # noqa
                errors.append(f"{act}: {e}")
        return errors

# ----------------------------------------------------------------------------
# E/S segura
# ----------------------------------------------------------------------------

def backup_file(path, manifest):
    path = Path(path)
    bak = path.with_name(path.name + f".bak-{now_stamp()}")
    shutil.copy2(path, bak)
    if manifest:
        manifest.record_backup(path, bak)
    return bak

def write_json_atomic(path, data, manifest):
    """Backup (si existe) + escritura atómica + revalidación."""
    path = Path(path)
    # Validar serialización antes de tocar disco.
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    if existed:
        backup_file(path, manifest)
    else:
        if manifest:
            manifest.record_create(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # Revalidar.
    try:
        read_json(path)
    except Exception as e:
        raise MigrateError(f"Validación post-escritura falló para {path}: {e}")

def copytree_tracked(src, dst, manifest, ignore=None):
    dst = Path(dst)
    if dst.exists():
        raise MigrateError(f"El destino ya existe: {dst}")
    shutil.copytree(src, dst, ignore=ignore)
    if manifest:
        manifest.record_create(dst)

def copyfile_tracked(src, dst, manifest):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    new = not dst.exists()
    shutil.copy2(src, dst)
    if manifest and new:
        manifest.record_create(dst)

def ignore_denylist(_dir, names):
    return [n for n in names if n in DENYLIST_NAMES]

# ----------------------------------------------------------------------------
# Modelo
# ----------------------------------------------------------------------------

@dataclass
class Team:
    account: str
    org: str
    name: str
    email: str
    org_type: str
    base: Path
    num_projects: int = 0
    index: int = 0

    @property
    def org_dir(self):
        return self.base / SESSIONS_DIRNAME / self.account / self.org

    @property
    def spaces_path(self):
        return self.org_dir / "spaces.json"

    @property
    def label(self):
        nm = self.name or "(sin nombre)"
        return f"{nm}  [{self.org[:8]}…]"

@dataclass
class Space:
    raw: dict
    team: Team

    @property
    def id(self): return self.raw.get("id", "")
    @property
    def name(self): return self.raw.get("name", "")
    @property
    def instructions(self): return self.raw.get("instructions", "")
    @property
    def folders(self):
        return [f.get("path") for f in self.raw.get("folders", []) if f.get("path")]
    @property
    def memory_dir(self):
        return self.team.org_dir / "spaces" / self.id / "memory"

# ----------------------------------------------------------------------------
# Descubrimiento
# ----------------------------------------------------------------------------

def sessions_root(base):
    return Path(base) / SESSIONS_DIRNAME

def find_team_identity(org_dir):
    """Busca un .claude.json para extraer organizationName/email. Depth-limitado."""
    org_dir = Path(org_dir)
    skip_dirs = {"outputs", "uploads", "Cache", "backups", "tool-results",
                 "node_modules", ".git"}
    base_depth = len(org_dir.parts)
    for root, dirs, files in os.walk(org_dir):
        depth = len(Path(root).parts) - base_depth
        if depth >= 5:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if ".claude.json" in files:
            try:
                data = read_json(Path(root) / ".claude.json")
                acc = data.get("oauthAccount") or {}
                if acc.get("organizationName") or acc.get("emailAddress"):
                    return (acc.get("organizationName", ""),
                            acc.get("emailAddress", ""),
                            acc.get("organizationType", ""))
            except Exception:
                pass
    return ("", "", "")

def enumerate_teams(base):
    """Devuelve lista ordenada de Team detectados en la Mac."""
    root = sessions_root(base)
    teams = []
    if not root.is_dir():
        return teams
    for acc_dir in sorted(root.iterdir()):
        if not acc_dir.is_dir() or not is_uuid(acc_dir.name):
            continue
        for org_dir in sorted(acc_dir.iterdir()):
            if not org_dir.is_dir() or not is_uuid(org_dir.name):
                continue
            name, email, otype = find_team_identity(org_dir)
            num = 0
            sp = org_dir / "spaces.json"
            if sp.exists():
                try:
                    num = len(read_json(sp).get("spaces", []))
                except Exception:
                    num = -1
            teams.append(Team(account=acc_dir.name, org=org_dir.name, name=name,
                              email=email, org_type=otype, base=Path(base),
                              num_projects=num))
    for i, t in enumerate(teams):
        t.index = i
    return teams

def active_account(base):
    p = Path(base) / ACTIVE_ACCOUNT_FILE
    if p.exists():
        try:
            return read_json(p).get("ownerAccountId")
        except Exception:
            return None
    return None

def resolve_team(base, selector, teams=None):
    teams = teams if teams is not None else enumerate_teams(base)
    if not teams:
        raise MigrateError("No se detectaron Teams de Cowork en esta Mac.")
    s = str(selector).strip()
    # índice
    if s.isdigit():
        i = int(s)
        if 0 <= i < len(teams):
            return teams[i]
        raise MigrateError(f"Índice de Team fuera de rango: {i}")
    # orgId exacto o prefijo
    matches = [t for t in teams if t.org == s or t.org.startswith(s)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise MigrateError(f"Prefijo de orgId ambiguo '{s}'.")
    # nombre (case-insensitive)
    by_name = [t for t in teams if t.name.lower() == s.lower()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise MigrateError(f"Nombre de Team ambiguo '{s}'. Usa el orgId.")
    # contiene
    contains = [t for t in teams if s.lower() in t.name.lower()]
    if len(contains) == 1:
        return contains[0]
    raise MigrateError(f"No se encontró un Team para '{s}'. Usa `list-teams`.")

def load_spaces(team):
    if not team.spaces_path.exists():
        return []
    return read_json(team.spaces_path).get("spaces", [])

def list_sessions(team):
    out = []
    for jf in sorted(team.org_dir.glob("local_*.json")):
        try:
            data = read_json(jf)
        except Exception:
            continue
        sdir = jf.with_suffix("")
        out.append({"json": jf, "dir": sdir,
                    "id": data.get("sessionId", sdir.name),
                    "title": data.get("title", ""),
                    "outputs": sdir / "outputs"})
    return out

def find_session(team, selector):
    s = str(selector).strip()
    sessions = list_sessions(team)
    by_id = [x for x in sessions if x["id"] == s or x["dir"].name == s
             or x["dir"].name == f"local_{s}"]
    if by_id:
        return by_id[0]
    by_title = [x for x in sessions if x["title"].lower() == s.lower()]
    if len(by_title) == 1:
        return by_title[0]
    contains = [x for x in sessions if s.lower() in x["title"].lower()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        raise MigrateError(f"Título de chat ambiguo '{s}'. Usa el sessionId.")
    raise MigrateError(f"No se encontró un chat '{s}' en el Team {team.label}.")

def safe_folder_name(name):
    n = re.sub(r"[/\x00]", "-", name).strip() or "Proyecto importado"
    return n

def find_space(team, project_selector):
    spaces = load_spaces(team)
    s = str(project_selector).strip()
    by_id = [sp for sp in spaces if sp.get("id") == s]
    if by_id:
        return Space(by_id[0], team)
    by_name = [sp for sp in spaces if sp.get("name", "").lower() == s.lower()]
    if len(by_name) == 1:
        return Space(by_name[0], team)
    if len(by_name) > 1:
        raise MigrateError(f"Nombre de proyecto ambiguo '{s}'. Usa el id.")
    raise MigrateError(f"No se encontró el proyecto '{s}' en el Team {team.label}.")

# ----------------------------------------------------------------------------
# Seguridad: detección de app abierta
# ----------------------------------------------------------------------------

def claude_app_running():
    """True si la app Claude Desktop parece estar abierta (macOS y Windows)."""
    if os.name == "nt":                                # Windows: tasklist
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                               capture_output=True, text=True)
            return "Claude.exe" in (r.stdout or "")
        except FileNotFoundError:
            return False
    # macOS / Linux: pgrep
    for pattern in ("Claude", "Claude Helper"):
        try:
            r = subprocess.run(["pgrep", "-x", pattern],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return True
        except FileNotFoundError:
            break
    try:
        r = subprocess.run(["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return True
    except FileNotFoundError:
        pass
    return False

# ----------------------------------------------------------------------------
# Conectores
# ----------------------------------------------------------------------------

def connector_report(team):
    """Reporte 'reconecta esto': conectores con tools activos en las sesiones."""
    by_connector = {}  # connector -> set(tools)
    remote_names = {}   # uuid -> nombre/url
    for jf in sorted(team.org_dir.glob("local_*.json")):
        try:
            data = read_json(jf)
        except Exception:
            continue
        for rc in data.get("remoteMcpServersConfig", []) or []:
            if isinstance(rc, dict):
                uid = rc.get("id") or rc.get("serverId") or rc.get("uuid")
                nm = rc.get("name") or rc.get("url")
                if uid and nm:
                    remote_names[uid] = nm
        for key, enabled in (data.get("enabledMcpTools") or {}).items():
            if not enabled:
                continue
            parts = key.split(":")
            if parts[0] == "local" and len(parts) >= 3:
                conn = f"{parts[1]} (local)"
                tool = parts[2]
            elif len(parts) >= 2:
                conn = parts[0]
                tool = ":".join(parts[1:])
            else:
                conn = key; tool = ""
            by_connector.setdefault(conn, set()).add(tool)
    # Resolver nombres legibles para UUIDs
    resolved = {}
    for conn, tools in by_connector.items():
        name = remote_names.get(conn, conn)
        resolved[name] = len(tools)
    return resolved

# ----------------------------------------------------------------------------
# Sesiones / chat (best-effort, experimental)
# ----------------------------------------------------------------------------

def candidate_sessions(team, space):
    """Sesiones cuya ruta aprobada/cwd referencia la carpeta del proyecto."""
    out = []
    folders = [os.path.normpath(p) for p in space.folders]
    for jf in sorted(team.org_dir.glob("local_*.json")):
        try:
            data = read_json(jf)
        except Exception:
            continue
        paths = []
        paths += data.get("userApprovedFileAccessPaths", []) or []
        paths += [f if isinstance(f, str) else f.get("path", "")
                  for f in (data.get("fsDetectedFiles", []) or [])]
        confidence = "baja"
        for p in paths:
            pn = os.path.normpath(str(p))
            if any(pn == fp or pn.startswith(fp + os.sep) for fp in folders):
                confidence = "alta"
                break
        title = data.get("title", "")
        if confidence != "alta" and space.name and space.name.lower() in title.lower():
            confidence = "media"
        if confidence in ("alta", "media"):
            out.append({"json": jf, "dir": jf.with_suffix(""),
                        "title": title, "confidence": confidence})
    return out

# ----------------------------------------------------------------------------
# Pre-vuelo
# ----------------------------------------------------------------------------

def scan_large_files(folders, threshold_bytes):
    large = []
    for fp in folders:
        p = Path(fp)
        if not p.exists():
            continue
        for root, _d, files in os.walk(p):
            for f in files:
                fpath = Path(root) / f
                try:
                    sz = fpath.stat().st_size
                except OSError:
                    continue
                if sz >= threshold_bytes:
                    large.append((str(fpath), sz))
    return large

# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------

def cmd_list_teams(args):
    base = Path(args.base_dir)
    teams = enumerate_teams(base)
    if not teams:
        warn(f"No se detectaron Teams en {sessions_root(base)}")
        return 0
    act = active_account(base)
    if args.json:
        print(json.dumps([{
            "index": t.index, "account": t.account, "org": t.org,
            "name": t.name, "email": t.email, "type": t.org_type,
            "projects": t.num_projects, "active_account": t.account == act
        } for t in teams], indent=2, ensure_ascii=False))
        return 0
    head("Teams de Cowork detectados")
    print(f"{'#':>2}  {'Team':<28} {'Proyectos':>9}  {'orgId':<10} {'email'}")
    print("-" * 78)
    for t in teams:
        star = _c(" ●", C.GRN) if t.account == act else "  "
        nm = (t.name or "(sin nombre)")[:27]
        print(f"{t.index:>2}{star}{nm:<28} {t.num_projects:>9}  "
              f"{t.org[:8]+'…':<10} {t.email}")
    print(_c("\n● = cuenta activa. Selecciona un Team por # , orgId o nombre.", C.DIM))
    return 0

def cmd_list_projects(args):
    base = Path(args.base_dir)
    team = resolve_team(base, args.team)
    spaces = [Space(sp, team) for sp in load_spaces(team)]
    head(f"Proyectos en {team.label}")
    if not spaces:
        warn("Este Team no tiene proyectos (spaces.json vacío o ausente).")
        return 0
    for sp in spaces:
        mem = "sí" if sp.memory_dir.exists() else "no"
        folder = sp.folders[0] if sp.folders else "(sin carpeta)"
        size = human_size(dir_size(sp.folders[0])) if sp.folders and Path(sp.folders[0]).exists() else "-"
        print(f"  • {_c(sp.name, C.B)}")
        print(f"      id={sp.id}")
        print(f"      carpeta={folder} ({size})  memoria={mem}")
    return 0

def cmd_inspect(args):
    base = Path(args.base_dir)
    team = resolve_team(base, args.team)
    space = find_space(team, args.project)
    threshold = args.max_file_size * 1024 * 1024
    head(f"Inspección: «{space.name}» en {team.label}")
    print(f"  id: {space.id}")
    print(f"  carpetas:")
    for fp in space.folders:
        exists = "ok" if Path(fp).exists() else _c("NO EXISTE", C.RED)
        sz = human_size(dir_size(fp)) if Path(fp).exists() else "-"
        print(f"    - {fp}  ({sz}) {exists}")
    print(f"  instrucciones: {'sí ('+str(len(space.instructions))+' chars)' if space.instructions else 'no'}")
    print(f"  memoria del proyecto: {'sí' if space.memory_dir.exists() else 'no'}", end="")
    if space.memory_dir.exists():
        n = len(list(space.memory_dir.glob('*.md')))
        print(f" ({n} archivos .md)")
    else:
        print()
    large = scan_large_files(space.folders, threshold)
    if large:
        warn(f"Archivos grandes (>= {args.max_file_size} MB):")
        for fp, sz in large:
            print(f"    - {Path(fp).name}  {human_size(sz)}")
    head("Conectores detectados en el Team origen")
    rep = connector_report(team)
    if rep:
        for name, ntools in sorted(rep.items(), key=lambda x: -x[1]):
            print(f"    - {name}  ({ntools} tools)")
    else:
        print("    (ninguno)")
    head("Sesiones candidatas (chat) — heurística")
    cands = candidate_sessions(team, space)
    if cands:
        for c in cands:
            print(f"    - [{c['confidence']}] {c['title'] or c['dir'].name}")
    else:
        print("    (ninguna asociable con confianza)")
    print(_c("\nNota: el chat es best-effort/experimental (audit encriptado; "
             "transcript ligado a la cuenta).", C.DIM))
    return 0

def cmd_migrate(args):
    base = Path(args.base_dir)
    teams = enumerate_teams(base)
    src = resolve_team(base, args.from_team, teams)
    dst = resolve_team(base, args.to_team, teams)
    if src.org == dst.org and src.account == dst.account:
        raise MigrateError("El Team origen y destino son el mismo.")
    space = find_space(src, args.project)

    head(f"Migración: «{space.name}»")
    print(f"  origen : {src.label}  (cuenta {src.account[:8]}…)")
    print(f"  destino: {dst.label}  (cuenta {dst.account[:8]}…)")

    # ---- Fase A: pre-vuelo ----
    head("Fase A — Pre-vuelo")
    is_sandbox = str(base.resolve()) != str(DEFAULT_BASE.resolve())
    if args.dry_run:
        info("  (dry-run: chequeo de app omitido)")
    elif not is_sandbox and not args.force:
        if claude_app_running():
            raise MigrateError("La app Claude está abierta. Ciérrala por completo "
                               "(Cmd+Q) y reintenta, o usa --force bajo tu riesgo.")
        ok("App Claude cerrada")
    else:
        warn("Chequeo de app omitido (sandbox o --force)")

    if not dst.org_dir.is_dir():
        raise MigrateError("El Team destino no existe en disco. Inicia sesión en él "
                           "al menos una vez en la app Claude y reintenta.")
    ok("Team destino existe")

    new_name = args.rename or space.name
    dst_spaces = load_spaces(dst)
    if any(sp.get("name", "").lower() == new_name.lower() for sp in dst_spaces):
        raise MigrateError(f"Ya existe un proyecto llamado «{new_name}» en el destino. "
                           f"Usa --rename <nuevo nombre>.")
    ok("Sin colisión de nombre en el destino")

    new_id = str(uuid.uuid4())

    # Plan de carpeta
    folder_plan = []   # (modo, src, dst)
    for fp in space.folders:
        if args.copy_folder:
            target = Path(args.copy_folder)
            if len(space.folders) > 1:
                target = target / Path(fp).name
            folder_plan.append(("copy", fp, str(target)))
        else:
            folder_plan.append(("share", fp, fp))  # misma Mac: comparte ruta

    # Espacio en disco (solo si copiamos)
    if args.copy_folder:
        need = sum(dir_size(fp) for _m, fp, _t in folder_plan if Path(fp).exists())
        free = shutil.disk_usage(str(dst.org_dir)).free
        if need * 1.2 > free:
            raise MigrateError(f"Espacio insuficiente: se necesitan ~{human_size(need)} "
                               f"y hay {human_size(free)} libres.")
        ok(f"Espacio en disco suficiente ({human_size(free)} libres)")

    cands = [] if args.no_chat or not args.include_chat else \
        [c for c in candidate_sessions(src, space) if c["confidence"] == "alta"]
    rep = connector_report(src)

    # ---- Fase B: mostrar plan ----
    head("Fase B — Plan")
    print(f"  • Crear proyecto «{new_name}» (nuevo id {new_id[:8]}…) en el destino")
    for mode, fp, tgt in folder_plan:
        if mode == "share":
            print(f"  • Carpeta: COMPARTIR ruta existente {fp}")
        else:
            print(f"  • Carpeta: COPIAR {fp} -> {tgt}")
    print(f"  • Memoria de proyecto: {'sí' if space.memory_dir.exists() else 'no hay'}")
    print(f"  • Memoria de workspace: {'merge' if not args.no_workspace_memory else 'omitir'}")
    print(f"  • Chat (experimental): {len(cands)} sesión(es) de alta confianza"
          if (args.include_chat and not args.no_chat) else "  • Chat: omitido")
    print(f"  • Conectores a reconectar luego: {', '.join(rep.keys()) if rep else '(ninguno)'}")

    if args.dry_run:
        warn("--dry-run: no se escribió nada.")
        return 0

    if not args.yes:
        resp = input(_c("\n¿Proceder con la migración? [s/N] ", C.YLW)).strip().lower()
        if resp not in ("s", "si", "sí", "y", "yes"):
            warn("Cancelado por el usuario.")
            return 1

    # ---- Fase C: ejecución transaccional ----
    head("Fase C — Ejecución")
    log_dir = base / LOGS_DIRNAME / f"migrate-{now_stamp()}"
    manifest = Manifest(meta={
        "tool": "cowork-migrate", "version": "1.0", "timestamp": now_stamp(),
        "source": {"account": src.account, "org": src.org, "name": src.name},
        "dest": {"account": dst.account, "org": dst.org, "name": dst.name},
        "old_space_id": space.id, "new_space_id": new_id, "new_name": new_name,
        "connectors_to_reconnect": list(rep.keys()),
    })
    try:
        # 1) Carpeta
        new_folders = []
        for mode, fp, tgt in folder_plan:
            if mode == "share":
                new_folders.append({"path": fp})
            else:
                if Path(tgt).exists():
                    raise MigrateError(f"La carpeta destino ya existe: {tgt}")
                copytree_tracked(fp, tgt, manifest, ignore=ignore_denylist)
                new_folders.append({"path": tgt})
                ok(f"Carpeta copiada -> {tgt}")

        # 2) Memoria de proyecto
        if space.memory_dir.exists():
            dst_mem = dst.org_dir / "spaces" / new_id / "memory"
            dst_mem.parent.mkdir(parents=True, exist_ok=True)
            if manifest:
                manifest.record_create(dst.org_dir / "spaces" / new_id)
            copytree_tracked(space.memory_dir, dst_mem, manifest, ignore=ignore_denylist)
            ok("Memoria de proyecto copiada")

        # 3) Memoria de workspace (merge)
        if not args.no_workspace_memory:
            merge_workspace_memory(src, dst, manifest)

        # 4) Chat (best-effort)
        if cands:
            for c in cands:
                copy_session(c, dst, manifest)
            ok(f"{len(cands)} sesión(es) copiada(s) (experimental)")

        # 5) spaces.json del destino (COMMIT final)
        new_space = {
            "id": new_id, "name": new_name,
            "folders": new_folders, "projects": [], "links": [],
            "origin": "user",
            "createdAt": int(_dt.datetime.now().timestamp() * 1000),
            "updatedAt": int(_dt.datetime.now().timestamp() * 1000),
        }
        if space.instructions:
            new_space["instructions"] = space.instructions
        dst_doc = read_json(dst.spaces_path) if dst.spaces_path.exists() else {"spaces": []}
        dst_doc.setdefault("spaces", []).append(new_space)
        write_json_atomic(dst.spaces_path, dst_doc, manifest)
        ok("spaces.json del destino actualizado (proyecto visible)")

    except Exception as e:
        err(f"Fallo durante la migración: {e}")
        warn("Ejecutando rollback automático…")
        rb_err = manifest.rollback()
        if rb_err:
            err("Rollback con errores: " + "; ".join(rb_err))
        else:
            ok("Rollback completo. El destino quedó como estaba.")
        manifest.save(log_dir / "manifest-failed.json")
        return 2

    mpath = manifest.save(log_dir / "manifest.json")

    # ---- Fase D: verify + resumen ----
    head("Fase D — Verificación")
    verify_space(dst, new_id, new_name, new_folders)

    head("Resumen")
    ok(f"Proyecto «{new_name}» migrado a {dst.label}")
    print(f"  manifest: {mpath}")
    if rep:
        warn("Reconecta estos conectores en el Team destino (reautenticación manual):")
        for name in rep:
            print(f"    - {name}")
    print(_c(f"\nPara deshacer:  python3 cowork_migrate.py rollback --manifest \"{mpath}\"",
             C.DIM))
    return 0

def ignore_artifacts(_dir, names):
    """Excluye secretos + el dir interno .claude al copiar outputs de una sesión."""
    return [n for n in names if n in DENYLIST_NAMES or n == ".claude"]

def ignore_session(_dir, names):
    """Al copiar una sesión (chat) SÍ incluimos el transcript (audit.jsonl) y su
    .audit-key — son la conversación del propio usuario. Solo descartamos basura."""
    return [n for n in names if n == ".DS_Store"]

def cmd_import_session(args):
    """Crea un proyecto NUEVO en el Team destino a partir de un chat/sesión."""
    base = Path(args.base_dir)
    teams = enumerate_teams(base)
    src = resolve_team(base, args.from_team, teams)
    dst = resolve_team(base, args.to_team, teams)
    sess = find_session(src, args.session)
    proj_name = args.project_name or sess["title"] or "Proyecto importado"

    head(f"Importar chat → proyecto: «{sess['title'] or sess['dir'].name}»")
    print(f"  origen : {src.label}  (chat {sess['dir'].name})")
    print(f"  destino: {dst.label}")
    print(f"  proyecto nuevo: «{proj_name}»")

    # Carpeta destino del nuevo proyecto
    if args.folder:
        dest_folder = Path(args.folder)
    else:
        dest_folder = Path.home() / "Documents" / "Claude" / "Projects" / safe_folder_name(proj_name)

    outputs = sess["outputs"]
    n_files = 0
    if outputs.is_dir():
        for root, dirs, files in os.walk(outputs):
            if ".claude" in dirs:
                dirs.remove(".claude")
            n_files += len([f for f in files if f not in DENYLIST_NAMES])

    # ---- Pre-vuelo ----
    head("Fase A — Pre-vuelo")
    is_sandbox = str(base.resolve()) != str(DEFAULT_BASE.resolve())
    if args.dry_run:
        info("  (dry-run: chequeo de app omitido)")
    elif not is_sandbox and not args.force:
        if claude_app_running():
            raise MigrateError("La app Claude está abierta. Ciérrala (Cmd+Q) y reintenta.")
        ok("App Claude cerrada")
    else:
        warn("Chequeo de app omitido (sandbox o --force)")
    if not dst.org_dir.is_dir():
        raise MigrateError("El Team destino no existe en disco. Inicia sesión en él una vez.")
    if any(sp.get("name", "").lower() == proj_name.lower() for sp in load_spaces(dst)):
        raise MigrateError(f"Ya existe un proyecto «{proj_name}» en el destino. Usa --project-name.")
    if dest_folder.exists():
        raise MigrateError(f"La carpeta destino ya existe: {dest_folder}. Usa --folder otra ruta.")
    ok("Sin colisiones")

    # ---- Plan ----
    head("Fase B — Plan")
    print(f"  • Crear carpeta {dest_folder}")
    print(f"  • Copiar {n_files} archivo(s) de los outputs del chat (sin .claude ni secretos)")
    print(f"  • Registrar proyecto «{proj_name}» en {dst.label}")
    print(f"  • Chat: {'copiar (experimental)' if args.include_chat else 'omitir'}")
    if args.dry_run:
        warn("--dry-run: no se escribió nada.")
        return 0
    if not args.yes:
        resp = input(_c("\n¿Proceder? [s/N] ", C.YLW)).strip().lower()
        if resp not in ("s", "si", "sí", "y", "yes"):
            warn("Cancelado."); return 1

    # ---- Ejecución ----
    head("Fase C — Ejecución")
    log_dir = base / LOGS_DIRNAME / f"import-{now_stamp()}"
    new_id = str(uuid.uuid4())
    manifest = Manifest(meta={
        "tool": "cowork-migrate import-session", "version": "1.0", "timestamp": now_stamp(),
        "source": {"account": src.account, "org": src.org, "name": src.name,
                   "session": sess["dir"].name},
        "dest": {"account": dst.account, "org": dst.org, "name": dst.name},
        "new_space_id": new_id, "new_name": proj_name, "folder": str(dest_folder),
    })
    try:
        # 1) Carpeta con artefactos
        if outputs.is_dir() and n_files > 0:
            copytree_tracked(outputs, dest_folder, manifest, ignore=ignore_artifacts)
            ok(f"{n_files} archivo(s) copiados -> {dest_folder}")
        else:
            dest_folder.mkdir(parents=True, exist_ok=False)
            manifest.record_create(dest_folder)
            warn("El chat no tenía outputs; carpeta creada vacía.")

        # 2) Chat (opcional)
        if args.include_chat:
            copyfile_tracked(sess["json"], dst.org_dir / sess["json"].name, manifest)
            if sess["dir"].is_dir():
                tgt = dst.org_dir / sess["dir"].name
                if not tgt.exists():
                    copytree_tracked(sess["dir"], tgt, manifest, ignore=ignore_session)
            ok("Chat copiado (con transcript)")

        # 3) spaces.json destino (commit)
        new_space = {
            "id": new_id, "name": proj_name,
            "folders": [{"path": str(dest_folder)}],
            "projects": [], "links": [], "origin": "user",
            "createdAt": int(_dt.datetime.now().timestamp() * 1000),
            "updatedAt": int(_dt.datetime.now().timestamp() * 1000),
        }
        dst_doc = read_json(dst.spaces_path) if dst.spaces_path.exists() else {"spaces": []}
        dst_doc.setdefault("spaces", []).append(new_space)
        write_json_atomic(dst.spaces_path, dst_doc, manifest)
        ok("Proyecto registrado en el destino")
    except Exception as e:
        err(f"Fallo: {e}")
        warn("Rollback automático…")
        rb = manifest.rollback()
        ok("Rollback completo.") if not rb else err("Rollback con errores: " + "; ".join(rb))
        manifest.save(log_dir / "manifest-failed.json")
        return 2

    mpath = manifest.save(log_dir / "manifest.json")
    head("Fase D — Verificación")
    verify_space(dst, new_id, proj_name, [{"path": str(dest_folder)}])
    head("Resumen")
    ok(f"Chat importado como proyecto «{proj_name}» en {dst.label}")
    print(f"  carpeta: {dest_folder}")
    print(f"  manifest: {mpath}")
    print(_c(f"\nDeshacer:  python3 cowork_migrate.py rollback --manifest \"{mpath}\"", C.DIM))
    return 0

def cmd_move_chat(args):
    """Mueve (copia) un chat/sesión a otro Team para que aparezca en su lista de chats."""
    base = Path(args.base_dir)
    teams = enumerate_teams(base)
    src = resolve_team(base, args.from_team, teams)
    dst = resolve_team(base, args.to_team, teams)
    if src.org == dst.org and src.account == dst.account:
        raise MigrateError("Origen y destino son el mismo Team.")
    sess = find_session(src, args.session)

    head(f"Mover chat: «{sess['title'] or sess['dir'].name}»")
    print(f"  de: {src.label}\n  a:  {dst.label}")

    is_sandbox = str(base.resolve()) != str(DEFAULT_BASE.resolve())
    if args.dry_run:
        info("  (dry-run: chequeo de app omitido)")
    elif not is_sandbox and not args.force:
        if claude_app_running():
            raise MigrateError("La app Claude está abierta. Ciérrala (Cmd+Q) y reintenta.")
        ok("App Claude cerrada")
    if not dst.org_dir.is_dir():
        raise MigrateError("El Team destino no existe en disco. Inicia sesión en él una vez.")
    if (dst.org_dir / sess["dir"].name).exists():
        raise MigrateError("Ese chat ya existe en el destino.")
    ok("Sin colisión")

    print(f"  • Copiar chat con su transcript (audit.jsonl) a {dst.label}")
    if args.dry_run:
        warn("--dry-run: no se escribió nada."); return 0
    if not args.yes:
        if input(_c("¿Mover el chat? [s/N] ", C.YLW)).strip().lower() not in ("s","si","sí","y","yes"):
            warn("Cancelado."); return 1

    log_dir = base / LOGS_DIRNAME / f"movechat-{now_stamp()}"
    manifest = Manifest(meta={"tool": "cowork-migrate move-chat", "timestamp": now_stamp(),
        "source": {"account": src.account, "org": src.org, "name": src.name,
                   "session": sess["dir"].name},
        "dest": {"account": dst.account, "org": dst.org, "name": dst.name}})
    try:
        copyfile_tracked(sess["json"], dst.org_dir / sess["json"].name, manifest)
        if sess["dir"].is_dir():
            copytree_tracked(sess["dir"], dst.org_dir / sess["dir"].name, manifest,
                             ignore=ignore_session)
        ok("Chat copiado con transcript")
    except Exception as e:
        err(f"Fallo: {e}"); warn("Rollback…")
        manifest.rollback(); manifest.save(log_dir / "manifest-failed.json")
        return 2
    mpath = manifest.save(log_dir / "manifest.json")
    ok(f"Chat «{sess['title']}» movido a {dst.label}")
    print(f"  manifest: {mpath}")
    print(_c(f"Deshacer: python3 cowork_migrate.py rollback --manifest \"{mpath}\"", C.DIM))
    return 0

def merge_workspace_memory(src, dst, manifest):
    src_mem = src.org_dir / "agent" / "memory"
    if not src_mem.is_dir():
        return
    dst_mem = dst.org_dir / "agent" / "memory"
    dst_mem.mkdir(parents=True, exist_ok=True)
    conflicts = 0
    for f in sorted(src_mem.glob("*.md")):
        if f.name in DENYLIST_NAMES:
            continue
        target = dst_mem / f.name
        if f.name.upper() == "MEMORY.MD":
            # índice: append delimitado, con backup
            if target.exists():
                backup_file(target, manifest)
                with open(target, "a", encoding="utf-8") as out:
                    out.write(f"\n\n## Importado de {src.name or src.org[:8]} "
                              f"({now_stamp()})\n\n")
                    out.write(f.read_text(encoding="utf-8"))
            else:
                copyfile_tracked(f, target, manifest)
            continue
        if not target.exists():
            copyfile_tracked(f, target, manifest)
        else:
            try:
                same = sha256_file(f) == sha256_file(target)
            except Exception:
                same = False
            if not same:
                alt = dst_mem / f"{f.stem}.from-{(src.name or 'origen').replace(' ', '_')}.md"
                copyfile_tracked(f, alt, manifest)
                conflicts += 1
    if conflicts:
        warn(f"Memoria de workspace: {conflicts} conflicto(s) guardados con sufijo .from-…")
    ok("Memoria de workspace mergeada")

def copy_session(cand, dst, manifest):
    jf = cand["json"]; sdir = cand["dir"]
    # json de sesión
    copyfile_tracked(jf, dst.org_dir / jf.name, manifest)
    # dir de sesión (sin secretos)
    if sdir.is_dir():
        target = dst.org_dir / sdir.name
        if not target.exists():
            copytree_tracked(sdir, target, manifest, ignore=ignore_session)

def verify_space(team, space_id, name, folders):
    spaces = load_spaces(team)
    sp = next((s for s in spaces if s.get("id") == space_id), None)
    if not sp:
        err("El proyecto NO aparece en spaces.json del destino.")
        return False
    ok("Proyecto presente en spaces.json del destino")
    for fol in folders:
        p = Path(fol["path"])
        if p.exists():
            ok(f"Carpeta OK: {p}")
        else:
            warn(f"Carpeta no existe en disco: {p}")
    mem = team.org_dir / "spaces" / space_id / "memory"
    if mem.exists():
        ok("Memoria de proyecto presente")
    return True

def _cmd_list_sessions(args):
    base = Path(args.base_dir)
    team = resolve_team(base, args.team)
    head(f"Chats/sesiones en {team.label}")
    sessions = list_sessions(team)
    if not sessions:
        warn("Sin sesiones."); return 0
    for s in sessions:
        nout = len(list(s["outputs"].glob("*"))) if s["outputs"].is_dir() else 0
        print(f"  • {_c(s['title'] or '(sin título)', C.B)}")
        print(f"      id={s['id']}  outputs={nout} archivo(s)")
    return 0

def cmd_rollback(args):
    mpath = Path(args.manifest)
    if not mpath.exists():
        raise MigrateError(f"No existe el manifest: {mpath}")
    doc = read_json(mpath)
    man = Manifest(meta=doc.get("meta", {}))
    man.actions = doc.get("actions", [])
    head("Rollback")
    print(f"  manifest: {mpath}")
    print(f"  acciones a deshacer: {len(man.actions)}")
    if not args.yes:
        resp = input(_c("¿Deshacer esta migración? [s/N] ", C.YLW)).strip().lower()
        if resp not in ("s", "si", "sí", "y", "yes"):
            warn("Cancelado.")
            return 1
    errs = man.rollback()
    if errs:
        err("Rollback con errores:")
        for e in errs:
            print(f"    - {e}")
        return 2
    ok("Rollback completo.")
    return 0

def cmd_verify(args):
    base = Path(args.base_dir)
    team = resolve_team(base, args.team)
    space = find_space(team, args.project)
    head(f"Verificación: «{space.name}» en {team.label}")
    folders = [{"path": p} for p in space.folders]
    verify_space(team, space.id, space.name, folders)
    return 0

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        r = input(_c(prompt + suffix + ": ", C.YLW)).strip()
    except EOFError:
        return default
    return r or default

def cmd_wizard(args):
    """Asistente interactivo para compañeros no técnicos."""
    base = Path(args.base_dir)
    if not sys.stdin.isatty():
        raise MigrateError("El asistente necesita una terminal interactiva. "
                           "Usa el subcomando `migrate` con opciones.")
    print(_c("\n╔══════════════════════════════════════════════╗", C.CYA))
    print(_c("║   Cowork — Asistente de migración de proyectos ║", C.B + C.CYA))
    print(_c("╚══════════════════════════════════════════════╝", C.CYA))
    print(_c("Mueve tus proyectos de un Team (ambiente) a otro.\n", C.DIM))

    if not args.force and claude_app_running():
        warn("La app Claude está ABIERTA. Ciérrala por completo (Cmd+Q) antes de continuar.")
        if _ask("¿Ya la cerraste? Escribe 'si' para seguir", "no").lower() not in ("si", "sí", "s", "y"):
            warn("Cancelado. Cierra Claude y vuelve a abrir el asistente.")
            return 1

    teams = enumerate_teams(base)
    if len(teams) < 2:
        raise MigrateError("Se necesitan al menos 2 Teams (ambientes) en esta Mac. "
                           "Inicia sesión en el Team destino al menos una vez.")
    head("Tus Teams (ambientes):")
    for t in teams:
        print(f"  {t.index}) {t.name or '(sin nombre)'}  — {t.num_projects} proyecto(s)")

    si = _ask("\n¿De qué Team quieres SACAR proyectos? (número)")
    src = resolve_team(base, si, teams)
    di = _ask("¿A qué Team quieres LLEVARLOS? (número)")
    dst = resolve_team(base, di, teams)
    if src.org == dst.org and src.account == dst.account:
        raise MigrateError("Origen y destino son el mismo Team.")

    spaces = [Space(sp, src) for sp in load_spaces(src)]
    if not spaces:
        warn(f"El Team «{src.name}» no tiene proyectos para mover.")
        return 0
    head(f"Proyectos en «{src.name}»:")
    for i, sp in enumerate(spaces):
        print(f"  {i}) {sp.name}")
    sel = _ask("\n¿Cuáles mover? (números separados por coma, o 'todos')", "todos")
    if sel.lower() in ("todos", "all", "*"):
        chosen = spaces
    else:
        idxs = [int(x) for x in re.findall(r"\d+", sel)]
        chosen = [spaces[i] for i in idxs if 0 <= i < len(spaces)]
    if not chosen:
        warn("No seleccionaste proyectos."); return 0

    dry = _ask("\n¿Hacer primero una SIMULACIÓN sin cambios? (si/no)", "si").lower() in ("si", "sí", "s", "y")

    head("Resumen")
    print(f"  De:  {src.name}")
    print(f"  A:   {dst.name}")
    print(f"  Proyectos: {', '.join(s.name for s in chosen)}")
    print(f"  Modo: {'SIMULACIÓN (no escribe)' if dry else 'REAL'}")
    if not dry:
        if _ask("\nEscribe 'MIGRAR' para confirmar", "").strip().upper() != "MIGRAR":
            warn("Cancelado."); return 1

    rc = 0
    for n, sp in enumerate(chosen):
        ns = argparse.Namespace(
            base_dir=str(base), from_team=str(src.index), to_team=str(dst.index),
            project=sp.id, rename=None, dry_run=dry, yes=True, copy_folder=None,
            max_file_size=500, no_workspace_memory=(n > 0), include_chat=False,
            no_chat=True, force=args.force)
        try:
            r = cmd_migrate(ns)
            rc = rc or r
        except MigrateError as e:
            err(f"«{sp.name}»: {e}")
            rc = 1
    # --- Chats sueltos (opcional) ---
    sessions = list_sessions(src)
    if sessions and _ask(f"\n¿Mover también CHATS de «{src.name}» a «{dst.name}»? (si/no)", "no").lower() in ("si", "sí", "s", "y"):
        head(f"Chats en «{src.name}»:")
        for i, s in enumerate(sessions):
            print(f"  {i}) {s['title'] or '(sin título)'}")
        csel = _ask("¿Cuáles? (números separados por coma, o 'todos')", "todos")
        if csel.lower() in ("todos", "all", "*"):
            cchosen = sessions
        else:
            cidx = [int(x) for x in re.findall(r"\d+", csel)]
            cchosen = [sessions[i] for i in cidx if 0 <= i < len(sessions)]
        for s in cchosen:
            ns = argparse.Namespace(base_dir=str(base), from_team=str(src.index),
                to_team=str(dst.index), session=s["id"], dry_run=dry, yes=True, force=args.force)
            try:
                rc = cmd_move_chat(ns) or rc
            except MigrateError as e:
                err(f"chat «{s['title']}»: {e}"); rc = 1

    head("Fin del asistente")
    if dry:
        info("Fue una simulación. Vuelve a ejecutar y elige 'no' en la simulación para hacerlo de verdad.")
    return rc

def build_parser():
    p = argparse.ArgumentParser(
        prog="cowork_migrate.py",
        description="Migrador de proyectos de Claude Cowork entre Teams (misma Mac).")
    p.add_argument("--base-dir", default=str(DEFAULT_BASE),
                   help="Base de Claude (default: %(default)s). Útil para sandbox.")
    sub = p.add_subparsers(dest="cmd")

    wz = sub.add_parser("wizard", help="Asistente interactivo (recomendado para empezar).")
    wz.add_argument("--force", action="store_true")
    wz.set_defaults(func=cmd_wizard)

    lt = sub.add_parser("list-teams", help="Lista los Teams detectados.")
    lt.add_argument("--json", action="store_true")
    lt.set_defaults(func=cmd_list_teams)

    lp = sub.add_parser("list-projects", help="Lista proyectos de un Team.")
    lp.add_argument("--team", required=True)
    lp.set_defaults(func=cmd_list_projects)

    ins = sub.add_parser("inspect", help="Pre-vuelo read-only de un proyecto.")
    ins.add_argument("--team", required=True)
    ins.add_argument("--project", required=True)
    ins.add_argument("--max-file-size", type=int, default=500,
                     help="Umbral de archivo grande en MB (default 500).")
    ins.set_defaults(func=cmd_inspect)

    mg = sub.add_parser("migrate", help="Migra un proyecto de un Team a otro.")
    mg.add_argument("--from", dest="from_team", required=True, help="Team origen.")
    mg.add_argument("--to", dest="to_team", required=True, help="Team destino.")
    mg.add_argument("--project", required=True, help="Nombre o id del proyecto.")
    mg.add_argument("--rename", help="Nuevo nombre en el destino.")
    mg.add_argument("--dry-run", action="store_true", help="Solo muestra el plan.")
    mg.add_argument("--yes", "-y", action="store_true", help="Sin confirmación.")
    mg.add_argument("--copy-folder", metavar="RUTA",
                    help="Duplicar la carpeta a RUTA (default: compartir ruta).")
    mg.add_argument("--max-file-size", type=int, default=500)
    mg.add_argument("--no-workspace-memory", action="store_true")
    mg.add_argument("--include-chat", action="store_true",
                    help="Intentar migrar el chat (experimental).")
    mg.add_argument("--no-chat", action="store_true", help="No migrar chat (default).")
    mg.add_argument("--force", action="store_true",
                    help="Saltar el chequeo de app abierta (riesgoso).")
    mg.set_defaults(func=cmd_migrate)

    ims = sub.add_parser("import-session",
                         help="Crea un proyecto nuevo en un Team a partir de un chat/sesión.")
    ims.add_argument("--from", dest="from_team", required=True, help="Team del chat.")
    ims.add_argument("--to", dest="to_team", required=True, help="Team destino.")
    ims.add_argument("--session", required=True, help="sessionId o título del chat.")
    ims.add_argument("--project-name", help="Nombre del proyecto nuevo (default: título del chat).")
    ims.add_argument("--folder", help="Carpeta destino (default: ~/Documents/Claude/Projects/<nombre>).")
    ims.add_argument("--include-chat", action="store_true", help="Copiar también el chat (experimental).")
    ims.add_argument("--dry-run", action="store_true")
    ims.add_argument("--yes", "-y", action="store_true")
    ims.add_argument("--force", action="store_true")
    ims.set_defaults(func=cmd_import_session)

    mc = sub.add_parser("move-chat", help="Mueve un chat/sesión a otro Team (aparece en su lista de chats).")
    mc.add_argument("--from", dest="from_team", required=True)
    mc.add_argument("--to", dest="to_team", required=True)
    mc.add_argument("--session", required=True, help="sessionId o título del chat.")
    mc.add_argument("--dry-run", action="store_true")
    mc.add_argument("--yes", "-y", action="store_true")
    mc.add_argument("--force", action="store_true")
    mc.set_defaults(func=cmd_move_chat)

    sis = sub.add_parser("list-sessions", help="Lista los chats/sesiones de un Team.")
    sis.add_argument("--team", required=True)
    sis.set_defaults(func=lambda a: _cmd_list_sessions(a))

    rb = sub.add_parser("rollback", help="Deshace una migración desde su manifest.")
    rb.add_argument("--manifest", required=True)
    rb.add_argument("--yes", "-y", action="store_true")
    rb.set_defaults(func=cmd_rollback)

    vf = sub.add_parser("verify", help="Verifica integridad de un proyecto.")
    vf.add_argument("--team", required=True)
    vf.add_argument("--project", required=True)
    vf.set_defaults(func=cmd_verify)

    return p

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        # Sin subcomando -> asistente interactivo.
        args.func = cmd_wizard
        if not hasattr(args, "force"):
            args.force = False
    try:
        return args.func(args)
    except MigrateError as e:
        err(str(e))
        return 1
    except KeyboardInterrupt:
        err("Interrumpido.")
        return 130

if __name__ == "__main__":
    sys.exit(main())
