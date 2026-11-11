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
# The app exits to restart (after data is uploaded from the PC, or on request); loop so it comes straight back,
# unpacking an uploaded bundle first.
APP=""
trap 'if [ -n "$APP" ]; then kill -TERM "$APP"; wait "$APP"; fi; exit 0' TERM INT
# Code pushed from the PC (`music-hunter pi push`) runs instead of the release until `pi revert` or a new add-on
# version; if it fails to start, it's set aside and the release runs.
export MUSIC_HUNTER_RELEASE=/app
while true; do
  python3 -c 'import pathlib; from music_hunter.transfer import unpack_incoming; unpack_incoming(pathlib.Path("/data"))'
  SRC=$(python3 -c 'import pathlib; from music_hunter.transfer import prepare_code; print(prepare_code(pathlib.Path("/data"), pathlib.Path("/app")))' || echo /app/src)
  STARTED=$(date +%s)
  PYTHONPATH="$SRC" python3 -c 'import sys; sys.argv = ["music-hunter", "serve"]; from music_hunter import main; main()' &
  APP=$!
  if wait "$APP"; then STATUS=0; else STATUS=$?; fi
  APP=""
  if [ "$STATUS" -ne 0 ]; then
    if [ "$SRC" != "/app/src" ] && [ $(( $(date +%s) - STARTED )) -lt 60 ]; then
      python3 -c 'import pathlib; from music_hunter.transfer import code_failed; code_failed(pathlib.Path("/data"))' || true
    else
      sleep 5
    fi
  fi
done
