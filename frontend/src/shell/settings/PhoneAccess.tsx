import { useCallback, useState } from "react";
import { Button, Switch } from "@mantine/core";
import { Ti } from "../Ti";
import { mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";
import { askConfirm, copyText } from "./confirm";
import "./generators.css";

/* Phone — Settings → Connections. The companion app's door, from the desk.
 *
 * Backed by bgate_ui/remote.py through /api/remote, which is LOOPBACK-ONLY:
 * from the tailnet side these endpoints do not exist. That is the whole
 * security model of the panel — the phone cannot read the QR it is meant to
 * scan, reopen its own door, or un-revoke itself.
 *
 * Four controls, each one honest about what it does:
 *   - the switch closes the tailnet door without stopping the server (every
 *     phone gets 403 on its next poll; the desktop page is untouched). It can
 *     only OPEN a door that exists: a server started without --remote has no
 *     tailnet socket, and the panel says to restart rather than pretending.
 *   - rotate mints a new phone token and QR. Every paired phone is cut off
 *     at once; the desktop keeps its own token, which is a different secret.
 *   - revoke refuses one device while the rest keep working.
 *   - forget drops a row and is NOT a revoke, said beside the button.
 *
 * Polls while on screen, because the point of the device table is watching
 * a phone go online, and nothing on the event bus describes that. */

type Device = {
  id: string; ip: string; user_agent: string; first_seen: number; last_seen: number;
  requests: number; last_path: string; revoked: boolean; refused: number;
  online: boolean; idle_s: number;
};
type Refusal = { device: string; ip: string; user_agent: string; path: string; why: string; at: number };
type RemoteStatus = {
  listening: boolean; enabled: boolean; host: string; url: string; token: string;
  project: string; payload: string; qr: string; token_file: string;
  rotated_at: number | null; devices: Device[]; refusals: Refusal[]; restart_hint: string;
};

const EMPTY: RemoteStatus = {
  listening: false, enabled: false, host: "", url: "", token: "", project: "",
  payload: "", qr: "", token_file: "", rotated_at: null, devices: [], refusals: [],
  restart_hint: "",
};

function ago(ts: number): string {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function deviceName(ua: string): string {
  if (!ua) return "unknown device";
  const m = ua.match(/\(([^)]+)\)/);
  const inside = m ? m[1] : "";
  const app = ua.split(/[\s/]/)[0] || "device";
  return inside ? `${app} · ${inside}` : app;
}

function DeviceRow({ d, busy, onAct }: {
  d: Device; busy: string;
  onAct: (path: string, method: string, key: string) => void;
}) {
  const lamp = d.revoked ? "" : (d.online ? "good" : "warn");
  const word = d.revoked ? "revoked" : (d.online ? "online" : `idle ${ago(d.last_seen)}`);
  return (
    <div className={`gen-card s-${d.revoked ? "unconfigured" : (d.online ? "ready" : "unhealthy")}`}
         data-device={d.id}>
      <div className="gen-top">
        <Ti name="device-mobile" size={15} />
        <span className="gen-name">{deviceName(d.user_agent)}</span>
        <span className={`gen-lamp ${lamp}`}>{word}</span>
      </div>
      <div className="gen-kv">
        <span className="k">address</span><span className="v">{d.ip || "?"}</span>
        <span className="k">requests</span><span className="v">{d.requests}{d.refused ? ` (${d.refused} refused)` : ""}</span>
        <span className="k">last</span><span className="v">{d.last_path} · {ago(d.last_seen)}</span>
        <span className="k">first seen</span><span className="v">{ago(d.first_seen)}</span>
      </div>
      <div className="gen-row" style={{ marginTop: 9 }}>
        {d.revoked
          ? <Button size="xs" variant="outline" loading={busy === `restore:${d.id}`}
                    onClick={() => onAct(`/api/remote/devices/${d.id}/restore`, "POST", `restore:${d.id}`)}>
              let it back in
            </Button>
          : <Button size="xs" variant="outline" color="red" loading={busy === `revoke:${d.id}`}
                    onClick={() => onAct(`/api/remote/devices/${d.id}/revoke`, "POST", `revoke:${d.id}`)}>
              revoke this device
            </Button>}
        <Button size="xs" variant="default" loading={busy === `forget:${d.id}`}
                onClick={() => onAct(`/api/remote/devices/${d.id}`, "DELETE", `forget:${d.id}`)}>
          forget
        </Button>
        <span className="gen-fnote">
          forget drops the row only — a phone that still holds the token is back on its next request
        </span>
      </div>
    </div>
  );
}

export function PhoneAccess({ active }: { active: boolean }) {
  const [st, setSt] = useState<RemoteStatus & { __error?: string }>(EMPTY);
  const [busy, setBusy] = useState("");
  const [showToken, setShowToken] = useState(false);

  const load = useCallback(async () => {
    const d = await readJSON<RemoteStatus & { __error?: string }>("/api/remote", EMPTY);
    setSt(d);
  }, []);
  /* 4 s: the device table is the thing being watched, and "online" is a
     20 s window on the server; a slower poll would show a phone as gone
     while it was still there. */
  useEvents(load, { enabled: active, kinds: [], fallbackMs: 4000 });

  const act = useCallback(async (path: string, method: string, key: string, said?: string) => {
    if (busy) return;
    setBusy(key);
    const res = await mutate<RemoteStatus>(path, { method, quiet: true });
    setBusy("");
    if (!res.ok) { toast(res.error || "that did not land"); return; }
    if (res.data) setSt(res.data);
    if (said) toast(said, "ok");
  }, [busy]);

  async function toggle(on: boolean) {
    if (on) { await act("/api/remote/enable", "POST", "switch", "phone access on"); return; }
    const n = st.devices.filter((d) => d.online).length;
    const yes = await askConfirm({
      title: "Close the door to phones?",
      body: (n ? `${n} device${n === 1 ? " is" : "s are"} online right now and will get "phone access is off" on the next poll. `
               : "") + "The server keeps running and this dashboard is untouched. Flip the switch back to reopen.",
      ok: "close it", cancel: "leave it open",
    });
    if (yes) await act("/api/remote/disable", "POST", "switch", "phone access off");
  }

  async function rotate() {
    const yes = await askConfirm({
      title: "New token and QR?",
      body: "Every paired phone is cut off at once and has to scan the new QR. "
        + "This dashboard keeps its own token — that is a different secret. "
        + `${st.devices.length ? `The ${st.devices.length} device${st.devices.length === 1 ? "" : "s"} in the table will be cleared.` : ""}`,
      ok: "rotate it", cancel: "keep the current one", danger: true,
    });
    if (yes) await act("/api/remote/rotate", "POST", "rotate", "new token — scan the new QR");
  }

  const online = st.devices.filter((d) => d.online && !d.revoked).length;
  const stage = !st.listening ? "unconfigured" : (st.enabled ? "ready" : "unhealthy");
  const lamp = !st.listening ? "" : (st.enabled ? "good" : "warn");
  const word = !st.listening ? "no tailnet socket" : (st.enabled ? (online ? `open · ${online} online` : "open") : "closed");

  return (
    <div className="gen-wrap">
      {st.__error && <div className="gen-warn">{st.__error}</div>}
      <div className={`gen-card s-${stage}`} data-remote>
        <div className="gen-top">
          <Ti name="device-mobile" size={15} />
          <span className="gen-name">Phone access</span>
          <span className={`gen-lamp ${lamp}`}>{word}</span>
          <span style={{ marginLeft: "auto" }}>
            <Switch size="sm" checked={st.enabled} disabled={!st.listening || busy === "switch"}
                    onChange={(e) => toggle(e.currentTarget.checked)}
                    label={st.enabled ? "on" : "off"} />
          </span>
        </div>
        <div className="gen-help">
          The companion app reaches this dashboard over Tailscale with its own token, which is not
          the one this page uses. Whoever holds that token holds this dashboard, and the dashboard
          can run the engine, which can run anything on this PC: treat the QR like a password. The
          switch closes the door without stopping the server and stays closed across restarts;
          rotating the token cuts every phone off until it scans again. Revoking a device refuses
          its address and user agent - a client that changes either is a new device - so when in
          doubt, rotate.
        </div>
        {!st.listening && st.restart_hint && <div className="gen-why">{st.restart_hint}</div>}

        {st.listening && (
          <div className="gen-f">
            <div className="gen-flab">
              <span className="n">Pair a phone</span>
              <span className="v">{st.enabled ? "scan from the app's Settings" : "open the door first — the QR still works, the phone just gets refused"}</span>
            </div>
            <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
              {st.qr
                ? <img alt="pairing QR" src={st.qr}
                       style={{ width: 168, height: 168, imageRendering: "pixelated", background: "#fff",
                                borderRadius: 10, padding: 6, flex: "none" }} />
                : <div className="gen-why">segno is not installed — enter the URL and token by hand</div>}
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="gen-kv">
                  <span className="k">project</span><span className="v">{st.project}</span>
                  <span className="k">url</span><span className="v"><code className="gen-mono">{st.url}</code></span>
                  <span className="k">token</span>
                  <span className="v">
                    <code className="gen-mono">{showToken ? st.token : "•".repeat(Math.min(24, st.token.length || 8))}</code>
                    {" "}
                    <Button size="compact-xs" variant="subtle" onClick={() => setShowToken((v) => !v)}>
                      {showToken ? "hide" : "show"}
                    </Button>
                  </span>
                  {st.rotated_at && <><span className="k">rotated</span><span className="v">{ago(st.rotated_at)}</span></>}
                </div>
                <div className="gen-row" style={{ marginTop: 10 }}>
                  <Button size="xs" variant="outline" color="red" loading={busy === "rotate"} onClick={rotate}>
                    new token + QR
                  </Button>
                  <Button size="xs" variant="default" onClick={() => copyText(st.url, toast, "url copied")}>copy url</Button>
                  <Button size="xs" variant="default" onClick={() => copyText(st.token, toast, "token copied")}>copy token</Button>
                  <Button size="xs" variant="default" onClick={() => window.open("/pair", "_blank")}>open as a page</Button>
                </div>
                <div className="gen-fnote">stored at {st.token_file}</div>
              </div>
            </div>
          </div>
        )}

        <div className="gen-f">
          <div className="gen-flab">
            <span className="n">Devices</span>
            <span className="v">
              {st.devices.length
                ? `${st.devices.length} seen this session · ${online} online`
                : "nothing has connected since this server started"}
            </span>
            {st.devices.length > 0 && (
              <span style={{ marginLeft: "auto" }}>
                <Button size="compact-xs" variant="subtle" loading={busy === "forget-all"}
                        onClick={() => act("/api/remote/devices", "DELETE", "forget-all", "table cleared")}>
                  clear the table
                </Button>
              </span>
            )}
          </div>
          <div className="gen-grid">
            {st.devices.map((d) => <DeviceRow key={d.id} d={d} busy={busy} onAct={act} />)}
          </div>
          {st.refusals.length > 0 && (
            <div className="gen-insp">
              <div className="gen-ih">Refused lately</div>
              {st.refusals.slice().reverse().map((r, i) => (
                <div className="gen-node" key={`${r.at}-${i}`}>
                  <span className="gen-mono">{ago(r.at)}</span>
                  <span>{r.ip || "?"} · {deviceName(r.user_agent)}</span>
                  <span style={{ color: "var(--warn)" }}>{r.why}</span>
                  <span className="gen-mono" style={{ color: "var(--text-3)" }}>{r.path}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
