"""Full-track chunked CLAP embeddings (SPEC.md §6)."""

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

from .config import CONFIG

CFG = CONFIG["clap"]


@dataclass
class TrackEmbedding:
    track: np.ndarray            # (dim,) mean of chunks, L2-normalised
    chunks: np.ndarray           # (n_chunks, dim), each L2-normalised
    spans: list[tuple[float, float]]  # (start_s, end_s) of real audio per chunk


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
        self.model_name = CFG["checkpoint"]
        # Once the model is downloaded, load it offline: no Hugging Face Hub check (and no
        # "unauthenticated requests" warning) on every start. Must be set before transformers loads.
        cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
        if (cache / f"models--{self.model_name.replace('/', '--')}").exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from transformers import ClapModel, ClapProcessor

        self.device = CFG["device"] if torch.cuda.is_available() else "cpu"
        self.model = ClapModel.from_pretrained(self.model_name).to(self.device).eval()
        self.extractor = ClapProcessor.from_pretrained(self.model_name).feature_extractor

    @torch.no_grad()
    def embed_chunks(self, pieces: list[np.ndarray]) -> np.ndarray:
        out = []
        for i in range(0, len(pieces), CFG["batch_size"]):
            inputs = self.extractor(pieces[i:i + CFG["batch_size"]],
                                    sampling_rate=CFG["sample_rate"], return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            feats = self.model.get_audio_features(**inputs).pooler_output
            out.append(torch.nn.functional.normalize(feats, dim=-1).cpu().numpy())
        return np.concatenate(out)

    def embed_track(self, samples: np.ndarray) -> TrackEmbedding:
        if len(samples) == 0:
            raise ValueError("empty audio")
        pieces, spans = chunk(samples)
        chunks = self.embed_chunks(pieces)
        mean = chunks.mean(axis=0)
        return TrackEmbedding(mean / np.linalg.norm(mean), chunks, spans)
