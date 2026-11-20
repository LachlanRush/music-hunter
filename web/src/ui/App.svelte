<script lang="ts">
  // The whole app: the screen for the address, and over every screen the one player bar, full view, menu, note and
  // dialogs.
  import { route } from "../core/router";
  import AddToPlaylist from "./AddToPlaylist.svelte";
  import EditDetails from "./EditDetails.svelte";
  import FullView from "./FullView.svelte";
  import Home from "./Home.svelte";
  import Menu from "./Menu.svelte";
  import PlayerBar from "./PlayerBar.svelte";
  import PlaylistScreen from "./Playlist.svelte";
  import Results from "./Results.svelte";
  import Toast from "./Toast.svelte";
  import { fullView } from "./overlay";
</script>

{#if $route.name === "home"}
  <Home />
{:else if $route.name === "playlist"}
  {#key $route.id}<PlaylistScreen id={$route.id} />{/key}
{:else if $route.name === "results"}
  {#key $route.id}<Results id={$route.id} />{/key}
{:else}
  <main><p class="empty">Nothing here.</p></main>
{/if}

<Menu />
<Toast />
<EditDetails />
<AddToPlaylist />
<PlayerBar onopen={() => fullView.set(true)} />
{#if $fullView}<FullView onclose={() => fullView.set(false)} />{/if}
