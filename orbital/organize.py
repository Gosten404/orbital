"""Organización nativa de archivos: move / classify / undo SIN que el modelo escriba bash.

Garantías (no dependen del modelo):
- Nunca sobrescribe: si el destino existe, usa "nombre (1).ext", "nombre (2).ext"...
- Nunca borra. Mover no destruye nada y cada lote queda en una bitácora para deshacerlo.
- Previsualización (dry-run): con "preview": true no toca nada y devuelve el plan.
- Solo el primer nivel de la carpeta (no recursivo): lo que ya está en subcarpetas no se toca.
- Ignora ocultos, carpetas, enlaces simbólicos, descargas a medias (.part, .crdownload) y lo que
  core marque como secreto o protegido.

Por qué bitácora y no copia de respaldo: copiar 2550 archivos duplica el disco y tarda; un mover
dentro del mismo disco es un rename atómico. La bitácora (origen -> destino) basta para revertir.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

# Categorías por defecto de clasificar (nombres sin acentos: menos líos en scripts y terminal).
CATEGORIES: dict[str, list[str]] = {
    "Imagenes": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "tif", "tiff", "ico", "avif"],
    "Videos": ["mp4", "mkv", "webm", "avi", "mov", "m4v", "3gp", "flv", "wmv"],
    "Audio": ["mp3", "ogg", "opus", "wav", "flac", "m4a", "aac", "wma"],
    "PDF": ["pdf"],
    "Documentos": ["doc", "docx", "odt", "rtf", "txt", "md", "epub"],
    "Hojas": ["xls", "xlsx", "ods", "csv", "tsv"],
    "Presentaciones": ["ppt", "pptx", "odp", "key"],
    "Comprimidos": ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "tar.gz", "tar.xz", "tar.bz2", "zst"],
    "Instaladores": ["deb", "appimage", "rpm", "run", "flatpakref", "snap", "exe", "msi"],
    "Codigo": ["py", "js", "ts", "sh", "c", "cpp", "h", "java", "rs", "go", "html", "css", "json", "yaml",
               "yml", "toml", "ipynb", "sql"],
}
PARTIAL = (".part", ".crdownload", ".download", ".partial", ".tmp")
UNCLASSIFIED = "Otros"

Skip = Callable[[Path], bool]


def _matcher(rule: str) -> Callable[[str], bool]:
    """'pdf' / '.pdf' / 'tar.gz' -> por extensión;  '*factura*' / 'IMG_*.jpg' -> glob sobre el nombre."""
    r = rule.strip().lower()
    if any(c in r for c in "*?["):
        return lambda name: fnmatch.fnmatch(name.lower(), r)
    ext = "." + r.lstrip(".")
    # "pdf" = extensión; "tarea.pdf" = ese archivo exacto (antes se leía como extensión ".tarea.pdf" y no movía nada)
    return lambda name: name.lower().endswith(ext) or name.lower() == r


def _name_filter(args: dict) -> Callable[[str], bool] | None:
    """'filter': solo entran archivos cuyo NOMBRE coincide. "whatsapp" = contiene; "IMG-*-WA*" = glob."""
    f = args.get("filter")
    if not f:
        return None
    pats = [f] if isinstance(f, str) else [str(x) for x in f]
    pats = [p.strip().lower() for p in pats if str(p).strip()]
    if not pats:
        return None
    globs = [p if any(c in p for c in "*?[") else f"*{p}*" for p in pats]
    return lambda name: any(fnmatch.fnmatch(name.lower(), g) for g in globs)


_DATE_RX = __import__("re").compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])(?:[-_.]?(?:0[1-9]|[12]\d|3[01]))?(?!\d)")


def _period(f: Path, mode: str) -> str:
    """Subcarpeta por fecha: la saca del NOMBRE ("WhatsApp Image 2024-03-16", "IMG-20240316-WA0001");
    si el nombre no trae fecha usa la de modificación."""
    m = _DATE_RX.search(f.name)
    if m:
        y, mo = m.group(1), m.group(2)
    else:
        try:
            t = time.localtime(f.stat().st_mtime)
        except OSError:
            return "sin-fecha"
        y, mo = f"{t.tm_year:04d}", f"{t.tm_mon:02d}"
    return y if str(mode).lower().startswith(("a", "y")) else f"{y}-{mo}"


_CAMERA_RX = __import__("re").compile(r"^\*?(img|vid|pxl|dsc|mvimg|screenshot)[-_ ]?\*?$", __import__("re").I)


def _core(p: str) -> str:
    return p.strip().lower().strip("*?.[] ")


def clean_rules(args: dict) -> tuple[dict, list[str]]:
    """Con 'filter', las reglas de categoría no pueden repetir un patrón de NOMBRE del filtro.
    El 4b puso "*IMG*" dentro de la categoría imagenes: como la primera regla que coincide gana,
    IMG_0030.MOV acabó en imagenes y no en videos. El filtro decide QUÉ se mueve; las reglas, A DÓNDE."""
    r, f = args.get("rules"), args.get("filter")
    if not (isinstance(r, dict) and r and f):
        return args, []
    fcores = {_core(x) for x in ([f] if isinstance(f, str) else [str(y) for y in f]) if _core(x)}
    out, dropped = {}, []
    for cat, pats in r.items():
        pats = [pats] if isinstance(pats, str) else [str(x) for x in pats]
        keep = []
        for p in pats:
            is_glob = any(c in p for c in "*?[") and not p.strip().startswith("*.")
            if is_glob and any(_core(p) and (_core(p) in fc or fc in _core(p)) for fc in fcores):
                dropped.append(p)
            else:
                keep.append(p)
        if keep:
            out[cat] = keep
    notes = [f"Reglas duplicadas del filtro ({', '.join(dropped)}) omitidas: el filtro decide qué se mueve; las reglas, a dónde"] if dropped else []
    return {**args, "rules": out or None}, notes


def broad_filter_note(args: dict) -> str | None:
    """Aviso cuando el filtro es un prefijo de cámara pelón (IMG*, VID*): atrapa fotos que no son de WhatsApp."""
    f = args.get("filter")
    pats = [f] if isinstance(f, str) else [str(y) for y in (f or [])]
    bad = [p for p in pats if _CAMERA_RX.match(p.strip())]
    if not bad:
        return None
    return (f"Filtro {bad} incluye también fotos y videos de cámara (IMG_0030.jpg). "
            "Patrón WhatsApp: '*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA*' (IMG, VID, AUD, PTT, DOC).")


MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
SEL_ARGS = {"ext", "since", "until", "older_days", "newer_days", "min_mb", "max_mb"}   # selector común
RENAME_OPTS = {"rename", "format", "replace", "clean"}                                  # cómo se renombra
MOVE_ARGS = {"source", "path", "paths", "pattern", "dest", "preview", "exclude", "filter"} | SEL_ARGS | RENAME_OPTS
CLASSIFY_ARGS = {"path", "dest", "rules", "filter", "exclude", "by_date", "by", "other", "preview",
               "names"} | SEL_ARGS | RENAME_OPTS
RENAME_ARGS = {"paths", "path", "source", "pattern", "filter", "exclude", "preview", "depth"} | SEL_ARGS | RENAME_OPTS
GROUP_ARGS = {"paths", "path", "source", "dest", "unit", "filter", "depth", "min_group", "threshold",
                "content", "preview", "txt"} | SEL_ARGS
EMPTIES_ARGS = {"paths", "path", "source", "preview"}


def unknown_note(args: dict, known: set[str]) -> list[str]:
    """Un argumento que la herramienta no conoce NO se ignora en silencio (así se perdió 'rename')."""
    bad = sorted(k for k in args if k not in known and k not in ("cwd",))
    return [f"Argumentos desconocidos (ignorados): {', '.join(bad)}"] if bad else []


def _truthy(v) -> bool:
    return bool(v) and str(v).lower() not in ("no", "false", "0", "ninguno", "none")


PIECES = ("name", "ext", "mon", "yy", "year", "mm", "dd", "n")


def _fmt_ok(args: dict) -> bool:
    """Formato válido: lleva llaves y todas sus piezas existen (una pieza desconocida acabaría literal en el nombre)."""
    f = args.get("format")
    return isinstance(f, str) and "{" in f and all(k in PIECES for k in re.findall(r"\{(\w+)", f))


def _replace_spec(args: dict):
    r = args.get("replace")
    if isinstance(r, dict) and r.get("from"):
        return str(r["from"]), str(r.get("to", ""))
    if isinstance(r, (list, tuple)) and len(r) == 2 and r[0]:
        return str(r[0]), str(r[1])
    return None


def _rename_wanted(args: dict) -> bool:
    """¿Hay que renombrar? Sí si piden renombrar, dan un formato válido, replace o clean."""
    return _truthy(args.get("rename")) or _fmt_ok(args) or _replace_spec(args) is not None or _truthy(args.get("clean"))


def rename_note(args: dict) -> list[str]:
    f = args.get("format")
    if f and not _fmt_ok(args):
        return [f"Formato {f!r} inválido (llaves ausentes o piezas desconocidas); piezas: " + " ".join("{%s}" % k for k in PIECES) + " "
                "(p. ej. {name}_{mon}{yy}). " + ("Aplicado name_monyy." if _truthy(args.get("rename")) else "Sin renombrar.")]
    return []


def slugify(stem: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "archivo"


def new_name(f: Path, args: dict, n: int = 1) -> str:
    """Nombre nuevo. Piezas que se combinan: 'replace' {from, to}, 'clean' (minúsculas, sin acentos ni espacios),
    y 'rename'/'format' (por defecto {name}_{mon}{yy} con la ÚLTIMA MODIFICACIÓN: Linux no guarda la creación
    de forma fiable): vaso.blend -> vaso_mar25.blend. Idempotente: si ya termina así no lo repite."""
    if not _rename_wanted(args):
        return f.name
    stem, suffix = f.stem, f.suffix
    rep = _replace_spec(args)
    if rep:
        stem = stem.replace(rep[0], rep[1])
    if _truthy(args.get("clean")):
        stem = slugify(stem)
    if _truthy(args.get("rename")) or _fmt_ok(args):
        fmt = args["format"] if _fmt_ok(args) else "{name}_{mon}{yy}"
        try:
            t = time.localtime(f.stat().st_mtime)
        except OSError:
            return f.name
        vals = {"name": stem, "ext": suffix, "mon": MESES[t.tm_mon - 1], "yy": f"{t.tm_year % 100:02d}",
                "year": str(t.tm_year), "mm": f"{t.tm_mon:02d}", "dd": f"{t.tm_mday:02d}", "n": n}

        class _D(dict):
            def __missing__(self, k):
                return "{" + k + "}"
        if not re.search(r"\{n[:}]", fmt):   # con contador {n} no hay idempotencia posible
            tail = fmt.replace("{ext}", "").format_map(_D({**vals, "name": ""}))
            if tail and stem.endswith(tail):
                return stem + suffix
        try:
            out = fmt.format_map(_D(vals))
        except (ValueError, IndexError):
            out = stem
        out = re.sub(r"[/\\\x00]", "_", out)
        return out if "{ext}" in fmt else out + suffix
    return stem + suffix


def _sel(args: dict):
    """Selector común (ext, fechas, antigüedad, tamaño) sobre Path; el nombre lo filtra cada herramienta."""
    from . import survey as mp
    return mp.path_selector(args, names=False)


def _custom_names(args: dict) -> dict[str, str]:
    """'names': {"Audio": "Audios", "Imagenes": "Fotos"} -> cómo se llaman las carpetas de cada categoría."""
    n = args.get("names")
    if not isinstance(n, dict):
        return {}
    out = {}
    for k, v in n.items():
        v = re.sub(r"[/\\\x00]", "_", str(v)).strip()
        if v and str(k).strip():
            out[str(k).strip().lower()] = v
    return out


def _candidates(folder: Path, skip: Skip | None) -> list[Path]:
    out = []
    try:
        entries = sorted(folder.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return []
    for e in entries:
        if e.name.startswith(".") or e.is_symlink() or e.name.lower().endswith(PARTIAL):
            continue
        try:
            if not e.is_file():
                continue
        except OSError:
            continue
        if e.name.endswith(".orbital.bak"):
            continue
        if skip and skip(e):
            continue
        out.append(e)
    return out


def _free_name(dest_dir: Path, name: str, taken: set[str]) -> Path:
    """Nombre libre en dest_dir: no pisa ni lo que ya existe ni lo que este mismo lote va a crear."""
    cand = dest_dir / name
    if not cand.exists() and str(cand) not in taken:
        return cand
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    if stem.lower().endswith(".tar"):  # foo.tar.gz -> "foo (1).tar.gz"
        stem, suffix = stem[:-4], ".tar" + suffix
    i = 1
    while True:
        cand = dest_dir / f"{stem} ({i}){suffix}"
        if not cand.exists() and str(cand) not in taken:
            return cand
        i += 1


def _rules(args: dict) -> dict[str, list[str]]:
    r = args.get("rules")
    if isinstance(r, dict) and r:
        return {str(k): ([v] if isinstance(v, str) else [str(x) for x in v]) for k, v in r.items()}
    return CATEGORIES


def plan_classify(args: dict, skip: Skip | None = None) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Plan (origen, destino) de classify. No toca nada. Devuelve también avisos."""
    folder = Path(args.get("path") or ".").expanduser()
    if not folder.is_dir():
        return [], [f"ERROR: {folder} no es una carpeta"]
    pre0 = unknown_note(args, CLASSIFY_ARGS)
    args, pre_notes = clean_rules(args)
    pre_notes = pre0 + list(pre_notes)
    broad = broad_filter_note(args)
    dest_root = Path(args.get("dest") or folder).expanduser()
    # la primera regla que coincide gana (en el orden en que vienen)
    rules = [(cat, [_matcher(x) for x in pats]) for cat, pats in _rules(args).items()]
    moves, taken, notes = [], set(), list(pre_notes) + ([broad] if broad else [])
    leftover = 0
    keep = _name_filter(args)
    cands = _candidates(folder, skip)
    total = len(cands)
    drop = _name_filter({"filter": args.get("exclude")})
    if drop:
        cands = [f for f in cands if not drop(f.name)]
    sel = _sel(args)
    if sel:
        cands = [f for f in cands if sel(f)]
    if keep:
        cands = [f for f in cands if keep(f.name)]
        notes.append(f"Filtro {args.get('filter')!r}: {len(cands)} de {total} archivos sueltos")
    elif sel:
        notes.append(f"Selección (ext/fecha/tamaño): {len(cands)} de {total} archivos sueltos")
    custom = _custom_names(args)
    seq = 0
    for f in cands:
        by_ext = str(args.get("by") or "").lower() == "ext"
        cat = (f.suffix.lower().lstrip(".") or "sin_ext") if by_ext else next((c for c, ms in rules if any(m(f.name) for m in ms)), None)
        if cat is None:
            if not args.get("other"):
                leftover += 1
                continue
            cat = UNCLASSIFIED
        cat = custom.get(cat.lower(), cat)
        dest_dir = dest_root / cat
        if args.get("by_date"):
            dest_dir = dest_dir / _period(f, str(args["by_date"]))
        if f.parent.resolve() == dest_dir.resolve():
            continue
        seq += 1
        dst = _free_name(dest_dir, new_name(f, args, seq), taken)
        taken.add(str(dst))
        moves.append((f, dst))
    notes += rename_note(args)
    if total and not keep and not sel and len(moves) == total and total > 1:
        notes.append(f"Mueve TODOS los archivos sueltos de {folder} ({total}); \"filter\" limita a un subconjunto")
    if leftover:
        notes.append(f"{leftover} archivos sin categoría permanecen (\"other\": true los mueve a {UNCLASSIFIED}/)")
    return moves, notes


