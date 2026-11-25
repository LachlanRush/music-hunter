// A playlist's data and everything done to it. Shown at once from what was seen last; every change is shown straight
// away and the Pi confirms it behind (on failure the Pi's view comes back with a message). Plain TypeScript, so any
// screen can use it.
import { api, busy, cached, keep, type DeletedTrack, type Playlist, type Track } from "./api";
import { Store } from "./store";

const nowStamp = () => new Date().toISOString().replace("T", " ").slice(0, 23);

// The playlist's order, as the Pi keeps it: To rate first, newest first; then by stars, 5 at the top, unstarred last;
// within the same stars my dragged order, then the most recently starred first.
export function sortTracks(list: Track[]): Track[] {
  const added = (t: Track) => (t.status === "added" ? 1 : 0);
  const cmp = (a: string, b: string) => (a < b ? 1 : a > b ? -1 : 0);
  return [...list].sort((a, b) =>
    (added(b) - added(a))
    || (added(a) && added(b) ? cmp(a.added_at || "", b.added_at || "") : 0)
    || (Number(a.stars == null) - Number(b.stars == null))
    || ((b.stars || 0) - (a.stars || 0))
    || (Number(a.position != null) - Number(b.position != null))
    || ((a.position ?? 0) - (b.position ?? 0))
    || cmp(a.starred_at || a.added_at || "", b.starred_at || b.added_at || "")
    || cmp(a.added_at || "", b.added_at || "")
    || (b.id - a.id));
}

export class PlaylistModel {
  readonly data: Store<Playlist | null>;
  readonly error = new Store<string>("");
  private settleTimer: ReturnType<typeof setTimeout> | undefined;
  private pollTimer: ReturnType<typeof setTimeout> | undefined;
  private watching = 0;
  private watchTimer: ReturnType<typeof setInterval> | undefined;
  private loadFailed = false;     // the message is a failed load's (cleared by the next load), not a failed change's
  private edits = 0;              // changes shown at once so far
  private sending = 0;            // … whose request hasn't been answered yet

  constructor(readonly id: number) {
    this.data = new Store<Playlist | null>(cached<Playlist>(this.key));
  }

  private get key(): string {
    return `next:playlist:${this.id}`;
  }

  private get p(): Playlist | null {
    return this.data.get();
  }

  /** The Pi's view. Not applied over changes made after it was asked for, or still on their way (feedback #118: ten
   *  songs deleted in a row came back, from a view fetched between the deletions); it's asked for again once quiet. */
  async load(force = false): Promise<void> {
    const at = this.edits;
    try {
      const p = await api<Playlist>(`/api/playlists/${this.id}`);
      if (!force && (this.sending > 0 || this.edits !== at)) { this.settleLater(); return; }
      this.data.set(p);
      keep(this.key, p);
      if (this.loadFailed) this.error.set("");
      this.loadFailed = false;
      clearTimeout(this.pollTimer);
      if (busy(p.job)) this.pollTimer = setTimeout(() => this.load(), 3000);   // an import's progress
    } catch (e) {
      this.error.set(`Couldn't load the playlist: ${(e as Error).message}`);
      this.loadFailed = true;
    }
  }

  /** While the playlist is on screen: an import or add started elsewhere shows up within a few seconds. */
  watch(): () => void {
    if (this.watching++ === 0) {
      this.watchTimer = setInterval(async () => {
        if (busy(this.p?.job) || document.hidden) return;
        try {
          const r = await api<{job: Playlist["job"]}>(`/api/playlists/${this.id}/job`);
          if (busy(r.job)) void this.load();
        } catch { /* tries again */ }
      }, 5000);
    }
    return () => { if (--this.watching === 0) clearInterval(this.watchTimer); };
  }

