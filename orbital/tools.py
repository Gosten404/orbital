"""Herramientas que el agente puede usar. Cada una recibe args (dict) y devuelve texto."""
from __future__ import annotations

import asyncio
from pathlib import Path

TOOL_DOCS = """\
- read_file {"path": str, "max_chars"?: int, "pages"?: int}: lee un archivo de texto o el TEXTO COMPLETO de un PDF (hasta 12000 caracteres, 30 páginas).
- list {"path": str, "hidden"?: bool}: lista una carpeta: primero archivos (del más reciente al más viejo), luego carpetas.
- recent {"path": str, "hours"?: int, "hidden"?: bool}: archivos modificados en las últimas N horas (24 por defecto), del más nuevo al más viejo, sin carpetas ocultas (cachés de apps). Úsala cuando el usuario diga que creó o cambió algo hace poco, o no sepa el nombre.
- find {"name": str, "path": str}: busca ARCHIVOS POR NOMBRE. Sin comodines: todas las palabras en cualquier orden, sin acentos ni mayúsculas ("pasaporte scout"). Con comodines: patrón ("*.py"). Más recientes primero.
- grep {"pattern": str, "path": str}: busca TEXTO DENTRO de los archivos de una carpeta (hasta 4 niveles). No sirve para buscar por nombre.
- write_file {"path": str, "content": str}: crea o reemplaza un archivo completo.
- SELECTOR común (classify, move, rename, group, tree, inspect): "filter" (texto o comodines), "ext": [..], "since"/"until": "AAAA-MM-DD", "older_days"/"newer_days": N (última modificación), "min_mb"/"max_mb": N.
- classify {"path": str, "filter"?, "exclude"?, "by"?: "type" | "ext", "by_date"?: "month" | "year", "dest"?: str, "names"?: {"Audio": "Audios"}, "other"?: bool, "rules"?: {"Carpeta": ["ext" | "*patron*"]}, "preview"?: bool}: ordena los archivos SUELTOS en subcarpetas por tipo (Imagenes, Videos, Audio, PDF, Documentos, Hojas, Presentaciones, Comprimidos, Instaladores, Codigo). "by": "ext" crea una subcarpeta por extensión en vez de por tipo. Sin selector mueve TODOS: si piden solo algunos ("los de WhatsApp") usa el selector. "names" cambia cómo se llaman las carpetas. Nunca sobrescribe ni borra.
- move {"source": str, "pattern": str | [str], "dest": str, "exclude"?, "rename"?: bool, "preview"?: bool}: mueve a dest los archivos SUELTOS de source que coinciden; lo de dentro de subcarpetas no se toca. "rename": true añade mes y año de modificación (vaso.blend -> vaso_mar25.blend).
- rename {"paths": [str], "format"?: "{name}_{mon}{yy}", "replace"?: {"from": str, "to": str}, "clean"?: bool, "pattern"?, "depth"?: 1-4, "preview"?: bool}: cambia nombres EN SU LUGAR. Piezas de format: {name} {ext} {mon} {yy} {year} {mm} {dd} {n}. "clean" = minúsculas, sin acentos ni espacios.
- tree {"paths": [str], "depth"?: 1-8 (2), "only"?: "dirs" | "empty", "txt"?: bool}: JERARQUÍA de una o varias carpetas (archivos, tamaño, fechas, [VACÍA]); úsala en vez de list repetido. "txt": true guarda la lista COMPLETA sin resumir en un .txt del proyecto.
- inspect {"paths": [str], "max_files"?: 20, "chars"?: 160}: vistazo al contenido: texto, PDF, imagen (tamaño; NO interpreta lo visual), audio/video (duración, etiquetas), zip.
- map {"paths": [str], "unit"?: "files" | "dirs", "min_group"?: 3, "txt"?: bool}: agrupa lo de la MISMA IDEA/PROYECTO (nombres, fechas, contenido) con cohesión y distancia.
- group {como map + "dest"?: str, "preview"?: bool}: mueve cada grupo a dest/<Nombre>/. Previsualiza primero.
- empties {"paths": [str], "preview"?: bool}: elimina SOLO carpetas sin archivos (undo las recrea). Rechazada si el usuario pidió no borrar.
- batch {"tool": str, "calls": [{args}, ...]}: varias llamadas de LA MISMA herramienta en un turno.
- undo {"id"?: str}: revierte el último lote de move/classify/rename/group/empties (o el lote con ese id).
- run {"command": str, "cwd": str}: ejecuta un comando bash y devuelve stdout/stderr.
- ask_human {"question": str}: úsala cuando la tarea te supere; el usuario consultará a un modelo más capaz y te devolverá la respuesta."""


def _p(args: dict, key: str = "path") -> Path:
    return Path(args.get(key) or ".").expanduser()


