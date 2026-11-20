<script lang="ts">
  // A playlist: its songs (To rate, Tracks, songs still downloading), stars, menus, dragging within stars, Sort by
  // sound, the star filter, Set to BPM, imports and Add a song, Recently deleted. Find music comes in milestone 3.
  import { onDestroy, onMount, tick } from "svelte";
  import { api, busy, type Track } from "../core/api";
  import { tint } from "../core/colour";
  import { findSimilar, playlistModel } from "../core/library";
  import { go, route } from "../core/router";
  import { player } from "../app";
  import { ask, askText } from "./dialogs";
  import Feedback from "./Feedback.svelte";
  import { pressable } from "./gestures";
  import { PAUSE, PLAY, SHUFFLE } from "./icons";
  import { editing, menuAt, menuPoint, sheet, toast, type MenuItem } from "./overlay";
  import { playlistItems } from "./playlistMenu";
  import TrackRow from "./TrackRow.svelte";

  let { id }: { id: number } = $props();
  // svelte-ignore state_referenced_locally
  const model = playlistModel(id);
  const data = model.data;
  const error = model.error;
  const s = player.state;

  let sortOrder = $state<{title: string; ids: number[]} | null>(null);
  let drag = $state<{id: number; stars: number; ids: number[]} | null>(null);
  let found = $state<number | null>(null);
  let link = $state("");
  let deletedOpen = $state(false);
  let main: HTMLElement | undefined = $state();

  const p = $derived($data);
  $effect(() => { if (p) player.update(p); });
  $effect(() => { void tint(p?.image ?? null); });
  $effect(() => { if (p) document.title = `${p.name} · Music Hunter`; });

  const mine = $derived($s.source?.kind === "playlist" && $s.source.pid === id && !$s.source.deleted);
  const finding = $derived($s.source?.kind === "find" && $s.source.pid === id);
  const passes = (t: Track) => !p?.min_stars || (t.stars || 0) >= p.min_stars;
  const ready = $derived(p ? p.tracks.filter((t) => t.audio) : []);
  const waiting = $derived(p ? p.tracks.filter((t) => !t.audio) : []);
  const toRate = $derived(ready.filter((t) => t.status === "added"));
  const tracks = $derived.by(() => {
    const list = ready.filter((t) => t.status !== "added");
    if (!drag) return list;
    // While a song is dragged: its stars' songs in the order being dragged into.
    const d = drag, group = new Map(list.filter((t) => (t.stars ?? 0) === d.stars).map((t) => [t.id, t]));
    let k = 0;
    return list.map((t) => ((t.stars ?? 0) === d.stars ? group.get(d.ids[k++])! : t));
  });
  const sorted = $derived.by(() => {
    if (!sortOrder || !p) return null;
    const at = new Map(sortOrder.ids.map((x, i) => [x, i]));
    return [...p.tracks].sort((a, b) => (at.get(a.id) ?? 1e9) - (at.get(b.id) ?? 1e9));
  });
  const total = $derived(p ? p.tracks.reduce((a, t) => a + (t.duration_s || 0), 0) : 0);
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const length = (secs: number) => {
    const m = Math.round(secs / 60);
    return m >= 60 ? `${Math.floor(m / 60)} hr ${m % 60} min` : `${m} min`;
  };

  // --- loading, and work started elsewhere ---------------------------------------------------------------------
  let stopWatch = () => {};
  onMount(() => {
    document.body.classList.add("glow");              // the glow behind the header, in the cover's colour
    void model.load().then(() => findSong());
    void loadFind();
    stopWatch = model.watch();
    const stop = pressable(main!, {
      pick: (el) => (el.closest(".stars, .more, .menu") ? null : el.closest("li[data-row]")),
      canDrag: (li) => !!li.closest("ol.tracks") && !sortOrder,
      menu: (li, x, y) => { const t = byId(Number(li.dataset.row)); if (t) menuPoint(x, y, trackItems(t)); },
      start: (li) => {
        const sid = Number(li.dataset.row), stars = Number(li.dataset.stars);
        drag = {id: sid, stars, ids: tracks.filter((t) => (t.stars ?? 0) === stars).map((t) => t.id)};
      },
      move: (x, y) => {
        if (!drag) return;
        const over = (document.elementFromPoint(x, y) as HTMLElement | null)?.closest("ol.tracks li[data-row]") as HTMLElement | null;
        if (!over || Number(over.dataset.row) === drag.id || Number(over.dataset.stars) !== drag.stars) return;
        const r = over.getBoundingClientRect(), target = Number(over.dataset.row);
        const ids = drag.ids.filter((x) => x !== drag!.id);
        ids.splice(ids.indexOf(target) + (y > r.top + r.height / 2 ? 1 : 0), 0, drag.id);
        drag = {...drag, ids};
      },
      end: () => { const d = drag; drag = null; if (d) model.reorder(d.ids); },
      cancel: () => { drag = null; },
    });
    const clock = setInterval(() => { if (busy(p?.job)) now = Date.now(); }, 1000);
    return () => { stop(); clearInterval(clock); };
  });
  onDestroy(() => { stopWatch(); clearTimeout(findTimer); document.body.classList.remove("glow"); });

  const byId = (tid: number) => p?.tracks.find((t) => t.id === tid);
  $effect(() => { if ($route) void findSong(); });      // "Playing from …" while already here

  // "Playing from …" in the full view: the song's row, shown and lit up briefly (#38).
  async function findSong() {
    const want = Number(new URLSearchParams(location.search).get("track"));
    if (!want) return;
    history.replaceState(history.state, "", location.pathname);
    await tick();
    const row = main?.querySelector(`li[data-row="${want}"]`);
    if (!row) return;
    row.scrollIntoView({block: "center"});
    found = want;
    setTimeout(() => (found = null), 1500);
  }

  // A change that moves rows (a star): a row on screen, not the one moving, stays exactly where it was.
  async function keepView(except: number | null, change: () => void) {
    const ref = [...(main?.querySelectorAll<HTMLElement>("li[data-row]") ?? [])].find((r) => {
      if (Number(r.dataset.row) === except) return false;
      const b = r.getBoundingClientRect();
      return b.top >= 0 && b.bottom <= innerHeight;
    });
    const before = ref?.getBoundingClientRect().top ?? 0, rid = ref?.dataset.row;
    change();
    await tick();
    const now = rid && main?.querySelector(`li[data-row="${rid}"]`);
    if (now) window.scrollBy(0, now.getBoundingClientRect().top - before);
  }

  // --- playing ---------------------------------------------------------------------------------------------------
  function playAll() {
    if (!p) return;
    if (mine && $s.track) return player.toggle();
    player.play(p, {});
  }
  function shuffleAll() {
    if (!p) return;
    if (mine && $s.playing) return player.setShuffle(!$s.shuffle);
    player.play(p, {shuffle: true});
  }
  function playRow(t: Track) {
    if (!p || !t.audio) return;
    if (player.isCurrent(id, t.id)) { if (!$s.playing) player.toggle(); return; }
    player.play(p, {first: t.id});
  }
  function rowClick(e: MouseEvent) {
    if ((e.target as HTMLElement).closest(".stars, .more")) return;
    const li = (e.target as HTMLElement).closest("li[data-row]") as HTMLElement | null;
    const t = li && byId(Number(li.dataset.row));
    if (t) playRow(t);
  }
  function setFilter(v: number) {
    model.setFilter(v);
  }

  // --- a song's options ----------------------------------------------------------------------------------------
  function trackItems(t: Track): MenuItem[] {
    const items: MenuItem[] = [
      {label: "Add to playlist…", act: () => sheet.set({trackId: t.id, title: t.title, artist: t.artist, pid: id, folderId: p?.folder_id ?? null})},
      {label: "Add to queue", act: async () => {
        if (!t.audio) return;
        toast(await player.enqueue(t, id) === "find" ? "Find music is playing: it picks its own next track" : `Added to queue: ${t.title}`);
      }},
      {label: "Sort by sound like this", act: async () => {
        const order = await model.soundOrder(t.id);
        if (!order) return;
        sortOrder = {title: t.title, ids: order};
        player.setOrder(id, order);
        await tick();
        window.scrollTo({top: (main?.querySelector(".sortbar") as HTMLElement | null)?.offsetTop ?? 0, behavior: "smooth"});
      }},
      {label: "Find similar", act: async () => {
        try { go(`/playlist/${await findSimilar(t.id)}`); }
        catch (e) { model.error.set(`Couldn't make it: ${(e as Error).message}`); }
      }},
    ];
    if (t.art) items.push({label: "Use as playlist cover", act: () => model.coverFrom(t.id)});
    items.push({label: "Play at … BPM", act: async () => {
      const v = await askText(`Play "${t.title}" at`, {value: String(t.bpm_override || t.bpm_shown || ""),
                                                      placeholder: "BPM", ok: "Set", inputmode: "decimal"});
      if (v) await model.ownBpm(t.id, Number(v));
    }});
    items.push("hr", {label: "Remove from this playlist", danger: true, act: () => keepView(null, () => model.remove(t.id))});
    return items;
  }
  function clearSort() {
    sortOrder = null;
    player.setOrder(id, null);
  }

  // --- imports and Add a song ------------------------------------------------------------------------------------
  let now = $state(Date.now());
  let step = {name: "", at: 0};
  function stepLine(message: string | undefined, _now: number): string {
    if (!message || message === "Done" || message === "Reading Spotify") return "";
    const name = message.split(": ")[0];
    if (name !== step.name) step = {name, at: Date.now()};
    const secs = Math.floor((Date.now() - step.at) / 1000);
    return `${name} · ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
  }
  function rowStatus(t: Track): string {
    const job = p?.job;
    if (!busy(job)) return "No audio";
    return (job!.progress?.message || "").startsWith(`${t.artist} – ${t.title}`) ? "now" : "Waiting to download";
  }
  const job = $derived(p?.job ?? null);
  const fails = $derived(job?.progress?.failures ?? []);
  const retryable = $derived(!!job && (job.kind === "import" || job.kind === "add") && !busy(job)
    && (job.state === "failed" || fails.some((f) => /^(download|analysis) failed/.test(f.reason))));
  async function addSong(e: SubmitEvent) {
    e.preventDefault();
    if (await model.addSong(link.trim())) link = "";
  }

  // --- Recently deleted --------------------------------------------------------------------------------------------
  const ago = (stamp: string) => {
    const days = Math.floor((Date.now() - Date.parse(stamp.replace(" ", "T") + "Z")) / 864e5);
    return days ? `${days} day${days > 1 ? "s" : ""} ago` : "today";
  };
  async function forget(t: Track) {
    if (await ask(`Delete "${t.title}" for good?`, {detail: "It can't be restored.", ok: "Delete", danger: true})) model.deleteForever(t.id);
  }
  async function forgetAll() {
    const n = p?.deleted?.length ?? 0;
    if (n && await ask(`Delete all ${n} track${n > 1 ? "s" : ""} for good?`,
        {detail: "Everything in Recently deleted goes. It can't be restored.", ok: "Delete all", danger: true})) model.deleteAllForever();
  }
  function playDeleted(t: Track) {
    if (!p || !t.audio) return;
    if (player.isCurrentDeleted(id, t.id)) { if (!$s.playing) player.toggle(); return; }
    player.playDeleted(p, t.id);
  }

  // --- Find music: its picks play in the bar; tapping it again (or Play, Shuffle, a song) goes back to the playlist ---
  interface FindState { state: "none" | "preparing" | "ready" | "error"; playable?: number; early_start?: number;
                        message?: string; seeds: number; starred: number; }
  let find = $state<FindState | null>(null);
  let findTimer: ReturnType<typeof setTimeout> | undefined;
  async function loadFind() {
    try { find = await api<FindState>(`/api/playlists/${id}/find`); } catch { return; }
    clearTimeout(findTimer);
    if (find.state === "preparing") findTimer = setTimeout(loadFind, 3000);
  }
  async function findMusic() {
    if (!find || !p) return;
    if (find.state === "ready") { if (finding) player.leaveFind(); else player.startFind(p); return; }
    if (find.state === "none" && !await ask(`Find music for "${p.name}"?`, {ok: "Find music",
        detail: `New tracks are found from your ${find.seeds} ${find.starred ? "starred " : ""}track${find.seeds > 1 ? "s" : ""}, `
          + `downloaded and analysed: a few minutes before the first ${find.early_start ?? 20} are ready; the rest keep coming in the background.`})) return;
    try { await api(`/api/playlists/${id}/find`, {}); } catch (e) { model.error.set(`Couldn't start Find music: ${(e as Error).message}`); }
    void loadFind();
  }
  const findPct = $derived(find?.state === "preparing" && find.early_start ? Math.min(100, Math.round(100 * (find.playable ?? 0) / find.early_start)) : 0);
