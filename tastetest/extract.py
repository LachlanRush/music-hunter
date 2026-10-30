"""Step 2: features from each audio model (SPEC.md §16.1).

    uv run python extract.py <model> [<model> …]
    models: mert  muq  mulan  omarrq  essentia  maest  signal  stems

Each writes runs/taste-test/features/<model>.npz with track-level features (mean over the
track) and per-section features (for the "weakest part" analysis). MERT loads with
trust_remote_code (it runs code from its Hugging Face page; approved). MAEST runs from
Essentia's plain model file instead of the Hugging Face (remote-code) version.
"""

import json
import os
import sys
import time
import urllib.request

import numpy as np

from common import OUT, decode, load_dataset, save_features, split

DEVICE = "cuda"

# Plain-English descriptions scored by MuQ-MuLan (text ↔ audio similarity): interpretable taste features.
PROMPTS = [
    "energetic club track", "deep hypnotic groove", "dark moody electronic music", "bright uplifting euphoric music",
    "minimal stripped back track", "busy dense arrangement", "heavy bassline", "rolling sub bass",
    "punchy kick drum", "shuffling garage swing rhythm", "four on the floor house beat", "broken beat breakbeat",
    "prominent lead vocals", "chopped vocal samples", "instrumental with no vocals", "male rap vocals",
    "female soulful vocals", "catchy vocal hook", "repetitive loop", "melodic synth lead", "piano chords",
    "acid 303 bassline", "tech house", "UK garage", "speed garage", "bass house", "disco funk samples",
    "rave stabs", "trance pads", "lo-fi raw sound", "polished commercial production", "aggressive distorted sound",
    "chill laid back vibe", "build up and drop", "long dj friendly intro",
]


def tracks():
    data = [r for r in load_dataset() if r["audio"]]
    if os.environ.get("TASTE_LIMIT"):                             # smoke tests: only the first N tracks
        data = data[:int(os.environ["TASTE_LIMIT"])]
    print(f"{len(data)} tracks with audio")
    return data


def pooled(frames: np.ndarray) -> np.ndarray:
    return frames.mean(axis=-2)


def run_muq():
    import torch
    from muq import MuQ

    model = MuQ.from_pretrained("OpenMuQ/MuQ-large-msd-iter").to(DEVICE).eval()
    keys, track, chunks = [], [], []
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], 24000)
        per = []
        for piece in split(y, 24000, 10.0):
            with torch.no_grad():
                out = model(torch.from_numpy(piece)[None].to(DEVICE), output_hidden_states=True)
            per.append(np.stack([pooled(h[0].float().cpu().numpy()) for h in out.hidden_states]))   # (13, 1024)
        per = np.stack(per).astype(np.float16)                                                     # (chunks, 13, 1024)
        keys.append(r["key"]); track.append(per.astype(np.float32).mean(0)); chunks.append(per)
        print(f"  muq {i}: {len(per)} sections", flush=True)
    save_features("muq", keys, np.stack(track), chunks, meta={"model": "OpenMuQ/MuQ-large-msd-iter", "layers": 13,
                                                                "chunk_seconds": 10})


def run_mert():
    """MERT-v1-330M (24 kHz, 5 s context, 25 layers). Loads with trust_remote_code (approved 2026-10-29)."""
    import torch
    from transformers import AutoModel, Wav2Vec2FeatureExtractor

    name = "m-a-p/MERT-v1-330M"
    rev = "5240c2708a5acaee1007f43fb9735c7dcd0b78c9"   # pinned: the remote code reviewed and approved on 2026-10-29
    model = AutoModel.from_pretrained(name, revision=rev, trust_remote_code=True).to(DEVICE).eval()
    proc = Wav2Vec2FeatureExtractor.from_pretrained(name, revision=rev, trust_remote_code=True)
    keys, track, chunks = [], [], []
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], proc.sampling_rate)
        pieces = split(y, proc.sampling_rate, 5.0)
        per = []
        for b in range(0, len(pieces), 8):
            inp = proc(pieces[b:b + 8], sampling_rate=proc.sampling_rate, return_tensors="pt").to(DEVICE)
            with torch.no_grad():
                out = model(**inp, output_hidden_states=True)
            per.append(torch.stack(out.hidden_states, dim=1).float().mean(2).cpu().numpy())   # (batch, 25, 1024)
        per = np.concatenate(per)
        ten = np.stack([per[j:j + 2].mean(0) for j in range(0, len(per), 2)]).astype(np.float16)   # ~10 s sections
        keys.append(r["key"]); track.append(per.mean(0)); chunks.append(ten)
        print(f"  mert {i}: {len(ten)} sections", flush=True)
    save_features("mert", keys, np.stack(track), chunks, meta={"model": name, "layers": 25, "chunk_seconds": 10})