def plan_move(args: dict, skip: Skip | None = None) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Plan de move: archivos de 'source' (carpeta o archivo) que coinciden con 'pattern' -> carpeta 'dest'.
    Solo archivos SUELTOS: lo que está dentro de una subcarpeta no se toca. Soporta excluir y renombrar."""
    src = Path(args.get("source") or args.get("path") or ".").expanduser()
    if not args.get("dest"):
        return [], ["ERROR: falta 'dest'"]
    dest_dir = Path(args["dest"]).expanduser()
    if dest_dir.exists() and not dest_dir.is_dir():
        return [], [f"ERROR: el destino {dest_dir} existe y no es una carpeta"]
    notes = unknown_note(args, MOVE_ARGS)
    pats = args.get("pattern") or "*"
    pats = [pats] if isinstance(pats, str) else [str(p) for p in pats]
    ms = [_matcher(p) for p in pats]
    keep = _name_filter(args)
    drop = _name_filter({"filter": args.get("exclude")})
    if src.is_file():
        files = [src] if not (skip and skip(src)) else []
    elif src.is_dir():
        files = [f for f in _candidates(src, skip) if any(m(f.name) for m in ms)]
    else:
        return [], [f"ERROR: no existe {src}"]
    if keep:
        files = [f for f in files if keep(f.name)]
    if drop:
        files = [f for f in files if not drop(f.name)]
    sel = _sel(args)
    if sel:
        files = [f for f in files if sel(f)]
    moves, taken = [], set()
    renaming = _rename_wanted(args)
    notes += rename_note(args)
    for k, f in enumerate(files, 1):
        name = new_name(f, args, k)
        if f.parent.resolve() == dest_dir.resolve() and name == f.name:
            continue
        dst = _free_name(dest_dir, name, taken)
        taken.add(str(dst))
        moves.append((f, dst))
    if renaming and moves:
        notes.append("Sufijo con mes y año de última modificación; idempotente")
    return moves, notes


def plan_rename(args: dict, skip: Skip | None = None) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Renombra EN SU LUGAR (no mueve). Mismas piezas que en move: format, replace, clean, rename."""
    from . import survey as mp
    notes = unknown_note(args, RENAME_ARGS)
    if not _rename_wanted(args):
        return [], notes + rename_note(args) + ["ERROR: falta la acción: format (\"{name}_{mon}{yy}\"), replace "
                                                '{"from": "x", "to": "y"}, clean: true o rename: true']
    rts = mp.roots(args)
    bad = [r for r in rts if not (r.is_dir() or r.is_file())]
    if bad:
        return [], [f"ERROR: no existe {bad[0]}"]
    pats = args.get("pattern") or "*"
    ms = [_matcher(p) for p in ([pats] if isinstance(pats, str) else [str(x) for x in pats])]
    keep = _name_filter(args)
    drop = _name_filter({"filter": args.get("exclude")})
    sel = _sel(args)
    depth = max(1, min(4, int(args.get("depth", 1) or 1)))
    files = [f for f in mp._collect_files(rts, depth, None, skip) if not f.name.startswith(".")
             and not f.name.lower().endswith(PARTIAL) and not f.name.endswith(".orbital.bak")]
    files = [f for f in files if any(m(f.name) for m in ms) and (not keep or keep(f.name))
             and (not drop or not drop(f.name)) and (not sel or sel(f))]
    moves, taken = [], set()
    for k, f in enumerate(sorted(files, key=lambda p: (str(p.parent), p.name.lower())), 1):
        name = new_name(f, args, k)
        if name == f.name:
            continue
        dst = _free_name(f.parent, name, taken)
        taken.add(str(dst))
        moves.append((f, dst))
    notes += rename_note(args)
    if _truthy(args.get("rename")) or _fmt_ok(args):
        notes.append("Fechas: última modificación (la creación no es fiable en Linux)")
    return moves, notes


