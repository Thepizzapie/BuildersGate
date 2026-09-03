"""Generate the studio floor's soundtrack: chill lofi work tracks, several
genres, downloaded next to the floor's other art and listed in a manifest.

WHY THIS IS A SCRIPT AND NOT AN MCP TOOL, which is the open question the floor
plan left and this settles. The music tools that already exist -
music_generate, music_keep, music_install - file every take as an ARTIFACT
REVISION under the engine project and install the kept one into the GAME's music
library. That is correct for a game's score and wrong for this: the floor's
soundtrack is harness UI audio, the same kind of thing as the cast sprites and
the environment tiles, and a game that shipped the dashboard's background music
in its own res://audio folder would be carrying eight lofi tracks no cue in the
game ever plays.

So this writes where the floor's other art lives - frontend/public/audio/floor/ -
which Vite copies verbatim into bgate_ui/static/, so the files are served at
/static/audio/floor/, committed like the rest of the build output, and present
inside both the wheel and the PyInstaller build with no CDN to reach for.

IT COSTS REAL CREDITS AND IT SAYS SO BEFORE IT SPENDS ANY. One Suno request
returns several takes and every take is downloaded, so a four-brief run is
roughly eight tracks. --dry-run prints the briefs and the balance and submits
nothing, which is the sensible first thing to run.

NOTHING IS AUDITIONED HERE AND THAT IS DELIBERATE. The game pipeline has a keep
/discard gate because a wrong cue ships inside somebody's game. These are
background tracks in a dashboard the person running this script is looking at;
the audition is putting the music on and pressing skip. Deleting a file from the
folder and re-running --manifest-only is the whole of "discard".

    python scripts/gen_floor_music.py --dry-run --project ../bg-testbed
    python scripts/gen_floor_music.py --project ../bg-testbed
    python scripts/gen_floor_music.py --manifest-only
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from _floorpaths import FLOOR_AUDIO  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = FLOOR_AUDIO
MANIFEST = OUT / "manifest.json"

sys.path.insert(0, str(ROOT))


# THE BRIEFS ARE THE PLAN'S OWN WORDS: "chill lofi work tracks in several
# genres, generated as a set and CYCLED, so the floor has a soundtrack that does
# not repeat within a session."
#
# SEVERAL GENRES IS THE POINT, not one prompt run four times. A set that is four
# variations on the same brief is a single track with four masters, and the
# no-repeat bag in floorMusic.ts has nothing to work with - the reader hears the
# same room whichever file comes up. Each brief below is a different room to be
# in, and all of them are instrumental, mid-tempo and unobtrusive, because this
# plays under somebody trying to read a board.
#
# NO VOCALS ANYWHERE. A voice in the background of a dashboard is a second
# person talking in a room where the agents are already the ones with something
# to say - the same objection the floor's banter rules are built around.
BRIEFS: list[dict] = [
    {
        "genre": "lofi hip hop",
        "title": "Standup at Ten",
        "prompt": (
            "Chill instrumental lo-fi hip hop for background work. Dusty vinyl "
            "crackle, soft boom bap drums around 80 bpm, warm electric piano "
            "chords, muted upright bass. Calm, unhurried, no build, no drop. "
            "Nothing attention grabbing. Loops comfortably."
        ),
    },
    {
        "genre": "jazzhop",
        "title": "The Good Chair",
        "prompt": (
            "Mellow instrumental jazzhop. Brushed drums, walking double bass, "
            "soft rhodes and a distant muted trumpet. Late afternoon office "
            "feel, relaxed swing, around 85 bpm. Background music, never a "
            "solo that pulls focus."
        ),
    },
    {
        "genre": "ambient",
        "title": "Server Room Hum",
        "prompt": (
            "Warm ambient instrumental. Slow analog synth pads, gentle tape "
            "saturation, soft low drone underneath, occasional soft bell "
            "tones. No drums. Spacious, calm, patient. Suitable as quiet "
            "background for long focused work."
        ),
    },
    {
        "genre": "bossa lofi",
        "title": "Notes From Narrative",
        "prompt": (
            "Instrumental lo-fi bossa nova. Nylon string guitar, light shaker "
            "and rimshot percussion, soft rhodes pads, warm room tone, around "
            "90 bpm. Gentle and sunny without being cheerful. Background "
            "music for an office afternoon."
        ),
    },
    {
        "genre": "chillhop",
        "title": "Build Is Green",
        "prompt": (
            "Instrumental chillhop with a light head-nod groove. Clean plucked "
            "guitar, sub bass, soft tape drums around 88 bpm, warm pad wash. "
            "Steady and even throughout, no dramatic sections, no vocals."
        ),
    },
    {
        "genre": "downtempo",
        "title": "Ticket Closed Friday",
        "prompt": (
            "Slow downtempo instrumental. Deep round bass, sparse rim clicks, "
            "soft filtered synth chords, faint vinyl noise floor, around 72 "
            "bpm. Late and quiet. Background listening, no lead melody in "
            "front."
        ),
    },
]


def slugify(text: str) -> str:
    """A filename that is stable across regenerations and safe on Windows.

    Suno names a take whatever it likes and some of them contain characters no
    filesystem here will take. The manifest keeps the real title, so nothing is
    lost by making the file boring.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "track"


