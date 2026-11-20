import { describe, expect, it, vi } from "vitest";
import type { Playlist, Track } from "../api";
import type { Engine, EngineEvent } from "../engine";
import { Player } from "../player";
import { buildQueue, playable, ShuffleRound, type KeyValue } from "../queue";

// A stand-in for the sound: records what it was asked to do, and lets a test end a song.
class FakeEngine implements Engine {
  loaded: string[] = [];
  preloaded: (string | null)[] = [];
  paused = true;
  time = 0;
  duration = 200;
  private ls = new Set<(e: EngineEvent) => void>();
  load(url: string) { this.loaded.push(url); this.time = 0; }
  async play() { this.paused = false; this.emit({type: "playing"}); }
  pause() { this.paused = true; }
  seek(s: number) { this.time = s; }
  setGain() {}
  setVolume() {}
  preload(url: string | null) { this.preloaded.push(url); }
  on(l: (e: EngineEvent) => void) { this.ls.add(l); return () => this.ls.delete(l); }
  emit(e: EngineEvent) { for (const l of this.ls) l(e); }
}

const track = (id: number, extra: Partial<Track> = {}): Track => ({
  id, artist: `A${id}`, title: `T${id}`, duration_s: 200, gain_db: 0, bpm: 140, bpm_shown: 140, stars: 3,
  status: "member", audio: `/audio/${id}`, art: null, rate: 1, tempo_note: "", starred_at: null, added_at: null,
  position: null, bpm_override: null, ...extra,
});
const playlist = (tracks: Track[], extra: Partial<Playlist> = {}): Playlist => ({
  id: 7, name: "No More", description: null, image: null, tracks, min_stars: 0, back_restart_s: 5,
  phone_headroom_db: 6, folder_id: null, tempo: {on: false, bpm: null, target: 140, auto: 140, max_stretch: 1}, ...extra,
});
const memory = (): KeyValue => { const m = new Map<string, string>(); return {getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v)}; };
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("queue", () => {
  it("plays To rate first, then the list, and leaves out songs without audio or below the star filter", () => {
    const ts = [track(1), track(2, {status: "added"}), track(3, {audio: null}), track(4, {stars: 1})];
    expect(playable(ts).map((t) => t.id)).toEqual([2, 1, 4]);
    expect(playable(ts, 3).map((t) => t.id)).toEqual([2, 1]);
  });

  it("starts at a tapped song and carries on round the list; Up next comes straight after it", () => {
    const ts = [1, 2, 3, 4].map((i) => track(i));
    expect(buildQueue(ts, ts, {first: 3}).map((t) => t.id)).toEqual([3, 4, 1, 2]);
    expect(buildQueue(ts, ts, {first: 3, upNext: [track(9)]}).map((t) => t.id)).toEqual([3, 9, 4, 1, 2]);
  });

  it("shuffle doesn't repeat a song until the round is over, and the last one heard goes last", () => {
    const ts = [1, 2, 3, 4].map((i) => track(i));
    const r = new ShuffleRound("7", memory());
    const first = r.order(ts);
    first.forEach((t) => r.mark(t.id));
    const again = r.order(ts);
    expect(new Set(again.map((t) => t.id))).toEqual(new Set([1, 2, 3, 4]));
    expect(again[again.length - 1].id).toBe(first[first.length - 1].id);
  });
});

describe("player", () => {
  it("plays a playlist, goes on to the next song when one ends, and fetches the one after ahead", async () => {
    const e = new FakeEngine();
    const p = new Player(e, () => {});
    p.play(playlist([1, 2, 3].map((i) => track(i))), {shuffle: false});
    await flush();
    expect(e.loaded).toEqual(["/audio/1"]);
    expect(p.state.get().track?.id).toBe(1);
    expect(e.preloaded).toContain("/audio/2");
    e.emit({type: "ended"});
    await flush();
    expect(e.loaded).toEqual(["/audio/1", "/audio/2"]);
    expect(p.state.get().prevTrack?.id).toBe(1);
    expect(p.state.get().nextTrack?.id).toBe(3);
  });

  it("back restarts a song a few seconds in, else goes to the one before", async () => {
    const e = new FakeEngine();
    const p = new Player(e, () => {});
    p.play(playlist([1, 2].map((i) => track(i))), {shuffle: false});
    await flush();
    p.next();
    await flush();
    e.time = 30;
    p.prev();
    expect(e.time).toBe(0);
    expect(p.state.get().track?.id).toBe(2);
    e.time = 2;
    p.prev();
    await flush();
    expect(p.state.get().track?.id).toBe(1);
  });

  it("asks for the stretched copy under Set to BPM, and plays the original until it's made", async () => {
    const e = new FakeEngine();
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      return {ok: true, json: async () => ({state: url.includes("/tempo/1?") ? "ready" : "rendering",
                                             url: url.includes("/tempo/1?") ? "/tempo/1/audio?rate=1.05&pl=7" : null})};
    }));
    const p = new Player(e, () => {});
    p.play(playlist([track(1, {rate: 1.05}), track(2, {rate: 0.97})]), {shuffle: false});
    await flush(); await flush();
    expect(e.loaded[0]).toBe("/tempo/1/audio?rate=1.05&pl=7");
    expect(p.state.get().rate).toBe(1.05);
    p.next();
    await flush(); await flush();
    expect(e.loaded[1]).toBe("/audio/2");                           // its copy isn't made yet
    expect(calls.some((c) => c.startsWith("/tempo/2?rate=0.97&pl=7"))).toBe(true);
    vi.unstubAllGlobals();
  });
});

