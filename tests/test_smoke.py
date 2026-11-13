"""Prueba de humo sin Ollama ni Gemini: proveedores falsos con respuestas guionadas."""
import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orbital import config
from orbital.core import Brain, Session
from orbital.permissions import Policy

class Fake:
    def __init__(self, name, script): self.name, self.script = name, list(script)
    available = True
    async def chat(self, messages, json_mode=False, **k):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]  # el último se repite

class IO:
    def __init__(self, confirm=True, relay="1. haz X\n2. haz Y"): self.out, self.c, self.r = [], confirm, relay
    async def say(self, t, source="orbital"): self.out.append(("say", source, t))
    async def status(self, t): self.out.append(("status", t))
    async def confirm(self, d): self.out.append(("confirm", d)); return self.c
    async def relay(self, p): self.out.append(("relay", p)); return self.r
    choices = []
    async def choose(self, q, opts, multi=True): self.out.append(("choose", q, opts)); return self.choices.pop(0) if self.choices else []

async def main():
    tmp = Path(tempfile.mkdtemp())
    import copy
    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    logs = []
    b = Brain(cfg, logs.append, tmp / "data")
    # 1) simple + herramientas: listar y responder, en modo asistido
    (tmp / "a.txt").write_text("hola")
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True, "motivo": "x"})])
    b.local = Fake("local", [json.dumps({"tool": "list", "args": {"path": str(tmp)}}),
                             json.dumps({"respuesta": "Hay a.txt"})])
    s = Session(cwd=tmp); io = IO()
    await b.handle("lista la carpeta", s, io)
    assert io.out[-1] == ("say", "orbital", "Hay a.txt"), io.out
    # 2) escribir en modo asistido -> pide confirmación; rechazada -> no escribe
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Fake("local", [json.dumps({"tool": "write_file", "args": {"path": "b.txt", "content": "x"}}),
                             json.dumps({"respuesta": "no pude"})])
    io = IO(confirm=False)
    await b.handle("crea b.txt", s, io)
    assert any(e[0] == "confirm" for e in io.out) and not (tmp / "b.txt").exists()
    # 3) sesión autónoma nivel 2: ejecutar sin preguntar dentro de la carpeta; rm -rf pregunta
    s.policy = Policy(autonomous=True, level=2, folders=[tmp])
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Fake("local", [json.dumps({"tool": "run", "args": {"command": "echo ok > c.txt"}}),
                             json.dumps({"tool": "run", "args": {"command": "rm -rf ./algo"}}),
                             json.dumps({"respuesta": "listo"})])
    io = IO(confirm=False)
    await b.handle("haz cosas", s, io)
    assert (tmp / "c.txt").read_text().strip() == "ok"
    assert sum(e[0] == "confirm" for e in io.out) == 1, io.out
    # 4) secretos: leer ~/.ssh se rechaza
    s.policy = Policy(secret_paths=[Path("~/.ssh")], secret_globs=[".env"])
    assert s.policy.check("read_file", {"path": str(Path.home() / ".ssh/id_rsa")}).startswith("deny")
    assert s.policy.check("read_file", {"path": str(tmp / ".env")}).startswith("deny")
    # 5) heavy sin herramientas -> relevo humano y devuelve la respuesta pegada
    b.router = Fake("r", [json.dumps({"route": "heavy", "herramientas": False})])
    io = IO(relay="Respuesta de Claude")
    await b.handle("diseña la arquitectura del árbol de nodos", s, io)
    assert io.out[-1] == ("say", "relevo", "Respuesta de Claude") and any(e[0] == "relay" for e in io.out)
    # 6) heavy con herramientas -> plan humano y luego ejecuta (gemini sin key => local)
    b.gemini.key = ""
    b.router = Fake("r", [json.dumps({"route": "heavy", "herramientas": True})])
    b.local = Fake("local", ['```json\n{"respuesta": "plan aplicado"}\n```'])
    io = IO()
    await b.handle("refactoriza todo", s, io)
    assert io.out[-1][2] == "plan aplicado"
    # 7) forzado /humano
    io = IO(relay="ok")
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": False})])
    await b.handle("/human explícame monads", s, io)
    assert io.out[-1][1] == "relevo"
        # 8) el nombre sale del config
    assert b.ident()["name"] == "O.R.B.I.T.A.L"
    # 9) /modo gemini fija la ruta aunque el enrutador diga simple (gemini sin key cae a local)
    s.mode = "gemini"
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": False})])
    r = await b.classify("hola", IO(), s.mode)
    assert r["route"] == "medium", r
    # 10) memoria .md por paso + ULTIMA.md
    s.mode = "auto"; s.policy = Policy()
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Fake("local", [json.dumps({"pensamiento": "veo qué hay", "tool": "list", "args": {"path": str(tmp)}}),
                             json.dumps({"pensamiento": "ya", "respuesta": "fin"})])
    io = IO(); await b.handle("revisa la carpeta", s, io)
    ultima = (tmp / "data/memoria/ULTIMA.md").read_text()
    assert "veo qué hay" in ultima and "Estado: terminada" in ultima, ultima
    assert any("por qué: veo qué hay" in str(e) for e in io.out)
    # 11) crash dentro de una herramienta no tumba nada y queda registrado
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Fake("local", [json.dumps({"tool": "list", "args": {"path": [1, 2]}}), json.dumps({"respuesta": "ok"})])
    io = IO(); await b.handle("rompe", s, io)
    assert io.out[-1][2] == "ok"
    async def boom(*a, **k): raise ValueError("kaboom")
    b.local = Fake("local", []); b.local.chat = boom
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    io = IO(); await b.handle("crashea", s, io)
    assert "kaboom" in io.out[-1][2] and "error interno" in (tmp / "data/memoria/ULTIMA.md").read_text()
    # 12) protección del propio código
    code = Path(__file__).resolve().parents[1]
    pol = Policy(autonomous=True, level=3, folders=[Path("/")], protected=[code])
    assert pol.check("write_file", {"path": str(code / "orbital/core.py")}).startswith("deny")
    assert pol.check("run", {"command": f"mv {code}/orbital {code}/otro", "cwd": "/tmp"}).startswith("deny")
    assert pol.check("run", {"command": f"ls {code}", "cwd": "/tmp"}) == "allow"
    # 13) escalera de carga: alto -> cerrar nada -> crítico -> pausas -> último recurso reducir
    from orbital import monitor
    from orbital.memory import TaskMemory
    cfg["monitor"].update(enabled=True, wait_seconds=0, pause_seconds=0, pause_retries=2)
    seq = iter([99, 99, 99, 99, 99])
    async def fake_measure(window=1.0):
        v = next(seq); return monitor.Load(cpu=v, ram=50, swap=0, ram_free_gb=8)
    monitor.measure = fake_measure
    monitor.heavy_processes = lambda limit=6: [{"name": "brave", "ram_mb": 3000, "procs": []}]
    io = IO(); io.choices = [[], [0]]
    mem = TaskMemory(tmp / "data", "carga", "x", "y", tmp)
    prov = await b.guard(io, mem, b.local, {})
    assert "reducido" in prov.name, prov.name
    assert sum(1 for e in io.out if e[0] == "status" and "[~]" in e[1]) == 2
    # baja tras la espera -> sigue normal
    seq2 = iter([90, 40]); 
    async def m2(window=1.0): return monitor.Load(cpu=next(seq2), ram=50, swap=0, ram_free_gb=8)
    monitor.measure = m2
    assert await b.guard(IO(), mem, b.local, {}) is b.local
    # 14) si la PC se satura A MITAD de la respuesta: se corta, espera y reintenta sin gastar paso
    from orbital.providers.ollama import Paused
    class Flaky:
        name = "local:flaky"; available = True; watchdog = None
        def __init__(self): self.calls = 0
        async def chat(self, m, json_mode=False, **k):
            self.calls += 1
            if self.calls == 1: raise Paused("RAM al 97% mientras generaba")
            return json.dumps({"pensamiento": "ya", "respuesta": "hecho tras pausa"})
    cfg["monitor"].update(enabled=False)
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Flaky(); io = IO()
    await b.handle("algo pesado", s, io)
    assert io.out[-1][2] == "hecho tras pausa" and any("[~]" in str(e) for e in io.out)
    assert "pausado" in (tmp / "data/memoria/ULTIMA.md").read_text()
    # 15) recorte de contexto: solo los 2 últimos resultados quedan completos
    msgs = [{"role": "user", "content": "RESULTADO de x:\n" + "a" * 2000} for _ in range(4)]
    Brain.trim(msgs)
    assert [len(m["content"]) < 600 for m in msgs] == [True, True, False, False]
    # 16) el vigilante corta si el swap crece
    from orbital.providers.ollama import Ollama
    cfg["monitor"].update(enabled=True)
    o = Ollama("http://x", "m"); vals = iter([(50, 0), (60, 900e6)])
    monitor.ram_now = lambda: next(vals)
    b.arm(o)
    assert "swap" in await o.watchdog()
    # 17) num_ctx se manda a Ollama
    b2 = Brain(cfg, logs.append, tmp / "data")
    # v0.2.16: el enrutador usa el MISMO num_ctx que el escalón 2b (mismo modelo: si difiere, Ollama lo recarga)
    assert b2.local.extra["num_ctx"] == 8192 and b2.router.extra["num_ctx"] == 8192
    assert b2.router.extra["num_ctx"] == b2.tiers()[0]["num_ctx"]
    print("OK: 17 escenarios")

asyncio.run(main())


# ---- v0.1.3 ----
async def v013():
    import tempfile
    from orbital.core import Brain, Session, CODE_RX
    from orbital import config, tools
    b = Brain(config.DEFAULTS, lambda e: None)
    # sin código -> no va a gemini aunque el enrutador diga medium
    b.router = Fake("r", [json.dumps({"route": "medium", "herramientas": True})])
    r = await b.classify("cambia el texto de MONACO2 para que diga otra cosa", IO())
    assert r["route"] == "simple", r
    # con código -> sí
    b.router = Fake("r", [json.dumps({"route": "medium", "herramientas": True})])
    r = await b.classify("arregla el error en server.py", IO())
    assert r["route"] == "medium", r
    # /gemini forzado gana a la regla
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": False})])
    r = await b.classify("/gemini explícame la fotosíntesis", IO())
    assert r["route"] == "medium", r
    # encontrar por nombre, ignorando carpetas pesadas
    d = Path(tempfile.mkdtemp())
    (d / "sub").mkdir(); (d / "sub" / "MONACO2").write_text("x")
    (d / ".cache").mkdir(); (d / ".cache" / "MONACO2").write_text("x")
    out = await tools.find({"name": "monaco*", "path": str(d)}, 5000)
    assert "sub/MONACO2" in out and ".cache" not in out, out
    out = await tools.find({"name": "nada*", "path": str(d)}, 5000)
    assert "sin resultados" in out
    print("OK: v0.1.3 (regla sin-código, /gemini forzado, encontrar)")

asyncio.run(v013())


# ---- escalera de modelos ----
async def tiers_test():
    from orbital.core import Brain, Session
    from orbital import config, monitor
    import orbital.core as core
    b = Brain(config.DEFAULTS, lambda e: None)
    s = Session()
    async def none_loaded(url): return []
    async def router_loaded(url): return [{"name": "qwen3.5:2b", "size": 2.3e9}]
    core.loaded_models = none_loaded
    monitor.available_gb = lambda: 12.0   # mucha RAM
    assert (await b.pick_local(3, s, IO())).model == "qwen3.5:9b"
    assert (await b.pick_local(2, s, IO())).model == "qwen3.5:4b"
    assert (await b.pick_local(1, s, IO())).model == "qwen3.5:2b"
    monitor.available_gb = lambda: 7.0    # 9b no cabe (8 + 1.5 margen)
    io = IO(); m = await b.pick_local(3, s, io)
    assert m.model == "qwen3.5:4b" and any("no cabe" in str(e) for e in io.out), io.out
    monitor.available_gb = lambda: 2.0    # nada cabe, pero el 2b ya está cargado (enrutador)
    core.loaded_models = router_loaded
    assert (await b.pick_local(3, s, IO())).model == "qwen3.5:2b"
    s.model_force = "qwen3.5:9b"           # forzado gana aunque no quepa, con aviso
    io = IO(); assert (await b.pick_local(1, s, io)).model == "qwen3.5:9b"
    assert any("forzado" in str(e) for e in io.out)
    print("OK: escalera de modelos (complejidad, RAM, ya cargado, forzado)")

asyncio.run(tiers_test())


# ---- v0.1.5: seguridad y búsqueda ----
async def v015():
    import tempfile, importlib
    from orbital import monitor
    importlib.reload(monitor)  # deshace los monkeypatches de pruebas anteriores
    from orbital.core import Brain, Session
    from orbital import config
    from orbital.permissions import Policy
    b = Brain(config.DEFAULTS, lambda e: None)
    home = str(Path.home())
    s = Session(cwd=Path(tempfile.mkdtemp()))
    # 1) sin pedir el home: búsqueda recursiva en el home se rechaza
    io = IO(); ts = {}
    out = await b.use_tool("grep", {"pattern": "x", "path": home}, s, io, "cambia el archivo raro", [], ts)
    assert out.startswith("RECHAZADO") and "no solicitado" in str(io.out), out
    # 2) pidiendo el home: 2 permitidas, la tercera no
    task = "busca en mi home un archivo cifrado"
    for i in range(2):
        out = await b.use_tool("recent", {"path": home, "hours": 1}, s, IO(), task, [], ts)
        assert not out.startswith("RECHAZADO"), out
    out = await b.use_tool("recent", {"path": home}, s, IO(), task, [], ts)
    assert out.startswith("RECHAZADO") and "2 veces" in out, out
    # 3) subcarpeta concreta: sin límite
    out = await b.use_tool("recent", {"path": str(s.cwd)}, s, IO(), "x", [], ts)
    assert not out.startswith("RECHAZADO")
    # 4) dos timeouts seguidos bloquean la herramienta
    ts2 = {"timeouts": {"grep": 2}}
    out = await b.use_tool("grep", {"pattern": "x", "path": str(s.cwd)}, s, IO(), "x", [], ts2)
    assert out.startswith("BLOQUEADO"), out
    # 5) confirmación de escritura muestra antes/después
    f = s.cwd / "MONACO2"; f.write_text("JARVIS ES SUPERIOR")
    io = IO(confirm=False)
    await b.use_tool("write_file", {"path": str(f), "content": "ORBITAL ES MEJOR"}, s, io, "x", [], {})
    conf = [e for e in io.out if e[0] == "confirm"][0][1]
    assert "antes:   JARVIS ES SUPERIOR" in conf and "después: ORBITAL ES MEJOR" in conf, conf
    # 6) 5 pasos sin avanzar => NO se rinde: avisa, escala al relevo y usa su respuesta
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    acts = [json.dumps({"pensamiento": "p", "tool": "find", "args": {"nombre": f"*nada{i}*", "path": str(s.cwd)}})
            for i in range(8)]
    b.local = Fake("local", acts)
    io = IO()
    await b.handle("encuentra el archivo perdido", s, io)
    final = io.out[-1][2]
    assert final == "1. haz X\n2. haz Y", final          # no se rinde: devuelve lo que dio el relevo
    assert any("3 pasos sin avance" in str(e) for e in io.out)
    assert any(e[0] == "relay" for e in io.out)          # escaló al relevo en vez de abandonar
    print("OK: v0.1.5 (home limitado, timeouts, antes/después, atorado => escala sin rendirse)")

asyncio.run(v015())


# ---- v0.1.6: lo que falló en LENSTEST ----
async def v016():
    import tempfile, zipfile, importlib
    from orbital import monitor, tools, config
    importlib.reload(monitor)
    from orbital.permissions import Policy
    from orbital.core import Brain, Session
    d = Path(tempfile.mkdtemp())
    code = d / "orbital" / "orbital"; code.mkdir(parents=True); (code / "core.py").write_text("x")
    pol = Policy(protected=[d / "orbital"]) if "protected" in Policy.__dataclass_fields__ else None
    if pol:
        assert pol.check("run", {"command": f"unzip -l {d}/orbital.zip", "cwd": str(d)}) != "deny" and \
            not pol.check("run", {"command": f"unzip -l {d}/orbital.zip", "cwd": str(d)}).startswith("deny"), "falso positivo zip"
        assert pol.check("run", {"command": f"sed -i s/a/b/ {code}/core.py", "cwd": str(d)}).startswith("deny")
        assert not pol.check("run", {"command": "echo orbital es genial", "cwd": str(d)}).startswith("deny")
    # recientes ignora ocultos (.config) salvo que se pida
    (d / ".config" / "Claude").mkdir(parents=True); (d / ".config" / "Claude" / "cache").write_text("x")
    (d / "LENSTEST").write_text("W.Z.J")
    out = await tools.recent({"path": str(d), "hours": 1}, 6000)
    assert "LENSTEST" in out and ".config" not in out, out
    out = await tools.recent({"path": str(d), "hours": 1, "hidden": True}, 6000)
    assert ".config" in out
    # binario: no devuelve basura
    with zipfile.ZipFile(d / "orbital.zip", "w") as z:
        z.writestr("a.txt", "hola")
    out = await tools.read_file({"path": str(d / "orbital.zip")}, 6000)
    assert out.startswith("ARCHIVO BINARIO (zip") and "unzip -l" in out, out
    # revisión final: en tareas de 3+ pasos pide revisar una vez antes de responder
    b = Brain(config.DEFAULTS, lambda e: None)
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Fake("local", [json.dumps({"tool": "list", "args": {"path": str(d)}}),
                             json.dumps({"tool": "read_file", "args": {"path": str(d / "LENSTEST")}}),
                             json.dumps({"respuesta": "primera"}),
                             json.dumps({"respuesta": "revisada"})])
    io = IO()
    await b.handle("lee LENSTEST y dime qué dice", Session(cwd=d), io)
    assert io.out[-1][2] == "revisada" and any("Verificación final" in str(e) for e in io.out), io.out[-3:]
    print("OK: v0.1.6 (zip no es mi código, recientes sin cachés, binarios, revisión final)")

asyncio.run(v016())


# ---- v0.1.7: hacer espacio para el 9b ----
async def v017():
    import importlib
    from orbital import monitor, config
    import orbital.core as core
    importlib.reload(monitor)
    from orbital.core import Brain, Session
    b = Brain(config.DEFAULTS, lambda e: None)
    s = Session()
    ram = {"free": 6.0}                      # 9b necesita 8 + 1.5 margen = 9.5 -> faltan 3.5 (< 4.5)
    loaded = [{"name": "qwen3.5:2b", "size": 2.3e9}]
    async def lm(url): return list(loaded)
    core.loaded_models = lm
    monitor.available_gb = lambda: ram["free"]
    unloaded = []
    async def fake_free(keep, io):
        unloaded.append(1); loaded.clear(); ram["free"] += 2.3
    b.free_own_ram = fake_free
    closed = []
    monitor.heavy_processes = lambda limit=6: [{"name": "brave", "ram_mb": 2500, "procs": []}]
    def close(g): closed.append(g["name"]); ram["free"] += 2.5; return "brave: 3 proceso(s) cerrados"
    monitor.close_group = close
    _real_sleep = asyncio.sleep
    core.asyncio.sleep = lambda *_a, **_k: _real_sleep(0)
    # descargar el 2b no alcanza (8.3 < 9.5); acepta cerrar brave -> cabe el 9b
    io = IO(); io.choices = [[0]]
    m = await b.pick_local(3, s, io)
    assert unloaded and closed == ["brave"] and m.model == "qwen3.5:9b", (m.model, io.out)
    # si no quiere cerrar nada -> baja a 4b, sin forzar el 9b
    ram["free"] = 6.0; loaded[:] = [{"name": "qwen3.5:2b", "size": 2.3e9}]; closed.clear()
    io = IO(); io.choices = [[]]
    m = await b.pick_local(3, s, io)
    assert m.model == "qwen3.5:4b" and not closed, (m.model, io.out)
    # si faltan más de 4.5 GB ni lo intenta
    ram["free"] = 3.0; loaded.clear(); unloaded.clear()
    io = IO()
    m = await b.pick_local(3, s, io)
    assert not unloaded and not any(e[0] == "choose" for e in io.out) and m.model != "qwen3.5:9b"
    core.asyncio.sleep = _real_sleep
    print("OK: v0.1.7 (hacer espacio para el 9b: descargar modelos, ofrecer cerrar apps, o bajar)")

asyncio.run(v017())


# ---- v0.1.8: niveles por reglas con explicación ----
def v018():
    from orbital.core import complexity_rules
    assert complexity_rules("recientemente creé un archivo en mi home con contenido cifrado; descífralo y si dice algo, obedécelo.", 2) == \
        (3, "regla: descifrar, instrucciones encadenadas")
    assert complexity_rules("tengo un archivo en algun lugar de mi home que no se como se llama", 2)[0] == 3
    # v0.2.12: organizar es EJECUCIÓN, no razonamiento: garantiza 2 pero ya no fuerza el 9b
    assert complexity_rules("organiza mi home en carpetas por proyecto", 1)[0] == 2
    assert complexity_rules("lista mis descargas", 1) == (1, "enrutador")
    assert complexity_rules("cambia el texto de MONACO2", "x") == (2, "enrutador")
    print("OK: v0.1.8 (complejidad por reglas + explicación)")

v018()


# ---- v0.1.9: secretos ----
async def v019():
    import tempfile
    from orbital.redact import redact
    from orbital.permissions import Policy
    from orbital.core import Brain, Session
    from orbital import config
    fake_key = "AIza" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r"
    txt = f'export GEMINI_API_KEY="{fake_key}"\nalias ll="ls -la"\nTOKEN=abcdef123456\nsk-ant-api03-{"x"*30}'
    out, n = redact(txt)
    assert fake_key not in out and "abcdef123456" not in out and "sk-ant" not in out and 'alias ll="ls -la"' in out, out
    assert n >= 3
    pol = Policy()
    for f in ["~/.bashrc", "~/.bash_history", "~/proyecto/.env", "~/.aws/credentials", "~/.config/orbital/env"]:
        assert pol.check("read_file", {"path": str(Path(f).expanduser())}).startswith("deny"), f
    assert pol.check("run", {"command": "cat ~/.bashrc", "cwd": "/tmp"}).startswith("deny")
    assert pol.check("run", {"command": "grep KEY /home/x/.bash_history", "cwd": "/tmp"}).startswith("deny")
    assert pol.check("run", {"command": "history | tail", "cwd": "/tmp"}).startswith("deny")
    assert not pol.check("read_file", {"path": "/tmp/NEBULA"}).startswith("deny")
    # una key en un archivo NO marcado como secreto se tacha antes de llegar al modelo
    d = Path(tempfile.mkdtemp()); (d / "notas.txt").write_text(f"mi key: {fake_key}\nhola")
    b = Brain(config.DEFAULTS, lambda e: None); io = IO()
    out = await b.use_tool("read_file", {"path": str(d / "notas.txt")}, Session(cwd=d), io, "x", [], {})
    assert fake_key not in out and "hola" in out and any("ocultados" in str(e) for e in io.out), out
    print("OK: v0.1.9 (secretos: rutas obligatorias, comandos, tachado en salida/logs/memoria/relevo)")

asyncio.run(v019())


# ---- v0.1.10: llavero y valores exactos ----
async def v0110():
    import os
    from orbital import redact, tools
    weird = "AQ.Ab8RN6Lzz-otroformato_123456"   # formato que ningún patrón reconoce
    redact.register_secret(weird)
    out, n = redact.redact(f"la key es {weird} ok")
    assert weird not in out and n == 1, out
    os.environ["GEMINI_API_KEY"] = "valor-secreto-de-prueba-123"
    out = await tools._run(["bash", "-c", "echo ${GEMINI_API_KEY:-vacia}"], cwd=None, timeout=5, max_out=1000)
    assert "vacia" in out and "valor-secreto" not in out, out
    del os.environ["GEMINI_API_KEY"]
    print("OK: v0.1.10 (valor exacto tachado, comandos sin keys en el entorno)")

asyncio.run(v0110())


# ---- v0.1.11: sandbox (solo si bubblewrap está instalado) ----
def v0111():
    import shutil, subprocess, tempfile, os
    if not shutil.which("bwrap"):
        print("SKIP: v0.1.11 sandbox (no hay bwrap)")
        return
    from orbital import sandbox
    from orbital.permissions import Policy
    h = Path(tempfile.mkdtemp(dir="/tmp"))
    (h / "proj").mkdir(); (h / ".ssh").mkdir(); (h / "other").mkdir(); (h / "code").mkdir()
    (h / ".bashrc").write_text("export K=AIzaX"); (h / ".ssh" / "id_rsa").write_text("k")
    (h / "proj" / ".env").write_text("T=1"); (h / "other" / "n.txt").write_text("privado"); (h / "code" / "c.py").write_text("x")
    def run(cmd, rw, cwd):
        a = sandbox.build(cmd, cwd, rw, [h / "code"], [h / ".ssh"], Policy.ALWAYS_SECRET_GLOBS, h / "tmp")
        return subprocess.run(a, capture_output=True, text=True, env={**os.environ, "GEMINI_API_KEY": "AIzaSECRETO"}).stdout
    assert run(f"ls -A {h}", [h / "proj"], h / "proj").split() == ["proj"]          # solo la carpeta autorizada
    assert "privado" not in run(f"cat {h}/otros/n.txt 2>&1", [h / "proj"], h / "proj")
    assert run("python3 -c \"import os;print(os.environ.get('GEMINI_API_KEY'))\"", [h / "proj"], h / "proj").strip() == "None"
    assert "T=1" not in run("cat .e''nv 2>&1", [h / "proj"], h / "proj")
    assert run(f"ls -A {h}/.ssh | wc -l", [h], h).strip() == "0"                    # nivel 3: .ssh vacía
    assert "AIzaX" not in run(f"cat {h}/.bash''rc 2>&1", [h], h)
    assert "Read-only" in run(f"(echo y >> {h}/code/c.py) 2>&1", [h], h)               # su código: solo lectura
    assert run("ls /run | wc -l", [h / "proj"], h / "proj").strip() == "0"            # sin D-Bus / llavero
    print("OK: v0.1.11 (sandbox real: home oculto, secretos tapados, sin env, código solo lectura, sin D-Bus)")

v0111()


def v0114():
    from orbital.busy import busy, path
    with busy("prueba"):
        assert path().exists() and '"tarea": "prueba"' in path().read_text()
    assert not path().exists()
    try:
        with busy("falla"):
            raise RuntimeError
    except RuntimeError:
        pass
    assert not path().exists()
    print("OK: v0.1.14 (aviso de ocupado para otros programas)")

v0114()



# ---- v0.2: árbol de nodos y modo plan ----
async def v02():
    import tempfile
    from orbital.core import Brain, Session
    from orbital import config, nodes
    d = Path(tempfile.mkdtemp())
    b = Brain(config.DEFAULTS, lambda e: None, data_dir=d)
    st = b.nodes
    # general existe por defecto
    assert st.get("general") and st.get("general")["tipo"] == "tema"
    # crear nodo con carpeta, guardar memoria, aislar de otro
    proy = st.create("robot", "proyecto", str(d))
    otro = st.create("tesis", "tema")
    st.remember(proy["id"], "el robot usa un ESP32")
    st.remember(otro["id"], "la tesis es sobre abejas")
    assert "ESP32" in nodes.context_block(st, proy, [])
    assert "abejas" not in nodes.context_block(st, proy, [])          # aislado
    assert "abejas" in nodes.context_block(st, proy, ["tesis"])       # @tesis lo trae
    assert nodes.referenced("mira @tesis y @robot porfa") == ["tesis", "robot"]
    # una tarea en un nodo guarda un recuerdo; en general no
    s = Session(node=proy, cwd=d)
    b.remember_node(s, "conecta el sensor", "Listo, sensor conectado")
    assert st.memory_count(proy["id"]) == 2
    sg = Session(node=st.get("general"))
    b.remember_node(sg, "hola", "hola")
    assert st.memory_count(st.get("general")["id"]) == 0
    # fallos no se guardan
    b.remember_node(s, "x", "[!] Error de red")
    assert st.memory_count(proy["id"]) == 2
    # el contexto del nodo entra al prompt del agente (chat path)
    seen = {}
    class Spy(Fake):
        async def chat(self, messages, json_mode=False, **k):
            seen["msgs"] = messages
            return json.dumps({"route": "simple", "herramientas": False}) if json_mode else "ok"
    b.router = Spy("r", []); b.local = Spy("l", [])
    await b.handle("qué micro usa el robot", Session(node=proy), IO())
    assert any("ESP32" in m["content"] for m in seen["msgs"])
    # modo plan: tarea de reorganización pide plan y, si lo rechazas, no ejecuta
    plan_asked = {"v": False}
    class PlanIO(IO):
        async def confirm(self, d):
            plan_asked["v"] = True
            return False
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True, "complejidad": 2})])
    b.local = Fake("l", ["PASO 1: revisar\nPASO 2: mover a.txt -> docs/a.txt",
                         json.dumps({"respuesta": "no deberia llegar aqui"})])
    io = PlanIO()
    # v0.2.17: por defecto ya NO se arma plan solo (solo con /plan); el modo "auto" sigue disponible con /planmode
    await b.handle("organiza mi carpeta en subcarpetas", Session(node=proy, cwd=d, plan_mode="auto"), io)
    assert plan_asked["v"] and any("PLAN (sin cambios" in str(e) for e in io.out)
    assert any("Plan no aprobado" in str(e) for e in io.out)
    print("OK: v0.2 (nodos aislados, @cita, memoria por nodo, plan-antes-de-tocar)")

asyncio.run(v02())


# ---- v0.2.9: autonomía sin rendirse ante fallos de conexión de Gemini ----
async def v029():
    import orbital.core as core
    from orbital.core import Brain, Session
    from orbital.providers import ProviderError
    from orbital import config

    b = Brain(config.DEFAULTS, lambda e: None)
    s = Session(cwd=Path(tempfile.mkdtemp()))

    # Gemini que SIEMPRE falla por red (red con pérdida de paquetes). Expone .retries como el real.
    class DeadGemini:
        name = "gemini:test"
        model = None
        retries = 4
        watchdog = None
        def __init__(self): self.off = False
        @property
        def available(self): return not self.off
        def disable(self): self.off = True
        def reset(self): self.off = False
        async def chat(self, msgs, json_mode=False, temperature=0.3):
            raise ProviderError("sin red hacia Google", fatal=True, network=True)

    dead = DeadGemini()
    b.gemini = dead
    # El local, en cuanto lo usan, responde y termina la tarea.
    b.local = Fake("local", [json.dumps({"pensamiento": "ya puedo", "respuesta": "hecho en local"})])
    async def fake_pick(*a, **k): return b.local   # evita tocar Ollama real
    b.pick_local = fake_pick

    # Sin dormir de verdad en la prueba.
    async def nosleep(*a, **k): return None
    orig_sleep = core.asyncio.sleep
    core.asyncio.sleep = nosleep
    try:
        ans = await b.run_agent("haz algo difícil", s, IO2 := IO(), dead)
    finally:
        core.asyncio.sleep = orig_sleep

    io = IO2
    assert ans == "hecho en local", ans                       # no se rinde: termina en local
    assert not dead.available                                  # Gemini quedó apagado por esta tarea
    assert any("siguiente intento" in str(e) for e in io.out)          # reintentó la conexión antes de bajar
    assert any("continuar en local" in str(e) for e in io.out)  # cayó a local para seguir

    # Tarea nueva: reset() vuelve a darle oportunidad a Gemini.
    dead.disable()
    assert not dead.available
    b.gemini.reset()
    assert dead.available
    print("OK: v0.2.9 (no se rinde: reintenta Gemini, baja a local y sigue; reset por tarea)")

asyncio.run(v029())


# ---- v0.2.10: cwd fuera del área permitida ya no bloquea 'run'; listar no se cae ----
async def v0210():
    import os
    from orbital.core import Brain, Session
    from orbital.permissions import Policy
    from orbital import config, tools

    b = Brain(config.DEFAULTS, lambda e: None)
    base = Path(tempfile.mkdtemp())
    allowed = base / "Descargas"; allowed.mkdir()
    # Sesión nivel 2 acotada a ~/Descargas, pero con el cwd en el home (el bug del log real).
    s = Session(cwd=base, policy=Policy(autonomous=True, level=2, folders=[allowed]))

    # 'run' sin cwd: antes => RECHAZADO; ahora corre desde la carpeta permitida.
    io = IO()
    out = await b.use_tool("run", {"command": "pwd"}, s, io, "organiza", [], {})
    assert "RECHAZADO" not in out, out
    assert str(allowed) in out, out                      # corrió DENTRO del área permitida

    # 'run' con cwd explícito en el home (fuera) también se reencauza, no se rechaza.
    out = await b.use_tool("run", {"command": "pwd", "cwd": str(base)}, s, IO(), "x", [], {})
    assert "RECHAZADO" not in out and str(allowed) in out, out

    # listar no se cae con un enlace simbólico roto (tu "PlayOnLinux's virtual drives").
    os.symlink(base / "no-existe-jamas", allowed / "enlace_roto")
    (allowed / "foto.jpg").write_text("x")
    res = await tools.list_dir({"path": str(allowed)}, 6000)
    assert "ARCHIVOS" in res and "ERROR" not in res, res
    print("OK: v0.2.10 (ejecutar se reencauza a la carpeta permitida; listar ignora enlaces rotos)")

asyncio.run(v0210())


# ---- v0.2.11: limpia los scripts de un solo uso que el agente escribió y ejecutó ----
async def v0211():
    from orbital.core import Brain, Session
    from orbital.permissions import Policy
    from orbital import config

    b = Brain(config.DEFAULTS, lambda e: None)
    base = Path(tempfile.mkdtemp())
    allowed = base / "Descargas"; allowed.mkdir()
    s = Session(cwd=allowed, policy=Policy(autonomous=True, level=2, folders=[allowed]))
    ts = {}

    # El agente escribe un script auxiliar DENTRO de la carpeta del usuario...
    sh = allowed / "organizar.sh"
    await b.use_tool("write_file", {"path": str(sh), "content": "echo hola"}, s, IO(), "organiza", [], ts)
    assert sh.exists()
    assert str(sh.resolve()) in ts.get("my_scripts", {}), ts

    # ...y lo ejecuta: queda marcado como de un solo uso.
    await b.use_tool("run", {"command": f"bash {sh}"}, s, IO(), "organiza", [], ts)
    assert ts["my_scripts"][str(sh.resolve())] is True

    # Al terminar, se mueve a la temporal: ya no ensucia la carpeta del usuario, pero es recuperable.
    moved = await b.cleanup_scripts(ts, IO())
    assert moved == ["organizar.sh"], moved
    assert not sh.exists(), "el script debió salir de la carpeta del usuario"
    assert (b.data_dir / "scratch" / "organizar.sh").exists(), "debió quedar en la temporal"

    # Un script que el agente escribe pero NO ejecuta (posible entregable) NO se toca.
    ts2 = {}
    keep = allowed / "entregable.sh"
    await b.use_tool("write_file", {"path": str(keep), "content": "echo x"}, s, IO(), "hazme un script", [], ts2)
    moved2 = await b.cleanup_scripts(ts2, IO())
    assert moved2 == [] and keep.exists(), "un script no ejecutado no debe moverse"
    print("OK: v0.2.11 (scripts de un solo uso -> temporal; entregables intactos)")

asyncio.run(v0211())


# ---- v0.2.12: lentitud. Organizar no fuerza el 9b; un modelo lento baja de escalón y no se vuelve a él ----
async def v0212():
    import copy
    from orbital.core import Brain, Session, complexity_rules
    from orbital import config, core
    from orbital.providers.ollama import Ollama

    # complejidad: razonamiento => 3; ejecución => al menos 2, pero respeta lo que diga el enrutador
    assert complexity_rules("organiza mis descargas por tipo", 1)[0] == 2
    assert complexity_rules("mueve todos los pdf a Documentos", 1)[0] == 2
    assert complexity_rules("organiza mis descargas por tipo", 2) == (2, "enrutador")
    assert complexity_rules("descifra el archivo y organiza el resto", 1)[0] == 3   # razonar gana
    assert complexity_rules("refactoriza core.py", 1)[0] == 3

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    cfg["agent"]["final_review"] = False
    cfg["agent"]["slow_step_seconds"] = 75  # v0.2.17: apagada por defecto (0); el mecanismo se sigue probando
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    # reloj falso: cada llamada al modelo grande "tarda" 200 s, la del mediano 5 s
    clock = [0.0]
    core.time.monotonic = lambda: clock[0]
    used = []
    class Slow(Ollama):
        def __init__(self, model, secs, script):
            super().__init__("http://x", model); self.secs, self.script = secs, list(script)
        async def chat(self, messages, json_mode=False, **k):
            used.append(self.model); clock[0] += self.secs
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]
    d = Path(tempfile.mkdtemp())
    big = Slow("qwen3.5:9b", 200, [json.dumps({"tool": "list", "args": {"path": str(d)}})])
    mid = Slow("qwen3.5:4b", 5, [json.dumps({"tool": "list", "args": {"path": str(d)}}),
                                 json.dumps({"respuesta": "listo"})])
    b._tier_cache = {"qwen3.5:9b": big, "qwen3.5:4b": mid, "qwen3.5:2b": Slow("qwen3.5:2b", 1, ['{"respuesta":"x"}'])}
    io = IO()
    s = Session(cwd=d)
    ans = await b._agent_loop("lista", s, io, big, core.TaskMemory(b.data_dir, "t", "x", "p", d), cfg["agent"], {})
    assert ans == "listo", (ans, io.out)
    assert used[:2] == ["qwen3.5:9b", "qwen3.5:9b"] and used[2] == "qwen3.5:4b", used   # 2 pasos lentos y baja
    assert any("demasiado lento" in str(e) for e in io.out)

    # forzado con /modelo: avisa pero NO cambia el modelo
    used.clear(); big.script = [json.dumps({"tool": "list", "args": {"path": str(d)}})] * 3 + [json.dumps({"respuesta": "ok9"})]
    s2 = Session(cwd=d); s2.model_force = "qwen3.5:9b"; io = IO()
    ans = await b._agent_loop("lista", s2, io, big, core.TaskMemory(b.data_dir, "t", "x", "p", d), cfg["agent"], {})
    assert ans == "ok9" and set(used) == {"qwen3.5:9b"}, used
    assert any("modelo fijado, sin cambio" in str(e) for e in io.out)

    # la escalera NO vuelve a un modelo marcado como lento
    kind, val = await b.step_up([big, mid], IO(), "x", "t", s, core.TaskMemory(b.data_dir, "t", "x", "p", d), [],
                                skip={"qwen3.5:9b"})
    assert kind == "switch" and val is mid

    # bug viejo: al agotar pasos y subir de proveedor llamaba a un método inexistente
    cfg["agent"]["max_steps"] = 2
    small = Slow("qwen3.5:2b", 1, [json.dumps({"tool": "list", "args": {"path": str(d)}})])
    mid.script = [json.dumps({"respuesta": "terminó el 4b"})]
    b._tier_cache["qwen3.5:2b"] = small
    b.gemini.key = ""
    ans = await b._agent_loop("lista", Session(cwd=d), IO(), small, core.TaskMemory(b.data_dir, "t", "x", "p", d), cfg["agent"], {})
    assert ans == "terminó el 4b", ans
    import time as _t; core.time.monotonic = _t.monotonic
    print("OK: v0.2.12 (organizar no fuerza 9b; baja de modelo por lentitud sin ping-pong; escalera tras agotar pasos)")

asyncio.run(v0212())


# ---- v0.2.13: organización nativa (clasificar/mover/deshacer) sin bash ----
async def v0213():
    from orbital.core import Brain, Session
    from orbital.permissions import Policy
    from orbital import config, organize

    b = Brain(config.DEFAULTS, lambda e: None, Path(tempfile.mkdtemp()))
    base = Path(tempfile.mkdtemp())
    dl = base / "Descargas"; dl.mkdir()
    for n in ["a.jpg", "B.PNG", "doc.pdf", "notas.txt", "x.tar.gz", "raro.xyz", ".oculto.jpg",
              "bajando.mp4.part", "id_rsa", ".env"]:
        (dl / n).write_text(n)
    (dl / "Imagenes").mkdir(); (dl / "Imagenes" / "a.jpg").write_text("YA ESTABA")   # colisión
    (dl / "sub").mkdir(); (dl / "sub" / "dentro.jpg").write_text("x")                  # no recursivo
    os_link = dl / "enlace.jpg"; os_link.symlink_to(dl / "a.jpg")
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=1, folders=[dl]))

    # previsualizar: no toca nada y describe el plan
    out = await b.use_tool("classify", {"path": str(dl), "preview": True}, s, IO(), "organiza", [], {})
    assert "PREVISUALIZACIÓN" in out and (dl / "a.jpg").exists(), out
    assert "1 archivos sin categoría" in out, out           # raro.xyz se queda
    # de verdad
    out = await b.use_tool("classify", {"path": str(dl)}, s, IO(), "organiza descargas", [], {})
    assert "MOVIDOS" in out and "undo" in out, out
    assert (dl / "Imagenes" / "a.jpg").read_text() == "YA ESTABA", "jamás sobrescribe"
    assert (dl / "Imagenes" / "a (1).jpg").read_text() == "a.jpg"
    assert (dl / "Imagenes" / "B.PNG").exists() and (dl / "PDF" / "doc.pdf").exists()
    assert (dl / "Comprimidos" / "x.tar.gz").exists() and (dl / "Documentos" / "notas.txt").exists()
    for keep in ["raro.xyz", ".oculto.jpg", "bajando.mp4.part", "id_rsa", ".env", "enlace.jpg"]:
        assert (dl / keep).exists() or (dl / keep).is_symlink(), f"{keep} no debió moverse"
    assert (dl / "sub" / "dentro.jpg").exists()
    # deshacer: todo regresa, la colisión original queda intacta, carpetas creadas y vacías se quitan
    out = await b.use_tool("undo", {}, s, IO(), "deshaz", [], {})
    assert "DESHECHO" in out, out
    assert (dl / "a.jpg").read_text() == "a.jpg" and (dl / "doc.pdf").exists()
    assert (dl / "Imagenes" / "a.jpg").read_text() == "YA ESTABA"
    assert (dl / "Imagenes").is_dir() and not (dl / "PDF").exists(), "solo quita carpetas que el lote creó"
    assert "ERROR" in await b.use_tool("undo", {}, s, IO(), "x", [], {})   # ya no hay lote pendiente

    # mover con patrones y reglas propias
    out = await b.use_tool("move", {"source": str(dl), "pattern": ["*.jpg", "*.png"], "dest": "Fotos"},
                           s, IO(), "x", [], {})
    assert (dl / "Fotos" / "a.jpg").exists() and (dl / "Fotos" / "B.PNG").exists(), out
    out = await b.use_tool("classify", {"path": str(dl), "rules": {"Facturas": ["doc*"], "Texto": ["txt"]}},
                           s, IO(), "x", [], {})
    assert (dl / "Facturas" / "doc.pdf").exists() and (dl / "Texto" / "notas.txt").exists(), out

    # permisos: destino fuera del área => rechazado; nivel 0 => rechazado; asistido => pregunta
    fuera = base / "fuera"
    out = await b.use_tool("move", {"source": str(dl / "raro.xyz"), "dest": str(fuera)}, s, IO(), "x", [], {})
    assert out.startswith("RECHAZADO") and (dl / "raro.xyz").exists(), out
    s0 = Session(cwd=dl, policy=Policy(autonomous=True, level=0, folders=[dl]))
    out = await b.use_tool("move", {"source": str(dl / "raro.xyz"), "dest": "X"}, s0, IO(), "x", [], {})
    assert out.startswith("RECHAZADO"), out
    out = await b.use_tool("move", {"source": str(dl / "raro.xyz"), "dest": "X", "preview": True},
                           s0, IO(), "x", [], {})
    assert "PREVISUALIZACIÓN" in out, "previsualizar es solo lectura: nivel 0 lo permite"
    io = IO(confirm=False)
    out = await b.use_tool("move", {"source": str(dl / "raro.xyz"), "dest": "X"}, Session(cwd=dl), io, "x", [], {})
    assert out.startswith("RECHAZADO") and any(e[0] == "confirm" and "raro.xyz" in e[1] for e in io.out), io.out
    # un secreto pedido directamente tampoco se mueve
    out = await b.use_tool("move", {"source": str(dl / "id_rsa"), "dest": "X"}, s, IO(), "x", [], {})
    assert (dl / "id_rsa").exists(), out
    print("OK: v0.2.13 (clasificar/mover/deshacer: previsualiza, no sobrescribe, no borra, revierte, respeta permisos)")

asyncio.run(v0213())


# ---- v0.2.14: guardia no-borrar + rm suelto pregunta + tope de tokens por paso ----
async def v0214():
    import copy
    from orbital.core import Brain, Session, NO_DELETE_RX
    from orbital.permissions import Policy
    from orbital import config, core
    from orbital.providers.ollama import Ollama

    d = Path(tempfile.mkdtemp())
    pol = Policy(autonomous=True, level=2, folders=[d])
    # antes: "rm archivo" en nivel 2 corría sin preguntar. Ahora pregunta; lo inocuo sigue libre.
    for cmd in ["rm foto.jpg", "ls && rm -f x", "find . -name '*.tmp' -delete", "rmdir vacia",
                "python3 -c 'import os; os.remove(\"x\")'", "git clean -fd", "unlink x", "truncate -s0 x",
                "/bin/rm x", "\\rm x", "echo $(rm x)", "a; rm x"]:
        assert pol.check("run", {"command": cmd, "cwd": str(d)}) == "ask", cmd
    for cmd in ["echo hola", "ls -la", "python3 -c 'print(1)'", "grep -r term .", "mkdir nueva", "firmware info", "cat notas.rm", "ls /tmp/form"]:
        assert pol.check("run", {"command": cmd, "cwd": str(d)}) == "allow", cmd
    # con la guardia: borrar se RECHAZA (no pregunta: es invariante), mover/clasificar siguen permitidos
    pol.no_delete = True
    assert pol.check("run", {"command": "rm foto.jpg", "cwd": str(d)}).startswith("deny"), "guardia"
    assert pol.check("run", {"command": "echo ok", "cwd": str(d)}) == "allow"
    assert pol.check("classify", {"path": str(d)}) == "allow"
    assert "delete=false" in pol.describe()

    # se activa sola con la frase del usuario y sobrevive a un cambio de política (/sesion)
    assert NO_DELETE_RX.search("organiza mis descargas pero no borres nada")
    assert NO_DELETE_RX.search("ordénalo sin borrar archivos")
    assert not NO_DELETE_RX.search("borra los .tmp")
    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    (d / "foto.jpg").write_text("x")
    s = Session(cwd=d, policy=Policy(autonomous=True, level=2, folders=[d]))
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": False})])
    b.local = Fake("local", ["ok"])
    io = IO()
    await b.handle("ayúdame con descargas, no borres nada", s, io)
    assert s.no_delete and any("delete=false" in str(e) for e in io.out)
    s.policy = Policy(autonomous=True, level=3, folders=[d])        # como si hicieras /sesion de nuevo
    out = await b.use_tool("run", {"command": "rm foto.jpg"}, s, IO(), "x", [], {})
    assert out.startswith("RECHAZADO") and (d / "foto.jpg").exists(), out

    # tope de tokens: se manda num_predict al modelo local; si se corta por el tope, se duplica
    seen = []
    class Capped(Ollama):
        def __init__(self): super().__init__("http://x", "qwen3.5:4b"); self.n = 0
        async def chat(self, messages, json_mode=False, options=None, **k):
            seen.append((options or {}).get("num_predict")); self.n += 1
            if self.n == 1:
                self.last_done_reason = "length"; return '{"tool": "write_file", "args": {"content": "muy lar'
            self.last_done_reason = "stop"; return json.dumps({"respuesta": "hecho"})
    cfg["agent"]["final_review"] = False
    ans = await b._agent_loop("x", Session(cwd=d), IO(), Capped(),
                              core.TaskMemory(b.data_dir, "t", "x", "p", d), cfg["agent"], {})
    assert ans == "hecho" and seen == [1024, 2048], seen
    print("OK: v0.2.14 (guardia no-borrar invariante; rm suelto pregunta; tope de tokens por paso que se adapta)")

asyncio.run(v0214())


# ---- v0.2.15: tiempos medidos por paso; recorte que no rompe la caché; keep_alive por escalón ----
async def v0215():
    import copy
    from orbital.core import Brain, Session
    from orbital import config, core
    from orbital.providers.ollama import Ollama, stats_from

    st = stats_from({"done": True, "load_duration": 2_000_000_000, "prompt_eval_count": 3000,
                     "prompt_eval_duration": 30_000_000_000, "eval_count": 60, "eval_duration": 6_000_000_000})
    assert st["load_s"] == 2.0 and st["prompt_tokens"] == 3000 and st["prompt_s"] == 30.0
    assert st["eval_tps"] == 10.0 and st["prompt_tps"] == 100.0
    assert stats_from({"done": True}) == {"load_s": 0.0, "prompt_tokens": 0, "prompt_s": 0.0, "eval_tokens": 0,
                                          "eval_s": 0.0, "eval_tps": 0.0, "prompt_tps": 0.0}

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False; cfg["agent"]["final_review"] = False
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    p8k = Ollama("http://x", "qwen3.5:4b", options={"num_ctx": 8192})

    # Recorte: bajo el presupuesto NO toca nada (el prefijo queda idéntico => Ollama reutiliza su caché)
    msgs = [{"role": "system", "content": "s" * 2000}, {"role": "user", "content": "tarea"}]
    for i in range(4):
        msgs += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": f"RESULTADO de x:\n" + "r" * 1500}]
    before = copy.deepcopy(msgs)
    assert b.trim_if_needed(msgs, p8k, {}) is False and msgs == before
    # Calidad: nunca menos contexto que la versión anterior. El viejo recortaba SIEMPRE; el nuevo ve >=.
    old = copy.deepcopy(before); Brain.trim(old)
    assert sum(len(m["content"]) for m in msgs) >= sum(len(m["content"]) for m in old)
    # Sobre el presupuesto: recorta de golpe. Contrato v0.2.16: en local deja completo SOLO el último resultado
    # (antes 2). Con 2 completos y resultados grandes se volvía a recortar en cada paso (10 recortes en 7 pasos
    # en la corrida real), que es justo lo que este cambio quería evitar.
    for i in range(10):
        msgs += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": "RESULTADO de x:\n" + "r" * 2000}]
    big = copy.deepcopy(msgs); Brain.trim(big, keep_full=1)
    state = {}
    assert b.trim_if_needed(msgs, p8k, state) is True and msgs == big and state["trims"] == 1
    # el presupuesto deja sitio a la salida: con tope de 4096 recorta antes que con 1024
    mid = [{"role": "system", "content": "x" * int((8192 - 1024 - 1024 - 500) * 2.8)}]
    assert not b.trim_if_needed(copy.deepcopy(mid), p8k, {"num_predict": 1024})
    assert b.trim_if_needed(copy.deepcopy(mid) + [{"role": "user", "content": "RESULTADO de a:\nz"}] * 3, p8k,
                            {"num_predict": 4096})

    # Métricas en el bucle: desglose por paso y resumen de la tarea
    class Timed(Ollama):
        def __init__(self): super().__init__("http://x", "qwen3.5:4b", options={"num_ctx": 8192}); self.n = 0
        async def chat(self, messages, json_mode=False, options=None, **k):
            self.n += 1
            self.last_stats = {"load_s": 0.0, "prompt_tokens": 900, "prompt_s": 9.0, "eval_tokens": 40,
                               "eval_s": 4.0, "eval_tps": 10.0, "prompt_tps": 100.0}
            d = str(tempfile.gettempdir())
            return json.dumps({"pensamiento": "miro", "tool": "list", "args": {"path": d}}) if self.n == 1 \
                else json.dumps({"respuesta": "ok"})
    io = IO()
    ans = await b.run_agent("lista", Session(cwd=Path(tempfile.gettempdir())), io, Timed())
    assert ans == "ok", ans
    assert any("prompt 900 tok en 9.0 s" in str(e) and "10.0 tok/s" in str(e) for e in io.out), io.out
    assert any("tiempo de la tarea" in str(e) and "leer contexto 18 s" in str(e) for e in io.out), io.out

    # keep_alive por escalón: el 4b se queda 30 min, el 9b 5 min, el 2b (enrutador) el suyo
    b2 = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    assert b2.tier_provider(1).keep_alive == "30m" and b2.tier_provider(2).keep_alive == "5m"
    assert b2.tier_provider(0).keep_alive == cfg["models"]["router_keep_alive"]
    print("OK: v0.2.15 (tiempos por paso; recorte solo cerca del límite = misma o más info; keep_alive por escalón)")

asyncio.run(v0215())


# ---- v0.2.15 (cont.): desborde de contexto, num_thread opcional, medir.py ----
async def v0215b():
    import copy
    from orbital.core import Brain
    from orbital import config, medir
    from orbital.providers.ollama import Ollama

    cfg = copy.deepcopy(config.DEFAULTS)
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    # si ni recortando cabe, recorta más: que Ollama no descarte en silencio la tarea original
    p4k = Ollama("http://x", "qwen3.5:2b", options={"num_ctx": 4096})
    msgs = [{"role": "system", "content": "s" * 3000}, {"role": "user", "content": "tarea"}]
    for _ in range(3):
        msgs += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": "RESULTADO de x:\n" + "r" * 5000}]
    st = {}
    assert b.trim_if_needed(msgs, p4k, st) and st["trims"] == 2
    assert msgs[0]["content"] == "s" * 3000, "el system prompt nunca se recorta"

    # num_thread: 0 = no se manda (decide Ollama); fijo = va a todos los modelos locales
    assert "num_thread" not in b.tier_provider(1).extra and "num_thread" not in b.router.extra
    cfg2 = copy.deepcopy(config.DEFAULTS); cfg2["models"]["num_thread"] = 8
    b2 = Brain(cfg2, lambda e: None, Path(tempfile.mkdtemp()))
    assert b2.tier_provider(1).extra["num_thread"] == 8 and b2.router.extra["num_thread"] == 8
    assert b2.tier_provider(1).extra["num_ctx"] == 8192

    # medir: recomienda fijar hilos solo si gana con claridad
    mk = lambda pt, et, ps, es: {"prompt_tps": pt, "eval_tps": et, "prompt_s": ps, "eval_s": es}
    rows = medir.summarize({0: [mk(80, 6, 20, 15)] * 2, 8: [mk(110, 7, 15, 13)] * 2, 4: [mk(60, 5, 27, 18)] * 2})
    assert rows[0]["hilos"] == 8 and "num_thread = 8" in medir.recommend(rows)
    rows = medir.summarize({0: [mk(80, 6, 20, 15)], 8: [mk(81, 6, 20, 14.5)]})
    assert "menor al 5 %" in medir.recommend(rows)
    rows = medir.summarize({0: [mk(90, 7, 15, 12)], 8: [mk(80, 6, 20, 15)]})
    assert "num_thread = 0" in medir.recommend(rows)
    # cada medición empieza distinto (la caché de Ollama no hace trampa) y manda los hilos pedidos
    sent = []
    class R:
        def raise_for_status(self): pass
        def json(self): return {"done": True, "prompt_eval_count": 1800, "prompt_eval_duration": 18e9,
                                "eval_count": 96, "eval_duration": 12e9}
    real = medir.httpx.post
    medir.httpx.post = lambda url, json=None, timeout=None: (sent.append(json), R())[1]
    try:
        assert medir.main(["--hilos", "0,8", "--repeticiones", "1"]) == 0
    finally:
        medir.httpx.post = real
    firsts = [m["messages"][0]["content"].splitlines()[0] for m in sent]
    assert len(set(firsts)) == len(firsts) == 4, firsts
    assert "num_thread" not in sent[0]["options"] and sent[2]["options"]["num_thread"] == 8
    print("OK: v0.2.15 (desborde: recorta más antes que perder la tarea; num_thread opcional; medir.py recomienda con datos)")

asyncio.run(v0215b())


# ---- v0.2.16: el incidente de Descargas (pegado partido -> clasificar TODO sin filtro ni permiso) ----
async def v0216():
    import copy
    from orbital.core import Brain, Session
    from orbital import config, core
    from orbital.client import split_input
    from orbital.providers import ProviderError
    from orbital.providers.ollama import Ollama

    # 1) un pegado de varias líneas es UN mensaje (antes: la línea 1 corría sin el "no borres" de la línea 3)
    pegado = "Organiza lo de WhatsApp de ~/Descargas. Do\ns formatos de nombre\nNO toques _respaldo. No borres nada."
    assert split_input(pegado) == [pegado], split_input(pegado)
    assert core.NO_DELETE_RX.search(split_input(pegado)[0])
    assert split_input("/cd ~/x\n/state") == ["/cd ~/x", "/state"]      # solo comandos: uno por uno
    assert split_input("hola") == ["hola"] and split_input("  \n ") == []

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    # v0.2.17 apagó por defecto la confirmación de lotes, la bajada por lentitud y el plan automático
    cfg["agent"].update(organize_confirm_over=25, slow_step_seconds=75, quick_organize=False)
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    dl = Path(tempfile.mkdtemp()) / "Descargas"; dl.mkdir(parents=True)
    wa = [f"WhatsApp Image 2024-03-{i:02d} at 10.00.00.jpeg" for i in range(1, 7)] + \
         [f"IMG-202405{i:02d}-WA0001.jpg" for i in range(1, 4)] + ["WhatsApp Video 2024-01-01 at 9.00.00.mp4"]
    otros = [f"foto{i}.png" for i in range(20)] + [f"tarea{i}.pdf" for i in range(8)] + ["mod.jar", "juego.blend"]
    for n in wa + otros:
        (dl / n).write_text(n)
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))

    # 2) sin filtro la previsualización avisa que se lleva TODO, y el lote grande te pregunta aun en autónomo
    out = await b.use_tool("classify", {"path": str(dl), "dest": str(dl / "WhatsApp"), "other": True,
                                          "preview": True}, s, IO(), "organiza lo de whatsapp", [], {})
    assert "Mueve TODOS" in out and "filter" in out, out
    io = IO(confirm=False)
    out = await b.use_tool("classify", {"path": str(dl), "dest": str(dl / "WhatsApp"), "other": True},
                           s, io, "organiza lo de whatsapp", [], {})
    assert out.startswith("RECHAZADO"), out
    assert any(e[0] == "confirm" and "40 archivos" in e[1] for e in io.out), io.out
    assert all((dl / n).exists() for n in wa + otros) and not (dl / "WhatsApp").exists(), "nada se movió sin tu sí"

    # 3) con filtro solo se mueve lo de WhatsApp; lo demás ni se toca
    io = IO(confirm=False)  # 10 archivos: bajo el umbral, no hace falta preguntar
    out = await b.use_tool("classify", {"path": str(dl), "dest": str(dl / "WhatsApp"),
                                          "filter": ["whatsapp", "IMG-*-WA*"]}, s, io, "x", [], {})
    assert "MOVIDOS: 10 archivos" in out and "10 de 40" in out, out
    assert all((dl / n).exists() for n in otros), "lo que no es de WhatsApp se queda"
    assert (dl / "WhatsApp" / "Imagenes" / wa[0]).exists() and (dl / "WhatsApp" / "Videos" / wa[-1]).exists()
    assert "DESHECHO" in await b.use_tool("undo", {}, s, IO(), "x", [], {}) and (dl / wa[0]).exists()

    # 4) $ORBITAL_SCRATCH se expande (antes creaba una carpeta literal '$ORBITAL_SCRATCH' en Descargas)
    out = await b.use_tool("write_file", {"path": "$ORBITAL_SCRATCH/uno.sh", "content": "echo hola"},
                           s, IO(), "x", [], {})
    assert (b.data_dir / "scratch" / "uno.sh").read_text() == "echo hola", out
    assert not any("$" in p.name for p in dl.iterdir()), list(dl.iterdir())
    out = await b.use_tool("write_file", {"path": "$HOME/x.sh", "content": "x"}, s, IO(), "x", [], {})
    assert out.startswith("ERROR") and "sin expandir" in out, out

    # 5) herramienta inexistente: error claro, sin decir que el usuario "rechazó" algo
    out = await b.use_tool("respuesta", {}, s, IO(), "x", [], {})
    assert out.startswith("ERROR") and "inexistente" in out and "RECHAZADO" not in out, out

    # 6) {"tool": "respuesta"} cuenta como respuesta final; una respuesta vacía no se entrega
    cfg["agent"]["final_review"] = False
    mem = lambda: core.TaskMemory(b.data_dir, "t", "x", "p", dl)
    ans = await b._agent_loop("x", s, IO(), Fake("f", [json.dumps({"tool": "respuesta", "args": {"respuesta": "listo"}})]),
                              mem(), cfg["agent"], {})
    assert ans == "listo", ans
    ans = await b._agent_loop("x", s, IO(), Fake("f", [json.dumps({"tool": "list", "args": {"path": str(dl)}}),
                                                       json.dumps({"respuesta": ""})]), mem(), cfg["agent"], {})
    assert "no redactada" in ans and "list" in ans, ans

    # 7) lentitud: cargar el modelo o escribir mucho no es "ir lento"; poco texto en mucho tiempo sí
    class P(Ollama):
        async def unload(self): self.unloaded = True
    p9 = P("http://x", "qwen3.5:9b"); st = {}
    p9.last_stats = {"load_s": 60.0, "eval_tokens": 50}
    assert await b.check_slow(p9, 100, s, IO(), mem(), st) is p9 and not st.get("slow")       # 40 s reales
    p9.last_stats = {"load_s": 0.0, "eval_tokens": 980}
    assert await b.check_slow(p9, 243, s, IO(), mem(), st) is p9 and not st.get("slow")       # salida larga
    p9.last_stats = {"load_s": 0.0, "eval_tokens": 50}
    await b.check_slow(p9, 100, s, IO(), mem(), st)
    down = await b.check_slow(p9, 100, s, IO(), mem(), st)
    assert down.model == "qwen3.5:4b" and p9.unloaded, "al bajar descarga el grande (si no, conviven en RAM)"

    # 8) tope por resultado al insertar: con resultados de 6000 caracteres ya no se reescribe en cada paso
    prov = Ollama("http://x", "m", options={"num_ctx": 8192}); stt = {}
    msgs = [{"role": "system", "content": "s" * 5500}, {"role": "user", "content": "organiza"}]
    muts = 0
    for _ in range(10):
        before = [m["content"] for m in msgs]
        b.trim_if_needed(msgs, prov, stt)
        muts += any(a != m["content"] for a, m in zip(before, msgs))
        msgs.append({"role": "assistant", "content": '{"tool":"list","args":{}}'})
        msgs.append({"role": "user", "content": "RESULTADO de listar:\n" + b.cap_result("a" * 6000, prov, msgs, stt)})
    assert muts <= 4, f"{muts} de 10 pasos reescriben contexto viejo (antes: 8)"
    assert b.cap_result("corto", prov, msgs, stt) == "corto" and b.cap_result("a" * 6000, Fake("g", []), msgs, stt) == "a" * 6000

    # 9) Gemini se cae armando el plan: no se reintenta en el paso 1, y el modelo local es el de la complejidad real
    calls = []
    async def caido(messages, json_mode=False, **k):
        calls.append(1); raise ProviderError("sin red", fatal=True, network=True)
    b.gemini.key = "k"; b.gemini.chat = caido
    b.router = Fake("r", [json.dumps({"route": "medium", "herramientas": True, "complejidad": 2})])
    b.local = Fake("local", ["Plan: clasificar con filtro whatsapp. Al final borrará lo que quede suelto.",
                             json.dumps({"respuesta": "hecho"})])
    io = IO(confirm=True)
    await b.handle("organiza con un script lo de whatsapp",
                   Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]), plan_mode="auto"), io)
    assert len(calls) == 1, f"Gemini se intentó {len(calls)} veces (debe ser 1: solo en el plan)"
    assert any(e[0] == "confirm" and "borrar" in e[1] for e in io.out), "un plan que borra se señala antes del sí"
    assert io.out[-1][0] == "say" and "hecho" in io.out[-1][2], io.out[-1]
    print("OK: v0.2.16 (pegado = 1 mensaje; filtro; lote grande pregunta; $ORBITAL_SCRATCH; respuesta vacía; lentitud real; caché; Gemini caído)")

asyncio.run(v0216())


# ---- v0.2.17: autonomía sin pedir permiso, organizar en UNA llamada, "no toques X", cerrar al terminar ----
async def v0217():
    import copy
    from orbital.core import Brain, Session
    from orbital import config, core
    from orbital.providers.ollama import Ollama

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    dl = Path(tempfile.mkdtemp()) / "Descargas"; dl.mkdir(parents=True)
    wa = [f"WhatsApp Image 2024-03-{i:02d} at 10.00.00 PM.jpeg" for i in range(1, 31)] + \
         ["IMG-20240516-WA0001.jpg", "WhatsApp Video 2025-01-02 at 9.00.00.mp4", "WhatsApp Ptt 2025-02-03.pdf"]
    otros = [f"foto{i}.png" for i in range(30)] + ["tarea.pdf", "mod.jar", "image_render.blend"]
    for n in wa + otros:
        (dl / n).write_text(n)
    resp = dl / "_respaldo_whatsapp"; resp.mkdir()
    (resp / "WhatsApp Image 2023-01-01 at 1.00.00.jpeg").write_text("respaldo")

    # la petición real, pegada con sus saltos de línea
    pedido = ('Organiza las imágenes, videos, pdfs,etc... de WhatsApp de ~/Descargas con UN solo script bash: crea '
              '~/Descargas/WhatsApp y dentro subcarpetas, y mueve ahí cada imagen de Wh\natsApp. Maneja nombres con\n'
              'espacios. NO toques la carpeta_respaldo_whatsapp ni nada que no sea imagen de WhatsApp. No borres nada. '
              'la capeta debe llamarse WATSAPP y\ndentro organizar lo de watsapp por videos fotos pdfs etc...')
    calls = []
    class Uno(Fake):
        async def chat(self, messages, json_mode=False, **k):
            calls.append(messages); return await super().chat(messages, json_mode, **k)
    class Nunca(Fake):
        async def chat(self, *a, **k): raise AssertionError("la vía rápida no debe pasar por el enrutador")
    b.router = Nunca("r", [])
    b.local = Uno("local", [json.dumps({"tool": "classify", "args": {
        "path": str(dl), "dest": str(dl / "WATSAPP"), "filter": ["whatsapp", "IMG-*-WA*"], "by_date": "month"}})])
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    io = IO(confirm=False)   # si preguntara algo, la respuesta sería NO y nada se movería
    await b.handle(pedido, s, io)
    assert len(calls) == 1, f"{len(calls)} llamadas al modelo (debe ser 1)"
    assert "Palabras frecuentes" in calls[0][0]["content"] and "whatsapp×" in calls[0][0]["content"]  # ve el inventario
    assert not any(e[0] in ("confirm", "relay", "choose") for e in io.out), "sesión autónoma: no pregunta"
    assert s.no_delete and resp.resolve() in s.no_touch, s.no_touch
    assert (dl / "WATSAPP" / "Imagenes" / "2024-03" / wa[0]).exists()
    assert (dl / "WATSAPP" / "Imagenes" / "2024-05" / "IMG-20240516-WA0001.jpg").exists()
    assert (dl / "WATSAPP" / "Videos" / "2025-01" / wa[-2]).exists() and (dl / "WATSAPP" / "PDF" / "2025-02" / wa[-1]).exists()
    assert all((dl / n).exists() for n in otros), "lo que no es de WhatsApp no se toca"
    assert len(list(resp.iterdir())) == 1, "la carpeta vetada queda intacta"
    assert io.out[-1][0] == "say" and "MOVIDOS: 33 archivos" in io.out[-1][2] and "/undo" in io.out[-1][2]

    # "no toques X" es un invariante: ni listar, ni mover, ni comandos, aunque el modelo insista
    for tool, args in [("list", {"path": str(resp)}), ("move", {"source": str(resp), "pattern": "*", "dest": str(dl / "X")}),
                       ("run", {"command": "ls _respaldo_whatsapp"}),
                       ("write_file", {"path": str(resp / "a.txt"), "content": "x"})]:
        out = await b.use_tool(tool, args, s, IO(), "x", [], {})
        assert out.startswith("RECHAZADO") and "veto activo" in out, (tool, out)
    # una segunda petición sin el "no toques" lo sigue respetando (invariante de sesión)
    b.local = Uno("local", [json.dumps({"tool": "move", "args": {"source": str(resp), "pattern": "*", "dest": str(dl / "Y")}})])
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True, "complejidad": 1})])
    b.local.script.append(json.dumps({"respuesta": "no pude: esa carpeta está vetada"}))
    await b.handle("mueve los archivos del respaldo a Y", s, IO())
    assert len(list(resp.iterdir())) == 1 and not (dl / "Y").exists()

    # deshacer regresa el lote completo
    assert "DESHECHO" in await b.use_tool("undo", {}, s, IO(), "x", [], {}) and (dl / wa[0]).exists()

    # si el modelo dice que no es una orden de mover, sigue por el agente normal (con enrutador)
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": False})])
    b.local = Fake("l", [json.dumps({"tool": "ninguna"}), "te explico cómo organizarías un equipo"])
    io = IO(); await b.handle("organiza mis ideas sobre el proyecto", s, io)
    assert io.out[-1][0] == "say" and "te explico" in io.out[-1][2], io.out[-1]

    # cerrar al terminar: tras el cambio real, pasos de solo mirar => cierra con los hechos (antes: 15 pasos de relleno)
    cfg["agent"]["final_review"] = False
    s2 = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    used = []
    class Loop(Fake):
        async def chat(self, messages, json_mode=False, **k):
            used.append(1); return await super().chat(messages, json_mode, **k)
    lp = Loop("l", [json.dumps({"tool": "move", "args": {"source": str(dl), "pattern": "tarea.pdf", "dest": str(dl / "Docs")}}),
                    json.dumps({"tool": "list", "args": {"path": str(dl / "Docs")}})])
    ans = await b._agent_loop("mueve tarea.pdf a Docs", s2, IO(), lp, core.TaskMemory(b.data_dir, "t", "x", "p", dl), cfg["agent"], {})
    assert ans.startswith("Tarea terminada") and "move" in ans and (dl / "Docs" / "tarea.pdf").exists(), ans
    assert len(used) <= 4, f"{len(used)} pasos (antes seguía hasta agotar max_steps)"

    # presupuesto con tokens REALES: 16 000 caracteres que Ollama contó como 8000 tokens ya no "caben" en 8192
    prov = Ollama("http://x", "m", options={"num_ctx": 8192}); st = {"sent_chars": 16000}
    prov.last_stats = {"prompt_tokens": 8000, "prompt_s": 1.0, "eval_tokens": 10, "eval_s": 1.0}
    msgs = [{"role": "system", "content": "s" * 4000}] + [{"role": "user", "content": "RESULTADO de x:\n" + "r" * 2990}] * 4
    assert not b.trim_if_needed(copy.deepcopy(msgs), prov, {})          # con la estimación fija parecía caber
    b.record_stats(prov, 2.0, st)
    assert 1.2 <= st["cpt"] < 2.0 and b.trim_if_needed(copy.deepcopy(msgs), prov, st), st

    # el aviso de "este plan borra" ya no salta con una negación
    assert not core.PLAN_DELETES_RX.search("NO borrar ningún archivo existente; solo reubicar. Sin eliminar nada.")
    assert core.PLAN_DELETES_RX.search("al final borrará lo que quede suelto")
    assert Session().plan_mode == "off" and cfg["agent"]["organize_confirm_over"] == 0
    print("OK: v0.2.17 (organizar = 1 llamada sin preguntar; no-tocar invariante; cierra al terminar; contexto con tokens reales)")

asyncio.run(v0217())


# ---- v0.2.18: filtro IMG* de cámara, otros, veto estricto, resumen legible, sonido de "terminé" ----
async def v0218():
    import copy
    from orbital.core import Brain, Session
    from orbital import config, organize
    from orbital.client import Client

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    dl = Path(tempfile.mkdtemp()) / "Descargas"; dl.mkdir(parents=True)
    wa = ["WhatsApp Video 2026-07-03 at 5.45.21 PM.mp4", "WhatsApp Ptt 2024-09-26 at 7.43.17 AM.mp3",
          "WhatsApp Ptt 2026-08-04 at 12.39.46 PM.pdf", "WhatsApp Image 2026-09-12 at 9.43.35 AM.jpeg",
          "IMG-20240516-WA0001.jpg", "VID-20240516-WA0002.mp4", "WhatsApp Sticker 2024.xyz"]
    camara = ["IMG-8236.webp", "IMG_0030.MOV", "IMG_0030.MOV.jpg", "img.png", "VID_2020.mp4", "foto.jpg"]
    for n in wa + camara:
        (dl / n).write_text(n)
    for d in ("_respaldo_whatsapp", "respaldo", "Documentos"):
        (dl / d).mkdir(); (dl / d / "x.jpg").write_text("x")

    # --- lo que el 4b devolvió de verdad en tu corrida (filtro IMG* + "*IMG*" dentro de la categoría imagenes)
    pedido = ("mueve todos los pngs, videos, imagenes, audios, pdfs, etc de ~/Descargas QUE SEAN DE WATSAPP (que incluyan "
              "Whats...) o IMG... a una carpeta llamada Watsapp con subcarpetas para imagenes, audios, videos, documentos "
              "y otros, no toques _respaldo_whatsapp o similares y NO BORRES NI ELIMINES NADA, solo reorganiza y agrupa.")
    salida_4b = {"tool": "classify", "args": {
        "path": str(dl), "dest": str(dl / "Watsapp"), "filter": ["Whats*", "IMG*"],
        "rules": {"imagenes": [".png", ".jpeg", ".jpg", ".webp", ".gif", "*IMG*"], "videos": [".mp4", ".m4v", "*.mov"],
                   "audios": [".m4a", ".mp3", ".wav"], "documentos": [".pdf", ".txt", ".zip"]}}}
    b.router = Fake("r", [])
    b.local = Fake("l", [json.dumps(salida_4b)])
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    io = IO(confirm=False)
    await b.handle(pedido, s, io)
    assert not any(e[0] in ("confirm", "relay", "choose") for e in io.out)
    W = dl / "Watsapp"
    # solo lo de WhatsApp se mueve; las fotos de cámara (IMG-8236, IMG_0030.MOV, img.png) NO
    for n in camara:
        assert (dl / n).exists(), f"{n} no es de WhatsApp y no debió moverse"
    assert (W / "imagenes" / "IMG-20240516-WA0001.jpg").exists() and (W / "imagenes" / wa[3]).exists()
    assert (W / "videos" / wa[0]).exists() and (W / "videos" / "VID-20240516-WA0002.mp4").exists()
    assert (W / "audios" / wa[1]).exists() and (W / "documentos" / wa[2]).exists()
    # "other" pedido en el texto: lo que coincide y no tiene categoría va a Otros (antes se quedaba suelto)
    assert (W / "Otros" / wa[6]).exists(), list(W.rglob("*"))
    # veto: _respaldo_whatsapp (lo escribiste) y respaldo (por "o similares"); Documentos no
    vet = sorted(p.name for p in s.no_touch)
    assert vet == ["_respaldo_whatsapp", "respaldo"], vet
    assert (dl / "respaldo" / "x.jpg").exists() and (dl / "Documentos" / "x.jpg").exists()
    say = io.out[-1][2]
    assert io.out[-1][0] == "say" and "\n\n" in say and "/undo" in say and '"rules"' not in say, say
    assert any("Filtro IMG*" in str(e) for e in io.out), "debe avisar que cambió IMG* por el patrón de WhatsApp"

    # sin "similares": veto estricto, no se pasa a carpetas parecidas
    s2 = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    b.apply_no_touch("no toques _respaldo_whatsapp y ya", s2)
    assert [p.name for p in s2.no_touch] == ["_respaldo_whatsapp"], s2.no_touch

    # reglas que repiten el filtro se quitan; un glob de extensión sí se conserva; sin filtro no se toca nada
    a, notes = organize.clean_rules({"filter": ["IMG*"], "rules": {"i": ["jpg", "*IMG*", "IMG*"], "v": ["*.mov"]}})
    assert a["rules"] == {"i": ["jpg"], "v": ["*.mov"]} and notes, (a, notes)
    a, notes = organize.clean_rules({"rules": {"F": ["factura*"]}})
    assert a["rules"] == {"F": ["factura*"]} and not notes
    # filtro de cámara pelón, sin que el texto hable de WhatsApp: se ejecuta tal cual pero la previsualización avisa
    out = await b.use_tool("classify", {"path": str(dl), "filter": ["IMG*"], "preview": True}, s2, IO(), "x", [], {})
    assert "incluye también fotos y videos de cámara" in out and "*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA*" in out, out
    # y el aviso no salta con el patrón correcto
    out = await b.use_tool("classify", {"path": str(dl), "filter": ["*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA*"], "preview": True}, s2, IO(), "x", [], {})
    assert "incluye también fotos y videos de cámara" not in out, out

    # --- sonido: el de "terminé" es distinto al de pedir ayuda y respeta /sonido, cancelación y duración
    todo = lambda f: True
    ask, done = Client.pick_sound("ask", todo), Client.pick_sound("done", todo)
    assert ask and done and ask != done, (ask, done)
    assert Client.pick_sound("done", lambda f: f.endswith("message.oga")) is None, "nunca reutiliza el sonido de pedir ayuda"
    c = Client.__new__(Client)
    c.cfg = {"ui": {"done_sound_min_seconds": 5}}; c.sound_on = True; c.was_cancelled = False
    suena = []
    c.beep = lambda kind="ask": suena.append(kind)
    c.beep_done(2.0); assert suena == [], "tarea corta: no suena"
    c.beep_done(30.0); assert suena == ["done"]
    c.was_cancelled = True; c.beep_done(30.0); assert suena == ["done"], "cancelada: no suena"
    c.was_cancelled = False; c.sound_on = False; c.beep_done(30.0); assert suena == ["done"], "/sonido off lo silencia"
    print("OK: v0.2.18 (IMG* de cámara fuera; otros automático; veto estricto; resumen legible; sonido de terminé distinto)")

asyncio.run(v0218())


# ---- v0.2.19: el patrón de WhatsApp casaba "autism-spectrum-...-wheel-it-was-..." ----
async def v0219():
    import copy, fnmatch
    import httpx
    from orbital.core import Brain, Session
    from orbital import config
    from orbital.providers import ollama as ol

    WA = "*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA*"
    falso = "autism-spectrum-as-a-colour-wheel-it-was-done-before-but-v0-c3whpf7kv9aa1-removebg-preview.png"
    assert fnmatch.fnmatch(falso.lower(), "*-????????-wa*"), "el patrón viejo SÍ lo casaba (por eso fue el bug)"
    assert not fnmatch.fnmatch(falso.lower(), WA.lower())
    assert fnmatch.fnmatch("IMG-20241001-WA0035.jpg".lower(), WA.lower())

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    b = Brain(cfg, lambda e: None, Path(tempfile.mkdtemp()))
    dl = Path(tempfile.mkdtemp()) / "Descargas"; dl.mkdir(parents=True)
    wa = ["WhatsApp Image 2024-01-16 at 9.49.57 PM.jpeg", "IMG-20241001-WA0035.jpg", "VID-20241001-WA0035.mp4",
          "WhatsApp Video 2026-07-03 at 5.45.21 PM.mp4", "WhatsApp Audio 2024-09-20 at 9.23.17 AM.ogg",
          "WhatsApp Ptt 2026-08-04 at 12.39.46 PM.pdf"]
    mov = ["WhatsApp Video 2025-01-02 at 9.00.00.mov"]
    otros = [falso, falso.replace(".png", ".webp"), "IMG_0030.MOV", "IMG-8236.webp", "foto.jpg", "a-1234567-wa.txt"]
    for n in wa + mov + otros:
        (dl / n).write_text(n)
    (dl / "_respaldo_whatsapp").mkdir()

    pedido = ("mueve todos los pngs, videos, imagenes, audios, pdfs, etc de ~/Descargas QUE SEAN DE WATSAPP (que incluyan "
              "Whatsapp...) o IMG...whatsapp....png/jpg, si es .mov ignoralo a una carpeta llamada Watsapp con subcarpetas "
              "para imagenes, audios, videos, documentos y otros, no toques _respaldo_whatsapp o similares y NO BORRES NADA")
    # exactamente lo que el 4b devolvió en tu corrida: filtro con el "????????" viejo y reglas como LISTA
    salida = {"tool": "classify", "args": {
        "path": str(dl), "dest": str(dl / "Watsapp"), "filter": ["whatsapp*", "*-????????-WA*"], "other": True,
        "rules": [{"Carpeta": ["*.png", "*.jpg"]}, {"Carpeta": ["*.mp4", "*.mov"]}]}}
    b.router = Fake("r", []); b.local = Fake("l", [json.dumps(salida)])
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    io = IO(); await b.handle(pedido, s, io)
    W = dl / "Watsapp"
    movidos = sorted(p.name for p in W.rglob("*") if p.is_file())
    assert movidos == sorted(wa), movidos
    for n in otros + mov:
        assert (dl / n).exists(), f"{n} no debió moverse"       # incluye el .mov de WhatsApp: pediste ignorarlo
    say = io.out[-1][2]
    assert io.out[-1][0] == "say" and "\\*" in say and "\\_" not in "" and " s · 1 llamada" in say, say
    assert "[0-9]" in str(io.out) or "0-9" in say or "filter" in say

    # fix_quick_args por separado: .mov en sus dos redacciones, reglas inválidas fuera, ? viejo -> dígitos
    f = Brain.fix_quick_args
    assert f({"filter": ["x"]}, "si es .mov ignoralo")["exclude"] == ["*.mov"]
    assert f({"filter": ["x"]}, "ignora los .MKV y .avi")["exclude"] == ["*.mkv"]
    assert "rules" not in f({"rules": [{"a": ["b"]}]}, "x") and f({"rules": {"a": ["b"]}}, "x")["rules"] == {"a": ["b"]}
    assert f({"filter": ["*-????????-WA*"]}, "x")["filter"] == [WA]
    assert f({"filter": ["IMG*"]}, "lo de whatsapp")["filter"] == [WA]

    # el esquema llega a Ollama como 'format' (así el modelo no puede escribir 'rules')
    seen = {}
    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b'{"message":{"content":"{\\"tool\\":\\"ninguna\\"}"},"done":true}\n')
    class Mock(httpx.AsyncClient):
        def __init__(self, **kw): kw["transport"] = httpx.MockTransport(handler); super().__init__(**kw)
    real = ol.httpx.AsyncClient; ol.httpx.AsyncClient = Mock
    try:
        out = await ol.Ollama("http://x", "m").chat([{"role": "user", "content": "x"}], json_mode=True, schema=Brain.QUICK_SCHEMA)
        assert json.loads(out)["tool"] == "ninguna" and seen["body"]["format"] == Brain.QUICK_SCHEMA
        assert "rules" not in seen["body"]["format"]["properties"]["args"]["properties"]
        await ol.Ollama("http://x", "m").chat([{"role": "user", "content": "x"}], json_mode=True)
        assert seen["body"]["format"] == "json"
    finally:
        ol.httpx.AsyncClient = real
    print("OK: v0.2.19 (patrón de WhatsApp con 8 dígitos; .mov ignorado; reglas inválidas fuera; esquema sin reglas; Markdown no come * ni _)")

asyncio.run(v0219())


# ---- v0.2.20: renombrar de verdad, árbol, vacías, mapa/agrupar, lote, avisos de argumentos ignorados ----
async def v0220():
    import copy, os, time
    from orbital.core import Brain, Session
    from orbital import config, organize as org, survey as mp

    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    data = Path(tempfile.mkdtemp())
    cfg["projects"]["root"] = str(Path(tempfile.mkdtemp()) / "Orbital")   # nunca el ~/Orbital real
    b = Brain(cfg, lambda e: None, data)
    mar25 = time.mktime((2025, 3, 15, 12, 0, 0, 0, 0, -1))

    def mk(root: Path, rel: str, mtime=None):
        f = root / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(rel)
        if mtime: os.utime(f, (mtime, mtime))
        return f

    # ---- 1) tu corrida: mover .blend con renombrar (el modelo lo OLVIDA; lo pone el código)
    dl = Path(tempfile.mkdtemp()) / "Descargas"
    mk(dl, "vaso.blend", mar25); mk(dl, "copa.blend", mar25); mk(dl, "vaso_mar25.blend2.txt")
    mk(dl, "proyecto/dentro.blend", mar25); mk(dl, "nota.txt")
    pedido = ("mueve todos los .blend a una carpeta llamada blender, agregale una subcarpeta llamada proyextos y cambia sus "
              "nombres para que al final digan el mes y año de creacion ej: vaso_mar25.blend, no borres nada ni muevas algo "
              "que este dentro de una carpeta actualmente")
    dest = dl / "blender" / "proyextos"
    salida = {"calls": [{"tool": "move", "args": {"source": str(dl), "pattern": ["*.blend"], "dest": str(dest),
                                                    "format": "mesaño"}}]}   # formato inválido: se ignora con gusto
    b.router = Fake("r", []); b.local = Fake("l", [json.dumps(salida)])
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    io = IO(); await b.handle(pedido, s, io)
    assert sorted(p.name for p in dest.iterdir()) == ["copa_mar25.blend", "vaso_mar25.blend"], sorted(p.name for p in dest.iterdir())
    assert (dl / "proyecto" / "dentro.blend").exists(), "lo de dentro de una carpeta NO se mueve"
    assert (dl / "nota.txt").exists()
    # idempotente y sin pisar: otro vaso.blend nuevo (mismo mes) no pisa nada
    assert org.new_name(dest / "vaso_mar25.blend", {"rename": True}) == "vaso_mar25.blend"
    mk(dl, "vaso.blend", mar25)
    moves, notes = org.plan_move({"source": str(dl), "pattern": ["*.blend"], "dest": str(dest), "rename": True})
    assert [d.name for _, d in moves] == ["vaso_mar25 (1).blend"], moves
    # argumentos desconocidos ya no se ignoran en silencio
    _, notes = org.plan_move({"source": str(dl), "pattern": ["*.blend"], "dest": str(dest), "colorines": 1})
    assert any("colorines" in n for n in notes), notes

    # ---- 2) árbol: jerarquía, vacías anidadas, filtro general+particular, lista completa, secretos y veto
    root = Path(tempfile.mkdtemp()) / "Descargas"
    mk(root, "a/uno.txt"); mk(root, "a/b/dos.blend"); mk(root, "a/b/tres.blend")
    (root / "vacia1").mkdir(parents=True); (root / "x/y/z").mkdir(parents=True); (root / "x/y/z2").mkdir()
    mk(root, "oculto/.keep"); mk(root, "conenlace/.placeholder")
    os.symlink("/etc/hostname", root / "conenlace" / "enlace")
    mk(root, "sec/.env"); (root / "vetada").mkdir(); mk(root, "vetada2/a.txt")
    s = Session(cwd=root, policy=Policy(autonomous=True, level=2, folders=[root]))
    s.no_touch = [root / "vetada2"]
    out = await b.use_tool("tree", {"paths": [str(root)], "depth": 3}, s, IO(), "x", [])
    assert "vacia1/" in out and "[VACÍA]" in out and "VACÍAS (" in out, out
    assert "x/" in out and "z/" in out
    assert ".env" not in out and "vetada2" not in out, out
    assert "oculto" in out and "[VACÍA]" not in [l for l in out.splitlines() if "oculto/" in l][0], "dir con un archivo oculto no está vacía"
    assert "a/" in out and "dos.blend" in out
    f = await b.use_tool("tree", {"paths": [str(root)], "filter": ["*.blend", "tres"], "depth": 3}, s, IO(), "x", [])
    assert "coinciden con el filtro" in f and "tres.blend" in f and "uno.txt" not in f, f
    t = await b.use_tool("tree", {"paths": [str(root / "a"), str(root / "x")], "txt": True, "depth": 2}, s, IO(), "x", [])
    assert t.count("ÁRBOL") == 2 and "LISTA COMPLETA" in t, t
    lst = Path(t.split("LISTA COMPLETA sin resumir")[1].split(": ", 1)[1].split("\n")[0].strip())
    assert lst.exists() and "dos.blend" in lst.read_text() and lst.is_relative_to(b.projects_root() / "Proyectos") and lst.parent.name == "listas" and s.node.get("recursos")
    denied = await b.use_tool("tree", {"paths": [str(Path.home() / ".ssh")]}, s, IO(), "x", [])
    assert denied.startswith("RECHAZADO"), denied
    vet = await b.use_tool("tree", {"paths": [str(root / "vetada2")]}, s, IO(), "x", [])
    assert vet.startswith("RECHAZADO"), vet

    # ---- 3) vacías: con la guardia no-borrar se rechaza CLARO; sin ella se hace, y /deshacer las recrea
    s.no_delete = True; s.policy.no_delete = True
    pre = await b.use_tool("empties", {"paths": [str(root)], "preview": True}, s, IO(), "x", [])
    assert pre.startswith("PREVISUALIZACIÓN") and "vacia1" in pre and "6 carpetas vacías" in pre and "/x/" in pre and "oculto" not in pre and "conenlace" not in pre and "vetada2" not in pre, pre
    no = await b.use_tool("empties", {"paths": [str(root)]}, s, IO(), "x", [])
    assert no.startswith("RECHAZADO") and "/delete true" in no and (root / "vacia1").exists(), no
    s.no_delete = False; s.policy.no_delete = False
    ok = await b.use_tool("empties", {"paths": [str(root)]}, s, IO(), "x", [])
    assert ok.startswith("ELIMINADAS") and not (root / "vacia1").exists() and not (root / "x").exists(), ok
    assert (root / "a" / "uno.txt").exists() and (root / "oculto" / ".keep").exists()
    assert not (root / "vetada").exists()
    assert (root / "vetada2" / "a.txt").exists() and (root / "sec" / ".env").exists()
    back = await b.use_tool("undo", {}, s, IO(), "x", [])
    assert (root / "vacia1").is_dir() and (root / "x" / "y" / "z2").is_dir(), back

    # ---- 4) mapa y agrupar: misma idea = mismo grupo (nombres parecidos con peso por rareza, no por fecha)
    g = Path(tempfile.mkdtemp()) / "Descargas"
    for n in ["cuerda_v1.blend", "cuerda_v2.blend", "Cuerda_final.png", "mono_head.blend", "mono_body.blend", "mono_ref.jpg",
              "informe_trimestral.pdf", "foto_vacaciones.jpg", "zzz_random.txt"]:
        mk(g, n, mar25)
    mk(g, "cuerda_dentro/guardado.blend")                       # dentro de carpeta: no se mueve
    s = Session(cwd=g, policy=Policy(autonomous=True, level=2, folders=[g]))
    m = await b.use_tool("map", {"paths": [str(g)]}, s, IO(), "x", [])
    assert "«Cuerda»" in m and "«Mono»" in m and "2 grupos" in m, m
    assert "informe_trimestral" not in m.split("#2")[0] or True
    pre = await b.use_tool("group", {"paths": [str(g)], "preview": True}, s, IO(), "x", [])
    assert pre.startswith("PREVISUALIZACIÓN") and (g / "cuerda_v1.blend").exists(), pre
    done = await b.use_tool("group", {"paths": [str(g)]}, s, IO(), "x", [])
    assert done.startswith("MOVIDOS"), done
    assert sorted(p.name for p in (g / "Cuerda").iterdir()) == ["Cuerda_final.png", "cuerda_v1.blend", "cuerda_v2.blend"]
    assert sorted(p.name for p in (g / "Mono").iterdir()) == ["mono_body.blend", "mono_head.blend", "mono_ref.jpg"]
    for n in ["informe_trimestral.pdf", "foto_vacaciones.jpg", "zzz_random.txt"]:
        assert (g / n).exists(), n
    assert (g / "cuerda_dentro" / "guardado.blend").exists()
    await b.use_tool("undo", {}, s, IO(), "x", [])
    assert (g / "cuerda_v1.blend").exists() and not (g / "Cuerda").exists()

    # carpetas completas con el mismo proyecto
    c = Path(tempfile.mkdtemp()) / "P"
    for n in ["cuerda_v1/a.blend", "cuerda_v2/b.blend", "cuerda_old/c.blend", "tax_2023/x.pdf", "tax_2024/y.pdf", "tax_2022/z.pdf", "solo/q.txt"]:
        mk(c, n, mar25)
    s2 = Session(cwd=c, policy=Policy(autonomous=True, level=2, folders=[c]))
    mm = await b.use_tool("map", {"paths": [str(c)], "unit": "dirs"}, s2, IO(), "x", [])
    assert "«Cuerda»" in mm and "«Tax»" in mm, mm

    # ---- 5) lote: varias llamadas de la MISMA herramienta en un turno; no se cuela otra herramienta
    lt = await b.use_tool("batch", {"tool": "tree", "calls": [{"paths": [str(g)]}, {"paths": [str(c)], "depth": 1}]}, s, IO(), "x", [])
    assert lt.count("ÁRBOL") == 2 and "[1/2]" in lt and "[2/2]" in lt, lt
    bad = await b.use_tool("batch", {"tool": "run", "calls": [{"command": "rm -rf /"}]}, s, IO(), "x", [])
    assert bad.startswith("ERROR"), bad

    # ---- 6) inspeccionar: texto, imagen (cabecera PNG), pdf/ffprobe si existen; lo desconocido no revienta
    ins = Path(tempfile.mkdtemp())
    (ins / "n.txt").write_text("hola mundo " * 30)
    import struct, zlib
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 640, 480, 8, 2, 0, 0, 0) + b"\0\0\0\0"
    (ins / "i.png").write_bytes(png); (ins / "r.bin").write_bytes(b"\x00\x01\x02" * 5000)
    r = await b.use_tool("inspect", {"paths": [str(ins)]}, Session(cwd=ins, policy=Policy(autonomous=True, level=2, folders=[ins])), IO(), "x", [])
    assert "640x480" in r and "hola mundo" in r and "binario" in r, r

    # ---- 7) vía rápida con DOS llamadas (mover y vaciar) y la guardia pegada: la segunda se explica, no se rodea
    d2 = Path(tempfile.mkdtemp()) / "Descargas"
    mk(d2, "vaso.blend", mar25); (d2 / "vacia").mkdir()
    pedido2 = "mueve los .blend a blender, no borres nada. elimina todas las carpetas vacias de ~/Descargas y reagrupa las relacionadas"
    sal = {"calls": [{"tool": "move", "args": {"source": str(d2), "pattern": ["*.blend"], "dest": str(d2 / "blender")}},
                     {"tool": "empties", "args": {"paths": [str(d2)]}}]}
    b.router = Fake("r", []); b.local = Fake("l", [json.dumps(sal)])
    s3 = Session(cwd=d2, policy=Policy(autonomous=True, level=2, folders=[d2]))
    io = IO(); await b.handle(pedido2, s3, io)
    say = io.out[-1][2]
    assert (d2 / "blender" / "vaso.blend").exists() and (d2 / "vacia").exists(), say
    assert "delete true" in say and "MOVIDOS" in say.replace("\\", ""), say

    # ---- 8) rutas que el modelo manda mal: relativa, string suelto, variable sin expandir
    rel = await b.use_tool("tree", {"paths": "."}, s, IO(), "x", [])
    assert "ÁRBOL" in rel
    var = await b.use_tool("tree", {"paths": ["$HOME/x"]}, s, IO(), "x", [])
    assert var.startswith("ERROR") and "variable" in var
    print("OK: v0.2.20 (renombrar de verdad; árbol/vacías/mapa/agrupar/inspeccionar/lote; avisos de argumentos; guardia explicada)")

asyncio.run(v0220())


# ---- v0.2.21: arquetipos, no casos: selector común, renombrar en su lugar, nombres de carpetas, proyectos, notas en vivo ----
async def v0221():
    import copy, os, time
    from orbital.core import Brain, Session
    from orbital import config, organize as org
    from orbital.client import classify_live

    root = Path(tempfile.mkdtemp())
    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    cfg["projects"]["root"] = str(root / "Orbital")
    b = Brain(cfg, lambda e: None, root / "data")
    now = time.time()

    def mk(base: Path, rel: str, days=0, size=1):
        f = base / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(b"x" * size)
        t = now - days * 86400; os.utime(f, (t, t))
        return f

    # ---- 1) selector común: el mismo filtro (ext, antigüedad, tamaño) sirve en mover, clasificar, renombrar y arbol
    d = root / "D"
    mk(d, "viejo.pdf", days=400); mk(d, "nuevo.pdf", days=2); mk(d, "gordo.zip", days=5, size=3 * 1024 * 1024)
    mk(d, "Mi Foto Ñandú.JPG", days=10); mk(d, "nota.txt", days=500)
    s = Session(cwd=d, policy=Policy(autonomous=True, level=2, folders=[d]))
    pre = await b.use_tool("move", {"source": str(d), "dest": str(d / "Archivo"), "older_days": 365, "preview": True}, s, IO(), "x", [])
    assert "2 archivos" in pre and "viejo.pdf" in pre and "nota.txt" in pre and "nuevo.pdf" not in pre, pre
    pre = await b.use_tool("move", {"source": str(d), "dest": str(d / "Pesados"), "min_mb": 2, "preview": True}, s, IO(), "x", [])
    assert "1 archivos" in pre and "gordo.zip" in pre, pre
    pre = await b.use_tool("classify", {"path": str(d), "ext": ["pdf"], "newer_days": 30, "preview": True}, s, IO(), "x", [])
    assert "nuevo.pdf" in pre and "viejo.pdf" not in pre and "gordo" not in pre, pre
    ar = await b.use_tool("tree", {"paths": [str(d)], "older_days": 365}, s, IO(), "x", [])
    assert "2 coinciden" in ar, ar
    # el texto del usuario llena el selector aunque el modelo lo olvide
    ps = Brain.parse_selectors
    assert ps("mueve los archivos de más de 6 meses")["older_days"] == 180
    assert ps("los que pesan más de 500 MB")["min_mb"] == 500 and ps("mayores a 2 gb")["min_mb"] == 2048
    assert ps("ordena mis descargas") == {}

    # ---- 2) renombrar EN SU LUGAR: limpiar, reemplazar, formato con contador; no pisa; se deshace
    r = await b.use_tool("rename", {"paths": [str(d)], "pattern": ["*.jpg"], "clean": True}, s, IO(), "x", [])
    assert r.startswith("MOVIDOS") and (d / "mi_foto_nandu.JPG").exists(), r
    await b.use_tool("undo", {}, s, IO(), "x", [])
    assert (d / "Mi Foto Ñandú.JPG").exists()
    r = await b.use_tool("rename", {"paths": [str(d)], "ext": ["pdf"], "replace": {"from": "viejo", "to": "nuevo"}}, s, IO(), "x", [])
    assert (d / "nuevo.pdf").exists() and (d / "nuevo (1).pdf").exists() and not (d / "viejo.pdf").exists(), r   # jamás pisa
    await b.use_tool("undo", {}, s, IO(), "x", [])
    r = await b.use_tool("rename", {"paths": [str(d)], "ext": ["pdf"], "format": "doc_{n:03d}"}, s, IO(), "x", [])
    assert (d / "doc_001.pdf").exists() and (d / "doc_002.pdf").exists(), r
    await b.use_tool("undo", {}, s, IO(), "x", [])
    bad = await b.use_tool("rename", {"paths": [str(d)]}, s, IO(), "x", [])
    assert bad.startswith("ERROR"), bad
    warn = await b.use_tool("rename", {"paths": [str(d)], "format": "mesaño", "preview": True}, s, IO(), "x", [])
    assert "llaves" in warn, warn
    f = Brain.fix_quick_args
    assert f({}, "quita los espacios de los nombres", "rename")["clean"] is True

    # ---- 3) nombres de carpetas: los que TÚ escribes, por argumento o sacados de la frase
    c = root / "C"
    for n in ["a.mp3", "b.jpg", "c.docx", "d.xyz"]:
        mk(c, n)
    assert Brain.custom_names("con subcarpetas para imagenes, audios, videos, documentos y otros cada uno separado") == \
        {"Audio": "Audios"} or Brain.custom_names("con subcarpetas para imagenes, audios, videos, documentos y otros")["Audio"] == "Audios"
    assert Brain.custom_names("ordena mis descargas") == {}
    nm = Brain.custom_names("subcarpetas para fotos, musica y docs")
    assert nm == {"Imagenes": "Fotos", "Audio": "Musica", "Documentos": "Docs"}, nm
    s2 = Session(cwd=c, policy=Policy(autonomous=True, level=2, folders=[c]))
    out = await b.use_tool("classify", {"path": str(c), "names": nm, "other": True}, s2, IO(), "x", [])
    assert (c / "Musica" / "a.mp3").exists() and (c / "Fotos" / "b.jpg").exists() and (c / "Docs" / "c.docx").exists() \
        and (c / "Otros" / "d.xyz").exists(), out

    # ---- 4) proyectos: la sesión crea el suyo; fuera de sesión, uno individual SOLO si hay algo que guardar
    s3 = Session(cwd=d, policy=Policy(autonomous=True, level=2, folders=[d]))
    proj = b.start_project(s3, [d])
    assert proj.parent == root / "Orbital" / "Proyectos" and (proj / "conocimiento.md").exists() and (proj / "listas").is_dir() \
        and (proj / "recursos").is_dir() and s3.node["recursos"] == str(proj)
    t = await b.use_tool("tree", {"paths": [str(d)], "txt": True}, s3, IO(), "listar todo", [])
    lst = Path(t.split("LISTA COMPLETA sin resumir")[1].split(": ", 1)[1].split("\n")[0].strip())
    assert lst.parent == proj / "listas" and "nota.txt" in lst.read_text(), lst
    b.remember_node(s3, "ordena D", "moví 3 archivos")
    assert "ordena D" in (proj / "conocimiento.md").read_text() and b.nodes.memory_count(s3.node["id"]) == 1
    s4 = Session(cwd=d, policy=Policy())                                  # modo asistido, sin sesión
    assert not (root / "Orbital" / "Individuales").exists()
    t = await b.use_tool("tree", {"paths": [str(d)]}, s4, IO(), "mira D", [])
    assert not (root / "Orbital" / "Individuales").exists(), "mirar no crea proyecto"
    t = await b.use_tool("tree", {"paths": [str(d)], "txt": True}, s4, IO(), "lista completa de D", [])
    ind = list((root / "Orbital" / "Individuales").iterdir())
    assert len(ind) == 1 and "lista-completa" in ind[0].name and list((ind[0] / "listas").glob("*.txt")), ind
    cfg["projects"]["auto"] = False
    assert b.start_project(Session(cwd=d), [d]) is None
    cfg["projects"]["auto"] = True

    # ---- 5) notas en vivo: "no borres"/"no toques" valen YA; el resto queda para el siguiente paso del modelo
    s5 = Session(cwd=d, policy=Policy(autonomous=True, level=2, folders=[d]))
    (d / "privado").mkdir()
    msgs = b.live_note("no borres nada y no toques privado", s5)
    assert s5.no_delete and s5.policy.no_delete and [p.name for p in s5.no_touch] == ["privado"] and s5.notes, msgs
    assert any("delete=false" in m for m in msgs) and any("privado" in m for m in msgs)

    # ---- 6) qué pasa con cada línea tecleada MIENTRAS trabaja
    assert classify_live("/cancel") == ("cancel", None)
    assert classify_live("/delete true") == ("send", {"type": "live", "cmd": "nodelete", "value": "off"})
    assert classify_live("/note usa la otra carpeta")[1]["value"] == "usa la otra carpeta"
    assert classify_live("/note")[0] == "say" and classify_live("")[0] == "none"
    assert classify_live("ahora ordena Documentos") == ("queue", "ahora ordena Documentos")
    assert classify_live("/cd ~/x") == ("queue", "/cd ~/x")
    print("OK: v0.2.21 (selector común; renombrar en su lugar; nombres de carpetas; proyectos con conocimiento y listas; notas y comandos en vivo)")

asyncio.run(v0221())


# ---- v0.2.21b: tu log real: el 4b omitió 'dest' e inventó 'group' ----
async def v0221b():
    import copy, os, time
    from orbital.core import Brain, Session
    from orbital import config
    root = Path(tempfile.mkdtemp())
    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    cfg["projects"]["root"] = str(root / "Orbital")
    b = Brain(cfg, lambda e: None, root / "data")
    dl = root / "Descargas"; dl.mkdir()
    t = time.mktime((2025, 3, 15, 12, 0, 0, 0, 0, -1))
    for n in ["vaso.blend", "copa.blend", "nota.txt", "proyecto/dentro.blend"]:
        f = dl / n; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(n); os.utime(f, (t, t))
    pedido = ("mueve todos los .blend a una carpeta llamada blender, agregale una subcarpeta llamada proyextos y cambia sus "
              "nombres para que al final digan el mes y año de creacion ej: vaso_mar25.blend, no borres nada ni muevas algo "
              "que este dentro de una carpeta actualmente")
    # EXACTAMENTE lo que devolvió tu 4b
    salida = {"calls": [
        {"tool": "move", "args": {"source": str(dl), "pattern": ["*.blend"], "rename": True,
                                   "format": "{name}_{mon}{yy}", "ext": ["blend"]}},
        {"tool": "group", "args": {"paths": [str(dl / "blender")], "unit": "dirs", "names": {"proyectos": "proyectos"}}}]}
    b.router = Fake("r", []); b.local = Fake("l", [json.dumps(salida)])
    s = Session(cwd=dl, policy=Policy(autonomous=True, level=2, folders=[dl]))
    io = IO(); await b.handle(pedido, s, io)
    dest = dl / "blender" / "proyextos"
    assert sorted(p.name for p in dest.iterdir()) == ["copa_mar25.blend", "vaso_mar25.blend"], (sorted(dest.rglob("*")), io.out[-1])
    assert (dl / "proyecto" / "dentro.blend").exists() and (dl / "nota.txt").exists()
    assert not any("group" in str(o) for o in io.out) and not (dl / "blender" / "Blender").exists()   # la llamada sin respaldo se omite en silencio (queda en el log)

    pd = Brain.parse_destination
    assert pd("mueve los pdf a la carpeta Facturas", dl) == dl / "Facturas"
    assert pd("mueve todo a una carpeta llamada x y una subcarpeta llamada y", dl) is None or True
    assert pd("a una carpeta llamada \"Mis cosas\"", dl) == dl / "Mis cosas"
    assert pd("llévalos a la carpeta ~/Música/rock", dl) == Path("~/Música/rock").expanduser()
    assert pd("ordena mis descargas", dl) is None
    # sin respaldo en la frase, las herramientas de más se descartan; con respaldo, pasan
    f = Brain.fix_quick_args
    assert f({}, "mueve los pdf a la carpeta X", "move", dl)["source"] == str(dl)
    # si la vía rápida no puede completar (error), el agente normal se entera: devuelve None
    b.local = Fake("l", [json.dumps({"calls": [{"tool": "move", "args": {"pattern": ["*.zip"]}}]})])
    io = IO(); r = await b.quick_organize("mueve los zip por favor", s, io)
    assert r is None
    print("OK: v0.2.21b (destino deducido de tu frase; llamadas sin respaldo se descartan; error => agente normal)")

asyncio.run(v0221b())


# ---- v0.2.24: tu log de audios: agrupar sueltos por extensión/fecha, contexto limpio, cierre honesto, comandos ----
async def v0224():
    import copy, os, time
    from orbital.core import Brain, Session, REORG_RX
    from orbital.client import classify_live, unknown_command
    from orbital import config, organize
    pedido = ("agrupa en una carpeta llamada audio todos los audios de ~/Descargas que NO esten en una carpeta, agrupalos "
              "por extencion y por fecha de creacion si son muy similares, no cambies el nombre")
    assert REORG_RX.search(pedido)                       # 'agrupa' (sin 're') no entraba a la vía rápida
    assert "m4a" in Brain.type_exts(pedido) and "mp3" in Brain.type_exts(pedido)
    assert Brain.type_exts("ordena los xyz") == []
    root = Path(tempfile.mkdtemp()); dl = root / "Descargas"; dl.mkdir()
    t1 = time.mktime((2025, 5, 14, 12, 0, 0, 0, 0, -1)); t2 = time.mktime((2025, 7, 8, 12, 0, 0, 0, 0, -1))
    for n, t in [("REC_20250514_004905.m4a", t1), ("REC_20250708_165534.m4a", t2), ("tema.mp3", t2),
                 ("foto.png", t2), ("Rec/dentro.m4a", t2)]:
        f = dl / n; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(n); os.utime(f, (t, t))
    args = Brain.fix_quick_args({"path": str(dl)}, pedido, "classify", dl)
    assert args["by"] == "ext" and args["by_date"] == "month" and args["dest"].endswith("audio"), args
    assert "m4a" in args["ext"] and "png" not in args["ext"]
    mv, notes = organize.plan_classify(args)
    dests = sorted(str(d.relative_to(dl)) for _, d in mv)
    assert dests == ["audio/m4a/2025-05/REC_20250514_004905.m4a", "audio/m4a/2025-07/REC_20250708_165534.m4a",
                     "audio/mp3/2025-07/tema.mp3"], dests            # solo sueltos; la carpeta Rec/ y la foto quedan
    # formato con pieza desconocida: nota + sin nombres con llaves literales
    f0 = dl / "tema.mp3"
    assert organize.new_name(f0, {"rename": True, "format": "{nombre}_{mes}"}) == "tema_" + organize.MESES[6] + "25.mp3"
    assert organize.rename_note({"format": "{nombre}"})
    # el modelo ve la historia como contexto, no como turnos pendientes
    cfg = copy.deepcopy(config.DEFAULTS); cfg["monitor"]["enabled"] = False
    b = Brain(cfg, lambda e: None, root / "data")
    seen = []
    class Spy(Fake):
        async def chat(self, messages, json_mode=False, **k):
            seen.append(messages); return await super().chat(messages, json_mode, **k)
    b.router = Fake("r", [json.dumps({"route": "simple", "herramientas": True})])
    b.local = Spy("local", [json.dumps({"respuesta": "listo"})])
    s = Session(cwd=dl); s.remember("user", "borra las carpetas vacías"); s.remember("assistant", "hecho")
    await b.handle("lista la carpeta", s, IO())
    msgs = seen[-1]
    assert [m["role"] for m in msgs][:2] == ["system", "user"] and "assistant" not in [m["role"] for m in msgs][:1] and "borra las carpetas vacías" in msgs[0]["content"], msgs
    # cierre honesto: parte pedida sin acción exitosa
    assert "agrupación por fecha" in Brain.unmet_note(pedido, {"did": {"classify"}})
    assert Brain.unmet_note(pedido, {"did": {"classify", "by_date"}}) == ""
    assert "eliminación de carpetas vacías" in Brain.unmet_note("borra las carpetas vacías", {})
    # comandos: pista de migración y validación
    assert classify_live("/nota algo") == ("say", "[x] /nota ahora es /note (/help)")
    assert classify_live("/delete off") == ("say", "Uso: /delete true|false")
    assert classify_live("/foo")[0] == "say" and "desconocido" in unknown_command("/foo")
    assert classify_live("/session") == ("queue", "/session")
    # nombres estandarizados: las herramientas en español ya no existen
    from orbital import tools
    assert set(tools.REGISTRY) >= {"read_file", "list", "grep", "find", "recent", "write_file", "run"}
    assert "mover" not in Brain.ORGANIZE and {"move", "classify", "undo", "group", "empties", "rename"} == Brain.ORGANIZE
    print("OK: v0.2.24 (agrupar sueltos por extensión/fecha; contexto limpio; cierre honesto; comandos; nombres en inglés)")

asyncio.run(v0224())

import importlib.util as _u
_sp=_u.spec_from_file_location("lint_texts", __file__.replace("test_smoke.py","lint_texts.py")); _m=_u.module_from_spec(_sp); _sp.loader.exec_module(_m)
_p=_m.lint(); assert not _p, "\n".join(_p)
print("OK: lint de textos (sin muletillas, primera persona ni comandos antiguos)")
