// The one player (SPEC.md §20.3): alive for as long as the app is open, whatever screen is showing. Screens read its
// state and call its methods; nothing else plays sound: a playlist's songs, or its Find music's picks (find.ts).
import type { Playlist, Track } from "./api";
import type { Engine, EngineEvent, Meta } from "./engine";
import { asTrack, DOWN, FindDriver, findCopies, UP } from "./find";
import { buildQueue, playable, ShuffleRound } from "./queue";
import { Store } from "./store";
import { playlistCopies, TempoFollower } from "./tempo";

export interface Source {
  kind: "playlist" | "find";
  pid: number;
  name: string;
  image: string | null;
  deleted?: boolean;     // playing through Recently deleted
}

export interface PlayerState {
  source: Source | null;
  track: Track | null;
  pos: number;
  length: number;
  playing: boolean;
  loading: boolean;
  t: number;
  d: number;
  rate: number;          // what's loaded (1 = the original)
  note: string;          // "Set to 142 BPM", "Switching to 142 BPM…"
  shuffle: boolean;
  prevTrack: Track | null;
  nextTrack: Track | null;
  vote: number | null;   // Find music: the − (1) or + (3) lit for the pick on air
  canBack: boolean;
}

export interface Hooks {
  played(pid: number): void;            // a song of this playlist (or its Find music) started
  added(pid: number): void;             // Find music's + changed the playlist's To rate
  adding?(pid: number, t: Track, on: boolean): void;   // … shown there at once
  notice(text: string): void;           // a short note ("Added to …", a failure)
}

const defaultHooks: Hooks = {
  played: (pid) => void fetch(`/api/playlists/${pid}/played`, {method: "POST"}).catch(() => {}),
  added: () => {},
  notice: () => {},
};

const meta = (t: Track) => ({title: t.title, artist: t.artist, art: t.art});

const EMPTY: PlayerState = {
  source: null, track: null, pos: -1, length: 0, playing: false, loading: false, t: 0, d: 0, rate: 1, note: "",
  shuffle: false, prevTrack: null, nextTrack: null, vote: null, canBack: false,
};

export class Player {
  readonly state = new Store<PlayerState>(EMPTY);
  private all: Track[] = [];
  private queue: Track[] = [];
  private pos = -1;
  private upNext: Track[] = [];
  private round: ShuffleRound | null = null;
  private shuffle = false;
  private source: Source | null = null;
  private backRestart = 5;
  private headroom = 6;
  private minStars = 0;
  private order: {pid: number; ids: number[]} | null = null;   // Sort by sound like this: Play follows that order
  private tempo: TempoFollower;
  private fm: FindDriver | null = null;
  // While the next song is being fetched, the bar shows its start, not the song being left: that one's last events
  // (its pause) arrived meanwhile and flashed its old position and length on the slider (2026-11-20).
  private held: {t: number; d: number} | null = null;
  private findTempo: {on: boolean; bpm: number | null; target: number} = {on: false, bpm: null, target: 0};
  private hooks: Hooks;

  constructor(private engine: Engine, hooks: Partial<Hooks> | ((pid: number) => void) = {}) {
    this.hooks = {...defaultHooks, ...(typeof hooks === "function" ? {played: hooks} : hooks)};
    this.tempo = new TempoFollower(playlistCopies(0), (url, rate, was) => {
      const at = this.engine.time * was / rate, playing = !this.engine.paused;
      const t = this.fm?.onAir() ? asTrack(this.fm.onAir()!) : this.queue[this.pos];
      this.engine.load(url, at, t ? meta(t) : undefined);
      if (playing) void this.engine.play().catch(() => {});
    }, () => this.publish());
    engine.on((e) => this.onEngine(e));
    this.mediaHandlers();
  }

  // --- what screens call -----------------------------------------------------------------------------------------

