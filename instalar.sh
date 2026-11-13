#!/usr/bin/env bash
# Instala o actualiza O.R.B.I.T.A.L. Se puede correr las veces que quieras.
# Uso:  cd ~/orbital && bash instalar.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$HOME/.config/orbital"
BIN="$HOME/.local/bin"
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }

# 1. Python
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Necesitas Python 3.10 o superior"; exit 1; }

# 2. Entorno virtual (se conserva entre actualizaciones)
if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv"
  ok "entorno virtual creado"
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR"
ok "paquete instalado"

# 3. Comandos
mkdir -p "$BIN"
ln -sf "$DIR/.venv/bin/orbital"  "$BIN/orbital"
ln -sf "$DIR/.venv/bin/orbitald" "$BIN/orbitald"
for old in jarvis jarvisd; do   # enlaces de la versión anterior
  [ -L "$BIN/$old" ] && rm "$BIN/$old" && ok "quité el comando viejo '$old'"
done
case ":$PATH:" in *":$BIN:"*) ;; *)
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  warn "agregué ~/.local/bin al PATH; abre una terminal nueva" ;;
esac

# 4. Detener el servicio viejo de Jarvis si existía
if systemctl --user list-unit-files 2>/dev/null | grep -q '^jarvisd.service'; then
  systemctl --user disable --now jarvisd.service 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/jarvisd.service"
  ok "servicio jarvisd detenido y eliminado"
fi
pkill -f 'jarvis.daemon|bin/jarvisd' 2>/dev/null || true

# 5. Configuración (migra la API key y la memoria de Jarvis si existen)
mkdir -p "$CFG"
if [ ! -f "$CFG/env" ] && [ -f "$HOME/.config/jarvis/env" ]; then
  cp "$HOME/.config/jarvis/env" "$CFG/env"; ok "API key migrada desde ~/.config/jarvis/env"
fi
[ -f "$CFG/env" ] || { echo 'GEMINI_API_KEY=' > "$CFG/env"; warn "pon tu key en $CFG/env"; }
chmod 600 "$CFG/env"
if [ ! -f "$CFG/config.toml" ]; then
  cp "$DIR/config.example.toml" "$CFG/config.toml"; ok "config creado en $CFG/config.toml"
else
  ok "config existente conservado ($CFG/config.toml)"
fi
if [ -d "$HOME/.local/share/jarvis" ] && [ ! -d "$HOME/.local/share/orbital" ]; then
  cp -r "$HOME/.local/share/jarvis" "$HOME/.local/share/orbital"; ok "logs y memoria migrados"
fi

# 5a. Ofrecer mover la API key al llavero del sistema (cifrada con tu contraseña de sesión)
ENVKEY=$(sed -n 's/^GEMINI_API_KEY=//p' "$CFG/env" | tr -d "\"' ")
if [ -n "$ENVKEY" ] && [ -t 0 ]; then
  if ! command -v secret-tool >/dev/null; then
    warn "para guardar la key cifrada en el llavero: sudo apt install libsecret-tools (y vuelve a correr esto)"
  elif [ -z "$(secret-tool lookup service orbital key gemini 2>/dev/null)" ]; then
    read -r -p "¿Muevo tu key de Gemini al llavero del sistema (cifrada) y la quito de $CFG/env? [s/N] " r
    if [ "$r" = "s" ] || [ "$r" = "S" ]; then
      if printf %s "$ENVKEY" | secret-tool store --label="O.R.B.I.T.A.L Gemini" service orbital key gemini \
         && [ "$(secret-tool lookup service orbital key gemini)" = "$ENVKEY" ]; then
        sed -i 's/^GEMINI_API_KEY=.*/GEMINI_API_KEY=/' "$CFG/env"
        ok "key guardada en el llavero y borrada de $CFG/env"
      else
        warn "no pude guardarla en el llavero; la dejo en $CFG/env"
      fi
    fi
  else
    sed -i 's/^GEMINI_API_KEY=.*/GEMINI_API_KEY=/' "$CFG/env"
    ok "la key ya está en el llavero; la quito de $CFG/env"
  fi
fi
unset ENVKEY

# 5b. Tachar secretos que hayan quedado en logs/memoria de versiones anteriores
"$DIR/.venv/bin/python" -m orbital.redact && ok "logs y memoria revisados" || warn "no pude revisar los logs"

# 6. Servicio
mkdir -p "$HOME/.config/systemd/user"
cp "$DIR/systemd/orbitald.service" "$HOME/.config/systemd/user/"
if systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable orbitald.service >/dev/null 2>&1 || true
  systemctl --user restart orbitald.service || true
  sleep 1
  if systemctl --user is-active --quiet orbitald.service; then
    ok "orbitald corriendo (logs: journalctl --user -u orbitald -f)"
  else
    warn "orbitald no arrancó; mira: journalctl --user -u orbitald -n 30"
  fi
else
  warn "no pude usar systemd de usuario; arranca a mano con: orbitald"
fi

# 6b. Sandbox para los comandos del agente
if command -v bwrap >/dev/null; then
  if bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid true 2>/dev/null; then
    ok "sandbox (bubblewrap) funcionando"
  else
    warn "bubblewrap está instalado pero no puede crear el sandbox (¿user namespaces desactivados?)"
  fi
else
  warn "SIN SANDBOX: los comandos del agente correrán como tú → sudo apt install bubblewrap"
fi

# 7. Modelos
if command -v ollama >/dev/null; then
  for m in qwen3.5:2b qwen3.5:4b; do
    ollama list 2>/dev/null | grep -q "^$m" || warn "falta el modelo $m → ollama pull $m"
  done
  ollama list 2>/dev/null | grep -q "^qwen3.5:9b" || warn "opcional: qwen3.5:9b para tareas difíciles → ollama pull qwen3.5:9b"
else
  warn "no encontré ollama"
fi

echo
ok "Listo. Abre una terminal y escribe: orbital"
