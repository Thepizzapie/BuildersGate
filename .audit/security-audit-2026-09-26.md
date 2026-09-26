# Builders Gate — Public Repository Audit

**Date:** 2026-09-26 · **Commit:** `994f9a3` · **Branch:** `claude/confident-johnson-skm7di`
**Scope:** 1,303 tracked files — 177,067 LOC source (301 Python modules), 85,426 LOC tests
(308 modules), 101 TypeScript/TSX files, plus the complete git history of all branches.

---

## 1. Secrets & PII — CLEAN (critical section)

**No exposed secrets or personal information were found.** Stated explicitly, as requested.

### What was scanned

| Check | Coverage | Result |
|---|---|---|
| Credential formats in tracked files | all 1,303 files | clean |
| Credential formats across **full git history** | every blob, every commit, all branches | clean |
| Secret-bearing file types ever committed | `.env`, `*.pem`, `*.key`, `*.p12`, `id_rsa`, `credentials`, `secrets.*` | none — only `.env.example` |
| Email addresses / personal identifiers | all tracked content | clean |
| Internal hostnames, private IPs, non-public URLs | all tracked content | clean |
| `npm audit` (advisories) | `frontend/` lockfile tree only — see §2 | 0 vulnerabilities |

Patterns searched: OpenAI (`sk-`, `sk-proj-`), Anthropic (`sk-ant-api03-`), GitHub
(`ghp_`, `gho_`, `github_pat_`), AWS (`AKIA…`), Google (`AIza…`), Slack (`xox[baprs]-`),
Twitch (`oauth:…`), PEM private-key headers, and JWTs.

### Every match was a deliberate synthetic fixture

All 12 hits in tracked files, and all 9 distinct hits across history, are test data for the
repository's own redaction logic — identifiable by their padding and self-labelling:

```
tests/board/test_streamer.py      sk-ant-api03-AAAA…, ghp_AAAA…, AKIAIOSFODNN7EXAMPLE
tests/runtime/test_provider_keys.py   sk-test-DO-NOT-USE-000000000000abcd
tests/ui/test_api_contract.py         sk-live-NOTREAL-abcdefghijklmnop
tests/adapters/test_adapters_adjust.py  sk-do-not-leak-this-value
```

`AKIAIOSFODNN7EXAMPLE` is AWS's own published documentation placeholder. None is a live
credential.

### Apparent PII / internal infrastructure — all benign

- **`marijn@haverbeke.berlin`** — upstream author attribution inside the vendored CodeMirror
  `LICENSE`. Correct and required by the license.
- **`hunter2@github.com`**, **`someone@someone.tmi.twitch.tv`** — joke and protocol
  placeholders in redaction tests.
- **`wizard-small@2x.bmp`** — a filename that matched the email regex, not an address.
- **`169.254.169.254`** — the cloud metadata endpoint, appearing only as an SSRF **test
  fixture** asserting the loopback guard rejects it (`tests/adapters/test_web.py:420`).
- **`100.64.0.9`** — CGNAT-range address in tailnet remote-access tests.
- **`192.168.1.40`** — a ComfyUI URL fixture in `tests/runtime/test_localruntimes.py`.
- **`Thepizzapie`** — the repository owner's public GitHub handle, expected in a public repo.

### Controls that are working

- `.gitignore` correctly excludes `.env`, `.env.*` (with `!.env.example`), `.bgate/`,
  `.claude/`, and build output. `.env.example` ships **empty values only** — names, no keys.
- The repository contains a purpose-built **secret-scrubbing filter**
  (`src/bgate_core/board/streamer.py`) with real test coverage. It redacts known key formats
  *and* credentials embedded in URLs, username half included
  (`https://marta:hunter2@…` → scrubbed).
- Credential writes are deliberately **not** exposed as an MCP tool — documented in
  `CLAUDE.md` as a guard against an agent provisioning a provider nobody paid for.
