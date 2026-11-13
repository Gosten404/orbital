"""Proveedores de modelos. Todos exponen: async chat(messages, json_mode=False) -> str."""
from __future__ import annotations

import json
import re


class ProviderError(RuntimeError):
    """fatal=True: el proveedor no está disponible (red, key, servidor); no tiene caso reintentar.
    network=True: fue un fallo de CONEXIÓN (no una petición mala). Se reintenta y, tras el tope,
    se baja a local para seguir; nunca se queda esperando a reconectar."""
    def __init__(self, msg: str, fatal: bool = False, network: bool = False):
        super().__init__(msg)
        self.fatal = fatal
        self.network = network


def parse_json(text: str) -> dict:
    """Extrae el primer objeto JSON de una respuesta (tolera ```json y texto alrededor)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else []:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break
    raise ProviderError(f"El modelo no devolvió JSON válido: {text[:300]}")
