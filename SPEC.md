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
5. **Fill the pool** to the target size (default **150**; was 75 until 2026-10-29, raised so the top pick always comes from a larger pool) using quotas:
   - Each seed gets a share proportional to its **seed average** (§5) relative to other seeds, with a **minimum of one third of an equal share**.
   - Within a seed's share, the split between Last.fm and YouTube Music follows the **source averages** (§5).
   - First pull: all seeds and sources start at 3, so shares are equal.
6. **Download** pool tracks only: full audio with yt-dlp, **paced** (a few seconds between downloads, no parallel bursts).
7. **Analyse** each downloaded track (§6), then score (§7).
8. **First pull only**: playback can start once the first ~20 tracks are analysed and scored; the rest join as they finish.

### 4.2 Refill
- Trigger a new pull when **fewer than 100 unrated tracks** remain (was 25 until 2026-10-29), or the **best remaining score drops below a threshold** (configurable). (The score threshold is off by default: scores are normalised across the current pool, so an absolute threshold has no stable meaning.)
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
- Current track: artist, title — hidden until the track is rated (added 2026-10-29, configurable), so familiarity with an artist doesn't sway the rating.
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

---

## 14. Later considerations (not in scope yet)

Open points raised during the first test. None of these is built; changing scoring mid-test would make early and late plays incomparable, so anything that affects ranking waits until the ~100-play test is done.

1. **Volume slider** — a player volume control, applied after the per-track loudness gain so normalisation still holds. No effect on the test; could be added at any time.
2. **BPM and key analysis as score components** — measure tempo and musical key per track (e.g. librosa or Essentia) at analysis time. Possible components: closeness to the BPM range of liked tracks, and key compatibility (Camelot wheel). Would join re-weighting like the others, with sign constraints. Open questions: how reliable tempo detection is on this genre (half/double-tempo errors), and whether CLAP already captures most of it.
3. **Play at a chosen BPM** — switch the current track to a target tempo and back, pitch preserved (browser `playbackRate` with `preservesPitch`, or offline time-stretching for better quality). Needs per-track BPM (item 2). If it is used while rating, log the tempo it was played at, since a rating then describes a modified track.
4. **Richer feedback** — optional reason tags alongside the 1–5 rating, e.g. "good beat, bad lyrics/vocals", "good song but too boring", "too long", "wrong energy". First step: log tags only (no learning effect) to see how often each reason occurs. If vocals turn out to drive many low ratings, consider:
   - separating vocals from the instrumental (stem separation, e.g. Demucs) and embedding each, so a liked beat and a disliked vocal style become separate signals (e.g. split `audio_match` / `dislike_match` into beat and vocal parts);
   - transcribing lyrics (e.g. Whisper) to judge their content — note this edges towards LLM sources, currently out of scope (§1).
   Tags could also steer what a low rating penalises (e.g. a "bad vocals" 2 penalises vocal similarity, not beat similarity).
