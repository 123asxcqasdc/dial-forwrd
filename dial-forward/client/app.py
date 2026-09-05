#!/usr/bin/env python3
"""Dial Forward — клиент P2P-звонков через группу Telegram.

Меню: Аккаунт | Звонок (личные чаты) | Групповой звонок
Управление звонком: микрофон, видео, стрим, пригласить, завершить.
При отклонении/завершении группа звонка удаляется.
Закрытие окна = работа в фоне (входящие принимаются).
"""
import asyncio
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

# под pythonw.exe stdout/stderr равны None — print() падает
for _n in ("stdout", "stderr"):
    if getattr(sys, _n, None) is None:
        try:
            setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))
        except OSError:
            pass

from call import CallSession, capture_loop, run_async
from relay_client import RelayClient
from webrtc import WebRtcPeer

WS_URL = "ws://127.0.0.1:4545"
CLIENT_ID = "dialfwd-gui-" + secrets.token_hex(3)

# ---------- автообновление ----------
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_PATH = os.path.join(APP_ROOT, "VERSION")


def _here_dir():
    """Каталог, рядом с которым лежат icons/settings/VERSION.
    В dev — client/; в PyInstaller-frozen — каталог ресурсов (_MEIPASS/_internal)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _res(*parts):
    return os.path.join(_here_dir(), *parts)


UPDATE_BASES = [
    "https://uliigra2.c6t.ru/dial-forward/",
    "https://raw.githubusercontent.com/123asxcqasdc/uliigra2/main/dial-forward/",
]
UPDATE_FILES = [
    "client/app.py", "client/call.py", "client/protocol.py",
    "client/relay_client.py", "client/webrtc.py",
    "relay/relay.py", "launcher.py",
]
UPD_CHECK_INTERVAL = 3 * 3600   # фоновая проверка каждые 3 часа
UPD_INTERACT_THROTTLE = 300     # при взаимодействии — не чаще раза в 5 минут
RESTART_CODE = 75               # launcher перезапускает relay+app
MSI_EXIT_CODE = 76              # выход без перезапуска: идёт MSI-обновление
MSI_URL = "https://github.com/123asxcqasdc/dial-forwrd/releases/latest/download/DialForward.msi"
RELEASES_API = "https://api.github.com/repos/123asxcqasdc/dial-forwrd/releases/latest"
MSI_MIN_SIZE = 8 << 20          # меньше 8 МБ — это не DialForward.msi
APP_LOCK_PORT = 4548            # single-instance: локальный порт-замок


def acquire_app_lock():
    """Вернёт слушающий сокет, если мы первый экземпляр; иначе None."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", APP_LOCK_PORT))
        srv.listen(4)
        return srv
    except OSError:
        try:
            srv.close()
        except OSError:
            pass
        return None


