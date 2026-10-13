"""Short-time features of an audio signal, with numpy only (no librosa, no scipy).

    frame_db(x, sr, hop, win)         loudness per frame, dBFS (RMS)
    cepstra(x, sr, hop, win)          12 MFCC-style coefficients per frame (c1-c12 of the log-mel
                                      spectrum), mean-normalized over the file: what speech sounds
                                      like, independent of how loud it is
    band_db(x, sr, hop, win, bands)   energy in a few broad bands per frame (background "shape")
    sequence_similarity(a, b)         0..1: how alike two stretches sound (DTW over cosine distances)
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

EPS = 1e-10


def frames(x: np.ndarray, sr: int, hop_sec: float, win_sec: float) -> np.ndarray:
    """(n_frames, win) view of x; frame i starts at i * hop. The last partial frame is dropped."""
    hop, win = max(1, int(round(hop_sec * sr))), max(1, int(round(win_sec * sr)))
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    n = 1 + (len(x) - win) // hop
    return np.lib.stride_tricks.as_strided(x, shape=(n, win), strides=(x.strides[0] * hop, x.strides[0]),
                                           writeable=False)


def frame_db(x: np.ndarray, sr: int, hop_sec: float = 0.01, win_sec: float = 0.02) -> np.ndarray:
    f = frames(x, sr, hop_sec, win_sec)
    return 10.0 * np.log10(np.mean(np.square(f, dtype=np.float64), axis=1) + EPS)


@lru_cache(maxsize=8)
def _mel_filters(sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float) -> np.ndarray:
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    hz = mel_to_hz(mels)
    bins = np.fft.rfftfreq(n_fft, 1.0 / sr)
    fb = np.zeros((n_mels, len(bins)), dtype=np.float32)
    for m in range(n_mels):
        lo, mid, hi = hz[m], hz[m + 1], hz[m + 2]
        up = (bins - lo) / max(mid - lo, EPS)
        down = (hi - bins) / max(hi - mid, EPS)
        fb[m] = np.clip(np.minimum(up, down), 0.0, None)
    return fb


def power_spectrum(x: np.ndarray, sr: int, hop_sec: float, win_sec: float, chunk: int = 20000):
    """|FFT|^2 of Hann-windowed frames, in chunks so an hour of audio doesn't need gigabytes."""
    f = frames(x, sr, hop_sec, win_sec)
    win = f.shape[1]
    n_fft = 1 << int(np.ceil(np.log2(win)))
    window = np.hanning(win).astype(np.float32)
    for start in range(0, len(f), chunk):
        block = f[start:start + chunk] * window
        yield np.square(np.abs(np.fft.rfft(block, n=n_fft, axis=1))).astype(np.float32), n_fft


def cepstra(x: np.ndarray, sr: int, hop_sec: float = 0.01, win_sec: float = 0.025, n_mels: int = 32,
            n_coeffs: int = 12, mean: np.ndarray | None = None) -> np.ndarray:
    """(n_frames, n_coeffs) cepstral coefficients c1..c12 (c0, the loudness, left out), with the
    file's mean removed so the microphone and the room don't decide the similarity. `mean`: remove
    this one instead (a short piece compared with the rest of its file)."""
    out = []
    dct = None
    for spec, n_fft in power_spectrum(x, sr, hop_sec, win_sec):
        fb = _mel_filters(sr, n_fft, n_mels, 60.0, sr / 2.0)
        logmel = np.log(spec @ fb.T + EPS)
        if dct is None:
            k = np.arange(n_mels)
            dct = np.cos(np.pi / n_mels * (k[None, :] + 0.5) * np.arange(1, n_coeffs + 1)[:, None]).astype(np.float32)
        out.append(logmel @ dct.T)
    c = np.concatenate(out) if out else np.zeros((0, n_coeffs), np.float32)
    if not len(c):
        return c
    return c - (c.mean(axis=0, keepdims=True) if mean is None else mean)


def band_db(x: np.ndarray, sr: int, hop_sec: float = 0.02, win_sec: float = 0.04,
            edges=(60, 250, 500, 1000, 2000, 4000)) -> np.ndarray:
    """(n_frames, n_bands) energy in dB per broad band, for comparing background noise."""
    out = []
    for spec, n_fft in power_spectrum(x, sr, hop_sec, win_sec):
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
        cols = []
        bounds = list(edges) + [sr / 2.0]
        for lo, hi in zip(bounds[:-1], bounds[1:]):
            sel = (freqs >= lo) & (freqs < hi)
            cols.append(10.0 * np.log10(spec[:, sel].sum(axis=1) + EPS))
        out.append(np.stack(cols, axis=1))
    return np.concatenate(out) if out else np.zeros((0, len(edges)), np.float32)


def _unit(rows: np.ndarray) -> np.ndarray:
    return rows / (np.linalg.norm(rows, axis=1, keepdims=True) + EPS)


def sequence_similarity(a: np.ndarray, b: np.ndarray, band: float = 0.2) -> float:
    """How alike two feature sequences are, 0..1: 1 - the mean cosine distance along the best
    time alignment (dynamic time warping, within a band of +-20% of the length). Identical
    stretches give ~1; two different syllables usually under 0.6."""
    if len(a) == 0 or len(b) == 0:
        return 0.0
    ua, ub = _unit(a), _unit(b)
    dist = 1.0 - ua @ ub.T                                       # 0 (same) .. 2 (opposite)
    n, m = dist.shape
    width = max(2, int(band * max(n, m)))
    acc = np.full((n + 1, m + 1), np.inf)
    acc[0, 0] = 0.0
    steps = np.zeros((n + 1, m + 1), dtype=np.int32)
    for i in range(1, n + 1):
        center = i * m / n
        lo, hi = max(1, int(center - width)), min(m, int(center + width) + 1)
        for j in range(lo, hi + 1):
            prev = (acc[i - 1, j - 1], acc[i - 1, j], acc[i, j - 1])
            k = int(np.argmin(prev))
            acc[i, j] = dist[i - 1, j - 1] + prev[k]
            steps[i, j] = (steps[i - 1, j - 1], steps[i - 1, j], steps[i, j - 1])[k] + 1
    if not np.isfinite(acc[n, m]):
        return 0.0
    return float(max(0.0, 1.0 - acc[n, m] / max(steps[n, m], 1)))


def lag_similarity(c: np.ndarray, lag: int) -> np.ndarray:
    """Cosine similarity between frame t and frame t+lag, for every t (length len(c) - lag)."""
    if lag <= 0 or lag >= len(c):
        return np.zeros(0, np.float32)
    u = _unit(c)
    return np.einsum("ij,ij->i", u[:-lag], u[lag:])


def moving_mean(x: np.ndarray, n: int) -> np.ndarray:
    """Mean of x[t:t+n] for every t (length len(x) - n + 1)."""
    if n <= 0 or len(x) < n:
        return np.zeros(0, np.float64)
    s = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    return (s[n:] - s[:-n]) / n
