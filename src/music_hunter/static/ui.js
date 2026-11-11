// The app's own question boxes (2026-11-11), instead of the browser's plain confirm/prompt windows:
//   await ask("Delete …?", {detail, ok: "Delete", danger: true})  → true / false
//   await askText("Folder name", {value, placeholder, ok})        → the text, or null if cancelled
// Esc, Cancel or a tap outside closes it as "no".
const UI = window.UI = (() => {
  function open(build) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.className = "ask";
      d.innerHTML = build();
      document.body.appendChild(d);
      const input = d.querySelector("input");
      const done = (v) => { d.close(); d.remove(); resolve(v); };
      d.querySelector("[data-no]").onclick = () => done(input ? null : false);
      d.querySelector("form").onsubmit = (e) => { e.preventDefault(); done(input ? input.value.trim() || null : true); };
      d.addEventListener("cancel", (e) => { e.preventDefault(); done(input ? null : false); });
      d.addEventListener("click", (e) => { if (e.target === d) done(input ? null : false); });   // the backdrop
      d.showModal();
      (input || d.querySelector("[data-yes]")).focus();
      if (input) input.select();
    });
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  return {
    ask(title, {detail = "", ok = "OK", cancel = "Cancel", danger = false} = {}) {
      return open(() => `<form><h3>${esc(title)}</h3>${detail ? `<p>${esc(detail)}</p>` : ""}
        <div class="row"><button type="button" class="btn" data-no>${esc(cancel)}</button>
        <button class="btn ${danger ? "danger-fill" : "primary"}" data-yes>${esc(ok)}</button></div></form>`);
    },
    askText(title, {value = "", placeholder = "", ok = "Save", inputmode = ""} = {}) {
      return open(() => `<form><h3>${esc(title)}</h3>
        <input type="text" value="${esc(value)}" placeholder="${esc(placeholder)}" ${inputmode ? `inputmode="${inputmode}"` : ""} maxlength="200">
        <div class="row"><button type="button" class="btn" data-no>Cancel</button>
        <button class="btn primary" data-yes>${esc(ok)}</button></div></form>`);
    },
  };
})();
const ask = UI.ask, askText = UI.askText;