def _drop_hidden(items):
    return [i for i in items if not i.name.startswith(".") and not i.name.lower().endswith(PARTIAL)
            and not i.name.endswith(".orbital.bak")]


def plan_group(args: dict, skip: Skip | None = None) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Mueve cada grupo de archivos (o carpetas) 'de la misma idea' a destino/<Nombre>/. Calculado por survey.py."""
    from . import survey as mp
    notes = unknown_note(args, GROUP_ARGS)
    rts = mp.roots(args)
    bad = [r for r in rts if not r.is_dir()]
    if bad:
        return [], [f"ERROR: {bad[0]} no es una carpeta"]
    dest_root = Path(args.get("dest") or rts[0]).expanduser()
    items = _drop_hidden(mp.collect_items(args, skip))
    if len(items) < 2:
        return [], notes + ["Elementos sueltos insuficientes para agrupar"]
    mg = max(2, int(args.get("min_group", 3) or 3))
    groups, singles, _ = mp.cluster(items, float(args.get("threshold", 0.30) or 0.30), mg)
    moves, taken = [], set()
    for g in groups:
        label = mp._safe_label(g.label)
        members = [items[i] for i in g.members]
        dest_dir = dest_root / label
        mine = {m.path.resolve() for m in members}
        if (dest_dir.exists() and not dest_dir.is_dir()) or dest_dir.resolve() in mine:
            dest_dir = dest_root / (label + "-grupo")   # ni sobre un archivo ni una carpeta dentro de sí misma
        for it in members:
            if it.path.parent.resolve() == dest_dir.resolve():
                continue
            dst = _free_name(dest_dir, it.path.name, taken)
            taken.add(str(dst))
            moves.append((it.path, dst))
    unidad = "carpetas" if items and items[0].is_dir else "archivos"
    notes.append(f"{len(groups)} grupos / {len(items)} {unidad} (mín. {mg}); {len(singles)} sin grupo permanecen. "
                 "Solo elementos sueltos del primer nivel.")
    return moves, notes


def plan_empties(args: dict, skip: Skip | None = None) -> tuple[list[Path], list[str]]:
    """Carpetas SIN ningún archivo en todo su subárbol (de la más profunda a la menos). La carpeta raíz no cuenta.
    Si algo dentro está vetado, es secreto o es un enlace, la carpeta NO se considera vacía."""
    from . import survey as mp
    notes = unknown_note(args, EMPTIES_ARGS)
    out: list[Path] = []
    for r in mp.roots(args):
        if not r.is_dir():
            return [], [f"ERROR: {r} no es una carpeta"]
        root = mp.scan(r, skip, None, None, None, None, [mp.MAX_SCAN])
        if root.err:
            notes.append(f"Recorrido incompleto en {r}: solo se cuentan las carpetas verificadas")
        out += [e.path for e in mp.empties(root) if not e.err]
    return out, notes


def describe_empties(dirs: list[Path], notes: list[str], dry: bool, max_out: int) -> str:
    if not dirs:
        return "\n".join(["(sin carpetas vacías)"] + notes)
    tops = [d for d in dirs if not any(o != d and o in d.parents for o in dirs)]
    head = ("PREVISUALIZACIÓN (sin cambios)" if dry else "ELIMINADAS") + f": {len(dirs)} carpetas vacías"
    lines = [head] + [f"  {d}/" for d in sorted(tops)[:40]] + ([f"  …(+{len(tops) - 40})"] if len(tops) > 40 else [])
    return "\n".join(lines + notes)[:max_out]


def execute_empties(dirs: list[Path], journal_dir: Path, label: str) -> tuple[str, list[str], list[list[str]]]:
    """rmdir (solo borra carpetas realmente vacías; si apareció algo, falla y no toca nada). Bitácora primero."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 1000:03d}"
    jpath = journal_dir / f"{jid}.json"
    n = 1
    while jpath.exists():
        jpath = journal_dir / f"{jid}-{n}.json"
        n += 1
    jid = jpath.stem
    data = {"id": jid, "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "tarea": label, "movimientos": [],
            "carpetas_borradas": [str(d) for d in dirs], "deshecho": False}
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    done, failed = [], []
    for d in dirs:   # ya vienen de la más profunda a la menos
        try:
            d.rmdir()
            done.append(str(d))
        except OSError as e:
            failed.append([str(d), f"{type(e).__name__}: {e}"])
    data["carpetas_borradas"], data["fallidos"] = done, failed
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return jid, done, failed


