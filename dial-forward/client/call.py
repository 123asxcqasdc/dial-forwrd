#!/usr/bin/env python3
"""Сессия звонка: обмен offer/answer/ice через группу Telegram.

client_id — уникальный id инстанса клиента. Сигналы с чужим client_id
принимаются, со своим — игнорируются. Это позволяет двум клиентам
одного аккаунта (и через один relay) работать на одну группу.

Поле "to" адресует сигнал конкретному клиенту (групповой меш):
пустое = всем участникам 1:1 звонка.
"""
import asyncio
import secrets

_MAIN_LOOP = None


def capture_loop():
    """Вызвать из main-корутины (в потоке с работающим asyncio loop)."""
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()


def run_async(coro):
    """Запускает корутину из не-asyncio контекста (например, GLib-callback)."""
    if _MAIN_LOOP is None:
        print("[call] run_async: loop не захвачен — asyncio.run в этом потоке", flush=True)
        return asyncio.run(coro)

    def _schedule():
        fut = asyncio.ensure_future(coro)
        fut.add_done_callback(
            lambda t: t.exception() and print(
                f"[call] run_async task: {t.exception()!r}", flush=True))

    _MAIN_LOOP.call_soon_threadsafe(_schedule)


class CallSession:
    def __init__(self, client, chat_id, peer_id, self_id, client_id=None):
        self.client = client
        self.chat_id = chat_id
        self.peer_id = peer_id
        self.self_id = self_id
        self.client_id = client_id or secrets.token_hex(4)
        self.role = None  # "offerer" | "answerer"
        self.on_remote_offer = None   # (sdp, peer_client_id)
        self.on_remote_answer = None  # (sdp, peer_client_id)
        self.on_remote_ice = None     # (candidate, peer_client_id)
        self.on_remote_join = None    # (user_id, peer_client_id)
        self.on_hangup = None         # (peer_client_id)
        self._ice_buffer = []

    # ---- отправка ----

    async def send_offer(self, sdp, to=""):
        self.role = "offerer"
        print(f"[call] send_offer: {len(sdp)} b to={to!r}", flush=True)
        await self.client.send_signal(self.chat_id,
                                      {"type": "offer", "sdp": sdp, "cid": self.client_id, "to": to})

    async def send_answer(self, sdp, to=""):
        self.role = "answerer"
        await self.client.send_signal(self.chat_id,
                                      {"type": "answer", "sdp": sdp, "cid": self.client_id, "to": to})

    async def send_ice(self, candidate, to=""):
        await self.client.send_signal(self.chat_id,
                                      {"type": "ice", "candidate": candidate,
                                       "cid": self.client_id, "to": to})

    async def announce_join(self):
        await self.client.send_signal(self.chat_id,
                                      {"type": "join", "cid": self.client_id,
                                       "user_id": self.self_id, "to": ""})

    async def send_ring(self, title=""):
        await self.client.send_signal(self.chat_id,
                                      {"type": "ring", "cid": self.client_id,
                                       "to": "", "title": title})

    async def hangup(self, to=""):
        await self.client.send_signal(self.chat_id,
                                      {"type": "hangup", "cid": self.client_id, "to": to})

    # ---- приём (вызывается из asyncio-задачи listener) ----

    def handle(self, chat_id, from_id, payload):
        if chat_id != self.chat_id or payload.get("cid") == self.client_id:
            return
        to = payload.get("to", "")
        if to and to != self.client_id:
            return
        t = payload.get("type")
        peer_cid = payload.get("cid", "")
        if t == "offer" and self.on_remote_offer:
            self.on_remote_offer(payload.get("sdp", ""), peer_cid)
        elif t == "answer" and self.on_remote_answer:
            self.on_remote_answer(payload.get("sdp", ""), peer_cid)
        elif t == "ice" and self.on_remote_ice:
            self.on_remote_ice(payload.get("candidate", ""), peer_cid)
        elif t == "join" and self.on_remote_join:
            self.on_remote_join(payload.get("user_id", 0), peer_cid)
        elif t == "hangup" and self.on_hangup:
            self.on_hangup(peer_cid)


class CallManager:
    """Создаёт группу звонка и раздаёт сигналы по сессиям."""

    def __init__(self, relay_client, self_id):
        self.client = relay_client
        self.self_id = self_id
        self.sessions = {}  # chat_id -> CallSession

    async def call_user(self, username, title=None):
        r = await self.client.call(username, title)
        if not r.get("ok"):
            return None, r
        chat_id = r["chat_id"]
        session = CallSession(self.client, chat_id, r.get("user_id"), self.self_id)
        self.sessions[chat_id] = session
        return session, r

    def on_signal(self, chat_id, from_id, payload):
        session = self.sessions.get(chat_id)
        if session:
            session.handle(chat_id, from_id, payload)
