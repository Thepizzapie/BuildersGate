"""Turn the games on this machine into a directory a static host can serve.

The output is deliberately boring: HTML, CSS, an SVG, and the Godot builds. No
JavaScript of ours runs on the page — the only script on the site is the engine's
own loader. That is what makes it safe to hand to any host and what makes a
broken publish obvious (a missing file 404s) instead of subtle.

Layout written:

    index.html                 the arcade
    arcade.css  favicon.svg
    _headers                   Cloudflare Pages / Netlify
    games.json                 machine-readable index of what shipped
    games/<slug>/index.html    the game's page (embeds the build)
    games/<slug>/build/...     the Godot Web export, verbatim
    games/<slug>/cover.png     if the project has cover art

`build/` is a subdirectory rather than the game page itself so the engine's own
shell keeps its filenames (index.js/index.wasm/index.pck resolve relative to it)
AND the game gets a real page around it. Serving Godot's shell as the game page
would mean no title, no description, no controls, no way back.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import collect

THEME = Path(__file__).resolve().parent / "theme"

# Written into the output root on the first publish. Nothing is ever deleted
# from a directory that does not carry it — a publish command that can rm -rf an
# arbitrary --out is one typo away from eating someone's Desktop.
STAMP = ".bgate-arcade"

# _headers is NOT in here: it is generated (theme/_headers is its preamble),
# because the per-file Content-Encoding rules below depend on what shipped.
STATIC = ("arcade.css", "favicon.svg")

REBUILD_MODES = ("stale", "always", "never")

MIB = 1024 * 1024

# Per-file upload ceilings, because Godot 4's release wasm is ~38MiB and every
# free host has an opinion about that. `bgate publish` refuses to let you find
# this out from a failed deploy: it measures, compresses, and says so.
#
# Cloudflare Pages and Workers both stop at 25MiB per asset. GitHub Pages allows
# 100MiB per file (1GB per site) and itch.io a gigabyte, so neither needs the
# compression pass — but it costs them nothing either.
HOSTS = {
    "cloudflare": {"limit": 25 * MIB, "precompress": True,
                   "deploy": "npx wrangler pages deploy {out}"},
    "netlify":    {"limit": 25 * MIB, "precompress": True,
                   "deploy": "npx netlify deploy --prod --dir {out}"},
    "github":     {"limit": 100 * MIB, "precompress": False,
                   "deploy": "commit {out} to a gh-pages branch"},
    "itch":       {"limit": 1000 * MIB, "precompress": False,
                   "deploy": "zip {out} and upload it to itch.io"},
    "none":       {"limit": 0, "precompress": False, "deploy": ""},
}

# Only these are worth compressing — the rest of a Godot export is already
# small, and gzipping a 2KB png costs more than it saves.
COMPRESSIBLE = (".wasm", ".pck", ".js", ".data", ".symbols")

_MARK = (
    '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M14 12 V52" stroke="#9aa3b2" stroke-width="7" stroke-linecap="square"/>'
    '<path d="M28 20 L44 32 L28 44" stroke="#ff6a3d" stroke-width="8" fill="none" '
    'stroke-linecap="square"/></svg>'
)

_SLUG_OK = re.compile(r"[^a-z0-9._-]+")


def _slug(text: str) -> str:
    """A slug safe to use as a directory name and a URL segment."""
    cleaned = _SLUG_OK.sub("-", str(text).strip().lower()).strip("-._")
    return cleaned or "game"


def _esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit in ("B", "KB") else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GB"


def _gzip_in_place(path: Path) -> int:
    """Replace a file with its gzip bytes, keeping the name. Returns new size.

    The name is kept on purpose. The engine's loader asks for index.wasm and it
    is not ours to patch, so the file at that URL stays index.wasm and the host
    is told, via _headers, that its body is gzipped. Browsers have decompressed
    Content-Encoding responses since forever; the transport layer unwraps it
    before WebAssembly.instantiateStreaming ever sees a byte.

    gzip rather than brotli because it is in the standard library and every
    browser accepts it. Brotli would save ~1.5MiB on a 38MiB wasm and cost a
    dependency plus a failure mode on clients that do not advertise `br`.
    """
    import gzip

    raw = path.read_bytes()
    packed = gzip.compress(raw, 9)
    path.write_bytes(packed)
    return len(packed)


def _fill(template: str, values: dict) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def _paragraphs(text: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", str(text or "")) if b.strip()]
    return "".join(f"<p>{_esc(b)}</p>" for b in blocks)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _controls_html(actions: list[dict]) -> str:
    """The project's real bindings, or an honest empty state.

    Never invent a default control scheme. The dashboard learned this the hard
    way: a hardcoded hint told every player the wrong keys for every game but
    one.
    """
    rows = []
    for action in actions:
        keys = list(action.get("keys") or []) + list(action.get("buttons") or [])
        if not keys:
            continue
        pressed = "".join(f"<kbd>{_esc(k)}</kbd>" for k in keys)
        label = _esc(str(action.get("action", "")).replace("_", " "))
        rows.append(f"<tr><td>{label}</td><td>{pressed}</td></tr>")
    if not rows:
        return ('<p style="color:var(--ash2);font-size:13.5px">This game does not '
                'declare any custom input actions.</p>')
    return '<table class="keys">' + "".join(rows) + "</table>"


def _card_html(game: dict) -> str:
    cover = game.get("cover_url") or ""
    if cover:
        klass = " class=\"icon\"" if cover.endswith(".svg") else ""
        art = f'<img src="{_esc(cover)}" alt=""{klass} loading="lazy">'
    else:
        art = ('<svg viewBox="0 0 24 24" width="52" height="52" fill="none" '
               'stroke="#3a3a44" stroke-width="1.6" aria-hidden="true">'
               '<rect x="2" y="6" width="20" height="12" rx="3"/>'
               '<path d="M7 12h3M8.5 10.5v3M15 11.5h.01M17.5 13.5h.01"/></svg>')
    tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in game.get("tags", [])[:3])
    return f"""<a class="card" href="games/{_esc(game['slug'])}/">
  <div class="shot">{art}</div>
  <div class="body">
    <h2>{_esc(game['title'])}</h2>
    <p>{_esc(game.get('tagline') or '')}</p>
    <div class="foot">{tags or _esc(game.get('dimension') or '')}
      <span class="play">play &rarr;</span></div>
  </div>