describe("player, milestone 2", () => {
  it("follows Sort by sound and the star filter for the rest of the queue, keeping the song playing", async () => {
    const e = new FakeEngine();
    const p = new Player(e, () => {});
    const pl = playlist([track(1, {stars: 5}), track(2, {stars: 1}), track(3, {stars: 4}), track(4, {stars: 2})]);
    p.play(pl, {shuffle: false});
    await flush();
    p.setOrder(7, [1, 4, 3, 2]);
    expect(p.state.get().nextTrack?.id).toBe(4);
    p.update({...pl, min_stars: 3});
    expect(p.state.get().track?.id).toBe(1);
    expect(p.state.get().nextTrack?.id).toBe(3);
    expect(p.state.get().length).toBe(2);
  });

  it("plays through Recently deleted at the songs' own tempo", async () => {
    const e = new FakeEngine();
    const p = new Player(e, () => {});
    const del = [track(8, {rate: 1.1}), track(9, {audio: null}), track(10)].map((t) => ({...t, deleted_at: "2026-11-20 10:00:00"}));
    p.playDeleted(playlist([track(1)], {deleted: del}), 8);
    await flush();
    expect(e.loaded).toEqual(["/audio/8"]);
    expect(p.isCurrentDeleted(7, 8)).toBe(true);
    expect(p.isCurrent(7, 8)).toBe(false);
    expect(p.state.get().nextTrack?.id).toBe(10);
  });

  it("a song queued from another playlist plays next, at the playing playlist's Set to BPM", async () => {
    const e = new FakeEngine();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ok: true, json: async () =>
      (url.startsWith("/api/playlists/7/rate/50") ? {rate: 1, tempo_note: "Own tempo"} : {state: "failed"})})));
    const p = new Player(e, () => {});
    p.play(playlist([1, 2].map((i) => track(i))), {shuffle: false});
    await flush();
    await p.enqueue(track(50, {rate: 0.9}), 3);
    expect(p.state.get().nextTrack?.id).toBe(50);
    expect(p.state.get().nextTrack?.rate).toBe(1);
    vi.unstubAllGlobals();
  });
});

describe("Find music", () => {
  const pick = (id: number, play: number | null, extra: Record<string, unknown> = {}) => ({
    play_id: play, track_id: id, artist: `A${id}`, title: `T${id}`, art: null, rating: null, duration_s: 200, gain_db: 0,
    bpm: 140, rate: 1, tempo_note: "", audio: `/find/7/audio/${id}`, ...extra});

  it("resumes, fetches the next pick ahead, − moves on to it at once, and + adds", async () => {
    const e = new FakeEngine();
    const calls: string[] = [];
    let next = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const body = (v: unknown) => ({ok: true, status: 200, json: async () => v});
      if (url.endsWith("/api/resume")) return body(pick(1, 10));
      if (url.endsWith("/api/upcoming")) return body(pick(2 + next, null));
      if (url.endsWith("/api/next")) { next++; return body(pick(1 + next, 10 + next)); }
      if (url.endsWith("/api/rate")) { const b = JSON.parse(String(init!.body)); return body(pick(b.play_id === 10 ? 1 : 2, b.play_id, {rating: b.rating})); }
      return body({ok: true});
    }));
    const added: number[] = [], notes: string[] = [];
    const p = new Player(e, {played: () => {}, added: (pid) => added.push(pid), notice: (t) => notes.push(t)});
    p.startFind(playlist([track(1)]));
    for (let i = 0; i < 6; i++) await flush();
    expect(p.state.get().source?.kind).toBe("find");
    expect(p.state.get().track?.id).toBe(1);
    expect(e.preloaded).toContain("/find/7/audio/2");
    expect(p.state.get().nextTrack?.id).toBe(2);
    p.vote("down");
    for (let i = 0; i < 3; i++) await flush();
    expect(p.state.get().track?.id).toBe(2);                   // started before the Pi confirmed
    expect(calls).toContain("POST /find/7/api/rate");
    for (let i = 0; i < 6; i++) await flush();
    p.vote("up");
    for (let i = 0; i < 6; i++) await flush();
    expect(p.state.get().vote).toBe(3);
    expect(notes).toContain("Added to No More");
    expect(added).toContain(7);
    expect(await p.enqueue(track(5), 7)).toBe("find");
    p.play(playlist([track(1)]));                              // Play leaves Find music
    expect(p.state.get().source?.kind).toBe("playlist");
    vi.unstubAllGlobals();
  });
});