  /** A playlist's latest data (the playlist screen passes it on every load): rates follow a changed Set to BPM. */
  update(p: Playlist): void {
    if (!this.source || this.source.pid !== p.id) return;
    if (this.fm) {
      const t = p.tempo, was = this.findTempo;
      this.findTempo = {on: t.on, bpm: t.bpm, target: t.target};
      this.headroom = p.phone_headroom_db ?? 6;
      this.backRestart = p.back_restart_s ?? 5;
      this.source = {...this.source, name: p.name, image: p.image};
      if (was.on !== t.on || was.target !== t.target) void this.fm.followTempo(t);
      this.publish();
      return;
    }
    const filter = this.minStars;
    this.take(p);
    const t = this.queue[this.pos];
    if (t && t.audio) void this.tempo.retune(t.id, t.audio, this.rateOf(t));
    if (filter !== this.minStars && !this.source.deleted) this.rebuildRest();   // the star filter changed
    this.publish();
  }

  /** Sort by sound like this (`ids`), or back to normal order (null): Play follows the order shown. */
  setOrder(pid: number, ids: number[] | null): void {
    this.order = ids ? {pid, ids} : null;
    if (this.source?.pid === pid && !this.source.deleted) this.rebuildRest();
    this.publish();
  }

  /** A song in Recently deleted: it plays, then the rest of that list (no shuffle, no star filter). */
  playDeleted(p: Playlist, trackId: number): void {
    this.leaveFind();
    const list = (p.deleted ?? []).filter((t) => t.audio).map((t) => ({...t, rate: 1}));
    const i = list.findIndex((t) => t.id === trackId);
    if (i < 0) return;
    this.source = {kind: "playlist", pid: p.id, name: p.name, image: p.image, deleted: true};
    this.take(p);
    this.shuffle = false;
    this.tempo.setSource(playlistCopies(p.id));
    this.queue = list.slice(i);
    void this.playAt(0);
  }

  /** Play a playlist: from the top, from a tapped song (`first`), shuffled or not. */
  play(p: Playlist, opts: {first?: number | null; shuffle?: boolean; paused?: boolean} = {}): void {
    this.leaveFind();
    const same = this.source?.pid === p.id && this.source.kind === "playlist";
    this.source = {kind: "playlist", pid: p.id, name: p.name, image: p.image};
    this.take(p);
    this.round = new ShuffleRound(String(p.id));
    this.shuffle = opts.shuffle ?? (same ? this.shuffle : this.round.on);
    this.round.on = this.shuffle;
    this.tempo.setSource(playlistCopies(p.id));
    this.queue = buildQueue(this.all, this.list(), {
      first: opts.first, shuffle: this.shuffle ? this.round : null, upNext: this.upNext,
    });
    this.upNext = [];
    if (this.queue.length) void this.playAt(0, !opts.paused);
  }

  /** Whether a song is the one playing (tapping it again doesn't restart it). */
  isCurrent(pid: number, trackId: number): boolean {
    return this.source?.kind === "playlist" && this.source.pid === pid && !this.source.deleted && this.queue[this.pos]?.id === trackId;
  }

  // --- Find music ------------------------------------------------------------------------------------------------

  /** Find music for a playlist, in the bar: the song that was on air when it was left (the Pi knows it), else a pick. */
  startFind(p: Playlist, autoplay = true): void {
    this.leaveFind();
    this.engine.pause();
    this.queue = []; this.pos = -1; this.upNext = [];
    this.source = {kind: "find", pid: p.id, name: p.name, image: p.image};
    this.headroom = p.phone_headroom_db ?? 6;
    this.backRestart = p.back_restart_s ?? 5;
    this.findTempo = {on: p.tempo.on, bpm: p.tempo.bpm, target: p.tempo.target};
    this.tempo.setSource(findCopies(p.id));
    const fm = this.fm = new FindDriver(p.id, p.name, {
      engine: this.engine, tempo: this.tempo,
      headroom: () => this.headroom, backRestart: () => this.backRestart,
      targetBpm: () => this.findTempo.target || null,
      publish: (extra) => { if (this.fm === fm) this.publish(extra); },
      media: (t) => this.media(t),
      hold: (h) => { if (this.fm === fm) this.held = h; },
      preload: (id, audio, rate, m, still) => this.preloadFor(id, audio, rate, m, still),
      played: (pid) => this.hooks.played(pid),
      added: (pid) => this.hooks.added(pid),
      adding: (pid, t, on) => this.hooks.adding?.(pid, t, on),
      notice: (text) => this.hooks.notice(text),
      ended: () => { if (this.fm === fm) this.leaveFind(); },
    });
    this.publish({loading: true});
    void fm.start(p.tempo, autoplay);
  }