</a>"""


def _nav(config: dict) -> str:
    links = []
    url = str(config.get("source_url") or "")
    if url:
        links.append(f'<a href="{_esc(url)}" target="_blank" rel="noopener">source</a>')
    for item in config.get("links", []) or []:
        if isinstance(item, dict) and item.get("href") and item.get("label"):
            links.append(f'<a href="{_esc(item["href"])}" target="_blank" '
                         f'rel="noopener">{_esc(item["label"])}</a>')
    return "".join(links)


def _shell(config: dict) -> dict:
    author = str(config.get("author") or "")
    year = datetime.now().year
    footer = f"&copy; {year} {_esc(author)}" if author else \
        f"published {datetime.now():%d %b %Y}"
    return {
        "TITLE": _esc(config.get("title")),
        "TAGLINE": _esc(config.get("tagline")),
        "MARK": _MARK,
        "NAV": _nav(config),
        "FOOTER": footer,
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _claim(out: Path, force: bool) -> Optional[str]:
    """Make sure we are allowed to write here. Returns an error string or None."""
    stamp = out / STAMP
    if stamp.exists():
        return None
    if out.exists() and any(out.iterdir()) and not force:
        return (f"{out} is not empty and was not created by bgate publish — "
                "pass --force to publish into it anyway (existing files with "
                "the same names WILL be overwritten)")
    out.mkdir(parents=True, exist_ok=True)
    stamp.write_text(
        "This directory is generated by `bgate publish`. Files here are\n"
        "overwritten on every publish — edit the theme in bgate_site/theme/\n"
        "or the per-game .bgate/site.json instead.\n", encoding="utf-8")
    return None


def _copy_build(src: Path, dest: Path) -> int:
    """Replace dest with the current export. Returns bytes copied."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())


