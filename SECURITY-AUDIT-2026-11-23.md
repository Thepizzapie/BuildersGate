# Builders Gate — repository audit
**Date:** 2026-11-23 · **Commit:** 6e6bfa2 · **Scope:** working tree + all 124 commits of reachable history

---

## 1. Secrets & PII — CRITICAL section: **clean**

**No live secrets were found**, in the working tree or anywhere in history.

Scans run:

| Check | Result |
|---|---|
| Provider key prefixes (`sk-`, `sk-proj-`, `ghp_`, `github_pat_`, `xox[baprs]-`, `AKIA`/`ASIA`, `AIza`, `ya29.`, `hf_`, `r8_`, `glpat-`, `npm_`, `dop_v1_`, Stripe live, SendGrid, Twilio, JWTs, PEM private keys) across tracked files | 18 hits, **all synthetic test fixtures** |
| Same prefixes across all history diffs (`git log --all -G`) | 2 commits, **same fixtures** |
| `OPENAI_API_KEY=` / `KREA_` / `KIE_` / `RETRO_DIFFUSION_` / `DEEPGRAM_` / `STABILITY_` / `TRIPO_` / `MESHY_` / `TWITCH_OAUTH_TOKEN` with a value, full history | **test fixtures only** (`k-global`, `old-value`, `project-key`…) |
| High-entropy `UPPER_VAR=<24+ chars>` lines added anywhere in history | **none** |
| `.env` / `.pem` / `.key` / `id_rsa` / `credentials` / `secrets.*` ever tracked (`--diff-filter=A`, all history) | **only `.env.example`**, values empty |
| Unreachable/dangling objects (`git fsck --lost-found`) | **none** |
| Internal or private hostnames / IPs | **none** — all public vendor endpoints or `127.0.0.1` |

Most of the prefix hits are *deliberate*: `tests/board/test_streamer.py` feeds fake keys to the
output scrubber to prove it redacts them. That is the right reason for a key-shaped
string to be in a repo.

`.gitignore` correctly covers `.env` and `.env.*` with a `!.env.example` exception, and
both `init` and `adopt` stamp it into user projects.

### Two notes, neither an active exposure

**1.1 — One commit carries a personal email address.**
`4051fc7` (2026-10-25) is authored with a personal address (see `git log -1 --format=%ae 4051fc7`;
it is deliberately not reproduced here, because this report is itself committed to a public
repository). The other 123 commits
use GitHub `noreply` addresses (`Thepizzapie@users.noreply.github.com`,
`70639735+Thepizzapie@…`, `182161941+bmudgebos@…`). Git author metadata is public and
permanent on a public repo. If the noreply address is the intent everywhere else, this one
is an outlier. Fixing it means rewriting that commit; setting
`git config user.email` to the noreply address prevents a repeat and is the cheaper half.

**1.2 — The previously-committed key is not in current history, which is not the same as safe.**
`.github/workflows/security.yml` refers to "the failure this repo has already had once (a
committed API key)", and `CLAUDE.md` says an earlier revision of its own instructions caused
one. **That key is not in the 124 commits reachable today** — so history was rewritten, or it
predates this history. Rewritten history survives in GitHub's unreachable objects, in forks,
and in third-party caches. Unless it was already rotated, treat that key as burned.

Related, and the single highest-value action in this report:
**enable GitHub secret scanning with push protection** (Settings → Code security).
`security.yml` argues for this itself and explains why it deliberately ships no
third-party scanning action instead. It is a repository setting, so no workflow can
supply it and I cannot verify from here whether it is on. Push protection refuses the
push; every scan in this report, including mine, only tells you after the fact.

---

## 2. Code Quality — strong

This codebase is better defended and better tended than most. Notable:

- **Lint is clean.** The CI ruleset (ruff `E9` + `F`) passes on `src/` with zero findings.
  A broader sweep found **zero unused imports (F401)** and **zero unused locals (F841)** across
  `src/` *and* `tests/` — genuinely rare at 189k lines. Dead code is not a problem here.
  Residue: 10 commented-out-code blocks (ERA001), 39 TODO/FIXME/HACK markers.
- **Tests are real.** 338 test files / 91k lines against 323 source files / 189k lines.
  The 62 skip markers are honest capability gates (`blender.available()`, `godot.available()`,
  `sys.platform != "win32"`), not disabled tests, and `nightly.yml` exists specifically to run
  what the merge gate deselects.
- **Dependencies are clean right now.** `pip-audit` on the declared floors: *no known
  vulnerabilities*. `npm audit --audit-level=low` on the frontend lock: *0 vulnerabilities*.
  The floors-vs-resolved-tree distinction in `security.yml` is the correct and uncommon call.
