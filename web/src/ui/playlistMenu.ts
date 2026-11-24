// A playlist's options: the same from its ⋯ on its own screen and from Home (right-click, or hold and let go).
import { api, cached, keep, type Playlist } from "../core/api";
import { playlistModel, type PlaylistModel } from "../core/library";
import { go } from "../core/router";
import { ask, askText, pickPicture } from "./dialogs";
import { editing, menu, type MenuItem, type MenuSpec } from "./overlay";

/** Playlists being deleted: Home leaves them out even if a view from before the deletion arrives. */
export const deleting = new Set<number>();

export function playlistItems(p: Playlist, after: () => void = () => {}, removed?: () => void): MenuItem[] {
  const m = playlistModel(p.id);
  const sub = (items: MenuItem[]) => {
    const at = menu.get();
    if (at) menu.set({...at, items, key: undefined} as MenuSpec);
  };
  const items: MenuItem[] = [
    {label: "Edit details", act: () => editing.set({pid: p.id, name: p.name, description: p.description ?? ""})},
    {label: p.image ? "Change picture" : "Add picture", act: async () => {
      const f = await pickPicture();
      if (f) { await m.picture(f); after(); }
    }},
    {label: p.folder_id ? "Move to another folder" : "Move to folder", keep: true, act: () => sub(folderItems(p, m, after))},
  ];
  if (p.can_refresh) items.push({label: "Refresh from Spotify", act: async () => { await m.refresh(); after(); }});
  const app = typeof window !== "undefined" ? window.MHApp : undefined;
  if (app) {                                           // the phone app: the playlist kept on the phone, for offline
    let st: {on?: boolean; tracks?: number; stored?: number} = {};
    try { st = JSON.parse(app.offlineState(p.id)); } catch { /* off */ }
    const label = !st.on ? "Download" : (st.stored ?? 0) < (st.tracks ?? 0) ? "Downloading" : "Downloaded";
    items.push({label, off: !st.on, act: async () => {
      if (st.on && !await ask("Remove the downloads from this phone?", {detail: "It will still play while you're connected.", ok: "Remove"})) return;
      app.offlineSet(p.id, !st.on);
    }});
  }
  const synced = !!p.sync?.on;
  items.push({label: synced ? "Synced" : "Sync", off: !synced, act: async () => { await m.setSync(!synced); after(); }});
  items.push({label: "Results", act: () => go(`/playlist/${p.id}/results`)});
  if (p.others?.length) items.push({label: "Merge into…", keep: true, act: () => sub(mergeItems(p, m))});
  items.push("hr", {label: "Delete playlist", danger: true, act: async () => {
    if (!await ask(`Delete "${p.name}"?`, {ok: "Delete", danger: true, detail: "This can't be undone: the playlist, its stars, "
        + "its Find music and its Recently deleted list go, and songs in no other playlist lose their audio."})) return;
    deleting.add(p.id);                              // gone from Home at once (#115: it seemed not to work)
    removed?.();
    try { keep("next:playlists", (cached<{id: number}[]>("next:playlists") ?? []).filter((q) => q.id !== p.id)); } catch { /* */ }
    go("/");
    await m.remove_playlist();
    deleting.delete(p.id);
    after();
  }});
  return items;
}

function folderItems(p: Playlist, m: PlaylistModel, after: () => void): MenuItem[] {
  const move = (id: number | null) => async () => { await m.setFolder(id); after(); };
  return [
    {heading: "Move to folder"},
    ...(p.folders ?? []).map((f): MenuItem => ({label: `${f.id === p.folder_id ? "✓ " : ""}${f.name}`, act: move(f.id)})),
    {label: "New folder…", act: async () => {
      const name = await askText("New folder", {placeholder: "Folder name", ok: "Create"});
      if (!name) return;
      const r = await api<{folder_id: number}>("/api/folders", {name});
      await move(r.folder_id)();
    }},
    ...(p.folder_id ? ["hr", {label: "Move to Playlists", act: move(null)}] as MenuItem[] : []),
  ];
}

function mergeItems(p: Playlist, m: PlaylistModel): MenuItem[] {
  return [
    {heading: "Merge into"},
    ...(p.others ?? []).map((o): MenuItem => ({label: o.name, act: async () => {
      if (!await ask(`Merge "${p.name}" into "${o.name}"?`, {ok: "Merge", danger: true,
          detail: `Its songs join "${o.name}" (a song in both keeps the higher stars), then "${p.name}" and its Find music are deleted.`})) return;
      if (await m.merge(o.id)) go(`/playlist/${o.id}`, true);
    }})),
  ];
}
