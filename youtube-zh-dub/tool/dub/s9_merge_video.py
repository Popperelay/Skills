"""Stage 9 · merge_video -> media/video_final.mp4 (+ tmp/audio_mixed.m4a, metadata/subtitles.zh.srt)

output_mode=both: subtitle source = metadata/timings.json (actual_start_time/actual_end_time only).
Mix: [dub]volume=1.0 + [bgm]volume=0.30, amix normalize=0 (reference), plus alimiter (deviation:
clip protection).  Video: libx264 -preset fast -crf 23, +faststart, temp file + atomic replace.
Extra: original English audio kept as a 2nd (non-default) audio track.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import FFMPEG, FFPROBE, log, read_json, require, session_dir, target_text  # noqa: E402

SUBTITLE_PUNCTUATION = set("，,；;：:。?？!！、")
SUBTITLE_PROTECTED_PAIRS = {"《": "》", "（": "）", "【": "】", "「": "」", "『": "』", "(": ")", "“": "”"}
SUBTITLE_CLOSING_QUOTES = set('"\'」』》）】\u201d\u2019])')
SUBTITLE_MIN_FRAGMENT_LEN = 5
SUBTITLE_MIN_DURATION_MS = 200
SUBTITLE_TAIL_BUFFER_MS = 100
SUBTITLE_DURATION_FLOOR_MS = 600
MAX_LINE_UNITS = 22  # landscape: keep one line on screen


def _srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_protected(text: str):
    segs, buf, stack = [], "", []
    for ch in text:
        buf += ch
        if ch in SUBTITLE_PROTECTED_PAIRS:
            stack.append(SUBTITLE_PROTECTED_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif not stack and ch in SUBTITLE_PUNCTUATION:
            segs.append(buf); buf = ""
    if buf:
        segs.append(buf)
    return segs


def _units(t: str) -> float:
    return len(re.findall(r"[\u4e00-\u9fff]", t)) + len(re.findall(r"[A-Za-z0-9_.]+", t)) * 1.6


def split_subtitle_text(text: str, max_units: float = MAX_LINE_UNITS):
    text = text.strip()
    if not text:
        return []
    segs = _split_protected(text)
    # attach closing quotes
    fixed = []
    for sgm in segs:
        if fixed and sgm and sgm[0] in SUBTITLE_CLOSING_QUOTES:
            fixed[-1] += sgm[0]; sgm = sgm[1:]
        if sgm:
            fixed.append(sgm)
    # greedily pack fragments into display lines (min len 5, max ~max_units)
    lines, cur = [], ""
    for sgm in fixed:
        if not cur:
            cur = sgm
        elif len(cur.strip()) < SUBTITLE_MIN_FRAGMENT_LEN or _units(cur + sgm) <= max_units:
            cur += sgm
        else:
            lines.append(cur); cur = sgm
    if cur:
        if lines and len(cur.strip()) < SUBTITLE_MIN_FRAGMENT_LEN:
            lines[-1] += cur
        else:
            lines.append(cur)
    out = []
    for ln in lines:
        ln = re.sub(r"[，,。]+$", "", ln.strip())
        ln = re.sub(r"\s+", " ", ln)
        if ln:
            out.append(ln)
    return out or [text]


def _allocate(frags, total):
    w = [max(1, len(f.replace(" ", ""))) for f in frags]
    tw = sum(w)
    res, alloc = [], 0
    for k, f in enumerate(frags):
        if k == len(frags) - 1:
            res.append(max(100, total - alloc)); break
        share = round(total * w[k] / tw)
        if total >= SUBTITLE_DURATION_FLOOR_MS:
            share = max(SUBTITLE_MIN_DURATION_MS, min(share, total - alloc - SUBTITLE_TAIL_BUFFER_MS))
        else:
            share = max(SUBTITLE_MIN_DURATION_MS // 2, share)
        res.append(share); alloc += share
    return res


def write_srt(timings_file: Path, s: Path, name: str = "subtitles.zh.srt") -> Path:
    tr = read_json(timings_file)["translation"]
    out = s / "metadata" / name
    lines, idx = [], 1
    for it in tr:
        st = it.get("actual_start_time", it["start_time"])
        en = it.get("actual_end_time", it["end_time"])
        if en <= st:
            continue
        frags = split_subtitle_text(target_text(it))
        cursor = st
        for f, d in zip(frags, _allocate(frags, en - st)):
            lines += [str(idx), f"{_srt_time(cursor)} --> {_srt_time(cursor + d)}", f, ""]
            cursor += d; idx += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def probe_size(video: Path):
    r = subprocess.run([FFPROBE(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().splitlines()[0].split(",", 1)
        return int(w), int(h)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--bgm-volume", type=float, default=float(config.get("mix.bgm_volume", 0.30)))
    ap.add_argument("--font", default=config.get("subtitles.hard_font", "Microsoft YaHei"))
    ap.add_argument("--font-size", type=int, default=int(config.get("subtitles.hard_font_size", 15)))
    ap.add_argument("--start", type=float, default=None, help="preview: start seconds")
    ap.add_argument("--duration", type=float, default=None, help="preview: duration seconds")
    ap.add_argument("--no-hard", action="store_true", help="only build tmp/audio_mixed.m4a (+ SRT)")
    a = ap.parse_args()
    s = session_dir(a.id).resolve()
    preview = a.start is not None
    sfx = ".preview" if preview else ""
    video = require(s / "media" / "video_source.mp4", "video_source")
    dubbing = require(s / "tmp" / f"audio_dubbing{sfx}.wav", "audio_dubbing")
    bgm = require(s / "media" / "audio_bgm.wav", "audio_bgm")
    timings = require(s / "metadata" / f"timings{sfx}.json", "timings")
    srt_name = f"subtitles{sfx}.zh.srt"
    srt = write_srt(timings, s, srt_name)
    log("merge_video", f"subtitles -> {srt}")

    mixed = s / "tmp" / f"audio_mixed{sfx}.m4a"
    fc = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.0[a0];"
          f"[1:a]aresample=48000,aformat=channel_layouts=stereo,volume={a.bgm_volume}[a1];"
          f"[a0][a1]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=disabled[aout]")
    subprocess.run([FFMPEG(), "-y", "-loglevel", "error", "-i", str(dubbing), "-i", str(bgm), "-filter_complex", fc,
                    "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", str(mixed)], check=True)
    log("merge_video", f"mixed -> {mixed}")
    if a.no_hard and not preview:
        log("merge_video", "ok -> mix only (hard-sub output disabled)")
        return

    size = probe_size(video)
    portrait = bool(size and size[1] > size[0])
    margin_v = 70 if portrait else 8
    style = (f"FontName={a.font},FontSize={a.font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
             f"BorderStyle=1,Outline=1.4,Shadow=0.5,Alignment=2,MarginV={margin_v}")
    vf = f"subtitles=filename='metadata/{srt_name}':force_style='{style}'"
    final = s / "media" / ("video_preview.mp4" if preview else "video_final.mp4")
    tmp = s / "media" / f".video_final.{uuid.uuid4().hex}.mp4"
    cmd = [FFMPEG(), "-y", "-loglevel", "error", "-stats"]
    if preview:
        cmd += ["-ss", str(a.start), "-t", str(a.duration or 60)]
        vf = f"setpts=PTS+{a.start}/TB," + vf + ",setpts=PTS-STARTPTS"
    cmd += ["-i", str(video)]
    if preview:
        cmd += ["-ss", str(a.start), "-t", str(a.duration or 60)]
    cmd += ["-i", str(mixed), "-vf", vf,
            "-map", "0:v:0", "-map", "1:a:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-metadata:s:a:0", "language=chi", "-metadata:s:a:0", "title=中文配音",
            "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English (original)",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            "-movflags", "+faststart", "-shortest", str(tmp)]
    try:
        subprocess.run(cmd, check=True, cwd=str(s))
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log("merge_video", f"ok -> {final} ({final.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
