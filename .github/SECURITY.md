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
`src/bgate_ui/api.py` installs three checks on the mutating surface:

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
provider keys, the revert, workflow gates, and promoting a candidate to the build.

**What it explicitly is not designed to protect against:**

- **A hostile local user or hostile process on the same machine.** Anything that
  can read `.bgate/` has the token, and anything that can write your project can
  do whatever an agent could.
- **A hostile game project or a hostile prompt.** The seat lanes, asset locks
  and the canon gate are *coordination* controls that stop agents from stomping
  each other's work. They are not a sandbox and were never meant to contain an
  adversary. The PreToolUse hook fails **open** by design: a crashing hook must
  never dam a session.
- **Exposure to a network.** By default it binds `127.0.0.1` only. There is no
  multi-user model, no roles, and no audit trail suitable for one. Do not put it
  behind a reverse proxy or on a shared host. The one supported exception is
  opt-in remote mode, which has its own section below — read it before using it,
  because it does open a listener on every interface.
- **Untrusted input to the adapters.** GDScript and Blender Python passed to
  `godot_run` / `blender_run` are executed as-is. That is the feature.
- **Secret exfiltration by an agent you ran.** Keys are loaded per-project and
  never logged, but an agent with Bash can read the `.env` like any other file.

## Remote (phone) access — `bgate serve --remote`

`bgate serve --remote` is the one supported way to reach the dashboard from
somewhere other than the machine running it: your phone, over Tailscale. It is
off unless you ask for it.

It shipped undocumented, and this file used to say the server binds loopback
full stop. In this mode it does not, so the details below are the ones that
matter.

**It binds `0.0.0.0`** — every interface, not just the Tailscale one. Binding
the single tailnet IP was tried first and dropped the phone's packets, so the
bind is wide and **the bind is not the boundary**. Anything that can route to
this machine can open a TCP connection to the port, including the coffee-shop
LAN. What stands between that connection and your project is the admission
check, not the socket.

A request that is not the desk's by *every* signal at once — loopback socket
peer, loopback `Host`, and no proxy stamps (`is_desk()` in
`src/bgate_ui/api.py`) — is treated as a phone and held to four things the
loopback side is not:

1. **Path allowlist.** Only `/api/`, `/play/` and `/favicon`. Never `/`, whose
   HTML carries the desk's own `ui-token` — a remote client that could read it
   would hold a second credential that rotating the phone token does not
   revoke. Never `/api/remote` or `/pair` either: those are the controls over
   its own door. Anything else is a 404.
2. **An explicit switch.** Settings → Companion must be on. Checked per
   request, so the tailnet side closes and reopens without a restart.
3. **A separate token on every method, including GET.** A `GET /api/state` is
   the whole project, so there is no safe-method exemption. The phone token is
   machine-wide (`~/.bgate/remote-token`, 0600, `BGATE_HOME` honoured), *not*
   the per-project `ui-token`: rotating it re-pairs phones and does not log the
   desktop out of itself. It travels in `X-BGate-Token` or
   `Authorization: Bearer`. A cookie is accepted **only** for `/play/*` and the
   game's own telemetry POST, because a web build's engine fetches its own
   `.wasm`/`.pck` as subresources it cannot add a header to.
4. **A device that has not been revoked.** Every tailnet-side request lands in
   a bounded device table (address, user agent, first/last seen, count); any
   one can be revoked by hand. Wrong-token attempts are kept in a short ring so
   a failing phone looks like knocking rather than silence.

Two refusals are deliberate and both exit non-zero rather than falling back:
remote mode will not start under `BGATE_NO_AUTH` (it is nothing but gates), and
it will not start if no Tailscale address is found.

### Do not relay the port with `tailscale serve --tcp`

A raw TCP passthrough hands the connection over from `127.0.0.1` with nothing
added, so a tailnet client that writes `Host: 127.0.0.1` becomes
indistinguishable from the desk — which means it is served `/`, and `/` carries
the desk's token. This is the one relay the checks above cannot see through.
Serve the tailnet IP directly (what `--remote` does) or put an HTTP proxy in
front (`tailscale serve --https=443 http://127.0.0.1:PORT`), which stamps
`X-Forwarded-For` and is therefore seen.

### Known residual risks in this mode

- **The wide bind.** Covered above: admission is the control, not the bind.
- **DNS rebinding against the webhook.** `webhook_target()` requires HTTPS and
  resolves the host, refusing any non-public address, but the connection is
  made after the check — a hostile DNS server could answer differently the
  second time. Accepted rather than hidden: the alternative is pinning the
  socket and forging a `Host` header, for an opt-in, off-by-default switch on a
  single-operator tool.
- **MagicDNS names are not admitted**, only the detected tailnet IP. That is a
  deliberately narrow allowlist, so anti-rebinding holds.
- Everything in *"what it explicitly is not designed to protect against"* still
  applies. Remote mode adds a door for **your** phone. It does not turn this
  into multi-user software, and nothing about it is suitable for a shared host.

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