</script>

<main bind:this={main}>
  {#if p}
    <section class="head">
      <button class="backlink" aria-label="Back to the playlists" onclick={() => go("/")}>‹</button>
      <div class="cover">{#if p.image}<img src={p.image} alt="">{:else}{p.name.slice(0, 1).toUpperCase()}{/if}</div>
      <div class="head-text">
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
        <h1 onclick={() => editing.set({pid: id, name: p.name, description: p.description ?? ""})}>{p.name}</h1>
        {#if p.description}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
          <p class="desc" onclick={() => editing.set({pid: id, name: p.name, description: p.description ?? ""})}>{p.description}</p>
        {/if}
        <div class="meta">{[plural(p.tracks.length, "track"), length(total), p.tempo?.target ? `${Math.round(p.tempo.target)} BPM` : ""].filter(Boolean).join(" · ")}</div>
      </div>
    </section>

    <section class="actions">
      <button class="play-big" aria-label={mine && $s.playing ? "Pause" : "Play"} onclick={playAll}>
        <svg viewBox="0 0 24 24"><path d={mine && $s.playing ? PAUSE : PLAY}/></svg></button>
      <button class="icon-btn" class:on={mine && $s.playing && $s.shuffle} aria-label="Shuffle" aria-pressed={mine && $s.shuffle} onclick={shuffleAll}>
        <svg viewBox="0 0 24 24"><path d={SHUFFLE}/></svg></button>
      <button class="find" class:ready={find?.state === "ready"} class:on={finding} disabled={find?.state === "preparing" || (find?.state === "none" && !find.seeds)}
              title={find?.state === "none" && !find.seeds ? "Add a song first" : ""} onclick={findMusic}>
        {find?.state === "preparing" ? "Preparing…" : find?.state === "error" ? "Try again" : "Find music"}</button>
      <select class="filter" aria-label="Play only tracks with at least" value={String(p.min_stars || 0)}
              onchange={(e) => setFilter(Number(e.currentTarget.value))}>
        <option value="0">All tracks</option><option value="1">★ 1+</option><option value="2">★ 2+</option>
        <option value="3">★ 3+</option><option value="4">★ 4+</option><option value="5">★ 5</option>
      </select>
      <button class="icon-btn" aria-label="More for this playlist" onclick={(e) => menuAt(e.currentTarget, playlistItems(p), false)}>
        <svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg></button>
    </section>

    <section class="tempo" aria-label="Tempo">
      <label><input type="checkbox" checked={p.tempo.on} onchange={(e) => model.saveTempo(e.currentTarget.checked, p.tempo.bpm)}> Set to</label>
      <label><input type="number" min="60" max="200" step="0.5" inputmode="decimal" aria-label="Target BPM" value={p.tempo.target}
             onchange={(e) => { const v = Number(e.currentTarget.value); model.saveTempo(p.tempo.on, v >= 60 && v <= 200 ? v : null); }}> BPM</label>
    </section>

    {#if find?.state === "preparing"}
      <section class="box">
        <div class="label">Finding music · {find.playable} of {find.early_start} tracks ready to start</div>
        <div class="bar live" role="progressbar" aria-valuenow={findPct} aria-valuemin={0} aria-valuemax={100}><i style:width={`${findPct}%`}></i></div>
        {#if find.message}<div class="meta">{find.message}</div>{/if}
      </section>
    {:else if find?.state === "error"}
      <section class="box"><div class="error">Find music stopped: {find.message}</div></section>
    {/if}

    {#if job && busy(job)}
      {@const pr = job.progress ?? {}}
      {@const pct = pr.total ? Math.round(100 * (pr.done ?? 0) / pr.total) : 0}
      <section class="box">
        <div class="label">{job.state === "queued" ? "Waiting to import" : `Importing ${pr.done ?? 0} of ${pr.total || "?"}`}</div>
        <div class="bar live" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><i style:width={`${pct}%`}></i></div>
        <div class="meta">{stepLine(pr.message, now)}</div>
      </section>
    {/if}

    {#if $error}<div class="error">{$error}</div>{/if}

    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <div class="lists" onclick={rowClick}>
      {#if sorted}
        <div class="sortbar"><span>Sorted by sound like <b>{sortOrder?.title}</b></span>
          <button class="link" onclick={clearSort}>Back to normal order</button></div>
        <section>
          <h2>Tracks <span class="meta">{plural(sorted.length, "track")}</span></h2>
          <ol>
            {#each sorted as t (t.id)}
              <TrackRow {t} playing={mine && $s.track?.id === t.id} filtered={!passes(t)} found={found === t.id}
                        onstar={(n) => keepView(t.id, () => model.setStars(t.id, n))} onmore={(el) => menuAt(el, trackItems(t))} />
            {/each}
          </ol>
        </section>
      {:else}
        {#if toRate.length}
          <section>
            <h2>To rate <span class="meta">{plural(toRate.length, "track")}</span></h2>
            <ol>
              {#each toRate as t (t.id)}
                <TrackRow {t} playing={mine && $s.track?.id === t.id} filtered={!passes(t)} found={found === t.id}
                          onstar={(n) => keepView(t.id, () => model.setStars(t.id, n))} onmore={(el) => menuAt(el, trackItems(t))} />
              {/each}
            </ol>
          </section>
        {/if}
        <section>
          <h2>Tracks {#if tracks.length}<span class="meta">{plural(tracks.length, "track")}</span>{/if}</h2>
          <ol class="tracks">
            {#each tracks as t (t.id)}
              <TrackRow {t} playing={mine && $s.track?.id === t.id} filtered={!passes(t)} found={found === t.id} dragging={drag?.id === t.id}
                        onstar={(n) => keepView(t.id, () => model.setStars(t.id, n))} onmore={(el) => menuAt(el, trackItems(t))} />
            {:else}
              <li class="empty">No tracks yet.</li>
            {/each}
          </ol>
        </section>
        {#if waiting.length}
          <section>
            <h2>{busy(job) ? "Downloading" : "No audio"} <span class="meta">{plural(waiting.length, "track")}</span></h2>
            <ol>
              {#each waiting as t (t.id)}
                <TrackRow {t} status={rowStatus(t)} found={found === t.id}
                          onstar={(n) => keepView(t.id, () => model.setStars(t.id, n))} onmore={(el) => menuAt(el, trackItems(t))} />
              {/each}
            </ol>
          </section>
        {/if}
      {/if}
    </div>

    <form class="add-song" onsubmit={addSong}>
      <input type="url" bind:value={link} placeholder="Add a song — Spotify or YouTube Music link" aria-label="Song link" required>
      <button class="btn" type="submit">Add</button>
    </form>

    {#if fails.length || job?.state === "failed"}
      <section class="job">
        {#if job?.state === "failed"}<div class="error">Import stopped: {job.error}</div>{/if}
        {#if fails.length}
          <details class="failures"><summary>{plural(fails.length, "track")} couldn't be imported</summary>
            <ul>{#each fails as f, i (i)}<li>{f.artist} – {f.title}: {f.reason}</li>{/each}</ul></details>
        {/if}
        {#if retryable}<button class="btn" onclick={() => model.retry()}>Try again</button>{/if}
      </section>
    {/if}

    {#if p.deleted?.length}
      <details class="failures deleted" bind:open={deletedOpen}>
        <summary>Recently deleted ({p.deleted.length})</summary>
        <div class="del-all"><button class="btn danger" onclick={forgetAll}>Delete all</button></div>
        <ol>
          {#each p.deleted as t (t.id)}
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
            <li class:playing={player.isCurrentDeleted(id, t.id) && !!$s.track} class:unavailable={!t.audio}
                onclick={(e) => { if (!(e.target as HTMLElement).closest(".acts")) playDeleted(t); }}>
              {#if t.art}<img class="art" src={t.art} alt="" loading="lazy">{:else}<span class="art"></span>{/if}
              <div class="t"><b>{t.title}</b><span>{t.artist}</span>
                <span class="sub">{t.stars ? "★".repeat(t.stars) + " • " : ""}{ago(t.deleted_at)}</span></div>
              <div class="acts"><button onclick={() => keepView(null, () => model.restore(t.id))}>Restore</button>
                <button style="color:var(--bad)" onclick={() => forget(t)}>Delete</button></div>
            </li>
          {/each}
        </ol>
      </details>
    {/if}

    <Feedback />
  {:else if $error}
    <p class="error">{$error}</p>
  {/if}
</main>

<style>
  main { overflow-anchor: none; }                    /* the page keeps its own place when rows move (keepView) */
  .head { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 22px; align-items: end; padding-top: 8px; position: relative; }
  .head .backlink { position: absolute; top: 8px; left: -54px; border: 0; cursor: pointer; }
  @media (max-width: 1440px) and (min-width: 601px) { .head { padding-left: 50px; } .head .backlink { left: 0; } }
  .cover { width: 200px; height: 200px; border-radius: 6px; background: var(--raised); display: grid; place-items: center;
    overflow: hidden; font: 800 64px var(--display); color: var(--faint); box-shadow: 0 14px 40px rgba(0, 0, 0, 0.55); }
  .cover img { width: 100%; height: 100%; object-fit: cover; }
  h1 { font: 800 clamp(30px, 6vw, 56px)/1.05 var(--display); letter-spacing: -0.03em; margin: 0 0 8px; overflow-wrap: anywhere; cursor: text; }
  .desc { margin: 0 0 8px; color: var(--muted); font-size: 14px; line-height: 1.45; max-width: 60ch; white-space: pre-line; cursor: text; }
  .head .meta { font-size: 14px; color: var(--fg); opacity: .85; }
  .actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .actions .play-big { width: 44px; height: 44px; }
  .actions .play-big svg { width: 20px; height: 20px; }
  .filter { appearance: none; -webkit-appearance: none; border-radius: 999px; padding: 7px 14px; font: 600 13px var(--body);
    border: 0; cursor: pointer; background: var(--accent-soft); color: var(--accent); }
  .find { border: 0; border-radius: 999px; background: rgba(255, 255, 255, 0.07); padding: 7px 14px; font: 600 13px var(--body); color: var(--fg); }
  .find:hover:not(:disabled) { background: rgba(255, 255, 255, 0.12); }
  .find:disabled { opacity: .4; cursor: default; }
  .find.ready { background: var(--accent-soft); color: var(--accent); }
  .find.ready.on { background: var(--accent); color: #111; }       /* Find music playing in the bar */
  .tempo { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; font-size: 14px; color: var(--muted); }
  .tempo label { display: inline-flex; gap: 8px; align-items: center; cursor: pointer; }
  .tempo input[type=checkbox] { width: 16px; height: 16px; margin: 0; }
  .tempo input[type=number] { width: 72px; padding: 5px 8px; }
  .box, .job { display: grid; gap: 10px; justify-items: stretch; }
  .job { justify-items: start; }
  .lists { display: grid; gap: 24px; }
  .sortbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 10px 14px; border-radius: var(--radius);
    background: var(--accent-soft); font-size: 14px; }
  .sortbar b { color: var(--accent); }
  h2 { font: 700 20px/1.2 var(--display); margin: 0 0 8px; letter-spacing: -0.01em; }
  h2 .meta { font-weight: 400; margin-left: 8px; }
  ol { list-style: none; margin: 0; padding: 0; display: grid; }
  .empty { display: block; }
  .add-song { display: flex; gap: 8px; }
  .add-song input { flex: 1 1 auto; min-width: 0; border-radius: 999px; }
  .failures ul { margin: 8px 0 0; padding-left: 18px; }
  .deleted ol { margin-top: 8px; }
  .del-all { margin-top: 10px; }
  .del-all .btn { padding: 6px 14px; font-size: 13px; }
  .deleted li { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 12px; align-items: center;
    padding: 6px 8px; margin-inline: -8px; border-radius: 6px; cursor: pointer; color: var(--fg); font-size: 15px; }
  @media (hover: hover) { .deleted li:not(.unavailable):hover { background: rgba(255, 255, 255, 0.07); } }
  .deleted li.playing .t b { color: var(--accent); }
  .deleted li.unavailable { opacity: .45; cursor: default; }
  .deleted .art { width: 44px; height: 44px; border-radius: 4px; background: var(--raised); object-fit: cover; display: block; }
  .deleted .t .sub { color: var(--faint); font-size: 12px; }
  .deleted .acts { display: flex; gap: 6px; }
  .deleted .acts button { padding: 6px 12px; border-radius: 999px; border: 1px solid rgba(255, 255, 255, 0.28);
    background: none; font: 600 12px var(--body); color: var(--fg); }
  @media (max-width: 600px) {
    .head { grid-template-columns: 1fr; justify-items: center; gap: 18px; }
    .head .backlink { left: 0; }
    .head-text { justify-self: stretch; }
    .cover { width: min(62vw, 240px); height: min(62vw, 240px); }
    h1 { font-size: clamp(26px, 8vw, 34px); }
  }
</style>
