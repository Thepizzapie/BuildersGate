# PyInstaller spec for the Windows desktop build.
#   pyinstaller packaging/bgate.spec --noconfirm
#
# Two things about this app decide the whole layout of the bundle:
#
#  1. Data trees are resolved RELATIVE TO THE SOURCE TREE, not via
#     importlib.resources:
#         bgate_ui/app.py      Path(__file__).with_name("static")
#         bgate_core/scaffold  Path(__file__).resolve().parent.parent / "templates"
#     PyInstaller unpacks modules under sys._MEIPASS keeping package paths, so
#     `templates/` has to land at the BUNDLE ROOT (sibling of bgate_core) and
#     `static/` inside bgate_ui/. Get this wrong and the dashboard 404s every
#     asset while still starting up perfectly happily.
#
#  2. uvicorn and pywebview both import their real implementations by NAME at
#     runtime. Nothing references them statically, so PyInstaller's analysis
#     cannot see them and the frozen app dies on first request with an
#     inscrutable "unknown loop setup". They are listed by hand below.

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

SPECDIR = Path(SPECPATH).resolve()
ROOT = SPECDIR.parent

# ── data ────────────────────────────────────────────────────────────────────
# (source, destination-inside-bundle)
datas = [
    (str(ROOT / "bgate_ui" / "static"), "bgate_ui/static"),
    (str(ROOT / "bgate_site" / "theme"), "bgate_site/theme"),
    # Sibling of bgate_core, because scaffold.py walks up two parents to find it.
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "bgate_engine"), "bgate_engine"),
]
# Pillow ships binary plugins it loads dynamically.
datas += collect_data_files("PIL", include_py_files=False)

# WebView2Loader.dll — the native window loads this directly (see
# bgate_ui/webview2.py). pywebview vendors it under
# webview/lib/runtimes/<arch>/native/, and collect_data_files keeps that layout
# so the same relative lookup works frozen. Without it the app silently loses
# its window and falls back to the browser.
datas += collect_data_files("webview", includes=["lib/runtimes/**/*.dll"])

# ── hidden imports ──────────────────────────────────────────────────────────
hiddenimports = [
    # uvicorn picks these at runtime from a string in its config.
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    # WebView2 backend on Windows; chosen by platform detection, not by import.
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader", "pythonnet",
    # Pillow codecs.
    "PIL._tkinter_finder",
    "encodings.idna",
]
# The app's own packages: routes/ and adapters are reached through the router
# registry rather than by direct import from the entry point.
#
# bgate_adapters is filtered, not collected whole. collect_submodules IMPORTS
# each module to walk it, and bgate_adapters/_whisper_runner.py imports
# faster_whisper — whose own docstring says "Never import faster_whisper into
# the server process". Doing it at analysis time dragged in torch, onnxruntime
# and the CUDA runtime and produced a 415 MB binary. That runner is spawned as
# a SUBPROCESS and speech-to-text is an optional extra, so it does not belong
# in the desktop bundle at all.
for pkg in ("bgate_ui", "bgate_core", "bgate_cli", "bgate_site"):
    hiddenimports += collect_submodules(pkg)
hiddenimports += [
    m for m in collect_submodules("bgate_adapters")
    if "whisper" not in m and "transcribe" not in m
]

a = Analysis(
    [str(SPECDIR / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing the desktop app does at runtime needs any of these, and each one
    # costs tens to hundreds of MB. torch + onnxruntime + the CUDA runtime come
    # in through faster_whisper (optional STT, which runs out-of-process and
    # needs a model download anyway); pygame arrives via a transitive audio
    # dependency. Leaving them in produced a 415 MB exe.
    excludes=[
        "torch", "torchaudio", "torchvision", "onnxruntime", "faster_whisper",
        "ctranslate2", "transformers", "pygame", "sounddevice", "av",
        "tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook",
        "pytest", "numpy.testing", "setuptools._distutils",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ── onedir, deliberately, not onefile ───────────────────────────────────────
# The first release shipped --onefile and Windows Defender quarantined it as
# Trojan:Win32/Sabsik.TE.A!ml. The !ml suffix is a machine-learning verdict
# rather than a signature hit, and a onefile build presents every trait those
# models weight: a self-extracting stub that unpacks a compressed archive into
# %TEMP% and executes code out of it, ~46 MB of high-entropy zlib that reads as
# a packed payload, and no Authenticode signature to weigh against any of it.
# That is behaviourally indistinguishable from a dropper.
#
# onedir has no stub and no runtime unpack — the interpreter, the DLLs and the
# data sit on disk as ordinary files — which removes the biggest trigger. It
# also starts faster, because nothing is extracted on every launch.
#
# This does NOT make the binary trusted. The real fix is code signing; until
# there is a certificate, expect SmartScreen's "unrecognised app" prompt on
# first run. Ship the folder zipped, and publish the SHA256.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # binaries+datas go in the COLLECT below
    name="BuildersGate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX would compress the binaries back into high-entropy blobs, which is
    # the thing being avoided here — and it mangles the WebView2 loader.
    upx=False,
    # No console window on a double-click. `BuildersGate.exe serve` still works
    # from a terminal, it just cannot print back to one.
    console=False,
    icon=str(ROOT / "packaging" / "icon.ico")
        if (ROOT / "packaging" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BuildersGate",
)