- **The localhost threat model is right.** `src/bgate_ui/api.py` gates on Host-header allowlist
  (explicitly *not* disabled by `BGATE_NO_AUTH`), same-origin, and a `0600` token — and the
  comments name DNS rebinding as the reason. Given that `godot_run` reaches `OS.execute()`,
  that ordering is load-bearing and it is correct.
- **No XSS found.** I checked every `innerHTML` sink in `frontend/public/` (23 JS files) with a
  script that flags unescaped `${…}` interpolation. Every string-valued interpolation routes
  through `esc()`/`E()`; the unescaped ones are numbers, booleans, constant-table lookups, or
  internal icon helpers. `streamer.js` and `icons.js` assign only static strings.
- **Storage is indexed.** 74 `CREATE INDEX` across ~30 tables. No N+1 pattern stood out.
- `subprocess` use is clean: one `shell=True`, in `agents/claudeusage.py:142`, re-running the
  user's *own* pre-existing Claude status-line command. Their config, their shell — not an
  injection path.

**Main maintainability concern:** `src/bgate_mcp/server.py` is **11,357 lines**, roughly 3× the
next largest module (`bgate_adapters/blender.py`, 6,466). It is the tool-registration surface,
so it accretes by design, but at that size it is the one file where a reviewer cannot hold
the whole thing in their head.

---

## 3. Suggested Enhancements — prioritized

### P1 · Nothing audits the vendored JavaScript *(security)*
`frontend/public/vendor/` ships committed third-party code — **CodeMirror 5.65.16**, **three.js**,
Tabler icons, Inter/JetBrains fonts — and it is covered by **none** of the four scanners:

- `npm audit` sees only `frontend/package.json`; these files are not in it.
- Dependabot has no entry that reaches them.
- CodeQL is Python-only (below).
- `pip-audit` is irrelevant to them.

This is the dashboard's own origin — the page that carries `window.BGATE_TOKEN`, whose
compromise `api.py` correctly traces to `OS.execute()`. CodeMirror 5 is the legacy line
(CodeMirror 6 is the maintained one). *I found no advisory affecting the pinned versions* —
the finding is that **no process would tell you if one landed tomorrow.**
→ Record the vendored versions in a manifest and check them in `security.yml`, or pull them
through npm so the existing `npm audit` job covers them.

### P2 · CodeQL does not analyze the frontend *(security)*
`security.yml` sets `languages: python`. The ~83 hand-written JS files in `frontend/public/` —
every `innerHTML` sink I just audited by hand — get no static analysis at all.
→ Add `javascript-typescript` to the CodeQL matrix. The comment there about narrowing to the
default suite rather than `security-and-quality` applies equally and should carry over.

### P3 · Dependabot has no npm ecosystem *(security / maintenance)*
`.github/dependabot.yml` covers `pip` and `github-actions` only, but `frontend/package-lock.json`
is a real tree that `release-exe.yml` builds and bundles into the shipped `.exe`. The weekly
`npm audit --audit-level=high` job catches *known high* advisories; nothing ever proposes
the bump, and nothing catches moderate ones.
→ Add a third `updates:` entry: `package-ecosystem: "npm"`, `directory: "/frontend"`.

### P4 · The `numpy` floor is unsatisfiable on Python 3.13+ *(correctness — verified)*
`pyproject.toml` declares `requires-python = ">=3.11"` and `numpy>=1.26`.
`scripts/dep_floors.py` emits `numpy==1.26`, which resolves to **1.26.0** — and
**numpy 1.26.0 declares `<3.13,>=3.9`**. So on Python 3.13 or 3.14 a constrained resolver
gets `No matching distribution found for numpy==1.26.0`. I reproduced this: `pip-audit` spun
up a 3.13 venv and the install failed exactly that way. It is invisible today only because
every `security.yml` job pins Python 3.12.
→ Raise the floor to `numpy>=1.26.2` (the first 1.26 without the cap). One character, and it
restores the guarantee the floors comment is written to make.

### P5 · `mcp>=1.30.0,<2` is now a major version behind *(maintenance)*
The `<2` cap holds users on the mcp 1.x line while **2.3.0** is current (2.0.0 → 2.3.0 are all
released). Since Builders Gate registers as an MCP server inside environments that may pull
mcp 2.x themselves, the cap is a resolver conflict waiting to happen in a shared venv.
→ Test against 2.x and widen to `<3`, or leave the cap and add a comment saying which 2.x
incompatibility justifies it — every other pin in that file explains itself, this one does not.

---

### Verdict
**No secrets, no PII leak beyond one commit's author email, no XSS, no vulnerable dependency.**
The five items above are coverage gaps and version hygiene, not live defects. P1–P3 all share
one root cause worth naming: **the security tooling was built around the Python package and
never extended to the frontend that ships alongside it.**