# WHAT SUNO HANDS BACK IS 169 kb/s STEREO, WHICH IS A MASTERING BITRATE FOR A
# FILE THAT PLAYS UNDER A DASHBOARD. Twelve of them is 42 MB committed to a
# repository forever, for background music nobody is listening closely to; at
# 96 kb/s the set is 22 MB and there is no audible difference at the volume this
# plays at. Done HERE rather than as a cleanup pass, so a regenerated set is
# never accidentally committed at the original size.
#
# BEST EFFORT, AND THE ORIGINAL IS KEPT WHEN IT FAILS. ffmpeg is not a hard
# dependency of this product - `bgate doctor` reports it as a row that may be
# red - so a machine without it still gets its tracks, just larger ones.
TARGET_BITRATE = "96k"


def transcode(src: Path, dest: Path) -> bool:
    """Re-encode `src` to `dest` at TARGET_BITRATE. False if ffmpeg could not."""
    try:
        from bgate_core.runtime import proc
    except Exception:
        return False
    exe = shutil.which("ffmpeg")
    if not exe:
        print("  (ffmpeg not found - keeping the provider's bitrate)")
        return False
    try:
        r = proc.run([exe, "-hide_banner", "-loglevel", "error", "-y",
                      "-i", str(src), "-c:a", "libmp3lame",
                      "-b:a", TARGET_BITRATE, "-map_metadata", "0", str(dest)],
                     capture_output=True, text=True, timeout=180)
    except Exception as exc:
        print(f"  (ffmpeg failed: {exc} - keeping the original)")
        return False
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        print("  (ffmpeg failed - keeping the original)")
        return False
    src.unlink(missing_ok=True)
    return True


def read_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            return {"tracks": []}
    return {"tracks": []}


