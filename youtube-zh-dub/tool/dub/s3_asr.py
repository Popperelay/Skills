"""Stage 3 · asr (Whisper, word timestamps) -> metadata/asr.json
Stage 4 · asr_fix -> metadata/asr_fixed.json

asr_fix here *does* re-segment into sentences (word-timestamp based), then applies the
reference padding/clamp (start_pad=100, end_pad=300, min_gap=50).  Re-segmentation gives
sentence-complete units for translation and TTS; whisper's raw segments often cut mid-clause.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import ensure_ffmpeg_on_path, log, read_json, require, session_dir, source_info, write_json  # noqa: E402

PROMPT_MAX_CHARS = 400


def build_prompt(s: Path, override: str = "") -> str:
    """Whisper initial_prompt from source metadata: title, tags, first sentence of description.
    It biases spelling of names/jargon; it must stay short (Whisper keeps ~224 prompt tokens)."""
    if override:
        return override[:PROMPT_MAX_CHARS]
    info = source_info(s)
    parts = []
    title = (info.get("title") or "").strip()
    if title:
        parts.append(re.sub(r"[_\-]+", " ", title) + ".")
    who = info.get("uploader") or info.get("channel")
    if who:
        parts.append(f"By {who}.")
    tags = [t for t in (info.get("tags") or []) if isinstance(t, str)][:15]
    if tags:
        parts.append(", ".join(tags) + ".")
    desc = (info.get("description") or "").strip()
    if desc:
        first = re.split(r"(?<=[.!?])\s|\n", desc, maxsplit=1)[0]
        if not re.search(r"https?://", first):
            parts.append(first)
    return " ".join(parts)[:PROMPT_MAX_CHARS]


def detect_language(model, y16k: np.ndarray) -> tuple[str, float]:
    """Language ID on the most energetic 30 s window within the first 10 minutes."""
    import whisper
    win, hop = 30 * 16000, 10 * 16000
    head = y16k[: 600 * 16000]
    best, best_e = 0, -1.0
    for st in range(0, max(1, len(head) - win + 1), hop):
        e = float((head[st:st + win] ** 2).mean()) if len(head[st:st + win]) else 0.0
        if e > best_e:
            best, best_e = st, e
    clip = whisper.pad_or_trim(head[best: best + win])
    mel = whisper.log_mel_spectrogram(clip, n_mels=model.dims.n_mels).to(model.device)
    _, probs = model.detect_language(mel)
    lang = max(probs, key=probs.get)
    return lang, float(probs[lang])


def to_ms(x: float) -> int:
    return int(round(float(x) * 1000))


def run_asr(s: Path, model_name: str, prompt: str = "", force: bool = False) -> Path:
    out = s / "metadata" / "asr.json"
    if out.exists() and not force:
        log("asr", "reuse asr.json")
        return out
    ensure_ffmpeg_on_path()
    import librosa
    import soundfile as sf
    import torch
    import whisper

    vocals = require(s / "media" / "audio_vocals.wav", "audio_vocals")
    info = sf.info(str(vocals))
    duration_ms = int(round(info.frames / info.samplerate * 1000))
    y, sr = sf.read(str(vocals), dtype="float32", always_2d=True)
    y = y.mean(1)
    y = librosa.resample(y, orig_sr=sr, target_sr=16000, res_type="soxr_hq").astype(np.float32)
    log("asr", f"loaded vocals {duration_ms/1000:.1f}s; loading whisper {model_name}")
    root = config.get("paths.whisper_download_root") or str(Path.home() / ".cache" / "whisper")
    model = whisper.load_model(model_name, device="cuda", download_root=root)
    lang, p = detect_language(model, y)
    want = config.get("asr.language", "en")
    if lang != want:
        log("asr", f"WARNING: detected language {lang!r} (p={p:.2f}) but pipeline expects {want!r}; "
                   f"translation quality will suffer if the source is not {want}")
    else:
        log("asr", f"language check: {lang} p={p:.2f}")
    prompt = build_prompt(s, prompt)
    log("asr", f"initial_prompt: {prompt!r}")
    result = model.transcribe(y, language=want, word_timestamps=True, verbose=False,
                              condition_on_previous_text=False, initial_prompt=prompt or None, fp16=True)
    utts = []
    for seg in result.get("segments", []):
        words = [{"text": w["word"], "start_time": to_ms(w["start"]), "end_time": to_ms(w["end"]),
                  "prob": round(float(w.get("probability", 1.0)), 3)} for w in seg.get("words", [])]
        utts.append({"text": seg["text"].strip(), "start_time": to_ms(seg["start"]), "end_time": to_ms(seg["end"]),
                     "no_speech_prob": round(float(seg.get("no_speech_prob", 0)), 3),
                     "compression_ratio": round(float(seg.get("compression_ratio", 0)), 3),
                     "words": words})
    if not utts:
        raise RuntimeError("Whisper did not return any segments.")
    write_json(out, {"audio_info": {"duration": duration_ms},
                     "language_check": {"detected": lang, "prob": round(p, 3), "expected": want},
                     "initial_prompt": prompt,
                     "result": {"text": (result.get("text") or "").strip(), "utterances": utts}})
    del model
    torch.cuda.empty_cache()
    log("asr", f"ok -> {len(utts)} segments")
    return out


# ---------------------------------------------------------------- asr_fix
_ABBR = {"e.g.", "i.e.", "etc.", "vs.", "mr.", "dr.", "ms.", "mrs.", "approx."}
MAX_SENT_MS = 14000
MAX_SENT_WORDS = 42
GAP_BREAK_MS = 900
MERGE_SHORT_MS = 1200


def _ends_sentence(w: str) -> bool:
    t = w.strip()
    if not t:
        return False
    if t.lower() in _ABBR:
        return False
    return bool(re.search(r"[.?!]['\")\]]*$", t)) and not re.search(r"\d\.$", t[-3:] if len(t) > 2 else t)


def _ends_clause(w: str) -> bool:
    return bool(re.search(r"[,;:]['\")\]]*$", w.strip()))


def _flatten(utts):
    words = []
    for u in utts:
        if u.get("no_speech_prob", 0) > 0.8 and u.get("compression_ratio", 0) > 2.4:
            continue  # likely hallucination
        for w in u.get("words", []):
            if w["text"].strip() and w["end_time"] >= w["start_time"]:
                words.append(w)
    words.sort(key=lambda w: w["start_time"])
    return words


def _split_long(ws):
    """Recursively split an over-long word run at the best clause/gap boundary."""
    dur = ws[-1]["end_time"] - ws[0]["start_time"]
    if (dur <= MAX_SENT_MS and len(ws) <= MAX_SENT_WORDS) or len(ws) < 6:
        return [ws]
    best, best_score = None, -1e9
    n = len(ws)
    for i in range(2, n - 2):  # split after word i
        gap = ws[i + 1]["start_time"] - ws[i]["end_time"]
        balance = -abs((i + 1) / n - 0.5) * 600
        score = gap + balance + (500 if _ends_clause(ws[i]["text"]) else 0) + (900 if _ends_sentence(ws[i]["text"]) else 0)
        if score > best_score:
            best, best_score = i, score
    return _split_long(ws[: best + 1]) + _split_long(ws[best + 1:])


def resegment(utts):
    words = _flatten(utts)
    sents, cur = [], []
    for i, w in enumerate(words):
        if cur and w["start_time"] - cur[-1]["end_time"] >= GAP_BREAK_MS:
            sents.append(cur); cur = []
        cur.append(w)
        if _ends_sentence(w["text"]):
            sents.append(cur); cur = []
    if cur:
        sents.append(cur)
    out = []
    for ws in sents:
        out.extend(_split_long(ws))
    # merge very short runs into a neighbour when the gap is small
    merged = []
    for ws in out:
        dur = ws[-1]["end_time"] - ws[0]["start_time"]
        if merged:
            prev = merged[-1]
            gap = ws[0]["start_time"] - prev[-1]["end_time"]
            pdur = prev[-1]["end_time"] - prev[0]["start_time"]
            if (dur < MERGE_SHORT_MS or len(ws) <= 2) and gap < 400 and pdur + dur + gap <= MAX_SENT_MS:
                merged[-1] = prev + ws
                continue
            if (pdur < MERGE_SHORT_MS or len(prev) <= 2) and gap < 400 and pdur + dur + gap <= MAX_SENT_MS:
                merged[-1] = prev + ws
                continue
        merged.append(ws)
    res = []
    for ws in merged:
        text = "".join(w["text"] for w in ws).strip()
        text = re.sub(r"\s+", " ", text)
        res.append({"text": text, "start_time": ws[0]["start_time"], "end_time": ws[-1]["end_time"]})
    return res


def _start_pad(idx, u, sp, ep, mg):
    s0 = u[idx]["start_time"]
    if idx == 0:
        return max(0, s0 - sp)
    gap = s0 - u[idx - 1]["end_time"]
    tot = sp + ep
    if gap >= tot + mg:
        return s0 - sp
    if gap > mg:
        return s0 - int((gap - mg) * sp / tot)
    return u[idx - 1]["end_time"] + gap // 2


def _end_pad(idx, u, dur, sp, ep, mg):
    e0 = u[idx]["end_time"]
    if idx == len(u) - 1:
        return min(dur, e0 + ep) if dur else e0 + ep
    gap = u[idx + 1]["start_time"] - e0
    tot = sp + ep
    if gap >= tot + mg:
        return e0 + ep
    if gap > mg:
        return e0 + int((gap - mg) * ep / tot)
    return e0 + gap // 2


def pad_utterances(u, dur, sp=100, ep=300, mg=50):
    padded = []
    for i in range(len(u)):
        st = max(0, _start_pad(i, u, sp, ep, mg))
        en = _end_pad(i, u, dur, sp, ep, mg)
        if dur:
            en = min(dur, en)
        padded.append({**u[i], "start_time": int(st), "end_time": int(en)})
    return padded


def run_asr_fix(s: Path, force: bool = False) -> Path:
    out = s / "metadata" / "asr_fixed.json"
    if out.exists() and not force:
        log("asr_fix", "reuse asr_fixed.json")
        return out
    data = read_json(require(s / "metadata" / "asr.json", "asr"))
    dur = data["audio_info"].get("duration", 0)
    before = len(data["result"]["utterances"])
    u = resegment(data["result"]["utterances"])
    if not u:
        raise RuntimeError("ASR result has no utterances.")
    padded = pad_utterances(u, dur)
    write_json(out, {"audio_info": data["audio_info"], "result": {"text": data["result"]["text"], "utterances": padded}})
    durs = np.array([x["end_time"] - x["start_time"] for x in padded]) / 1000
    log("asr_fix", f"Re-segmented {before} -> {len(padded)} sentences; dur mean={durs.mean():.2f}s "
                   f"p95={np.percentile(durs,95):.2f}s max={durs.max():.2f}s")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--model", default=config.get("asr.model", "large-v3-turbo"))
    ap.add_argument("--prompt", default=config.get("asr.prompt", ""))
    ap.add_argument("--only-asr", action="store_true")
    ap.add_argument("--only-fix", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    s = session_dir(a.id)
    if not a.only_fix:
        run_asr(s, a.model, a.prompt, force=a.force)
    if not a.only_asr:
        run_asr_fix(s, force=a.force or not a.only_fix)


if __name__ == "__main__":
    main()
