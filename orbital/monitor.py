"""Medidor de saturación de la PC y escalera de mitigación.

Antes de cada paso del agente:
  1. normal                      -> sigue
  2. alto (>= umbral_alto)       -> espera unos segundos; si sigue alto, ofrece cerrar procesos pesados
  3. crítico (>= umbral_crítico) -> pausa el razonamiento 30 s y vuelve a medir (varias veces)
  4. sigue crítico               -> ÚLTIMO RECURSO: pide permiso para bajar la intensidad del razonamiento
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import psutil

# Procesos que nunca se ofrecen para cerrar
PROTECTED = {"ollama", "orbitald", "orbital", "systemd", "Xorg", "Xwayland", "gnome-shell",
             "pipewire", "pulseaudio", "wireplumber", "dbus-daemon", "NetworkManager", "sshd", "bash", "zsh"}


@dataclass
class Load:
    cpu: float      # % promedio de CPU en la ventana medida
    ram: float      # % de RAM usada
    swap: float     # % de swap usado
    ram_free_gb: float

    @property
    def score(self) -> float:
        """Saturación 0-100: lo peor entre CPU y RAM; el swap activo empeora la RAM."""
        ram = self.ram + (10 if self.swap > 50 else 0)
        return max(self.cpu, min(ram, 100))

    def bar(self) -> str:
        n = int(self.score / 10)
        return f"[{'#' * n}{'.' * (10 - n)}] {self.score:.0f}% · CPU {self.cpu:.0f}% · RAM {self.ram:.0f}% ({self.ram_free_gb:.1f} GB libres) · swap {self.swap:.0f}%"


async def measure(window: float = 1.0) -> Load:
    psutil.cpu_percent(None)
    await asyncio.sleep(window)
    vm = psutil.virtual_memory()
    return Load(cpu=psutil.cpu_percent(None), ram=vm.percent, swap=psutil.swap_memory().percent,
                ram_free_gb=vm.available / 1e9)


def available_gb() -> float:
    return psutil.virtual_memory().available / 1e9


def ram_now() -> tuple[float, int]:
    """Lectura instantánea (sin esperar) para vigilar mientras el modelo genera.
    Durante la inferencia la CPU SIEMPRE está al 100 %, así que aquí solo cuentan RAM y swap."""
    return psutil.virtual_memory().percent, psutil.swap_memory().used


def heavy_processes(limit: int = 6) -> list[dict]:
    me = os.getpid()
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "username"]):
        try:
            info = p.info
            if info["pid"] == me or info["name"] in PROTECTED or info["username"] != psutil.Process(me).username():
                continue
            procs.append({"pid": info["pid"], "name": info["name"], "ram_mb": info["memory_info"].rss / 1e6,
                          "proc": p})
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, TypeError):
            continue
    # agrupa por nombre (Chrome/Brave abren decenas de procesos)
    groups: dict[str, dict] = {}
    for p in procs:
        g = groups.setdefault(p["name"], {"name": p["name"], "ram_mb": 0.0, "procs": []})
        g["ram_mb"] += p["ram_mb"]
        g["procs"].append(p["proc"])
    return sorted(groups.values(), key=lambda g: -g["ram_mb"])[:limit]


def close_group(group: dict) -> str:
    """Cierre amable (SIGTERM) para que la app guarde su estado."""
    n = 0
    for p in group["procs"]:
        try:
            p.terminate()
            n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return f"{group['name']}: {n} proceso(s) cerrados"
