"""Stage 6 · split_audio -> segments/vocals/{i:04d}.wav  (start-80 ms, end+160 ms, i from 1)

Fingerprinted: segments/vocals/_index.json remembers the [start, end] each clip was cut from, so a
re-run only re-cuts lines whose timing changed (resume-safe, no bulk deletion)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, read_json, require, session_dir, write_json  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    s = session_dir(a.id)
    vocals = require(s / "media" / "audio_vocals.wav", "audio_vocals")
    tr = read_json(require(s / "metadata" / "translation.zh.json", "translation"))["translation"]
    out = s / "segments" / "vocals"
    out.mkdir(parents=True, exist_ok=True)
    idx_p = out / "_index.json"
    index = read_json(idx_p) if idx_p.exists() else {}
    made = 0
    with sf.SoundFile(str(vocals)) as f:
        sr, total = f.samplerate, f.frames
        for i, it in enumerate(tr, 1):
            p = out / f"{i:04d}.wav"
            span = [int(it["start_time"]), int(it["end_time"])]
            prev = index.get(str(i))
            if p.exists() and not a.force and (prev is None or prev == span):
                index.setdefault(str(i), span)  # legacy clips without index are trusted
                continue
            st = max(0, span[0] - 80) * sr // 1000
            en = min(total, (span[1] + 160) * sr // 1000)
            f.seek(st)
            y = f.read(en - st, dtype="float32", always_2d=True)
            tmp = out / f".{i:04d}.pending.wav"
            sf.write(str(tmp), y, sr, subtype="PCM_16")
            tmp.replace(p)
            index[str(i)] = span
            made += 1
    write_json(idx_p, index)
    log("split_audio", f"Created vocal reference segments: {made} new, {len(tr)} total")


if __name__ == "__main__":
    main()
