import { useCallback, useState } from "react";
import { Ti } from "../Ti";
import { mutate, readJSON, toast } from "../../bridge";
import { useEvents } from "../../hooks";

/* THE STREAM PANEL — the redaction switch, and the six overlays nobody could
 * reach.
 *
 * WHAT WAS WRONG. This product ships six browser-source pages for OBS —
 * agent-feed, agent-ticker, agent-stage, live-crew, live-deck, live-critique —
 * all served, all working, and NOTHING in the app links to any of them. The
 * only way to use one was to know its filename. A feature you have to already
 * know about is a feature that does not exist, and these are the ones a person
 * streaming their build reaches for first.
 *
 * THE REDACTION SWITCH IS THE OTHER HALF AND IT IS THE ONE THAT MATTERS.
 * `privacy.streamer` puts a filter in front of every response that substitutes
 * absolute paths, identity and key material. The header chip reports its state
 * and cannot change it, so turning it on meant leaving the console, finding
 * Settings, finding Privacy. Before a stream is exactly when nobody wants a
 * scavenger hunt, and the cost of forgetting is your home directory and your
 * API keys on camera.
 *
 * DELIBERATELY NOT A ONE-CLICK OFF. Turning the filter ON is one press.
 * Turning it OFF asks, because the failure is asymmetric and silent: a stray
 * click that disables redaction looks identical to one that did nothing, right
 * up until a path with your name in it renders on stream.
 */

type Status = {
  on?: boolean; env_var?: string; note?: string;
  /** Counts, never values — the endpoint is explicit about that. */
  paths?: number; keys?: number; identities?: number;
};

/** Each overlay, with what it is FOR. A list of six filenames helps nobody
 *  choose; the sentence is the whole point of the row. */
const OVERLAYS: { file: string; name: string; what: string; ratio: string }[] = [
  { file: "agent-stage.html", name: "Stage", ratio: "1920×1080",
    what: "the full scene — sprites on a lane, panes above, ticker along the bottom" },
  { file: "live-deck.html", name: "Deck", ratio: "1920×1080",
    what: "the busiest one: activity, agents, console state and critique together" },
  { file: "live-crew.html", name: "Crew", ratio: "1920×1080",
    what: "two rows of cells, one per seat, with the agent's name and what it holds" },
  { file: "agent-feed.html", name: "Feed", ratio: "480×1080",
    what: "a vertical column of what just happened — good as a side bar" },
  { file: "agent-ticker.html", name: "Ticker", ratio: "1920×120",
    what: "one line, transparent background, whoever is running right now" },
  { file: "live-critique.html", name: "Critique", ratio: "1920×1080",
    what: "the art critique feed — verdicts and the images they are about" },
];

export function Streamer() {
  const [st, setSt] = useState<Status & { __error?: string }>({});
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setSt(await readJSON<Status>("/api/streamer", {}));
  }, []);
  useEvents(load, { kinds: ["settings.*"] });

  const on = !!st.on;

  async function toggle() {
    /* OFF IS THE DANGEROUS DIRECTION, so only that one asks. */
    if (on && !window.confirm(
      "Turn redaction OFF?\\n\\nAbsolute paths, identity and key material will "
      + "render in full in every response — including anything already on screen.")) {
      return;
    }
    setBusy(true);
    const r = await mutate("/api/settings", {
      method: "PATCH", body: { "privacy.streamer": !on }, quiet: true,
    });
    setBusy(false);
    if (!r.ok) { toast(r.error || "the switch did not move", "bad"); return; }
    toast(!on ? "redaction on — paths, identity and keys are covered"
              : "redaction off", !on ? "ok" : "warn");
    /* The filter caches its answer for a couple of seconds because it is
       consulted on every request; the route drops that cache on this key, so a
       read now reports the new state rather than the old one. */
    load();
  }

  const copy = (file: string) => {
    const url = `${location.origin}/static/${file}`;
    void navigator.clipboard?.writeText(url);
    toast("URL copied — add it as a Browser source");
  };

  return (
    <div className="bg4-stream">
      <div className={`bg4-stream-sw${on ? " on" : ""}`}>
        <Ti name={on ? "eye-off" : "eye"} size={17} />
        <div className="b">
          <div className="t">{on ? "Redaction is on" : "Redaction is off"}</div>
          <div className="s">
            {on
              ? "Absolute paths, identity and key material are substituted in every response."
              : "Every response carries full paths, identity and key material — including yours."}
          </div>
        </div>
        <button className="bg4-act" onClick={toggle} disabled={busy}>
          {busy ? "…" : on ? "turn off" : "turn on"}
        </button>
      </div>

      {/* The counts the endpoint reports. It is explicit that it returns counts
          and never values — a redactor that printed what it was hiding would be
          the leak it exists to prevent. */}
      {on && (st.paths || st.keys || st.identities) ? (
        <div className="bg4-stream-n">
          {st.paths ? <span>{st.paths} paths</span> : null}
          {st.keys ? <span>{st.keys} keys</span> : null}
          {st.identities ? <span>{st.identities} identities</span> : null}
          <span className="dim">covered</span>
        </div>
      ) : null}

      {st.env_var && (
        <div className="bg4-stream-env">
          Also settable as <code>{st.env_var}=1</code> before the dashboard starts.
        </div>
      )}

      <div className="bg4-stream-h">Overlays — add as a Browser source in OBS</div>
      {OVERLAYS.map((o) => (
        <div className="bg4-overlay" key={o.file}>
          <div className="b">
            <div className="t">{o.name} <span className="r">{o.ratio}</span></div>
            <div className="s">{o.what}</div>
          </div>
          <button className="bg4-act" onClick={() => copy(o.file)} title="copy the URL">
            <Ti name="copy" size={13} />url
          </button>
          <button className="bg4-act"
                  onClick={() => window.open(`/static/${o.file}`, "_blank", "noopener")}
                  title="open it in a tab to see what it looks like">
            <Ti name="external-link" size={13} />
          </button>
        </div>
      ))}
      {!on && (
        <div className="bg4-stream-warn">
          <Ti name="alert-triangle" size={14} />
          These render whatever the API returns. With redaction off that includes
          your absolute paths and any key a response carries.
        </div>
      )}
      {st.__error && (
        <div className="bg4-stream-warn">could not read the filter — {st.__error}</div>
      )}
    </div>
  );
}
