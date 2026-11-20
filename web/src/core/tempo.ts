// Set to BPM: songs play at the playlist's tempo, pitch kept, from stretched copies the Pi renders (Rubber Band).
// Until a song's copy is ready the original plays, and the copy swaps in at the same point in the music.

export type CopyState = {state: "ready" | "rendering" | "failed"; url?: string | null};

export interface TempoSource {
  /** Is the copy of `id` at `rate` ready? Asks the Pi to make it if not (`ahead`: after the playing song's). */
  status(id: number, rate: number, ahead?: boolean): Promise<CopyState>;
}

// The playlist's copies (`/tempo/<id>?rate=…&pl=<playlist>`); Find music's live under /find/<playlist>/ (milestone 3).
export function playlistCopies(pid: number, base = ""): TempoSource {
  return {
    async status(id, rate, ahead = false) {
      try {
        const r = await fetch(`${base}/tempo/${id}?rate=${rate}&pl=${pid}${ahead ? "&ahead=1" : ""}`);
        return r.ok ? ((await r.json()) as CopyState) : {state: "failed"};
      } catch {
        return {state: "failed"};
      }
    },
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Follows one song's copy: where to start, and when to swap to the copy once it's made. */
export class TempoFollower {
  /** The rate of what's loaded now (1 = the original). */
  loaded = 1;
  private token = 0;

  constructor(private source: TempoSource, private swap: (url: string, rate: number, loadedRate: number) => void,
              private changed: (rate: number) => void = () => {}) {}

  setSource(source: TempoSource): void {
    this.source = source;
  }

  /** The address to start a song with: its copy if ready, else `original` (the copy follows when it's ready). */
  async start(id: number, original: string, rate: number): Promise<string> {
    const my = ++this.token;
    this.loaded = 1;
    if (rate && rate !== 1) {
      const s = await this.source.status(id, rate);
      if (s.state === "ready" && s.url && my === this.token) {
        this.loaded = rate;
        this.changed(rate);
        return s.url;
      }
      if (s.state === "rendering") void this.follow(my, id, rate);
    }
    this.changed(1);
    return original;
  }

  /** What the next song would start with now (asks for its copy ahead if it isn't made yet). */
  async peek(id: number, original: string, rate: number): Promise<{url: string; rate: number}> {
    if (rate && rate !== 1) {
      const s = await this.source.status(id, rate, true);
      if (s.state === "ready" && s.url) return {url: s.url, rate};
    }
    return {url: original, rate: 1};
  }

  /** The setting changed while a song plays: change copies, keeping the place in the music. */
  async retune(id: number, original: string, rate: number): Promise<void> {
    const my = ++this.token;
    rate = rate || 1;
    if (rate === this.loaded) return;
    if (rate === 1) return this.doSwap(original, 1);
    const s = await this.source.status(id, rate);
    if (my !== this.token) return;
    if (s.state === "ready" && s.url) this.doSwap(s.url, rate);
    else if (s.state === "rendering") void this.follow(my, id, rate);
  }

  /** Nothing to follow any more (another song, or stopped). */
  cancel(): void {
    this.token++;
  }

  private doSwap(url: string, rate: number): void {
    const was = this.loaded;
    this.loaded = rate;
    this.swap(url, rate, was);
    this.changed(rate);
  }

  private async follow(my: number, id: number, rate: number): Promise<void> {
    for (let i = 0; i < 300; i++) {                 // up to ~10 minutes (a queue of renders on the Pi)
      await sleep(2000);
      if (my !== this.token) return;
      const s = await this.source.status(id, rate);
      if (my !== this.token) return;
      if (s.state === "ready" && s.url) return this.doSwap(s.url, rate);
      if (s.state !== "rendering") return;
    }
  }
}
