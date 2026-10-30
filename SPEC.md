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
