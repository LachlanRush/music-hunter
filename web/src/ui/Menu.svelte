<script lang="ts">
  // The one menu: opens downwards (feedback #40); if it would run under the player bar the page scrolls up just
  // enough to show it all, with room added below when there isn't enough; kept inside the screen sideways (#52).
  import { tick } from "svelte";
  import { player } from "../app";
  import { menu, type MenuItem } from "./overlay";

  let el: HTMLDivElement | undefined = $state();
  let top = $state(0);
  let left = $state(0);
  let maxHeight = $state(400);
  let room = $state(0);          // extra space below the page, so it can scroll far enough
  let placing = false;

  $effect(() => {
    const m = $menu;
    if (!m) return;
    void place(m.x, m.y, !!m.alignRight);
  });

  async function place(x: number, y: number, right: boolean) {
    placing = true;
    top = y;
    left = -9999;
    await tick();
    if (!el) return;
    const bar = player.state.get().track ? (document.querySelector(".player") as HTMLElement | null)?.offsetHeight ?? 110 : 0;
    const bottom = innerHeight - bar - 8;
    maxHeight = Math.max(160, bottom - 24);
    await tick();
    const r = el.getBoundingClientRect(), edge = document.documentElement.clientWidth - 8;
    left = Math.max(8, Math.min(right ? x - r.width : x, edge - r.width));
    const over = y + r.height - bottom;
    if (over > 0) {
      const short = scrollY + innerHeight + over - document.documentElement.scrollHeight;
      if (short > 0) room += short;
      await tick();
      window.scrollBy(0, over);
      top = y - over;
    }
    setTimeout(() => (placing = false), 50);
  }

  async function choose(item: MenuItem) {
    if (typeof item !== "object" || !("act" in item)) return;
    if (!item.keep) menu.set(null);
    await item.act();
  }

  function outside(e: Event) {
    if (!$menu || (el && el.contains(e.target as Node))) return;
    const opener = (e.target as HTMLElement).closest?.("[data-menu-key]") as HTMLElement | null;
    if (opener && opener.dataset.menuKey === $menu.key) return;     // its own ⋯ toggles it
    menu.set(null);
  }
  function key(e: KeyboardEvent) { if (e.key === "Escape") menu.set(null); }
  function scrolled() { if (!placing && $menu) menu.set(null); }
</script>

<svelte:window onpointerdown={outside} onkeydown={key} onscroll={scrolled} onresize={() => menu.set(null)} />
<div style:height={`${room}px`} aria-hidden="true"></div>

{#if $menu}
  <div class="menu fixed" bind:this={el} style:top={`${top}px`} style:left={`${left}px`} style:max-height={`${maxHeight}px`} role="menu">
    {#each $menu.items as item, i (i)}
      {#if item === "hr"}<hr>
      {:else if "heading" in item}<div class="label">{item.heading}</div>
      {:else}
        <button role="menuitem" class:off={item.off} style:color={item.danger ? "var(--bad)" : undefined}
                onclick={() => choose(item)}>{item.label}</button>
      {/if}
    {/each}
  </div>
{/if}

<style>
  .menu.fixed { position: fixed; right: auto; }
  .menu :global(button.off) { color: var(--muted); }
</style>
