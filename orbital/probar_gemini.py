"""Prueba rápida: ¿Gemini responde AHORA? Reusa tu config, tu key del llavero y el provider real
(IPv4 forzado + reintentos). No imprime la key; solo su origen. Sale 0 si responde, 1 si no.

    python -m orbital.probar_gemini           # una petición real a Gemini
    python -m orbital.probar_gemini && orbital # encadénalo: solo lanza el cliente si Gemini vive
"""
from __future__ import annotations

import asyncio
import sys
import time

from . import config
from .providers import ProviderError
from .providers.gemini import Gemini


async def main() -> int:
    cfg = config.load()
    m = cfg["models"]
    g = Gemini(m.get("gemini_models") or m["gemini_model"], m["gemini_api_key_env"], m.get("ipv4_only", True),
               read_timeout=m.get("gemini_read_timeout", 25.0),
               retries=m.get("gemini_retries", 4),
               base=m.get("gemini_base", "https://generativelanguage.googleapis.com/v1beta"))

    if not g.available:
        print("[x] No hay API key de Gemini. Guárdala en el llavero:")
        print("    printf %s 'TU_KEY' | secret-tool store --label=\"O.R.B.I.T.A.L Gemini\" "
              "service orbital key gemini")
        return 1
    print(f"[i] Key encontrada (origen: {g.key_source}). Modelo: {g.model}. IPv4 forzado: {m.get('ipv4_only', True)}")
    print(f"[i] {m.get('gemini_retries', 4)} reintentos, timeout {m.get('gemini_read_timeout', 25.0)} s. "
          "Mando una petición mínima...")

    t0 = time.monotonic()
    try:
        r = await g.chat([{"role": "user", "content": "Responde solo: ok"}])
    except ProviderError as e:
        secs = time.monotonic() - t0
        kind = "fallo de CONEXIÓN" if getattr(e, "network", False) else "error de la petición"
        print(f"[x] Gemini NO respondió ({kind}) tras {secs:.1f} s: {e}")
        print("    -> En modo autónomo esto NO te cuelga: reintenta y, tras "
              f"{cfg['agent'].get('gemini_conn_retries', 8)} intentos de conexión, baja a local y sigue.")
        return 1
    secs = time.monotonic() - t0
    print(f"[+] Gemini respondió en {secs:.1f} s: {r.strip()[:80]!r}")
    print("    -> Existe y conecta. Puedes dejarle la tarea.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
