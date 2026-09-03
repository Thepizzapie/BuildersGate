# PyInstaller spec for the Windows desktop build.
#   pyinstaller packaging/bgate.spec --noconfirm
#
# Two things about this app decide the whole layout of the bundle:
#
#  1. Data trees are resolved RELATIVE TO THE SOURCE TREE, not via
#     importlib.resources:
#         bgate_ui/app.py      Path(__file__).with_name("static")
#         bgate_core/store/scaffold  Path(__file__).resolve().parents[2] / "templates"
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
# The importable packages live under src/; the bundle keeps them at its own
# root, which is what the module paths inside them already assume.
SRC = ROOT / "src"

# ── data ────────────────────────────────────────────────────────────────────
# (source, destination-inside-bundle)
datas = [
    (str(SRC / "bgate_ui" / "static"), "bgate_ui/static"),
    (str(SRC / "bgate_site" / "theme"), "bgate_site/theme"),
    # Sibling of bgate_core, because scaffold.py walks up two parents to find it.
    (str(SRC / "templates"), "templates"),
    # Also embedded as resource id 1 by EXE(icon=...) below. This copy is the
    # fallback bgate_ui/window/webview2.py reads when the resource lookup fails.
    (str(ROOT / "packaging" / "icon.ico"), "."),
]
# THE FLOOR'S ART AND AMBIENCE, WHICH ARE NO LONGER PART OF static/. They moved
# into their own distribution (`builders-gate-floor-assets`, the `floor` extra),
# so the bundle has to carry the PACKAGE rather than a subdirectory of the
# dashboard's static tree — the installer's floor component points here, and
# ISCC fails the build outright if this payload is missing, which is how the
# move was caught. Shipped with its `__init__.py` so `_internal` being on
# sys.path makes `import builders_gate_floor_assets` resolve exactly as a pip
# install does: app.py needs no frozen-app branch.
_FLOOR_PKG = ROOT / "packaging" / "floor-assets" / "builders_gate_floor_assets"
if _FLOOR_PKG.is_dir():
    datas += [
        (str(_FLOOR_PKG / "__init__.py"), "builders_gate_floor_assets"),
        (str(_FLOOR_PKG / "img"), "builders_gate_floor_assets/img"),
        (str(_FLOOR_PKG / "audio"), "builders_gate_floor_assets/audio"),
    ]
# THE ADAPTERS THAT ARE READ AS SOURCE, NOT IMPORTED AS MODULES — and this is
# the same disease as the two exclusions documented further down, one level
# over. `collect_submodules` makes a module IMPORTABLE; it compiles it into the
# archive and puts no .py on disk. These four are never imported for their
# behaviour: three are handed to another interpreter by PATH (Blender's
# `--python`, a whisper subprocess) and the fourth has its text spliced into a
# generated script. `Path(__file__).with_name(...)` has to find a real file.
#
# Verified against a shipped bundle before this list existed: dist/BuildersGate/
# _internal contained no bgate_adapters directory at all, so RUNNER resolved to
# a path that did not exist and EVERY Blender-backed feature in the packaged
# app — modelling, rigging, sprite baking — plus whisper transcription failed
# at run time. Nothing caught it because nothing ran the frozen binary.
#
# tests/packaging/test_packaging.py::test_every_adapter_read_from_disk_is_shipped scans
# for the `with_name` calls and fails if one is added without landing here.
datas += [
    (str(SRC / "bgate_adapters" / name), "bgate_adapters")
    for name in ("_blender_runner.py", "_blender_sprites.py",
                 "_whisper_runner.py", "bodymeasure.py")
]
# Pillow ships binary plugins it loads dynamically.
datas += collect_data_files("PIL", include_py_files=False)

# WebView2Loader.dll — the native window loads this directly (see
# bgate_ui/window/webview2.py). pywebview vendors it under
# webview/lib/runtimes/<arch>/native/, and collect_data_files keeps that layout
# so the same relative lookup works frozen. Without it the app silently loses
# its window and falls back to the browser.
datas += collect_data_files("webview", includes=["lib/runtimes/**/*.dll"])

# faster-whisper's VAD MODEL, which is a DATA file inside the package and so is
# invisible to the import graph PyInstaller builds.
#
# WHAT ITS ABSENCE LOOKS LIKE, because it does not look like a packaging bug.
# Recording works, the audio lands, and processing then dies with
# `NoSuchFile: faster_whisper/assets/silero_vad_v6.onnx`. The session is banked
# `failed` with no transcript, so the review screen has nothing to show and the
# playtest looks like it broke. Measured on a shipped install: three sessions
# (21, 27, 28) lost this way before anyone traced the message, and the app's own
# director agent was the one that finally read it out.
#
# include_py_files=False on purpose: assets/__init__.py comes in through the
# import graph already, and the only thing missing is the .onnx beside it.
datas += collect_data_files("faster_whisper", include_py_files=False)