  /** Leaving Find music (Play, a song, Find music again): it stops; coming back plays the pick that was on air. */
  leaveFind(): void {
    if (!this.fm) return;
    this.held = null;
    this.fm.stop();
    this.fm = null;
    this.engine.pause();
    this.source = null;
    this.publish({playing: false, loading: false});
  }

  isFinding(pid: number): boolean {
    return this.source?.kind === "find" && this.source.pid === pid;
  }

  /** − (Off: moves on at once) or + (Add: into To rate, plays on); the lit one again takes it back to heard. */
  vote(r: "down" | "up"): void {
    void this.fm?.rate(r === "down" ? DOWN : UP);
  }

  isCurrentDeleted(pid: number, trackId: number): boolean {
    return this.source?.pid === pid && !!this.source.deleted && this.queue[this.pos]?.id === trackId;
  }

  toggle(): void {
    if (this.pos < 0 && !this.fm?.onAir()) return;
    if (this.engine.paused) void this.engine.play().catch(() => {});
    else this.engine.pause();
  }

  next(): void {
    if (this.fm) { void this.fm.skip(); return; }
    if (this.pos + 1 < this.queue.length) void this.playAt(this.pos + 1);
  }

  /** Back: a few seconds in, the song restarts; before that, the song before it. `force`: always the song before
   *  (swiping the cover). */
  prev(force = false): void {
    if (this.fm) { void this.fm.back(force); return; }
    if (this.pos < 0) return;
    if (this.pos === 0 || (!force && this.engine.time >= this.backRestart)) this.engine.seek(0);
    else void this.playAt(this.pos - 1);
  }

  seek(seconds: number): void {
    if (this.pos >= 0 || this.fm) this.engine.seek(seconds);
  }

  /** Shuffle on or off: the song playing stays, the rest is reshuffled (or put back in order). */
  setShuffle(on: boolean): void {
    if (this.fm) return;
    this.shuffle = on;
    if (this.round) this.round.on = on;
    if (this.pos >= 0 && this.round && !this.source?.deleted) {
      if (on) this.round.mark(this.queue[this.pos].id);
      this.rebuildRest();
    }
    this.publish();
  }

  /** Add to queue: after the song playing, ahead of the rest, first added first; while nothing plays, first. A song
   *  from another playlist's page plays at the playing playlist's Set to BPM (#63). */
  async enqueue(t: Track, fromPid?: number): Promise<"queued" | "find"> {
    if (this.fm) return "find";                         // Find music picks its own next track
    const item = {...t};
    const pid = this.source?.pid;
    if (pid != null && fromPid != null && pid !== fromPid && !this.all.some((x) => x.id === t.id)) {
      try {
        const r = await fetch(`/api/playlists/${pid}/rate/${t.id}`);
        if (r.ok) Object.assign(item, await r.json());
      } catch { /* its own tempo, then */ }
    }
    if (this.pos >= 0) this.queue.splice(this.pos + 1 + this.pendingQueued(), 0, item);
    else this.upNext.push(item);
    this.queued.add(item);
    this.preloadNext();
    this.publish();
    return "queued";
  }

  setVolume(v: number): void {
    this.engine.setVolume(v);
  }

  // --- inside -------------------------------------------------------------------------------------------------------

  private queued = new WeakSet<Track>();

  /** The playable songs in the order shown (sorted by sound, if it is), star filter applied. */
  private list(): Track[] {
    const ids = this.order && this.order.pid === this.source?.pid ? this.order.ids : undefined;
    return playable(this.all, this.minStars, ids);
  }

  /** The song playing stays, then any songs added with Add to queue; the rest follows the filter, order and shuffle. */
  private rebuildRest(): void {
    if (this.pos < 0) return;
    const cur = this.queue[this.pos];
    const waiting = this.queue.slice(this.pos + 1, this.pos + 1 + this.pendingQueued());
    const rest = this.list().filter((t) => t.id !== cur.id && !waiting.some((w) => w.id === t.id));
    this.queue = [cur, ...waiting, ...(this.shuffle && this.round ? this.round.order(rest) : rest)];
    this.pos = 0;
    this.preloadNext();
  }

