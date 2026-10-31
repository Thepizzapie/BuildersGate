# Builders Gate — public repository audit

**Date:** 2026-10-31 · **Commit:** `9175edc` · **Scope:** 1,252 tracked files, full git history
(188,833 LOC `src/`, 31,826 LOC `frontend/src/`, 90,102 LOC `tests/`)

---

## 1. Secrets & PII — CRITICAL section: **clean**

**No exposed secrets were found.** Confirmed explicitly, across both the working tree and the
complete commit history:

| Check | Result |
|---|---|
| Key prefixes (`sk-`, `sk-ant-`, `AKIA`, `ghp_`, `AIza`, `xox*`, `glpat-`, `hf_`, PEM blocks, …) in tracked files | 14 hits, **all synthetic test fixtures** (`AAAA…`, `NOTAREAL`, `DO-NOT-USE`, `AKIAIOSFODNN7EXAMPLE`) |
| Same patterns across **all** commits, all branches | 9 distinct strings, all the same fixtures |
| Sensitive filenames ever committed (`.env`, `*.pem`, `*.key`, `id_rsa`, `.npmrc`, `.netrc`, service accounts) | **none, ever** |
| Real email addresses | none — only `example.com` placeholders and one upstream vendored author credit |
| Personal names / home paths | all synthetic (`marta`, `nina`, `robin`, `bryan`) in test fixtures and docstrings |
| Non-loopback IPs / private URLs | none — every hit is `127.0.0.1` |
| Commit author identities | all three use GitHub `users.noreply.github.com` addresses |

`.env.example` ships **empty** values only. `.gitignore` correctly excludes `.env`, `.env.*`,
`.bgate/` (which holds the minted dashboard token), and `export_presets.cfg`.

Worth noting as a genuine strength: `src/bgate_core/board/streamer.py` implements a runtime
`Redactor` that scrubs home directories, usernames, hostnames and key-shaped strings out of
streamed agent output, and it is covered by a dedicated test suite. Few projects of this kind
have one.

### Files that should not be public — one real finding

**[HIGH — exposure vs. intent] `.gitignore:54` is a dead pattern, and the file it was meant to
exclude is public.**

```
.github\instructions\codacy.instructions.md
```

Git only parses forward slashes, so this pattern matches nothing on any platform. Verified:

```
$ git check-ignore -v .github/instructions/codacy.instructions.md
  → no match (pattern is dead)
$ git ls-files --error-unmatch .github/instructions/codacy.instructions.md
  → tracked
```

Someone intended this file to be private; it has been public instead. It contains no
credentials, but two things make it worth acting on:

- It is an **agent-instruction file with `applyTo: '**'`**, so it injects directives
  ("CRITICAL… YOU MUST IMMEDIATELY…", "Failure to follow this rule is considered a critical
  error") into every AI agent any contributor runs in this repo — including instructions to
  install and run a third-party CLI. On a public repo that is an unreviewed influence on
  contributors' tooling.
- Its frontmatter is **malformed** — a duplicated `---` block at lines 1–5 — so it is unlikely
  to be parsed as intended anyway.

**Fix:** rewrite the pattern as `.github/instructions/codacy.instructions.md`, then
`git rm --cached` the file (or keep it deliberately, repaired and reviewed — but pick one).

---

## 2. Code Quality

### The baseline is strong — stated plainly so the findings below are read in proportion

- **6,554 test functions** across **335 test files**; 90k LOC of tests against 188k LOC of
  source (~0.48:1). "Missing tests" is not a general problem here.
- **Zero** `TODO`/`FIXME`/`HACK`/`XXX` in `src/`.
- `ruff check .` on the repo's own config: **all checks passed**. Zero unused imports (`F401`)
  in `src/` — no dead-import rot.
- CI is unusually thorough: dual `pip-audit` (both the resolved tree **and** the declared
  dependency floors — the floor audit is a genuinely uncommon and correct idea), CodeQL,
  a check that the committed `static/dist/` bundle matches a fresh `npm run build`, and a
  wheel smoke test in a clean venv specifically to catch a wheel shipping without its JS.
- Dependencies are current and documented as a security control. Frontend lockfile is clean:
  `vite 6.4.5`, `esbuild 0.25.12`, `react 19.2.8`; **`npm audit` reports 0 vulnerabilities**.

### Verified *not* problems (checked so they don't get re-raised)

A broad `ruff` ruleset produces alarming counts that do not survive inspection. Recording the
dispositions:

- **63 × `S608` "possible SQL injection" — all safe.** Sampled across `console.py`,
  `history.py`, `art_tournament.py`, `queue.py`, `db.py`: every one is either `?`-placeholder
  expansion for an `IN (…)` clause (`", ".join("?" * len(ids))`), a column allowlist
  (`k in _RUN_FIELDS`), or a statically-built `WHERE` fragment with all values bound as
  parameters. **No SQL injection found.**
- **20 × `S105` "hardcoded password" — all false positives.** These are placeholder *token
  names* for string substitution (`PROMPT_TOKEN`, `SEED_TOKEN`, `HOME_TOKEN`) and
  `TOKEN_FILENAME = "ui-token"`. No hardcoded credentials.
- **354 × `RUF100` "unused noqa" — an artifact of my broader ruleset**, not a real finding: the
  `# noqa: BLE001` directives are correct for a config that doesn't select `BLE001`.
- **Path traversal: defended.** All file-serving routes resolve and re-anchor before serving —
  `audio_ws._safe_audio` (`resolve()` + `relative_to()` + suffix allowlist) and
  `proof.image_path` (`os.path.commonpath` + extension allowlist + `is_file`). `.resolve()`
  means a symlink escaping the project is correctly rejected too.
- **`shell=True` in `agents/claudeusage.py:142` is not a vulnerability.** It replays the user's
  *own pre-existing* `statusLine` command from their own `settings.json` — a command Claude Code
  already runs through a shell. `shell=True` is required for fidelity.

### Real quality gaps

**[MEDIUM] The entire front end is outside static analysis and type checking.**
- `security.yml:100` sets `languages: python`. The ~32k LOC of TS/TSX and 83 JS files — including
  the dashboard shell that *handles the auth token* — get no CodeQL coverage.
- `frontend/package.json` defines a `typecheck` script (`tsc --noEmit`). **No workflow runs it.**
  TypeScript type errors cannot block a merge.
- **Zero unit tests for `frontend/src`.** 31,826 LOC is covered by one e2e spec
  (`frontend/e2e/critical.spec.ts`); there is no `*.test.tsx` anywhere. (The two
  `game.test.ts` files are scaffolding templates for *generated* games, not tests of this UI.)
- `dependabot.yml` covers `pip` and `github-actions` but **not `npm`**. The lockfile is clean
  today, so this is a process gap rather than a live exposure — but it is the one ecosystem here
  with no automated update path.

**[MEDIUM] File size is the dominant maintainability risk.**

| File | Lines |
|---|---|
| `src/bgate_mcp/server.py` | **11,357** |
| `src/bgate_adapters/blender.py` | 6,466 |
| `src/bgate_adapters/godot.py` | 4,160 |
| `src/bgate_ui/agents/dispatch.py` | 3,519 |
| `src/bgate_core/cine/cinematic.py` | 3,270 |

Under a broader ruleset `src/` shows 326 `C901` (complex function), 174 `PLR0912` (too many
branches) and 103 `PLR0915` (too many statements). The project already argues — persuasively, in
`security.yml` — against gates nobody can make green, so the answer is not to switch these on
repo-wide; it is to keep new code out of the largest files.

**[LOW] ~40 MB of binaries are in git history** (23 MB mp3 + 16 MB png), `.git` is 46 MB, and
`.gitattributes` marks them `binary` but configures no LFS. Every clone pays for the floor-asset
soundtrack. Not urgent; relevant if clone time becomes a complaint.

---

## 3. Suggested Enhancements

Prioritized by security first, then maintainability, then performance.

**1. Fix the dead `.gitignore` pattern and decide the Codacy file's fate.** *(security /
exposure — one line, highest confidence)* Correct the backslashes to forward slashes and either
`git rm --cached` the file or repair its frontmatter and keep it deliberately. Leaving an
`applyTo: '**'` agent-instruction file public *by accident* is the only confirmed
intent-vs-reality gap in the repo.

