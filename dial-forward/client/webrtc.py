#!/usr/bin/env python3
"""GStreamer webrtcbin peer.

Архитектура: GLib.MainLoop живёт в отдельном потоке (один на процесс —
GStreamer делит default main context, несколько потоков его бы залочили).
Вызовы из asyncio-стороны (set_remote_offer, add_remote_ice, close)
делаются через GLib.idle_add. Обратные вызовы (on_offer_ready,
on_ice_candidate, on_connection_state) вызываются в GLib-потоке —
глue должен сам переносить их в свой контекст.
"""
import threading
import os
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

Gst.init(None)


def _bundled_plugin_dirs():
    """Директории с GStreamer-плагинами в бандле (frozen Windows). В dev/Linux —
    системные пути, здесь не вмешиваемся."""
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        dirs = []
        for var in ("GST_PLUGIN_PATH", "GST_PLUGIN_PATH_1_0"):
            envp = os.environ.get(var)
            if envp:
                dirs += [p for p in envp.split(os.pathsep) if os.path.isdir(p)]
        if not dirs:
            for base in filter(None, (getattr(sys, "_MEIPASS", None),
                                      os.path.dirname(sys.executable))):
                for pkg in ("gstreamer_plugins", "gstreamer_libs"):
                    d = os.path.join(base, pkg, "lib", "gstreamer-1.0")
                    if os.path.isdir(d):
                        dirs.append(d)
        return dirs
    return []


def ensure_bundled_plugins(timeout=60):
    """Дождаться фоновой регистрации бандл-плагинов (return True, если webrtcbin есть).
    На Linux/в venv скана нет — возвращает False, системные плагины работают сами."""
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        _gst_scan.wait(timeout)
    return _gst_scan_ok[0]


# --- фоновая регистрация плагинов бандла (frozen Windows) ---
# Плагины грузим пофайлово (Gst.Plugin.load_file) прямо в процесс, минуя внешний
# gst-plugin-scanner, который в frozen-приложении не находит DLL. Процесс вызывается
# приложением со сплешем (setup_plugin_import), т.к. даёт реальный прогресс.
# Первый запуск: файла реестра нет -> импорт-цикл с прогрессом, GStreamer сам
# сохранит реестр при выходе. Повторные запуски: реестр подхватывается из кэша
# при Gst.init, импорт пропускается.
_gst_scan = threading.Event()
_gst_scan_ok = [False]
_import_started = False
_import_lock = threading.Lock()


def _plugin_files():
    files = []
    for d in _bundled_plugin_dirs():
        try:
            for n in sorted(os.listdir(d)):
                if n.lower().endswith(".dll"):
                    files.append(os.path.join(d, n))
        except OSError:
            pass
    return files


def _scan_bundled_plugins(on_progress=None):
    try:
        files = _plugin_files()
        total = len(files)
        for i, f in enumerate(files, 1):
            try:
                Gst.Plugin.load_file(f)
            except Exception:
                pass
            if on_progress:
                try:
                    on_progress(i, total)
                except Exception:
                    pass
        feats = Gst.Registry.get().get_feature_list(Gst.ElementFactory)
        _gst_scan_ok[0] = any(f.name == "webrtcbin" for f in feats)
        print(f"[webrtc] импорт библиотек: {total} файлов, "
              f"factories={len(feats)} webrtcbin={_gst_scan_ok[0]}", flush=True)
    except Exception as e:
        print(f"[webrtc] импорт библиотек сбой: {e!r}", flush=True)
    finally:
        _gst_scan.set()
        if on_progress:
            try:
                on_progress(total, total)
            except Exception:
                pass


def setup_plugin_import(on_progress=None):
    """Старт импорта GStreamer-библиотек из бандла (frozen Windows), из app после
    создания сплеша. on_progress(done, total) вызывается из фонового потока."""
    global _import_started
    with _import_lock:
        if _import_started:
            return
        _import_started = True

    if not (getattr(sys, "frozen", False) and sys.platform == "win32"):
        _gst_scan.set()
        return

    # Реестр уже подхвачен GStreamer из кэша — импорт не нужен.
    try:
        feats = Gst.Registry.get().get_feature_list(Gst.ElementFactory)
        if any(f.name == "webrtcbin" for f in feats):
            _gst_scan_ok[0] = True
            print("[webrtc] библиотеки импортированы (кэш реестра)", flush=True)
            _gst_scan.set()
            if on_progress:
                try:
                    on_progress(1, 1)
                except Exception:
                    pass
            return
    except Exception:
        pass

    threading.Thread(target=_scan_bundled_plugins, args=(on_progress,),
                     daemon=True, name="gstreamer-import").start()


