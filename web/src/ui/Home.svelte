<script lang="ts">
  // The playlists, in their sections: my folders and the built-in Playlists (everything in no folder, where new ones
  // go). Each section opens and closes with its arrow (remembered on this device); drag a playlist into a folder, out
  // of one or to a new place, and a section by its name row; a playlist's options by right-click or a held finger.
  import { onMount } from "svelte";
  import { api, busy, cached, keep, type Folder, type Job, type Playlist, type PlaylistSummary } from "../core/api";
  import { playlistModel } from "../core/library";
  import { go, href } from "../core/router";
  import { askText, ask } from "./dialogs";
  import Feedback from "./Feedback.svelte";
  import { pressable } from "./gestures";
  import { menuAt, menuPoint, toast } from "./overlay";
  import { playlistItems } from "./playlistMenu";

  let lists = $state<PlaylistSummary[]>(cached<PlaylistSummary[]>("next:playlists") ?? []);
  let folders = $state<Folder[]>(cached<Folder[]>("next:folders") ?? []);
  let error = $state("");
  let closed = $state(readClosed());
  let link = $state("");
  let importing = $state(false);
  let main: HTMLElement | undefined = $state();

  // Dragging: a playlist to a place (section, index), or onto a section's name row; or a section to a new place.
  let cardDrag = $state<{id: number; section: number; index: number; into: number | null} | null>(null);
  let secDrag = $state<{id: number; order: number[]} | null>(null);
  let ghost = $state<{x: number; y: number; image: string | null; label: string} | null>(null);

  function readClosed(): Set<string> {
    try { return new Set((JSON.parse(localStorage.getItem("closedFolders") || "[]") as unknown[]).map(String)); }
    catch { return new Set(); }
  }
  function toggle(id: number) {
    const n = new Set(closed), k = String(id);
    if (n.has(k)) n.delete(k); else n.add(k);
    closed = n;
    try { localStorage.setItem("closedFolders", JSON.stringify([...n])); } catch { /* only a convenience */ }
  }

  let timer: ReturnType<typeof setTimeout> | undefined;
  let prefetched = false;
  async function load() {
    try {
      const [l, f] = await Promise.all([api<PlaylistSummary[]>("/api/playlists"), api<Folder[]>("/api/folders")]);
      if (cardDrag || secDrag) return;                  // not under something being dragged
      lists = l;
      folders = f;
      keep("next:playlists", l);
      keep("next:folders", f);
      error = "";
      clearTimeout(timer);
      timer = l.some((p) => busy(p.job)) ? setTimeout(load, 3000) : undefined;
      if (!prefetched) {                                // each playlist's data ahead, so opening one is instant
        prefetched = true;
        l.forEach((p, i) => setTimeout(() => void playlistModel(p.id).load(), 300 + 150 * i));
      }
    } catch (e) {
      error = `Can't reach the music-hunter server: ${(e as Error).message}`;
    }
  }

  onMount(() => {
    document.title = "Music Hunter";
    void load();
    const poll = setInterval(() => { if (!document.hidden && !timer) void load(); }, 10000);   // imports started elsewhere
    const changed = () => void load();
    window.addEventListener("mh:playlists", changed);
    const stop = pressable(main!, {
      pick: (el) => (el.closest(".folder-more") ? null : (el.closest("[data-card]") || el.closest("[data-head]")) as HTMLElement | null),
      canDrag: () => true,
      menu: (el, x, y) => { if (el.dataset.card) void openMenu(Number(el.dataset.card), x, y); },
      start: (el, x, y) => {
        if (el.dataset.card) {
          const id = Number(el.dataset.card), p = lists.find((q) => q.id === id)!;
          const sec = sections.find((s) => s.lists.some((q) => q.id === id))!;
          cardDrag = {id, section: sec.id, index: sec.lists.findIndex((q) => q.id === id), into: null};
          ghost = {x, y, image: p.image, label: p.name};
        } else {
          const id = Number(el.dataset.head);
          secDrag = {id, order: sections.map((s) => s.id)};
          ghost = {x, y, image: null, label: sections.find((s) => s.id === id)?.name ?? ""};
        }
      },
      move: (x, y) => {
        if (ghost) ghost = {...ghost, x, y};
        const el = document.elementFromPoint(x, y) as HTMLElement | null;
        if (!el) return;
        if (secDrag) {
          const over = el.closest("[data-section]") as HTMLElement | null;
          const sid = over && Number(over.dataset.section);
          if (over == null || sid == null || sid === secDrag.id) return;
          const r = over.getBoundingClientRect(), order = secDrag.order.filter((s) => s !== secDrag!.id);
          order.splice(order.indexOf(sid) + (y > r.top + r.height / 2 ? 1 : 0), 0, secDrag.id);
          secDrag = {...secDrag, order};
          return;
        }
        if (!cardDrag) return;
        const head = el.closest("[data-head]") as HTMLElement | null;
        if (head) { cardDrag = {...cardDrag, into: Number(head.dataset.head)}; return; }   // into it, at its end
        const over = el.closest("[data-card]") as HTMLElement | null;
        if (over && Number(over.dataset.card) !== cardDrag.id) {                            // beside another playlist
          const sid = Number((over.closest("[data-section]") as HTMLElement).dataset.section);
          const base = rest(sid), i = base.findIndex((q) => q.id === Number(over.dataset.card));
          const r = over.getBoundingClientRect();
          cardDrag = {...cardDrag, section: sid, index: i + (x > r.left + r.width / 2 ? 1 : 0), into: null};
          return;
        }
        const grid = el.closest("[data-grid]") as HTMLElement | null;                       // empty space: its end
        if (grid) {
          const sid = Number(grid.dataset.grid);
          if (sid !== cardDrag.section) cardDrag = {...cardDrag, section: sid, index: rest(sid).length, into: null};
        } else if (cardDrag.into != null) cardDrag = {...cardDrag, into: null};
      },
      end: () => void finish(),
      cancel: () => { cardDrag = null; secDrag = null; ghost = null; void load(); },
    });
    return () => { stop(); clearInterval(poll); clearTimeout(timer); window.removeEventListener("mh:playlists", changed); };
  });

  // The sections in my order, each with its playlists (a dragged one shown where it would go).
  const base = $derived.by(() => {
    const mine = new Set(folders.filter((f) => !f.builtin).map((f) => f.id));
    return folders
      .map((f, i) => ({...f, n: f.builtin ? 1000 : i}))
      .sort((x, y) => (Number(x.position == null) - Number(y.position == null)) || ((x.position ?? 0) - (y.position ?? 0)) || (x.n - y.n))
      .map((f) => ({...f, lists: lists.filter((p) => (f.builtin ? !mine.has(p.folder_id ?? -1) : p.folder_id === f.id))}));
  });
  const rest = (sid: number) => (base.find((s) => s.id === sid)?.lists ?? []).filter((p) => p.id !== cardDrag?.id);
  const sections = $derived.by(() => {
    let out = base;
    if (secDrag) {
      const at = new Map(secDrag.order.map((id, i) => [id, i]));
      out = [...out].sort((a, b) => at.get(a.id)! - at.get(b.id)!);
    }
    const d = cardDrag;
    if (d && d.into == null) {
      const card = lists.find((p) => p.id === d.id)!;
      out = out.map((s) => {
        const l = s.lists.filter((p) => p.id !== d.id);
        if (s.id === d.section) l.splice(Math.min(d.index, l.length), 0, card);
        return {...s, lists: l};
      });
    }
    return out;
  });

  async function finish() {
    const c = cardDrag, sd = secDrag;
    ghost = null;
    let order = sections;
    if (c && c.into != null) {                       // dropped on a section's name row: at its end
      const card = lists.find((p) => p.id === c.id)!;
      order = sections.map((s) => ({...s, lists: [...s.lists.filter((p) => p.id !== c.id), ...(s.id === c.into ? [card] : [])]}));
    }
    // The whole page's order, sections and playlists together (a section's playlists after it).
    const items: {kind: string; id?: number}[] = [];
    for (const s of order) {
      items.push(s.builtin ? {kind: "loose"} : {kind: "folder", id: s.id});
      for (const p of s.lists) items.push({kind: "playlist", id: p.id});
    }
    const target = c ? (c.into ?? order.find((s) => s.lists.some((p) => p.id === c.id))?.id ?? 0) : null;
    // Shown in its new place at once; the Pi follows.
    if (c) lists = lists.map((p) => (p.id === c.id ? {...p, folder_id: target || null} : p));
    const ranked = new Map(items.filter((i) => i.kind === "playlist").map((i, k) => [i.id!, k]));
    lists = [...lists].sort((a, b) => (ranked.get(a.id) ?? 0) - (ranked.get(b.id) ?? 0));
    if (sd) {
      const at = new Map(sd.order.map((id, i) => [id, i]));
      folders = folders.map((f) => ({...f, position: at.get(f.id) ?? f.position}));
    }
    cardDrag = null;
    secDrag = null;
    try {
      if (c && target !== (base.find((s) => s.lists.some((p) => p.id === c.id))?.id ?? 0)) {
        await api(`/api/playlists/${c.id}/folder`, {folder_id: target || null}, "PUT");
      }
      await api("/api/home-order", {items}, "PUT");
    } catch (e) {
      error = `Couldn't move it: ${(e as Error).message}`;
    }
    void load();
  }

  async function openMenu(id: number, x: number, y: number) {
    const m = playlistModel(id);
    let p: Playlist | null = m.data.get();
    if (!p) { await m.load(); p = m.data.get(); }
    if (!p) return;
    menuPoint(x, y, [{heading: p.name}, ...playlistItems(p, () => {
      const e = m.error.get();
      if (e) toast(e);
      void load();
    })]);
    void m.load();                                    // fresh for next time
  }

  function folderMenu(el: HTMLElement, f: Folder) {
    menuAt(el, [
      {label: "Rename", act: async () => {
        const n = await askText("Rename folder", {value: f.name, ok: "Rename"});
        if (!n) return;
        try { await api(`/api/folders/${f.id}`, {name: n}, "PATCH"); } catch (e) { error = (e as Error).message; }
        void load();
      }},
      {label: "Delete folder", danger: true, act: async () => {
        if (!await ask(`Delete the folder "${f.name}"?`, {detail: "Its playlists stay; they move to Playlists.", ok: "Delete", danger: true})) return;
        try { await api(`/api/folders/${f.id}`, undefined, "DELETE"); } catch (e) { error = (e as Error).message; }
        void load();
      }},
    ], false);
  }

  // A folder's songs and their length, counting a song once however many of its playlists have it.
  const totalLine = (f: Folder) => {
    const mins = Math.round((f.duration_s || 0) / 60), k = f.tracks || 0;
    return `${k} track${k === 1 ? "" : "s"} · ${mins >= 60 ? `${Math.floor(mins / 60)} hr ${mins % 60} min` : `${mins} min`}`;
  };
  const jobPct = (j: Job) => (j.progress?.total ? Math.round(100 * (j.progress.done ?? 0) / j.progress.total) : 0);

  async function importLink(e: SubmitEvent) {
    e.preventDefault();
    error = "";
    importing = true;
    try {
      const r = await api<{playlist_id: number}>("/api/playlists", {spotify_url: link.trim()});
      link = "";
      go(`/playlist/${r.playlist_id}`);
    } catch (err) {
      error = `Couldn't import: ${(err as Error).message}`;
    }
    importing = false;
  }
  async function newPlaylist() {
    const name = await askText("New playlist", {placeholder: "Playlist name", ok: "Create"});
    if (!name) return;
    try { go(`/playlist/${(await api<{playlist_id: number}>("/api/playlists", {name})).playlist_id}`); }
    catch (e) { error = `Couldn't create it: ${(e as Error).message}`; }
  }
  async function newFolder() {
    const name = await askText("New folder", {placeholder: "Folder name", ok: "Create"});
    if (!name) return;
    try { await api("/api/folders", {name}); void load(); } catch (e) { error = (e as Error).message; }
  }