- `bgate key set` prompts with echo off and accepts no key argument, so keys cannot land in
  shell history.

**Nothing requires rotation, and no history rewrite is needed.**

---

## 2. Code Quality

### Strengths worth recording

This is a well-maintained codebase. Several things are materially better than typical:

- **Zero** `TODO` / `FIXME` / `HACK` / `XXX` markers across all 1,303 files.
- **Effectively no dead code.** An exhaustive sweep for defined-but-uncalled helpers across
  the whole tree produced only the two minor findings below.
- **Dependency floors are treated as a security control**, documented as such in
  `pyproject.toml`, and `security.yml` audits *both* the resolved tree and the floors pinned
  exactly — because a floor-only vulnerability is invisible to a resolved-tree audit. That is
  a subtle failure mode most projects miss.
- **A genuine, documented threat model.** `src/bgate_ui/api.py` implements a non-optional Host
  gate specifically against DNS rebinding, with the full attack chain written out: rebind →
  same-origin illusion → scrape `window.BGATE_TOKEN` from `/` → `POST /api/godot/run` →
  `OS.execute()` → shell as the desktop user. The gate deliberately cannot be disabled by
  `BGATE_NO_AUTH`, unlike the other two checks.
- **CI verifies committed build output matches a fresh build** (`frontend-drift` job), closing
  the usual drift hole in a repo that commits `dist/`.
- pip-audit, CodeQL, Dependabot, a multi-OS test matrix, and Playwright e2e are all wired up.
- `tsconfig.json` is strict (`strict`, `noUnusedLocals`, `noUnusedParameters`,
  `noFallthroughCasesInSwitch`).

### Gaps

**a) The JavaScript/TypeScript surface has no static analysis or dependency monitoring in CI.**
The frontend is 101 TS/TSX files (React 19, Mantine 9, vite 6) that ship in both the dashboard
and the signed `.exe`. Yet:
- `.github/dependabot.yml` covers `pip` and `github-actions` — **no `npm` entry**.
- **No `npm audit`** anywhere in `.github/`.
- CodeQL runs `languages: python` only — **no `javascript-typescript`**.
- `npm run typecheck` exists in `package.json` but **is never invoked by any workflow**.
  `vite build` uses esbuild, which strips types without checking them.
- No ESLint config exists.

Verified during this audit: `npm audit` reports **0 vulnerabilities** and `tsc --noEmit`
**passes cleanly**. So this is a *monitoring* gap, not a live defect — but nothing would catch
a regression, and a committed `package-lock.json` means the tree is already pinned and ready
to audit.

**b) The Python lint ruleset is very narrow.** `pyproject.toml` sets
`[tool.ruff.lint] select = ["E9", "F"]` — syntax errors and pyflakes only. CI runs
`ruff check` and passes. Running broader rules over `src/` surfaces, among others:

| Rule | Count | Meaning |
|---|---|---|
| `B904` | 245 | `raise` inside `except` without `from` — destroys exception chains |
| `B905` | 38 | `zip()` without `strict=` — silently truncates on length mismatch |
| `B008` | 18 | mutable/function-call default argument |
| `B023` | 7 | closure captures loop variable — a classic late-binding bug |
| `S324` | 4 | insecure hash (`md5` at `godot.py:1211`) |

Note `line-length = 100` is configured but unenforced, since `E501` is not in `select`.

**c) 268 instances of `except Exception: pass`.** Concentrated in
`src/bgate_mcp/server.py` (42), `src/bgate_ui/agents/dispatch.py` (29),
`src/bgate_ui/app.py` (12), `src/bgate_ui/agents/followup.py` (10). Many are legitimate
best-effort cleanup, but at this density they make failures in the MCP and dispatch layers
silent and hard to diagnose from a user's bug report.

**d) 133 of 341 MCP tools (39%) have no test referencing them by name.** The largest clusters
are `blender_*` (30), `cinematic_*` (19), `storyboard_*` (11), `quest_*` (6), `web_*` (5).

