#!/usr/bin/env bash
# ============================================================
#  Dial Forward — installer (Linux, bash)
#  Работает на голой системе БЕЗ python:
#      curl -sL https://uliigra2.c6t.ru/dial-forward/installer | bash
#  Устанавливает: python-окружение (venv+pip), приложение, ярлык.
#  Системные пакеты доставляются ТОЛЬКО если чего-то не хватает;
#  если всё уже есть (как у настроенной системы) — ничего не меняет.
# ============================================================
set -euo pipefail

GITHUB_RAW="https://raw.githubusercontent.com/123asxcqasdc/dial-forwrd/main/dial-forward"
MIRROR_BASE="https://uliigra2.c6t.ru/dial-forward"
APP_DIR="${1:-$HOME/dial-forward}"

say()  { printf '\033[1;32m[installer]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[installer]\033[0m %s\n' "$*"; }

if [ "$(uname -s)" != "Linux" ]; then
  warn "Это Linux-установщик. На Windows:"
  warn "  curl -o installer.bat $MIRROR_BASE/installer.bat"
  warn "  installer.bat"
  exit 1
fi

say "Архитектура: $(uname -m) ($(uname -s))"

# ---------- выбор python (как настроен у пользователя) ----------
find_python() {
  # 1) системный /usr/bin/python3 (Debian/Ubuntu, с python3-venv)
  # 2) любой python3 с модулем venv
  for p in /usr/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    [ -n "$p" ] && [ -x "$p" ] && "$p" -c 'import venv' >/dev/null 2>&1 && { echo "$p"; return 0; }
  done
  return 1
}

install_system() {  # install_system <пакеты apt-имён> [dnf-имя] [pacman-имя]
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -o Acquire::Retries=3 || warn "apt update не удался — продолжаю"
    sudo apt-get install -y $1
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y ${2:-$1}
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm ${3:-$1}
  else
    warn "Пакетный менеджер не распознан — установите вручную: $1"
    exit 1
  fi
}

# ---------- 1. python, если его нет ----------
PY=""
if ! PY="$(find_python)"; then
  warn "python3 с venv не найден — ставлю..."
  if command -v apt-get >/dev/null 2>&1; then
    install_system "python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null 2>&1; then
    install_system "python3 python3-pip" "python3 python3-pip" "python python-pip"
  elif command -v pacman >/dev/null 2>&1; then
    install_system "python python-pip" "python python-pip" "python python-pip"
  else
    warn "python3, python3-venv, python3-pip"
    exit 1
  fi
  PY="$(find_python)" || { warn "python3 так и не найден"; exit 1; }
fi
say "python: $PY ($($PY --version 2>&1))"

# ---------- 2. код приложения ----------
fetch_file() {  # fetch_file <remote_path> <local_path>
  if ! curl -fsSL --max-time 30 --retry 4 --retry-delay 2 \
       "$GITHUB_RAW/$1" -o "$2"; then
    curl -fsSL --max-time 30 --retry 4 --retry-delay 2 \
         "$MIRROR_BASE/$1" -o "$2"
  fi
}

stop_instance() {
  # мягко останавливаем ранее запущенное приложение,
  # чтобы после установки работал свежий код (а не старый из памяти)
  pkill -f "$APP_DIR/launcher.py" 2>/dev/null || true
  pkill -f "$APP_DIR/client/app.py" 2>/dev/null || true
  pkill -f "$APP_DIR/relay/relay.py" 2>/dev/null || true
  sleep 1
}

fetch_code() {
  mkdir -p "$APP_DIR/client" "$APP_DIR/relay"
  say "Качаю код приложения..."
  for f in app.py call.py protocol.py relay_client.py webrtc.py; do
    fetch_file "client/$f" "$APP_DIR/client/$f"
  done
  fetch_file "relay/relay.py" "$APP_DIR/relay/relay.py"
  fetch_file "launcher.py" "$APP_DIR/launcher.py"
  fetch_file "requirements.txt" "$APP_DIR/requirements.txt"
  mkdir -p "$APP_DIR/icons"
  fetch_file "icons/dial_forward.png" "$APP_DIR/icons/dial_forward.png"
  fetch_file "VERSION" "$APP_DIR/VERSION"
  chmod +x "$APP_DIR/launcher.py"
}

