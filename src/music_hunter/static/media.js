// Phone media controls and loudness levelling (v5).
// - Media Session: title, artist and cover on the lock screen / notification, with play, pause, next, previous and
//   seeking. Find music passes no names or cover until a track is rated.
// - Levelling: on computers through Web Audio (it can raise quiet tracks, and carries the volume slider); on touch
//   screens through the element's own volume, so Chrome treats it as plain audio playback and shows its controls —
//   everything a little quieter (headroom) so quiet tracks can still be raised to the same loudness.
const Media = (() => {
  const desktop = matchMedia("(hover: hover) and (pointer: fine)").matches;
  const ms = "mediaSession" in navigator ? navigator.mediaSession : null;

  function metadata({title, artist, album, art}) {
    if (!ms) return;
    const artwork = [{src: new URL(art || "/static/icon-512.png", location.href).href, sizes: "512x512",
                      type: art ? "image/jpeg" : "image/png"}];
    ms.metadata = new MediaMetadata({title: title || "", artist: artist || "", album: album || "", artwork});
  }
  function handlers(map) {
    if (!ms) return;
    for (const [action, fn] of Object.entries(map)) {
      try { ms.setActionHandler(action, fn); } catch (e) { /* not supported here */ }
    }
  }
  function position(audio) {
    if (!ms || !ms.setPositionState || !isFinite(audio.duration) || !audio.duration) return;
    try {
      ms.setPositionState({duration: audio.duration, position: Math.min(audio.currentTime, audio.duration),
                           playbackRate: audio.playbackRate || 1});
    } catch (e) {}
  }
  function playing(on) { if (ms) ms.playbackState = on ? "playing" : "paused"; }
  // Phone levelling: the element's volume (0–1) for a track's gain, with `headroom` dB to spare.
  function phoneVolume(gainDb, headroom) { return Math.min(1, Math.pow(10, ((gainDb || 0) - headroom) / 20)); }

  return {desktop, metadata, handlers, position, playing, phoneVolume};
})();
