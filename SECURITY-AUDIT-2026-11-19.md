# Builders Gate — public repository audit

**Date:** 2026-11-19 · **Commit:** `6e6bfa2` · branch `claude/confident-johnson-qv4306`
**Scope:** 1,267 tracked files, full 910-commit history, CI config, dependency trees, test suite.

> The clone arrived shallow (124 commits). I ran `git fetch --unshallow` first so the
> history scan below covers all 910 commits, not just the recent slice.

---

## 1. Secrets & PII — CRITICAL section

### No secrets found. Confirmed explicitly.

| Check | Result |
|---|---|
| Key prefixes (`sk-`, `sk-ant-`, `sk-proj-`, `AKIA`, `ghp_`/`gho_`/`github_pat_`, `AIza`, `xox*`, `glpat-`, `hf_`, `r8_`, `SG.`, `dop_v1_`, `npm_`, `pypi-`, PEM blocks, JWTs, `oauth:`) across all tracked files | 14 hits — **all synthetic test fixtures** (`AAAA…`, `NOTAREAL`, `DO-NOT-USE`, and AWS's own documented `AKIAIOSFODNN7EXAMPLE`) |
| Same patterns across **all 910 commits** of history | Nothing beyond those same fixtures |
| Sensitive paths ever committed (`.env`, `*.pem`, `*.key`, `*.p12`, `id_rsa`, `credentials.*`, `.npmrc`, `.pypirc`, `.netrc`, service-account JSON, `*.jks`, `.htpasswd`) | **None.** Only `.env.example`, and every value in it is empty |
| Secret-shaped assignments with literal values | 11 hits, all benign template placeholders (`__BGATE_PROMPT__`, `__PROJECT_SLUG__`, …) |
| Emails in tracked files | Vendored CodeMirror `LICENSE` (upstream author — legitimate) and test fixtures only |
| Private IPs / internal hostnames | `192.168.1.40`, `10.0.0.5` — both inside test files |
| Leaked local user paths | All synthetic (`marta`, `nina`, `robin`, `someone`, `x`) — fixtures for the path-scrubber feature |

`.gitignore` correctly covers `.env` and `.env.*` with a `!.env.example` exception, and both
`init` and `adopt` stamp that into new projects. The design decision to keep credentials out of
MCP tool reach (only the CLI and the Generators panel can write a key) is sound and worth keeping.

### PII — minor, inherent to git, no action strictly required

- **Commit authorship.** 837 commits use GitHub `noreply` addresses, but **7 commits carry a
  personal Gmail address** and **8 carry `adrian@Adrians-Mac-Studio.local`** (a personal machine
  hostname). A contributor's corporate address appears on 2. This is public and permanent.
  Going forward: `git config user.email <id>+<user>@users.noreply.github.com`. Rewriting published
  history is not worth it for this.
  Verify with: `git log --all --format='%an <%ae>' | sort | uniq -c | sort -rn`

- **Screenshots.** The 20 images in `docs/screenshots/` were captured with the project's *own*
  redaction feature switched off — the `streamer off` badge is visible in frame, and
  `docs/capture_screenshots.py` never enables it. I inspected them: they show real project
  content and a first name, but **no keys, no tokens, and no filesystem paths**. Capturing future
  shots with streamer mode on costs nothing and removes the category.

### Tracked but apparently not meant to be

- **`.github/instructions/codacy.instructions.md`** is committed even though `.gitignore:54` tries
  to exclude it. The rule is written with **Windows backslashes** —
  `.github\instructions\codacy.instructions.md` — and gitignore patterns only match forward
  slashes, so it has never matched anything (`git check-ignore` confirms: not ignored).
  Note that fixing the rule alone won't help: the file is already tracked and needs
  `git rm --cached`. It is leftover vendor config instructing any AI agent in the repo to run
  Codacy MCP tools after every edit.

---

## 2. Code Quality

### Baseline — this is a healthy codebase

Everything below was run, not assumed:

- `ruff check .` — **clean**
- `tsc --noEmit` (frontend) — **clean**
- `npm audit` (frontend) — **0 vulnerabilities**
- `pip-audit` on the resolved runtime tree — **clean**
- `pip-audit` on all declared dependency floors — **clean**
- Full suite: **7,328 passed, 95 skipped**, 318 slow deselected
- Coverage: **74% over 72,086 statements**
- **Dead code: 1 function out of 3,400** undecorated module-level definitions
  (`src/bgate_core/store/modules.py:545 effective_seat` — defined, documented, never called)
- **4 TODOs** across 1,267 files

The CI is more rigorous than most commercial repos: workflow-wide `permissions: contents: read`,
no `pull_request_target`, CodeQL, per-job timeouts, dependabot on both pip and actions, and a
pip-audit job that checks the resolved tree *and* the declared floors *and* the hash-pinned release
lock *and* the release extras' floors. The auth guard explicitly defends against DNS rebinding, and
the secret scrubber matches literal env values first, then prefix shapes, then a named-assignment
fallback, URL credentials, bearer headers and PEM blocks. Credit where due.

### Findings

#### 2.1 — Dashboard auth fails open when the token file cannot be written  *(important)*

`src/bgate_ui/api.py` (end of `install_guard`):

```python
try:
    expected = ensure_token(root_fn())
except Exception:
    expected = ""        # no project yet: first-run must still be reachable
if expected:             # <-- empty string skips the check entirely
    ...compare_digest...
return await call_next(request)
```

**Demonstrated, not theoretical.** With `.bgate/ui-token` made unwritable, an unauthenticated
`POST /api/queue` returns **200** and creates the item:

```
A: normal                     -> 401  (correct)
B: ensure_token raises IsADirectoryError
B: POST with NO token         -> 200  <-- failed open
```

Real triggers: disk full, read-only filesystem, permission denied on `.bgate/`, or `ui-token`
not being a regular file — on Windows, a file held open by antivirus or a sync agent.

Why it matters: `sec-fetch-site` and `origin` are **browser-only headers**, so for a local
non-browser caller both preceding gates pass vacuously and the token is the sole remaining
boundary. The mutating surface behind it includes agent dispatch with Bash access and
`POST /api/godot/run` (GDScript → `OS.execute` → a shell as the desktop user).

**Fix:** distinguish the two cases. `root_fn()` raising `HTTPException(503)` is the legitimate
"no project yet" path; an `OSError` from `ensure_token` is a failure to establish the credential
and should deny with 503 rather than wave the request through.

#### 2.2 — The one dependency floor the floor-auditor never sees  *(moderate)*

`pyproject.toml` declares `[build-system] requires = ["setuptools>=68"]`. `scripts/dep_floors.py`
reads only `[project].dependencies` and `[project].optional-dependencies`, so this floor is
invisible to the audit. Auditing it directly:

```
setuptools 68.0.0  PYSEC-2025-49        fix 78.1.1
setuptools 68.0.0  PYSEC-2026-1918      fix 70.0.0
setuptools 68.0.0  PYSEC-2026-3447      fix 83.0.0
setuptools 68.0.0  GHSA-xmj3-9gf7-h52w  fix 84.1.0
```

The same `>=68` floor appears in `packaging/floor-assets/pyproject.toml`. The shipped `.exe` is
unaffected — `packaging/build-requirements.lock` already pins `setuptools==84.2.1` — so the
exposure is building from source on a constrained resolver. This directly contradicts the
project's own policy comment ("FLOORS ARE A SECURITY CONTROL, NOT A FORMALITY"), which is
exactly why it's worth closing.

#### 2.3 — Third-party actions float on mutable tags, including the signing one  *(moderate)*

No action is SHA-pinned. Two are third-party:

- `azure/trusted-signing-action@v0` — **receives `AZURE_CLIENT_SECRET`** and the signing endpoint
- `softprops/action-gh-release@v2`

A major-version tag is re-pointable by its owner at any commit. Pin both to a full SHA; dependabot
already watches `github-actions` and will keep the pins current.

#### 2.4 — Release build holds `contents: write` while running PR code  *(moderate, already known)*

`release-exe.yml` gives the `build` job `contents: write` for one tag-gated attach step, while the
workflow also triggers on `pull_request`. The repo's own comment at lines 86–93 states the problem
*and* the fix ("Moving the attach step into its own `needs: [build]` job that downloads the
artifact would close it properly"). Fork tokens are downgraded by GitHub; the residual is branch
PRs from collaborators. Worth actually doing — the analysis is already written.

#### 2.5 — 30 Pillow calls scheduled for removal, against an uncapped floor  *(dated breakage)*

The suite emits, repeatedly:

```
DeprecationWarning: Image.Image.getdata is deprecated and will be removed in
Pillow 14 (2027-10-15). Use get_flattened_data instead.
```

There are **30 `.getdata()` call sites** in `src/` (`framegate.py`, `chroma.py`, `tilemask.py`,
`spritekit.py`, `roomqa.py`, `cinecut.py`, `blender.py`, `server.py`, …) and `pyproject.toml`
declares `Pillow>=13.0.0` with **no upper bound**. The day Pillow 14 ships, a fresh
`pip install builders-gate` resolves to it and the art pipeline breaks at runtime.

#### 2.6 — The coverage mass is concentrated in one file  *(maintainability)*

| Module | Statements | Uncovered | Coverage |
|---|---|---|---|
| `src/bgate_mcp/server.py` | 4,612 | **2,273** | 51% |
| `src/bgate_cli/main.py` | 1,181 | 607 | 49% |
| `src/bgate_mcp/tools_level.py` | 1,094 | 597 | 45% |
| `src/bgate_adapters/blender.py` | 1,080 | 540 | 50% |

`server.py` is also the largest file in the repo at **11,357 lines**. It is simultaneously the
biggest maintenance surface and the biggest untested mass. Several 0% modules are legitimately
hard to test (`window/webview2.py` needs Windows WebView2; `_blender_runner.py` runs inside
Blender) and are not the issue.

#### 2.7 — The frontend typecheck is defined but never runs in CI  *(gap)*

`frontend/package.json` defines `typecheck: tsc --noEmit`. CI runs `npm ci`, the build, and
`npm run e2e` against `frontend/`, and runs build + test + audit against the *generated* web
templates — but never `tsc --noEmit` on the dashboard's own **38,619 LOC** of TypeScript.
Vite/esbuild strips types without checking them, so a type error merges silently. It passes
today (I ran it); the point is that nothing would catch it tomorrow.

#### 2.8 — Floor-assets package has drifted and ships without a license  *(consistency)*

`packaging/floor-assets/pyproject.toml` is at **0.1.43** while the main package is at **0.1.46**,
despite that package's own README stating "The version tracks the main package's `pyproject.toml`;
bump both together." Nothing in CI enforces it — in contrast to the `guard` job that checks
tag/pyproject/CHANGELOG agreement. It also declares **no `license` field**, so the wheel publishes
with no license metadata while the root project is MIT.

#### 2.9 — One test encodes how the suite is launched  *(test quality)*

`tests/adapters/test_web.py:195` asserts the process image name starts with `"python"`. Launched
via the `pytest` console script the real name is `"pytest"` and it fails; under `python -m pytest`
it passes. The product code is correct — `_image_name()` returned the true name. This was the
single failure in my first run and vanished in the second; the assertion should accept either.

---

## 3. Suggested Enhancements — prioritized

**1. Close the auth fail-open.** (§2.1) Highest severity, smallest diff. Deny when the credential
can't be established; allow only the genuine no-project first-run case. Add a regression test
beside the existing ones in `tests/ui/test_auth_guard.py`, which already proves the happy paths.

**2. Extend floor auditing to `[build-system].requires`, and raise setuptools.** (§2.2) Teach
`scripts/dep_floors.py` to read `[build-system]`, bump both pyprojects to `setuptools>=84.1.0`.
This is the project's own stated policy applied to the one place it currently isn't — and the
tooling change means it can't regress.

**3. Harden the release pipeline.** (§2.3, §2.4) SHA-pin `azure/trusted-signing-action` and
`softprops/action-gh-release`, and move the release-attach step into its own `needs: [build]` job
so `contents: write` never coexists with PR code. The second half is already specified in the
workflow's own comments.

**4. Cap Pillow, then vectorize the pixel paths — one change, two wins.** (§2.5) Add `Pillow<14`
now, then migrate the 30 `.getdata()` sites. Most of them are hot loops worth rewriting rather
than translating: `framegate.pixel_diff_fraction` walks `list(a.getdata())` against
`list(b.getdata())` comparing tuples in Python (~1M iterations per 1024² frame pair, run per frame
across a sheet), and `palette_of` builds a dict over every pixel. **numpy is already a hard
dependency** (`numpy>=1.26`) but is imported by only 8 of 303 source modules. Replacing these with
array operations removes the deprecated API *and* the bottleneck in a single pass — expect one to
two orders of magnitude on the frame-QA path.

**5. Three cheap CI additions.** (§2.7, §2.8, §2.9) Add `npm run typecheck` to the browser-smoke
job; add a floor-assets version-sync assertion to `tests/packaging/`; relax the process-name
assertion. Together these are perhaps 15 lines and they close three ways the suite can be
green while something is wrong.

*Longer-horizon, not urgent:* `server.py` at 11,357 lines / 51% covered (§2.6) is the main
structural debt. The `tools_*.py` split already exists as a pattern — continuing to carve tool
families out of `server.py` along those seams would make the untested half reachable by tests
without a rewrite. Separately, ~30MB of generated MP3s in `packaging/floor-assets/` account for
most of the 111MB clone; since they already ship as a separate wheel, serving them from a release
artifact would cut clone time for every contributor.
