# PyInstaller runtime hook: set PYGI_DLL_DIRS for gstreamer-meta on Windows.
# Without this, gi.require_version('Gst', '1.0') fails with
# "Could not deduce DLL directories, please set PYGI_DLL_DIRS".
import os, sys

if sys.platform == "win32" and getattr(sys, "frozen", False):
    base = os.path.dirname(sys.executable)
    dirs = [base]
    for name in os.listdir(base):
        if name.startswith("_gstreamer") or name.startswith("gi"):
            p = os.path.join(base, name)
            if os.path.isdir(p):
                dirs.append(p)
    os.environ["PYGI_DLL_DIRS"] = os.pathsep.join(dirs)