def run_mulan():
    import torch
    from muq import MuQMuLan

    model = MuQMuLan.from_pretrained("OpenMuQ/MuQ-MuLan-large").to(DEVICE).eval()
    with torch.no_grad():
        text = model(texts=PROMPTS).float()
    text = torch.nn.functional.normalize(text, dim=-1)
    keys, track, chunks, attrs = [], [], [], []
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], 24000)
        with torch.no_grad():
            emb = torch.cat([model(wavs=torch.from_numpy(p)[None].to(DEVICE)).float() for p in split(y, 24000, 10.0)])
        emb = torch.nn.functional.normalize(emb, dim=-1)
        keys.append(r["key"]); chunks.append(emb.cpu().numpy().astype(np.float16))
        track.append(emb.mean(0).cpu().numpy())
        attrs.append((emb @ text.T).mean(0).cpu().numpy())        # mean similarity to each description
        print(f"  mulan {i}", flush=True)
    save_features("mulan", keys, np.stack(track), chunks, meta={"model": "OpenMuQ/MuQ-MuLan-large", "chunk_seconds": 10})
    save_features("mulan_text", keys, np.stack(attrs), names=PROMPTS, meta={"model": "OpenMuQ/MuQ-MuLan-large",
                                                                            "what": "similarity to each description"})


def run_omarrq():
    import torch
    from omar_rq import get_model

    model = get_model(model_id="mtg-upf/omar-rq-multifeature-25hz-fsq", device=DEVICE)
    layers = None
    keys, track, chunks = [], [], []
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], 24000)
        per = []
        for piece in split(y, 24000, 10.0):
            x = torch.from_numpy(piece)[None].to(DEVICE)              # (1, T)
            with torch.no_grad():
                if layers is None:                               # find how many layers the model has
                    n = 24
                    while n > 1:
                        try:
                            model.extract_embeddings(x, layers=set(range(n)))
                            break
                        except Exception:
                            n -= 1
                    layers = set(range(n))
                    print(f"  omar-rq has {n} layers")
                e = model.extract_embeddings(x, layers=layers)    # (L, 1, T, C)
            per.append(e[:, 0].float().mean(1).cpu().numpy())    # (L, C)
        per = np.stack(per).astype(np.float16)
        keys.append(r["key"]); track.append(per.astype(np.float32).mean(0)); chunks.append(per)
        print(f"  omarrq {i}: {len(per)} sections", flush=True)
    save_features("omarrq", keys, np.stack(track), chunks,
                  meta={"model": "mtg-upf/omar-rq-multifeature-25hz-fsq", "layers": len(layers), "chunk_seconds": 10})