Framing this accurately: registry-level coverage **does** exist — several tests assert over
`server.mcp.list_tools()`, so registration and per-module gating are verified, and
`tests/test_tool_ceiling.py` guards tool count. The `blender_*` group is also structurally
hard to test, since it needs real Blender (exercised by the nightly `-m slow` job). The gap is
**behavioural** coverage of individual tools, not registration.

**e) `src/bgate_mcp/server.py` is 10,717 lines** — nearly 3× the next-largest module
(`blender.py`, 6,458) and the densest cluster of swallowed exceptions. It is the main
maintainability risk in the tree.

**f) Vendored third-party JS has no version or provenance manifest.**
`frontend/public/vendor/` holds committed copies of three.js (687 KB), a Draco decoder
(512 KB), CodeMirror (170 KB) and Tabler. `LICENSE` files are present — license compliance is
correct — but there is no recorded version or upstream URL for any of them. Because they are
committed rather than npm dependencies, neither Dependabot nor `npm audit` would ever flag
them, even once (a) is fixed.

**g) Two minor dead-code items.**
- `src/bgate_ui/agents/dispatch.py:2982` — `_add_step` is defined and never called anywhere;
  the only reference is a prose comment in `phases.py:20`.
- `src/bgate_core/design/quests.py:91` — `_int_or_none` is byte-identical to the copy at
  `decisions.py:333` and unused in `quests.py`. The `decisions.py` copy carries the
  explanatory docstring; the `quests.py` copy is the stray.

### Dependencies — one CRITICAL finding

**The two web scaffolding templates carry 10 known vulnerabilities between them (2 critical,
2 high, 6 moderate), and they are copied into every user's new project.**

This is the most consequential finding in the audit and it was missed on the first pass. It
surfaced from a GitHub Dependabot notice on push, then was reproduced locally and attributed
exactly.

`src/templates/web/2d/package.json` and `src/templates/web/3d/package.json` pin `vite ^5.4.0`
and `vitest ^2.1.0` — a major version behind the `vite ^6.0.11` the project's own `frontend/`
uses. Auditing each template at the versions those carets **actually resolve to today**:

