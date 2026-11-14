"""Beat grid and phrase marks for DJing (2026-11-13, feedback #54): every beat, which beat starts a bar, and phrase
starts every 8 bars, lined up on the drop, so marks in Rekordbox fall where the music changes.

Electronic dance tracks keep one tempo, so the grid is a straight line: the tempo (a whole BPM where it fits) and the
time of one beat, found from the kicks. The bar and phrase starts are anchored on the drop: the beat after which the low end
(kick and bass) comes back hardest after a quieter stretch. Sections in this music change every 8 or 16 bars, so
counting 8-bar phrases from the drop lines up with the other changes too; how well they line up is reported as
`agreement` (the share of the track's clearest changes that land on a phrase start).

Runs on the PC (librosa); the result is a few numbers per track.
"""

from dataclasses import dataclass

import numpy as np

SR = 22050
HOP = 256


@dataclass
class Grid:
    bpm: float              # exact tempo of the grid
    first_beat: float       # seconds: the first beat at or after 0 that starts a bar (Rekordbox's Inizio)
    drop: float             # seconds: the drop (a bar and phrase start)
    phrase_bars: int        # bars per phrase (8)
    agreement: float        # share of the clearest changes that land on a phrase start (0–1)

    def beats(self, duration: float) -> np.ndarray:
        p = 60.0 / self.bpm
        return np.arange(self.first_beat, duration, p)

    def phrase_starts(self, duration: float, bars: int | None = None) -> np.ndarray:
        """Every `bars` bars (default: a phrase) through the track, through the drop."""
        step = 60.0 / self.bpm * 4 * (bars or self.phrase_bars)
        t = self.drop - step * np.floor(self.drop / step)
        return np.arange(t, duration, step)


def _bands(y: np.ndarray, sr: int):
    import librosa

    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    f = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = S[(f >= 30) & (f < 150)].sum(0)                      # kick and bass
    full = S.sum(0)
    chroma = librosa.feature.chroma_stft(S=S ** 2, sr=sr)      # the notes: chords change on the bar
    return low, full, chroma


def _flux(x: np.ndarray) -> np.ndarray:
    """How sharply the level rises, frame by frame."""
    return np.maximum(0, np.diff(np.log1p(x * 10), prepend=0))


def _mean_at(env: np.ndarray, t0: float, period: float, dur: float) -> float:
    t = np.arange(t0, dur, period)
    fr = np.clip(np.round(t * SR / HOP).astype(int), 0, len(env) - 1)
    return float(env[fr].mean())


def _tempo_phase(kick: np.ndarray, bpm0: float, dur: float) -> tuple[float, float]:
    """The straight grid near bpm0 whose beats sit on the kicks. Tracks are made at a whole tempo, so a whole BPM is
    taken when it fits the kicks about as well (checked against Rekordbox on 32 songs, 2026-11-14: our tempo was off
    by up to 1.3 BPM on 16 of them, a few hundred ms out after four minutes; whole BPMs matched 30)."""
    best = (-1.0, bpm0, 0.0)
    for bpm in np.arange(bpm0 - 0.6, bpm0 + 0.6001, 0.02):
        p = 60.0 / bpm
        for t0 in np.arange(0, p, 0.004):
            s = _mean_at(kick, t0, p, dur)
            if s > best[0]:
                best = (s, float(bpm), float(t0))
    s, bpm, t0 = best
    whole = round(bpm)
    p = 60.0 / whole
    phases = np.arange(0, p, 0.002)
    scores = [_mean_at(kick, x, p, dur) for x in phases]
    if max(scores) >= 0.97 * s:
        return float(whole), float(phases[int(np.argmax(scores))])
    return bpm, t0


def _fine_phase(env: np.ndarray, t0: float, p: float, dur: float) -> float:
    """The exact beat position: where the whole sound's attacks line up best, within a quarter beat of the kicks'
    (the kick's low end peaks ~35 ms after its attack; searching only near the kicks keeps off-beat hats out)."""
    xs = np.arange(len(env)) * HOP / SR
    best = (-1.0, t0)
    for o in np.arange(-p / 4, p / 4, 0.001):
        t = np.arange(t0 + o, dur, p)
        t = t[t >= 0]
        s = float(np.interp(t, xs, env).mean())
        if s > best[0]:
            best = (s, t0 + o)
    return best[1] % p


def _per(x: np.ndarray, times: np.ndarray, fps: float) -> np.ndarray:
    """Mean of each stretch between the given times (1-D or 2-D, along the last axis)."""
    fr = np.clip(np.round(times * fps).astype(int), 0, x.shape[-1] - 1)
    edges = np.append(fr, x.shape[-1])
    return np.stack([x[..., a:b].mean(-1) if b > a else x[..., a] for a, b in zip(edges[:-1], edges[1:])], -1)


