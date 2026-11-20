// Menus (⋯, right-click, a held finger) and short notes: one of each, drawn by App.svelte over every screen.
import { Store } from "../core/store";

export type MenuItem =
  | {label: string; act: () => void | Promise<void>; danger?: boolean; off?: boolean; keep?: boolean}
  | {heading: string}
  | "hr";

export interface MenuSpec {
  items: MenuItem[];
  x: number;            // where it opens: below this point (an anchor's bottom-left or bottom-right)
  y: number;
  alignRight?: boolean; // its right edge at x (a ⋯ at the end of a row)
  key?: string;         // what opened it (tapping the same ⋯ again closes it)
}

export const menu = new Store<MenuSpec | null>(null);

/** Opens below a button; the same button again closes it. */
export function menuAt(el: HTMLElement, items: MenuItem[], alignRight = true): void {
  const key = el.dataset.menuKey || (el.dataset.menuKey = Math.random().toString(36).slice(2));
  if (menu.get()?.key === key) return menu.set(null);
  const r = el.getBoundingClientRect();
  menu.set({items, x: alignRight ? r.right : r.left, y: r.bottom + 4, alignRight, key});
}

/** Opens at a point (right-click, or a finger held and let go). */
export function menuPoint(x: number, y: number, items: MenuItem[]): void {
  menu.set({items, x, y});
}

export const toastText = new Store<{text: string; n: number} | null>(null);
let toastTimer: ReturnType<typeof setTimeout> | undefined;

export function toast(text: string): void {
  toastText.set({text, n: (toastText.get()?.n ?? 0) + 1});
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastText.set(null), 2200);
}

/** Edit details (a playlist's name and description). */
export const editing = new Store<{pid: number; name: string; description: string} | null>(null);

/** Add to playlist… for a song (`pid`: the playlist it was opened from). */
export const sheet = new Store<{trackId: number; title: string; artist: string; pid: number; folderId: number | null} | null>(null);

/** The playing song's full view. */
export const fullView = new Store<boolean>(false);
