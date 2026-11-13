"""Política de permisos.

Fuera de una sesión autónoma: leer es libre; escribir y ejecutar SIEMPRE piden confirmación.
En una sesión autónoma: lo que está dentro del nivel y de las carpetas permitidas se hace sin
preguntar; lo que queda fuera se rechaza; los comandos peligrosos siempre piden confirmación.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = {
    0: "solo lectura",
    1: "lectura + escritura en carpetas permitidas",
    2: "lo anterior + ejecutar comandos en carpetas permitidas",
    3: "sistema completo (ejecutar en cualquier ruta; lo destructivo pide confirmación)",
}

DANGEROUS = [
    r"\brm\s+(-\w*r\w*f|-\w*f\w*r)\b",
    r"\bsudo\b",
    r"\bmkfs",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\s+777\b",
    r"\bchown\s+-R\b",
    r":\(\)\s*\{",            # fork bomb
    r"\bcurl\b.*\|\s*(ba)?sh",
    r"\bwget\b.*\|\s*(ba)?sh",
    r"\bgit\s+push\b.*--force",
    r"\bshutdown\b|\breboot\b|\bpoweroff\b",
    r"\bsystemctl\s+(stop|disable|mask)\b",
]

# Comandos que BORRAN (o destruyen contenido sin respaldo). Fuera de la guardia: en sesión autónoma
# preguntan siempre (antes un "rm archivo" suelto corría sin preguntar). Con la guardia: se rechazan.
DELETE = [
    r"(^|[;&|(`]\s*|\s|/|\\)(rm|rmdir|unlink|shred|srm|trash-put|gio\s+(trash|remove))\b",
    r"\bfind\b.*\s-delete\b",
    r"\bfind\b.*-exec(dir)?\s+(rm|unlink|shred)\b",
    r"\bxargs\b.*\b(rm|unlink|shred)\b",
    r"\btruncate\b",
    r"\bmv\b.*\s/dev/null\b",
    r"\bgit\s+(clean|reset\s+--hard|checkout\s+--\s)",
    r"os\.(remove|unlink|rmdir|removedirs)\b|shutil\.rmtree|\.unlink\(|\.rmdir\(",
]

READ_TOOLS = {"read_file", "list", "grep", "find", "recent", "tree", "inspect", "map"}
WRITE_TOOLS = {"write_file"}
EXEC_TOOLS = {"run"}
ORGANIZE_TOOLS = {"move", "classify", "undo", "group", "empties", "rename"}  # mueven (nivel 1, como escribir); solo "empties" elimina, y solo carpetas sin archivos


def tool_paths(args: dict) -> list[Path]:
    """Todas las rutas de origen que nombra una llamada: ruta, origen y la lista 'paths'."""
    out = []
    for key in ("path", "source"):
        if args.get(key):
            out.append(Path(str(args[key])))
    r = args.get("paths")
    for x in ([r] if isinstance(r, str) else (r or [])):
        out.append(Path(str(x)))
    return out


@dataclass
class Policy:
    autonomous: bool = False
    level: int = 0
    folders: list[Path] = field(default_factory=list)
    secret_paths: list[Path] = field(default_factory=list)
    secret_globs: list[str] = field(default_factory=list)
    protected: list[Path] = field(default_factory=list)  # su propio código
    no_delete: bool = False  # guardia "no borrar": invariante mientras dure la sesión
    no_touch: list[Path] = field(default_factory=list)  # "no toques X": ni leer ni mover ni escribir ahí

    def touches_forbidden(self, tool: str, args: dict) -> Path | None:
        if not self.no_touch:
            return None
        for p0 in tool_paths(args) + ([Path(str(args["dest"]))] if args.get("dest") else []):
            p = p0.expanduser().resolve()
            for f in self.no_touch:
                if p == f or f in p.parents:
                    return f
        cmd = str(args.get("command") or "")
        for f in self.no_touch:
            if cmd and (str(f) in cmd or f.name in cmd):
                return f
        return None

    def describe(self) -> str:
        guard = " · delete=false" if self.no_delete else ""
        if not self.autonomous:
            return "Modo asistido: leer libre, escribir/ejecutar con confirmación." + guard
        dirs = ", ".join(str(f) for f in self.folders) or "(ninguna)"
        return f"Sesión autónoma · nivel {self.level} ({LEVELS[self.level]}) · carpetas: {dirs}" + guard

    # --- utilidades ---
    # Siempre secretos, aunque tu config.toml tenga una lista vieja: suelen guardar keys y contraseñas.
    ALWAYS_SECRET_GLOBS = [".env", ".env.*", "*.env", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.kdbx",
                           ".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile",
                           ".bash_history", ".zsh_history", ".python_history", ".mysql_history", ".psql_history",
                           ".netrc", ".pgpass", ".git-credentials", "credentials", "credentials.json",
                           "secrets.*", "*.keystore", "*.jks"]
    ALWAYS_SECRET_PATHS = ["~/.ssh", "~/.gnupg", "~/.password-store", "~/.aws", "~/.config/gcloud",
                           "~/.docker", "~/.kube", "~/.config/orbital/env", "~/.mozilla", "~/.config/BraveSoftware",
                           "~/.config/google-chrome", "~/.local/share/keyrings"]

    def is_secret(self, path: Path) -> bool:
        p0 = path.expanduser()
        if any(fnmatch.fnmatch(p0.name, g) for g in self.ALWAYS_SECRET_GLOBS):
            return True
        try:
            r = p0.resolve()
            if any(r == Path(x).expanduser().resolve() or Path(x).expanduser().resolve() in r.parents
                   for x in self.ALWAYS_SECRET_PATHS):
                return True
        except OSError:
            pass
        p = path.expanduser().resolve()
        for s in self.secret_paths:
            s = s.expanduser().resolve()
            if p == s or s in p.parents:
                return True
        return any(fnmatch.fnmatch(p.name, g) for g in self.secret_globs)

    def in_folders(self, path: Path) -> bool:
        p = path.expanduser().resolve()
        return any(p == f or f in p.parents for f in (x.expanduser().resolve() for x in self.folders))

    def is_protected(self, path: Path) -> bool:
        p = path.expanduser().resolve()
        return any(p == x or x in p.parents for x in (y.resolve() for y in self.protected))

    def mentions_protected(self, cmd: str) -> bool:
        """¿El comando nombra una ruta dentro de mi código? Compara rutas reales, no subcadenas:
        'unzip -l ~/orbital.zip' NO toca el código; 'sed -i ... ~/orbital/orbital/core.py' sí."""
        import shlex
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        for w in words:
            for piece in w.replace("=", " ").replace("'", " ").replace('"', " ").split():
                if "/" not in piece and not piece.startswith("~"):
                    continue
                cand = Path(piece.rstrip(";,)")).expanduser()
                try:
                    cand = cand.resolve()
                except OSError:
                    continue
                if any(cand == x.resolve() or x.resolve() in cand.parents for x in self.protected):
                    return True
        return False

    def mentions_secret(self, cmd: str) -> bool:
        import shlex
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        for w in words:
            for piece in re.split(r"[=;|&<>()]", w):
                if piece and ("/" in piece or piece.startswith((".", "~")) or "secret" in piece.lower()):
                    if self.is_secret(Path(piece)):
                        return True
        return bool(re.search(r"\bhistory\b|\bprintenv\b|^\s*env\s*$|\bset\s*$|secret-tool|/proc/\S*/environ|"
                              r"os\.environ|getenv|\$\{?\w*(KEY|TOKEN|SECRET)", cmd, re.I))

    @staticmethod
    def is_dangerous(cmd: str) -> bool:
        return any(re.search(rx, cmd) for rx in DANGEROUS)

    @staticmethod
    def is_delete(cmd: str) -> bool:
        return any(re.search(rx, cmd) for rx in DELETE)

    # --- decisión: "allow" | "ask" | "deny:<motivo>" ---
    def check(self, tool: str, args: dict) -> str:
        hit = self.touches_forbidden(tool, args)
        if hit is not None:
            return f"deny:veto activo sobre {hit.name}: excluida por completo"
        path = Path(args.get("path") or args.get("cwd") or ".")
        if tool in READ_TOOLS:
            if any(self.is_secret(x) for x in (tool_paths(args) or [path])):
                return "deny:ruta marcada como secreta"
            return "allow"

        if tool in WRITE_TOOLS:
            if self.is_protected(path):
                return "deny:código propio protegido; nombre y ajustes se cambian en config.toml"
            if self.is_secret(path):
                return "deny:ruta marcada como secreta"
            if not self.autonomous:
                return "ask"
            if self.level >= 1 and self.in_folders(path):
                return "allow"
            return "deny:fuera del nivel o de las carpetas permitidas"

        if tool in EXEC_TOOLS:
            cmd = args.get("command", "")
            if self.mentions_secret(cmd):
                return "deny:el comando toca un archivo secreto (historial, .bashrc, llaves, credenciales)"
            if self.mentions_protected(cmd) and not cmd.strip().startswith(("ls", "cat", "grep", "git status", "git log", "git diff")):
                return "deny:el comando modifica la carpeta de código propio (protegida); leer y listar sí"
            deletes = self.is_delete(cmd)
            if deletes and self.no_delete:
                return ("deny:delete=false: borrar prohibido en la sesión. Usar move o classify; /delete true lo permite")
            if not self.autonomous:
                return "ask"
            if self.is_dangerous(cmd) or deletes:
                return "ask"
            if self.level >= 3:
                return "allow"
            if self.level >= 2 and self.in_folders(path):
                return "allow"
            return "deny:fuera del nivel o de las carpetas permitidas"

        if tool in ORGANIZE_TOOLS:
            srcs = tool_paths(args) or [Path(".")]
            if args.get("preview"):
                return self.check_write_paths(srcs, read_only=True)
            if tool == "empties" and self.no_delete:
                return ("deny:delete=false: eliminar carpetas vacías requiere /delete true. Informar al usuario; sin reintentos ni rodeos con ejecutar")
            return self.check_write_paths(srcs + ([Path(args["dest"])] if args.get("dest") else []))

        if tool == "ask_human":
            return "allow"
        return "deny:herramienta desconocida"

    def check_write_paths(self, paths: list[Path], read_only: bool = False) -> str:
        """Regla de escritura aplicada a TODAS las rutas que toca una operación (origen y destino)."""
        for p in paths:
            if self.is_secret(p):
                return "deny:ruta marcada como secreta"
            if not read_only and self.is_protected(p):
                return "deny:código propio protegido"
        if read_only:
            return "allow"
        if not self.autonomous:
            return "ask"
        if self.level >= 1 and all(self.in_folders(p) for p in paths):
            return "allow"
        return "deny:fuera del nivel o de las carpetas permitidas (origen y destino deben estar dentro)"
