# Music Recommender Prototype — Specification

Personal, single-user prototype to test one idea: **can a recommender that combines external suggestions (Last.fm, YouTube Music) with audio-similarity ranking and my 1–5 ratings find music I like better than the sources alone, and improve as I rate?**

Everything runs locally on one home PC. Personal use only.

---

## 0. Environment (confirmed 2026-10-28)

| Item | Value |
|---|---|
| OS | CachyOS Linux (kernel 7.2.10-1-cachyos) |
| GPU | NVIDIA GeForce RTX 5060 Ti (16 GB VRAM), driver 615.78, CUDA 13.4 |
| RAM | 32 GB |
| Python | 3.12, uv-managed project venv (system Python 3.14 not used) |
| Last.fm API key | set — stored in git-ignored `.env` as `LASTFM_API_KEY`, never in this file |
| ffmpeg | n9.0.2 (system) |
| yt-dlp | project venv dependency (update with `uv lock --upgrade-package yt-dlp && uv sync`); JavaScript runtime: Node v25.9.0 (system) |
| CLAP checkpoint | `laion/larger_clap_music` (Hugging Face `transformers` ClapModel; 10 s clips at 48 kHz) |

Suggested stack (implementer may adjust): Python backend, SQLite database, minimal web UI served on localhost. All tunable numbers below live in one config file.

---

## 1. Scope

**In scope**
- One playlist / one genre at a time.
- Candidate sources: Last.fm artist similarity (`artist.getSimilar` → `artist.getTopTracks`, see §4.1) and YouTube Music radio (via `ytmusicapi`).
- Full-track download with yt-dlp, loudness-normalised playback.
- Audio analysis with CLAP (music checkpoint).
- Scoring, 1–5 ratings, seed and source weighting with decay, automatic re-weighting.
- Blind control plays for evaluation.
- Full logging and a results view.
- Localhost UI: play current track, rate it, move to next.

**Out of scope (do not build)**
- Raspberry Pi, Wake-on-LAN, phone access.
- Multiple playlists.
- Spotify, Discogs, ListenBrainz, LLM sources. (ListenBrainz re-tested 2026-10-28: 4 of 5 seeds aren't in MusicBrainz, and the fifth has no similar recordings.)
- Last.fm `track.getSimilar`, and any fallback chains between Last.fm methods — each source is tested as-is. (Changed 2026-10-28: `track.getSimilar` returns nothing for tracks from roughly the last decade, including all 5 seeds, so Last.fm artist similarity replaced it as the Last.fm source; see §4.1.)
- Artist caps, skip-ahead / intro handling, "already heard" imports.
- File expiry and cleanup.
- Re-rating tracks later (beyond correcting the most recent rating).
- Comparing embedding models (MuQ, MERT etc.).
- Implicit signals (skips, play time) as learning input.
- Exploration slider, user-ordered seeds.

---

## 2. Folders

```
references/   # 5 original reference tracks + tracks rated 4–5 (learning plays only)
keepers/      # tracks rated 3 (learning plays) + control tracks rated 3–5
cache/        # downloaded pool audio (internal)
data/         # SQLite DB, embeddings, logs
```

- The 5 original reference tracks are local audio files with **artist and title tags** (or `Artist - Title` filenames). They are treated as rated **5**.
- No other folders are needed for the prototype.

---

## 3. Ratings

| Rating | Meaning | Folder | Reference set | Seed? | Audio file | Playback |
|---|---|---|---|---|---|---|
| 5 | Amazing | `references/` | Positive, weight 2 | Yes, from next pull | Kept | Plays to end |
| 4 | Great | `references/` | Positive, weight 1 | No | Kept | Plays to end |
| 3 | Good (tentative keeper) | `keepers/` | Not included | No | Kept | Plays to end |
| 2 | Bad | — | Negative, weight 1 | No | Deleted (embedding kept) | Skips immediately |
| 1 | Terrible | — | Negative, weight 2 | No | Deleted (embedding kept) | Skips immediately |

- Manual skip is always available.
- The most recent rating can be changed (mis-tap correction); all effects update accordingly. (Audio of a 1–2 rating is held in `data/undo/` and deleted when the next rating is made, so a correction can restore it.)
- Skipping without rating is allowed: the track counts as shown (never re-shown) and has no learning effect.
- Ratings are used as-is (1–5) for every average.

**Seeds** = the 5 originals + every track rated 5 (learning plays only).
**Positive reference set** = originals + tracks rated 4–5.
**Negative set** = tracks rated 1–2.

---

## 4. Candidate pool

### 4.1 Pull (one "generation")
1. **Match seeds to YouTube** (first run, and for new 5-rated seeds): search YouTube Music (`ytmusicapi`, songs filter) by artist + title; accept only if duration is within ±5 s of the local file.
2. **Query sources** for every seed:
   - Last.fm artist similarity — `artist.getSimilar` for the seed's artist (default top **10** similar artists, excluding the seed's own artist(s) and duplicate names), then `artist.getTopTracks` for each (default top **5**), ≈50 results. These count as source "Last.fm" for weighting and credit.
     - _Why not `track.getSimilar`_: Last.fm's track-level similarity is empty for tracks from roughly the last decade (tested 2026-10-28: 0 results for all 5 seeds and for hits such as Dua Lipa – Levitating and Sabrina Carpenter – Espresso; older tracks such as Daft Punk – Get Lucky return 50).
   - YouTube Music radio — `get_watch_playlist(videoId, radio=True)` — default up to 50 results.
   - These return lists only (no downloads).
