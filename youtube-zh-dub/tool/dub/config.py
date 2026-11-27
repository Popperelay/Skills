"""Configuration: built-in defaults <- dub.toml (repo root) <- --set key.sub=value overrides.

Every stage script reads the same merged config through ``cfg()``.  Overrides given to
``python -m dub run --set ...`` are forwarded to stage subprocesses via the DUB_OVERRIDES
environment variable (JSON), so a single run sees one consistent configuration.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parents[1] / "dub.toml"
ROOT = Path(os.environ.get("YOUTUBE_ZH_DUB_WORKDIR") or
            Path(os.environ["LOCALAPPDATA"]) / "YouTubeZhDub" / "work").resolve()
RUNTIME = Path(sys.executable).resolve().parents[1]

DEFAULTS: dict = {
    "paths": {
        "ffmpeg_dir": str(RUNTIME / "bin"),
        "deno": str(RUNTIME / "bin" / "deno.exe"),
        "voxcpm_model_dir": str(RUNTIME / "models" / "voxcpm2"),
        "whisper_download_root": str(RUNTIME / "models" / "whisper"),
        "sessions_dir": "sessions",
        "logs_dir": "logs",
    },
    "download": {"max_height": 1080},
    "separate": {"model": "htdemucs_ft", "shifts": 1},
    "asr": {"model": "large-v3-turbo", "language": "en", "prompt": ""},
    "translate": {"chunk_size": 110, "zh_units_per_sec": 4.6, "max_over_ratio": 0.08, "hard_over": 1.15},
    "tts": {"mode": "ult", "cfg": 2.0, "steps": 10, "seed": 42},
    "qa": {
        "cer_bad": 0.20,            # CJK-only CER that marks a take as bad
        "cer_gate": 0.25,           # remaining lines above this count toward the rewrite gate
        "dur_ratio_max": 1.6,
        "dur_ratio_min": 0.25,
        "sim_min": 0.40,            # speaker similarity to the global reference
        "original_cer": 0.35,       # still this bad after all rounds and short -> keep original voice
        "original_max_slot_s": 4.0,
        "gate_ratio": 0.03,         # remaining bad lines / tts lines above this -> exit 30
        "max_rewrite_passes": 1,
        "rounds": [
            {"mode": "ult", "seed": 7, "cfg": 2.0, "speakable": True},
            {"mode": "ult", "seed": 13, "cfg": 2.5, "speakable": True},
            {"mode": "seg", "seed": 99, "cfg": 2.5, "speakable": True},
        ],
    },
    "mix": {"bgm_volume": 0.30},
    "subtitles": {
        "hard_font": "Microsoft YaHei",
        "hard_font_size": 15,
        "zh_font": "Microsoft YaHei",
        "en_font": "Segoe UI",
    },
    "output": {
        "formats": ["hard", "soft"],   # hard = burned-in mp4; soft = mkv+mp4 with selectable tracks
        "dir": "output",               # deliverables: <dir>/<视频标题>/<视频标题>.中文配音.mkv ...
        "link": True,                  # hard-link instead of copy (same volume: no extra disk space)
    },
}


def _merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _parse_value(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        low = raw.lower()
        if low in ("true", "false"):
            return low == "true"
        return raw


def parse_overrides(pairs: list[str]) -> dict:
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--set expects key.sub=value, got {p!r}")
        key, raw = p.split("=", 1)
        node = out
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _parse_value(raw.strip())
    return out


_CACHE: dict | None = None


def cfg() -> dict:
    global _CACHE
    if _CACHE is None:
        c = copy.deepcopy(DEFAULTS)
        if CONFIG_FILE.exists():
            _merge(c, tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        env = os.environ.get("DUB_OVERRIDES")
        if env:
            _merge(c, json.loads(env))
        _CACHE = c
    return _CACHE


def get(path: str, default=None):
    node = cfg()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def repo_path(p: str) -> Path:
    q = Path(p).expanduser()
    return q if q.is_absolute() else ROOT / q


# ---------------------------------------------------------------- tool discovery
_KNOWN_FFMPEG_ROOTS = [Path.home() / ".workbuddy" / "binaries" / "ffmpeg_btbn",
                       Path.home() / ".workbuddy" / "binaries" / "ffmpeg"]


def ffmpeg_dir() -> Path:
    conf = get("paths.ffmpeg_dir")
    if conf:
        d = repo_path(conf)
        if (d / "ffmpeg.exe").exists() or (d / "ffmpeg").exists():
            return d
        raise RuntimeError(f"paths.ffmpeg_dir={conf} has no ffmpeg")
    for root in _KNOWN_FFMPEG_ROOTS:
        for cand in sorted(root.glob("*/bin")):
            if (cand / "ffmpeg.exe").exists() and (cand / "ffprobe.exe").exists():
                return cand
    found = shutil.which("ffmpeg")
    if found:
        return Path(found).parent
    raise RuntimeError("ffmpeg not found (set paths.ffmpeg_dir in dub.toml)")


def deno_exe() -> Path | None:
    conf = get("paths.deno")
    if conf:
        return repo_path(conf)
    cand = Path(sys.executable).parent / ("deno.exe" if os.name == "nt" else "deno")
    if cand.exists():
        return cand
    found = shutil.which("deno")
    return Path(found) if found else None


def sessions_dir() -> Path:
    return repo_path(get("paths.sessions_dir"))


def logs_dir() -> Path:
    return repo_path(get("paths.logs_dir"))