def _phrase(lowb: np.ndarray, fullb: np.ndarray, phrase_bars: int) -> tuple[int, float]:
    """Phrases, from each bar's low end and overall level: of the `phrase_bars` possible phrase starts, the one that
    sits on the biggest bar-to-bar changes across the whole track; the drop (the biggest rise of the low end after a
    quieter stretch) counts double, as the strongest single landmark. Returns the drop's bar, moved onto the nearest
    phrase start, and the share of the clearest changes that land on a phrase start.
    Against hand-set drops on 14 songs (2026-11-14): on Rekordbox's bars, the phrase lines passed through 12."""
    n = len(lowb)
    nov = np.r_[0, np.abs(np.diff(np.log(lowb + 1e-9) + np.log(fullb + 1e-9)))]
    rise = np.full(n, -np.inf)                         # the drop: low end over the next 8 bars vs the previous 4
    for i in range(4, n - 8):
        rise[i] = lowb[i:i + 8].mean() / (lowb[i - 4:i].mean() + 1e-9)
    drop_bar = int(np.argmax(rise)) if n > 12 else 0
    weights = nov.copy()
    weights[drop_bar] += nov.max()
    k = int(np.argmax([weights[j::phrase_bars].sum() for j in range(phrase_bars)]))
    drop_bar = k + phrase_bars * int(round((drop_bar - k) / phrase_bars))
    drop_bar = min(max(drop_bar, k), k + phrase_bars * ((n - 1 - k) // phrase_bars))
    top = np.argsort(-nov)[:max(4, n // 16)]
    agree = float(np.mean([(b - k) % phrase_bars == 0 for b in top]))
    return drop_bar, agree


def rekordbox_downbeats(markers: list, duration: float) -> list[float]:
    """Every bar's first beat from Rekordbox's grid: its TEMPO markers [(Inizio, Bpm, Battito)], each holding until
    the next (Rekordbox adds one where the bars shift mid-track); Battito is the beat's place in its bar (1–4).
    Times as Rekordbox gives them."""
    out = []
    ms = sorted(markers)
    for i, (start, bpm, beat) in enumerate(ms):
        end = ms[i + 1][0] if i + 1 < len(ms) else duration
        p = 60.0 / bpm
        t = start + ((5 - int(beat)) % 4) * p
        while t < end - 1e-6:
            out.append(round(t, 4))
            t += 4 * p
    return out


def phrases(y: np.ndarray, sr: int, downbeats: list[float], phrase_bars: int = 8) -> tuple[int, float] | None:
    """Phrases on bars someone else found (Rekordbox's): the drop's bar index and the agreement, as in analyse."""
    import librosa

    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR)
    low, full, _ = _bands(y, SR)
    keep = [i for i, t in enumerate(downbeats) if 0 <= t < len(y) / SR - 1]
    if len(keep) < 2 * phrase_bars:
        return None
    bars, i0, fps = np.asarray([downbeats[i] for i in keep]), keep[0], SR / HOP
    drop_bar, agree = _phrase(_per(low, bars, fps), _per(full, bars, fps), phrase_bars)
    return i0 + drop_bar, agree


def analyse(y: np.ndarray, sr: int, bpm_hint: float | None = None, phrase_bars: int = 8) -> Grid | None:
    import librosa

    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR)
        sr = SR
    low, full, chroma = _bands(y, sr)
    fps = sr / HOP
    dur = len(y) / sr
    if not bpm_hint:
        onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
        bpm_hint = float(np.atleast_1d(librosa.beat.tempo(onset_envelope=onset, sr=sr, hop_length=HOP))[0])
    if dur < 30 or not bpm_hint:
        return None
    # The beats (2026-11-14, against Rekordbox's grids on 32 songs): tempo and which beat from the kick, the exact
    # position from the whole sound. Before, onsets of everything picked the beat, and 21 of 32 grids sat half a beat
    # out, on the off-beat hats and bass.
    bpm, t0 = _tempo_phase(_flux(low), bpm_hint, dur)
    period = 60.0 / bpm
    t0 = _fine_phase(_flux(full), t0, period, dur)
    per = lambda x, times: _per(x, times, fps)
    # Bar 1: the beat where the notes change most (chords move on the bar) and the low end changes most, beat by
    # beat. Against Rekordbox's bar 1 on 32 songs (2026-11-14): 27 the same, against 21 from bar-level loudness.
    beats_t = np.arange(t0, dur - period, period)
    if len(beats_t) < 64:
        return None
    C = per(chroma, beats_t)
    C = C / (np.linalg.norm(C, axis=0) + 1e-9)
    chord = np.r_[0, 1 - (C[:, 1:] * C[:, :-1]).sum(0)]
    bass = np.r_[0, np.abs(np.diff(np.log(per(low, beats_t) + 1e-9)))]
    z = lambda v: (v - v.mean()) / (v.std() + 1e-9)
    r = int(np.argmax(z(np.array([chord[i::4].mean() for i in range(4)])) +
                      z(np.array([bass[i::4].mean() for i in range(4)]))))
    bars_t = np.arange(t0 + r * period, dur - 4 * period, 4 * period)
    if len(bars_t) < 2 * phrase_bars:
        return None
    first_bar = float(bars_t[0])
    drop_bar, agree = _phrase(per(low, bars_t), per(full, bars_t), phrase_bars)
    drop = float(bars_t[drop_bar])
    return Grid(bpm=bpm, first_beat=float(first_bar), drop=drop, phrase_bars=phrase_bars, agreement=agree)