def _pdf_full(p: Path, pages: int) -> str | None:
    import re
    import shutil
    import subprocess
    if not shutil.which("pdftotext"):
        return None
    r = subprocess.run(["pdftotext", "-l", str(pages), "-q", str(p), "-"], capture_output=True, text=True, timeout=30)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", r.stdout)).strip()


async def read_file(args: dict, max_out: int) -> str:
    p = _p(args)
    if p.suffix.lower() == ".pdf":
        pages = max(1, min(int(args.get("pages", 30) or 30), 60))
        text = await asyncio.to_thread(_pdf_full, p, pages)
        if text is None:
            return "ERROR: falta pdftotext (sudo apt install poppler-utils) para leer PDFs."
        if not text:
            return f"PDF sin texto extraíble ({p.name}): puede ser un escaneo."
        limit = min(int(args.get("max_chars", 12000) or 12000), 16000)
        extra = f"\n…[recortado, {len(text)} caracteres en total]" if len(text) > limit else ""
        return text[:limit] + extra
    head = p.open("rb").read(4096)
    if b"\x00" in head:
        kind = {b"PK": "zip", b"\x7fE": "ejecutable ELF", b"%P": "pdf", b"\x89P": "png", b"\xff\xd8": "jpg"}.get(head[:2], "binario")
        return (f"ARCHIVO BINARIO ({kind}, {p.stat().st_size} bytes): no se puede leer como texto. "
                + ("Contenido: run 'unzip -l <path>'." if kind == "zip" else ""))
    limit = min(int(args.get("max_chars", max_out)), max_out)
    text = p.read_text(errors="replace")
    extra = f"\n…[recortado, {len(text)} caracteres en total]" if len(text) > limit else ""
    return text[:limit] + extra


def _is_dir_safe(e) -> bool:
    try:
        return e.is_dir()           # un enlace roto da False, no crashea
    except OSError:
        return False

def _mtime_safe(e) -> float:
    for follow in (True, False):    # enlace colgante: stat() falla, lstat() del propio enlace sí sirve
        try:
            return e.stat(follow_symlinks=follow).st_mtime
        except (OSError, TypeError):
            continue
    return 0.0

async def list_dir(args: dict, max_out: int) -> str:
    p = _p(args)
    show_hidden = bool(args.get("hidden"))
    entries = [e for e in p.iterdir() if show_hidden or not e.name.startswith(".")]
    files = sorted((e for e in entries if not _is_dir_safe(e)), key=_mtime_safe, reverse=True)
    dirs = sorted((e.name for e in entries if _is_dir_safe(e)), key=str.lower)
    hidden = 0 if show_hidden else sum(1 for e in p.iterdir() if e.name.startswith("."))
    if len(files) > 60:  # muchos archivos: resumen por extensión + ejemplos (no inundar el contexto)
        from collections import Counter
        ext = Counter((f.suffix.lower() or "(sin ext)") for f in files)
        resumen = ", ".join(f"{e}×{n}" for e, n in ext.most_common(15))
        ejemplos = ", ".join(f.name for f in files[:8])
        out = [f"ARCHIVOS ({len(files)}). Por extensión: {resumen}",
               f"Ejemplos (más recientes): {ejemplos}",
               "(son muchos: para organizarlos usa classify o move con patrones, primero con "
               "\"preview\": true; no necesitas la lista completa de nombres)"]
    else:
        out = [f"ARCHIVOS ({len(files)}, del más reciente al más viejo): "
               + (", ".join(f.name for f in files) or "(ninguno)")]
    out.append(f"CARPETAS ({len(dirs)}): " + (", ".join(dirs[:150]) or "(ninguna)"))
    if len(dirs) > 150:
        out.append(f"…y {len(dirs) - 150} carpetas más")
    if hidden:
        out.append(f"({hidden} entradas ocultas sin mostrar; \"hidden\": true los incluye)")
    return "\n".join(out)[:max_out]


# carpetas enormes que no vale la pena recorrer (en tu home: VMs, cachés, snaps, entornos virtuales)
HEAVY_DIRS = [".git", "node_modules", ".cache", "snap", ".local", ".venv", "venv", "__pycache__",
              "VirtualBox VMs", ".npm", ".cargo", ".rustup", ".gradle", ".m2", "Library", ".steam"]


def _prune_args() -> list[str]:
    prune: list[str] = []
    for d in HEAVY_DIRS:
        prune += (["-o"] if prune else []) + ["-name", d]
    return ["(", *prune, ")", "-prune", "-o"]


