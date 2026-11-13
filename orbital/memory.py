"""Memoria de tareas en Markdown: un archivo por tarea, escrito en cada paso.

~/.local/share/orbital/memoria/
    2026-11-08_224012_renombrarme.md   <- bitácora completa de una tarea
    ULTIMA.md                          <- copia de la última, para retomar

En la v0.2 esto se mueve dentro de cada nodo del árbol.
"""
from __future__ import annotations

from .redact import clean

import datetime as dt
import re
import shutil
from pathlib import Path


class TaskMemory:
    def __init__(self, base: Path, task: str, model: str, policy: str, cwd: Path):
        self.dir = base / "memoria"
        self.dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:40].strip("-") or "tarea"
        self.path = self.dir / f"{dt.datetime.now():%Y-%m-%d_%H%M%S}_{slug}.md"
        self.status = "en curso"
        self.task = task
        self.header = (f"# Tarea\n{task}\n\n- Inicio: {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n"
                       f"- Modelo: {model}\n- Permisos: {policy}\n- Carpeta: {cwd}\n")
        self.steps: list[str] = []
        self.flush()

    def step(self, n: int, thought: str, action: str, result: str) -> None:
        r = result.strip().replace("\n", "\n  > ")
        self.steps.append(f"### Paso {n}\n- **Por qué:** {thought or '(sin explicar)'}\n- **Acción:** {action}\n"
                          f"- **Resultado:**\n  > {r[:800]}\n")
        self.flush()

    def note(self, text: str) -> None:
        self.steps.append(f"> [i] {text}\n")
        self.flush()

    def finish(self, status: str, answer: str = "") -> None:
        self.status = status
        if answer:
            self.steps.append(f"## Resultado final\n{answer}\n")
        self.flush()

    def flush(self) -> None:
        body = (self.header + f"- **Estado: {self.status}**\n\n## Pasos\n" + "\n".join(self.steps)
                + ("\n## Retomar\nContinuar desde el último paso sin repetir lo que ya funcionó.\n" if self.status != "terminada" else ""))
        self.path.write_text(clean(body))
        shutil.copyfile(self.path, self.dir / "ULTIMA.md")


def last_memory(base: Path) -> str | None:
    p = base / "memoria" / "ULTIMA.md"
    return p.read_text() if p.exists() else None


def last_summary(base: Path) -> str | None:
    text = last_memory(base)
    if not text:
        return None
    task = text.split("\n")[1] if "\n" in text else "?"
    m = re.search(r"\*\*Estado: (.+?)\*\*", text)
    return f"Última tarea: «{task[:70]}» · {m.group(1) if m else '?'} · /resume continúa"
