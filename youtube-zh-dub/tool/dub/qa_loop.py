"""tts_qa stage: QA -> retry rounds -> pick best -> original-voice fallback -> report / gate 30.

All heavy work (VoxCPM, Whisper, CAM++) runs in subprocesses through ``sub(script, args)``;
this module only reads/writes JSON and WAV files, so the orchestrator never holds GPU memory.

Files:
  segments/tts/_qa.json        rows for the clips currently in segments/tts (kept in sync by pick())
  segments/tts_rN/_qa.json     rows for retry round N
  segments/tts/_pick.json      chosen source per line
  metadata/qa_decisions.json   {"original": [i, ...]} lines that keep the speaker's original voice
  metadata/rewrites.json       written by AI at gate 30: {"i": "new zh" | "__ORIGINAL__"}
  metadata/qa_report.json      final summary; status ok | needs_rewrite | accepted_with_warnings
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import EXIT_REWRITE, audio_mode, log, read_json, target_text, write_json  # noqa: E402
from s5_translate import apply_overlays  # noqa: E402
from s7b_tts_qa import cjk_cer  # noqa: E402


class GateRewrite(Exception):
    code = EXIT_REWRITE

    def __init__(self, info: dict):
        super().__init__("rewrite needed")
        self.info = info


# ------------------------------------------------------------------ scoring
def _q() -> dict:
    return config.get("qa")


def row_cjk(row: dict, tgt: str) -> float:
    return float(row["cjk_cer"]) if "cjk_cer" in row else cjk_cer(tgt, row.get("hyp", ""))


def ratio(row: dict) -> float:
    return row["dur"] / max(row["slot"], 0.1)


def penalty(row: dict, tgt: str) -> float:
    q = _q()
    r = ratio(row)
    pen = row_cjk(row, tgt)
    if r > q["dur_ratio_max"] or r < q["dur_ratio_min"]:
        pen += 0.3
    if row.get("sim_ref", 1.0) < q["sim_min"]:
        pen += 0.3
    return round(pen, 3)


def is_bad(row: dict, tgt: str) -> bool:
    q = _q()
    r = ratio(row)
    return (row_cjk(row, tgt) > q["cer_bad"] or r > q["dur_ratio_max"] or r < q["dur_ratio_min"]
            or row.get("sim_ref", 1.0) < q["sim_min"])


def counts_for_gate(row: dict, tgt: str) -> bool:
    """Speaker-similarity-only outliers are reported but never block the run."""
    q = _q()
    r = ratio(row)
    return row_cjk(row, tgt) > q["cer_gate"] or r > q["dur_ratio_max"] or r < q["dur_ratio_min"]


def fresh_rows(d: Path, tr) -> dict:
    p = d / "_qa.json"
    if not p.exists():
        return {}
    out = {}
    for r in read_json(p)["rows"]:
        i = r["i"]
        w = d / f"{i:04d}.wav"
        if not (1 <= i <= len(tr)) or not w.exists():
            continue
        if r.get("dst") is not None and r["dst"] != target_text(tr[i - 1]):
            continue
        if r.get("wav_mtime") is not None and r["wav_mtime"] != os.path.getmtime(w):
            continue
        out[i] = r
    return out


# ------------------------------------------------------------------ pick best take
def pick(s: Path, tr, lines, retry_dirs) -> int:
    main = s / "segments" / "tts"
    main_rows = fresh_rows(main, tr)
    cand_rows = [(d, fresh_rows(d, tr)) for d in retry_dirs]
    meta_p = main / "_tts_meta.json"
    meta = read_json(meta_p) if meta_p.exists() else {}
    pick_p = main / "_pick.json"
    picks = read_json(pick_p) if pick_p.exists() else {}
    all_rows = {r["i"]: r for r in read_json(main / "_qa.json")["rows"]} if (main / "_qa.json").exists() else {}
    swapped = 0
    for i in lines:
        tgt = target_text(tr[i - 1])
        best_d, best_r = (main, main_rows[i]) if i in main_rows else (None, None)
        best_p = penalty(best_r, tgt) if best_r else 9.0
        for d, rows in cand_rows:
            r = rows.get(i)
            if r is not None and penalty(r, tgt) < best_p:
                best_d, best_r, best_p = d, r, penalty(r, tgt)
        if best_r is None:
            continue
        if best_d != main:
            dst = main / f"{i:04d}.wav"
            tmp = main / f".{i:04d}.pick.wav"
            shutil.copyfile(best_d / f"{i:04d}.wav", tmp)
            tmp.replace(dst)
            rmeta_p = best_d / "_tts_meta.json"
            if rmeta_p.exists():
                rm = read_json(rmeta_p).get(str(i))
                if rm:
                    meta[str(i)] = rm
            best_r = {**best_r, "wav_mtime": os.path.getmtime(dst), "dst": tgt}
            all_rows[i] = best_r
            swapped += 1
        picks[str(i)] = {"pen": best_p, "cjk_cer": round(row_cjk(best_r, tgt), 3),
                         "dur_ratio": round(ratio(best_r), 2), "src": best_d.name}
    write_json(meta_p, meta)
    write_json(main / "_qa.json", {"rows": [all_rows[k] for k in sorted(all_rows)]})
    write_json(pick_p, picks)
    return swapped


# ------------------------------------------------------------------ original-voice fallback
def use_original(s: Path, tr, i: int):
    it = tr[i - 1]
    main = s / "segments" / "tts"
    dst = main / f"{i:04d}.wav"
    if dst.exists():
        shutil.copyfile(dst, main / f"{i:04d}.tts_best.wav")
    with sf.SoundFile(str(s / "media" / "audio_vocals.wav")) as f:
        sr = f.samplerate
        f.seek(int(it["start_time"]) * sr // 1000)
        y = f.read((int(it["end_time"]) - int(it["start_time"])) * sr // 1000, dtype="float32", always_2d=True)
    tmp = main / f".{i:04d}.orig.wav"
    sf.write(str(tmp), y, sr, subtype="PCM_16")
    tmp.replace(dst)
    it["audio_mode"] = "original"


def _save_decisions(s: Path, originals: set):
    write_json(s / "metadata" / "qa_decisions.json", {"original": sorted(originals)})


# ------------------------------------------------------------------ stage entry
def run(s: Path, sid: str, sub, state_extra: dict) -> bool:
    """Returns True if any audio changed. Raises GateRewrite when AI rewriting is needed."""
    q = _q()
    tp = s / "metadata" / "translation.zh.json"
    tr = read_json(tp)["translation"]
    main = s / "segments" / "tts"

    # 1) overlays from AI rewrites / earlier decisions
    changed_txt, to_orig = apply_overlays(s, tr)
    decided = set()
    dp = s / "metadata" / "qa_decisions.json"
    if dp.exists():
        decided = set(read_json(dp).get("original", []))
    rp = s / "metadata" / "rewrites.json"
    if rp.exists():
        decided |= {int(k) for k, v in read_json(rp).items() if isinstance(v, str) and v.strip() == "__ORIGINAL__"}
    for i in to_orig:
        use_original(s, tr, i)
    decided -= {i for i in changed_txt if audio_mode(tr[i - 1]) == "tts"}
    _save_decisions(s, decided)
    write_json(tp, {"translation": tr})
    any_change = bool(changed_txt or to_orig)
    if changed_txt:
        state_extra["rewrite_passes"] = state_extra.get("rewrite_passes", 0) + 1
        log("tts_qa", f"applied {len(changed_txt)} rewritten lines, {len(to_orig)} switched to original voice")
        sub("s7_tts.py", ["--only", ",".join(map(str, sorted(changed_txt))), "--force"])

    # 2) QA of the main take set (incremental)
    sub("s7b_tts_qa.py", ["--dir", str(main)])
    tts_lines = [i for i in range(1, len(tr) + 1) if audio_mode(tr[i - 1]) == "tts"]

    def bad_now():
        rows = fresh_rows(main, tr)
        return [i for i in tts_lines if i in rows and is_bad(rows[i], target_text(tr[i - 1]))]

    bad = bad_now()
    log("tts_qa", f"main takes: {len(bad)}/{len(tts_lines)} lines flagged")

    # 3) retry rounds on still-bad lines only
    dirs = []
    for n, rnd in enumerate(q["rounds"], 1):
        if not bad:
            break
        d = s / "segments" / f"tts_r{n}"
        dirs.append(d)
        args = ["--only", ",".join(map(str, bad)), "--out", str(d), "--mode", rnd.get("mode", "ult"),
                "--seed", str(rnd.get("seed", 7 * n)), "--cfg", str(rnd.get("cfg", 2.0))]
        if rnd.get("speakable", True):
            args.append("--speakable")
        sub("s7_tts.py", args)
        sub("s7b_tts_qa.py", ["--dir", str(d), "--only", ",".join(map(str, bad))])
        swapped = pick(s, tr, bad, dirs)
        any_change = any_change or swapped > 0
        prev = len(bad)
        bad = bad_now()
        log("tts_qa", f"round {n} ({rnd.get('mode')}, seed={rnd.get('seed')}, cfg={rnd.get('cfg')}): "
                      f"swapped {swapped}, flagged {prev} -> {len(bad)}")

    # 4) original-voice fallback for short lines that are still unintelligible
    rows = fresh_rows(main, tr)
    newly = []
    for i in bad:
        it = tr[i - 1]
        slot = (it["end_time"] - it["start_time"]) / 1000
        if i in rows and row_cjk(rows[i], target_text(it)) > q["original_cer"] and slot <= q["original_max_slot_s"]:
            use_original(s, tr, i)
            decided.add(i)
            newly.append(i)
    if newly:
        _save_decisions(s, decided)
        write_json(tp, {"translation": tr})
        any_change = True
        log("tts_qa", f"kept original voice for {len(newly)} short lines: {newly}")

    # 5) report + gate
    tts_lines = [i for i in range(1, len(tr) + 1) if audio_mode(tr[i - 1]) == "tts"]
    rows = fresh_rows(main, tr)
    remaining, sim_only = [], []
    for i in tts_lines:
        r = rows.get(i)
        if r is None:
            continue
        tgt = target_text(tr[i - 1])
        entry = {"i": i, "src": tr[i - 1]["src"], "dst": tgt, "hyp": r.get("hyp", ""),
                 "cjk_cer": round(row_cjk(r, tgt), 3), "slot_s": r["slot"], "dur_s": r["dur"],
                 "sim_ref": r.get("sim_ref")}
        if counts_for_gate(r, tgt):
            remaining.append(entry)
        elif is_bad(r, tgt):
            sim_only.append(i)
    share = len(remaining) / max(1, len(tts_lines))
    passes = state_extra.get("rewrite_passes", 0)
    if share > q["gate_ratio"] and passes < q["max_rewrite_passes"]:
        status = "needs_rewrite"
    elif remaining:
        status = "accepted_with_warnings"
    else:
        status = "ok"
    report = {"status": status, "tts_lines": len(tts_lines), "original_lines": sorted(decided),
              "remaining_share": round(share, 4), "gate_ratio": q["gate_ratio"], "rewrite_passes": passes,
              "remaining": remaining, "speaker_similarity_only": sim_only}
    write_json(s / "metadata" / "qa_report.json", report)
    log("tts_qa", f"{status}: remaining {len(remaining)} ({share:.1%}), original {len(decided)}, "
                  f"sim-only {len(sim_only)}")
    if status == "needs_rewrite":
        raise GateRewrite({"gate": "rewrite", "report": str(s / "metadata" / "qa_report.json"),
                           "rewrites": str(s / "metadata" / "rewrites.json"),
                           "lines": [e["i"] for e in remaining]})
    return any_change
