"""Mide la velocidad REAL de tus modelos en tu PC, con hilos distintos, para decidir num_thread con datos.

    python -m orbital.medir                       # qwen3.5:4b con: lo que decida Ollama, 8, 6 y 4 hilos
    python -m orbital.medir --modelo qwen3.5:9b --hilos 0,8 --repeticiones 3

No cambia la calidad: es el mismo modelo con los mismos pesos; solo cambia cuántos núcleos lo calculan.
Mide las dos fases por separado:
  - leer:    procesar el contexto (prompt del agente + resultados). Depende sobre todo del CPU.
  - generar: escribir la respuesta token a token. Depende sobre todo del ANCHO DE BANDA de la RAM.
Cada medición empieza con un texto distinto para que la caché de Ollama no haga trampa.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

import httpx

from . import config, tools
from .core import AGENT_PROMPT
from .providers.ollama import stats_from

TASK = ("Organiza mi carpeta de Descargas por tipo de archivo. Hay 2550 archivos: jpg, pdf, zip, mp4, deb. "
        "No borres nada. Empieza revisando la carpeta.")


def build_messages(nonce: str) -> list[dict]:
    system = AGENT_PROMPT.format(tools=tools.TOOL_DOCS, cwd="/home/usuario/Descargas",
                                 policy="Sesión autónoma · nivel 1", name="O.R.B.I.T.A.L",
                                 config="~/.config/orbital/config.toml", code="~/orbital/orbital")
    return [{"role": "system", "content": f"[medición {nonce}]\n{system}"}, {"role": "user", "content": TASK}]


def run_once(url: str, model: str, threads: int, num_ctx: int, nonce: str, timeout: float = 900) -> dict:
    opts = {"temperature": 0, "seed": 42, "num_predict": 96, "num_ctx": num_ctx}
    if threads > 0:
        opts["num_thread"] = threads
    body = {"model": model, "messages": build_messages(nonce), "stream": False, "think": False,
            "format": "json", "keep_alive": "5m", "options": opts}
    r = httpx.post(f"{url.rstrip('/')}/api/chat", json=body, timeout=httpx.Timeout(timeout, connect=10))
    r.raise_for_status()
    return stats_from(r.json())


def summarize(results: dict[int, list[dict]]) -> list[dict]:
    """Mediana por configuración de hilos, ordenada de más rápida a más lenta generando."""
    rows = []
    for th, runs in results.items():
        if not runs:
            continue
        rows.append({"hilos": th,
                     "leer_tps": round(statistics.median(x["prompt_tps"] for x in runs), 1),
                     "generar_tps": round(statistics.median(x["eval_tps"] for x in runs), 1),
                     "paso_tipico_s": round(statistics.median(x["prompt_s"] + x["eval_s"] for x in runs), 1)})
    return sorted(rows, key=lambda r: r["paso_tipico_s"])


def recommend(rows: list[dict], margin: float = 0.05) -> str:
    """Solo recomienda fijar hilos si gana con claridad (>5 %) a dejar que Ollama decida."""
    if not rows:
        return "Sin datos."
    base = next((r for r in rows if r["hilos"] == 0), None)
    best = rows[0]
    if base is None or best["hilos"] == 0:
        return "num_thread = 0: la elección automática de Ollama ya es la óptima."
    if best["paso_tipico_s"] < base["paso_tipico_s"] * (1 - margin):
        gain = 100 * (1 - best["paso_tipico_s"] / base["paso_tipico_s"])
        return (f"Configurar en ~/.config/orbital/config.toml, sección [models]: num_thread = {best['hilos']}"
                f" ({gain:.0f} % más rápido por paso). Reiniciar orbitald.")
    return "Diferencia menor al 5 %: num_thread = 0."


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modelo", default=cfg["models"]["local"])
    ap.add_argument("--hilos", default="0,8,6,4", help="lista; 0 = lo que decida Ollama")
    ap.add_argument("--repeticiones", type=int, default=2)
    ap.add_argument("--num-ctx", type=int, default=8192)
    a = ap.parse_args(argv)
    url = cfg["models"]["ollama_url"]
    hilos = [int(x) for x in a.hilos.split(",") if x.strip() != ""]
    print(f"Midiendo {a.modelo} · hilos {hilos} · {a.repeticiones} repeticiones (cierra apps pesadas para "
          "que la medición sea limpia)")
    results: dict[int, list[dict]] = {}
    for th in hilos:
        label = "Ollama decide" if th == 0 else f"{th} hilos"
        try:
            print(f"[~] {label}: calentando (carga el modelo con esta configuración)...", flush=True)
            run_once(url, a.modelo, th, a.num_ctx, f"calentar-{th}-{time.time()}")
            results[th] = []
            for i in range(a.repeticiones):
                st = run_once(url, a.modelo, th, a.num_ctx, f"{th}-{i}-{time.time()}")
                results[th].append(st)
                print(f"    leer {st['prompt_tokens']} tok a {st['prompt_tps']} tok/s · "
                      f"generar {st['eval_tokens']} tok a {st['eval_tps']} tok/s", flush=True)
        except httpx.HTTPError as e:
            print(f"[x] {label}: {type(e).__name__} {e}. ¿Ollama corre? (systemctl status ollama)")
            return 1
    rows = summarize(results)
    print("\nhilos         leer tok/s   generar tok/s   paso típico")
    for r in rows:
        name = "Ollama" if r["hilos"] == 0 else str(r["hilos"])
        print(f"{name:<13} {r['leer_tps']:>10}   {r['generar_tps']:>13}   {r['paso_tipico_s']:>9} s")
    print("\n" + recommend(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
