// Find music in the one player (SPEC.md §19.3): random picks from the playlist's pool, − / + (Off / Add), next or a song
// ending without either = heard (Close). The next pick is fetched ahead, names and all, so − and next start it at once
// while the Pi confirms behind. Back steps through the last few picks, whose − / + can still be changed. A pick that
// doesn't start is loaded again from the plain file, then skipped (#85).
import type { Track } from "./api";
import type { Engine } from "./engine";
import type { TempoFollower, TempoSource } from "./tempo";

export const DOWN = 1, HEARD = 2, UP = 3;

/** A pick, as the Pi describes it (web.py track_info). */
export interface Pick {
  play_id: number | null;   // null: started ahead of the Pi's confirmation
  track_id: number;
  artist: string | null;
  title: string | null;
  art: string | null;
  rating: number | null;
  duration_s: number | null;
  gain_db: number;
  bpm: number | null;
  rate: number;
  tempo_note: string;
  audio: string;
  can_change?: boolean;
  heard?: boolean;          // its − / + taken back (stored as heard)
  retried?: boolean;
}

export function findCopies(pid: number): TempoSource {
  return {
    async status(id, rate, ahead = false) {
      try {
        const r = await fetch(`/find/${pid}/tempo/${id}?rate=${rate}${ahead ? "&ahead=1" : ""}`);
        return r.ok ? await r.json() : {state: "failed"};
      } catch {
        return {state: "failed"};
      }
    },
  };
}

/** What the find driver needs from the player. */
export interface FindHost {
  engine: Engine;
  tempo: TempoFollower;
  headroom(): number;
  backRestart(): number;
  targetBpm(): number | null;
  publish(extra?: {loading?: boolean; t?: number; d?: number}): void;
  media(t: Track): void;
  hold(h: {t: number; d: number} | null): void;     // what the slider shows until the song is loaded
  preload(id: number, audio: string, rate: number, meta: {title: string; artist: string; art: string | null},
          still: () => boolean): Promise<void>;
  played(pid: number): void;
  added(pid: number): void;            // a + or a taken-back + : the playlist's To rate follows
  notice(text: string): void;
  ended(): void;                       // Find music stopped (an error): the player forgets it
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const meta = (p: Pick) => ({title: p.title ?? "", artist: p.artist ?? "", art: p.art});

export function asTrack(p: Pick): Track {
  return {id: p.track_id, artist: p.artist ?? "", title: p.title ?? "", duration_s: p.duration_s, gain_db: p.gain_db,
    bpm: p.bpm, bpm_shown: null, stars: null, status: "member", audio: p.audio, art: p.art, rate: p.rate || 1,
    tempo_note: p.tempo_note || "", starred_at: null, added_at: null, position: null, bpm_override: null};
}

export class FindDriver {
  cur: Pick | null = null;
  prev: Pick | null = null;
  viewing: "current" | "previous" = "current";
  upcoming: Pick | null = null;
  private hist: Pick[] | null = null;
  /** The last few rated picks before the one on air, newest first (where back goes), fetched ahead so the full
   *  view can show the previous cover while swiping, and back starts at once (#87). */
  private recent: Pick[] = [];
  private step = 0;
  private shown: Pick | null = null;
  private played = 0;
  private lastPos = 0;
  private waiting = false;
  private loadStart = 0;
  private loadSeq = 0;
  private stopped = false;
  private startInfo: Record<string, unknown> = {};

  constructor(readonly pid: number, readonly name: string, private host: FindHost) {}

  private get base(): string {
    return `/find/${this.pid}`;
  }

  private async call<T>(path: string, body?: unknown): Promise<T> {
    const r = await fetch(this.base + path, body === undefined ? {} : {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (!r.ok) throw new Error(((await r.json().catch(() => ({}))) as {detail?: string}).detail || r.statusText);
    return (await r.json()) as T;
  }

  report(kind: string, extra: Record<string, unknown> = {}): void {
    const t = this.onAir();
    void fetch(this.base + "/api/event", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind, play_id: t ? t.play_id : null, data: extra})}).catch(() => {});
  }

