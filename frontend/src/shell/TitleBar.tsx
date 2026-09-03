/* TitleBar — the app's own window caption.
 *
 * The desktop window is created frameless (bgate_ui/webview2.py), so Windows
 * draws no caption and the page owns the top 44px: the drag strip, the app
 * name, and minimise / maximise / close.
 *
 * IT RENDERS NOTHING UNLESS THERE IS A WINDOW TO CONTROL. The same bundle is
 * served to a browser tab and to a window started with BGATE_NATIVE_FRAME=1,
 * which has a real system caption. In either case a drawn title bar would be a
 * lie with a close button on it, so /api/window/state is asked first and the
 * component returns null until it answers yes.
 *
 * THE DRAG REGION IS NOT CSS. `-webkit-app-region: drag` needs a WebView2
 * setting this host does not turn on; instead the window's WM_NCHITTEST claims
 * the top CAPTION_H pixels, minus CAPTION_BUTTONS_W on the right, as HTCAPTION.
 * That is why the geometry below is not free-form — the numbers are shared with
 * the wndproc and tests/test_webview2.py asserts they match. Drag, double-click
 * to maximise, edge-snap and shake all come from Windows for free as a result;
 * none of it is reimplemented here.
 *
 * The buttons still call the API rather than relying on the caption gesture,
 * because HTCAPTION covers the strip they sit in and a click there would
 * otherwise start a drag instead of pressing them. */
import { useEffect, useState } from "react";

/* Shared with CAPTION_H / CAPTION_BUTTONS_W in bgate_ui/webview2.py. */
export const CAPTION_H = 44;
export const BUTTON_W = 46;
export const CAPTION_BUTTONS_W = BUTTON_W * 3;

type WindowState = { available: boolean; maximized: boolean };

async function post(path: string): Promise<WindowState | null> {
  try {
    const r = await fetch(path, { method: "POST" });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

export function TitleBar() {
  const [state, setState] = useState<WindowState | null>(null);

  useEffect(() => {
    let alive = true;
    const read = async () => {
      try {
        const r = await fetch("/api/window/state");
        const d: WindowState = await r.json();
        if (alive) setState(d);
      } catch {
        if (alive) setState({ available: false, maximized: false });
      }
    };
    read();
    /* The user can maximise without touching this bar — a double-click on the
       drag strip, Win+Up, a snap gesture — so the icon has to follow the window
       rather than the button. Every one of those changes the viewport, so the
       page's own resize event is the notification; the slow timer is only
       there for a state change that somehow moved no pixels. */
    let settle = 0;
    const onResize = () => {
      window.clearTimeout(settle);
      settle = window.setTimeout(read, 120);
    };
    window.addEventListener("resize", onResize);
    const t = window.setInterval(read, 30000);
    return () => {
      alive = false;
      window.removeEventListener("resize", onResize);
      window.clearTimeout(settle);
      window.clearInterval(t);
    };
  }, []);

  const shown = !!state?.available;

  useEffect(() => {
    /* The class is what reserves the space; the bar itself is fixed so it
       cannot be pushed around by a deck's own layout. Removed on unmount so a
       fallback to the browser does not leave a 44px gap at the top. */
    document.documentElement.classList.toggle("bg-framed", shown);
    return () => document.documentElement.classList.remove("bg-framed");
  }, [shown]);

  if (!shown) return null;

  const maximized = !!state?.maximized;
  return (
    <div className="bg-titlebar" style={{ height: CAPTION_H }}>
      {/* MOUSEDOWN, NOT A CSS DRAG REGION. `-webkit-app-region: drag` needs a
          WebView2 setting this host does not enable, and the window's own
          HTCAPTION hit test never sees these pixels because the WebView2 child
          window covers them. Reporting the press lets Windows run its normal
          move loop — one request per drag, not one per mouse-move.

          Double-click maximises, which is what a caption does everywhere else
          and what people try before they look for the button. */}
      <div
        className="bg-titlebar-drag"
        onMouseDown={(e) => { if (e.button === 0) post("/api/window/drag"); }}
        onDoubleClick={async () => setState(await post("/api/window/maximize") ?? state)}
      >
        <span className="bg-titlebar-name">Builders Gate</span>
      </div>
      <div className="bg-titlebar-buttons" style={{ width: CAPTION_BUTTONS_W }}>
        <button
          className="bg-winbtn"
          title="Minimise"
          aria-label="Minimise"
          onClick={() => post("/api/window/minimize")}
        >
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <path d="M0 5h10" stroke="currentColor" strokeWidth="1" />
          </svg>
        </button>
        <button
          className="bg-winbtn"
          title={maximized ? "Restore" : "Maximise"}
          aria-label={maximized ? "Restore" : "Maximise"}
          onClick={async () => setState(await post("/api/window/maximize") ?? state)}
        >
          {maximized ? (
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
              <path
                d="M2.5 2.5h5v5h-5z M0.5 7.5v-7h7"
                fill="none"
                stroke="currentColor"
                strokeWidth="1"
              />
            </svg>
          ) : (
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
              <rect
                x="0.5"
                y="0.5"
                width="9"
                height="9"
                fill="none"
                stroke="currentColor"
                strokeWidth="1"
              />
            </svg>
          )}
        </button>
        <button
          className="bg-winbtn bg-winbtn-close"
          title="Close"
          aria-label="Close"
          onClick={() => post("/api/window/close")}
        >
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <path d="M0 0l10 10M10 0L0 10" stroke="currentColor" strokeWidth="1" />
          </svg>
        </button>
      </div>
    </div>
  );
}
