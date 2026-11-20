<script lang="ts">
  // The playing song, full screen (feedback #58–#64): slides up and down, swipe the cover to change song (the songs
  // either side come in beside it), drag down to close. One view for every screen, fed by the one player.
  import { cubicOut } from "svelte/easing";
  import { fly } from "svelte/transition";
  import { fmt, player } from "../app";
  import type { Track } from "../core/api";
  import { playlistModel } from "../core/library";
  import { go } from "../core/router";
  import Marquee from "./Marquee.svelte";
  import { CHEVRON, MINUS, NEXT, PAUSE, PLAY, PLUS, PREV, SHUFFLE } from "./icons";

  let { onclose: closeView }: { onclose: () => void } = $props();
  let np: HTMLDivElement | undefined = $state();
  // The page itself takes the finger's moves on the view (touch-action alone wasn't enough on the phone): otherwise
  // Android treats a swipe down as a fling and swallows the next tap to stop it, so right after closing the view by
  // swiping, a tap did nothing (#91, #94; seen on the Pixel, not the emulator).
  $effect(() => {
    const el = np;
    if (!el) return;
    const stop = (e: TouchEvent) => { if (e.cancelable) e.preventDefault(); };
    el.addEventListener("touchmove", stop, {passive: false});
    return () => el.removeEventListener("touchmove", stop);
  });
  // Closing: from this moment it lets every tap through, before its slide down has even begun.
  function onclose() {
    if (np) np.style.pointerEvents = "none";
    closeView();
  }
  const s = player.state;

  // Its stars, when the song is in the playlist it's playing from (Recently deleted: none).
  const pid = $derived($s.source && !$s.source.deleted ? $s.source.pid : null);
  const finding = $derived($s.source?.kind === "find");
  // (A Find music pick, once added with +, is matched by its names: the playlist knows it under another id.)
  const match = $derived($s.track ? ($s.source?.kind === "find" ? `${$s.track.artist}\n${$s.track.title}` : $s.track.id) : null);
  let inList = $state<Track | null>(null);
  $effect(() => {
    const p = pid, k = match;
    if (p == null || k == null) { inList = null; return; }
    return playlistModel(p).data.subscribe((d) => {
      inList = d?.tracks.find((x) => (typeof k === "number" ? x.id === k : `${x.artist}\n${x.title}` === k)) ?? null;
    });
  });

  let stage: HTMLDivElement | undefined = $state();
  let dx = $state(0);          // the cover dragged sideways
  let dy = $state(0);          // the view dragged down
  let sliding = $state(false); // animating to the next/previous song, or back
  let dragging = $state(false); // the view following a finger down
  let seeking = $state(false);
  let seekValue = $state(0);
  const pos = $derived(seeking ? seekValue : ($s.d ? Math.round(1000 * $s.t / $s.d) : 0));
  const gap = () => (stage?.querySelector(".np-art") as HTMLElement | null)?.offsetWidth ?? 300;
  const canGo = (dir: number) => (dir > 0 ? !!$s.nextTrack : $s.canBack);

  let g: {x: number; y: number; t: number; axis: "x" | "y" | null; id: number; onArt: boolean} | null = null;

  function down(e: PointerEvent) {
    if (e.button || (e.target as HTMLElement).closest("button, input, a")) return;
    g = {x: e.clientX, y: e.clientY, t: performance.now(), axis: null, id: e.pointerId,
         onArt: !!(e.target as HTMLElement).closest(".np-stage")};
  }
  function move(e: PointerEvent) {
    if (!g || e.pointerId !== g.id || sliding) return;
    const mx = e.clientX - g.x, my = e.clientY - g.y;
    if (!g.axis) {
      if (Math.hypot(mx, my) < 8) return;
      g.axis = Math.abs(mx) > Math.abs(my) && g.onArt ? "x" : my > 0 ? "y" : null;
      if (!g.axis) { g = null; return; }
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    }
    if (g.axis === "y") { dragging = true; dy = Math.max(0, my); }
    else dx = mx * (canGo(mx < 0 ? 1 : -1) ? 1 : 0.25);   // nothing that way: it gives a little, then springs back
  }
  function up(e: PointerEvent) {
    if (!g || e.pointerId !== g.id) return;
    const mx = e.clientX - g.x, my = e.clientY - g.y, axis = g.axis;
    const v = (axis === "x" ? Math.abs(mx) : my) / Math.max(1, performance.now() - g.t);
    g = null;
    dragging = false;
    if (axis === "y") {
      if (my > 140 || (my > 40 && v > 0.6)) onclose(); else dy = 0;
    } else if (axis === "x") {
      const dir = mx < 0 ? 1 : -1;
      if ((Math.abs(mx) > gap() * 0.28 || (Math.abs(mx) > 40 && v > 0.5)) && canGo(dir)) change(dir);
      else { sliding = true; dx = 0; setTimeout(() => (sliding = false), 260); }
    }
  }
  // The song changes at once; the covers then glide into place: the new song's cover starts where it was beside the
  // old one (so nothing jumps) and slides to the middle, the old one going out the other side.
  // Computers (my note, 2026-11-20): the view is as wide as the bar, so the songs either side would show; there it
  // has no neighbours and the new cover just appears.
  const desktop = typeof matchMedia !== "undefined" && matchMedia("(hover: hover) and (pointer: fine)").matches;

  function change(dir: number) {
    const w = gap() + 24;
    if (dir > 0) player.next(); else player.prev(true);
    if (desktop) { dx = 0; sliding = false; return; }
    sliding = false;
    dx = dx + dir * w;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      sliding = true; dx = 0;
      setTimeout(() => (sliding = false), 270);
    }));
  }
  function step(dir: number) {
    if (dir < 0 && $s.t >= 5) return player.prev();          // a few seconds in: back restarts the song
    if (canGo(dir)) change(dir);
    else if (finding) { if (dir > 0) player.next(); else player.prev(true); }   // the next pick isn't fetched yet
  }
  function key(e: KeyboardEvent) { if (e.key === "Escape") onclose(); }
  // Sliding away it no longer catches taps: they go straight through to what's under it, the bar included (#91, #94:
  // for the length of the slide the screen didn't answer).
  const leaving = (e: Event) => { (e.currentTarget as HTMLElement).style.pointerEvents = "none"; };