  onAir(): Pick | null {
    return this.viewing === "previous" ? this.prev : this.cur;
  }

  /** The − / + lit for the pick on air (null: neither). */
  vote(): number | null {
    const t = this.onAir();
    return t && t.rating && !t.heard && t.rating !== HEARD ? t.rating : null;
  }

  canBack(): boolean {
    return this.viewing === "current" ? this.recent.length > 0 : !!this.hist && this.step < this.hist.length;
  }

  /** The picks either side of the one on air: older (back) and newer (forward, or the next pick). */
  neighbours(): {older: Pick | null; newer: Pick | null} {
    if (this.viewing === "current") {
      const up = this.upcoming && this.upcoming.title != null ? this.upcoming : null;
      return {older: this.recent[0] ?? null, newer: up};
    }
    const h = this.hist ?? [];
    return {older: h[this.step] ?? null, newer: this.step > 1 ? h[this.step - 2] : this.cur};
  }

  private async loadRecent(): Promise<void> {
    const cur = this.cur;
    if (!cur?.play_id) return;
    try {
      const r = await this.call<Pick[]>(`/api/recent?exclude=${cur.play_id}`);
      if (this.cur === cur && !this.stopped && Array.isArray(r)) { this.recent = r; this.host.publish(); }
    } catch { /* back fetches it then */ }
  }

  /** Start (or come back): the playlist's Set to BPM is Find music's too; then the song that was on air. */
  async start(tempo: {on: boolean; bpm: number | null}, autoplay = true): Promise<void> {
    await this.syncTempo(tempo);
    await this.next("/api/resume", autoplay);
  }

  stop(): void {
    this.stopped = true;
    this.host.tempo.cancel();
  }

  private async syncTempo(t: {on: boolean; bpm: number | null}): Promise<void> {
    try {
      await fetch(this.base + "/api/tempo", {method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({on: t.on, bpm: t.bpm})});
    } catch { /* it keeps its own */ }
  }

  /** The playlist's Set to BPM changed: Find music follows. */
  async followTempo(t: {on: boolean; bpm: number | null}): Promise<void> {
    await this.syncTempo(t);
    const p = this.onAir();
    if (!p || this.stopped) return;
    try { Object.assign(p, await this.call(`/api/tempo/${p.track_id}`)); } catch { return; }
    await this.host.tempo.retune(p.track_id, p.audio, p.rate);
    this.host.publish();
  }

  // The next pick is fetched while this one plays; − or next start it at once.
  private async prefetch(attempt = 0): Promise<void> {
    if (this.stopped) return;
    const cur = this.cur;
    let up: Pick;
    try {
      up = await this.call<Pick>("/api/upcoming");
    } catch (e) {                                       // a check track whose audio the Pi is fetching: ask again
      if (attempt < 12 && /being fetched/.test((e as Error).message)) {
        setTimeout(() => { if (!this.stopped && this.cur === cur) void this.prefetch(attempt + 1); }, 5000);
      }
      return;
    }
    if (!up?.track_id || this.stopped) return;
    if (this.onAir()?.track_id === up.track_id) return;  // never the song playing now
    this.upcoming = up;
    await this.host.preload(up.track_id, up.audio, up.rate || 1, meta(up),
                            () => !this.stopped && this.upcoming === up && this.viewing === "current");
    this.host.publish();
  }

  /** Play the pick most likely next, before the Pi says (it confirms behind). */
  private startUpcoming(): boolean {
    const up = this.upcoming;
    if (!up || up.title == null) return false;
    if (this.cur?.play_id) this.recent = [this.cur, ...this.recent.filter((x) => x.play_id !== this.cur!.play_id)];
    this.shown = {...up, play_id: null};
    this.upcoming = null;
    this.viewing = "current"; this.prev = null; this.step = 0; this.hist = null;
    this.cur = this.shown;
    void this.load(this.shown, 0, 0);
    return true;
  }