class GlibRunner:
    """Один GLib main loop в одном потоке на весь процесс."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._loop = None
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="glib-main")
        self._thread.start()
        self._started.wait(5)

    def _run(self):
        self._loop = GLib.MainLoop()
        self._started.set()
        self._loop.run()

    def idle(self, fn, *args):
        if self._loop:
            GLib.idle_add(fn, *args)


class WebRtcPeer:
    def __init__(self, audio_src=None, audio_sink=None, name="peer", runner=None,
                 auto_play=True):
        self.name = name
        self.audio_src = audio_src    # None -> autoaudiosrc, или "audiotestsrc"
        self.audio_sink = audio_sink  # None -> autoaudiosink, или "fakesink"
        self.auto_play = auto_play    # False -> ждёт офер, потом играет
        self.runner = runner or GlibRunner.get()
        self.on_offer_ready = None       # (sdp_string)
        self.on_answer_ready = None      # (sdp_string)
        self.on_ice_candidate = None     # (candidate_string)
        self.on_connection_state = None  # (state_string)
        self.on_incoming_stream = None   # (element_name)
        self.on_build_error = None       # (error_string) — пайплайн не собрался
        self.on_gst_error = None         # (error_string) — ошибка из bus
        self.pipeline = None
        self.webrtc = None
        self._built = threading.Event()
        self._held = []  # удерживаем promise-ответы живыми (use-after-free защита)
        self.ice_state = "new"
        self._seen_ice = set()
        self._negotiating = False
        self._remote_desc_set = False
        self._remote_type = None
        self._renegotiate_pending = False
        self._pending_ice = []
        self._build_error = None
        self._pending_remote = None

    # ---------------- запуск ----------------

    def start(self):
        self.runner.idle(self._build_pipeline)
        return self

    # ---------------- сборка пайплайна (GLib-поток) ----------------

    def _build_pipeline(self):
        if self.webrtc is not None:
            self._built.set()
            return
        try:
            if getattr(sys, "frozen", False) and sys.platform == "win32":
                if not ensure_bundled_plugins():
                    raise RuntimeError(
                        "бандл-плагины не импортированы: нет элемента webrtcbin")
            self._do_build_pipeline()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._build_error = f"{e!r}"
            print(f"[{self.name}] СБОЙ СБОРКИ: {e!r}", flush=True)
            if self.on_build_error:
                try:
                    self.on_build_error(f"{e!r}")
                except Exception:
                    pass
        finally:
            self._built.set()

    def _register_diag(self):
        """Собрать отчёт о состоянии GStreamer-регистра для диагностики Windows."""
        try:
            from gi.repository import Gst as _Gst
            reg = _Gst.Registry.get()
            feats = reg.get_feature_list(_Gst.ElementFactory)
            names = sorted(f.name for f in feats)
            cnt = len(names)
            webrtc_ok = "webrtcbin" in names
        except Exception as e:
            return f"registry-error={e!r}"
        env = {k: os.environ.get(k) for k in
               ("GST_PLUGIN_PATH", "GST_PLUGIN_SCANNER", "GI_TYPELIB_PATH",
                "PYGI_DLL_DIRS", "GST_REGISTRY_1_0")}
        return (f"Gst={_Gst.version_string()} factories={cnt} webrtcbin={webrtc_ok} "
                f"env={ {k: v for k, v in env.items() if v is not None} }")

    def _do_build_pipeline(self):
        pipeline = Gst.Pipeline.new(f"{self.name}-pipeline")
        try:
            webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        except Exception as e:
            raise RuntimeError(f"no such element 'webrtcbin' ({e!r}). "
                               f"{self._register_diag()}")
        if webrtc is None:
            raise RuntimeError("no such element 'webrtcbin' "
                               "(плагин GStreamer не загрузился). "
                               f"{self._register_diag()}")
        webrtc.set_property("name", self.name)
        webrtc.set_property("bundle-policy", 2)  # max-bundle
        # STUN: иначе из-за NAT только host/mDNS-кандидаты, между машинами не связаться
        for stun in ("stun://stun.l.google.com:19302",
                     "stun.l.google.com:19302"):
            try:
                webrtc.set_property("stun-server", stun)
                print(f"[{self.name}] STUN: {stun}", flush=True)
                break
            except Exception:
                continue
        pipeline.add(webrtc)

        src = Gst.ElementFactory.make(self.audio_src or "autoaudiosrc", "src")
        conv = Gst.ElementFactory.make("audioconvert", "aconv")
        res = Gst.ElementFactory.make("audioresample", "ares")
        enc = Gst.ElementFactory.make("opusenc", "enc")
        pay = Gst.ElementFactory.make("rtpopuspay", "pay")

        dec = Gst.ElementFactory.make("rtpopusdepay", "depay")
        opus = Gst.ElementFactory.make("opusdec", "dec")
        sink = Gst.ElementFactory.make(self.audio_sink or "autoaudiosink", "asink")

        # Подавление эха: webrtcdsp (AEC/NS/AGC) в тракте записи, а webrtcechoprobe
        # в тракте воспроизведения подаёт в него референс — то, что реально
        # звучит из динамиков. Без референса AEC не работает.
        dsp = Gst.ElementFactory.make("webrtcdsp", "dsp")
        probe = Gst.ElementFactory.make("webrtcechoprobe", "echoprobe")
        echo_ok = dsp is not None and probe is not None
        if echo_ok:
            caps_aec = Gst.Caps.from_string(
                "audio/x-raw,format=(string)S16LE,layout=(string)interleaved,"
                "rate=(int)48000,channels=(int)1")
            cap_dsp = Gst.ElementFactory.make("capsfilter", "aec_caps")
            cap_dsp.set_property("caps", caps_aec)
            conv2 = Gst.ElementFactory.make("audioconvert", "aconv_dsp")
            cap_ref = Gst.ElementFactory.make("capsfilter", "ref_caps")
            cap_ref.set_property("caps", caps_aec)
            conv3 = Gst.ElementFactory.make("audioconvert", "aconv_ref")
            res2 = Gst.ElementFactory.make("audioresample", "ares_ref")

        for e in (src, conv, res, enc, pay):
            pipeline.add(e)
        if echo_ok:
            for e in (cap_dsp, dsp, conv2):
                pipeline.add(e)
        src.link(conv)
        conv.link(res)
        if echo_ok:
            res.link(cap_dsp)
            cap_dsp.link(dsp)
            dsp.link(conv2)
            conv2.link(enc)
            dsp.set_property("probe", "echoprobe")
        else:
            res.link(enc)
        enc.link(pay)

        for e in (dec, opus, sink):
            pipeline.add(e)
        if echo_ok:
            for e in (conv3, res2, cap_ref, probe):
                pipeline.add(e)
            dec.link(opus)
            opus.link(conv3)
            conv3.link(res2)
            res2.link(cap_ref)
            cap_ref.link(probe)
            probe.link(sink)
        else:
            dec.link(opus)
            opus.link(sink)

        def on_pad_added(el, pad):
            caps = pad.get_current_caps()
            name = caps.to_string().split(",")[0] if caps else "?"
            if name.startswith("application/x-rtp"):
                print(f"[{self.name}] новый RTP-пад: {caps.to_string()[:100]}", flush=True)
                dec.sync_state_with_parent()
                pad.link(dec.get_static_pad("sink"))
                if self.on_incoming_stream:
                    self.on_incoming_stream(name)

        webrtc.connect("pad-added", on_pad_added)
        webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        webrtc.connect("notify::ice-connection-state", self._on_conn_state)

        self.pipeline = pipeline
        self.webrtc = webrtc
        self._pay = pay
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        sink_pad = webrtc.request_pad_simple("sink_%u")
        pay.get_static_pad("src").link(sink_pad)
        if self.auto_play:
            pipeline.set_state(Gst.State.PLAYING)
            print(f"[{self.name}] pipeline playing, payloader linked", flush=True)
        else:
            pipeline.set_state(Gst.State.READY)
            print(f"[{self.name}] pipeline ready (ждём офер)", flush=True)
        self._built.set()

    # ---------------- сигналы webrtcbin ----------------

    def _on_bus_message(self, bus, msg):
        if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
            err, dbg = msg.parse_error() if msg.type == Gst.MessageType.ERROR else msg.parse_warning()
            text = f"{err.message}"
            print(f"[{self.name}] GStreamer {msg.type}: {text}", flush=True)
            if dbg:
                print(f"  debug: {dbg[:300]}", flush=True)
            if self.on_gst_error and msg.type == Gst.MessageType.ERROR:
                try:
                    self.on_gst_error(text)
                except Exception:
                    pass
        return True

    def begin_negotiation(self):
        self.runner.idle(self._begin_when_ready)

    def _begin_when_ready(self, retries=0):
        if self.webrtc is not None:
            self._on_negotiation_needed(self.webrtc)
            return
        if self._build_error:
            print(f"[{self.name}] begin_negotiation: пайплайн не собран "
                  f"({self._build_error})", flush=True)
            return
        if retries and retries % 50 == 0:
            print(f"[{self.name}] ждём сборки пайплайна "
                  f"({retries * 100 / 1000:.1f}s)...", flush=True)
        GLib.timeout_add(100, lambda: self._begin_when_ready(retries + 1))

    def play(self):
        self.runner.idle(self._do_play)

    def _do_play(self):
        if self.pipeline and self.pipeline.get_state(0).state != Gst.State.PLAYING:
            self.pipeline.set_state(Gst.State.PLAYING)
            print(f"[{self.name}] pipeline playing", flush=True)

    def _on_conn_state(self, element, pspec):
        state = element.get_property("ice-connection-state")
        nick = GstWebRTC.WebRTCICEConnectionState(state).value_nick
        self.ice_state = nick
        print(f"[{self.name}] ice-connection-state: {nick}", flush=True)
        if self.on_connection_state:
            self.on_connection_state(nick)

    def _on_ice_candidate(self, webrtc, mlineindex, candidate):
        print(f"[{self.name}] local ICE: {candidate[:60]}", flush=True)
        if self.on_ice_candidate:
            self.on_ice_candidate(candidate)

    def _on_negotiation_needed(self, webrtc):
        if self._negotiating:
            return
        if self._remote_desc_set and not self._renegotiate_pending:
            return
        self._negotiating = True
        self._wait_caps_then_negotiate()

    def _pay_caps_ok(self):
        caps = self._pay.get_static_pad("src").get_current_caps()
        return caps is not None and caps.get_size() > 0

    def _wait_caps_then_negotiate(self, attempts=50):
        if self._pay_caps_ok():
            print(f"[{self.name}] caps готовы", flush=True)
            self._negotiating = False
            self.runner.idle(self._negotiate_now)
            return
        if attempts > 0:
            GLib.timeout_add(200, lambda: self._wait_caps_then_negotiate(attempts - 1))
            return
        print(f"[{self.name}] caps не появились — оффер без m-line", flush=True)
        self._negotiating = False
        self.runner.idle(self._negotiate_now)

    def _negotiate_now(self):
        if self._negotiating:
            self._renegotiate_pending = True
            print(f"[{self.name}] negotiate занят — отложено", flush=True)
            return
        self._negotiating = True
        promise = Gst.Promise.new_with_change_func(self._create_offer_cb, None, None)
        self.webrtc.emit("create-offer", None, promise)

    def _create_offer_cb(self, promise, user_data=None, *extra):
        reply = promise.get_reply()
        if reply is None:
            print(f"[{self.name}] create-offer: пустой reply — офер не создан",
                  flush=True)
            self._negotiating = False
            return
        self._held.append(reply)
        desc = reply.get_value("offer")
        if desc is None:
            print(f"[{self.name}] create-offer: нет значения offer в reply",
                  flush=True)
            self._negotiating = False
            return
        self._held.append(desc)
        promise2 = Gst.Promise.new_with_change_func(self._offer_set_cb, desc, None)
        self.webrtc.emit("set-local-description", desc, promise2)

    def _offer_set_cb(self, promise, desc, *extra):
        self._offer_set(desc)

    def _offer_set(self, desc):
        sdp = desc.sdp.as_text()
        if self.on_offer_ready:
            self.on_offer_ready(sdp)

    # ---------------- установка удалённого описания ----------------

    def set_remote_offer(self, sdp_text):
        self.runner.idle(self._set_remote_when_ready, sdp_text,
                         GstWebRTC.WebRTCSDPType.OFFER)

    def set_remote_answer(self, sdp_text):
        self.runner.idle(self._set_remote_when_ready, sdp_text,
                         GstWebRTC.WebRTCSDPType.ANSWER)

    def _set_remote_when_ready(self, sdp_text, sdp_type, retries=0):
        if self.webrtc is None and not self._build_error:
            self._pending_remote = (sdp_text, sdp_type)
            if retries and retries % 50 == 0:
                print(f"[{self.name}] ждём сборки пайплайна "
                      f"({retries * 100 / 1000:.1f}s)...", flush=True)
            GLib.timeout_add(100, lambda: self._set_remote_when_ready(
                *self._pending_remote, retries + 1))
            return
        self._pending_remote = None
        if self.webrtc is None:
            print(f"[{self.name}] set_remote: пайплайн не собран "
                  f"({self._build_error}) — офер отброшен", flush=True)
            return
        self._do_set_remote(sdp_text, sdp_type)

    def _do_set_remote(self, sdp_text, sdp_type):
        try:
            res, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
            desc = GstWebRTC.WebRTCSessionDescription.new(sdp_type, sdp)
            self._held.append(desc)
            self._remote_type = sdp_type
            kind = "OFFER" if sdp_type == GstWebRTC.WebRTCSDPType.OFFER else "ANSWER"
            print(f"[{self.name}] установлен удалённый {kind}: "
                  f"{len(sdp_text)} b", flush=True)
            promise = Gst.Promise.new_with_change_func(self._remote_set_cb, None, None)
            self.webrtc.emit("set-remote-description", desc, promise)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[{self.name}] СБОЙ set_remote: {e!r}", flush=True)

    def _remote_set_cb(self, promise, user_data=None, *extra):
        try:
            reply = promise.get_reply()
            if reply is not None:
                self._held.append(reply)
            self._remote_desc_set = True
            self._do_play()
            for cand in self._pending_ice:
                self._do_add_ice(cand)
            self._pending_ice.clear()
            if self._remote_type == GstWebRTC.WebRTCSDPType.OFFER:
                self._negotiating = False
                promise2 = Gst.Promise.new_with_change_func(self._create_answer_cb, None, None)
                self.webrtc.emit("create-answer", None, promise2)
            else:
                self._negotiating = False
                if self._renegotiate_pending:
                    self._renegotiate_pending = False
                    print(f"[{self.name}] повторная ренегоциация", flush=True)
                    self._wait_caps_then_negotiate()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[{self.name}] СБОЙ remote_set_cb: {e!r}", flush=True)

    def _create_answer_cb(self, promise, user_data=None, *extra):
        reply = promise.get_reply()
        if reply is None:
            print(f"[{self.name}] create-answer: пустой reply — "
                  f"answer не создан", flush=True)
            return
        self._held.append(reply)
        desc = reply.get_value("answer")
        if desc is None:
            print(f"[{self.name}] create-answer: нет значения answer в reply",
                  flush=True)
            return
        self._held.append(desc)
        promise2 = Gst.Promise.new_with_change_func(self._answer_set_cb, desc, None)
        self.webrtc.emit("set-local-description", desc, promise2)

    def _answer_set_cb(self, promise, desc, *extra):
        self._answer_set(desc)

    def _answer_set(self, desc):
        sdp = desc.sdp.as_text()
        if self.on_answer_ready:
            self.on_answer_ready(sdp)

    # ---------------- ICE ----------------

    def add_remote_ice(self, candidate):
        self.runner.idle(self._queue_or_add_ice, candidate)

    def _queue_or_add_ice(self, candidate):
        if candidate in self._seen_ice:
            return
        self._seen_ice.add(candidate)
        if self._remote_desc_set:
            self._do_add_ice(candidate)
        else:
            self._pending_ice.append(candidate)

    def _do_add_ice(self, candidate):
        if not candidate or not candidate.startswith("candidate:"):
            return
        self.webrtc.emit("add-ice-candidate", 0, candidate)

    # ---------------- завершение ----------------

    def set_muted(self, muted):
        self._muted = muted
        self.runner.idle(self._do_mute)

    def _do_mute(self):
        src = self.pipeline.get_by_name("src") if self.pipeline else None
        if not src:
            return
        if self._muted:
            src.set_state(Gst.State.NULL)
            print(f"[{self.name}] микрофон выключен", flush=True)
        else:
            src.set_state(Gst.State.PLAYING)
            print(f"[{self.name}] микрофон включён", flush=True)

    def close(self):
        self.runner.idle(self._do_close)

    def _do_close(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.webrtc = None
