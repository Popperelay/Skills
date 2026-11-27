"""Stage 5 · translate -> metadata/translation.zh.json

The translation itself is done by AI workers (one per chunk; orchestrated by the skill when the
pipeline exits with code 10).  This module only prepares inputs, validates outputs and merges:

  prep_if_needed : asr_fixed.json -> translate_jobs/chunk_XX.json (+ per-line spoken budget),
                   _manifest.json (content hash per chunk), glossary.json, context.md, guide.md
                   A chunk whose source changed gets its stale out_XX.json renamed (never deleted).
  validate       : out_XX.json completeness + spoken-length budget
  merge          : out_XX.json (+ overlays rewrites.json / qa_decisions.json) -> translation.zh.json

CLI (manual use):  python dub/s5_translate.py prep|merge|check --id <sid>
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import log, read_json, require, session_dir, source_info, write_json  # noqa: E402

GUIDE_SRC = Path(__file__).resolve().parent / "translate_guide.md"
ORIGINAL_MARK = "__ORIGINAL__"


def units_per_sec() -> float:
    return float(config.get("translate.zh_units_per_sec", 4.6))


def spoken_units(text: str) -> float:
    """Estimate spoken duration in CJK-syllable units for mixed zh/en text."""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_words = re.findall(r"[A-Za-z]+", text)
    en = sum(max(1.0, len(w) / 3.2) for w in en_words)
    digits = len(re.findall(r"\d", text))
    pauses = len(re.findall(r"[，。！？；：,.!?;:、]", text)) * 0.6
    return cjk + en + digits * 0.9 + pauses


def budget_for(dur_ms: int) -> int:
    return max(3, int(math.floor(dur_ms / 1000 * units_per_sec())))


def jobs_dir(s: Path) -> Path:
    d = s / "metadata" / "translate_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ prep
def build_items(utts):
    items = []
    for i, u in enumerate(utts, 1):
        nxt = utts[i]["start_time"] if i < len(utts) else u["end_time"] + 1500
        free_after = max(0, nxt - u["end_time"])
        slot = u["end_time"] - u["start_time"]
        items.append({"i": i, "start": u["start_time"], "end": u["end_time"], "slot_s": round(slot / 1000, 2),
                      "gap_after_s": round(free_after / 1000, 2),
                      "budget": budget_for(slot + min(free_after, 600)), "en": u["text"]})
    return items


def _chunk_hash(part) -> str:
    key = json.dumps([[x["i"], x["start"], x["end"], x["en"]] for x in part], ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_IDENT = re.compile(r"(?<![\w.])(?:[A-Za-z_]\w*\.[A-Za-z_][\w.]*\w|[A-Za-z]+_\w+|[a-z]+[A-Z]\w*|[A-Za-z]+\d+\w*)(?!\w)")
_PROPER = re.compile(r"(?<=[a-z,;:] )([A-Z][a-zA-Z]{2,})")
_STOP = {"The", "This", "That", "And", "But", "So", "Okay", "Yeah", "Now", "Then", "What", "When",
         "Here", "There", "Let", "Because", "If", "It", "We", "You", "They", "In", "On", "For", "Of"}


def extract_glossary(utts, top: int = 60):
    code, names = collections.Counter(), collections.Counter()
    for u in utts:
        t = u["text"]
        for m in _IDENT.findall(t):
            code[m.strip(".")] += 1
        for m in _PROPER.findall(t):
            if m not in _STOP:
                names[m] += 1
    terms = [{"term": k, "count": v, "kind": "code", "zh": ""} for k, v in code.items() if v >= 3]
    terms += [{"term": k, "count": v, "kind": "name", "zh": ""} for k, v in names.items() if v >= 4]
    terms.sort(key=lambda x: -x["count"])
    return terms[:top]


def write_context(s: Path):
    """context.md = source facts + glossary; regenerated each time so AI edits of glossary.json flow in."""
    jd = jobs_dir(s)
    info = source_info(s)
    gl = read_json(jd / "glossary.json")["terms"] if (jd / "glossary.json").exists() else []
    lines = ["# 本片信息（只作背景，不要翻译本文件）", ""]
    for label, key in (("标题", "title"), ("作者/频道", "uploader"), ("时长(秒)", "duration")):
        if info.get(key):
            lines.append(f"- {label}：{info[key]}")
    if info.get("tags"):
        lines.append(f"- 标签：{', '.join(info['tags'][:15])}")
    desc = (info.get("description") or "").strip().split("\n")[0][:300]
    if desc:
        lines.append(f"- 简介首段：{desc}")
    lines += ["", "# 术语表（全片统一）",
              "给出了 zh 的按 zh 译；没给出的：代码标识符按规范保留英文并写成念得出的形式，人名/产品名保留英文。", ""]
    for t in gl:
        z = f" → {t['zh']}" if t.get("zh") else ""
        lines.append(f"- `{t['term']}`（{t['kind']}，×{t['count']}）{z}")
    (jd / "context.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prep_if_needed(s: Path) -> dict:
    """Idempotent: (re)writes only chunks whose source content changed. Returns summary."""
    jd = jobs_dir(s)
    utts = read_json(require(s / "metadata" / "asr_fixed.json", "asr_fixed"))["result"]["utterances"]
    items = build_items(utts)
    size = int(config.get("translate.chunk_size", 110))
    manifest_p = jd / "_manifest.json"
    old = read_json(manifest_p)["chunks"] if manifest_p.exists() else {}
    new, changed, stamp = {}, [], time.strftime("%Y%m%d-%H%M%S")
    nchunks = math.ceil(len(items) / size)
    for n in range(nchunks):
        part = items[n * size:(n + 1) * size]
        h = _chunk_hash(part)
        new[f"{n:02d}"] = h
        cp = jd / f"chunk_{n:02d}.json"
        prev = old.get(f"{n:02d}")
        if prev is None and cp.exists():  # legacy session without manifest: compare file content
            try:
                prev = _chunk_hash(read_json(cp)["items"])
            except Exception:
                prev = None
        if prev == h and cp.exists():
            continue
        k = n * size
        write_json(cp, {"chunk": n, "context_before": [x["en"] for x in items[max(0, k - 3):k]],
                        "context_after": [x["en"] for x in items[k + size:k + size + 2]], "items": part})
        op = jd / f"out_{n:02d}.json"
        if op.exists():
            op.replace(jd / f"out_{n:02d}.stale-{stamp}.json")
        changed.append(n)
    for p in sorted(jd.glob("chunk_[0-9][0-9].json")):  # chunks that no longer exist
        if p.stem.split("_")[1] not in new:
            p.replace(p.with_name(f"{p.stem}.stale-{stamp}.json"))
    if changed and old:
        for name in ("rewrites.json", "qa_decisions.json"):
            p = s / "metadata" / name
            if p.exists():
                p.replace(p.with_name(f"{p.stem}.stale-{stamp}.json"))
    write_json(manifest_p, {"chunks": new, "lines": len(items)})
    if not (jd / "glossary.json").exists() or changed:
        prev_zh = {}
        if (jd / "glossary.json").exists():
            prev_zh = {t["term"]: t.get("zh", "") for t in read_json(jd / "glossary.json")["terms"]}
        terms = extract_glossary(utts)
        for t in terms:
            t["zh"] = prev_zh.get(t["term"], "")
        write_json(jd / "glossary.json", {"terms": terms})
    shutil.copyfile(GUIDE_SRC, jd / "guide.md")
    write_context(s)
    if changed:
        log("translate", f"prepared chunks {changed} ({len(items)} lines, {nchunks} chunks)")
    return {"chunks": nchunks, "lines": len(items), "changed": changed}


# ------------------------------------------------------------------ validate
def validate(s: Path, nn: int) -> dict:
    jd = jobs_dir(s)
    chunk = read_json(jd / f"chunk_{nn:02d}.json")
    outp = jd / f"out_{nn:02d}.json"
    res = {"chunk": nn, "in": str(jd / f"chunk_{nn:02d}.json"), "out": str(outp), "lines": len(chunk["items"]),
           "first_i": chunk["items"][0]["i"], "last_i": chunk["items"][-1]["i"]}
    if not outp.exists():
        return {**res, "status": "missing", "pass": False}
    try:
        out = json.loads(outp.read_text(encoding="utf-8"))
        if not isinstance(out, dict):
            raise ValueError("top level must be an object")
    except Exception as e:
        return {**res, "status": "invalid_json", "error": str(e)[:200], "pass": False}
    want = {str(x["i"]) for x in chunk["items"]}
    missing = sorted(want - set(out), key=int)
    extra = sorted(set(out) - want, key=lambda k: (len(k), k))
    over, empty = [], []
    for x in chunk["items"]:
        zh = out.get(str(x["i"]), "")
        if not isinstance(zh, str) or not zh.strip():
            empty.append(x["i"]); continue
        u = spoken_units(zh)
        if u > x["budget"]:
            over.append((x["i"], round(u, 1), x["budget"]))
    hard = [o for o in over if o[1] > o[2] * float(config.get("translate.hard_over", 1.15))]
    ok = (not missing and not extra and not empty and not hard
          and len(over) <= float(config.get("translate.max_over_ratio", 0.08)) * len(want))
    return {**res, "status": "pass" if ok else "fail", "pass": ok, "missing": missing, "extra": extra,
            "empty": empty, "over_budget": over, "over_by_too_much": hard}


def validate_all(s: Path) -> list[dict]:
    m = read_json(jobs_dir(s) / "_manifest.json")
    return [validate(s, int(k)) for k in sorted(m["chunks"])]


# ------------------------------------------------------------------ overlays + merge
def _decided_original(s: Path) -> set:
    p = s / "metadata" / "qa_decisions.json"
    return set(int(x) for x in read_json(p).get("original", [])) if p.exists() else set()


def apply_overlays(s: Path, tr: list[dict]) -> tuple[set, set]:
    """rewrites.json {"i": text | "__ORIGINAL__"} and qa_decisions.json {"original": [i]} -> tr in place.
    Returns (indices whose spoken text changed, indices switched to original audio)."""
    changed, to_orig = set(), set()
    decided = _decided_original(s)
    rp = s / "metadata" / "rewrites.json"
    if rp.exists():
        for k, v in read_json(rp).items():
            i = int(k)
            if not (1 <= i <= len(tr)) or not isinstance(v, str) or not v.strip():
                continue
            it = tr[i - 1]
            if v.strip() == ORIGINAL_MARK:
                decided.add(i)
                continue
            v = re.sub(r"\s+", " ", v.strip())
            if it.get("dst") != v:
                it["dst"] = v
                changed.add(i)
            if it.get("audio_mode") != "tts" and i not in decided:
                it["audio_mode"] = "tts"
                changed.add(i)
    for i in decided:
        if 1 <= i <= len(tr) and tr[i - 1].get("audio_mode") != "original":
            tr[i - 1]["audio_mode"] = "original"
            to_orig.add(i)
    return changed, to_orig


def merge(s: Path, allow_missing: bool = False) -> bool:
    """Returns True if translation.zh.json content changed."""
    utts = read_json(require(s / "metadata" / "asr_fixed.json", "asr_fixed"))["result"]["utterances"]
    jd = jobs_dir(s)
    zh = {}
    for f in sorted(jd.glob("out_[0-9][0-9].json")):
        for k, v in read_json(f).items():
            zh[int(k)] = v
    missing = [i for i in range(1, len(utts) + 1) if i not in zh]
    if missing and not allow_missing:
        raise RuntimeError(f"missing translations for {len(missing)} lines, e.g. {missing[:10]}")
    out = []
    for i, u in enumerate(utts, 1):
        dst = re.sub(r"\s+", " ", (zh.get(i) or "").strip())
        out.append({"src": u["text"], "dst": dst, "audio_mode": "tts" if dst else "original",
                    "src_lang": "en", "dst_lang": "zh", "start_time": u["start_time"],
                    "end_time": u["end_time"], "speaker": "1"})
    apply_overlays(s, out)
    tp = s / "metadata" / "translation.zh.json"
    new = {"translation": out}
    if tp.exists() and read_json(tp) == new:
        log("translate", f"merged {len(out)} lines (unchanged)")
        return False
    write_json(tp, new)
    log("translate", f"merged {len(out)} lines (missing={len(missing)})")
    return True


def check(s: Path, ratio: float):
    tr = read_json(require(s / "metadata" / "translation.zh.json", "translation"))["translation"]
    over = []
    for i, it in enumerate(tr, 1):
        if it["audio_mode"] != "tts":
            continue
        nxt = tr[i]["start_time"] if i < len(tr) else it["end_time"] + 1500
        b = budget_for(it["end_time"] - it["start_time"] + min(600, max(0, nxt - it["end_time"])))
        u = spoken_units(it["dst"])
        if u > b * ratio:
            over.append((i, round(u, 1), b))
    log("translate", f"{len(over)} lines exceed budget*{ratio}: {over[:20]}")
    return over


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "merge", "check"])
    ap.add_argument("--id", required=True)
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--ratio", type=float, default=1.15)
    a = ap.parse_args()
    s = session_dir(a.id)
    {"prep": lambda: prep_if_needed(s), "merge": lambda: merge(s, a.allow_missing),
     "check": lambda: check(s, a.ratio)}[a.cmd]()


if __name__ == "__main__":
    main()
