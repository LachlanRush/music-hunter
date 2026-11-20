<script lang="ts">
  // A line of text that, while `active` (the song is playing) and too long to fit, scrolls across its space: a pause
  // at the start, then it glides left and comes round again (a second copy follows the first), like Spotify's.
  let { text, active = false }: { text: string; active?: boolean } = $props();

  let box: HTMLSpanElement | undefined = $state();
  let one: HTMLSpanElement | undefined = $state();
  let over = $state(0);            // how far the text runs past the box (0: it fits)
  let width = $state(0);           // the text's own width

  $effect(() => {
    const b = box, o = one;
    void text;
    if (!b || !o) return;
    const measure = () => { width = o.scrollWidth; over = Math.max(0, width - b.clientWidth); };   // (its full width, even cut short)
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(b);
    ro.observe(o);
    return () => ro.disconnect();
  });

  const GAP = 48;                                   // px between the end and the copy coming round
  const scrolling = $derived(active && over > 1);
  const dist = $derived(width + GAP);
  const secs = $derived(dist / 30 + 2.5);           // 30 px a second, plus the pause at the start

  let track: HTMLSpanElement | undefined = $state();
  $effect(() => {
    const t = track;
    if (!t || !scrolling) return;
    const pause = 2.5 / secs;
    const a = t.animate([
      {transform: "translateX(0)", offset: 0},
      {transform: "translateX(0)", offset: pause},
      {transform: `translateX(${-dist}px)`, offset: 1},
    ], {duration: secs * 1000, iterations: Infinity});
    return () => a.cancel();
  });
</script>

<span class="marquee" class:scrolling bind:this={box}>
  <span class="track" bind:this={track}>
    <span class="one" bind:this={one}>{text}</span>{#if scrolling}<span class="copy" aria-hidden="true">{text}</span>{/if}
  </span>
</span>

<style>
  .marquee { display: block; overflow: hidden; white-space: nowrap; min-width: 0; }
  .marquee:not(.scrolling) .one { display: block; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  .track { display: inline-flex; }
  .marquee:not(.scrolling) .track { display: block; }
  .one, .copy { flex: none; white-space: nowrap; }
  .copy { padding-left: 48px; }
  .scrolling { -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 6px, #000 calc(100% - 16px), transparent);
    mask-image: linear-gradient(90deg, transparent 0, #000 6px, #000 calc(100% - 16px), transparent); }
  .scrolling .track { will-change: transform; }
</style>