def _copy_cover(cover: Path, dest_dir: Path) -> str:
    suffix = cover.suffix.lower() or ".png"
    target = dest_dir / f"cover{suffix}"
    shutil.copy2(cover, target)
    return target.name


def _fit_host(build_dir: Path, url_prefix: str, host: dict) -> tuple[list[dict], list[dict]]:
    """Squeeze a copied build under the host's per-file ceiling.

    Returns (encoded, oversize): files that were gzipped and now need a
    Content-Encoding rule, and files that are STILL too big to upload. The
    second list is the one that matters — it is the difference between finding
    out here and finding out from a deploy that dies four minutes in.
    """
    limit = host.get("limit") or 0
    encoded: list[dict] = []
    oversize: list[dict] = []
    if not limit:
        return encoded, oversize

    for path in sorted(build_dir.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size <= limit:
            continue
        rel = path.relative_to(build_dir).as_posix()
        if host.get("precompress") and path.suffix.lower() in COMPRESSIBLE:
            packed = _gzip_in_place(path)
            if packed <= limit:
                encoded.append({"url": f"{url_prefix}/{rel}", "was": size,
                                "now": packed})
                continue
            oversize.append({"url": f"{url_prefix}/{rel}", "bytes": packed,
                             "gzipped": True})
        else:
            oversize.append({"url": f"{url_prefix}/{rel}", "bytes": size,
                             "gzipped": False})
    return encoded, oversize


def _write_headers(out: Path, encoded: list[dict], host_name: str) -> None:
    """theme/_headers, plus one rule per pre-compressed file.

    Content-Type is restated because the whole point of the exercise is a file
    whose bytes are gzip while its name still says .wasm — a host that sniffed
    the body instead of the extension would otherwise serve it as something the
    engine refuses to stream.
    """
    text = (THEME / "_headers").read_text(encoding="utf-8")
    if encoded:
        types = {".wasm": "application/wasm", ".js": "text/javascript",
                 ".pck": "application/octet-stream"}
        lines = [
            "",
            "# ---- generated by bgate publish, do not hand-edit ----",
            f"# These files are over the per-file upload limit for {host_name} in",
            "# their raw form, so they ship gzipped UNDER THEIR ORIGINAL NAMES and",
            "# the browser is told to unwrap them. Remove these rules and the",
            "# matching files become undecodable garbage, not merely slower.",
        ]
        for item in encoded:
            suffix = Path(item["url"]).suffix.lower()
            lines.append(f"/{item['url'].lstrip('/')}")
            lines.append("  Content-Encoding: gzip")
            if suffix in types:
                lines.append(f"  Content-Type: {types[suffix]}")
            lines.append("  Vary: Accept-Encoding")
        text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    (out / "_headers").write_text(text, encoding="utf-8")


def _prune(out: Path, keep: set[str]) -> list[str]:
    """Remove game directories from a previous publish that no longer apply.

    Only ever inside out/games, only ever directories, and only when the stamp
    said this tree is ours.
    """
    games = out / "games"
    if not games.is_dir():
        return []
    dropped = []
    for child in sorted(games.iterdir()):
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)
            dropped.append(child.name)
    return dropped


