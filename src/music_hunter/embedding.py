"""Full-track chunked audio embeddings with MAEST (SPEC.md §6, §16.1).

v3: MAEST (Discogs-trained audio transformer) replaces CLAP. The Hugging Face PyTorch weights load as the
standard transformers ASTForAudioClassification at a pinned revision; the model page's own feature-extraction
code is not used — its mel-spectrogram (preprocessor_config.json settings) is rebuilt here from transformers'
audio utilities. A chunk's embedding is the mean over tokens of the chosen layer's output.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

from .config import CONFIG

CFG = CONFIG["embedding"]
N_FFT, HOP, MEL_BINS, FRAMES = 512, 256, 96, 1876          # preprocessor_config.json of the pinned revision
MEL_MEAN, MEL_STD = 2.06755686098554, 1.268292820667291


@dataclass
class TrackEmbedding:
    track: np.ndarray            # (dim,) mean of chunks, L2-normalised
    chunks: np.ndarray           # (n_chunks, dim), each L2-normalised
    spans: list[tuple[float, float]]  # (start_s, end_s) of real audio per chunk


def model_name() -> str:
    """Stored with every embedding, so embeddings from another model or layer are never mixed in."""
    return f"{CFG['model']}@layer{CFG['layer']}"


def chunk(samples: np.ndarray) -> tuple[list[np.ndarray], list[tuple[float, float]]]:
    """Split into consecutive native-length chunks, dropping a too-short tail."""
    sr = CFG["sample_rate"]
    size = int(CFG["chunk_seconds"] * sr)
    min_tail = int(CFG["min_last_chunk_seconds"] * sr)
    pieces, spans = [], []
    for start in range(0, len(samples), size):
        piece = samples[start:start + size]
        if len(piece) < min_tail and pieces:
            break
        pieces.append(piece)
        spans.append((start / sr, (start + len(piece)) / sr))
    return pieces, spans


class Embedder:
    def __init__(self):
        self.model_name = model_name()
        self.sample_rate = CFG["sample_rate"]
        # Once the model is downloaded, load it offline: no Hugging Face Hub check on every start.
        cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
        if (cache / f"models--{CFG['model'].replace('/', '--')}").exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from transformers import ASTForAudioClassification
        from transformers.audio_utils import mel_filter_bank, window_function

        self.device = CFG["device"] if torch.cuda.is_available() else "cpu"
        self.model = ASTForAudioClassification.from_pretrained(CFG["model"], revision=CFG["revision"]) \
            .to(self.device).eval()
        self.window = window_function(N_FFT, "hann")
        self.mel_fb = mel_filter_bank(num_frequency_bins=N_FFT // 2 + 1, num_mel_filters=MEL_BINS, min_frequency=0,
                                      max_frequency=self.sample_rate / 2, sampling_rate=self.sample_rate,
                                      norm="slaney", mel_scale="slaney")

    def mel(self, piece: np.ndarray) -> np.ndarray:
        """Log-compressed mel spectrogram, padded/cut to the model's patch length, normalised."""
        from transformers.audio_utils import spectrogram

        m = spectrogram(piece, window=self.window, frame_length=N_FFT, hop_length=HOP, power=2,
                        mel_filters=self.mel_fb, min_value=1e-30, mel_floor=1e-30, pad_mode="constant").T
        m = np.log10(1 + m * 10000)[:FRAMES]
        m = np.pad(m, ((0, FRAMES - len(m)), (0, 0)))
        return ((m - MEL_MEAN) / (MEL_STD * 2)).astype(np.float32)

    @torch.no_grad()
    def embed_chunks(self, pieces: list[np.ndarray]) -> np.ndarray:
        out = []
        for i in range(0, len(pieces), CFG["batch_size"]):
            x = torch.from_numpy(np.stack([self.mel(p) for p in pieces[i:i + CFG["batch_size"]]])).to(self.device)
            hidden = self.model(x, output_hidden_states=True).hidden_states[CFG["layer"]]   # (batch, tokens, 768)
            out.append(torch.nn.functional.normalize(hidden.mean(1), dim=-1).float().cpu().numpy())
        return np.concatenate(out)

    def embed_track(self, samples: np.ndarray) -> TrackEmbedding:
        if len(samples) == 0:
            raise ValueError("empty audio")
        pieces, spans = chunk(samples)
        chunks = self.embed_chunks(pieces)
        mean = chunks.mean(axis=0)
        return TrackEmbedding(mean / np.linalg.norm(mean), chunks, spans)
