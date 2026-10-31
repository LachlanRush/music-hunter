#!/bin/sh
# Turns the add-on's options into the app's .env and config overrides, then starts the app. Data lives in /data.
set -e
python3 - <<'PY'
import json, pathlib
data = pathlib.Path("/data")
o = json.loads((data / "options.json").read_text()) if (data / "options.json").exists() else {}
env = {"WORKER_TOKEN": o.get("worker_token", ""), "LASTFM_API_KEY": o.get("lastfm_api_key", ""),
       "TIDAL_CLIENT_ID": o.get("tidal_client_id", ""), "TIDAL_CLIENT_SECRET": o.get("tidal_client_secret", "")}
(data / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items() if v))
(data / ".env").chmod(0o600)
entity = (o.get("wake_entity_id") or "").replace('"', "")
(data / "config.toml").write_text(f"""# Written by the add-on at every start; put your own tweaks in config.local.toml.
[ui]
host = "0.0.0.0"
extra_hosts = []

[worker]
mode = "remote"

[worker.wake]
entity_id = "{entity}"
""")
PY
exec python3 -c 'import sys; sys.argv = ["music-hunter", "serve"]; from music_hunter import main; main()'
