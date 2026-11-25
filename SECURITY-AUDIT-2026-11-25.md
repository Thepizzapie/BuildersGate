# Builders Gate — repository audit

**Date:** 2026-11-25 · **Repo:** `Thepizzapie/BuildersGate` (public) · **Ref audited:** `claude/confident-johnson-qkuvcy` @ `6e6bfa2`
**Scope:** 1,267 tracked files; **912 commits** of full history (the session clone arrived shallow at 124 commits — unshallowed before scanning, see *Method*).

---

## 1. Secrets & PII — critical

### No exposed secrets. Confirmed.

Scanned the working tree **and the complete 912-commit history** for:

| Checked | Result |
|---|---|
| Provider key shapes (`sk-`, `sk-proj-`, `sk-ant-`, `sk_live_`, `ghp_`, `gho_`, `github_pat_`, `AKIA`, `AIza`, `xox[baprs]-`, `glpat-`, `r8_`, `hf_`, `dop_v1_`, `SG.`) | **0 real hits** |
| PEM private-key blocks, JWTs | **0 real hits** |
| `VAR=<value>` credential assignments | **0 real hits** |
| Files ever added matching `.env` / `*.pem` / `*.key` / `*.p12` / `id_rsa*` / `*credential*` / `.npmrc` / `.pypirc` / `*secret*` | **only `.env.example`**, which holds empty keys |

Every pattern match was a deliberate test fixture (`sk-ant-api03-AAAA…`, `AKIAIOSFODNN7EXAMPLE`, `sk-not-a-real-key`, `hunter2@github.com`) or documentation. The `.gitignore` correctly covers `.env` and `.env.*` with a `!.env.example` exception, and `~/.bgate/.env` lives outside any repository.

Worth saying plainly: the defences here are better than most. `src/bgate_core/board/streamer.py` is a purpose-built redactor for ~10 provider key shapes plus PEM blocks, usernames, hostnames and home paths, with real test coverage (`tests/board/test_streamer.py`). `bgate key set` takes no key argument and prompts with echo off. Key-writing is deliberately not an MCP tool.

### PII: personal email and machine name in commit metadata — 8 commits on `main`

This is the one genuine PII finding, and it is permanently public.

1,608 of 1,636 author/committer entries use the `@users.noreply.github.com` privacy alias. **25 do not:**

