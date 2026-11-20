import { afterEach, describe, expect, it, vi } from "vitest";
import type { Playlist, Track } from "../api";
import { PlaylistModel, sortTracks } from "../library";

const track = (id: number, extra: Partial<Track> = {}): Track => ({
  id, artist: `A${id}`, title: `T${id}`, duration_s: 200, gain_db: 0, bpm: 140, bpm_shown: 140, stars: null,
  status: "member", audio: `/audio/${id}`, art: null, rate: 1, tempo_note: "", starred_at: null, added_at: "2026-11-01 10:00:00",
  position: null, bpm_override: null, ...extra,
});
const flush = () => new Promise((r) => setTimeout(r, 0));

afterEach(() => vi.unstubAllGlobals());

describe("playlist order", () => {
  it("To rate first (newest first), then by stars, my dragged order, then the most recently starred", () => {
    const ts = [
      track(1, {stars: 3, starred_at: "2026-11-02"}),
      track(2, {status: "added", added_at: "2026-11-03"}),
      track(3, {stars: 5}),
      track(4, {stars: 3, starred_at: "2026-11-05"}),
      track(5, {status: "added", added_at: "2026-11-04"}),
      track(6),
      track(7, {stars: 3, position: 0}),
    ];
    expect(sortTracks(ts).map((t) => t.id)).toEqual([5, 2, 3, 4, 1, 7, 6]);
  });
});

describe("playlist model", () => {
  const data: Playlist = {id: 4, name: "pump", description: null, image: null, min_stars: 0, back_restart_s: 5, phone_headroom_db: 6,
    folder_id: null, tempo: {on: false, bpm: null, target: 140, auto: 140, max_stretch: 1},
    tracks: [track(1, {stars: 4}), track(2, {status: "added"}), track(3, {stars: 2})], deleted: []};

  it("a star shows and moves the song at once; the Pi is told", async () => {
    const calls: [string, RequestInit | undefined][] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return {ok: true, status: 200, json: async () => data};
    }));
    const m = new PlaylistModel(4);
    m.data.set(structuredClone(data));
    m.setStars(2, 5);
    expect(m.data.get()!.tracks.map((t) => t.id)).toEqual([2, 1, 3]);
    expect(m.data.get()!.tracks[0].status).toBe("member");
    await flush();
    expect(calls[0][0]).toBe("/api/playlists/4/tracks/2");
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({stars: 5});
    m.setStars(2, 5);                                        // the same star again clears it
    expect(m.data.get()!.tracks.find((t) => t.id === 2)!.stars).toBeNull();
  });

  it("removing moves the song into Recently deleted at once, and restoring brings it back", () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ok: true, status: 200, json: async () => ({})})));
    const m = new PlaylistModel(4);
    m.data.set(structuredClone(data));
    m.remove(3);
    expect(m.data.get()!.tracks.map((t) => t.id)).toEqual([1, 2]);
    expect(m.data.get()!.deleted!.map((t) => t.id)).toEqual([3]);
    m.restore(3);
    expect(m.data.get()!.tracks.map((t) => t.id)).toEqual([2, 1, 3]);
    expect(m.data.get()!.deleted).toEqual([]);
  });

  it("a failed change shows a message and brings back the Pi's view", async () => {
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => (n++ === 0
      ? {ok: false, status: 500, statusText: "boom", json: async () => ({detail: "boom"})}
      : {ok: true, status: 200, json: async () => data})));
    const m = new PlaylistModel(4);
    m.data.set(structuredClone(data));
    m.remove(1);
    await flush(); await flush();
    expect(m.error.get()).toBe("Couldn't remove it: boom");
    await flush();
    expect(m.data.get()!.tracks.map((t) => t.id)).toEqual([1, 2, 3]);
  });
});

describe("addresses", () => {
  it("home is / at the top, so going home always leaves the page", async () => {
    const { href } = await import("../router");
    expect(href("/")).toBe("/");
    expect(href("/playlist/3")).toBe("/playlist/3");
  });
});
