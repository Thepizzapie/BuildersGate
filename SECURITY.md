# Security

## What this software is

Builders Gate is a local developer tool that, by design, **executes code and
spends money on your behalf**. It runs Godot and Blender as subprocesses,
executes arbitrary GDScript through `godot_run` (which can reach `OS.execute()`,
i.e. a shell as the desktop user), spawns Claude Code sessions with edit
permissions inside your game project, and calls paid image APIs with keys it
loads from a project-local `.env`.

Treat it as you would a build system with a shell in it. Run it on your own
machine, on projects you trust.

## Threat model

**What the guards are designed to stop:** a web page you happen to have open in
your browser reaching the dashboard on `127.0.0.1` and driving it. `127.0.0.1`
is not a security boundary, since any page can POST to localhost, so
`bgate_ui/api.py` installs three checks on the mutating surface:

1. **Host allowlist.** The request's `Host` header must resolve to a loopback
   name. This is checked first and is **not** disabled by `BGATE_NO_AUTH`,
   because it closes a hole the other two cannot see: a page on `evil.com` that
   rebinds its DNS to `127.0.0.1` genuinely *is* same-origin as far as the
   browser is concerned, and would otherwise be able to read `/`, scrape the
   token out of the HTML, and own everything.
2. **Same-origin.** `sec-fetch-site` and `Origin` are checked against the host.
3. **Per-project bearer token.** Minted into `.bgate/ui-token` (0600,
   gitignored), injected into the page, and required on every mutation.
   `BGATE_NO_AUTH=1` opts out for a scripted or CI run.

Approval is human-only throughout: a dispatched agent carries
`BGATE_ACTOR=agent:item-<id>` and is refused the bible, the scope filing, the
budget, the revert, workflow gates, and promoting a candidate to the build.

**What it explicitly is not designed to protect against:**

- **A hostile local user or hostile process on the same machine.** Anything that
  can read `.bgate/` has the token, and anything that can write your project can
  do whatever an agent could.
- **A hostile game project or a hostile prompt.** The seat lanes, asset locks
  and the canon gate are *coordination* controls that stop agents from stomping
  each other's work. They are not a sandbox and were never meant to contain an
  adversary. The PreToolUse hook fails **open** by design: a crashing hook must
  never dam a session.
- **Exposure to a network.** It binds to `127.0.0.1` and there is no multi-user
  model, no roles, and no audit trail suitable for one. Do not put it behind a
  reverse proxy or on a shared host.
- **Untrusted input to the adapters.** GDScript and Blender Python passed to
  `godot_run` / `blender_run` are executed as-is. That is the feature.
- **Secret exfiltration by an agent you ran.** Keys are loaded per-project and
  never logged, but an agent with Bash can read the `.env` like any other file.

## Reporting a vulnerability

Report privately, not as a public issue: use **GitHub Security Advisories**
(Security → Report a vulnerability) on
<https://github.com/Thepizzapie/BuildersGate>.

Useful in a report: what an attacker controls to begin with, the exact request
or file that triggers it, and what they get. This is a solo project with no SLA.
You will get an honest answer, not a fast one.

Note that `docs/qa-nitpick-audit.md` is a **historical**
self-audit; several issues it describes are fixed. Its status header lists which.
Please check the code before reporting something from it.
