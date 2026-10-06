# Builders Gate — Public Repository Audit

**Date:** 2026-10-06 · **Commit:** `5610742` · **Scope:** full history (588 commits, unshallowed), 1,325 tracked files, 183k lines Python + 31k lines TypeScript

---

## 1. Secrets & PII (Critical)

### No secrets found — confirmed explicitly

Scanned all 588 commits (not just the working tree) for provider key prefixes
(`sk-`, `sk-ant-`, `ghp_`, `github_pat_`, `AKIA`, `AIza`, `xox[baprs]-`,
`glpat-`, `hf_`, `r8_`, `dop_v1_`), PEM private-key blocks, JWTs, bearer
tokens, URL-embedded credentials, non-empty `*_API_KEY=` assignments, and
high-entropy base64/hex strings.

**Every single match is a synthetic test fixture.** The complete set of
secret-shaped strings that have ever existed in this repository:

```
AIzaSyAAAA…  AKIAIOSFODNN7EXAMPLE  ghp_AAAA…  glpat-AAAA…  hf_AAAA…
sk-AAAA…  sk-ant-api03-AAAA…  sk-do-not-leak-this-value
sk-live-NOTREAL-abcdefghijklmnop  sk-live-NOTREAL-zyxwvutsrqponm
sk-proj-AAAA…  sk-test-DO-NOT-USE-000000000000abcd
xoxb-NOTAREALTOKEN-AAAA…  BEGIN RSA PRIVATE KEY (3-line stub)
```

All live in `tests/board/test_streamer.py`, `tests/runtime/test_provider_keys.py`,
`tests/ui/test_api_contract.py` and `tests/adapters/test_adapters_adjust.py`.
The only high-entropy strings outside tests are npm SRI `sha512-` integrity
hashes in `frontend/package-lock.json`.

Supporting controls are genuinely good:

- The only tracked env file is `.env.example`, and **every value in it is empty**.
- `.gitignore` covers `.env` and `.env.*` with an `!.env.example` exception.
- No `.pem`, `.key`, `.p12`, `id_rsa`, `.npmrc`, `.pypirc` or keystore has
  **ever** been committed.
- `src/bgate_core/board/streamer.py` is a purpose-built redaction layer
  (vendor-shape matching, literal matching against the live process env,
  URL credentials, PEM blocks, one-way/non-reversible) with a thorough test
  class behind it. The fixtures are even deliberately written to avoid
  tripping GitHub push protection — a nice detail.

### Finding P1 — Real personal data in commit metadata (medium)

File *contents* are clean, but commit authorship is not. Three author
identities in the history are real personal data rather than GitHub `noreply`
aliases, and they are permanently public via `git log` and the GitHub API:

- An **external contributor's corporate email address** (2 commits). This one
  is not the maintainer's to publish.
- The maintainer's **personal email address** (1 commit). The other 544 commits
  correctly use the GitHub `noreply` alias, so this is a single-machine slip.
- A **first name plus local machine hostname** (4 commits).

The specific addresses and commit SHAs are deliberately **not reproduced in
this file** — writing them here would turn metadata into grep-able,
search-indexable page text and make the exact problem being reported worse.
They were delivered to the maintainer privately.

The repo already shows awareness here — one commit is literally *"Tests use a
fictional name, not the account name"* — so this is a gap in the same effort,
not a blind spot. Metadata can only be changed by rewriting history, which
breaks every clone and fork; the pragmatic fix is to set `user.email` to the
`noreply` alias on every development machine going forward, and to ask the
external contributor whether they want their corporate address scrubbed.

### Finding P2 — Verify the historical key was rotated (medium)

`.github/workflows/security.yml` notes in its own comments that this repo has
previously had a credential committed. **No such credential exists anywhere in
the reachable history scanned here** — it was removed before the current
history, or rewritten out.

