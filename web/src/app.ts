// The app's one player and its engine (SPEC.md §20.3): the browser's audio, or in the phone app its native player.
import { api, type Playlist } from "./core/api";
import { BrowserEngine } from "./core/engine";
import { playlistModel } from "./core/library";
import { NativeEngine } from "./core/native";
import { Player } from "./core/player";
import { go, route } from "./core/router";
import { fullView, menu, toast } from "./ui/overlay";

export const inApp = typeof window !== "undefined" && !!window.MHApp;
export const engine = inApp ? new NativeEngine(window.MHApp!) : new BrowserEngine();
export const player = new Player(engine, {
  played: (pid) => {
    void fetch(`/api/playlists/${pid}/played`, {method: "POST"}).catch(() => {});
    remember(pid);
  },
  added: (pid) => void playlistModel(pid).load(),      // a + shows up in To rate (and leaves again if taken back)
  notice: toast,
});

export const fmt = (s: number | null | undefined): string => {
  if (s == null || !isFinite(s) || s < 0) return "0:00";
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};

// --- the phone app -----------------------------------------------------------------------------------------------

declare global {
  interface Window {
    __mhBack?: () => boolean;
    __mhShowPlaying?: () => void;
  }
}

// What was playing last (feedback #60): reopening the app brings back that playlist with its song loaded and paused
// (Find music: its latest pick), within two weeks; not the place in the song.
function remember(pid: number): void {
  const s = player.state.get();
  try {
    localStorage.setItem("lastPlaying", JSON.stringify({pid, mode: s.source?.kind === "find" ? "find" : "playlist",
                                                        track_id: s.track?.id ?? null, at: Date.now()}));
  } catch { /* only a convenience */ }
}

async function resume(): Promise<void> {
  let last: {pid: number; mode: string; track_id: number | null; at: number} | null = null;
  try { last = JSON.parse(localStorage.getItem("lastPlaying") || "null"); } catch { /* none */ }
  if (!last || Date.now() - last.at > 14 * 86_400_000) return;
  if (route.get().name === "home") go(`/playlist/${last.pid}`, true);
  try {
    const p = await api<Playlist>(`/api/playlists/${last.pid}`);
    if (player.state.get().track) return;               // something started meanwhile
    if (last.mode === "find") player.startFind(p, false);
    else if (last.track_id && p.tracks.some((t) => t.id === last!.track_id && t.audio)) player.play(p, {first: last.track_id, paused: true});
  } catch { /* the playlist, as it is */ }
}

if (inApp) {
  // The phone's back button goes up a level: the full view or a menu closes first; Results to its playlist, a
  // playlist to the playlists; at the top the app goes to the background (false).
  window.__mhBack = () => {
    if (menu.get()) { menu.set(null); return true; }
    const open = document.querySelector("dialog[open]") as HTMLDialogElement | null;
    if (open) { open.dispatchEvent(new Event("cancel")); if (open.open) open.close("cancel"); return true; }
    if (fullView.get()) { fullView.set(false); return true; }
    const r = route.get();
    if (r.name === "results") { go(`/playlist/${r.id}`); return true; }
    if (r.name !== "home") { go("/"); return true; }
    return false;
  };
  window.__mhShowPlaying = () => { if (player.state.get().track) fullView.set(true); };   // the notification was tapped
  void resume();
}
