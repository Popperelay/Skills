"""Stage 8 · merge_audio -> tmp/audio_dubbing.wav, metadata/timings.json, segments/stretched/

Two-factor alignment (global base factor + per-line local factor), the only stage that
writes actual_start_time / actual_end_time.

Deviations from the reference adapter, all in favour of natural-sounding speech:
  * TTS clips are silence-trimmed and RMS-levelled before alignment (consistent loudness).
  * Slow-down is capped at 1.04 (stretching speech >4% slower sounds draggy); short clips
    simply leave silence instead of being dragged out.
  * An over-long line may borrow the silent gap before the next line's original start;
    only when that is not enough does it speed up further, down to ratio 0.78 (~1.28x).
  * Time-stretch uses ffmpeg rubberband (formant-preserving) when available, else atempo.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FFMPEG, audio_mode, log, read_json, require, session_dir, write_json  # noqa: E402

BASE_FACTOR_MIN, BASE_FACTOR_MAX, BASE_FACTOR_SAFETY = 0.80, 1.04, 0.99
RATIO_MIN, RATIO_MAX = 0.78, 1.04
SPEED_NOOP_EPSILON = 1e-2
TARGET_RMS_DBFS = -20.0
NEXT_GUARD_MS = 60


def _resample(y, sr_from, sr_to):
    if sr_from == sr_to:
        return y
    import librosa
    return librosa.resample(y, orig_sr=sr_from, target_sr=sr_to, res_type="soxr_hq").astype(np.float32)


def _trim_level(y: np.ndarray, sr: int, trim: bool = True) -> np.ndarray:
    import librosa
    if y.ndim > 1:
        y = y.mean(1)
    y = y.astype(np.float32)
    if trim:
        _, (a, b) = librosa.effects.trim(y, top_db=38, frame_length=1024, hop_length=256)
        pad = int(0.04 * sr)
        y = y[max(0, a - pad): min(len(y), b + pad)]
    # RMS over voiced frames only
    frame = int(0.03 * sr)
    if len(y) > frame:
        fr = np.lib.stride_tricks.sliding_window_view(y, frame)[::frame // 2]
        rms = np.sqrt((fr ** 2).mean(1) + 1e-12)
        thr = np.percentile(rms, 30)
        act = rms[rms >= thr]
        level = float(np.sqrt((act ** 2).mean())) if len(act) else float(np.sqrt((y ** 2).mean()))
    else:
        level = float(np.sqrt((y ** 2).mean() + 1e-12))
    gain = 10 ** (TARGET_RMS_DBFS / 20) / max(level, 1e-5)
    gain = min(gain, 8.0)
    y = y * gain
    peak = np.abs(y).max(initial=0.0)
    if peak > 0.97:
        y = y * (0.97 / peak)
    # 8 ms fades to avoid clicks
    f = min(len(y) // 2, int(0.008 * sr))
    if f > 0:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        y[:f] *= ramp
        y[-f:] *= ramp[::-1]
    return y


_FILTERS = None


def _has_filter(name: str) -> bool:
    global _FILTERS
    if _FILTERS is None:
        r = subprocess.run([FFMPEG(), "-hide_banner", "-filters"], capture_output=True, text=True)
        _FILTERS = r.stdout
    return f" {name} " in _FILTERS


def _stretch(y: np.ndarray, sr: int, ratio: float, tmp_dir: Path, name: str) -> np.ndarray:
    """ratio = target_len / source_len (<1 => faster)."""
    if abs(ratio - 1.0) < SPEED_NOOP_EPSILON:
        return y
    src, dst = tmp_dir / "_stretch_in.wav", tmp_dir / f"{name}.wav"  # src is overwritten, never deleted
    sf.write(str(src), y, sr, subtype="FLOAT")
    tempo = 1.0 / ratio
    filters = [f"atempo={tempo:.5f}"]
    if _has_filter("rubberband"):
        filters.insert(0, f"rubberband=tempo={tempo:.5f}:pitch=1:transients=smooth:detector=soft:phase=laminar:window=standard:formant=preserved:pitchq=quality")
    for af in filters:
        proc = subprocess.run([FFMPEG(), "-y", "-loglevel", "error", "-i", str(src), "-af", af, "-ar", str(sr),
                               "-c:a", "pcm_f32le", str(dst)])
        if proc.returncode == 0:
            break
        if af == filters[-1]:
            proc.check_returncode()
        log("merge_audio", f"retrying clip {name} with atempo (rubberband exit {proc.returncode})")
    out, _ = sf.read(str(dst), dtype="float32")
    return out[: int(round(len(y) * ratio))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="preview: only first N lines -> *.preview.* files")
    a = ap.parse_args()
    s = session_dir(a.id)
    suffix = ".preview" if a.limit else ""
    dubbing, timings = s / "tmp" / f"audio_dubbing{suffix}.wav", s / "metadata" / f"timings{suffix}.json"
    if dubbing.exists() and timings.exists() and not a.force:
        log("merge_audio", "reuse cached dubbing + timings")
        return
    tr = read_json(require(s / "metadata" / "translation.zh.json", "translation"))["translation"]
    if a.limit:
        tr = tr[: a.limit]
    tts_dir = s / "segments" / "tts"
    stretched = s / "segments" / "stretched"
    stretched.mkdir(parents=True, exist_ok=True)
    files = [tts_dir / f"{i:04d}.wav" for i in range(1, len(tr) + 1)]
    for p in files:
        if not p.exists():
            raise FileNotFoundError(f"Missing TTS segment: {p}")
    sr = sf.info(str(files[0])).samplerate
    vocals_info = sf.info(str(s / "media" / "audio_vocals.wav"))
    total_ms = vocals_info.frames / vocals_info.samplerate * 1000
    if a.limit:
        total_ms = min(total_ms, tr[-1]["end_time"] + 2000)

    clips = []
    for it, p in zip(tr, files):
        y, r = sf.read(str(p), dtype="float32", always_2d=True)
        y = y.mean(1)
        if audio_mode(it) == "tts":
            y = _trim_level(_resample(y, r, sr), sr)
        else:
            y = _trim_level(_resample(y, r, sr), sr, trim=False)  # original: level only, keep exact span
        clips.append(y)

    cur_total = sum(len(c) / sr for c, it in zip(clips, tr) if audio_mode(it) == "tts")
    des_total = sum(max(0, (it["end_time"] - it["start_time"]) / 1000) for it in tr if audio_mode(it) == "tts")
    base = 1.0 if cur_total <= 0 else min(BASE_FACTOR_MAX, max(BASE_FACTOR_MIN, des_total / cur_total * BASE_FACTOR_SAFETY))
    log("merge_audio", f"cur_total={cur_total:.1f}s des_total={des_total:.1f}s base={base:.3f}")

    pieces, cursor = [], 0  # cursor in samples
    drifts, ratios = [], []
    for idx, (it, y) in enumerate(zip(tr, clips)):
        last_end_ms = cursor / sr * 1000
        real_start = max(float(it["start_time"]), last_end_ms)
        if real_start * sr / 1000 > cursor:
            n = int(round(real_start * sr / 1000)) - cursor
            pieces.append(np.zeros(n, dtype=np.float32)); cursor += n
        real_start = cursor / sr * 1000
        if audio_mode(it) == "original":
            n = int(round((it["end_time"] - it["start_time"]) * sr / 1000))
            out = y[:n]
            ratio = 1.0
        else:
            cur = len(y) / sr
            nxt = tr[idx + 1]["start_time"] if idx + 1 < len(tr) else total_ms
            avail_end = max(float(it["end_time"]), float(nxt) - NEXT_GUARD_MS)
            avail = max(0.05, (avail_end - real_start) / 1000)
            ratio = base
            if cur * ratio > avail:
                ratio = avail / cur
            ratio = min(RATIO_MAX, max(RATIO_MIN, ratio))
            out = _stretch(y, sr, ratio, stretched, f"{idx+1:04d}")
        pieces.append(out.astype(np.float32)); cursor += len(out)
        real_end = cursor / sr * 1000
        it["actual_start_time"] = int(real_start)
        it["actual_end_time"] = int(max(real_end, float(it["end_time"])) if audio_mode(it) == "tts" else real_end)
        drifts.append(real_start - it["start_time"]); ratios.append(ratio)
    if cursor < total_ms * sr / 1000:
        pieces.append(np.zeros(int(total_ms * sr / 1000) - cursor, dtype=np.float32))
    final = np.concatenate(pieces)
    sf.write(str(dubbing), final, sr, subtype="PCM_16")
    write_json(timings, {"translation": tr})
    d, r = np.array(drifts) / 1000, np.array(ratios)
    log("merge_audio", f"ok -> {dubbing.name} {len(final)/sr:.1f}s; drift mean={d.mean():.2f}s p95={np.percentile(d,95):.2f}s "
                       f"max={d.max():.2f}s; ratio min={r.min():.3f} p5={np.percentile(r,5):.3f} median={np.median(r):.3f}; "
                       f"lines with drift>1.5s: {(d>1.5).sum()}")


if __name__ == "__main__":
    main()
