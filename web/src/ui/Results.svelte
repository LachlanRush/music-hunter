<script lang="ts">
  // A playlist's Find music results (SPEC.md §18.30): Off and Add by the track's score when it played.
  import { onMount } from "svelte";
  import { api } from "../core/api";
  import { go } from "../core/router";
  import Feedback from "./Feedback.svelte";

  interface Point { x: number; off: number; add: number; n: number; }
  interface Data { name: string; plays: number; off: number; add: number; left_out: number; curve: Point[]; rug: number[]; max: number; }

  let { id }: { id: number } = $props();
  let d = $state<Data | null>(null);
  let error = $state("");
  let width = $state(600);
  let tip = $state<{p: Point; x: number; y: number} | null>(null);
  let svg: SVGSVGElement | undefined = $state();

  onMount(async () => {
    try {
      d = await api<Data>(`/api/playlists/${id}/results`);
      document.title = `${d.name} · Results · Music Hunter`;
    } catch (e) {
      error = `Couldn't load the results: ${(e as Error).message}`;
    }
  });

  const W = $derived(Math.max(300, Math.round(width - 24)));
  const H = $derived(W < 520 ? 280 : 360);
  const L = 40, R = 40, T = 14, B = 46;
  const ymax = $derived(d ? Math.min(100, Math.ceil(Math.max(10, ...d.curve.flatMap((c) => [c.off, c.add])) / 10) * 10 + 10) : 100);
  const x = (v: number) => L + (W - L - R) * v / (d?.max || 1);
  const y = (v: number) => T + (H - T - B) * (1 - v / ymax);
  const yTicks = $derived(Array.from({length: Math.floor(ymax / (ymax > 60 ? 20 : 10)) + 1}, (_, i) => i * (ymax > 60 ? 20 : 10)));
  const xTicks = $derived.by(() => {
    if (!d) return [];
    const step = d.max > 3 || W < 520 ? 1 : 0.5, out: number[] = [];
    for (let v = 0; v <= d.max + 1e-9; v += step) out.push(v);
    return out;
  });
  const line = (key: "off" | "add") => d ? "M" + d.curve.map((p) => `${x(p.x).toFixed(1)},${y(p[key]).toFixed(1)}`).join("L") : "";
  // Direct labels at the right end, pushed apart when the lines meet there.
  const ends = $derived.by(() => {
    if (!d?.curve.length) return null;
    const last = d.curve[d.curve.length - 1];
    let yo = Math.min(y(last.off), H - B - 4), ya = Math.min(y(last.add), H - B - 4);
    if (Math.abs(yo - ya) < 14) { if (last.off >= last.add) yo = ya - 14; else ya = yo - 14; }
    return {x: x(last.x) + 6, yo, ya};
  });

  function move(ev: PointerEvent) {
    if (!d || !svg) return;
    const pt = svg.getBoundingClientRect(), sx = (ev.clientX - pt.left) * W / pt.width;
    if (sx < L || sx > W - R) { tip = null; return; }
    const p = d.curve.reduce((a, b) => (Math.abs(x(b.x) - sx) < Math.abs(x(a.x) - sx) ? b : a));
    tip = {p, x: Math.min(ev.clientX + 12, innerWidth - 240), y: ev.clientY - 36};
  }
</script>

<main class="viz">
  <nav><button class="backlink" aria-label="Back to the playlist" onclick={() => go(`/playlist/${id}`)}>‹</button></nav>
  <section class="intro">
    <h1>{d?.name ? `${d.name} · Results` : "Results"}</h1>
    <p class="sub">Off and Add in Find music by the track's score when it played, from the gate (0) up. Each point on the lines averages the plays near that score, and the lines stop where there are too few to say; ticks along the bottom are the plays.</p>
  </section>
  {#if error}<p class="error">{error}</p>{/if}
  {#if d}
    <div class="legend"><span><i style="background:var(--off)"></i>Off</span><span><i style="background:var(--add)"></i>Add</span></div>
    <div class="chart" bind:clientWidth={width}>
      <p class="sub">{d.plays < 2 ? "Not enough rated plays yet." : `${d.plays} rated plays · ${d.off}% Off · ${d.add}% Add`
        + (d.left_out ? ` · ${d.left_out} left out (from an over-full seed share, the pool bug fixed on 6 Nov)` : "")}</p>
      {#if d.plays >= 2}
        <svg bind:this={svg} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Off and Add rate by score"
             onpointermove={move} onpointerleave={() => (tip = null)}>
          {#each yTicks as v (v)}
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
            <text x={L - 8} y={y(v) + 4} text-anchor="end">{v}%</text>
          {/each}
          {#each xTicks as v (v)}<text x={x(v)} y={H - B + 18} text-anchor="middle">{v.toFixed(1)}</text>{/each}
          <text x={(L + W - R) / 2} y={H - 6} text-anchor="middle">score when played (0 = the gate)</text>
          {#each d.rug as v, i (i)}<line x1={x(v)} x2={x(v)} y1={H - B} y2={H - B + 5} stroke="var(--muted)" stroke-opacity="0.5" />{/each}
          {#each ["off", "add"] as const as k (k)}
            <path d={line(k)} fill="none" stroke={`var(--${k})`} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
          {/each}
          {#if ends}
            <text class="end" x={ends.x} y={ends.yo + 4} style="fill: var(--off)">Off</text>
            <text class="end" x={ends.x} y={ends.ya + 4} style="fill: var(--add)">Add</text>
          {/if}
          {#if tip}
            <line x1={x(tip.p.x)} x2={x(tip.p.x)} y1={T} y2={H - B} stroke="var(--muted)" stroke-dasharray="2 3" />
            {#each ["off", "add"] as const as k (k)}
              <circle cx={x(tip.p.x)} cy={y(tip.p[k])} r="4" fill={`var(--${k})`} stroke="var(--surface)" stroke-width="2" />
            {/each}
          {/if}
        </svg>
      {/if}
    </div>
    {#if tip}<div class="tip" style:left={`${tip.x}px`} style:top={`${tip.y}px`}>score {tip.p.x.toFixed(2)} · Off {tip.p.off}% · Add {tip.p.add}% · ~{Math.round(tip.p.n)} plays</div>{/if}
  {/if}
  <Feedback />
</main>

<style>
  .viz { --off: #e66767; --add: #3987e5; --grid: rgba(255, 255, 255, 0.1); max-width: 1100px; }
  nav .backlink { border: 0; cursor: pointer; }
  .intro { display: grid; gap: 8px; }
  h1 { font: 800 clamp(26px, 5vw, 40px)/1.05 var(--display); letter-spacing: -0.02em; margin: 0; }
  .sub { color: var(--muted); margin: 0; max-width: 68ch; font-size: 14px; }
  .legend { display: flex; gap: 16px; font-size: 13px; color: var(--muted); }
  .legend i { display: inline-block; width: 14px; height: 2px; margin-right: 6px; vertical-align: 3px; }
  .chart { background: rgba(255, 255, 255, 0.04); border-radius: 10px; padding: 12px 12px 6px; }
  .chart .sub { margin: 2px 4px 8px; font-size: 13px; }
  svg { display: block; width: 100%; height: auto; overflow: visible; touch-action: none; }
  svg text { font: 400 11px var(--mono); fill: var(--muted); }
  svg text.end { font: 600 12px var(--body); }
  .tip { position: fixed; pointer-events: none; background: var(--fg); color: var(--bg); font: 400 12px/1.4 var(--body);
    padding: 6px 9px; border-radius: 6px; z-index: 10; }
</style>
