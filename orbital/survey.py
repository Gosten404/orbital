"""Árbol, inspección y mapa de relaciones entre archivos y carpetas.

Todo es determinista (sin modelo): un 4b en CPU no sirve para comparar miles de nombres, pero el código sí,
en segundos. El modelo solo decide QUÉ pedir; aquí se calcula.

  tree          jerarquía de una o varias carpetas, con filtros (nombre, extensión, fechas) y carpetas vacías
  inspect       metadatos y un vistazo al contenido (texto, PDF, imágenes, audio/video, zip)
  map           grupos de archivos o carpetas con "la misma idea": nombres parecidos (con peso por rareza),
                fechas cercanas, misma extensión y contenido (texto/PDF). Da cohesión y distancia entre grupos.

Límite honesto: no interpreta el CONTENIDO VISUAL de las imágenes ni el audio (un modelo local de texto no lo
ve). De una imagen usa tamaño, formato, fecha y nombre; de un audio, duración y etiquetas si hay ffprobe.
"""
from __future__ import annotations

import fnmatch
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Callable

DATA_DIR: Path | None = None        # respaldo si no hay proyecto
OUT_DIR: Path | None = None         # lo fija Brain por tarea: <proyecto>/listas (listas completas .txt)
Skip = Callable[[Path], bool]
MAX_SCAN = 250_000                  # archivos que se recorren como máximo en una llamada
PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}   # se cuentan, no se recorren


# ---------------------------------------------------------------- utilidades
def roots(args: dict) -> list[Path]:
    r = args.get("paths") or args.get("path") or args.get("source") or "."
    r = [r] if isinstance(r, str) else [str(x) for x in r]
    return [Path(x).expanduser() for x in r]


