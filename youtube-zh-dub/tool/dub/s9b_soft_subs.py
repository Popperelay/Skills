"""Stage merge_softsub · selectable subtitles: 关闭字幕 / 中英双语 / 中文 / English

Outputs (all inside the session, existing hard-sub artifacts are untouched):
  metadata/subtitles.zh.srt / .en.srt / .zh-en.srt        (for MP4 mov_text)
  metadata/subtitles.zh.ass / .en.ass / .zh-en.ass        (styled, for MKV)
  media/video_final_softsub.mkv                           (recommended: styled ASS tracks)
  media/video_final_softsub.mp4                           (compatibility: mov_text tracks)
Video = original stream copy (no burned subs, no re-encode); audio = dub mix (default) + English original.
Track 1 is an empty "关闭字幕" track marked default, so playback starts without subtitles.

Timing:
  zh    : actual_start_time/actual_end_time (dub timeline, same as the hard-sub track)
  en    : original word timestamps from asr.json (matches the speaker on screen)
  zh-en : zh fragments on the dub timeline; English words assigned to each fragment by
          proportional position inside the sentence (keeps every cue to ~1 zh + 1-2 en lines)
"""
from __future__ import annotations

import argparse
import bisect
import re
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import FFMPEG, log, read_json, require, session_dir, target_text  # noqa: E402
from s9_merge_video import _allocate, _srt_time, split_subtitle_text  # noqa: E402

EN_MAX_CHARS = 84        # per cue (ASS wraps to <=2 lines at 1080p)
EN_MAX_MS = 7000
EN_MIN_MS = 700
EN_TAIL_MS = 250


# ------------------------------------------------------------------ word buckets
def sentence_words(s: Path, tm):
    asr = read_json(Path(require(s / "metadata" / "asr.json", "asr")))["result"]["utterances"]
    words = sorted([w for u in asr for w in u["words"] if w["text"].strip()], key=lambda w: w["start_time"])
    starts = [t["start_time"] for t in tm]
    buckets = [[] for _ in tm]
    for w in words:
        mid = (w["start_time"] + w["end_time"]) / 2
        k = bisect.bisect_right(starts, mid) - 1
        if k >= 0 and mid <= tm[k]["end_time"] + 1:
            buckets[k].append(w)
    def flat(x):
        return re.sub(r"\s+", " ", x).strip()
    bad = [i for i, b in enumerate(buckets) if flat("".join(w["text"] for w in b)) != flat(tm[i]["src"])]
    if bad:
        # A zero-duration word at the shared boundary can belong to either sentence.
        # Recover the ASR sentence boundaries from the original word order and text.
        from s3_asr import _flatten
        ordered = _flatten(asr)
        buckets = []
        pos = 0
        for i, sentence in enumerate(tm):
            expected = flat(sentence["src"])
            start = pos
            while pos < len(ordered) and len(_join(ordered[start:pos])) < len(expected):
                pos += 1
            bucket = ordered[start:pos]
            if _join(bucket) != expected:
                raise RuntimeError(f"word sequence does not reproduce source text for line {i+1}")
            buckets.append(bucket)
        if pos != len(ordered):
            raise RuntimeError("unused ASR words after the last subtitle sentence")
    return buckets


def _join(ws):
    return re.sub(r"\s+", " ", "".join(w["text"] for w in ws)).strip()


# ------------------------------------------------------------------ cue builders
def zh_cues(tm):
    cues = []
    for it in tm:
        st, en = it.get("actual_start_time", it["start_time"]), it.get("actual_end_time", it["end_time"])
        if en <= st:
            continue
        frags = split_subtitle_text(target_text(it))
        cur = st
        for f, d in zip(frags, _allocate(frags, en - st)):
            cues.append((cur, cur + d, f)); cur += d
    return cues