| Identity | Commits | On `main`? |
|---|---|---|
| `<owner-personal-gmail>` (personal address) | 14 | **yes — 4** (`4051fc7`, `c15a861`, `bf45995`, `e71eeed`) |
| `<owner>@<personal-machine>.local` (personal machine name) | 8 | **yes — 4** (`c1a5934`, `c866a2a`, `0f42ffa`, `8c60cb0`) |
| `<contributor>@<employer-domain>` (a contributor's corporate address) | 3 | no — PR/branch refs only |

> The three addresses are redacted **in this committed copy only** — it is a new, searchable file in a public repo, and republishing a personal address verbatim to fix a personal-address leak would be self-defeating. The commit SHAs identify every affected commit, and `git log --pretty='%ae' <sha>` gives the exact value.

The near-total use of the alias everywhere else shows the intent; these look like a local `git config` on one machine that was never set to match. Notably, commit `0f42ffa` is literally *"Tests use a fictional name, not the account name"* — this exact class of cleanup is already part of the project's own standard.

**This is not fixable without rewriting published history**, which would invalidate every existing clone and fork for a low-severity leak. The proportionate fix is forward-looking:

```bash
git config --global user.email "<id>+Thepizzapie@users.noreply.github.com"
```

on every machine, plus GitHub → Settings → Emails → **"Keep my email addresses private"** and **"Block command line pushes that expose my email"**, which makes the mistake unrepeatable. The `.local` hostname also quietly discloses a macOS workstation, which is mildly ironic given `CLAUDE.md` lists macOS as untested.

### Should-not-be-public: a tracked file whose ignore rule silently does nothing

`.github/instructions/codacy.instructions.md` is **tracked**, despite `.gitignore:60` attempting to exclude it:

```
.github\instructions\codacy.instructions.md
```

Git treats `\` as an escape character, never as a path separator, so this pattern matches no file on any platform. Verified: `git check-ignore -v` returns exit 1, and `git ls-files` lists the file.

The content is a repo-wide AI-agent directive (`applyTo: '**'`) instructing any agent working in the repo to run Codacy MCP tools after every edit, with "Failure to follow this rule is considered a critical error." It is not malicious — it asks consent before installing anything — but it is a third-party vendor's behavioural override shipped to everyone who clones a public repo, it conflicts with the project's own `CLAUDE.md`, and its YAML front matter is malformed (doubled `---`, indented keys). Someone clearly meant to exclude it.

**Fix:** forward slashes, and untrack it.

```bash
sed -i 's#^\.github\\instructions\\codacy\.instructions\.md$#.github/instructions/codacy.instructions.md#' .gitignore
git rm --cached .github/instructions/codacy.instructions.md
```

### GitHub secret scanning: could not verify, and the repo's own workflow says to turn it on

`.github/workflows/security.yml` deliberately omits a third-party secret scanner, with this reasoning:

> *"GitHub's own secret scanning with push protection is free on public repos, scans the whole history, and refuses the push instead of telling you about the key after it is already on the internet… it is the better answer for the failure this repo has already had once (a committed API key). Turn it on under Settings → Code security."*

That is the right call — but this session's token cannot read `/secret-scanning/alerts`, `/code-scanning/alerts` or `/dependabot/alerts` (403), so **I cannot confirm it was ever enabled.** Given the workflow records a prior committed-key incident, this is the single highest-value item on the list. My full-history scan found nothing, so if that key was in a pre-`2026-10-11` history it is gone — but it should be treated as rotated regardless, since dangling objects can survive on GitHub via old PR refs.

---

## 2. Code Quality

Context for the findings below: this is a **well-defended codebase**, and several things I went looking for are already handled correctly. Stating that so the list below is read at the right weight:

- **SQL injection — not present.** All 63 `ruff S608` flags interpolate only code-side column names and bind every value as `?`. The two dynamic-column builders that take caller keys (`queue.set_run_fields`, `playtest.update_item`) both gate them through explicit allow-lists (`_RUN_FIELDS`, `_ITEM_FIELDS`) that *raise* on anything else.
- **Path traversal — not present.** `deps.safe_under()` resolves then `relative_to()`-checks, with suffix allow-lists; every file-serving route (`/api/preview`, `/play/{path}`, `/api/model3d/raw`, `/api/cutout/file`, `/api/lifecycle/proof-image`) is guarded.
- **SSRF — handled.** `agents/followup.webhook_target()` is HTTPS-only and requires every resolved address to be public; the residual DNS-rebind window is documented rather than hidden.
- **Auth — unusually thoughtful.** The Host gate in `api.install_guard` is DNS-rebinding-aware and is **not** bypassable by `BGATE_NO_AUTH`, with the reasoning written out at the call site.
- `ruff check .` passes clean. **7,327 tests pass** (1 failure, below; 95 skipped).

### Findings

**a) 3.8 MB of vendored third-party JS that no scanner can see.** `frontend/public/vendor/` carries **three.js r169** (© 2010-2024 — roughly two years stale) and **CodeMirror 5.65.16** (the legacy 5.x line; 6.x is current), plus tabler-icons. None appear in `package.json`, so `npm audit` (which reports *0 vulnerabilities*), Dependabot, and `security.yml`'s own `npm-frontend` job are **all blind to them** — even though the dashboard serves them to the browser. This is the one real hole in an otherwise exemplary dependency posture: the workflow audits the resolved tree, the declared floors, the release lock *and* the Windows desktop extra, and then 3.8 MB of unmanaged browser code walks straight past all four.

**b) Pillow `getdata()` has a dated removal and the floor has no ceiling.** 30 call sites across 11 modules (`art/vfx.py`, `art/chroma.py`, `level/roomqa.py`, `adapters/blender.py`, `adapters/sprites.py`, `cine/cinecut.py`, `bgate_mcp/server.py`, …). It is the **only** deprecation class the suite emits (40 warnings), and the message is specific: *"removed in Pillow 14 (2027-10-15)"*. With `Pillow>=13.0.0` and no upper cap, the day Pillow 14 ships a fresh `pip install builders-gate` breaks the art, sprite, cinematic and level-QA paths. Mechanical fix: `get_flattened_data()`.

**c) `mcp>=1.30.0,<2` caps off the entire 2.x line.** PyPI's current `mcp` is **2.3.0**. This is the project's core protocol dependency — the thing the product *is* — held a full major version behind. The cap is defensible as deliberate, but unlike the `ruff<0.13` cap (which carries a written rationale) this one has none, so it reads as drift rather than a decision.

**d) `npm run typecheck` exists but no CI job runs it.** 31,826 lines of TS/TSX are ungated. I ran `tsc --noEmit`: it **passes today**, so this is prevention, not a live break — and the `frontend-drift` job already proves the committed bundle matches source, which is the harder half. One more step in that job closes it.

**e) Error swallowing at scale.** 1,220 `except Exception` and 302 `try/except/pass` in `src/`. Many are deliberate and commented (`# best effort`, `# pre-0043 database`), and the `routes/` registry notably turned exactly this failure mode into a queryable fact (`/api/routes/status`, `BGATE_STRICT_ROUTES=1`) — the right instinct. But at this density, a real failure has many quiet places to land, and it is the dominant debuggability risk in the codebase.

**f) A stale comment contradicts the code it guards, in a security-relevant spot.** `src/bgate_ui/app.py:2366`:

> `# Opt-in remote mode binds the tailnet IP so a phone can reach it over Tailscale — never 0.0.0.0, and it refuses rather than falling back.`

Line **2394** binds `0.0.0.0`. The change is correct and well-reasoned (binding the single tailnet IP dropped the phone's packets; Host allow-list plus per-project token still gate every request) and it *is* explained — twenty lines below the comment that flatly denies it. A reader checking the bind posture reads the first comment and stops.

**g) One test fails on Linux for an environmental reason.** `tests/adapters/test_web.py::TestDevServerBookkeeping::test_our_own_process_reads_as_ours_by_image_name` asserts the process image name starts with `python`; launched via the `pytest` console script it reads `pytest` and fails. It passes under `python -m pytest`. CI marks Linux advisory so this is invisible there, but the assertion is testing the launcher, not the code.

