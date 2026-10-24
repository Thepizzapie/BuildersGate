# Builders Gate — Security, Secrets & Code Quality Audit

**Date:** 2026-10-24 · **Commit:** `1622ef6` · **Branch:** `claude/confident-johnson-r9sc0d`
**Scope:** full git history (656 commits, unshallowed), 188k LOC src / 90k LOC tests, frontend lockfile, CI workflows.

---

## 1. Secrets & PII — CRITICAL SECTION: **CLEAN**

**No exposed secrets were found.** Stated explicitly, as requested.

Verification performed:

| Check | Method | Result |
|---|---|---|
| Credential patterns, working tree | 20+ vendor regexes (OpenAI, AWS, GitHub, Google, Slack, HF, GitLab, Anthropic, Stripe, DO, SendGrid, npm, PyPI) | 15 hits, **all synthetic fixtures** |
| Credential patterns, **full history** | every blob in all 656 commits (5,230 blobs) | 13 distinct values, **all synthetic** |
| `.env` ever committed | `git log --all --name-only` | **Never.** Only `.env.example`, all values blank |
| Private keys | PEM-block grep across all history | 1 hit: a 2-line fake (`MIIEow\nAAAA`) in a redaction test |
| Project-specific keys (KREA, KIE, RETRO_DIFFUSION, DEEPGRAM, STABILITY, TRIPO, MESHY) | value-bearing assignment grep, full history | 1 hit: `KIE_API_KEY=machine-wide` (test fixture) |
| PII (emails, names, home paths) | full tree | **None real** — see below |

The synthetic values are unmistakably placeholders: `sk-...AAAA`, `AKIAIOSFODNN7EXAMPLE`,
`sk-live-NOTREAL-...`, `sk-test-DO-NOT-USE-...`, `sk-do-not-leak-this-value`,
`xoxb-NOTAREALTOKEN-...`. They exist because the repo **tests its own redaction filter**.

### PII
The only three email addresses in the tree are benign:
- `marijn@haverbeke.berlin` — copyright line in the vendored CodeMirror `LICENSE`
- `someone@someone.tmi.twitch.tv` — Twitch IRC protocol format example
- `hunter2@github.com` — joke credential in a URL-redaction test

Absolute paths (`C:\Users\robin\…`, `C:\Users\me\…`) appear only inside docstrings as
illustrative placeholders. No contributor's real machine path is committed.

### Positive finding: the redaction filter is genuinely well-built
`src/bgate_core/board/streamer.py` layers 13 vendor shape patterns, PEM blocks, URL
credentials, `Bearer`/`Basic` headers, `name=value` assignment forms, JWTs — and, critically,
**exact-value matching against the live environment** (`_env_secrets()`). That last layer is
what makes the four art-provider keys safe even though none has a recognizable prefix shape.
The code documents the reasoning: *"a redactor with a per-vendor allowlist fails exactly when
a new integration is being demonstrated on stream."* This is the right design.

### One caveat I could not close
`.github/workflows/security.yml` candidly records that this repo **has previously committed an
API key**. That key is **not present anywhere in the 656 commits now reachable**, so it was
purged by a history rewrite. Two things I cannot verify from inside a clone:
- whether GitHub still serves the pre-rewrite objects (orphaned commits remain fetchable by SHA
  until GC, and forks keep their own copies permanently);
- whether **secret scanning + push protection** is enabled on the repository.

The workflow's own recommendation remains correct and unactioned-as-far-as-I-can-tell:
turn on **Settings → Code security → Secret scanning, with push protection**. It is free on
public repos, scans all history server-side, and blocks the push rather than reporting after
the fact. **If the historical key was ever live, treat it as compromised and rotate it** — a
rewrite removes it from `main`, not from forks, clones, or anyone's cache.

---

## 2. Files & patterns that should not be public

### 2.1 A `.gitignore` rule written with backslashes never fired
`.gitignore:44`

```
.github\instructions\codacy.instructions.md
```

Git paths are always forward-slashed, so this line matches nothing. Confirmed:

