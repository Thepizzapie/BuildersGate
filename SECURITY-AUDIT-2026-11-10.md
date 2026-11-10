# Builders Gate — public repository audit

**Date:** 2026-11-10 · **Commit:** `9175edc` (branch `claude/confident-johnson-k1ynth`)
**Scope:** working tree + full git history (`--all`), 1,252 tracked files,
~187k LOC source / ~90k LOC tests, Python + React frontend, CI workflows.

---

## 1. Secrets & PII — CRITICAL

### No secrets found. Confirmed explicitly.

| Check | Result |
|---|---|
| Known token prefixes (`sk-`, `sk-ant-`, `sk-proj-`, `ghp_`, `github_pat_`, `gho_`, `AKIA`, `AIza`, `xox[baprs]-`, `glpat-`, `dop_v1_`, `hf_`, `SG.`, `oauth:`, PEM private keys) in working tree | 0 real — every hit is a placeholder |
| Same scan across **every commit diff in all branches** (`git log --all -p`) | 0 real — only `AAAA…`-style fixtures |
| Credential files ever tracked (`.env`, `*.pem`, `*.key`, `id_rsa`, `.npmrc`, `.pypirc`, `credentials*`) | only `.env.example`, all values empty |
| Real email addresses | none |
| `pip-audit` against the declared dependency floors | **No known vulnerabilities found** |
| `npm audit --package-lock-only` (frontend) | **0 vulnerabilities** |

Every apparent hit was verified as a deliberate test fixture:

- `tests/board/test_streamer.py`, `tests/ui/test_streamer_ui.py` — fake keys that
  exercise `src/bgate_core/board/streamer.py`'s redaction filter.
- `tests/runtime/test_provider_keys.py:24` — `sk-test-DO-NOT-USE-…`.
- `tests/board/test_streamer.py:173` — `https://marta:hunter2@github.com/x/y.git`,
  a credential-in-URL fixture for the same scrubber.
- Private IPs `100.64.0.9` (Tailscale CGNAT), `192.168.1.40`, `10.0.0.5` — fixtures
  for the tailnet pairing guard and the webhook SSRF guard, not real infrastructure.
- Home paths with names (`marta`, `nina`, `robin`, `bryan`) — fixtures for the
  path-redaction tests.

`.gitignore` correctly ignores `.env` and `.env.*` with a `!.env.example` exception,
and both `init` and `adopt` stamp that file into adopted projects. Commit authorship
uses GitHub `noreply` addresses throughout — no personal email in history.

### Should-not-be-public: one file

**`.github/instructions/codacy.instructions.md` is public, and someone tried to
stop that.** `.gitignore` line 48 reads:

```
.github\instructions\codacy.instructions.md
```

Backslashes. Git treats `\` as an escape character, never as a path separator, so
this pattern matches nothing — `git check-ignore` exits 1 and the file is tracked.
The intent to exclude it is on the record; the exclusion never happened.

Beyond the hygiene point, the content matters for *this* repo specifically. It is a
71-line vendor-generated agent instruction file with `applyTo: '**'` front matter and
directives phrased as `## CRITICAL: …` / `YOU MUST IMMEDIATELY run the
codacy_cli_analyze tool` / *"Failure to follow this rule is considered a critical
error."* It also tells agents to offer to install a CLI and to send users to
`github.com/settings/copilot/features`. In a public repository whose entire purpose is
to be driven by coding agents, that is an unreviewed instruction surface sitting in
the conventional directory agents read first.

**Fix:** decide whether Codacy is in use. If not, `git rm` it. If yes, keep it
deliberately and fix the ignore rule to use forward slashes (`.github/instructions/codacy.instructions.md`)
so the next file dropped there is actually excluded.

---

## 2. Code Quality

### What is genuinely strong

Stating this because an audit that only lists problems misrepresents the codebase.

- **Auth.** All four token comparisons use `secrets.compare_digest`
  (`api.py:533,579`, `routes/remote.py:48`, `routes/voice.py:215`). No string `==`.
- **DNS-rebinding gate** (`api.py:485-492`) runs *before* and independently of
  `BGATE_NO_AUTH`, and `0.0.0.0` is deliberately excluded from `_LOOPBACK_HOSTS`
  with the "0.0.0.0 day" bypass written up in the comment.
- **The websocket does not silently escape the middleware.**
  `@app.middleware("http")` never runs for a websocket scope; `routes/voice.py:179-196`
  re-implements `is_desk`, the origin check and the token check by hand, and the
  module docstring says why. This is the single most commonly missed auth bypass in
  FastAPI apps and it is closed here.
- **SSRF guard** on the outbound webhook (`agents/followup.py:webhook_target`):
  https-only, and *every* address the host resolves to must be public. The residual
  DNS-rebinding TOCTOU is documented and accepted rather than hidden.
- **Archive handling.** `runtime/toolbin.py:install` pins a SHA-256, verifies
  **before** extracting, extracts by basename into a tempdir (no zip-slip), then
  `os.replace`s atomically.
- **SQLite.** WAL, `busy_timeout`, per-thread cached connections, and migrations
  applied as one transaction with the `user_version` bump — the comment at
  `db.py:_apply_sql_step` documents the wedged-database bug that motivated it.
- **Path containment** on `/api/preview` uses `resolve()` + `relative_to()`, the
  correct idiom.
- **Dependency discipline is better than most commercial projects.** Floors are
  treated as a security control, `security.yml` audits both the resolved tree and
  the pinned floors (because a floor-only vulnerability is invisible to the resolved
  run), and CodeQL is narrowed to the default suite with the triage of the 277-alert
  `security-and-quality` pass written down, including the one real ReDoS it found.
- `ruff check .` passes clean. **Zero** `TODO`/`FIXME`/`XXX`/`HACK` markers in `src/`.

### Findings

**Q1 — `src/bgate_mcp/server.py` is 11,357 lines and holds a deliberate import cycle.**
Ten tool modules do `from bgate_mcp.server import _root, _tool, …`, and `server.py`
star-imports all ten back at line 11021. Under `python -m bgate_mcp.server` — the
command every MCP registration runs — this only boots because of a
`sys.modules.setdefault("bgate_mcp.server", sys.modules[__name__])` at line 53. The
cycle is documented at both ends, which makes it a known risk rather than an unknown
one, but the failure mode is sharp: moving any shared name below line 11021, or
adding a tool module that needs something defined later, breaks the whole MCP server
boot, and the error surfaces as a circular `ImportError` far from the cause. Ten
`# noqa: F401,F403` suppressions mean the linter cannot help.

