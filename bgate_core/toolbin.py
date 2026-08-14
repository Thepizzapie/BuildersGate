"""External tools the app can fetch for itself, into a directory it owns.

THE PROBLEM THIS EXISTS FOR is not packaging, it is the sentence a new user
reads. Builders Gate is a harness over tools it does not ship — Godot, ffmpeg,
Blender — and when one was absent the app printed an instruction:

    speech-to-text needs a Python with faster-whisper installed; the packaged
    app does not bundle one. Set BGATE_WHISPER_PYTHON to an interpreter that
    has it, or run Builders Gate from a source checkout.

Somebody who installed a .exe did so precisely to avoid all of that. Telling
them to get a source checkout tells them their install was the wrong choice,
and the feature might as well not exist.

NONE OF THOSE TOOLS ACTUALLY NEED INSTALLING. Every one is a portable binary in
a zip — no admin, no PATH entry, no registry, no package manager. Downloading
one and unzipping it into a folder we own is the entire operation. So the rule
this module exists to enforce is:

    if the app can NAME what is missing, the app can FETCH it.
    an instruction is only acceptable for something that is an ACCOUNT,
    not a file.

WHERE THEY GO, and why this is not new. bgate_core/ffmpegbin.py already resolves
``~/.bgate/bin/ffmpeg.exe`` ABOVE anything on PATH — written after a system
ffmpeg silently produced corrupt Theora, so that a known-good binary could be
kept somewhere PATH could not override. That directory is exactly the right
home for tools the app installs, and this module generalises the same
precedence to every tool rather than inventing a second location.

~/.bgate is not a repository and is not inside any game, so a tool installed
once is inherited by every project on the machine and leaks into none of them.

WHAT THIS DELIBERATELY DOES NOT DO
  · No speculative downloads. Nothing is fetched until a human presses a button
    for a feature they are trying to use. The installer stays small and a user
    who never opens the cutscene tools never downloads ffmpeg.
  · No system installs. Nothing here writes outside ~/.bgate, touches PATH, or
    asks for elevation. Uninstalling the app can delete one directory and be
    done.
  · No silent upgrades. A tool that is present is used; this never replaces a
    binary somebody chose.

A NOTE ON WHAT DOWNLOADING BINARIES LOOKS LIKE FROM OUTSIDE. An unsigned
application that fetches an executable and runs it is behaviourally similar to
a dropper, and this project has already had a build quarantined by Defender's
ML. The mitigations here are the ones that actually apply: every download is
user-initiated, every URL is the tool's official release host and is written
down in this file rather than computed, every archive is checked against a
recorded SHA-256 before anything is extracted, and everything lands in a
user-owned directory. That reduces the risk; code signing is what settles it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

#: Where installed tools live. Beside the global provider-key store, for the
#: same reason: a fact about the MACHINE, not about any one game.
def bin_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".bgate" / "bin"


@dataclass(frozen=True)
class Tool:
    """One fetchable tool.

    ``url`` and ``sha256`` are written down rather than discovered. A tool
    installer that resolves "the latest release" at run time installs a
    different thing on different days and cannot be checksummed at all — and
    this project has already been bitten by exactly that, when the ffmpeg a
    package manager happened to install encoded Theora that would not decode.
    Pinning is the point.

    ``members`` are the paths INSIDE the archive to keep. Everything else is
    discarded: the ffmpeg release is 80 MB of documentation, presets and
    libraries around three executables nothing here calls directly.
    """
    name: str
    summary: str                       # what the user loses without it
    url: str
    sha256: str
    size_mb: int                       # for the button, before they press it
    members: tuple[str, ...]           # archive paths -> flattened into bin/
    exes: tuple[str, ...]              # what resolve() looks for, in order


# ── the registry ────────────────────────────────────────────────────────────
# ffmpeg 7.1-essentials, from gyan.dev's own GitHub releases.
#
# THE VERSION IS NOT AN ACCIDENT AND MUST NOT BE BUMPED CASUALLY. See
# bgate_core/ffmpegbin.py: the 8.1.1-full build from the same packager has a
# libtheora that encodes without error and produces files the decoder cannot
# read — measured at 37 decode errors in a one-second probe against 0 for this
# build. "Newer" was the wrong instinct once already.
FFMPEG = Tool(
    name="ffmpeg",
    summary="video and audio export — cutscenes, playtest recording, the audio lab",
    url="https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip",
    # Recorded by scripts/pin_tool.py against the release above. GitHub release
    # assets are immutable, so a mismatch means a corrupt or intercepted
    # download, not a new upstream build.
    sha256="fa7d4d7e795db0e2503f49f105f46ed5852386f0cfdd819899be3b65ebde24fc",
    size_mb=88,
    members=("bin/ffmpeg.exe", "bin/ffprobe.exe"),
    exes=("ffmpeg.exe", "ffmpeg"),
)

TOOLS: dict[str, Tool] = {t.name: t for t in (FFMPEG,)}


# ── resolution ──────────────────────────────────────────────────────────────
def env_var(name: str) -> str:
    """The override variable for a tool: ffmpeg -> BGATE_FFMPEG."""
    return f"BGATE_{name.upper()}"


def local(name: str) -> Optional[str]:
    """The copy in ~/.bgate/bin, if there is one. Absolute path or None."""
    tool = TOOLS.get(name)
    candidates = tool.exes if tool else (f"{name}.exe", name)
    for exe in candidates:
        p = bin_dir() / exe
        if p.is_file():
            return str(p)
    return None


def resolve(name: str, given: str = "") -> Optional[str]:
    """The binary this machine should use for ``name``, or None.

    Precedence, most specific first — deliberately identical to ffmpegbin's,
    which this generalises:

      1. ``given``            an explicit argument, for a one-off
      2. ``BGATE_<NAME>``     this machine's choice
      3. ``~/.bgate/bin``     what the app installed, or what somebody put there
      4. PATH                 what everyone gets who has not thought about it

    An override naming something absent does NOT fall through to PATH. Falling
    through hands back the very binary the override existed to avoid and
    reports success — the failure ffmpegbin was written after.
    """
    explicit = (given or "").strip()
    if explicit:
        return _usable(explicit)
    override = (os.environ.get(env_var(name)) or "").strip()
    if override:
        return _usable(override)
    return local(name) or shutil.which(name)


def _usable(name: str) -> Optional[str]:
    if os.path.isfile(name):
        return name
    return shutil.which(name)


def status(name: str) -> dict:
    """Everything a panel needs to draw one row and its button."""
    tool = TOOLS.get(name)
    path = resolve(name)
    installed = local(name)
    return {
        "name": name,
        "summary": tool.summary if tool else "",
        "path": path or "",
        "present": bool(path),
        "managed": bool(installed and path == installed),
        "installable": bool(tool and tool.sha256),
        "size_mb": tool.size_mb if tool else 0,
        "source": ("override" if os.environ.get(env_var(name))
                   else "installed" if installed and path == installed
                   else "PATH" if path else "missing"),
    }


def statuses() -> list[dict]:
    return [status(n) for n in TOOLS]


# ── installation ────────────────────────────────────────────────────────────
class ToolError(RuntimeError):
    """Anything that stopped an install, in words a panel can show."""


def install(name: str, *, progress: Optional[Callable[[int, int], None]] = None,
            timeout: float = 300.0) -> dict:
    """Fetch, verify and unpack one tool into ~/.bgate/bin.

    Verified BEFORE anything is extracted, and extracted to a temporary
    directory before anything is moved into place, so a failed or tampered
    download cannot leave a half-installed binary that later reports present.

    ``progress`` is called with (bytes_so_far, bytes_total); total is 0 when the
    server does not send a length.
    """
    import tempfile
    import urllib.request

    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"no such tool: {name}")
    if not tool.sha256:
        raise ToolError(
            f"{name} has no pinned checksum, so it cannot be installed safely. "
            f"Run scripts/pin_tool.py {name} to record one.")

    target = bin_dir()
    target.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"bgate-{name}-") as tmp:
        archive = Path(tmp) / "download.zip"
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(tool.url, timeout=timeout) as r:
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with archive.open("wb") as fh:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        except Exception as exc:                                # noqa: BLE001
            raise ToolError(f"download failed: {type(exc).__name__}: {exc}") from exc

        got = digest.hexdigest()
        if got != tool.sha256:
            raise ToolError(
                f"checksum mismatch for {name} — expected {tool.sha256[:16]}…, "
                f"got {got[:16]}…. Nothing was installed.")

        staged = Path(tmp) / "staged"
        staged.mkdir()
        kept: list[str] = []
        try:
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                for member in tool.members:
                    # The archive root is a version-stamped directory, so match
                    # on the tail rather than on the full path — otherwise every
                    # release bump silently extracts nothing.
                    hit = next((n for n in names if n.replace("\\", "/").endswith(member)), None)
                    if hit is None:
                        raise ToolError(f"{member} is not in the {name} archive")
                    out = staged / Path(member).name
                    with zf.open(hit) as src, out.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    kept.append(out.name)
        except zipfile.BadZipFile as exc:
            raise ToolError(f"the {name} download is not a readable zip") from exc

        for f in kept:
            dest = target / f
            # Replace atomically where possible; on Windows a running binary
            # cannot be overwritten, which is a clearer error than a partial copy.
            try:
                os.replace(staged / f, dest)
            except OSError as exc:
                raise ToolError(
                    f"could not write {dest} — is it running? ({exc})") from exc
            if os.name != "nt":
                dest.chmod(dest.stat().st_mode | 0o111)

    return {"ok": True, "name": name, "installed": kept,
            "dir": str(target), **status(name)}
