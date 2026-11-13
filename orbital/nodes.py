"""Árbol de nodos: cada tema de estudio o proyecto tiene su propia memoria, aislada de los demás.

Un nodo = {nombre, tipo (tema|proyecto), carpeta opcional}. La memoria de un nodo son notas y hechos
que el agente acumula ahí. Por defecto una tarea solo ve la memoria del nodo actual; con @otro_nodo
en el mensaje, además toma la de ese nodo. Si el nodo tiene carpeta, trabajar en él hace cd ahí, así
el sandbox de cada comando ve solo esa carpeta.

Se guarda en SQLite: ~/.local/share/orbital/orbital.db
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

GENERAL = "general"  # nodo por defecto cuando no eliges ninguno


def slug(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).strip()


class NodeStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY, nombre TEXT UNIQUE NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'proyecto', carpeta TEXT, creado INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS node_memory (
                id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL, kind TEXT NOT NULL,
                texto TEXT NOT NULL, creado INTEGER NOT NULL,
                FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE);
            """
        )
        try:   # bases creadas antes de 0.2.21
            self.db.execute("ALTER TABLE nodes ADD COLUMN recursos TEXT")
        except sqlite3.OperationalError:
            pass
        self.db.commit()
        if not self.get(GENERAL):
            self.create(GENERAL, "tema")

    # ---- nodos ----
    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM nodes ORDER BY nombre")]

    def get(self, nombre: str) -> dict | None:
        r = self.db.execute("SELECT * FROM nodes WHERE nombre = ?", (slug(nombre),)).fetchone()
        return dict(r) if r else None

    def create(self, nombre: str, tipo: str = "proyecto", carpeta: str | None = None,
               recursos: str | None = None) -> dict:
        nombre = slug(nombre)
        if tipo not in ("tema", "proyecto"):
            tipo = "proyecto"
        carpeta = str(Path(carpeta).expanduser().resolve()) if carpeta else None
        self.db.execute("INSERT INTO nodes(nombre, tipo, carpeta, creado, recursos) VALUES (?,?,?,?,?)",
                        (nombre, tipo, carpeta, int(time.time()), recursos))
        self.db.commit()
        return self.get(nombre)

    def set_folder(self, nombre: str, carpeta: str | None) -> None:
        c = str(Path(carpeta).expanduser().resolve()) if carpeta else None
        self.db.execute("UPDATE nodes SET carpeta = ? WHERE nombre = ?", (c, slug(nombre)))
        self.db.commit()

    def delete(self, nombre: str) -> bool:
        if slug(nombre) == GENERAL:
            return False
        cur = self.db.execute("DELETE FROM nodes WHERE nombre = ?", (slug(nombre),))
        self.db.commit()
        return cur.rowcount > 0

    # ---- memoria del nodo ----
    def remember(self, node_id: int, texto: str, kind: str = "nota") -> None:
        texto = texto.strip()
        if texto:
            self.db.execute("INSERT INTO node_memory(node_id, kind, texto, creado) VALUES (?,?,?,?)",
                            (node_id, kind, texto[:1000], int(time.time())))
            self.db.commit()

    def memory(self, node_id: int, limit: int = 25) -> list[dict]:
        rows = self.db.execute(
            "SELECT kind, texto, creado FROM node_memory WHERE node_id = ? ORDER BY id DESC LIMIT ?",
            (node_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def memory_count(self, node_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM node_memory WHERE node_id = ?", (node_id,)).fetchone()[0]

    def forget(self, node_id: int) -> None:
        self.db.execute("DELETE FROM node_memory WHERE node_id = ?", (node_id,))
        self.db.commit()


def referenced(text: str) -> list[str]:
    """Nombres de nodos citados con @nombre en el mensaje (una sola palabra, letras/números/_/-)."""
    return [m.lower() for m in re.findall(r"(?<!\w)@([A-Za-z0-9_\-]{2,40})", text)]


def context_block(store: NodeStore, node: dict, extra_names: list[str]) -> str:
    """Texto de contexto con la memoria del nodo actual y, si se citaron, la de otros nodos."""
    parts: list[str] = []
    mem = store.memory(node["id"])
    if mem:
        parts.append(f"MEMORIA DEL NODO «{node['nombre']}» (lo que sabes de este {node['tipo']}):")
        parts += [f"- {m['texto']}" for m in mem]
    for name in dict.fromkeys(extra_names):
        other = store.get(name)
        if other and other["id"] != node["id"]:
            om = store.memory(other["id"], limit=12)
            if om:
                parts.append(f"\nMEMORIA DEL NODO CITADO «{other['nombre']}»:")
                parts += [f"- {m['texto']}" for m in om]
    return "\n".join(parts)
