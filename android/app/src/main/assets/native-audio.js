// No dark box behind whatever is tapped (the tap highlight), and no outline left around it (the focus ring): on a
// phone there's no keyboard to show focus for.
(() => {
  const st = document.createElement("style");
  st.textContent = "* { -webkit-tap-highlight-color: transparent; }"
    + " a:focus, button:focus, summary:focus, input[type=range]:focus, input[type=checkbox]:focus, [tabindex]:focus"
    + " { outline: none !important; box-shadow: none; }";      // nor a focus ring around what was tapped
  (document.head || document.documentElement).appendChild(st);
})();

// In the app, the pages' `new Audio()` and lock-screen calls go to the native player (SPEC.md §19), so the same pages
// work in a browser and here. Injected by the app before any page script runs.
(() => {
  if (!window.NativeAudioBridge) return;
  const B = window.NativeAudioBridge, PREFIX = "native-preload:";
  let current = null;

  class NativeAudio extends EventTarget {
    constructor() {
      super();
      this._src = ""; this._t = 0; this._d = NaN; this._paused = true; this._ended = false; this._volume = 1;
      this.preload = "auto"; this.error = null; this.readyState = 0; this.networkState = 0; this.playbackRate = 1;
      current = this;
    }
    get src() { return this._src; }
    set src(v) {
      this._src = v.startsWith(PREFIX) ? v : new URL(v, location.href).href;
      this._t = 0; this._d = NaN; this._ended = false; this.readyState = 0; this.error = null;
      current = this;
      B.load(this._src.startsWith(PREFIX) ? this._src.slice(PREFIX.length) : this._src);
    }
    get currentTime() { return this._t; }
    set currentTime(s) { this._t = s; this._ended = false; B.seek(Math.round(s * 1000)); }
    get duration() { return this._d; }
    get paused() { return this._paused; }
    get ended() { return this._ended; }
    get volume() { return this._volume; }
    set volume(v) { this._volume = v; B.setVolume(v); }
    play() { this._paused = false; this._ended = false; B.play(); return Promise.resolve(); }
    pause() { this._paused = true; B.pause(); }
    load() {}
    _fire(type) { this.dispatchEvent(new Event(type)); }
  }
  window.Audio = NativeAudio;

  // Called by the app: one player event, with where the player is.
  window.__nativeAudio = (type, s) => {
    const a = current;
    if (!a) return;
    if (s.t !== undefined) a._t = s.t;
    if (s.d !== undefined && s.d > 0) a._d = s.d;
    switch (type) {
      case "timeupdate": a._fire("timeupdate"); break;
      case "loadedmetadata": a.readyState = 4; a._fire("durationchange"); a._fire("loadedmetadata"); a._fire("canplay"); break;
      case "play": a._paused = false; a._fire("play"); break;
      case "playing": a._paused = false; a._fire("playing"); break;
      case "waiting": a.readyState = 2; a._fire("waiting"); break;
      case "pause": a._paused = true; a._fire("pause"); break;
      case "seeked": a._fire("seeked"); a._fire("timeupdate"); break;
      case "ended": a._ended = true; a._paused = true; a._fire("timeupdate"); a._fire("pause"); a._fire("ended"); break;
      case "error": a.error = {code: s.code || 4, message: s.message || ""}; a._fire("error"); break;
    }
  };

  // Lock screen and notification.
  const handlers = {};
  class Metadata { constructor(m) { Object.assign(this, m); } }
  window.MediaMetadata = Metadata;
  let meta = null;
  const session = {
    get metadata() { return meta; },
    set metadata(m) {
      meta = m;
      if (m) B.setMetadata(m.title || "", m.artist || "", m.album || "", (m.artwork && m.artwork[0] && m.artwork[0].src) || "");
    },
    playbackState: "none",
    setActionHandler(action, fn) { handlers[action] = fn; },
    setPositionState() {},
  };
  Object.defineProperty(navigator, "mediaSession", {value: session, configurable: true});
  window.__nativeAction = (action) => { if (handlers[action]) handlers[action]({action}); };

  // The pages' desktop test: in the app it's always the phone path (levelling by the player's volume).
  const realMatch = window.matchMedia.bind(window);
  window.matchMedia = (q) => (q.includes("pointer: fine") ? {matches: false, media: q, addEventListener() {}, removeEventListener() {}} : realMatch(q));

  // Find music fetches the next track into memory and plays that copy. The native player can't read page memory, and
  // streams from the Pi anyway, so the "memory copy" stands in for the track's address.
  const urls = new WeakMap(), realFetch = window.fetch.bind(window), realCreate = URL.createObjectURL.bind(URL), realRevoke = URL.revokeObjectURL.bind(URL);
  window.fetch = (u, o) => {
    if (typeof u === "string" && /\/audio\/\d+$/.test(u) && !(o && o.method && o.method !== "GET")) {
      const full = new URL(u, location.href).href;
      return Promise.resolve({ok: true, status: 200, statusText: "", blob: async () => { const b = new Blob([]); urls.set(b, full); return b; }});
    }
    return realFetch(u, o);
  };
  URL.createObjectURL = (b) => (urls.has(b) ? PREFIX + urls.get(b) : realCreate(b));
  URL.revokeObjectURL = (u) => { if (!String(u).startsWith(PREFIX)) realRevoke(u); };

  // As in a browser, leaving a page stops its audio.
  window.addEventListener("pagehide", () => B.stop());
})();

