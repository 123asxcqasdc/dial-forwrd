#!/usr/bin/env python3
"""Клиент relay: WS-команды (короткие соединения) + постоянный приём событий."""
import asyncio
import json
import time

import websockets

from protocol import decode


def _ts():
    return time.strftime("%H:%M:%S")


class RelayClient:
    def __init__(self, url="ws://127.0.0.1:4545"):
        self.url = url
        self.on_message = None   # (chat_id, from_id, msg_id, text)
        self.on_signal = None    # (chat_id, from_id, payload)
        self.on_event = None     # любой event (dict)

    # ---- команды (каждая на своём соединении, ответ ждём) ----

    async def cmd(self, c: str, **kw) -> dict:
        try:
            t0 = time.monotonic()
            async with websockets.connect(self.url, max_size=8 << 20,
                                          open_timeout=5) as ws:
                await ws.send(json.dumps({"cmd": c, **kw}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), 40)
                    msg = json.loads(raw)
                    if "event" in msg:
                        self._dispatch(msg)
                    else:
                        print(f"[{_ts()}][relay_client] cmd({c}) -> "
                              f"{'ok' if msg.get('ok') else 'FAIL ' + str(msg.get('error'))} "
                              f"за {(time.monotonic() - t0) * 1000:.0f} мс",
                              flush=True)
                        return msg
        except Exception as e:
            print(f"[{_ts()}][relay_client] cmd({c}): {type(e).__name__}: {e}",
                  flush=True)
            raise

    # ---- события (постоянное соединение) ----

    def _dispatch(self, msg: dict):
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception as e:
                print(f"[relay_client] on_event: {e!r}", flush=True)
        if msg.get("event") == "message":
            try:
                if self.on_message:
                    self.on_message(msg.get("chat_id"), msg.get("from_id"),
                                    msg.get("msg_id"), msg.get("text"))
                payload = decode(msg.get("text") or "")
                if payload and self.on_signal:
                    self.on_signal(msg.get("chat_id"), msg.get("from_id"), payload)
            except Exception as e:
                print(f"[relay_client] обработка сообщения: {e!r}", flush=True)

    async def listen(self):
        attempt = 0
        while True:
            try:
                async with websockets.connect(self.url, max_size=8 << 20,
                                              open_timeout=5,
                                              ping_interval=20) as ws:
                    if attempt:
                        print(f"[{_ts()}][relay_client] listen: подключён "
                              f"после {attempt} неудач", flush=True)
                    else:
                        print(f"[{_ts()}][relay_client] listen: подключён",
                              flush=True)
                    attempt = 0
                    async for raw in ws:
                        try:
                            self._dispatch(json.loads(raw))
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                attempt += 1
                delay = min(3 * attempt, 15)
                print(f"[{_ts()}][relay_client] listen: ошибка "
                      f"{type(e).__name__}: {e} — повтор через {delay}с "
                      f"(попытка {attempt})", flush=True)
                await asyncio.sleep(delay)

    # ---- хелперы ----

    async def status(self):
        return await self.cmd("status")

    async def resolve(self, username):
        return await self.cmd("resolve", username=username)

    async def call(self, username=None, title=None, user_id=None):
        return await self.cmd("call", username=username or "", title=title or "",
                              user_id=user_id)

    async def call_group(self, usernames, title=None):
        return await self.cmd("call_group", usernames=usernames, title=title or "")

    async def invite(self, chat_id, username):
        return await self.cmd("invite", chat_id=chat_id, username=username)

    async def dialogs(self):
        return await self.cmd("dialogs")

    async def leave(self, chat_id):
        return await self.cmd("leave", chat_id=chat_id)

    async def chat_info(self, chat_id):
        return await self.cmd("chat_info", chat_id=chat_id)

    async def send_message(self, chat_id, text):
        return await self.cmd("send", chat_id=chat_id, text=text)

    async def send_signal(self, chat_id, payload):
        from protocol import encode
        return await self.cmd("send", chat_id=chat_id, text=encode(payload))
