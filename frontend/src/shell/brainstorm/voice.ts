import { useCallback, useEffect, useRef, useState } from "react";

/* THE ROOM'S MICROPHONE AND SPEAKER, over the dashboard's Deepgram relay.
 *
 * NOT A SECOND CLIENT. brainstorm.js already owns a Voice object and a Mic
 * class that between them handle the MediaStream, the AudioContext, the
 * resample to the server's declared rate, the websocket handshake that sends
 * the dashboard token as its FIRST FRAME rather than in a query string, and a
 * stop() that four callers race into. That code is exported now
 * (Brainstorm.voice / Brainstorm.Mic) and this file drives it. Writing a React
 * mic client beside it would be a second place for all of that to be subtly
 * wrong — and the wrong one would be the untested one, while the failure mode
 * is a live microphone with no visible button to turn it off.
 *
 * THE KEY IS NEVER HERE. routes/voice.py holds DEEPGRAM_API_KEY and relays both
 * directions over loopback, precisely so no page script can read it. Nothing in
 * this file fetches a key, and the status endpoint does not serve one.
 *
 * DEGRADING IS THE NORMAL CASE. Most installs have no Deepgram key, so
 * `available` is false and `reason` is a sentence written for a tooltip. The
 * controls stay visible and disabled with that sentence attached rather than
 * vanishing: a button that is absent teaches nothing, and "why can't I talk to
 * this room" is otherwise unanswerable from the screen.
 */

export type VoiceStatus = {
  available: boolean; reason?: string; key?: boolean; websockets?: boolean;
  listen_model?: string; speak_model?: string; speak_models?: string[];
  max_speak_chars?: number;
  audio?: { encoding?: string; sample_rate?: number; channels?: number };
  usd_per_minute?: number | null; usd_per_1k_chars?: number | null;
};

type MicHandlers = {
  interim?(text: string): void;
  final?(text: string): void;
  state?(name: string, detail: string): void;
  error?(msg: string): void;
};

type MicLike = { live: boolean; start(): Promise<void>; stop(): void };

type VoiceLike = {
  status: VoiceStatus | null;
  load(): Promise<VoiceStatus>;
  speak(text: string, model?: string): Promise<string>;
  stopSpeaking(): void;
};

function client(): { voice: VoiceLike; Mic: new (h: MicHandlers) => MicLike } | null {
  const b = window.Brainstorm as unknown as {
    voice?: VoiceLike; Mic?: new (h: MicHandlers) => MicLike;
  } | undefined;
  if (!b?.voice || !b?.Mic) return null;
  return { voice: b.voice, Mic: b.Mic };
}

/** Persisted so the preference survives a reload — a person who talks to the
 *  room talks to it every time, and re-arming TTS on every visit is the kind of
 *  friction that gets a feature called broken. The MIC is deliberately NOT
 *  persisted: a microphone that turns itself on because it was on yesterday is
 *  a privacy bug, not a convenience. */
const TTS_KEY = "bg-room-tts";
const readTts = () => {
  try { return localStorage.getItem(TTS_KEY) === "1"; } catch { return false; }
};

export function useVoice(active: boolean) {
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [tts, setTts] = useState(readTts);
  const [micLive, setMicLive] = useState(false);
  const [micState, setMicState] = useState("");
  const [heard, setHeard] = useState("");        // interim, for the caret line
  const mic = useRef<MicLike | null>(null);
  /* The caller's sink for a finished sentence. Held in a ref so flipping it
     does not tear down a live microphone mid-sentence. */
  const onFinal = useRef<(text: string) => void>(() => {});

  useEffect(() => {
    if (!active) return;
    const c = client();
    if (!c) {
      setStatus({
        available: false,
        reason: "brainstorm.js is not loaded on this build — the voice relay "
              + "client lives in it",
      });
      return;
    }
    let alive = true;
    void c.voice.load().then((s) => { if (alive) setStatus(s); });
    return () => { alive = false; };
  }, [active]);

  /* A HOT MIC MUST NOT SURVIVE THE SCREEN. Leaving the room, switching seats or
     a hot reload all unmount this, and the browser would otherwise keep the
     capture indicator lit with nothing on screen to stop it. */
  useEffect(() => () => {
    try { mic.current?.stop(); } catch { /* already dead */ }
    try { client()?.voice.stopSpeaking(); } catch { /* nothing playing */ }
  }, []);

  const stopMic = useCallback(() => {
    try { mic.current?.stop(); } catch { /* idempotent */ }
    mic.current = null;
    setMicLive(false);
    setHeard("");
    setMicState("");
  }, []);

  const startMic = useCallback(async () => {
    const c = client();
    if (!c || !status?.available) return;
    const m = new c.Mic({
      interim: (text) => setHeard(text),
      final: (text) => { setHeard(""); onFinal.current(text); },
      state: (name, detail) => {
        setMicState(detail || name);
        /* The RELAY decides when the mic is really live — "starting" is not
           listening, and painting the button hot before the socket is up is how
           a failed start looks like a working one. */
        if (name === "closed" || name === "error") stopMic();
      },
      error: (msg) => { setMicState(msg); stopMic(); },
    });
    mic.current = m;
    setMicLive(true);
    try {
      await m.start();
    } catch (e) {
      setMicState(String((e as Error)?.message || e));
      stopMic();
    }
  }, [status, stopMic]);

  const toggleMic = useCallback(() => {
    if (micLive) stopMic(); else void startMic();
  }, [micLive, startMic, stopMic]);

  /* ESCAPE KILLS THE MICROPHONE FROM ANYWHERE, the one binding brainstorm.js
     had that is worth more than the button: a capture you cannot find the
     control for is what makes people stop trusting the feature. */
  useEffect(() => {
    if (!micLive) return;
    const esc = (ev: KeyboardEvent) => { if (ev.key === "Escape") stopMic(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [micLive, stopMic]);

  const toggleTts = useCallback(() => {
    setTts((on) => {
      const next = !on;
      try { localStorage.setItem(TTS_KEY, next ? "1" : "0"); } catch { /* private mode */ }
      if (!next) { try { client()?.voice.stopSpeaking(); } catch { /* nothing playing */ } }
      return next;
    });
  }, []);

  /** Read a reply out loud, if the toggle is on. Safe to call unconditionally —
   *  it is a no-op when TTS is off or the relay is unavailable, so the caller
   *  does not carry the condition. */
  const speak = useCallback((text: string) => {
    if (!tts || !status?.available || !text.trim()) return;
    try { void client()?.voice.speak(text); } catch { /* never breaks a turn */ }
  }, [tts, status]);

  return {
    status, tts, toggleTts, micLive, micState, heard, toggleMic, stopMic,
    speak, setOnFinal: (fn: (text: string) => void) => { onFinal.current = fn; },
  };
}