// Now playing on other pages (SPEC.md §19): while the music comes from another page, this page shows the same player
// bar as a playlist page, so moving around doesn't change what the player looks like. Its buttons drive the page
// the music comes from; tapping the track goes back to that page.
(() => {
  if (!window.NativeAudioBridge) return;
  const B = window.NativeAudioBridge;
  const CSS = `
#mh-now { position: fixed; left: 0; right: 0; bottom: 0; z-index: 50; background: var(--surface); border-top: 1px solid var(--line);
  padding: 10px 16px calc(10px + env(safe-area-inset-bottom)); font: 400 15px/1.5 var(--body); color: var(--fg); }
#mh-now .in { max-width: 760px; margin: 0 auto; display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 12px; align-items: center; }
#mh-now .art { width: 44px; height: 44px; border-radius: 4px; background: var(--surface); object-fit: cover; display: block; }
#mh-now .t { min-width: 0; }
#mh-now .t b { display: block; font: 600 14px/1.3 var(--body); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#mh-now .t span { display: block; font-size: 13px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#mh-now .controls { display: flex; gap: 6px; align-items: center; }
#mh-now .round { width: 40px; height: 40px; border-radius: 50%; border: 1px solid var(--line); background: var(--bg); color: var(--fg);
  display: grid; place-items: center; padding: 0; font: inherit; }
#mh-now .round svg { width: 15px; height: 15px; fill: currentColor; }
#mh-now .round.primary { background: var(--fg); color: var(--bg); border-color: var(--fg); }
#mh-now .round.on { background: var(--fg); color: var(--bg); border-color: var(--fg); }
#mh-now .seek { grid-column: 1 / -1; display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center;
  font: 400 11px var(--mono); color: var(--muted); }
#mh-now input[type=range] { width: 100%; accent-color: var(--accent); margin: 0; }
body.mh-now-on { padding-bottom: 140px !important; }`;
  const PLAY = '<path d="M7 4l13 8-13 8z"/>', PAUSE = '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>';
  const HTML = `<div class="in">
  <img class="art" id="mh-art" alt="">
  <div class="t" id="mh-open"><b id="mh-title"></b><span id="mh-artist"></span></div>
  <div class="controls">
    <button class="round" id="mh-prev" aria-label="Previous"><svg viewBox="0 0 24 24"><path d="M19 5l-9 7 9 7zM5 5h3v14H5z"/></svg></button>
    <button class="round primary" id="mh-play" aria-label="Play"><svg viewBox="0 0 24 24" id="mh-icon">${PLAY}</svg></button>
    <button class="round" id="mh-next" aria-label="Next"><svg viewBox="0 0 24 24"><path d="M5 5l9 7-9 7zM16 5h3v14h-3z"/></svg></button>
    <button class="round" id="mh-shuffle" aria-label="Shuffle" aria-pressed="false" hidden><svg viewBox="0 0 24 24"><path d="M4 7h3.5l7 10H18v-2l3 3-3 3v-2h-4.5l-7-10H4zm10.5 0H18V5l3 3-3 3V9h-3.5l-1.6 2.3-1.2-1.7zM4 17h3.5l1.6-2.3 1.2 1.7L8.5 19H4z"/></svg></button>
  </div>
  <div class="seek"><span id="mh-pos">0:00</span><input type="range" id="mh-seek" min="0" max="1000" value="0" aria-label="Position"><span id="mh-dur">0:00</span></div>
</div>`;
  let bar = null, seeking = false, last = null;
  const fmt = (s) => { if (!isFinite(s) || s < 0) s = 0; s = Math.floor(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  function build() {
    if (bar || !document.body) return bar;
    const style = document.createElement("style"); style.textContent = CSS; document.head.appendChild(style);
    bar = document.createElement("div"); bar.id = "mh-now"; bar.hidden = true; bar.innerHTML = HTML;
    document.body.appendChild(bar);
    const $ = (id) => bar.querySelector("#" + id);
    $("mh-play").onclick = () => B.npToggle();
    $("mh-prev").onclick = () => B.npAction("previoustrack");
    $("mh-next").onclick = () => B.npAction("nexttrack");
    $("mh-shuffle").onclick = () => B.npShuffle();
    $("mh-open").onclick = $("mh-art").onclick = () => B.npOpen();
    const seek = $("mh-seek");
    seek.addEventListener("input", () => { seeking = true; if (last && last.d) $("mh-pos").textContent = fmt(seek.value / 1000 * last.d); });
    seek.addEventListener("change", () => { if (last && last.d) B.npSeek(Math.round(seek.value / 1000 * last.d * 1000)); seeking = false; });
    return bar;
  }
  // Called by the app with what's playing, whenever it changes (and the position while it plays).
  window.__nowPlaying = (s) => {
    last = s;
    if (!build()) return;
    bar.hidden = !s.show;
    document.body.classList.toggle("mh-now-on", !!s.show);
    if (!s.show) return;
    const $ = (id) => bar.querySelector("#" + id);
    $("mh-title").textContent = s.title || ""; $("mh-artist").textContent = s.artist || "";
    if ($("mh-art").getAttribute("src") !== (s.art || "")) $("mh-art").src = s.art || "";
    $("mh-art").style.visibility = s.art ? "visible" : "hidden";
    $("mh-icon").innerHTML = s.playing ? PAUSE : PLAY;
    $("mh-play").setAttribute("aria-label", s.playing ? "Pause" : "Play");
    const sh = $("mh-shuffle"); sh.hidden = s.shuffle === null || s.shuffle === undefined;
    sh.classList.toggle("on", !!s.shuffle); sh.setAttribute("aria-pressed", String(!!s.shuffle));
    $("mh-dur").textContent = fmt(s.d);
    if (!seeking) { $("mh-pos").textContent = fmt(s.t); $("mh-seek").value = s.d ? Math.round(1000 * s.t / s.d) : 0; }
  };
  document.addEventListener("DOMContentLoaded", () => { build(); B.npReady(); });
})();

// Tell the app when this page has its content, so it can swap it in straight away (SPEC.md §19): it has loaded and its
// own data has come back. Covers and anything slower fill in afterwards, in space kept for them.
(() => {
  if (!window.NativeAudioBridge) return;
  let inflight = 0, sent = false, quietSince = 0;
  const realFetch = window.fetch;
  const slow = (u) => /\/find(\b|\/)|\/tempo/.test(String(u && u.url || u));   // Find music's status mustn't hold the page up
  window.fetch = function (...args) {
    if (slow(args[0]) && !location.pathname.startsWith("/find/")) return realFetch.apply(this, args);
    inflight++;
    return realFetch.apply(this, args).finally(() => { inflight--; quietSince = performance.now(); });
  };
  function check() {
    if (sent) return;
    if (document.readyState !== "loading" && inflight === 0 && performance.now() - quietSince > 20) {
      sent = true;
      requestAnimationFrame(() => window.NativeAudioBridge.pageReady());
      return;
    }
    setTimeout(check, 15);
  }
  document.addEventListener("DOMContentLoaded", () => { quietSince = performance.now(); setTimeout(check, 15); });
})();