</script>

<main bind:this={main} class:dragging-card={!!cardDrag || !!secDrag}>
  <section class="home" aria-label="Your playlists and folders">
    {#each sections as f (f.id)}
      <div class="folder" class:closed={closed.has(String(f.id))} class:dragging={secDrag?.id === f.id}
           class:drop-here={cardDrag?.into === f.id} data-section={f.id}>
        <div class="folder-head" data-head={f.id}>
          <span class="folder-toggle" role="button" tabindex="0" aria-expanded={!closed.has(String(f.id))}
                onclick={() => toggle(f.id)} onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(f.id); } }}>
            <svg class="chev" viewBox="0 0 24 24"><path d="M7 9.5l5 5 5-5z"/></svg><b>{f.name}</b>
            {#if !f.builtin}<span class="meta">{totalLine(f)}</span>{/if}</span>
          {#if !f.builtin}
            <div class="folder-more"><button class="icon-btn" aria-label={`More for ${f.name}`} onclick={(e) => folderMenu(e.currentTarget, f)}>⋯</button></div>
          {/if}
        </div>
        <div class="grid" data-grid={f.id}>
          {#each f.lists as p (p.id)}
            <a class="card" class:dragging={cardDrag?.id === p.id} href={href(`/playlist/${p.id}`)} data-card={p.id} draggable="false"
               onclick={(e) => { e.preventDefault(); go(`/playlist/${p.id}`); }}>
              <div class="cover">{#if p.image}<img src={p.image} alt="" draggable="false">{:else}{(p.name || "?")[0].toUpperCase()}{/if}</div>
              <b>{p.name}</b>
              {#if p.description}<div class="desc">{p.description}</div>{/if}
              {#if p.job && busy(p.job)}
                <div class="bar live" role="progressbar" aria-valuenow={jobPct(p.job)} aria-valuemin={0} aria-valuemax={100}><i style:width={`${jobPct(p.job)}%`}></i></div>
                <div class="meta">{p.job.state === "queued" ? "Waiting to import…" : `Importing ${p.job.progress?.done ?? 0} of ${p.job.progress?.total || "?"}`}</div>
              {/if}
            </a>
          {/each}
        </div>
      </div>
    {:else}
      {#if !error}<div class="empty">Loading…</div>{/if}
    {/each}
  </section>
  <section class="add" aria-label="Add a playlist">
    <form onsubmit={importLink}>
      <input type="url" bind:value={link} placeholder="Spotify playlist link — https://open.spotify.com/playlist/…" aria-label="Spotify playlist link" required>
      <button type="submit" class="btn" disabled={importing}>{importing ? "Reading…" : "Import"}</button>
    </form>
    <div class="new-row"><button class="btn" onclick={newPlaylist}>New playlist</button><button class="btn" onclick={newFolder}>New folder</button></div>
    {#if error}<div class="error">{error}</div>{/if}
  </section>
  <Feedback />
</main>

{#if ghost}
  <div class="ghost" class:folder-ghost={!!secDrag} style:left={`${ghost.x}px`} style:top={`${ghost.y}px`}>
    {#if secDrag}<b>{ghost.label}</b>{:else if ghost.image}<img src={ghost.image} alt="">{:else}<div>{ghost.label[0]?.toUpperCase() ?? "?"}</div>{/if}
  </div>
{/if}

<style>
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; padding: 6px 0 18px; }
  .card { display: grid; gap: 4px; align-content: start; text-decoration: none; color: inherit; min-width: 0;
    -webkit-touch-callout: none; user-select: none; -webkit-user-select: none; }
  .card:hover .cover { filter: brightness(1.08); }
  .cover { aspect-ratio: 1; border-radius: 6px; background: var(--raised); display: grid; place-items: center;
    overflow: hidden; font: 800 42px var(--display); color: var(--faint); margin-bottom: 8px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45); }
  .cover img { width: 100%; height: 100%; object-fit: cover; }
  .card b { font: 700 15px/1.3 var(--display); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card .desc { font-size: 12px; line-height: 1.35; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
    -webkit-box-orient: vertical; overflow: hidden; }
  .card .bar { margin-top: 6px; }
  .card.dragging { opacity: .25; }
  .card:global(.held) { transform: scale(.96); transition: transform .15s; }
  @media (max-width: 600px) {
    .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .card { gap: 2px; }
    .cover { margin-bottom: 6px; font-size: 28px; box-shadow: 0 6px 16px rgba(0, 0, 0, 0.4); }
    .card b { font-size: 13px; }
    .card .meta { font-size: 11px; }
  }
  .home { display: grid; gap: 6px; }
  .folder-head { display: flex; align-items: center; gap: 6px; padding: 6px 0; cursor: grab; user-select: none;
    -webkit-user-select: none; -webkit-touch-callout: none; border-radius: 6px; }
  .folder-toggle { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; min-width: 0; }
  .chev { width: 20px; height: 20px; fill: var(--muted); transition: transform .2s; flex: none; }
  .folder.closed .chev { transform: rotate(-90deg); }
  .folder-toggle b { font: 700 16px/1.3 var(--display); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .folder-toggle .meta { margin-left: 4px; }
  .folder-toggle:hover b { text-decoration: underline; text-underline-offset: 3px; }
  .folder-more { margin-left: 2px; }
  .folder-more .icon-btn { width: 32px; height: 32px; }
  .folder.closed .grid { display: none; }
  .folder.dragging { opacity: .3; }
  .dragging-card .folder:not(.closed) .grid:empty { display: block; min-height: 80px; border: 2px dashed rgba(255, 255, 255, 0.18); border-radius: 8px; margin-bottom: 12px; }
  .folder.drop-here > .folder-head { background: var(--accent-soft); outline: 2px solid rgba(255, 255, 255, 0.35); outline-offset: -2px; }
  .dragging-card { cursor: grabbing; }
  .ghost { position: fixed; z-index: 50; pointer-events: none; width: 120px; border-radius: 6px; overflow: hidden;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6); transform: translate(-50%, -60%) scale(1.04); opacity: .95; }
  .ghost img, .ghost div { width: 120px; height: 120px; object-fit: cover; display: grid; place-items: center;
    background: var(--raised); font: 800 36px var(--display); color: var(--faint); }
  .ghost.folder-ghost { width: auto; display: flex; align-items: center; gap: 10px; padding: 10px 16px; background: var(--raised);
    font: 700 15px var(--display); transform: translate(-50%, -50%); }
  .add { display: grid; gap: 12px; padding-top: 12px; }
  form { display: flex; flex-wrap: wrap; gap: 8px; }
  form input { flex: 1 1 260px; min-width: 0; }
  .add .btn { padding: 9px 18px; }
  .new-row { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
