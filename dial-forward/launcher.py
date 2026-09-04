#!/usr/bin/env python3
"""Dial Forward launcher — запускает relay и приложение.

Работает от текущего пользователя: никаких sudo/systemd, пароль не нужен.
Если relay уже запущен (ws://127.0.0.1:4545 отвечает) — не трогает его.
При закрытии приложения (или Ctrl+C) останавливает relay, который запустил сам.

Использование:  python launcher.py [аргументы приложения...]
                python launcher.py --call username
"""
import json
import os
import socket
import subprocess
import sys
import time

# под pythonw.exe stdout/stderr равны None — print() падает
for _n in ("stdout", "stderr"):
    if getattr(sys, _n, None) is None:
        try:
            setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))
        except OSError:
            pass

ROOT = os.path.dirname(os.path.abspath(__file__))
RELAY_DIR = os.path.join(ROOT, "relay")
CLIENT_DIR = os.path.join(ROOT, "client")
WS_ADDR = ("127.0.0.1", 4545)


def pick_python():
    if os.name == "nt":
        candidates = [
            sys.executable,
            os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
            os.path.join("C:", os.sep, "msys64", "mingw64", "bin", "python.exe"),
        ]
    else:
        candidates = [
            sys.executable,
            os.path.join(ROOT, ".venv", "bin", "python"),
        ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return sys.executable


def ws_alive(timeout=2):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(WS_ADDR)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _local_version():
    """Версия, которую мы ожидаем от relay (файл VERSION рядом с launcher)."""
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0"
    except OSError:
        return "0"


def _ws_query_version(timeout=3):
    """Версия запущенного relay через WS (cmd=status). None при любой ошибке."""
    import asyncio
    try:
        import websockets
    except ImportError:
        return None

    async def _q():
        try:
            async with websockets.connect("ws://127.0.0.1:4545",
                                          open_timeout=timeout) as ws:
                await ws.send('{"cmd":"status"}')
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout)
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "event" in msg:
                        continue
                    return msg.get("version")
        except Exception:
            return None

    return asyncio.run(_q())


def _kill_existing_relay(timeout=5):
    """Останавливает запущенный relay: сперва WS-команда shutdown, потом kill."""
    import asyncio
    try:
        import websockets
        has_ws = True
    except ImportError:
        has_ws = False

    if has_ws:
        async def _kill():
            try:
                async with websockets.connect("ws://127.0.0.1:4545",
                                              open_timeout=2) as ws:
                    await ws.send('{"cmd":"shutdown"}')
                    try:
                        await asyncio.wait_for(ws.recv(), 2)
                    except (asyncio.TimeoutError, Exception):
                        pass
            except Exception:
                pass
        try:
            asyncio.run(_kill())
        except Exception:
            pass

    # дожидаемся, пока порт освободится
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ws_alive():
            return True
        time.sleep(0.3)
    # не освободился — бьём по имени процесса
    if os.name == "nt":
        subprocess.run(["taskkill", "/IM", "Relay.exe", "/F"],
                       capture_output=True, **popen_kwargs())
    else:
        subprocess.run(["pkill", "-f", "relay.py"], capture_output=True)
    time.sleep(0.5)
    return not ws_alive()


def wait_ws(seconds=40):
    for _ in range(int(seconds / 0.5)):
        if ws_alive():
            return True
        time.sleep(0.5)
    return False


def popen_kwargs():
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


def app_already_running():
    """True, если экземпляр приложения уже работает (порт-замок 4548)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 4548), timeout=2) as c:
            c.sendall(b"show\n")
        return True
    except OSError:
        return False


def main():
    py = pick_python()
    while True:
        if app_already_running():
            print("[launcher] Dial Forward уже запущен — показываю окно",
                  flush=True)
            return 0
        rc = run_once(py)
        if rc != 75:
            return rc
        print("[launcher] обновление установлено — перезапускаю...", flush=True)


def run_once(py):
    relay_was_up = ws_alive()
    relay_proc = None
    expected = _local_version()

    if relay_was_up:
        running = _ws_query_version()
        if running is not None and running != expected:
            print(f"[launcher] relay {running} != ожидаемый {expected} — "
                  f"перезапускаю relay", flush=True)
            _kill_existing_relay()
            relay_was_up = False

    if not relay_was_up:
        print("[launcher] relay не запущен — стартую...", flush=True)
        relay_proc = subprocess.Popen(
            [py, os.path.join(RELAY_DIR, "relay.py")],
            cwd=RELAY_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_kwargs())
        if not wait_ws():
            print("[launcher] relay не поднялся за 40с — проверьте интернет/Telegram", flush=True)
            relay_proc.terminate()
            return 1
        print("[launcher] relay готов", flush=True)
    else:
        print("[launcher] relay уже работает", flush=True)

    if not os.environ.get("DISPLAY") and os.name == "posix":
        print("[launcher] предупреждение: DISPLAY не задан — окно может не открыться", flush=True)

    print("[launcher] запускаю Dial Forward...", flush=True)
    app_args = [py, os.path.join(CLIENT_DIR, "app.py")] + sys.argv[1:]
    try:
        rc = subprocess.run(app_args, cwd=CLIENT_DIR,
                            **popen_kwargs()).returncode
    finally:
        if relay_proc is not None and not relay_was_up:
            if rc == 75:
                print("[launcher] перезапуск — перезапускаю relay", flush=True)
            else:
                print("[launcher] приложение закрыто — останавливаю relay", flush=True)
            relay_proc.terminate()
            try:
                relay_proc.wait(5)
            except subprocess.TimeoutExpired:
                relay_proc.kill()
    print(f"[launcher] выход, код {rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