# ---------- 3. python-окружение ----------
setup_env() {
  local venv_py="$APP_DIR/.venv/bin/python"
  local PIP_RETRY="--retries 6 --timeout 20"
  say "Создаю виртуальное окружение..."
  "$PY" -m venv "$APP_DIR/.venv"
  "$venv_py" -m pip install --upgrade pip $PIP_RETRY >/dev/null 2>&1 || true
  say "Устанавливаю python-библиотеки (pip)..."
  if "$venv_py" -m pip install $PIP_RETRY -r "$APP_DIR/requirements.txt" >/tmp/df-pip.log 2>&1; then
    :  # всё встало из колёс (amd64/arm64: pygobject и pillow есть на PyPI)
  else
    warn "pip-колёса не встали — пробую системные gi/pillow (сеть или архитектура)"
    rm -rf "$APP_DIR/.venv"
    "$PY" -m venv --system-site-packages "$APP_DIR/.venv"
    grep -vE "^(pygobject|pillow)" "$APP_DIR/requirements.txt" > /tmp/df-req.txt
    if ! "$venv_py" -m pip install $PIP_RETRY -r /tmp/df-req.txt >/tmp/df-pip2.log 2>&1; then
      warn "python-библиотеки (telethon/websockets/qrcode/pystray) не установились — сеть нестабильна"
      warn "лог: /tmp/df-pip2.log — после установки запустите ещё раз установщик"
    fi
    if ! "$venv_py" -c 'import gi' >/dev/null 2>&1; then
      install_system "python3-gi python3-gi-cairo python3-pil" "python3-gobject python3-pillow" "python-gobject python-pillow"
    fi
  fi
  # проверка GStreamer (typelibs и плагины) — доставляем только если нет
  if ! "$venv_py" -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst' >/dev/null 2>&1; then
    warn "GStreamer typelibs не найдены — ставлю системные пакеты..."
    if command -v apt-get >/dev/null 2>&1; then
      install_system "gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-nice"
    elif command -v dnf >/dev/null 2>&1; then
      install_system "gstreamer1-devel gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad gstreamer1-plugins-ugly gstreamer1-libav" "gstreamer1-devel gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad gstreamer1-plugins-ugly gstreamer1-libav gstreamer1-nice" "gst-python gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav gst-plugin-pipewire"
    else
      warn "gstreamer + плагины — вручную"
    fi
  fi
  if ! "$venv_py" -c 'import gi; gi.require_version("Gst", "1.0")' >/dev/null 2>&1; then
    warn "GStreamer так и не подключился. Проверьте вручную: python3 -c 'import gi; gi.require_version(\"Gst\",\"1.0\")'"
  fi
  # финальная проверка всех зависимостей
  if ! "$venv_py" -c 'import gi, pystray, PIL, telethon, websockets, qrcode' >/dev/null 2>&1; then
    warn "Часть python-библиотек не установилась (плохая сеть?). Перезапустите установщик — он продолжит с того же места."
  fi
}

# ---------- 4. ярлык на рабочий стол ----------
create_shortcut() {
  local apps="$HOME/.local/share/applications"
  local ico_dir="$HOME/.local/share/icons/hicolor/256x256/apps"
  mkdir -p "$apps" "$ico_dir"
  if [ -f "$APP_DIR/icons/dial_forward.png" ]; then
    cp "$APP_DIR/icons/dial_forward.png" "$ico_dir/dial-forward.png"
  fi
  cat > "$apps/dial-forward.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Dial Forward
Comment=P2P звонки через Telegram
Exec=$APP_DIR/.venv/bin/python $APP_DIR/launcher.py
Path=$APP_DIR
Icon=dial-forward
Terminal=false
Categories=Network;Telephony;
EOF
  chmod +x "$apps/dial-forward.desktop"
  say "Ярлык: $apps/dial-forward.desktop"
}

create_autostart() {
  local ad="$HOME/.config/autostart"
  mkdir -p "$ad"
  cat > "$ad/dial-forward.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Dial Forward
Comment=P2P звонки через Telegram (фоновый приём звонков)
Exec=$APP_DIR/.venv/bin/python $APP_DIR/launcher.py --minimized
Path=$APP_DIR
Icon=dial-forward
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
  say "Автозапуск: $ad/dial-forward.desktop"
}

say "Dial Forward installer"
stop_instance
fetch_code
say "Версия: $(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?')"
setup_env
create_shortcut
create_autostart
say "Готово! Запуск: $APP_DIR/.venv/bin/python $APP_DIR/launcher.py"
say "или через меню приложений: Dial Forward"
say "Обновления приложение проверяет само и предложит скачать."