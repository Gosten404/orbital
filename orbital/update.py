"""orbital update: baja la última versión del repositorio, la verifica y reinicia el daemon.

La instalación es un checkout de git (instalar.sh hace `pip install -e`). Actualizar es:
fetch -> pull --ff-only -> reinstalar -> comprobar imports -> reiniciar. Si algo falla después del pull,
vuelve a la versión anterior (`refs/orbital/prev`). `--rollback` hace lo mismo a mano.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PREV_REF = "refs/orbital/prev"
USAGE = "Uso: orbital update [--check | --rollback | --full | --no-restart]"


class UpdateError(Exception):
    pass


def repo_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode:
        raise UpdateError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def _version(repo: Path, ref: str = "") -> str:
    try:
        txt = _git(repo, "show", f"{ref}:orbital/__init__.py") if ref else (repo / "orbital/__init__.py").read_text()
    except (UpdateError, OSError):
        return "?"
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else "?"


def _py(repo: Path) -> str:
    venv = repo / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def _verify(repo: Path, full: bool) -> None:
    py = _py(repo)
    steps = [[py, "-m", "compileall", "-q", "orbital"],
             [py, "-c", "import orbital.core, orbital.daemon, orbital.client"]]
    if full:
        steps.append([py, "tests/test_smoke.py"])
    for cmd in steps:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                           env={**os.environ, "ORBITAL_CONFIG": "/nonexistent"})
        if r.returncode:
            raise UpdateError(f"verificación fallida ({' '.join(cmd[-2:])}): {(r.stderr or r.stdout).strip()[-300:]}")


def _install(repo: Path) -> None:
    r = subprocess.run([_py(repo), "-m", "pip", "install", "-q", "-e", str(repo)], capture_output=True, text=True)
    if r.returncode:
        raise UpdateError(f"pip install: {(r.stderr or r.stdout).strip()[-300:]}")


def _restart() -> str:
    try:
        r = subprocess.run(["systemctl", "--user", "restart", "orbitald.service"], capture_output=True, text=True)
    except FileNotFoundError:
        return "sin systemd: reiniciar a mano con orbitald"
    return "daemon reiniciado" if r.returncode == 0 else "daemon no reiniciado: systemctl --user restart orbitald"


def update(repo: Path | None = None, *, check: bool = False, rollback: bool = False, full: bool = False,
           restart: bool = True, install: bool = True, out=print) -> int:
    repo = repo or repo_dir()
    try:
        if not (repo / ".git").exists():
            raise UpdateError(f"{repo} no es un checkout de git: clonar el repositorio y correr instalar.sh")
        if _git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("hay cambios locales sin guardar; commit o stash antes de actualizar")
        before = _git(repo, "rev-parse", "HEAD")
        old = _version(repo)
        if rollback:
            prev = _git(repo, "rev-parse", "--verify", PREV_REF)
            if prev == before:
                out("Sin versión anterior distinta a la actual")
                return 0
            _git(repo, "reset", "--hard", prev)
            if install:
                _install(repo)
            out(f"Rollback: v{old} → v{_version(repo)} · {_restart() if restart else 'sin reinicio'}")
            return 0
        _git(repo, "fetch", "--tags", "--quiet", "origin")
        try:
            upstream = _git(repo, "rev-parse", "@{u}")
        except UpdateError:   # rama sin upstream configurado (push sin -u): usar origin/<rama>
            upstream = _git(repo, "rev-parse", f"origin/{_git(repo, 'rev-parse', '--abbrev-ref', 'HEAD')}")
        if upstream == before:
            out(f"Al día: v{old}")
            return 0
        n = _git(repo, "rev-list", "--count", f"{before}..{upstream}")
        new = _version(repo, upstream)
        if check:
            out(f"Disponible: v{old} → v{new} · {n} commit(s). Aplicar: orbital update")
            return 0
        _git(repo, "update-ref", PREV_REF, before)
        _git(repo, "merge", "--ff-only", upstream)
        try:
            if install:
                _install(repo)
            _verify(repo, full)
        except UpdateError as e:
            _git(repo, "reset", "--hard", before)
            if install:
                try:
                    _install(repo)
                except UpdateError:
                    pass
            out(f"[x] Actualización revertida a v{old}: {e}")
            return 1
        out(f"Actualizado: v{old} → v{new} · {n} commit(s) · verificación OK · "
            f"{_restart() if restart else 'sin reinicio'} · volver: orbital update --rollback")
        return 0
    except UpdateError as e:
        out(f"[x] {e}")
        return 1


def main(argv: list[str]) -> int:
    known = {"--check", "--rollback", "--full", "--no-restart"}
    if any(a not in known for a in argv):
        print(USAGE)
        return 2
    return update(check="--check" in argv, rollback="--rollback" in argv, full="--full" in argv,
                  restart="--no-restart" not in argv)