def size_h(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


def day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def name_filter(f) -> Callable[[str], bool] | None:
    """'texto' = el nombre lo contiene; con comodines (* ? [) = glob. Sin distinguir mayúsculas."""
    if not f:
        return None
    pats = [f] if isinstance(f, str) else [str(x) for x in f]
    pats = [p.strip().lower() for p in pats if p and p.strip()]
    if not pats:
        return None
    globs = [p if any(c in p for c in "*?[") else f"*{p}*" for p in pats]
    return lambda name: any(fnmatch.fnmatch(name.lower(), g) for g in globs)


def _date(s) -> float | None:
    try:
        return time.mktime(time.strptime(str(s)[:10], "%Y-%m-%d")) if s else None
    except ValueError:
        return None


def _skip_fast(skip, name: str) -> bool:
    fast = getattr(skip, "fast", None)
    return bool(fast and fast(name))


def out_dir() -> Path:
    d = OUT_DIR or (DATA_DIR or Path(tempfile.gettempdir()) / "orbital") / "listas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _exts(ex) -> set[str]:
    return {str(x).lower().lstrip(".") for x in ([ex] if isinstance(ex, str) else (ex or [])) if x}


def file_selector(args: dict, names: bool = True):
    """Selector COMÚN a todas las herramientas: nombre ('filter'), extensión ('ext'), fechas ('since','until',
    'older_days','newer_days' sobre la última modificación) y tamaño ('min_mb','max_mb').
    Devuelve f(nombre, tamaño, mtime) -> bool, o None si no hay ningún criterio."""
    keep = name_filter(args.get("filter")) if names else None
    exts = _exts(args.get("ext"))
    t0, t1 = _date(args.get("since")), _date(args.get("until"))
    now = time.time()

    def num(k):
        try:
            return float(args[k]) if args.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    older, newer, lo, hi = num("older_days"), num("newer_days"), num("min_mb"), num("max_mb")
    if not (keep or exts or t0 is not None or t1 is not None or older is not None or newer is not None
            or lo is not None or hi is not None):
        return None

    def ok(name: str, size: int, mt: float) -> bool:
        return ((keep is None or keep(name)) and (not exts or Path(name).suffix.lower().lstrip(".") in exts)
                and (t0 is None or mt >= t0) and (t1 is None or mt < t1 + 86400)
                and (older is None or mt <= now - older * 86400) and (newer is None or mt >= now - newer * 86400)
                and (lo is None or size >= lo * 1048576) and (hi is None or size <= hi * 1048576))
    return ok


def path_selector(args: dict, names: bool = False):
    """Lo mismo, sobre un Path (hace stat)."""
    sel = file_selector(args, names)
    if sel is None:
        return None

    def ok(p: Path) -> bool:
        try:
            st = p.stat()
        except OSError:
            return False
        return sel(p.name, st.st_size, st.st_mtime)
    return ok


# ---------------------------------------------------------------- recorrido
class Node:
    __slots__ = ("path", "name", "dirs", "files", "nfiles", "nmatch", "size", "tmin", "tmax",
                 "blocked", "pruned", "err")

    def __init__(self, path: Path):
        self.path, self.name = path, path.name or str(path)
        self.dirs: list[Node] = []
        self.files: list[tuple[str, int, float]] = []   # archivos que pasan los filtros (acotado salvo txt)
        self.nfiles = 0                                  # archivos TOTALES del subárbol (vacía = 0)
        self.nmatch = 0                                  # archivos que pasan los filtros en el subárbol
        self.size = 0
        self.tmin = self.tmax = 0.0
        self.blocked = 0                                 # entradas vetadas/secretas dentro (no cuenta como vacía)
        self.pruned = False
        self.err = False

    @property
    def empty(self) -> bool:
        return self.nfiles == 0 and not self.blocked and not self.pruned and not self.err


def scan(path: Path, skip: Skip | None, keep: Callable[[str], bool] | None, exts: set[str] | None,
         t0: float | None, t1: float | None, budget: list[int], full: bool = False, pred=None) -> Node:
    n = Node(path)
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError:
        n.err = True
        return n
    for e in entries:
        p = Path(e.path)
        try:
            is_dir, is_file = e.is_dir(follow_symlinks=False), e.is_file(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if skip and skip(p):
                n.blocked += 1
                continue
            if e.name in PRUNE:
                c = Node(p); c.pruned = True
            else:
                c = scan(p, skip, keep, exts, t0, t1, budget, full, pred)
            n.dirs.append(c)
            n.nfiles += c.nfiles + (1 if c.pruned else 0)
            n.nmatch += c.nmatch
            n.size += c.size
            for t in (c.tmin, c.tmax):
                if t:
                    n.tmin = t if not n.tmin else min(n.tmin, t)
                    n.tmax = max(n.tmax, t)
        elif is_file:
            if _skip_fast(skip, e.name):
                n.blocked += 1
                continue
            budget[0] -= 1
            if budget[0] < 0:
                n.err = True
                break
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            n.nfiles += 1
            ok = ((keep is None or keep(e.name)) and (not exts or Path(e.name).suffix.lower().lstrip(".") in exts)
                  and (t0 is None or st.st_mtime >= t0) and (t1 is None or st.st_mtime < t1 + 86400)
                  and (pred is None or pred(e.name, st.st_size, st.st_mtime)))
            if ok:
                n.nmatch += 1
                n.size += st.st_size
                n.tmin = st.st_mtime if not n.tmin else min(n.tmin, st.st_mtime)
                n.tmax = max(n.tmax, st.st_mtime)
                if full or len(n.files) < 400:
                    n.files.append((e.name, st.st_size, st.st_mtime))
        else:
            n.blocked += 1   # enlace simbólico u objeto raro: una carpeta que lo contiene NO está vacía
    return n


def _walk(n: Node):
    yield n
    for c in n.dirs:
        yield from _walk(c)


def empties(root: Node) -> list[Node]:
    """Todas las carpetas SIN ningún archivo en su subárbol (la raíz no cuenta), de más profunda a menos."""
    out = [c for c in _walk(root) if c is not root and c.empty]
    return sorted(out, key=lambda c: -len(c.path.parts))


def _render(n: Node, depth: int, filt: bool, ocultos: bool, solo: str, lines: list[str], lvl: int = 0) -> None:
    pad = "  " * lvl
    count = n.nmatch if filt else n.nfiles
    rng = f" · {day(n.tmin)}→{day(n.tmax)}" if n.tmin and count else ""
    flag = "  [VACÍA]" if n.empty and lvl > 0 else ""
    lines.append(f"{pad}{n.name}/  {count} arch · {size_h(n.size)}{rng}{flag}")
    if lvl >= depth:
        if n.dirs:
            lines[-1] += f"  (+{len(n.dirs)} subcarpetas)"
        return
    if solo != "dirs" and n.files and solo != "empties":
        names = [f[0] for f in n.files if ocultos or not f[0].startswith(".")]
        if len(names) <= 6:
            if names:
                lines.append(f"{pad}  · " + ", ".join(names))
        else:
            ext = Counter((Path(x).suffix.lower() or "(sin ext)") for x in names)
            lines.append(f"{pad}  · {len(names)} archivos: " + ", ".join(f"{e}×{c}" for e, c in ext.most_common(5))
                         + " | ej: " + ", ".join(names[:3]))
    for c in n.dirs:
        if not ocultos and c.name.startswith("."):
            continue
        if filt and c.nmatch == 0:
            continue
        if solo == "empties" and not (c.empty or any(d.empty for d in _walk(c))):
            continue
        _render(c, depth, filt, ocultos, solo, lines, lvl + 1)


def _full_list(nodes: list[Node], label: str) -> tuple[Path, int]:
    p = out_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:40]}.txt"
    n = 0
    with p.open("w", encoding="utf-8") as fh:
        for root in nodes:
            for d in _walk(root):
                rel = os.path.relpath(d.path, root.path.parent)
                vac = "\tVACÍA" if d.empty and d is not root else ""
                fh.write(f"D\t{rel}/\t{d.nfiles} arch{vac}\n")
                for name, size, mt in d.files:
                    fh.write(f"F\t{os.path.join(rel, name)}\t{size}\t{day(mt)}\n")
                    n += 1
    return p, n


