#!/usr/bin/env python3
"""TGRTC relay — Telegram MTProto сигналинг для P2P-звонков.

- ключи MTProto встроены (tdata и Telegram Desktop не используются)
- логинится как пользователь (MTProto, Telethon) — вход через код/QR
- по команде создаёт приватную группу и обменивается в ней сигналингом
- локальный WebSocket API (127.0.0.1:4545) для GUI-клиента
"""
import asyncio
import json
import logging
import os
import sys
import time

import websockets
from telethon import TelegramClient, functions
from telethon.errors import (PhoneCodeInvalidError, SendCodeUnavailableError,
                             SessionPasswordNeededError)
from telethon.events import NewMessage
from telethon.network.connection.tcpobfuscated import ConnectionTcpObfuscated
from telethon.tl.types import Message as TLMessage

log = logging.getLogger("relay")

OFFICIAL_API_ID = 2040
OFFICIAL_API_HASH = "b18441a1ff607e10a989891a5462e627"
WS_ADDR = ("127.0.0.1", 4545)


def _relay_version():
    """Версия сборки relay: из VERSION рядом с бандлом (frozen) или в repo (dev)."""
    cands = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cands.append(os.path.join(base, "VERSION"))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        cands += [os.path.join(here, "..", "VERSION"),
                  os.path.join(here, "VERSION")]
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip() or "0"
        except OSError:
            continue
    return "0"


# ---------- ключи (без tdata и без Telegram Desktop) ----------

def extract_keys():
    """Официальные ключи MTProto встроены. Ничего не читаем из tdata:
    вход только через код/QR, сессия Telegram Desktop не используется."""
    return {"api_id": OFFICIAL_API_ID, "api_hash": OFFICIAL_API_HASH, "source": "builtin"}


# ---------- DC probing ----------

DC_CANDIDATES = [
    (2, "2001:67c:4e8:f002::a", 443),
    (2, "2001:67c:4e8:f006::a", 443),
    (2, "149.154.167.51", 443),
    (2, "149.154.167.51", 80),
    (2, "149.154.167.41", 80),
    (2, "2001:b28:f23f:f005::a", 443),
    (1, "2001:67c:4e8:d001::a", 443),
    (1, "2001:b28:f23d:f001::a", 443),
    (1, "149.154.175.53", 443),
    (5, "91.108.56.130", 443),
]


# ---------- proxy (SOCKS5/HTTP) with auto-detection ----------
# В РФ Telegram заблокирован, поэтому relay сам ищет рабочий путь к DC:
#   1) прямой доступ (если работает — прокси не нужен)
#   2) известные локальные SOCKS5/HTTP-прокси (Nova/WARP/Clash/v2ray и т.п.)
#   3) системный прокси Windows (реестр WinINET)
# Явная настройка опциональна — переменная окружения DIALFWD_PROXY:
#   DIALFWD_PROXY=socks5://127.0.0.1:1372
#   DIALFWD_PROXY=http://127.0.0.1:1370
#   DIALFWD_PROXY=user:pass@host:port (scheme по умолчанию socks5)
from urllib.parse import urlparse

# Прокси в порядке приоритета. Много совпадающих локальных портов —
# автоопределение просто перебирает их, пока не найдёт рабочий.
PROXY_CANDIDATES = [
    {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1372, "rdns": True},   # Nova
    {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1370, "rdns": True},   # WARP (socks)
    {"proxy_type": "http",   "addr": "127.0.0.1", "port": 1371, "rdns": True},   # Opera (http)
    {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 7891, "rdns": True},   # Clash (socks)
    {"proxy_type": "http",   "addr": "127.0.0.1", "port": 7890, "rdns": True},   # Clash (http)
    {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080, "rdns": True},   # общий SOCKS5
    {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 10808, "rdns": True},  # v2ray (socks)
    {"proxy_type": "http",   "addr": "127.0.0.1", "port": 10809, "rdns": True},  # v2ray (http)
]

