from __future__ import annotations

import asyncio

import httpx

from . import ProviderError

BASE = "https://generativelanguage.googleapis.com/v1beta"


class Gemini:
    def __init__(self, model: str, api_key_env: str = "GEMINI_API_KEY", ipv4_only: bool = True,
                 read_timeout: float = 25.0, retries: int = 4, base: str = BASE):
        self.ipv4_only = ipv4_only
        self.model = model
        self.base = base.rstrip("/")
        self.read_timeout = read_timeout
        self.retries = max(1, retries)
        from .. import secrets
        from ..redact import register_secret
        self.key, self.key_source = secrets.get("gemini", api_key_env)
        register_secret(self.key)  # su valor exacto se tacha en todo lo que sale, tenga el formato que tenga
        self.name = f"gemini:{model}"

    @property
    def available(self) -> bool:
        return bool(self.key) and not getattr(self, "off", False)

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
        url = f"{self.base}/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.key}

        # La red pierde paquetes a ciertas IPs de Google de forma intermitente. Cada intento abre una
        # conexión NUEVA (otra IP posible): si una se cuelga, se abandona rápido y se reintenta.
        last = "sin detalle"
        for attempt in range(1, self.retries + 1):
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
            if r.status_code in (429, 503):  # saturado o sobre el límite: espera corta y reintenta
                last = f"HTTP {r.status_code} (Gemini saturado o sobre el límite del plan)"
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
