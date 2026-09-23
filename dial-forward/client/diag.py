# -*- coding: utf-8 -*-
"""Диагностика: шифрованный сбор и передача логов разработчику.

Логи могут содержать личные данные, поэтому в открытую Firebase
пишется только один зашифрованный блоб. Расшифровать его способен
только держатель приватного ключа (эта машина разработчика).
Сеансовый AES-256-GCM ключ шифруется RSA-OAEP публичным ключом.
"""
import base64
import json
import os
import platform
import sys
import time
import urllib.request
import uuid

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PUB_KEY_TEXT = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA47TRZA0fuiFnnRv/PRaM
wo4Fl8Gz0W6gIVovsVATtLPDdIrmE6K9V2ar1Kd81QwosIRxCsJv8cTRbacIoa3x
crrPIV82A+sf9+4vo7CcWQzhLMCVWM7gryKbIoIa99S4z1Bv+Sv4W4uBS+aLpBs1
ueOfpopK0ErQvZpVD36anbKqO52Vq13ExNAS5YCvINXLSdcnBxwisUNqiIu/Fupk
hu+QZngybRqskDlibzlm2wtWYwVUln0Oh7V+RnxIb8W3ldnFxA271VhjclTLEyjM
gIQ2JCXb488xQKrH4mDyblbpyTDOwNpA3iB7HVS5dxcU+7iDFkr8z9naz5a9QBvx
6+nOEYHNvtDiDjzupQtLK+NfQR1i18d9CTLFvOT8Sy5NIdClzTjccbqMFIIJCqSg
5e6YyAM8PKe63ibkntTdoz7QM8bHVfxIOx/Y+MvO7Zo3SRNp6lclvtmo2GkR+JBY
ddnw6PG7fbQbVAiwBlOzTJVHxuUnISgPpmBekIGZWg01SVExcggBwoupQSubTTET
Smo0ehZwk03C7VCEpHahP0FXiI5cBJTpJohGQDbJZyT5OGpY1jnQJrU1Q76a8QEO
NKKGbJtnHTliaHd03w3si7pyx0BaGqj86WHxOZOI1cWIGO5fYg/63yQW68HIR3aQ
8lRrfXq7QgclDBNvhne3/yMCAwEAAQ==
-----END PUBLIC KEY-----"""

FB_BASE = "https://kcalls-default-rtdb.firebaseio.com"
FB_PATH = "/logs"
_MAX_LOG_BYTES = 4 * 1024 * 1024  # на всякий случай не больше 4 МиБ


def app_log_path():
    """Путь к app.log (тот же, что в app.py)."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") \
            or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        d = os.path.join(base, "DialForward")
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, "app.log")


def gst_log_path():
    """GStreamer/devnull-лог (dialforward.log), если есть."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "dialforward.log")
    return None


def _tail(path, limit):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def collect_logs(reason=""):
    """Собирает текст логов + технические данные (до шифрования)."""
    app = _tail(app_log_path(), _MAX_LOG_BYTES)
    gst_p = gst_log_path()
    gst = _tail(gst_p, _MAX_LOG_BYTES) if gst_p else ""
    ver = "?"
    vp = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "VERSION")
    try:
        with open(vp, encoding="utf-8") as f:
            ver = f.read().strip()
    except OSError:
        pass
    return {
        "v": ver,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plat": sys.platform,
        "py": platform.python_version(),
        "reason": reason,
        "app_log": app,
        "gst_log": gst or None,
    }


def encrypt(payload):
    """Шифрует dict: AES-256-GCM + RSA-OAEP(SHA-256). Возвращает dict."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    key = os.urandom(32)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, None)
    pub = serialization.load_pem_public_key(PUB_KEY_TEXT.encode("ascii"))
    enc_key = pub.encrypt(
        key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None))
    return {
        "ct": base64.b64encode(ct).decode("ascii"),
        "ek": base64.b64encode(enc_key).decode("ascii"),
        "n": base64.b64encode(nonce).decode("ascii"),
    }


def upload(blob):
    """Публикует зашифрованный блоб в открытую Firebase /logs."""
    name = uuid.uuid4().hex[:16]
    url = f"{FB_BASE}{FB_PATH}/{name}.json"
    body = json.dumps(blob).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    return name


def send_logs(reason=""):
    """Полный цикл: собрать → зашифровать → опубликовать.
    Возвращает (ok, message). Вызывать в фоновом потоке."""
    try:
        blob = encrypt(collect_logs(reason))
        name = upload(blob)
        return True, name
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"