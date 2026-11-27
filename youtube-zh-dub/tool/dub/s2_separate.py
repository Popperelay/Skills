"""Stage 2 · separate (Demucs htdemucs_ft) -> media/audio_vocals.wav, media/audio_bgm.wav

Mirrors the reference adapter: chunked windows (600 s owned + 10 s overlap read-ahead),
linear crossfade on the overlap tail, global peak scaling 1/max(1.01*peak, 1),
pending -> final atomic publish, RF64 for large intermediates.
Deviation: SHIFTS defaults to 1 (lecture audio, no music) to cut runtime ~3x.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import FFMPEG, FFPROBE, log, require, session_dir  # noqa: E402

CHUNK_SECONDS = 600
OVERLAP_SECONDS = 10
FINALIZE_BLOCK_FRAMES = 1 << 20


def chunk_plan(total, chunk, overlap):
    plan, start = [], 0
    while start < total:
        stop = min(start + chunk, total)
        if total - stop <= overlap:
            stop = total
        plan.append((start, stop, min(stop + overlap, total)))
        start = stop
    return plan


def crossfade(tail, head):
    n = min(len(tail), len(head))
    if n <= 0:
        return head
    ramp = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32).reshape(-1, 1)
    out = head.copy()
    out[:n] = tail[:n] * (1 - ramp) + head[:n] * ramp
    return out


def probe_channels(video: Path) -> int:
    r = subprocess.run([FFPROBE(), "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=channels", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                       check=True, capture_output=True, text=True)
    return int(r.stdout.strip().splitlines()[0])


def finalize(raw: Path, dst: Path, peak: float, sr: int, ch: int):
    scale = 1.0 / max(1.01 * peak, 1.0)
    with sf.SoundFile(str(raw)) as r, sf.SoundFile(str(dst), "w", samplerate=sr, channels=ch,
                                                   subtype="PCM_16", format="RF64") as w:
        while True:
            blk = r.read(FINALIZE_BLOCK_FRAMES, dtype="float32", always_2d=True)
            if not len(blk):
                break
            w.write(blk * scale)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--shifts", type=int, default=int(config.get("separate.shifts", 1)))
    ap.add_argument("--model", default=config.get("separate.model", "htdemucs_ft"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    s = session_dir(a.id)
    video = require(s / "media" / "video_source.mp4", "video_source")
    vocals, bgm = s / "media" / "audio_vocals.wav", s / "media" / "audio_bgm.wav"
    if vocals.exists() and bgm.exists() and not a.force:
        log("separate", "reuse cached stems")
        return
    vp, bp = s / "media" / ".audio_vocals.pending.wav", s / "media" / ".audio_bgm.pending.wav"
    tin, vraw, braw = s / "tmp" / "demucs_input.wav", s / "tmp" / "demucs_vocals.raw.rf64", s / "tmp" / "demucs_bgm.raw.rf64"
    for p in (vocals, bgm, vp, bp, tin, vraw, braw):
        p.unlink(missing_ok=True)

    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model(a.model)
    model.eval()
    sr, ch = model.samplerate, model.audio_channels
    src_ch = probe_channels(video)
    pan = "pan=stereo|c0=c0|c1=c0" if src_ch == 1 else "pan=stereo|c0=c0|c1=c1"
    subprocess.run([FFMPEG(), "-y", "-loglevel", "error", "-i", str(video), "-vn", "-map", "0:a:0",
                    "-af", pan, "-ar", str(sr), "-c:a", "pcm_f32le", "-rf64", "auto", str(tin)], check=True)

    # global normalisation stats (demucs convention: per-track mean/std of mono mix)
    acc, acc2, n = 0.0, 0.0, 0
    with sf.SoundFile(str(tin)) as r:
        while True:
            blk = r.read(FINALIZE_BLOCK_FRAMES, dtype="float32", always_2d=True)
            if not len(blk):
                break
            m = blk.mean(1).astype(np.float64)
            acc += m.sum(); acc2 += (m * m).sum(); n += len(m)
    mean = acc / n
    std = max(np.sqrt(acc2 / n - mean * mean), 1e-8)

    peaks = {"vocals": 0.0, "bgm": 0.0}
    tails = {"vocals": None, "bgm": None}
    try:
        with sf.SoundFile(str(tin)) as reader, \
             sf.SoundFile(str(vraw), "w", samplerate=sr, channels=ch, subtype="FLOAT", format="RF64") as vw, \
             sf.SoundFile(str(braw), "w", samplerate=sr, channels=ch, subtype="FLOAT", format="RF64") as bw:
            plan = chunk_plan(len(reader), CHUNK_SECONDS * sr, OVERLAP_SECONDS * sr)
            if not plan:
                raise RuntimeError(f"Extracted audio is empty: {tin}")
            vi = model.sources.index("vocals")
            for idx, (own_start, own_stop, read_stop) in enumerate(plan):
                reader.seek(own_start)
                win = reader.read(read_stop - own_start, dtype="float32", always_2d=True)
                mix = torch.from_numpy(np.ascontiguousarray(win.T))
                mix = (mix - mean) / std
                with torch.no_grad():
                    out = apply_model(model, mix[None], device="cuda", shifts=a.shifts, split=True,
                                      overlap=0.25, progress=False)[0]
                out = out * std + mean
                stems = out.cpu().numpy()  # (S, C, T)
                voc = stems[vi].T
                acc_ = sum(stems[j] for j in range(stems.shape[0]) if j != vi).T
                own = own_stop - own_start
                for name, samples, w in (("vocals", voc, vw), ("bgm", acc_, bw)):
                    core = samples[:own]
                    if tails[name] is not None:
                        core = crossfade(tails[name], core)
                    w.write(core.astype(np.float32))
                    peaks[name] = max(peaks[name], float(np.abs(core).max(initial=0.0)))
                    tails[name] = samples[own:].copy()
                del mix, out, stems, voc, acc_
                torch.cuda.empty_cache()
                log("separate", f"part {idx+1}/{len(plan)} done")
        finalize(vraw, vp, peaks["vocals"], sr, ch)
        finalize(braw, bp, peaks["bgm"], sr, ch)
        vp.replace(vocals)
        bp.replace(bgm)
    except BaseException:
        for p in (vocals, bgm, vp, bp):
            p.unlink(missing_ok=True)
        raise
    finally:
        for p in (tin, vraw, braw, vp, bp):
            p.unlink(missing_ok=True)
    log("separate", f"ok -> {vocals.name}, {bgm.name} (sr={sr}, peaks={peaks})")


if __name__ == "__main__":
    main()