3. **Record provenance**: every candidate stores every (seed, source) pair that suggested it.
4. **Filter**:
   - Already rated, already shown, or already in pool.
   - Duplicates: same ISRC, or same normalised artist + title.
   - **Versions**: Extended Mix, Radio Edit, Original Mix (and similar edit/length labels) are the **same song** — keep one, **prefer Extended if available**. Named remixes (e.g. "(X Remix)") are **different tracks**.
   - Last.fm candidates must match on YouTube Music: search artist + title (songs filter), require the same normalised artist + title, and accept if duration within ±5 s of Last.fm's duration; prefer Extended version if the search returned one. `artist.getTopTracks` gives no duration, so it is fetched with `track.getInfo`; when Last.fm has none (about half: 98 of 210 matches in the first pull), the name match alone is accepted. YouTube Music radio candidates already have IDs.
   - YouTube Music user uploads (channel name in the artist field) take artist and title from an `Artist - Title` video title.
   - Unmatched candidates are dropped (logged with reason).
5. **Fill the pool** to the target size (default **75**) using quotas:
   - Each seed gets a share proportional to its **seed average** (§5) relative to other seeds, with a **minimum of one third of an equal share**.
   - Within a seed's share, the split between Last.fm and YouTube Music follows the **source averages** (§5).
   - First pull: all seeds and sources start at 3, so shares are equal.
6. **Download** pool tracks only: full audio with yt-dlp, **paced** (a few seconds between downloads, no parallel bursts).
7. **Analyse** each downloaded track (§6), then score (§7).
8. **First pull only**: playback can start once the first ~20 tracks are analysed and scored; the rest join as they finish.

### 4.2 Refill
- Trigger a new pull when **fewer than 25 unrated tracks** remain, or the **best remaining score drops below a threshold** (configurable). (The score threshold is off by default: scores are normalised across the current pool, so an absolute threshold has no stable meaning.)
- New candidates **merge** into the existing pool.
- At each new generation: apply decay (§5), recalculate quotas, add new 5-rated seeds (starting at average 3).
- Refills run in the background while listening.

---

## 5. Seed and source averages

Decayed average rating with a neutral prior of 3:

```
avg = (3 × k + Σ wᵢ · rᵢ) / (k + Σ wᵢ)
```

- `rᵢ` = ratings (1–5) of tracks the seed/source suggested (learning plays only).
- `wᵢ` = decay weight by generation: current = 1, previous = 0.5, two back = 0.25, … (decay factor **0.5**, configurable).
- `k` = prior strength, default **3**.
- Effect: one bad batch lowers an average but doesn't crash it; with little recent data it drifts back to 3.
- **Credit rule**: every seed and source that suggested a track gets full credit for its rating.
- **Control plays give no credit.**
- Same formula for seeds and for sources (Last.fm, YouTube Music).

---

## 6. Audio analysis

- Model: **LAION CLAP, music checkpoint** (confirm exact checkpoint ID on Hugging Face). Runs on the GPU.
- **Full-track, chunked**: split each track into consecutive chunks of the model's native clip length, embed each, then average and L2-normalise for the track embedding.
- **Store both**: the track embedding and all per-chunk embeddings.
- Embed the 5 original references, all pool candidates, and keep embeddings of every rated track (including deleted audio).
- The same model for everything — embeddings from different models are not comparable.

---

## 7. Scoring

All components normalised to 0–1 across the current pool before weighting.

| Component | Definition | Sign |
|---|---|---|
| `audio_match` | Weighted mean cosine similarity to the **3 most similar** tracks in the positive reference set (k=1 if fewer than 5 references); 5-rated references count double | + |
| `dislike_match` | Highest similarity to any track in the negative set; 1-rated count double. (Decided 2026-10-29: similarity to each disliked track is rescaled to 0–1 across the pool, 2-rated tracks count half, then the max is taken; not re-normalised, so a pool close only to 2-rated tracks tops out at 0.5.) | − |
| `source_score` | Highest source average among the sources that suggested it | + |
| `seed_score` | Highest seed average among the seeds that suggested it | + |
| `agreement` | `log(1 + number of distinct (seed, source) suggestions)` | + |
| `personal_model` | Regression on the CLAP embedding predicting rating (1–5), trained on learning ratings; **0 until 50 learning ratings**. (Ridge regression; strength picked per fit by leave-one-out error.) | + |
| `diversity_penalty` | Highest similarity to the last 5 played tracks | − |

