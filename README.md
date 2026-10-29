# music-hunter

Personal music recommender prototype. Design: [SPEC.md](SPEC.md). Tunable numbers: [config.toml](config.toml).

## Setup

```sh
uv sync
```

## Milestone 1 — CLAP references

1. Put the 5 original reference tracks in `references/`, tagged with artist and title (or named `Artist - Title.ext`).
2. Embed them (chunked, stored in `data/music_hunter.db`):

   ```sh
   uv run music-hunter embed-references          # --force to re-embed
   ```

3. Check the similarity matrix, optionally against extra test files (not stored):

   ```sh
   uv run music-hunter similarity [similar.mp3 different.mp3 ...]
   ```

## Milestones 2–3 — candidates and pool

```sh
uv run music-hunter pull            # candidates from Last.fm + YouTube Music, then fill the pool
uv run music-hunter candidates      # candidate list with sources and filter-reason counts
uv run music-hunter fill            # resume filling the pool (downloads + analysis) after an interruption
uv run music-hunter pool            # analysed pool tracks, quotas, failures
```

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

Keys: 1–5 rate · space play/pause · N skip. Ratings 3–5 play to the end; 1–2 skip straight away.
The score, sources and control status appear only after rating. The most recent rating can be changed.
If no pull has run yet (or the pool fill was interrupted), `serve` runs it in the background and
playback starts once 20 tracks are analysed.

## Milestone 6 — learning and evaluation

All of this runs inside `music-hunter serve`:

- 1 play in each block of 5 is a control (the median-scored track), revealed only after rating.
- Re-weighting runs at 30, 45, 60, 75 learning ratings, then every 15; each is evaluated on the next 15.
- The personal model joins the score from 50 learning ratings.
- When fewer than 25 unplayed tracks remain, a new generation is pulled in the background.
- Results: http://127.0.0.1:8765/results

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
