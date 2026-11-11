"""Previewing UI changes without a release: `music-hunter dev`.

A small server on the PC that serves the pages, scripts and styles from this working tree and passes every other
request (data, audio, covers, stars, ratings) through to the app on the Pi. So the pages show my real playlists and
play real audio, while their code is whatever is on disk here. Every page gets a small script that reloads it when a
file changes (styles are swapped in place, so a style change doesn't stop the music).

Only the pages' code is local: a change to the Python app (a new endpoint, different data) still needs the add-on
updated. Everything done on a dev page (stars, ratings, removals) happens on the Pi, as in the real app.
"""

import re
from contextlib import asynccontextmanager
from pathlib import Path

from .config import CONFIG

STATIC = Path(__file__).parent / "static"
NATIVE_AUDIO = Path(__file__).resolve().parents[2] / "android/app/src/main/assets/native-audio.js"

# The app's HTML routes (web.py) and the file each serves. The Pi is asked first, so its redirects still apply
# (e.g. Find music not started yet goes back to the playlist).
PAGES = [
    (re.compile(r"^/$"), "home.html"),
    (re.compile(r"^/playlist/\d+$"), "playlist.html"),
    (re.compile(r"^/playlist/\d+/results$"), "overview.html"),
    (re.compile(r"^/find/\d+$"), "index.html"),
    (re.compile(r"^/listen$"), "index.html"),
]
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host"}

RELOAD_JS = """(() => {
  // Dev preview (music-hunter dev): reload when a page file changes; swap styles in place.
  const badge = document.createElement("div");
  badge.textContent = "DEV";
  badge.style.cssText = "position:fixed;top:4px;right:4px;z-index:99999;font:600 9px/1 system-ui;letter-spacing:.08em;"
    + "padding:3px 5px;border-radius:4px;background:#c2410c;color:#fff;opacity:.7;pointer-events:none";
  const add = () => document.body.appendChild(badge);
  document.body ? add() : document.addEventListener("DOMContentLoaded", add);
  let seen = null;
  async function poll() {
    try {
      const files = await (await fetch("/__dev/files", {cache: "no-store"})).json();
      if (seen) {
        const changed = Object.keys(files).filter(f => files[f] !== seen[f]);
        if (changed.length && changed.every(f => f.endsWith(".css"))) {
          for (const l of document.querySelectorAll('link[rel="stylesheet"]')) {
            const u = new URL(l.href, location.href);
            if (u.origin !== location.origin) continue;
            u.searchParams.set("dev", Date.now());
            l.href = u.toString();
          }
        } else if (changed.length) {
          location.reload();
          return;
        }
      }
      seen = files;
    } catch (e) { /* server restarting: try again */ }
    setTimeout(poll, POLL_MS);
  }
  poll();
})();
"""


def _files() -> dict:
    out = {p.name: p.stat().st_mtime_ns for p in STATIC.iterdir() if p.is_file()}
    if NATIVE_AUDIO.exists():
        out["native-audio.js"] = NATIVE_AUDIO.stat().st_mtime_ns
    return out


def make_app(upstream: str):
    import httpx
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    poll_ms = CONFIG["dev"]["poll_ms"]
    client = httpx.AsyncClient(base_url=upstream, timeout=httpx.Timeout(None, connect=5.0))

    def page(name: str) -> HTMLResponse:
        html = (STATIC / name).read_text()
        tag = '<script src="/__dev/reload.js"></script>'
        html = html.replace("</body>", tag + "</body>", 1) if "</body>" in html else html + tag
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def files(request):
        return JSONResponse(_files(), headers={"Cache-Control": "no-store"})

    async def reload_js(request):
        return Response(RELOAD_JS.replace("POLL_MS", str(poll_ms)), media_type="text/javascript",
                        headers={"Cache-Control": "no-store"})

    async def native_audio(request):
        # The Android dev build loads its injected script from here, so the app's own parts (the now playing bar)
        # change without reinstalling it.
        return FileResponse(NATIVE_AUDIO, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    async def proxy(request):
        path = request.url.path
        if path.startswith("/static/"):
            f = STATIC / path.removeprefix("/static/")
            if f.is_file() and STATIC in f.resolve().parents:
                return FileResponse(f, headers={"Cache-Control": "no-store"})
        local = next((name for rx, name in PAGES if rx.match(path)), None) if request.method == "GET" else None

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        url = path + (f"?{request.url.query}" if request.url.query else "")
        body = await request.body() if request.method not in ("GET", "HEAD") else None
        req = client.build_request(request.method, url, headers=headers, content=body)
        try:
            r = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            if local:                                    # Pi unreachable: the page still shows (its data won't)
                return page(local)
            return Response(f"Pi unreachable: {e}", status_code=502)
        if local and r.status_code == 200 and r.headers.get("content-type", "").startswith("text/html"):
            await r.aclose()
            return page(local)
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
        if "location" in out:
            out["location"] = out["location"].replace(upstream, "")
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out,
                                 background=BackgroundTask(r.aclose))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await client.aclose()

    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    return Starlette(lifespan=lifespan, routes=[
        Route("/__dev/files", files),
        Route("/__dev/reload.js", reload_js),
        Route("/__dev/native-audio.js", native_audio),
        Route("/{path:path}", proxy, methods=methods),
    ])


def serve(upstream: str | None = None) -> None:
    """Listens on the same addresses as the app ([ui] host and extra_hosts, so the phone can reach it over
    Tailscale), on [dev] port."""
    import socket

    import uvicorn

    upstream = (upstream or CONFIG["worker"]["server"]).rstrip("/")
    port = CONFIG["dev"]["port"]
    sockets = []
    for host in [CONFIG["ui"]["host"], *CONFIG["ui"].get("extra_hosts", [])]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as e:
            print(f"Can't listen on {host}:{port} ({e}); skipping it")
            sock.close()
            continue
        sockets.append(sock)
        print(f"Dev preview: http://{host}:{port}")
    print(f"Pages from {STATIC}; everything else from {upstream}")
    uvicorn.Server(uvicorn.Config(make_app(upstream), log_level="warning", timeout_keep_alive=75)).run(sockets=sockets)
