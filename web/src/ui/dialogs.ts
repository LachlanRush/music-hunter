// The app's own question boxes (as static/ui.js), instead of the browser's plain confirm and prompt windows:
//   await ask("Delete …?", {detail, ok: "Delete", danger: true})  → true / false
//   await askText("Folder name", {value, placeholder, ok})        → the text, or null if cancelled
// Esc, Cancel or a tap outside closes it as "no".

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]!));

function open<T>(html: string, value: (input: HTMLInputElement | null) => T, no: T): Promise<T> {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "ask";
    d.innerHTML = html;
    document.body.appendChild(d);
    const input = d.querySelector("input");
    const done = (v: T) => { d.close(); d.remove(); resolve(v); };
    (d.querySelector("[data-no]") as HTMLElement).onclick = () => done(no);
    d.querySelector("form")!.onsubmit = (e) => { e.preventDefault(); done(value(input)); };
    d.addEventListener("cancel", (e) => { e.preventDefault(); done(no); });
    d.showModal();
    ((input || d.querySelector("[data-yes]")) as HTMLElement).focus();
    input?.select();
  });
}

export function ask(title: string, {detail = "", ok = "OK", cancel = "Cancel", danger = false} = {}): Promise<boolean> {
  return open(`<form><h3>${esc(title)}</h3>${detail ? `<p>${esc(detail)}</p>` : ""}
    <div class="row"><button type="button" class="btn" data-no>${esc(cancel)}</button>
    <button class="btn ${danger ? "danger-fill" : "primary"}" data-yes>${esc(ok)}</button></div></form>`, () => true, false);
}

export function askText(title: string, {value = "", placeholder = "", ok = "Save", inputmode = ""} = {}): Promise<string | null> {
  return open(`<form><h3>${esc(title)}</h3>
    <input type="text" value="${esc(value)}" placeholder="${esc(placeholder)}" ${inputmode ? `inputmode="${inputmode}"` : ""} maxlength="200">
    <div class="row"><button type="button" class="btn" data-no>Cancel</button>
    <button class="btn primary" data-yes>${esc(ok)}</button></div></form>`, (i) => i?.value.trim() || null, null);
}

/** A picture from the device (a hidden file input, opened on the spot). */
export function pickPicture(): Promise<File | null> {
  return new Promise((resolve) => {
    const i = document.createElement("input");
    i.type = "file";
    i.accept = "image/*";
    i.onchange = () => resolve(i.files?.[0] ?? null);
    i.oncancel = () => resolve(null);
    i.click();
  });
}

// A tap or click outside any open box (Add to playlist, Edit details, a question) cancels and closes it.
if (typeof document !== "undefined") {
  document.addEventListener("click", (e) => {
    const d = e.target;
    if (!(d instanceof HTMLDialogElement) || !d.open) return;
    const r = d.getBoundingClientRect();
    if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return;
    if (d.classList.contains("ask")) d.dispatchEvent(new Event("cancel"));
    else d.close("cancel");
  });
}