async def grep_text(args: dict, max_out: int) -> str:
    import shlex
    find = ["find", str(_p(args)), "-maxdepth", "4", *_prune_args(), "-type", "f", "-size", "-2M", "-print0"]
    grep = ["xargs", "-0", "-r", "grep", "-nI", "-m", "5", "--", args.get("pattern", "")]
    cmd = " ".join(shlex.quote(x) for x in find) + " 2>/dev/null | " + " ".join(shlex.quote(x) for x in grep) + " | head -n 80"
    return await _run(["bash", "-c", cmd], cwd=None, timeout=30, max_out=max_out)


async def recent(args: dict, max_out: int) -> str:
    import shlex
    horas = max(1, min(int(args.get("hours", 24) or 24), 24 * 30))
    hidden = [] if args.get("hidden") else ["-path", "*/.*", "-prune", "-o"]  # .config, .mozilla… se reescriben cada minuto
    find = ["find", str(_p(args)), "-maxdepth", "4", *_prune_args(), *hidden, "-type", "f",
            "-mmin", f"-{horas * 60}", "-printf", "%T@ %TY-%Tm-%Td %TH:%TM  %p\\n"]
    cmd = " ".join(shlex.quote(x) for x in find) + " 2>/dev/null | sort -rn | cut -d' ' -f2- | head -n 40"
    out = await _run(["bash", "-c", cmd], cwd=None, timeout=20, max_out=max_out)
    body = out.splitlines()[1:]
    if not any(l.strip() for l in body) and "timeout" not in out:
        return out + f"\n(nada modificado en las últimas {horas} h)"
    return out


def _norm(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if not unicodedata.combining(c))


def _find_tokens(root: Path, name: str, depth: int = 6) -> list[str]:
    """Nombres que contienen todas las palabras, en cualquier orden, sin distinguir mayúsculas ni acentos."""
    import datetime as dt
    import os
    import re
    toks = [_norm(t) for t in re.split(r"[\s_\-.]+", name) if t]
    hits: list[tuple[float, str]] = []
    base = len(root.parts)
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in HEAVY_DIRS and not x.startswith(".")]
        if len(Path(d).parts) - base >= depth:
            dirs[:] = []
        for f in files:
            n = _norm(f)
            if all(t in n for t in toks):
                p = os.path.join(d, f)
                try:
                    hits.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    hits.sort(reverse=True)
    return [f"{dt.datetime.fromtimestamp(m):%Y-%m-%d %H:%M}  {p}" for m, p in hits]


async def find(args: dict, max_out: int) -> str:
    name = args.get("name", "*")
    if not any(c in name for c in "*?[") and name.strip():
        lines = await asyncio.to_thread(_find_tokens, _p(args), name)
        if not lines:
            return f"[find {name!r}] (sin resultados con todas las palabras; probar menos palabras)"
        return (f"[find {name!r}] {len(lines)} resultados, más recientes primero\n" + "\n".join(lines[:30])
                + (f"\n…y {len(lines) - 30} más" if len(lines) > 30 else ""))
    out = await _run(
        ["find", str(_p(args)), "-maxdepth", "6", *_prune_args(),
         "-iname", args.get("name", "*"), "-print"],
        cwd=None, timeout=20, max_out=max_out,
    )
    lines = out.splitlines()
    body = [l for l in lines[1:] if l.strip()]
    if not body and "timeout" not in out:
        return out + "\n(sin resultados; probar comodines, p. ej. \"*name*\")"
    return "\n".join(lines[:1] + body[:50]) + (f"\n…y {len(body) - 50} más" if len(body) > 50 else "")


async def write_file(args: dict, max_out: int) -> str:
    p = _p(args)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    if p.exists():
        p.with_suffix(p.suffix + ".orbital.bak").write_bytes(p.read_bytes())  # respaldo antes de pisar
    p.write_text(content)
    return f"Escrito {p} ({len(content)} caracteres). Respaldo previo: {p.name}.orbital.bak si existía."


async def run(args: dict, max_out: int, timeout: int = 60, sandbox_argv: list[str] | None = None) -> str:
    cwd = _p(args, "cwd")
    argv = sandbox_argv or ["bash", "-lc", args.get("command", "")]
    return await _run(argv, cwd=cwd if cwd.is_dir() else None, timeout=timeout, max_out=max_out)


async def _run(argv: list[str], cwd: Path | None, timeout: int, max_out: int) -> str:
    import os, re
    # los comandos del agente no heredan keys ni tokens del entorno de orbitald
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"KEY|TOKEN|SECRET|PASSW|PWD|CREDENTIAL", k, re.I)}
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"[timeout tras {timeout}s]"
    text = out.decode(errors="replace")
    if len(text) > max_out:
        text = text[: max_out // 2] + "\n…[recortado]…\n" + text[-max_out // 2 :]
    return f"[código de salida {proc.returncode}]\n{text}"


REGISTRY = {
    "read_file": read_file,
    "list": list_dir,
    "grep": grep_text,
    "find": find,
    "recent": recent,
    "write_file": write_file,
    "run": run,
}
