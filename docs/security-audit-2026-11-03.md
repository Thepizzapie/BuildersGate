# Repository audit — 2026-11-03

Scope: the public `Thepizzapie/BuildersGate` tree at `9175edc`, plus all 110
reachable commits. Automated scans (secret patterns, AST dead-code, ruff with an
extended rule set, `npm audit`) plus manual review of the dashboard's auth
middleware, the SQL layer, and the download/extract paths.

Status: **no secrets or PII exposed.** One security hardening item, two CI gaps,
one broken `.gitignore` rule, one cheap performance win.

---

## Secrets & PII — clean

**No live credentials anywhere in the working tree or in reachable history.**
Scanned for OpenAI / Anthropic / GitHub (PAT and classic) / AWS / Google /
Slack / GitLab / HuggingFace / Replicate / DigitalOcean token shapes, PEM
private keys, JWTs, and `*_KEY|*_TOKEN|*_SECRET|PASSWORD = <value>`
assignments, across both the checkout and `git log -p --all`.

- Only `.env.example` is tracked, and every value in it is empty.
- `.gitignore` covers `.env` and `.env.*` with a `!.env.example` exception, and
  ignores `.bgate/` — which holds both the project DB and the 0600 dashboard
  token (`src/bgate_ui/api.py:420`). The token never travels with a game repo.
- Every token-shaped string in the tree is a test fixture: `tests/board/test_streamer.py`,
  `tests/runtime/test_provider_keys.py`, `tests/ui/test_api_contract.py`. All are
  `AAAA…` / `NOTAREAL` / `EXAMPLE` / `DO-NOT-USE` placeholders, and they exist to
  feed the `Redactor` tests — i.e. the repo tests that it scrubs these shapes.
- Keys are never accepted on a command line: `bgate key set` prompts with echo
  off and takes no key argument.

**PII: none.** No real email addresses, no real home directory paths, no
non-loopback IP addresses. The personal names and `C:\Users\…` paths that appear
(`marta`, `bryan`, `nina`, `robin`) are all redaction-test fixtures. One public
personal handle (`twitch.tv/thepizzzapie`) appears in the chat integration — the
owner's own, and intentional.

### One thing to confirm outside this repo

`.github/workflows/security.yml` states in its own header that this repository
"has already had once (a committed API key)". That key is **not** present in any
reachable commit, so it was purged or predates this history. The same comment
notes that GitHub secret scanning with push protection is a *repository setting,
not a workflow*, and is the better control for that failure. I cannot read repo
settings from here:

- [ ] Confirm **Settings → Code security → Secret scanning + push protection** is on.
- [ ] Confirm the historically committed key was **rotated**, not just removed.

### Files flagged as "should this be public?"

**`.github/instructions/codacy.instructions.md` is tracked, and the rule meant
to exclude it does not work.** `.gitignore` carries:

```
.github\instructions\codacy.instructions.md
```

