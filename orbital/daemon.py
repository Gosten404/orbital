"""orbitald: proceso de fondo. Escucha en un socket Unix (JSON por línea).

Cliente -> daemon:
  {"type": "user", "text": "..."}
  {"type": "session", "level": 0-3, "folders": [...], "max_steps": n}   # inicia sesión autónoma
  {"type": "session_end"}
  {"type": "cd", "path": "..."}
  {"type": "state"}
  {"type": "reply", "id": n, "value": ...}                             # respuesta a confirm/relay/choose
  {"type": "cancel"}                                                   # Ctrl+C: corta la tarea en curso
Daemon -> cliente:
  {"type": "say"|"status"|"info", "text": ...}
  {"type": "confirm"|"relay", "id": n, "text": ...}
  {"type": "done"}
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import os
import signal
from pathlib import Path

from . import config
from .core import Brain, Session
from .memory import last_memory, last_summary
from .redact import clean, register_env_secrets
from .permissions import LEVELS, Policy
from .nodes import GENERAL as _GEN


class Logger:
    def __init__(self, cfg: dict):
        self.dir = config.data_dir(cfg) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, ev: dict) -> None:
        ev = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **ev}
        with (self.dir / f"{dt.date.today()}.jsonl").open("a") as f:
            f.write(clean(json.dumps(ev, ensure_ascii=False)) + "\n")


class SocketIO:
    """Un lector en segundo plano separa los mensajes: 'cancel' interrumpe la tarea en curso
    (aunque el modelo esté a media respuesta); lo demás va a una cola."""
    _ids = itertools.count(1)

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.r, self.w = reader, writer
        self.queue: asyncio.Queue = asyncio.Queue()
        self.current: asyncio.Task | None = None
        self.on_live = None   # atiende {"type": "live"} al instante, aunque haya una tarea corriendo
        self.reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        while True:
            line = await self.r.readline()
            if not line:
                if self.current and not self.current.done():
                    self.current.cancel()
                await self.queue.put(None)
                return
            msg = json.loads(line)
            if msg.get("type") == "cancel":
                if self.current and not self.current.done():
                    self.current.cancel()
                continue
            if msg.get("type") == "live":
                if self.on_live:
                    try:
                        await self.on_live(msg)
                    except Exception as e:  # noqa: BLE001 - un comando en vivo nunca tumba la conexión
                        await self.send({"type": "status", "text": f"[x] Comando en vivo fallido: {e}"})
                continue
            await self.queue.put(msg)

    async def send(self, msg: dict) -> None:
        self.w.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.w.drain()

    async def recv(self) -> dict | None:
        return await self.queue.get()

    async def _ask(self, kind: str, text: str, **extra):
        mid = next(self._ids)
        await self.send({"type": kind, "id": mid, "text": text, **extra})
        stash: list[dict] = []
        try:
            while True:
                msg = await self.recv()
                if msg is None:
                    raise ConnectionError("cliente desconectado")
                if msg.get("type") == "reply" and msg.get("id") == mid:
                    return msg.get("value")
                stash.append(msg)   # otro mensaje llegó mientras esperaba tu respuesta: no se pierde
        finally:
            for m in stash:
                self.queue.put_nowait(m)

    async def say(self, text: str, source: str = "orbital") -> None:
        await self.send({"type": "say", "text": text, "source": source})

    async def status(self, text: str) -> None:
        await self.send({"type": "status", "text": text})

    async def confirm(self, description: str) -> bool:
        return bool(await self._ask("confirm", description))

    async def relay(self, prompt: str) -> str:
        return str(await self._ask("relay", prompt) or "")

    async def choose(self, question: str, options: list[str], multi: bool = True) -> list[int]:
        value = await self._ask("choose", question, options=options, multi=multi)
        return [int(x) for x in (value or [])]


class Daemon:
    def __init__(self):
        register_env_secrets()
        self.cfg = config.load()
        self.log = Logger(self.cfg)
        self.data = config.data_dir(self.cfg)
        self.brain = Brain(self.cfg, self.log, self.data)
        sec = self.cfg["security"]
        self.secret_paths = [Path(p) for p in sec["secret_paths"]]
        self.secret_globs = sec["secret_globs"]
        self.session = Session(policy=self.new_policy())
        self.session.node = self.brain.nodes.get("general") or self.session.node
        self.session.plan_mode = self.cfg["agent"].get("plan_mode", "auto")
        self.busy = asyncio.Lock()

    def new_policy(self, **kw) -> Policy:
        code_root = Path(__file__).resolve().parent.parent  # carpeta del proyecto (incluye .venv)
        return Policy(secret_paths=self.secret_paths, secret_globs=self.secret_globs, protected=[code_root], **kw)

    async def client(self, reader, writer) -> None:
        io = SocketIO(reader, writer)
        io.on_live = lambda m: self.live(m, io)
        last = last_summary(self.data)
        await io.send({"type": "info", "text": f"{self.cfg['identity']['name']} · {self.session.policy.describe()} · cwd {self.session.cwd}"
                       + (f"\n{last}" if last else "")})
        try:
            while (msg := await io.recv()) is not None:
                async with self.busy:
                    io.current = asyncio.create_task(self.dispatch(msg, io))
                    try:
                        await io.current
                    except asyncio.CancelledError:
                        if io.reader_task.done():  # se fue el cliente
                            break
                        self.log({"ev": "cancelled"})
                        await io.send({"type": "status", "text": "[x] Tarea cancelada"})
                    finally:
                        io.current = None
                await io.send({"type": "done"})
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            io.reader_task.cancel()
            writer.close()

    @staticmethod
    def delete_flag(s: Session) -> str:
        return "false" if s.no_delete else "true"

    async def live(self, msg: dict, io: SocketIO) -> None:
        """Comandos que valen MIENTRAS corre una tarea: delete, touch, state y note."""
        s, cmd, val = self.session, msg.get("cmd"), str(msg.get("value") or "").strip()
        if cmd == "nodelete":
            if val.lower() in ("on", "off"):
                s.no_delete = s.policy.no_delete = (val.lower() == "on")
                self.log({"ev": "no_delete_" + val.lower(), "live": True})
            await io.send({"type": "status", "text": f"[+] delete={self.delete_flag(s)}"})
        elif cmd == "notouch_clear":
            s.no_touch.clear(); s.policy.no_touch = []
            await io.send({"type": "status", "text": "[+] veto vacío"})
        elif cmd == "state":
            proj = s.node.get("recursos") or (str(s.task_project) if s.task_project else "-")
            await io.send({"type": "status", "text": (
                f"[i] {s.policy.describe()} · cwd {s.cwd} · delete={self.delete_flag(s)} · "
                f"veto: {', '.join(p.name for p in s.no_touch) or '-'} · proyecto: {proj}")})
        elif cmd == "nota" and val:
            for line in self.brain.live_note(val, s):
                await io.send({"type": "status", "text": "[+] " + line})
        else:
            await io.send({"type": "status", "text": f"[x] Comando en vivo desconocido: {cmd}"})
        self.log({"ev": "live", "cmd": cmd, "value": val[:200]})

    def sandbox_state(self) -> str:
        from . import sandbox
        mode = self.cfg["agent"].get("sandbox", "auto")
        if mode == "off":
            return "off"
        if sandbox.available():
            return "bubblewrap" + ("" if self.cfg["agent"].get("sandbox_network", True) else " · sin red")
        return "bubblewrap no instalado (sudo apt install bubblewrap)" + (" · run bloqueado" if mode == "required" else "")

    async def handle_node(self, msg: dict, s: Session, io: SocketIO) -> None:
        store = self.brain.nodes
        action = msg.get("action", "list")
        if action == "list":
            lines = ["Nodos:"]
            for n in store.list():
                marca = " *" if n["id"] == s.node["id"] else ""
                carp = (f" [{n['carpeta']}]" if n["carpeta"] else "") + (f" recursos {n['recursos']}" if n.get("recursos") else "")
                lines.append(f"- {n['nombre']} ({n['tipo']}, {store.memory_count(n['id'])} recuerdos){carp}{marca}")
            lines.append("Uso: /node <name> · /node new <name> [topic|project] [folder] · /node folder <path> · "
                         "/node forget · /node delete <name> · @name en un mensaje cita otro nodo")
            await io.send({"type": "info", "text": "\n".join(lines)})
        elif action == "new":
            nombre = msg.get("nombre", "").strip()
            if not nombre:
                await io.send({"type": "info", "text": "Uso: /node new <name> [topic|project] [folder]"})
                return
            if store.get(nombre):
                await io.send({"type": "info", "text": f"Nodo «{nombre}» ya existe."})
                return
            node = store.create(nombre, msg.get("tipo", "proyecto"), msg.get("carpeta") or None)
            s.node = node
            extra = ""
            if node.get("carpeta"):
                cp = Path(node["carpeta"])
                if cp.is_dir():
                    s.cwd = cp
                    extra = f" · cwd {node['carpeta']}"
                else:
                    extra = f" · [!] carpeta inexistente: {node['carpeta']}"
            await io.send({"type": "info", "text": f"Nodo «{node['nombre']}» creado y activo{extra}"})
        elif action == "switch":
            node = store.get(msg.get("nombre", ""))
            if not node:
                await io.send({"type": "info", "text": f"Nodo «{msg.get('nombre','')}» no existe (/node para listar)"})
                return
            s.node = node
            extra = ""
            if node.get("carpeta") and Path(node["carpeta"]).is_dir():
                s.cwd = Path(node["carpeta"]); extra = f" · cwd {node['carpeta']}"
            await io.send({"type": "info", "text": f"Nodo activo: «{node['nombre']}» ({store.memory_count(node['id'])} recuerdos){extra}"})
        elif action == "folder":
            store.set_folder(s.node["nombre"], msg.get("carpeta") or None)
            s.node = store.get(s.node["nombre"])
            if s.node.get("carpeta"):
                s.cwd = Path(s.node["carpeta"])
            await io.send({"type": "info", "text": f"Carpeta del nodo «{s.node['nombre']}»: {s.node.get('carpeta') or '-'}"})
        elif action == "forget":
            store.forget(s.node["id"])
            await io.send({"type": "info", "text": f"Memoria del nodo «{s.node['nombre']}» eliminada"})
        elif action == "delete":
            nombre = msg.get("nombre", "")
            if store.delete(nombre):
                s.node = store.get(_GEN)
                await io.send({"type": "info", "text": f"Nodo «{nombre}» eliminado · activo: general"})
            else:
                await io.send({"type": "info", "text": f"Nodo «{nombre}» no se puede eliminar (inexistente o general)"})

    async def dispatch(self, msg: dict, io: SocketIO) -> None:
        t = msg.get("type")
        s = self.session
        if t == "user":
            from .busy import busy
            try:
                with busy(msg["text"]):  # el bot de capturas lo detecta y no captura durante la tarea
                    await self.brain.handle(msg["text"], s, io)
            except ConnectionError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log({"ev": "error", "error": repr(e)})
                await io.say(f"[!] Error interno: {e}")
        elif t == "session":
            level = max(0, min(3, int(msg.get("level", 0))))
            folders = [Path(f).expanduser().resolve() for f in msg.get("folders", [])]
            s.policy = self.new_policy(autonomous=True, level=level, folders=folders)
            # El cwd debe estar dentro del área permitida; si no, 'run' se rechaza siempre.
            if folders and not any(s.cwd == f or f in s.cwd.parents for f in folders):
                s.cwd = folders[0]
            if msg.get("max_steps"):
                self.cfg["agent"]["max_steps"] = int(msg["max_steps"])
            self.log({"ev": "session_start", "level": level, "folders": [str(f) for f in folders]})
            proj = self.brain.start_project(s, folders or None)   # cada sesión crea su proyecto
            await io.send({"type": "info", "text": f"{s.policy.describe()}\ncwd {s.cwd}" + (f"\nproyecto {proj}" if proj else "")})
        elif t == "session_end":
            s.policy = self.new_policy()
            if s.node.get("recursos"):
                s.node = self.brain.nodes.get(_GEN) or s.node   # el proyecto queda guardado; /node <name> lo reabre
            self.log({"ev": "session_end"})
            await io.send({"type": "info", "text": s.policy.describe()})
        elif t == "cd":
            p = Path(msg.get("path", "~")).expanduser().resolve()
            if p.is_dir():
                s.cwd = p
                await io.send({"type": "info", "text": f"cwd {p}"})
            else:
                await io.send({"type": "info", "text": f"Carpeta inexistente: {p}"})
        elif t == "state":
            m = self.cfg["models"]
            gem = (f"{m['gemini_model']} (key: {self.brain.gemini.key_source})" if self.brain.gemini.available else "sin API key")
            await io.send({"type": "info", "text": (
                f"{s.policy.describe()}\n"
                f"cwd {s.cwd} · modo {s.mode} · plan {getattr(s, 'plan_mode', 'auto')}\n"
                f"delete={self.delete_flag(s)} · veto: {', '.join(p.name for p in s.no_touch) or '-'}\n"
                f"nodo «{s.node['nombre']}» ({s.node['tipo']}" + (f", carpeta {s.node['carpeta']}" if s.node.get('carpeta') else "") + ")"
                f" · proyecto {s.node.get('recursos') or '-'}\n"
                f"sandbox {self.sandbox_state()}\n"
                f"router {m['router']} · locales {', '.join(x['model'] for x in self.brain.tiers())} (forzado: {s.model_force}) · gemini {gem}\n"
                f"niveles: " + " | ".join(f"{k}={v}" for k, v in LEVELS.items())
            )})
        elif t == "undo":
            s.policy.no_touch = []  # undo devuelve archivos a su sitio aunque ese sitio esté vetado
            out = await self.brain.use_tool("undo", {"id": msg.get("id") or ""}, s, io, "undo", [], {})
            s.policy.no_touch = list(s.no_touch)
            await io.send({"type": "info", "text": out})
        elif t == "notouch_clear":
            s.no_touch.clear(); s.policy.no_touch = []
            await io.send({"type": "info", "text": "Veto vacío"})
        elif t == "forget":
            s.history.clear()
            await io.send({"type": "info", "text": "Historial de conversación eliminado"})
        elif t == "resume":
            text = last_memory(self.data)
            if not text:
                await io.send({"type": "info", "text": "Sin memoria de tareas anteriores"})
            else:
                s.remember("user", "Contexto: bitácora de la tarea anterior. Continuar donde quedó, sin repetir lo que ya funcionó:\n\n" + text[-6000:])
                s.remember("assistant", "Bitácora de la tarea anterior cargada. ¿Continuar con ella?")
                await io.send({"type": "info", "text": "Última tarea cargada en el contexto:\n" + last_summary(self.data)
                               + "\nEscribir «continúa» o nuevas instrucciones."})
        elif t == "mode":
            v = msg.get("value", "auto")
            if v in ("auto", "local", "gemini", "human"):
                s.mode = v
                await io.send({"type": "info", "text": f"Modo: {v}" + (" (el router decide)" if v == "auto" else f" (todo va a {v} hasta /mode auto)")})
            else:
                await io.send({"type": "info", "text": "Uso: /mode auto | local | gemini | human"})
        elif t == "model":
            v = (msg.get("value") or "").strip()
            names = [x["model"] for x in self.brain.tiers()]
            short = {n.split(":")[-1]: n for n in names}  # "4b" -> "qwen3.5:4b"
            v = short.get(v, v)
            if v == "auto" or v in names:
                s.model_force = v
                note = "según complejidad y RAM libre" if v == "auto" else "forzado: si no cabe en RAM la PC pasa a swap; /model auto lo revierte"
                await io.send({"type": "info", "text": f"Modelo local: {v} ({note})"})
            else:
                await io.send({"type": "info", "text": "Uso: /model auto | " + " | ".join(short)})
        elif t == "nodelete":
            v = (msg.get("value") or "").strip().lower()
            if v in ("on", "off"):
                s.no_delete = s.policy.no_delete = (v == "on")
                self.log({"ev": "no_delete_" + v})
            await io.send({"type": "info", "text": f"delete={self.delete_flag(s)}" + (
                " (borrar prohibido; move y classify siguen permitidos)" if s.no_delete else
                " (borrar pide confirmación en sesión autónoma)")})
        elif t == "node":
            await self.handle_node(msg, s, io)
        elif t == "planmode":
            v = (msg.get("value") or "").strip()
            if v in ("auto", "always", "off"):
                s.plan_mode = v
                txt = {"auto": "plan solo al reorganizar", "always": "plan en toda tarea con herramientas",
                       "off": "sin plan (/plan <task> lo pide puntualmente)"}[v]
                await io.send({"type": "info", "text": f"Plan: {v} ({txt})"})
            else:
                await io.send({"type": "info", "text": "Uso: /planmode auto | always | off"})
        elif t == "hello":
            await io.send({"type": "info", "text": self.cfg["identity"]["name"]})


async def main() -> None:
    d = Daemon()
    sock = config.socket_path(d.cfg)
    if sock.exists():
        sock.unlink()
    server = await asyncio.start_unix_server(d.client, path=str(sock))
    os.chmod(sock, 0o600)  # solo tu usuario puede hablarle
    d.log({"ev": "daemon_start", "socket": str(sock)})
    print(f"orbitald · socket {sock}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()
    sock.unlink(missing_ok=True)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
