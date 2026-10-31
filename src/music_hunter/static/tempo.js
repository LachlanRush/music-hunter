// Same BPM (v5): tracks played at one tempo, pitch kept. The playlist and its Find music each have their own
// setting, kept on the server, which also decides each track's rate and renders the stretched copies (Rubber Band;
// the browser's own speed change sounded crackly). Until a track's copy is ready the original plays, and the copy
// swaps in at the same point in the music.
const SameBpm = (() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // `query` picks the copy's owner: "pl=<id>" on the playlist page; empty in Find music (its URL says which).
  function player(audio, base, query = "", onChange = () => {}) {
    let loaded = 1, token = 0;
    const q = query ? `&${query}` : "";

    async function status(id, rate, ahead = false) {
      try {
        const r = await fetch(`${base}/tempo/${id}?rate=${rate}${q}${ahead ? "&ahead=1" : ""}`);
        return r.ok ? await r.json() : {state: "failed"};
      } catch (e) { return {state: "failed"}; }
    }
    function swapTo(url, rate) {
      const at = audio.currentTime * loaded / rate, playing = !audio.paused;
      loaded = rate;
      audio.src = url;
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = at; }, {once: true});
      if (playing) audio.play().catch(() => {});
      onChange(rate);
    }
    async function follow(my, id, rate) {
      for (let i = 0; i < 300; i++) {             // up to ~10 minutes (a queue of renders on the Pi)
        await sleep(2000);
        if (my !== token) return;
        const s = await status(id, rate);
        if (my !== token) return;
        if (s.state === "ready") return swapTo(s.url, rate);
        if (s.state !== "rendering") return;
      }
    }
    // The URL to start a track with: its stretched copy if ready, else `original` (the copy follows when ready).
    async function start(id, original, rate) {
      const my = ++token;
      loaded = 1;
      if (rate && rate !== 1) {
        const s = await status(id, rate);
        if (s.state === "ready" && my === token) { loaded = rate; onChange(rate); return s.url; }
        if (s.state === "rendering") follow(my, id, rate);
      }
      onChange(1);
      return original;
    }
    // The setting changed while a track plays: switch copies, keeping the place in the music.
    async function retune(id, original, rate) {
      const my = ++token;
      rate = rate || 1;
      if (rate === loaded) return;
      if (rate === 1) return swapTo(original, 1);
      const s = await status(id, rate);
      if (my !== token) return;
      if (s.state === "ready") swapTo(s.url, rate);
      else if (s.state === "rendering") follow(my, id, rate);
    }
    // Render a track's copy ahead of time (the next one), behind the one playing.
    function prepare(id, rate) { if (rate && rate !== 1) status(id, rate, true); }
    return {start, retune, prepare, rate: () => loaded};
  }

  // The note under the track: what it plays at, and whether its copy is still being made.
  function note(track, loadedRate) {
    if (!track || !track.tempo_note) return "";
    return track.rate !== 1 && loadedRate !== track.rate ? `${track.tempo_note} · getting it ready…` : track.tempo_note;
  }

  return {player, note};
})();
