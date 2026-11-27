"""Stage 1 · download -> media/video_source.mp4 + metadata/ytdlp_info.json | metadata/local_info.json

Sources:
  * YouTube URL  : yt-dlp, original English audio track locked (YouTube auto-dub tracks and
                   "-drc" variants excluded), deno solves the n-challenge.
                   Login wall ("Sign in to confirm you're not a bot") -> exit 20 (needs cookies.txt).
  * local file   : ffprobe; H.264+AAC is stream-copied, otherwise transcoded to H.264/AAC.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from common import (EXIT_COOKIES, FFMPEG, FFPROBE, ensure_ffmpeg_on_path, log, session_dir,  # noqa: E402
                    write_json)

AUTH_MARKERS = ("sign in to confirm", "use --cookies", "login required", "confirm your age",
                "members-only", "private video", "cookies-from-browser")


def youtube_format(h: int) -> str:
    en = "ba[language^=en][format_id!*=drc]"
    anya = "ba[format_id!*=drc]"
    return "/".join([
        f"bv*[height<={h}][vcodec^=avc1]+{en}[ext=m4a]",
        f"bv*[height<={h}][vcodec^=avc1]+{en}",
        f"bv*[height<={h}]+{en}",
        f"bv*[height<={h}][vcodec^=avc1]+{anya}[ext=m4a]",   # videos without language tags
        f"bv*[height<={h}]+{anya}",
        f"b[height<={h}]",
        "b",
    ])


def download_youtube(url: str, s: Path, cookies: str | None, max_height: int) -> int:
    final = s / "media" / "video_source.mp4"
    ffdir = ensure_ffmpeg_on_path()
    import yt_dlp
    opts = {
        "format": youtube_format(max_height),
        "merge_output_format": "mp4",
        "outtmpl": str(s / "tmp" / "dl.%(ext)s"),
        "ffmpeg_location": str(ffdir),
        "noplaylist": True,
        "quiet": False,
        "noprogress": True,
        "concurrent_fragment_downloads": 8,
    }
    deno = config.deno_exe()
    if deno:
        # the host's node carries a preload shim that node's --permission sandbox cannot read
        opts["js_runtimes"] = {"deno": {"path": str(deno)}}
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            out = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        msg = str(e)
        if any(m in msg.lower() for m in AUTH_MARKERS):
            log("download", f"AUTH_REQUIRED: {msg.splitlines()[0][:300]}")
            return EXIT_COOKIES
        raise
    if not out.exists():
        cands = sorted((s / "tmp").glob("dl.*"))
        raise RuntimeError(f"download produced no mp4; found {cands}")
    out.replace(final)
    keep = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "duration", "width", "height", "fps",
                                      "language", "webpage_url", "format", "format_id", "vcodec", "acodec",
                                      "tags", "categories")}
    keep["description"] = (info.get("description") or "")[:1500]
    keep["source"] = "youtube"
    write_json(s / "metadata" / "ytdlp_info.json", keep)
    log("download", f"ok -> {final} ({final.stat().st_size/1e6:.1f} MB) format={keep['format_id']} "
                    f"title={keep['title']!r} duration={keep['duration']}s")
    return 0


def probe(path: Path) -> dict:
    r = subprocess.run([FFPROBE(), "-v", "error", "-show_entries",
                        "stream=index,codec_type,codec_name,channels,width,height:format=duration",
                        "-of", "json", str(path)], check=True, capture_output=True, text=True)
    return json.loads(r.stdout)


def import_local(src: Path, s: Path) -> int:
    final = s / "media" / "video_source.mp4"
    info = probe(src)
    streams = info.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    if v is None or a is None:
        raise RuntimeError(f"local file needs one video and one audio stream: {src}")
    vcopy, acopy = v.get("codec_name") == "h264", a.get("codec_name") == "aac"
    tmp = s / "media" / ".video_source.pending.mp4"
    cmd = [FFMPEG(), "-y", "-loglevel", "error", "-stats", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0"]
    cmd += ["-c:v", "copy"] if vcopy else ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if acopy else ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.replace(final)
    st = src.stat()
    meta = {"source": "local", "source_path": str(src.resolve()), "size": st.st_size, "mtime": st.st_mtime,
            "title": src.stem, "duration": float(info.get("format", {}).get("duration") or 0),
            "width": v.get("width"), "height": v.get("height"), "vcodec": v.get("codec_name"),
            "acodec": a.get("codec_name"), "channels": a.get("channels"),
            "transcoded_video": not vcopy, "transcoded_audio": not acopy}
    write_json(s / "metadata" / "local_info.json", meta)
    log("download", f"ok -> imported {src.name} ({'copy' if vcopy else 'x264'}/{'copy' if acopy else 'aac'}) "
                    f"{final.stat().st_size/1e6:.1f} MB, {meta['duration']:.1f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="YouTube URL or local video path")
    ap.add_argument("--id", required=True)
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--max-height", type=int, default=int(config.get("download.max_height", 1080)))
    a = ap.parse_args()

    s = session_dir(a.id)
    final = s / "media" / "video_source.mp4"
    if final.exists() and final.stat().st_size > 0:
        log("download", f"reuse {final}")
        return 0
    ensure_ffmpeg_on_path()
    if os.path.exists(a.source):
        return import_local(Path(a.source), s)
    return download_youtube(a.source, s, a.cookies, a.max_height)


if __name__ == "__main__":
    sys.exit(main())
