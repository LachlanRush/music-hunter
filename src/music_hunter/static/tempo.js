// Same BPM (v5): every track played at one tempo, pitch kept — on the playlist page and in its Find music.
// The stretched copy is rendered on the server (Rubber Band; the browser's own speed change sounded crackly). Until
// it's ready the original plays, and the copy swaps in at the same point in the music. The setting is remembered per
// playlist in this browser and shared by the playlist and its Find music.
const SameBpm = (() => {
  const FOLDS = [1, 2, 0.5, 1.5, 2 / 3];   // a measured BPM can be a half, double or 2:3 reading of the real tempo
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function settings(pid) {
    try { return {on: false, bpm: null, ...JSON.parse(localStorage.getItem(`tempo-${pid}`) || "{}")}; }
    catch (e) { return {on: false, bpm: null}; }
  }
  function store(pid, s) { try { localStorage.setItem(`tempo-${pid}`, JSON.stringify(s)); } catch (e) {} }
  function median(bpms) {
    const b = bpms.filter(Boolean).sort((x, y) => x - y);
    return b.length ? Math.round(b[Math.floor(b.length / 2)] * 2) / 2 : 128;
  }

  // The playback rate for a track: the reading of its BPM closest to the target; 1 if off, unknown or too far.
  function plan(bpm, s, defaultTarget, max) {
    if (!s.on) return {rate: 1, target: null, note: ""};
    const target = s.bpm || defaultTarget;
    if (!bpm || !target) return {rate: 1, target: null, note: "tempo unknown: original speed"};
    const reading = FOLDS.map((f) => bpm * f).sort((a, b) => Math.abs(target / a - 1) - Math.abs(target / b - 1))[0];
    const r = target / reading;
    if (Math.abs(r - 1) > max) {
      return {rate: 1, target: null, note: `original tempo (${Math.round(reading)} BPM is beyond ±${Math.round(max * 100)}%)`};
    }
    const rate = Math.round(r * 1000) / 1000, pct = Math.round((rate - 1) * 1000) / 10;
    return {rate, target, note: `${target} BPM (${pct >= 0 ? "+" : ""}${pct}%)`};
  }

  async function status(base, id, rate) {
    try {
      const r = await fetch(`${base}/tempo/${id}?rate=${rate}`);
      return r.ok ? await r.json() : {state: "failed"};
    } catch (e) { return {state: "failed"}; }
  }

  // Keeps track of which copy of the current track an <audio> element has loaded (original = rate 1).
  function player(audio, base, onChange = () => {}) {
    let loaded = 1, token = 0;

    function swapTo(url, rate) {
      const at = audio.currentTime * loaded / rate, playing = !audio.paused;
      loaded = rate;
      audio.src = url;
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = at; }, {once: true});
      if (playing) audio.play().catch(() => {});
      onChange(rate);
    }
    async function follow(my, id, rate) {
      for (let i = 0; i < 150; i++) {             // up to ~5 minutes on a slow render
        await sleep(2000);
        if (my !== token) return;
        const s = await status(base, id, rate);
        if (my !== token) return;
        if (s.state === "ready") return swapTo(s.url, rate);
        if (s.state !== "rendering") return;
      }
    }
    // The URL to start a track with: its stretched copy if ready, else `original` (the copy follows when ready).
    async function start(id, original, rate) {
      const my = ++token;
      loaded = 1;
      if (rate !== 1) {
        const s = await status(base, id, rate);
        if (s.state === "ready" && my === token) { loaded = rate; onChange(rate); return s.url; }
        if (s.state === "rendering") follow(my, id, rate);
      }
      onChange(1);
      return original;
    }
    // The setting changed while a track plays: switch copies, keeping the place in the music.
    async function retune(id, original, rate) {
      const my = ++token;
      if (rate === loaded) return;
      if (rate === 1) return swapTo(original, 1);
      const s = await status(base, id, rate);
      if (my !== token) return;
      if (s.state === "ready") swapTo(s.url, rate);
      else if (s.state === "rendering") follow(my, id, rate);
    }
    // Render a track's copy ahead of time (the next one).
    function prepare(id, rate) { if (rate !== 1) status(base, id, rate); }
    return {start, retune, prepare, rate: () => loaded};
  }

  return {settings, store, median, plan, player};
})();