  private pendingQueued(): number {
    let n = 0;
    while (this.queue[this.pos + 1 + n] && this.queued.has(this.queue[this.pos + 1 + n])) n++;
    return n;
  }

  private take(p: Playlist): void {
    this.all = p.tracks;
    this.backRestart = p.back_restart_s ?? 5;
    this.headroom = p.phone_headroom_db ?? 6;
    this.minStars = p.min_stars || 0;
    if (this.source) this.source = {...this.source, name: p.name, image: p.image};
  }

  private fresh(t: Track): Track {
    if (this.source?.deleted) return t;
    return this.all.find((x) => x.id === t.id) ?? t;
  }

  private rateOf(t: Track): number {
    return this.fresh(t).rate || 1;
  }

  private async playAt(i: number, autoplay = true): Promise<void> {
    this.pos = i;
    const t = this.queue[i];
    if (!t || !t.audio) return;
    if (this.shuffle) this.round?.mark(t.id);
    const t0 = performance.now();
    this.engine.pause();                                  // the song being left stops now (#53)
    this.held = {t: 0, d: t.duration_s || 0};
    this.publish({loading: true});
    this.engine.setGain(t.gain_db || 0, this.headroom);
    this.media(t);
    if (this.source) this.hooks.played(this.source.pid);
    const src = await this.tempo.start(t.id, t.audio, this.rateOf(t));
    if (this.queue[this.pos] !== t) return;              // another song was chosen meanwhile
    // How long it takes to start (logged on the Pi once it plays): the wait for the copy's status, then the engine's.
    this.starting = {pid: this.source?.pid ?? null, track_id: t.id, t0, status_ms: Math.round(performance.now() - t0),
                     copy: src !== t.audio, ahead: !!this.ahead && this.aheadUrl === src};
    this.engine.load(src, 0, meta(t));
    this.held = null;
    if (!autoplay) this.publish({loading: false, playing: false});
    else {
      try {
        await this.engine.play();
      } catch {
        this.publish({loading: false});
      }
    }
    this.preloadNext();
  }

  private async preloadNext(): Promise<void> {
    const n = this.queue[this.pos + 1];
    if (!n || !n.audio) return;
    await this.preloadFor(n.id, n.audio, this.rateOf(n), meta(n), () => this.queue[this.pos + 1] === n);
  }

  // What was fetched ahead. If that was the original because the song's stretched copy wasn't made yet, it's checked
  // again near the end of the song playing, so the app's player goes on to the copy (else the page would swap to the
  // copy a moment after the song started, starting it again).
  private ahead: {id: number; audio: string; rate: number; got: number; at: number; meta: Meta; still: () => boolean} | null = null;

  private starting: {pid: number | null; track_id: number; t0: number; status_ms: number; copy: boolean; ahead: boolean} | null = null;
  private aheadUrl: string | null = null;

