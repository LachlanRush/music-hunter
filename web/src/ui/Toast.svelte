<script lang="ts">
  // A short note ("Added to queue: …"), above the player bar.
  import { toastText } from "./overlay";
</script>

<div class="toast" class:show={!!$toastText} role="status">{$toastText?.text ?? ""}</div>

<style>
  .toast { position: fixed; left: 50%; bottom: 120px; transform: translate(-50%, 20px); z-index: 30; opacity: 0;
    background: var(--fg); color: #111; font: 600 13px var(--body); padding: 8px 14px; border-radius: 999px;
    pointer-events: none; transition: opacity .2s, transform .2s; max-width: calc(100vw - 32px); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
</style>
