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

if sys.platform == "win32" and getattr(sys, "frozen", False):
    # --- GUI app без консоли: GStreamer-WARNING про сканер/плагины иначе не видны.
    # Дублируем stderr/stdout в лог под USERPROFILE, при старте обрезаем до куска.
    try:
        _log_dir = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        _logp = os.path.join(_log_dir, "dialforward.log")
        if os.path.exists(_logp) and os.path.getsize(_logp) > 2_000_000:
            try:
                os.replace(_logp, _logp + ".1")
            except OSError:
                pass
        _fd = os.open(_logp, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        os.dup2(_fd, 2)
        os.dup2(_fd, 1)
        os.close(_fd)
        sys.stdout = os.fdopen(1, "a", encoding="utf-8", errors="replace")
        sys.stderr = sys.stdout
        print("=== DialForward boot ===", flush=True)
    except OSError:
        pass

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

    # --- GStreamer plugin dirs (mirror of gstreamer_libs.environment) ---
    # ВАЖНО: GST_PLUGIN_SCANNER сознательно НЕ ставим — внешний
    # gst-plugin-scanner в frozen Windows уходит в утиль (не находит свои DLL),
    # его запуск вешает сканирование. Плагины грузим напрямую в процесс через
    # Gst.Plugin.load_file() (в webrtc.setup_plugin_import) с прогрессом на сплеше.
    plugin_dirs = [p for p in (
        _under("gstreamer_libs", "lib", "gstreamer-1.0"),
        _under("gstreamer_plugins", "lib", "gstreamer-1.0"),
    ) if p]
    bin_dir = _under("gstreamer_libs", "bin")
    # Windows-загрузчик GStreamer инлочит плагины через LOAD_WITH_ALTERED_SEARCH_PATH,
    # где зависимости ищутся ТОЛЬКО по PATH (os.add_dll_directory не помогает).
    # Валим в PATH bin-каталоги ВСЕХ gstreamer-пакетов (как делает гstreamer_libs'
    # setup_python_environment): gstreamer_libs/bin, gstreamer_plugins_libs/bin
    # (там лежит nice-10.dll, без которого не грузится gstwebrtc.dll -> webrtcbin).
    path_add = []
    _seen_bin = set()
    for _b in bases:
        try:
            _tops = os.listdir(_b)
        except OSError:
            continue
        for _name in _tops:
            _bd = os.path.join(_b, _name, "bin")
            if not os.path.isdir(_bd) or _bd in _seen_bin:
                continue
            try:
                _has_dll = any(f.lower().endswith(".dll") for f in os.listdir(_bd))
            except OSError:
                _has_dll = False
            if _has_dll:
                _seen_bin.add(_bd)
                path_add.append(_bd)
    if bin_dir and bin_dir not in path_add:
        path_add.append(bin_dir)
    if path_add:
        os.environ["PATH"] = (os.pathsep.join(path_add) + os.pathsep +
                              os.environ.get("PATH", ""))
        print(f"[hook] PATH_ADD={os.pathsep.join(path_add)}", flush=True)
    if plugin_dirs:
        paths = os.pathsep.join(plugin_dirs)
        os.environ["GST_PLUGIN_PATH"] = paths
        os.environ["GST_PLUGIN_PATH_1_0"] = paths
        os.environ["GST_PLUGIN_SYSTEM_PATH"] = paths
        os.environ["GST_PLUGIN_SYSTEM_PATH_1_0"] = paths

    # --- typelibs (gstreamer_libs ships the Gst core ones; gstreamer_python the rest) ---
    typelib_dirs = [p for p in (
        _under("gstreamer_libs", "lib", "girepository-1.0"),
        _under("gstreamer_python", "Lib", "girepository-1.0"),
    ) if p]
    if typelib_dirs:
        os.environ["GI_TYPELIB_PATH"] = os.pathsep.join(typelib_dirs)

    # --- стабильный реестр: GStreamer сам сохраняет его при выходе, поэтому
    #     повторные запуски подхватывают плагины из кэша мгновенно (импорт —
    #     только при первом запуске). ---
    try:
        regdir = os.path.join(os.environ.get("LOCALAPPDATA")
                              or os.path.expanduser("~"), "DialForward")
        os.makedirs(regdir, exist_ok=True)
        reg = os.path.join(regdir, "gstreamer-registry.bin")
        os.environ["GST_REGISTRY_1_0"] = reg
        print(f"[hook] REGISTRY={reg} cached={os.path.exists(reg)}", flush=True)
    except OSError as e:
        print(f"[hook] registry path fail: {e!r}", flush=True)

    print(f"[hook] MEIPASS={getattr(sys, '_MEIPASS', '')}", flush=True)
    print(f"[hook] GST_PLUGIN_PATH={os.environ.get('GST_PLUGIN_PATH')}", flush=True)
    print(f"[hook] TYPELIB={os.environ.get('GI_TYPELIB_PATH')}", flush=True)
    for d in (os.environ.get('GST_PLUGIN_PATH') or '').split(os.pathsep):
        print(f"[hook] plugin_dir_exists {d} -> {os.path.isdir(d)}", flush=True)