def describe(moves: list[tuple[Path, Path]], notes: list[str], dry: bool, max_out: int) -> str:
    if not moves:
        return "\n".join(["(sin coincidencias)"] + notes)
    by_dir: dict[Path, list[tuple[Path, Path]]] = defaultdict(list)
    for s, d in moves:
        by_dir[d.parent].append((s, d))
    dup = re.compile(r" \(\d+\)(\.[^.]*)?$")
    renamed = sum(1 for s, d in moves if dup.search(d.name) and not dup.search(s.name))
    head = ("PREVISUALIZACIÓN (sin cambios)" if dry else "MOVIDOS") + f": {len(moves)} archivos a {len(by_dir)} carpetas"
    lines = [head]
    for dd, items in sorted(by_dir.items(), key=lambda kv: -len(kv[1])):
        ej = ", ".join(s.name if s.name == d.name else f"{s.name}→{d.name}" for s, d in items[:5]) + (
            f", …(+{len(items) - 5})" if len(items) > 5 else "")
        lines.append(f"  {dd}/ <- {len(items)}: {ej}")
    if renamed:
        lines.append(f"  ({renamed} con nombre repetido: sufijo (1), (2)…; sin sobrescritura)")
    lines += notes
    return "\n".join(lines)[:max_out]


def execute(moves: list[tuple[Path, Path]], journal_dir: Path, label: str) -> tuple[str, list, list]:
    """Mueve de verdad. La bitácora se escribe ANTES de mover (si algo se cae a medias, deshacer igual
    sirve: solo revierte lo que encuentre movido). Devuelve (id, hechos, fallidos)."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 1000:03d}"
    jpath = journal_dir / f"{jid}.json"
    n = 1
    while jpath.exists():
        jpath = journal_dir / f"{jid}-{n}.json"
        n += 1
    jid = jpath.stem
    data = {"id": jid, "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "tarea": label,
            "movimientos": [[str(s), str(d)] for s, d in moves], "deshecho": False}
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    done, failed, created = [], [], []
    for s, d in moves:
        try:
            if not d.parent.exists():
                d.parent.mkdir(parents=True)
                created.append(str(d.parent))
            if d.exists():  # apareció entre el plan y ahora: jamás pisar
                d = _free_name(d.parent, d.name, set())
            shutil.move(str(s), str(d))
            done.append([str(s), str(d)])
        except OSError as e:
            failed.append([str(s), f"{type(e).__name__}: {e}"])
    data["movimientos"] = done
    data["fallidos"] = failed
    data["carpetas_creadas"] = created
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return jid, done, failed


def last_journal(journal_dir: Path, jid: str | None = None) -> Path | None:
    if not journal_dir.is_dir():
        return None
    if jid:
        p = journal_dir / f"{jid}.json"
        return p if p.exists() else None
    for p in sorted(journal_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            if not json.loads(p.read_text()).get("deshecho"):
                return p
        except (OSError, ValueError):
            continue
    return None


def undo(jpath: Path) -> tuple[int, list[str]]:
    """Revierte un lote: cada destino vuelve a su origen si el origen está libre. Nunca pisa."""
    data = json.loads(jpath.read_text())
    back, problems = 0, []
    for src, dst in reversed(data.get("movimientos", [])):
        s, d = Path(src), Path(dst)
        if not d.exists():
            problems.append(f"ausente: {d}")
            continue
        if s.exists():
            problems.append(f"{d.name} sin revertir: ocupado {s}")
            continue
        try:
            s.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(s))
            back += 1
        except OSError as e:
            problems.append(f"{d.name}: {e}")
    for dd in sorted({Path(x) for x in data.get("carpetas_borradas", [])}, key=lambda p: len(p.parts)):
        try:
            dd.mkdir(parents=True, exist_ok=True)
            back += 1
        except OSError as e:
            problems.append(f"{dd} sin recrear: {e}")
    # solo las carpetas que ESTE lote creó y quedaron vacías (rmdir no borra contenido; las tuyas no se tocan)
    for dd in sorted({Path(x) for x in data.get("carpetas_creadas", [])}, key=lambda p: -len(p.parts)):
        try:
            dd.rmdir()
        except OSError:
            pass
    data["deshecho"] = True
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return back, problems


def journal_paths(jpath: Path) -> list[Path]:
    try:
        data = json.loads(jpath.read_text())
    except (OSError, ValueError):
        return []
    return [Path(p) for pair in data.get("movimientos", []) for p in pair] + [
        Path(p) for p in data.get("carpetas_borradas", [])]
