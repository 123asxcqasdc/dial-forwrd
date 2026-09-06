# PyInstaller runtime hook: make GStreamer/PyGObject DLLs findable on Windows.
# gstreamer-meta wheels scatter DLLs across several dirs (top-level, and
# under gstreamer_*/gi). We add every dir that contains a .dll/.pyd to the
# Windows DLL search path (os.add_dll_directory) so _gi.pyd and its
# dependencies (libgobject, libglib, libgirepository, ...) load correctly,
# and set PYGI_DLL_DIRS for the gi/Gst typelib lookup.
#
# NOTE: in a normal venv these env vars (GST_PLUGIN_PATH, GST_PLUGIN_SCANNER,
# GI_TYPELIB_PATH, ...) are injected by gstreamer_libs' .pth file. PyInstaller
# frozen apps do NOT process .pth files, so we must replicate that setup here,
# pointing at the bundle-relative paths. Without it GStreamer finds no plugins
# (=> "no such element 'webrtcbin'") and gi can miss the typelibs.
import os
import sys
import tempfile

if sys.platform == "win32" and getattr(sys, "frozen", False):
    # PyInstaller 6.x (onedir) кладёт данные в <dist>/_internal, старые — рядом
    # с exe. Ищем по всем кандидатам.
    bases = [b for b in (getattr(sys, "_MEIPASS", None),
                         os.path.dirname(sys.executable)) if b]
    base = bases[0]
    dirs = list(bases)
    for b in bases:
        for root, _subs, files in os.walk(b):
            for f in files:
                if f.lower().endswith((".dll", ".pyd")):
                    if root not in dirs:
                        dirs.append(root)
                    break

    for d in dirs:
        try:
            os.add_dll_directory(d)
        except (OSError, ValueError):
            pass

    if dirs:
        os.environ["PYGI_DLL_DIRS"] = os.pathsep.join(dirs)

    def _under(*rel):
        for b in bases:
            p = os.path.join(b, *rel)
            if os.path.exists(p):
                return p
        return None

    # --- GStreamer plugin dirs + scanner (mirror of gstreamer_libs.environment) ---
    plugin_dirs = [p for p in (
        _under("gstreamer_libs", "lib", "gstreamer-1.0"),
        _under("gstreamer_plugins", "lib", "gstreamer-1.0"),
    ) if p]
    scanner = (_under("gstreamer_libs", "libexec", "gstreamer-1.0",
                      "gst-plugin-scanner.exe")
               or _under("gstreamer_libs", "bin", "gst-plugin-scanner.exe"))
    bin_dir = _under("gstreamer_libs", "bin")
    if bin_dir:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    if plugin_dirs:
        paths = os.pathsep.join(plugin_dirs)
        os.environ["GST_PLUGIN_PATH"] = paths
        os.environ["GST_PLUGIN_PATH_1_0"] = paths
        os.environ["GST_PLUGIN_SYSTEM_PATH"] = paths
        os.environ["GST_PLUGIN_SYSTEM_PATH_1_0"] = paths
    if scanner:
        os.environ["GST_PLUGIN_SCANNER"] = scanner
        os.environ["GST_PLUGIN_SCANNER_1_0"] = scanner

    # --- typelibs (gstreamer_libs ships the Gst core ones; gstreamer_python the rest) ---
    typelib_dirs = [p for p in (
        _under("gstreamer_libs", "lib", "girepository-1.0"),
        _under("gstreamer_python", "Lib", "girepository-1.0"),
    ) if p]
    if typelib_dirs:
        os.environ["GI_TYPELIB_PATH"] = os.pathsep.join(typelib_dirs)

    #    registry in the bundle is read-only; force a writable per-user cache
    try:
        reg = os.path.join(tempfile.gettempdir(),
                           f"dialforward-gst-registry-{os.getpid()}.bin")
        os.environ["GST_REGISTRY_1_0"] = reg
    except OSError:
        pass