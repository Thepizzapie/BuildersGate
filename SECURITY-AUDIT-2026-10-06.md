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

### Finding Q1 — The frontend is the repo's systematic blind spot (high)

Three independent gaps all land on the same 31,327 lines of TypeScript/React:

1. **Tests.** 105 source files are covered by a single `frontend/e2e/critical.spec.ts`
   (153 lines, 4 tests). No unit tests at all. Python gets 86k lines of tests;
   the UI gets four.
2. **Static analysis.** `security.yml` runs CodeQL with `languages: python`.
   The entire JS/TS tree is unanalysed.
3. **Dependency updates.** `.github/dependabot.yml` declares `pip` and
   `github-actions` ecosystems only — **no `npm` entry** — so `frontend/` has
   no automated update path.

Gap 3 is why Q2 exists.

### Finding Q2 — High-severity vulnerability in a frontend dependency (high)

```
source-map-js  1.2.1   HIGH   GHSA-68fv-2mgg-jv7q
  event-loop denial of service via indexed source-map section offsets
  path: vite → postcss@8.5.26 → source-map-js ^1.2.1
```

Real severity in context is lower than "high" suggests — it is a build-time
dev dependency, not shipped to users. But `postcss` requires `^1.2.1`, so the
patched release satisfies the existing range: `npm audit fix` resolves it with
a **lockfile-only bump, no breaking change**. Nothing in CI would ever have
reported it.

### Finding Q3 — `src/bgate_mcp/server.py` is an 11,064-line module (medium)

Nearly 3× the next-largest file (`blender.py`, 6,458). It is the MCP tool
surface — the single most-edited file in an agent-driven project and the
hardest to review, merge or navigate. The `tools_level.py` split (2,477 lines)
shows the extraction pattern already exists; it just hasn't been applied to the
bulk.

### Finding Q4 — Repository weight (low)

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

**1. Add `npm` to Dependabot and run `npm audit` in CI — security, ~10 minutes.**
A six-line addition to `.github/dependabot.yml` (`package-ecosystem: "npm"`,
`directory: "/frontend"`) plus an `npm audit --audit-level=high` step in
`security.yml`. This closes the hole that let Q2 through and prevents the next
one. Do this one first — it is the cheapest item on the list and the only one
that is purely preventive.

**2. Fix the vulnerability and the broken ignore rule — security, ~5 minutes.**
`npm audit fix` in `frontend/` (lockfile only), then `git rm --cached
.github/instructions/codacy.instructions.md` and correct `.gitignore:54` to use
forward slashes. Two unrelated one-liners, both trivially verifiable.

**3. Close the PII loop — privacy, ~15 minutes.**
Set `git config --global user.email` to the GitHub `noreply` alias on every
development machine. Confirm the historical key from P2 was rotated
and that secret scanning with push protection is on. Ask the external
contributor whether they want their corporate address kept.

**4. Extend CodeQL to JavaScript/TypeScript — security + maintainability, ~30 minutes.**
Add `javascript-typescript` to the CodeQL language matrix. Use the default
suite, for exactly the reason security.yml already documents about the 277-alert
`security-and-quality` run: a gate nobody can make green is a gate everyone
ignores. 31k lines of UI code that renders agent output and handles user paths
deserves the same analysis the Python already gets.

**5. Start splitting `server.py`, and add frontend unit tests alongside — maintainability, ongoing.**
Extract tool groups from the 11k-line module following the `tools_level.py`
precedent, one group per PR so review stays tractable. In parallel, add Vitest
unit coverage for the UI's pure logic (state reducers, path handling,
formatting) — the parts where four e2e tests give the least protection and
where a unit test is cheapest to write.

---

### Bottom line

**No secrets, in the working tree or anywhere in 588 commits of history** —
and the project has a real redaction layer and a documented security posture
behind that result. The genuine issues are a third party's corporate email in
commit metadata, one high-severity (build-time) npm vulnerability, a
`.gitignore` rule silently broken by Windows path separators, and a frontend
that is excluded from testing, static analysis and dependency management alike.
Items 1–3 are under half an hour of work combined.
