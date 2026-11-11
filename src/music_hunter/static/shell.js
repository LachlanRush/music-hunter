// The browser shell (2026-11-11): music carries on between pages in Chrome, as it does in the phone app. Each page
// runs in a frame of this one; moving to another page opens it in a new frame and keeps the playing page alive,
// hidden, with its player bar shown over whatever page is on screen. Pressing play on another page hands the music
// over to it (the old playing page is closed). The pages are unchanged: they ask `go()` (static/nav.js) to move, and
// tell the bar what's playing (window.__playerState / __playerControl).
const MHShell = window.MHShell = (() => {
  let current = null, owner = null, bar = null, seeking = false;
  const key = (u) => { const x = new URL(u, location.href); return (x.pathname.replace(/\/+$/, "") || "/") + x.search; };
  const win = (f) => { try { return f && f.contentWindow; } catch (e) { return null; } };
  const playing = (f) => { const a = win(f) && win(f).__audio; return !!(a && a.src && !a.ended && (!a.paused || f === owner)); };

  function frame(url) {
    const f = document.createElement("iframe");
    f.className = "page behind";
    f.setAttribute("title", "Music Hunter");
    f.src = url;
    f.addEventListener("load", () => wire(f));
    document.body.appendChild(f);
    return f;
  }

  // Each page as it loads: its links come here, and its player tells us when it starts.
  function wire(f) {
    const w = win(f), d = w && w.document;
    if (!d) return;
    d.addEventListener("click", (e) => {
      if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return;
      const u = new URL(a.href, w.location.href);
      if (u.origin !== location.origin || u.pathname.startsWith("/api/") || u.pathname.startsWith("/static/")) return;
      e.preventDefault();
      open(u.pathname + u.search);
    });
    const a = w.__audio;
    if (a && !a.__mhWired) {
      a.__mhWired = true;
      a.addEventListener("play", () => claim(f));
    }
    if (f === current) syncAddress(true);
    paint();
  }

  // Pressing play on a page makes it the playing page; the one before stops (and goes, if it's not on screen).
  function claim(f) {
    if (owner && owner !== f) {
      try { win(owner).__audio.pause(); } catch (e) {}
      if (owner !== current) owner.remove();
    }
    owner = f;
    paint();
  }

  function show(f) {
    const was = current;
    current = f;
    f.classList.remove("behind");
    if (was && was !== f) {
      was.classList.add("behind");
      if (was !== owner) was.remove();                     // only the playing page stays alive behind
    }
    try { win(f).focus(); } catch (e) {}
    syncAddress(false);
    paint();
  }

  // Open a page: the playing page as it is; any other afresh (it shows its kept copy at once, then updates).
  function open(url, push = true) {
    const k = key(url);
    if (push && key(location.href) !== k) history.pushState({}, "", url);
    if (owner && win(owner) && key(win(owner).location.href) === k) {
      // The playing page kept alive: catch up with changes made meanwhile on other pages (e.g. a song added to it
      // from another playlist, feedback #46), as the app does when it returns to a kept page.
      if (owner !== current) { try { const w = win(owner); if (typeof w.load === "function") w.load(); if (typeof w.loadFind === "function") w.loadFind(); } catch (e) {} }
      show(owner); return;
    }
    if (current && win(current) && key(win(current).location.href) === k) return;
    const f = frame(url);
    let done = false;
    const swap = () => { if (!done) { done = true; show(f); } };
    f.addEventListener("load", swap, {once: true});
    setTimeout(swap, 800);                                  // a slow answer: show it, it fills in
  }

  // The address bar and tab title follow the page on screen (a page can redirect, e.g. Find music not started).
  function syncAddress(replace) {
    const w = win(current);
    if (!w) return;
    try {
      const here = w.location.pathname + w.location.search;
      if (replace && key(here) !== key(location.href)) history.replaceState({}, "", here);
      document.title = w.document.title;
    } catch (e) {}
  }
  window.addEventListener("popstate", () => open(location.pathname + location.search, false));

  // --- the player bar for a page playing behind the one on screen ------------------------------------------------
  const fmt = (s) => { if (!isFinite(s) || s < 0) s = 0; s = Math.floor(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const PLAY = '<path d="M7 4l13 8-13 8z"/>', PAUSE = '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>';
  function build() {
    bar = document.createElement("div");
    bar.id = "mh-bar"; bar.hidden = true;
    bar.innerHTML = `<div class="in">
      <img class="art" id="b-art" alt="">
      <div class="info"><div class="t" id="b-open"><b id="b-title"></b><span id="b-artist"></span><span class="note" id="b-note"></span></div>
        <button class="vote" data-c="down" id="b-down" aria-label="Not for this playlist" hidden><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg></button>
        <button class="vote" data-c="up" id="b-up" aria-label="Add to this playlist" hidden><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12h8M12 8v8"/></svg></button></div>
      <div class="controls">
        <button class="round" data-c="prev" aria-label="Previous"><svg viewBox="0 0 24 24"><path d="M19 5l-9 7 9 7zM5 5h3v14H5z"/></svg></button>
        <button class="round primary" data-c="toggle" aria-label="Play"><svg viewBox="0 0 24 24" id="b-icon">${PLAY}</svg></button>
        <button class="round" data-c="next" aria-label="Next"><svg viewBox="0 0 24 24"><path d="M5 5l9 7-9 7zM16 5h3v14h-3z"/></svg></button>
        <button class="round" data-c="shuffle" id="b-shuffle" aria-label="Shuffle" hidden><svg viewBox="0 0 24 24"><path d="M4 7h3.5l7 10H18v-2l3 3-3 3v-2h-4.5l-7-10H4zm10.5 0H18V5l3 3-3 3V9h-3.5l-1.6 2.3-1.2-1.7zM4 17h3.5l1.6-2.3 1.2 1.7L8.5 19H4z"/></svg></button>
      </div>
      <div class="seek"><span id="b-pos">0:00</span><input type="range" id="b-seek" min="0" max="1000" value="0" aria-label="Position"><span id="b-dur">0:00</span>
        <input type="range" class="volume" id="b-volume" min="0" max="100" value="100" aria-label="Volume"></div>
    </div>`;
    document.body.appendChild(bar);
    const $ = (id) => bar.querySelector("#" + id);
    bar.addEventListener("click", (e) => {
      const c = e.target.closest("[data-c]");
      if (c) { try { win(owner).__playerControl(c.dataset.c); } catch (err) {} setTimeout(paint, 150); return; }
      if (e.target.closest("#b-open, #b-art") && owner) {                   // back to where it's playing
        if (owner === current) { try { win(owner).__playerControl("reveal"); } catch (err) {} return; }   // (feedback #38)
        try { history.pushState({}, "", win(owner).location.pathname + win(owner).location.search); } catch (err) {}
        show(owner);
      }
    });
    $("b-volume").addEventListener("input", () => { try { win(owner).__playerControl("volume", $("b-volume").value / 100); } catch (e) {} });
    const seek = $("b-seek");
    seek.addEventListener("input", () => { seeking = true; });
    seek.addEventListener("change", () => {
      try { const s = win(owner).__playerState(); if (s && s.d) win(owner).__playerControl("seek", seek.value / 1000 * s.d); } catch (e) {}
      seeking = false;
    });
  }
  let lastArt = null;
  // One bar for every page, so it doesn't change as I move around: over other pages, and on a playlist page in
  // place of its own player (Find music's own controls are its player, so not there).
  function paint() {
    if (!bar) return;
    let s = null;
    try {
      const w = win(owner);
      if (w && w.__playerState && (owner !== current || w.__barOnPage)) s = w.__playerState();
    } catch (e) {}
    const on = !!(s && playing(owner));
    bar.hidden = !on;
    try { win(current).document.body.classList.toggle("mh-now-on", on); } catch (e) {}
    if (!on) return;
    const $ = (id) => bar.querySelector("#" + id);
    $("b-title").textContent = s.title || ""; $("b-artist").textContent = s.artist || "";
    if (lastArt !== (s.art || "")) {
      lastArt = s.art || "";
      $("b-art").src = lastArt; $("b-art").style.visibility = lastArt ? "visible" : "hidden";
      Tint.apply(lastArt || null, bar);                     // grey with a hint of the song's cover (none while hidden)
    }
    $("b-note").textContent = s.note || "";
    bar.querySelector('[data-c="toggle"]').classList.toggle("loading", !!s.loading);
    if (s.volume !== undefined && document.activeElement !== $("b-volume")) $("b-volume").value = Math.round(s.volume * 100);
    $("b-icon").innerHTML = s.playing ? PAUSE : PLAY;
    bar.querySelector('[data-c="toggle"]').setAttribute("aria-label", s.playing ? "Pause" : "Play");
    $("b-down").hidden = $("b-up").hidden = !s.find;                  // Find music playing: its thumbs
    $("b-down").classList.toggle("on", s.thumb === 1); $("b-up").classList.toggle("on", s.thumb === 3);
    const sh = $("b-shuffle"); sh.hidden = s.shuffle === null || s.shuffle === undefined; sh.classList.toggle("on", !!s.shuffle);
    $("b-dur").textContent = fmt(s.d);
    if (!seeking) { $("b-pos").textContent = fmt(s.t); $("b-seek").value = s.d ? Math.round(1000 * s.t / s.d) : 0; }
  }
  setInterval(() => { paint(); if (current) syncAddress(false); }, 500);

  document.addEventListener("DOMContentLoaded", () => {
    build();
    // Arrived as /shell#<page>: the address bar shows the page itself from here on.
    const target = (location.pathname === "/shell" ? location.hash.slice(1) : location.pathname + location.search) || "/";
    history.replaceState({}, "", target);
    show(frame(target));
  });
  // "Add to queue" on a page that isn't the playing one: the song goes to the playing page's queue.
  function enqueue(track, from) {
    const w = win(owner);
    if (!w || w === from || !playing(owner)) return null;          // nothing playing elsewhere: the page queues it
    if (!w.__enqueue) return "unsupported";                          // Find music chooses its own next track
    w.__enqueue(track);
    setTimeout(paint, 100);
    return "queued";
  }
  return {open, enqueue};
})();