**Q2 — Dead code on a hot path: `src/bgate_core/audio/audiohooks.py:181-183`.**

```python
starts = [0]
for ch in text:
    starts.append(starts[-1] + (1 if ch == "\n" else 0))

def line_of(pos: int) -> int:
    return text.count("\n", 0, pos) + 1
```

`starts` is never read after the loop — `line_of` ignores it entirely. This is a
per-character Python loop and a list one element long per byte, built and discarded
for **every source file in the project**, inside a function the docstring describes as
serving a workspace poll. Deleting three lines removes the whole cost. (While there:
`line_of` is `O(n)` per call via `text.count`, so the scan is `O(n·matches)` — the
discarded `starts` array was presumably the intended fix for exactly that, and
`bisect` over a newline-offset list would finish the job.)

**Q3 — The `[floor]` extra cannot currently be built.** `pyproject.toml` is at
`0.1.46`; `packaging/floor-assets/pyproject.toml` is at `0.1.43`. Its own README says
*"The version tracks the main package's pyproject.toml; bump both together."* Reproduced:

```
$ python packaging/build_floor_assets.py
error: pyproject.toml says 0.1.46 but packaging/floor-assets/pyproject.toml says 0.1.43 — bump them together
$ echo $?
1
```

The guard works. Nothing in CI runs it, so three releases of drift went unnoticed —
exactly the failure mode `frontend-drift` was added to `ci.yml` to catch for the
committed React bundle. Same shape, different artifact.

**Q4 — The npm ecosystem is unmonitored.** `.github/dependabot.yml` covers `pip` and
`github-actions` only. `frontend/package-lock.json` (vite, react, playwright, and
their transitive tree) is scanned by nothing, and `src/bgate_ui/static/dist/` is
**committed build output that ships inside the wheel** — so a frontend advisory would
reach every `pip install builders-gate` user with no scan anywhere in its path. The
Python side gets `pip-audit` twice plus dependabot; the JavaScript side gets nothing.
`npm audit --package-lock-only` is clean today, so this is a monitoring gap rather
than a live vulnerability — but it is the only ecosystem in the repo with no
standing check.

**Q5 — The dashboard sets no security response headers, and `/api/preview` serves
`.svg` inline.** There is no CSP and no `X-Content-Type-Options: nosniff` anywhere in
`bgate_ui` (only the static marketing site's `src/bgate_site/theme/_headers` sets
them). `.svg` is in `_IMAGE_SUFFIXES` (`app.py:384`) and returned via bare
`FileResponse`, and `/api/preview` needs no token (every GET is exempt by
`_SAFE_METHODS`, and it is also in `_OPEN_PATHS`).

