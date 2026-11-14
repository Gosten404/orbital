# O.R.B.I.T.A.L · v0.2.27

Asistente local para Linux (X11). Requiere Python 3.10 o superior. Una petición pasa por un enrutador local que decide a quién mandarla:

| Ruta | Destino | Ejemplos |
|---|---|---|
| simple | `qwen3.5:4b` local | renombrar, listar, comandos obvios, tareas con instrucciones exactas |
| medium | Gemini Flash | explicar temas, revisar/editar código, depurar |
| heavy | **Tú → Claude** (relevo humano) | arquitectura, bugs difíciles, razonamiento largo |

En la ruta *heavy*, O.R.B.I.T.A.L arma un prompt completo y lo copia al portapapeles. Tú lo pegas en tu chat con Claude, pegas la respuesta de vuelta en la terminal y terminas con `FIN`. Si la tarea necesita herramientas, O.R.B.I.T.A.L toma esa respuesta como plan y lo ejecuta con Gemini.

## Arquitectura

```
orbital (TUI) ──socket Unix──> orbitald (servicio systemd de usuario)
                                 ├─ enrutador  qwen3.5:2b (Ollama)
                                 ├─ local      qwen3.5:4b (Ollama)
                                 ├─ medium     Gemini (REST)
                                 ├─ heavy      relevo humano
                                 ├─ herramientas: leer, listar, buscar, escribir, ejecutar
                                 ├─ permisos por sesión
                                 └─ log JSONL  ~/.local/share/orbital/logs/
```

Cerrar la terminal **no** apaga a O.R.B.I.T.A.L. En la v0.3 la voz se conecta a este mismo daemon.

## Instalación y actualización