def tree(args: dict, max_out: int, skip: Skip | None = None) -> str:
    depth = max(1, min(8, int(args.get("depth", 2) or 2)))
    keep = name_filter(args.get("filter"))
    ex = args.get("ext")
    exts = {str(x).lower().lstrip(".") for x in ([ex] if isinstance(ex, str) else (ex or []))}
    t0, t1 = _date(args.get("since")), _date(args.get("until"))
    pred = file_selector(args, names=False)
    filt = bool(keep or exts or t0 is not None or t1 is not None or pred)
    solo = str(args.get("only") or "").lower()
    ocultos = bool(args.get("hidden"))
    full = bool(args.get("txt"))
    budget = [MAX_SCAN]
    nodes, out = [], []
    for r in roots(args):
        if not r.is_dir():
            out.append(f"ERROR: {r} no es una carpeta")
            continue
        nodes.append(scan(r, skip, keep, exts or None, t0, t1, budget, full, pred))
    for root in nodes:
        allc = [c for c in _walk(root)]
        emp = empties(root)
        head = (f"ÁRBOL {root.path} · {root.nfiles} archivos en {len(allc) - 1} carpetas · {size_h(root.size)} · "
                f"{len(emp)} carpetas vacías" + (f" · {root.nmatch} coinciden con el filtro" if filt else ""))
        if root.err:
            head += " · [!] recorrido incompleto (límite o permisos)"
        d = depth
        while True:   # si no cabe, baja la profundidad antes que cortar a la mitad
            lines: list[str] = []
            _render(root, d, filt, ocultos, solo, lines)
            body = "\n".join(lines)
            if len(body) <= max_out - 700 or d == 1:
                break
            d -= 1
        out.append(head + (f"  [profundidad {d}: ajuste de tamaño]" if d < depth else ""))
        out.append(body)
        if emp and solo != "dirs":
            tops = [e for e in emp if not any(o is not e and o.path in e.path.parents for o in emp)]
            tops = sorted(tops, key=lambda c: str(c.path))
            out.append(f"VACÍAS ({len(emp)}; de primer nivel {len(tops)}): "
                       + ", ".join(os.path.relpath(e.path, root.path) for e in tops[:25])
                       + (" …" if len(tops) > 25 else ""))
    if full and nodes:
        p, n = _full_list(nodes, nodes[0].name)
        out.append(f"LISTA COMPLETA sin resumir ({n} archivos): {p}")
    if not nodes and not out:
        out.append("ERROR: no hay carpetas que recorrer")
    text = "\n".join(out)
    return text if len(text) <= max_out else text[:max_out - 40] + "\n…[recortado: filter o txt]"


