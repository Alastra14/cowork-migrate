# cowork-migrate

Migrador de **proyectos de Claude Cowork** de un **Team a otro** en la misma computadora.

Cowork guarda todo localmente (JSON + archivos) y **no** tiene función oficial de
exportar/compartir entre Teams. Este programa lo hace por ti de forma **segura y reversible**:
copia la entrada del proyecto al `spaces.json` del Team destino, copia la memoria del proyecto,
mergea la memoria del workspace y te deja un reporte de los conectores a reconectar.

> Un solo archivo Python, **sin dependencias** (solo stdlib, Python ≥ 3.9). **macOS y Windows.**

> 🎯 **En Ternova el Team destino es: `Ternova`.** Cuando el asistente pregunte a qué Team llevar
> los proyectos, elige **`Ternova`** (el número en tu lista puede variar — guíate por el nombre).

## 🚀 Inicio rápido (para compañeros)

**Requisitos:** Claude Desktop con Cowork + Python 3, y haber **iniciado sesión al menos una vez**
tanto en tu Team de origen como en el **Team destino (`Ternova`)** — así ambos existen en tu equipo.
(Python 3 ya viene en macOS; en Windows instálalo desde python.org marcando *"Add to PATH"*.)

### macOS — 1 línea (recomendado)

1. **Cierra Claude** por completo (Cmd + Q).
2. Abre **Terminal** (Spotlight → "Terminal") y pega:

```bash
cd ~/Downloads && curl -fsSL https://raw.githubusercontent.com/Alastra14/cowork-migrate/main/cowork_migrate.py -o cowork_migrate.py && python3 cowork_migrate.py wizard
```

### Windows — 1 línea (recomendado)

1. **Cierra Claude** por completo.
2. Abre **PowerShell** (menú Inicio → "PowerShell") y pega:

```powershell
cd $HOME\Downloads; curl.exe -fsSL https://raw.githubusercontent.com/Alastra14/cowork-migrate/main/cowork_migrate.py -o cowork_migrate.py; python cowork_migrate.py wizard
```

3. (Ambos) Sigue el **asistente**: de qué Team sacar, a cuál llevar (**`Ternova`**), qué
   **proyectos** y qué **chats**. Hace una **simulación** primero; para ejecutar escribes `MIGRAR`.
4. Abre Claude, cambia al Team `Ternova` y revisa.

El script es **un solo archivo sin dependencias**; no instala nada ni pide permisos del sistema.

### Alternativa (doble clic)

Descarga el repo (**Code → Download ZIP**), descomprime y doble clic:
**`Migrar-proyectos-Cowork.command`** (macOS) o **`Migrar-proyectos-Cowork.bat`** (Windows).
En macOS la primera vez: clic derecho → **Abrir** → **Abrir**.
⚠️ El ZIP de GitHub a veces quita el permiso de ejecución en macOS; si no abre, usa la forma de 1 línea.

## ⚠️ Antes de empezar (importante)

1. **Cierra la app Claude** por completo (Cmd + Q) antes de migrar. El programa lo verifica.
2. **Inicia sesión en el Team destino** al menos una vez en la app Claude, para que exista en disco.
3. Esto **no es una función oficial** de Anthropic: manipula el almacén interno de Cowork. El
   programa minimiza el riesgo (backups con timestamp, escritura atómica, rollback), pero el
   formato podría cambiar en futuras versiones de la app.
4. **Las credenciales de los conectores NO se migran** (están encriptadas): hay que reautenticarlos
   en el Team destino. El programa te da la lista.
5. **El chat sí se mueve** (`--include-chat`): la conversación vive en `audit.jsonl` (JSON plano)
   dentro de la carpeta de la sesión, así que se copia completa junto con su `.audit-key`. La única
   incógnita es si la app lo **re-renderiza igual** tras moverlo a otro Team; pruébalo abriendo Claude.
   Los artefactos y la memoria migran de forma totalmente fiable.

## Uso

```bash
# 1. Ver tus Teams
python3 cowork_migrate.py list-teams

# 2. Ver los proyectos de un Team (por #, orgId o nombre)
python3 cowork_migrate.py list-projects --team "Ternova"

# 3. Pre-vuelo de un proyecto (solo lectura: rutas, memoria, conectores, chat)
python3 cowork_migrate.py inspect --team "Ternova" --project "Andy"

# 4. Simular la migración (no escribe nada)
python3 cowork_migrate.py migrate --from "Ternova" --to "Ternova DIRECTORES" \
    --project "Andy" --dry-run

# 5. Migrar de verdad (cierra la app Claude antes)
python3 cowork_migrate.py migrate --from "Ternova" --to "Ternova DIRECTORES" --project "Andy"

# 6. Verificar el resultado
python3 cowork_migrate.py verify --team "Ternova DIRECTORES" --project "Andy"

# 7. Deshacer si algo no te gustó
python3 cowork_migrate.py rollback --manifest "<ruta del manifest mostrada al final>"
```

