/* Brainstorm workspace — the cheap room, with three surfaces and one Deploy.
 *
 * WHAT THIS IS. A chat thread, a writing pad and a drawing pad side by side,
 * plus a Deploy button that is deliberately TWO steps: synthesize proposes a
 * plan and writes nothing, and only an explicit confirm files it. Backed by
 * bgate_core/brainstorm.py through bgate_ui/routes/brainstorm.py.
 *
 * MOUNT CONTRACT — one call, from a seat module's render():
 *
 *     Brainstorm.mount(container, { seat: "director" });
 *     Brainstorm.mount(container, { seat: "narrative" });
 *
 * `seat` is the only parameter. It picks which sessions are listed, which seat
 * new sessions are created under, and what the server is allowed to file from
 * them — a narrative brainstorm may only propose narrative work, enforced
 * server-side in ALLOWED_SEATS, not here. There is no narrative fork of this
 * module and there must not be one; the two variants differ by a string and a
 * few labels, and the moment they are two files they start drifting on which
 * one remembered to flush the notes pad before deploying.
 *
 * WHY THE DRAWING PAD IS HAND-BUILT AND NOT EXCALIDRAW.
 *
 * Excalidraw was the ask, and the reason it is not here is measured, not
 * assumed. @excalidraw/excalidraw 0.18.1:
 *
 *   prod JS + CSS (index + 3 chunks)     2.80 MB
 *   prod/fonts (Excalifont, Nunito, …)  14.00 MB
 *   react 18.3.1 + react-dom UMD         0.14 MB
 *   -----------------------------------------------
 *   realistic vendored floor            ~17 MB, against a 3.6 MB static tree
 *
 * Size alone might be arguable. This is not: dist/prod/index.js is pure ESM
 * that imports twenty bare specifiers — @radix-ui/react-popover, jotai, pako,
 * perfect-freehand, nanoid, clsx, browser-fs-access, fractional-indexing and
 * the rest. A browser cannot resolve any of them without an import map over
 * the whole transitive tree or a bundler, and index.html's rule is "no build
 * step, no node, no CDN". React 18 still ships UMD; React 19 dropped it, so
 * even the shim route is already deprecated.
 *
 * So the pad below is purpose-built and emits EXCALIDRAW-SHAPED ELEMENTS —
 * typed shapes with real geometry, ids, labels and arrow bindings, never
 * strokes-as-pixels. That is what makes the model able to read the drawing:
 * brainstorm.drawing_digest() walks el.type / el.id / el.x,y,w,h /
 * el.text|el.label.text / el.startBinding.elementId and turns it into lines,
 * and everything this pad writes is in exactly those fields. Shapes carry
 * `label: {text}` rather than a separate bound-text element because that is
 * both what the digest reads AND what Excalidraw's own
 * convertToExcalidrawElements() accepts as input — so swapping in the real
 * thing later is a component swap, not a data migration. Same reason the scene
 * is wrapped as {type:"excalidraw", version:2, elements, appState}.
 *
 * The model can write elements BACK through PATCH /api/brainstorm/{id}
 * {drawing:{...}}; this pad renders whatever the session holds, so an agent
 * that adds a box is just a reload away from being visible.
 *
 * Vanilla JS, no deps. Self-contained: injects its own <style>, touches no
 * other module's DOM, and uses window.Split (split.js) and window.BGIcon only
 * if they are present.
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------------
   * Escaping. Everything that reaches innerHTML goes through esc() first —
   * session titles, model errors, plan briefs and the human's own notes are
   * all untrusted as far as markup is concerned.
   * ------------------------------------------------------------------ */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function icon(name, size) {
    return (window.BGIcon && BGIcon.has(name)) ? BGIcon(name, { size: size || 15 }) : "";
  }

  /* Tool glyphs the icon set genuinely does not have (a rectangle, a diamond,
     a scribble). Drawn on the set's own 24x24 grid at its 1.75 stroke weight
     rather than borrowed from a symbol font — the unicode-pictograph ban in
     tests/test_icons.py is pointing AT geometry, not away from it. */
  function shapeIcon(body, size) {
    return '<svg class="bgi" width="' + (size || 15) + '" height="' + (size || 15) +
      '" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" ' +
      'stroke-width="1.75" stroke-linecap="square" stroke-linejoin="miter">' + body + "</svg>";
  }
  var TOOL_ICON = {
    select: '<path d="M5 3.5 L5 18 L9 14 L11.5 19.5 L14 18.5 L11.5 13.5 L17 13 Z"/>',
    rectangle: '<rect x="3.5" y="5.5" width="17" height="13" rx="1"/>',
    ellipse: '<ellipse cx="12" cy="12" rx="8.5" ry="6.5"/>',
    diamond: '<path d="M12 3.5 L20.5 12 L12 20.5 L3.5 12 Z"/>',
    arrow: '<path d="M4 20 L20 4"/><path d="M13 4 H20 V11"/>',
    line: '<path d="M4 20 L20 4"/>',
    freedraw: '<path d="M3.5 16.5 C7 9 9 18.5 12 12 C14.5 6.5 17 15 20.5 8.5"/>',
    text: '<path d="M5 6.5 V4.5 H19 V6.5"/><path d="M12 4.5 V19.5"/><path d="M8.5 19.5 H15.5"/>'
  };

  /* =====================================================================
   * THE ADAPTER. Every network call the workspace makes lives in this one
   * object, so re-pointing it at a different route prefix or envelope is a
   * single edit rather than a hunt through the render code.
   *
   * Envelope, from bgate_ui/api.py: success is {ok:true, data:…, …extra} and
   * failure is {ok:false, error:{code,message,detail}}. call() unwraps data
   * and throws an Error carrying .status/.code/.detail so callers can tell a
   * 409 "already filed" from a 503 "no API key".
   * ================================================================== */
  var API = {
    base: "/api/brainstorm",

    async call(path, opts) {
      opts = opts || {};
      var init = { method: opts.method || "GET" };
      if (opts.body !== undefined) {
        init.headers = { "Content-Type": "application/json" };
        init.body = JSON.stringify(opts.body);
      }
      var res, body;
      try {
        res = await fetch(this.base + path, init);
      } catch (e) {
        throw Object.assign(new Error("the dashboard is not reachable"), { status: 0 });
      }
      try { body = await res.json(); } catch (e) { body = null; }
      if (!res.ok || !body || body.ok === false) {
        var err = (body && body.error) || {};
        throw Object.assign(
          new Error(err.message || (res.status + " " + res.statusText) || "request failed"),
          { status: res.status, code: err.code || "error", detail: err.detail || {},
            extra: body || {} });
      }
      // `data` is the payload; page-level extras (seats, model) ride beside it.
      var data = body.data;
      if (data && typeof data === "object" && !Array.isArray(data)) {
        Object.keys(body).forEach(function (k) {
          if (k !== "ok" && k !== "data" && data[k] === undefined) data[k] = body[k];
        });
      }
      return data;
    },

    list(seat, status) {
      var q = "?seat=" + encodeURIComponent(seat);
      if (status) q += "&status=" + encodeURIComponent(status);
      return this.call(q);
    },
    create(seat, title) { return this.call("", { method: "POST", body: { seat: seat, title: title || "" } }); },
    read(id) { return this.call("/" + id); },
    patch(id, body) { return this.call("/" + id, { method: "PATCH", body: body }); },
    archive(id, archived) { return this.call("/" + id + "/archive", { method: "POST", body: { archived: !!archived } }); },
    remove(id) { return this.call("/" + id, { method: "DELETE" }); },
    message(id, text) { return this.call("/" + id + "/message", { method: "POST", body: { text: text } }); },
    // Ends the PARTNER PROCESS, not the session. See close_partner in
    // bgate_core/brainstorm.py for the three words kept apart: close is about
    // the process, archive is about the document, deployed is a status.
    close(id) { return this.call("/" + id + "/close", { method: "POST", body: {} }); },
    synthesize(id) { return this.call("/" + id + "/synthesize", { method: "POST", body: {} }); },
    deploy(id, plan, again) { return this.call("/" + id + "/deploy", { method: "POST", body: { plan: plan, again: !!again } }); }
  };

  /* =====================================================================
   * VOICE — talking to the agent, and hearing it answer.
   *
   * WHY THE MIC TALKS TO OUR OWN SERVER AND NOT TO DEEPGRAM. Deepgram's
   * realtime socket is normally opened by the client holding the API key. That
   * would put DEEPGRAM_API_KEY in this file, in the devtools console and in the
   * DOM. So the browser streams to /api/voice/listen on the dashboard and the
   * SERVER relays — see bgate_ui/routes/voice.py for the full argument,
   * including why Deepgram's short-lived /v1/auth/grant token lost. Nothing in
   * this module has ever seen a provider key and there is no field it could
   * arrive in.
   *
   * THE AUDIO CONTRACT IS THE SERVER'S. /api/voice/status serves encoding,
   * sample_rate and channels, and Mic honours whatever comes back rather than
   * hardcoding 16000 in two places that can drift. Wrong rate is the classic
   * failure here and it does not error — it transcribes as chipmunks.
   *
   * WHY interim VS final MATTERS. Deepgram sends three grades of result and
   * conflating them makes the feature unusable:
   *   final=false        a guess, revised as you keep talking. PAINT it.
   *   final=true         this segment is settled; the sentence may continue.
   *   speech_final=true  the human stopped. THIS is what becomes a message.
   * Sending on `final` instead would fire a chat message per clause.
   *
   * THE MIC BEING LIVE IS NEVER SUBTLE. An always-listening panel with no
   * signal is a privacy problem wearing a feature's clothes, so a hot mic gets
   * a full-width bar over the composer, a pulsing dot, the words being heard,
   * and a button that has visibly changed state. Every exit path — stop,
   * Escape, a socket error, unmounting the workspace, switching sessions —
   * goes through Mic.stop(), which stops the MediaStream tracks so the
   * browser's own recording indicator goes out too.
   * ================================================================== */

  /* The capture worklet, as source. A Blob URL rather than a file under
     static/: index.html's rule is no build step, and this is 12 lines that
     belong next to the code that posts its output. addModule can still fail
     (older engines, a locked-down context), so _startWorklet falls back to the
     deprecated ScriptProcessorNode rather than losing the feature. */
  var WORKLET_SRC =
    'class C extends AudioWorkletProcessor{' +
    'constructor(){super();this.b=[];this.n=0}' +
    'process(inputs){var ch=inputs[0]&&inputs[0][0];if(!ch)return true;' +
    'this.b.push(new Float32Array(ch));this.n+=ch.length;' +
    /* ~2048 samples a post: small enough that interim results feel immediate,
       large enough that we are not posting 375 messages a second. */
    'if(this.n>=2048){var out=new Float32Array(this.n),o=0;' +
    'for(var i=0;i<this.b.length;i++){out.set(this.b[i],o);o+=this.b[i].length}' +
    'this.port.postMessage(out,[out.buffer]);this.b=[];this.n=0}' +
    'return true}}' +
    'registerProcessor("bs-capture",C);';

  /* Float32 [-1,1] -> little-endian signed 16-bit PCM, which is what
     `encoding=linear16` means. Clamped rather than wrapped: a sample over 1.0
     that wraps becomes a loud click, and Deepgram hears the click. */
  function toPCM16(f32) {
    var out = new Int16Array(f32.length);
    for (var i = 0; i < f32.length; i++) {
      var s = f32[i] < -1 ? -1 : f32[i] > 1 ? 1 : f32[i];
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  /* Linear resample. Only used when the AudioContext refused the sample rate we
     asked for — Chrome honours {sampleRate}, some engines quietly do not, and
     sending 48k audio labelled 16k is the chipmunk bug. */
  function resample(f32, from, to) {
    if (from === to) return f32;
    var ratio = from / to, len = Math.floor(f32.length / ratio);
    var out = new Float32Array(len);
    for (var i = 0; i < len; i++) {
      var pos = i * ratio, lo = Math.floor(pos), hi = Math.min(lo + 1, f32.length - 1);
      out[i] = f32[lo] + (f32[hi] - f32[lo]) * (pos - lo);
    }
    return out;
  }

  var Voice = {
    status: null,

    /** What the server says it can do. Never throws — a dashboard without the
        voice route at all must degrade the same way a missing key does. */
    async load() {
      try {
        var res = await fetch("/api/voice/status");
        var body = await res.json();
        this.status = (body && body.data) || null;
      } catch (e) {
        this.status = null;
      }
      if (!this.status) {
        this.status = { available: false, websockets: false, key: false,
          reason: "the dashboard has no voice endpoint - this build predates it, " +
                  "or bgate_ui/routes/voice.py failed to import (see /api/routes/status)" };
      }
      return this.status;
    },

    /** Speak `text` WHOLE, in as many requests as the cap needs.
     *
     * WHAT CHANGED AND WHY. This used to truncate at the first sentence
     * boundary before Deepgram's 2000-character cap and speak only that. That
     * was defensible against a small chat model told to answer in two short
     * paragraphs; it is not defensible against a Claude Code session, which
     * routinely writes past the cap — the human would hear a confident,
     * grammatical answer that simply stopped having a second half, with nothing
     * on screen saying so. A written reply that is cut off is visibly cut off;
     * a spoken one is not.
     *
     * So the text is SPLIT at sentence boundaries and the pieces are spoken in
     * order, each its own request. Never mid-word and never mid-sentence: the
     * cap is a transport limit and it must not become an editorial one.
     */
    async speak(text, model) {
      text = String(text || "").trim();
      if (!text) return "";
      var max = (this.status && this.status.max_speak_chars) || 2000;
      var parts = chunkForSpeech(text, max);
      this.stopSpeaking();
      this._speakToken = (this._speakToken || 0) + 1;
      var token = this._speakToken;
      for (var i = 0; i < parts.length; i++) {
        // A new reply, or a Stop, invalidates everything still queued. Without
        // this a long answer keeps talking over the next one for a minute.
        if (token !== this._speakToken) return "";
        var why = await this._speakOne(parts[i], model, i > 0);
        if (why) return why;
      }
      return "";
    },

    async _speakOne(text, model, queued) {
      var res;
      try {
        res = await fetch("/api/voice/speak", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text, model: model || undefined })
        });
      } catch (e) { return "the dashboard is not reachable"; }
      if (!res.ok) {
        var why = "";
        try { why = ((await res.json()).error || {}).message || ""; } catch (e) {}
        return why || ("speech failed (" + res.status + ")");
      }
      var blob = await res.blob();
      var url = URL.createObjectURL(blob);
      var audio = new Audio(url);
      this._audio = audio;
      // AWAITED TO COMPLETION, which is the whole reason this is its own
      // method. play() resolves the moment playback STARTS, so a loop over
      // chunks that only awaited play() would fire every request at once and
      // the listener would hear the last sentence over the first.
      var done = new Promise(function (resolve) {
        // Revoke on every path or a long reply leaks a blob per chunk.
        audio.addEventListener("ended", function () {
          URL.revokeObjectURL(url); resolve("");
        });
        audio.addEventListener("error", function () {
          URL.revokeObjectURL(url); resolve("");
        });
        audio.addEventListener("pause", function () {
          // Stop, or the next reply arriving. Resolve rather than hang.
          if (!audio.ended) { URL.revokeObjectURL(url); resolve(""); }
        });
      });
      try { await audio.play(); } catch (e) {
        URL.revokeObjectURL(url);
        return queued ? "" :
          "the browser blocked autoplay - click the page once, then try again";
      }
      await done;
      return "";
    },

    /** Stop talking, and CANCEL anything still queued behind it. Bumping the
     *  token is the half that matters: pausing the current chunk of a
     *  six-chunk reply without it just lets chunk two start. */
    stopSpeaking() {
      this._speakToken = (this._speakToken || 0) + 1;
      if (this._audio) { try { this._audio.pause(); } catch (e) {} this._audio = null; }
    }
  };

  /** Split prose into speakable pieces no longer than `max`.
   *
   * Sentence boundaries first, then line breaks, then — only for a single
   * "sentence" that is itself over the cap, which in practice means a pasted
   * URL or a code block — a hard cut. Never mid-word if a space is available.
   */
  function chunkForSpeech(text, max) {
    var out = [];
    var rest = String(text || "").trim();
    max = Math.max(200, max || 2000);
    while (rest.length > max) {
      var head = rest.slice(0, max);
      var stop = Math.max(head.lastIndexOf(". "), head.lastIndexOf("! "),
        head.lastIndexOf("? "), head.lastIndexOf(".\n"), head.lastIndexOf("\n\n"));
      if (stop < max * 0.4) stop = head.lastIndexOf("\n");
      if (stop < max * 0.4) stop = head.lastIndexOf(" ");
      if (stop < max * 0.4) stop = max - 1;
      out.push(rest.slice(0, stop + 1).trim());
      rest = rest.slice(stop + 1).trim();
    }
    if (rest) out.push(rest);
    return out;
  }

  /**
   * One live microphone session. Owns the MediaStream, the AudioContext and the
   * websocket, and releases all three in stop() — which is idempotent, because
   * it is called from the stop button, from Escape, from socket errors and from
   * the workspace being torn down, sometimes two of those at once.
   *
   * handlers: { interim(text), final(text), state(name, detail), error(msg) }
   */
  function Mic(handlers) {
    this.on = handlers || {};
    this.live = false;
    this.buffer = "";   // finalised segments of the sentence in progress
  }

  Mic.prototype._state = function (name, detail) {
    if (this.on.state) this.on.state(name, detail || "");
  };

  Mic.prototype.start = async function () {
    if (this.live) return;
    var self = this;
    this.buffer = "";
    this._state("starting");

    var want = (Voice.status && Voice.status.audio) || {};
    var rate = want.sample_rate || 16000;

    try {
      // Asked for one channel at the server's rate. The browser may give us
      // neither; both are checked below rather than assumed.
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
      });
    } catch (e) {
      // NotAllowedError is a decision, not a fault, and it needs a different
      // sentence from "you have no microphone".
      this._state("off");
      this.on.error && this.on.error(
        e && e.name === "NotAllowedError"
          ? "the browser blocked microphone access - allow it for this page and try again"
          : e && e.name === "NotFoundError"
            ? "no microphone found on this machine"
            : "could not open the microphone: " + ((e && e.name) || e));
      return;
    }

    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      try { this.ctx = new Ctx({ sampleRate: rate }); }
      catch (e) { this.ctx = new Ctx(); }
      this.source = this.ctx.createMediaStreamSource(this.stream);
      await this._open(rate);
      await this._capture(rate);
    } catch (e) {
      this.stop();
      this.on.error && this.on.error("could not start listening: " + (e && e.message || e));
      return;
    }
    this.live = true;
  };

  Mic.prototype._open = function (rate) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var proto = location.protocol === "https:" ? "wss://" : "ws://";
      var ws = new WebSocket(proto + location.host + "/api/voice/listen");
      ws.binaryType = "arraybuffer";
      self.ws = ws;
      var settled = false;

      ws.addEventListener("open", function () {
        // The dashboard token as the FIRST frame, not a query parameter: a
        // query string lands in the access log. The WebSocket API cannot set
        // headers, which is the only reason this is not the X-Bgate-Token every
        // other call carries.
        ws.send(JSON.stringify({ token: window.BGATE_TOKEN || "" }));
      });

      ws.addEventListener("message", function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }

        if (msg.type === "ready") {
          settled = true;
          self._state("live");
          resolve(msg);
          return;
        }
        if (msg.type === "unavailable" || msg.type === "error") {
          if (!settled) { settled = true; reject(new Error(msg.reason || "voice refused")); }
          else { self.on.error && self.on.error(msg.reason || "the voice relay stopped"); self.stop(); }
          return;
        }
        if (msg.type === "closed") {
          // The bill for the turn, from the only side that can count it.
          self._state("cost", msg);
          return;
        }
        if (msg.type === "Results" || msg.type === "UtteranceEnd") {
          self._heard(msg);
        }
      });

      ws.addEventListener("error", function () {
        if (!settled) { settled = true; reject(new Error("the voice relay is not reachable")); }
      });
      ws.addEventListener("close", function () {
        if (!settled) { settled = true; reject(new Error("the voice relay closed the connection")); }
        else if (self.live) { self.stop(); }
      });
      // A relay that never answers must not leave the mic light on forever.
      setTimeout(function () {
        if (!settled) { settled = true; reject(new Error("the voice relay did not answer")); }
      }, 8000);
    });
  };

  Mic.prototype._heard = function (msg) {
    var text = String(msg.text || "");
    if (msg.type === "Results" && text) {
      if (msg.final) this.buffer = (this.buffer + " " + text).trim();
      else if (this.on.interim) this.on.interim((this.buffer + " " + text).trim());
    }
    // speech_final (or the UtteranceEnd backstop) is the ONLY thing that sends.
    // See the section docstring — firing on `final` would post a message per
    // clause and make the thread unreadable.
    if (msg.speech_final) {
      var whole = this.buffer.trim();
      this.buffer = "";
      if (this.on.interim) this.on.interim("");
      if (whole && this.on.final) this.on.final(whole);
    }
  };

  Mic.prototype._capture = async function (rate) {
    var self = this;
    var actual = this.ctx.sampleRate;
    var send = function (f32) {
      if (!self.ws || self.ws.readyState !== 1) return;
      var pcm = toPCM16(resample(f32, actual, rate));
      // bufferedAmount is the backpressure signal the browser gives us. If the
      // socket is already a second behind, dropping this frame is better than
      // queueing audio the human has moved on from — the transcript loses a
      // syllable; the alternative loses the whole conversation to lag.
      if (self.ws.bufferedAmount > rate * 2) return;
      self.ws.send(pcm.buffer);
    };

    try {
      var url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "text/javascript" }));
      await this.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      var node = new AudioWorkletNode(this.ctx, "bs-capture");
      node.port.onmessage = function (ev) { send(ev.data); };
      this.source.connect(node);
      // Not connected to destination: routing the mic to the speakers is
      // feedback, and a worklet runs whether or not its output goes anywhere.
      this.node = node;
      return;
    } catch (e) { /* fall through */ }

    // ScriptProcessorNode is deprecated and universally implemented. It is the
    // fallback rather than the default because it runs on the main thread and
    // stutters under a busy drawing pad — which is exactly what this workspace
    // has next to the chat.
    var proc = this.ctx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = function (ev) { send(ev.inputBuffer.getChannelData(0)); };
    this.source.connect(proc);
    // This one DOES need a destination edge or it never fires; a zero gain
    // keeps the microphone out of the speakers.
    var mute = this.ctx.createGain();
    mute.gain.value = 0;
    proc.connect(mute);
    mute.connect(this.ctx.destination);
    this.node = proc;
    this.mute = mute;
  };

  /** Release everything. Safe to call twice, and from any of the five paths
      that need it. The MediaStream tracks are stopped LAST and explicitly —
      closing the AudioContext alone leaves the browser's recording indicator
      lit, which tells the user they are still being listened to. */
  Mic.prototype.stop = function () {
    this.live = false;
    if (this.ws) {
      try { if (this.ws.readyState === 1) this.ws.send(JSON.stringify({ type: "CloseStream" })); } catch (e) {}
      try { this.ws.close(); } catch (e) {}
      this.ws = null;
    }
    try { if (this.node) this.node.disconnect(); } catch (e) {}
    try { if (this.mute) this.mute.disconnect(); } catch (e) {}
    try { if (this.source) this.source.disconnect(); } catch (e) {}
    try { if (this.ctx) this.ctx.close(); } catch (e) {}
    if (this.stream) {
      this.stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) {} });
      this.stream = null;
    }
    this.node = this.mute = this.source = this.ctx = null;
    this.buffer = "";
    this._state("off");
  };

  /* =====================================================================
   * SCENE MODEL — Excalidraw-shaped elements.
   * ================================================================== */
  var SCENE_TYPE = "excalidraw";
  var SOURCE = "builders-gate/brainstorm";

  function uid() {
    // Eight chars, because drawing_digest() truncates ids to eight when it
    // names an element to the model — longer ids just read as noise there.
    var s = "";
    while (s.length < 8) s += Math.random().toString(36).slice(2);
    return s.slice(0, 8);
  }

  function emptyScene() {
    return {
      type: SCENE_TYPE, version: 2, source: SOURCE,
      elements: [],
      appState: { viewBackgroundColor: "transparent", gridSize: null }
    };
  }

  function normaliseScene(scene) {
    if (!scene || typeof scene !== "object") return emptyScene();
    var out = emptyScene();
    if (Array.isArray(scene.elements)) {
      out.elements = scene.elements.filter(function (e) {
        return e && typeof e === "object" && !e.isDeleted;
      });
    }
    if (scene.appState && typeof scene.appState === "object") {
      out.appState = Object.assign(out.appState, scene.appState);
    }
    return out;
  }

  /* A full element in Excalidraw's runtime shape. The fields the digest reads
     are load-bearing; the rest are here so a real Excalidraw can load this
     scene without a migration. */
  function makeElement(type, props) {
    return Object.assign({
      id: uid(), type: type,
      x: 0, y: 0, width: 0, height: 0, angle: 0,
      strokeColor: "#c9c6c1", backgroundColor: "transparent",
      fillStyle: "solid", strokeWidth: 2, strokeStyle: "solid", roughness: 1,
      opacity: 100, groupIds: [], frameId: null, roundness: null,
      seed: Math.floor(Math.random() * 2147483647), version: 1,
      versionNonce: Math.floor(Math.random() * 2147483647),
      isDeleted: false, boundElements: null, updated: Date.now(),
      link: null, locked: false
    }, props || {});
  }

  function labelOf(el) {
    if (!el) return "";
    if (el.type === "text") return String(el.text || "");
    if (el.label && typeof el.label === "object") return String(el.label.text || "");
    return "";
  }
  function setLabel(el, text) {
    text = String(text == null ? "" : text);
    if (el.type === "text") { el.text = text; el.originalText = text; }
    else if (text) { el.label = { text: text }; }
    else { delete el.label; }
    el.version = (el.version || 1) + 1;
    el.updated = Date.now();
  }

  var LINEAR = { arrow: 1, line: 1, freedraw: 1 };
  function isLinear(el) { return !!LINEAR[el && el.type]; }

  /* Axis-aligned bounds in scene space, for hit-testing and arrow binding. */
  function bounds(el) {
    if (isLinear(el) && Array.isArray(el.points) && el.points.length) {
      var xs = el.points.map(function (p) { return el.x + p[0]; });
      var ys = el.points.map(function (p) { return el.y + p[1]; });
      return { x1: Math.min.apply(null, xs), y1: Math.min.apply(null, ys),
               x2: Math.max.apply(null, xs), y2: Math.max.apply(null, ys) };
    }
    var w = el.width || 0, h = el.height || 0;
    return { x1: Math.min(el.x, el.x + w), y1: Math.min(el.y, el.y + h),
             x2: Math.max(el.x, el.x + w), y2: Math.max(el.y, el.y + h) };
  }

  function textSize(text, fontSize) {
    var lines = String(text || "").split("\n");
    var longest = lines.reduce(function (m, l) { return Math.max(m, l.length); }, 0);
    // Approximate: no canvas measureText round-trip for a value that only has
    // to be close enough for a bounding box and the digest's "WxH".
    return { width: Math.max(12, longest * fontSize * 0.55),
             height: Math.max(fontSize * 1.25, lines.length * fontSize * 1.25) };
  }

  /* =====================================================================
   * THE PAD. An SVG surface over the element list.
   * ================================================================== */
  function Pad(host, onChange) {
    this.host = host;
    this.onChange = onChange || function () {};
    this.scene = emptyScene();
    this.tool = "select";
    this.selected = null;
    this.view = { x: 0, y: 0, scale: 1 };
    this.undoStack = [];
    this.drag = null;
    this._build();
  }

  Pad.prototype._build = function () {
    var self = this;
    this.host.innerHTML =
      '<div class="bs-pad">' +
        '<div class="bs-pad-tools" role="toolbar" aria-label="Drawing tools"></div>' +
        '<div class="bs-pad-surface">' +
          '<svg class="bs-svg" xmlns="http://www.w3.org/2000/svg"></svg>' +
          '<input class="bs-pad-input" hidden aria-label="Element label">' +
        '</div>' +
        '<div class="bs-pad-foot">' +
          '<span class="bs-pad-count"></span>' +
          '<span class="bs-pad-hint">drag to draw · double-click to label · Delete removes</span>' +
        '</div>' +
      '</div>';
    this.svg = this.host.querySelector(".bs-svg");
    this.input = this.host.querySelector(".bs-pad-input");
    this.countEl = this.host.querySelector(".bs-pad-count");

    var tools = ["select", "rectangle", "ellipse", "diamond", "arrow", "line", "freedraw", "text"];
    var bar = this.host.querySelector(".bs-pad-tools");
    bar.innerHTML =
      tools.map(function (t) {
        return '<button class="bs-tool" data-tool="' + t + '" title="' + esc(t) +
          '" aria-label="' + esc(t) + '">' + shapeIcon(TOOL_ICON[t]) + "</button>";
      }).join("") +
      '<span class="bs-pad-sep"></span>' +
      '<button class="bs-tool bs-act" data-act="undo" title="Undo">' + shapeIcon('<path d="M8 6.5 L3.5 11 L8 15.5"/><path d="M3.5 11 H14 A6 6 0 0 1 14 23"/>') + "</button>" +
      '<button class="bs-tool bs-act" data-act="fit" title="Fit to content">' + shapeIcon('<path d="M3.5 8.5 V3.5 H8.5 M15.5 3.5 H20.5 V8.5 M20.5 15.5 V20.5 H15.5 M8.5 20.5 H3.5 V15.5"/>') + "</button>" +
      '<button class="bs-tool bs-act" data-act="clear" title="Clear the board">' + shapeIcon('<path d="M5 6.5 H19"/><path d="M9.5 6.5 V4.5 H14.5 V6.5"/><path d="M6.5 6.5 L7.5 20 H16.5 L17.5 6.5"/>') + "</button>";

    bar.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn) return;
      if (btn.dataset.tool) { self.setTool(btn.dataset.tool); return; }
      if (btn.dataset.act === "undo") self.undo();
      if (btn.dataset.act === "fit") { self.fit(); self.render(); }
      if (btn.dataset.act === "clear") {
        if (!self.scene.elements.length) return;
        self.snapshot();
        self.scene.elements = [];
        self.selected = null;
        self.commit();
      }
    });

    this.svg.addEventListener("pointerdown", this._down.bind(this));
    this.svg.addEventListener("pointermove", this._move.bind(this));
    this.svg.addEventListener("pointerup", this._up.bind(this));
    this.svg.addEventListener("pointercancel", this._up.bind(this));
    this.svg.addEventListener("dblclick", this._dbl.bind(this));
    this.svg.addEventListener("wheel", this._wheel.bind(this), { passive: false });

    // Keys are bound on the pad, not the document: a Delete pressed while the
    // notes pad has focus must delete a character, not a shape.
    this.host.setAttribute("tabindex", "-1");
    this.host.addEventListener("keydown", this._key.bind(this));

    this.input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); self._commitLabel(); }
      if (ev.key === "Escape") { ev.preventDefault(); self._cancelLabel(); }
      ev.stopPropagation();
    });
    this.input.addEventListener("blur", function () { self._commitLabel(); });

    this.setTool("select");
    this._ro = new ResizeObserver(function () { self.render(); });
    this._ro.observe(this.host);
  };

  Pad.prototype.destroy = function () { if (this._ro) this._ro.disconnect(); };

  Pad.prototype.setTool = function (tool) {
    this.tool = tool;
    if (tool !== "select") this.selected = null;
    this.host.querySelectorAll(".bs-tool[data-tool]").forEach(function (b) {
      b.classList.toggle("on", b.dataset.tool === tool);
    });
    this.svg.style.cursor = tool === "select" ? "default" : "crosshair";
    this.render();
  };

  Pad.prototype.load = function (scene) {
    this.scene = normaliseScene(scene);
    this.selected = null;
    this.undoStack = [];
    this.fit();
    this.render();
  };

  Pad.prototype.snapshot = function () {
    this.undoStack.push(JSON.stringify(this.scene.elements));
    if (this.undoStack.length > 40) this.undoStack.shift();
  };
  Pad.prototype.undo = function () {
    var prev = this.undoStack.pop();
    if (prev == null) return;
    try { this.scene.elements = JSON.parse(prev); } catch (e) { return; }
    this.selected = null;
    this.render();
    this.onChange(this.scene);
  };
  Pad.prototype.commit = function () { this.render(); this.onChange(this.scene); };

  /* Screen -> scene coordinates. */
  Pad.prototype.pt = function (ev) {
    var r = this.svg.getBoundingClientRect();
    return { x: this.view.x + (ev.clientX - r.left) / this.view.scale,
             y: this.view.y + (ev.clientY - r.top) / this.view.scale };
  };

  Pad.prototype.hit = function (p, skipId) {
    // Back to front: the thing drawn last is the thing you meant to click.
    for (var i = this.scene.elements.length - 1; i >= 0; i--) {
      var el = this.scene.elements[i];
      if (el.id === skipId) continue;
      var b = bounds(el);
      var pad = isLinear(el) ? 8 : 0;
      if (p.x >= b.x1 - pad && p.x <= b.x2 + pad && p.y >= b.y1 - pad && p.y <= b.y2 + pad) return el;
    }
    return null;
  };
  Pad.prototype.byId = function (id) {
    return this.scene.elements.filter(function (e) { return e.id === id; })[0] || null;
  };

  Pad.prototype._handleAt = function (p) {
    var el = this.selected && this.byId(this.selected);
    if (!el || isLinear(el)) return false;
    var b = bounds(el), t = 10 / this.view.scale;
    return Math.abs(p.x - b.x2) < t && Math.abs(p.y - b.y2) < t;
  };

  Pad.prototype._down = function (ev) {
    if (ev.button !== 0) return;
    this.host.focus({ preventScroll: true });
    var p = this.pt(ev);
    try { this.svg.setPointerCapture(ev.pointerId); } catch (e) {}

    if (this.tool === "select") {
      if (this._handleAt(p)) {
        this.snapshot();
        this.drag = { mode: "resize", id: this.selected, from: p };
        return;
      }
      var el = this.hit(p);
      if (el) {
        this.selected = el.id;
        this.snapshot();
        this.drag = { mode: "move", id: el.id, from: p, ox: el.x, oy: el.y };
      } else {
        this.selected = null;
        this.drag = { mode: "pan", from: p, vx: this.view.x, vy: this.view.y,
                      sx: ev.clientX, sy: ev.clientY };
      }
      this.render();
      return;
    }

    if (this.tool === "text") {
      this.snapshot();
      var t = makeElement("text", { x: p.x, y: p.y, text: "", originalText: "",
        fontSize: 20, fontFamily: 1, textAlign: "left", verticalAlign: "top",
        containerId: null, lineHeight: 1.25, width: 12, height: 25 });
      this.scene.elements.push(t);
      this.selected = t.id;
      this.render();
      this._editLabel(t, true);
      return;
    }

    this.snapshot();
    var born;
    if (this.tool === "freedraw") {
      born = makeElement("freedraw", { x: p.x, y: p.y, points: [[0, 0]],
        pressures: [], simulatePressure: true, lastCommittedPoint: null });
    } else if (this.tool === "arrow" || this.tool === "line") {
      born = makeElement(this.tool, { x: p.x, y: p.y, points: [[0, 0], [0, 0]],
        lastCommittedPoint: null, startBinding: null, endBinding: null,
        startArrowhead: null, endArrowhead: this.tool === "arrow" ? "arrow" : null });
    } else {
      born = makeElement(this.tool, { x: p.x, y: p.y, width: 0, height: 0,
        roundness: this.tool === "rectangle" ? { type: 3 } : null });
    }
    this.scene.elements.push(born);
    this.selected = born.id;
    this.drag = { mode: "create", id: born.id, from: p };
    this.render();
  };

  Pad.prototype._move = function (ev) {
    if (!this.drag) return;
    var p = this.pt(ev);
    var el = this.drag.id ? this.byId(this.drag.id) : null;

    if (this.drag.mode === "pan") {
      this.view.x = this.drag.vx - (ev.clientX - this.drag.sx) / this.view.scale;
      this.view.y = this.drag.vy - (ev.clientY - this.drag.sy) / this.view.scale;
      this.render();
      return;
    }
    if (!el) return;

    if (this.drag.mode === "move") {
      var dx = p.x - this.drag.from.x, dy = p.y - this.drag.from.y;
      el.x = this.drag.ox + dx; el.y = this.drag.oy + dy;
      this._reflow(el.id);
    } else if (this.drag.mode === "resize") {
      el.width = Math.max(8, p.x - el.x);
      el.height = Math.max(8, p.y - el.y);
      this._reflow(el.id);
    } else if (this.drag.mode === "create") {
      if (el.type === "freedraw") {
        el.points.push([p.x - el.x, p.y - el.y]);
      } else if (isLinear(el)) {
        el.points[1] = [p.x - el.x, p.y - el.y];
      } else {
        el.x = Math.min(this.drag.from.x, p.x);
        el.y = Math.min(this.drag.from.y, p.y);
        el.width = Math.abs(p.x - this.drag.from.x);
        el.height = Math.abs(p.y - this.drag.from.y);
      }
    }
    el.version = (el.version || 1) + 1;
    this.render();
  };

  Pad.prototype._up = function (ev) {
    if (!this.drag) return;
    var mode = this.drag.mode, id = this.drag.id;
    this.drag = null;
    try { this.svg.releasePointerCapture(ev.pointerId); } catch (e) {}
    var el = id ? this.byId(id) : null;

    if (mode === "create" && el) {
      var b = bounds(el);
      // A click with the rectangle tool is a mis-click, not a zero-size shape.
      if (el.type !== "text" && (b.x2 - b.x1) < 4 && (b.y2 - b.y1) < 4) {
        this.scene.elements = this.scene.elements.filter(function (e) { return e.id !== id; });
        this.selected = null;
        this.undoStack.pop();
        this.render();
        return;
      }
      if (el.type === "freedraw" || el.type === "line" || el.type === "arrow") {
        el.width = b.x2 - b.x1; el.height = b.y2 - b.y1;
        el.lastCommittedPoint = el.points[el.points.length - 1];
      }
      if (el.type === "arrow" || el.type === "line") this._bind(el);
      // Back to select after one shape: the alternative is a board full of
      // rectangles from a user who did not notice the tool was still armed.
      this.setTool("select");
    }
    if (mode !== "pan") this.commit(); else this.render();
  };

  /* Arrow endpoints that land on a shape BIND to it. This is the field
     drawing_digest() reads to tell the model "hub -> shrine" instead of
     "there is a line somewhere". */
  Pad.prototype._bind = function (arrow) {
    var a = { x: arrow.x + arrow.points[0][0], y: arrow.y + arrow.points[0][1] };
    var z = { x: arrow.x + arrow.points[1][0], y: arrow.y + arrow.points[1][1] };
    var s = this.hit(a, arrow.id), e = this.hit(z, arrow.id);
    arrow.startBinding = s && !isLinear(s) ? { elementId: s.id, focus: 0, gap: 4 } : null;
    arrow.endBinding = e && !isLinear(e) && (!s || e.id !== s.id)
      ? { elementId: e.id, focus: 0, gap: 4 } : null;
  };

  /* A bound arrow follows the shape it points at. Without this, moving a box
     leaves its arrows behind and the digest keeps claiming a relationship the
     picture no longer shows. */
  Pad.prototype._reflow = function (movedId) {
    var self = this;
    this.scene.elements.forEach(function (el) {
      if (el.type !== "arrow" && el.type !== "line") return;
      var sb = el.startBinding && el.startBinding.elementId;
      var eb = el.endBinding && el.endBinding.elementId;
      if (sb !== movedId && eb !== movedId) return;
      var src = sb ? self.byId(sb) : null, dst = eb ? self.byId(eb) : null;
      var a = src ? centre(src) : { x: el.x + el.points[0][0], y: el.y + el.points[0][1] };
      var z = dst ? centre(dst) : { x: el.x + el.points[1][0], y: el.y + el.points[1][1] };
      if (src) a = edgePoint(src, z);
      if (dst) z = edgePoint(dst, a);
      el.x = a.x; el.y = a.y;
      el.points = [[0, 0], [z.x - a.x, z.y - a.y]];
      el.width = Math.abs(z.x - a.x); el.height = Math.abs(z.y - a.y);
    });
  };

  function centre(el) {
    var b = bounds(el);
    return { x: (b.x1 + b.x2) / 2, y: (b.y1 + b.y2) / 2 };
  }
  /* Where a ray from the shape's centre toward `to` leaves its box. */
  function edgePoint(el, to) {
    var b = bounds(el), c = centre(el);
    var dx = to.x - c.x, dy = to.y - c.y;
    if (!dx && !dy) return c;
    var hw = Math.max(1, (b.x2 - b.x1) / 2 + 4), hh = Math.max(1, (b.y2 - b.y1) / 2 + 4);
    var t = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
    return { x: c.x + dx * t, y: c.y + dy * t };
  }

  Pad.prototype._dbl = function (ev) {
    var el = this.hit(this.pt(ev));
    if (!el) return;
    ev.preventDefault();
    this.selected = el.id;
    this.render();
    this._editLabel(el, false);
  };

  Pad.prototype._editLabel = function (el, isNew) {
    var b = bounds(el), r = this.svg.getBoundingClientRect();
    var host = this.host.querySelector(".bs-pad-surface").getBoundingClientRect();
    var sx = (b.x1 - this.view.x) * this.view.scale + (r.left - host.left);
    var sy = (b.y1 - this.view.y) * this.view.scale + (r.top - host.top);
    var w = Math.max(90, (b.x2 - b.x1) * this.view.scale);
    this.input.hidden = false;
    this.input.style.left = sx + "px";
    this.input.style.top = sy + "px";
    this.input.style.width = w + "px";
    this.input.value = labelOf(el);
    this.input.placeholder = el.type === "text" ? "text…" : "name this shape…";
    this._editing = { id: el.id, isNew: !!isNew };
    this.input.focus();
    this.input.select();
  };

  Pad.prototype._commitLabel = function () {
    var ed = this._editing;
    if (!ed) return;
    this._editing = null;
    this.input.hidden = true;
    var el = this.byId(ed.id);
    if (!el) return;
    var text = this.input.value.trim();
    if (el.type === "text" && !text) {
      // An empty text element is an invisible click target nobody can find.
      this.scene.elements = this.scene.elements.filter(function (e) { return e.id !== ed.id; });
      this.selected = null;
      this.commit();
      return;
    }
    setLabel(el, text);
    if (el.type === "text") {
      var m = textSize(text, el.fontSize || 20);
      el.width = m.width; el.height = m.height;
    }
    this.commit();
  };

  Pad.prototype._cancelLabel = function () {
    var ed = this._editing;
    if (!ed) return;
    this._editing = null;
    this.input.hidden = true;
    if (ed.isNew) {
      this.scene.elements = this.scene.elements.filter(function (e) { return e.id !== ed.id; });
      this.selected = null;
      this.commit();
    }
  };

  Pad.prototype._key = function (ev) {
    if (this._editing) return;
    if (ev.key === "Delete" || ev.key === "Backspace") {
      if (!this.selected) return;
      var id = this.selected;
      ev.preventDefault();
      this.snapshot();
      this.scene.elements = this.scene.elements.filter(function (e) {
        // An arrow bound to a deleted shape keeps a binding that names nothing,
        // and the digest would report an edge to a box that is gone.
        if (e.id === id) return false;
        if (e.startBinding && e.startBinding.elementId === id) e.startBinding = null;
        if (e.endBinding && e.endBinding.elementId === id) e.endBinding = null;
        return true;
      });
      this.selected = null;
      this.commit();
      return;
    }
    if (ev.key === "Escape") { this.selected = null; this.setTool("select"); return; }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); this.undo(); }
  };

  Pad.prototype._wheel = function (ev) {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {
      var r = this.svg.getBoundingClientRect();
      var before = this.pt(ev);
      this.view.scale = Math.max(0.2, Math.min(4, this.view.scale * (ev.deltaY < 0 ? 1.1 : 1 / 1.1)));
      var after = { x: this.view.x + (ev.clientX - r.left) / this.view.scale,
                    y: this.view.y + (ev.clientY - r.top) / this.view.scale };
      this.view.x += before.x - after.x;
      this.view.y += before.y - after.y;
    } else {
      this.view.x += ev.deltaX / this.view.scale;
      this.view.y += ev.deltaY / this.view.scale;
    }
    this.render();
  };

  Pad.prototype.fit = function () {
    var r = this.svg.getBoundingClientRect();
    var w = r.width || 400, h = r.height || 300;
    if (!this.scene.elements.length) { this.view = { x: 0, y: 0, scale: 1 }; return; }
    var b = this.scene.elements.map(bounds).reduce(function (a, c) {
      return { x1: Math.min(a.x1, c.x1), y1: Math.min(a.y1, c.y1),
               x2: Math.max(a.x2, c.x2), y2: Math.max(a.y2, c.y2) };
    });
    var pad = 40;
    var scale = Math.min(w / Math.max(1, b.x2 - b.x1 + pad * 2),
                         h / Math.max(1, b.y2 - b.y1 + pad * 2), 1.6);
    this.view.scale = Math.max(0.2, scale);
    this.view.x = b.x1 - pad - (w / this.view.scale - (b.x2 - b.x1 + pad * 2)) / 2;
    this.view.y = b.y1 - pad - (h / this.view.scale - (b.y2 - b.y1 + pad * 2)) / 2;
  };

  Pad.prototype.render = function () {
    var r = this.svg.getBoundingClientRect();
    var w = (r.width || 400) / this.view.scale, h = (r.height || 300) / this.view.scale;
    this.svg.setAttribute("viewBox", this.view.x + " " + this.view.y + " " + w + " " + h);
    var self = this;
    var parts = ['<defs><marker id="bs-ah" viewBox="0 0 10 10" refX="9" refY="5" ' +
      'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
      '<path d="M0 0 L10 5 L0 10" fill="none" stroke="context-stroke" stroke-width="2"/>' +
      "</marker></defs>"];
    this.scene.elements.forEach(function (el) { parts.push(self._draw(el)); });
    var sel = this.selected && this.byId(this.selected);
    if (sel) {
      var b = bounds(sel), m = 5 / this.view.scale;
      parts.push('<rect class="bs-sel" x="' + (b.x1 - m) + '" y="' + (b.y1 - m) +
        '" width="' + (b.x2 - b.x1 + m * 2) + '" height="' + (b.y2 - b.y1 + m * 2) +
        '" vector-effect="non-scaling-stroke"/>');
      if (!isLinear(sel)) {
        var hs = 8 / this.view.scale;
        parts.push('<rect class="bs-grip" x="' + (b.x2 - hs / 2) + '" y="' + (b.y2 - hs / 2) +
          '" width="' + hs + '" height="' + hs + '" vector-effect="non-scaling-stroke"/>');
      }
    }
    this.svg.innerHTML = parts.join("");
    var n = this.scene.elements.length;
    this.countEl.textContent = n
      ? n + " element" + (n === 1 ? "" : "s") + " · the director reads these as text"
      : "empty board";
  };

  Pad.prototype._draw = function (el) {
    var b = bounds(el), body = "";
    var stroke = 'vector-effect="non-scaling-stroke"';
    if (el.type === "rectangle") {
      body = '<rect class="bs-shape" x="' + b.x1 + '" y="' + b.y1 + '" width="' + (b.x2 - b.x1) +
        '" height="' + (b.y2 - b.y1) + '" rx="6" ' + stroke + "/>";
    } else if (el.type === "ellipse") {
      body = '<ellipse class="bs-shape" cx="' + ((b.x1 + b.x2) / 2) + '" cy="' + ((b.y1 + b.y2) / 2) +
        '" rx="' + Math.max(1, (b.x2 - b.x1) / 2) + '" ry="' + Math.max(1, (b.y2 - b.y1) / 2) + '" ' + stroke + "/>";
    } else if (el.type === "diamond") {
      var cx = (b.x1 + b.x2) / 2, cy = (b.y1 + b.y2) / 2;
      body = '<path class="bs-shape" d="M' + cx + " " + b.y1 + " L" + b.x2 + " " + cy +
        " L" + cx + " " + b.y2 + " L" + b.x1 + " " + cy + ' Z" ' + stroke + "/>";
    } else if (el.type === "freedraw") {
      body = '<polyline class="bs-stroke" points="' +
        (el.points || []).map(function (p) { return (el.x + p[0]) + "," + (el.y + p[1]); }).join(" ") +
        '" ' + stroke + "/>";
    } else if (el.type === "arrow" || el.type === "line") {
      var p0 = el.points[0] || [0, 0], p1 = el.points[1] || [0, 0];
      body = '<line class="bs-stroke" x1="' + (el.x + p0[0]) + '" y1="' + (el.y + p0[1]) +
        '" x2="' + (el.x + p1[0]) + '" y2="' + (el.y + p1[1]) + '" ' + stroke +
        (el.type === "arrow" ? ' marker-end="url(#bs-ah)"' : "") + "/>";
    } else if (el.type === "text") {
      var fs = el.fontSize || 20;
      body = String(el.text || "").split("\n").map(function (line, i) {
        return '<text class="bs-text" x="' + el.x + '" y="' + (el.y + fs * (i + 1)) +
          '" style="font-size:' + fs + 'px">' + esc(line) + "</text>";
      }).join("");
    } else {
      // An unknown type came from somewhere else (an agent, a future
      // Excalidraw). Draw its box rather than dropping it silently.
      body = '<rect class="bs-shape bs-unknown" x="' + b.x1 + '" y="' + b.y1 +
        '" width="' + Math.max(4, b.x2 - b.x1) + '" height="' + Math.max(4, b.y2 - b.y1) + '" ' + stroke + "/>";
    }
    var label = el.type === "text" ? "" : labelOf(el);
    if (label) {
      body += '<text class="bs-label" x="' + ((b.x1 + b.x2) / 2) + '" y="' + ((b.y1 + b.y2) / 2) +
        '" text-anchor="middle" dominant-baseline="central">' + esc(label) + "</text>";
    }
    return body;
  };

  /* =====================================================================
   * A very small markdown renderer for the notes preview and for assistant
   * replies. ESCAPES FIRST, then adds markup — the input is a model reply and
   * the human's own pad, and neither is trusted to be inert.
   * ================================================================== */
  function md(text) {
    var out = esc(text).replace(/\r/g, "");
    out = out.replace(/```([\s\S]*?)```/g, function (_m, code) {
      return "<pre><code>" + code.replace(/^\n/, "") + "</code></pre>";
    });
    out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    out = out.replace(/^###\s+(.+)$/gm, "<h4>$1</h4>")
             .replace(/^##\s+(.+)$/gm, "<h3>$1</h3>")
             .replace(/^#\s+(.+)$/gm, "<h3>$1</h3>");
    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
    out = out.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
    out = out.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
    return "<p>" + out + "</p>";
  }

  function ago(when) {
    var t = String(when || "");
    if (!t) return "";
    var ms = Date.parse(t.replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(t) ? "" : "Z"));
    if (!isFinite(ms)) return "";
    var s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 45) return "just now";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    if (s < 172800) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function toast(msg, bad) {
    if (window.BGWS && BGWS.toast) return BGWS.toast(msg, bad);
    var t = document.getElementById("bs-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "bs-toast";
      t.style.cssText = "position:fixed;bottom:18px;left:50%;transform:translateX(-50%);" +
        "z-index:9999;padding:9px 16px;border-radius:10px;font-size:13px;" +
        "background:var(--surface-3);border:1px solid var(--line);transition:opacity .3s";
      document.body.appendChild(t);
    }
    t.style.color = bad ? "var(--bad)" : "var(--good)";
    t.textContent = msg;
    t.style.opacity = "1";
    clearTimeout(t._to);
    t._to = setTimeout(function () { t.style.opacity = "0"; }, 2600);
  }

  /* =====================================================================
   * STYLE. Injected once, custom properties only — a vanta-black glassy
   * theme is landing and any hardcoded colour here would survive it.
   * ================================================================== */
  var STYLE_ID = "bs-style";
  var CSS = `
.bs{position:relative;display:flex;flex-direction:column;height:100%;min-height:560px;
  color:var(--text);font-size:13px}
.bs *{box-sizing:border-box}

/* ---- top bar ---- */
.bs-bar{display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--surface-1);
  border:1px solid var(--line);border-radius:12px 12px 0 0;border-bottom:0;flex:none;flex-wrap:wrap}
.bs-bar .bs-title{font-weight:var(--fw-semi,600);font-size:14px;background:transparent;border:1px solid transparent;
  color:var(--text);padding:4px 8px;border-radius:8px;min-width:140px;flex:1;max-width:420px;font-family:inherit}
.bs-bar .bs-title:hover{border-color:var(--line)}
.bs-bar .bs-title:focus{border-color:var(--accent-line);outline:none;background:var(--surface-2)}
.bs-spacer{flex:1}
.bs-pill{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;
  border:1px solid var(--line);background:var(--surface-2);color:var(--text-3);font-size:11px;white-space:nowrap}
.bs-pill.seat{color:var(--text-2);border-color:var(--line-strong)}
/* The partner chip is a button when it can do something, and reads as a plain
   pill when it cannot — a disabled control that still looks pressable is how
   "did that work?" starts. */
.bs-partner{cursor:pointer;font:inherit}
.bs-partner[disabled]{cursor:default;opacity:.75}
.bs-partner.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}


/* The writing pad's toolbar. Sticky so it survives a long document, and it
   inserts markdown into the textarea below rather than owning a document of
   its own — see NotePad. */
.bs-mdbar{display:flex;flex-wrap:wrap;align-items:center;gap:2px;padding:5px 7px;
  background:var(--solid-1);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
.bs-mdb{min-width:26px;height:24px;padding:0 6px;border:1px solid transparent;
  border-radius:5px;background:none;color:var(--text-2);cursor:pointer;
  font:12px/1 var(--sans,inherit);display:inline-flex;align-items:center;justify-content:center}
.bs-mdb:hover:not([disabled]){background:var(--solid-2);color:var(--text-1);border-color:var(--line)}
.bs-mdb[disabled]{opacity:.35;cursor:default}
.bs-mdb b,.bs-mdb i,.bs-mdb s{font-style:normal;font-weight:700}
.bs-mdb i{font-style:italic;font-weight:600}
.bs-mdb s{text-decoration:line-through;font-weight:600}
.bs-mdsep{width:1px;height:16px;background:var(--line);margin:0 4px}
.bs-pill.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}
.bs-pill.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}
.bs-pill.bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}

.bs-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border-radius:9px;
  border:1px solid var(--line);background:var(--surface-2);color:var(--text-2);font:inherit;
  font-size:12px;cursor:pointer;white-space:nowrap}
.bs-btn:hover{background:var(--surface-3);color:var(--text);border-color:var(--line-strong)}
.bs-btn:disabled{opacity:.45;cursor:not-allowed}
.bs-btn.on{background:var(--surface-4);color:var(--text);border-color:var(--line-strong)}
.bs-btn.ghost{background:transparent}
.bs-btn.danger:hover{color:var(--bad);border-color:var(--bad-line)}
.bs-btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg);font-weight:var(--fw-semi,600)}
.bs-btn.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:var(--accent-fg)}
.bs-btn.primary:disabled:hover{background:var(--accent);border-color:var(--accent)}
.bs-deploy{position:relative}
.bs-deploy small{display:block;font-size:9.5px;opacity:.8;letter-spacing:.03em;font-weight:400;line-height:1}

/* ---- body: drawer + panes ---- */
.bs-body{flex:1;min-height:0;display:grid;grid-template-columns:0 minmax(0,1fr);
  border:1px solid var(--line);border-radius:0 0 12px 12px;overflow:hidden;
  transition:grid-template-columns .16s ease}
.bs-body.drawer-open{grid-template-columns:246px minmax(0,1fr)}

.bs-drawer{min-width:0;overflow:hidden auto;background:var(--surface-1);border-right:1px solid var(--line);
  display:flex;flex-direction:column}
.bs-drawer-h{display:flex;align-items:center;gap:6px;padding:9px 10px;border-bottom:1px solid var(--line-soft);
  position:sticky;top:0;background:var(--surface-1);z-index:2}
.bs-drawer-h .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);flex:1}
.bs-filter{display:flex;gap:4px;padding:8px 10px 4px}
.bs-filter button{flex:1;padding:4px 0;font-size:11px;border-radius:7px;border:1px solid var(--line);
  background:transparent;color:var(--text-3);cursor:pointer;font-family:inherit}
.bs-filter button.on{background:var(--surface-3);color:var(--text);border-color:var(--line-strong)}
.bs-list{list-style:none;margin:0;padding:6px}
.bs-list li{padding:8px 9px;border-radius:9px;cursor:pointer;border:1px solid transparent;margin-bottom:2px}
.bs-list li:hover{background:var(--surface-2)}
.bs-list li.on{background:var(--surface-3);border-color:var(--accent-line)}
.bs-list .t{display:block;font-size:12.5px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bs-list .m{display:flex;gap:7px;font-size:10.5px;color:var(--text-3);margin-top:3px;align-items:center}
.bs-list li.arch .t{color:var(--text-3);text-decoration:line-through}
.bs-dot{width:6px;height:6px;border-radius:50%;flex:none;background:var(--line-strong)}
.bs-dot.deployed{background:var(--good)}
.bs-dot.open{background:var(--accent)}

/* ---- the three panes ---- */
.bs-panes{min-width:0;display:grid;
  grid-template-columns:var(--bs-chat-w,1.15fr) 7px var(--bs-notes-w,1fr) 7px minmax(240px,1fr);
  background:var(--bg)}
.bs-pane{min-width:0;display:flex;flex-direction:column;overflow:hidden;background:var(--surface-2)}
.bs-pane + .split + .bs-pane{border-left:0}
.bs-ph{display:flex;align-items:center;gap:7px;padding:8px 11px;border-bottom:1px solid var(--line-soft);
  background:var(--surface-1);flex:none;min-height:37px}
.bs-ph .n{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-2);font-weight:var(--fw-semi,600)}
.bs-ph .bs-spacer{flex:1}
.bs-ph .sub{font-size:10.5px;color:var(--text-3)}

.split{background:var(--line);cursor:col-resize;position:relative;flex:none}
.split:hover,.split.dragging{background:var(--accent-line)}
.split::after{content:"";position:absolute;inset:0 -3px;cursor:col-resize}

/* ---- chat ---- */
.bs-thread{flex:1;min-height:0;overflow:auto;padding:14px 14px 4px;display:flex;flex-direction:column;gap:12px}
.bs-msg{max-width:88%;display:flex;flex-direction:column;gap:3px}
.bs-msg.user{align-self:flex-end;align-items:flex-end}
.bs-msg .who{font-size:9.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3)}
.bs-msg .bub{padding:9px 12px;border-radius:12px;border:1px solid var(--line);background:var(--surface-3);
  line-height:1.55;overflow-wrap:anywhere}
.bs-msg.user .bub{background:var(--accent-soft);border-color:var(--accent-line)}
.bs-msg .bub p{margin:0 0 8px}.bs-msg .bub p:last-child{margin:0}
.bs-msg .bub ul{margin:4px 0;padding-left:18px}
.bs-msg .bub code{background:var(--surface-1);padding:1px 4px;border-radius:4px;font-size:12px}
.bs-msg .bub pre{background:var(--surface-1);padding:8px 10px;border-radius:8px;overflow:auto;margin:6px 0}
.bs-msg .bub pre code{background:none;padding:0}
.bs-msg.err .bub{border-color:var(--bad-line);background:var(--bad-soft);color:var(--bad)}
.bs-typing span{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--text-3);
  margin-right:4px;animation:bs-blink 1.2s infinite}
.bs-typing span:nth-child(2){animation-delay:.2s}.bs-typing span:nth-child(3){animation-delay:.4s}
@keyframes bs-blink{0%,60%,100%{opacity:.25}30%{opacity:1}}

.bs-compose{flex:none;border-top:1px solid var(--line-soft);padding:9px 11px;background:var(--surface-1)}
.bs-compose textarea{width:100%;resize:none;min-height:56px;max-height:180px;padding:9px 11px;border-radius:10px;
  border:1px solid var(--line);background:var(--surface-2);color:var(--text);font:inherit;font-size:13px;line-height:1.5}
.bs-compose textarea:focus{outline:none;border-color:var(--accent-line)}
.bs-cfoot{display:flex;align-items:center;gap:8px;margin-top:7px;font-size:10.5px;color:var(--text-3);flex-wrap:wrap}

/* ---- voice ----
   THE HOT-MIC BAR IS OPAQUE, AND THAT IS THE ORBIT RULE, NOT A PREFERENCE:
   anything you read is opaque, transparency only where a blur actually
   renders. This bar is read at a glance while somebody talks over a drawing
   pad, and a translucent panel over a #000 ground with shapes moving behind it
   is exactly where "is the mic on?" becomes unanswerable. */
.bs-voice{display:none;align-items:center;gap:9px;margin-bottom:8px;padding:8px 11px;
  border-radius:10px;border:1px solid var(--bad-line);background:var(--surface-1);
  color:var(--text-2);font-size:12px;line-height:1.45}
.bs-voice.on{display:flex}
.bs-voice .heard{flex:1;min-width:0;color:var(--text);overflow-wrap:anywhere}
.bs-voice .heard:empty::before{content:"listening…";color:var(--text-3)}
/* A dot that moves. A static red square reads as decoration; the animation is
   the part the eye catches from the drawing pad. */
.bs-live{width:9px;height:9px;border-radius:50%;flex:none;background:var(--bad);
  animation:bs-pulse 1.15s ease-in-out infinite}
@keyframes bs-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.72)}}
@media (prefers-reduced-motion:reduce){
  /* Reduced motion must not mean "no signal". The ring stays, the movement goes. */
  .bs-live{animation:none;box-shadow:0 0 0 3px var(--bad-soft)}
}
.bs-mic.hot{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
.bs-voice-why{color:var(--text-3);font-size:10.5px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;max-width:100%}
/* The cheapness of this room is a UI claim as much as an architectural one. */
.bs-cheap{display:inline-flex;align-items:center;gap:5px;color:var(--text-3);font-size:10.5px}
.bs-cheap b{color:var(--text-2);font-weight:var(--fw-semi,600)}

/* ---- notes ---- */
.bs-notes-area{flex:1;min-height:0;width:100%;resize:none;border:0;background:var(--surface-2);
  color:var(--text);font:inherit;font-size:13px;line-height:1.65;padding:14px;outline:none}
.bs-notes-prev{flex:1;min-height:0;overflow:auto;padding:14px;line-height:1.65}
.bs-notes-prev h3,.bs-notes-prev h4{margin:14px 0 6px;font-size:14px}
.bs-notes-prev ul{padding-left:20px}
.bs-notes-prev code{background:var(--surface-1);padding:1px 4px;border-radius:4px}
.bs-notes-prev pre{background:var(--surface-1);padding:9px 11px;border-radius:8px;overflow:auto}

/* ---- drawing pad ---- */
.bs-pad{flex:1;min-height:0;display:flex;flex-direction:column}
.bs-pad-tools{display:flex;gap:2px;padding:6px 8px;border-bottom:1px solid var(--line-soft);
  background:var(--surface-1);flex-wrap:wrap;align-items:center}
.bs-tool{width:27px;height:27px;display:inline-flex;align-items:center;justify-content:center;
  border-radius:7px;border:1px solid transparent;background:transparent;color:var(--text-3);cursor:pointer;padding:0}
.bs-tool:hover{background:var(--surface-3);color:var(--text)}
.bs-tool.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent)}
.bs-pad-sep{width:1px;height:18px;background:var(--line);margin:0 5px}
.bs-pad-surface{flex:1;min-height:0;position:relative;overflow:hidden;
  background-image:radial-gradient(var(--grid-dot) 1px,transparent 1px);background-size:22px 22px}
.bs-svg{position:absolute;inset:0;width:100%;height:100%;touch-action:none;display:block}
.bs-shape{fill:var(--surface-3);stroke:var(--text-2);stroke-width:1.6}
.bs-shape.bs-unknown{stroke-dasharray:4 3;fill:none}
.bs-stroke{fill:none;stroke:var(--text-2);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.bs-text{fill:var(--text);font-family:inherit}
.bs-label{fill:var(--text);font-size:12px;font-family:inherit;paint-order:stroke;
  stroke:var(--surface-3);stroke-width:3px;stroke-linejoin:round}
.bs-sel{fill:none;stroke:var(--accent);stroke-width:1.2;stroke-dasharray:4 3}
.bs-grip{fill:var(--accent);stroke:var(--accent-fg);stroke-width:1}
.bs-pad-input{position:absolute;z-index:4;padding:4px 7px;border-radius:7px;border:1px solid var(--accent);
  background:var(--surface-1);color:var(--text);font:inherit;font-size:13px;outline:none}
.bs-pad-foot{flex:none;display:flex;gap:10px;padding:5px 11px;border-top:1px solid var(--line-soft);
  background:var(--surface-1);font-size:10.5px;color:var(--text-3)}
.bs-pad-hint{margin-left:auto;text-align:right}

/* The Agents-view host. Its layout lives here rather than inline in index.html:
   an inline display declaration outranks the UA sheet's [hidden]{display:none},
   so an inline flex left this 630px tall while hidden and pushed the cockpit off
   the bottom of the view. Paired with a [hidden] rule so the attribute wins —
   the same trap atlas_code.js:103 and audiolab.js:341 already record.
   (No backticks in this comment: it sits inside a template literal.) */

/* ---- the deploy review sheet ---- */
.bs-sheet{position:absolute;inset:0;z-index:30;display:flex;align-items:center;justify-content:center;
  padding:24px;background:var(--overlay);backdrop-filter:blur(3px)}
.bs-sheet[hidden]{display:none}
.bs-card{width:min(860px,100%);max-height:100%;display:flex;flex-direction:column;
  background:var(--surface-2);border:1px solid var(--line-strong);border-radius:14px;box-shadow:var(--shadow-4);overflow:hidden}
.bs-card-h{padding:15px 20px;border-bottom:1px solid var(--line);background:var(--surface-1);flex:none}
.bs-card-h h3{margin:0 0 3px;font-size:15px;display:flex;align-items:center;gap:8px}
/* The one thing the user must not misread. It is a banner, not a footnote. */
.bs-safe{display:flex;align-items:center;gap:8px;margin-top:9px;padding:8px 12px;border-radius:9px;
  background:var(--good-soft);border:1px solid var(--good-line);color:var(--good);font-size:12px}
.bs-safe.armed{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent)}
.bs-safe.done{background:var(--good-soft);border-color:var(--good-line);color:var(--good)}
.bs-card-b{padding:16px 20px;overflow:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:14px}
.bs-sec-h{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);margin-bottom:6px}
.bs-summary{line-height:1.6;color:var(--text-2)}
.bs-qs{margin:0;padding-left:20px;color:var(--warn);line-height:1.6}
.bs-notes-list{margin:0;padding-left:20px;color:var(--text-3);font-size:12px;line-height:1.6}
.bs-item{border:1px solid var(--line);border-radius:11px;background:var(--surface-1);padding:11px 13px;margin-bottom:9px}
.bs-item-h{display:flex;align-items:center;gap:8px;margin-bottom:7px;flex-wrap:wrap}
.bs-item-n{width:21px;height:21px;flex:none;border-radius:6px;background:var(--surface-4);color:var(--text-2);
  display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-variant-numeric:tabular-nums}
.bs-item select,.bs-item input,.bs-item textarea{background:var(--surface-2);border:1px solid var(--line);
  color:var(--text);font:inherit;font-size:12.5px;border-radius:7px;padding:5px 8px}
.bs-item input.ti{flex:1;min-width:150px;font-weight:var(--fw-semi,600)}
.bs-item textarea{width:100%;resize:vertical;min-height:62px;line-height:1.55;font-size:12px;color:var(--text-2)}
.bs-item select:focus,.bs-item input:focus,.bs-item textarea:focus{outline:none;border-color:var(--accent-line)}
.bs-chain{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:9px;background:var(--surface-1);
  border:1px solid var(--line);color:var(--text-2);font-size:12px}
.bs-chain input{accent-color:var(--accent)}
.bs-card-f{padding:12px 20px;border-top:1px solid var(--line);background:var(--surface-1);flex:none;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bs-card-f .bs-spacer{flex:1}
.bs-filed{display:flex;flex-direction:column;gap:7px}
.bs-filed a,.bs-filed span.f{display:flex;align-items:center;gap:9px;padding:8px 11px;border-radius:9px;
  background:var(--surface-1);border:1px solid var(--good-line);color:var(--text);text-decoration:none;font-size:12.5px}
.bs-filed .id{color:var(--good);font-variant-numeric:tabular-nums;font-weight:var(--fw-semi,600)}

.bs-empty{padding:26px 20px;text-align:center;color:var(--text-3);font-size:12.5px;line-height:1.7}
.bs-empty b{display:block;color:var(--text-2);font-size:13.5px;margin-bottom:5px}
.bs-spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line-strong);
  border-top-color:var(--accent);border-radius:50%;animation:bs-spin .7s linear infinite}
@keyframes bs-spin{to{transform:rotate(360deg)}}

/* 1280px is the floor this has to work at. Below it the drawing pad is the
   first thing to fold — it is the surface you can still reach through the
   Deploy review, and a 200px chat is not a chat. */
@media (max-width:1150px){
  .bs-panes{grid-template-columns:var(--bs-chat-w,1.2fr) 7px minmax(220px,1fr) 0 0}
  .bs-panes > .bs-pane:last-child,.bs-panes > .split:last-of-type{display:none}
}
`;

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* =====================================================================
   * THE WORKSPACE
   * ================================================================== */
  var COPY = {
    director: {
      label: "Director brainstorm",
      blurb: "what to build",
      chatSub: "thinking out loud about pillars, the core loop and priorities",
      deployNote: "synthesises this session into work items for the board",
      empty: "Talk through an idea. When it holds together, Deploy turns it into work items you review before anything is filed."
    },
    narrative: {
      label: "Narrative brainstorm",
      blurb: "what is true",
      chatSub: "thinking out loud about canon, lore and consistency",
      deployNote: "synthesises this session into canon work - narrative seat only",
      empty: "Talk through the world. When it holds together, Deploy turns it into canon updates you review before anything is filed."
    }
  };

  /* =====================================================================
   * THE WRITING PAD — a toolbar over a textarea, and MARKDOWN STAYS THE STORE.
   *
   * WHY NOT A RICH-TEXT EDITOR. The notes column is text and three things read
   * it as markdown: the synthesis prompt, the preview button, and — since the
   * partner became a spawned session — pad_read on the scoped MCP server. A
   * contenteditable storing HTML would make all three worse, and the one that
   * matters most is the third: a model handed markdown reads a document, and a
   * model handed serialised HTML reads soup. So this inserts markdown INTO the
   * textarea and the textarea remains the truth. Typing `- ` by hand keeps
   * working, because nothing here intercepts typing.
   *
   * WHY NOT TIPTAP / PROSEMIRROR / QUILL. index.html states the rule and the
   * CSP enforces it: no build step, no npm, no CDN. Vendoring ProseMirror is
   * ~400KB of someone else's code into a repo that purpose-built its own
   * drawing pad for exactly this reason. Measured against what a scratchpad
   * needs, it is not close.
   *
   * WHAT WAS CUT FROM THE REFERENCE TOOLBAR, and why — it is another product's
   * list, not a specification:
   *   tables      markdown tables are painful to edit by hand in a textarea and
   *               a scratchpad is not where anyone lays out a grid. A table
   *               button that produces a skeleton nobody can then maintain is
   *               worse than no button.
   *   callouts    not standard markdown. It would render in our preview and
   *               nowhere else, including in the model's reading of the pad.
   *   import /    sessions ARE the document model here: the browser, archive
   *   export /    and new-session already exist and are backed by the DB. A
   *   new doc     second document concept beside brainstorm sessions is the
   *               thing this pad must not grow.
   * KEPT: bold, italic, strike, inline code, H1-H3, bullet/numbered/checklist,
   * quote, code block, link, rule. Each one is a line or a wrap, each one is
   * markdown somebody might have typed anyway.
   * ================================================================== */
  var NOTE_TOOLS = [
    { a: "b", t: "Bold", k: "Ctrl+B", ic: "bold", wrap: "**" },
    { a: "i", t: "Italic", k: "Ctrl+I", ic: "italic", wrap: "*" },
    { a: "s", t: "Strikethrough", ic: "strike", wrap: "~~" },
    { a: "code", t: "Inline code", ic: "code", wrap: "`" },
    { sep: true },
    { a: "h1", t: "Heading 1", ic: "h1", line: "# " },
    { a: "h2", t: "Heading 2", ic: "h2", line: "## " },
    { a: "h3", t: "Heading 3", ic: "h3", line: "### " },
    { sep: true },
    { a: "ul", t: "Bullet list", ic: "list", line: "- " },
    { a: "ol", t: "Numbered list", ic: "olist", line: "1. " },
    { a: "task", t: "Checklist", ic: "check", line: "- [ ] " },
    { sep: true },
    { a: "quote", t: "Quote", ic: "quote", line: "> " },
    { a: "fence", t: "Code block", ic: "block", block: "```" },
    { a: "link", t: "Link", k: "Ctrl+K", ic: "link" },
    { a: "hr", t: "Horizontal rule", ic: "rule", block: "---" }
  ];

  // Glyphs, not registry icons: the registry has no h1/h2/quote/strike and it
  // is another agent's file today. Text marks are what a writing toolbar uses
  // anyway — B and I read faster than any pictogram of them.
  var NOTE_GLYPH = {
    bold: "<b>B</b>", italic: "<i>I</i>", strike: "<s>S</s>", code: "&lt;/&gt;",
    h1: "H1", h2: "H2", h3: "H3", list: "•-", olist: "1.", check: "☑",
    quote: "❝", block: "▤", link: "🔗", rule: "―"
  };

  /** Markdown editing affordances over an existing textarea.
   *
   * Owns no state: every action reads the textarea, rewrites a slice of it, and
   * restores the selection. That is what keeps typed markdown and clicked
   * markdown the same thing — there is no document model to get out of step.
   */
  function NotePad(area, onChange) {
    var self = this;
    this.area = area;
    this.onChange = onChange || function () {};
    var bar = document.createElement("div");
    bar.className = "bs-mdbar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Writing pad formatting");
    bar.innerHTML = NOTE_TOOLS.map(function (t) {
      if (t.sep) return '<i class="bs-mdsep"></i>';
      return '<button type="button" class="bs-mdb" data-md="' + t.a + '" title="' +
        esc(t.t + (t.k ? " (" + t.k + ")" : "")) + '" aria-label="' + esc(t.t) +
        '">' + (NOTE_GLYPH[t.ic] || esc(t.a)) + "</button>";
    }).join("");
    area.parentNode.insertBefore(bar, area);
    this.bar = bar;
    bar.addEventListener("mousedown", function (ev) {
      // Stops the textarea losing its selection to the button. Without this
      // every action applies to a collapsed caret at position 0.
      if (ev.target.closest(".bs-mdb")) ev.preventDefault();
    });
    bar.addEventListener("click", function (ev) {
      var b = ev.target.closest(".bs-mdb");
      if (b) self.apply(b.dataset.md);
    });
    // SCOPED TO THE PAD. Bound on the textarea, not the document, so Ctrl+B in
    // the chat composer or over the drawing pad does nothing here — a shortcut
    // that fires into the wrong surface is worse than not having it.
    area.addEventListener("keydown", function (ev) {
      if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return;
      var k = String(ev.key || "").toLowerCase();
      var hit = k === "b" ? "b" : k === "i" ? "i" : k === "k" ? "link" : "";
      if (!hit) return;
      ev.preventDefault();
      self.apply(hit);
    });
  }

  NotePad.prototype.sync = function () {
    this.bar.querySelectorAll(".bs-mdb").forEach(function (b) {
      b.disabled = false;
    });
    if (this.area.disabled) {
      this.bar.querySelectorAll(".bs-mdb").forEach(function (b) { b.disabled = true; });
    }
  };

  NotePad.prototype.apply = function (action) {
    var t = NOTE_TOOLS.filter(function (x) { return x.a === action; })[0];
    if (!t || this.area.disabled) return;
    if (t.wrap) this._wrap(t.wrap);
    else if (t.line) this._line(t.line);
    else if (action === "fence") this._fence();
    else if (action === "hr") this._insert("\n---\n");
    else if (action === "link") this._link();
    this.area.focus();
    this.onChange();
  };

  NotePad.prototype._set = function (value, from, to) {
    this.area.value = value;
    this.area.setSelectionRange(from, to === undefined ? from : to);
  };

  /** Wrap the selection, or UNWRAP it if it is already wrapped. A bold button
   *  that can only ever add asterisks makes `****text****` on a double click. */
  NotePad.prototype._wrap = function (mark) {
    var a = this.area, s = a.selectionStart, e = a.selectionEnd, v = a.value;
    var sel = v.slice(s, e);
    var n = mark.length;
    if (sel.length >= n * 2 && sel.slice(0, n) === mark && sel.slice(-n) === mark) {
      var bare = sel.slice(n, -n);
      this._set(v.slice(0, s) + bare + v.slice(e), s, s + bare.length);
      return;
    }
    if (v.slice(s - n, s) === mark && v.slice(e, e + n) === mark) {
      this._set(v.slice(0, s - n) + sel + v.slice(e + n), s - n, e - n);
      return;
    }
    this._set(v.slice(0, s) + mark + sel + mark + v.slice(e),
      s + n, e + n);
  };

  /** Prefix every selected line, or strip the prefix if all of them have it.
   *  Numbered lists renumber; a "1." button that writes 1. four times is the
   *  kind of half-help that makes people stop using a toolbar. */
  NotePad.prototype._line = function (prefix) {
    var a = this.area, v = a.value;
    var s = v.lastIndexOf("\n", a.selectionStart - 1) + 1;
    var e = v.indexOf("\n", a.selectionEnd);
    if (e < 0) e = v.length;
    var lines = v.slice(s, e).split("\n");
    var ordered = /^\d+\. $/.test(prefix);
    var has = lines.every(function (l) {
      return ordered ? /^\d+\. /.test(l) : l.indexOf(prefix) === 0;
    });
    var out = lines.map(function (l, i) {
      if (has) return l.replace(ordered ? /^\d+\. / : prefix, "");
      // A line already carrying a DIFFERENT block prefix swaps rather than
      // stacks: "## - [ ] thing" is not what anybody meant by clicking H2.
      var bare = l.replace(/^(#{1,6} |> |- \[[ x]\] |- |\d+\. )/, "");
      return (ordered ? (i + 1) + ". " : prefix) + bare;
    }).join("\n");
    this._set(v.slice(0, s) + out + v.slice(e), s, s + out.length);
  };

  NotePad.prototype._fence = function () {
    var a = this.area, s = a.selectionStart, e = a.selectionEnd, v = a.value;
    var sel = v.slice(s, e) || "";
    var block = "```\n" + sel + "\n```\n";
    this._set(v.slice(0, s) + block + v.slice(e), s + 4, s + 4 + sel.length);
  };

  NotePad.prototype._insert = function (text) {
    var a = this.area, s = a.selectionStart, v = a.value;
    this._set(v.slice(0, s) + text + v.slice(a.selectionEnd), s + text.length);
  };

  NotePad.prototype._link = function () {
    var a = this.area, s = a.selectionStart, e = a.selectionEnd, v = a.value;
    var sel = v.slice(s, e);
    // No prompt(): a modal for a link is heavier than typing the brackets, and
    // the caret lands where the URL goes so it is one keystroke away anyway.
    var made = "[" + (sel || "text") + "](url)";
    var urlAt = s + made.length - 4;
    this._set(v.slice(0, s) + made + v.slice(e), urlAt, urlAt + 3);
  };

  /** A cheap "has this scene changed" fingerprint, for the browser's own use.
   *  Deliberately NOT the server's rev — nothing compares the two, and a client
   *  that had to agree with a server hash would be a second implementation of
   *  the same thing waiting to disagree. */
  function padRev(scene) {
    try { return JSON.stringify((scene && scene.elements) || []); }
    catch (e) { return ""; }
  }

  var ACTIVE = null;

  function Workspace(host, opts) {
    this.host = host;
    this.seat = (opts && opts.seat) === "narrative" ? "narrative" : "director";
    // ONE LAYOUT, and the cut-down variants that used to be here are gone.
    //
    // A `pads:false` mount and then a `chrome:"minimal"` mount were both built
    // for the Agents view and both removed, because the Agents view does not
    // want this module's UI at all — it already has a conversation (the
    // console) and what it wanted was for that conversation to be able to talk
    // to the brainstorm BACKEND instead of the dispatch one. Two chat surfaces
    // in one view was the wrong shape however much chrome was shaved off it.
    // See agents_console.js, which routes its own composer.
    //
    // The workspace lives in the director and narrative seats and is whole
    // there: sessions, both pads, archive, Deploy. A brainstorm started from
    // the Agents view is an ordinary director session and shows up in this
    // list, which is the property that keeps the two from becoming parallel
    // tracks.
    this.copy = COPY[this.seat];
    this.sessions = [];
    this.filter = "active";
    this.session = null;
    this.pad = null;
    this.sending = false;
    this.plan = null;
    this.saveTimers = {};
    this.modelInfo = null;
    // Speaking replies starts OFF. A workspace that begins talking the moment
    // it loads is a surprise in a shared room, and the button says which it is.
    this.tts = false;
    this.mic = null;
    this._build();
  }

  Workspace.prototype.destroy = function () {
    var self = this;
    Object.keys(this.saveTimers).forEach(function (k) { clearTimeout(self.saveTimers[k]); });
    // The PROCESS is deliberately NOT closed here. Navigating between views is
    // not "I am done thinking", and killing the partner on every tab change
    // would make the resume path pay for itself several times an hour. It ends
    // on the close button, on archive, on the kill switch, or on the idle reap.
    if (this.pad) this.pad.destroy();
    // The mic and the speaker outlive the DOM if nobody says otherwise: an
    // AudioContext and a MediaStream are held by this object, not by the node
    // tree, so navigating to another seat would leave the recording indicator
    // lit and a reply talking into an empty room.
    if (this.mic) { try { this.mic.stop(); } catch (e) {} }
    Voice.stopSpeaking();
    if (this._esc) document.removeEventListener("keydown", this._esc);
    this.dead = true;
  };

  Workspace.prototype.$ = function (sel) { return this.root.querySelector(sel); };

  Workspace.prototype._build = function () {
    injectStyle();
    var self = this;
    var c = this.copy;
    this.host.innerHTML = "";
    this.root = document.createElement("div");
    this.root.className = "bs";
    this.root.dataset.seat = this.seat;
    this.root.innerHTML =
      '<div class="bs-bar">' +
        '<button class="bs-btn ghost" data-a="drawer" title="Saved sessions">' +
          icon("sheet") + "<span>Sessions</span></button>" +
        '<input class="bs-title" data-a="title" placeholder="untitled brainstorm" disabled>' +
        '<span class="bs-pill seat">' + icon(this.seat) + esc(c.label) + "</span>" +
        '<span class="bs-pill" data-a="status">no session</span>' +
        '<span class="bs-spacer"></span>' +
        '<span class="bs-pill" data-a="model"></span>' +
        // THE PARTNER, and whether it is running. A process that only ever
        // ended on a 30-minute idle timer, an eviction or the kill switch was
        // invisible from here: there was no way to see it was on and no way to
        // turn it off. Chip says which; the button next to it is the answer.
        '<button class="bs-pill bs-partner" data-a="partner" hidden></button>' +
        '<button class="bs-btn ghost" data-a="archive" title="Archive this session" disabled>' + icon("lock") + "</button>" +
        '<button class="bs-btn primary bs-deploy" data-a="deploy" disabled title="' + esc(c.deployNote) + '">' +
          icon("gate", 16) + "<span>Deploy<small>review first</small></span></button>" +
      "</div>" +
      '<div class="bs-body">' +
        '<aside class="bs-drawer">' +
          '<div class="bs-drawer-h"><span class="lbl">' + esc(this.seat) + " sessions</span>" +
            '<button class="bs-btn ghost" data-a="new" title="New brainstorm">' + icon("edit") + "New</button></div>" +
          '<div class="bs-filter">' +
            '<button data-f="active" class="on">Active</button>' +
            '<button data-f="archived">Archived</button>' +
            '<button data-f="all">All</button></div>' +
          '<ul class="bs-list"></ul>' +
        "</aside>" +
        '<div class="bs-panes">' +
          '<section class="bs-pane bs-chat">' +
            '<div class="bs-ph">' + icon("agents") + '<span class="n">Chat</span>' +
              '<span class="sub">' + esc(c.chatSub) + "</span></div>" +
            '<div class="bs-thread"></div>' +
            '<div class="bs-compose">' +
              '<div class="bs-voice" data-a="voicebar" role="status" aria-live="polite">' +
                '<i class="bs-live"></i>' +
                '<span><b>Mic is live</b></span>' +
                '<span class="heard" data-a="heard"></span>' +
                '<button class="bs-btn" data-a="micoff">' + icon("stop", 13) + "Stop</button>" +
              "</div>" +
              '<textarea data-a="say" rows="2" placeholder="think out loud…" disabled></textarea>' +
              '<div class="bs-cfoot">' +
                '<button class="bs-btn bs-mic" data-a="mic" disabled>' +
                  icon("record", 14) + "<span>Talk</span></button>" +
                '<button class="bs-btn ghost" data-a="tts" disabled ' +
                  'title="Speak the agent’s replies out loud">' +
                  icon("mute", 13) + "<span>Silent</span></button>" +
                // THE FOOTGUN GUARD. In the Agents view this composer sits one
                // toggle away from the console's, which files a work item and
                // spawns an agent for every sentence. Two chat boxes with
                // opposite consequences need the difference stated in the box,
                // not in the tab above it.
                '<span class="bs-cheap">' + icon("spend", 13) +
                  "<b>files nothing</b> · thinking only - no work item, no " +
                  "dispatch, until you press Deploy</span>" +
                '<span class="bs-spacer" style="flex:1"></span>' +
                '<span class="bs-voice-why" data-a="voicewhy"></span>' +
                '<span data-a="sendhint">Enter to send · Shift+Enter for a new line</span>' +
              "</div></div>" +
          "</section>" +
          '<div class="split" data-split="bs-chat" data-split-var="--bs-chat-w" ' +
            'data-split-min="320" data-split-max="58%"></div>' +
          '<section class="bs-pane bs-notes">' +
            '<div class="bs-ph">' + icon("note") + '<span class="n">Writing pad</span>' +
              '<span class="bs-spacer"></span>' +
              '<span class="sub" data-a="notestate"></span>' +
              '<button class="bs-btn ghost" data-a="prev" title="Preview markdown">' + icon("overview", 13) + "</button></div>" +
            '<textarea class="bs-notes-area" data-a="notes" placeholder="notes, markdown welcome - autosaves" disabled></textarea>' +
            '<div class="bs-notes-prev" hidden></div>' +
          "</section>" +
          '<div class="split" data-split="bs-notes" data-split-var="--bs-notes-w" ' +
            'data-split-min="220" data-split-max="45%"></div>' +
          '<section class="bs-pane bs-draw">' +
            '<div class="bs-ph">' + icon("concept") + '<span class="n">Drawing pad</span>' +
              '<span class="bs-spacer"></span>' +
              '<span class="sub" data-a="drawstate"></span></div>' +
            '<div class="bs-padhost" style="flex:1;min-height:0;display:flex"></div>' +
          "</section>" +
          // NO TRANSCRIPT PANE HERE, deliberately. A pane rendering the
          // session's stream-json events was built and removed: asked for "the
          // actual terminal claude code embedded", it would have been a
          // different thing wearing the right label, which is worse than
          // nothing. A real interactive PTY is somebody else's feature. The raw
          // log path still rides in the payload as `thinker.log` for debugging.
        "</div>" +
      "</div>" +
      '<div class="bs-sheet" hidden></div>';
    this.host.appendChild(this.root);

    if (window.Split && Split.init) Split.init(this.root);

    this.root.addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-a],[data-f]");
      if (!b || !self.root.contains(b)) return;
      if (b.dataset.f) { self.setFilter(b.dataset.f); return; }
      var a = b.dataset.a;
      if (a === "drawer") self.toggleDrawer();
      if (a === "new") self.newSession();
      if (a === "deploy") self.openDeploy();
      if (a === "archive") self.toggleArchive();
      if (a === "prev") self.togglePreview();
      if (a === "mic") self.toggleMic();
      if (a === "micoff") self.stopMic();
      if (a === "tts") self.toggleTts();
      if (a === "partner") self.closePartner();
    });

    // Escape kills the mic from anywhere in the workspace. A hot mic you cannot
    // find the button for is the thing that makes people distrust the feature.
    this._esc = function (ev) {
      if (ev.key === "Escape" && self.mic && self.mic.live) self.stopMic();
    };
    document.addEventListener("keydown", this._esc);

    this.$('[data-a="title"]').addEventListener("change", function () { self.rename(this.value); });

    var say = this.$('[data-a="say"]');
    say.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); self.send(); }
    });
    say.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(180, this.scrollHeight) + "px";
    });

    var notes = this.$('[data-a="notes"]');
    notes.addEventListener("input", function () { self.queueSave("notes"); });
    this.notes = new NotePad(notes, function () { self.queueSave("notes"); });
    this.pad = new Pad(this.$(".bs-padhost"), function () { self.queueSave("drawing"); });

    this.renderThread();
    this.load();
    this.loadVoice();
  };

  /* ---- voice ----------------------------------------------------------
   * Everything below degrades to "typing still works". The controls are born
   * disabled and only the status call can enable them, so a dashboard with no
   * voice route, no Deepgram key or no websockets extra renders a labelled,
   * explained pair of dead buttons rather than a live one that fails on click.
   * That is the same contract the chat already has for a missing OPENAI_API_KEY
   * — 200 with reply:null, "your message is saved". */
  Workspace.prototype.loadVoice = async function () {
    var status = await Voice.load();
    if (this.dead) return;
    var mic = this.$('[data-a="mic"]'), tts = this.$('[data-a="tts"]');
    var why = this.$('[data-a="voicewhy"]');
    if (status.available) {
      mic.disabled = false;
      tts.disabled = false;
      mic.title = "Talk to the " + this.seat + " - Deepgram " +
        (status.listen_model || "") + ", ~$" +
        ((status.usd_per_minute || 0)).toFixed(4) + "/min while the mic is live";
      why.textContent = "";
      return;
    }
    // Disabled, and the reason is BOTH on screen and in the tooltip: the footer
    // truncates on a narrow pane and the sentence is the actionable part.
    mic.disabled = true;
    tts.disabled = true;
    mic.title = tts.title = status.reason || "voice is not configured";
    why.textContent = status.key === false
      ? "voice off · no Deepgram key"
      : "voice off · " + (status.websockets === false
          ? "websockets extra not installed" : "unavailable");
    why.title = status.reason || "";
  };

  Workspace.prototype.toggleTts = function () {
    this.tts = !this.tts;
    var b = this.$('[data-a="tts"]');
    b.classList.toggle("on", this.tts);
    b.innerHTML = icon(this.tts ? "audio" : "mute", 13) +
      "<span>" + (this.tts ? "Speaking" : "Silent") + "</span>";
    if (!this.tts) Voice.stopSpeaking();
  };

  Workspace.prototype.toggleMic = function () {
    if (this.mic && this.mic.live) return this.stopMic();
    this.startMic();
  };

  Workspace.prototype.startMic = async function () {
    var self = this;
    if (!this.session || this.session.status === "archived") {
      toast("open a session before talking to it", true);
      return;
    }
    var bar = this.$('[data-a="voicebar"]');
    var heard = this.$('[data-a="heard"]');
    var btn = this.$('[data-a="mic"]');

    this.mic = new Mic({
      interim: function (text) { heard.textContent = text; },
      // A settled sentence goes through the ORDINARY send path — same optimistic
      // render, same 409 handling, same "your message is saved" banner. Voice is
      // an input method here, not a second chat client.
      final: function (text) {
        heard.textContent = "";
        // BARGE-IN, AND WHY IT IS A QUEUE RATHER THAN AN INTERRUPT. A CLI turn
        // is atomic: there is no mid-thought interjection, and cancelling one
        // throws away money already spent and the answer with it. Firing a
        // second message into a busy pipe is worse still — the CLI would answer
        // both in one turn and the thread would show one reply for two things
        // said. So a sentence finished while a turn is in flight is HELD, the
        // bar says so, and send() posts it the moment the reply lands.
        //
        // The turnaround this is papering over is real and cannot be
        // engineered away: a spawned session answers in seconds where the old
        // chat call answered in about one. It can only be made honest.
        if (self.sending) {
          self.heldUtterance = self.heldUtterance
            ? self.heldUtterance + " " + text : text;
          self.setVoiceNote("held until the partner answers: " + self.heldUtterance);
          return;
        }
        self.$('[data-a="say"]').value = text;
        self.send();
      },
      state: function (name) {
        var live = name === "live";
        bar.classList.toggle("on", live || name === "starting");
        btn.classList.toggle("hot", live);
        btn.innerHTML = icon(live ? "waveform" : "record", 14) +
          "<span>" + (live ? "Listening" : name === "starting" ? "…" : "Talk") + "</span>";
        btn.setAttribute("aria-pressed", live ? "true" : "false");
      },
      error: function (msg) { toast(msg, true); }
    });
    await this.mic.start();
  };

  Workspace.prototype.stopMic = function () {
    if (this.mic) this.mic.stop();
    this.$('[data-a="heard"]').textContent = "";
  };

  Workspace.prototype.toggleDrawer = function (force) {
    var body = this.$(".bs-body");
    var open = force === undefined ? !body.classList.contains("drawer-open") : !!force;
    body.classList.toggle("drawer-open", open);
    this.$('[data-a="drawer"]').classList.toggle("on", open);
  };

  Workspace.prototype.setFilter = function (f) {
    this.filter = f;
    this.root.querySelectorAll(".bs-filter button").forEach(function (b) {
      b.classList.toggle("on", b.dataset.f === f);
    });
    this.load();
  };

  Workspace.prototype.load = async function () {
    var status = this.filter === "archived" ? "archived" : null;
    try {
      var data = await API.list(this.seat, status);
      if (this.dead) return;
      var list = data.sessions || [];
      if (this.filter === "active") {
        list = list.filter(function (s) { return s.status !== "archived"; });
      }
      this.sessions = list;
      this.modelInfo = data.model || null;
      this.renderModel();
      this.renderList();
      if (!this.session) {
        var want = null;
        try { want = Number(localStorage.getItem("bs-last-" + this.seat)) || null; } catch (e) {}
        var pick = this.sessions.filter(function (s) { return s.id === want; })[0]
          || this.sessions.filter(function (s) { return s.status !== "archived"; })[0];
        if (pick) this.open(pick.id);
        else { this.toggleDrawer(true); this.renderThread(); }
      }
    } catch (e) {
      this.sessions = [];
      this.$(".bs-list").innerHTML =
        '<li class="bs-empty"><b>cannot reach the brainstorm API</b>' + esc(e.message) + "</li>";
      this.toggleDrawer(true);
    }
  };

  Workspace.prototype.renderModel = function () {
    var el = this.$('[data-a="model"]');
    var m = this.modelInfo;
    if (!m) { el.hidden = true; return; }
    el.hidden = false;
    el.className = "bs-pill " + (m.available ? "" : "warn");
    // `label` is "claude · sonnet" — the RUNNER and the model, because the
    // partner is a spawned CLI session now and which CLI it is matters as much
    // as which model. This pill used to render a bare model name from an API
    // this room no longer talks to.
    el.innerHTML = m.available
      ? icon("verify", 12) + esc(m.label || m.model || "partner ready")
      : icon("doctor", 12) + esc(m.label ? m.label + " · unavailable" : "no partner");
    el.title = m.available
      ? ("thinking partner: " + (m.label || "") +
         (m.readonly_by ? "\n\nIt cannot write to this project: " + m.readonly_by : ""))
      : (m.reason || "");
  };

  Workspace.prototype.renderList = function () {
    var self = this;
    var ul = this.$(".bs-list");
    if (!this.sessions.length) {
      ul.innerHTML = '<li class="bs-empty"><b>no sessions yet</b>New starts one. ' +
        'Nothing here is on the board.</li>';
      return;
    }
    ul.innerHTML = this.sessions.map(function (s) {
      var on = self.session && self.session.id === s.id;
      return '<li data-id="' + s.id + '" class="' + (on ? "on " : "") +
        (s.status === "archived" ? "arch" : "") + '">' +
        '<span class="t">' + esc(s.title) + "</span>" +
        '<span class="m"><i class="bs-dot ' + esc(s.status) + '"></i>' +
        esc(s.status) + " · " + (s.messages || 0) + " msg · " + esc(ago(s.updated_at)) + "</span></li>";
    }).join("");
    ul.querySelectorAll("li[data-id]").forEach(function (li) {
      li.addEventListener("click", function () { self.open(Number(li.dataset.id)); });
    });
  };

  Workspace.prototype.newSession = async function () {
    try {
      var s = await API.create(this.seat, "");
      toast("new " + this.seat + " brainstorm");
      await this.load();
      this.open(s.id);
      this.$('[data-a="title"]').focus();
      this.$('[data-a="title"]').select();
    } catch (e) { toast(e.message, true); }
  };

  Workspace.prototype.open = async function (id) {
    // Never carry one session's unsaved pad into another — nor a live mic,
    // which would post the tail of one conversation into the next one.
    if (this.mic && this.mic.live) this.stopMic();
    await this.flush();
    try {
      var s = await API.read(id);
      if (this.dead) return;
      this.session = s;
      try { localStorage.setItem("bs-last-" + this.seat, String(id)); } catch (e) {}
      this.paint();
      this.renderList();
    } catch (e) { toast(e.message, true); }
  };

  Workspace.prototype.paint = function () {
    var s = this.session;
    var live = !!s && s.status !== "archived";
    var t = this.$('[data-a="title"]');
    t.value = s ? s.title : "";
    t.disabled = !live;
    this.$('[data-a="say"]').disabled = !live;
    this.$('[data-a="notes"]').disabled = !live;
    this.$('[data-a="notes"]').value = (s && s.notes) || "";
    this.$('[data-a="archive"]').disabled = !s;
    this.$('[data-a="archive"]').innerHTML =
      icon(s && s.status === "archived" ? "run" : "lock") +
      (s && s.status === "archived" ? "Reopen" : "");
    this.$('[data-a="archive"]').title = s && s.status === "archived"
      ? "Reopen this session" : "Archive this session";
    this.$('[data-a="deploy"]').disabled = !live;
    // An archived session takes no new turns, so it takes no voice either — and
    // the mic must not stay armed over one. Only ever ENABLES when voice is
    // actually available; loadVoice owns that fact and this must not override it.
    if (!live && this.mic && this.mic.live) this.stopMic();
    if (!live) {
      this.$('[data-a="mic"]').disabled = true;
    } else if (Voice.status && Voice.status.available) {
      this.$('[data-a="mic"]').disabled = false;
    }

    var pill = this.$('[data-a="status"]');
    pill.className = "bs-pill" + (!s ? "" : s.status === "deployed" ? " good" : s.status === "archived" ? " warn" : "");
    pill.textContent = s ? s.status : "no session";
    if (s && (s.deploys || []).length) {
      var n = s.deploys.reduce(function (a, d) { return a + ((d.items || []).length); }, 0);
      pill.textContent = "deployed · " + n + " item" + (n === 1 ? "" : "s") + " filed";
    }

    this.pad.load(s ? s.drawing : null);
    this.drawRev = padRev(s ? s.drawing : null);
    this.$('[data-a="drawstate"]').textContent = "";
    this.$('[data-a="notestate"]').textContent = "";
    if (this.notes) this.notes.sync();
    this.renderPartner(s ? s.thinker : null);
    this.renderThread();
  };

  /* =====================================================================
   * THE PARTNER — a real process, and you can see it and stop it.
   *
   * THREE WORDS THAT ALL SOUND FINAL, and the UI has to keep them apart or the
   * "confidently close" this was built for is not confidence, it is guessing:
   *
   *   close      ends the PROCESS. Chip goes from "live" to "closed"; the
   *              conversation, the notes and the drawing stay on screen. The
   *              next message reopens it, continuing the same CLI session.
   *   archive    files the SESSION away. The lock button. No new turns at all
   *              until reopened, and it closes the process on the way.
   *   deployed   a STATUS in the other pill. Says work was filed; says nothing
   *              about whether anyone is still talking.
   * ================================================================== */
  Workspace.prototype.renderPartner = function (t) {
    if (t) this.thinker = t;
    t = this.thinker;
    var el = this.$('[data-a="partner"]');
    if (!this.session || !t) { el.hidden = true; return; }
    el.hidden = false;
    var live = !!t.live;
    var cost = Number(t.spent_usd || 0);
    el.className = "bs-pill bs-partner" + (live ? " good" : "");
    // Cost is shown WHENEVER any was spent, live or not. A room that quietly
    // billed for six turns and then went idle should still say so — that is
    // the whole reason spend.py exists.
    el.innerHTML = icon(live ? "run" : "stop", 12) +
      "<span>" + (live ? "partner live" : (t.resumable ? "closed · resumes" : "closed")) +
      (t.turns ? " · " + t.turns + " turn" + (t.turns === 1 ? "" : "s") : "") +
      (cost ? " · $" + cost.toFixed(2) : "") + "</span>" +
      (live ? icon("close", 11) : "");
    el.disabled = !live;
    el.title = live
      ? ("A " + (t.runner || "CLI") + " session is running for this brainstorm" +
         (t.tools && t.tools.length
           ? " holding exactly: " + t.tools.join(", ") : " with no tools yet") +
         ".\nClick to close it. Nothing you have written is lost - the next " +
         "message picks the same conversation back up.")
      : (t.resumable
         ? "No process running. Your next message resumes the same CLI session."
         : "No process running. Your next message starts one.");
  };

  Workspace.prototype.closePartner = async function () {
    if (!this.session || !this.thinker || !this.thinker.live) return;
    var el = this.$('[data-a="partner"]');
    el.disabled = true;
    try {
      var out = await API.close(this.session.id);
      if (this.dead) return;
      this.renderPartner(out.thinker);
      // Said out loud rather than left to a chip changing colour: "did that
      // actually do anything" is the exact doubt this button exists to remove.
      toast(out.stopped ? "partner closed - your session is untouched"
        : "no partner was running");
    } catch (e) { toast(e.message, true); el.disabled = false; }
  };

  /** The drawing changed on the SERVER — the partner drew something.
   *
   * Reload the pad rather than letting the next autosave post a scene that
   * predates it. Skipped while the human is mid-stroke (a pending save timer),
   * because yanking the canvas out from under a drag is worse than being one
   * poll late; the merge is by element id server-side, so their strokes survive
   * either way. */
  Workspace.prototype.adoptDrawing = function (scene) {
    if (!this.pad) return;
    var rev = padRev(scene);
    if (rev === this.drawRev) return;
    if (this.saveTimers.drawing) return;
    this.drawRev = rev;
    this.session.drawing = scene;
    this.pad.load(scene);
    this.$('[data-a="drawstate"]').textContent = "the partner drew";
  };

  Workspace.prototype.rename = async function (title) {
    if (!this.session) return;
    title = String(title || "").trim();
    if (!title || title === this.session.title) {
      this.$('[data-a="title"]').value = this.session.title;
      return;
    }
    try {
      this.session = await API.patch(this.session.id, { title: title });
      await this.load();
      this.renderList();
    } catch (e) { toast(e.message, true); this.$('[data-a="title"]').value = this.session.title; }
  };

  Workspace.prototype.toggleArchive = async function () {
    if (!this.session) return;
    var to = this.session.status !== "archived";
    try {
      this.session = await API.archive(this.session.id, to);
      toast(to ? "archived" : "reopened");
      this.paint();
      await this.load();
      this.renderList();
    } catch (e) { toast(e.message, true); }
  };

  Workspace.prototype.togglePreview = function () {
    var ta = this.$('[data-a="notes"]'), pv = this.$(".bs-notes-prev");
    var showing = !pv.hidden;
    pv.hidden = showing;
    ta.hidden = !showing;
    this.$('[data-a="prev"]').classList.toggle("on", !showing);
    if (!showing) pv.innerHTML = ta.value.trim() ? md(ta.value) : '<div class="bs-empty">nothing written yet</div>';
  };

  /* ---- autosave -------------------------------------------------------
   * Debounced, per surface, and the pending state is on screen. The pads are
   * whole-document writes (see set_notes / set_drawing) so a coalesced save is
   * the whole save, not a lost keystroke. */
  Workspace.prototype.queueSave = function (what) {
    var self = this;
    if (!this.session || this.session.status === "archived") return;
    var mark = this.$(what === "notes" ? '[data-a="notestate"]' : '[data-a="drawstate"]');
    mark.textContent = "…";
    clearTimeout(this.saveTimers[what]);
    this.saveTimers[what] = setTimeout(function () { self.save(what); },
      what === "notes" ? 700 : 1100);
  };

  Workspace.prototype.save = async function (what) {
    if (!this.session || this.session.status === "archived") return;
    var mark = this.$(what === "notes" ? '[data-a="notestate"]' : '[data-a="drawstate"]');
    var body = what === "notes"
      ? { notes: this.$('[data-a="notes"]').value }
      : { drawing: this.pad.scene };
    try {
      var s = await API.patch(this.session.id, body);
      if (this.dead) return;
      // Keep the local pads: the response is authoritative for everything
      // EXCEPT the surface being typed into right now.
      if (what === "notes") s.drawing = this.session.drawing;
      else s.notes = this.$('[data-a="notes"]').value;
      this.session = Object.assign(this.session, s);
      // `changed` is the server saying what it actually persisted. A 200 that
      // did not include our surface is not a save, and reporting it as one is
      // how a pad silently stops writing.
      var changed = s.changed || [];
      var landed = changed.indexOf(what) >= 0;
      mark.style.color = landed ? "" : "var(--warn)";
      mark.textContent = landed ? "saved" : "not stored";
      setTimeout(function () { if (mark.textContent === "saved") mark.textContent = ""; }, 1600);
    } catch (e) {
      mark.style.color = "var(--bad)";
      if (e.status === 409) {
        mark.textContent = "archived";
        toast("this session is archived - reopen it before adding to it", true);
        this.session.status = "archived";
        this.paint();
        return;
      }
      mark.textContent = "unsaved";
      toast(what + ": " + e.message, true);
    }
  };

  /** Flush anything pending before a navigation that would lose it. */
  Workspace.prototype.flush = async function () {
    var self = this;
    var pending = Object.keys(this.saveTimers).filter(function (k) { return self.saveTimers[k]; });
    if (!pending.length || !this.session) return;
    pending.forEach(function (k) { clearTimeout(self.saveTimers[k]); self.saveTimers[k] = null; });
    await Promise.all(pending.map(function (k) { return self.save(k).catch(function () {}); }));
  };

  /* ---- chat ---------------------------------------------------------- */
  Workspace.prototype.renderThread = function () {
    var thread = this.$(".bs-thread");
    var s = this.session;
    if (!s) {
      thread.innerHTML = '<div class="bs-empty"><b>' + esc(this.copy.label) + "</b>" +
        esc(this.copy.empty) + "<br><br>Open a session from <b>Sessions</b>, or start a new one.</div>";
      return;
    }
    var msgs = s.messages || [];
    var voice = this.seat;
    var html = msgs.length ? msgs.map(function (m) {
      return '<div class="bs-msg ' + (m.role === "user" ? "user" : "bot") + '">' +
        '<span class="who">' + (m.role === "user" ? "you" : esc(voice)) + "</span>" +
        '<div class="bub">' + md(m.text) + "</div></div>";
    }).join("") : '<div class="bs-empty"><b>nothing said yet</b>' + esc(this.copy.empty) + "</div>";
    if (this.sending) {
      html += '<div class="bs-msg bot"><span class="who">' + esc(voice) + "</span>" +
        '<div class="bub bs-typing"><span></span><span></span><span></span></div></div>';
    }
    if (this.chatError) {
      // TWO DIFFERENT FAILURES, AND CONFLATING THEM LOSES WORK. `kept` means
      // the server stored what the human typed and only the model did not
      // answer (no API key returns 200 with reply:null) — telling them "not
      // sent" there would have them retype a message that is already saved.
      html += '<div class="bs-msg bot err"><span class="who">' +
        (this.chatErrorKept ? "your message is saved - the model did not answer" : "not sent") +
        "</span>" + '<div class="bub">' + esc(this.chatError) + "</div></div>";
    }
    thread.innerHTML = html;
    thread.scrollTop = thread.scrollHeight;
  };

  Workspace.prototype.send = async function () {
    if (!this.session || this.sending) return;
    var box = this.$('[data-a="say"]');
    var text = box.value.trim();
    if (!text) return;
    box.value = "";
    box.style.height = "auto";
    this.chatError = null;
    this.chatErrorKept = false;
    this.sending = true;
    // Optimistic: the server stores the human's message before it calls the
    // model and keeps it if the call fails, so showing it immediately is
    // honest rather than hopeful.
    (this.session.messages = this.session.messages || []).push(
      { id: "tmp", role: "user", text: text });
    this.renderThread();
    try {
      var out = await API.message(this.session.id, text);
      if (this.dead) return;
      this.session.messages = (this.session.messages || []).filter(function (m) { return m.id !== "tmp"; });
      if (out.message) this.session.messages.push(out.message);
      if (out.reply) this.session.messages.push(out.reply);
      this.renderPartner(out.thinker);
      // The partner may have drawn while it was thinking. Pick that up before
      // the next autosave posts a scene that predates it.
      if (out.thinker && out.thinker.pads) this.refreshDrawing();
      // THE SPOKEN CHANNEL IS THE REPLY TEXT AND ONLY THE REPLY TEXT. The
      // terminal channel of the same turn carries tool calls and their JSON
      // results, and speaking that would recite an Excalidraw scene out loud.
      // Never blocks the thread render: a slow synthesis must not delay the
      // words appearing, and now that a long reply is spoken in several chunks
      // it can run for a while.
      if (this.tts && out.reply && out.reply.text) {
        Voice.speak(out.reply.text).then(function (why) {
          if (why) toast(why, true);
        });
      }
      // 200 with reply:null is the no-API-key path. The text IS stored, so the
      // box stays empty and the banner says so; refilling it would invite a
      // duplicate of a message the server already has.
      if (out.model && out.model.ok === false) {
        this.chatError = out.model.error || "the model did not answer";
        this.chatErrorKept = true;
      }
    } catch (e) {
      this.session.messages = (this.session.messages || []).filter(function (m) { return m.id !== "tmp"; });
      this.chatError = e.status === 409
        ? "this session is archived - reopen it before adding to it"
        : e.message;
      this.chatErrorKept = false;
      // Nothing was stored on a thrown error, so give them their text back.
      box.value = text;
      if (e.status === 409) { this.session.status = "archived"; this.paint(); }
    }
    this.sending = false;
    this.renderThread();
    // BARGE-IN LANDS HERE. A CLI turn is atomic — there is no interrupting it
    // mid-thought the way you would a person — so an utterance finalised while
    // one was in flight was HELD rather than dropped or fired into a busy pipe.
    // Now that the turn is over it goes.
    if (this.heldUtterance) {
      var held = this.heldUtterance;
      this.heldUtterance = null;
      this.setVoiceNote("");
      this.$('[data-a="say"]').value = held;
      this.send();
    }
  };

  /** What the voice bar says about a turn it cannot interrupt. */
  Workspace.prototype.setVoiceNote = function (text) {
    var el = this.$('[data-a="heard"]');
    if (el) el.textContent = text || "";
  };

  Workspace.prototype.refreshDrawing = async function () {
    try {
      var s = await API.read(this.session.id);
      if (this.dead || !this.session || s.id !== this.session.id) return;
      this.adoptDrawing(s.drawing);
    } catch (e) { /* the pad is still whatever it was; nothing is lost */ }
  };

  /* =====================================================================
   * DEPLOY — two steps, and the first one writes nothing.
   * ================================================================== */
  Workspace.prototype.sheet = function (html) {
    var el = this.$(".bs-sheet");
    el.hidden = false;
    el.innerHTML = '<div class="bs-card">' + html + "</div>";
    return el;
  };
  Workspace.prototype.closeSheet = function () {
    var el = this.$(".bs-sheet");
    el.hidden = true;
    el.innerHTML = "";
    this.plan = null;
  };

  Workspace.prototype.openDeploy = async function () {
    if (!this.session) return;
    await this.flush();
    this.sheet(
      '<div class="bs-card-h"><h3>' + icon("gate", 17) + "Reading the session…</h3>" +
      '<div class="bs-safe"><span class="bs-spin"></span>' +
      "Synthesising a proposal. <b>Nothing is being queued</b> - this step only reads." +
      "</div></div>" +
      '<div class="bs-card-b"><div class="bs-empty">the model is reading the conversation, ' +
      "the notes pad and the drawing…</div></div>");
    try {
      var out = await API.synthesize(this.session.id);
      if (this.dead) return;
      this.plan = out.plan;
      this.synthMeta = out;
      this.renderReview();
    } catch (e) {
      this.sheet(
        '<div class="bs-card-h"><h3>' + icon("doctor", 17) + "Could not synthesize</h3>" +
        '<div class="bs-safe done">' + icon("verify", 14) + "Nothing was queued.</div></div>" +
        '<div class="bs-card-b"><div class="bs-summary">' + esc(e.message) + "</div></div>" +
        '<div class="bs-card-f"><span class="bs-spacer"></span>' +
        '<button class="bs-btn" data-x="close">Close</button>' +
        '<button class="bs-btn primary" data-x="retry">Try again</button></div>');
      this.wireSheet();
    }
  };

  Workspace.prototype.renderReview = function () {
    var self = this;
    var plan = this.plan || { items: [], summary: "", questions: [], notes: [] };
    var items = plan.items || [];
    var seats = (this.seat === "narrative")
      ? ["narrative"]
      : ["director", "narrative", "gameplay", "tech", "art", "audio", "cinematic", "qa"];
    var meta = this.synthMeta || {};
    var prior = meta.already_filed;

    var itemHtml = items.length ? items.map(function (it, i) {
      return '<div class="bs-item" data-i="' + i + '">' +
        '<div class="bs-item-h">' +
          '<span class="bs-item-n">' + (plan.chained ? i + 1 : "·") + "</span>" +
          '<select data-k="seat" aria-label="seat">' + seats.map(function (s) {
            return '<option value="' + esc(s) + '"' + (s === it.seat ? " selected" : "") + ">" + esc(s) + "</option>";
          }).join("") + "</select>" +
          '<input class="ti" data-k="title" value="' + esc(it.title) + '" aria-label="title">' +
          '<button class="bs-btn ghost danger" data-x="drop" data-i="' + i + '" title="Remove this item">Remove</button>' +
        "</div>" +
        '<textarea data-k="brief" aria-label="brief" placeholder="the brief the agent will act on">' +
          esc(it.brief) + "</textarea></div>";
    }).join("") : '<div class="bs-empty"><b>the model proposed no work items</b>' +
      "There is nothing to file from this session yet. Close this, keep talking, and try again.</div>";

    this.sheet(
      '<div class="bs-card-h">' +
        "<h3>" + icon("gate", 17) + "Review the plan before anything is filed</h3>" +
        '<div class="bs-safe">' + icon("verify", 14) +
          "<span><b>Nothing has been queued.</b> This is a proposal - no work item exists " +
          "until you press Confirm below.</span></div>" +
      "</div>" +
      '<div class="bs-card-b">' +
        (plan.summary ? '<div><div class="bs-sec-h">what this session decided</div>' +
          '<div class="bs-summary">' + md(plan.summary) + "</div></div>" : "") +
        ((plan.questions || []).length ? '<div><div class="bs-sec-h">open questions</div>' +
          '<ul class="bs-qs">' + plan.questions.map(function (q) { return "<li>" + esc(q) + "</li>"; }).join("") +
          "</ul></div>" : "") +
        ((plan.notes || []).length ? '<div><div class="bs-sec-h">corrections made to the model’s answer</div>' +
          '<ul class="bs-notes-list">' + plan.notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") +
          "</ul></div>" : "") +
        (prior ? '<div class="bs-safe" style="background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)">' +
          icon("lock", 14) + "<span>This exact plan was already filed from this session as " +
          esc((prior.items || []).map(function (i) { return "#" + i.id; }).join(", ")) +
          ". Confirming again files a second copy.</span></div>" : "") +
        '<div><div class="bs-sec-h">work items - ' + items.length +
          (items.length === 1 ? " item" : " items") + " this will file</div>" + itemHtml + "</div>" +
        (items.length > 1 ? '<label class="bs-chain">' +
          '<input type="checkbox" data-k="chained"' + (plan.chained ? " checked" : "") + ">" +
          "<span><b>Run as a chain</b> - each item waits for the one before it. " +
          "Leave off when they are independent; priority alone will not stop two agents " +
          "starting in the same tick.</span></label>" : "") +
      "</div>" +
      '<div class="bs-card-f">' +
        '<span class="bs-pill">' + esc((meta.model && meta.model.model) || "") + " · ~$" +
          esc(((meta.model && meta.model.estimated_usd) || 0).toFixed(4)) + "</span>" +
        '<span class="bs-spacer"></span>' +
        '<button class="bs-btn" data-x="close">Cancel - file nothing</button>' +
        '<button class="bs-btn primary" data-x="confirm"' + (items.length ? "" : " disabled") + ">" +
          icon("run", 15) + "Confirm - file " + items.length + " item" + (items.length === 1 ? "" : "s") +
        "</button>" +
      "</div>");
    this.wireSheet();
  };

  Workspace.prototype.wireSheet = function () {
    var self = this;
    var el = this.$(".bs-sheet");
    el.querySelectorAll("[data-x]").forEach(function (b) {
      b.addEventListener("click", function () {
        var x = b.dataset.x;
        if (x === "close") self.closeSheet();
        if (x === "retry") self.openDeploy();
        if (x === "confirm") self.confirm(false);
        if (x === "again") self.confirm(true);
        if (x === "drop") {
          self.readSheet();
          self.plan.items.splice(Number(b.dataset.i), 1);
          self.renderReview();
        }
      });
    });
  };

  /** Pull the human's edits out of the review form and into the plan. This is
      what gets filed — deploy never re-synthesises. */
  Workspace.prototype.readSheet = function () {
    var el = this.$(".bs-sheet");
    if (!el || !this.plan) return;
    var items = [];
    el.querySelectorAll(".bs-item").forEach(function (row) {
      var get = function (k) {
        var f = row.querySelector('[data-k="' + k + '"]');
        return f ? f.value : "";
      };
      items.push({ seat: get("seat"), title: get("title").trim(),
                   brief: get("brief").trim(), priority: 0 });
    });
    if (items.length) this.plan.items = items;
    var chain = el.querySelector('[data-k="chained"]');
    this.plan.chained = chain ? chain.checked : false;
  };

  Workspace.prototype.confirm = async function (again) {
    this.readSheet();
    var plan = this.plan;
    if (!plan || !(plan.items || []).length) return;
    var self = this;
    var btn = this.$('.bs-sheet [data-x="confirm"], .bs-sheet [data-x="again"]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="bs-spin"></span> filing…'; }
    try {
      var out = await API.deploy(this.session.id, plan, again);
      if (this.dead) return;
      this.session = out.session || this.session;
      await this.load();
      this.renderList();
      this.paint();
      var filed = out.filed || [];
      this.sheet(
        '<div class="bs-card-h"><h3>' + icon("verify", 17) + "Filed to the board</h3>" +
        '<div class="bs-safe done">' + icon("gate", 14) + "<span>" + filed.length +
          " work item" + (filed.length === 1 ? "" : "s") + " queued" +
          (out.chained ? " as a chain - each waits for the one before it" : "") +
          ". The board dispatches them.</span></div></div>" +
        '<div class="bs-card-b"><div class="bs-filed">' +
          filed.map(function (f) {
            return '<span class="f"><span class="id">#' + esc(f.id) + "</span>" +
              '<span class="bs-pill">' + esc(f.seat) + "</span>" +
              "<span>" + esc(f.title) + "</span>" +
              (f.chain_pos ? '<span class="bs-pill">step ' + esc(f.chain_pos) + "</span>" : "") +
              "</span>";
          }).join("") + "</div></div>" +
        '<div class="bs-card-f"><span class="bs-spacer"></span>' +
        '<button class="bs-btn primary" data-x="close">Done</button></div>');
      this.wireSheet();
      toast("filed " + filed.length + " item" + (filed.length === 1 ? "" : "s"));
    } catch (e) {
      if (e.status === 409) {
        // The double-file guard fired. Say what already exists and make the
        // override explicit rather than retrying it silently.
        this.sheet(
          '<div class="bs-card-h"><h3>' + icon("lock", 17) + "Already filed</h3>" +
          '<div class="bs-safe" style="background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)">' +
          icon("verify", 14) + "Nothing new was queued.</div></div>" +
          '<div class="bs-card-b"><div class="bs-summary">' + esc(e.message) + "</div></div>" +
          '<div class="bs-card-f"><span class="bs-spacer"></span>' +
          '<button class="bs-btn" data-x="close">Close</button>' +
          '<button class="bs-btn primary" data-x="again">File a second copy anyway</button></div>');
        this.wireSheet();
        return;
      }
      if (btn) { btn.disabled = false; btn.textContent = "Confirm - file " + plan.items.length + " items"; }
      toast(e.message, true);
    }
  };

  /* =====================================================================
   * PUBLIC
   * ================================================================== */
  var Brainstorm = {
    /**
     * Mount the workspace into `host`.
     *   Brainstorm.mount(el, { seat: "director" })
     *   Brainstorm.mount(el, { seat: "narrative" })
     *
     * ONE LAYOUT. `pads:false` and `chrome:"minimal"` were both built for the
     * Agents view and both removed: that view does not want this UI, it wants
     * its own conversation to be able to talk to the brainstorm backend. See
     * agents_console.js. Cut-down variants of a workspace are how one module
     * quietly becomes two.
     *
     * Replaces the host's contents. Safe to call again — the previous
     * instance's timers and observers are torn down first.
     */
    mount: function (host, opts) {
      if (!host) return null;
      if (ACTIVE) { try { ACTIVE.destroy(); } catch (e) {} }
      ACTIVE = new Workspace(host, opts || {});
      return ACTIVE;
    },
    unmount: function () {
      if (ACTIVE) { try { ACTIVE.destroy(); } catch (e) {} ACTIVE = null; }
    },
    get active() { return ACTIVE; },
    // Exposed for tests and for anything that wants to hand the pad a scene.
    _api: API,
    _emptyScene: emptyScene
  };

  window.Brainstorm = Brainstorm;
})();