Requisitos, una sola vez:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:2b
ollama pull qwen3.5:4b
ollama pull qwen3.5:9b      # opcional: solo se usa en tareas difíciles y si hay RAM
sudo apt install xclip
```

Luego, cada vez que instales o actualices:

```bash
cd ~ && unzip -o orbital.zip      # descomprime ENCIMA de la carpeta: así se conserva el .venv
cd ~/orbital && bash instalar.sh
```

`instalar.sh` hace lo siguiente:
- crea o reutiliza el entorno virtual e instala el paquete;
- crea los comandos `orbital` y `orbitald`;
- detiene y elimina el servicio viejo `jarvisd`;
- migra tu API key, tus logs y tu memoria desde `~/.config/orbital` y `~/.local/share/orbital`;
- instala y arranca el servicio `orbitald`;
- avisa si te falta algún modelo.

La API key va en `~/.config/orbital/env` (`GEMINI_API_KEY=...`) y los ajustes en `~/.config/orbital/config.toml`.

Para usarlo, escribe `orbital`. Logs del daemon: `journalctl --user -u orbitald -f`.

## Permisos

- **Modo asistido** (por defecto): leer es libre; escribir y ejecutar siempre te piden confirmación.
- **`/sesion`**: eliges el nivel, las carpetas y el máximo de pasos antes de trabajar en autónomo.
  - 0: solo lectura
  - 1: lectura y escritura en las carpetas permitidas
  - 2: lo anterior y ejecutar comandos en esas carpetas
  - 3: ejecutar en cualquier ruta
- Los comandos peligrosos (`sudo`, `rm -rf`, `dd`, `curl | sh`, `push --force`…) **siempre** preguntan, aun en nivel 3.
- Las rutas secretas (`~/.ssh`, `~/.gnupg`, `.env`, `*.pem`…) nunca se leen, para que no terminen en un prompt hacia Gemini.
- Antes de reemplazar un archivo, O.R.B.I.T.A.L guarda un respaldo `archivo.orbital.bak`.

## Lo que ves en cada paso

```
· carga [###.......] 34% · CPU 34% · RAM 61% (6.1 GB libres) · swap 0%
· [=] paso 2/15 · local:qwen3.5:4b pensando…
·    [i] por qué: necesito ver el config antes de editarlo  (11.4 s)
·    [>] leer_archivo /home/tú/.config/orbital/config.toml
·    [<] [identity] | name = "O.R.B.I.T.A.L" | ...
```

## Saturación de la PC

Antes de cada paso, O.R.B.I.T.A.L mide la CPU, la RAM y el swap:
0. Antes de molestarte, descarga de la RAM los modelos de Ollama que no va a usar.
1. **≥ 85 %**: espera 8 s. Si la carga no baja, te ofrece cerrar las apps más pesadas (cierre amable con SIGTERM; solo las que tú elijas).
2. **≥ 95 %**: pausa el razonamiento 30 s y vuelve a medir, hasta 3 veces.
3. **Si sigue crítica**, como último recurso te pregunta qué hacer: bajar la intensidad (qwen 2B con poco contexto), mandar la tarea a Gemini, seguir igual o detenerla.

**Mientras qwen genera**, un vigilante revisa cada 2 s la RAM y el swap (la CPU no cuenta, porque durante la inferencia siempre está al 100 % por diseño). Si la RAM llega al nivel crítico o el swap crece más de 400 MB, corta la generación, libera RAM, espera 30 s y reintenta sin gastar un paso. Si se repite más de 3 veces, pasa al último recurso.

El contexto de cada modelo está limitado (`router_num_ctx = 2048`, `local_num_ctx = 8192`), y los resultados viejos de herramientas se recortan en cada paso. Sin esos dos límites, el prompt crece paso a paso hasta llenar la RAM, que es justo lo que congeló la PC en el paso 15.

Los umbrales se ajustan en `[monitor]` del config.

## Memoria de tareas

Cada tarea con herramientas escribe una bitácora en `~/.local/share/orbital/memoria/`, actualizada en cada paso: por qué hizo cada cosa, qué hizo y qué obtuvo. `ULTIMA.md` siempre apunta a la más reciente. Al arrancar te dice en qué estado quedó la última tarea, y con `/retomar` la carga para continuar.

## Protección de su propio código

O.R.B.I.T.A.L **no puede** escribir en su carpeta de código ni ejecutar comandos que la toquen (solo puede leerla con `ls`, `cat`, `grep` o `git log/diff/status`), ni siquiera en nivel 3. Si se modifica a sí mismo, se rompe. Su nombre y sus ajustes van en `~/.config/orbital/config.toml`.

## v0.2.25: actualizar, publicar y base de voz

**Actualizar** (en la PC de uso): `orbital update`. Hace `git fetch`, `merge --ff-only`, reinstala, comprueba que todo importa y reinicia `orbitald`. Si algo falla después de bajar el código, vuelve solo a la versión anterior.
`orbital update --check` solo informa si hay versión nueva · `--rollback` vuelve a la anterior · `--full` corre además la suite de pruebas · `--no-restart` no reinicia el daemon · `orbital --version`.
Se niega a actualizar con cambios locales sin guardar. Requiere que la instalación sea un clon de git (el que usa `instalar.sh`).

**Publicar** (en la PC de desarrollo): `python3 tools/release.py 0.2.26`. Exige árbol limpio, corre pruebas y lint, sube la versión (`pyproject.toml`, `orbital/__init__.py`, título del README), hace commit y tag `v0.2.26` y empuja. `--no-push` deja el commit y el tag en local. El título del cliente ya sale de `__version__`.

**Voz (base de v0.3)**: `/voice on|off`. El daemon emite eventos breves; el cliente los pasa a un motor.
- `start`: «Tarea iniciada.» · `help` (confirmación, elección o relevo): «Se requiere intervención. …» · `error` · `done`.
- El texto sale de plantillas y de limpiar la respuesta (sin markdown, código, URLs ni rutas). Las cifras de move/classify/undo se leen directo del resultado («160 archivos movidos a 1 carpetas») sin modelo.
- El 2b resume **una sola vez, al terminar**, y solo si la respuesta es larga. Sin resumen por paso: el 4b ya ocupa la CPU. Si el 2b no responde en `summary_timeout`, se recorta el texto.
- Con la voz activa, el prompt vuelve cuando sale el último evento (el cliente solo lee mensajes durante la tarea).
- Motores: `[voice] backend = "print"` muestra `[voz:done] …`; `"none"` no hace nada. Un motor de audio se agrega como clase con `async speak(text, kind)` en `voice.BACKENDS`.

Incluye la licencia GPL-3.0-or-later (`LICENSE`, aviso al final del README).

## v0.2.24: identificadores en inglés y cuatro fallos del log de audios

Herramientas, argumentos y piezas de formato pasan a inglés (la interfaz sigue en español). Corte limpio: los nombres anteriores ya no existen.

| Herramientas | |
|---|---|
| `leer_archivo` `listar` `recientes` `encontrar` `buscar` `escribir_archivo` `ejecutar` | `read_file` `list` `recent` `find` `grep` `write_file` `run` |
| `clasificar` `mover` `renombrar` `agrupar` `vacias` `deshacer` | `classify` `move` `rename` `group` `empties` `undo` |
| `arbol` `inspeccionar` `mapa` `lote` `pedir_ayuda_humano` | `tree` `inspect` `map` `batch` `ask_human` |

| Argumentos | |
|---|---|
| `ruta` `rutas` `origen` `destino` `patron` `contenido` `comando` | `path` `paths` `source` `dest` `pattern` `content` `command` |
| `filtro` `excluir` `previsualizar` `profundidad` `ocultos` `horas` `umbral` | `filter` `exclude` `preview` `depth` `hidden` `hours` `threshold` |
| `por_fecha` (`mes`/`año`) `unidad` (`archivos`/`carpetas`) `solo` (`carpetas`/`vacias`) | `by_date` (`month`/`year`) `unit` (`files`/`dirs`) `only` (`dirs`/`empty`) |
| `desde` `hasta` `mas_de_dias` `menos_de_dias` `min_grupo` `max_archivos` | `since` `until` `older_days` `newer_days` `min_group` `max_files` |
| `formato` `reemplazar {de, por}` `limpiar` `renombrar` `nombres` `reglas` `otros` | `format` `replace {from, to}` `clean` `rename` `names` `rules` `other` |
| piezas `{nombre} {mes} {aa} {anio}` | `{name} {mon} {yy} {year}` (`{ext} {mm} {dd} {n}` igual) |

Fallos corregidos (log de «agrupar audios sueltos»):
- **«agrupa» no entraba a la vía rápida**: la expresión exigía «reagrupa». Ahora entran `agrupa`, `junta`, `mete`, `pasa`.
- **Nuevo `classify.by: "ext"`**: una subcarpeta por extensión (`audio/m4a/`, `audio/mp3/`) en vez de por tipo; con `by_date` anida `2025-07/`. «Por extensión/extencion» y «por fecha/mes/año» se detectan sin modelo. «Todos los audios/fotos/videos…» se traduce a extensiones sin modelo.
- **Historial como contexto**: las tareas anteriores van en el system prompt como «ya terminadas», no como turnos de chat; el 4b retomaba una petición vieja (carpetas vacías) como pendiente.
- **Cierre con hechos**: si la petición pedía fecha, similitud, renombrado o eliminar vacías y no hubo ninguna acción exitosa de eso, la respuesta añade «Sin ejecutar: …».
- **Formato con piezas desconocidas** (`{nombre}`) ya no deja llaves literales en los archivos: avisa y usa el formato por defecto.
- **Comandos**: `/nota` responde «ahora es /note»; los comandos con `/` desconocidos no se encolan como petición; `/delete x` inválido muestra el uso.
- Salida más limpia: paréntesis explicativos de parche eliminados; el lint (`tests/lint_texts.py`) también falla ante identificadores antiguos.

## v0.2.23: textos y comandos estandarizados

Estilo de los mensajes: hechos en infinitivo o impersonal («No se puede X»), sin primera ni segunda persona, sin muletillas. `tests/lint_texts.py` (incluido en la suite) falla ante regresiones.

| Antes | Ahora |
|---|---|
| `/noborrar on\|off` | `/delete false\|true` (false = borrar prohibido) |
| `/deshacer` · `/ayuda` · `/sesion` · `/fin` | `/undo` · `/help` · `/session` · `/end` |
| `/estado` · `/retomar` · `/olvidar` | `/state` · `/resume` · `/forget` |
| `/modo` · `/modelo` · `/nodo` · `/tocar` | `/mode` (auto\|local\|gemini\|human) · `/model` · `/node` · `/touch clear` |
| `/sonido` · `/voz` · `/salir` · `/humano` | `/sound` · `/voice` · `/quit` · `/human` |
| en vivo: nota/cancelar | `/note` · `/cancel` |

Sin alias: los comandos antiguos responden «Comando desconocido (/help)». Los nombres de herramientas y los prompts al modelo siguen en español.

## v0.2.22: la vía rápida ya no depende de que el 4b llene todo

En tu log el 4b olvidó el `destino` ("a una carpeta llamada blender, subcarpeta proyextos") e inventó un `agrupar`.
Ahora, en código: el destino se deduce de tu frase (`blender/proyextos`, comillas, `~/ruta`), el origen por defecto es la
carpeta de trabajo, y una herramienta que la frase no pide (`agrupar`, `vacias`, `renombrar`) se descarta. Si la vía rápida
no puede completar la orden, pasa al agente normal en vez de mostrarte "nada que hacer".

## v0.2.21: piezas que se combinan, comandos mientras trabaja y proyectos en tu home

La idea de esta versión: no arreglar una petición concreta, sino el tipo de petición. Las herramientas comparten piezas.

**Selector común** (vale igual en `clasificar`, `mover`, `renombrar`, `agrupar`, `arbol`, `inspeccionar`):
`filtro`, `ext`, `desde`/`hasta`, `mas_de_dias`/`menos_de_dias`, `min_mb`/`max_mb`. "Los PDF de más de 6 meses", "lo que pesa
más de 500 MB": el código saca antigüedad y tamaño de tu frase aunque el modelo los olvide.

**Renombrar** es una herramienta propia (`renombrar`, en su lugar) y también una opción de `mover`/`clasificar`. Piezas:
`formato` (`{nombre} {mes} {aa} {anio} {mm} {dd} {n} {ext}`, p. ej. `foto_{n:03d}`), `reemplazar` `{de, por}`, `limpiar`
(minúsculas, sin acentos ni espacios). Nunca pisa un archivo y se deshace con `/deshacer`. Un formato sin llaves avisa.

**Nombres de carpetas**: `clasificar` acepta `nombres: {"Audio": "Audios"}`; si escribes "subcarpetas para fotos, música y
docs", se usan esos nombres tal cual.

**Escribir mientras trabaja** (línea `↳` bajo la salida; el progreso va en la barra inferior):
- `/cancelar` o Ctrl+C: corta la tarea.
- `/noborrar on|off`, `/tocar`, `/estado`: valen en ese instante.
- `/nota <texto>`: el modelo la lee en su siguiente paso; si dice "no borres" o "no toques X", aplica YA.
- Cualquier otra cosa queda en cola y corre al terminar (si cancelas, la cola se vacía).

**Proyectos** (`[projects] root = "~/Orbital"`, `auto = true`):
- Cada `/sesion` crea `~/Orbital/Proyectos/<fecha>_<carpeta>/` con `conocimiento.md`, `listas/` y `recursos/`, y su nodo de memoria.
- Fuera de sesión, lo que genera algo que guardar (una lista completa, un mapa) va a `~/Orbital/Individuales/<fecha>_<tarea>/`.
  Mirar o charlar no crea carpetas.
- Las listas completas sin resumir (`txt: true`) se guardan en `listas/` del proyecto. Cada tarea terminada añade una línea a `conocimiento.md`.

Sin probar con tu modelo: que el 4b elija `renombrar`/`nombres` por sí solo (hay red determinista para lo más común).

## v0.2.20: ver la jerarquía, entender qué va con qué, y renombrar de verdad

Lo que falló en tu corrida: `mover` **ignoraba en silencio** `renombrar`/`formato` (los `.blend` se movieron sin cambiar de
nombre), y "elimina carpetas vacías y reagrupa" no tenía herramienta, así que el agente dio 40 vueltas con `listar` y `ejecutar`.

Herramientas nuevas (todas calculan en código, en un hilo; el modelo solo decide qué pedir):

| herramienta | para qué |
|---|---|
| `arbol` | jerarquía de una o varias carpetas: archivos, tamaño, rango de fechas, `[VACÍA]`. Filtros a la vez general y particular (`["*.blend","cuerda"]`), `ext`, `desde`/`hasta`, `solo`, `txt: true` = lista COMPLETA sin resumir en `listas/` |
| `inspeccionar` | vistazo al contenido: texto, PDF (con `pdftotext`), imagen (tamaño leído de la cabecera), audio/video (duración y etiquetas con `ffprobe`), zip |
| `mapa` | agrupa lo que es "de la misma idea": nombres parecidos con peso por rareza (compartir `cuerda` pesa más que `final`), fechas cercanas, misma extensión y contenido de texto/PDF. Da cohesión por grupo y distancia entre grupos. 2 500 archivos en ~0.3 s |
| `agrupar` | mueve cada grupo del mapa a `destino/<Nombre>/` (archivos sueltos o carpetas completas), con previsualización y `/deshacer` |
| `vacias` | elimina SOLO carpetas sin ningún archivo en su subárbol. Un archivo oculto, un enlace, un secreto o algo vetado cuenta como contenido. `/deshacer` las recrea |
| `lote` | varias llamadas de la MISMA herramienta en un turno (`arbol`, `mapa`, `mover`, `buscar`…): un turno del modelo en CPU cuesta decenas de segundos |

Cambios en lo existente:
- `mover` ahora entiende `renombrar: true` (mes y año de la **última modificación**: Linux no guarda la creación de forma fiable;
  `vaso.blend` -> `vaso_mar25.blend`, no se repite si ya termina así), `formato` (`{nombre}_{mes}{aa}`), `excluir` y `filtro`.
- Un argumento que la herramienta no conoce ya no se ignora: sale un aviso `OJO: ignoré argumentos...`.
- La vía rápida acepta VARIAS llamadas en orden (mover + vaciar + agrupar) en una sola respuesta del modelo.
- Si pediste "no borres" y luego "elimina carpetas vacías", `vacias` se rechaza con el motivo y `/noborrar off`; la previsualización sí funciona. El agente tiene instrucción de no rodearlo con `ejecutar`.
- `arbol`/`mapa` respetan secretos, tu código y los vetos "no toques".

Límites honestos: no interpreta el contenido VISUAL de imágenes ni el audio (el modelo local es de texto): de ellos usa
tamaño, formato, fecha, duración, etiquetas y nombre. La similitud es por nombre/fecha/texto; revisa la previsualización de `agrupar`.

## v0.2.19: el patrón de WhatsApp era demasiado flojo

En la corrida real de la 0.2.18 se movieron archivos que no eran de WhatsApp (`autism-spectrum-as-a-colour-wheel-…png`). Causa: `?` casa CUALQUIER carácter, y `*-????????-WA*` encontraba `-wheel-it-wa` dentro de `…colour-wheel-it-was…`.

- **El patrón ahora exige 8 dígitos:** `*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA*`. Si el modelo copia el `????????` viejo, se vuelve a 8 dígitos solo.
- **"si es .mov ignóralo" / "ignora los .mov" ahora excluye ese tipo** (antes el modelo lo metía en la categoría de videos).
- **Esquema JSON en la vía rápida:** Ollama obliga al modelo a devolver solo los campos válidos (ruta, destino, filtro, excluir, por_fecha, otros...). Ya no puede escribir `reglas`, que era lo que tardaba ~40 de los 47 s. Una lista de `reglas` mal formada se descarta.
- **El resumen ya no pierde los `*` ni los `_`:** Markdown los volvía cursiva (`['whatsapp*', '-????????-WA']`, `IMG_0030`).

## v0.2.18: la vía rápida acierta con tu petición real

Salió de la primera corrida real de la vía rápida (252 archivos en 52 s, pero casi todos eran fotos de cámara):

- **`IMG*` ya no atrapa fotos de cámara.** Si pides "los de WhatsApp" y el modelo pone el prefijo pelón `IMG*` o `VID*`, se cambia por `*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA*` (IMG, VID, AUD, PTT, DOC de WhatsApp) y lo avisa. Si no hablas de WhatsApp, se ejecuta tal cual pero la previsualización advierte que ese filtro también atrapa fotos de cámara.
- **El filtro y las reglas ya no se mezclan.** Con `filtro`, una regla de categoría que repite un patrón del filtro (como `*IMG*` dentro de `imagenes`) se quita: el filtro decide QUÉ se mueve y las reglas A DÓNDE. Así `IMG_0030.MOV` ya no acaba en imágenes.
- **`otros` automático:** si dices "Otros" en la petición, lo que coincide pero no tiene categoría va a `Otros/` y no se queda suelto.
- **Más rápida:** el prompt le dice que no reescriba las categorías por defecto (en la corrida real eran 46 de 52 s).
- **Veto más preciso.** "No toques X" veta lo que escribiste. Solo si añades "o similares" se amplía a las carpetas que empiezan con la misma primera palabra (`_respaldo_whatsapp` → `respaldo`).
- **Resumen legible:** una línea por carpeta, sin repetir los argumentos.
- **Sonido de "terminé":** al acabar una tarea suena un tono distinto (`complete.oga`) al de pedir autorización; sin ese archivo suena una doble campana en vez de una. No suena si cancelaste con Ctrl+C ni si la tarea duró menos de `[ui] done_sound_min_seconds` (5). `/sonido off` silencia los dos.

## v0.2.17: autónomo de verdad

- **Sesión autónoma = no pregunta.** Se quitó la confirmación de lotes grandes (`organize_confirm_over = 0`) y el plan automático (`plan_mode = "off"`; `/plan <tarea>` lo sigue pidiendo). La red de seguridad pasa a ser: invariantes que RECHAZAN en vez de preguntar, y `/deshacer`.
- **`/deshacer [id]`** regresa a su lugar el último lote movido, sin pasar por el modelo.
- **"NO toques X" es invariante de sesión**, como la guardia no-borrar: X no se puede listar, leer, mover ni nombrar en un comando. Tolera que lo escribas pegado (`carpeta_respaldo_whatsapp` encuentra `_respaldo_whatsapp`). `/estado` lo muestra y `/tocar` lo vacía.
- **Organizar en UNA llamada** (`quick_organize`). En sesión autónoma, una petición de ordenar/clasificar/mover no pasa por el enrutador, ni por plan, ni por el bucle de pasos: el modelo solo llena los argumentos de `clasificar`/`mover` con un prompt corto que ya trae el inventario de la carpeta (extensiones y palabras frecuentes en los nombres). `quick_tier` elige el escalón (0 = 2b, 1 = 4b). Si no aplica, sigue por el agente normal.
- **`clasificar` gana `por_fecha`** (`"mes"` o `"año"`: subcarpetas `AAAA-MM` sacadas del nombre) y `excluir`. Un patrón sin comodines que es un nombre exacto (`tarea.pdf`) ya coincide.
- **Cierra al terminar.** Tras un cambio real, si el modelo encadena pasos de solo mirar o repite llamadas idénticas, se le avisa una vez y luego la tarea se cierra con los hechos (`done_readonly_steps = 6`).
- **Contexto medido con tokens reales.** El presupuesto se calibra con lo que Ollama cuenta. La estimación fija se quedaba corta con nombres de archivo y fechas: el contexto llegó a 8120 tokens de 8192 y cada paso releía todo (100 s).
- **La bajada de modelo por lentitud queda apagada por defecto** (`slow_step_seconds = 0`). En las dos corridas reales entregó la tarea a un modelo más débil a la mitad.
- **Si pides "un script" para ordenar, usa la herramienta** y lo dice; `pedir_ayuda_humano` no es para pedir permiso.

Pendiente (no entra aquí): teclear comandos mientras corre una tarea, carpeta de proyecto en el home con sus recursos, y listas completas en `.txt`.

## v0.2.16: el incidente de Descargas

Una petición pegada en varias líneas terminó moviendo los 2548 archivos sueltos de Descargas a `WhatsApp/`. Causas y arreglos:

- **Un pegado de varias líneas es UN mensaje.** Antes cada línea salía como tarea aparte: la primera corrió sin el "NO toques" ni el "No borres nada" que venían en las siguientes. Solo si todas las líneas son comandos (`/cd`, `/estado`) van una por una.
- **`clasificar` acepta `filtro`** (`"whatsapp"` = el nombre lo contiene; `"IMG-*-WA*"` = comodín). Sin filtro avisa en la previsualización que se lleva TODO lo suelto.
- **Lote grande = te pregunta a ti.** `mover`/`clasificar` de más de `organize_confirm_over` archivos (25) pide confirmación con el resumen aunque la sesión sea autónoma. La previsualización la revisaba el mismo modelo que la pidió.
- **`$ORBITAL_SCRATCH` se expande en las rutas de las herramientas** y `escribir_archivo` puede escribir ahí. Antes creaba una carpeta literal `$ORBITAL_SCRATCH`. Otras variables sin expandir se rechazan.
- **`{"tool": "respuesta"}` cuenta como respuesta final** y una respuesta vacía no se entrega: se pide de nuevo y, si insiste, sale un resumen de lo que consta que se hizo. Una herramienta inexistente da un error claro, sin decir que "el usuario rechazó".
- **Gemini caído en el plan no se reintenta en el paso 1**, y al bajar a local se usa la complejidad real de la tarea (antes un 3 fijo: el 9b para mover archivos).
- **Un plan que habla de borrar se señala** antes de pedirte el sí.
- **Lentitud medida de verdad:** no cuenta el tiempo de cargar el modelo ni los pasos que generaron más de `slow_step_long_output` tokens (300). Al bajar de escalón se descarga el modelo grande (los dos juntos hicieron crecer 2 GB el swap).
- **Caché:** cada resultado se recorta una sola vez al entrar al contexto local, y el recorte por desborde deja completo solo el último. El enrutador y el escalón 2b usan el mismo `num_ctx` (8192) para que Ollama no recargue el modelo. Si tu `config.toml` define `router_num_ctx` o `[[models.tiers]]`, iguálalos.

## v0.2.15: tiempo de respuesta (sin tocar la calidad)

- **Medición por paso.** Cada paso muestra en qué se fue el tiempo: `12.4 s · lee 900 tok en 9.0 s · genera 40 tok en 3.1 s (12.9 tok/s)`, y si hubo, la carga del modelo. Al terminar sale el resumen de la tarea (cargar / leer contexto / generar). También queda en el log (`step_stats`, `task_stats`) y en la memoria de la tarea.
- **El recorte de contexto ya no rompe la caché de Ollama.** Antes se recortaban resultados viejos en cada paso, lo que cambiaba un mensaje de en medio y obligaba a Ollama a releer todo lo posterior. Ahora solo se recorta cuando el contexto se acerca al límite, y de golpe. El modelo ve igual o más que antes. Al recortar queda exactamente como lo dejaba la versión anterior.
- **El 4b se queda 30 min en RAM** (`keep_alive` por escalón). Así no se recarga del disco en cada tarea. El 9b sigue en 5 min. Si tu `config.toml` define `[[models.tiers]]`, añádeles `keep_alive` o se usa `local_keep_alive`.
- **Si ni recortando cabe**, recorta un nivel más (último resultado completo, viejos a 250 caracteres). Si no, Ollama descartaría en silencio los mensajes más viejos, empezando por la tarea original.
- **`num_thread` opcional** (0 = lo decide Ollama). Para decidirlo con datos de tu PC: `python -m orbital.medir`. Mide la lectura y la generación con 0, 8, 6 y 4 hilos, y solo recomienda fijarlo si gana por más de 5 %. El modelo y la calidad son los mismos; solo cambia cuántos núcleos calculan.
- **Descartado a propósito**, porque cuesta calidad: bajar `max_tool_output`, reescribir los prompts, saltarse la revisión final, enrutar por reglas sin el 2b y la caché KV en q8.

## v0.2.14: guardia "no borrar" y tope de tokens

- **Guardia no-borrar.** Si dices "no borres", "sin borrar" o "no elimines nada", queda activa **toda la sesión**, aunque vuelvas a hacer `/sesion`. Con la guardia, cualquier comando que borre se **rechaza**, no se pregunta. `/noborrar on|off` la controla a mano y `/estado` la muestra. Mover y clasificar siguen permitidos.
- **Un `rm` suelto ya pregunta.** Antes solo `rm -rf` pedía permiso. Ahora en sesión autónoma preguntan `rm`, `rmdir`, `unlink`, `shred`, `find -delete`, `git clean`, `truncate` y `os.remove`/`shutil.rmtree` en Python, incluido `/bin/rm`.
- **Límite honesto:** un script que borre desde dentro, como `python3 limpiar.py`, no se puede detectar por el texto del comando. La guardia ataja los borrados directos, no los ocultos en un script.
- **Tope de tokens por paso** (`max_step_tokens = 1024`). Evita que el modelo local se extienda cientos de segundos en el `pensamiento`. Si una respuesta legítima se corta por el tope, por ejemplo al escribir un archivo largo, el tope se duplica solo, hasta `max_step_tokens_hard = 4096`.

## v0.2.13: organización nativa (sin bash)

Tres herramientas nuevas que el agente usa en vez de escribir scripts de bash:

- `clasificar`: manda los archivos sueltos de una carpeta a subcarpetas por tipo (Imagenes, Videos, Audio, PDF, Documentos, Hojas, Presentaciones, Comprimidos, Instaladores, Codigo) o con reglas propias, como `{"Facturas": ["factura*"], "Fotos": ["jpg", "png"]}`.
- `mover`: mueve a una carpeta los archivos que coinciden con uno o varios patrones.
- `deshacer`: revierte el último lote, o uno por id.

Garantías que no dependen del modelo:

- **Nunca sobrescribe.** Si el nombre existe, guarda como `nombre (1).ext`.
- **Nunca borra.**
- **Previsualización.** Con `"previsualizar": true` no toca nada. Previsualizar es solo lectura, así que funciona hasta en nivel 0.
- **Bitácora por lote** en `~/.local/share/orbital/movimientos/`. Se escribe antes de mover, así que sirve aunque algo se caiga a medias.
- **Solo el primer nivel** de la carpeta.
- Ignora ocultos, enlaces simbólicos, descargas a medias (`.part`, `.crdownload`), secretos y el propio código.
- Los permisos se revisan en el origen **y** en el destino.

Por qué bitácora y no copia de respaldo: copiar 2550 archivos duplica el disco. Un mover dentro del mismo disco es un rename atómico, y la bitácora basta para revertirlo.

## v0.2.12: lentitud

- "Organizar", "clasificar" o "mover varios" ya **no fuerzan el 9b**. Son ejecución, no razonamiento: garantizan complejidad 2, y si el enrutador dice 3, se respeta. Descifrar, condiciones encadenadas, refactorizar y arquitectura siguen forzando 3.
- **Baja de modelo por lentitud.** Si un paso local tarda más de `slow_step_seconds` (75 s) dos veces en la misma tarea, baja un escalón. Ese modelo queda marcado y ni la subida por JSON malo ni la escalera vuelven a él en esa tarea, para que no rebote entre modelos. Con `/modelo` forzado solo avisa.
- **Bug arreglado:** al agotar `max_steps`, la escalera llamaba a un método inexistente (`_agent_loop_continue`) y la tarea terminaba en "error interno". Ahora sigue con el siguiente proveedor.

## Cambios en v0.1.14: aviso de "estoy trabajando"

Mientras corre una tarea existe `$XDG_RUNTIME_DIR/orbital/ocupado`, un JSON con `pid`, `desde`, `tarea` y `mouse`. Se borra al terminar, aunque la tarea se cancele o falle. Otros programas, como tu bot de capturas, pueden revisarlo para no tomar capturas ni mover el mouse mientras O.R.B.I.T.A.L trabaja. Desde la v0.4, `mouse: true` indicará que está controlando el mouse.

## v0.2.11: no se queda esperando a Gemini

El modo autónomo ya no se para a esperar que Gemini reconecte. Ahora:

- **Fallo de conexión → reintenta.** Cuando Gemini no responde por pérdida de paquetes, cada intento abre una conexión nueva (otra IP posible) y lo vuelve a intentar, sin gastar un paso de la tarea.
- **Más de `gemini_conn_retries` intentos (8 por defecto) → baja a local y SIGUE.** Apaga Gemini por lo que queda de la tarea y continúa con el mejor modelo local que quepa, en vez de abandonar o quedarse colgado. La escalera, el relevo y el último recurso dejan de ofrecer Gemini automáticamente.
- **Tarea nueva → Gemini vuelve a tener oportunidad.** Como los cortes son intermitentes (no una caída permanente), cada petición nueva lo reactiva con `reset()`.
- **JSON malo → no se rinde.** Se mantiene la recuperación de JSON cortado (v0.2.8): fuerza JSON compacto, recorta contexto y sube de modelo; solo al agotar `json_retries` escala por la escalera, nunca se corta en seco.
- Esto aplica igual al plan (`make_plan`), a la conversación (`chat`) y al bucle del agente (`_agent_loop`): ninguno devuelve ya un `[!]` por un fallo de red de Gemini; caen a local y terminan.

## v0.2.8: aguanta carpetas enormes

- **`listar` ya no inunda el contexto.** Con muchos archivos (como tus 2550 de Descargas) resume por extensión y da unos ejemplos, en vez de volcar cientos de nombres. Así al modelo local le queda espacio para responder y no se le corta el JSON a la mitad.
- **Recuperación de JSON cortado.** Si la respuesta del modelo se corta (típico al llenarse la ventana de contexto), recorta los resultados viejos, pide un JSON compacto y reintenta; si insiste, recorta al mínimo. Solo se rinde tras varios intentos (`json_retries`), no a los 3.

## v0.2.7

- **Aguanta la presión de RAM:** si el plan o un paso se corta porque la PC se satura (típico con Meet o algo pesado abierto), libera memoria, reintenta, y tras un par de cortes baja solo a un modelo más chico (9b → 4b → 2b) hasta lograrlo, en vez de rendirse. Aunque hayas forzado el 9b.
- **Aviso sonoro:** suena cuando necesita tu autorización, para que lo oigas aunque estés en otra ventana (una reunión, por ejemplo). Se apaga con `/sonido off`.

## v0.2.0: árbol de nodos y modo plan

**Nodos.** Cada tema de estudio o proyecto tiene su propia memoria, aislada de los demás. Una tarea solo ve la memoria del nodo activo; para traer la de otro, cita `@nombre` en tu mensaje. Si el nodo tiene una carpeta, al activarlo O.R.B.I.T.A.L hace `cd` ahí, así el sandbox de cada comando ve solo esa carpeta.

- `/nodo` — lista los nodos y cuántos recuerdos tiene cada uno.
- `/nodo nuevo <nombre> [tema|proyecto] [carpeta]` — crea uno y lo activa.
- `/nodo <nombre>` — cambia de nodo (y de carpeta, si tiene).
- `/nodo carpeta <ruta>` — ata una carpeta al nodo actual.
- `/nodo olvidar` — borra la memoria del nodo. `/nodo borrar <nombre>` — borra el nodo.
- En cualquier mensaje, `@otro_nodo` suma la memoria de ese nodo al contexto.

La memoria se guarda en SQLite (`~/.local/share/orbital/orbital.db`). El agente anota ahí, por nodo, qué se pidió y cómo terminó; las tareas que fallan no se guardan. El nodo `general` no acumula memoria (es el cajón por defecto). Los embeddings para búsqueda semántica llegan en una v0.2.x.

**Modo plan.** Antes de tocar nada en tareas de reorganización (mover, borrar o renombrar en masa), O.R.B.I.T.A.L propone un plan —qué revisa, qué cambios hará en orden, qué no toca, los riesgos— y espera tu aprobación. Si lo rechazas, no ejecuta nada.

- `/plan <tarea>` — fuerza el plan para esa tarea.
- `/planmode auto|always|off` — `auto` (plan solo al reorganizar, por defecto), `always` (en toda tarea con herramientas), `off`.

## Cambios en v0.1.13

- Gemini se conecta **solo por IPv4** (`ipv4_only` en `[models]`), porque tu IPv6 no tiene salida a internet. Además reintenta 2 veces si falla la conexión.

## Cambios en v0.1.12

- Los errores de red de Gemini ya dicen qué pasó: falta de conexión en 15 s (red, proxy o DNS) o falta de respuesta en 120 s. Antes esperaba 180 s y mostraba un error vacío.

## Cambios en v0.1.11: sandbox para los comandos del agente

Es lo que tu jefe describió con Docker: el agente nace sin la key y no tiene de dónde sacarla. Se hace con **bubblewrap** (lo mismo que usa Flatpak), que no necesita root ni un daemon. Docker no sirve aquí: pertenecer al grupo `docker` equivale a ser root, y su daemon consume RAM.

Cada comando de `ejecutar`:
- **Solo ve las carpetas autorizadas:** la carpeta de trabajo en modo asistido, las de `/sesion` en nivel 1 y 2, o todo el home en nivel 3. El resto de tu home no existe para él.
- **Encuentra tapados los secretos** que caigan dentro de esas carpetas: `.bashrc`, `.env`, llaves, historiales y `~/.ssh` aparecen vacíos o sin permiso.
- **Ve el código de O.R.B.I.T.A.L en solo lectura.**
- **No tiene D-Bus,** así que no puede pedir nada al llavero.
- **No ve los procesos de fuera,** así que no puede leer `/proc` de `orbitald`.
- **Arranca con un entorno limpio,** sin variables con keys.
- Tiene su propio `/tmp`, que se conserva entre comandos de la misma tarea.

Se configura en `[agent]`: `sandbox = "auto" | "required" | "off"` y `sandbox_network = true/false`. `/estado` muestra si el sandbox está activo. Instálalo con `sudo apt install bubblewrap`.

Las reglas de texto (bloquear `cat ~/.bashrc`, `os.environ`…) siguen activas como primera barrera. El sandbox es lo que atrapa los comandos disfrazados para esquivarlas.

## Cambios en v0.1.10: la key en el llavero del sistema

- **Llavero:** O.R.B.I.T.A.L busca la key primero en el llavero (GNOME Keyring vía `secret-tool`), donde queda cifrada con tu contraseña de sesión, y luego en `~/.config/orbital/env`. `instalar.sh` te ofrece moverla al llavero y borrarla del archivo; para eso necesitas `sudo apt install libsecret-tools`. `/estado` muestra de dónde salió la key.
- **Tachado por valor exacto:** el valor real de tu key, y de cualquier variable del entorno con KEY, TOKEN, SECRET o PASSWORD en el nombre, se tacha aunque no tenga un formato conocido.
- **Los comandos del agente ya no heredan esas variables:** un `python3 -c "import os; print(os.environ)"` no la puede ver.

Para que Aider use la key del llavero, cambia el `export GEMINI_API_KEY=...` de tu `.bashrc` por:
`alias aider='GEMINI_API_KEY="$(secret-tool lookup service orbital key gemini)" aider'`

## Cambios en v0.1.9: secretos

En la prueba de NEBULA, el agente leyó `~/.bashrc`, encontró `GEMINI_API_KEY`, la usó como si fuera una llave de cifrado y la metió en un comando y en el prompt del relevo.

- **Rutas siempre secretas,** sin importar lo que diga tu config: `.bashrc`, `.profile`, `.zshrc`, historiales (`.bash_history`…), `.env`, llaves, `~/.ssh`, `~/.aws`, `~/.docker`, los perfiles de navegador, los llaveros y `~/.config/orbital/env`.
- **`ejecutar` bloquea los comandos que tocan esas rutas,** además de `history`, `printenv` y `env`.
- **Tachado automático:** API keys de Google, OpenAI y Anthropic, tokens de GitHub, Slack y AWS, JWT, llaves privadas y asignaciones como `TOKEN=…` o `PASSWORD: …` se reemplazan por `[SECRETO TACHADO]` antes de llegar al modelo, a los logs, a la memoria o al prompt del relevo.
- `instalar.sh` tacha los secretos que hayan quedado en los logs y la memoria de versiones anteriores.

## Cambios en v0.1.8: la complejidad se explica

- Cada tarea muestra **por qué** recibió su nivel, por ejemplo: `[i] complejidad 3: regla: descifrar, instrucciones encadenadas (el enrutador dijo 2)`.
- **Las reglas fijas solo suben el nivel, nunca lo bajan.** Si la petición menciona descifrar, que no sabes el nombre o la ubicación de un archivo, obedecer lo que diga, organizar, arquitectura o varios archivos, el nivel es 3. Si ninguna regla aplica, decide el enrutador y debe justificarlo.
- El cliente se reconecta solo cuando `orbitald` se reinicia (por ejemplo, al correr `instalar.sh` con el cliente abierto), en vez de salir con un error.

## Cambios en v0.1.7: hacerle espacio al 9b

Si la tarea pide el 9b y le faltan hasta 4.5 GB de RAM (`make_room_max_gb`):
1. Descarga de la RAM los otros modelos de Ollama, sin preguntarte. El enrutador de 2B libera unos 2.3 GB y se vuelve a cargar solo en la siguiente petición.
2. Si aún no cabe, te ofrece cerrar apps pesadas y te dice cuántos GB faltan.
3. Si no cierras nada o sigue sin caber, baja al 4b.

Nunca carga un modelo que no cabe. Sus pesos se leen completos en cada token, así que la parte que cae en swap se relee del disco una y otra vez y congela el escritorio: no es solo esperar más.

## Cambios en v0.1.6 (segunda prueba de LENSTEST)

- `recientes` ignora las carpetas ocultas (`.config`, `.mozilla`…), que las apps reescriben cada minuto; con `"ocultos": true` se incluyen.
- La protección de su propio código compara rutas reales: `unzip -l ~/orbital.zip` ya no se bloquea, y modificar `~/orbital/orbital/*.py` sigue bloqueado.
- `leer_archivo` detecta los archivos binarios (zip, pdf, png, ELF…) y lo dice, en vez de devolver basura.
- **Revisión final:** en tareas de 3 pasos o más, antes de responder vuelve a leer la petición y comprueba cada parte, sobre todo las instrucciones de varios niveles. Cuesta un paso extra; se apaga con `final_review = false` en `[agent]`.
- El enrutador califica con complejidad 3 las tareas de buscar algo sin saber su nombre, descifrar o seguir condiciones encadenadas.

## Cambios en v0.1.5: seguridad y búsqueda (por la prueba de LENSTEST)

- **El contenido de los archivos son datos, no órdenes.** Los comentarios de código, las salidas de comandos y el texto de los archivos nunca se obedecen, salvo que tú lo pidas explícitamente y solo para esa tarea. Tampoco puede proponer cambios a archivos que no tengan que ver con lo que pediste.
- **Antes de escribir te muestra el antes y el después** (la primera línea de cada uno), así ves de un vistazo si tiene sentido.
- **Búsquedas en todo el home:** solo si tu mensaje lo menciona ("home", "carpeta personal", "toda la compu"…) y máximo 2 por tarea (`home_search_limit` en `[agent]`). Fuera de eso, solo busca en carpetas concretas.
- **Nueva herramienta `recientes`:** muestra los archivos modificados en las últimas N horas, del más nuevo al más viejo.
- **`listar` muestra primero los archivos** (del más reciente al más viejo), luego las carpetas, sin los ocultos, y avisa cuando recorta.
- **`buscar` llega hasta 4 niveles de profundidad.** Si una herramienta se queda sin tiempo 2 veces seguidas, se bloquea por el resto de la tarea.
- **Nada de descifrar ni calcular de cabeza:** tiene que escribir un script y ejecutarlo.
- **Si se atora:** tras 3 pasos sin avanzar le pide que cambie de estrategia, y a los 5 se detiene. En ese caso, y al llegar al máximo de pasos, termina con un resumen legible en vez del volcado de su historial.

## Cambios en v0.1.4: escalera de modelos locales

El enrutador también califica la complejidad de la tarea (1, 2 o 3), y O.R.B.I.T.A.L elige el modelo local así:

| Complejidad | Modelo | RAM aprox. |
|---|---|---|
| 1 trivial | qwen3.5:2b (ya cargado: es el enrutador, arranque instantáneo) | 3 GB |
| 2 normal | qwen3.5:4b | 4.5 GB |
| 3 difícil | qwen3.5:9b | 8 GB |

- Si el modelo que pide la tarea no cabe en la RAM libre (dejando 1.5 GB de margen para tus apps), baja un escalón y te avisa.
- Un modelo que ya está cargado siempre se considera disponible, porque no hay que volver a cargarlo.
- Si un modelo falla el formato JSON 2 veces, sube un escalón, siempre que quepa en la RAM.
- `/modelo 2b|4b|9b` lo fuerza (no recomendado) y `/modelo auto` regresa a la elección automática. Los escalones se ajustan en `[[models.tiers]]`.

## Cambios en v0.1.3

- Sin emojis: marcadores ASCII (`[+]` éxito, `[x]` error, `[!]` aviso, `[~]` pausa, `[>]` acción, `[<]` resultado, `[i]` por qué, `[#]` memoria) y un spinner `[|] [/] [-] [\]` en una sola línea mientras el modelo piensa.
- Regla fija: si la petición no tiene que ver con código, no va a Gemini (`gemini_only_for_code` en `[routing]`). `/gemini` y `/modo gemini` siguen ganando.
- Nueva herramienta `encontrar` para buscar archivos por nombre, saltando carpetas enormes (VMs, cachés, snaps, entornos virtuales). `buscar` ahora solo busca texto dentro de los archivos.
- Los comandos que no existen dan error en vez de mandarse al modelo.
- Modelo local por defecto: `qwen3.5:4b`. El 9b llevaba el swap al 46 % con tus apps abiertas.

## Comandos

**Ctrl+C** mientras trabaja cancela la tarea al instante, aunque el modelo esté a media respuesta. La tarea queda en la memoria para retomarla con `/resume`.
`/session` `/end` `/cd <path>` `/state` `/resume` `/forget` `/help` `/quit`
`/delete true|false`: `false` prohíbe borrar en la sesión.
`/mode auto|local|gemini|human` fija el destino de todas las peticiones. `/local …`, `/gemini …` y `/human …` lo fijan solo para esa petición.

## Pruebas

```bash
ORBITAL_CONFIG=/nonexistent python3 tests/test_smoke.py   # sin Ollama ni Gemini, con modelos falsos
```

## Hoja de ruta

- **v0.2**: árbol de nodos (tema o proyecto) con memoria aislada en SQLite y embeddings, contexto de otros nodos con `@nodo`, Aider por proyecto
- **v0.3**: voz que se puede apagar (openWakeWord, whisper.cpp, Piper) y eventos del sistema y de desarrollo
- **v0.4**: tus scripts de visión y clicks, y O.N.I.X, como herramientas
- **v0.45**: overlay de arranque en X11 que muestra la "toma de control" con datos reales
- **v0.5**: O.R.B.I.T.A.L propone automatizaciones a partir del log

## Licencia

Copyright (C) 2026 Gosten404

Este programa es software libre: se puede redistribuir y modificar bajo los términos de la Licencia Pública General de GNU, versión 3 o (a elección) cualquier versión posterior, publicada por la Free Software Foundation. Se distribuye SIN GARANTÍA, ni siquiera la implícita de comerciabilidad o idoneidad para un fin concreto. Texto completo en `LICENSE`.