  async next(path = "/api/next", autoplay = true): Promise<void> {
    let got: Pick | null = null, err: Error | null = null;
    for (let i = 0; i < 3 && !this.stopped; i++) {
      try { got = await this.call<Pick>(path, {}); err = null; break; }
      catch (e) { err = e as Error; await sleep(1000); }
    }
    if (this.stopped) return;
    if (err || !got) {
      this.host.notice(`Find music: ${err?.message ?? "nothing to play"}`);
      this.host.ended();
      return;
    }
    const early = this.shown && got.track_id === this.shown.track_id && this.onAir() === this.shown;
    if (early) { Object.assign(this.shown!, got); this.cur = this.shown; }
    else this.cur = got;
    this.shown = null;
    this.viewing = "current"; this.prev = null; this.step = 0; this.hist = null;
    if (!early) await this.load(this.cur!, 0, 0, autoplay);
    else this.host.publish();
    void this.prefetch();
    void this.loadRecent();
  }

  private async load(t: Pick, startAt: number, played: number, autoplay = true): Promise<void> {
    const {engine, tempo} = this.host;
    this.played = played; this.lastPos = startAt;
    engine.pause();                                     // the song being left stops now (#53)
    this.host.hold({t: startAt, d: t.duration_s || 0});
    this.host.publish({loading: true});
    engine.setGain(t.gain_db || 0, this.host.headroom());
    this.host.media(asTrack(t));
    this.host.played(this.pid);
    const src = await tempo.start(t.track_id, t.audio, t.rate || 1);
    if (this.stopped || this.onAir() !== t) return;
    engine.load(src, startAt / (tempo.loaded || 1), meta(t));
    this.host.hold(null);
    this.loadStart = performance.now();
    this.waiting = true;
    this.startInfo = {rate: tempo.loaded, want: t.rate || 1};
    this.watch(t);
    this.host.publish();
    if (!autoplay) { this.waiting = false; this.host.publish({loading: false}); return; }
    try { await engine.play(); } catch { this.host.publish({loading: false}); }
  }

  // A pick that doesn't start: after 6 s without sound it's logged and loaded again from the Pi's original file;
  // still nothing, and it's skipped. An error from the player does the same at once.
  private watch(t: Pick): void {
    const my = ++this.loadSeq;
    setTimeout(() => {
      if (this.stopped || my !== this.loadSeq || !this.waiting || this.host.engine.paused || this.onAir() !== t) return;
      this.recover(t, "autoplay_stuck", {});
    }, 6000);
  }

  recover(t: Pick | null, kind: string, extra: Record<string, unknown>): void {
    if (!t || this.stopped) return;
    this.report(kind, {track_id: t.track_id, retried: !!t.retried, ...extra});
    if (t.retried) { t.retried = false; void (t === this.cur ? this.skip() : this.forward()); return; }
    t.retried = true;
    this.host.engine.load(t.audio, 0, meta(t));         // the plain file, not a copy or what was fetched ahead
    this.loadStart = performance.now();
    this.waiting = true;
    void this.host.engine.play().catch(() => {});
    this.watch(t);
  }

  playing(): void {
    if (!this.waiting) return;
    this.waiting = false;
    this.report("playing", {latency_ms: Math.round(performance.now() - this.loadStart), ...this.startInfo});
  }

  time(): void {
    const e = this.host.engine, d = e.time - this.lastPos;
    if (!e.paused && d > 0 && d < 1.5) this.played += d;
    this.lastPos = e.time;
  }

  private tempoReport() {
    const t = this.onAir(), r = this.host.tempo.loaded;
    return {played_bpm: t && t.bpm ? (r !== 1 ? this.host.targetBpm() : t.bpm) : null, playback_rate: r};
  }