Git treats `\` as an escape character, not a path separator, so this pattern
matches nothing. `git check-ignore -v` confirms the file is **NOT IGNORED**, and
`git ls-files` confirms it is committed. Somebody decided this file should not be
in the repo and the rule silently failed.

The content holds no secrets. It is Codacy's vendor boilerplate — but it carries
`applyTo: '**'` and a set of "CRITICAL / YOU MUST" directives aimed at any
coding agent working in the repo (run `codacy_cli_analyze` after every edit,
offer to install the Codacy CLI). In a repository whose product *is* agent
automation, and which ships its own `CLAUDE.md` with different instructions, that
is a conflicting instruction surface shipped to every clone. Decide one way or
the other: delete the file, or fix the rule to `.github/instructions/`.

Nothing else problematic is tracked: no `.env`, no private keys, no internal-only
docs. `docs/qa-nitpick-audit.md` is a deliberate historical self-audit and says so.

**Licensing provenance (not security):** ~12 MB of mp3s under
`packaging/floor-assets/builders_gate_floor_assets/audio/` are generated through
the KIE API by `scripts/gen_floor_music.py`, and are redistributed under this
repo's MIT license. Worth confirming KIE's terms permit redistribution under a
permissive license.

---

## Code Quality

The baseline here is unusually strong, and the gaps are narrow.

**What is already right**

| Check | Result |
|---|---|
| `ruff check .` (repo's configured E9+F) | All checks passed |
| Dead code | 1 unreferenced function out of 3,274 |
| `TODO` / `FIXME` / `XXX` / `HACK` markers | 0, in ~194k lines of source |
| Test suite | ~90k LOC, ~6.3k test functions |
| `npm audit` (frontend) | 0 vulnerabilities |
| Direct npm deps | all 4 current (`@mantine` 9.7.4, React 19.3.1) |
| SQL injection | none found |
| Archive path traversal | none found |

- **Dead code is effectively absent.** An AST pass over every undecorated
  top-level function in `src/` (3,274 of them), cross-referenced against every
  mention anywhere in the repo, found exactly one that is never referenced:
  `effective_seat` at `src/bgate_core/store/modules.py:540`.
- **SQL is clean.** Every f-string-built statement (`board/jobs.py:76`,
  `board/queue.py:2503`, `qa/playtest.py:1487`, `store/db.py`) interpolates only
  hardcoded or allowlist-filtered column names — `set_run_fields` filters against
  `_RUN_FIELDS` before building assignments. No user-controlled identifier reaches
  a query.
- **Downloads are verified.** `runtime/toolbin.py` pins SHA-256 and refuses on
  mismatch before extracting, and writes members as `Path(member).name`, so a
  malicious archive cannot traverse out of the staging directory.
- **No unsafe deserialization.** No `yaml.load`, `pickle.loads`, `marshal`, or
  `os.system` in `src/`. `exec` appears only in the Blender/GDScript runners,
  which is the product's documented feature, and `SECURITY.md` is candid about it.
- **CI is thorough**: lint, Windows pytest on 3.11 and 3.12, advisory Linux,
  wheel build-install-import smoke, committed-bundle drift detection, browser
  smoke, and a weekly `pip-audit` over *both* the resolved tree and the
  exactly-pinned dependency floors.
- **`SECURITY.md` is honest about the threat model** — it states plainly what the
  guards stop and what they do not, including that the PreToolUse hook fails open
  and that dispatched agents run `--permission-mode acceptEdits` with Bash.

**Gaps**

1. **TypeScript is never typechecked in CI.** `.github/CONTRIBUTING.md:98` calls
   `npm run typecheck` "the gate", and `frontend/package.json` defines it — but no
   workflow invokes it. Vite/esbuild strips types without checking them, so a type
   error in 38.6k lines of TSX merges green.
2. **Dependabot does not watch npm.** `.github/dependabot.yml` covers `pip` and
   `github-actions` only, while `frontend/package-lock.json` exists and the built
   bundle is committed to `src/bgate_ui/static/dist` *and shipped inside the wheel
   and the `.exe`*. A vulnerable transitive dep would reach users with nothing
   watching. (Clean today — this is a monitoring gap, not a live vulnerability.)
3. **Silent exception handling at scale.** In `src/`: 302 `try/except/pass`,
   29 `try/except/continue`, 168 suppressible-exception sites. Many are
   deliberately best-effort and documented as such. At that volume, though, a real
   failure is indistinguishable from an intended one.
4. **`src/bgate_mcp/server.py` is 11,357 lines** — the clear maintainability
   outlier (next largest source file is 6,466).
5. **No coverage measurement.** `.coverage` is gitignored but nothing in CI
   produces or gates on it, so test-coverage drift is invisible.

---

## Suggested Enhancements

### 1. Security — drop the `testserver` sentinel from the production Host allowlist

`src/bgate_ui/api.py:348`:

```python
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
```

`testserver` is Starlette `TestClient`'s default Host, and the comment above it
argues it is safe because "`testserver` is not a registrable public name — nobody
can make a browser send it." That holds for *public DNS registration*; it does not
hold for local name resolution. On Windows — the supported platform — a
single-label name is resolved via LLMNR / NBT-NS / mDNS, which **any host on the
same network segment can answer**. A search domain or a hosts-file entry does it too.

That reopens exactly the DNS-rebinding hole the Host gate was written to close:

1. Victim loads any page; it embeds `<iframe src="http://testserver:7788/">`.
2. Attacker answers the `testserver` name query with their own address, serves JS.
3. The name rebinds to `127.0.0.1`. The iframe's JS is now, as far as the browser
   is concerned, same-origin with the dashboard.
4. `GET /` passes — the Host gate admits `testserver`, and safe methods return
   before the token check (`api.py:546`). The response embeds the token
   (`app.py:705`, `window.BGATE_TOKEN`).
5. Scrape the token, then `POST /api/godot/run` (`routes/godot_ws.py:439`) →
   arbitrary GDScript → `OS.execute()` → a shell as the desktop user.

Severity: needs network adjacency or control of a search domain, so not remote-
unauthenticated — but the payoff is full RCE, and the fix is two lines.

Two clean options, keeping the "a control the tests never exercise breaks
silently" principle intact:

- Admit the sentinel only when the suite is running —
  `{"127.0.0.1", "localhost", "::1"} | ({"testserver"} if os.environ.get("BGATE_TESTING") else set())`,
  with the conftest setting it; or
- give the `TestClient` `base_url="http://localhost"` and remove the sentinel entirely.

Either way, add a test asserting `Host: testserver` is **403** in a non-test
configuration, so the gate keeps proving itself.

### 2. Add `npm run typecheck` to CI, and npm to Dependabot

Two small diffs for the two gaps above. The `frontend-drift` job already does
`npm ci` with the right Node and cache, so the typecheck is one extra step in a
job that is already paying the install cost. Dependabot needs one stanza:

```yaml
  - package-ecosystem: "npm"
    directory: "/frontend"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 3
    labels: ["dependencies"]
