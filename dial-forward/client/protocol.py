#!/usr/bin/env python3
"""Протокол сигналинга поверх сообщений Telegram.

Формат: строка "#[call]" + JSON {"type": ..., ...}
Клиент шлёт такие сообщения в группу звонка, relay просто доставляет их.
"""
import json

PREFIX = "#[call]"


def encode(payload: dict) -> str:
    return PREFIX + json.dumps(payload, ensure_ascii=False)


def decode(text: str):
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    try:
        return json.loads(text[len(PREFIX):])
    except json.JSONDecodeError:
        return None


def offer(sdp: str) -> str:
    return encode({"type": "offer", "sdp": sdp})


def answer(sdp: str) -> str:
    return encode({"type": "answer", "sdp": sdp})


def ice(candidate: str) -> str:
    return encode({"type": "ice", "candidate": candidate})


def hangup() -> str:
    return encode({"type": "hangup"})