The origin it is served from is the one whose HTML carries `window.BGATE_TOKEN` plus
a global `fetch` shim that attaches it to every same-origin request
(`app.py:704-712`). The repo's own rebinding comment at `api.py:470-484` traces where
that leads: scrape the token, reach `POST /api/godot/run`, arbitrary GDScript,
`OS.execute()`.

Reaching it through SVG is harder than that comment's path, and the honest version of
the severity depends on it: the UI only ever renders previews in `<img>`, where SVG
script does not execute, and `components/Markdown.tsx:37` admits only
`^https?://` links, so agent-written markdown cannot produce a clickable relative
link to one. It needs the operator to navigate to the preview URL directly, with an
attacker-controlled `.svg` already inside the project — a downloaded reference image,
an asset pack, a contributor's commit to the game repo. The Host gate keeps it
local-origin only. So: defence-in-depth, not a live hole — but a three-line fix on an
endpoint whose own origin is this product's crown jewel.

**Fix:** add `X-Content-Type-Options: nosniff` and a restrictive
`Content-Security-Policy` to the app's responses, and either drop `.svg` from
`_PREVIEW_SUFFIXES` or serve it with `Content-Disposition: attachment`.

### Minor

- **`src/bgate_adapters/animlib.py:231`** uses
  `str(target).startswith(str(root.resolve()))` for zip containment. Prefix matching
  admits a sibling whose name extends the root's (`…/animpacks/mixamo` vs
  `…/animpacks/mixamo-evil`). Not exploitable — the pack bytes are SHA-256-pinned to a
  reviewed archive — but it is the one place that does not use the `relative_to()`
  idiom `app.py:1024` gets right.
- **`packaging/floor-assets/pyproject.toml` declares no `license`.** A redistributable
  wheel built from an MIT repo with no license metadata of its own.
- **~24 MB of `.mp3` tracked in git** (`size-pack: 45.6 MiB`). Correctly excluded from
  the wheel via `exclude-package-data` and split into its own package, but every clone
  carries them forever. Worth a decision now rather than after the history grows.
- **`ruff` runs `E9,F` only** — a deliberate, documented choice. An extended pass
  (`B,SIM,PERF,C4,RUF`) surfaced no additional real bugs: all 7 `B023`
  loop-variable-capture hits are immediately-invoked closures and all 8 are false
  positives. The narrow rule set is not hiding anything.

---

## 3. Suggested Enhancements

Prioritized by security, then maintainability, then performance.

**1. Fix the broken `.gitignore` entry and settle the Codacy file.** *(security · 5 min)*
The backslash pattern means the next file dropped in `.github/instructions/` is also
public. Fix the path separator, and either remove the vendor instruction file or keep
it as a reviewed decision. Lowest effort of anything here.

**2. Add `npm` to `dependabot.yml` and an `npm audit` step to `security.yml`.** *(security · 20 min)*
The committed bundle ships to every user through the wheel. The Python tree is audited
twice and the JavaScript tree not at all; that asymmetry is the finding, and it closes
with about eight lines of YAML.

**3. Add the two response headers and close the inline-SVG path.** *(security · 30 min)*
`nosniff` plus a CSP on the dashboard's own responses, and `.svg` either dropped from
`_PREVIEW_SUFFIXES` or served `Content-Disposition: attachment`. Cheap
defence-in-depth around the origin that holds the dashboard token.

**4. Put the version-sync and floor-assets build checks in CI.** *(maintainability · 30 min)*
`python packaging/build_floor_assets.py` already exits 1 on drift and has been failing
for three releases because nothing calls it. A job that runs it — next to
`frontend-drift`, which exists for precisely this class of bug — turns a silent broken
release path into a red PR. This is the finding most likely to bite a real user.

**5. Break up `bgate_mcp/server.py` and retire the import cycle.** *(maintainability · substantial)*
Not a weekend task, and the cycle is documented rather than accidental — but 11,357
lines acting as a shared namespace, with boot correctness depending on definition
order relative to line 11021, is the repo's largest structural risk. The incremental
move is to lift the names the tool modules import (`_root`, `_tool`, `_work_item_id`,
and the rest of the shared plumbing) into a `bgate_mcp/_shared.py` that imports
nothing from `server`. Each tool module then imports from `_shared` instead, the
cycle's direction becomes one-way, the `sys.modules.setdefault` hack and the ten
`F403` suppressions can go, and `server.py` shrinks as a side effect rather than as a
goal. Doable one module at a time, with the existing suite as the check.

Alongside (5), **delete `audiohooks.py:181-183`** — three lines, removes a
per-character scan of every project source file from the workspace poll.

---

*No source, configuration or dependency was changed by this audit — the only
addition is this report. Every fix above is a recommendation awaiting your call.*