### Importar un chat/sesión como proyecto nuevo

A veces el trabajo no está guardado como "proyecto" (space) sino como un **chat** con sus archivos
(p. ej. en otro Team que solo usaste para conversar). Para convertir ese chat en un proyecto dentro
de tu Team principal:

```bash
# Ver los chats de un Team
python3 cowork_migrate.py list-sessions --team "Ternova DIRECTORES"

# Mover un chat suelto a otro Team (aparece en su lista de chats, con su conversación)
python3 cowork_migrate.py move-chat --from "Ternova DIRECTORES" --to "Ternova" \
    --session "Cockpit reverse engineering" --dry-run   # quita --dry-run para ejecutar

# Crear un proyecto nuevo en "Ternova" con los archivos de ese chat (y opcionalmente el chat)
python3 cowork_migrate.py import-session \
    --from "Ternova DIRECTORES" --to "Ternova" \
    --session "Cockpit reverse engineering" \
    --include-chat --dry-run        # quita --dry-run para ejecutar
```

`import-session` copia los `outputs/` del chat (sin `.claude` ni secretos) a una carpeta nueva
(`~/Documents/Claude/Projects/<nombre>` por defecto, o `--folder`), registra el proyecto en el Team
destino con nuevo UUID, y con `--include-chat` copia también la sesión (experimental). Es
**no destructivo**: el chat original queda intacto. Reversible con `rollback`.

### Opciones de `migrate`

| Opción | Qué hace |
|---|---|
| `--from`, `--to` | Team origen y destino (por #, orgId o nombre). |
| `--project` | Nombre o id del proyecto a migrar. |
| `--rename NOMBRE` | Renombrar el proyecto en el destino (útil si hay colisión). |
| `--dry-run` | Muestra el plan completo y termina sin escribir. |
| `--yes` / `-y` | No pedir confirmación interactiva. |
| `--copy-folder RUTA` | Duplicar la carpeta del proyecto a RUTA (default: **compartir** la misma ruta, ideal en la misma Mac). |
| `--no-workspace-memory` | No mezclar la memoria del workspace del Team origen. |
| `--include-chat` | Intentar migrar el chat (experimental, solo sesiones de alta confianza). |
| `--no-chat` | No migrar chat (default). |
| `--max-file-size MB` | Umbral para marcar archivos grandes (default 500 MB). |
| `--force` | Saltar el chequeo de "app Claude abierta" (riesgoso). |
| `--base-dir RUTA` | Apuntar a otra base (para pruebas en sandbox). |

## Qué migra y cómo

| Componente | Comportamiento |
|---|---|
| **Entrada del proyecto** (`spaces.json`) | Se crea con **nuevo UUID** en el destino (el origen queda intacto). Se escribe **al final** (commit) de forma atómica y con backup. |
| **Instrucciones** | Se copian del campo `instructions` del proyecto. |
| **Carpeta física** | Misma Mac: se **comparte** la ruta (cero duplicación). `--copy-folder` para duplicar. |
| **Memoria del proyecto** | `spaces/{id}/memory/` → copia limpia. |
| **Memoria del workspace** | `agent/memory/` → **merge sin pisar**; archivos distintos se guardan con sufijo `.from-<Team>.md`; `MEMORY.md` por append delimitado, con backup. |
| **Conectores** | Solo **reporte** "reconecta esto" (credenciales nunca se migran ni se loguean). |
| **Chat** | Opt-in `--include-chat`. Copia la carpeta de la sesión **con el transcript** (`audit.jsonl`, JSON plano) y su `.audit-key`. Lo único no garantizado es que la app lo re-renderice idéntico en el destino. |

## Seguridad

Nunca se copian ni se imprimen: `.audit-key`, `audit.jsonl`, `config.json`,
`cowork-enabled-cli-ops.json`. Las claves sensibles (token/secret/authorization/apiKey/password)
se enmascaran en cualquier salida o manifest.

## Rollback y auditoría

Cada migración escribe un `manifest.json` en
`~/Library/Application Support/Claude/cowork-migrate-logs/migrate-<timestamp>/`.
Ese manifest registra cada acción (creaciones y backups) y es lo que usa `rollback` para deshacer.
Si algo falla a mitad de la migración, se ejecuta un **rollback automático**.

## Probado

Validado end-to-end en un sandbox sintético (`--base-dir`): migración, verificación, rollback
(estado restaurado), idempotencia (bloquea colisión de nombre), append de memoria de workspace y
exclusión de secretos.
