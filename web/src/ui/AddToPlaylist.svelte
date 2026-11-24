<script lang="ts">
  // Add to playlist… (like Spotify): every playlist with its cover, ticked where the song is, in Home's sections and
  // order with the current playlist's section first; tick and untick, then Done.
  import { untrack } from "svelte";
  import { api, cached, type Folder, type Playlist, type PlaylistSummary } from "../core/api";
  import { playlistModel } from "../core/library";
  import { sheet } from "./overlay";

  interface Pick { id: number; name: string; image: string | null; folder_id: number | null; has: boolean; }

  let dlg: HTMLDialogElement | undefined = $state();
  let lists = $state<Pick[] | null>(null);
  let folders = $state<Folder[]>([]);
  let ticked = $state(new Set<number>());
  let was = new Set<number>();
  let error = $state("");
  let done = false;

  $effect(() => {
    const s = $sheet;
    if (!s || !dlg) return;
    lists = null;
    error = "";
    done = false;
    if (!dlg.open) dlg.showModal();
    untrack(() => void load(s.trackId));               // (what load reads mustn't re-run this)
  });

  // Shown at once from what the device already knows (Home's playlists and each playlist's songs, kept as they were
  // last seen; feedback #119: it sometimes sat loading), then checked with the Pi; ticks I've changed meanwhile stay.
  function local(trackId: number): boolean {
    const l = cached<PlaylistSummary[]>("next:playlists"), f = cached<Folder[]>("next:folders");
    if (!l || !f) return false;
    const has = (pid: number) => !!cached<Playlist>(`next:playlist:${pid}`)?.tracks.some((t) => t.id === trackId);
    const picks = l.map((p) => ({id: p.id, name: p.name, image: p.image, folder_id: p.folder_id, has: has(p.id)}));
    was = new Set(picks.filter((p) => p.has).map((p) => p.id));
    ticked = new Set(was);
    folders = f;
    lists = picks;
    return true;
  }

  async function load(trackId: number) {
    const shown = local(trackId);
    let touched = false;
    const before = ticked;
    try {
      const [l, f] = await Promise.all([api<Pick[]>(`/api/tracks/${trackId}/playlists`), api<Folder[]>("/api/folders")]);
      if (done || $sheet?.trackId !== trackId) return;
      touched = shown && ticked !== before;            // (toggled while the Pi answered: keep my ticks)
      was = new Set(l.filter((p) => p.has).map((p) => p.id));
      if (!touched) ticked = new Set(was);
      folders = f;
      lists = l;
    } catch (e) {
      if (!shown) error = `Couldn't load the playlists: ${(e as Error).message}`;
    }
  }

  const sections = $derived.by(() => {
    const s = $sheet;
    if (!lists || !s) return [];
    const mine = new Set(folders.filter((f) => !f.builtin).map((f) => f.id));
    const sec = (fid: number | null) => (fid != null && mine.has(fid) ? fid : 0);
    const here = sec(s.folderId);
    return folders.map((f, i) => ({...f, n: f.builtin ? 1000 : i}))
      .sort((x, y) => (Number(x.id !== here) - Number(y.id !== here)) || (Number(x.position == null) - Number(y.position == null))
        || ((x.position ?? 0) - (y.position ?? 0)) || (x.n - y.n))
      .map((f) => ({...f, lists: lists!.filter((p) => sec(p.folder_id) === f.id)}))
      .filter((f) => f.lists.length);
  });

  function toggle(id: number) {
    const n = new Set(ticked);
    if (n.has(id)) n.delete(id); else n.add(id);
    ticked = n;
  }

  function finish() {
    done = true;
    dlg?.close();
  }

  async function closed() {
    const s = sheet.get();
    sheet.set(null);
    if (!done || !s || !lists) return;
    const add = [...ticked].filter((id) => !was.has(id)), drop = [...was].filter((id) => !ticked.has(id));
    if (!add.length && !drop.length) return;
    // Still ticked somewhere: a move, not a delete — it just leaves the unticked ones (#76).
    await playlistModel(s.pid).changePlaylists(s.trackId, add, drop, ticked.size > 0);
    for (const id of [...add, ...drop]) if (id !== s.pid) void playlistModel(id).load();
  }
</script>

<dialog class="sheet" bind:this={dlg} onclose={closed}>
  <h3>Add to playlist</h3>
  {#if $sheet}<div class="sheet-song">{$sheet.title} · {$sheet.artist}</div>{/if}
  <ul>
    {#if error}<li class="error">{error}</li>
    {:else if !lists}<li class="empty">Loading…</li>
    {:else}
      {#each sections as f (f.id)}
        <li class="sheet-sec">{f.name}</li>
        {#each f.lists as p (p.id)}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_to_interactive_role -->
          <li class:on={ticked.has(p.id)} role="checkbox" aria-checked={ticked.has(p.id)} tabindex="0" onclick={() => toggle(p.id)}>
            {#if p.image}<img class="pic" src={p.image} alt="">{:else}<span class="pic">{(p.name || "?")[0].toUpperCase()}</span>{/if}
            <b>{p.name}</b><span class="tick"></span>
          </li>
        {/each}
      {/each}
    {/if}
  </ul>
  <div class="row"><button class="btn" onclick={() => dlg?.close()}>Cancel</button><button class="btn primary" onclick={finish}>Done</button></div>
</dialog>

<style>
  .sheet { padding: 18px 16px; }
  .sheet-song { color: var(--muted); font-size: 13px; margin: -8px 0 10px; }
  ul { list-style: none; margin: 0; padding: 0; max-height: 60vh; overflow: auto; }
  li { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 6px;
    margin: 0; border-radius: 6px; cursor: pointer; }
  li:hover { background: rgba(255, 255, 255, 0.06); }
  li.sheet-sec, li.empty, li.error { display: block; padding: 12px 6px 4px; cursor: default; }
  li.sheet-sec { font: 700 12px var(--body); letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
  li.sheet-sec:hover, li.empty:hover, li.error:hover { background: none; }
  .pic { width: 44px; height: 44px; border-radius: 4px; background: var(--surface); object-fit: cover; display: grid;
    place-items: center; color: var(--faint); font: 800 18px var(--display); }
  b { font: 500 15px var(--body); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tick { width: 24px; height: 24px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.4); display: grid; place-items: center; }
  li.on .tick { background: var(--accent); border-color: var(--accent); }
  li.on .tick::after { content: ""; width: 6px; height: 11px; border: solid #111; border-width: 0 2px 2px 0; transform: translateY(-1px) rotate(45deg); }
</style>
