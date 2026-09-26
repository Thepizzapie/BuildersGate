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
| `npm audit` (dependency advisories) | resolved lockfile tree | 0 vulnerabilities |

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

### Dependencies

Actively and carefully managed — no action needed. Floors are current and documented,
`ruff` is capped at `<0.13` with a stated reason, Dependabot runs weekly on pip and actions,
`npm audit` is clean, and `mcp`/`fastapi`/`starlette`/`Pillow` floors sit at or above their
last advisory fixes.

---

## 3. Suggested Enhancements

Prioritized by security, then maintainability, then performance.

### 1 · Extend CI coverage to the JavaScript/TypeScript surface — *security, highest value*
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

### 2 · Widen the ruff ruleset, starting with `B904` and `B905` — *security + maintainability*
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

### 3 · Replace `except Exception: pass` with debug logging in the MCP and dispatch layers — *maintainability*
Not a blanket sweep — target the top four files (93 of 268 sites). A one-line
`log.debug("...", exc_info=True)` in place of `pass` preserves today's
best-effort behaviour exactly while making failures diagnosable from a user's log. This is the
single change that would most improve the project's ability to act on bug reports, given that
`server.py` and `dispatch.py` are where user-visible failures surface.

### 4 · Record vendor provenance, and split `server.py` — *maintainability*
Two independent items:
- Add `frontend/public/vendor/VERSIONS.md` listing each library's upstream URL, version and
  retrieval date. Without it, no tooling and no human can answer "is our three.js affected by
  this advisory?" — 1.2 MB of committed third-party code is currently unversioned.
- Split `src/bgate_mcp/server.py` (10,717 lines) along the seams already present in the
  package — `tools_level.py`, `tools_cinematic.py` and `tools_reuse.py` show the intended
  pattern. This is a mechanical, test-guarded refactor: the registry tests over
  `list_tools()` plus `test_tool_ceiling.py` will catch any tool lost in the move.

### 5 · Add behavioural tests for the highest-value untested tool clusters — *maintainability*
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
| **Code Quality** | **Strong.** No dead code of consequence, no debt markers, a real threat model, thorough Python CI. Main gaps: the JS/TS surface is unmonitored in CI, the ruff ruleset is narrow, 268 swallowed exceptions, and `server.py` is oversized. |
| **Enhancements** | 5 proposed. #1 (wire npm/CodeQL/typecheck into CI) is the highest-value and lowest-risk — both checks pass today. |

### Caveats on method
- Secret detection was pattern- and history-based. It cannot detect a credential with no
  recognizable format (e.g. a bare hex string with no prefix), though nothing suggests one.
- The broader ruff counts were produced with ruff 0.15.8, the version available here; the
  project pins `<0.13`, so exact counts may shift slightly.
- `npm audit` and `tsc --noEmit` were run against a fresh `npm ci` from the committed
  lockfile. `node_modules/` was removed afterwards; the working tree is unmodified.
- No source files were changed by this audit.
