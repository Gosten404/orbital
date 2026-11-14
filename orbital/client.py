"""orbital: cliente de terminal. Se conecta a orbitald; cerrarlo no apaga a O.R.B.I.T.A.L."""
from __future__ import annotations

import asyncio
import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from . import __version__, config
from . import voice as voicemod
from .permissions import LEVELS

con = Console()

HELP = """[bold]Comandos[/]
  /session            iniciar sesión autónoma (nivel, carpetas, pasos)
  /end                terminar sesión autónoma
  /cd <path>          cambiar carpeta de trabajo
  /state              permisos, modelos, proyecto, veto, delete
  /undo [id]          revertir el último lote de archivos (o el lote indicado)
  /delete true|false  false = borrar prohibido (move y classify siguen permitidos). Sin argumento: estado
  /touch clear        vaciar el veto («no toques X» lo crea)
  /mode auto|local|gemini|human     fijar el destino de las peticiones
  /local|/gemini|/human <text>      forzar solo esa petición
  /model auto|2b|4b|9b              fijar el modelo local
  /plan <task>        plan con aprobación previa · /planmode auto|always|off
  /node [new <name> [topic|project] [folder] | <name> | folder <path> | forget | delete <name>]
  /resume             cargar la bitácora de la última tarea
  /forget             vaciar el historial de la conversación
  /sound on|off       campana al pedir autorización; doble tono al terminar
  /voice on|off       eventos de voz (inicio, ayuda, error, fin)
  /help · /quit
[bold]Durante una tarea[/] (línea ↳)
  /cancel  /delete true|false  /touch clear  /state  /note <text>   (otro texto queda en cola)
  Ctrl+C cancela la tarea"""


def copy_clipboard(text: str) -> bool:
    if shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False)
        return True
    if shutil.which("xsel"):
        subprocess.run(["xsel", "--clipboard", "--input"], input=text.encode(), check=False)
        return True
    return False


def split_input(raw: str) -> list[str]:
    """Qué se manda al daemon a partir de lo tecleado o pegado.
    Un pegado de varias líneas es UN solo mensaje. Antes cada línea salía como tarea aparte: la
    primera corría sin las restricciones que venían en las siguientes ("no toques...", "no borres").
    Única excepción: si TODAS las líneas son comandos (/session, /cd...), van una por una."""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return lines
    if all(ln.startswith("/") for ln in lines):
        return lines
    return ["\n".join(lines)]


LIVE_HELP = "/cancel · /delete true|false · /touch clear · /state · /note <text> · otro texto queda en cola"


KNOWN = {"/quit", "/exit", "/help", "/voice", "/session", "/end", "/cd", "/mode", "/state", "/resume",
         "/forget", "/local", "/gemini", "/human", "/model", "/node", "/plan", "/planmode", "/sound",
         "/delete", "/undo", "/touch"}
RENAMED = {"/ayuda": "/help", "/deshacer": "/undo", "/sesion": "/session", "/fin": "/end", "/estado": "/state",
           "/retomar": "/resume", "/olvidar": "/forget", "/modo": "/mode", "/modelo": "/model", "/nodo": "/node",
           "/tocar": "/touch", "/sonido": "/sound", "/voz": "/voice", "/salir": "/quit", "/humano": "/human",
           "/noborrar": "/delete", "/nota": "/note", "/cancelar": "/cancel"}
USAGE_DELETE = "Uso: /delete true|false"


def unknown_command(cmd: str) -> str:
    new = RENAMED.get(cmd.lower())
    return f"[x] {cmd} ahora es {new} (/help)" if new else f"[x] Comando desconocido: {cmd} (/help)"


def parse_bool(v: str) -> str | None:
    """'true'/'false' -> valor interno de la guardia: delete=false activa la guardia ('on'), delete=true la apaga ('off')."""
    return {"true": "off", "false": "on"}.get(v.strip().lower())