ESSENTIA = "https://essentia.upf.edu/models/"
HEADS = {   # name: (folder/file stem, output layer) — heads on discogs-effnet embeddings
    "danceability": ("danceability/danceability-discogs-effnet-1", "model/Softmax"),
    "mood_aggressive": ("mood_aggressive/mood_aggressive-discogs-effnet-1", "model/Softmax"),
    "mood_happy": ("mood_happy/mood_happy-discogs-effnet-1", "model/Softmax"),
    "mood_party": ("mood_party/mood_party-discogs-effnet-1", "model/Softmax"),
    "mood_relaxed": ("mood_relaxed/mood_relaxed-discogs-effnet-1", "model/Softmax"),
    "mood_sad": ("mood_sad/mood_sad-discogs-effnet-1", "model/Softmax"),
    "mood_acoustic": ("mood_acoustic/mood_acoustic-discogs-effnet-1", "model/Softmax"),
    "mood_electronic": ("mood_electronic/mood_electronic-discogs-effnet-1", "model/Softmax"),
    "voice_instrumental": ("voice_instrumental/voice_instrumental-discogs-effnet-1", "model/Softmax"),
    "gender": ("gender/gender-discogs-effnet-1", "model/Softmax"),
    "timbre": ("timbre/timbre-discogs-effnet-1", "model/Softmax"),
    "tonal_atonal": ("tonal_atonal/tonal_atonal-discogs-effnet-1", "model/Softmax"),
    "approachability": ("approachability/approachability_regression-discogs-effnet-1", "model/Identity"),
    "engagement": ("engagement/engagement_regression-discogs-effnet-1", "model/Identity"),
}


def fetch_model(path: str) -> tuple[str, dict]:
    """Download an Essentia model (.pb graph + .json metadata) once into runs/taste-test/models/."""
    folder = OUT / "models"
    folder.mkdir(parents=True, exist_ok=True)
    pb, js = folder / (path.split("/")[-1] + ".pb"), folder / (path.split("/")[-1] + ".json")
    for f, ext in ((pb, ".pb"), (js, ".json")):
        if not f.exists():
            urllib.request.urlretrieve(ESSENTIA + path + ext, f)
    return str(pb), json.loads(js.read_text())


def run_essentia():
    import essentia.standard as es

    effnet_pb, _ = fetch_model("feature-extractors/discogs-effnet/discogs-effnet-bs64-1")
    effnet = es.TensorflowPredictEffnetDiscogs(graphFilename=effnet_pb, output="PartitionedCall:1")
    genre_pb, genre_meta = fetch_model("classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1")
    genre = es.TensorflowPredict2D(graphFilename=genre_pb, input="serving_default_model_Placeholder",
                                   output="PartitionedCall:0")
    heads, names = {}, []
    for name, (stem, layer) in HEADS.items():
        try:
            pb, meta = fetch_model("classification-heads/" + stem)
        except Exception as e:
            print(f"  skipping {name}: {e}")
            continue
        heads[name] = (es.TensorflowPredict2D(graphFilename=pb, output=layer), meta.get("classes") or [name])
    musicnn_pb, _ = fetch_model("feature-extractors/musicnn/msd-musicnn-1")
    musicnn = es.TensorflowPredictMusiCNN(graphFilename=musicnn_pb, output="model/dense/BiasAdd")
    av = {}
    for name in ("deam", "emomusic"):
        pb, _ = fetch_model(f"classification-heads/{name}/{name}-msd-musicnn-2")
        av[name] = es.TensorflowPredict2D(graphFilename=pb, output="model/Identity")

    keys, emb_track, emb_chunks, desc, styles = [], [], [], [], []
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], 16000)
        frames = effnet(y)                                        # (frames, 1280), ~one per 2 s
        row, names = [], []
        for name, (model, classes) in heads.items():
            p = model(frames).mean(0)
            if len(p) == 1:                                       # regression head
                row.append(float(p[0])); names.append(name)
            else:                                                 # first class, e.g. "danceable", "aggressive"
                row.append(float(p[0])); names.append(f"{name}:{classes[0]}")
        mn = musicnn(y)
        for name, model in av.items():
            v = model(mn).mean(0)                                 # valence, arousal (1–9 scale)
            row += [float(v[0]), float(v[1])]; names += [f"{name}:valence", f"{name}:arousal"]
        keys.append(r["key"]); desc.append(row)
        emb_track.append(frames.mean(0))
        n = max(1, len(frames) // 5)                              # ~10 s sections
        emb_chunks.append(np.stack([c.mean(0) for c in np.array_split(frames, n)]).astype(np.float16))
        styles.append(genre(frames).mean(0))
        print(f"  essentia {i}", flush=True)
    save_features("effnet", keys, np.stack(emb_track), emb_chunks, meta={"model": "discogs-effnet-bs64-1"})
    save_features("discogs_styles", keys, np.stack(styles), names=genre_meta["classes"],
                  meta={"model": "genre_discogs400-discogs-effnet-1", "what": "400 Discogs style activations"})
    save_features("descriptors", keys, np.array(desc), names=names,
                  meta={"what": "Essentia classifiers (effnet heads) + arousal/valence (musicnn heads)"})


def run_maest():
    """MAEST (Discogs 400 styles, 30 s patches) through Essentia — a plain model file, no remote code.

    Embeddings from layers 5, 7, 9 and 11 (layer 7 is the documented default) and the 400 style activations.
    """
    import essentia.standard as es

    pb, meta = fetch_model("feature-extractors/maest/discogs-maest-30s-pw-2")
    layers = [5, 7, 9, 11]
    nets = {l: es.TensorflowPredictMAEST(graphFilename=pb, output=f"PartitionedCall/Identity_{l}") for l in layers}
    styles_net = es.TensorflowPredictMAEST(graphFilename=pb, output="PartitionedCall/Identity_13")
    keys, track, chunks, styles = [], [], [], []
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], 16000)
        per_layer = []
        for l in layers:
            e = np.asarray(nets[l](y))                            # (patches, …, 768)
            per_layer.append(e.reshape(e.shape[0], -1, e.shape[-1]).mean(1))   # (patches, 768)
        per = np.stack(per_layer, axis=1).astype(np.float16)      # (patches, 4, 768)
        keys.append(r["key"]); chunks.append(per); track.append(per.astype(np.float32).mean(0))
        styles.append(np.asarray(styles_net(y)).reshape(-1, 400).mean(0))
        print(f"  maest {i}: {len(per)} patches", flush=True)
    save_features("maest", keys, np.stack(track), chunks,
                  meta={"model": "discogs-maest-30s-pw-2", "layers": layers, "chunk_seconds": 30})
    save_features("maest_styles", keys, np.stack(styles), names=meta["classes"],
                  meta={"model": "discogs-maest-30s-pw-2", "what": "400 Discogs style activations"})


