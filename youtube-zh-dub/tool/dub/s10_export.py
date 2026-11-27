"""Stage export · user-facing deliverables named after the source video.

  <output.dir>/<标题>/<标题>.中文配音.mkv            soft subtitles (recommended)
                    <标题>.中文配音.mp4            soft subtitles, MP4
                    <标题>.中文配音-硬字幕.mp4      burned-in subtitles
                    字幕/<标题>.zh-en.srt  .zh.srt  .en.srt

<标题> = YouTube title (metadata/ytdlp_info.json) or the local file's original name.
Files are NTFS hard links to the session artifacts: no extra disk space, and they survive deleting
the session folder.  Falls back to a copy when output.dir is on another volume (or output.link=false).
The SRTs sit in a sub-folder so players do not auto-load them over the "subtitles off" default.
Manifest: metadata/export.json.  Folder ownership: <output.dir>/.dub_index.json (sid -> folder).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import log, read_json, session_dir, source_info, write_json  # noqa: E402

_FULLWIDTH = {"<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／", "\\": "＼", "|": "｜", "?": "？", "*": "＊"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
TITLE_MAX = 80     # keeps <repo>/output/<title>/<title>.中文配音-硬字幕.mp4 well under MAX_PATH

# (kind, session-relative source, name pattern relative to the export folder)
DELIVERABLES = [
    ("soft_mkv", "media/video_final_softsub.mkv", "{t}.中文配音.mkv"),
    ("soft_mp4", "media/video_final_softsub.mp4", "{t}.中文配音.mp4"),
    ("hard_mp4", "media/video_final.mp4", "{t}.中文配音-硬字幕.mp4"),
    ("srt_zh_en", "metadata/subtitles.zh-en.srt", "字幕/{t}.zh-en.srt"),
    ("srt_zh", "metadata/subtitles.zh.srt", "字幕/{t}.zh.srt"),
    ("srt_en", "metadata/subtitles.en.srt", "字幕/{t}.en.srt"),
]
KIND_FORMAT = {"soft_mkv": "soft", "soft_mp4": "soft", "hard_mp4": "hard"}


def safe_title(title: str | None, fallback: str, max_len: int | None = TITLE_MAX) -> str:
    """Windows-safe, human-readable file name stem (keeps CJK and spaces)."""
    t = (title or "").strip()
    t = re.sub(r"\s*:\s+", " - ", t)                    # "Part 4: Becoming" -> "Part 4 - Becoming"
    t = "".join(_FULLWIDTH.get(c, c) for c in t)        # remaining illegal chars -> full-width look-alikes
    t = re.sub(r"[\x00-\x1f\x7f]", "", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(". ")
    if max_len is not None and len(t) > max_len:
        t = t[:max_len].rstrip(" .-")
    if not t:
        t = fallback
    if t.split(".")[0].upper() in _RESERVED:
        t += "_"
    return t


def output_root() -> Path:
    return config.repo_path(config.get("output.dir", "output"))


def _hide(p: Path):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x02)   # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def _index(root: Path) -> tuple[Path, dict]:
    p = root / ".dub_index.json"
    return p, (read_json(p) if p.exists() else {})


def _save_index(p: Path, idx: dict):
    if p.exists() and os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x80)       # normal, so it can be replaced
    write_json(p, idx)
    _hide(p)


def choose_folder(root: Path, sid: str, title: str) -> Path:
    """<title>/ unless another session (or a foreign non-empty folder) already owns it -> '<title> [sid]'."""
    idx_p, idx = _index(root)
    if sid in idx and (root / idx[sid]).is_dir() and idx[sid] in (title, f"{title} [{sid}]"):
        return root / idx[sid]
    cand = root / title
    owner = next((k for k, v in idx.items() if v == title), None)
    if (owner and owner != sid) or (owner is None and cand.exists() and any(cand.iterdir())):
        cand = root / f"{title} [{sid}]"
    idx[sid] = cand.name
    root.mkdir(parents=True, exist_ok=True)
    _save_index(idx_p, idx)
    return cand


def place(src: Path, dst: Path, allow_link: bool = True) -> str:
    """Hard-link (or copy) src to dst atomically; no-op when dst already is the same file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if os.path.samefile(src, dst):
                return "same"
        except OSError:
            pass
    tmp = dst.with_name(f".{dst.name}.pending")
    if tmp.exists():
        tmp.replace(dst.with_name(f".{dst.name}.stale"))   # never delete; just get it out of the way
    how = "copy"
    if allow_link:
        try:
            os.link(src, tmp)
            how = "link"
        except OSError:
            pass
    if how == "copy":
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)     # fails with PermissionError if a player has dst open
    return how


def export(s: Path, sid: str, formats: list[str]) -> dict:
    info = source_info(s)
    root = output_root()
    raw_title = info.get("title") or sid
    title = safe_title(raw_title, sid, max_len=None)
    # Preserve the full title when the actual destination has room, including
    # the collision suffix and the temporary filename used during publishing.
    for _, _, pattern in DELIVERABLES:
        dst = root / f"{title} [{sid}]" / pattern.format(t=title)
        temporary = dst.with_name(f".{dst.name}.pending")
        if len(str(temporary).encode("utf-16-le")) // 2 >= 260:
            title = safe_title(raw_title, sid)
            break
    folder = choose_folder(root, sid, title)
    allow_link = bool(config.get("output.link", True))
    files, hows = [], {}
    for kind, rel, pattern in DELIVERABLES:
        fmt = KIND_FORMAT.get(kind)
        if fmt and fmt not in formats:
            continue
        src = s / rel
        if not src.exists():
            continue
        dst = folder / pattern.format(t=title)
        how = place(src, dst, allow_link)
        hows[how] = hows.get(how, 0) + 1
        files.append({"kind": kind, "path": str(dst), "src": rel, "how": how})
    manifest = {"root": str(root), "dir": str(folder), "title": title, "files": files}
    write_json(s / "metadata" / "export.json", manifest)
    log("export", f"ok -> {folder} ({len(files)} files; {', '.join(f'{k}={v}' for k, v in hows.items())})")
    return manifest


def export_ok(s: Path) -> bool:
    p = s / "metadata" / "export.json"
    if not p.exists():
        return False
    m = read_json(p)
    if Path(m.get("root", "")) != output_root():
        return False                        # output.dir changed -> export again
    return bool(m.get("files")) and all(Path(f["path"]).exists() for f in m["files"])


def exported_paths(s: Path) -> list[str] | None:
    if not export_ok(s):
        return None
    return [f["path"] for f in read_json(s / "metadata" / "export.json")["files"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--formats", default="hard,soft")
    a = ap.parse_args()
    export(session_dir(a.id), a.id, [x for x in a.formats.split(",") if x])


if __name__ == "__main__":
    main()
