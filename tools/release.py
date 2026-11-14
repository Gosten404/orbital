#!/usr/bin/env python3
"""Publicar una versión: pruebas -> subir versión -> commit -> tag -> push.

Uso:  python3 tools/release.py 0.2.25 [--no-push]
Requisitos: árbol limpio, rama main con upstream. Usa la identidad de git del repositorio (git config user.*).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "pyproject.toml": (r'^(version = ")[^"]+(")', r"\g<1>{v}\g<2>"),
    "orbital/__init__.py": (r'^(__version__ = ")[^"]+(")', r"\g<1>{v}\g<2>"),
    "README.md": (r"^(# O\.R\.B\.I\.T\.A\.L · v)[0-9.]+", r"\g<1>{v}"),
}


def sh(*cmd: str, check: bool = True, env: dict | None = None) -> str:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if check and r.returncode:
        sys.exit(f"[x] {' '.join(cmd)}\n{(r.stdout + r.stderr).strip()[-600:]}")
    return r.stdout.strip()


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        print(__doc__)
        return 2
    v = args[0]
    if sh("git", "status", "--porcelain", "--untracked-files=no"):
        sys.exit("[x] hay cambios sin guardar: commit o stash antes de publicar")
    if sh("git", "tag", "--list", f"v{v}"):
        sys.exit(f"[x] el tag v{v} ya existe")
    print("Pruebas…")
    py = sys.executable
    sh(py, "tests/test_smoke.py", env={"ORBITAL_CONFIG": "/nonexistent"})
    sh(py, "tests/lint_texts.py")
    for name, (rx, rep) in FILES.items():
        p = ROOT / name
        new, n = re.subn(rx, rep.format(v=v), p.read_text(), count=1, flags=re.M)
        if n != 1:
            sys.exit(f"[x] no se encontró la línea de versión en {name}")
        p.write_text(new)
    sh("git", "add", *FILES)
    sh("git", "commit", "-q", "-m", f"v{v}")
    sh("git", "tag", "-a", f"v{v}", "-m", f"v{v}")
    print(f"Commit y tag v{v} creados.")
    if "--no-push" in argv:
        print("Sin push (--no-push). Publicar: git push origin main --tags")
        return 0
    sh("git", "push", "origin", "main", "--tags")
    print(f"Publicado v{v}. En la PC de uso: orbital update")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