def _split_en_words(ws):
    """Greedy chunking at clause punctuation / pauses, bounded by EN_MAX_CHARS and EN_MAX_MS."""
    chunks, cur = [], []
    for k, w in enumerate(ws):
        cand = cur + [w]
        too_long = len(_join(cand)) > EN_MAX_CHARS or (cand[-1]["end_time"] - cand[0]["start_time"]) > EN_MAX_MS
        if cur and too_long:
            # backtrack to the last clause boundary inside cur if it is not too early
            cut = None
            for j in range(len(cur) - 1, max(0, len(cur) - 7), -1):
                if re.search(r"[,;:.?!]$", cur[j]["text"].strip()):
                    cut = j + 1; break
            if cut and cut >= max(3, len(cur) // 2):
                chunks.append(cur[:cut]); cur = cur[cut:] + [w]
            else:
                chunks.append(cur); cur = [w]
        else:
            cur = cand
        nxt = ws[k + 1] if k + 1 < len(ws) else None
        if cur and nxt is not None and nxt["start_time"] - w["end_time"] > 700 and len(_join(cur)) >= 20:
            chunks.append(cur); cur = []
    if cur:
        if chunks and len(_join(cur)) < 12 and len(_join(chunks[-1] + cur)) <= EN_MAX_CHARS + 16:
            chunks[-1] += cur
        else:
            chunks.append(cur)
    return chunks


def en_cues(tm, buckets):
    raw = []
    for ws in buckets:
        for ch in _split_en_words(ws):
            raw.append([ch[0]["start_time"], ch[-1]["end_time"], _join(ch)])
    for k, c in enumerate(raw):  # tail hold + minimum duration, never overlapping the next cue
        nxt = raw[k + 1][0] if k + 1 < len(raw) else c[1] + 2000
        c[1] = min(max(c[1] + EN_TAIL_MS, c[0] + EN_MIN_MS), nxt - 10)
        c[1] = max(c[1], c[0] + 200)
    return [tuple(c) for c in raw]


BI_EN_TARGET_CHARS = 90   # bilingual: aim for <= ~90 English chars under each Chinese line


def bi_cues(tm, buckets):
    import math
    from s9_merge_video import MAX_LINE_UNITS, _units
    cues = []
    for it, ws in zip(tm, buckets):
        st, en = it.get("actual_start_time", it["start_time"]), it.get("actual_end_time", it["end_time"])
        if en <= st:
            continue
        zh = target_text(it)
        # a long English sentence under a short Chinese line would stack 3+ lines on screen:
        # split the Chinese finer (at its own punctuation) so each cue carries ~BI_EN_TARGET_CHARS of English
        k = max(1, math.ceil(len(_join(ws)) / BI_EN_TARGET_CHARS))
        max_units = min(MAX_LINE_UNITS, max(5.0, _units(zh) / k + 1))
        frags = split_subtitle_text(zh, max_units)
        durs = _allocate(frags, en - st)
        # fragment boundaries as proportions of the dub span
        bounds, acc = [0.0], 0
        for d in durs:
            acc += d; bounds.append(acc / (en - st))
        bounds[-1] = 1.0
        o0, o1 = it["start_time"], max(it["end_time"], it["start_time"] + 1)
        parts = [[] for _ in frags]
        for w in ws:
            q = ((w["start_time"] + w["end_time"]) / 2 - o0) / (o1 - o0)
            k = min(len(frags) - 1, max(0, bisect.bisect_right(bounds, q) - 1))
            parts[k].append(w)
        cur = st
        for f, d, p in zip(frags, durs, parts):
            cues.append((cur, cur + d, f, _join(p)))
            cur += d
    return cues


# ------------------------------------------------------------------ writers
def write_srt(path: Path, cues, bilingual=False):
    lines = []
    for n, c in enumerate(cues, 1):
        text = f"{c[2]}\n{c[3]}" if bilingual and c[3] else c[2]
        lines += [str(n), f"{_srt_time(c[0])} --> {_srt_time(c[1])}", text, ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ass_time(ms):
    ms = max(0, int(ms)); cs = ms // 10
    h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s_, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s_:02d}.{cs:02d}"


def _ass_esc(t: str) -> str:
    return t.replace("\\", "\u29f5").replace("{", "(").replace("}", ")").replace("\n", " ")


def ass_head(zh_font: str, en_font: str) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,{zh_font},54,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2.6,1,2,120,120,26,1
Style: EN,{en_font},44,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2.4,1,2,160,160,26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(path: Path, cues, kind: str):
    zf, ef = config.get("subtitles.zh_font", "Microsoft YaHei"), config.get("subtitles.en_font", "Segoe UI")
    out = [ass_head(zf, ef)]
    for c in cues:
        if kind == "zh":
            style, text = "ZH", _ass_esc(c[2])
        elif kind == "en":
            style, text = "EN", _ass_esc(c[2])
        else:
            style = "ZH"
            text = _ass_esc(c[2])
            if c[3]:
                text += rf"\N{{\fn{ef}\fs36\c&HD8D8D8&\bord2.2}}" + _ass_esc(c[3])
        out.append(f"Dialogue: 0,{_ass_time(c[0])},{_ass_time(c[1])},{style},,0,0,0,,{text}\n")
    path.write_text("".join(out), encoding="utf-8-sig")


# ------------------------------------------------------------------ mux
TRACKS = [("off", "zxx", "关闭字幕"), ("zh-en", "chi", "中英双语"), ("zh", "chi", "中文"), ("en", "eng", "English")]


def mux(s: Path, kind: str, default_key: str):
    """Video + both audio tracks stream-copied; subtitle tracks: 关闭字幕 / 中英双语 / 中文 / English.
    The empty "关闭字幕" track (default unless --default says otherwise) makes "no subtitles" the
    out-of-the-box state even in MP4 players that ignore disposition flags."""
    md = s / "metadata"
    empty = md / ".subtitles.off.ass"   # an empty SRT is rejected by the mov_text encoder; empty ASS is fine
    empty.write_text(ass_head("Arial", "Arial"), encoding="utf-8-sig")
    ext = "ass" if kind == "mkv" else "srt"
    srcs = [empty if key == "off" else md / f"subtitles.{key}.{ext}" for key, _, _ in TRACKS]
    video = require(s / "media" / "video_source.mp4", "video_source")
    mixed = require(s / "tmp" / "audio_mixed.m4a", "audio_mixed")
    final = s / "media" / f"video_final_softsub.{kind}"
    tmp = s / "media" / f".video_final_softsub.{uuid.uuid4().hex}.{kind}"
    cmd = [FFMPEG(), "-y", "-loglevel", "error", "-i", str(video), "-i", str(mixed)]
    for p in srcs:
        cmd += ["-i", str(p)]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-map", "0:a:0"]
    for k in range(len(srcs)):
        cmd += ["-map", f"{k + 2}:0"]
    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "ass" if kind == "mkv" else "mov_text",
            "-metadata:s:a:0", "language=chi", "-metadata:s:a:0", "title=中文配音",
            "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English (original)",
            "-disposition:a:0", "default", "-disposition:a:1", "0"]
    for k, (key, lang, title) in enumerate(TRACKS):
        cmd += [f"-metadata:s:s:{k}", f"language={lang}", f"-metadata:s:s:{k}", f"title={title}",
                f"-disposition:s:{k}", "default" if key == default_key else "0"]
    if kind == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += [str(tmp)]
    try:
        subprocess.run(cmd, check=True)
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log("soft_subs", f"ok -> {final} ({final.stat().st_size/1e6:.1f} MB)")
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--default", choices=["off", "zh", "en", "zh-en"], default="off")
    a = ap.parse_args()
    s = session_dir(a.id).resolve()
    tm = read_json(require(s / "metadata" / "timings.json", "timings"))["translation"]
    buckets = sentence_words(s, tm)
    zc, ec, bc = zh_cues(tm), en_cues(tm, buckets), bi_cues(tm, buckets)
    md = s / "metadata"
    write_srt(md / "subtitles.zh.srt", zc)
    write_srt(md / "subtitles.en.srt", ec)
    write_srt(md / "subtitles.zh-en.srt", bc, bilingual=True)
    write_ass(md / "subtitles.zh.ass", zc, "zh")
    write_ass(md / "subtitles.en.ass", ec, "en")
    write_ass(md / "subtitles.zh-en.ass", bc, "bi")
    log("soft_subs", f"cues: zh={len(zc)} en={len(ec)} zh-en={len(bc)}")
    for kind in ("mkv", "mp4"):
        mux(s, kind, a.default)


if __name__ == "__main__":
    main()
