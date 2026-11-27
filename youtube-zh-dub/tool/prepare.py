"""Check the local Windows runtime and fetch only missing or damaged components."""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

TOOL = Path(__file__).resolve().parent
RUNTIME = Path(sys.executable).parent.parent
DEMUCS_REV = "d74ac89c3a1e874fc78f152555cf4d8533f06cd4"
FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-essentials_build.zip"
FFMPEG_SHA = "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd"
CUDA_INDEX = "https://download.pytorch.org/whl/cu128"


def sha256(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def download(url, dest, expected, size):
    """Publish only verified files. Resume an interrupted transfer when supported."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset != size:
        headers = {"Range": f"bytes={offset}-"} if 0 < offset < size else {}
        req = urllib.request.Request(url, headers=headers)
        print(f"Downloading {dest.name}...", flush=True)
        with urllib.request.urlopen(req, timeout=90) as response:
            append = response.status == 206 and bool(headers)
            if response.status == 206 and not response.headers.get("Content-Range", "").startswith(
                f"bytes {offset if append else 0}-"
            ):
                raise RuntimeError(f"Unexpected HTTP byte range for {dest.name}")
            with part.open("ab" if append else "wb") as out:
                shutil.copyfileobj(response, out, 1024 * 1024)
    if part.stat().st_size != size:
        raise RuntimeError(f"Incomplete download of {dest.name}. Run the same command to retry.")
    if sha256(part) != expected:
        part.unlink()
        raise RuntimeError(f"Checksum mismatch for {dest.name}. Run the same command to retry.")
    part.replace(dest)


def pip(*args):
    subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", *args], check=True)


def ensure_packages():
    module_names = {"openai-whisper": "whisper", "yt-dlp": "yt_dlp", "triton-windows": "triton"}
    for filename in ("constraints-cu128.txt", "requirements.txt"):
        missing = []
        repair = []
        for line in (TOOL / filename).read_text(encoding="utf-8").splitlines():
            req = line.partition("#")[0].strip()
            if not req:
                continue
            spec, _, pin = req.partition("==")
            name = spec.partition("[")[0]
            try:
                current = metadata.version(name)
            except metadata.PackageNotFoundError:
                current = None
            if not current or (pin and pin != current):
                missing.append(req)
            elif importlib.util.find_spec(module_names.get(name, name)) is None:
                repair.append(req)
        # CUDA wheels are already installed before resolving the other packages.
        index = ["--index-url", CUDA_INDEX] if filename.startswith("constraints") else [
            "-c", str(TOOL / "constraints-cu128.txt")
        ]
        if repair:
            pip("--no-deps", "--force-reinstall", *index, *repair)
        if missing:
            # Let pip resolve dependencies of the complete, constrained environment.
            pip(*index, "-r", str(TOOL / filename))
    check = [sys.executable, "-m", "pip", "--disable-pip-version-check", "check"]
    if subprocess.run(check, stdout=subprocess.DEVNULL).returncode:
        pip("-c", str(TOOL / "constraints-cu128.txt"), "-r", str(TOOL / "requirements.txt"))
        subprocess.run(check, check=True)


def executable_ok(path):
    if not path.is_file():
        return False
    try:
        return subprocess.run(
            [str(path), "-version" if path.name.startswith("ff") else "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_binaries(runtime):
    target = runtime / "bin"
    target.mkdir(parents=True, exist_ok=True)
    names = [n for n in ("ffmpeg.exe", "ffprobe.exe") if not executable_ok(target / n)]
    if names:
        archive = runtime / "ffmpeg-essentials.zip"
        if not archive.exists() or sha256(archive) != FFMPEG_SHA:
            download(FFMPEG_URL, archive, FFMPEG_SHA, 119467219)
        with zipfile.ZipFile(archive) as z:
            for name in names:
                member = next(m for m in z.namelist() if m.endswith("/bin/" + name))
                part = target / (name + ".part")
                with z.open(member) as src, part.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                part.replace(target / name)
        archive.unlink()
    if not executable_ok(target / "deno.exe"):
        source = Path(sys.executable).parent / "Scripts" / "deno.exe"
        if not executable_ok(source):
            pip("--force-reinstall", "--no-deps", "deno==" + metadata.version("deno"))
        part = target / "deno.exe.part"
        shutil.copyfile(source, part)
        part.replace(target / "deno.exe")


def cached_sources(item):
    path = Path(item["path"])
    home = Path.home()
    if path.parent.name == "whisper":
        yield home / ".cache" / "whisper" / path.name
    if path.parent.name == "voxcpm2" and os.environ.get("VOXCPM_MODEL_DIR"):
        yield Path(os.environ["VOXCPM_MODEL_DIR"]) / path.name
    if path.parent.name == "campplus":
        yield home / ".cache/modelscope/models/iic--speech_campplus_sv_zh_en_16k-common_advanced/snapshots/master" / path.name
    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or home / ".cache/huggingface") / "hub")
    if path.parts[:2] == ("models", "hub"):
        yield hub.joinpath(*path.parts[2:])
    if path.parent.name == "voxcpm2":
        yield from hub.glob("models--openbmb--VoxCPM2/snapshots/*/" + path.name)


def ensure_models(runtime, items):
    stamp = runtime / "model-checks.json"
    try:
        checks = json.loads(stamp.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        checks = {}
    for item in items:
        key, size, digest = item["path"], item["size"], item["sha256"]
        dest = runtime / key
        if dest.is_file() and dest.stat().st_size == size:
            signature = [digest, size, dest.stat().st_mtime_ns]
            if checks.get(key) == signature:
                continue
            if sha256(dest) == digest:
                checks[key] = signature
                continue
        for source in cached_sources(item):
            if source.is_file() and source.stat().st_size == size and sha256(source) == digest:
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + ".part")
                shutil.copyfile(source, part)
                part.replace(dest)
                break
        else:
            download(item["url"], dest, digest, size)
        checks[key] = [digest, size, dest.stat().st_mtime_ns]
    tmp = stamp.with_suffix(".tmp")
    tmp.write_text(json.dumps(checks), encoding="utf-8")
    tmp.replace(stamp)


def main():
    ensure_packages()
    ensure_binaries(RUNTIME)
    ensure_models(RUNTIME, json.loads((TOOL / "resources.json").read_text(encoding="utf-8")))
    ref = RUNTIME / "models/hub/models--adefossez--HTDemucs-ft/refs/main"
    if not ref.exists() or ref.read_text().strip() != DEMUCS_REV:
        ref.parent.mkdir(parents=True, exist_ok=True)
        temp = ref.with_suffix(".tmp")
        temp.write_text(DEMUCS_REV, encoding="ascii")
        temp.replace(ref)
    print("Runtime and model checks passed.", flush=True)


if __name__ == "__main__":
    main()
