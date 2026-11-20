<script lang="ts">
  // A note for later review, at the bottom of every screen; the screen and the song playing go with it.
  import { build, player } from "../app";

  let open = $state(false);
  let text = $state("");
  let sending = $state(false);
  let failed = $state("");

  async function send() {
    if (!text.trim()) return;
    sending = true;
    const s = player.state.get();
    const context = s.track ? {playlist_id: s.source?.pid, track_id: s.track.id, position_s: Math.round(s.t), app: "next", build} : {app: "next", build};
    try {
      const r = await fetch("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text, page: location.pathname, context})});
      if (!r.ok) throw new Error(r.statusText);
      text = "";
      failed = "";
      open = false;
    } catch (e) {
      failed = ` — couldn't send (${(e as Error).message})`;
    }
    sending = false;
  }
</script>

<details class="feedback" bind:open>
  <summary>Feedback{failed}</summary>
  <textarea rows="6" placeholder="What's working, what isn't…" aria-label="Feedback" bind:value={text}></textarea>
  <button type="button" class="feedback-send" disabled={sending} onclick={send}>Send</button>
</details>
