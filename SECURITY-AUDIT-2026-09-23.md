# Builders Gate — Security & Quality Audit
**Date:** 2026-09-23 · **Commit:** 4b73deb · **Scope:** full public repo (1,308 tracked files, 174k LOC Python + 30k LOC TypeScript)

---

## 1. Secrets & PII — CLEAN

**No exposed secrets were found.** Confirmed explicitly:

| Check | Result |
|---|---|
| Known credential formats (`sk-`, `ghp_`, `AKIA`, `AIza`, `xox*`, `glpat-`, PEM private keys) in tracked files | No real hits |
| Same scan across **all 95 commits** of reachable history | Clean |
| Hardcoded `api_key=`/`token=`/`password=` assignments | None |
| Tracked `.env`, `*.pem`, `*.key`, keystore or credential files | None (only `.env.example`, all values blank) |
| `npm audit` on `frontend/package-lock.json` (145 deps) | 0 vulnerabilities |

Every match for a credential pattern was one of two benign things:
- **`src/bgate_core/board/streamer.py`** — the repo's own secret-scrubbing filter, which *contains* those patterns by design in order to redact them from streamed agent output.
- **Test fixtures** — `sk-ant-api03-AAAA…`, `AKIAIOSFODNN7EXAMPLE`, `ghp_AAAA…`, all obviously synthetic and used to assert the scrubber works.

**PII:** the apparent personal data (`C:\Users\marta`, `/home/bryan`, `marta:hunter2@github.com`, `marta-desktop`) is deliberate fiction — `tests/board/test_streamer.py:25` documents it: *"`marta` is fiction and has to stay fiction."* These are fixtures proving the scrubber strips usernames, hostnames and URL credentials. `marijn@haverbeke.berlin` is the CodeMirror copyright line in a vendored LICENSE. Nothing to remediate.

**One minor real item:** git commit metadata carries one outside contributor's corporate email address (2 commits) and the owner's personal address (1 of 95 commits). Inherent to how git records authorship and low severity, but noted since the repo is public. The addresses are deliberately not reproduced here, so that this file does not move them from commit metadata into tracked repository content.

**Historical note:** `.github/workflows/security.yml` states the repo *"has already had once (a committed API key)"*. My scan of all reachable history is clean, so that key is out of the current object graph — but see finding S1.

---

## 2. Code Quality — STRONG

This is a well-maintained codebase. Measured, not assumed:

- **Zero** unused imports, unused variables or redefinitions (`ruff F401,F841,F811` across `src/`, `frontend/`, `scripts/`) — **no dead code**.
- **Zero** bare `except:` clauses.
- **One** `TODO` marker in 205k LOC.
- **94% module test coverage** by reference (261 of 277 source modules); 83k LOC of tests against 175k LOC of source.
- Dependency **floors are treated as a security control** — `pyproject.toml` pins minimums at patched releases, and `security.yml` audits both the resolved tree *and* the floors in a separate venv, because a floor-only vulnerability is invisible to a resolved-tree audit. This is a genuinely sophisticated practice most projects miss.
- The dashboard's auth is **unusually well-reasoned**: a Host allow-list that fires before all other gates and is *not* disabled by `BGATE_NO_AUTH`, explicit DNS-rebinding defence, deliberate refusal of the `0.0.0.0`-day bypass, `secrets.compare_digest` everywhere a token is compared, and remote mode that *refuses to start* rather than degrade.

**Verified non-issues.** I checked these rather than reporting the raw linter count:
- **58 × `S608` "SQL injection"** — all false positives. Every case interpolates only literal clause fragments or generated `?` placeholder runs; all values are bound parameters. Confirmed in `art_tournament.py` and `agentreg.py`.
- **4 × `S324` weak hash** — all non-security: cache keys, content fingerprints, and `godot.py`'s MD5, which must match Godot's own import format.
- **`shell=True`** in `agents/claudeusage.py:142` — re-runs the user's *own* previously configured statusline command; the trust boundary is the user's own `settings.json`.

### Real quality findings

- **Q1 · Exception swallowing at scale.** 1,115 `except Exception` handlers in `src/`, of which 276 are `try/except/pass` and 23 are `except/continue`. Concentrated in `bgate_mcp/server.py` (88), `agents/dispatch.py` (64), `agents/followup.py` (30). Individually defensible for a tool that shells out to Blender and Godot; collectively this is the single largest source of silent failure, and it is where a genuine bug will hide longest.
- **Q2 · `bgate_mcp/server.py` is 10,551 lines** in one file — the largest module by 60%, and also the densest cluster of swallowed exceptions. It is the MCP entry point, so it is the hardest file to review and the most security-relevant to get right.

---

## 3. Suggested Enhancements — prioritized

### S1 · Enable GitHub secret scanning with push protection — *security, highest value, ~2 minutes*
`security.yml` already argues for this in a comment and it was never done. It is free on public repos, scans the full history, and **refuses the push** rather than reporting a key that is already public. Given this repo has leaked a key once, this is the single highest-return action available. Settings → Code security → Secret scanning + Push protection.

### S2 · Close the frontend supply-chain blind spot — *security*
**30,566 lines of TypeScript/React and 145 npm dependencies are audited by nothing.**
- `dependabot.yml` declares only `pip` and `github-actions` — **no `npm` ecosystem**, despite `frontend/package-lock.json` being tracked and `npm ci` running in CI.
- `security.yml` runs CodeQL with `languages: python` — **the entire frontend is unscanned**.

`npm audit` is clean *today*, which is exactly why this is worth fixing now rather than after an advisory lands. Two small edits: add an `npm` block to `dependabot.yml` pointing at `/frontend`, and add `javascript-typescript` to the CodeQL language matrix. Note the second will open an initial alert backlog — apply the same narrowing rationale already documented for the Python suite.

### S3 · Add `npm audit --audit-level=high` to CI — *security*
CI already does `npm ci` in four jobs. One extra line makes npm parity with the excellent `pip-audit` treatment Python already gets, and fails the merge path instead of waiting on a weekly Dependabot PR.

### S4 · Make swallowed exceptions observable — *maintainability*
Not a request to remove the 276 `except/pass` blocks — most guard genuinely optional paths. Instead route them through one `_soft_fail(exc, context)` helper that debug-logs and counts. This preserves current behaviour exactly while making silent failures discoverable, and would turn Q1 from a permanent blind spot into a diagnosable one. Highest leverage in `server.py` and `agents/dispatch.py`.

### S5 · Widen the ruff ruleset selectively — *maintainability*
`select = ["E9", "F"]` is pyflakes plus syntax errors only. The project's stated (and correct) reason for narrowing is that a wall of alerts gets ignored. `B` (bugbear) is the sweet spot — it catches real bugs like mutable default arguments and loop-variable capture, and produces a small, actionable set. I would *not* enable `S` wholesale: as shown above, its two biggest categories here are false positives.

### Optional · Split `bgate_mcp/server.py`
Sibling modules (`tools_level.py`, `tools_cinematic.py`, `tools_reuse.py`) already establish the pattern. Worth doing opportunistically rather than as a dedicated refactor — and note those three siblings are among the 16 modules with no direct test coverage.

---

## Bottom line
No secrets, no PII leakage, no dead code, and a security model that is better reasoned than most production web applications. The one material gap is that the **frontend half of the repo sits outside every scanner the backend half enjoys** (S2), and the highest-value action is a **two-minute repository setting** (S1).