```

This matters more than usual here because the bundle is committed and shipped in
the wheel and the `.exe`, so a frontend dependency problem travels to users who
never run npm.

### 3. Use the `lean=1` payload the backend already has

`/api/state`'s own docstring records the measurement: the full body is **1.4 MB,
1.08 MB of it `asset_groups`**, and `lean=1` already exists to drop it. Only the
phone passes it. The desktop shell does not:

- `frontend/src/shell/seats/api.ts:117` — `useSeats` polls the **full**
  `/api/state` every 15 s and reads only `seats`. Its own comment calls it "the
  most expensive body on the page."
- `frontend/src/floor-overlay.tsx:44` and `frontend/src/shell/brainstorm/Room.tsx:324`
  each fetch the full body for one small slice.

Appending `?lean=1` to these three call sites drops roughly 1 MB per poll, every
15 seconds, for data none of them read. A narrow `/api/seats` would be better
still, but the one-character version captures almost all of it.

### 4. Fix the broken `.gitignore` rule

Replace the backslash pattern with `.github/instructions/`, or delete
`codacy.instructions.md` and the rule together. Right now the repository's stated
intent and its actual contents disagree, and the file ships a conflicting agent
instruction surface to every clone. While there: a quick pass for other
backslash-written patterns, since the same mistake fails just as silently.

### 5. Stop the silent-exception surface from growing

Turn on `S110`, `S112` and `SIM105` in `[tool.ruff.lint]`, then grandfather every
existing site with per-file ignores. New code then has to either name the
exception via `contextlib.suppress(OSError)` or carry a one-line reason — while
the 331 deliberate best-effort sites stay untouched. This keeps the repo's
existing "bugs, not taste" rule philosophy (the current `E9+F` selection) and
only binds what gets written next.

**Also worth scheduling:** split `src/bgate_mcp/server.py` (11,357 lines) along
the lines the `tools_*.py` modules already establish, and add `--cov` to the
Linux advisory pytest job so coverage is at least visible without gating.
