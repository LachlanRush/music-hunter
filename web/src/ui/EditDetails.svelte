<script lang="ts">
  // Edit details: a playlist's name and description (its ⋯, Home's menu, or tapping the name or description).
  import { playlistModel } from "../core/library";
  import { editing } from "./overlay";

  let dlg: HTMLDialogElement | undefined = $state();
  let name = $state("");
  let description = $state("");

  $effect(() => {
    const e = $editing;
    if (!e || !dlg) return;
    name = e.name;
    description = e.description;
    if (!dlg.open) dlg.showModal();
  });

  async function closed() {
    const e = editing.get();
    editing.set(null);
    if (!e || dlg?.returnValue !== "save" || !name.trim()) return;
    await playlistModel(e.pid).details(name.trim(), description);
    homeChanged();
  }
  // Home's list follows (it reloads when told).
  const homeChanged = () => window.dispatchEvent(new Event("mh:playlists"));
</script>

<dialog bind:this={dlg} onclose={closed}>
  <h3>Edit details</h3>
  <form method="dialog">
    <input type="text" bind:value={name} aria-label="Name" placeholder="Name" maxlength="200" required>
    <textarea bind:value={description} aria-label="Description" placeholder="Add an optional description" maxlength="500"></textarea>
    <div class="row"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary" value="save">Save</button></div>
  </form>
</dialog>
