// Holding and dragging (a song within its stars, a playlist or section on Home). A mouse drags at once; a finger holds
// a moment first, so scrolling still works: held and let go without moving opens the item's options, held and moved
// drags it. Right-click opens the options too.

export interface PressOptions {
  /** The item under the pointer (a row, a card, a section's name row), or null. */
  pick(target: HTMLElement): HTMLElement | null;
  canDrag(item: HTMLElement): boolean;
  menu(item: HTMLElement, x: number, y: number): void;
  start(item: HTMLElement, x: number, y: number): void;
  move(x: number, y: number): void;
  end(): void;
  cancel(): void;
}

const HOLD_MS = 350, SLOP = 8;

export function pressable(root: HTMLElement | Document, o: PressOptions): () => void {
  type D = {item: HTMLElement; x0: number; y0: number; x: number; y: number; touch: boolean; active: boolean;
            held?: boolean; timer?: ReturnType<typeof setTimeout>; since?: number};
  let d: D | null = null, justDragged = false, lastTouch = 0;
  const swallow = () => { justDragged = true; setTimeout(() => (justDragged = false), 50); };
  const begin = () => {
    if (!d) return;
    d.active = true;
    d.since = performance.now();
    d.item.classList.remove("held");
    navigator.vibrate?.(15);
    o.start(d.item, d.x, d.y);
  };
  // Near the top or bottom edge (above the player bar) the page scrolls, faster the nearer the edge (feedback #116:
  // a wide fixed zone moved the page while dragging in the lower part of the screen).
  const scroll = (y: number) => {
    if (!d || performance.now() - (d.since ?? 0) < 300) return;      // (not in the drag's first moments)
    const bar = (document.querySelector(".player") as HTMLElement | null)?.offsetHeight ?? 0;
    const top = 48, bottom = innerHeight - bar - 48;
    if (y < top) window.scrollBy(0, -Math.ceil((top - y) / 5));
    else if (y > bottom) window.scrollBy(0, Math.ceil((y - bottom) / 5));
  };

  const onDown = (e: Event) => {
    const m = e as MouseEvent;
    if (m.button !== 0) return;
    const item = o.pick(m.target as HTMLElement);
    if (item) d = {item, x0: m.clientX, y0: m.clientY, x: m.clientX, y: m.clientY, touch: false, active: false};
  };
  const onMove = (e: Event) => {
    const m = e as MouseEvent;
    if (!d || d.touch) return;
    d.x = m.clientX; d.y = m.clientY;
    if (!d.active && o.canDrag(d.item) && Math.hypot(m.clientX - d.x0, m.clientY - d.y0) > SLOP) begin();
    else if (d.active) { scroll(m.clientY); o.move(m.clientX, m.clientY); }
  };
  const onUp = () => {
    if (!d || d.touch) return;
    if (d.active) { o.end(); swallow(); }
    d = null;
  };
  const onTouchStart = (e: Event) => {
    const t = e as TouchEvent;
    if (t.touches.length !== 1) return;
    const item = o.pick(t.target as HTMLElement);
    if (!item) return;
    lastTouch = Date.now();
    const p = t.touches[0];
    const mine: D = {item, x0: p.clientX, y0: p.clientY, x: p.clientX, y: p.clientY, touch: true, active: false};
    mine.timer = setTimeout(() => {
      if (d === mine && !d.active) { d.held = true; item.classList.add("held"); navigator.vibrate?.(15); }
    }, HOLD_MS);
    d = mine;
  };
  const onTouchMove = (e: Event) => {
    const t = e as TouchEvent;
    if (!d || !d.touch) return;
    const p = t.touches[0];
    const far = Math.hypot(p.clientX - d.x0, p.clientY - d.y0) > SLOP;
    if (!d.active && !d.held) { if (far) { clearTimeout(d.timer); d = null; } return; }   // scrolling
    if (!t.cancelable) {                                // the page is already scrolling under the finger: no drag (#116)
      clearTimeout(d.timer);
      d.item.classList.remove("held");
      if (d.active) o.cancel();
      d = null;
      return;
    }
    t.preventDefault();
    d.x = p.clientX; d.y = p.clientY;
    if (!d.active) { if (!far || !o.canDrag(d.item)) return; begin(); }
    scroll(p.clientY);
    o.move(p.clientX, p.clientY);
  };
  const onTouchEnd = (e: Event) => {
    if (!d || !d.touch) return;
    clearTimeout(d.timer);
    if (d.active) { e.preventDefault(); o.end(); swallow(); }
    else if (d.held) { e.preventDefault(); d.item.classList.remove("held"); swallow(); o.menu(d.item, d.x0, d.y0); }
    d = null;
  };
  const onTouchCancel = () => {
    if (!d) return;
    clearTimeout(d.timer);
    d.item.classList.remove("held");
    if (d.active) o.cancel();
    d = null;
  };
  const onClick = (e: Event) => {
    if (justDragged && o.pick(e.target as HTMLElement)) { e.preventDefault(); e.stopPropagation(); }
  };
  const onContext = (e: Event) => {
    const m = e as MouseEvent;
    const item = o.pick(m.target as HTMLElement);
    if (!item) return;
    m.preventDefault();
    if (Date.now() - lastTouch > 1500) o.menu(item, m.clientX, m.clientY);     // (a held finger: handled above)
  };

  const on: [EventTarget, string, EventListener, AddEventListenerOptions?][] = [
    [root, "mousedown", onDown], [document, "mousemove", onMove], [document, "mouseup", onUp],
    [root, "touchstart", onTouchStart, {passive: true}], [document, "touchmove", onTouchMove, {passive: false}],
    [document, "touchend", onTouchEnd], [document, "touchcancel", onTouchCancel],
    [root, "click", onClick, {capture: true}], [root, "contextmenu", onContext],
  ];
  for (const [t, k, f, opt] of on) t.addEventListener(k, f, opt);
  return () => { for (const [t, k, f, opt] of on) t.removeEventListener(k, f, opt); };
}
