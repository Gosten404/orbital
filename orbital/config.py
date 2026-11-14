"""Carga de configuración: ~/.config/orbital/config.toml sobre valores por defecto."""
from __future__ import annotations

import os
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 (Ubuntu 22.04)
    import tomli as tomllib
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("ORBITAL_CONFIG", "~/.config/orbital/config.toml")).expanduser()

DEFAULTS: dict = {
    "voice": {
        "enabled": False,         # eventos de voz desde el arranque (/voice on|off en la sesión)
        "backend": "print",       # print = muestra el texto; none = nada; un motor de audio se agrega en voice.BACKENDS
        "max_chars": 200,         # largo máximo de una frase hablada
        "summarize": True,        # resumir con el modelo pequeño SOLO al terminar y si la respuesta es larga
        "summary_timeout": 12,    # s; si el modelo no responde, se recorta el texto
    },
    "identity": {
        "name": "O.R.B.I.T.A.L",   # nombre visible; cámbialo en config.toml
    },
    "models": {
        "ollama_url": "http://127.0.0.1:11434",
        "router": "qwen3.5:2b",        # decide la ruta (siempre cargado)
        "local": "qwen3.5:4b",         # solo se usa si tiers está vacío
        # Escalera de modelos locales: se elige según complejidad de la tarea y RAM libre.
        # ram_gb = RAM aproximada que necesita cargado (pesos + contexto). Ajusta si tu medición difiere.
        "tiers": [
            {"model": "qwen3.5:2b", "ram_gb": 3.0, "num_ctx": 8192},   # trivial (y ya está cargado: es el enrutador)
            # keep_alive: cuánto se queda en RAM tras usarse. El 4b se queda 30 min para no recargarlo del
            # disco en cada tarea (2b + 4b = 7.5 GB). El 9b se va pronto porque estorba a tus apps.
            {"model": "qwen3.5:4b", "ram_gb": 4.5, "num_ctx": 8192, "keep_alive": "30m"},   # normal
            {"model": "qwen3.5:9b", "ram_gb": 8.0, "num_ctx": 8192, "keep_alive": "5m"},    # complejo, solo si hay RAM
        ],
        "ram_margin_gb": 1.5,          # RAM que siempre se deja libre para tus apps
        "num_thread": 0,               # 0 = lo decide Ollama. Fíjalo SOLO si `python -m orbital.medir` muestra que otro va mejor
        "make_room_max_gb": 4.5,       # si al modelo pedido le falta hasta esto, intenta hacerle espacio
        "router_keep_alive": "30m",
        "local_keep_alive": "5m",      # se descarga pronto para liberar RAM
        # Contexto máximo. Sin límite, Ollama puede reservar GBs de RAM para el contexto y congelar la PC.
        "router_num_ctx": 8192,       # IGUAL al del escalón 2b: es el mismo modelo y Ollama lo RECARGA si num_ctx cambia
        "local_num_ctx": 8192,
        "gemini_models": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],   # escalera: si el primero agota su cuota diaria, sigue el siguiente (flash normal, 20/día, descartado)
        "gemini_api_key_env": "GEMINI_API_KEY",
        "ipv4_only": True,
        "gemini_read_timeout": 25.0,   # si Gemini no responde en N s, abandona y reintenta con otra conexión
        "gemini_retries": 4,          # reintentos ante cuelgue/503/429 (tu red pierde paquetes a veces)
        "gemini_base": "https://generativelanguage.googleapis.com/v1beta",  # cámbialo para usar otro gateway compatible
    },
    "routing": {
        # simple -> local | medium -> gemini | heavy -> humano (tú me lo llevas a mí)
        "simple": "local",
        "medium": "gemini",
        "heavy": "human",
        "gemini_only_for_code": True,  # sin código de por medio, lo "medium" se queda en local
    },
    "agent": {
        "max_steps": 15,
        "command_timeout": 60,
        "max_tool_output": 6000,
        "home_search_limit": 2,
        "final_review": True,
        "resource_retries": 8,
        "json_retries": 6,        # cuántos JSON cortados aguanta (recortando contexto) antes de escalar
        "gemini_conn_retries": 8, # intentos de CONEXIÓN a Gemini antes de bajar a local y seguir (nunca espera a reconectar)
        "slow_step_seconds": 0,   # 0 = apagado (v0.2.17): en las dos corridas reales bajar de modelo a media tarea la empeoró. Ponlo en 75 para reactivarlo  # un paso local más lento que esto cuenta como "lento" (0 = desactivado)
        "slow_step_strikes": 2,   # pasos lentos en la misma tarea antes de bajar un escalón de modelo
        "max_step_tokens": 1024,  # tope de tokens por paso local (0 = sin tope); si se corta por él, se duplica
        "max_step_tokens_hard": 4096,
        "quick_organize": True,        # organizar en sesión autónoma: UNA llamada al modelo (sin enrutador, sin plan, sin bucle)
        "quick_tier": 1,               # escalón para esa llamada: 0 = 2b (más rápido), 1 = 4b (más fiable con peticiones enredadas)
        "done_readonly_steps": 6,      # tras mover/escribir, si encadena esta cantidad de pasos de solo mirar, doy la tarea por terminada
        "slow_step_long_output": 300,  # un paso que generó más tokens que esto no es "lento": es largo (escribir un archivo)
        "organize_confirm_over": 0,   # mover/clasificar más archivos que esto SIEMPRE te pregunta, aun en sesión autónoma (0 = nunca)
        "result_chars_min": 1500,      # piso del tope por resultado de herramienta en modelos locales (el tope real depende de num_ctx)
        "sandbox": "auto",        # auto: bubblewrap si está instalado | required: sin bwrap no ejecuta | off
        "sandbox_network": True,  # los comandos del agente tienen internet (pip, git…); false para cortarla
        "plan_mode": "off",      # auto: plan solo al reorganizar | always: en toda tarea con herramientas | off     # en tareas de 3+ pasos, revisa que no falte nada antes de responder (+1 paso)   # búsquedas recursivas en todo el home por tarea (solo si las pides)
    },
    "monitor": {
        "enabled": True,
        "high": 85,             # % de saturación: espera y ofrece cerrar apps
        "critical": 95,         # %: pausa el razonamiento
        "wait_seconds": 8,
        "pause_seconds": 30,
        "pause_retries": 3,
        "reduced_model": "qwen3.5:2b",
        "reduced_num_ctx": 2048,
        "swap_growth_mb": 400,  # si el swap crece esto mientras genera, se corta (la PC empezó a congelarse)
    },
    "ui": {
        "done_sound_min_seconds": 5,   # el sonido de "terminé" solo suena si la tarea duró al menos esto (0 = siempre)
    },
    "projects": {
        # Cada SESIÓN autónoma crea su proyecto en <root>/Proyectos/<fecha_carpeta>/ (conocimiento.md, listas/, recursos/).
        # Lo que se hace fuera de una sesión va, solo si genera algo que guardar (listas completas, mapas), a
        # <root>/Individuales/<fecha_tarea>/. Los árboles de conocimiento viajan junto con sus recursos.
        "auto": True,
        "root": "~/Orbital",
    },
    "paths": {
        "data_dir": "~/.local/share/orbital",
        "socket": "",  # vacío = $XDG_RUNTIME_DIR/orbital.sock
    },
    "security": {
        # Nunca se leen ni se mandan a un modelo
        "secret_paths": ["~/.ssh", "~/.gnupg", "~/.password-store", "~/.config/orbital/secrets"],
        "secret_globs": [".env", "*.pem", "*.key", "id_rsa*", "*.kdbx"],
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    cfg = DEFAULTS
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            cfg = _merge(DEFAULTS, tomllib.load(f))
    return cfg


def data_dir(cfg: dict) -> Path:
    p = Path(cfg["paths"]["data_dir"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def socket_path(cfg: dict) -> Path:
    if cfg["paths"]["socket"]:
        return Path(cfg["paths"]["socket"]).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/orbital-{os.getuid()}"
    Path(runtime).mkdir(parents=True, exist_ok=True)
    return Path(runtime) / "orbital.sock"

