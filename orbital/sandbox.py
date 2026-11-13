"""Sandbox para los comandos del agente (bubblewrap).

Lo que tu jefe describió con Docker: el agente "nace" sin la key y no tiene de dónde sacarla.
Cada comando de `ejecutar` corre así:
  - El sistema (/usr, /etc…) en solo lectura.
  - Tu home NO existe, salvo las carpetas autorizadas (la carpeta de trabajo, o las de /session).
  - Dentro de esas carpetas, los archivos secretos (.bashrc, .env, llaves, historiales…) aparecen vacíos.
  - Mi propio código, si cae dentro, en solo lectura.
  - /run vacío: no hay D-Bus, así que no hay llavero. Procesos propios: no ve /proc de orbitald.
  - Entorno limpio: ninguna variable con keys.
bubblewrap es lo que usa Flatpak: no necesita root ni un daemon como Docker.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path

HOME = Path.home()


def available() -> bool:
    return shutil.which("bwrap") is not None


def _secret_files(root: Path, globs: list[str], max_depth: int = 3, limit: int = 400) -> list[Path]:
    """Archivos secretos dentro de una carpeta autorizada (poco profundo, para que sea rápido)."""
    found: list[Path] = []
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache"}
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in skip and depth < max_depth]
        for f in filenames:
            if any(fnmatch.fnmatch(f, g) for g in globs):
                found.append(Path(dirpath) / f)
                if len(found) >= limit:
                    return found
    return found


def build(command: str, cwd: Path, rw: list[Path], ro: list[Path], secret_dirs: list[Path],
          secret_globs: list[str], tmp_dir: Path, network: bool = True, scratch: Path | None = None) -> list[str]:
    cwd = cwd.resolve()
    rw = [p.resolve() for p in rw if p.exists()]
    if scratch:
        scratch = scratch.resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        if scratch not in rw:
            rw = rw + [scratch]
    a = ["bwrap", "--die-with-parent", "--new-session",
         "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if not network:
        a.append("--unshare-net")
    # sistema en solo lectura (Ubuntu usa /usr fusionado: /bin, /lib… son enlaces)
    a += ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for d in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        p = Path("/") / d
        if p.is_symlink():
            a += ["--symlink", os.readlink(p), str(p)]
        elif p.exists():
            a += ["--ro-bind", str(p), str(p)]
    for d in ("/opt", "/sys", "/var/lib", "/snap"):
        a += ["--ro-bind-try", d, d]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/run", "--tmpfs", "/home"]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    a += ["--bind", str(tmp_dir), "/tmp"]  # /tmp persistente entre comandos del agente, separado del tuyo
    # carpetas autorizadas (lectura/escritura)
    for p in rw:
        a += ["--bind", str(p), str(p)]
    # mi código y similares: solo lectura
    for p in ro:
        p = p.resolve()
        if p.exists() and any(p == r or r in p.parents for r in rw):
            a += ["--ro-bind", str(p), str(p)]
    # tapar secretos que caigan dentro de lo autorizado
    masked: list[Path] = []
    for d in secret_dirs:
        d = d.expanduser()
        if d.is_dir() and any(d.resolve() == r or r in d.resolve().parents for r in rw):
            masked.append(d.resolve())
            a += ["--tmpfs", str(d.resolve())]
    for r in rw:
        for f in _secret_files(r, secret_globs):
            inside_masked = any(m in f.parents for m in masked)  # ya tapado por la carpeta: ni el nombre se ve
            if f.is_file() and not f.is_symlink() and not inside_masked:
                a += ["--ro-bind", "/dev/null", str(f)]
    # entorno limpio
    a += ["--clearenv",
          "--setenv", "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
          "--setenv", "HOME", str(HOME), "--setenv", "USER", os.environ.get("USER", "user"),
          "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"), "--setenv", "TERM", "dumb",
          "--setenv", "ORBITAL_SANDBOX", "1"]
    if scratch:
        a += ["--setenv", "ORBITAL_SCRATCH", str(scratch)]
    if not any(cwd == r or r in cwd.parents for r in rw):
        cwd = Path("/tmp")
    a += ["--chdir", str(cwd), "bash", "-c", command]
    return a
