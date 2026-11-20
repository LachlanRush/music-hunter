// A tiny observable value (the same contract as a Svelte store, so screens can use `$store`), kept free of any UI
// framework so the player and data logic could serve other screens later (SPEC.md §20.2).
export type Subscriber<T> = (value: T) => void;

export class Store<T> {
  private value: T;
  private subs = new Set<Subscriber<T>>();

  constructor(value: T) {
    this.value = value;
  }

  get(): T {
    return this.value;
  }

  set(value: T): void {
    this.value = value;
    for (const s of [...this.subs]) s(value);
  }

  update(f: (value: T) => T): void {
    this.set(f(this.value));
  }

  subscribe(s: Subscriber<T>): () => void {
    this.subs.add(s);
    s(this.value);
    return () => this.subs.delete(s);
  }
}
