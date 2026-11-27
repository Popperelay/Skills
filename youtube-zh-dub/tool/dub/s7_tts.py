"""Stage 7 · tts (VoxCPM2 cross-lingual voice cloning) -> segments/tts/{i:04d}.wav

Reference strategies (--mode):
  ref  : one fixed, clean reference clip of the original speaker for every line
         (reference_wav_path; prompt cache built once -> consistent timbre).
  seg  : per-line reference = segments/vocals/{i:04d}.wav (reference adapter behaviour);
         lines shorter than VOXCPM_MIN_REFERENCE_MS fall back to the fixed clip.
  ult  : fixed clip as both reference_wav and prompt_wav + its English transcript
         (VoxCPM2 "ultimate cloning" / continuation).

The fixed clip is picked automatically: 7-14 s of continuous, confidently-recognised,
low-leakage speech (scored from asr.json word probabilities + vocal/bgm energy ratio).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

os.environ.setdefault("TQDM_DISABLE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import audio_mode, log, read_json, require, session_dir, target_text, write_json  # noqa: E402

MIN_REFERENCE_MS = int(os.getenv("VOXCPM_MIN_REFERENCE_MS", "1200"))


_ABBR_EXPAND = {"inv": "inverse", "std": "S T D", "emb": "embedding", "init": "init", "dim": "dim",
                "bn": "B N", "nn": "N N", "nll": "N L L", "mlp": "M L P", "gpu": "G P U", "cpu": "C P U"}


def speakable(text: str) -> str:
    """Make code identifiers pronounceable: vowel-less 2-4 letter tokens -> spaced capitals."""
    def fix(m):
        w = m.group(0)
        lw = w.lower()
        if lw in _ABBR_EXPAND:
            return _ABBR_EXPAND[lw]
        if 2 <= len(w) <= 4 and not re.search(r"[aeiouy]", lw):
            return " ".join(w.upper())
        return w
    t = re.sub(r"[A-Za-z]+", fix, text)
    return re.sub(r"\s+", " ", t).strip()


def tts_text(item: dict, make_speakable: bool = False) -> str:
    t = target_text(item)
    if not isinstance(t, str) or not t.strip():
        raise ValueError("target text must be a non-empty string")
    t = t.replace("\n", " ").replace("`", "")
    t = re.sub(r"\s+", " ", t).strip()
    return speakable(t) if make_speakable else t


# ------------------------------------------------------------------ reference picking
def pick_reference(s: Path, min_s=7.0, max_s=14.0) -> tuple[Path, str]:
    out = s / "segments" / "ref"
    out.mkdir(parents=True, exist_ok=True)
    ref_wav, ref_txt = out / "global_ref.wav", out / "global_ref.txt"
    if ref_wav.exists() and ref_txt.exists():
        return ref_wav, ref_txt.read_text(encoding="utf-8")
    asr = read_json(require(s / "metadata" / "asr.json", "asr"))["result"]["utterances"]
    words = [w for u in asr for w in u.get("words", [])]
    words.sort(key=lambda w: w["start_time"])
    voc = sf.SoundFile(str(s / "media" / "audio_vocals.wav"))
    bgm = sf.SoundFile(str(s / "media" / "audio_bgm.wav"))
    sr = voc.samplerate
    best, best_score = None, -1e9
    i = 0
    # candidate windows: runs of words with no pause > 600 ms, length in [min_s, max_s]
    while i < len(words):
        j = i
        while j + 1 < len(words) and words[j + 1]["start_time"] - words[j]["end_time"] < 600 \
                and (words[j + 1]["end_time"] - words[i]["start_time"]) <= max_s * 1000:
            j += 1
        dur = (words[j]["end_time"] - words[i]["start_time"]) / 1000
        if dur >= min_s:
            ws = words[i:j + 1]
            prob = float(np.mean([w.get("prob", 1.0) for w in ws]))
            st, en = ws[0]["start_time"], ws[-1]["end_time"]
            voc.seek(st * sr // 1000); v = voc.read((en - st) * sr // 1000, dtype="float32", always_2d=True).mean(1)
            bgm.seek(st * sr // 1000); b = bgm.read((en - st) * sr // 1000, dtype="float32", always_2d=True).mean(1)
            ve, be = float(np.sqrt((v ** 2).mean() + 1e-12)), float(np.sqrt((b ** 2).mean() + 1e-12))
            leak_db = 20 * np.log10(ve / be)
            fr = np.lib.stride_tricks.sliding_window_view(v, int(0.05 * sr))[:: int(0.025 * sr)]
            frms = np.sqrt((fr ** 2).mean(1) + 1e-12)
            voiced = float((frms > 0.1 * frms.max()).mean())
            clip = float((np.abs(v) > 0.98).mean())
            score = prob * 10 + min(leak_db, 30) / 3 + voiced * 5 - clip * 100 - abs(dur - 10) * 0.3
            if score > best_score:
                best_score, best = score, (st, en, "".join(w["text"] for w in ws).strip())
        i = j + 1
    if best is None:
        raise RuntimeError("no suitable reference window found")
    st, en, text = best
    st, en = max(0, st - 60), en + 120
    voc.seek(st * sr // 1000)
    y = voc.read((en - st) * sr // 1000, dtype="float32", always_2d=True)
    sf.write(str(ref_wav), y, sr, subtype="PCM_16")
    ref_txt.write_text(text, encoding="utf-8")
    log("tts", f"picked global reference {st/1000:.1f}-{en/1000:.1f}s score={best_score:.2f}")
    return ref_wav, text


# ------------------------------------------------------------------ synthesis
def load_model(optimize: bool):
    from voxcpm import VoxCPM
    path = os.getenv("VOXCPM_MODEL_DIR") or str(config.repo_path(config.get("paths.voxcpm_model_dir")))
    return VoxCPM.from_pretrained(path, load_denoiser=False, optimize=optimize)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--mode", choices=["ref", "seg", "ult"], default=config.get("tts.mode", "ult"))
    ap.add_argument("--only", default=None, help="comma list of 1-based indices")
    ap.add_argument("--out", default=None, help="output dir (default segments/tts)")
    ap.add_argument("--cfg", type=float, default=float(config.get("tts.cfg", 2.0)))
    ap.add_argument("--steps", type=int, default=int(config.get("tts.steps", 10)))
    ap.add_argument("--style", default="", help="optional VoxCPM2 style prefix, e.g. '(relaxed lecture tone)'")
    # torch.compile warm-up hangs on Windows (inductor); default to eager. Measured eager RTF ~1.0 on RTX 5080.
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seed", type=int, default=config.get("tts.seed", None))
    ap.add_argument("--speakable", action="store_true", help="spell out vowel-less code tokens")
    a = ap.parse_args()

    s = session_dir(a.id)
    tr = read_json(require(s / "metadata" / "translation.zh.json", "translation"))["translation"]
    vocals_dir = s / "segments" / "vocals"
    out_dir = Path(a.out) if a.out else s / "segments" / "tts"
    out_dir.mkdir(parents=True, exist_ok=True)
    idxs = [int(x) for x in a.only.split(",") if x.strip()] if a.only else list(range(1, len(tr) + 1))
    meta_path = out_dir / "_tts_meta.json"
    meta = read_json(meta_path) if meta_path.exists() else {}

    def up_to_date(i: int) -> bool:
        """File exists and was synthesised from the current target text (legacy clips w/o text are trusted)."""
        if not (out_dir / f"{i:04d}.wav").exists():
            return False
        m = meta.get(str(i))
        return m is None or m.get("dst") is None or m["dst"] == target_text(tr[i - 1])

    # original-audio lines never need the model
    todo = []
    for i in idxs:
        it, p = tr[i - 1], out_dir / f"{i:04d}.wav"
        if audio_mode(it) == "original":
            if p.exists() and not a.force:
                continue
            src = require(vocals_dir / f"{i:04d}.wav", "vocal segment")
            y, sr = sf.read(str(src), dtype="float32")
            sf.write(str(p), y, sr, subtype="PCM_16")
            continue
        if not a.force and up_to_date(i):
            continue
        todo.append(i)
    if not todo:
        log("tts", "nothing to do")
        return

    import torch
    ref_wav, ref_text = pick_reference(s)
    model = load_model(optimize=a.optimize)
    tm = model.tts_model
    sr_out = tm.sample_rate
    if a.mode == "ult":
        global_cache = tm.build_prompt_cache(prompt_text=ref_text, prompt_wav_path=str(ref_wav),
                                             reference_wav_path=str(ref_wav))
    else:
        global_cache = tm.build_prompt_cache(prompt_text=None, prompt_wav_path=None,
                                             reference_wav_path=str(ref_wav))
    t0, audio_s = time.time(), 0.0
    log("tts", f"synthesising {len(todo)} clips (mode={a.mode} cfg={a.cfg} seed={a.seed} speakable={a.speakable})")
    for n, i in enumerate(todo, 1):
        it = tr[i - 1]
        if a.seed is not None:  # per-line seed: a resumed run reproduces the same takes
            torch.manual_seed(int(a.seed) * 100003 + i)
            np.random.seed((int(a.seed) * 100003 + i) % (2 ** 32))
        body = tts_text(it, a.speakable)
        text = (a.style + body) if a.style else body
        cache = global_cache
        if a.mode == "seg":
            ref = vocals_dir / f"{i:04d}.wav"
            if ref.exists() and sf.info(str(ref)).duration * 1000 >= MIN_REFERENCE_MS:
                cache = tm.build_prompt_cache(prompt_text=None, prompt_wav_path=None, reference_wav_path=str(ref))
        res = tm._generate_with_prompt_cache(target_text=text, prompt_cache=cache, min_len=2, max_len=4096,
                                             inference_timesteps=a.steps, cfg_value=a.cfg, retry_badcase=True,
                                             retry_badcase_max_times=3, retry_badcase_ratio_threshold=6.0,
                                             streaming=False)
        wav, _, _ = next(res)
        try:
            res.close()
        except Exception:
            pass
        y = wav.squeeze(0).float().cpu().numpy()
        tmp = out_dir / f".{i:04d}.pending.wav"
        sf.write(str(tmp), y, sr_out, subtype="PCM_16")
        tmp.replace(out_dir / f"{i:04d}.wav")
        audio_s += len(y) / sr_out
        meta[str(i)] = {"mode": a.mode, "cfg": a.cfg, "steps": a.steps, "seed": a.seed, "speakable": a.speakable,
                        "dst": target_text(it), "text": text, "dur": round(len(y) / sr_out, 3)}
        write_json(meta_path, meta)
        if n % 20 == 0 or n == len(todo):
            el = time.time() - t0
            log("tts", f"Prepared {n}/{len(todo)} TTS clips; RTF={el/max(audio_s,1e-6):.2f}; "
                       f"ETA {(el/n)*(len(todo)-n)/60:.1f} min")
    write_json(meta_path, meta)


if __name__ == "__main__":
    main()
