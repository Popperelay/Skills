"""Shared helpers for the dubbing pipeline.

Stage contract (artifact names are the hand-off surface between stages, i starts at 1):
  download      -> media/video_source.mp4, metadata/ytdlp_info.json | metadata/local_info.json
  separate      -> media/audio_vocals.wav, media/audio_bgm.wav
  asr           -> metadata/asr.json
  asr_fix       -> metadata/asr_fixed.json
  translate     -> metadata/translation.zh.json          (gate 10: AI translates translate_jobs/)
  split_audio   -> segments/vocals/{i:04d}.wav
  tts           -> segments/tts/{i:04d}.wav
  tts_qa        -> metadata/qa_report.json               (gate 30: AI rewrites hard lines)
  merge_audio   -> tmp/audio_dubbing.wav, metadata/timings.json
  merge_video   -> tmp/audio_mixed.m4a [+ media/video_final.mp4 when output has "hard"]
  merge_softsub -> media/video_final_softsub.{mkv,mp4}, metadata/subtitles.{zh,en,zh-en}.{srt,ass}
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

ROOT = config.ROOT
DATA_DIR = ROOT / "data"
MODEL_CACHE_DIR = DATA_DIR / "modelscope"

# exit codes shared by stage scripts and the pipeline
EXIT_OK, EXIT_ERROR, EXIT_TRANSLATE, EXIT_COOKIES, EXIT_REWRITE = 0, 1, 10, 20, 30


def ffmpeg_bin_dir() -> Path:
    return config.ffmpeg_dir()


def ensure_ffmpeg_on_path() -> Path:
    d = ffmpeg_bin_dir()
    if str(d) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    return d


def _exe(name: str) -> str:
    d = ffmpeg_bin_dir()
    p = d / f"{name}.exe"
    return str(p if p.exists() else d / name)


def FFMPEG() -> str:
    return _exe("ffmpeg")


def FFPROBE() -> str:
    return _exe("ffprobe")


def session_dir(video_id: str) -> Path:
    s = config.sessions_dir() / video_id
    for sub in ("media", "metadata", "segments", "tmp"):
        (s / sub).mkdir(parents=True, exist_ok=True)
    return s


def read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_json(p, obj) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.pending")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def log(stage: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{stage}] {msg}", flush=True)


def require(p, what: str) -> Path:
    p = Path(p)
    if not p.exists():
        raise FileNotFoundError(f"Missing cached pipeline artifact: {what} ({p})")
    return p


def audio_mode(item: dict) -> str:
    mode = item.get("audio_mode")
    if mode is not None:
        if mode not in ("tts", "original"):
            raise ValueError("audio_mode must be one of: tts, original")
        return mode
    text = item.get("dst") or item.get("zh", "")
    return "original" if isinstance(text, str) and not text.strip() else "tts"


def target_text(item: dict) -> str:
    return item.get("dst") or item.get("zh", "")


def source_info(s: Path) -> dict:
    """Title/tags/description of the source (yt-dlp metadata or local import info)."""
    for name in ("ytdlp_info.json", "local_info.json"):
        p = s / "metadata" / name
        if p.exists():
            return read_json(p)
    return {}


if __name__ == "__main__":
    print(ensure_ffmpeg_on_path())