Removal from reachable history is not the same as being safe: a public repo can
retain unreachable objects, and forks keep independent copies. So the action is
not a search, it is a confirmation — verify that credential was rotated, and
verify **Settings → Code security → Secret scanning + Push protection** is
enabled. security.yml recommends exactly this and deliberately declines to add
a third-party scanning action in its place, which makes the repository setting
the only control covering it.

### Finding P3 — A broken `.gitignore` rule is publishing a file (low)

`.gitignore:54` reads:

```
.github\instructions\codacy.instructions.md
```

Backslashes are **escape characters** in gitignore syntax, not path separators.
The pattern matches nothing, `git check-ignore` confirms the file is not
ignored, and `.github/instructions/codacy.instructions.md` is tracked and
public. It is the only backslash pattern in the file.

The file isn't sensitive, but it is an auto-generated vendor agent-instruction
file scoped `applyTo: '**'` that tells any AI agent reading the repo *"YOU MUST
IMMEDIATELY run the codacy_cli_analyze tool… Failure to follow this rule is
considered a critical error."* That directly competes with `CLAUDE.md` for any
contributor using an agent. Someone intended to exclude it; the rule just never
worked. Fix: `git rm --cached` it and change the line to forward slashes.

---

## 2. Code Quality

The engineering discipline here is well above average, and that is worth
stating plainly before the gaps:

- `ruff check .` — **clean** on the repo's own E9+F config.
- **Zero** unused imports, unused variables or redefinitions across 183k lines
  (`F401/F811/F841/F823` all empty).
- Exactly **one** TODO/FIXME/HACK marker in the entire source tree.
- ~86k lines of tests across 315 files against ~183k lines of Python — close to
  a 1:1 ratio.
- Four CI workflows (`ci`, `nightly`, `security`, `release-exe`), Dependabot,
  CodeQL, `pip-audit` run against **both** the resolved tree and the declared
  floors, plus a `frontend-drift` job. The committed React bundle is currently
  in sync (dist and source last moved in the same commit, `2da7d1f`).
- Dependency floors in `pyproject.toml` are treated as a security control with
  the reasoning written down.
- The SQLite layer is strong: 74 indexes over 63 tables, WAL, `busy_timeout`,
  `foreign_keys = ON`, and forward-only migrations made transactional to fix a
  documented real-world wedging bug.

### Finding Q1 — Two critical RCE advisories in the scaffold templates shipped to users (critical)

**This is the most serious finding in the audit, and it is in the product
surface rather than the repo's own build.**

`bgate init --engine web` scaffolds a user's new game from
`src/templates/web/2d/package.json` or `src/templates/web/3d/package.json`.
Both pin `vite: ^5.4.0` and `vitest: ^2.1.0`, and **neither ships a
lockfile** — so every scaffolded project resolves fresh and inherits whatever
those ranges give. Today that is `vite@5.4.21` and `vitest@2.1.9`, and
`npm audit` on each template reports **6 vulnerabilities (2 critical, 1 high,
3 moderate)**:

| Severity | Package | Advisory |
|---|---|---|
| **CRITICAL** | `tinypool` ≤2.1.1 (via `vitest@2.1.9`) | Prototype pollution gadget in worker options → **RCE** — GHSA-5gmw-xhrv-c9v3 |
| **CRITICAL** | `tinypool` ≤2.1.1 (via `vitest@2.1.9`) | Prototype pollution gadget to RCE in `run()` options — GHSA-85c8-ppgw-ccpr |
| high/moderate | `esbuild` (via `vite` ≤6.4.2) | **Any website can send requests to the dev server and read the response** — GHSA-67mh-4wv8-2f99 |

The esbuild advisory deserves particular weight *in this product's context*:
Builders Gate exists to run a dev server for playtesting and actively
instructs the user to start one. "Any website you have open can read responses
from your dev server" is a materially worse property here than in a generic
web project.

