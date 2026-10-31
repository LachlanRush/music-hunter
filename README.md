# music-hunter

Personal music recommender prototype. Design: [SPEC.md](SPEC.md). Tunable numbers: [config.toml](config.toml).

## Setup

```sh
uv sync
```

## Seeds (v2)

```sh
uv run music-hunter add-seeds <Spotify track or playlist links>   # or --file links.txt
uv run music-hunter embed-seeds                                   # audio files you put in seeds/ yourself
uv run music-hunter similarity [extra.mp3 ...]                    # how the seeds relate
uv run music-hunter reembed                                       # after changing [embedding] model: re-analyse
```

Spotify links are read from public pages (no account), matched on YouTube Music (same song, ±5 s),
downloaded into `seeds/` and analysed. v1's data is archived in `runs/v1/`.

## v5: playlists

```sh
uv run music-hunter serve           # then open http://127.0.0.1:8765 (or the PC's Tailscale address)
```

Home page: your playlists; import one from a Spotify link or create an empty one. In a playlist: star tracks (starred
tracks are the seeds), remove them, add them to another playlist, play or shuffle.

**Find music** (on a playlist's page, once a track is starred): the first click confirms and starts finding tracks
from that playlist's seeds, with a progress bar; once ready, clicking it opens the listening interface for that
playlist (Off · Close · Add, as in v4). Each playlist has its own session (`data/sessions/playlist-<id>.db`). Added
tracks appear at the top of the playlist under "Added — waiting for stars" and are pulled for once; star them to keep
them as seeds. Seed shares follow stars × how well each seed's suggestions are rated.

```sh
uv run music-hunter --playlist 1 veto     # any command on playlist 1's session (pool, scores, report, end-run, pull…)
```

## v4: veto and random picks

```sh
uv run music-hunter veto            # what the veto allows and blocks, and why
uv run music-hunter end-run         # end the run now and unseal the results
```

Every analysed track is scored on closeness to the seeds, closeness to your likes and number of sources; tracks
far below the run's median are vetoed (audio dropped, fingerprint kept) and picks are random from the rest. Now and
then a vetoed track is slipped in to test the veto. Settings: `[veto]` in config.toml.

## Candidates and pool

```sh
uv run music-hunter pull            # candidates from every source, then fill the pool
uv run music-hunter candidates      # candidate list (most source families first), sources, overlap counts
uv run music-hunter fill            # resume filling the pool (downloads + analysis) after an interruption
uv run music-hunter pool            # analysed pool tracks, quotas, failures
```

Sources (v3): Last.fm artist similarity, YouTube Music radio and "You might also like", Deezer artist radio, and
TIDAL similar tracks (needs `TIDAL_CLIENT_ID` / `TIDAL_CLIENT_SECRET` in `.env`; skipped if missing). About 100
suggestions per source per seed. Candidates suggested by two or more source families go into the pool first;
YouTube matching happens when a candidate is selected. `pull --no-tidal` / `--no-deezer` / `--no-lastfm` skip one.

`pull --fresh` forgets earlier pulls and pool tracks (downloaded files in `cache/` stay).
Files are kept as downloaded; loudness normalisation is a per-track playback gain stored in the DB.

## Milestone 4 — scoring and ratings

```sh
uv run music-hunter scores          # pool ranked by current score, with component breakdown
uv run music-hunter averages        # seed/source averages, reference set sizes
uv run pytest                       # scripted ratings check (runs on a temporary copy of the DB)
```

## Milestone 5 — player

```sh
uv run music-hunter serve           # then open http://127.0.0.1:8765
```

Buttons (v3): Off · Close · Add (labels in `[ratings]`) · keys: space play/pause · N skip · B back.
Close and Add play to the end; Off skips straight away. Only Add saves the track (to `collection/`) and makes it a seed.

Audio analysis (v3): MAEST (`[embedding]`), on the GPU. Scoring works within each seed's pool: agreement, source
average and similarity to the seed are compared among the tracks that seed suggested, and seed averages steer both
pool shares and scores.
Nothing about scores, sources or controls is ever shown in the player; artist, title and artwork appear after rating.
Tempo: set a target BPM (empty = median of seeds and liked tracks); "Match tempo" plays tracks within ±12% at that
tempo with pitch kept. The target also steers ranking (`bpm_match`). `music-hunter analyse-bpm` fills in missing BPMs.
Tempo is locked during a run (`[tempo]` in config.toml: target BPM, Match tempo); set `locked = false` to use the
player's controls. While a track plays, the likely next track is fetched into memory so it starts at once.
Back returns to the previous track: change its rating there (or rate it, if you skipped it); N returns
to the current track where you left it.
The most recent rating can be changed.
If no pull has run yet (or the pool fill was interrupted), `serve` runs it in the background and
playback starts once 20 tracks are analysed.

## Learning and evaluation

All of this runs inside `music-hunter serve`:

- Controls (v3): each play is a random pool track with a probability the run draws for itself (1 in 4 to 1 in 10),
  stores and never shows. The run ends by itself after 25 rated controls; until then the results page and `report`
  are sealed (only the number of ratings). Control ratings count for learning, but a control never becomes a seed.
- Re-weighting: from 20 ratings, every 5 — small steps (≤0.05 per weight) within guardrails (no weight above 0.35),
  choosing between the search's optimum and every earlier weight set by how well each fits the whole rating history.
- The personal model joins the score from 20 ratings (with at least 3 Offs and 3 Close/Add).
- When fewer than 100 unplayed tracks remain, or fewer than 5 multi-source ones, a new generation is pulled in the
  background.
- Results: http://127.0.0.1:8765/results — after the run: ranked vs control (average and Add rate, with 95% ranges),
  whether higher-ranked random controls were rated higher, ratings by number of sources, weight changes, sources,
  seeds and tempo.

`uv run pytest` includes a simulated ~110-play session (offline, on a temporary copy of the DB).

## Monitoring

```sh
uv run music-hunter report          # sessions, delays, pipeline timings, errors, disk use
```

- `data/music-hunter.log` — rotating text log: every event below, background job progress, tracebacks.
- `events` table in `data/music_hunter.db` — the same events, queryable: `shown` (scoring time),
  `rating_changed`, `reweighted`, `pull_start/done` (API calls, errors, time per source), `fill_start/done`,
  `track_ready/failed` (download, analysis and wait times, file size), `job_start/done/error`,
  `server_start/stop`, and from the player `player_playing` (start latency), `player_stall`,
  `player_audio_error`, `player_ended`, `player_page_error`.
- `plays`, `generations` (stats JSON), `reweightings` hold the experiment's own logs (SPEC.md §10).