5. **Re-weighting after every song** — instead of at 30, 45, 60, 75, then every 15. Pros: faster adaptation. Cons: fitting on very few ratings overfits (in the simulated session the fitted concordance jumped while the next-15 evaluation was mixed), and weights that change every play make the evaluation harder to read. Options: re-weight every song but shrink towards the previous weights (or the initial ones), or require a minimum number of ratings and only adopt changes that also hold on held-out recent ratings.
6. **Show the algorithm's read of a track, and let me correct it** — display estimated attributes (e.g. energy, mood, vocal/instrumental, darkness, groove) and let me say what I think. CLAP's text encoder can score a track against descriptions such as "high-energy club track" or "mellow, sparse" without extra training, so estimates are cheap. Corrections become labelled data: they could calibrate the estimates, learn which attributes I prefer, and feed new score components. Show it only after rating (or make it clear it never affects the current track's rating), so it doesn't bias the blind rating. Ties in with item 4 (feedback tags).
7. **Target sliders (energy, deepness, …)** — sliders for the kind of track I want right now, using the same estimated attributes as item 6. Each slider starts at the average of the seeds or reference set (so by default it asks for "more like my references"), and I can move it. A new score component rewards closeness to the targets. Notes: this steers the ranking, close to the "exploration slider" ruled out in §1, so it belongs after the test; if it is used during an evaluation, log the slider values with every play, because moving them changes what "top-scored" means and affects the ranked vs control comparison.
8. **"Don't save" — separate learning from my collection** — for a track I rate highly (so it should steer the algorithm, even become a seed or reference) but don't want saved for personal listening later, e.g. I already know it, or it's great but not for my list. Proposal: make saving a separate toggle next to the 1–5 rating ("Save to my collection", on by default for 3–5), so the rating alone drives learning: a 5 still becomes a seed and a weight-2 reference, a 4 a reference, with full seed/source credit. When not saved, the audio goes nowhere personal: learning only needs the stored embedding (references, personal model) and the YouTube ID (seeds), not the file, so it can be deleted or kept in an internal folder. This also means `references/` stops doubling as my collection: the reference *set* lives in the DB, and a collection folder holds only what I chose to save. Updated 2026-10-29: this replaces the earlier idea of a "like" that wouldn't count as a seed. To decide: whether "known" tracks should also be flagged in the results, since familiarity can inflate ratings and blur the ranked vs control comparison (e.g. report results with and without known tracks). Related: "already heard" imports are out of scope (§1); this flag would be the in-session way to mark them.
    - **Alternative to discuss: rating plus a separate "Add" button.** Instead of a save toggle that defaults on for 3–5, the 1–5 rating is purely the vote (learning only), and an explicit "Add to my collection" button saves the track, with nothing saved unless I press it. Trade-offs: clearer separation and no accidental saves, but one more action per keeper; also decide whether "Add" itself carries a learning signal (e.g. a nudge upwards) or is purely personal. Also affects the results' "keep rate" (§11), which currently means rated 3–5: it could instead mean "added".
9. **Release-age slider** — a preferred era (e.g. pre-1990 for soul, last couple of years for electronic) that influences the score rather than filtering: a component rewarding closeness of the release year to the target (soft fall-off, so older or newer tracks still appear, just lower). Could default to the seeds' or references' release years, like item 7. Open question: getting reliable release years — YouTube Music sometimes gives a year, MusicBrainz has first-release dates but poor coverage of recent electronic releases (§1 note), and upload dates are misleading for re-uploads; tracks with no known year would score neutral. Same caveat as item 7: it steers the ranking, so log the slider value with every play if used during an evaluation.
10. **Artwork in the player** — show the track's cover art. YouTube Music returns thumbnails with search and radio results, and yt-dlp can fetch the thumbnail at download time; tracks already in the pool would need it fetched once (by video ID). Store a local copy so the player doesn't depend on YouTube at playback time. Display only; it doesn't affect ranking, so like the volume slider it could be added at any time without disturbing the test. Like the artist and title, keep it hidden until the track is rated.
11. **Popularity** — use how widely played a track is, as a weight influence (or optional filter). Data is available: Last.fm `track.getInfo` gives listeners and play count (already called for Last.fm candidates), and yt-dlp reports YouTube view counts at download time. Could work as a component rewarding closeness to a preferred popularity level (a slider: underground ↔ well known), defaulting to the references' level. Notes: counts differ wildly between platforms and with track age, so compare on a log scale and within source; new releases look unpopular simply because they're new.
12. **Rating scale: a neutral "Fine"** — consider Bad · Fine · Good · Great · Amazing, where only Bad feeds the negative set and Fine is neutral (not a dislike, not kept). Concern: 1–2 ratings given for reasons other than the sound (lyrics, boring, etc.) push similar-sounding tracks down, so the system may learn to avoid sounds I like. At 33 plays (2026-10-29): 26 learning ratings, 18 of them 1–2 and one 4, so the negative set (18) was three times the positive set (6). Decided to keep the current scale for this test; check at ~100 plays whether `dislike_match` and the re-weighted weights look distorted (e.g. dislike weight driven high while audio match falls). Ties in with item 4: reason tags could make a "bad lyrics" low rating neutral for sound similarity.
    - **Update (2026-10-29): a "Fine" / "Pass" / "Not right" option could cover many cases that aren't really dislikes:** don't like the lyrics but like the song; like it but it isn't a good fit; one small part I don't like but it's on the right path; already know it and don't want to add it (better handled by item 8's "don't save" toggle, which keeps the full positive signal). Options for its effect: fully neutral (no learning, not kept); a mild signal (e.g. counts as a weak positive for sound similarity, since "right path" and "good beat, bad lyrics" are near-misses, while not counting as a keeper); or combined with reason tags (item 4) so each reason has its own effect. Open choice: a separate button alongside 1–5 (so 1–2 stay genuine dislikes), or replacing 2 as in the scale above.
13. **Dislike saturation and negative scores** — observed at ~60 plays (2026-10-29): after re-weighting at 30 and 45 ratings the weights were audio_match +0.05 (from 0.45), dislike_match −0.66 (from −0.30), seed_score +0.36, diversity −0.16; every score was negative (top −0.28). With 18 tracks in the negative set, `dislike_match` (max over disliked tracks of per-track normalised similarity) was ≥0.95 for most of the pool (median 0.99), so the top pick became the track least similar to anything rated (the #1 had audio_match 0.00). Re-weighting concordance was weak (0.58 → 0.60 at 45). Likely causes: the max over a growing negative set saturates in a pool of similar-sounding tracks; and 1–2 ratings given for non-sound reasons (item 12) taught re-weighting that sound similarity doesn't predict ratings. Ideas: make `dislike_match` a mean over the k most similar disliked tracks (like `audio_match`) or a net "closer to likes than dislikes" measure; shrink re-weighting towards the initial weights; reason tags (item 4); neutral "Fine" (item 12). Negative scores themselves are harmless (only order matters), but the shift to "least similar wins" works against the idea being tested.

---

## 15. Version 2 plan (agreed 2026-10-29, not built yet)

Based on the first 100-play test (§14, and the review on 2026-10-29: ranked 2.38 vs control 2.14, +0.24★, 95% range −0.26 to +0.73; agreement between sources was the only strong signal, +0.41; audio similarity +0.12; dislike match ran the wrong way; personal model −0.18; two of three re-weightings made the next 15 ratings worse). Where v2 differs from §1–§13, this section wins.

### 15.1 Fresh run
- v2 is a new ~100-play run with a **new set of seeds** I provide. v1's database, folders and logs are archived (e.g. `runs/v1/`), not carried into learning.
- Seeds can be given as **Spotify links** — track links or one public playlist link — as well as local files. Artist, title, length and release date are read from Spotify's public pages (no API or account; checked 2026-10-29), the same version is found on YouTube Music (name match, ±5 s), downloaded and analysed. Links that can't be read or matched are logged so the track can be supplied another way. (Unofficial pages, so this may need fixing if Spotify changes them.)
- Folders: `seeds/` (the original seeds: provided files or downloaded from links), `collection/` (tracks I Add), `cache/`, `data/`. `references/` and `keepers/` are retired; the reference set lives in the database.

### 15.2 Rating buttons (replaces §3)
| Button | Meaning | Learning | Saved |
|---|---|---|---|
| **Off** (1) | wrong direction | negative set | no |
| **Close** (2) | near, or right sound with a bad part | neutral | no |
| **On target** (3) | right sound, not for my collection | positive set, weight 1 | no |
| **Add** (4) | right sound, and I want it | positive set, weight 2; becomes a seed | yes, to `collection/` |
- Rating = direction (learning only); Add = saving. A good-sounding track with a bad part is On target or Close, never Off.
- Audio of tracks not Added is deleted after the next rating (held until then so the back button can still change it).
- Playback (built 2026-10-29, configurable `play_to_end`): On target and Add keep playing to the end; Off and Close move straight to the next track.
- Skipped tracks' audio is deleted once a later rating makes them unreachable with the back button.
- Averages (§5) use the 1–4 values, with the prior at the scale midpoint (2.5).

### 15.3 Blind listening (replaces the §11 reveal)
- Before and after rating, the player shows only what's needed to listen: no score, rank, breakdown, sources or control status — ever. Artist and title (and artwork) appear only after rating.
- Everything stays in the logs and the results page. The results page stays available; v1 data is wiped from the live database at the start of v2 (archived, §15.1), so it shows v2 only.

### 15.4 Controls (replaces §8 control rule)
- Each play is a control with probability 1 in 5, decided independently (no blocks, so they can't be counted).
- A control is a **random** unplayed pool track (not the median). Reasons (discussed 2026-10-29): a random track is exactly "what the sources alone would play", so ranked vs control measures the lift over sources directly; and because random controls land at every rank, their rank vs rating shows whether higher scores really mean better tracks (median controls all sit mid-rank and can't show that). "Better than what it thinks is average" can still be checked from the controls that land mid-rank.
- Controls' ratings are used to evaluate weight sets (§15.8) — they're the only tracks rated without being chosen by the weights. This doesn't bias the comparison: controls are still picked at random.

### 15.5 Sources (changes §1, §4.1)
- Add **Deezer artist radio** as a third source (free API, no key; covered all 7 seeds tested on 2026-10-29). Deezer also supplies **ISRCs** (exact duplicate detection) and **release dates**.
- Agreement was the strongest signal, so: starting agreement weight raised (0.10 → 0.25); more sources mean more chances for overlap. **Partial artist credit** (built 2026-10-29): `agreement = log(1 + distinct (seed, source) suggestions + 0.5 × other sources that suggested the same artist but not this track)`. Checked on v1 ratings: artist-level agreement tracked ratings as well as track-level (+0.37 vs +0.35); artists suggested by 2 sources averaged 2.90 vs 2.10, and 2.72 even when the track itself came from one source. In the first v2 pull it reaches 146 of 446 candidates, against 24 for exact-track agreement.
- Deezer has no track radio, and artist radio is thin for new artists (2–3 tracks for Leon Vera and Deaf Toucan, 2026-10-29), so the source uses the artist radio of **every artist credited on the seed**, interleaved (e.g. La Musica gets 50 via NewTone). Own-artist share was low where radio was full (3–4 of 50). A seed whose artists have thin radios simply contributes fewer Deezer candidates; quotas pass its unused share to the other sources.
- Spotify (recommendations closed to new apps since 2024-11) stays out. TIDAL (track-level similar tracks; needs a developer account) is a possible later fourth source.

### 15.6 New score components
- **BPM**: measured from the audio at analysis time (Deezer's BPM field is empty for these tracks), folded into a configurable range (85–175) to avoid half/double-tempo errors. Built 2026-10-29: librosa beat tracking, then the mean of the steady beat gaps across the whole track (single gaps are quantised to ~12 ms); exact to ±0.3 BPM on synthetic beats. **3:2 tie-break** (built 2026-10-29, at my suggestion): a dotted rhythm can lock the tracker onto 2/3 of the tempo (Sam Alfred – No More read 94.6, really ~140), which folding can't undo; so the reading and its 3:2-related readings are compared by repetition strength (onset autocorrelation at one and two beats) × a gentle preference for the seeds' median BPM (half an octave wide). Seeds are read twice: plain readings give the median, then each is re-read with it. Result: No More 142.1, the other seeds unchanged; straight synthetic beats at 95 and 174 keep their tempo even with a 138 BPM preference. `bpm_match`: with no target set, it rewards closeness to the median BPM of seeds and On target/Add tracks; when I set a Target BPM in the player, tracks within ±12% of it (the window Match tempo can stretch) get credit that is highest at the target and falls linearly to half at the window's edge — closer is better — then drops away quickly beyond it. No range sliders (decided 2026-10-29).
- **Play at target BPM**: when a target is set, tracks play time-stretched to it with pitch preserved, within a maximum stretch (configurable, e.g. ±12%); tracks needing less stretch score higher via `bpm_match`. The tempo played is logged with each rating.
- **Release year** (§14 item 9): `year_match` rewards closeness to the release years of my On target/Add tracks (soft fall-off; no year = neutral). In v1, newer tracks rated slightly higher (+0.21).
- Key, popularity (no link in v1), attribute sliders (§14 6–7), reason tags and vocal separation (§14 4) stay parked.

### 15.7 Dislike fix (§14 item 13)
- Only **Off** feeds the negative set. `dislike_match` becomes the mean similarity to the few closest disliked tracks (not the single closest), so one match can't dominate. Built 2026-10-29: mean of the 3 closest Off tracks, min–max normalised like the other components.

### 15.8 Re-weighting v2 (replaces §9)
- **When**: after 20 learning ratings, then every 5.
- **Objective**: top-weighted — reward weights that would have ranked Add/On target at the top and kept Off/Close away from it; mistakes near the top count most. Recent ratings count more.
- **Small steps**: each weight moves at most ±0.05 per update, so drift is gradual.
- **Guardrails**: audio_match ≥ 0.20, agreement ≥ 0.10, dislike ≥ −0.30, diversity ≥ −0.20; no weight changes sign.
- **Continuous re-evaluation (agreed 2026-10-29)**: at every update, the current weights, the proposed small step and every earlier weight set are re-scored against the **whole rating history** (recency-weighted, top-weighted objective, control ratings included). The best is re-decided each time, so no set stays the target because of a few lucky picks: as new ratings arrive, a set that would rank them badly loses its place. Live results of each set's own picks are logged but don't drive decisions.
- Moving to an earlier set still respects the ±0.05 step limit (it is approached gradually), unless config says otherwise.
- Built 2026-10-29: objective = share of correctly ordered pairs of rated plays (learning and control), each pair weighted by recency (half-life 30 ratings) × the gap in 2^rating, so Add-vs-Off mistakes count far more than Close-vs-Off. Each update tries the step towards the search's optimum and towards every earlier set, and adopts the best-scoring step — staying put included, so the fit to history never gets worse. Logged per update: what it moved towards, and every candidate's score. Simulated check: a set that fitted the first 20 ratings best (0.978) fell to 0.628 by 100 as later ratings contradicted it, while the weights drifted ≤0.05 per update in the right direction.
- No separate pool check or escalation rule (dropped 2026-10-29): continuous re-evaluation against the full history is the only mechanism.
- All numbers live in the config file.

### 15.9 Player and collection
- Off / Close / On target / Add buttons; back button kept; volume slider (applied after the loudness gain); artwork, hidden until rated; optional target-BPM control.

### 15.10 Success test (replaces §12)
- Ranked vs control: **average rating** (1–4) and **Add rate**, with error ranges; plus, from the random controls, rank vs rating (do higher scores mean better tracks?).
- No pre-set pass criterion: judged from the data.
- Aim for ~25–30 controls (more plays, or the 1 in 5 rate kept and the run extended), so the comparison is tighter than v1's 21.

### 15.11 Milestones (build in order, stop after each for my check)
1. **Fresh run setup** — archive v1, new folders and seeds, config for v2.
2. **Deezer source** — artist radio, ISRC duplicates, release dates, agreement changes. Check: candidate list with three sources and overlap counts.
3. **Ratings and blind player** — four buttons, Add/collection, deletion of non-added audio, no reveal, random controls, volume, artwork. Check: listen-and-rate loop with nothing revealed.
4. **BPM and year** — BPM measurement, `bpm_match`, `year_match`, target-BPM playback. Check: BPM values on the pool look right; stretched playback sounds clean.
5. **Re-weighting v2 and dislike fix** — objective, steps, guardrails, continuous re-evaluation of all weight sets. Check: simulated session shows gradual drift, and a set that started with lucky picks losing its place as ratings accumulate.
6. **Results and report** — v2 measures, weight-set timeline with reverts, simulated end-to-end session. Then the 100-play run.

---

## 16. Version 3 plan (draft, 2026-10-29 — not built)

### 16.0 Why: v2 results (93 plays, 2026-10-29)
- Ranked 2.42 vs control 2.22 (+0.19, 95% range −0.36 to +0.75); Add rate 22% vs 11% (+11 points, −7 to +29). Strong start (plays 1–30: ranked 2.80, Add 35%, vs control 1.89), then a fade.
- **Exact agreement** (track suggested by 2+ sources) was again the strongest signal (+0.28; 2.88 vs 2.15, Add 28% vs 18%), but scarce: 32 of 162 pool tracks, mostly used up in the first 30 plays.
- **Release year** +0.26 (newer better); BPM closeness to 141 +0.15.
- **Not working:** CLAP audio match (v1 +0.12, v2 −0.11), personal model on CLAP (v1 −0.18, v2 −0.26; predictions all 2.38–2.49), artist-level agreement credit (v2 −0.06), controls' rank vs rating (0.01, 18 controls).
- Re-weighting over-committed to release year (0.10 → 0.57) while agreement fell to its floor and the dislike penalty to −0.09; picks drifted towards tracks close to Off ratings.
- Conclusion on CLAP: it separates **genre/style**, not **taste within a style** — and the sources already deliver one style, so it adds nothing here.

### 16.1 Milestone 1 — Taste-model test (offline, before anything else)
Find out from my existing ratings whether any audio model or analysis captures my taste within a style. Bring in "comparing embedding models" (§1 out of scope until now) for this test.

**Data**
- Every rated track from v1 (103) and v2 (90), with rating, run, control flag and the non-audio facts already logged (sources/agreement, release year, BPM).
- Audio deleted after rating is re-downloaded from the stored YouTube IDs (~150 tracks, ~25 min); seeds and collection files are reused.
- Ratings differ in scale (v1 1–5 with noisy low ratings, v2 1–4): analyse each run on its own and pooled with per-run normalisation (rank within run); targets: rating, and "liked" (v2 On target/Add; v1 4–5).

**Representations to compare** (all fit in 16 GB VRAM; weights mostly non-commercial licences, fine for personal use)
1. CLAP `laion/larger_clap_music` — baseline.
2. **MERT-v1-330M** (24 kHz; sweep its 25 layers — task performance varies by layer).
3. **MuQ-large-msd-iter** and **MuQ-MuLan-large** (24 kHz; MuLan also allows text-attribute scoring).
4. **OMAR-RQ** (MTG, 2025; multifeature variants).
5. **MAEST** (Discogs 400 styles; electronic-heavy training data — fine-grained sub-styles) and **Discogs-EffNet** (style and track-similarity embeddings): embeddings plus the 400 style probabilities as interpretable features.
6. **Essentia classifiers** (interpretable descriptors): danceability, arousal/valence (energy/positiveness), mood aggressive/happy/party/relaxed/sad, voice/instrumental, vocal gender, engagement, approachability, electronic/acoustic, bright/dark timbre; plus signal features (loudness range, onset density, spectral brightness, BPM, key).
7. **Stems** (Demucs): vocal share over time, and the best model run separately on vocals and instrumental — tests "good beat, bad vocals/lyrics" (§14 item 4).

**Analyses** (for each representation)
- Nearest-neighbour prediction (rating of the most similar rated tracks), regularised regression / logistic, and pairwise ranking (the same "right order" measure re-weighting uses).
- **Section analysis**: per-chunk predictions aggregated by mean vs **weakest section** vs spread — tests "one bad part ruins it".
- **Added value**: best audio features combined with agreement, year and BPM, against those alone.

**Evaluation**
- Cross-validation **grouped by artist** (no artist in both training and test), so a model can't score by recognising an artist.
- Metrics: rank correlation with ratings, AUC for liked vs not, and top-10 precision ("would its top picks have been my Adds?").
- **Permutation baseline**: the same analysis on shuffled ratings, to tell signal from noise; bootstrap 95% ranges.
- Caveat: ~190 ratings can only show moderate effects (a correlation around 0.2 is near the limit of what this many ratings can confirm).

**As built (2026-10-29)**
- Runs in its own environment, `tastetest/` (own `pyproject.toml`): OMAR-RQ needs `transformers` < 5, the player uses 5.x. Outputs in `runs/taste-test/`; settings in `config.toml` `[taste_test]`.
- Dataset: 193 rated plays (v1 103, v2 90), 59 liked, 105 artists; 135 tracks re-downloaded.
- Models run: CLAP (stored), MuQ (13 layers), MuQ-MuLan (+ 35 plain-English descriptions), OMAR-RQ (24 layers), Discogs-EffNet (+ 400 styles, 14 Essentia descriptor heads, DEAM/emoMusic arousal–valence), MAEST via Essentia's plain model file (layers 5/7/9/11 + 400 styles), signal measurements (librosa), Demucs vocal share.
- **MERT-v1-330M** (25 layers, 5 s context, averaged into ~10 s sections): loads with `trust_remote_code` (runs Python from its Hugging Face page) — approved by me on 2026-10-29, and pinned to the reviewed revision `5240c27…` so later changes on Hugging Face can't run unreviewed code.
- Essentia's TensorFlow build runs on the CPU (no support for this GPU generation) — slower, same results.

**Decision rule**
- A representation enters v3 scoring only if it clearly beats both the shuffled baseline and CLAP on held-out artists. If none does, v3 drops audio-similarity components and builds on what works (agreement, year, tempo).
- Output: a results table per representation and analysis, written to `runs/taste-test/` and summarised for review.

**Results (2026-10-30)** — full tables in `runs/taste-test/results.md`, `ensemble.md`
- Beat the shuffled baseline: **MuQ-MuLan text descriptions ρ +0.25** (95% +0.10 to +0.37, p 0.005; v2 +0.39), MuQ-MuLan audio +0.19 (p 0.005; v2 +0.32), MERT layer 11 +0.20 (p 0.035), MuQ layer 4 +0.19 (p 0.02), EffNet +0.16 (p 0.015; best top-10 at 50% vs 31% base), Essentia descriptors +0.13, Discogs styles +0.10.
- **MAEST** (layer 9; finished later, CPU-only ~28 s/track): ρ **+0.275** (95% +0.15 to +0.40, p 0.005; v1 +0.26, v2 +0.33) — level with MuQ-MuLan text, not clearly better (the ranges overlap almost entirely). Its predictions correlate 0.70 with MuQ-MuLan's; combined: +0.28 (MuLan text + MAEST), no real gain. Its 400 style probabilities: +0.14.
- **MAEST vs MuQ-MuLan, paired** (same tracks, same held-out artists, 2,000 resamples): vs MuLan text ρ +0.03 (95% −0.07 to +0.14; MAEST ahead in 71%), AUC +0.06 (−0.01 to +0.13); vs MuLan audio fingerprint ρ +0.08 (−0.02 to +0.18; ahead in 95%), AUC +0.07 (+0.01 to +0.14). By run: MAEST v1 +0.26 / v2 +0.33, MuLan text +0.18 / +0.39 — MAEST steadier across runs. (MAEST's layer was picked from several, which flatters it slightly here; the shuffled baseline accounts for that.)
- **MAEST on the GPU**: the 28 s/track was the test script running the network once per layer read, on the CPU (Essentia's TensorFlow lacks this GPU). The Hugging Face PyTorch weights (`mtg-upf/discogs-maest-30s-pw-129e`, standard transformers class, pinned revision; mel front end rebuilt from transformers' audio utilities, so no code from the model page runs) take **~0.6 s/track** on the GPU, all 12 layers in one pass, and reproduce the result: ρ +0.282 (layer 9), AUC 0.67.
- No signal: **CLAP −0.05** (p 0.59), OMAR-RQ +0.01, signal measurements −0.10, Demucs vocal share −0.11 (how *much* vocal doesn't matter; the *kind* does).
- What it says about my taste: higher — female vocals (+0.28), energetic (+0.18), bright (+0.14), happy (+0.13), Euro/progressive house; lower — "build-up and drop" (−0.22), heavy bassline (−0.19), punchy kick (−0.18), lo-fi/raw (−0.18), minimal stripped-back (−0.17), instrumental without vocals (−0.17).
- **Section consistency** (how even the per-section predictions are) beat the whole-track average for MuQ (+0.25 vs +0.15), EffNet (+0.22 vs +0.01) and MuLan (+0.19 vs +0.04) — supports "one bad part ruins it".
- **Combining audio models** didn't beat the best single one (+0.22 to +0.24 vs +0.25): they hear largely the same things. **Combining audio with the non-audio facts did** (v2): facts alone +0.33 (top-10 55%) → **facts + MuQ-MuLan +0.43 (top-10 59%)**.
- Cost: MuQ-MuLan ~2 s per track on the GPU (~3 GB VRAM); Essentia ~3 s/track on CPU; MAEST ~28 s/track on CPU.
- Newer-model check (2026-10-30): no successor to MuQ-MuLan found; 2026 recommendation studies still place MuQ-MuLan among the best (alongside MERT, MuQ, and older Jukebox/MusicFM on some metrics); CLaMP 3 (2025) is a music–text retrieval model, not shown better for taste; 2026 music LLMs (e.g. MOSS-Music) are captioners, not embedding models. A 2026 paper (arXiv 2601.19109) improves MuQ-MuLan similarity by weighting per-instrument (stem) similarities — a possible later refinement.

**Decision (2026-10-30): replace CLAP with MuQ-MuLan** — audio fingerprint, text-description scores and section consistency — combined with the non-audio facts (agreement, release year, BPM). MERT, MuQ, EffNet and OMAR-RQ dropped (no gain in combination, extra cost). Update (2026-10-30): MAEST came out level with MuQ-MuLan text and ahead of its audio fingerprint, and runs on the GPU after all (below). **Decided: MAEST replaces MuQ-MuLan** for the audio parts of v3 (likes/dislikes match, similarity to the owner seed, personal model); MuQ-MuLan's text descriptions left out for now. Newer-model check (2026-10-30) found **MERT-v2** (m-a-p, released 2026-09-29; 30 s and full-song versions, 632M; leads MARBLE over MuQ and MERT v1) — tested 2026-10-30 (model code approved, pinned to the reviewed revisions): **a dead heat with MAEST**. MERT-v2-30s ρ +0.29, full-song +0.29 (both layer 9), MAEST +0.28; paired vs MAEST: ρ +0.01 (95% −0.09 to +0.12) and +0.00 (−0.09 to +0.10), AUC −0.03 for both; by run v1 +0.36/+0.32 vs MAEST +0.27, v2 +0.24/+0.30 vs +0.34. Predictions agree 0.71–0.73 with MAEST's. MAEST + MERT-v2 averaged: +0.31 (top-10 51–53%) — a small gain within the noise. Section consistency: full-song spread +0.14 (MAEST +0.09), 30 s version none. Cost ~1–3 s/track (MAEST 0.6). **MAEST stays** (equal result, faster, smaller); MERT-v2 is the fallback or a later second opinion.

### 16.2 Agreed changes for v3 (2026-10-29)
All five agreed; item 4 depends on the taste-model test (16.1) — CLAP is reassessed first, not dropped blindly.
- **Agreement as the backbone**: more multi-source tracks (deeper source lists, a fourth source such as TIDAL, quotas favouring multi-source candidates), and refill when multi-source tracks run out, not only when the pool shrinks.
- Turn off artist-level agreement credit (not supported in v2).
- Re-weighting: cap any single weight (e.g. ≤ 0.35) and raise agreement's floor, so one signal can't crowd out the rest.
- Drop or replace CLAP audio match and the personal model, depending on 16.1.
- Keep release year; keep BPM as a mild factor.
- Artist preference: left out (decided 2026-10-29). Control design: see 16.6.

### 16.3 More sources (researched 2026-10-29)
Goal: more multi-source agreement, the strongest signal in both runs.
- **Spotify via playlists I generate** (most promising): Spotify's own recommendations reach me as playlists — a song's or artist's "radio", Discover Weekly, Daily Mixes. Pasting their links, the existing public-page reader (§15.1) can list their tracks, which then go through the usual YouTube matching. Legitimate (my own playlists, public pages) and genuinely Spotify's algorithm; semi-manual (I create/paste the links). To test: whether Spotify-generated radio/Discover playlists are readable via their public embed page.
- **YouTube Music "You might also like"** (`get_song_related`): 20 tracks for some seeds, only 3 overlapping radio; empty for others (La Musica, Cloudburst). Cheap extra list, but the same company as YouTube radio, so less independent.
- **Playlist neighbours** (tracks that co-occur with a seed in public playlists — how Spotify's own recommendations work), tested 2026-10-29 via YouTube Music community playlists and Deezer playlist search: not viable for these seeds — at most 1 playlist per seed contained it (usually the artist's own discography) on YouTube Music, none on Deezer; too new/underground for public playlists.
- **TIDAL — adopted (2026-10-29)**: one-time developer sign-up done (client ID/secret in `.env`), app-only access, unattended. Seeds are found exactly by **ISRC** (all 4 v2 seeds found); `similarTracks` returns up to ~99 tracks per seed (20 per page) with ISRCs. Covered 2 of 4 v2 seeds (Feel The Pressure, No More; none for the newest artists, La Musica and Cloudburst). Largely independent: only 4 and 13 of the 99 were already suggested by the other three sources — enough overlap to create agreement (one overlap was a track I'd Added).
- **YouTube Music "You might also like"** — adopted as an extra list where available.
- **Not usable**: Chosic playlist generator (blocks automated access, HTTP 403; it would mean working around a site's bot protection); ReccoBeats (free API, but didn't know any of the 4 seeds, nor a well-known test track); Apify Spotify scraper (paid, scrapes Spotify against its terms); Spotify's own recommendations endpoint (closed to new apps since 2024-11).

### 16.4 Other changes (agreed 2026-10-29)
- **More seeds** (8–10 rather than 4): more candidates and more chances of agreement.
- **Deeper pulls and a bigger pool**: ~100 suggestions per source per seed (was 50); pool 250 (was 150), filled with **multi-source tracks first**; refill when multi-source tracks run out, not only when the pool shrinks. (A bigger pool of single-source tracks wouldn't help: nothing tested so far ranks those well.)
- **Three buttons**: Off (wrong direction; negative set) · Close (sounds right, not a keeper; mild positive, not saved) · Add (right and saved; strongest positive; seed). "Close" and "On target" were the same idea to me ("sounds right, not quite a fit"), though v2 used all four about equally (ranked: 21 / 16 / 19 / 16). Trade-off accepted: "great but don't save" becomes Close.
- **Hold settings steady during an evaluation run** (v2 had 26 tempo-setting changes): choose the target BPM before a run and lock it in the player during it (changeable in config), because a changing target changes what "top-scored" means.
- **Faster track starts** (v2: 1 in 10 took 1.8–3.7 s): start loading the next track's audio as soon as it's chosen.
- **Report noise**: only log player stalls longer than ~250 ms (v2 logged ~100 stalls of 0–20 ms).

**As built — milestone 2 (2026-10-30)**
- Five source lists per seed, ~100 each: Last.fm (20 similar artists × 5 top tracks), YouTube radio (100), YouTube "You might also like" (up to 20; empty for some new releases), Deezer artist radio (100, all credited artists interleaved), TIDAL similar tracks (up to 100; seed found by the ISRC Deezer gives).
- **Sources counted separately** for agreement and multi-source filling, including YouTube radio and "You might also like" (decided 2026-10-30: keep them separate, analyse after the run; `[sources.families]` can group sources).
- **YouTube matching when selected**: suggestions are compared by name, ISRC and YouTube ID straight away; finding a Last.fm/Deezer/TIDAL suggestion on YouTube Music (and Deezer's ISRC and release date) waits until the candidate is picked for the pool. Same rules as §4.1 step 4. A duplicate found only then (same ISRC or YouTube video) is merged into the other candidate with its provenance, and that one takes the slot; a failed match is replaced by the next candidate from the same list. Why: ~5,000 suggestions for 10 seeds would take over an hour of searches up front; the candidate phase now takes ~1.5 min for 4 seeds.
- **Each seed has its own share of the pool** (added 2026-10-30, at my request): by seed average with the one-third floor, so new or obscure seeds that few sources know aren't crowded out by well-known ones. Test pull: well-known seeds had 48 and 56 multi-source candidates, the two newest 13 and 15. Within a seed's share, its multi-source candidates go in first (most families, then most suggestions, then best list position; beyond the share if needed), then its lists fill the rest by source average. A track suggested from several seeds fills the share that is least full (`pool_seed`, its "owner"). Leftover slots go to the same seed's other lists; only a seed with nothing left passes them on.
- **Refill**: also when fewer than 5 unplayed multi-source tracks remain, once 10 plays have passed since the last pull.
- Artist-level agreement credit off (`artist_agreement_credit = 0`).
- Test pull with the v2 seeds on a scratch copy (fresh run, nothing shown): 1,136 candidates; 71 suggested by 2+ sources (v2's first pull: 32 multi-source of 446). TIDAL covered 2 of 4 seeds (99 each), "You might also like" 2 of 4. Fill test with a 24-track pool: 6 per seed, including the two newest.
- **For milestone 3 (agreed 2026-10-30): scoring within each seed's pool.** Each track has an owner seed (the share it fills, least-full rule). Source-dependent components (agreement, source average) are normalised within the owner's pool — a pool where everything ties gets full credit — so a seed whose sources rarely overlap can still have tracks at the top of those components. A track suggested from several seeds is scored in each of their pools and keeps its best. New component: similarity to the owner seed (within its pool). Taste components (audio taste model, dislike, year, BPM, diversity) stay normalised across the whole pool. **Self-balancing, no blend setting:** the seed average drives both the seed's pool share and the track score (seed component), so if multi-source tracks really are better, their seeds' averages rise and they get more pool and score; a one-source seed whose tracks disappoint drops in both, one whose tracks please rises. Re-weighting gets a minimum weight for the seed component (agreed 2026-10-30), so the balancing can't be switched off by re-weighting.

**As built — milestone 3 (2026-10-30)**
- **Three buttons** Off · Close · Add (`[ratings]`): Off → negative set; Close → positive set at weight 0.5, not saved; Add → positive weight 2, seed, saved. Averages on 1–3 with the prior at 2. Add plays to the end; Off and Close skip (`play_to_end`). The player builds its buttons from the config.
- **MAEST replaces CLAP** for every embedding (`[embedding]`: Hugging Face weights at a pinned revision, layer 9, 30 s chunks, ~0.6 s/track). Its fingerprints match the taste test's exactly (cosine 1.000). Plain cosine similarity used: on v1/v2, closeness to the three nearest liked tracks by other artists tracked ratings at +0.27 / +0.44 (CLAP's audio match: +0.12 / −0.11); centring or whitening the fingerprints made it worse. `music-hunter reembed` re-analyses tracks after a model change. Section consistency left out: weak for MAEST (+0.09).
- **Scoring within each seed's pool** as agreed (per_seed_components = agreement, source average, seed_match). Each play logs the seed pool it was scored in (`plays.scored_for_seed`).
- **New component `seed_match`**: MAEST similarity to the seed whose pool the track is scored in.
- **Guardrails**: no weight above 0.35; floors audio match 0.15, agreement 0.15, seed average 0.10; penalties capped as before.
- **Personal model from 20 learning ratings** (was 50; at least 3 Offs and 3 Close/Add), at my request. Threshold checked offline on v1/v2 (trained on the first N plays, tested on the rest; old ratings not used in v3): on v2 it helped from ~10–15 ratings and from 20 was worse than nothing in 1% of random draws; v1 needed ~25–30. Its value is the predicted rating on the rating scale, not stretched across the pool, so an early, uncertain model barely moves the ranking.
- **Close as a mild like, checked on v2** (2026-10-30; v3's Close = v2's Close + On target): both sound like Adds — closer to the nearest Adds than to the nearest Offs for 70% (Close) and 80% (On target) of tracks, as much as Adds themselves (72%), while Offs lean slightly away. Counting them as likes helped rank the rest (Add-vs-Off AUC 0.825 with Adds only → 0.835–0.842); weight 0.5 vs 1 made no clear difference, so Close stays at 0.5 (Add 2).
- **Descriptions ("female vocals", "energetic"…)**: MAEST already carries what MuQ-MuLan's descriptions measure — predicting the 35 description scores from MAEST on held-out artists gives median r 0.80 (chill 0.89, heavy bassline 0.86, female vocals 0.84, bright/euphoric 0.81; weakest "build-up and drop" 0.50, a structure that a track-average hides). Not added: no gain in prediction, only readability (§14 items 6–7, later).
- **Results: ratings by number of sources** (1, 2, 3+; all / ranked / control), to check after the run whether tracks only one source knew were treated fairly.
- **Initial weights**: audio match 0.30, agreement 0.30, personal model 0.25 (from 20 learning ratings), seed average 0.15, seed match 0.15, year 0.15, source average 0.10, BPM 0.10, dislike −0.20, diversity −0.10.

**As built — milestone 4 (2026-10-30)**
- **Preloading** (`[ui] preload_next`): v2's slow starts (up to 3.7 s) came after the track was chosen (choosing took ~4 ms) — fetching and buffering the file. While a track plays, the player asks for the track most likely to play next and fetches its audio into memory; if that's the one chosen, it starts from memory. The control decision for the next play (and a control's random track) is drawn ahead and kept until that play starts — it never depends on ratings, so this changes nothing about controls; a learning pick can still change with the rating in between (then the normal load is used). Each start logs whether it came from memory.
- **Tempo locked for the run** (`[tempo]`: locked, target_bpm, match_tempo): the player shows the settings but can't change them (the server refuses, 409); change them in config between runs. With locked = false, the v2 player controls work as before.
- **Stalls** logged only from 250 ms (`[ui] stall_log_ms`; v2: 3 of 519 logged stalls were longer).
- **Same-artist seeds count once per source** for agreement (agreed 2026-10-30): Last.fm and Deezer suggest by artist, so seeds by one artist get identical lists — three X CLUB. seeds would otherwise make one Last.fm + one Deezer suggestion look like six. Agreement between seeds by different artists still counts. Each artist's Last.fm and Deezer lists are fetched once per pull. Pool shares stay per seed (X CLUB. and 6 SENSE get three shares each) — to be judged from the run.
- **Seeds chosen** (2026-10-30, not added yet): Spotify playlist `7pc3JHwWspD5BnZMRoX4PG`, 12 tracks, all readable — WOLTERS, X CLUB. ×3, Jas Van Houten, Basement Jaxx & Gorgon City, 6 SENSE ×3, TWOFACED ×2, Clouds.

**As built — milestone 5 (2026-10-30)**
- **Secret control rate**: drawn once per run, uniform between 1 in 10 and 1 in 4 (`[controls]`), stored in the database and never shown or logged as an event; each play is a control independently at that rate.
- **The run ends** after 25 rated controls: the player shows "Run complete" with a link to the results, and no new plays start.
- **Sealed results**: until then the results page and `report` show only the number of ratings. The "shown" event no longer records control status or rank (it's in the plays table), so the text log can't reveal controls either.
- **Control ratings count for learning**: likes and dislikes, seed and source credit, the personal model, re-weighting's schedule and history; a control Added still never becomes a seed.

**As built — milestone 6 (2026-10-30)**
- **Results page** (after the run): ranked vs control, Add rate, controls by rank, ratings by number of sources, **ratings by seed pool** (which seeds' pools produced the favourites), the secret control rate revealed, tempo lock, weight history. `report`: per pull, multi-source counts, seed shares (target/unplayed), seeds not covered by a source, tracks unusable at selection.
- **Simulated session** (pytest, ~110 plays through the web API): blind throughout, results sealed until the run ends, then unsealed; re-weighting on schedule within the guardrails.
- **Fixed from the simulation**: (1) the multi-source refill now needs a seed no pull has queried yet (a new Add) — all multi-source tracks already go in first, so pulling again with the same seeds found nothing and repeated every ~10 plays; (2) seed and source averages now fade by rating count (a rating 30 ratings back counts half, `decay_half_life_ratings`) instead of by generation, since refills can come every few dozen plays.
- **Fresh run started**: v2 archived to `runs/v2/` (moved, not deleted). Seeds added from the playlist: 10 of 12 — Jump N' Shout (Gorgon City Extended Mix, 5:39) isn't on YouTube Music in that version (only the 3:58 Gorgon City Remix edit); 6 SENSE – No Joke ! isn't there under 6 SENSE. Seed BPMs 139–147 (TWOFACED – Love Again read 93.6, corrected to 140.5 by the 3:2 tie-break).

**Double-count fixes before the first rating (2026-10-30, agreed):** (1) audio_match compares with tracks liked in this run (Close/Add) only — the seeds were in its likes at double weight while seed_match also measured similarity to the seed (the two correlated 0.56 on the first pool); (2) each track is scored only in its owner's pool (the seed whose share it fills, as I first proposed), not in every suggesting seed's pool keeping the best — agreement already counts several seeds, and every early pool track came from 3–7 seeds. Left as they are: seed and source averages steering both pool shares and scores (intended); the three audio components learning from the same ratings (re-weighting arbitrates); the same source suggesting from several seeds counting as agreement (revisit if needed). The first pull and pool were cleared and refilled under these rules (seeds kept).

**Change during the first v3 pull (2026-10-30, my decision): agreed tracks land naturally.** Reason: multi-source tracks were being pushed twice — forced into the pool first, then scored up for agreement. The first pull (10 seeds) gave 1,778 candidates in 3 minutes, 273 suggested by 2+ sources (v2: 32) — more than every seed's whole share (30–136 per seed vs 25 places), so the pool would have been entirely multi-source, and controls (random pool tracks) too, so ranked vs control would no longer measure the lift over the sources. (My original concern was different and is handled by scoring within each seed's pool: a seed only one or two sources cover — here Jas Van Houten: Last.fm and YouTube, Deezer 2 tracks, no TIDAL — has fewer chances of multi-source tracks.) Now each share is filled from the top of the seed's source lists by quota (as before milestone 2); agreed tracks sit high in those lists anyway (simulated on this pull: 41% of the pool multi-source, 33 tracks from 3+ sources), and agreement still counts in the score (weight 0.30, cap 0.35). The first 22 pool tracks were added under the old rule.

**Phone access over Tailscale (2026-10-30, at my request; was out of scope in §1):** the server also listens on the PC's Tailscale address (`[ui] extra_hosts`), so my phone's browser can open http://100.68.159.86:8765 over Tailscale. Only my tailnet devices can reach it; nothing opens on the home network. (`tailscale serve` would also work but needs Serve enabled on the tailnet and root.) A phone app / home-screen install with lock-screen controls and screen-off playback is a possible later step.

**Unrated counts as Close (2026-10-30, at my request):** a track skipped (N) or played to the end without a rating is recorded as Close (`[ratings] unrated_counts_as`), marked `auto_rated` so it can be separated in the analysis; it can still be changed like any most-recent rating. An unrated track that ends now moves on to the next one. (Replaces §3's "skipping without rating has no learning effect".) Close now plays to the end like Add (`play_to_end = [2, 3]`); only Off skips straight away.

### 16.7 v3 result and deep dive across all runs (2026-10-30)
**v3 run** (ended early at 74 plays; secret control rate 1 in 6.3, only 9 controls): ranked 1.86 vs control 1.78 (+0.08, 95% −0.57 to +0.74); plays 1–50 ranked 2.02 / Add 29%, plays 51–74 1.57 / Add 0%. No refill ran all run (one pull of 250), so the 12 Adds never became active seeds. Other models wouldn't have done better: trained on v1+v2 and tested on v3, every model scored +0.1 to +0.2 (MAEST +0.14; MuQ-MuLan, v1/v2's best, fell to ~0).

**Deep dive** (`tastetest/deepdive.py`, all 267 rated plays, results in `runs/taste-test/deepdive*.json`):
- **Across runs, ranked vs control: v1 +0.24, v2 +0.19, v3 +0.08** — consistently small. By half: first halves +0.09 percentile (ranked ahead), second halves +0.01 — the ranking's advantage is used up as it plays the pool's best first (ranked liked rate by blocks of 15: v2 67→33%, v3 27→0%), while random controls stay flat (listener standards don't drift).
- **Closeness to the seeds' sound (MAEST) is the one strong signal, and it works as a gate, not a dial**: all plays below ~0.75 (3 nearest seeds) liked 10%, above 31%; in v2 (whose CLAP ranking didn't narrow it) 14% below vs ~58% above, flat above. Controls agree (rank percentile 0.38 below 0.78, 0.53 above). v3 played tracks were almost all above the gate already (spread 0.026 vs 0.061 in the pool), so it had nothing left to do there.
- **Inside the gate**, pooled across runs, only small signals remain: closeness to seeds +0.19, to earlier likes +0.18, number of sources +0.17 (v1/v2 strong, v3 nil, controls nil), seed artist +0.12.
- **No signal**: position in the source's list, which source, which seed (no seed differs significantly), tracks from Added-track seeds (pooled −0.02; v2 59% vs 44%), artist history/familiarity, duration, BPM, release year (weak +0.14), novelty across runs.
- **Wrong way**: closeness to earlier dislikes predicts *higher* ratings in every run (pooled +0.21) — dislikes sit in the same sound region as likes, so the dislike penalty works against me; the diversity penalty also ran the wrong way. The personal model never helped (v1 −0.18, v2 −0.26, v3 −0.07).
- **Rating consistency**: 19 songs were rated in two or three runs; their rating percentiles agree only ρ +0.21 (liked/not 78%), about the level of the best predictors — single ratings are noisy and context-dependent, which caps what any ranking can show in ~100 plays.
- **Power**: 48 controls over three runs (per-run 95% ranges ±0.6); realistic effects (~0.1–0.3) can't be confirmed run by run.
- **Avoiding the worst works better than finding the best** (checked 2026-10-30): an equal-weight mix of closeness to seeds, closeness to earlier likes and number of sources (no fitted weights) picks out Offs better than likes (v2 AUC Off 0.85 vs liked 0.73 for sources alone; v3 likes-closeness: Off 0.66 vs liked 0.43). Cutting the bottom 10% of each run's plays: 85% of those were Off (base 43%), 3 of 74 likes lost; bottom 20%: 66–72% Off, 6–8 likes lost; kept tracks Off 43% → 36–38%, liked 28% → 31–32%. Strong in v1/v2 (bottom 25%: 81% and 70% Off), weak in v3 (42% vs 35%) because v3's ranking had already kept the bottom of its pool from being played. Closeness to *dislikes* is no use for this (it predicts higher ratings).
- **Close counted as Off? No** (checked 2026-10-31): in v3 no signal separates Off, Close and Add from each other among played tracks (pairwise AUC 0.45–0.59; all three sit equally near the Adds and Offs by sound); replaying "sounds like my likes" with Adds only as likes was slightly worse than Close + Add (ρ +0.15 vs +0.18; spotting Offs AUC 0.65 vs 0.70), and subtracting closeness to Off+Close wiped out Off detection (0.56). In v2, Close sat with the positives by sound (Off vs Close AUC 0.77; Close vs On target 0.55). Counting Close as Off would put right-sound tracks into the negative set and teach the system to avoid the right area. In v3, closeness to earlier likes slightly *anti*-predicted Adds (AUC 0.41–0.43) — possibly a taste for something fresh within the area; too weak to act on.
- **Finer rating scale? Little gain** (checked 2026-10-31): collapsing v1's 5 stars and v2's 4 levels to Off/Close/Add barely changes anything — v1 signals +0.37/+0.33/+0.26 (5 levels) vs +0.31/+0.28/+0.22 (3), the small gain coming from splitting the bottom (1 vs 2); v2 identical or better with 3 levels (+0.47 vs +0.49 for closeness to seeds). Ranked vs control effect sizes the same (v1 +0.21 vs +0.19 SD, v2 +0.17 vs +0.22 SD). **More ratings matter far more**: confirming a 0.2 SD difference needs ~390 ratings per group (0.3 SD: 175; 0.5 SD: 63) — no ~100-play run with 9–21 controls could have shown the effects seen.
- **Previews**: one 30 s chunk (a Deezer preview's length) tracks full-track seed closeness at ρ 0.66–0.74 within the (already narrow) pool — usable as a gate over all ~1,700 candidates of a pull instead of the 250 downloaded.

### 16.5 Milestones (build in order, stop after each for my check)
1. **Taste-model test** (16.1) — offline comparison of audio models and analyses on my ~190 ratings. Check: results table and a recommendation on CLAP. **Done 2026-10-30: MuQ-MuLan replaces CLAP** (MAEST check pending).
2. **Sources and pool** — TIDAL (ISRC lookup, similar tracks), YouTube Music related, deeper lists (~100 per source per seed), multi-source-first filling, refill when multi-source tracks run out, pool 250, artist-level credit off. Check: candidate list with four-plus sources and overlap counts.
3. **Ratings and scoring** — three buttons; weight cap (≤ 0.35) and higher agreement floor; **MuQ-MuLan replaces CLAP** (audio match, text-description scores, section consistency; personal model retrained on MuQ-MuLan features or dropped); release year and BPM kept.
4. **Player and monitoring** — preload the next track, tempo settings locked during a run, stall logging threshold.
5. **Controls and sealed results** (16.6) — secret per-run control rate, run ends at 25 rated controls, results sealed until then, control ratings count for learning.
6. **Results, report, simulated session** — then a fresh run with 8–10 new seeds. **Done 2026-10-30; fresh run set up with 10 seeds.**

Decided 2026-10-29: no artist-preference factor (evidence weak: v1 +0.14, v2 −0.04). Controls: see 16.6.

### 16.6 Controls (decided 2026-10-29)
- **Still a random pool track**: a random pick from the same pool the ranking chooses from (what the sources alone would give), so ranked vs control measures exactly what the ranking adds. Median or lowest-ranked controls were considered and rejected (median can't show whether scores mean anything; lowest exaggerates the difference and serves the worst picks).
- **How many**: aim for **25 rated controls** per run. With v2's rating spread (SD 1.11) and ~100 ranked plays, that is enough to confirm a clear difference (about +0.5 on the 1–4 scale; +0.7 needs ~11, +0.3 ~110).
- **Secret rate**: at the start of each run the system draws its own control rate from a range (default 1 in 4 to 1 in 10), stores it, and never shows it; each play is still a control independently at that rate.
- **The run ends by itself** when 25 controls have been rated: the player shows "run complete".
- **Sealed results**: until the run completes, the results page and `report` show only that a run is in progress — no ranked/control counts or comparisons, which would reveal the rate. Everything is revealed at completion (and stays logged throughout).
- **Control ratings count for learning**: likes and dislikes (positive/negative sets), seed and source credit, and the personal model (if kept). Controls still never become seeds, so random picks don't steer the sources. This doesn't bias the comparison, because which plays are controls stays random.

---

## 17. Version 4 plan (draft, revised 2026-10-31 — not built)

Based on the deep dive (§16.7) and my review on 2026-10-31. Where v4 differs from earlier sections, this section wins.

### 17.0 Idea in one line
Score every analysed track as before, **veto the bottom** (clearly wrong sound, little support), **pick at random from what passes**, keep the playable pool topped up, and test the veto by slipping in vetoed tracks now and then. Telling the right sound area from the wrong one is learnable; telling good from great within it isn't (with these signals and ratings), so v4 stops trying to rank the top.

### 17.1 Candidates and analysis (as v3)
- Same five sources (Last.fm, YouTube radio and "You might also like", Deezer, TIDAL), ~100 suggestions per source per seed, same-artist seeds counted once per source, YouTube matching when picked, per-seed shares when filling.
- **Full-track analysis, no previews** (decided 2026-10-31): a 30 s preview agreed with the full track only ρ 0.66–0.74 (half of the top 50 matched), and with a veto of ~10% the usual supply is enough. Previews only come back if a much harder cut ever needs a bigger supply.

### 17.2 Analysed vs playable — a rotating pool
- **Analysed** = every candidate downloaded and analysed this run. Its fingerprint, metadata, provenance and scores are kept for good (small).
- **Playable pool** = analysed tracks currently passing the veto, with their audio kept. Target size: **250 playable** (config). When tracks are vetoed or played, the pool is **topped up** with the next candidates from the seeds' source lists (each pull gives ~1,700; plenty of supply).
- **Vetoed tracks lose their audio, not their fingerprint.** If one comes back — suggested again by a later pull (its provenance and agreement grow) or because its score rises past the line — it is re-scored instantly from the stored fingerprint; audio is downloaded again only if it becomes playable or is chosen as an injected control (rare; ~8 s per track).
- New suggestions merge into any analysed track (playable, vetoed or played), not only the playable pool.

### 17.3 Scoring — all components kept and logged
- Every analysed track is re-scored after every rating (milliseconds; fingerprints are stored): all v3 components (closeness to seeds, closeness to likes, closeness to Offs, closeness to recent plays, agreement / number of sources, release year, BPM, seed and source averages, personal model) are **computed and logged** for every play, so the run collects data on all of them.
- **Directions fixed by the data**: closeness to Offs and to recent plays measure "in the right area" — a *low* value warns of an Off — so they count positively (they ran backwards as penalties). Spotting Offs (AUC): v3 likes-closeness 0.68, recent-plays closeness 0.66, seed closeness 0.64, Offs closeness 0.60, year 0.57; v2 year 0.65, agreement 0.61, BPM 0.60.
- **Veto score**: starts as the equal-weight mean of the strongest three — closeness to the 3 nearest seeds (original seeds and Adds), closeness to earlier likes (Close 0.5, Add 2; left out until there are likes), number of sources — each standardised. Weights can be adjusted during the run towards whatever spots Offs best (a steadier target than "find the best"); every change is recorded (17.6).

### 17.4 Veto rule
- **Score-based, not a fixed share** (decided 2026-10-31): veto a track when its veto score is more than **1.25 SD below the average** (measured robustly: median and a spread that ignores the long low tail), plus a backstop for tracks clearly out of the seeds' sound area (seed closeness far below the run's typical range). Checked on v1–v3: the 1.25 SD cut removed 8–12% depending on the run, 81% of them Offs, 3 of 74 likes lost — as clean as a fixed 10%, and it adapts to how big each pool's bad tail is.
- **Anchored statistics**: average and spread are taken over **all tracks analysed this run** (played, vetoed and playable), so the line moves only when new candidates arrive — not downwards as the best tracks are played out of the pool.
- **Two lines, no flip-flopping**: vetoed below 1.25 SD; a vetoed track becomes playable again only above 1.0 SD.
- **Every rule is logged for every play**: fixed 10% / 20%, SD cuts at 1.0 / 1.25 / 1.5, seed closeness under the absolute 0.75, and whether the track was playable or vetoed at that moment — so the run shows which rule would have worked best.
- Closeness to Offs is never used to *veto* (it predicts higher ratings).

### 17.5 Picking and checking the veto
- **Pick**: a random playable track. No ranking, so nothing is used up early (no burst-then-fade); the pool's per-seed shares keep seeds balanced (the seeds themselves are never played).
- **Injected vetoed tracks** (replace v3's controls): at a secret rate drawn per run (between 1 in 6 and 1 in 10), the next play is a random **vetoed** track instead. Compared with the normal picks, this tests the veto directly (expected gap ~66% vs ~38% Off; about 30 injected plays show it); "what the sources alone give" is reconstructed as the playable and vetoed shares mixed in proportion. The decision is drawn ahead (preloading); the audio is fetched just in time. Injected tracks' ratings count for learning like any other; they never become seeds.
- Blind as before: nothing shows whether a play was injected.

### 17.6 One long run, not fixed
- One long run; settings may change along the way if the data says so. Each play records the **settings version** it ran under (veto rule, weights, cut), so changes can be separated in the analysis.
- Results stay sealed while the run is going (injection counts would hint at the rate); unsealing ends the run, as in v3.

### 17.7 Reason tags (optional, under the rating)
- After rating, a row of optional chips under the buttons; tap any that fit, or none:
  - Off / Close: `vocals` · `lyrics` · `too repetitive` · `wrong energy` · `the drop` · `bassline` · `too long` · `know it already`
  - Add: `great vocals` · `groove` · `energy`
- Several allowed; changeable with the most recent rating. **Logged only** — no effect on the veto or picks. After the run: which reasons dominate, and whether Offs for non-sound reasons (lyrics, know it already) should stay out of the sound-based learning.

### 17.8 Refill from Adds — open
Every Add still becomes a seed (as v3). How and when Add-seeds are pulled for, and how they're weighted, is **to be discussed** (I have more ideas). Until then: a pull runs when 5 seeds haven't been pulled for yet, or 15 plays after the last pull if at least one has.

### 17.9 Kept from v3
Three buttons (Off · Close · Add), Close as a mild like, unrated/skipped counts as Close, Close and Add play to the end, MAEST fingerprints, blind listening (names and artwork after rating), preloading the next track, tempo locked unless changed in config, volume, phone access over Tailscale, sealed results. Removed: re-weighting towards "rank the best" (replaced by veto-weight adjustment, 17.3), top-first picking.

### 17.10 Results
- Off / Close / Add rates **over the whole run** (all plays) — the aim is a steady rate, no fade.
- Normal picks vs injected vetoed tracks (Off rate, Add rate, average), and the reconstructed "sources alone" baseline.
- Every logged veto rule scored after the fact: which would have removed the most Offs for the fewest likes.
- Ratings by number of sources and by seed; reason tags by rating; pool rotation (vetoed, returned, re-downloaded); refills and Add-seeds; settings versions.

### 17.12 As built — milestones 2 + 3 (2026-10-31; built together, since the rotating pool is driven by the veto)
- **Veto** (`veto.py`, `[veto]`): three components — closeness to the 3 nearest seeds (original seeds and Adds), to the 3 nearest likes (Close 0.5, Add 2), number of sources — each a robust z-score (median, MAD) over every track analysed this run; veto score = their weighted mean, standardised the same way. Vetoed below −1.25, back above −1.0, backstop at −2.5 on seed closeness; no vetoes until 40 tracks are analysed. Number of sources has a median of 1, so single-source tracks score 0 on it (no penalty for obscure tracks; only agreement adds). Re-scored after every rating and every 10 new tracks while filling.
- **Rotating pool**: vetoed tracks lose their audio, keep their fingerprint; the playable pool is topped up to 250 in rounds; tracks that become playable again get their audio back in the background (`restore` job); unused candidates stay available for later top-ups (v3's "not selected" leftovers are reused). Seed shares are equal (`share_by_seed_average = false`).
- **Picking**: a random playable track, drawn ahead for preloading (so the preloaded track now always matches). Controls are random vetoed tracks (built here; rate between 1 in 10 and 1 in 6), their audio fetched just in time. Re-weighting is off (`[reweighting] enabled = false`).
- **Logged per play**: all components as before, plus the veto details (score, z-scores, every rule's verdict, state) and the settings version (`settings_versions`).
- **Refill placeholder** (§17.8): a pull when 5 seeds haven't been queried yet, or one has and 15 plays have passed; queried seeds are remembered even if their sources returned nothing (found in simulation: a seed with no results looked "waiting" for ever and triggered a pull after every rating).
- `music-hunter veto` shows what's playable, what's vetoed and why; `music-hunter end-run` ends a run and unseals the results.
- **Checked on real data** (copy of the v3 database): of 251 analysed tracks, 34 vetoed (14%) — the worst clearly outside the area (Lethian Dreams ×4, seed closeness 0.49–0.58; a Nia Archives edit; Makina Time). Replaying the 74 v3 ratings: 56 ratings moved nothing across the line, 13 moved 1–2, the most was 6 (20 vetoed, 16 returned over the run). Only 2 of v3's played tracks would have been vetoed — v3's ranking had already kept away from that region. A real top-up of 8: 10 downloaded in one round, 2 of them vetoed (audio deleted), playable pool exactly at target.

### 17.13 As built — milestone 4 (2026-10-31)
- **Injected vetoed tracks** (built with 17.12): random vetoed track at a secret rate between 1 in 10 and 1 in 6 (`[controls]`), audio fetched just in time (in the background while the previous track plays, or on the spot if needed); no automatic end (`target = 0`) — `music-hunter end-run` ends the run and unseals.
- **Reason tags** (`[ratings.tags]`): optional chips in the "Last rated" panel (not under the rating buttons: an Off skips straight to the next track, which would hide them), labelled "Why? (optional)"; tap to add or remove, several allowed, only on the most recent rating; changing the rating keeps only the tags that still fit. Stored with the play (`plays.tags`); no effect on the veto or picks.

### 17.14 As built — milestone 5 (2026-10-31)
- **Results page** (after `end-run`): headline "picks minus vetoed tracks slipped in"; "Does the veto remove worse tracks?" — Off rate, Add rate and average for picks, for vetoed tracks slipped in, and for the reconstructed "sources alone" (the two mixed in the proportion the pool was split at each play — each play now logs that split); "Every veto rule, after the fact" — for each logged rule, the share of rated plays it would have cut, Off rate in the cut, Adds lost, Off rate in what's kept; Off and Add rates over the run (last 25 picks); reason tags by rating; pool rotation (vetoed / returned, audio re-fetched, pulls, Adds pulled as seeds) and settings versions. v3's "does a higher score mean a better track" is gone (picks aren't ranked). Ratings by number of sources and by seed pool stay.
- **`report`**: a VETO section — analysed / playable / vetoed now, vetoed and returned over the run, audio re-fetched, settings versions, reason-tag counts.
- **Simulated session** (pytest, 112 plays through the web API): veto active, picks random playable tracks, vetoed tracks slipped in at the secret rate, pool topped up and audio restored, refills for Adds, tags logged, results sealed until the run ends then unsealed with every v4 measure.

### 17.11 Milestones (build in order, stop after each for my check)
1. **Fresh run setup** — archive v3 to `runs/v3/`, seeds (to decide: the same 10 or new), v4 config.
2. **Analysed vs playable pool** — fingerprints kept for good, audio dropped for vetoed tracks, top-ups from the source lists, merging into any analysed track, re-download on return. Check: a real pull rotates correctly.
3. **Scoring and veto** — all components logged with corrected directions, veto score, anchored robust SD cut with two lines, every rule logged, settings versions; random picking. Check: veto shares and what gets vetoed on a real pull.
4. **Injected vetoed tracks and reason tags** — secret injection rate, just-in-time audio, tag chips in the player. Check: listen-and-rate loop with tags, nothing revealed.
5. **Results and report** — v4 measures (17.10), simulated end-to-end session. Then the run.
   **Milestones 2–5 built 2026-10-31.** Milestone 1 (archive v3, seeds) and the Add-seed rules (17.8) wait for my decision.

---

## 18. Version 5 plan (2026-10-31 — milestones 1–4 built)

From my notes on 2026-10-31. v5 turns the test rig into an everyday app: a playlist library with stars, "Find music" per playlist, and an always-on home on the Raspberry Pi, with the PC woken for heavy work. Where v5 differs from earlier sections, this section wins; v4's listening interface (veto, random picks, vetoed tracks slipped in, tags, Off · Close · Add) carries over inside "Find music". Now in scope (were out in §1): multiple playlists, the Pi, Wake-on-LAN.

### 18.1 Playlists
- **Home page**: my playlists (name, tracks, seeds, Find music status). **Import** by pasting a Spotify playlist link (public page, as §15.1): tracks matched on YouTube Music, downloaded and analysed, with progress. **Create** an empty playlist.
- **Inside a playlist**: an ordinary player — play, pause, skip, **shuffle** — not blind (names and artwork shown). Tracks **ordered by stars** (5 at the top, unstarred last). **Remove** a track; **add a track to another playlist** (no search needed). A track can be in several playlists (one shared library).
- **Stars (1–5)**: a starred track is a seed; no stars = not a seed. Changing stars takes effect at the next pull.
- **"Added — waiting for stars"**: a section at the top of the playlist with tracks Added in Find music, until I star or remove them.

### 18.2 Find music (one playlist at a time; each has its own seeds, pool and session)
- **First click**: a confirmation ("Find music for <playlist>? This downloads and analyses its seeds and finds candidates — the PC will be woken; about N minutes"), then a **progress bar** (waking the PC → seeds → candidates → pool x of 250) ending in **Ready**.
- **Clicking again** (and every time after): the current listening interface for that playlist — random picks that passed the veto, vetoed tracks slipped in at a secret rate, Off · Close · Add, optional reason tags. Pool top-ups and refills run in the background as in v4.
- **Add** puts the track in the playlist's "Added — waiting for stars" section (and saves the audio to the library). Off and Close don't touch the playlist; they teach the veto as in v4.

### 18.3 Seeds and weighting
- Seeds = the playlist's starred tracks. A seed's **weight = its stars × its seed score**; pool shares follow the weights (with a floor, so a 1-star seed still contributes). **Seed score** = the decayed average rating of the tracks it suggested (prior at the middle, as §5, decay by rating count), so seeds whose suggestions do well grow and those that disappoint shrink.
- **Adds**: pulled for **once, straight away** (their suggestions join the pool, weighted as a 3-star seed for that pull), then treated as unrated — not a seed for later pulls — until I star them. (Default weight configurable.)

### 18.4 Pi and PC split
- **Pi (always on; Home Assistant add-on)**: the web app, database, playlists and ratings, the audio library (playlists and the Find music pools), playback, Spotify link reading, the job queue, and small downloads (bringing back a vetoed track's audio). Reached over Tailscale on any device (and in Home Assistant's sidebar).
- **PC (woken when there's work)**: everything heavy — YouTube matching and downloads in bulk, loudness, BPM, MAEST analysis (GPU), candidate pulls. When jobs are queued (import, Find music, refills, top-ups, an Add's pull), the Pi asks Home Assistant to wake the PC (its Wake-on-LAN integration). A worker on the PC starts on boot, takes the queued jobs, sends audio, fingerprints and metadata back to the Pi, and **disconnects when the queue is empty** — the PC then turns itself off.
- While the PC is off, everything already downloaded keeps working: playlists play, Find music serves the existing pool; new work waits for the next wake.
- Pi: Raspberry Pi 5, 4 GB RAM, 468 GB SSD (~400 GB free; shared with Home Assistant OS). Audio for a 250-track pool plus playlists is a few GB.

### 18.5 Week-long trial
- One week of everyday use for more data. Per playlist session: v4's measures (picks vs vetoed tracks slipped in, every veto rule after the fact, Off/Add over time, tags), plus how seed weights moved and which seeds produced the Adds. Results sealed until the trial ends (`end-run`).

### 18.6 Milestones (build in order, stop after each for my check)
1. **Library and playlists** (on the PC first, usable over Tailscale) — archive v3 to `runs/v3/`; data model (playlists, playlist tracks with stars and status, one shared library); home page; Spotify import with progress; stars and ordering; remove; add to another playlist; playlist playback with shuffle.
2. **Find music per playlist** — sessions per playlist (own seeds, pool, candidates, plays); first-click confirmation and progress bar, then the listening interface; seeds from stars with stars × seed score weighting; Adds into "Added — waiting for stars" with their one-off pull.
3. **Pi app and PC worker** — job queue and worker API; the PC worker (starts on boot, disconnects when done); audio and fingerprint transfer; waking the PC through Home Assistant.
4. **Home Assistant add-on** — packaging for the Pi, install steps for me, checked on my Pi.
5. **Trial week.**

### 18.7 As built — milestone 1 (2026-10-31)
- v3 archived to `runs/v3/`; fresh database. Playlist audio lives in `library/` (covers in `library/covers/`).
- **Data**: `playlists`, `playlist_tracks` (stars 1–5 or none; status member / added), one shared library (`tracks`, origin `library`), and a `jobs` queue (imports now; Find music and the PC worker later). A job cut off by a restart is queued again (imports are re-runnable: tracks already in the library — same Spotify ID or YouTube video — are reused, not downloaded again).
- **Home page** (`/`): playlists as cards (cover, tracks, seeds, waiting for stars, import progress); import from a Spotify link (name, description and cover read from the page); create an empty playlist.
- **Playlist page** (`/playlist/<id>`): Added — waiting for stars on top, then tracks by stars (5 first, unstarred last); tap a star to set it, tap it again to clear; ⋯ menu to add to another playlist or remove; rename, delete; a normal player (names and artwork shown, loudness-normalised) with play, previous / next, seek and shuffle; import progress and the tracks that couldn't be imported, with reasons. Removing a track deletes its audio only once it's in no playlist (the fingerprint stays).
- The Results page is hidden from the navigation (still at `/results`); the listening interface is at `/listen` until it becomes per-playlist in milestone 2.
- **Checked**: importing "pump" from Spotify through the server: 10 of 12 tracks added with artwork, BPM and loudness (Jump N' Shout and No Joke ! aren't on YouTube Music, as in v3), 47 MB; pages reachable over Tailscale.

### 18.8 As built — milestone 2 (2026-10-31)
- **One session per playlist**: each playlist's Find music has its own database (`data/sessions/playlist-<id>.db`) with its own seeds, candidates, pool, plays, veto and secret control rate; the v4 machinery runs in it unchanged. The library (`data/music_hunter.db`) holds playlists and the shared audio library. Pool audio stays in `cache/` until Added.
- **Find music button** (playlist page): disabled until a track is starred. First click → confirmation → progress bar ("x of 20 tracks ready to start", with the current step: candidates, downloading and analysing) → **Ready · N tracks waiting**; clicking then opens the listening interface at `/find/<id>` (same player as v4, with links back to the playlist). If a job fails, the button offers "Try again". Pool top-ups and refills keep running in the background; at server start every started session picks up where it left off.
- **Seeds from stars**: the playlist's starred tracks are copied into the session (track, fingerprint, stars) when Find music starts and again at the start of every pull, so star changes take effect at the next pull; an unstarred track stops being a seed. Pool shares: **stars × (seed average ÷ prior)**, with the one-third floor (`share_by_seed_average` no longer applies in v5 sessions).
- **Adds**: rating Add puts the track in the playlist's **Added — waiting for stars** section (audio moved to `library/`, fingerprint copied to the library); changing the rating away from Add takes it out again (unless it's been starred meanwhile). An Add is pulled for **once, straight away**, as a 3-star seed (`[find] add_seed_stars`); after that pull it isn't a seed unless I star it in the playlist. Off and Close don't touch the playlist.
- **One session's downloads at a time**: if two playlists both have work, the second waits ("Waiting for another playlist's downloads to finish…"), so downloads stay paced.
- **Command line**: `music-hunter --playlist <id> <command>` runs `pool`, `scores`, `veto`, `report`, `end-run`, `pull` etc. on that playlist's session (`pull` carries the stars over first).
- Not yet: waking the PC (milestone 3) — everything runs on the PC for now.

### 18.9 As built — milestone 3 (2026-10-31)
- **What runs where** (changed from 18.4): the PC does only the heavy part — **download, loudness, BPM, MAEST and cover art** for one track at a time. Pulls, YouTube matching, the veto and scoring stay with the app on the Pi: they're web requests and arithmetic, and this way candidates can be found while the PC is off. The Pi also re-downloads a vetoed track's audio itself (no analysis needed).
- `[worker] mode`: **local** (everything on this machine, as before — the setting until the Pi is set up) or **remote** (the Pi). In remote mode, each track to analyse goes into a `work` queue in the library database; the pull/fill or import waits for it, showing "waiting for the PC to wake up" / "waiting for the PC" in its progress line.
- **Waking**: if no worker has asked for work in the last 90 s, the app calls a Home Assistant service (`[worker.wake]`, default `wake_on_lan/send_magic_packet` with the PC's MAC `9c:6b:00:98:16:83`; inside the add-on it uses the Supervisor's token, elsewhere `HA_TOKEN` in `.env`), and again every 10 min while it still hasn't connected.
- **PC worker** (`music-hunter worker`, a systemd user service `music-hunter-worker.service` that starts on boot — linger is on): asks the app for work every 5 s while busy and every 30 s once idle, downloads and analyses, sends the audio, cover, fingerprint and measurements back, and deletes its own copies. A shared `WORKER_TOKEN` (in `.env` on both sides) protects the worker API. A track claimed but not returned within 15 min goes back to the queue; tracks queued before an app restart are dropped (their jobs start again).
- **Turning off**: while working, the worker tries to hold a sleep inhibitor so the PC doesn't sleep mid-job, and releases it after 3 min without work. Found while testing: this PC has no desktop session after a wake (only SSH/systemd sessions), and outside one polkit refuses the inhibitor — so the worker carries on without it, and nothing on the PC itself turns it off afterwards. Decided 2026-10-31: leave it as it is.
- **Data location**: `MUSIC_HUNTER_ROOT` moves the database, audio, `.env` and an optional `config.toml` elsewhere (for the add-on's `/data`, and scratch copies).
- **Checked**: tests for the round trip, failures coming back, the token and stale items; and on a scratch copy, the app in remote mode with the real worker: a Spotify import queued, waited ("waiting for the PC to wake up"), and completed when the worker connected — audio in `library/`, 8-chunk fingerprint, loudness, BPM, cover. The pool filler checked the same way in local mode.

### 18.10 As built — milestone 4 (2026-10-31)
- **Waking**: Home Assistant's Wake-on-LAN **switch** for the PC is turned on (a button entity would be pressed). `[worker.wake] entity_id` (add-on option `wake_entity_id`); left empty, the app looks for it among Home Assistant's switches (name or ID mentioning wake / WoL / LAN) and shows the one it used at `/api/worker` — or the candidates, if there's more than one.
- **Add-on** (`homeassistant/music_hunter/`, with `repository.yaml` at the top of the repository so Home Assistant can add it by URL): Python 3.12 slim (Debian trixie, named in the Dockerfile itself — the first install on the Pi failed because the Supervisor rejects a plain `python:…` image in `build.yaml` and fell back to its Alpine base; `build.yaml` is deprecated anyway) with ffmpeg and Node 20 for yt-dlp; only the app's light dependencies (no torch, transformers or librosa — those stay on the PC); ~1.2 GB image. The source is fetched from GitHub at the tag matching the add-on version (`v0.5.0`). Data in the add-on's `/data` (`MUSIC_HUNTER_ROOT`). Options: worker token, Last.fm key, TIDAL ID/secret, wake switch; written to `/data/.env` and `/data/config.toml` (remote mode, listening on all addresses) at every start. The project's `config.toml` gives the defaults; `/data/config.local.toml` takes my own tweaks; both override key by key, so new settings get their defaults.
- **Reached** on port 8765 (Home Assistant's "Open web UI", or over Tailscale at `http://100.90.89.29:8765`). No sidebar panel: Home Assistant's ingress would need every page to work under a path prefix — later, if wanted.
- **PC**: `[worker] server` now points at the Pi; the worker service is written (`music-hunter-worker.service`) and is enabled once the add-on runs. `worker --once` waits 20 s of empty queue before stopping (jobs queue one track at a time). The app's keep-alive was raised to 75 s (the worker's polls were racing uvicorn's 5 s default).
- **Checked** (image built for amd64 on the PC from the same Dockerfile, run with an options file): the Spotify import of "pump" in the container with the real PC worker — 10 of 12 tracks, the same two missing as before; then Find music with 3 starred seeds — candidates found in the container (Last.fm, YouTube, Deezer), tracks analysed on the PC, Ready at 20 playable, the listening API serving picks. Not yet checked on the Pi itself (aarch64 build, Supervisor token, the real Wake-on-LAN switch).

**Fixes and additions after milestone 4 (2026-10-31)**
- **Covers lost**: the PC worker test deleted the live library's covers — the worker downloaded into the PC's own `cache/` and removed its copies after sending, which were the same files the PC's app used. The worker now works in `cache/worker/`; library tracks keep their covers in `library/art/` (imports and Adds copy them there); the 10 lost covers were fetched again.
- **v3's 15 Adds** (12 picks, 3 controls) copied into "pump" under Added — waiting for stars, with audio, cover and MAEST fingerprint (same model).
- **Same BPM** on the playlist page: a switch and a target (default: the playlist's median BPM) that play every track time-stretched to that tempo, pitch kept, within ±12% (`[bpm] max_stretch`); a track further off plays at its own tempo. A half, double or 2:3 reading of a track's BPM is used when it's the one closest to the target (e.g. Love Again read 93.6 → played as 140.4). Remembered per playlist in the browser; playback only, no effect on Find music.
- The "Star some tracks…" hint under the buttons is gone (the Find music button stays disabled until a track is starred).

### 18.11 Moving to the Pi, and keeping it reachable from the PC (2026-10-31)
- **Moving house**: `music-hunter migrate` packs consistent copies of the library database and every Find music session (SQLite backup, safe while the app runs) plus `library/`, `cache/` and `data/undo/`, and uploads it to the add-on (refused if the Pi already has playlists, unless `--force`). The add-on saves it, restarts, moves its old data to `/data/previous/` and unpacks the bundle; the start script loops, so the app comes straight back. Checked on the local add-on build: 889 MB, pump with its 25 tracks, stars, covers and the Find music session (29 plays, pool, veto) all intact, and the session carried on pulling for its new seeds.
- **Getting data back for analysis**: `music-hunter snapshot` fetches the databases and log into `runs/pi/<time>/`; the usual commands run on it with `MUSIC_HUNTER_ROOT=runs/pi/<time>` (e.g. `--playlist 1 report`).
- **Remote control**: `music-hunter pi log` (the app's log), `pi restart`, `pi update` (installs a new add-on version through Home Assistant; needs `HA_TOKEN` in the PC's `.env`). All app endpoints use the worker token.
- **Updates**: a change is committed, the add-on version bumped in `homeassistant/music_hunter/config.yaml`, tagged `v<version>` and pushed; Home Assistant then offers the update (or `pi update` installs it).
- **Moved 2026-10-31**: add-on 0.5.1 on the Pi (wake switch `switch.my_computer`); 993 MB sent in 91 s over Tailscale; pump (28 tracks, 18 seeds) and its Find music session (36 plays) intact. The PC's own server is stopped; the PC worker runs as a user service and the Pi's first pull (9 new seeds) is being analysed on the PC.

### 18.12 UI tidy-up (2026-10-31, my list)
- Header: MUSIC HUNTER goes home; a back arrow on the playlist page (to the playlists) and in Find music (to its playlist), which also says which playlist it's finding music for. No other navigation links.
- Playlist page: the cover is the play button (no separate play symbol); Rename and Delete moved into a ⋯ menu; "Added — waiting for stars" renamed **New**; only the track count under the name (no seeds / waiting counts, no "Playlist" label); no "Ready · N tracks waiting…" line once Find music is ready.
- Find music: removed the volume slider, the keys hint, the "Now playing" label, the "appear once you rate" line, the placeholder pool texts, the rating buttons' sub-texts and Add's highlight; the last-rated panel shows the title, artist, the rating buttons (the chosen one highlighted) and the optional tags, without the "Last rated", "Change" and "Why?" labels or a separate rating chip.
- **Parked**: playback that carries on across pages (a separate "now playing" queue, tapping it returns to where it's playing) — the playlist player and the Find music player live in different pages, so it needs the app turned into a single page.

### 18.13 Second round (2026-10-31, not released yet)
- Bigger stars; the back arrow sits right of MUSIC HUNTER (the logo doesn't move); Find music's status line no longer shows the download in progress.
- **Same BPM rebuilt**: the browser's pitch-keeping speed change sounded crackly, so the stretched copy is now rendered on the server with ffmpeg's Rubber Band filter (pitch kept, high quality; ~6 s for a 7-minute track on the PC, slower on the Pi) and cached in `cache/tempo/` (`[tempo] cache_mb`). Until it's ready the original plays; the copy then swaps in at the same point in the music. The next track's copy is rendered ahead. Find music now has the same Same BPM switch as the playlist (it was locked by the v3/v4 evaluation rule), sharing the playlist's setting; each play still logs the tempo it was played at.
- **Tags**: five bad (vocals, lyrics, boring, wrong energy, harsh sound) and five good (great vocals, groove, melody, energy, the drop); Off gets the bad ones, Add the good ones, Close both.
- Playlist page: tapping anywhere on a track's row plays it (not the stars or ⋯); Shuffle is a mode — it starts playing only when nothing is playing yet, and its button (and the player's shuffle icon) is filled while on; Play keeps the shuffle setting; the "couldn't be imported" list sits at the bottom. A volume slider on computers only (hidden on touch screens), on the playlist player and in Find music.
