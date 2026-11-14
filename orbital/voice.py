"""Base de voz: QUÉ se dice (eventos y texto breve) separado de CÓMO se dice (motor).

Eventos: start, help, error, done. El texto sale de plantillas y de limpiar la respuesta; el modelo pequeño
(el del enrutador) se usa UNA vez, al terminar, y solo si la respuesta es larga: durante la tarea el 4b ya
ocupa la CPU y un resumen por paso la frenaría.
Motores: clases con `async speak(text, kind)`. Hoy: print (muestra el texto) y none. Un motor real (TTS) se agrega
con `BACKENDS["nombre"] = Clase`, sin tocar el resto.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

KINDS = ("start", "help", "error", "done")


def speakable(text: str, max_chars: int = 200) -> str:
    """Texto apto para voz: sin markdown, código, URLs ni rutas largas; recortado en una frontera de frase."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"(?<![\w.])(?:~|/)[\w.@~/-]*/([\w.@-]+)", lambda m: m.group(1), t)   # ruta -> último tramo
    t = re.sub(r"[*_#>|]+", " ", t)
    t = re.sub(r"\[[+=<>x!~i?#]\]\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    stop = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(": "))
    return (cut[:stop] if stop > max_chars // 3 else cut.rsplit(" ", 1)[0]).rstrip(" ,;:.") + "."


_FACTS = [
    (re.compile(r"^MOVIDOS:\s*(\d+) archivos? a (\d+) carpetas?", re.M), "{0} archivos movidos a {1} carpetas"),
    (re.compile(r"^ELIMINADAS?:\s*(\d+) carpetas?", re.M), "{0} carpetas vacías eliminadas"),
    (re.compile(r"^(?:DESHECHO|REVERTIDO)S?:\s*(\d+)", re.M), "{0} movimientos revertidos"),
    (re.compile(r"^RENOMBRADOS?:\s*(\d+)", re.M), "{0} archivos renombrados"),
]


def facts_line(answer: str) -> str | None:
    """Resultado de las herramientas de organización: la frase sale de las cifras, sin modelo."""
    parts = [tpl.format(*m.groups()) for rx, tpl in _FACTS for m in [rx.search(answer)] if m]
    return ". ".join(parts) + "." if parts else None


def phrase(kind: str, text: str = "", max_chars: int = 200) -> str:
    """Plantillas de start/help/error (sin modelo)."""
    if kind == "start":
        return "Tarea iniciada."
    if kind == "help":
        base = "Se requiere intervención."
        s = speakable(text, 90)
        return f"{base} {s}" if s else base
    if kind == "error":
        s = speakable(re.sub(r"^\[!\]\s*|^ERROR:?\s*", "", text), 110)
        return f"Error: {s}" if s else "Error."
    return speakable(text, max_chars)


async def summarize(answer: str, provider, max_chars: int = 200, timeout: float = 25.0, use_model: bool = True) -> str:
    """Respuesta final -> frase corta. Orden: cifras de organización, texto corto tal cual, modelo pequeño, recorte."""
    facts = facts_line(answer)
    if facts:
        return facts
    plain = speakable(answer, 10_000)
    if len(plain) <= max_chars:
        return plain
    if use_model and provider is not None:
        prompt = ("Resume el texto en UNA o DOS frases cortas para leerlas en voz alta: solo lo esencial, impersonal, "
                  "sin listas, sin markdown, sin rutas ni cifras innecesarias. Devuelve solo las frases.\n\nTEXTO:\n" + plain[:4000])
        try:
            out = await asyncio.wait_for(provider.chat([{"role": "user", "content": prompt}], json_mode=False), timeout)
            out = speakable(str(out), max_chars)
            if out:
                return out
        except Exception:  # noqa: BLE001 - sin resumen del modelo se recorta; la voz nunca bloquea ni rompe la tarea
            pass
    return speakable(plain, max_chars)


class NullBackend:
    async def speak(self, text: str, kind: str) -> None:
        return None


class PrintBackend:
    """Muestra lo que se diría. Sirve para ajustar el texto antes de conectar un motor de audio."""
    def __init__(self, write):
        self.write = write

    async def speak(self, text: str, kind: str) -> None:
        self.write(f"[voz:{kind}] {text}")


BACKENDS = {"none": NullBackend, "print": PrintBackend}


def make_backend(name: str, write=print):
    if name not in BACKENDS:
        raise ValueError(f"motor de voz desconocido: {name} (disponibles: {', '.join(BACKENDS)})")
    return BACKENDS[name](write) if name == "print" else BACKENDS[name]()
