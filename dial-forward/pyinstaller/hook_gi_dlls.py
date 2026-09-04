# PyInstaller runtime hook: make GStreamer/PyGObject DLLs findable on Windows.
# gstreamer-meta wheels scatter DLLs across several dirs (top-level, and
# under gstreamer_*/gi). We add every dir that contains a .dll/.pyd to the
# Windows DLL search path (os.add_dll_directory) so _gi.pyd and its
# dependencies (libgobject, libglib, libgirepository, ...) load correctly,
# and set PYGI_DLL_DIRS for the gi/Gst typelib lookup.
import os, sys

if sys.platform == "win32" and getattr(sys, "frozen", False):
    base = os.path.dirname(sys.executable)
    dirs = [base]
    for root, _subs, files in os.walk(base):
        for f in files:
            if f.lower().endswith((".dll", ".pyd")):
                # 'root' is under base; include it
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