# ---------------------------------------------------------------- inspect
TEXT_EXT = {"txt", "md", "py", "js", "ts", "json", "csv", "tsv", "html", "css", "log", "yaml", "yml", "toml",
            "sh", "ini", "cfg", "xml", "sql", "c", "cpp", "h", "java", "rs", "go", "rpy", "mcmeta", "lang"}
IMG_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MEDIA_EXT = {"mp3", "m4a", "ogg", "opus", "wav", "flac", "aac", "mp4", "mkv", "webm", "mov", "avi", "m4v"}


def image_size(p: Path) -> str | None:
    """Ancho x alto leyendo solo la cabecera (sin Pillow)."""
    try:
        with p.open("rb") as f:
            h = f.read(32)
            if h[:8] == b"\x89PNG\r\n\x1a\n":
                w, hh = struct.unpack(">II", h[16:24])
                return f"{w}x{hh}"
            if h[:6] in (b"GIF87a", b"GIF89a"):
                w, hh = struct.unpack("<HH", h[6:10])
                return f"{w}x{hh}"
            if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
                k = h[12:16]
                if k == b"VP8X":
                    return f"{1 + int.from_bytes(h[24:27], 'little')}x{1 + int.from_bytes(h[27:30], 'little')}"
                if k == b"VP8L":
                    b = int.from_bytes(h[21:25], "little")
                    return f"{1 + (b & 0x3FFF)}x{1 + ((b >> 14) & 0x3FFF)}"
                if k == b"VP8 ":
                    w, hh = struct.unpack("<HH", h[26:30])
                    return f"{w & 0x3FFF}x{hh & 0x3FFF}"
                return None
            if h[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    m = f.read(4)
                    if len(m) < 4 or m[0] != 0xFF:
                        return None
                    if m[1] in (0xC0, 0xC1, 0xC2, 0xC3):
                        d = f.read(5)
                        hh, w = struct.unpack(">HH", d[1:5])
                        return f"{w}x{hh}"
                    f.seek(struct.unpack(">H", m[2:4])[0] - 2, 1)
    except (OSError, struct.error):
        return None
    return None


def _run(argv: list[str], timeout: float = 8) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def pdf_text(p: Path, chars: int = 3000, pages: int = 2) -> str:
    if not shutil.which("pdftotext"):
        return ""
    return re.sub(r"\s+", " ", _run(["pdftotext", "-l", str(pages), "-q", str(p), "-"]))[:chars].strip()


def media_info(p: Path) -> str:
    if not shutil.which("ffprobe"):
        return "(sin ffprobe: duración y etiquetas no disponibles)"
    o = _run(["ffprobe", "-v", "error", "-show_entries",
              "format=duration:format_tags=title,artist,album:stream=width,height", "-of", "default=nw=1", str(p)])
    d = dict(x.split("=", 1) for x in o.splitlines() if "=" in x)
    bits = []
    if d.get("duration"):
        try:
            s = float(d["duration"]); bits.append(f"{int(s // 60)}:{int(s % 60):02d}")
        except ValueError:
            pass
    if d.get("width"):
        bits.append(f"{d['width']}x{d.get('height', '?')}")
    for k in ("TAG:title", "TAG:artist", "TAG:album"):
        if d.get(k):
            bits.append(f"{k[4:]}={d[k][:40]}")
    return ", ".join(bits) or "(sin metadatos legibles)"


def peek(p: Path, chars: int) -> str:
    ext = p.suffix.lower().lstrip(".")
    try:
        if ext in IMG_EXT:
            return "imagen " + (image_size(p) or "?") + " (contenido visual no interpretado)"
        if ext == "pdf":
            t = pdf_text(p, chars)
            return ("pdf: " + t) if t else "pdf (sin texto extraíble o sin pdftotext)"
        if ext in MEDIA_EXT:
            return "medio " + media_info(p)
        if ext in ("zip", "jar"):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
            return f"zip {len(names)} entradas: " + ", ".join(names[:4])
        if ext in TEXT_EXT or p.stat().st_size < 4096:
            with p.open("rb") as f:
                raw = f.read(chars * 2)
            if b"\x00" in raw:
                return "binario"
            return "texto: " + re.sub(r"\s+", " ", raw.decode("utf-8", "replace"))[:chars].strip()
    except (OSError, zipfile.BadZipFile):
        return "(ilegible)"
    return "binario"


def _collect_files(rts: list[Path], depth: int, keep, skip: Skip | None, limit: int = 100_000, pred=None) -> list[Path]:
    out: list[Path] = []

    def rec(d: Path, lvl: int):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if len(out) >= limit:
                return
            p = Path(e.path)
            try:
                if e.is_dir(follow_symlinks=False):
                    if lvl < depth and e.name not in PRUNE and not (skip and skip(p)):
                        rec(p, lvl + 1)
                elif e.is_file(follow_symlinks=False):
                    if _skip_fast(skip, e.name) or (keep and not keep(e.name)):
                        continue
                    if pred is not None:
                        st = e.stat(follow_symlinks=False)
                        if not pred(e.name, st.st_size, st.st_mtime):
                            continue
                    out.append(p)
            except OSError:
                continue

    for r in rts:
        if r.is_file():
            if not (skip and skip(r)):
                out.append(r)
        elif r.is_dir():
            rec(r, 1)
    return out


def inspect_files(args: dict, max_out: int, skip: Skip | None = None) -> str:
    files = _collect_files(roots(args), max(1, int(args.get("depth", 1) or 1)),
                           name_filter(args.get("filter")), skip, pred=file_selector(args, names=False))
    if not files:
        return "(sin coincidencias)"
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    n = max(1, min(60, int(args.get("max_files", 20) or 20)))
    chars = max(40, min(600, int(args.get("chars", 160) or 160)))
    lines = [f"INSPECCIÓN: {len(files)} archivos (mostrados {min(n, len(files))}, más recientes primero)"]
    for p in files[:n]:
        st = p.stat()
        lines.append(f"- {p.name} | {size_h(st.st_size)} | {day(st.st_mtime)} | {peek(p, chars)}")
    if len(files) > n:
        lines.append(f"…+{len(files) - n} más (max_files o filter)")
    text = "\n".join(lines)
    return text if len(text) <= max_out else text[:max_out - 30] + "\n…[recortado]"


# ---------------------------------------------------------------- mapa de similitud
STOP = set("""img imagen image foto photo screenshot captura copia copy final nuevo nueva new old viejo test prueba
untitled sin titulo archivo file documento document download descarga descargas whatsapp video audio the and for
con del las los una uno que por para from with version ver backup respaldo temp tmp""".split())


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def tokens(name: str, minlen: int = 3) -> list[str]:
    """Palabras del nombre: sin extensión, sin números (las fechas se comparan aparte), partidas por
    separadores y por CamelCase. 'cuerda3-v2.blend' y 'Cuerda_final.blend' comparten 'cuerda'."""
    stem = Path(name).stem
    stem = re.sub(r"([a-záéíóúñ])([A-ZÁÉÍÓÚÑ])", r"\1 \2", stem)
    out = []
    for w in re.split(r"[^A-Za-zÁÉÍÓÚáéíóúÑñ]+", stem):
        w = _norm(w)
        if len(w) >= minlen and w not in STOP:
            out.append(w)
    return out


class Item:
    __slots__ = ("name", "path", "is_dir", "mtime", "size", "ext", "tf", "text")

    def __init__(self, name, path, is_dir, mtime, size, ext, tf, text):
        self.name, self.path, self.is_dir, self.mtime, self.size, self.ext = name, path, is_dir, mtime, size, ext
        self.tf: Counter = tf
        self.text: Counter | None = text


def _text_tf(p: Path) -> Counter | None:
    ext = p.suffix.lower().lstrip(".")
    raw = ""
    try:
        if ext == "pdf":
            raw = pdf_text(p, 6000)
        elif ext in TEXT_EXT and p.stat().st_size <= 300_000:
            with p.open("rb") as f:
                b = f.read(6000)
            raw = "" if b"\x00" in b else b.decode("utf-8", "replace")
    except OSError:
        return None
    if not raw:
        return None
    c = Counter(w for w in tokens(raw.replace("_", " "), 4))
    return Counter(dict(c.most_common(40))) if c else None


def collect_items(args: dict, skip: Skip | None) -> list[Item]:
    unidad = str(args.get("unit") or "files").lower()
    keep = name_filter(args.get("filter"))
    use_text = args.get("content", True) not in (False, "no", "false", 0)
    items: list[Item] = []
    if unidad.startswith("dir"):
        for r in roots(args):
            if not r.is_dir():
                continue
            for e in sorted(os.scandir(r), key=lambda e: e.name.lower()):
                p = Path(e.path)
                try:
                    if not e.is_dir(follow_symlinks=False) or e.name.startswith(".") or e.name in PRUNE:
                        continue
                except OSError:
                    continue
                if (skip and skip(p)) or (keep and not keep(e.name)):
                    continue
                sub = scan(p, skip, None, None, None, None, [20_000], full=True)
                if sub.nfiles == 0 or sub.blocked:
                    continue   # vacías: las trata 'empties'; con vetados dentro: no se tocan
                tf: Counter = Counter()
                for t in tokens(e.name):
                    tf[t] += 3
                names = [f[0] for d in _walk(sub) for f in d.files][:60]
                for nm in names:
                    for t in set(tokens(nm)):
                        tf[t] += 1
                ext = Counter(Path(nm).suffix.lower() for nm in names).most_common(1)
                items.append(Item(e.name, p, True, sub.tmax or e.stat().st_mtime, sub.size,
                                  ext[0][0] if ext else "", tf, None))
        return items
    depth = max(1, int(args.get("depth", 1) or 1))
    for p in _collect_files(roots(args), depth, keep, skip, pred=file_selector(args, names=False)):
        try:
            st = p.stat()
        except OSError:
            continue
        tf = Counter(tokens(p.name))
        if depth > 1:   # el nombre de la carpeta contenedora también dice de qué va
            for t in tokens(p.parent.name):
                tf[t] += 0.5
        items.append(Item(p.name, p, False, st.st_mtime, st.st_size, p.suffix.lower(), tf, None))
    if use_text and len(items) <= 600:
        for it in items:
            if it.ext.lstrip(".") in TEXT_EXT or it.ext == ".pdf":
                it.text = _text_tf(it.path)
    return items


def _wj(a: Counter, b: Counter, idf: dict[str, float], lam: float = 1.0) -> float:
    """Jaccard ponderado por rareza. Compartir 'cuerda' (rara) pesa más que compartir 'minecraft' (común).
    lam < 1 perdona las palabras NO compartidas: 'mono_head' y 'mono_body' son del mismo proyecto aunque
    cada uno añada su descriptor."""
    num = den = 0.0
    for k in set(a) | set(b):
        w = idf.get(k, 1.0)
        x, y = a.get(k, 0), b.get(k, 0)
        if x and y:
            num += w * min(x, y)
            den += w * max(x, y)
        else:
            den += lam * w * max(x, y)
    return num / den if den else 0.0


def similarity(a: Item, b: Item, idf: dict[str, float], tau_days: float = 10.0) -> float:
    """0..1. Sin vínculo de NOMBRE o CONTENIDO no hay similitud (fechas y extensión solo la modulan:
    todo lo descargado el mismo día no es 'del mismo proyecto')."""
    name = _wj(a.tf, b.tf, idf, 0.35)
    cont = _wj(a.text, b.text, idf) if a.text and b.text else 0.0
    sem = max(name, 0.85 * cont)
    if sem <= 0:
        return 0.0
    tsim = math.exp(-abs(a.mtime - b.mtime) / 86400 / tau_days)
    return sem * (0.8 + 0.15 * tsim + 0.05 * (1.0 if a.ext == b.ext else 0.0))


class Group:
    def __init__(self, members: list[int], label: str, cohesion: float):
        self.members, self.label, self.cohesion = members, label, cohesion
        self.neighbours: list[tuple[int, float]] = []


def cluster(items: list[Item], theta: float = 0.30, min_group: int = 3, max_frac: float = 0.4):
    n = len(items)
    df: Counter = Counter()
    for it in items:
        df.update(set(it.tf) | set(it.text or ()))
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
    index: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        for t in set(it.tf) | set(sorted(it.text or (), key=lambda k: -(it.text or {}).get(k, 0))[:12]):
            index.setdefault(t, []).append(i)
    pairs: set[tuple[int, int]] = set()
    for ids in index.values():
        if len(ids) <= 120:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    pairs.add((ids[x], ids[y]))
        else:   # palabra muy repetida (proyecto grande): cadena por fecha, cada uno con sus 4 vecinos en el tiempo
            ids = sorted(ids, key=lambda i: items[i].mtime)
            for x in range(len(ids)):
                for y in range(x + 1, min(x + 5, len(ids))):
                    pairs.add((min(ids[x], ids[y]), max(ids[x], ids[y])))
    edges = {pr: similarity(items[pr[0]], items[pr[1]], idf) for pr in pairs}
    edges = {pr: s for pr, s in edges.items() if s >= theta}

    def components(th: float, nodes: set[int]) -> list[list[int]]:
        parent = {i: i for i in nodes}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for (a, b), s in edges.items():
            if s >= th and a in nodes and b in nodes:
                parent[find(a)] = find(b)
        comp: dict[int, list[int]] = {}
        for i in nodes:
            comp.setdefault(find(i), []).append(i)
        return list(comp.values())

    limit = max(40, int(n * max_frac))
    todo, done, th = components(theta, set(range(n))), [], theta
    while todo:
        c = todo.pop()
        if len(c) > limit and th < 0.9:   # componente enorme = cadena de parecidos: se parte subiendo el umbral
            sub_th = th
            parts = [c]
            for _ in range(5):
                sub_th += 0.1
                parts = components(sub_th, set(c))
                if max(len(p) for p in parts) <= limit:
                    break
            done.extend(parts)
        else:
            done.append(c)
    groups, singles = [], []
    for c in done:
        if len(c) < min_group:
            singles.extend(c)
            continue
        share: Counter = Counter()          # en cuántos miembros aparece cada palabra
        for i in c:
            share.update(set(items[i].tf))
        ranked = sorted(share, key=lambda t: (-share[t], -idf.get(t, 1.0), t))
        label = ranked[0] if ranked else (items[c[0]].ext.lstrip(".") or "grupo")
        if len(ranked) > 1 and share[ranked[1]] >= 0.8 * len(c) and share[ranked[1]] == share[ranked[0]]:
            label = f"{ranked[0]}-{ranked[1]}"   # dos palabras que TODOS comparten ("cuerda-nudo")
        cs = [s for (a, b), s in edges.items() if a in c and b in c] if len(c) < 400 else []
        groups.append(Group(sorted(c, key=lambda i: items[i].name.lower()), label.capitalize(),
                            sum(cs) / len(cs) if cs else 0.0))
    groups.sort(key=lambda g: -len(g.members))
    # distancia entre grupos: parecido de su vocabulario medio, atenuado por lejanía en el tiempo
    prof = []
    for g in groups:
        tf: Counter = Counter()
        for i in g.members:
            for t, v in items[i].tf.items():
                tf[t] += v / len(g.members)
        prof.append((tf, sum(items[i].mtime for i in g.members) / len(g.members)))
    for a, g in enumerate(groups):
        ds = []
        for b in range(len(groups)):
            if a != b:
                tsim = math.exp(-abs(prof[a][1] - prof[b][1]) / 86400 / 30)
                ds.append((b, round(1 - _wj(prof[a][0], prof[b][0], idf, 0.5) * (0.7 + 0.3 * tsim), 2)))
        g.neighbours = [x for x in sorted(ds, key=lambda x: x[1])[:2] if x[1] < 0.95]
    return groups, sorted(singles), idf


def _safe_label(s: str) -> str:
    return re.sub(r"[^\w\- ]+", "", s).strip().replace(" ", "-")[:40] or "Grupo"


def map_groups(args: dict, max_out: int, skip: Skip | None = None) -> str:
    items = collect_items(args, skip)
    if len(items) < 2:
        return "(menos de 2 elementos)"
    mg = max(2, int(args.get("min_group", 3) or 3))
    groups, singles, _ = cluster(items, float(args.get("threshold", 0.30) or 0.30), mg)
    unidad = "carpetas" if items[0].is_dir else "archivos"
    lines = [f"MAPA · {len(items)} {unidad} · {len(groups)} grupos (mín. {mg}) · "
             f"{len(singles)} sin grupo · cohesión 0-1 (alta = parecidos) · distancia 0-1 (baja = cercanos)"]
    for k, g in enumerate(groups, 1):
        its = [items[i] for i in g.members]
        ext = ", ".join(f"{e or '(sin ext)'}×{c}" for e, c in Counter(i.ext for i in its).most_common(3))
        near = "; ".join(f"#{b + 1} «{groups[b].label}» d={d}" for b, d in g.neighbours)
        lines.append(f"#{k} «{g.label}» · {len(its)} {unidad} · {ext} · {day(min(i.mtime for i in its))}→"
                     f"{day(max(i.mtime for i in its))} · cohesión {g.cohesion:.2f}" + (f" · cerca de {near}" if near else ""))
        lines.append("    " + ", ".join(i.name for i in its[:6]) + (f", …(+{len(its) - 6})" if len(its) > 6 else ""))
    if args.get("txt"):
        p = out_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-map.txt"
        with p.open("w", encoding="utf-8") as fh:
            for k, g in enumerate(groups, 1):
                fh.write(f"## #{k} {g.label} ({len(g.members)})\n")
                for i in g.members:
                    fh.write(f"{items[i].path}\n")
            fh.write(f"## sin grupo ({len(singles)})\n")
            for i in singles:
                fh.write(f"{items[i].path}\n")
        lines.append(f"LISTA COMPLETA de grupos y miembros: {p}")
    lines.append("group mueve cada grupo a su carpeta (preview primero)")
    text = "\n".join(lines)
    return text if len(text) <= max_out else text[:max_out - 40] + "\n…[recortado: min_group o txt]"
