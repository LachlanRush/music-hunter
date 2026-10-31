# Music Hunter

Playlists with stars, and Find music per playlist. This add-on runs the web app, the database and the audio
library on the Pi. Downloads and analysis happen on the PC: the add-on wakes it through your Wake-on-LAN switch,
and the PC worker sends the results back.

## Options

- **worker_token**: the same value as `WORKER_TOKEN` in the PC's `.env`.
- **lastfm_api_key**, **tidal_client_id**, **tidal_client_secret**: as in the PC's `.env` (TIDAL is optional).
- **wake_entity_id**: the switch that wakes the PC, e.g. `switch.my_pc`. Leave it empty to let the add-on find it
  (a switch whose name or ID mentions wake, WoL or LAN); the one it used shows at `/api/worker`.

## Opening it

`http://<Home Assistant's address>:8765`, e.g. over Tailscale `http://100.90.89.29:8765`, or the "Open web UI"
button on this page.

## Data

Everything lives in the add-on's own data folder (`/data`): database, Find music sessions, the audio library.
Settings come from the project's `config.toml`; put your own changes in `/data/config.local.toml`.