| Advisory | Severity | Package | Resolves to |
|---|---|---|---|
| [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9) — path traversal / arbitrary file read via `@vitest/mocker` redirect mock | **critical** | `vitest <=4.1.10` | `vitest 2.1.9` |
| [GHSA-67mh-4wv8-2f99](https://github.com/advisories/GHSA-67mh-4wv8-2f99) — any website can send requests to the dev server and read the response | moderate | `esbuild <=0.24.2` via `vite <=6.4.2` | `vite 5.4.21` |

Each template reports **1 critical, 1 high, 3 moderate**. Two templates gives **2 critical,
2 high, 6 moderate** — an exact match for the 10 alerts GitHub reports on the default branch,
which identifies these two files as the whole of that count.

Why it matters more than an ordinary advisory: `bgate init --engine web` **scaffolds these
files into the user's game directory**, so every new web project inherits both. The templates
also ship no lockfile, so nothing pins the resolution. Both advisories are dev-server and
test-runner issues rather than shipped-game issues — but a dev server that any web page can
read files through is exactly the exposure a game developer runs for hours a day.

This also contradicts the project's own stated principle. `pyproject.toml` says floors "are a
security control, not a formality" and must sit "at or above the release that fixed the last
known advisory." The Python floors honour that — see below. The template manifests do not, and
no CI job looks at them.

**Verified fix:** `vite ^7.0.0` + `vitest ^5.0.0` resolves to vite 7.3.6 / vitest 5.0.2 /
esbuild 0.28.2 and audits **0 vulnerabilities**. It is a breaking bump, so it needs the
template build and scaffolding tests run behind it — left as a recommendation rather than
applied, since that is a scope call.

### Dependencies — everything else is well managed

The Python side is genuinely careful and needs no action. `pip-audit` run against the floors
pinned exactly — reproducing what `security.yml` does — reports **no known vulnerabilities**
across all 11 direct pins:

```
mcp==1.28.1  fastapi==0.141.1  starlette==1.3.1  uvicorn==0.31.1  Pillow==12.3.0
numpy==1.26  openai==1.30  segno==1.6  pytest==9.0.3  httpx==0.27.1  ruff==0.12
```

`frontend/package-lock.json` is also clean (0 vulnerabilities via `npm ci` + `npm audit`),
precisely because it commits a lockfile pinning fixed versions. Dependabot runs weekly on pip
and github-actions, and `ruff` is capped at `<0.13` with a stated reason.

The distinction is the lesson: **the manifests with a lockfile are clean, the manifests without
one are not.**

---

## 3. Suggested Enhancements

Prioritized by security, then maintainability, then performance. #1 is new since the
first pass of this report and supersedes the earlier "dependencies need no action" reading.

### 1 · Bump the web templates off the vulnerable vite/vitest line — *security, do this first*
The only finding with a live critical advisory and a blast radius beyond this repository. In
both `src/templates/web/2d/package.json` and `src/templates/web/3d/package.json`:

```diff
-    "vite": "^5.4.0",
-    "vitest": "^2.1.0"
+    "vite": "^7.0.0",
+    "vitest": "^5.0.0"
```

Verified during this audit to resolve clean (vite 7.3.6 / vitest 5.0.2 / esbuild 0.28.2,
`npm audit` 0 vulnerabilities). It is a major bump across two majors, so run each template's
own `npm run build` (which is `tsc --noEmit && vite build`) and the scaffolding tests behind
it. Bumping `three ^0.169.0` is worth doing in the same pass for currency, though it carries no
advisory.

Then close the hole that let it go unnoticed, which is that **no CI job and no Dependabot
ecosystem looks at the template manifests at all**. Enhancement #2 covers `frontend/`; extend
the same treatment to `src/templates/web/2d` and `src/templates/web/3d` as their own Dependabot
directories. Consider also committing a lockfile per template, or applying the floors-are-a-
security-control rule from `pyproject.toml` to these manifests too — they are the one place in
the project where a dependency floor propagates onto someone else's machine.

### 2 · Extend CI coverage to the JavaScript/TypeScript surface — *security*
Four small changes, each independently landable, closing the one structural monitoring gap:

```yaml
# .github/dependabot.yml — add:
  - package-ecosystem: "npm"
    directory: "/frontend"
    schedule: { interval: "weekly" }
    labels: ["dependencies"]
```
```yaml
# .github/workflows/security.yml — CodeQL matrix:
  languages: [python, javascript-typescript]
# and in the audit job:
  - run: npm audit --audit-level=high
    working-directory: frontend
```
```yaml
# .github/workflows/ci.yml — in the lint job:
  - run: npm run typecheck
    working-directory: frontend
```

Both `npm audit` and `tsc --noEmit` pass today, so all four lock in a currently-green state at
zero risk of a red build. This protects code that ships inside a **code-signed executable** —
the highest-consequence artifact the project distributes.

### 3 · Widen the ruff ruleset, starting with `B904` and `B905` — *security + maintainability*
Add flake8-bugbear incrementally so CI stays green:

```toml
[tool.ruff.lint]
select = ["E9", "F", "B", "C4", "SIM"]
ignore = ["B904"]   # 245 sites; remove this line once burned down
```

`B905` (38 sites) and `B023` (7 sites) are the ones most likely hiding real bugs — a
`zip()` that silently truncates and a closure capturing a loop variable both fail quietly and
produce wrong output rather than a crash. Worth fixing before the rule lands. Also consider
adding `E501` so the configured `line-length = 100` is actually enforced.

### 4 · Replace `except Exception: pass` with debug logging in the MCP and dispatch layers — *maintainability*
Not a blanket sweep — target the top four files (93 of 268 sites). A one-line
`log.debug("...", exc_info=True)` in place of `pass` preserves today's
best-effort behaviour exactly while making failures diagnosable from a user's log. This is the
single change that would most improve the project's ability to act on bug reports, given that
`server.py` and `dispatch.py` are where user-visible failures surface.

### 5 · Record vendor provenance, and split `server.py` — *maintainability*
Two independent items:
- Add `frontend/public/vendor/VERSIONS.md` listing each library's upstream URL, version and
  retrieval date. Without it, no tooling and no human can answer "is our three.js affected by
  this advisory?" — 1.2 MB of committed third-party code is currently unversioned.
- Split `src/bgate_mcp/server.py` (10,717 lines) along the seams already present in the
  package — `tools_level.py`, `tools_cinematic.py` and `tools_reuse.py` show the intended
  pattern. This is a mechanical, test-guarded refactor: the registry tests over
  `list_tools()` plus `test_tool_ceiling.py` will catch any tool lost in the move.

### 6 · Add behavioural tests for the highest-value untested tool clusters — *maintainability*
Rather than chase all 133, target the ones that are cheap to test and costly to break.
`storyboard_*` (11) and `quest_*` (6) are pure store operations over SQLite with no external
dependency, so they need no mocking and would follow existing `tests/store/` patterns
directly. `cinematic_*` (19) is higher value but needs provider stubs. Defer `blender_*` (30)
— it genuinely needs the nightly real-Blender job.

Also worth a smoke test that every registered tool's schema is well-formed and its arguments
validate, which would give all 341 tools a coverage floor for a fraction of the effort of
testing each.

---

## Summary

| Section | Verdict |
|---|---|
| **Secrets & PII** | **Clean.** No secrets, credentials, private keys or PII in any tracked file or anywhere in git history. No rotation or history rewrite needed. |
| **Dependencies** | **One critical issue.** The two web scaffolding templates carry 10 advisories (2 critical, 2 high, 6 moderate) on `vite ^5.4.0` / `vitest ^2.1.0`, and `bgate init --engine web` copies them into every user's project. Python floors and `frontend/` are both clean. |
| **Code Quality** | **Strong.** No dead code of consequence, no debt markers, a real threat model, thorough Python CI. Main gaps: no CI or Dependabot coverage of any JS/TS manifest, a narrow ruff ruleset, 268 swallowed exceptions, and an oversized `server.py`. |
| **Enhancements** | 6 proposed. #1 (bump the templates) is the only one with a live critical advisory and the only one whose blast radius reaches users' own machines. |

The through-line across the dependency findings: **every manifest in this repository that
commits a lockfile is clean, and every manifest that does not is either vulnerable or
unwatched.** `frontend/` has a lockfile and audits clean; the Python floors are pinned,
documented and audited twice over; the two template manifests have neither a lockfile nor any
CI job pointed at them, and that is exactly where the 10 alerts are.

### Caveats on method
- Secret detection was pattern- and history-based. It cannot detect a credential with no
  recognizable format (e.g. a bare hex string with no prefix), though nothing suggests one.
- The template advisories were found only after a Dependabot notice appeared on push. The
  first pass of this report enumerated dependency *manifests* but audited only `pyproject.toml`
  and `frontend/`, and wrongly concluded dependencies needed no action. Corrected above. The
  reproduction is local and exact: per-template `npm audit` totals sum to GitHub's own count.
- Template audits reflect what the caret ranges resolve to on 2026-09-26. npm carets float, so
  a user who scaffolded earlier may have different versions — all within the vulnerable range.
- The broader ruff counts were produced with ruff 0.15.8, the version available here; the
  project pins `<0.13`, so exact counts may shift slightly.
- `npm audit` and `tsc --noEmit` were run against a fresh `npm ci` from the committed
  lockfile. `node_modules/` was removed afterwards; the working tree is unmodified.
- No source files were changed by this audit.