  /** After changes shown at once, the Pi's view is fetched once things go quiet (not under my finger). */
  settleLater(ms = 1500): void {
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.load(), ms);
  }

  private change(f: (p: Playlist) => Playlist): void {
    const p = this.p;
    if (!p) return;
    const next = f(p);
    this.edits++;
    this.data.set(next);
    keep(this.key, next);
  }

  private async send(work: Promise<unknown>, failed: string, settle = 300): Promise<boolean> {
    this.sending++;
    try {
      await work;
      this.sending--;
      if (!this.loadFailed) this.error.set("");
      this.settleLater(settle);
      return true;
    } catch (e) {
      this.sending--;
      this.error.set(`${failed}: ${(e as Error).message}`);
      void this.load(true);                             // (the Pi's view back, as it is)
      return false;
    }
  }

  /** A Find music + shown in To rate at once (or taken out again), until the Pi's view brings the real song. */
  pending(t: Track, on: boolean): void {
    const same = (x: Track) => x.status === "added" && x.artist === t.artist && x.title === t.title;
    this.change((p) => {
      const rest = p.tracks.filter((x) => !(same(x) && (!on || x.id < 0)));
      if (on && !rest.some(same)) rest.unshift({...t, id: -t.id, status: "added", added_at: nowStamp(), rate: 1});
      return {...p, tracks: sortTracks(rest)};
    });
  }

  /** Tap a star; the same one again clears it. A starred To rate song joins the list, at the top of its stars. */
  setStars(trackId: number, n: number): void {
    const t = this.p?.tracks.find((x) => x.id === trackId);
    if (!t) return;
    const stars = t.stars === n ? null : n;
    this.change((p) => ({...p, tracks: sortTracks(p.tracks.map((x) => x.id !== trackId ? x : {
      ...x, stars, starred_at: nowStamp(), position: null, status: stars != null ? "member" : x.status}))}));
    void this.send(api(`/api/playlists/${this.id}/tracks/${trackId}`, {stars}, "PUT"), "Couldn't save the stars", 4000);
  }

  /** My order within one star level (dragged). */
  reorder(ids: number[]): void {
    const at = new Map(ids.map((id, i) => [id, i]));
    this.change((p) => ({...p, tracks: sortTracks(p.tracks.map((t) => (at.has(t.id) ? {...t, position: at.get(t.id)!} : t)))}));
    void this.send(api(`/api/playlists/${this.id}/track-order`, {ids}, "PUT"), "Couldn't save the order", 60000);
  }

  /** Into Recently deleted (restorable). */
  remove(trackId: number): void {
    const t = this.p?.tracks.find((x) => x.id === trackId);
    if (!t) return;
    this.change((p) => ({...p, tracks: p.tracks.filter((x) => x.id !== trackId),
                          deleted: [{...t, deleted_at: nowStamp().slice(0, 19)}, ...(p.deleted ?? [])]}));
    void this.send(api(`/api/playlists/${this.id}/tracks/${trackId}`, undefined, "DELETE"), "Couldn't remove it");
  }

  restore(trackId: number): void {
    const t = this.p?.deleted?.find((x) => x.id === trackId);
    if (!t) return;
    this.change((p) => ({...p, deleted: (p.deleted ?? []).filter((x) => x.id !== trackId),
                          tracks: sortTracks([...p.tracks, {...t, status: "member", rate: 1}])}));
    void this.send(api(`/api/playlists/${this.id}/deleted/${trackId}/restore`, {}), "Couldn't restore it");
  }

  deleteForever(trackId: number): void {
    this.change((p) => ({...p, deleted: (p.deleted ?? []).filter((x) => x.id !== trackId)}));
    void this.send(api(`/api/playlists/${this.id}/deleted/${trackId}`, undefined, "DELETE"), "Couldn't delete it");
  }

  deleteAllForever(): void {
    this.change((p) => ({...p, deleted: []}));
    void this.send(api(`/api/playlists/${this.id}/deleted`, undefined, "DELETE"), "Couldn't delete them");
  }

  /** Add to playlist…: ticked playlists get the song (To rate there); unticked ones lose it — a move if it's still
   *  ticked somewhere (#76), else into their Recently deleted. */
  async changePlaylists(trackId: number, add: number[], drop: number[], stillSomewhere: boolean): Promise<void> {
    const moved = stillSomewhere ? "?moved=1" : "";
    if (drop.includes(this.id)) {
      const t = this.p?.tracks.find((x) => x.id === trackId);
      if (t) this.change((p) => ({...p, tracks: p.tracks.filter((x) => x.id !== trackId),
        deleted: moved ? p.deleted : [{...t, deleted_at: nowStamp().slice(0, 19)}, ...(p.deleted ?? [])]}));
    }
    try {
      await Promise.all(add.map((id) => api(`/api/playlists/${id}/tracks`, {track_id: trackId})));   // there first
      await Promise.all(drop.map((id) => api(`/api/playlists/${id}/tracks/${trackId}${moved}`, undefined, "DELETE")));
      this.settleLater(300);
    } catch (e) {
      this.error.set(`Couldn't change the playlists: ${(e as Error).message}`);
      void this.load();
    }
  }

  setFilter(minStars: number): void {
    this.change((p) => ({...p, min_stars: minStars}));
    void this.send(api(`/api/playlists/${this.id}/play-filter`, {min_stars: minStars}, "PUT"), "Couldn't save", 60000);
  }

  /** Set to BPM: shown at once, the Pi follows (feedback #122: a typed 143 was saved, but a view fetched just before
   *  put the old number back in the box). The songs' rates come with the Pi's view. */
  /** Only what changed is sent (#136: ticking just after typing a number sent the old number with it). */
  async saveTempo(c: {on?: boolean; bpm?: number | null}): Promise<void> {
    this.change((p) => {
      const t = {...p.tempo, ...c};
      if ("bpm" in c) t.target = c.bpm ?? p.tempo.auto ?? p.tempo.target;
      return {...p, tempo: t};
    });
    await this.send(api(`/api/playlists/${this.id}/tempo`, c, "PUT"), "Couldn't save", 0);
  }

  async ownBpm(trackId: number, bpm: number): Promise<void> {
    try {
      await api(`/api/playlists/${this.id}/tracks/${trackId}/bpm`, {bpm}, "PUT");
      await this.load();
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }

  async details(name: string, description: string): Promise<void> {
    this.change((p) => ({...p, name, description}));
    await this.send(api(`/api/playlists/${this.id}`, {name, description}, "PATCH"), "Couldn't save", 0);
  }

  async picture(file: Blob): Promise<void> {
    try {
      const r = await fetch(`/api/playlists/${this.id}/cover`, {method: "POST", body: await file.arrayBuffer()});
      if (!r.ok) throw new Error(((await r.json().catch(() => ({}))) as {detail?: string}).detail || "Couldn't use that picture");
      this.error.set("");
      await this.load();
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }

  async coverFrom(trackId: number): Promise<void> {
    await this.send(api(`/api/playlists/${this.id}/cover/track/${trackId}`, {}), "Couldn't use that cover", 0);
  }

  async soundOrder(trackId: number): Promise<number[] | null> {
    try {
      return (await api<{order: number[]}>(`/api/playlists/${this.id}/sound-order/${trackId}`)).order;
    } catch (e) {
      this.error.set(`Couldn't sort: ${(e as Error).message}`);
      return null;
    }
  }

  async addSong(link: string): Promise<boolean> {
    return this.send(api(`/api/playlists/${this.id}/add`, {link}), "Couldn't add it", 0);
  }

  /** Try the last import (or added song) again: what was imported is kept, the rest tried again. */
  async retry(): Promise<void> {
    const job = this.p?.job;
    if (!job) return;
    await this.send(job.kind === "add" ? api(`/api/playlists/${this.id}/add`, {link: job.payload_link || ""})
                                       : api(`/api/playlists/${this.id}/import`, {}), "Couldn't start the import", 0);
  }

  async refresh(): Promise<void> {
    await this.send(api(`/api/playlists/${this.id}/import`, {}), "Couldn't refresh", 0);
  }

  async setFolder(folderId: number | null): Promise<void> {
    await this.send(api(`/api/playlists/${this.id}/folder`, {folder_id: folderId}, "PUT"), "Couldn't move it", 0);
  }

  async setSync(on: boolean): Promise<void> {
    this.change((p) => ({...p, sync: {...(p.sync ?? {tracks: 0, ready: 0}), on}}));
    await this.send(api(`/api/playlists/${this.id}/sync`, {on}, "PUT"), "Couldn't change Sync", 0);
  }

  async merge(into: number): Promise<boolean> {
    try {
      await api(`/api/playlists/${this.id}/merge`, {into});
      return true;
    } catch (e) {
      this.error.set(`Couldn't merge: ${(e as Error).message}`);
      return false;
    }
  }

  async remove_playlist(): Promise<boolean> {
    try {
      await api(`/api/playlists/${this.id}`, undefined, "DELETE");
      return true;
    } catch (e) {
      this.error.set(`Couldn't delete it: ${(e as Error).message}`);
      return false;
    }
  }
}

const models = new Map<number, PlaylistModel>();

export function playlistModel(id: number): PlaylistModel {
  let m = models.get(id);
  if (!m) {
    m = new PlaylistModel(id);
    models.set(id, m);
  }
  return m;
}

export async function findSimilar(trackId: number): Promise<number> {
  return (await api<{playlist_id: number}>(`/api/tracks/${trackId}/similar`, {})).playlist_id;
}

export type { DeletedTrack };