```
$ git check-ignore -v .github/instructions/codacy.instructions.md
--> not ignored by any rule
$ git ls-files --error-unmatch .github/instructions/codacy.instructions.md
.github/instructions/codacy.instructions.md     # TRACKED
```

The file is public despite clear intent to exclude it. It carries no secret, but it is
third-party agent-instruction content that issues imperative directives to **any** AI agent
working in this repo — *"YOU MUST IMMEDIATELY run the `codacy_cli_analyze` tool"*, *"Failure to
follow this rule is considered a critical error"*, plus a prompt to install a CLI. Shipping
unreviewed third-party instructions to every contributor's agent is worth a deliberate decision
rather than an ignore rule that silently failed.

**Fix:** `git rm --cached .github/instructions/codacy.instructions.md` and correct the rule to
`.github/instructions/codacy.instructions.md`.

### 2.2 Everything else checked out clean
- No internal hostnames or private URLs. Every external domain is a documented vendor API,
  a public doc site, or loopback.
- `169.254.169.254` appears once — inside the docstring of `webhook_target()`, explaining the
  SSRF it defends against. That is defensive code, not a leak.
- `.pulsiron/` is correctly scoped: panels and routines tracked, `feeds/` and two local panels ignored.
- `.gitignore` correctly covers `.env`, `.env.*` (with `!.env.example`), `.bgate/`, `.claude/`, keys and build output.

---

## 3. Code Quality

### Baseline: strong, and better than most repos this size

| Metric | Value |
|---|---|
| Test suite | **7,421 passed, 90 skipped, 0 failed** (356s) |
| Test code | 89,862 lines · 332 files · 6,308 test functions |
| Source | 187,908 lines (ratio ≈ 1 : 0.48 src:test) |
| `ruff check src tests` | **All checks passed** |
| `TODO`/`FIXME`/`HACK`/`XXX` in src | **1** |
| Declared `[project]` dependency floors | **0 known vulnerabilities** |
| Dead code | **None found** |

Two things worth noting. First, the inline commentary is unusually substantive — comments
record *why* a control exists and what broke without it (the "0.0.0.0 day" Host-gate bypass,
the DNS-rebinding chain through `POST /api/godot/run` to `OS.execute()`, the raw-TCP-relay hole
in `tailscale serve --tcp`). That is institutional memory, not noise.

Second, I initially flagged nine `src/bgate_ui/routes/*.py` modules as unreferenced. **They are
not dead** — `routes/__init__.py` auto-discovers them via `pkgutil.iter_modules`, and records
import failures in a queryable `FAILURES` list. False positive; retracted.

The security posture is deliberate and layered: Host allowlist ahead of every other gate
(non-bypassable, even under `BGATE_NO_AUTH`), per-project token at `0600`, `0.0.0.0` bind
reachable only via explicit `--remote` which hard-refuses when auth is disabled, SSRF
validation on the one outbound webhook, CodeQL narrowed to the default suite with the triage
reasoning written down.

### Issues found

**A. `[build-system].requires` floor carries 7 advisories, and the project's own floor audit cannot see it** — *highest-impact finding*

`pyproject.toml` opens with an emphatic, correct policy:

> `# FLOORS ARE A SECURITY CONTROL, NOT A FORMALITY.`

It is applied to `[project].dependencies` only. `[build-system]` declares `setuptools>=68`, and
that floor audits as:

```
setuptools 68.0.0  PYSEC-2025-49        fix: 78.1.1
setuptools 68.0.0  PYSEC-2026-1918      fix: 70.0.0
setuptools 68.0.0  PYSEC-2026-3447      fix: 83.0.0
setuptools 68.0.0  GHSA-xmj3-9gf7-h52w  fix: 84.1.0
(7 advisories total)
```

`scripts/dep_floors.py` reads `data["project"]` and nothing else, so `security.yml`'s
floor-audit step — the job built specifically to catch rotted floors — is structurally blind to
the build backend. This matters more than an ordinary dependency: installing from an sdist
**executes** the build backend on the user's machine.

**B. Frontend has 2 high-severity advisories, and CI never audits npm**