# ── hidden imports ──────────────────────────────────────────────────────────
hiddenimports = [
    # faster-whisper + CTranslate2 reach their backends by name at run time.
    "faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
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
# NOTHING IS FILTERED OUT OF bgate_adapters ANY MORE, and the two exclusions
# that used to be here are the reason two features were dead in every release.
#
# `_whisper_runner` was cut because collect_submodules IMPORTS each module to
# walk it, and importing it pulls faster_whisper — said to drag in torch,
# onnxruntime and the CUDA runtime for a 415 MB binary. Measured: faster-whisper
# runs on CTranslate2 and pulls NO torch. The real cost is ~121 MB, and cutting
# it meant the packaged app could never transcribe anything.
#
# `recorder` was cut, and an earlier version of this file filtered it on
# the reasoning that it "belongs to the record extra and is spawned out of
# process". Both halves were wrong: bgate_core.qa.playtest imports it IN PROCESS
# for every preflight, and it is how playtest audio is actually captured. Cutting
# it took numpy with it and left the Playtests screen permanently unable to
# record. See VENV_INSTALL in build_exe.py.
_ADAPTER_SKIP: tuple[str, ...] = ()

for pkg in ("bgate_ui", "bgate_core", "bgate_cli", "bgate_site"):
    hiddenimports += collect_submodules(pkg)
hiddenimports += [
    m for m in collect_submodules("bgate_adapters")
    if not any(s in m for s in _ADAPTER_SKIP)
]

a = Analysis(
    [str(SPECDIR / "launcher.py")],
    pathex=[str(SRC)],
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
    #
    # THE SECOND GROUP IS THERE FOR A DIFFERENT REASON, and it is the one worth
    # understanding: none of it is a dependency of this project. It was reaching
    # the bundle because PyInstaller analyses whatever is installed in the
    # interpreter that runs it, so a developer machine with unrelated SDKs on it
    # shipped them. Measured on one: 59 MB zipped locally against 37 MB from
    # CI's clean venv, same commit. build_exe.py now builds in an isolated venv
    # so this cannot happen by default; these stay as a backstop for anyone who
    # passes --no-isolate, because a silently fatter release is not a failure
    # anybody notices.
    excludes=[
        # torch IS still excluded and nothing needs it: faster-whisper runs on
        # CTranslate2, not PyTorch. The comment this block used to carry said
        # torch + onnxruntime + CUDA "come in through faster_whisper" and
        # produced a 415 MB exe — measured today, installing faster-whisper
        # pulls NO torch at all. That wrong belief is the only reason
        # speech-to-text was cut from the download.
        "torch", "torchaudio", "torchvision", "transformers", "pygame",
        "tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook",
        "pytest", "setuptools._distutils",

        # NEITHER numpy NOR sounddevice IS EXCLUDED, and both used to be.
        # bgate_adapters/recorder.py captures playtest mic audio through
        # sounddevice and measures it with numpy, so excluding them did not trim
        # an optional extra — it made recording impossible in every packaged
        # build, on the app's own Playtests screen. ~27 MB, mostly numpy's
        # OpenBLAS, and the feature does not exist without it.
        # uvicorn's --reload supervisor. A frozen app never reloads.
        "watchfiles",
        # Pillow's AVIF codec: a 7.5 MB binary for a format nothing here reads
        # or writes. Every other Pillow plugin is left alone.
        "PIL.AvifImagePlugin",
        # Cloud SDKs that are nobody's dependency here. azure.core drags in the
        # whole opentelemetry exporter stack, which drags in grpc and protobuf:
        # 20 MB for tracing this app does not emit.
        "azure", "opentelemetry", "grpc", "google.protobuf", "google.auth",

        # `cryptography` reaches the bundle through exactly one caller:
        # pywebview's __generate_ssl_cert(), which builds a self-signed
        # certificate for its own bottle server when a window is created with
        # ssl=True. bgate_ui.window.desktop never passes ssl — the window points at
        # loopback uvicorn — and pywebview's import is inside that function and
        # already wrapped in try/ImportError with a message telling you to
        # install pywebview[ssl]. So the only way to reach it is to add an
        # argument this app does not use, and the failure would be that
        # message rather than a crash. 9.5 MB of Rust bindings.
        #
        # THIS DOES NOT REMOVE TLS. Python's own _ssl/_hashlib and the OpenSSL
        # DLLs beside them are untouched, which is what httpx and openai
        # actually use to reach the network.
        "cryptography",
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
