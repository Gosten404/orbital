from __future__ import annotations

import json
import time
from typing import Awaitable, Callable

import httpx

from . import ProviderError


class Paused(ProviderError):
    """La generación se cortó a la mitad porque la PC entró en zona crítica."""
    def __init__(self, msg: str):
        super().__init__(msg, fatal=False)


class Ollama:
    def __init__(self, url: str, model: str, keep_alive: str = "5m", options: dict | None = None,
                 reduced: bool = False):
        self.url = url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.extra = options or {}
        self.name = f"local:{model}" + (" (reducido)" if reduced else "")
        # watchdog() -> motivo para abortar o None. Se consulta cada ~2 s mientras genera.
        self.watchdog: Callable[[], Awaitable[str | None]] | None = None
        self.last_done_reason: str | None = None
        self.last_stats: dict = {}

    async def chat(self, messages: list[dict], json_mode: bool = False, temperature: float = 0.2,
                   options: dict | None = None, schema: dict | None = None) -> str:
        """options: ajustes SOLO para esta llamada (p. ej. num_predict = tope de tokens del paso)."""
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,  # en streaming podemos cortar si la PC se satura a mitad de la respuesta
            "think": False,  # qwen3.5+ piensa por defecto; en CPU eso es lentísimo
            "keep_alive": self.keep_alive,
            "options": {"temperature": temperature, **self.extra, **(options or {})},
        }
        self.last_done_reason = None  # "length" = se cortó por el tope de tokens
        self.last_stats = {}
        if json_mode:
            body["format"] = schema or "json"  # con 'schema', Ollama fuerza esa forma: no puede inventar campos
        parts: list[str] = []
        last_check = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
                async with c.stream("POST", f"{self.url}/api/chat", json=body) as r:
                    if r.status_code >= 400:
                        await r.aread()
                        raise ProviderError(f"Ollama respondió {r.status_code}: {r.text[:200]}", fatal=True)
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if "error" in chunk:
                            raise ProviderError(f"Ollama: {chunk['error']}", fatal=True)
                        parts.append(chunk.get("message", {}).get("content", ""))
                        if chunk.get("done"):
                            self.last_done_reason = chunk.get("done_reason")
                            self.last_stats = stats_from(chunk)
                            break
                        if self.watchdog and time.monotonic() - last_check > 2:
                            last_check = time.monotonic()
                            why = await self.watchdog()
                            if why:  # salir del 'async with' cierra la conexión y Ollama deja de generar
                                raise Paused(why)
            return "".join(parts)
        except httpx.ConnectError as e:
            raise ProviderError("No hay conexión con Ollama. ¿Está corriendo? (systemctl status ollama)", fatal=True) from e
        except httpx.HTTPError as e:  # timeouts, etc.
            raise ProviderError(f"Error con Ollama: {type(e).__name__} {e}", fatal=True) from e

    async def unload(self) -> None:
        """Saca el modelo de la RAM (keep_alive=0). No falla si no estaba cargado."""
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"{self.url}/api/generate", json={"model": self.model, "keep_alive": 0})
        except httpx.HTTPError:
            pass


def stats_from(chunk: dict) -> dict:
    """Tiempos del último mensaje de Ollama (vienen en nanosegundos).
    prompt_tokens = tokens de contexto que tuvo que LEER (lo que ya estaba en caché no cuenta)."""
    ns = 1e9
    pe, ev = chunk.get("prompt_eval_count", 0) or 0, chunk.get("eval_count", 0) or 0
    ped, evd = (chunk.get("prompt_eval_duration", 0) or 0) / ns, (chunk.get("eval_duration", 0) or 0) / ns
    return {"load_s": round((chunk.get("load_duration", 0) or 0) / ns, 2),
            "prompt_tokens": pe, "prompt_s": round(ped, 2),
            "eval_tokens": ev, "eval_s": round(evd, 2),
            "eval_tps": round(ev / evd, 2) if evd > 0 else 0.0,
            "prompt_tps": round(pe / ped, 2) if ped > 0 else 0.0}


async def loaded_models(url: str) -> list[dict]:
    """Modelos que Ollama tiene en memoria ahora mismo (GET /api/ps)."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{url.rstrip('/')}/api/ps")
            return r.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []
