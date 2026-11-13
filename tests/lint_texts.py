"""Lint del texto visible: sin muletillas, sin primera persona ni 'no soy capaz', sin tuteo.

Reglas de estilo (README, sección «Estilo de los mensajes»):
  - Hechos, no capacidades: «No se puede X», nunca «no puedo/no soy capaz».
  - Infinitivo o impersonal; ninguna conjugación en primera o segunda persona.
  - Sin muletillas de desarrollo («ahora sí», «OJO», «no lo pediste», «lo peligroso…»).
Alcance: cadenas que llegan al usuario o al registro: status/say/send/confirm/choose/note/print, valores de retorno,
listas de avisos (append). Los prompts dirigidos al modelo y los patrones regex quedan fuera.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "orbital"
FILES = ["client", "daemon", "core", "organize", "survey", "permissions", "tools", "memory", "monitor", "medir", "nodes"]
SINKS = {"status", "say", "confirm", "choose", "relay", "note", "print", "append", "info"}
OLD_IDS = re.compile(r"\b(leer_archivo|escribir_archivo|pedir_ayuda_humano|previsualizar|por_fecha|min_grupo|max_archivos|"
                     r"mas_de_dias|menos_de_dias|inspeccionar|reemplazar|/noborrar|/deshacer|/ayuda|/sesion)\b|\"ruta\"|\"destino\"|\"origen\"")
SKIP_NAMES = {"AGENT_PROMPT", "CHAT_PROMPT", "PLAN_PROMPT", "QUICK_PROMPT", "RELAY_TEMPLATE", "TOOL_DOCS", "ROUTER_PROMPT"}

BAD = [
    (r"\bahora s[ií]\b", "muletilla"), (r"\bOJO\b", "muletilla"), (r"no lo pediste", "muletilla"),
    (r"\bLinux no\b", "muletilla"), (r"lo peligroso", "muletilla"), (r"rendi", "muletilla"),
    (r"\bno (puedo|pude|soy capaz|logré|he podido|sé|estoy)\b", "capacidad en primera persona"),
    (r"\b(moví|muevo|elimino|renombro|creé|cerré|descargué|taché|liberé|armo|pauso|retomo|reanudo|"
     r"reintento|uso el|sigo|voy a|te paso|te pido|le pido|doy la|dejo|escalé|bajé|subo a|bajo a|lo subo|"
     r"limpié|recorté|corté|apliqué|ajusté|descarto|intento hacer|intenté|decido|elijo|quité|consta que hice)\b", "primera persona"),
    (r"\b(dime|dame|escribe|usa|revisa|cierra|instala|haz|pon|lee|prueba|elige|avisa)\b(?! de)", "imperativo/tuteo"),
    (r"\b(pídele|pídeme|dile|asegúrate|mira|ten en cuenta)\b", "imperativo/tuteo"),
    (r"\b(me detuve|me quedé|me rindo|me encargo)\b", "primera persona"),
    (r"\b(tu|tus|tú|te)\b", "segunda persona"),
]
EXEMPT = re.compile(r"(\(\?|\\\\|\\w|\\b|\\s|\[\^|\{\{|^\s*$)")  # regex y plantillas


def strings(node):
    """Cadenas constantes (y partes constantes de f-strings) dentro de un nodo."""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.lineno, n.value


def collect(tree):
    skip_ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body and \
                isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            skip_ids.add(id(n.body[0].value))
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in SKIP_NAMES for t in n.targets):
            for c in ast.walk(n):
                skip_ids.add(id(c))
        if isinstance(n, ast.Dict):   # {"role": "user", "content": ...} es para el modelo
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in ("content", "system"):
                    for c in ast.walk(v):
                        skip_ids.add(id(c))
        if isinstance(n, ast.keyword) and n.arg in ("question", "use", "prompt"):
            for c in ast.walk(n.value):
                skip_ids.add(id(c))
    out = []
    for n in ast.walk(tree):
        nodes = []
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SINKS:
            nodes += list(n.args)
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == "text":
                    nodes.append(v)
        if isinstance(n, ast.Return) and n.value is not None:
            nodes.append(n.value)
        for nd in nodes:
            for ln, s in strings(nd):
                out.append((ln, s, id(nd)))
    # filtrar las excluidas
    ok = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and id(n) in skip_ids:
            ok.append((n.lineno, n.value))
    skipped = {x for x in ok}
    return [(ln, s) for ln, s, _ in out if (ln, s) not in skipped]


def old_ids() -> list[str]:
    """Identificadores en español (herramientas, argumentos, comandos) en cualquier cadena del código."""
    out = []
    for name in FILES:
        for n in ast.walk(ast.parse((ROOT / f"{name}.py").read_text())):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                m = OLD_IDS.search(n.value)
                if m:
                    out.append(f"{name}.py:{n.lineno} [identificador antiguo] «{m.group(0)}»")
    return out


def lint() -> list[str]:
    problems = old_ids()
    for name in FILES:
        path = ROOT / f"{name}.py"
        for ln, s in collect(ast.parse(path.read_text())):
            if len(s) < 4 or EXEMPT.search(s) or not re.search(r"[A-Za-záéíóú]{3}", s):
                continue
            for rx, why in BAD:
                m = re.search(rx, s, re.I if why != "muletilla" or "OJO" not in rx else 0)
                if m:
                    problems.append(f"{name}.py:{ln} [{why}] «{m.group(0)}» en: {s[:90]!r}")
                    break
    return problems


if __name__ == "__main__":
    probs = lint()
    print("\n".join(probs) if probs else "OK lint")
    sys.exit(1 if probs else 0)
