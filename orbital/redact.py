"""Tachado de secretos. Se aplica a TODO lo que sale de una herramienta antes de que lo vea un modelo,
y a lo que se escribe en logs, memoria y prompts de relevo. Si algo se cuela por una ruta no marcada
como secreta (p. ej. una API key en .bashrc), aquí se detiene."""
from __future__ import annotations

import re

PATTERNS = [
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("openai/anthropic-key", re.compile(r"\bsk-(?:ant-|proj-)?[0-9A-Za-z_\-]{20,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[0-9A-Za-z_]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)),
    # VAR_SECRETA=valor  /  export TOKEN="valor"  /  password: valor
    ("asignación", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSW(?:OR)?D|PASS|PWD|CREDENTIAL|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
        r"(\s*[:=]\s*)(['\"]?)(?!\[)([^\s'\"#]{6,})\3")),
]


# Valores exactos conocidos (tu key real, tokens del entorno). Cubre formatos que los patrones no reconocen.
KNOWN: set[str] = set()


def register_secret(value: str) -> None:
    if value and len(value) >= 12:
        KNOWN.add(value)


def register_env_secrets() -> None:
    import os
    for k, v in os.environ.items():
        if k in ("PWD", "OLDPWD"):  # son rutas de directorio, no contraseñas
            continue
        if re.search(r"KEY|TOKEN|SECRET|PASSW|PWD|CREDENTIAL", k, re.I):
            register_secret(v)


def redact(text: str) -> tuple[str, int]:
    """Devuelve (texto tachado, cuántos secretos se taparon)."""
    if not text:
        return text, 0
    n = 0
    for v in sorted(KNOWN, key=len, reverse=True):
        if v in text:
            n += text.count(v)
            text = text.replace(v, "[SECRETO TACHADO]")
    for name, rx in PATTERNS:
        if name == "asignación":
            text, k = rx.subn(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[SECRETO TACHADO]{m.group(3)}", text)
        else:
            text, k = rx.subn(f"[SECRETO TACHADO: {name}]", text)
        n += k
    return text, n


def clean(text: str) -> str:
    return redact(text)[0]


def scrub_data_dir() -> None:
    """Tacha secretos que hayan quedado en logs y memoria de versiones anteriores."""
    from . import config, secrets
    register_env_secrets()
    register_secret(secrets.keyring_get("gemini"))
    root = config.data_dir(config.load())
    total = files = 0
    for f in list(root.rglob("*.jsonl")) + list(root.rglob("*.md")):
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        new, n = redact(txt)
        if n:
            f.write_text(new)
            total += n
            files += 1
    print(f"secretos tachados en logs/memoria: {total} (en {files} archivos)")


if __name__ == "__main__":
    scrub_data_dir()
