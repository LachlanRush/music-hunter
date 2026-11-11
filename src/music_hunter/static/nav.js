// Moving to another page (2026-11-11): in a browser the pages run inside one shell page (static/shell.js), which keeps
// the playing page alive while others are shown; elsewhere (the phone app, or no shell) it's a normal page load.
function go(url) {
  try {
    if (window.parent !== window && window.parent.MHShell) { window.parent.MHShell.open(url); return; }
  } catch (e) { /* not our shell */ }
  location.href = url;
}
