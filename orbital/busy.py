"""Aviso de "O.R.B.I.T.A.L está trabajando" para otros programas (p. ej. tu bot de capturas).

Mientras corre una tarea existe:  $XDG_RUNTIME_DIR/orbital/ocupado   (JSON: pid, desde, tarea, mouse)
Tu bot solo tiene que revisar si existe antes de tomar captura o mover el mouse.
Se borra al terminar la tarea (también si se cancela o falla). Si orbitald muere de golpe, el
archivo queda con un pid que ya no existe: compruébalo con kill -0 para no quedarte bloqueado.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


def path() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/orbital-{os.getuid()}") / "orbital"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ocupado"


@contextmanager
def busy(task: str, mouse: bool = False):
    p = path()
    p.write_text(json.dumps({"pid": os.getpid(), "desde": int(time.time()), "tarea": task[:80], "mouse": mouse}))
    try:
        yield
    finally:
        p.unlink(missing_ok=True)
