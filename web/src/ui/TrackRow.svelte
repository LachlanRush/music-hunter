<script lang="ts">
  // One song in a playlist: cover, names, length and tempo (or where its audio is), stars, ⋯.
  import type { Track } from "../core/api";
  import { fmt } from "../app";

  let { t, playing = false, filtered = false, dragging = false, found = false, status = "", onstar, onmore }: {
    t: Track; playing?: boolean; filtered?: boolean; dragging?: boolean; found?: boolean; status?: string;
    onstar: (n: number) => void; onmore: (el: HTMLElement) => void;
  } = $props();
</script>

<li data-row={t.id} data-stars={t.stars ?? 0} class:playing class:unavailable={!t.audio} class:filtered class:dragging class:found>
  {#if t.art}<img class="art" src={t.art} alt="" loading="lazy" draggable="false">{:else}<span class="art">♪</span>{/if}
  <div class="t">
    <b>{t.title}</b><span>{t.artist}</span>
    <span class="sub">
      {#if t.audio}{fmt(t.duration_s)}{#if t.bpm_shown} • {t.bpm_shown}{/if}{#if t.bpm_override} → {t.bpm_override}{/if}
      {:else if status === "now"}<span class="row-step">Downloading now</span>
      {:else}{status || "No audio"}{/if}
    </span>
  </div>
  <span class="stars" role="group" aria-label="Stars">
    {#each [1, 2, 3, 4, 5] as n}
      <button class:on={!!t.stars && n <= t.stars} onclick={() => onstar(n)}
              aria-label={`${n} star${n > 1 ? "s" : ""}${t.stars === n ? " (tap again to clear)" : ""}`}>★</button>
    {/each}
  </span>
  <div class="more"><button aria-label={`More for ${t.title}`} onclick={(e) => onmore(e.currentTarget)}>⋯</button></div>
</li>

<style>
  li { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto auto; gap: 12px; align-items: center;
    padding: 6px 8px; margin-inline: -8px; border-radius: 6px; cursor: pointer;
    user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; }
  @media (hover: hover) { li:not(.unavailable):hover { background: rgba(255, 255, 255, 0.07); } }
  li.playing .t b { color: var(--accent); }
  li.unavailable { opacity: .45; cursor: default; }
  li.filtered { opacity: .4; }
  li.found { animation: found 1.4s ease-out; }
  @keyframes found { from { background: var(--accent-soft); } to { background: transparent; } }
  li:global(.held) { background: rgba(255, 255, 255, 0.1); }
  li.dragging { background: var(--raised) !important; box-shadow: 0 10px 28px rgba(0, 0, 0, 0.5); position: relative; z-index: 3; }
  .art { width: 44px; height: 44px; border-radius: 4px; background: var(--raised); object-fit: cover; display: grid;
    place-items: center; color: var(--faint); }
  .t .sub { color: var(--faint); font-size: 12px; }
  .row-step { color: var(--accent); }
  .stars { display: flex; gap: 2px; margin-left: 8px; }
  .stars button { border: 0; background: none; padding: 4px 2px; font-size: 19px; line-height: 1; color: rgba(255, 255, 255, 0.16); }
  .stars button.on { color: var(--accent); }
  @media (hover: hover) { .stars button:hover { color: var(--fg); } }
  .more button { border: 0; background: none; font-size: 20px; padding: 4px 8px; color: var(--muted); border-radius: 50%; }
  .more button:hover { color: var(--fg); }
  @media (max-width: 600px) {
    li { gap: 10px; }
    .stars { margin-left: 2px; }
    .stars button { font-size: 18px; padding: 4px 1px; }
  }
</style>
