"""Vocals and mood for one track at a time, for the PC worker (SPEC.md §18.34).

Runs in this environment (Demucs and Essentia live here, not in the app's), started once by the worker and kept
running so the models load once. Reads one audio file path per line on stdin, writes one JSON line per file on
stdout: Demucs vocal share over the track, and Essentia's descriptors from the Discogs-EffNet heads.

    uv run --project tastetest python tastetest/vocals_server.py
"""

import json
import sys

import numpy as np

from common import decode
from extract import DEVICE, HEADS, fetch_model

WANTED = ("mood_happy", "timbre", "approachability", "voice_instrumental", "gender", "mood_relaxed",
          "mood_aggressive", "danceability", "engagement")


def main() -> None:
    import essentia.standard as es
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    demucs = get_model("htdemucs").to(DEVICE).eval()
    vocal_idx = demucs.sources.index("vocals")
    effnet_pb, _ = fetch_model("feature-extractors/discogs-effnet/discogs-effnet-bs64-1")
    effnet = es.TensorflowPredictEffnetDiscogs(graphFilename=effnet_pb, output="PartitionedCall:1")
    heads = {}
    for name in WANTED:
        stem, layer = HEADS[name]
        pb, meta = fetch_model("classification-heads/" + stem)
        heads[name] = (es.TensorflowPredict2D(graphFilename=pb, output=layer), meta.get("classes") or [name])
    print(json.dumps({"ready": True}), flush=True)

    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        try:
            out = {}
            sr = demucs.samplerate
            y = decode(path, sr)
            x = torch.from_numpy(np.stack([y, y]))[None].to(DEVICE)
            with torch.no_grad():
                stems = apply_model(demucs, x, split=True, overlap=0.1, progress=False)[0]
            vocals = stems[vocal_idx].pow(2).mean(0).cpu().numpy()
            total = stems.sum(0).pow(2).mean(0).cpu().numpy() + 1e-9
            n = sr * 10
            share = np.array([vocals[j:j + n].sum() / total[j:j + n].sum() for j in range(0, len(vocals) - n // 2, n)])
            out.update(vocal_share_mean=float(share.mean()), vocal_share_max=float(share.max()),
                       vocal_sections_share=float((share > 0.15).mean()))
            frames = effnet(decode(path, 16000))
            for name, (model, classes) in heads.items():
                p = model(frames).mean(0)
                out[name if len(p) == 1 else f"{name}:{classes[0]}"] = round(float(p[0]), 4)
            print(json.dumps({"path": path, "features": {k: round(v, 4) for k, v in out.items()}}), flush=True)
        except Exception as e:                                       # one bad file mustn't stop the worker's helper
            print(json.dumps({"path": path, "error": str(e)[:200]}), flush=True)


if __name__ == "__main__":
    main()