The telling detail is that **the repo's own `frontend/` runs `vite@6.4.3` —
exactly one patch release above the vulnerable `≤6.4.2` range.** The toolchain
was moved past this advisory in one place and the templates were left behind.
There is no migration cost to fixing them either: a scaffold has no existing
code to break, and the project already demonstrably runs vite 6.

Current safe targets: `vite` ≥6.4.3 (latest 8.3.3), `vitest` ≥5.0.3 (which is
what pulls `tinypool` to 2.2.0), and `three` ^0.186 (pinned at `^0.169.0`,
resolving to 0.169.0 — no advisory, just 17 minor versions stale).

### Finding Q2 — Moderate vulnerability in the repo's own frontend (low)

`frontend/package-lock.json` carries one advisory:

```
source-map-js 1.2.1   HIGH (nominal)   GHSA-68fv-2mgg-jv7q
  event-loop denial of service via indexed source-map section offsets
  path: vite@6.4.3 -> postcss@8.5.26 -> source-map-js ^1.2.1
```

Real severity in context is well below the nominal rating — it is a build-time
dev dependency, not shipped to users, and the impact is a slow build rather
than a compromise. `postcss` requires `^1.2.1`, so the patched release
satisfies the existing range: `npm audit fix` resolves it with a
**lockfile-only bump, no breaking change.**

### Finding Q3 — The frontend is excluded from three CI controls (high)

Three independent gaps all land on JavaScript/TypeScript, and together they
are why Q1 went unnoticed:

1. **Dependency *updates*.** `.github/dependabot.yml` declares `pip` and
   `github-actions` only. There is **no `npm` ecosystem entry at all**, so
   neither `frontend/` nor either template directory has an automated update
   path. (Dependabot *alerts* are a separate, repo-wide feature and are
   working — GitHub currently reports 10 alerts on the default branch,
   2 critical. The alerts fired; nothing turned them into a pull request or a
   failing build.)
2. **CI gating.** `security.yml` runs `pip-audit` thoroughly — against both the
   resolved tree *and* the declared floors pinned exactly, which is better than
   most projects manage. **No equivalent `npm audit` step exists**, so no build
   has ever failed on a JS advisory.
3. **Static analysis.** CodeQL is configured `languages: python`. All 31,327
   lines of TypeScript across 105 files are unanalysed.

**Tests** compound it: those 105 source files are covered by a single
`frontend/e2e/critical.spec.ts` (153 lines, 4 tests) with no unit tests
anywhere. The Python side has ~86k lines of tests across 315 files.

### Finding Q4 — `src/bgate_mcp/server.py` is an 11,064-line module (medium)

Nearly 3× the next-largest file (`blender.py`, 6,458). It is the MCP tool
surface — the single most-edited file in an agent-driven project and the
hardest to review, merge or navigate. The `tools_level.py` split (2,477 lines)
shows the extraction pattern already exists; it just hasn't been applied to the
bulk.

### Finding Q5 — Repository weight (low)

`.git` is **170 MB** for a project whose working tree is ~70 MB.

- 45 MB of tracked binaries (257 `.mp3`/`.png`/`.glb`/`.wav` files), mostly
  `frontend/public/audio/floor/*.mp3` at 2.8–4.8 MB each.
- `assets/environment/oak_raw.glb` — **26.7 MB**, deleted in `a715b79`
  (*"local game output, not part of the public repo"*) but permanent in
  history, along with 12 other `assets/` files including a 3.3 MB zip.

Every contributor downloads all of it forever. Not urgent; relevant if clone
time becomes a complaint.

### Non-findings, checked and cleared

- `shell=True` at `src/bgate_ui/agents/claudeusage.py:142` executes the user's
  *own* pre-existing `statusLine` command from their `~/.claude/settings.json`.
  That is the same contract Claude Code itself uses. Not injectable.
- `exec()` in the Blender adapters compiles the tool's own generated kit
  scripts. By design.