def classify_live(line: str) -> tuple[str, object]:
    """Qué hacer con una línea tecleada MIENTRAS corre una tarea.
    ('send', mensaje) al daemon · ('cancel', None) · ('say', texto) · ('queue', línea) · ('none', None)."""
    line = line.strip()
    if not line:
        return "none", None
    cmd, _, rest = line.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    if cmd in ("/cancel", "/stop"):
        return "cancel", None
    if cmd == "/delete":
        if rest and parse_bool(rest) is None:
            return "say", USAGE_DELETE
        return "send", {"type": "live", "cmd": "nodelete", "value": parse_bool(rest) or ""}
    if cmd == "/touch":
        return ("send", {"type": "live", "cmd": "notouch_clear"}) if rest.lower() == "clear" else ("say", "Uso: /touch clear")
    if cmd == "/state":
        return "send", {"type": "live", "cmd": "state"}
    if cmd == "/note":
        return ("send", {"type": "live", "cmd": "nota", "value": rest}) if rest else ("say", "Uso: /note <text>")
    if cmd in ("/help", "?"):
        return "say", LIVE_HELP
    if cmd.startswith("/") and cmd not in KNOWN:
        return "say", unknown_command(cmd)
    return "queue", line


class Client:
    def __init__(self):
        self.cfg = config.load()
        try:
            self.voice_backend = voicemod.make_backend(self.cfg["voice"]["backend"],
                                                       lambda s: con.print(f"[magenta]{escape(s)}[/]"))
        except ValueError as e:
            con.print(f"[yellow]{escape(str(e))}[/]")
            self.voice_backend = voicemod.NullBackend()
        self.name = self.cfg['identity']['name']
        self.sound_on = True
        self.was_cancelled = False
        hist = config.data_dir(self.cfg) / "prompt_history"
        self.prompt = PromptSession(history=FileHistory(str(hist)))
        self.live_prompt = PromptSession(history=InMemoryHistory())   # la línea de entrada MIENTRAS trabaja
        self.queued: list[str] = []
        self.live_task: asyncio.Task | None = None
        self.live_active = False
        self.spin_text = ""
        self.spin_t0 = 0.0

    async def reconnect(self) -> bool:
        """orbitald se reinició (p. ej. por instalar.sh): espero hasta 15 s a que vuelva."""
        con.print(escape("[~] Conexión con orbitald perdida. Reconectando…"), style="yellow")
        sock = config.socket_path(self.cfg)
        for _ in range(30):
            try:
                self.r, self.w = await asyncio.open_unix_connection(str(sock))
                await self.recv()  # saludo
                con.print(escape("[+] Reconectado"), style="green")
                return True
            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
                await asyncio.sleep(0.5)
        con.print(escape("[x] orbitald no responde: journalctl --user -u orbitald -n 30"), style="red")
        return False

    async def connect(self):
        sock = config.socket_path(self.cfg)
        try:
            self.r, self.w = await asyncio.open_unix_connection(str(sock))
        except (FileNotFoundError, ConnectionRefusedError):
            con.print(f"[red]orbitald no está activo[/] ({sock}). Iniciar: [bold]systemctl --user start orbitald[/] o [bold]orbitald[/]")
            sys.exit(1)

    async def send(self, msg: dict):
        self.w.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.w.drain()

    async def recv(self) -> dict:
        line = await self.r.readline()
        if not line:
            raise ConnectionResetError("orbitald cerró la conexión")
        return json.loads(line)

    SOUND_FILES = ["/usr/share/sounds/freedesktop/stereo/message.oga",
                   "/usr/share/sounds/freedesktop/stereo/dialog-information.oga",
                   "/usr/share/sounds/freedesktop/stereo/bell.oga"]

    # Sonido de TERMINÉ: distinto al de pedir ayuda (campana/mensaje) para distinguirlos sin mirar la pantalla.
    DONE_SOUND_FILES = ["/usr/share/sounds/freedesktop/stereo/complete.oga",
                        "/usr/share/sounds/freedesktop/stereo/service-login.oga",
                        "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"]

    @staticmethod
    def pick_sound(kind: str, exists=None) -> str | None:
        """Archivo de sonido para 'ask' (necesito autorización) o 'done' (terminé). Nunca el mismo."""
        exists = exists or (lambda f: Path(f).exists())
        files = Client.DONE_SOUND_FILES if kind == "done" else Client.SOUND_FILES
        other = set(Client.SOUND_FILES if kind == "done" else Client.DONE_SOUND_FILES)
        return next((f for f in files if exists(f) and f not in other), None)

    def beep_done(self, secs: float) -> None:
        """Suena al TERMINAR una tarea que duró un rato (no al cancelar). Respeta /sonido."""
        if not getattr(self, "sound_on", True) or getattr(self, "was_cancelled", False):
            return
        if secs < float(self.cfg.get("ui", {}).get("done_sound_min_seconds", 5)):
            return
        self.beep("done")

    def beep(self, kind: str = "ask") -> None:
        """'ask': necesito tu autorización. 'done': terminé (útil si estás en otra ventana o en una reunión)."""
        if not getattr(self, "sound_on", True):
            return
        player = shutil.which("paplay") or shutil.which("aplay") or shutil.which("ffplay")
        snd = self.pick_sound(kind)
        if player and snd:
            args = [player, snd] if "ffplay" not in player else [player, "-nodisp", "-autoexit", "-loglevel", "quiet", snd]
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        sys.stdout.write("\a"); sys.stdout.flush()  # campana del terminal como respaldo
        if kind == "done":  # terminé = doble campana (pedir ayuda = una)
            try:
                asyncio.get_running_loop().call_later(0.25, lambda: (sys.stdout.write("\a"), sys.stdout.flush()))
            except RuntimeError:
                pass

    async def pump(self):
        """Procesa mensajes del daemon hasta 'done'. Ctrl+C mientras trabaja = cancelar la tarea."""
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self.cancel)
        try:
            with patch_stdout(raw=True):   # lo que imprimo sale ARRIBA de la línea de entrada
                self.start_live()
                try:
                    await self._pump()
                finally:
                    await self.stop_live()
        finally:
            loop.remove_signal_handler(signal.SIGINT)

    # ---- entrada viva: escribir comandos mientras la tarea corre ----
    def live_line(self, line: str) -> None:
        kind, val = classify_live(line)
        if kind == "cancel":
            self.cancel()
        elif kind == "send":
            self.w.write((json.dumps(val, ensure_ascii=False) + "\n").encode())
        elif kind == "say":
            con.print(f"[dim]{escape(str(val))}[/]")
        elif kind == "queue":
            self.queued.append(str(val))
            con.print(f"[dim]{escape(f'[+] En cola ({len(self.queued)}): {val[:70]}')}[/]")

    def toolbar(self) -> str:
        if self.spin_text:
            secs = asyncio.get_running_loop().time() - self.spin_t0
            return f" [{self.SPIN[int(secs * 6) % 4]}] {self.spin_text} {secs:4.0f}s · Ctrl+C cancela · /help"
        return " en curso · Ctrl+C cancela · /help"

    async def _prompt_live(self) -> str:
        kw = dict(bottom_toolbar=self.toolbar, refresh_interval=0.2)
        try:
            return await self.live_prompt.prompt_async("  ↳ ", handle_sigint=False, **kw)
        except TypeError:   # prompt_toolkit antiguo sin handle_sigint
            return await self.live_prompt.prompt_async("  ↳ ", **kw)

    async def _live_loop(self) -> None:
        while True:
            try:
                line = await self._prompt_live()
            except KeyboardInterrupt:   # Ctrl+C con la línea viva = cancelar la tarea
                self.cancel()
                await asyncio.sleep(0.3)
                continue
            except EOFError:
                return
            self.live_line(line)

    def start_live(self) -> None:
        if self.live_task is None or self.live_task.done():
            self.live_active = True
            self.live_task = asyncio.create_task(self._live_loop())

    async def stop_live(self) -> None:
        t, self.live_task, self.live_active = self.live_task, None, False
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def cancel(self):
        self.was_cancelled = True
        self.spin_stop()
        con.print("[bold yellow]" + escape("[x] Cancelando: la CPU se libera al terminar el paso") + "[/]")
        self.w.write(b'{"type": "cancel"}\n')

    # ---- spinner: una sola línea animada mientras el modelo piensa ----
    SPIN = "|/-\\"

    def spin_start(self, text: str) -> None:
        self.spin_stop()
        self.spin_text = text
        self.spin_t0 = asyncio.get_running_loop().time()
        if not self.live_active:   # con la línea viva el spinner va en la barra inferior (no pelea con la salida)
            self.spin_task = asyncio.create_task(self._spin())

    async def _spin(self) -> None:
        t0 = asyncio.get_running_loop().time()
        i = 0
        try:
            while True:
                secs = asyncio.get_running_loop().time() - t0
                sys.stdout.write(f"\r\033[K\033[36m[{self.SPIN[i % 4]}] {self.spin_text} {secs:4.0f}s\033[0m")
                sys.stdout.flush()
                i += 1
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            secs = asyncio.get_running_loop().time() - t0
            sys.stdout.write(f"\r\033[K\033[2m[=] {self.spin_text} ({secs:.0f}s)\033[0m\n")
            sys.stdout.flush()
            raise

    def spin_stop(self) -> None:
        task = getattr(self, "spin_task", None)
        if task and not task.done():
            task.cancel()
        elif self.spin_text and self.live_active:
            secs = asyncio.get_running_loop().time() - self.spin_t0
            con.print(f"[dim]{escape(f'[=] {self.spin_text} ({secs:.0f}s)')}[/]")
        self.spin_task = None
        self.spin_text = ""

    async def _pump(self):
        while True:
            m = await self.recv()
            if getattr(self, "spin_task", None) or (self.live_active and self.spin_text):
                self.spin_stop()
                await asyncio.sleep(0)  # deja que el spinner escriba su línea final
            t = m["type"]
            if t == "done":
                return
            if t == "status":
                txt = m["text"].strip()
                if txt.endswith("pensando...") or txt.endswith("decidiendo ruta..."):
                    self.spin_start(txt.removeprefix("[=] ").removesuffix("..."))
                    continue
                style = ("bold yellow" if txt.startswith(("[!]", "[~]")) else "red" if txt.startswith("[x]")
                         else "green" if txt.startswith("[+]") else "cyan" if txt.startswith("[=]") else "dim")
                con.print(f"[{style}]{escape(m['text'])}[/]")
            elif t == "info":
                con.print(Panel(m["text"], border_style="cyan", title=self.name))
            elif t == "voice":
                await self.voice_backend.speak(m["text"], m["kind"])
            elif t == "say":
                title = f"{self.name} · relevo humano" if m.get("source") == "relevo" else self.name
                con.print(Panel(Markdown(m["text"]), border_style="green", title=title))
            elif t in ("confirm", "choose", "relay"):
                self.beep()
                await self.stop_live()   # una sola entrada a la vez: la pregunta tiene prioridad
                try:
                    if t == "confirm":
                        value = await self.ask_yes("[yellow]¿Permitir?[/] " + escape(m["text"]))
                    elif t == "choose":
                        value = await self.do_choose(m)
                    else:
                        value = await self.do_relay(m["text"])
                except (KeyboardInterrupt, EOFError):
                    self.cancel()
                    continue
                await self.send({"type": "reply", "id": m["id"], "value": value})
                self.start_live()

    async def do_choose(self, m: dict) -> list[int]:
        con.print(Panel("\n".join(f"[bold]{i + 1}[/]. {escape(o)}" for i, o in enumerate(m["options"])),
                        title=escape(m["text"]), border_style="yellow"))
        hint = "números separados por coma; vacío = ninguno" if m.get("multi") else "un número"
        raw = (await self.prompt.prompt_async(f"  [{hint}] ")).strip()
        picks = []
        for x in raw.replace(" ", "").split(","):
            if x.isdigit() and 1 <= int(x) <= len(m["options"]):
                picks.append(int(x) - 1)
        return picks[:1] if not m.get("multi") else picks

    async def ask_yes(self, question: str) -> bool:
        con.print(question)
        ans = (await self.prompt.prompt_async("  [s/N] ")).strip().lower()
        return ans in ("s", "si", "sí", "y", "yes")

    async def do_relay(self, prompt: str) -> str:
        con.print(Panel(prompt, border_style="magenta",
                        title="Relevo humano · pegar en Claude", subtitle="relevo humano"))
        if copy_clipboard(prompt):
            con.print("[magenta]" + escape("[+]") + " Copiado al portapapeles[/]")
        else:
            con.print("[magenta]Portapapeles no disponible: sudo apt install xclip[/]")
        con.print("Pegar la respuesta y cerrar con una línea [bold]FIN[/] (o [bold]CANCELAR[/]).")
        lines = []
        while True:
            line = await self.prompt.prompt_async("| ")
            if line.strip() == "FIN":
                break
            if line.strip() == "CANCELAR":
                return "Consulta cancelada por el usuario. Continuar con lo disponible o indicar qué falta."
            lines.append(line)
        return "\n".join(lines)

    async def start_session(self):
        con.print(Panel("\n".join(f"{k} = {v}" for k, v in LEVELS.items()),
                        title="Niveles de permiso", border_style="yellow"))
        lvl = (await self.prompt.prompt_async("Nivel [0-3]: ")).strip()
        if lvl not in {"0", "1", "2", "3"}:
            con.print("[red]Nivel inválido: sesión cancelada[/]")
            return False
        raw = await self.prompt.prompt_async("Carpetas permitidas (coma; vacío = cwd): ")
        folders = [f.strip() for f in raw.split(",") if f.strip()] or [str(Path.cwd())]
        steps = (await self.prompt.prompt_async("Máximo de pasos [15]: ")).strip() or "15"
        con.print(f"[yellow]Nivel {lvl} · {', '.join(folders)} · {steps} pasos[/]")
        if not await self.ask_yes("¿Iniciar la sesión autónoma?"):
            return False
        await self.send({"type": "session", "level": int(lvl), "folders": folders, "max_steps": int(steps)})
        return True

    @staticmethod
    def parse_nodo(rest: str) -> dict:
        rest = rest.strip()
        if not rest:
            return {"type": "node", "action": "list"}
        sub, _, resto = rest.partition(" ")
        sub, resto = sub.lower(), resto.strip()
        if sub == "new":
            nombre, _, cola = resto.partition(" ")
            cola = cola.strip()
            tipo = "proyecto"
            primera, _, tras = cola.partition(" ")
            if primera.lower() in ("topic", "project"):
                tipo, cola = {"topic": "tema", "project": "proyecto"}[primera.lower()], tras.strip()
            return {"type": "node", "action": "new", "nombre": nombre, "tipo": tipo, "carpeta": cola or None}
        if sub == "folder":
            return {"type": "node", "action": "folder", "carpeta": resto or None}
        if sub == "forget":
            return {"type": "node", "action": "forget"}
        if sub == "delete":
            return {"type": "node", "action": "delete", "nombre": resto}
        return {"type": "node", "action": "switch", "nombre": rest}

    async def dispatch(self, cmd: str, rest: str, text: str) -> None:
        if cmd == "/session":
            if not await self.start_session():
                return
        elif cmd == "/end":
            await self.send({"type": "session_end"})
        elif cmd == "/cd":
            await self.send({"type": "cd", "path": rest or "~"})
        elif cmd == "/mode":
            await self.send({"type": "mode", "value": rest.strip() or "?"})
        elif cmd == "/model":
            await self.send({"type": "model", "value": rest.strip() or "?"})
        elif cmd == "/planmode":
            await self.send({"type": "planmode", "value": rest.strip() or "?"})
        elif cmd == "/voice" and rest.strip().lower() not in ("", "on", "off"):
            con.print("Uso: /voice on|off", style="red")
        elif cmd == "/voice":
            v = rest.strip().lower()
            await self.send({"type": "voice", **({"enabled": v == "on"} if v else {})})
        elif cmd == "/delete" and rest.strip() and parse_bool(rest) is None:
            con.print(USAGE_DELETE, style="red")
        elif cmd == "/delete":
            await self.send({"type": "nodelete", "value": (parse_bool(rest) or "?") if rest.strip() else "?"})
        elif cmd == "/node":
            await self.send(self.parse_nodo(rest))
        elif cmd == "/plan":
            await self.send({"type": "user", "text": text})
        elif cmd == "/state":
            await self.send({"type": "state"})
        elif cmd == "/resume":
            await self.send({"type": "resume"})
        elif cmd == "/undo":
            await self.send({"type": "undo", "id": rest.strip()})
        elif cmd == "/touch":
            if rest.strip().lower() != "clear":
                con.print("[dim]Uso: /touch clear[/]")
                return
            await self.send({"type": "notouch_clear"})
        elif cmd == "/forget":
            await self.send({"type": "forget"})
        else:
            await self.send({"type": "user", "text": text})
        self.was_cancelled = False
        t0 = asyncio.get_running_loop().time()
        await self.pump()
        if cmd not in ("/state", "/session", "/end", "/cd", "/mode", "/model", "/node", "/planmode", "/delete", "/touch", "/voice"):
            self.beep_done(asyncio.get_running_loop().time() - t0)

    async def run(self):
        await self.connect()
        first = await self.recv()  # saludo
        con.print(Panel(first["text"], border_style="cyan", title=f"{self.cfg['identity']['name']} · v{__version__}"))
        con.print("[dim]/help[/]")
        while True:
            try:
                raw = await self.prompt.prompt_async("tú > ")
            except (EOFError, KeyboardInterrupt):
                break
            quit_now = False
            parts = split_input(raw)
            for line in parts:
                if len(parts) > 1:
                    con.print(f"[dim]› {escape(line)}[/]")
                if await self.handle_line(line):
                    quit_now = True
                    break
            while self.queued and not quit_now and not self.was_cancelled:   # lo que escribiste mientras trabajaba
                nxt = self.queued.pop(0)
                con.print(f"[dim]› (cola) {escape(nxt)}[/]")
                quit_now = await self.handle_line(nxt)
            if self.was_cancelled:
                self.queued.clear()   # cancelaste: no sigo con lo que quedó pendiente
            if quit_now:
                break
        self.w.close()

    async def handle_line(self, text: str) -> bool:
        """Procesa una línea. Devuelve True si hay que salir del cliente."""
        cmd, _, rest = text.partition(" ")
        if cmd.startswith("/") and cmd not in KNOWN:
            con.print(escape(unknown_command(cmd)), style="red")
            return False
        if cmd in ("/quit", "/exit"):
            return True
        if cmd == "/help":
            con.print(HELP)
            return False
        if cmd == "/sound":
            v = rest.strip().lower()
            if v in ("on", "off"):
                self.sound_on = (v == "on")
                con.print(f"[dim]Sonido: {v}[/]")
            else:
                con.print("[dim]Uso: /sound on | off[/]")
            return False
        try:
            await self.dispatch(cmd, rest, text)
        except (ConnectionError, BrokenPipeError):
            self.spin_stop()
            if not await self.reconnect():
                return True
            con.print("[dim]Último mensaje posiblemente sin completar: reenviar[/]")
        return False


def run():
    args = sys.argv[1:]
    if args and args[0] in ("update", "--version"):
        if args[0] == "--version":
            print(f"O.R.B.I.T.A.L v{__version__}")
            return
        from . import update
        sys.exit(update.main(args[1:]))
    try:
        asyncio.run(Client().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