  /** − (DOWN) or + (UP): shown at once, saved behind; − moves on at once. The lit one again takes it back to heard. */
  async rate(r: number): Promise<void> {
    const t = this.onAir();
    if (!t || this.stopped) return;
    for (let i = 0; i < 50 && !t.play_id; i++) await sleep(100);      // still being confirmed
    if (!t.play_id || this.onAir() !== t) return;
    const undo = this.vote() === r, before = {rating: t.rating, heard: t.heard};
    const prev = this.viewing === "previous";
    t.rating = undo ? HEARD : r;
    t.heard = undo;
    this.host.publish();
    if (r === UP && !undo) this.host.notice(`Added to ${this.name}`);
    const moveOn = r === DOWN && !undo;
    const saving = before.rating == null && !prev
      ? this.call<Pick>("/api/rate", {play_id: t.play_id, rating: r, played_seconds: Math.round(this.played), ...this.tempoReport()})
      : this.call<Pick>("/api/change", {...(undo ? {rating: HEARD, heard: true} : {rating: r}), play_id: t.play_id});
    if (moveOn && !prev) this.startUpcoming();          // the next song starts now; the Pi catches up
    let res: Pick;
    try { res = await saving; }
    catch (e) { Object.assign(t, before); this.host.publish(); this.host.notice(`Couldn't save: ${(e as Error).message}`); return; }
    Object.assign(t, res, {heard: undo});
    if (undo) t.rating = HEARD;
    if (prev && this.hist && this.step) this.hist[this.step - 1] = t;
    this.host.publish();
    this.host.added(this.pid);
    if (!prev) void this.loadRecent();
    if (moveOn) return prev ? this.forward() : this.next();
  }

  /** Next: heard (Close, marked automatic), and on to the next pick. */
  async skip(): Promise<void> {
    if (this.viewing === "previous") return this.forward();
    const t = this.cur;
    if (!t || !t.play_id) return;
    const skipping = t.rating == null ? this.call("/api/skip", {play_id: t.play_id, played_seconds: Math.round(this.played)}) : null;
    this.report("skipped", {});
    this.startUpcoming();
    if (skipping) { try { await skipping; } catch (e) { this.host.notice(`Couldn't skip: ${(e as Error).message}`); } }
    await this.next();
  }

  ended(): void {
    this.report("ended", {rated: this.onAir()?.rating ?? null, played_s: Math.round(this.played)});
    void (this.viewing === "previous" ? this.forward() : this.skip());
  }

  /** Back: a few seconds in, the song restarts; else back through the last few picks. `force`: never a restart. */
  async back(force = false): Promise<void> {
    const e = this.host.engine;
    if (!force && e.time >= this.host.backRestart()) { e.seek(0); this.lastPos = 0; return; }
    if (!this.hist || this.viewing === "current") {
      if (this.recent.length) this.hist = this.recent.slice();          // fetched ahead: back starts at once
      else {
        try { this.hist = await this.call<Pick[]>(`/api/recent${this.cur?.play_id ? `?exclude=${this.cur.play_id}` : ""}`); }
        catch { this.hist = []; }
      }
    }
    if (this.step >= this.hist.length) { e.seek(0); return; }                 // as far back as it goes

    this.step += 1;
    const t = this.hist[this.step - 1];
    this.viewing = "previous";
    this.prev = {...t, heard: t.rating === HEARD};
    this.report("back", {to: t.play_id, steps: this.step});
    await this.load(this.prev, 0, 0);
  }

  async forward(): Promise<void> {
    this.step = Math.max(0, this.step - 1);
    if (this.step > 0 && this.hist) {
      const t = this.hist[this.step - 1];
      this.prev = {...t, heard: t.rating === HEARD};
      return this.load(this.prev, 0, 0);
    }
    this.viewing = "current";
    this.prev = null;
    const playOn = this.cur && this.cur.rating !== DOWN;   // the pick on air, unless it got a − (then the next one)
    // Songs always start at the start (#93: coming forward again picked the song up where it was left, sometimes
    // near its end, so it ended moments later and the next one started).
    if (playOn && this.cur!.rating == null) await this.load(this.cur!, 0, 0);
    else await this.next();
  }
}