_REDETECT_S = 30          # пере-поиск прокси, если доступных путей не было
_selected_proxy = None    # None=не выбран; "direct"=работает напрямую;
                          # dict=использовать прокси; "none"=нет пути
_selected_proxy_ts = 0.0


def parse_proxy_env():
    """Разбирает DIALFWD_PROXY в формат, понятный Telethon (dict)."""
    raw = os.environ.get("DIALFWD_PROXY", "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else "socks5://" + raw)
    scheme = (parsed.scheme or "socks5").lower()
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (1080 if scheme != "http" else 8080)
    proxy_type = "socks5" if scheme in ("socks5", "socks") else (
        "socks4" if scheme == "socks4" else "http")
    cfg = {
        "proxy_type": proxy_type,
        "addr": host,
        "port": port,
        "rdns": True,
    }
    if parsed.username:
        cfg["username"] = parsed.username
        cfg["password"] = parsed.password or ""
    return cfg


def _system_win_proxy():
    """Системный прокси Windows (WinINET) — registry; вне Windows -> None."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        with key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    if not server or "=" in server:  # per-protocol ("http=..." и т.п.) — пропускаем
        return None
    host = server
    port = 80
    if "://" in server:
        host = urlparse(server).hostname or server
    if ":" in host and not host.startswith("["):
        host, _, port_s = host.rpartition(":")
        if port_s.isdigit():
            port = int(port_s)
    return {"proxy_type": "http", "addr": host, "port": port, "rdns": True}


def _proxy_candidates():
    """Список прокси для перебора: явный DIALFWD_PROXY, известные, системный."""
    cands = []
    env_cfg = parse_proxy_env()
    if env_cfg:
        cands.append(env_cfg)
    for c in PROXY_CANDIDATES:
        if (c["proxy_type"], c["addr"], c["port"]) not in {
            (x["proxy_type"], x["addr"], x["port"]) for x in cands}:
            cands.append(c)
    sys_cfg = _system_win_proxy()
    if sys_cfg and (sys_cfg["proxy_type"], sys_cfg["addr"], sys_cfg["port"]) not in {
            (x["proxy_type"], x["addr"], x["port"]) for x in cands}:
        cands.append(sys_cfg)
    return cands


async def _raw_connect(ip, port, timeout=12):
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(ip, port), timeout=timeout)
    writer.close()


async def _open_proxy_connection(cfg, host, port, timeout=12):
    """Открывает TCP-соединение через прокси к (host, port).

    Использует python_socks (asyncio) — тот же стек, что и Telethon,
    поэтому поведение при зонде совпадает с реальным подключением.
    """
    import python_socks
    from python_socks import ProxyType
    from python_socks.async_.asyncio import Proxy

    ptype = {
        "socks5": ProxyType.SOCKS5,
        "socks4": ProxyType.SOCKS4,
        "http": ProxyType.HTTP,
    }.get(cfg.get("proxy_type", "socks5"), ProxyType.SOCKS5)
    proxy = Proxy.create(
        proxy_type=ptype,
        host=cfg["addr"],
        port=cfg["port"],
        username=cfg.get("username"),
        password=cfg.get("password"),
        rdns=cfg.get("rdns", True),
    )
    sock = await asyncio.wait_for(
        proxy.connect(dest_host=host, dest_port=port), timeout=timeout)
    sock.close()
    return True


async def _probe_candidates(cands, via_proxy=None):
    """Пробует DC-кандидатов (напрямую или через прокси), пока не найдёт живой."""
    for dc_id, ip, port in cands:
        try:
            if via_proxy:
                await _open_proxy_connection(via_proxy, ip, port)
            else:
                await _raw_connect(ip, port)
            log.info("probe ok%s: [%s]%s:%s",
                     " via proxy" if via_proxy else "", dc_id, ip, port)
            return True
        except Exception:
            log.info("probe fail%s: [%s]%s:%s",
                     " via proxy" if via_proxy else "", dc_id, ip, port)
    return False


async def detect_proxy(cands):
    """Возвращает 'direct', dict-прокси или 'none' — какой путь до Telegram доступен."""
    if await _probe_candidates(cands):
        return "direct"
    log.info("direct blocked — ищу локальный прокси...")
    for cfg in _proxy_candidates():
        log.info("trying proxy %s://%s:%s",
                 cfg["proxy_type"], cfg["addr"], cfg["port"])
        if await _probe_candidates(cands, via_proxy=cfg):
            return cfg
    return "none"


def get_session_proxy():
    """Прокси для TelegramClient: dict либо None (значит напрямую)."""
    if isinstance(_selected_proxy, dict):
        return _selected_proxy
    return None


async def probe_dc(fixed_dc=None):
    global _selected_proxy, _selected_proxy_ts
    cands = [c for c in DC_CANDIDATES if fixed_dc is None or c[0] == fixed_dc]
    if not cands:
        cands = DC_CANDIDATES

    # автоопределение пути: при первом запуске, либо если прошлый раз пути не было
    if _selected_proxy is None or (
            _selected_proxy == "none"
            and time.monotonic() - _selected_proxy_ts > _REDETECT_S):
        _selected_proxy = await detect_proxy(cands)
        _selected_proxy_ts = time.monotonic()
        if _selected_proxy == "direct":
            log.info("работаю напрямую (прокси не нужен)")
        elif _selected_proxy != "none":
            log.info("выбран прокси %s://%s:%s",
                     _selected_proxy["proxy_type"],
                     _selected_proxy["addr"], _selected_proxy["port"])
        else:
            log.warning("прокси не найден, прямой доступ тоже недоступен")

    proxy_cfg = _selected_proxy if isinstance(_selected_proxy, dict) else None
    if await _probe_candidates(cands, via_proxy=proxy_cfg):
        for dc_id, ip, port in cands:
            try:
                if proxy_cfg:
                    await _open_proxy_connection(proxy_cfg, ip, port)
                else:
                    await _raw_connect(ip, port)
                return dc_id, ip, port
            except Exception:
                continue
    return None


# ---------- relay ----------

class Relay:
    def __init__(self, keys):
        self.keys = keys
        self.client = None
        self.clients = set()
        self.self_id = None
        self.phone = None
        self.code_hash = None
        self.calls = {}
        self.seen_ids = {}
        self.qr = None
        self.qr_refresh_count = 0
        self._last_conn_state = None   # для broadcast только при изменении
        self._last_liveness_t = 0.0    # последняя реальная проверка Telegram

    async def start(self):
        asyncio.create_task(self._connection_loop())

    def _conn_state(self):
        """Текущее состояние канала к Telegram."""
        try:
            connected = bool(self.client and self.client.is_connected())
        except Exception:
            connected = False
        return {"connected": connected,
                "authorized": self.self_id is not None}

    async def _push_conn_state(self, force=False):
        st = self._conn_state()
        key = (st["connected"], st["authorized"])
        if force or key != self._last_conn_state:
            self._last_conn_state = key
            log.info("conn state -> connected=%s authorized=%s", *key)
            await self.broadcast({"event": "conn", **st})

    async def _connection_loop(self):
        """Живёт всегда: подключает Telegram и чинит разрывы соединения."""
        while True:
            await self._connect_once()
            # следим за соединением; при разрыве — переподключаемся
            while True:
                await asyncio.sleep(5)
                st = self._conn_state()
                if not st["connected"]:
                    log.warning("telegram disconnect обнаружен — переподключаюсь")
                    await self.broadcast({"event": "tg_disconnected"})
                    break
                await self._push_conn_state()
                # реальная проверка канала: is_connected долго не замечает
                # «тихий» обрыв, поэтому раз в 30с гоняем настоящий RPC
                if time.monotonic() - self._last_liveness_t >= 30:
                    self._last_liveness_t = time.monotonic()
                    try:
                        await asyncio.wait_for(self._ping_telegram(), timeout=6)
                    except Exception as e:
                        log.warning("telegram не отвечает (%s) — разрываю "
                                    "соединение", type(e).__name__)
                        try:
                            await self.client.disconnect()
                        except Exception:
                            pass
                        await self.broadcast({"event": "tg_disconnected"})
                        break

    async def _ping_telegram(self):
        """Лёгкий RPC, доказывающий живую TCP-связь к Telegram DC."""
        try:
            await self.client(functions.help.GetConfigRequest())
        except Exception:
            raise

    async def _connect_once(self):
        while True:
            fixed_dc = None
            if self.client is not None and self.client.session.auth_key:
                fixed_dc = self.client.session.dc_id
            dc = await probe_dc(fixed_dc)
            if dc is None:
                log.warning("нет доступного DC — повторный зонд через 10s")
                await asyncio.sleep(10)
                continue
            dc_id, ip, port = dc
            ipv6 = ":" in ip
            if self.client is None:
                log.info("создаю TelegramClient (dc %s %s:%s ipv6=%s)",
                         dc_id, ip, port, ipv6)
                self.client = TelegramClient(
                    "tgrtc.session", self.keys["api_id"], self.keys["api_hash"],
                    connection=ConnectionTcpObfuscated,
                    use_ipv6=ipv6, connection_retries=8, retry_delay=4,
                    proxy=get_session_proxy())
                @self.client.on(NewMessage)
                async def handler(event: NewMessage.Event):
                    await self.on_new_message(event)
            try:
                log.info("подключаюсь к Telegram [%s] %s:%s...", dc_id, ip, port)
                t0 = time.monotonic()
                self.client.session.set_dc(dc_id, ip, port)
                await self.client.connect()
                log.info("telegram connect ok за %.1fs (dc %s)",
                         time.monotonic() - t0, dc_id)
                break
            except Exception as e:
                log.warning("telegram connect failed: %s (%s) — retry in 10s",
                            type(e).__name__, e)
                name = type(e).__name__
                if (self.self_id is None and self.client is not None
                        and "AuthKeyNotFound" in name):
                    await self._reset_session()
                await asyncio.sleep(10)
        try:
            if await self.client.is_user_authorized():
                await self.after_login()
            else:
                self.qr_refresh_count = 0
                await self.broadcast({"event": "need_login"})
        except Exception as e:
            log.exception("auth check failed")
            await self.broadcast({"event": "fatal", "error": str(e)})
        await self._push_conn_state(force=True)

    async def _reset_session(self):
        """Удаляет битую (оборванную на рукопожатии) сессию, пока вход не завершён."""
        try:
            await self.client.disconnect()
        except Exception:
            pass
        self.client = None
        for f in ("tgrtc.session", "tgrtc.session-journal",
                  "tgrtc.session-shm", "tgrtc.session-wal"):
            try:
                os.remove(f)
            except OSError:
                pass
        log.info("session reset (broken transport key)")

    async def after_login(self):
        me = await self._retry(self.client.get_me)
        self.self_id = me.id
        self.qr = None
        self.qr_refresh_count = 0
        log.info("logged in as %s (%s)", me.first_name, me.id)
        await self.broadcast({"event": "logged_in", "self_id": me.id, "first_name": me.first_name})

    async def on_new_message(self, event):
        msg = event.message
        if not isinstance(msg, TLMessage) or not msg.message:
            return
        now = time.monotonic()
        self.seen_ids = {k: v for k, v in self.seen_ids.items() if now - v < 300}
        if msg.id in self.seen_ids:
            return
        self.seen_ids[msg.id] = now
        pid = getattr(msg, "peer_id", None)
        chat_id = (getattr(pid, "chat_id", None) or getattr(pid, "channel_id", None)
                   or getattr(pid, "user_id", None))
        from_id = getattr(msg.from_id, "user_id", None) if msg.from_id else None
        await self.broadcast({
            "event": "message",
            "chat_id": chat_id,
            "from_id": from_id,
            "msg_id": msg.id,
            "text": msg.message,
        })

    async def broadcast(self, data: dict):
        if not self.clients:
            return
        text = json.dumps(data)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ---- команды ----

    async def status(self):
        return {
            "api_id": self.keys["api_id"],
            "source": self.keys.get("source"),
            "self_id": self.self_id,
            "authorized": self.self_id is not None,
            "connected": bool(self.client and self.client.is_connected()),
            "version": _relay_version(),
        }

    async def shutdown(self):
        """Корректно завершает relay по WS-команде (вызывается launcher'ом при
        обновлении/несовпадении версии)."""
        log.info("shutdown command received — останавливаюсь")
        asyncio.create_task(self._bye())
        return {"bye": True}

    async def _bye(self):
        await asyncio.sleep(0.1)
        os._exit(0)

    async def login_phone(self, phone):
        self.phone = phone
        try:
            sent = await self._retry(lambda: self.client.send_code_request(phone))
        except SendCodeUnavailableError:
            return {"error": "код уже отправлен ранее — проверьте SMS/Telegram. Повторную отправку ограничили, подождите пару минут."}
        self.code_hash = sent.phone_code_hash
        return {"code_hash": self.code_hash}

    async def login_qr(self):
        if self.client is None or not self.client.is_connected():
            return {"error": "подключение к Telegram ещё идёт, попробуйте через пару секунд"}
        self.qr_refresh_count = 0
        qr = await self.client.qr_login()
        self.qr = qr
        log.info("qr login: токен выдан")
        asyncio.create_task(self._wait_qr(qr))
        return {"url": qr.url}

    async def _qr_refresh(self):
        """Тихо перевыпускает QR, если пользователь ещё не отсканировал."""
        if self.qr_refresh_count >= 10:
            log.warning("qr: лимит перевыпусков исчерпан")
            await self.broadcast({"event": "qr_expired"})
            return
        self.qr_refresh_count += 1
        try:
            log.info("qr: перевыпуск #%d", self.qr_refresh_count)
            qr = await self.client.qr_login()
            self.qr = qr
            await self.broadcast({"event": "qr_new", "url": qr.url})
            asyncio.create_task(self._wait_qr(qr))
        except Exception as e:
            log.exception("qr refresh failed")
            await self.broadcast({"event": "qr_error", "error": str(e)})

    async def _wait_qr(self, qr):
        try:
            await qr.wait(timeout=90)
            await self.after_login()
        except SessionPasswordNeededError:
            self.qr_password = True
            await self.broadcast({"event": "qr_password_needed"})
        except asyncio.TimeoutError:
            if self.self_id is None and self.qr is qr:
                await self._qr_refresh()          # автообновление вместо ошибки
        except Exception as e:
            if self.self_id is None:
                log.exception("qr login failed")
                await self.broadcast({"event": "qr_error", "error": str(e)})

    async def _retry(self, coro_factory, retries=3):
        last = None
        for _ in range(retries):
            try:
                return await coro_factory()
            except (ConnectionError, TimeoutError, OSError,
                    ValueError, RuntimeError) as e:
                last = e
                log.warning("transport error, retry: %s", e)
                await asyncio.sleep(3)
        raise last

    async def login_code(self, code, code_hash):
        try:
            await self._retry(lambda: self.client.sign_in(
                self.phone, code, phone_code_hash=code_hash))
        except SessionPasswordNeededError:
            return {"need_password": True}
        except PhoneCodeInvalidError:
            return {"error": "неверный код"}
        await self.after_login()
        return {}

    async def login_password(self, password):
        await self._retry(lambda: self.client.sign_in(self.phone, password=password))
        await self.after_login()
        return {}

    async def logout(self):
        """Полный выход: log_out + удаление файла сессии + пересоздание клиента."""
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception as e:
                log.warning("log_out: %s", e)
        self.self_id = None
        self.qr = None
        self.qr_refresh_count = 0
        self._last_conn_state = None
        self.calls.clear()
        await self.broadcast({"event": "logged_out"})
        await self._reset_session()
        log.info("logged out: файл сессии удалён, telegram-часть перезапустится")
        return {}

    async def resolve(self, username):
        username = username.lstrip("@").strip()
        entity = await self.client.get_entity(username)
        return {"user_id": entity.id, "username": getattr(entity, "username", ""), "first_name": getattr(entity, "first_name", "")}

    async def call(self, username, title, user_id=None):
        if username:
            username = username.lstrip("@").strip()
            user = await self.client.get_entity(username)
        else:
            user = await self.client.get_entity(int(user_id))
        if user.id in self.calls:
            chat_id = self.calls[user.id]
            try:
                members = await self.client.get_participants(chat_id)
                if not any(m.id == user.id for m in members):
                    await self.client(functions.messages.AddChatUserRequest(
                        chat_id=abs(int(chat_id)), user_id=user.id, fwd_limit=50))
            except Exception as err:
                log.warning("повторное добавление участника: %s", err)
            return {"chat_id": chat_id, "user_id": user.id}
        if not title:
            title = f"Call {getattr(user, 'first_name', username)}"
        async for d in self.client.iter_dialogs():
            e = d.entity
            if getattr(e, "title", "") == title:
                members = await self.client.get_participants(e.id)
                if any(m.id == user.id for m in members):
                    self.calls[user.id] = e.id
                    return {"chat_id": e.id, "user_id": user.id, "reused": True}
                if len(members) == 1:
                    try:
                        await self.client(functions.messages.AddChatUserRequest(
                            chat_id=abs(int(e.id)), user_id=user.id, fwd_limit=50))
                        self.calls[user.id] = e.id
                        return {"chat_id": e.id, "user_id": user.id, "reused": True,
                                "invite_sent": True}
                    except Exception as err:
                        log.warning("AddChatUser в существующей группе: %s", err)
        updates = await self.client(functions.messages.CreateChatRequest(
            users=[user],
            title=title,
        ))
        chat_id = None
        updates_obj = getattr(updates, "updates", None) or updates
        for u in getattr(updates_obj, "updates", []):
            m = getattr(u, "message", None)
            if m is not None and m.peer_id is not None:
                chat_id = getattr(m.peer_id, "chat_id", None)
                if chat_id:
                    break
        if chat_id is None:
            async for d in self.client.iter_dialogs():
                if d.title == title:
                    chat_id = d.id
                    break
        if chat_id is None:
            return {"error": "группа создана, но id не найден"}
        try:
            members = await self.client.get_participants(chat_id)
            if not any(m.id == user.id for m in members):
                await self.client(functions.messages.AddChatUserRequest(
                    chat_id=chat_id, user_id=user.id, fwd_limit=50))
        except Exception as e:
            log.warning("проверка/добавление участника: %s", e)
            await self.cleanup_group(chat_id)
            return {"error": "не удалось добавить участника — группа удалена"}
        self.calls[user.id] = chat_id
        return {"chat_id": chat_id, "user_id": user.id}

    async def call_group(self, usernames, title):
        users = []
        for u in usernames:
            u = u.lstrip("@").strip()
            entity = await self.client.get_entity(u)
            users.append(entity)
        if not title:
            title = "Call " + " & ".join(getattr(u, "first_name", u.id)
                                          for u in users[:3])
        title = title[:64]
        try:
            updates = await self.client(functions.messages.CreateChatRequest(
                users=users, title=title))
        except Exception as e:
            return {"error": f"не удалось создать группу: {e}"}
        chat_id = None
        updates_obj = getattr(updates, "updates", None) or updates
        for u in getattr(updates_obj, "updates", []):
            m = getattr(u, "message", None)
            if m is not None and m.peer_id is not None:
                chat_id = getattr(m.peer_id, "chat_id", None)
                if chat_id:
                    break
        if chat_id is None:
            async for d in self.client.iter_dialogs():
                if d.title == title:
                    chat_id = d.id
                    break
        if chat_id is None:
            return {"error": "группа создана, но id не найден"}
        try:
            members = await self.client.get_participants(chat_id)
            missing = [u for u in users if not any(m.id == u.id for m in members)]
            for u in missing:
                await self.client(functions.messages.AddChatUserRequest(
                    chat_id=chat_id, user_id=u.id, fwd_limit=50))
            if missing:
                log.info("call_group: добавлены недостающие: %s",
                         [u.id for u in missing])
        except Exception as e:
            log.warning("call_group: добавление участников: %s", e)
            await self.cleanup_group(chat_id)
            return {"error": "не удалось добавить участника — группа удалена"}
        for u in users:
            self.calls[u.id] = chat_id
        return {"chat_id": chat_id, "user_id": [u.id for u in users]}

    async def invite(self, chat_id, username):
        username = username.lstrip("@").strip()
        user = await self.client.get_entity(username)
        try:
            await self.client(functions.messages.AddChatUserRequest(
                chat_id=abs(int(chat_id)), user_id=user.id, fwd_limit=50))
        except Exception as e:
            log.warning("invite: %s: %s", username, e)
            await self.cleanup_group(chat_id)
            return {"error": "не удалось пригласить участника — группа удалена"}
        return {"user_id": user.id}

    async def chat_info(self, chat_id):
        users = await self.client.get_participants(chat_id)
        return {"members": [{
            "user_id": u.id,
            "username": getattr(u, "username", ""),
            "first_name": getattr(u, "first_name", ""),
        } for u in users]}

    async def cleanup_group(self, chat_id):
        """Удаляет всех участников из группы, затем саму группу."""
        chat_id = abs(int(chat_id))
        try:
            members = await self.client.get_participants(chat_id)
        except Exception as err:
            log.warning("cleanup: список участников: %s", err)
            members = []
        for m in members:
            if m.id == self.self_id:
                continue
            try:
                await self.client(functions.messages.DeleteChatUserRequest(
                    chat_id=chat_id, user_id=m.id))
                log.info("cleanup: участник %s исключён", m.id)
            except Exception as err:
                log.warning("cleanup: исключение участника %s: %s", m.id, err)
        try:
            await self.client(functions.messages.DeleteChatRequest(chat_id=chat_id))
            log.info("cleanup: группа %s удалена", chat_id)
        except Exception as err:
            log.warning("cleanup: DeleteChatRequest: %s", err)
        self.calls = {k: v for k, v in self.calls.items() if v != chat_id}

    async def leave(self, chat_id):
        chat_id = abs(int(chat_id))
        await self.cleanup_group(chat_id)
        return {}

    async def dialogs(self):
        out = []
        async for d in self.client.iter_dialogs():
            e = d.entity
            if hasattr(e, "first_name"):
                out.append({
                    "type": "user",
                    "id": e.id,
                    "title": d.title,
                    "username": getattr(e, "username", ""),
                    "first_name": getattr(e, "first_name", ""),
                    "contact": bool(getattr(e, "contact", False)),
                    "bot": bool(getattr(e, "bot", False)),
                })
            else:
                out.append({
                    "type": "chat",
                    "id": d.id,
                    "title": getattr(e, "title", ""),
                    "participants": getattr(e, "participants_count", None),
                })
        return {"dialogs": out}

    async def send_file(self, chat_id, path):
        if not os.path.isfile(path):
            return {"error": f"файл не найден: {path}"}
        name = os.path.basename(path)

        def progress_cb(current, total):
            asyncio.ensure_future(self.broadcast({
                "event": "progress",
                "chat_id": chat_id,
                "name": name,
                "current": current,
                "total": total,
            }))

        await self.client.send_file(chat_id, path, progress_callback=progress_cb)
        return {"name": name}

    async def check_group(self, user_ids):
        """Можно ли добавить пользователя в группу: нужен username или контакт."""
        result = {}
        for uid in user_ids:
            try:
                entity = await self.client.get_entity(int(uid))
                result[uid] = bool(getattr(entity, "username", "")) or bool(
                    getattr(entity, "contact", False))
            except Exception as e:
                log.warning("check_group %s: %s", uid, e)
                result[uid] = False
        return {"result": result}

    async def send(self, chat_id, text):
        if not text:
            return {"error": "пустое сообщение"}
        sent = await self.client.send_message(chat_id, text)
        # исходящие не порождают событий — раздаём их клиентам сами,
        # чтобы клиенты одного relay могли сигналить друг другу
        now = time.monotonic()
        self.seen_ids = {k: v for k, v in self.seen_ids.items() if now - v < 300}
        if getattr(sent, "id", None):
            self.seen_ids[sent.id] = now
        await self.broadcast({
            "event": "message",
            "chat_id": chat_id,
            "from_id": self.self_id,
            "msg_id": getattr(sent, "id", 0),
            "text": text,
        })
        return {}

    async def handle_cmd(self, cmd: str, data: dict) -> dict:
        handlers = {
            "ping": lambda: self._conn_state(),
            "status": self.status,
            "shutdown": self.shutdown,
            "login_phone": lambda: self.login_phone(data.get("phone", "")),
            "login_qr": self.login_qr,
            "login_code": lambda: self.login_code(data.get("code", ""), data.get("code_hash", "")),
            "login_password": lambda: self.login_password(data.get("password", "")),
            "logout": self.logout,
            "resolve": lambda: self.resolve(data.get("username", "")),
            "call": lambda: self.call(data.get("username", ""), data.get("title", ""),
                                      data.get("user_id")),
            "call_group": lambda: self.call_group(data.get("usernames", []), data.get("title", "")),
            "invite": lambda: self.invite(data.get("chat_id", 0), data.get("username", "")),
            "chat_info": lambda: self.chat_info(data.get("chat_id", 0)),
            "dialogs": self.dialogs,
            "leave": lambda: self.leave(data.get("chat_id", 0)),
            "send": lambda: self.send(data.get("chat_id", 0), data.get("text", "")),
            "send_file": lambda: self.send_file(data.get("chat_id", 0), data.get("path", "")),
            "check_group": lambda: self.check_group(data.get("user_ids", [])),
        }
        fn = handlers.get(cmd)
        if fn is None:
            return {"error": f"unknown command: {cmd}"}
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            log.exception("command %s failed", cmd)
            return {"error": str(e)}
        result = result or {}
        result.setdefault("ok", True)
        return result


async def ws_handler(relay: Relay, ws):
    relay.clients.add(ws)
    peer = getattr(ws, "remote_address", None)
    log.info("gui connected %s (клиентов: %d)", peer, len(relay.clients))
    await relay._push_conn_state(force=True)
    try:
        async for raw in ws:
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"ok": False, "error": "bad json"}))
                continue
            cmd = req.get("cmd", "")
            t0 = time.monotonic()
            resp = await relay.handle_cmd(cmd, req)
            dt = (time.monotonic() - t0) * 1000
            if resp.get("ok"):
                log.info("cmd %s -> ok (%.0f ms)", cmd, dt)
            else:
                log.warning("cmd %s -> FAIL %s (%.0f ms)",
                            cmd, resp.get("error"), dt)
            await ws.send(json.dumps(resp))
    except websockets.exceptions.ConnectionClosed as e:
        log.info("gui %s connection closed: %s", peer, e)
    finally:
        relay.clients.discard(ws)
        log.info("gui disconnected %s (клиентов: %d)", peer, len(relay.clients))


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    keys = extract_keys()
    log.info("keys: api_id=%s source=%s", keys["api_id"], keys["source"])

    relay = Relay(keys)
    await relay.start()

    async with websockets.serve(lambda ws: ws_handler(relay, ws), WS_ADDR[0], WS_ADDR[1],
                                max_size=8 << 20):
        log.info("relay listening on ws://%s:%d", *WS_ADDR)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
