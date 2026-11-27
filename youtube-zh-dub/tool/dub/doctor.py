"""Environment preflight: python -m dub doctor  (exit 0 = ready, 1 = something fatal is missing)."""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def _check(results, name, ok, detail, fatal=True):
    results.append({"check": name, "ok": bool(ok), "fatal": fatal and not ok, "detail": detail})


def run_doctor() -> int:
    r: list[dict] = []
    v = sys.version_info
    _check(r, "python", (3, 10) <= (v.major, v.minor) < (3, 13), f"{sys.version.split()[0]} ({sys.executable}); "
           "VoxCPM2 needs 3.10-3.12")
    try:
        import torch
        cuda = torch.cuda.is_available()
        detail = f"torch {torch.__version__}, cuda {torch.version.cuda}, available={cuda}"
        if cuda:
            cap = torch.cuda.get_device_capability(0)
            free, total = torch.cuda.mem_get_info()
            detail += f", {torch.cuda.get_device_name(0)} sm_{cap[0]}{cap[1]}, VRAM free {free/2**30:.1f}/{total/2**30:.1f} GB"
            arch_ok = f"sm_{cap[0]}{cap[1]}" in torch.cuda.get_arch_list()
            _check(r, "torch+cuda", arch_ok, detail + ("" if arch_ok else " -> this torch build lacks your GPU arch; "
                                                     "install the cu128 wheels"))
            _check(r, "vram_free", free / 2**30 >= 7.0, f"{free/2**30:.1f} GB free (VoxCPM2 peaks ~5.8 GB; "
                   "other GPU apps slow TTS down ~1.5x)", fatal=False)
        else:
            _check(r, "torch+cuda", False, detail)
    except Exception as e:  # noqa: BLE001
        _check(r, "torch+cuda", False, f"import torch failed: {e}")
    for mod in ("demucs", "whisper", "voxcpm", "funasr", "librosa", "soundfile", "yt_dlp", "modelscope"):
        try:
            m = importlib.import_module(mod)
            _check(r, f"pkg:{mod}", True, getattr(m, "__version__", "ok"))
        except Exception as e:  # noqa: BLE001
            _check(r, f"pkg:{mod}", False, str(e)[:160])
    try:
        ff = config.ffmpeg_dir()
        exe = ff / ("ffmpeg.exe" if (ff / "ffmpeg.exe").exists() else "ffmpeg")
        filters = subprocess.run([str(exe), "-hide_banner", "-filters"], capture_output=True).stdout.decode(
            "utf-8", "replace")
        need = {"subtitles": True, "rubberband": False, "alimiter": True}
        for f, fatal in need.items():
            _check(r, f"ffmpeg:{f}", f" {f} " in filters, str(ff), fatal=fatal)
    except Exception as e:  # noqa: BLE001
        _check(r, "ffmpeg", False, str(e))
    deno = config.deno_exe()
    if deno and Path(deno).exists():
        out = subprocess.run([str(deno), "--version"], capture_output=True, text=True).stdout.split("\n")[0]
        _check(r, "deno", True, f"{deno} ({out})", fatal=False)
    else:
        _check(r, "deno", False, "needed for YouTube n-challenge: pip install deno (inside the venv)", fatal=False)
    md = config.repo_path(config.get("paths.voxcpm_model_dir"))
    _check(r, "model:voxcpm2", (md / "model.safetensors").exists(), str(md))
    wroot = Path(config.get("paths.whisper_download_root") or Path.home() / ".cache" / "whisper")
    wname = config.get("asr.model", "large-v3-turbo")
    _check(r, f"model:whisper-{wname}", (wroot / f"{wname}.pt").exists(), f"{wroot} (auto-downloads if missing)",
           fatal=False)
    config.ROOT.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(config.ROOT)
    _check(r, "disk", du.free / 2**30 >= 15, f"{du.free/2**30:.0f} GB free on {config.ROOT.drive or '/'} "
           "(a 2-hour video needs ~10 GB of intermediates)", fatal=False)
    try:
        q = subprocess.run(["nvidia-smi", "--query-compute-apps=process_name", "--format=csv,noheader"],
                           capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
        heavy = sorted({Path(x.strip()).name for x in q.splitlines() if x.strip()} -
                       {"dwm.exe", "explorer.exe", "SearchHost.exe", "ShellExperienceHost.exe",
                        "StartMenuExperienceHost.exe", "ApplicationFrameHost.exe", "TextInputHost.exe"})
        _check(r, "gpu_apps", True, "other GPU clients (not touched): " + (", ".join(heavy[:12]) or "none"),
               fatal=False)
    except Exception:
        pass
    fatal = [x for x in r if x["fatal"]]
    for x in r:
        mark = "OK  " if x["ok"] else ("FAIL" if x["fatal"] else "WARN")
        print(f"[{mark}] {x['check']}: {x['detail']}")
    print("DOCTOR " + json.dumps({"ready": not fatal, "fatal": [x["check"] for x in fatal],
                                  "warnings": [x["check"] for x in r if not x["ok"] and not x["fatal"]]},
                                 ensure_ascii=False))
    return 0 if not fatal else 1
