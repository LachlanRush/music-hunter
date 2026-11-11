// A note for later review, at the bottom of every page: what you were looking at goes with it (the page, and in
// Find music the track playing). Stored in the library database (`music-hunter pi feedback`).
(() => {
  const box = document.createElement("details");
  box.className = "feedback";
  box.innerHTML = `<summary>Feedback</summary>
    <textarea rows="6" placeholder="What's working, what isn't…" aria-label="Feedback"></textarea>
    <button type="button" class="feedback-send">Send</button>`;
  (document.querySelector("main") || document.body).appendChild(box);
  const text = box.querySelector("textarea"), send = box.querySelector("button");
  send.addEventListener("click", async () => {
    if (!text.value.trim()) return;
    send.disabled = true;
    try {
      const context = typeof window.feedbackContext === "function" ? window.feedbackContext() : {};
      const r = await fetch("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text: text.value, page: location.pathname, context})});
      if (!r.ok) throw new Error(r.statusText);
      text.value = "";
      box.open = false;                                  // sent: fold it away
    } catch (e) { box.querySelector("summary").textContent = `Feedback — couldn't send (${e.message})`; }
    send.disabled = false;
  });
})();