```
score = Σ weight_c × component_c
```

Initial weights (configurable, replaced by re-weighting):

| Component | Weight |
|---|---|
| audio_match | 0.45 |
| dislike_match | −0.30 |
| source_score | 0.15 |
| seed_score | 0.15 |
| agreement | 0.10 |
| personal_model | 0.20 (once active) |
| diversity_penalty | −0.10 |

- **Re-score the whole pool after every rating** (milliseconds).
- New 4–5 ratings join the positive reference set immediately, so audio match updates for the whole pool straight away.

---

## 8. Playback selection

- **Learning plays**: the current top-scored unrated track.
- **Control plays**: **1 in 5 plays** is the current **median-scored** unrated track in the pool. (Exactly one per block of 5 plays, at a random position in the block, so the pattern can't be counted.)
- **Blind**: the UI never shows the score, breakdown or control status until after rating.
- **Controls are evaluation only**:
  - Rated and logged.
  - No seed or source credit.
  - Not added to the reference set or negative set.
  - Excluded from re-weighting and the personal model.
  - Control tracks rated 3–5 are saved to `keepers/` (even 4s and 5s, so they never enter `references/`).

---

## 9. Re-weighting

- Runs at **30, 45, 60 and 75 learning ratings**, then every 15.
- Uses each learning-rated track's component values **as logged at the time it was shown**.
- Objective: **concordance** — the fraction of pairs of rated tracks with different ratings where the higher-rated track has the higher score (equal-rating pairs skipped).
- Recent ratings count more: apply the same generation decay (0.5) as pair weights. (A pair's weight is the product of its two ratings' decay weights.)
- Components normalised; **sign constraints** as in §7.
- Method: implementer's choice (e.g. random/grid search over weights, or a pairwise / logistic fit). Small problem; should take well under a second.
- **New weights are always adopted.** Re-score the pool afterwards. No re-pull.
- **Evaluation only (logged, not gating)**: score how the new weights rank the next 15 learning ratings vs the previous weights.

---

## 10. Logging

**Every track shown**
- Timestamp, generation, play number.
- Control or learning.
- All (seed, source) pairs that suggested it.
- Every score component value, total score, rank in pool at the time.
- Rating, and how long it played before rating.

**Every generation**
- Seed averages, source averages, resulting quotas.
- Current weights.
- Pool stats: raw suggestions per seed/source, removals per filter reason, YouTube match failures, download failures, final pool size.

**Every re-weighting**
- Old and new weights, concordance of each on the data used.
- Later: concordance of old vs new on the next 15 learning ratings.

---

## 11. UI (localhost)

**Player view**
- Current track: artist, title.
- Standard audio player (play/pause/seek/skip), loudness-normalised.
- Five rating buttons (1–5).
- After rating: reveal score breakdown, sources, seeds, and whether it was a control; then advance per §3 playback rules.
- Option to change the most recent rating.

**Results view**
- Ranked vs control: **average rating** and **keep rate** (share rated 3–5), with error ranges.
- Rolling trend (window of 25 ratings) for ranked and control.
- Seed and source averages over time.
- Re-weighting history and its evaluation.
- Total ratings count.

---

## 12. Success test

- Target: about **100 plays** (≈80 learning + ≈20 control). No maximum — results update continuously.
- **Primary**: ranked average rating vs control average rating. Default criterion (confirm): ranked ≥ **0.5 stars** above control after ~100 plays.
- **Secondary**: ranked rolling trend rising over time; keep rate above my usual ~1 in 5; re-weights improving concordance on subsequent ratings.

---

## 13. Milestones

Build in order. Stop after each for my check.

1. **CLAP setup** — embed the 5 references (chunked, stored). Check: similarity matrix looks sensible; a couple of known-similar and known-different tracks score as expected.
2. **Candidates** — seed YouTube matching, Last.fm + YouTube Music queries, provenance, filters (incl. versions / Extended preference), YouTube matching with duration check. Check: candidate list with sources and filter-reason counts.
3. **Pool** — quotas, paced downloads, loudness normalisation, analysis. Check: 75 analysed tracks in the DB.
4. **Scoring and ratings** — components, score, rating effects (folders, sets, seeds, averages with decay and prior). Check: scripted test ratings produce the expected updates.
5. **UI** — localhost player, rating buttons, blind reveal, playback rules, early start after ~20 analysed. Check: full listen-and-rate loop works.
6. **Learning and evaluation** — controls, re-weighting schedule, personal model, refills, logging, results view. Check: end-to-end session with all logs populated.