```
vite           <=6.4.3   high   5 advisories (incl. launch-editor arbitrary
                                command execution on Windows; Vite cross-origin
                                script execution; fs.allow path escape)
source-map-js  1.2.1     high   event-loop DoS via indexed source-map offsets
```

The Windows command-execution advisory deserves attention because `CLAUDE.md` names Windows the
supported platform, so it is where contributors actually run `npm run dev`.

Both are **devDependencies**, so end users of the published package are not exposed — the
committed `src/bgate_ui/static/dist/` is what ships. The exposure is to contributors and CI.

The fix is free: `vite` is pinned `^6.0.11` and **6.4.5 is published and in-range**, so
`npm audit fix` clears both with a lockfile refresh and no manifest change.

The systemic cause is that **nothing in the repo's automation looks at JavaScript**, from two
directions at once:

- `security.yml` audits Python thoroughly and JS not at all — CI runs `npm ci` in four places
  across `ci.yml` and `release-exe.yml`, and `npm audit` in zero.
- `.github/dependabot.yml` declares only the `pip` and `github-actions` ecosystems. There is no
  `npm` entry, and `frontend/package-lock.json` is not in a monitored directory.

That second point explains why these two sat unfixed while a fix was in range. GitHub's
*dependency graph* detects the lockfile and raises **alerts** regardless of `dependabot.yml` —
the push to this branch printed `GitHub found 6 vulnerabilities on Thepizzapie/BuildersGate's
default branch (2 high, 3 moderate, 1 low)`, and the 2 high are these. But `dependabot.yml`
governs **update PRs**, not alerts, so with no `npm` ecosystem declared, no automated bump is
ever opened for the frontend. The alert fires into the Security tab and nothing carries it
forward. Hence vite pinned at 6.4.3 with 6.4.5 published and `^6.0.11` happily allowing it.

