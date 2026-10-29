"""Tempo (BPM) from the audio — SPEC.md §15.6.

librosa's beat tracker gives a coarse tempo and the beat times; the tempo is refined from
the median gap between beats, then folded into [bpm] min–max to undo half/double-tempo
errors (e.g. a 140 BPM track read as 70).
"""

import numpy as np

from .config import CONFIG

CFG = CONFIG["bpm"]


def fold(bpm: float) -> float:
    lo, hi = CFG["min"], CFG["max"]
    while bpm < lo:
        bpm *= 2
    while bpm >= hi:
        bpm /= 2
    return bpm


def candidates(bpm: float) -> list[float]:
    """The tempo and its 3:2-related readings (a dotted rhythm can lock the tracker onto 2/3 or 3/2)."""
    return sorted({round(fold(x), 2) for x in (bpm, bpm * 1.5, bpm / 1.5)})


def salience(oenv: np.ndarray, bpm: float, fps: float) -> float:
    """How strongly the onset pattern repeats at this beat length (and at two beats), from autocorrelation.

    Tested 2026-10-29 against a beat-grid measure: it kept straight 95 and 174 BPM beats where they
    were, while still resolving a dotted-rhythm misreading (Sam Alfred – No More: 94.6 → ~142).
    """
    import librosa

    ac = librosa.autocorrelate(oenv - oenv.mean(), max_size=int(fps * 60 / 40) + 2)
    ac = ac / (ac[0] or 1.0)
    lag = fps * 60 / bpm
    at = lambda L: float(np.interp(L, np.arange(len(ac)), ac))
    return max(1e-6, at(lag) + 0.5 * at(2 * lag))


def prior(bpm: float, around: float | None) -> float:
    """Gentle preference for tempos near the seeds' median (log scale, [bpm] prior_width_octaves wide)."""
    if not around:
        return 1.0
    return float(np.exp(-0.5 * (np.log2(bpm / around) / CFG["prior_width_octaves"]) ** 2))


def estimate(samples: np.ndarray, sr: int, around: float | None = None, details: dict | None = None) -> float | None:
    """BPM of mono float audio, or None if no steady beat is found.

    Beat times are quantised to the analysis hop (~12 ms here), so a single gap is coarse;
    averaging the steady gaps across the whole track recovers the tempo to a fraction of a BPM.
    Then 3:2 ambiguity is resolved: of the tempo and its 3:2-related readings, keep the one
    with the best repetition strength (salience) × closeness to `around` (the seeds' median BPM).
    """
    import warnings

    import librosa

    target_sr = CFG["analysis_sample_rate"]
    y = librosa.resample(samples, orig_sr=sr, target_sr=target_sr) if sr != target_sr else samples
    hop = CFG["hop_length"]
    with warnings.catch_warnings():               # numba's "invalid value encountered in cast" is harmless here
        warnings.simplefilter("ignore", RuntimeWarning)
        oenv = librosa.onset.onset_strength(y=y, sr=target_sr, hop_length=hop)
        tempo, beats = librosa.beat.beat_track(onset_envelope=oenv, sr=target_sr, hop_length=hop, units="time")
    coarse = float(np.atleast_1d(tempo)[0])
    if coarse <= 0:
        return None
    if len(beats) >= 16:
        gaps = np.diff(beats)
        median = np.median(gaps)
        steady = gaps[np.abs(gaps - median) < 0.1 * median]      # drop breakdowns and missed beats
        detected = fold(60.0 / steady.mean() if len(steady) >= 8 else coarse)
    else:
        detected = fold(coarse)
    fps = target_sr / hop
    scores = {c: salience(oenv, c, fps) * prior(c, around) for c in candidates(detected)}
    chosen = max(scores, key=scores.get)
    if details is not None:
        details.update(detected=round(detected, 2), chosen=chosen, scores={k: round(v, 3) for k, v in scores.items()})
    return round(chosen, 2)


def seeds_median(conn) -> float | None:
    """Median BPM of the seeds — the prior for every other track."""
    bpms = [r[0] for r in conn.execute("SELECT bpm FROM tracks WHERE origin = 'original_reference' AND bpm IS NOT NULL")]
    return float(np.median(bpms)) if bpms else None
