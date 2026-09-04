# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Dial Forward (Windows).

Produces a --onedir bundle 'DialForward' with two frozen executables so that
nothing depends on a system Python:
  - DialForward.exe  : the GUI app (launcher behavior folded in; it spawns
                       Relay.exe below)
  - Relay.exe        : the Telegram relay, launched by the app as a child.
Both import the bundled GStreamer/PyGObject wheels (gstreamer-meta) which the
Windows build environment installs via pip before running PyInstaller.
"""
from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

# --- GStreamer / PyGObject wheel data -------------------------------
# gstreamer_plugins holds the actual plugin .dlls; gstreamer_libs and
# gstreamer_python hold the core libs + gi bindings + typelibs.
gst_pkgs = ("gstreamer_plugins", "gstreamer_libs", "gstreamer_python",
            "gstreamer_ext_runtime")
gst_datas = []
for _p in gst_pkgs:
    gst_datas += collect_data_files(_p, include_py_files=False)
gst_bins = []
for _p in gst_pkgs:
    gst_bins += collect_dynamic_libs(_p)

# --- Strip unnecessary heavy GStreamer plugins / codecs -------------
# We only need the WebRTC call path (webrtc, sctp, dtls, srtp, nice,
# rtpmanager, sdp, opus, vpx). Everything below is not used by a call
# and just bloats the bundle, most with dedicated heavy codecs.
_unwanted = {
    # encoders / codecs NOT used by a WebRTC call (vpx/vp8/vp9 kept on purpose)
    "svtav1enc", "rav1e", "x264", "x265", "nvcodec", "vaapi",
    "avcodec", "avformat", "avutil", "swscale", "swresample", "dav1d",
    "openh264", "aom", "avif", "turbojpeg", "openh26", "nvjpeg",
    # media playback / documents not needed for a call
    "rsvg", "pango", "harfbuzz", "soup",
    # cloud / network services we don't use
    "aws", "reqwest", "quinn", "speechmatics", "elevenlabs", "demucs",
    "whisper", "transcriber", "pmt", "libsrt",
    # CD/DVD burning
    "burn",
    # display-only / game/desktop capture
    "d3dshader", "d3d12", "gamecapture",
    # SRT streaming & IceCast
    "rsrtsp", "icecast",
    # subtitle/closed-caption
    "closedcaption",
    "d3d11",
}
def _keep(tup):
    name = (tup[0] if isinstance(tup, tuple) else tup).lower()
    base = name.split("\\")[-1].split("/")[-1]
    return not (any(_u in base for _u in _unwanted))
gst_bins = [b for b in gst_bins if _keep(b[0])]
gst_datas = [d for d in gst_datas if _keep(d[0])]

# --- shared data for both executables ------------------------------
shared_datas = gst_datas + [
    ("../VERSION", "."),
    ("../icons/dial_forward.png", "icons"),
]
shared_bins = gst_bins

# --- app module roots (client/ modules imported by app.py) ---------
app_hidden = ["call", "protocol", "relay_client", "webrtc"]
# gi / gstreamer python submodules
gst_hidden = []
for _p in ("gstreamer_python", "gstreamer_libs", "gstreamer_plugins"):
    gst_hidden += collect_submodules(_p)

# ===================================================================
# GUI executable — entry: app.py (owns relay spawning + single instance)
# ===================================================================
a_gui = Analysis(
    ["../client/app.py"],
    pathex=["../client", "../relay"],
    binaries=shared_bins,
    datas=shared_datas,
    hiddenimports=app_hidden + gst_hidden + [
        "telethon", "websockets", "qrcode", "pystray", "PIL",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hook_gi_dlls.py"],
    excludes=[],
    noarchive=False,
)
pyz_gui = PYZ(a_gui.pure)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="DialForward",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="../icons/dial_forward.ico",
)

# ===================================================================
# Relay executable — entry: relay/relay.py
# ===================================================================
a_relay = Analysis(
    ["../relay/relay.py"],
    pathex=["../relay"],
    binaries=shared_bins,
    datas=shared_datas,
    hiddenimports=gst_hidden + ["telethon", "websockets"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hook_gi_dlls.py"],
    excludes=[],
    noarchive=False,
)
pyz_relay = PYZ(a_relay.pure)

exe_relay = EXE(
    pyz_relay,
    a_relay.scripts,
    [],
    exclude_binaries=True,
    name="Relay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon="../icons/dial_forward.ico",
)

# ===================================================================
# COLLECT both executables into one folder with all shared binaries/data.
# (PyInstaller merges multiple EXE + COLLECT in one spec into a single dir.)
# ===================================================================
coll = COLLECT(
    exe_gui, exe_relay,
    [b for b in (a_gui.binaries + a_relay.binaries) if _keep(b[0])],
    [d for d in (a_gui.datas + a_relay.datas) if _keep(d[0])],
    strip=False, upx=True, name="DialForward",
)