# ---------------------------------------------------------------------------
# The one call the CLI makes
# ---------------------------------------------------------------------------
def build(out: str | os.PathLike[str], *,
          roots: Optional[Iterable[str | os.PathLike[str]]] = None,
          rebuild: str = "stale",
          config: str | os.PathLike[str] | None = None,
          host: str = "cloudflare",
          force: bool = False,
          dry_run: bool = False) -> dict:
    """Publish every publishable game into `out`.

    rebuild   stale  — export any game whose build is missing or older than its
                       source (the default; what you almost always want)
              always — re-export everything, even if it looks current
              never  — ship whatever build is already on disk, or skip the game

    host      which upload limits to respect, and whether to pre-compress the
              files that break them. See HOSTS.

    Returns a report. `ok` is False only when the site could not be written at
    all — a single game that fails to export is reported in `errors` and the
    rest of the arcade still ships, because one broken project should not cost
    you the other five.
    """
    if rebuild not in REBUILD_MODES:
        return {"ok": False, "error": f"rebuild must be one of {REBUILD_MODES}"}
    if host not in HOSTS:
        return {"ok": False,
                "error": f"host must be one of {'|'.join(HOSTS)}, got {host!r}"}
    host_profile = HOSTS[host]

    started = time.time()
    out = Path(out).expanduser().resolve()
    settings = collect.site_config(config)
    cards = collect.discover(roots)

    report: dict = {
        "ok": True, "out": str(out), "dry_run": dry_run, "host": host,
        "config": settings.get("source", ""),
        "games": [], "skipped": [], "errors": [], "pruned": [],
        "compressed": [], "oversize": [],
        "deploy": host_profile["deploy"].format(out=out) if host_profile["deploy"] else "",
        "bytes": 0, "seconds": 0.0,
    }

    for card in cards:
        if not card.get("publishable"):
            report["skipped"].append({
                "slug": card.get("slug"), "root": card.get("root"),
                "reason": card.get("skip_reason") or "not publishable"})

    live = [c for c in cards if c.get("publishable")]

    # ---- export ----------------------------------------------------------
    if not dry_run and rebuild != "never":
        from bgate_ui import webbuild
        for card in live:
            need = rebuild == "always" or not card["built"] or card["stale"]
            if not need:
                continue
            result = webbuild.rebuild(card["root"])
            if not result.get("ok"):
                report["errors"].append({
                    "slug": card["slug"], "stage": "export",
                    "error": result.get("error", "export failed"),
                    "detail": result.get("detail", "")})
                continue
            card.update(collect.describe(card["root"]))

    ready, deferred = [], []
    for card in live:
        (ready if card.get("built") else deferred).append(card)
    failed = {row["slug"] for row in report["errors"]}
    for card in deferred:
        if card["slug"] in failed:
            continue          # the export error above already says why
        report["skipped"].append({
            "slug": card["slug"], "root": card["root"],
            "reason": "no web build to publish" +
                      (" (rebuild=never)" if rebuild == "never" else "")})

    # Slugs become directory names and URLs; two projects called "Ember Run" in
    # different folders must not silently overwrite each other's build.
    used: set[str] = set()
    for card in ready:
        slug = _slug(card["slug"])
        if slug in used:
            n = 2
            while f"{slug}-{n}" in used:
                n += 1
            slug = f"{slug}-{n}"
        used.add(slug)
        card["slug"] = slug

    if dry_run:
        report["games"] = [{
            "slug": c["slug"], "title": c["title"], "root": c["root"],
            "url": f"games/{c['slug']}/", "bytes": c["build_bytes"],
            "stale": c["stale"], "cover": bool(c["cover"])} for c in ready]
        report["bytes"] = sum(c["build_bytes"] for c in ready)
        report["seconds"] = round(time.time() - started, 2)
        return report

    # ---- write -----------------------------------------------------------
    problem = _claim(out, force)
    if problem:
        return {**report, "ok": False, "error": problem}

    for name in STATIC:
        shutil.copy2(THEME / name, out / name)

    shell = _shell(settings)
    game_template = (THEME / "game.html").read_text(encoding="utf-8")

    for card in ready:
        dest = out / "games" / card["slug"]
        dest.mkdir(parents=True, exist_ok=True)
        try:
            written = _copy_build(Path(card["build_dir"]), dest / "build")
        except OSError as exc:
            report["errors"].append({"slug": card["slug"], "stage": "copy",
                                     "error": f"{type(exc).__name__}: {exc}"})
            continue

        encoded, oversize = _fit_host(dest / "build",
                                      f"games/{card['slug']}/build", host_profile)
        report["compressed"].extend(encoded)
        for row in oversize:
            row["slug"] = card["slug"]
            report["oversize"].append(row)
            report["errors"].append({
                "slug": card["slug"], "stage": "size",
                "error": f"{row['url']} is {_size(row['bytes'])}"
                         + (" even gzipped" if row["gzipped"] else "")
                         + f", over the {_size(host_profile['limit'])} per-file "
                           f"limit for {host} — that deploy will be rejected"})
        if encoded:
            written = sum(p.stat().st_size for p in (dest / "build").rglob("*")
                          if p.is_file())

        cover_url = ""
        if card["cover"]:
            try:
                cover_url = _copy_cover(Path(card["cover"]), dest)
            except OSError:
                cover_url = ""
        card["cover_url"] = f"games/{card['slug']}/{cover_url}" if cover_url else ""

        body = _paragraphs(card["description"] or card["tagline"])
        if card["credits"]:
            body += f'<p style="color:var(--ash2);font-size:13px">{_esc(card["credits"])}</p>'
        og = (f'<meta property="og:image" content="{_esc(cover_url)}">'
              if cover_url and not cover_url.endswith(".svg") else "")
        kicker = " · ".join(x for x in [card["dimension"], card["engine"]] if x)

        (dest / "index.html").write_text(_fill(game_template, {
            **shell,
            "GAME_TITLE": _esc(card["title"]),
            "GAME_TAGLINE": _esc(card["tagline"]),
            "GAME_KICKER": _esc(kicker or "playable"),
            "GAME_BODY": body or "<p>No description yet.</p>",
            "CONTROLS": _controls_html(card["controls"]),
            "TAGS": "".join(f'<span class="tag">{_esc(t)}</span>'
                            for t in card["tags"]),
            "OG_IMAGE": og,
        }), encoding="utf-8")

        report["bytes"] += written
        report["games"].append({
            "slug": card["slug"], "title": card["title"], "root": card["root"],
            "url": f"games/{card['slug']}/", "bytes": written,
            "cover": bool(cover_url), "tagline": card["tagline"],
            "tags": card["tags"], "dimension": card["dimension"]})

    _write_headers(out, report["compressed"], host)
    report["pruned"] = _prune(out, {g["slug"] for g in report["games"]})

    published = [c for c in ready if any(g["slug"] == c["slug"]
                                         for g in report["games"])]
    cards_html = "\n".join(_card_html(c) for c in published)
    if cards_html:
        body_html = f'<div class="grid">{cards_html}</div>'
    else:
        body_html = (
            '<div class="empty"><b>No games here yet.</b><br><br>'
            'Every Builders Gate project with a Godot game and a Web export '
            'lands on this page. Make one with <code>bgate init my-game</code>, '
            'then run <code>bgate publish</code> again.</div>')

    (out / "index.html").write_text(_fill(
        (THEME / "index.html").read_text(encoding="utf-8"), {
            **shell,
            "KICKER": _esc(settings.get("kicker") or "arcade"),
            "CARDS": body_html,
            "GAME_COUNT": len(published),
            "GAME_WORD": " game" if len(published) == 1 else " games",
            "TOTAL_SIZE": _size(report["bytes"]),
            "UPDATED": datetime.now().strftime("%d %b"),
        }), encoding="utf-8")

    # games.json IS PUBLISHED. The report is not — it is printed on the machine
    # that ran the command, and its `root` is that machine's absolute path to
    # the game (`C:\Users\<name>\...`). Writing the report through to the
    # site verbatim put a username and a directory layout on the public web,
    # which is a fact about the author's computer and no use to a visitor.
    # Anything a reader needs is already keyed by `slug` and `url`.
    (out / "games.json").write_text(json.dumps({
        "title": settings.get("title"),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": [{k: v for k, v in g.items() if k != "root"}
                  for g in report["games"]],
    }, indent=2), encoding="utf-8")

    report["seconds"] = round(time.time() - started, 2)
    return report