**h) Minor:** `src/bgate_mcp/server.py` is 11,357 lines — 1.7× the next largest module. `.git` is 170 MiB (committed `dist/` bundle, 137 PNGs, 12 MP3s, vendored JS, plus 26 `assets/` files untracked in `8c60cb0` but still in history), which is a slow first clone for contributors.

### Test coverage

Genuinely strong, and my first-pass heuristic was wrong about it: 337 test files, and `tests/ui/test_routes_smoke.py` enumerates `app.routes` dynamically rather than hardcoding paths, so auto-discovered route modules are covered the night they are added — including its own fix for Starlette 1.0 silently shrinking the walk from 159 routes to 39. The thinnest spots are `routes/agent_fleet.py` (3 routes, no named assertions beyond the smoke pass), `routes/audio_hooks.py` and `routes/ref_sources.py`.

---

## 3. Suggested Enhancements

Prioritized: security, then maintainability, then performance.

### 1. Enable GitHub secret scanning + push protection — *security, ~2 minutes*
Settings → Code security. The repo's own `security.yml` argues for this in writing and records a prior committed-key incident; it scans the whole history and **refuses the push** instead of reporting the key after publication. Nothing else on this list buys as much for as little. While there: confirm that key is rotated regardless of my clean history scan.

### 2. Close the two hygiene gaps that are already intended — *security, ~10 minutes*
Fix the backslash `.gitignore` line and `git rm --cached` the Codacy instructions file (§1). Set the noreply alias in global `git config` on all machines and enable *"Block command line pushes that expose my email"* (§1). Both are cases where the intent exists and only the mechanism failed.

### 3. Bring `frontend/public/vendor/` under dependency management — *security / supply chain, ~half a day*
Today 3.8 MB of browser-executed third-party code is audited by nothing. Cheapest credible fix: declare `three@0.169.0`, `codemirror@5.65.19` and `@tabler/icons` in `frontend/package.json` as real dependencies, let `vite` emit them into the bundle, and delete the committed copies. `npm audit`, Dependabot and the existing `npm-frontend` job then cover them for free, and `frontend-drift` keeps the committed bundle honest. If vendoring must stay (the loaders are fetched as separate URLs by `GLTFLoader`, which is a real constraint), then at minimum record each package and version in a manifest file and add a job that diffs it against the upstream advisory feed — but pulling them into the bundle is the better answer. Also plan the CodeMirror 5 → 6 move: the 5.x line is maintenance-only.

### 4. Cap `Pillow<14` now, migrate `getdata()` over time — *maintainability, ~1 hour then ~half a day*
One-line change in `pyproject.toml` buys until 2027-10-15 and keeps `pip install` from breaking under a user with no idea why their art pipeline died. Then replace the 30 `getdata()` calls with `get_flattened_data()`. Treat the `mcp<2` cap the same way: either write down why 2.x is excluded, or schedule the bump — an undocumented cap on the core protocol dependency becomes indistinguishable from neglect.

### 5. Add two cheap gates: `typecheck` and the process-name fix — *maintainability, ~30 minutes*
Append to the existing `frontend-drift` job (deps are already installed, so this is nearly free):

```yaml
      - name: Typecheck
        working-directory: frontend
        run: npm run typecheck
```

And make the Linux test honest by accepting either launcher:

```python
assert name.startswith(("python", "pytest")), name
```

### Also worth considering (lower priority)
- **Performance:** `bgate_core/qa/feedback.py` already has one fixed ReDoS (41 `m`s → 26 s, triaged out of the first CodeQL pass). Given that `is_noise` runs on every whisper segment, a `pytest`-level timing assertion on pathological input would stop the regression from returning. No other hot-path bottleneck surfaced in this pass.
- **Maintainability:** `src/bgate_mcp/server.py` at 11,357 lines is the obvious split candidate, and the `tools_*.py` modules show the pattern already works.
- **Observability:** the `routes/status` approach — turning a silent failure into a queryable fact — is the right answer for the 302 `try/except/pass` sites too, at least the ones on write paths.

---

## Method & limitations

- Full history scanned **only after `git fetch --unshallow`**: the session clone arrived shallow (124 of 912 commits). Any audit of this repo from a cloud session that skips that step is scanning a seventh of the history while appearing complete.
- `ruff` was run at the project's pinned `0.12.12`, not the `0.16.9` that happened to be ahead of it on `PATH`.
- Test suite: `pytest -m "not slow" -q` → 7,327 passed, 1 failed, 95 skipped, 318 deselected (332 s). `-m slow` tests need real Blender/whisper and were not run.
- **Could not verify:** GitHub secret-scanning, code-scanning and Dependabot alert state — all three endpoints return 403 for this session's token. Findings about them are about configuration, not about alert contents.
- Not covered: runtime/DAST behaviour of the dashboard, the Godot/Blender/Unity adapter paths against real binaries, and the `.exe` packaging chain.