- The auth model is deliberate: `BGATE_NO_AUTH` is honoured for scripted runs
  but the DNS-rebinding check explicitly is **not** disabled by it, and
  `desktop.py` refuses to start remote mode when the flag is set.
- 87 `ARG001` unused-argument hits are overwhelmingly callback/protocol
  signatures, not dead code. 9 `ERA001` commented-out-code blocks are cosmetic.

---

## 3. Suggested Enhancements

Prioritized by impact per unit of effort.

**1. Bump the scaffold templates off vite 5 / vitest 2 — security, ~15 minutes.**
This is the only item that affects *users* rather than contributors. In both
`src/templates/web/2d/package.json` and `src/templates/web/3d/package.json`,
move `vite` to `^8` (anything ≥6.4.3 clears the esbuild advisory), `vitest` to
`^5` (clears both `tinypool` criticals), and `three` to `^0.186`. A scaffold
has no existing code to break and the repo's own frontend already runs vite 6,
so the usual "major bump = migration project" objection does not apply here.
Consider also committing a lockfile with the templates, or having `init` run
`npm install` and commit the result — an unlocked scaffold silently inherits
whatever the range resolves to on the day the user runs it, which is exactly
how this drifted.

**2. Add the three `npm` directories to Dependabot and gate on `npm audit` — security, ~15 minutes.**
`.github/dependabot.yml` needs `package-ecosystem: "npm"` entries for
`/frontend`, `/src/templates/web/2d` and `/src/templates/web/3d`, plus an
`npm audit --audit-level=high` step in `security.yml` alongside the existing
`pip-audit` jobs. Alerts already fire today; what is missing is anything that
turns an alert into a pull request or a red build. Pair this with
`npm audit fix` in `frontend/` for the `source-map-js` lockfile bump. Doing
this is what stops item 1 from recurring, so it is worth doing in the same
sitting rather than after.

**3. Fix the broken ignore rule — hygiene, ~2 minutes.**
`git rm --cached .github/instructions/codacy.instructions.md` and rewrite
`.gitignore:54` with forward slashes. One line, trivially verifiable.

**4. Close the PII loop — privacy, ~15 minutes.**
Set `git config --global user.email` to the GitHub `noreply` alias on every
development machine. Confirm the credential referenced in P2 was rotated and
that secret scanning with push protection is enabled. Ask the external
contributor whether they want their corporate address kept in the history.

**5. Extend CodeQL to TypeScript, and start splitting `server.py` — maintainability, ongoing.**
Add `javascript-typescript` to the CodeQL matrix, using the **default** suite
for exactly the reason `security.yml` already documents about the 277-alert
`security-and-quality` run: a gate nobody can make green is a gate everyone
learns to ignore. Separately, extract tool groups from the 11k-line
`server.py` following the existing `tools_level.py` precedent, one group per
PR so review stays tractable, and add Vitest unit coverage for the UI's pure
logic (state reducers, path handling, formatting) where four e2e tests give
the least protection.

### Bottom line

**No secrets, in the working tree or anywhere in 588 commits of history** —
and the project has a purpose-built redaction layer and a documented security
posture behind that result. The Python engineering is genuinely strong: lint
clean, zero unused imports across 183k lines, one TODO, a 1:1 test ratio, and
a `pip-audit` setup that checks declared floors as well as resolved versions.

The real problem is that none of that rigour reaches JavaScript. The scaffold
templates handed to every `--engine web` user still pin `vite ^5.4.0` and
`vitest ^2.1.0`, carrying **two critical prototype-pollution-to-RCE advisories
and a dev-server read advisory** — while the repo's own frontend sits one patch
release above the vulnerable range, which is the clearest possible sign this
was an oversight rather than a decision. Dependabot has been reporting it;
nothing converted those alerts into a pull request or a failing build, because
`dependabot.yml` has no npm entry, `security.yml` has no `npm audit` step, and
CodeQL is python-only.

Items 1–3 are roughly half an hour and close everything that is actually
exploitable.