def run_maest_gpu():
    """MAEST on the GPU: the Hugging Face PyTorch weights (standard transformers ASTForAudioClassification,
    pinned revision) with its mel-spectrogram front end rebuilt from transformers' own audio utilities, so
    no code from the model page runs. Same settings as its preprocessor_config.json. All 12 layers, one pass.
    """
    import torch
    from transformers import ASTForAudioClassification
    from transformers.audio_utils import mel_filter_bank, spectrogram, window_function

    model_id, revision = "mtg-upf/discogs-maest-30s-pw-129e", "e5d670316c61b18aaef46ce66242fcd73870d792"
    sr, n_fft, hop, frames, mean, std = 16000, 512, 256, 1876, 2.06755686098554, 1.268292820667291
    model = ASTForAudioClassification.from_pretrained(model_id, revision=revision).to(DEVICE).eval()
    window = window_function(n_fft, "hann")
    mel_fb = mel_filter_bank(num_frequency_bins=n_fft // 2 + 1, num_mel_filters=96, min_frequency=0,
                             max_frequency=sr / 2, sampling_rate=sr, norm="slaney", mel_scale="slaney")

    def mel(y):
        m = spectrogram(y, window=window, frame_length=n_fft, hop_length=hop, power=2, mel_filters=mel_fb,
                        min_value=1e-30, mel_floor=1e-30, pad_mode="constant").T
        m = np.log10(1 + m * 10000)[:frames]
        m = np.pad(m, ((0, frames - len(m)), (0, 0)))
        return ((m - mean) / (std * 2)).astype(np.float32)

    keys, track, chunks, styles = [], [], [], []
    for i, r in enumerate(tracks(), 1):
        t0 = time.monotonic()
        y = decode(r["audio"], sr)
        pieces = split(y, sr, 30.0)
        x = torch.from_numpy(np.stack([mel(p) for p in pieces])).to(DEVICE)
        with torch.no_grad():
            out = model(x, output_hidden_states=True)
        per = torch.stack([h.mean(1) for h in out.hidden_states[1:]], 1).float().cpu().numpy()   # (patches, 12, 768)
        keys.append(r["key"]); chunks.append(per.astype(np.float16)); track.append(per.mean(0))
        styles.append(torch.sigmoid(out.logits).mean(0).float().cpu().numpy())
        print(f"  maest_gpu {i}: {len(pieces)} patches, {time.monotonic() - t0:.2f} s", flush=True)
    save_features("maest_gpu", keys, np.stack(track), chunks,
                  meta={"model": model_id, "revision": revision, "layers": list(range(1, 13)), "chunk_seconds": 30})
    save_features("maest_gpu_styles", keys, np.stack(styles),
                  names=[model.config.id2label[i] for i in range(len(model.config.id2label))],
                  meta={"model": model_id, "what": "400 Discogs style activations"})


