"""Llavero del sistema (GNOME Keyring / KWallet vía libsecret).

La key queda cifrada en disco con tu contraseña de inicio de sesión y se desbloquea al entrar.
O.R.B.I.T.A.L la pide al arrancar; si el llavero no está disponible, usa la variable de entorno.

Guardar:   printf %s 'TU_KEY' | secret-tool store --label="O.R.B.I.T.A.L Gemini" service orbital key gemini
Consultar: secret-tool lookup service orbital key gemini
"""
from __future__ import annotations

import os
import shutil
import subprocess


def keyring_get(name: str) -> str:
    if not shutil.which("secret-tool"):
        return ""
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", "orbital", "key", name],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def get(name: str, env_var: str) -> tuple[str, str]:
    """(valor, origen). Primero el llavero, luego la variable de entorno."""
    v = keyring_get(name)
    if v:
        return v, "llavero"
    v = os.environ.get(env_var, "")
    return (v, "variable de entorno") if v else ("", "")
