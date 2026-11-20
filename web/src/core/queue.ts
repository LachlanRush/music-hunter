// What a playlist plays, in what order: its playable songs (star filter applied, To rate first), shuffled by a round
// that remembers what's been heard (feedback #33), with Up next (Add to queue) ahead of the rest.
import type { Track } from "./api";

export interface KeyValue {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const memory = (): KeyValue => {
  const m = new Map<string, string>();
  return {getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v)};
};

function defaultStore(): KeyValue {
  try {
    return localStorage;
  } catch {
    return memory();
  }
}

export function shuffled<T>(list: T[], rand = Math.random): T[] {
  const a = list.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// A shuffle round per playlist, kept on the device: songs not heard yet come first; once everything has been heard
// a new round starts, with the last song played last. (Same keys as the old pages, so a round carries over.)
export class ShuffleRound {
  private heard: number[];

  constructor(private key: string, private store: KeyValue = defaultStore(), private rand = Math.random) {
    this.heard = this.read("shuffle-heard", [] as number[]);
  }

  private read<T>(name: string, fallback: T): T {
    try {
      const v = this.store.getItem(`${name}:${this.key}`);
      return v === null ? fallback : (JSON.parse(v) as T);
    } catch {
      return fallback;
    }
  }

  private write(name: string, value: unknown): void {
    try {
      this.store.setItem(`${name}:${this.key}`, JSON.stringify(value));
    } catch {
      /* blocked storage: the round lasts as long as the page */
    }
  }

  get on(): boolean {
    return this.read<boolean>("shuffle-on", false) === true;
  }

  set on(v: boolean) {
    this.write("shuffle-on", !!v);
  }

  mark(id: number): void {
    this.heard = this.heard.filter((h) => h !== id).concat(id);
    this.write("shuffle-heard", this.heard);
  }

  order<T extends {id: number}>(tracks: T[]): T[] {
    const fresh = tracks.filter((t) => !this.heard.includes(t.id));
    if (fresh.length) {
      return [...shuffled(fresh, this.rand), ...shuffled(tracks.filter((t) => this.heard.includes(t.id)), this.rand)];
    }
    const last = this.heard[this.heard.length - 1];
    this.heard = [];
    this.write("shuffle-heard", this.heard);
    return [...shuffled(tracks.filter((t) => t.id !== last), this.rand), ...tracks.filter((t) => t.id === last)];
  }
}

// The songs Play goes through: with audio, at or above the star filter, To rate first, then the list's own order.
export function playable(tracks: Track[], minStars = 0, sortIds?: number[]): Track[] {
  const list = tracks.filter((t) => t.audio && (!minStars || (t.stars || 0) >= minStars));
  if (sortIds) {
    const at = new Map(sortIds.map((id, i) => [id, i]));
    return list.sort((a, b) => (at.get(a.id) ?? 1e9) - (at.get(b.id) ?? 1e9));
  }
  return [...list.filter((t) => t.status === "added"), ...list.filter((t) => t.status !== "added")];
}

// The queue when Play, Shuffle or a song is pressed. `first`: the song tapped (it plays first even when below the
// star filter); `upNext`: songs added with Add to queue while nothing played, which come straight after it.
export function buildQueue(
  all: Track[], list: Track[], opts: {first?: number | null; shuffle?: ShuffleRound | null; upNext?: Track[]},
): Track[] {
  let q = opts.shuffle ? opts.shuffle.order(list) : list.slice();
  const first = opts.first ?? null;
  if (first !== null && !q.some((t) => t.id === first)) {
    const t = all.find((x) => x.id === first);
    if (t) q = [t, ...q];
  }
  if (first !== null) {
    const i = q.findIndex((t) => t.id === first);
    if (i > 0) q = opts.shuffle ? [q[i], ...q.filter((_, k) => k !== i)] : q.slice(i).concat(q.slice(0, i));
  }
  const up = opts.upNext ?? [];
  if (up.length) q = first !== null ? [q[0], ...up, ...q.slice(1)] : [...up, ...q];
  return q;
}