**2. Reconcile the `0.0.0.0` comment and document remote mode's real exposure.** *(security /
docs accuracy)* `app.py:2365–2367` states remote mode binds the tailnet IP and "never `0.0.0.0`,
and it refuses rather than falling back." Line **2394** then sets `bind_host = "0.0.0.0"`, with
its own later comment explaining the reversal (binding the single tailnet IP dropped the phone's
packets). The code is defended — Host allowlist, phone token, device revocation — but
`--remote` does listen on *every* interface, so on untrusted Wi-Fi the port is reachable from the
LAN, not only the tailnet. Delete the stale clause and add a line to `SECURITY.md`'s threat model
saying so; recommend a host firewall rule for the port. The posture is fine; the documentation
currently understates it.

**3. Make the token check fail closed.** *(security hardening)* `api.py:574` sets
`expected = ""` when `ensure_token()` raises, and the following `if expected:` then lets the
mutation through **unauthenticated**. That is intended for the no-project first-run case, but it
also fires on a read-only or full disk, or a permissions error on `.bgate/` — the token gate
silently disappears exactly when the filesystem is misbehaving. Distinguish "no project yet"
(open) from "token could not be minted" (401). The Host and `sec-fetch-site` gates still stand in
front of it, so this is hardening, not a live hole.

**4. Extend CI to the front end — three small changes.** *(security + maintainability, cheap)*
Add `javascript-typescript` to CodeQL's `languages`; add `npm run typecheck` to `ci.yml` (the
script already exists); add an `npm` entry to `dependabot.yml` for `/frontend`. This closes the
single largest analysis blind spot for roughly a dozen lines of YAML.

**5. Cap the growth of `bgate_mcp/server.py`.** *(maintainability)* At 11,357 lines it is the
hardest file in the repo to review, and tool registration is a natural seam — `tools_level.py`,
`tools_cinematic.py` and friends already demonstrate the pattern. Extracting even the largest
tool groups behind the existing module convention would pay for itself; no behaviour change and
the 6,554-test suite makes it a safe refactor.

**Optional sixth:** seed `frontend/src` unit tests on the token-handling and API-client paths
first (`src/shell/`, `views/*/api.ts`). Those are where a silent regression has security
consequences, and they are the parts hardest to cover from an e2e spec.

---

### Bottom line

No secrets, no PII, no SQL injection, no path traversal, clean dependency tree. The auth design
(Host allowlist checked first and not disabled by `BGATE_NO_AUTH`, same-origin check, per-project
0600 token, separate tailnet door with its own token and revocation) is better reasoned than most
of what ships in local developer tooling, and the inline comments explain *why* rather than
*what*. The findings above are a dead ignore pattern, one stale security comment, one fail-open
edge, and a front end that CI does not look at.