def write_manifest(tracks: list[dict]) -> None:
    """The manifest lists WHAT IS ON DISK, not what was asked for.

    A row naming a file that is not there is a player that stalls on a track
    with nothing to show the reader about why, so the list is rebuilt from the
    directory every time rather than appended to.
    """
    on_disk = {p.name for p in OUT.glob("*.mp3")}
    kept = [t for t in tracks if t.get("file") in on_disk]
    known = {t["file"] for t in kept}
    # A file somebody dropped in by hand still plays; it just has no title but
    # its own name, which is better than being silently ignored.
    for name in sorted(on_disk - known):
        kept.append({"file": name, "title": Path(name).stem.replace("-", " ")})
    kept.sort(key=lambda t: t["file"])

    # SUNO NAMES BOTH TAKES OF A BRIEF THE SAME THING, because they are two
    # renderings of one idea and it titles the idea. The audio genuinely
    # differs, so the reader hears two tracks and sees one name twice - and the
    # now-playing label becomes useless for the one thing it is for, which is
    # telling somebody which track they liked. Numbered only where a title
    # actually collides: a unique title is left exactly as Suno wrote it.
    seen: dict[str, int] = {}
    for t in kept:
        seen[t["title"]] = seen.get(t["title"], 0) + 1
    run: dict[str, int] = {}
    for t in kept:
        if seen[t["title"]] > 1:
            run[t["title"]] = run.get(t["title"], 0) + 1
            t["title"] = f"{t['title']} ({run[t['title']]})"
    MANIFEST.write_text(
        json.dumps({"tracks": kept}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"manifest: {len(kept)} track(s) -> {MANIFEST}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the briefs and the balance, submit nothing")
    ap.add_argument("--manifest-only", action="store_true",
                    help="rebuild manifest.json from what is already on disk")
    ap.add_argument("--only", default="",
                    help="comma-separated genres to generate, default all")
    ap.add_argument("--model", default="", help="Suno model, default the adapter's")
    # WHERE THE KEY IS RESOLVED FROM, AND THE ONLY SAFE WAY TO POINT AT ONE.
    # This repo is not a game project and has no .env of its own, so a kie key
    # set per project - which is where `bgate key set kie` puts it without
    # --global - is invisible from here. Passing the PROJECT lets kie.api_key
    # do its own layered lookup (shell, then that project's .env, then
    # ~/.bgate/.env).
    #
    # THERE IS NO --key ARGUMENT AND THERE MUST NEVER BE ONE. A key on a command
    # line lands in shell history, in ps output and in any CI log that echoes
    # the command. Same rule `bgate key set` holds by prompting with echo off
    # and taking no key argument at all.
    ap.add_argument("--project", default="",
                    help="project directory whose .env holds KIE_API_KEY "
                         "(omit if the key is in the shell or ~/.bgate/.env)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    if args.manifest_only:
        write_manifest(read_manifest().get("tracks", []))
        return 0

    from bgate_adapters import kie

    wanted = [b for b in BRIEFS
              if not args.only or b["genre"] in
              {g.strip() for g in args.only.split(",")}]
    if not wanted:
        print(f"no brief matches --only={args.only!r}", file=sys.stderr)
        return 2

    key_root = args.project or None

    balance = None
    try:
        balance = kie.credit_balance(key_root)
    except Exception as exc:  # pragma: no cover - network
        print(f"balance unreadable: {exc}")

    print(f"out       {OUT}")
    print(f"balance   {balance if balance is not None else 'unknown'}")
    print(f"briefs    {len(wanted)}  (one request returns several takes)")
    for b in wanted:
        print(f"  - {b['genre']:<14} {b['title']}")
    if args.dry_run:
        print("\ndry run: nothing submitted, nothing charged")
        return 0

    try:
        kie.api_key(key_root)
    except Exception as exc:
        print(f"no kie key in force: {exc}", file=sys.stderr)
        print("set one with: bgate key set kie --global", file=sys.stderr)
        return 1

    tracks = read_manifest().get("tracks", [])
    by_file = {t["file"]: t for t in tracks}

    for brief in wanted:
        print(f"\n=== {brief['genre']}: {brief['title']} ===")
        staging = OUT / f".staging-{slugify(brief['genre'])}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        opts = {"instrumental": True, "root": key_root}
        if args.model:
            opts["model"] = args.model
        try:
            res = kie.generate_music(brief["prompt"], staging, **opts)
        except Exception as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            shutil.rmtree(staging, ignore_errors=True)
            continue
        if not res.get("ok"):
            print(f"  FAILED: {res.get('error') or res}", file=sys.stderr)
            shutil.rmtree(staging, ignore_errors=True)
            continue

        cost = res.get("credits_consumed")
        print(f"  charged: {cost if cost is not None else 'unknown'} "
              f"({res.get('credits_source')})")

        for n, take in enumerate(res.get("tracks", []), start=1):
            src = Path(take.get("path", ""))
            if not src.exists():
                print(f"  take {n}: no file at {src}", file=sys.stderr)
                continue
            name = f"{slugify(brief['genre'])}-{slugify(brief['title'])}-{n}.mp3"
            dest = OUT / name
            if not transcode(src, dest):
                shutil.move(str(src), dest)
            by_file[name] = {
                "file": name,
                # SUNO'S OWN TITLE WHEN IT GAVE ONE. It names a take after what
                # it heard in the brief, and that is a better label for a reader
                # picking a favourite than a number the script chose.
                "title": str(take.get("title") or brief["title"]),
                "genre": brief["genre"],
                "seconds": take.get("duration") or take.get("seconds"),
            }
            print(f"  take {n}: {name}")
        shutil.rmtree(staging, ignore_errors=True)

    write_manifest(list(by_file.values()))
    print("\nrebuild the frontend so the files reach bgate_ui/static:")
    print("  cd frontend && npm run build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
