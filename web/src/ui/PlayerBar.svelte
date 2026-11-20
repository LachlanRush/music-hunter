<script lang="ts">
  // The one player bar: on every screen, never replaced, so nothing moves or changes when the screen does.
  import { engine, fmt, player } from "../app";
  import { NEXT, PAUSE, PLAY, PREV, SHUFFLE } from "./icons";

  let { onopen }: { onopen: () => void } = $props();
  const s = player.state;
  let seeking = $state(false);
  let seekValue = $state(0);
  let volume = $state(readVolume());

  function readVolume(): number {
    try { const v = localStorage.getItem("volume"); return v === null ? 100 : Number(v); } catch { return 100; }
  }
  $effect(() => {
    engine.setVolume(volume / 100);
    try { localStorage.setItem("volume", String(volume)); } catch { /* only a convenience */ }
  });
  const pos = $derived(seeking ? seekValue : ($s.d ? Math.round(1000 * $s.t / $s.d) : 0));

  function open(e: MouseEvent) {
    if ((e.target as HTMLElement).closest("button, input, .seek")) return;
    onopen();
  }
</script>

{#if $s.track}
  <div class="player">
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <div class="player-in" onclick={open}>
      {#if $s.track.art}<img class="art" src={$s.track.art} alt="">{:else}<div class="art"></div>{/if}
      <div class="t">
        <b>{$s.track.title}</b><span>{$s.track.artist}</span>
        {#if $s.note}<span class="meta">{$s.note}</span>{/if}
      </div>
      <div class="controls">
        <span class="slot">
          {#if $s.source?.kind !== "find"}
          <button class="round" class:on={$s.shuffle} aria-label="Shuffle" aria-pressed={$s.shuffle}
                  onclick={() => player.setShuffle(!$s.shuffle)}><svg viewBox="0 0 24 24"><path d={SHUFFLE}/></svg></button>
          {/if}
        </span>
        <button class="round" aria-label="Previous" onclick={() => player.prev()}><svg viewBox="0 0 24 24"><path d={PREV}/></svg></button>
        <button class="round primary" class:loading={$s.loading} aria-label={$s.playing ? "Pause" : "Play"}
                onclick={() => player.toggle()}><svg viewBox="0 0 24 24"><path d={$s.playing ? PAUSE : PLAY}/></svg></button>
        <button class="round" aria-label="Next" onclick={() => player.next()}><svg viewBox="0 0 24 24"><path d={NEXT}/></svg></button>
      </div>
      <div class="seek">
        <span>{fmt(seeking ? seekValue / 1000 * $s.d : $s.t)}</span>
        <input type="range" min="0" max="1000" value={pos} aria-label="Position"
               oninput={(e) => { seeking = true; seekValue = Number(e.currentTarget.value); }}
               onchange={(e) => { player.seek(Number(e.currentTarget.value) / 1000 * $s.d); seeking = false; }}>
        <span>{fmt($s.d)}</span>
        <input type="range" class="volume" min="0" max="100" bind:value={volume} aria-label="Volume">
      </div>
    </div>
  </div>
{/if}