The remaining 3 moderate and 1 low alerts are visible in the Security tab but not from inside a
clone — `pip-audit` reports the `[project]` floors and the resolved tree clean, and `npm audit`
reports only the 2 high, so they are most likely the `setuptools` build floor in **A** (which
Dependabot reads from `pyproject.toml` and the repo's own floor audit cannot see) plus
`github-actions` pins. Worth reading at
`https://github.com/Thepizzapie/BuildersGate/security/dependabot` to close the loop.

**C. `Pillow>=13.0.0` is uncapped while 31 source call sites use an API Pillow 14 removes**

The only deprecation class in the entire 7,421-test run:

```
DeprecationWarning: Image.Image.getdata is deprecated and will be
removed in Pillow 14 (2027-10-15). Use get_flattened_data instead.
```

`getdata()` appears at **31 call sites in `src/`** (and 19 in tests), concentrated in the art
pipeline — `art/spritekit.py`, `art/chroma.py`, `art/framegate.py`, `art/tilemask.py`,
`art/artdirection.py`, `adapters/sprites.py`, `adapters/blender.py`, `level/roomqa.py`. With no
upper cap, the day Pillow 14 ships `pip install builders-gate` resolves to it and the art
pipeline breaks at runtime. The removal has a published date, so this is a scheduled break.

**D. The `numpy` floor is uninstallable on Python ≥ 3.13**

`numpy>=1.26` → `dep_floors.py` emits `numpy==1.26`, which under PEP 440 matches only `1.26.0`,
whose metadata is `Requires-Python >=3.9,<3.13`. The floor-audit step therefore cannot resolve
on 3.13+:

```
ERROR: Could not find a version that satisfies the requirement numpy==1.26
```

CI is green today only because `security.yml` pins `python-version: "3.12"`. Meanwhile
`requires-python = ">=3.11"` is unbounded but CI tests only 3.11 and 3.12 — so the declared
support range is wider than what is verified, and this floor silently prevents the audit job
from ever moving forward. End users are unaffected (an unconstrained resolver picks numpy 2.x).

**E. `src/bgate_mcp/server.py` is 11,246 lines**

294 top-level definitions and **207 MCP tool registrations** in a single module — by far the
largest file in the repo (next is `blender.py` at 6,466). Every tool change contends in one
file, and the surface is too large to review as a unit.

The fix is already proven in this codebase: `bgate_ui/routes/` auto-discovers router modules via
`pkgutil` and reports per-module import failures. The same pattern applied to
`bgate_mcp/tools_*.py` would let tools live beside their domain. Partial precedent exists —
`tools_level.py` is already split out.

**F. `mcp>=1.30.0,<2` now excludes an entire major line**

`mcp` 2.3.0 is published; the `<2` cap holds users on 1.x. Caps age into staleness quietly —
worth a deliberate re-evaluation rather than drift.

---

## 4. Suggested Enhancements — prioritized

### 1. Extend the floor policy to `[build-system]`, and enable secret scanning *(security)*
Two small changes, both closing gaps in controls that already exist:

- Raise `requires = ["setuptools>=68"]` to `>=84.1.0` (or `>=78.1.1` at minimum), and teach
  `scripts/dep_floors.py` to read `data["build-system"]["requires"]` alongside `data["project"]`.
  Three lines, and it makes the floor audit honest about code that executes at install time.
- Turn on **secret scanning with push protection** (Settings → Code security) — `security.yml`
  already argues for exactly this. And if the historical key was ever live, rotate it: a history
  rewrite does not reach forks or clones.

### 2. Add `npm audit` to `security.yml` *(security)*
The Python side is rigorous; the JS side is unscanned, which is why two high-severity advisories
are sitting in the committed lockfile. Run `npm audit fix` now (vite 6.4.5 is in-range, so this
is a lockfile-only change), then add the standing check beside the pip-audit job:

```yaml
  npm:
    name: npm audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "20", cache: npm, cache-dependency-path: frontend/package-lock.json }
      - run: npm audit --audit-level=high
        working-directory: frontend
```

`--audit-level=high` matches the repo's existing stance against gates nobody can make green.

Then add the missing ecosystem to `.github/dependabot.yml`, so a fix PR is raised rather than an
alert nobody is paged for:

```yaml
  - package-ecosystem: "npm"
    directory: "/frontend"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 3
    labels: ["dependencies"]
```

### 3. Cap Pillow and migrate off `getdata()` *(maintainability — dated deadline)*
Add `Pillow>=13.0.0,<14` today as the cheap stopgap, then migrate the 31 source call sites to
`get_flattened_data()`. Mechanical, well covered by existing tests, and it has a hard date
(2027-10-15). Capping without migrating just defers it — but capping first means the deadline
cannot land as a surprise breakage in users' installs.

### 4. Split `bgate_mcp/server.py` by domain *(maintainability)*
207 tool registrations in one 11k-line module is the single largest structural drag on the
codebase. Reuse the `bgate_ui/routes/` auto-discovery pattern — it already solves this exact
problem in-repo, including the "a module failed to import and nothing said so" failure mode that
`routes/__init__.py` documents. Incremental: move one domain at a time, `tools_level.py` shows
the shape.

### 5. Make the declared Python range match the tested one *(maintainability)*
Either test 3.13 in the CI matrix or declare `requires-python = ">=3.11,<3.13"`. While there,
fix the `numpy` floor to a resolvable three-component version that supports the whole declared
range (`numpy>=1.26.2`) so the floor-audit job is not permanently frozen at Python 3.12.

---

## Bottom line

**No secrets, no PII, no credentials — verified across the complete 656-commit history, not just
the working tree.** The test suite is green at 7,421 passing, the linter is clean, there is one
TODO in 188k lines, and no dead code. The security engineering is deliberate and documented to a
standard well above the norm.

The findings are gaps at the edges of controls that already exist and work: the floor policy
stops at `[project]`, the dependency audit stops at Python, the Pillow pin stops at a lower
bound, and the CI matrix stops below the version range the package advertises. The two
high-severity npm advisories are the only items with a live fix available right now, and that
fix is a lockfile refresh.

The through-line is narrower than "add more scanning": the Python supply chain is watched from
two directions and the JavaScript one from neither, and the one floor that executes at install
time is the one the floor audit does not read.
