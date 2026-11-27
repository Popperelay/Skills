"""Pipeline orchestrator: state machine, resume, invalidation and gates.

  run(source | id) walks STAGES in order.  A stage is skipped when its state is succeeded/skipped,
  its artifacts are present and nothing upstream changed in this run ("dirty").  GPU stages run as
  separate subprocesses (VRAM is released when the process exits); logs go to logs/<id>/<stage>.log.

Gates (exit codes; details also written to sessions/<id>/gate.json):
  0  done                     10 translation needed (AI translates translate_jobs/)
  20 cookies needed (YouTube) 30 rewrite needed (AI rewrites hard-to-speak lines)
  1  error (category + hint + log tail)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DUB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DUB_DIR))
import config  # noqa: E402
from common import (EXIT_COOKIES, EXIT_ERROR, EXIT_OK, EXIT_REWRITE, EXIT_TRANSLATE, audio_mode,  # noqa: E402
                    read_json, session_dir, write_json)

STAGES = ["download", "separate", "asr", "asr_fix", "translate", "split_audio", "tts", "tts_qa",
          "merge_audio", "merge_video", "merge_softsub", "export"]

YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


class Gate(Exception):
    def __init__(self, code: int, info: dict):
        super().__init__(info.get("gate", "gate"))
        self.code, self.info = code, info


class StageFailed(Exception):
    pass


# ------------------------------------------------------------------ identity
def slug(text: str, n: int = 40) -> str:
    t = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return (t[:n] or "video").strip("-")


def resolve_source(source: str) -> tuple[str, str]:
    """-> (kind, session_id). kind = youtube | local."""
    if os.path.exists(source):
        import hashlib
        p = Path(source).resolve()
        h = hashlib.sha1(f"{p.name}|{p.stat().st_size}".encode("utf-8")).hexdigest()[:8]
        return "local", f"{slug(p.stem)}-{h}"
    m = YOUTUBE_ID.search(source)
    if m:
        return "youtube", m.group(1)
    raise StageFailed(f"unsupported source (YouTube URL or existing local file expected): {source}")


# ------------------------------------------------------------------ state
class State:
    def __init__(self, s: Path, data: dict):
        self.s, self.d = s, data

    @property
    def path(self) -> Path:
        return self.s / "state.json"

    def save(self):
        self.d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json(self.path, self.d)

    def stage(self, name: str) -> dict:
        return self.d["stages"].setdefault(name, {"status": "pending"})

    @property
    def extra(self) -> dict:
        return self.d.setdefault("extra", {})


def _translation(s: Path):
    p = s / "metadata" / "translation.zh.json"
    return read_json(p)["translation"] if p.exists() else None


def artifacts_ok(s: Path, name: str, formats: list[str], cleaned: set) -> bool:
    def ex(rel):
        return rel in cleaned or (s / rel).exists()
    tr = None
    if name in ("split_audio", "tts"):
        tr = _translation(s)
        if tr is None:
            return False
    if name == "download":
        return ex("media/video_source.mp4")
    if name == "separate":
        return ex("media/audio_vocals.wav") and ex("media/audio_bgm.wav")
    if name == "asr":
        return ex("metadata/asr.json")
    if name == "asr_fix":
        return ex("metadata/asr_fixed.json")
    if name == "translate":
        return ex("metadata/translation.zh.json")
    if name == "split_audio":
        return "segments/vocals" in cleaned or all((s / "segments" / "vocals" / f"{i:04d}.wav").exists()
                                                    for i in range(1, len(tr) + 1))
    if name == "tts":
        return all((s / "segments" / "tts" / f"{i:04d}.wav").exists() for i in range(1, len(tr) + 1))
    if name == "tts_qa":
        p = s / "metadata" / "qa_report.json"
        return p.exists() and read_json(p).get("status") in ("ok", "accepted_with_warnings")
    if name == "merge_audio":
        return ex("tmp/audio_dubbing.wav") and ex("metadata/timings.json")
    if name == "merge_video":
        return ex("tmp/audio_mixed.m4a") and ("hard" not in formats or ex("media/video_final.mp4"))
    if name == "merge_softsub":
        return ex("media/video_final_softsub.mkv") and ex("media/video_final_softsub.mp4")
    if name == "export":
        import s10_export
        return s10_export.export_ok(s)
    raise KeyError(name)


def bootstrap(s: Path, sid: str, kind: str, source: str | None, formats) -> State:
    """Create state.json for a session that predates the tool, trusting existing artifacts."""
    st = State(s, {"id": sid, "kind": kind, "source": source, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "stages": {}, "extra": {}})
    tr = _translation(s)
    # legacy: original-voice lines decided by hand -> qa_decisions.json so a re-merge keeps them
    if tr is not None and not (s / "metadata" / "qa_decisions.json").exists():
        orig = [i for i, t in enumerate(tr, 1) if audio_mode(t) == "original" and (t.get("dst") or "").strip()]
        if orig:
            write_json(s / "metadata" / "qa_decisions.json", {"original": orig})
    # legacy: QA + manual pick happened before qa_report.json existed
    if not (s / "metadata" / "qa_report.json").exists() and (s / "segments" / "tts" / "_pick.json").exists():
        write_json(s / "metadata" / "qa_report.json", {"status": "accepted_with_warnings", "legacy": True,
                                                         "note": "bootstrapped from segments/tts/_pick.json"})
    upstream_ok = True
    for name in STAGES:
        ok = upstream_ok and artifacts_ok(s, name, formats, set())
        st.stage(name).update({"status": "succeeded" if ok else "pending",
                               "message": "bootstrapped from existing artifacts" if ok else ""})
        upstream_ok = ok
    st.save()
    return st


def load_state(s: Path, sid: str, kind: str, source: str | None, formats) -> State:
    p = s / "state.json"
    if p.exists():
        st = State(s, read_json(p))
        if source and not st.d.get("source"):
            st.d["source"] = source
        return st
    return bootstrap(s, sid, kind, source, formats)


# ------------------------------------------------------------------ subprocess runner
STAGE_LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] \[")


class Ctx:
    def __init__(self, sid: str, s: Path, st: State, opts: dict):
        self.sid, self.s, self.st, self.opts = sid, s, st, opts
        self.log_dir = config.logs_dir() / sid
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update({"PYTHONUNBUFFERED": "1", "TQDM_DISABLE": "1", "PYTHONIOENCODING": "utf-8",
                         "DUB_OVERRIDES": json.dumps(opts.get("overrides") or {}, ensure_ascii=False)})
        self.current = ""
        self.last_lines: list[str] = []

    def sub(self, script: str, args: list[str], ok_codes=(0,)) -> int:
        cmd = [sys.executable, str(DUB_DIR / script), "--id", self.sid, *args] if script != "s1_download.py" \
            else [sys.executable, str(DUB_DIR / script), *args]
        logf = self.log_dir / f"{self.current}.log"
        with open(logf, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(cmd)}\n")
            lf.flush()
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env,
                                 cwd=str(config.ROOT), text=True, encoding="utf-8", errors="replace")
            tail: list[str] = []
            for line in p.stdout:
                lf.write(line)
                ln = line.rstrip("\r\n")
                for piece in ln.split("\r"):
                    if piece.strip():
                        tail.append(piece)
                tail = tail[-40:]
                if STAGE_LINE.match(ln):
                    print(ln, flush=True)
            code = p.wait()
        self.last_lines = tail
        if code not in ok_codes:
            raise StageFailed(f"{script} exited with {code}")
        return code


# ------------------------------------------------------------------ handlers
def h_download(ctx: Ctx, force: bool) -> bool:
    src = ctx.st.d.get("source")
    if not src:
        raise StageFailed("no source recorded for this session; pass a URL or file path")
    args = [src, "--id", ctx.sid]
    if ctx.opts.get("cookies"):
        args += ["--cookies", ctx.opts["cookies"]]
    code = ctx.sub("s1_download.py", args, ok_codes=(0, EXIT_COOKIES))
    if code == EXIT_COOKIES:
        msg = next((ln for ln in reversed(ctx.last_lines) if "AUTH_REQUIRED" in ln), "login required")
        raise Gate(EXIT_COOKIES, {"gate": "cookies", "message": msg, "source": src,
                                  "how": "export youtube.com cookies (Netscape cookies.txt) and rerun with --cookies <path>"})
    return True


def h_separate(ctx, force):
    ctx.sub("s2_separate.py", ["--force"] if force else [])
    return True


def h_asr(ctx, force):
    args = ["--only-asr"] + (["--force"] if force else [])
    if ctx.opts.get("asr_prompt"):
        args += ["--prompt", ctx.opts["asr_prompt"]]
    ctx.sub("s3_asr.py", args)
    return True


def h_asr_fix(ctx, force):
    ctx.sub("s3_asr.py", ["--only-fix", "--force"])
    return True


def h_translate(ctx, force):
    import s5_translate as t5
    t5.prep_if_needed(ctx.s)
    results = t5.validate_all(ctx.s)
    todo = [r for r in results if not r["pass"]]
    if todo:
        jd = t5.jobs_dir(ctx.s)
        launcher = str(DUB_DIR.parents[1] / "dub.ps1").replace("'", "''")
        session = ctx.sid.replace("'", "''")
        raise Gate(EXIT_TRANSLATE, {
            "gate": "translate", "session": ctx.sid, "lines": sum(r["lines"] for r in results),
            "chunks_total": len(results), "chunks_done": len(results) - len(todo),
            "guide": str(jd / "guide.md"), "context": str(jd / "context.md"), "glossary": str(jd / "glossary.json"),
            "validator": f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File '{launcher}' validate --id '{session}' --chunk <NN>",
            "todo": [{k: r.get(k) for k in ("chunk", "in", "out", "lines", "first_i", "last_i", "status",
                                            "missing", "empty", "over_by_too_much")} for r in todo]})
    return t5.merge(ctx.s)


def h_split_audio(ctx, force):
    ctx.sub("s6_split_audio.py", [])
    return True


def h_tts(ctx, force):
    ctx.sub("s7_tts.py", [])
    return not any("nothing to do" in ln for ln in ctx.last_lines)


def h_tts_qa(ctx, force):
    import qa_loop
    try:
        return qa_loop.run(ctx.s, ctx.sid, ctx.sub, ctx.st.extra)
    except qa_loop.GateRewrite as g:
        raise Gate(EXIT_REWRITE, g.info)


def h_merge_audio(ctx, force):
    ctx.sub("s8_merge_audio.py", ["--force"])
    return True


def h_merge_video(ctx, force):
    ctx.sub("s9_merge_video.py", [] if "hard" in ctx.opts["formats"] else ["--no-hard"])
    return True


def h_merge_softsub(ctx, force):
    ctx.sub("s9b_soft_subs.py", [])
    return True


def h_export(ctx, force):
    import s10_export
    s10_export.export(ctx.s, ctx.sid, ctx.opts["formats"])
    return False


HANDLERS = {"download": h_download, "separate": h_separate, "asr": h_asr, "asr_fix": h_asr_fix,
            "translate": h_translate, "split_audio": h_split_audio, "tts": h_tts, "tts_qa": h_tts_qa,
            "merge_audio": h_merge_audio, "merge_video": h_merge_video, "merge_softsub": h_merge_softsub,
            "export": h_export}


# ------------------------------------------------------------------ error classification
ERROR_RULES = [
    (r"CUDA out of memory|OutOfMemoryError", "gpu_oom", "显存不足：关闭占用显卡的程序（Unity/游戏等）后重跑同一命令即可续跑"),
    (r"PermissionError|WinError 32|WinError 5\b|being used by another process", "file_locked",
     "文件被占用：关闭正在播放该视频的播放器（或资源管理器预览窗格）后重跑同一命令"),
    (r"n challenge|js_runtimes|JavaScript runtime", "youtube_js", "YouTube 签名求解失败：确认 venv 里有 deno（pip install deno）"),
    (r"Requested format is not available", "youtube_format", "没有可用格式：多半是签名求解失败或需要 cookies"),
    (r"SAFE_DELETE", "safe_delete", "宿主批量删除拦截：不要在一轮里删除超过 50 个文件"),
    (r"No space left|ENOSPC", "disk", "磁盘空间不足"),
    (r"ModuleNotFoundError|ImportError", "env", "依赖缺失：运行 python -m dub doctor 查看"),
    (r"ffmpeg|ffprobe", "ffmpeg", "ffmpeg 处理失败：看日志里 ffmpeg 的报错行"),
]


def classify(lines: list[str]) -> tuple[str, str]:
    text = "\n".join(lines)
    for pat, cat, hint in ERROR_RULES:
        if re.search(pat, text):
            return cat, hint
    return "unknown", "查看日志尾部；修复后重跑同一命令会从失败阶段续跑"


# ------------------------------------------------------------------ outputs
def outputs(s: Path, formats) -> list[str]:
    import s10_export
    exported = s10_export.exported_paths(s)
    if exported:
        return exported
    cands = []
    if "soft" in formats:
        cands += ["media/video_final_softsub.mkv", "media/video_final_softsub.mp4"]
    if "hard" in formats:
        cands += ["media/video_final.mp4"]
    cands += ["metadata/subtitles.zh-en.srt", "metadata/subtitles.zh.srt", "metadata/subtitles.en.srt"]
    return [str(s / c) for c in cands if (s / c).exists()]


def _write_gate(s: Path, info: dict):
    info = {**info, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    write_json(s / "gate.json", info)
    print("GATE " + json.dumps(info, ensure_ascii=False), flush=True)


# ------------------------------------------------------------------ run
def run(source: str | None = None, sid: str | None = None, cookies: str | None = None,
        formats: list[str] | None = None, from_stage: str | None = None, overrides: dict | None = None,
        asr_prompt: str | None = None) -> int:
    if overrides:
        os.environ["DUB_OVERRIDES"] = json.dumps(overrides, ensure_ascii=False)
        config._CACHE = None
    try:
        if source:
            kind, auto_id = resolve_source(source)
            sid = sid or auto_id
        elif not sid:
            raise StageFailed("need a source (URL / file) or --id of an existing session")
        else:
            kind = None
    except StageFailed as e:
        print(f"ERROR {e}", flush=True)
        return EXIT_ERROR
    s = session_dir(sid)
    formats = formats or list(config.get("output.formats", ["hard", "soft"]))
    st = load_state(s, sid, kind or "unknown", str(Path(source).resolve()) if source and os.path.exists(source)
                    else source, formats)
    if kind:
        st.d["kind"] = kind
    if not st.d.get("source"):
        info = s / "metadata" / "ytdlp_info.json"
        if info.exists():
            st.d["source"] = read_json(info).get("webpage_url")
    st.d["formats"] = formats
    st.save()
    opts = {"cookies": cookies, "formats": formats, "overrides": overrides or {}, "asr_prompt": asr_prompt}
    ctx = Ctx(sid, s, st, opts)
    cleaned = set(st.extra.get("cleaned", []))
    print(f"[pipeline] session {sid} -> {s}", flush=True)
    dirty = False
    t_all = time.time()
    for idx, name in enumerate(STAGES):
        rec = st.stage(name)
        forced = from_stage is not None and idx >= STAGES.index(from_stage)
        if name == "merge_softsub" and "soft" not in formats:
            rec.update({"status": "skipped", "message": "output.formats has no 'soft'"})
            continue
        if (not dirty and not forced and rec.get("status") in ("succeeded", "skipped")
                and artifacts_ok(s, name, formats, cleaned)):
            continue
        ctx.current = name
        rec.update({"status": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S"), "message": ""})
        rec["runs"] = rec.get("runs", 0) + 1
        st.save()
        print(f"[pipeline] {name}: running (force={dirty or forced})", flush=True)
        t0 = time.time()
        try:
            changed = HANDLERS[name](ctx, dirty or forced)
        except Gate as g:
            rec.update({"status": "needs_input", "message": g.info.get("gate"), "finished": time.strftime("%Y-%m-%d %H:%M:%S")})
            st.save()
            _write_gate(s, {**g.info, "stage": name, "code": g.code})
            return g.code
        except Exception as e:  # noqa: BLE001
            cat, hint = classify(ctx.last_lines + [str(e)])
            rec.update({"status": "failed", "message": f"{cat}: {e}", "finished": time.strftime("%Y-%m-%d %H:%M:%S")})
            st.save()
            _write_gate(s, {"gate": "error", "code": EXIT_ERROR, "stage": name, "category": cat, "hint": hint,
                            "error": str(e)[:500], "log": str(ctx.log_dir / f"{name}.log"),
                            "tail": ctx.last_lines[-15:]})
            return EXIT_ERROR
        msg = next((ln for ln in reversed(ctx.last_lines) if STAGE_LINE.match(ln)), "")
        rec.update({"status": "succeeded", "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "seconds": round(time.time() - t0, 1), "message": msg[11:300] if msg else ""})
        ctx.last_lines = []
        st.save()
        dirty = dirty or bool(changed)
    outs = outputs(s, formats)
    exp = s / "metadata" / "export.json"
    export_dir = read_json(exp)["dir"] if exp.exists() else None
    _write_gate(s, {"gate": "done", "code": EXIT_OK, "export_dir": export_dir, "outputs": outs,
                    "seconds": round(time.time() - t_all, 1)})
    return EXIT_OK


# ------------------------------------------------------------------ status / preview / clean
def status(sid: str) -> dict:
    s = config.sessions_dir() / sid
    if not (s / "state.json").exists():
        return {"id": sid, "error": "no state.json (run once to bootstrap)"}
    st = read_json(s / "state.json")
    out = {"id": sid, "source": st.get("source"), "formats": st.get("formats"),
           "stages": {k: {kk: v.get(kk) for kk in ("status", "seconds", "message")} for k, v in st["stages"].items()}}
    for name in ("gate.json", "metadata/qa_report.json"):
        p = s / name
        if p.exists():
            d = read_json(p)
            if name.endswith("qa_report.json"):
                d = {k: d.get(k) for k in ("status", "tts_lines", "remaining_share", "rewrite_passes")} | \
                    {"remaining": len(d.get("remaining", [])), "original_lines": len(d.get("original_lines", []))}
            out[name] = d
    out["outputs"] = {p: round(Path(p).stat().st_size / 1e6, 1) for p in outputs(s, st.get("formats") or ["hard", "soft"])}
    return out


def preview(sid: str, minutes: float) -> int:
    s = config.sessions_dir() / sid
    tr = _translation(s)
    if tr is None:
        print("ERROR no translation yet"); return EXIT_ERROR
    limit_ms = minutes * 60000
    n = max(1, sum(1 for t in tr if t["end_time"] <= limit_ms))
    missing = [i for i in range(1, n + 1) if not (s / "segments" / "tts" / f"{i:04d}.wav").exists()]
    if missing:
        print(f"ERROR tts clips missing for lines {missing[:10]} (run the pipeline past 'tts' first)"); return EXIT_ERROR
    ctx = Ctx(sid, s, State(s, {"stages": {}}), {"overrides": {}})
    ctx.current = "preview"
    ctx.sub("s8_merge_audio.py", ["--limit", str(n), "--force"])
    dur = tr[n - 1]["end_time"] / 1000 + 1.0
    ctx.sub("s9_merge_video.py", ["--start", "0", "--duration", f"{dur:.1f}"])
    print(f"[pipeline] preview -> {s / 'media' / 'video_preview.mp4'}")
    return EXIT_OK


CLEANABLE = ["media/audio_vocals.wav", "media/audio_bgm.wav", "segments/vocals", "segments/stretched",
             "segments/ref", "tmp/audio_dubbing.wav", "tmp/audio_dubbing.preview.wav", "media/video_preview.mp4"]


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def clean(sid: str, yes: bool) -> int:
    """List (default) or move intermediates into <repo>/trash/<id>-<ts>/ (rename, nothing is deleted)."""
    s = config.sessions_dir() / sid
    items = [c for c in CLEANABLE if (s / c).exists()]
    items += [str(p.relative_to(s)).replace("\\", "/") for p in sorted((s / "segments").glob("tts_r*"))]
    items += [str(p.relative_to(s)).replace("\\", "/") for p in sorted((s / "segments").glob("pilot_*"))]
    total = 0
    for c in items:
        sz = _size(s / c)
        total += sz
        print(f"  {sz/1e6:9.1f} MB  {c}")
    print(f"  {total/1e9:9.2f} GB  total")
    if not yes:
        print("dry run: nothing moved. Re-run with --yes to move these into trash/ (re-runs of separate/"
              "tts_qa/merge_* would then need: python -m dub run --id <id> --from separate)")
        return EXIT_OK
    dest = config.ROOT / "trash" / f"{sid}-{time.strftime('%Y%m%d-%H%M%S')}"
    for c in items:
        tgt = dest / c
        tgt.parent.mkdir(parents=True, exist_ok=True)
        (s / c).replace(tgt)
    stp = s / "state.json"
    if stp.exists():
        st = read_json(stp)
        st.setdefault("extra", {}).setdefault("cleaned", [])
        st["extra"]["cleaned"] = sorted(set(st["extra"]["cleaned"]) | set(items))
        write_json(stp, st)
    print(f"moved {len(items)} items -> {dest} (delete that folder yourself when sure)")
    return EXIT_OK
