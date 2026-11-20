// The phone app's player (SPEC.md §20.3): Media3 in a foreground service, behind the app's `MHApp` bridge. The page
// says what plays now and what's next; the player goes on to the next song by itself when one ends (with the screen
// off the page can be slow to hear about it, #72), and the page then confirms it. Events come back through
// window.__mhNative(type, data).
import type { Engine, EngineEvent, Meta } from "./engine";

export interface AppBridge {
  load(url: string, atMs: number, title: string, artist: string, art: string): void;
  setNext(url: string, title: string, artist: string, art: string): void;
  play(): void;
  pause(): void;
  seek(ms: number): void;
  setVolume(v: number): void;
  setVotes(v: number): void;
  offlineState(pid: number): string;
  offlineSet(pid: number, on: boolean): void;
}

declare global {
  interface Window {
    MHApp?: AppBridge;
    __mhNative?: (type: string, data: Record<string, unknown>) => void;
  }
}

const absolute = (u: string) => new URL(u, location.href).href;
const absArt = (m?: Meta) => (m?.art ? absolute(m.art) : "");

export class NativeEngine implements Engine {
  private listeners = new Set<(e: EngineEvent) => void>();
  private t = 0;
  private at = 0;                 // when `t` was last heard (performance.now)
  private d = 0;
  private isPaused = true;
  private loaded = "";
  private advanced: string | null = null;   // went on to it by itself; not confirmed by the page yet
  private gainDb = 0;
  private headroom = 6;
  private vol = 1;

  constructor(private bridge: AppBridge) {
    window.__mhNative = (type, data) => this.event(type, data ?? {});
  }

  /** The last few calls and events, for looking into a problem (window.__mhTrace, read over the debug port). */
  private trace(...a: unknown[]): void {
    const t = ((window as unknown as {__mhTrace?: unknown[]}).__mhTrace ??= []);
    t.push([Math.round(performance.now()), ...a]);
    if (t.length > 60) t.shift();
  }

  private event(type: string, data: Record<string, unknown>): void {
    if (type !== "time") this.trace("ev", type, data.url ?? "");
    if (typeof data.t === "number") { this.t = data.t; this.at = performance.now(); }
    if (typeof data.d === "number" && data.d > 0) this.d = data.d;
    switch (type) {
      case "playing": this.isPaused = false; return this.emit({type: "playing"});
      case "pause": this.isPaused = true; return this.emit({type: "pause"});
      case "waiting": return this.emit({type: "waiting"});
      case "time": return this.emit({type: "time"});
      case "loaded": return this.emit({type: "loaded"});
      case "ended": this.isPaused = true; return this.emit({type: "ended"});
      case "error": return this.emit({type: "error", message: String(data.message ?? "")});
      case "advanced":                              // the last song ended and the next one is already playing
        this.advanced = String(data.url ?? "");
        this.loaded = this.advanced;
        this.t = 0; this.at = performance.now(); this.d = 0; this.isPaused = false;
        return this.emit({type: "ended"});
      case "action": {
        const name = String(data.name);
        if (name === "nexttrack" || name === "previoustrack" || name === "vote_up" || name === "vote_down") {
          this.emit({type: "action", name});
        }
      }
    }
  }

  private emit(e: EngineEvent): void {
    for (const l of [...this.listeners]) l(e);
  }

  on(listener: (e: EngineEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  load(url: string, at = 0, meta?: Meta): void {
    const want = absolute(url);
    this.trace("load", want.slice(-50), at, this.advanced?.slice(-50) ?? "");
    if (this.advanced && want === this.advanced && !at) {   // the page confirms what's already playing
      this.advanced = null;
      queueMicrotask(() => { if (!this.isPaused) this.emit({type: "playing"}); });   // (it started without the page)
      return;
    }
    this.advanced = null;
    this.loaded = want;
    this.t = at; this.at = performance.now(); this.d = 0;
    this.bridge.load(want, Math.round(at * 1000), meta?.title ?? "", meta?.artist ?? "", absArt(meta));
  }

  async play(): Promise<void> {
    this.isPaused = false;
    this.bridge.play();
  }

  pause(): void {
    if (this.advanced) return;     // (the page stopping the song it's leaving, which has already ended)
    this.isPaused = true;
    this.bridge.pause();
  }

  seek(seconds: number): void {
    this.t = seconds; this.at = performance.now();
    this.bridge.seek(Math.round(seconds * 1000));
  }

  get time(): number {
    return this.isPaused ? this.t : this.t + Math.min(1, (performance.now() - this.at) / 1000);
  }

  get duration(): number {
    return this.d;
  }

  get paused(): boolean {
    return this.isPaused;
  }

  // Levelling by the player's volume, a few dB down so quiet songs can still be raised.
  setGain(db: number, headroomDb: number): void {
    this.gainDb = db || 0;
    this.headroom = headroomDb;
    this.applyVolume();
  }

  setVolume(v: number): void {
    this.vol = Math.max(0, Math.min(1, v));
    this.applyVolume();
  }

  private applyVolume(): void {
    this.bridge.setVolume(Math.min(1, Math.pow(10, (this.gainDb - this.headroom) / 20)) * this.vol);
  }

  preload(url: string | null, meta?: Meta): void {
    this.trace("next", url?.slice(-50) ?? "");
    this.bridge.setNext(url ? absolute(url) : "", meta?.title ?? "", meta?.artist ?? "", absArt(meta));
  }

  setVotes(v: number | null): void {
    this.bridge.setVotes(v ?? -1);
  }
}
