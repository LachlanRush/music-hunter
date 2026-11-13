"""Beat grid and phrase marks for DJing (2026-11-13, feedback #54): every beat, which beat starts a bar, and phrase
starts every 8 bars, lined up on the drop, so marks in Rekordbox fall where the music changes.

Electronic dance tracks keep one tempo, so the grid is a straight line: the tempo (refined from the beat tracker's
beats) and the time of one beat. The bar and phrase starts are anchored on the drop: the beat after which the low end
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
    onset = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S, ref=np.max), sr=sr, hop_length=HOP)
    return low, full, onset


def analyse(y: np.ndarray, sr: int, bpm_hint: float | None = None, phrase_bars: int = 8) -> Grid | None:
    import librosa

    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR)
        sr = SR
    low, full, onset = _bands(y, sr)
    fps = sr / HOP
    # Beats from the tracker, then one straight line through them (steady tempo).
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=HOP,
                                           start_bpm=bpm_hint or 128, tightness=400, units="time")
    if len(beats) < 32:
        return None
    period0 = 60.0 / bpm_hint if bpm_hint else float(np.median(np.diff(beats)))
    idx = np.round((beats - beats[0]) / period0)
    keep = np.abs((beats - beats[0]) - idx * period0) < period0 * 0.25
    A = np.vstack([idx[keep], np.ones(keep.sum())]).T
    (period, t0), *_ = np.linalg.lstsq(A, beats[keep], rcond=None)
    # Fine phase: the offset (within ±1/4 beat) where onsets line up best with the grid.
    dur = len(y) / sr
    grid_t = lambda off: np.arange(t0 + off, dur, period)
    def score(off):
        fr = np.clip(np.round(grid_t(off) * fps).astype(int), 0, len(onset) - 1)
        return onset[fr].mean()
    offs = np.linspace(-period / 4, period / 4, 41)
    t0 = t0 + offs[int(np.argmax([score(o) for o in offs]))]
    t0 = t0 - period * np.floor(t0 / period)                   # the first beat at or after 0
    beat_times = np.arange(t0, dur, period)
    # Bars and phrases: of the 4 possible bar starts and the 8 possible phrase starts, the pair whose phrase starts
    # sit on the biggest changes in the music (low end and overall loudness, bar by bar) across the whole track; the
    # drop (the biggest rise of the low end after a quieter stretch) counts double, as the strongest single landmark.
    def per(x, times):
        fr = np.clip(np.round(times * fps).astype(int), 0, len(x) - 1)
        edges = np.append(fr, len(x))
        return np.array([x[a:b].mean() if b > a else 0 for a, b in zip(edges[:-1], edges[1:])])
    best = None
    for r in range(4):
        bars_t = np.arange(t0 + r * period, dur - 4 * period, 4 * period)
        if len(bars_t) < 2 * phrase_bars:
            continue
        lowb, fullb = per(low, bars_t), per(full, bars_t)
        e = np.log(lowb + 1e-9) + np.log(fullb + 1e-9)
        nov = np.r_[0, np.abs(np.diff(e))]
        rise = np.full(len(bars_t), -np.inf)               # the drop: low end over the next 8 bars vs the previous 4
        for i in range(4, len(bars_t) - 8):
            rise[i] = lowb[i:i + 8].mean() / (lowb[i - 4:i].mean() + 1e-9)
        drop_bar = int(np.argmax(rise))
        weights = nov.copy()
        weights[drop_bar] += nov.max()
        for k in range(phrase_bars):
            sc = weights[k::phrase_bars].sum() / weights.sum()
            if best is None or sc > best[0]:
                best = (sc, r, k, bars_t, nov, drop_bar)
    if best is None:
        return None
    _, r, k, bars_t, nov, drop_bar = best
    first_bar = float(bars_t[0])
    # The drop moved onto the nearest phrase start; how many of the clearest changes land on phrase starts.
    drop_bar = k + phrase_bars * int(round((drop_bar - k) / phrase_bars))
    drop = float(bars_t[min(max(drop_bar, 0), len(bars_t) - 1)])
    top = np.argsort(-nov)[:max(4, len(nov) // 16)]
    agree = float(np.mean([(b - k) % phrase_bars == 0 for b in top]))
    return Grid(bpm=60.0 / period, first_beat=float(first_bar), drop=drop, phrase_bars=phrase_bars, agreement=agree)