def run_signal():
    """Plain signal measurements: tempo-independent brightness, density and dynamics."""
    import librosa

    keys, rows = [], []
    names = ["onset_rate", "centroid_mean", "centroid_std", "flatness_mean", "rms_std_db", "low_energy_share",
             "high_energy_share", "dynamic_range_db"]
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], 22050)
        onsets = librosa.onset.onset_detect(y=y, sr=22050, units="time")
        cent = librosa.feature.spectral_centroid(y=y, sr=22050)[0]
        flat = librosa.feature.spectral_flatness(y=y)[0]
        rms_db = librosa.amplitude_to_db(librosa.feature.rms(y=y)[0] + 1e-9)
        S = np.abs(librosa.stft(y, n_fft=2048)) ** 2
        freqs = librosa.fft_frequencies(sr=22050, n_fft=2048)
        total = S.sum() + 1e-9
        rows.append([len(onsets) / (len(y) / 22050), cent.mean(), cent.std(), flat.mean(), rms_db.std(),
                     S[freqs < 150].sum() / total, S[freqs > 5000].sum() / total,
                     np.percentile(rms_db, 95) - np.percentile(rms_db, 10)])
        keys.append(r["key"])
        print(f"  signal {i}", flush=True)
    save_features("signal", keys, np.array(rows), names=names, meta={"what": "librosa signal measurements"})


def run_stems():
    """Demucs vocal/instrumental separation: how much of each track is vocals, over time."""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model("htdemucs").to(DEVICE).eval()
    sr = model.samplerate
    vocal_idx = model.sources.index("vocals")
    keys, rows, chunks = [], [], []
    names = ["vocal_share_mean", "vocal_share_max", "vocal_share_std", "vocal_sections_share"]
    for i, r in enumerate(tracks(), 1):
        y = decode(r["audio"], sr)
        x = torch.from_numpy(np.stack([y, y]))[None].to(DEVICE)   # mono → stereo, as the model expects
        with torch.no_grad():
            stems = apply_model(model, x, split=True, overlap=0.1, progress=False)[0]   # (sources, 2, T)
        vocals = stems[vocal_idx].pow(2).mean(0).cpu().numpy()
        total = stems.sum(0).pow(2).mean(0).cpu().numpy() + 1e-9
        n = sr * 10
        share = np.array([vocals[j:j + n].sum() / total[j:j + n].sum() for j in range(0, len(vocals) - n // 2, n)])
        rows.append([share.mean(), share.max(), share.std(), float((share > 0.15).mean())])
        chunks.append(share.astype(np.float16)[:, None])
        keys.append(r["key"])
        print(f"  stems {i}: vocal share {share.mean():.2f}", flush=True)
    save_features("stems", keys, np.array(rows), chunks, names=names, meta={"model": "htdemucs", "chunk_seconds": 10})


RUNNERS = {"mert": run_mert, "muq": run_muq, "mulan": run_mulan, "omarrq": run_omarrq, "essentia": run_essentia, "maest": run_maest, "maest_gpu": run_maest_gpu,
           "signal": run_signal, "stems": run_stems}

if __name__ == "__main__":
    for name in sys.argv[1:] or RUNNERS:
        print(f"== {name}", flush=True)
        RUNNERS[name]()