  private reportStart(): void {
    const s = this.starting;
    if (!s) return;
    this.starting = null;
    const {t0, ...rest} = s;
    void fetch("/api/player-event", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind: "start", data: {...rest, latency_ms: Math.round(performance.now() - t0)}})}).catch(() => {});
  }

  private async preloadFor(id: number, audio: string, rate: number, m: Meta, still: () => boolean): Promise<void> {
    const at = Date.now();
    const p = await this.tempo.peek(id, audio, rate);
    if (!still()) return;
    this.engine.preload(p.url, m);
    this.aheadUrl = p.url;
    this.ahead = {id, audio, rate, got: p.rate, at, meta: m, still};
  }

  private checkAhead(): void {
    const a = this.ahead, left = this.engine.duration - this.engine.time;
    if (!a || a.got === a.rate || !a.still() || !(left < 45) || Date.now() - a.at < 10_000) return;
    a.at = Date.now();
    void this.preloadFor(a.id, a.audio, a.rate, a.meta, a.still);
  }

  private onEngine(e: EngineEvent): void {
    if (e.type === "action") {
      if (e.name === "nexttrack") this.next();
      else if (e.name === "previoustrack") this.prev();
      else this.vote(e.name === "vote_up" ? "up" : "down");
      return;
    }
    if (this.fm) {
      if (e.type === "ended") return this.fm.ended();
      if (e.type === "playing") this.fm.playing();
      if (e.type === "time") this.fm.time();
      if (e.type === "error") this.fm.recover(this.fm.onAir(), "audio_error", {message: e.message.slice(0, 120)});
    }
    switch (e.type) {
      case "time":
        this.checkAhead();
        this.publish();
        return;
      case "ended":
        if (this.pos + 1 < this.queue.length) void this.playAt(this.pos + 1);
        else this.publish({playing: false});
        return;
      case "playing":
        this.reportStart();
        this.publish({playing: true, loading: false});
        return;
      case "pause":
        this.publish({playing: false, loading: false});
        return;
      case "waiting":
        this.publish({loading: true});
        return;
      case "error":
        this.publish({loading: false});
        return;
      default:
        this.publish();
    }
  }

  private votesShown: number | null | undefined;

  /** The song's length as it plays: the engine's once it knows, else the song's own over the copy's rate (a stretched
   *  copy is shorter or longer), so the slider doesn't shift when the engine's figure arrives. */
  private length(f: Track | null): number {
    return this.engine.duration || (f?.duration_s ? f.duration_s / (this.tempo.loaded || 1) : 0);
  }

  private publish(extra: Partial<PlayerState> = {}): void {
    const v = this.fm ? this.fm.vote() ?? 0 : null;
    if (v !== this.votesShown) { this.votesShown = v; this.engine.setVotes?.(v); }
    if (this.fm) return this.publishFind(extra);
    const t = this.pos >= 0 ? this.queue[this.pos] : null;
    const f = t ? this.fresh(t) : null;
    const prev = this.state.get();
    const want = f ? f.rate || 1 : 1;
    const note = f && f.tempo_note
      ? (want !== 1 && !this.held && this.tempo.loaded !== want ? f.tempo_note.replace("Set to", "Switching to") + "…" : f.tempo_note) : "";
    this.state.set({
      ...prev,
      source: this.source, track: f, pos: this.pos, length: this.queue.length,
      playing: extra.playing ?? (t ? !this.engine.paused : false),
      loading: extra.loading ?? prev.loading,
      t: extra.t ?? this.held?.t ?? this.engine.time, d: extra.d ?? this.held?.d ?? this.length(f),
      rate: this.tempo.loaded, note, shuffle: this.shuffle,
      prevTrack: this.queue[this.pos - 1] ?? null, nextTrack: this.queue[this.pos + 1] ?? null,
      vote: null, canBack: this.pos > 0,
    });
  }

  private publishFind(extra: Partial<PlayerState>): void {
    const fm = this.fm!, p = fm.onAir(), prev = this.state.get();
    const f = p ? asTrack(p) : null;
    const want = f ? f.rate || 1 : 1;
    const note = f && f.tempo_note
      ? (want !== 1 && !this.held && this.tempo.loaded !== want ? f.tempo_note.replace("Set to", "Switching to") + "…" : f.tempo_note) : "";
    const {older, newer} = fm.neighbours();
    this.state.set({
      ...prev,
      source: this.source, track: f, pos: f ? 1 : -1, length: 0,
      playing: extra.playing ?? (f ? !this.engine.paused : false),
      loading: extra.loading ?? prev.loading,
      t: extra.t ?? this.held?.t ?? this.engine.time, d: extra.d ?? this.held?.d ?? this.length(f),
      rate: this.tempo.loaded, note, shuffle: false,
      prevTrack: older ? asTrack(older) : null, nextTrack: newer ? asTrack(newer) : null,
      vote: fm.vote(), canBack: fm.canBack(),
    });
  }

  // Lock screen / notification controls (Chrome on the phone, and computers' media keys).
  private media(t: Track): void {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title, artist: t.artist, artwork: t.art ? [{src: new URL(t.art, location.href).href}] : [],
    });
  }

  private mediaHandlers(): void {
    if (typeof navigator === "undefined" || !("mediaSession" in navigator)) return;
    const ms = navigator.mediaSession;
    ms.setActionHandler("play", () => this.toggle());
    ms.setActionHandler("pause", () => this.toggle());
    ms.setActionHandler("nexttrack", () => this.next());
    ms.setActionHandler("previoustrack", () => this.prev());
    try { ms.setActionHandler("seekto", (d) => { if (d.seekTime != null) this.seek(d.seekTime); }); } catch { /* old browsers */ }
  }
}

