"""TTS QA: back-transcribe TTS clips with Whisper (zh) -> CER vs target text, plus speaker
similarity (CAM++ embedding cosine) to the original speaker reference and per-line original clip.

Incremental: a row is reused while the clip's mtime and the target text are unchanged.
Only audio_mode == "tts" lines are checked.

  python dub/s7b_tts_qa.py --id <sid> --dir <tts_dir> [--only 1,2,3] [--out qa.json] [--full]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

os.environ.setdefault("TQDM_DISABLE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import audio_mode, log, read_json, require, session_dir, target_text, write_json  # noqa: E402


def norm(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[^\u4e00-\u9fffa-z0-9]", "", t)
    return t


def cer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else 1.0
    d = np.arange(len(h) + 1)
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return float(d[len(h)]) / len(r)


def load16k(p: Path) -> np.ndarray:
    import librosa
    y, sr = sf.read(str(p), dtype="float32", always_2d=True)
    y = y.mean(1)
    return librosa.resample(y, orig_sr=sr, target_sr=16000, res_type="soxr_hq").astype(np.float32) if sr != 16000 else y


class Spk:
    def __init__(self):
        from funasr import AutoModel
        self.m = AutoModel(model=str(config.RUNTIME / "models" / "campplus"), disable_update=True,
                           disable_pbar=True, log_level="ERROR")

    def emb(self, y16: np.ndarray) -> np.ndarray:
        r = self.m.generate(input=y16)
        e = r[0]["spk_embedding"]
        e = e.detach().cpu().numpy().reshape(-1) if hasattr(e, "detach") else np.asarray(e).reshape(-1)
        return e / (np.linalg.norm(e) + 1e-9)


def cjk(t: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff]", t))


def cjk_cer(tgt: str, hyp: str) -> float:
    """CER on Chinese characters only (English code tokens are unreliable for zh ASR)."""
    return cer(cjk(tgt), cjk(hyp)) if cjk(tgt) else cer(tgt, hyp)


def qa_prompt(s: Path) -> str:
    base = "以下是普通话的句子。"
    gp = s / "metadata" / "translate_jobs" / "glossary.json"
    if gp.exists():
        terms = [t["term"] for t in read_json(gp)["terms"][:12]]
        if terms:
            base += "其中可能出现：" + "、".join(terms) + "。"
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-spk", action="store_true")
    ap.add_argument("--full", action="store_true", help="re-check every clip (default: incremental)")
    a = ap.parse_args()
    s = session_dir(a.id)
    tr = read_json(require(s / "metadata" / "translation.zh.json", "translation"))["translation"]
    d = Path(a.dir)
    out = Path(a.out) if a.out else d / "_qa.json"
    idxs = [int(x) for x in a.only.split(",") if x.strip()] if a.only else \
        sorted(int(p.stem) for p in d.glob("[0-9][0-9][0-9][0-9].wav"))
    idxs = [i for i in idxs if 1 <= i <= len(tr) and audio_mode(tr[i - 1]) == "tts" and (d / f"{i:04d}.wav").exists()]
    old = {r["i"]: r for r in read_json(out)["rows"]} if out.exists() else {}

    def fresh(i: int) -> bool:
        r = old.get(i)
        return (r is not None and not a.full and r.get("wav_mtime") == os.path.getmtime(d / f"{i:04d}.wav")
                and r.get("dst") == target_text(tr[i - 1]))

    todo = [i for i in idxs if not fresh(i)]
    rows = dict(old)
    if todo:
        import torch
        import whisper
        root = config.get("paths.whisper_download_root") or str(Path.home() / ".cache" / "whisper")
        wm = whisper.load_model(config.get("asr.model", "large-v3-turbo"), device="cuda", download_root=root)
        spk = None if a.no_spk else Spk()
        ref_e = spk.emb(load16k(s / "segments" / "ref" / "global_ref.wav")) if spk else None
        prompt = qa_prompt(s)
        for i in todo:
            p = d / f"{i:04d}.wav"
            y = load16k(p)
            dur = len(y) / 16000
            hyp = wm.transcribe(y, language="zh", fp16=True, condition_on_previous_text=False,
                                initial_prompt=prompt)["text"]
            tgt = target_text(tr[i - 1])
            row = {"i": i, "dur": round(dur, 2),
                   "slot": round((tr[i - 1]["end_time"] - tr[i - 1]["start_time"]) / 1000, 2),
                   "cer": round(cer(tgt, hyp), 3), "cjk_cer": round(cjk_cer(tgt, hyp), 3), "hyp": hyp,
                   "dst": tgt, "wav_mtime": os.path.getmtime(p)}
            if spk:
                e = spk.emb(y)
                row["sim_ref"] = round(float(e @ ref_e), 3)
                orig = s / "segments" / "vocals" / f"{i:04d}.wav"
                if orig.exists():
                    row["sim_orig"] = round(float(e @ spk.emb(load16k(orig))), 3)
            rows[i] = row
        del wm
        torch.cuda.empty_cache()
    write_json(out, {"rows": [rows[k] for k in sorted(rows)]})
    cur = [rows[i] for i in idxs if i in rows]
    if not cur:
        log("tts_qa", f"{d.name}: nothing to check")
        return
    c = np.array([r.get("cjk_cer", r["cer"]) for r in cur])
    ratio = np.array([r["dur"] / max(r["slot"], 0.1) for r in cur])
    msg = (f"{d.name}: checked {len(todo)} new, n={len(cur)} CJK-CER mean={c.mean():.3f} median={np.median(c):.3f} "
           f">0.2:{(c>0.2).sum()} | dur/slot median={np.median(ratio):.2f}")
    sims = [r["sim_ref"] for r in cur if "sim_ref" in r]
    if sims:
        msg += f" | sim_ref mean={np.mean(sims):.3f}"
    log("tts_qa", msg)


if __name__ == "__main__":
    main()
