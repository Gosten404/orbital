from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

import httpx

from . import ProviderError

BASE = "https://generativelanguage.googleapis.com/v1beta"


class Gemini:
    def __init__(self, model: str | list[str], api_key_env: str = "GEMINI_API_KEY", ipv4_only: bool = True,
                 read_timeout: float = 25.0, retries: int = 4, base: str = BASE):
        self.ipv4_only = ipv4_only
        self.models = [model] if isinstance(model, str) else list(model)
        self.model = self.models[0]
        self.cool: dict[str, float] = {}
        self.spent: dict[str, str] = {}   # modelo -> día (Pacífico) en que agotó su cuota diaria o no existe
        self.base = base.rstrip("/")
        self.read_timeout = read_timeout
        self.retries = max(1, retries)
        from .. import secrets
        from ..redact import register_secret
        self.key, self.key_source = secrets.get("gemini", api_key_env)
        register_secret(self.key)  # su valor exacto se tacha en todo lo que sale, tenga el formato que tenga
        self.name = f"gemini:{self.model}"

    @property
    def available(self) -> bool:
        return bool(self.key) and not getattr(self, "off", False) and self._pick() is not None

    @staticmethod
    def _today() -> str:
        return f"{dt.datetime.now(ZoneInfo('America/Los_Angeles')):%Y-%m-%d}"   # la cuota diaria se renueva a medianoche del Pacífico

    def _live(self) -> list[str]:
        today = self._today()
        return [m for m in self.models if self.spent.get(m) != today]

    def _pick(self) -> str | None:
        """Primer modelo de la escalera con cuota diaria y sin enfriamiento por minuto."""
        now = time.monotonic()
        for m in self._live():
            if self.cool.get(m, 0) <= now:
                return m
        return (self._live() or [None])[0]   # todos enfriándose: el primero vivo (chat espera)

    def _spend(self, model: str) -> None:
        self.spent[model] = self._today()

    def disable(self) -> None:
        """Apaga Gemini SOLO para la tarea actual (tras agotar reintentos de conexión). Así la
        escalera, el relevo y el último recurso dejan de ofrecerlo y no me quedo esperando."""
        self.off = True

    def reset(self) -> None:
        """Nueva tarea: vuelvo a darle una oportunidad (los cortes son intermitentes, no permanentes)."""
        self.off = False

    async def chat(self, messages: list[dict], json_mode: bool = False, temperature: float = 0.3) -> str:
        if not self.key:
            raise ProviderError("Falta la API key de Gemini (variable GEMINI_API_KEY).", fatal=True)
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]
        body: dict = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        headers = {"x-goog-api-key": self.key}

        # La red pierde paquetes a ciertas IPs de Google de forma intermitente. Cada intento abre una
        # conexión NUEVA (otra IP posible): si una se cuelga, se abandona rápido y se reintenta.
        last = "sin detalle"
        attempt = 0
        while attempt < self.retries:
            model = self._pick()
            if model is None:
                raise ProviderError("Gemini: cuota diaria agotada en todos los modelos (se renueva a medianoche, hora del Pacífico).",
                                    fatal=True)
            wait = self.cool.get(model, 0) - time.monotonic()
            if wait > 0:   # todos los modelos vivos están en su límite por minuto
                await asyncio.sleep(min(wait, 20))
            self.model, self.name = model, f"gemini:{model}"
            url = f"{self.base}/models/{model}:generateContent"
            attempt += 1
            try:
                transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0") if self.ipv4_only \
                    else httpx.AsyncHTTPTransport()
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.read_timeout, connect=10),
                                             transport=transport) as c:
                    r = await c.post(url, headers=headers, json=body)
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError,
                    httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                last = type(e).__name__  # cuelgue o caída: reintenta con conexión nueva
                continue
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e or 'sin detalle'}"
                continue
            if r.status_code == 404 and len(self.models) > 1:   # el nombre no existe: siguiente modelo
                self._spend(model)
                last = f"{model}: HTTP 404 (modelo inexistente)"
                attempt -= 1
                continue
            if r.status_code == 429:
                daily = bool(re.search(r"PerDay|per day|daily", r.text, re.I))
                if daily:
                    self._spend(model)
                else:
                    self.cool[model] = time.monotonic() + 60   # límite por minuto: ese modelo descansa, el otro sigue
                last = f"{model}: HTTP 429 ({'cuota diaria' if daily else 'límite por minuto'})"
                attempt -= 1 if len(self.models) > 1 else 0
                continue
            if r.status_code == 503:  # saturado: espera corta y reintenta
                last = "HTTP 503 (Gemini saturado)"
                await asyncio.sleep(min(1.5 * attempt, 6))
                continue
            if r.status_code >= 400:  # 400/403/404: error real de la petición, no tiene caso reintentar
                raise ProviderError(f"Gemini respondió {r.status_code}: {r.text[:300]}", fatal=True)
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            except (KeyError, IndexError, ValueError) as e:
                raise ProviderError(f"Respuesta inesperada de Gemini: {r.text[:300]}", fatal=True) from e
        raise ProviderError(
            f"Gemini no respondió tras {self.retries} intentos (último: {last}). "
            "Tu red pierde paquetes hacia Google de forma intermitente; reintenta, o usa /local.",
            fatal=True, network=True)