</script>

<svelte:window onkeydown={key} />

{#if $s.track}
  <div class="np" bind:this={np} class:drag={dragging} style:transform={dy ? `translateY(${dy}px)` : undefined}
       transition:fly|global={{y: innerHeight, duration: 225, easing: cubicOut, opacity: 1}} onoutrostart={leaving}
       onpointerdown={down} onpointermove={move} onpointerup={up} onpointercancel={up} role="dialog" aria-label="Now playing" tabindex="-1">
    <div class="np-in">
      <div class="np-top">
        <button class="icon-btn" aria-label="Close" onclick={onclose}>
          <svg viewBox="0 0 24 24"><path d={CHEVRON} fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <div class="from">{finding ? "Finding music for" : "Playing from"}
          {#if $s.source}<button class="go" onclick={() => { onclose(); go(`/playlist/${$s.source!.pid}${finding ? "" : `?track=${$s.track?.id ?? ""}`}`); }}>{$s.source.name}</button>{/if}
        </div>
        <span></span>
      </div>
      <div class="np-stage" bind:this={stage}>
        {#if $s.prevTrack && !desktop}
          {#key $s.prevTrack.id}
            <img class="np-art nb" class:slide={sliding} src={$s.prevTrack.art ?? ""} alt="" draggable="false"
                 style:transform={`translateX(${dx - gap() - 24}px)`}>
          {/key}
        {/if}
        {#key $s.track.id}
          <img class="np-art" class:slide={sliding} src={$s.track.art ?? ""} alt="" draggable="false"
               style:transform={`translateX(${dx}px)`}>
        {/key}
        {#if $s.nextTrack && !desktop}
          {#key $s.nextTrack.id}
            <img class="np-art nb" class:slide={sliding} src={$s.nextTrack.art ?? ""} alt="" draggable="false"
                 style:transform={`translateX(${dx + gap() + 24}px)`}>
          {/key}
        {/if}
      </div>
      <!-- The title across the whole width (scrolling while it plays, if it doesn't fit); the artist below it, with
           the stars (or Find music's − / +) on the same line (my note, 2026-11-20). -->
      <div class="np-meta"><b class="title"><Marquee text={$s.track.title} active={$s.playing} /></b>
        <div class="row2"><span class="artist">{$s.track.artist}</span>
        {#if finding}
          <span class="side">
            <button class="vote" class:on={$s.vote === 1} aria-label="Not for this playlist" onclick={() => player.vote("down")}>
              <svg viewBox="0 0 24 24"><path d={MINUS}/></svg></button>
            <button class="vote" class:on={$s.vote === 3} aria-label="Add to this playlist" onclick={() => player.vote("up")}>
              <svg viewBox="0 0 24 24"><path d={PLUS}/></svg></button>
          </span>
        {:else if inList && pid != null}
          <span class="stars" role="group" aria-label="Stars">
            {#each [1, 2, 3, 4, 5] as n}
              <button class:on={!!inList.stars && n <= inList.stars} onclick={() => playlistModel(pid).setStars(inList!.id, n)}
                      aria-label={`${n} star${n > 1 ? "s" : ""}`}>★</button>
            {/each}
          </span>
        {/if}
        </div>
      </div>
      {#if finding && inList && pid != null}
        <span class="stars below" role="group" aria-label="Stars">
          {#each [1, 2, 3, 4, 5] as n}
            <button class:on={!!inList.stars && n <= inList.stars} onclick={() => playlistModel(pid).setStars(inList!.id, n)}
                    aria-label={`${n} star${n > 1 ? "s" : ""}`}>★</button>
          {/each}
        </span>
      {/if}
      <div class="np-seek">
        <span>{fmt(seeking ? seekValue / 1000 * $s.d : $s.t)}</span>
        <input type="range" min="0" max="1000" value={pos} aria-label="Position"
               oninput={(e) => { seeking = true; seekValue = Number(e.currentTarget.value); }}
               onchange={(e) => { player.seek(Number(e.currentTarget.value) / 1000 * $s.d); seeking = false; }}>
        <span>{fmt($s.d)}</span>
      </div>
      <div class="np-controls">
        {#if finding}<span></span>{:else}
          <button class="round" class:on={$s.shuffle} aria-label="Shuffle" onclick={() => player.setShuffle(!$s.shuffle)}>
            <svg viewBox="0 0 24 24"><path d={SHUFFLE}/></svg></button>{/if}
        <button class="round" aria-label="Previous" onclick={() => step(-1)}><svg viewBox="0 0 24 24"><path d={PREV}/></svg></button>
        <button class="round primary" class:loading={$s.loading} aria-label={$s.playing ? "Pause" : "Play"}
                onclick={() => player.toggle()}><svg viewBox="0 0 24 24"><path d={$s.playing ? PAUSE : PLAY}/></svg></button>
        <button class="round" aria-label="Next" onclick={() => step(1)}><svg viewBox="0 0 24 24"><path d={NEXT}/></svg></button>
        <span></span>
      </div>
    </div>
  </div>
{/if}

<style>
  .np { position: fixed; inset: 0; z-index: 30; display: flex; justify-content: center; overflow: hidden auto;
    background: linear-gradient(180deg, var(--glow, rgba(255,255,255,.06)) 0%, var(--bg) 70%), var(--bg);
    touch-action: none; will-change: transform; transition: transform .3s cubic-bezier(.2, .8, .2, 1); }
  .np.drag { transition: none; }
  .np-in { width: min(100%, 520px); height: 100dvh; min-height: 520px; display: flex; flex-direction: column; gap: 18px;
    padding: max(12px, env(safe-area-inset-top)) 22px calc(22px + env(safe-area-inset-bottom)); }
  /* Computers: as wide as the player bar (its content, up to 1280 px, inside 12 px sides) */
  .np-top { display: grid; grid-template-columns: 40px 1fr 40px; align-items: center; text-align: center; }
  .from { font-size: 12px; color: var(--muted); line-height: 1.3; }
  .go { display: block; margin: 0 auto; border: 0; background: none; padding: 0; color: var(--fg); font: 700 14px var(--body); }
  .go:hover { text-decoration: underline; }
  .np-stage { flex: 1 1 0; min-height: 140px; container-type: size; display: grid; place-items: center; cursor: grab; }
  .np-art { grid-area: 1 / 1; width: min(100cqw, 100cqh); aspect-ratio: 1; border-radius: 8px; object-fit: cover;
    background: var(--raised); box-shadow: 0 14px 40px rgba(0,0,0,.55); user-select: none; -webkit-user-drag: none;
    will-change: transform; }
  .np-art.nb { box-shadow: none; }
  .np-art.slide { transition: transform .26s cubic-bezier(.2, .8, .2, 1); }
  .np-meta { display: grid; gap: 2px; min-width: 0; }
  .np-meta .title { font: 700 22px/1.25 var(--body); min-width: 0; }
  .row2 { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; }
  .artist { color: var(--muted); font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .row2 .stars { margin-right: -2px; }
  .side { display: flex; align-items: center; gap: 4px; flex: none; }
  .vote { width: 44px; height: 44px; border: 0; padding: 0; background: none; color: rgba(255, 255, 255, 0.75);
    display: grid; place-items: center; }
  .vote svg { width: 26px; height: 26px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; }
  .vote:hover { color: var(--fg); }
  .vote.on { color: var(--accent); }
  .vote.on svg { stroke-width: 3; }
  .stars.below { margin: -10px 0 0 -4px; }
  .stars { display: flex; gap: 2px; flex: none; }
  .stars button { border: 0; background: none; padding: 4px 2px; font-size: 22px; line-height: 1; color: rgba(255, 255, 255, 0.16); }
  .stars button.on { color: var(--accent); }
  /* the times in fixed-width boxes: digits differ in width, so "0:01" → "0:02" nudged the slider (my note) */
  .np-seek { display: grid; grid-template-columns: 3.4em 1fr 3.4em; gap: 10px; align-items: center; font-size: 12px; color: var(--muted);
    font-variant-numeric: tabular-nums; }
  .np-seek > span:last-child { text-align: right; }   /* times flush with the title and the stars above */
  .np-controls { display: grid; grid-template-columns: 1fr auto auto auto 1fr; align-items: center; gap: 18px; }
  .np-controls > :first-child { justify-self: start; }
  .np-controls .round { width: 52px; height: 52px; }
  .np-controls .round :global(svg) { width: 26px; height: 26px; }
  .np-controls .round.primary { width: 72px; height: 72px; }
  /* Computers: as wide as the player bar (its content, up to 1280 px, inside 12 px sides), and a cover no bigger
     than 660 px (a tall window made it huge, 440 was too small; my notes) */
  @media (hover: hover) and (pointer: fine) {
    .np-in { width: min(100%, 1304px); padding-inline: 12px; }
    .np-art { width: min(100cqw, 100cqh, 660px); }
  }
</style>