def notify_running_app():
    """Попросить уже запущенный экземпляр показать окно."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", APP_LOCK_PORT),
                                      timeout=2) as c:
            c.sendall(b"show\n")
    except OSError:
        pass


def log(msg):
    print(msg, flush=True)


class CallHub:
    """Несколько WebRtcPeer на одну группу (меш). Работает в tk-потоке."""

    def __init__(self, app):
        self.app = app
        self.chat_id = None
        self.session = None
        self.is_offerer = False
        self.group_mode = False
        self.peers = {}  # peer_cid|None -> WebRtcPeer
        self.muted = False

    def begin_call(self, chat_id, offerer, group_mode=False):
        self.chat_id = chat_id
        self.is_offerer = offerer
        self.group_mode = group_mode
        self.session = CallSession(self.app.relay, chat_id, 0, self.app.self_id,
                                   client_id=CLIENT_ID)
        self.session.on_remote_offer = self.on_offer
        self.session.on_remote_answer = self.on_answer
        self.session.on_remote_ice = self.on_ice
        self.session.on_remote_join = self.on_join
        self.session.on_hangup = self.on_hangup
        run_async(self.session.announce_join())
        if offerer and not group_mode:
            self._add_peer(None, offerer=True)
        elif offerer and group_mode:
            run_async(self.session.send_ring("Групповой звонок"))

    def handle(self, chat_id, from_id, payload):
        if self.session:
            self.session.handle(chat_id, from_id, payload)

    # ---- сигналы ----

    def on_offer(self, sdp, cid):
        if not cid or cid == CLIENT_ID:
            return
        peer = self.peers.get(cid)
        if peer is None:
            peer = self.peers.get(None)
            if peer is not None and cid:
                self.peers[cid] = peer
                del self.peers[None]
        if peer is None:
            self.app.on_incoming_accepted()
            peer = self._add_peer(cid, offerer=False)
        else:
            log("[hub] повторный офер — ренегоциация (видео)")
        peer.set_remote_offer(sdp)

    def on_answer(self, sdp, cid):
        peer = self.peers.get(cid) or self.peers.get(None)
        if peer is None:
            return
        if cid and self.peers.get(None) is not None:
            self.peers[cid] = peer
            del self.peers[None]
        peer.set_remote_answer(sdp)

    def on_ice(self, candidate, cid):
        peer = self.peers.get(cid) or self.peers.get(None)
        if peer is None:
            return
        peer.add_remote_ice(candidate)

    def on_join(self, user_id, cid):
        if not cid or cid == CLIENT_ID or user_id == self.app.self_id:
            return
        if not (self.is_offerer and self.group_mode):
            return
        if cid in self.peers:
            return
        log(f"[hub] участник присоединился: user_id={user_id} cid={cid[:12]}...")
        peer = self._add_peer(cid, offerer=True)
        peer.begin_negotiation()

    def on_hangup(self, cid):
        peer = self.peers.pop(cid, None)
        if peer is None and None in self.peers:
            peer = self.peers.pop(None)
        if peer:
            peer.close()
        self.app.on_participant_left()
        if not self.peers:
            self.end_call("звонок завершён")

    def _add_peer(self, cid, offerer):
        name = f"p{len(self.peers)}"
        if sys.platform == "win32":
            audio_src, audio_sink = "autoaudiosrc", "autoaudiosink"
        else:
            audio_src, audio_sink = "pulsesrc", "pulsesink"
        peer = WebRtcPeer(audio_src=audio_src, audio_sink=audio_sink,
                          name=name, auto_play=offerer)
        to = cid or ""
        peer.on_offer_ready = lambda sdp: run_async(self.session.send_offer(sdp, to=to))
        peer.on_answer_ready = lambda sdp: run_async(self.session.send_answer(sdp, to=to))
        peer.on_ice_candidate = lambda c: run_async(self.session.send_ice(c, to=to))
        peer.on_connection_state = self.app.on_peer_state
        peer.start()
        self.peers[cid] = peer
        if offerer:
            peer.begin_negotiation()
        if self.muted:
            peer.set_muted(True)
        return peer

    def set_muted(self, muted):
        self.muted = muted
        for peer in self.peers.values():
            peer.set_muted(muted)

    def end_call(self, reason, delete_group=True):
        for peer in self.peers.values():
            peer.close()
        self.peers.clear()
        if self.session:
            run_async(self.session.hangup())
            self.session = None
        if delete_group and self.chat_id:
            self.app.do_cmd({"cmd": "leave", "chat_id": self.chat_id})
        self.app.on_call_ended(reason)


class DialApp:
    def __init__(self, root, auto_call=None, auto_answer=False,
                 start_minimized=False, lock_srv=None):
        self.root = root
        self.root.title("Dial Forward")
        self.root.geometry("640x700")
        self.root.resizable(False, False)
        self._apply_icon()
        self._init_tray()
        self.resp_q = queue.Queue()
        self.start_minimized = start_minimized
        self.upd_deferred = None      # версия, отложенная пользователем
        self.restart_code = 0         # 75 = перезапуск после обновления
        self._last_upd_check = 0.0
        self.lock_srv = lock_srv
        self.status_var = tk.StringVar(value="Подключение к relay...")
        self.self_id = None
        self.self_name = ""
        self.hub = CallHub(self)
        self.incoming = None   # (chat_id, payload)
        self.in_call = False
        self.contacts = []
        self._files_pending = 0
        self.boot_call = auto_call
        self.auto_answer = auto_answer
        self.settings = self._load_settings()
        self.progress_var = tk.StringVar(value="")

        self.relay = RelayClient(WS_URL)
        self.relay.on_signal = self._on_signal
        self.relay.on_event = self._on_relay_event

        self._build()
        if start_minimized:
            self.root.withdraw()
            log("[app] старт свёрнутым (автозапуск)")
        self.root.after(150, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._to_background)
        threading.Thread(target=self._listener, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()
        if lock_srv is not None:
            threading.Thread(target=self._lock_listen, daemon=True).start()
        self.root.after(1000, self._conn_watchdog)
        log(f"[app] старт, client_id={CLIENT_ID}")
        self.do_cmd({"cmd": "status"}, log_reply=True)

    # ---------- фоновая работа ----------

    def _listener(self):
        async def run():
            capture_loop()
            while True:
                try:
                    await self.relay.listen()
                except Exception as e:
                    log(f"[listen] {e!r}")
                await asyncio.sleep(3)
        asyncio.run(run())

    def _to_background(self):
        log("[app] работаю в фоне (трей), входящие принимаются")
        self.root.withdraw()

    def _show_window(self):
        """Показать окно поверх остальных (входящий звонок, клик по трею)."""
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _init_tray(self):
        """Трей-иконка (pystray): при закрытии окно полностью скрывается,
        приложение продолжает принимать входящие звонки."""
        try:
            import pystray
            from PIL import Image
        except ImportError:
            self.tray = None
            log("[app] трей недоступен (нет pystray/PIL) — окно сворачивается")
            return
        base = _res("icons")
        path = os.path.join(base, "dial_forward.png")
        if not os.path.isfile(path):
            self.tray = None
            log("[app] трей недоступен (нет иконки)")
            return
        image = Image.open(path)
        menu = pystray.Menu(
            pystray.MenuItem("Показать", lambda icon, item: self._show_window(),
                             default=True),
            pystray.MenuItem("Выйти", lambda icon, item: self._quit()),
        )
        self.tray = pystray.Icon("dial-forward", image, "Dial Forward", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()
        log("[app] трей готов")

    # ---------- UI ----------

    def _apply_icon(self):
        """Иконка приложения вместо дефолтной (X) — PNG рядом с app.py."""
        base = _res("icons")
        for name in ("dial_forward.png", "dial_forward_64.png"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                try:
                    img = tk.PhotoImage(file=path)
                    self.root.iconphoto(True, img)
                    self._icon_ref = img
                    return
                except tk.TclError:
                    pass

    def _build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Dial Forward", font=("Sans", 15, "bold")).pack(pady=(0, 6))

        self.screens = {}
        sc = ttk.Frame(outer)
        sc.pack(fill="both", expand=True)

        # ---- главное меню ----
        w = ttk.Frame(sc, padding=8)
        # --- индикатор подключения ---
        conn = ttk.Frame(w)
        conn.pack(pady=(2, 6))
        self.conn_dot = tk.Canvas(conn, width=18, height=18,
                                  highlightthickness=0)
        self._dot_item = self.conn_dot.create_oval(3, 3, 15, 15,
                                                   fill="#9e9e9e", outline="")
        self.conn_dot.pack(side="left", padx=(0, 7))
        self.conn_text = tk.StringVar(value="Подключение к релею...")
        self.conn_label = ttk.Label(conn, textvariable=self.conn_text,
                                    font=("Sans", 11, "bold"))
        self.conn_label.pack(side="left")
        ttk.Label(w, textvariable=self.status_var, foreground="#b00000",
                  wraplength=520, justify="center").pack(pady=8)
        self.btn_login_menu = ttk.Button(w, text="Войти в аккаунт", width=30,
                                         command=self._go_login)
        self.btn_login_menu.pack(pady=5)
        ttk.Button(w, text="Звонок — контакты", width=30,
                   command=self._go_contacts).pack(pady=5)
        ttk.Button(w, text="Групповой звонок", width=30,
                   command=self._go_contacts_group).pack(pady=5)
        ttk.Button(w, text="Аккаунт", width=30,
                   command=lambda: self._go("account")).pack(pady=5)
        ttk.Button(w, text="Настройки", width=30,
                   command=lambda: self._go("settings")).pack(pady=5)
        ttk.Button(w, text="Свернуть в фон", width=30,
                   command=self._to_background).pack(pady=5)
        self.btn_update = ttk.Button(w, text="⟳ Обновить приложение", width=30,
                                     command=self._do_update)
        ttk.Button(w, text="Выход", width=30, command=self._quit).pack(pady=5)
        self.screens["main"] = w

        # ---- аккаунт ----
        a = ttk.Frame(sc, padding=8)
        self.acct_info = ttk.Label(a, text="—", justify="left", font=("Sans", 11))
        self.acct_info.pack(anchor="w", pady=6)
        ttk.Button(a, text="Обновить",
                   command=lambda: self.do_cmd({"cmd": "status"}, True)).pack(pady=4)
        ttk.Button(a, text="Войти в аккаунт", command=self._go_login).pack(pady=4)
        ttk.Button(a, text="Выйти из аккаунта", command=self._logout).pack(pady=4)
        ttk.Button(a, text="← Главное меню", command=lambda: self._go("main")).pack(pady=4)
        self.screens["account"] = a

        # ---- вход в аккаунт (в приложении) ----
        lg = ttk.Frame(sc, padding=8)
        ttk.Label(lg, text="Вход в Telegram", font=("Sans", 13, "bold")).pack(pady=(0, 8))
        self.lg_screens = {}
        self.lg_status = tk.StringVar(value="")
        lw = ttk.Frame(lg, padding=8)
        ttk.Label(lw, text="Как войти?").pack(pady=6)
        ttk.Button(lw, text="По номеру телефона", width=28,
                   command=lambda: self._lg_go("phone")).pack(pady=4)
        ttk.Button(lw, text="По QR-коду", width=28, command=self._lg_start_qr).pack(pady=4)
        ttk.Button(lw, text="← Главное меню", width=28,
                   command=lambda: self._go("main")).pack(pady=4)
        self.lg_screens["welcome"] = lw
        lp = ttk.Frame(lg, padding=8)
        ttk.Label(lp, text="Номер телефона (международный формат, +79...):",
                  wraplength=520, justify="left").pack(anchor="w", pady=4)
        self.lg_phone = ttk.Entry(lp, width=36)
        self.lg_phone.pack(pady=4)
        self.lg_phone.bind("<Return>", lambda e: self._lg_send_code())
        ttk.Button(lp, text="Отправить код", command=self._lg_send_code).pack(pady=4)
        ttk.Button(lp, text="← Назад", command=lambda: self._lg_go("welcome")).pack(pady=4)
        self.lg_screens["phone"] = lp
        lc = ttk.Frame(lg, padding=8)
        ttk.Label(lc, text="Код из Telegram/SMS:").pack(anchor="w", pady=4)
        self.lg_code = ttk.Entry(lc, width=36)
        self.lg_code.pack(pady=4)
        self.lg_code.bind("<Return>", lambda e: self._lg_login_code())
        ttk.Button(lc, text="Войти по коду", command=self._lg_login_code).pack(pady=4)
        ttk.Button(lc, text="← Назад", command=lambda: self._lg_go("phone")).pack(pady=4)
        self.lg_screens["code"] = lc
        lps = ttk.Frame(lg, padding=8)
        ttk.Label(lps, text="Пароль двухфакторки:").pack(anchor="w", pady=4)
        self.lg_password = ttk.Entry(lps, width=36, show="*")
        self.lg_password.pack(pady=4)
        self.lg_password.bind("<Return>", lambda e: self._lg_login_password())
        ttk.Button(lps, text="Войти с паролем", command=self._lg_login_password).pack(pady=4)
        ttk.Button(lps, text="← Назад", command=lambda: self._lg_go("code")).pack(pady=4)
        self.lg_screens["password"] = lps
        lq = ttk.Frame(lg, padding=8)
        self.lg_qr_canvas = tk.Canvas(lq, width=240, height=240, bg="white",
                                      highlightthickness=1, highlightbackground="#cccccc")
        self.lg_qr_canvas.pack(pady=6)
        ttk.Label(lq, text="Откройте Telegram на телефоне:\n"
                          "Настройки → Устройства → Подключить устройство\n"
                          "и отсканируйте код.", justify="center").pack(pady=4)
        self.lg_qr_status = tk.StringVar(value="")
        ttk.Label(lq, textvariable=self.lg_qr_status, foreground="#b00000",
                  wraplength=320, justify="center").pack(pady=4)
        ttk.Button(lq, text="Обновить QR", command=self._lg_start_qr).pack(pady=4)
        ttk.Button(lq, text="← Назад", command=lambda: self._lg_go("welcome")).pack(pady=4)
        self.lg_screens["qr"] = lq
        ttk.Label(lg, textvariable=self.lg_status, foreground="#b00000",
                  wraplength=520, justify="center").pack(pady=4)
        self.lg_screens["welcome"].pack(fill="both", expand=True)
        self.screens["login"] = lg

        # ---- настройки ----
        st = ttk.Frame(sc, padding=8)
        ttk.Label(st, text="Настройки", font=("Sans", 13, "bold")).pack(pady=6)
        self.logs_var = tk.BooleanVar(value=self.settings.get("show_logs", True))
        ttk.Checkbutton(st, text="Показывать логи", variable=self.logs_var,
                        command=self._toggle_logs).pack(anchor="w", pady=4)
        ttk.Button(st, text="← Главное меню", command=lambda: self._go("main")).pack(pady=4)
        self.screens["settings"] = st

        # ---- контакты (личные чаты) ----
        c = ttk.Frame(sc, padding=8)
        ttk.Label(c, text="Люди, с которыми есть личные чаты\n"
                          "(Ctrl/Shift — выбор нескольких для группового звонка):",
                  wraplength=520, justify="left").pack(anchor="w", pady=4)
        self.contact_list = tk.Listbox(c, height=11, selectmode="extended")
        self.contact_list.pack(fill="both", expand=True, pady=4)
        row = ttk.Frame(c)
        row.pack(fill="x")
        ttk.Button(row, text="Позвонить", command=self._call_selected).pack(side="left", padx=4)
        ttk.Button(row, text="Групповой звонок", command=self._call_group_selected).pack(side="left", padx=4)
        ttk.Button(row, text="Обновить",
                   command=lambda: self.do_cmd({"cmd": "dialogs"}, True)).pack(side="left", padx=4)
        ttk.Button(row, text="← Меню", command=lambda: self._go("main")).pack(side="right", padx=4)
        self.screens["contacts"] = c

        # ---- звонок ----
        cl = ttk.Frame(sc, padding=8)
        self.call_title = tk.StringVar(value="Звонок")
        ttk.Label(cl, textvariable=self.call_title, font=("Sans", 13, "bold")).pack(pady=4)
        self.call_status = tk.StringVar(value="")
        ttk.Label(cl, textvariable=self.call_status, foreground="#b00000",
                  wraplength=520, justify="center").pack(pady=4)
        self.parts_list = ttk.Label(cl, text="", justify="left")
        self.parts_list.pack(anchor="w", pady=4)
        ctl = ttk.Frame(cl)
        ctl.pack(pady=6)
        self.btn_mic = ttk.Button(ctl, text="Микро: вкл", width=13, command=self._toggle_mic)
        self.btn_mic.grid(row=0, column=0, padx=4)
        ttk.Button(ctl, text="Файлы", width=13, command=self._send_files).grid(row=0, column=1, padx=4)
        ttk.Button(ctl, text="Пригласить", width=13, command=self._invite).grid(row=0, column=2, padx=4)
        self.btn_hangup = ttk.Button(cl, text="Завершить звонок", width=32,
                                     command=lambda: self.hub.end_call("вы завершили звонок"))
        self.btn_hangup.pack(pady=6)
        self.screens["call"] = cl

        # ---- входящий звонок ----
        inc = ttk.Frame(sc, padding=8)
        self.inc_title = tk.StringVar(value="Входящий звонок")
        ttk.Label(inc, textvariable=self.inc_title, font=("Sans", 13, "bold")).pack(pady=6)
        ttk.Button(inc, text="Ответить", width=24, command=self._answer_incoming).pack(pady=5)
        ttk.Button(inc, text="Отклонить", width=24, command=self._decline_incoming).pack(pady=5)
        self.screens["incoming"] = inc

        # ---- логи ----
        self.lf = ttk.Frame(outer)
        ttk.Label(self.lf, text="Логи:").pack(anchor="w")
        self.logw = scrolledtext.ScrolledText(self.lf, height=9, state="disabled", width=68)
        self.logw.pack(fill="x", pady=(0, 6))

        # ---- прогресс текущего действия ----
        self.progress_holder = ttk.Frame(outer)
        ttk.Label(self.progress_holder, textvariable=self.progress_var,
                  foreground="#333333").pack(side="left", padx=(0, 8))
        self.progress = ttk.Progressbar(self.progress_holder, length=280, mode="determinate")
        self.progress.pack(side="left")
        self.progress_holder.pack(fill="x", pady=(0, 6))

        if self.settings.get("show_logs", True):
            self.lf.pack(fill="x")

        self._go("main")

    def _go(self, name):
        self._maybe_check_update()
        for f in self.screens.values():
            f.pack_forget()
        self.screens[name].pack(fill="both", expand=True)

    def _go_contacts(self):
        self._go("contacts")
        self.do_cmd({"cmd": "dialogs"}, True)

    def _go_contacts_group(self):
        self._go("contacts")
        self.do_cmd({"cmd": "dialogs"}, True)

    def _go_login(self):
        self._go("login")
        self._lg_go("welcome")
        self.do_cmd({"cmd": "status"})

    # ---------- вход (в приложении) ----------

    def _lg_go(self, name):
        for n, f in self.lg_screens.items():
            f.pack_forget()
        self.lg_screens[name].pack(fill="both", expand=True)

    def _lg_send_code(self):
        phone = self.lg_phone.get().strip()
        if not phone:
            messagebox.showwarning("Dial Forward", "Введите номер телефона")
            return
        self.lg_status.set("Отправляю код...")
        self.do_cmd({"cmd": "login_phone", "phone": phone})

    def _lg_login_code(self):
        code = self.lg_code.get().strip()
        if not code:
            messagebox.showwarning("Dial Forward", "Введите код")
            return
        self.lg_status.set("Проверяю код...")
        self.do_cmd({"cmd": "login_code", "code": code})

    def _lg_login_password(self):
        password = self.lg_password.get()
        if not password:
            messagebox.showwarning("Dial Forward", "Введите пароль 2FA")
            return
        self.lg_status.set("Проверяю пароль...")
        self.do_cmd({"cmd": "login_password", "password": password})

    def _lg_start_qr(self):
        self._lg_go("qr")
        self.lg_qr_status.set("Запрашиваю QR-код...")
        self.do_cmd({"cmd": "login_qr"})

    def _lg_show_qr(self, url):
        try:
            import qrcode
            from PIL import Image
            img = qrcode.make(url).convert("RGB")
            img = img.resize((230, 230), Image.Resampling.LANCZOS)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(tmp.name, format="PNG")
            tmp.close()
            self.lg_qr_photo = tk.PhotoImage(file=tmp.name)
            self.lg_qr_canvas.delete("all")
            self.lg_qr_canvas.create_image(120, 120, image=self.lg_qr_photo)
            self.lg_qr_status.set("Жду подтверждения с телефона...")
            self._log("QR получен, ждём скан")
        except Exception as e:
            self.lg_qr_status.set(f"Ошибка генерации QR: {e}")
            self._log(f"qr error: {e}")

    def _login_done(self, resp=None):
        self._log("вход выполнен")
        self.lg_status.set("Вход выполнен")
        self._go("main")

    def _quit(self):
        if self.in_call:
            self.hub.end_call("выход")
        if getattr(self, "tray", None) is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---------- автообновление ----------

    @staticmethod
    def _local_version():
        import sys as _s
        cands = [VERSION_PATH,
                 os.path.join(getattr(_s, "_MEIPASS", ""), "VERSION")]
        for p in cands:
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read().strip() or "0"
            except OSError:
                continue
        return "0"

    @staticmethod
    def _vkey(v):
        import re
        nums = re.findall(r"\d+", v or "")
        return tuple(int(x) for x in nums) if nums else (0,)

    def _remote_version(self):
        for base in UPDATE_BASES:
            try:
                with urllib.request.urlopen(base + "VERSION", timeout=10) as r:
                    return r.read().decode("utf-8", "replace").strip()
            except Exception:
                continue
        # fallback: тег последнего релиза на GitHub (v1.3.7 -> 1.3.7)
        try:
            with urllib.request.urlopen(RELEASES_API, timeout=10) as r:
                tag = json.load(r).get("tag_name", "")
            v = tag[1:] if tag.startswith("v") else tag
            if v:
                return v
        except Exception:
            pass
        return None

    def _maybe_check_update(self, force=False):
        now = time.time()
        if not force and now - self._last_upd_check < UPD_INTERACT_THROTTLE:
            return
        self._last_upd_check = now
        threading.Thread(target=self._check_update_bg, daemon=True).start()

    def _check_update_bg(self):
        rv = self._remote_version()
        if not rv:
            return
        if self._vkey(rv) > self._vkey(self._local_version()):
            self.resp_q.put(("update_avail", rv))

    def _update_loop(self):
        while True:
            time.sleep(UPD_CHECK_INTERVAL)
            self._maybe_check_update(force=True)

    def _lock_listen(self):
        """Слушаем порт-замок: второй экземпляр просит показать окно."""
        srv = self.lock_srv
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(64)
                except OSError:
                    continue
            if b"show" in data:
                self.resp_q.put(("show_window", None))

    def _do_update(self):
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        if getattr(sys, "frozen", False) or os.name == "nt":
            # установленная MSI-сборка (и вообще Windows): качаем полный MSI
            self._msi_worker()
            return
        wanted = list(UPDATE_FILES) + ["VERSION"]
        ok = True
        for rel in wanted:
            data = None
            for base in UPDATE_BASES:
                try:
                    with urllib.request.urlopen(base + rel, timeout=30) as r:
                        data = r.read()
                    break
                except Exception:
                    continue
            if data is None:
                log(f"[update] не скачать {rel}")
                ok = False
                break
            path = os.path.join(APP_ROOT, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".new"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError as e:
                log(f"[update] не записать {rel}: {e!r}")
                ok = False
                break
        self.resp_q.put(("update_done" if ok else "update_fail", None))

    def _msi_worker(self):
        """Скачивает полный DialForward.msi из последнего релиза GitHub."""
        msi = os.path.join(tempfile.gettempdir(),
                           f"DialForward-{self._remote_version() or 'update'}.msi")
        try:
            req = urllib.request.Request(MSI_URL,
                                         headers={"User-Agent": "DialForward/updater"})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(msi, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            self.resp_q.put(("dlprogress", done, total))
        except Exception as e:
            log(f"[update] скачивание MSI: {e!r}")
            try:
                os.remove(msi)
            except OSError:
                pass
            self.resp_q.put(("update_done", False))
            return
        if not (os.path.isfile(msi) and os.path.getsize(msi) > MSI_MIN_SIZE):
            log(f"[update] MSI подозрительного размера: "
                f"{os.path.getsize(msi) if os.path.isfile(msi) else 0} байт")
            self.resp_q.put(("update_done", False))
            return
        self.resp_q.put(("msi_ready", msi))

    def _on_msi_ready(self, msi_path):
        self.clear_progress()
        self.upd_deferred = None
        self.btn_update.pack_forget()
        messagebox.showinfo(
            "Обновление",
            "Новая версия скачана. Приложение закроется,\n"
            "установится обновление и запустится заново.")
        log("[update] останавливаю relay для обновления...")
        try:
            _kill_relay()
        except Exception as e:
            log(f"[update] relay: {e!r}")
        try:
            self._launch_msi_install(msi_path)
        except Exception as e:
            log(f"[update] запуск установщика: {e!r}")
            messagebox.showerror("Обновление",
                                 f"Не удалось запустить установку:\n{e}")
            return
        self.restart_code = MSI_EXIT_CODE
        # жёсткий выход: никаких импортов/разборок после запуска установщика —
        # старые файлы _internal могут уже быть удалены, и ленивый импорт упадёт
        self.root.update_idletasks()
        os._exit(MSI_EXIT_CODE)

    def _launch_msi_install(self, msi_path):
        """Отложенный msiexec: ждём полного закрытия приложения/relay,
        ставим MSI, запускаем свежую версию из стандартного пути установки."""
        if os.name != "nt":
            raise RuntimeError("полное обновление доступно только на Windows")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.environ.get("USERPROFILE", ""), "AppData", "Local")
        exe = os.path.join(local, "DialForward", "DialForward.exe")
        log_path = os.path.join(tempfile.gettempdir(), "DialForward-update.log")
        helper = os.path.join(tempfile.gettempdir(), "DialForward-update.cmd")
        cli = ("@echo off\r\n"
               "set N=0\r\n"
               ":loop\r\n"
               "set /a N+=1\r\n"
               "if %N% GTR 300 goto done\r\n"
               'tasklist /FI "IMAGENAME eq DialForward.exe" 2>nul '
               '| find /I "DialForward.exe" >nul\r\n'
               "if not errorlevel 1 goto nap\r\n"
               'tasklist /FI "IMAGENAME eq Relay.exe" 2>nul '
               '| find /I "Relay.exe" >nul\r\n'
               "if not errorlevel 1 goto nap\r\n"
               "goto done\r\n"
               ":nap\r\n"
               "ping -n 2 127.0.0.1 >nul\r\n"
               "goto loop\r\n"
               ":done\r\n"
               'msiexec /i "%1" /qn /norestart /l*v "%2"\r\n'
               'if exist "%3" start "" "%3"\r\n')
        with open(helper, "w", encoding="ascii", errors="replace") as f:
            f.write(cli)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["cmd", "/c", helper, msi_path, log_path, exe],
            creationflags=flags, close_fds=True,
            cwd=os.path.dirname(msi_path))
        log("[update] установщик запущен (отложенный msiexec)")

    def _on_update_avail(self, ver):
        if messagebox.askyesno(
                "Обновление",
                f"Доступна новая версия Dial Forward ({ver}).\n"
                "Скачать и установить сейчас?"):
            self.set_progress("Обновление...", indeterminate=True)
            self._do_update()
        else:
            self.upd_deferred = ver
            self.btn_update.pack(pady=5)

    def _on_update_done(self, success):
        self.clear_progress()
        if not success:
            messagebox.showerror("Обновление", "Не удалось скачать обновление.\n"
                                 "Проверьте интернет и попробуйте ещё раз.")
            return
        self.upd_deferred = None
        self.btn_update.pack_forget()
        if messagebox.askyesno("Обновление",
                               "Обновление установлено. Перезапустить приложение?"):
            self.restart_code = RESTART_CODE
            if getattr(self, "tray", None) is not None:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.root.destroy()

    def _logout(self):
        def done(resp):
            self.self_id = None
            self.self_name = ""
            self.status_var.set("Выход выполнен" if resp.get("ok")
                                else f"ошибка: {resp.get('error')}")
            self._go("main")
        self.do_cmd({"cmd": "logout"}, on_done=done)

    # ---------- настройки ----------

    @staticmethod
    def _settings_path():
        return _res("settings.json")

    def _load_settings(self):
        try:
            with open(self._settings_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log(f"[app] не сохранить настройки: {e!r}")

    def _toggle_logs(self):
        show = self.logs_var.get()
        self.settings["show_logs"] = show
        self._save_settings()
        if show:
            self.lf.pack(fill="x", before=self.progress_holder)
        else:
            self.lf.pack_forget()
        self._log("логи включены" if show else "логи выключены")

    # ---------- прогресс ----------

    def set_progress(self, label, indeterminate=False, maximum=100):
        self.progress_var.set(label)
        if indeterminate:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=maximum, value=0)

    def update_progress(self, current, total):
        if total > 0:
            self.progress.configure(mode="determinate", maximum=total, value=current)

    def clear_progress(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.progress_var.set("")

    def _on_relay_event(self, msg):
        ev = msg.get("event")
        if ev == "progress":
            self.resp_q.put(("progress", msg))
        elif ev == "conn":
            if not msg.get("connected"):
                self._set_conn("orange", "Telegram подключается...")
            elif msg.get("authorized"):
                name = getattr(self, "self_name", "") or ""
                self._set_conn("green", f"Онлайн: {name}" if name else "Онлайн")
            else:
                self._set_conn("blue", "Telegram онлайн — войдите в аккаунт")
        elif ev == "tg_disconnected":
            self._set_conn("orange", "Telegram переподключается...")
        elif ev == "logged_out":
            self.self_id = None
            self.self_name = ""
            self.contacts = []
            self.contact_list.delete(0, "end")
            self.status_var.set("Выполнен выход из аккаунта")
            self.btn_login_menu.pack(pady=5)
        elif ev == "qr_new":
            url = msg.get("url")
            if url:
                self._lg_show_qr(url)
                self.lg_qr_status.set("QR обновлён автоматически — отсканируйте заново")
        elif ev == "logged_in":
            self.self_id = msg.get("self_id")
            self.self_name = msg.get("first_name") or str(self.self_id)
            self.status_var.set(f"Аккаунт: {self.self_name} (id {self.self_id})")
            self.acct_info.configure(text=f"id: {self.self_id}\nимя: {self.self_name}")
            self.btn_login_menu.pack_forget()
            self._login_done()
        elif ev == "qr_password_needed":
            self.lg_status.set("QR принят с телефона! Введите пароль двухфакторки.")
            self._lg_go("password")
            self.lg_password.focus_set()
        elif ev == "qr_expired":
            self.lg_qr_status.set("QR устарел — нажмите «Обновить QR»")
        elif ev == "qr_error":
            self.lg_qr_status.set("Ошибка QR: %s" % msg.get("error"))
        elif ev == "need_login":
            self.status_var.set("Не авторизован — нажмите «Войти в аккаунт»")
            self.btn_login_menu.pack(pady=5)

    def _log(self, text):
        if not self.settings.get("show_logs", True):
            return
        self.logw.configure(state="normal")
        self.logw.insert("end", text + "\n")
        self.logw.see("end")
        self.logw.configure(state="disabled")

    # ---------- индикатор подключения ----------

    CONN_COLORS = {"gray": "#9e9e9e", "red": "#e53935", "orange": "#fb8c00",
                   "blue": "#1e88e5", "green": "#43a047"}

    def _set_conn(self, color, text):
        col = self.CONN_COLORS.get(color, self.CONN_COLORS["gray"])
        try:
            self.conn_dot.itemconfigure(self._dot_item, fill=col)
        except Exception:
            pass
        self.conn_text.set(text)
        try:
            self.conn_label.configure(foreground=col)
        except Exception:
            pass

    def _conn_watchdog(self):
        """Раз в 10с тихо пингует релей и обновляет индикатор."""
        def done(resp):
            if not resp.get("connected") and resp.get("error"):
                self._set_conn("red", "Нет соединения с релеем")
            elif not resp.get("connected"):
                self._set_conn("orange", "Telegram подключается...")
            elif resp.get("authorized"):
                name = getattr(self, "self_name", "") or ""
                self._set_conn("green", f"Онлайн: {name}" if name else "Онлайн")
            else:
                self._set_conn("blue", "Telegram онлайн — войдите в аккаунт")
            self.root.after(10000, self._conn_watchdog)
        self.do_cmd({"cmd": "ping"}, on_done=done)

    # ---------- команды relay ----------

    def do_cmd(self, cmd, log_reply=False, on_done=None):
        def run():
            try:
                async def one():
                    kw = {k: v for k, v in cmd.items() if k != "cmd"}
                    return await self.relay.cmd(cmd.get("cmd"), **kw)
                resp = asyncio.run(one())
            except Exception as e:
                resp = {"error": str(e)}
            self.resp_q.put(("resp", cmd.get("cmd"), resp, log_reply, on_done))
        threading.Thread(target=run, daemon=True).start()

    # ---------- контакты / вызовы ----------

    def _refresh_contacts(self, resp):
        """Личные чаты без групп/каналов/ботов; звонить можно тем, у кого
        есть username или они в контактах (без проверки на relay)."""
        users = [d for d in resp.get("dialogs", []) if d.get("type") == "user"
                 and not d.get("bot")]
        allowed = [d for d in users if d.get("username") or d.get("contact")]
        self.contacts = allowed
        self.contact_list.delete(0, "end")
        for d in self.contacts:
            un = ("@" + d["username"]) if d.get("username") else f"(id {d['id']})"
            self.contact_list.insert("end", f"{d.get('first_name') or d['title']}  {un}")
        self._log(f"можно позвонить: {len(allowed)} из {len(users)}"
                  f" личных чатов" if users else "личных чатов нет")
        if not allowed:
            self._log("нет пользователей для звонка (нужны username или контакты)")
        self.clear_progress()

    def _selected_users(self):
        idx = list(self.contact_list.curselection())
        return [self.contacts[i] for i in idx if i < len(self.contacts)]

    def _call_selected(self):
        users = self._selected_users()
        if not users:
            messagebox.showinfo("Dial Forward", "Выберите человека из списка")
            return
        self._start_call(users)

    def _call_group_selected(self):
        users = self._selected_users()
        if len(users) < 2:
            messagebox.showinfo("Dial Forward", "Выберите несколько человек (Ctrl+клик)")
            return
        self._start_call(users)

    def _start_call(self, users):
        def done(resp):
            self.clear_progress()
            if resp.get("error"):
                self.status_var.set("Ошибка: " + resp["error"])
                self._log("ошибка звонка: " + resp["error"])
                return
            self._enter_call(resp.get("chat_id"), len(users) > 1)
        if len(users) > 1:
            self.status_var.set("Создаю групповой звонок...")
            self.set_progress("Создаю групповой звонок...", indeterminate=True)
            self.do_cmd({"cmd": "call_group",
                         "usernames": [u.get("username") or str(u.get("id"))
                                       for u in users]}, on_done=done)
        else:
            u = users[0]
            self.status_var.set("Звонок...")
            self.set_progress("Звонок...", indeterminate=True)
            self.do_cmd({"cmd": "call", "username": u.get("username") or "",
                         "user_id": u.get("id")}, on_done=done)

    def _enter_call(self, chat_id, group_mode):
        log(f"[app] звонок начат (chat {chat_id}, group={group_mode})")
        self._log(f"звонок начат (chat {chat_id})")
        self.in_call = True
        self.incoming = None
        self.call_title.set("Групповой звонок" if group_mode else "Звонок")
        self.call_status.set("Соединяемся...")
        self.btn_mic.configure(text="Микро: вкл")
        self.parts_list.configure(text="")
        self._go("call")
        self.root.deiconify()
        self.root.lift()
        try:
            self.hub.begin_call(chat_id, offerer=True, group_mode=group_mode)
        except Exception as e:
            log(f"[hub] begin_call: {e!r}")
            self._log(f"медиа недоступно: {e}")
            self.call_status.set(f"Ошибка медиа: {e}")

    def on_incoming_accepted(self):
        self.call_status.set("Соединяемся...")

    def on_participant_left(self):
        self._log("участник отключился")

    def on_peer_state(self, state):
        log(f"[hub] ice: {state}")
        if state in ("connected", "completed"):
            self.call_status.set("Разговор")
            self._log("разговор установлен")
        elif state == "failed":
            self._log("соединение не удалось")

    def on_call_ended(self, reason):
        self.in_call = False
        self.incoming = None
        log(f"[app] {reason}")
        self._log(reason)
        self.status_var.set("Готов к звонкам")
        self._go("main")

    # ---------- входящий звонок ----------

    def _on_signal(self, chat_id, from_id, payload):
        t = payload.get("type")
        log(f"[ui] сигнал {t} (chat {chat_id})")
        if from_id and from_id == self.self_id:
            log("[ui] собственный сигнал — игнорирую")
            return
        if self.hub.session and self.hub.chat_id == chat_id:
            self.resp_q.put(("signal", chat_id, payload))
        elif t in ("offer", "ring") and not self.in_call:
            self.resp_q.put(("incoming", chat_id, payload))
        else:
            self.resp_q.put(("signal", chat_id, payload))

    def _answer_incoming(self):
        if not self.incoming:
            return
        chat_id, payload = self.incoming
        self.in_call = True
        self.hub.begin_call(chat_id, offerer=False)
        if payload:
            self.hub.handle(chat_id, 0, payload)
        self.call_title.set("Звонок")
        self.call_status.set("Соединяемся...")
        self.btn_mic.configure(text="Микро: вкл")
        self._go("call")

    def _decline_incoming(self):
        chat_id, _ = self.incoming
        self.incoming = None
        self.status_var.set("Звонок отклонён")
        self._log("звонок отклонён, группа удаляется")
        self.do_cmd({"cmd": "leave", "chat_id": chat_id})
        self._go("main")

    # ---------- управление ----------

    def _toggle_mic(self):
        if not self.hub.session:
            return
        muted = not self.hub.muted
        self.hub.set_muted(muted)
        self.btn_mic.configure(text="Микро: выкл" if muted else "Микро: вкл")
        self._log("микрофон выключен" if muted else "микрофон включён")

    def _invite(self):
        if not self.hub.chat_id:
            return
        uname = simpledialog.askstring("Dial Forward", "Пригласить (username):",
                                       parent=self.root)
        if not uname:
            return
        self._log(f"приглашаю @{uname.lstrip('@')}...")
        self.set_progress("Приглашаю...", indeterminate=True)
        self.do_cmd({"cmd": "invite", "chat_id": self.hub.chat_id, "username": uname},
                    on_done=lambda r: (self.clear_progress(), self._log(
                        "приглашён, ждём подключения"
                        if r.get("ok") else f"не удалось: {r.get('error')}")))

    def _send_files(self):
        if not self.hub.chat_id:
            return
        paths = filedialog.askopenfilenames(parent=self.root,
                                            title="Отправить файлы в чат звонка")
        if not paths:
            return
        self._files_pending = len(paths)
        self.set_progress(f"Отправка файлов ({len(paths)})...", indeterminate=True)
        for path in paths:
            self.do_cmd({"cmd": "send_file", "chat_id": self.hub.chat_id, "path": path},
                        on_done=lambda r, p=path: self._file_done(p, r))

    def _file_done(self, path, resp):
        name = os.path.basename(path)
        if resp.get("ok"):
            self._log(f"файл отправлен: {name}")
        else:
            self._log(f"не удалось отправить {name}: {resp.get('error')}")
        self._files_pending -= 1
        if self._files_pending <= 0:
            self.clear_progress()

    # ---------- ответы и события ----------

    def _poll(self):
        try:
            while True:
                item = self.resp_q.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        if self.boot_call and self.self_id and not self.in_call:
            name, self.boot_call = self.boot_call, None
            self._auto_call(name)
        self.root.after(150, self._poll)

    def _handle(self, item):
        kind = item[0]
        if kind == "resp":
            _, cmd, resp, log_reply, on_done = item
            self._on_resp(cmd, resp, log_reply, on_done)
        elif kind == "incoming":
            _, chat_id, payload = item
            self._on_incoming(chat_id, payload)
        elif kind == "signal":
            _, chat_id, payload = item
            if self.hub.session and self.hub.chat_id == chat_id:
                self.hub.handle(chat_id, 0, payload)
        elif kind == "progress":
            _, msg = item
            total = msg.get("total") or 100
            current = msg.get("current") or 0
            self.set_progress(f"Отправка: {msg.get('name', '')}", maximum=total)
            self.update_progress(current, total)
            if current >= total - 1:
                self.clear_progress()
        elif kind == "update_avail":
            _, ver = item
            self._on_update_avail(ver)
        elif kind == "dlprogress":
            _, done, total = item
            if not self.progress_var.get():
                self.set_progress("Загрузка DialForward.msi...", maximum=total)
            self.update_progress(done, total)
        elif kind == "msi_ready":
            _, msi = item
            self._on_msi_ready(msi)
        elif kind == "update_done":
            self._on_update_done(item[1] is None)
        elif kind == "show_window":
            self._show_window()
        elif kind == "err":
            _, err = item
            log(f"[app] {err}")
            self._log(err)
            self.status_var.set(err)
        elif kind == "event":
            _, msg = item
            self._on_relay_event(msg)

    def _on_resp(self, cmd, resp, log_reply, on_done):
        if log_reply and resp.get("ok"):
            extra = {k: v for k, v in resp.items() if k != "ok"}
            self._log(f"{cmd}: ok {json.dumps(extra, ensure_ascii=False)}")
        if cmd == "status":
            if resp.get("authorized"):
                self.self_id = resp.get("self_id")
                self.self_name = (resp.get("first_name") or resp.get("username")
                                  or str(resp.get("self_id")))
                self.status_var.set(f"Аккаунт: {self.self_name} (id {self.self_id})")
                self.acct_info.configure(text=f"id: {self.self_id}\nимя: {self.self_name}")
                self.btn_login_menu.pack_forget()
            else:
                self.status_var.set("Не авторизован — нажмите «Войти в аккаунт»")
                self.acct_info.configure(text="нет авторизации")
                self.btn_login_menu.pack(pady=5)
        elif cmd == "dialogs" and resp.get("ok"):
            self._refresh_contacts(resp)
        elif cmd == "login_phone" and resp.get("ok"):
            self.lg_status.set("Код отправлен. Введите код из Telegram/SMS.")
            self._lg_go("code")
            self.lg_code.focus_set()
        elif cmd == "login_code":
            if resp.get("need_password"):
                self.lg_status.set("Требуется пароль двухфакторки.")
                self._lg_go("password")
                self.lg_password.focus_set()
            elif resp.get("ok"):
                self._login_done()
        elif cmd == "login_password" and resp.get("ok"):
            self._login_done()
        elif cmd == "login_qr" and resp.get("ok") and resp.get("url"):
            self._lg_show_qr(resp["url"])
        elif resp.get("error") and cmd in ("login_phone", "login_code", "login_password", "login_qr"):
            self.lg_status.set("Ошибка: " + resp["error"])
            self._log("ошибка входа: " + resp["error"])
        if on_done:
            try:
                on_done(resp)
            except Exception as e:
                log(f"[app] on_done: {e!r}")

    def _on_incoming(self, chat_id, payload):
        if self.in_call:
            log(f"[app] уже в звонке — игнорирую офер {chat_id}")
            return
        if self.incoming and self.incoming[0] == chat_id:
            log(f"[app] дубликат входящего {chat_id} — игнорирую")
            return
        self.incoming = (chat_id, payload)
        t = payload.get("type")
        log(f"[app] входящий звонок (chat {chat_id}, type={t})")
        self._log("входящий звонок" + (" (групповой)" if t == "ring" else ""))
        self.inc_title.set(payload.get("title") or "Входящий звонок")
        self._go("incoming")
        self._show_window()
        if self.auto_answer:
            self.root.after(300, self._answer_incoming)

    # ---------- автозвонок (--call) ----------

    def _auto_call(self, username):
        def fetch(resp):
            self._refresh_contacts(resp)
            users = [d for d in self.contacts if d.get("username") == username]
            if not users:
                log(f"[app] не найден личный чат с @{username}")
                self._log(f"не найден личный чат с @{username}")
                return
            self._start_call(users)
        self.do_cmd({"cmd": "dialogs"}, on_done=fetch, log_reply=False)


def _relay_bin_dir():
    """Каталог, где лежит Relay.exe/relay.py (рядом с бандлом или в dev)."""
    if getattr(sys, "frozen", False):
        # PyInstaller --onedir: exe в корне папки DialForward, Relay.exe рядом
        return os.path.dirname(sys.executable)
    return os.path.join(APP_ROOT, "relay")


def _dev_python(subprocess):
    """Интерпретатор для dev-запуска relay: venv проекта, иначе sys.executable."""
    cands = [os.path.join(APP_ROOT, ".venv", "bin", "python"),
             os.path.join(APP_ROOT, ".venv", "Scripts", "python.exe")]
    for c in cands:
        if not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import telethon, websockets"],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return sys.executable


def _ensure_relay():
    """Поднимает relay, если он не слушает ws://127.0.0.1:4545 или устарел."""
    import subprocess
    if _ws_alive():
        running = _ws_relay_version()
        expected = DialApp._local_version()
        if running is not None and running != expected:
            log(f"[app] relay {running} != ожидаемый {expected} — перезапускаю")
            _kill_relay()
        else:
            log(f"[app] relay уже работает (v{running})")
            return
    base = _relay_bin_dir()
    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
        relay = [os.path.join(here, "Relay.exe")]
        cwd = here
    else:
        relay = [_dev_python(subprocess), os.path.join(base, "relay.py")]
        cwd = base
    log("[app] запускаю relay...")
    p = subprocess.Popen(relay, cwd=cwd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    for _ in range(80):
        if _ws_alive():
            log("[app] relay готов")
            return p
        time.sleep(0.5)
    log("[app] relay не поднялся за 40с")
    try:
        p.terminate()
    except Exception:
        pass
    return None


def _ws_alive(timeout=2):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", 4545))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _ws_relay_version(timeout=3):
    """Версия запущенного relay (WS-команда status). None при ошибке."""
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


def _kill_relay():
    """Останавливает запущенный relay (shutdown -> taskkill/pkill)."""
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

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _ws_alive():
            return
        time.sleep(0.3)
    if os.name == "nt":
        subprocess.run(["taskkill", "/IM", "Relay.exe", "/F"],
                       capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "relay.py"], capture_output=True)
    time.sleep(0.5)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Dial Forward")
    ap.add_argument("--call", metavar="USERNAME",
                    help="автоматически позвонить пользователю")
    ap.add_argument("--auto-answer", action="store_true",
                    help="автоматически принимать входящие звонки")
    ap.add_argument("--minimized", action="store_true",
                    help="запуск свёрнутым в трей (автозагрузка)")
    args = ap.parse_args()

    lock = acquire_app_lock()
    if lock is None:
        log("[app] Dial Forward уже запущен — показываю существующее окно")
        notify_running_app()
        return 0

    _ensure_relay()

    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = DialApp(root, auto_call=(args.call or "").lstrip("@") or None,
                  auto_answer=args.auto_answer,
                  start_minimized=args.minimized, lock_srv=lock)
    root.mainloop()
    return app.restart_code


if __name__ == "__main__":
    sys.exit(main())
