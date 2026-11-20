// What actually makes the sound. The player talks to one of these; in a browser it's the BrowserEngine below, in the
// phone app (milestone 4) the native player behind a bridge.

export type EngineEvent =
  | {type: "playing"}
  | {type: "pause"}
  | {type: "waiting"}
  | {type: "time"}
  | {type: "loaded"}
  | {type: "ended"}
  | {type: "error"; message: string}
  | {type: "action"; name: "nexttrack" | "previoustrack" | "vote_up" | "vote_down"};   // the phone's notification

/** What the phone's notification and lock screen show for a song. */
export interface Meta {
  title: string;
  artist: string;
  art: string | null;
}

export interface Engine {
  /** Start a new song (or a copy of the same one) from `at` seconds; a song fetched ahead with preload starts at once. */
  load(url: string, at?: number, meta?: Meta): void;
  play(): Promise<void>;
  pause(): void;
  seek(seconds: number): void;
  readonly time: number;
  readonly duration: number;
  readonly paused: boolean;
  /** Loudness levelling: the song's gain in dB (computers: Web Audio; phones: the element's volume, `headroom` dB
   *  quieter so quiet songs can still be raised). */
  setGain(db: number, headroomDb: number): void;
  /** The volume slider (computers), applied after the levelling. */
  setVolume(v: number): void;
  /** What's most likely next: fetched ahead so it starts at once (the app's player also goes on to it by itself when
   *  a song ends, with the screen off). */
  preload(url: string | null, meta?: Meta): void;
  /** Find music's − / + in the phone's notification: null = not Find music, 0 = neither chosen, 1 = −, 3 = +. */
  setVotes?(v: number | null): void;
  on(listener: (e: EngineEvent) => void): () => void;
}

const absolute = (u: string) => new URL(u, location.href).href;

export class BrowserEngine implements Engine {
  private els: HTMLAudioElement[];
  private active = 0;
  private listeners = new Set<(e: EngineEvent) => void>();
  private ctx: AudioContext | null = null;
  private gains: GainNode[] = [];
  private volume: GainNode | null = null;
  private gainDb = 0;
  private headroom = 6;
  private vol = 1;
  readonly desktop = typeof matchMedia !== "undefined" && matchMedia("(hover: hover) and (pointer: fine)").matches;

  constructor() {
    this.els = [new Audio(), new Audio()];
    this.els.forEach((el, i) => {
      el.preload = "auto";
      const fwd = (e: EngineEvent) => { if (i === this.active) this.emit(e); };
      el.addEventListener("playing", () => fwd({type: "playing"}));
      el.addEventListener("pause", () => fwd({type: "pause"}));
      el.addEventListener("waiting", () => fwd({type: "waiting"}));
      el.addEventListener("timeupdate", () => fwd({type: "time"}));
      el.addEventListener("loadedmetadata", () => fwd({type: "loaded"}));
      el.addEventListener("ended", () => fwd({type: "ended"}));
      el.addEventListener("error", () => fwd({type: "error", message: el.error?.message || `code ${el.error?.code}`}));
    });
  }

  private get el(): HTMLAudioElement {
    return this.els[this.active];
  }

  private get spare(): HTMLAudioElement {
    return this.els[1 - this.active];
  }

  private emit(e: EngineEvent): void {
    for (const l of [...this.listeners]) l(e);
  }

  on(listener: (e: EngineEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  load(url: string, at = 0, _meta?: Meta): void {
    const want = absolute(url);
    const old = this.el;
    if (this.spare.src === want) {                     // fetched ahead: swap to it
      this.active = 1 - this.active;
      old.pause();
    } else {
      old.pause();
      this.el.src = want;
    }
    this.applyLevel();
    if (at) {
      const el = this.el;
      const set = () => { el.currentTime = at; };
      if (el.readyState >= 1) set(); else el.addEventListener("loadedmetadata", set, {once: true});
    } else if (this.el.currentTime) {
      this.el.currentTime = 0;
    }
  }

  async play(): Promise<void> {
    if (this.desktop) this.wire();
    if (this.ctx && this.ctx.state === "suspended") await this.ctx.resume();
    await this.el.play();
  }

  pause(): void {
    this.el.pause();
  }

  seek(seconds: number): void {
    this.el.currentTime = seconds;
  }

  get time(): number {
    return this.el.currentTime || 0;
  }

  get duration(): number {
    return this.el.duration || 0;
  }

  get paused(): boolean {
    return this.el.paused;
  }

  setGain(db: number, headroomDb: number): void {
    this.gainDb = db || 0;
    this.headroom = headroomDb;
    this.applyLevel();
  }

  setVolume(v: number): void {
    this.vol = Math.max(0, Math.min(1, v));
    if (this.volume) this.volume.gain.value = this.vol;
  }

  preload(url: string | null, _meta?: Meta): void {
    if (!url) return;
    const want = absolute(url);
    if (this.spare.src !== want && this.el.src !== want) this.spare.src = want;
  }

  // Computers level through Web Audio (one gain per element, then the volume); phones use the element's own volume,
  // so the browser treats it as ordinary playback and shows its lock-screen controls.
  private wire(): void {
    if (this.ctx) return;
    this.ctx = new AudioContext();
    this.volume = this.ctx.createGain();
    this.volume.gain.value = this.vol;
    this.volume.connect(this.ctx.destination);
    this.gains = this.els.map((el) => {
      const g = this.ctx!.createGain();
      this.ctx!.createMediaElementSource(el).connect(g).connect(this.volume!);
      return g;
    });
    this.applyLevel();
  }

  private applyLevel(): void {
    if (this.desktop) {
      this.gains.forEach((g, i) => { if (i === this.active) g.gain.value = Math.pow(10, this.gainDb / 20); });
    } else {
      this.el.volume = Math.min(1, Math.pow(10, (this.gainDb - this.headroom) / 20));
    }
  }
}
