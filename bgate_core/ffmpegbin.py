"""Which ffmpeg. One answer, one place, and overridable.

WHY THIS EXISTS, and it is not tidiness. Six modules independently called
``shutil.which("ffmpeg")`` — cinematic, cinecut, animatic, audiolab, doctor and
the playtest recorder — which means the project had no way to use an ffmpeg
other than the first one on PATH, and no way to say so.

That became a real problem rather than a theoretical one. The ffmpeg installed
on the developing machine (``8.1.1-full_build-www.gyan.dev``, via winget) has a
libtheora that ENCODES WITHOUT ERROR AND PRODUCES FILES THE DECODER CANNOT READ:
measured, 179 of 193 frames throwing ``error in unpack_block_qpis`` and a
cutscene of flat green rectangles in the game. ``--enable-libtheora`` is present,
the encoder loads, it exits 0. Nothing downstream could tell.

BE PRECISE ABOUT WHOSE FAULT THIS IS, because the obvious summary is wrong and
sends people to the wrong remedy. It is NOT "gyan.dev builds are bad": the
binary this project now ships alongside is ``7.1-essentials_build-www.gyan.dev``
and round-trips Theora with ZERO errors on the same machine and the same
command. It is a regression in one build line. Measured, one-second probe:

    8.1.1-full  (gyan)      37 decode errors
    7.1-essentials (gyan)    0 decode errors

GyanD/codexffmpeg#200 reports the same symptom against a 2025-09-25 build and
notes BtbN's build of the same version is fine, which is consistent with a
regression that entered the newer line. So the rule worth remembering is not
"prefer packager X" — it is "PROVE THE BUILD, DO NOT TRUST ITS VERSION STRING",
which is what cinematic.ffmpeg_status's round-trip probe now does.

RESOLUTION ORDER, most specific first:

    1. an explicit argument                     a deliberate one-off
    2. BGATE_FFMPEG                             this machine's choice
    3. ~/.bgate/bin/ffmpeg[.exe]                a known-good binary kept beside
                                                the global .env, for exactly the
                                                case where PATH's ffmpeg is the
                                                thing you are escaping
    4. ffmpeg on PATH                           what everyone else gets

Three sits above PATH deliberately. Somebody who put a binary in ``~/.bgate/bin``
did it on purpose; whatever is on PATH is usually whatever a package manager
happened to install, and on this machine that was the broken one.

DELIBERATELY NOT A SETTING IN THE DATABASE. This is a property of the MACHINE,
not of the game: two projects on one desktop want the same ffmpeg, and a project
copied to another desktop must not carry a path that only existed on the first.
It is the same reasoning, and the same location, as the global provider-key
store — and ``doctor`` prints WHICH source supplied the binary, because "it is
configured and nothing works" is the question that layer already learned to
answer up front.
"""
from __future__ import annotations

import os
import shutil
from typing import Optional

#: Environment variable naming the ffmpeg to use. Absolute path, or a bare name
#: resolved on PATH.
ENV_VAR = "BGATE_FFMPEG"


def local_bin() -> Optional[str]:
    """``~/.bgate/bin/ffmpeg``, if there is one. Absolute path or None.

    Beside the global ``.env`` and for the same reason: a fact about this
    machine that every project on it should inherit, in a directory that is not
    a repository and so has nothing to leak into.

    DELEGATES TO bgate_core.toolbin, which generalised this directory into the
    place the app INSTALLS tools rather than only the place a person may have
    put one. The resolution order below is unchanged and was the model for it;
    what changed is that the app can now fill the directory itself, so "ffmpeg
    is missing" is a button rather than a paragraph of instructions.
    """
    from bgate_core import toolbin
    return toolbin.local("ffmpeg")


def resolve(given: str = "") -> Optional[str]:
    """The ffmpeg this machine should use, or None if there isn't one.

    Precedence, most specific first — the same shape as the provider-key layer,
    for the same reason: an override that can be silently outranked is an
    override nobody can trust. See the module docstring for why ``~/.bgate/bin``
    outranks PATH rather than backstopping it.

      1. ``given``  — an explicit argument from the caller, for a one-off.
      2. ``BGATE_FFMPEG`` — this machine's choice.
      3. ``~/.bgate/bin/ffmpeg`` — a binary kept deliberately.
      4. ``ffmpeg`` on PATH — what everyone gets who has not thought about it.

    A ``BGATE_FFMPEG`` that names something that is not there does NOT fall
    through. Falling through would quietly hand back the very binary the
    override existed to avoid, and the caller would be told everything is fine —
    which is precisely the failure this module was written after. A path given
    by the caller behaves the same way, and for the same reason.
    """
    explicit = (given or "").strip()
    if explicit:
        return _usable(explicit)
    override = (os.environ.get(ENV_VAR) or "").strip()
    if override:
        # Refuse rather than fall back. See the docstring.
        return _usable(override)
    return local_bin() or shutil.which("ffmpeg")


def _usable(name: str) -> Optional[str]:
    """An absolute path that exists, or a name found on PATH. Else None."""
    if os.path.isfile(name):
        return name
    return shutil.which(name)


def source(exe: Optional[str] = None) -> str:
    """Where the resolved ffmpeg came from, for a human reading a doctor row.

    The question worth answering when a binary is set and nothing works is not
    "is one configured" but "WHICH one is actually in force", and that is only
    answerable by the code that did the resolving.
    """
    override = (os.environ.get(ENV_VAR) or "").strip()
    if override:
        return (f"{ENV_VAR}" if _usable(override)
                else f"{ENV_VAR} (set to {override!r}, which is not there)")
    if local_bin():
        return "~/.bgate/bin"
    return "PATH" if (exe or shutil.which("ffmpeg")) else "not found"
