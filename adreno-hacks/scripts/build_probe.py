"""Rebuild the bundled Android arm64 probe with a local Android NDK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--ndk',required=True)
args=p.parse_args()
ndk=Path(args.ndk).resolve()
assets=Path(__file__).resolve().parents[1]/'assets'
compilers=list((ndk/'toolchains/llvm/prebuilt').glob('*/bin/clang'+('.exe' if os.name=='nt' else '')))
if len(compilers)!=1: p.error('Expected one host clang in the selected NDK')
clang=compilers[0]
flags=['--target=aarch64-linux-android28','--sysroot='+str(clang.parents[1]/'sysroot'),'-O2','-std=c11','-D_POSIX_C_SOURCE=200809L','probe.c','-lvulkan','-o','qglprobe-arm64-v8a']
subprocess.run([str(clang),*flags],cwd=assets,check=True)
meta={'abi':'arm64-v8a','android_api':28,'ndk_version':ndk.name,
      'build_args':[s if not s.startswith('--sysroot=') else '--sysroot=<NDK toolchain>/sysroot' for s in flags],
      'files':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in assets.iterdir() if f.is_file() and f.name!='probe-build.json'},
      'source_origin':'Adapted from the local 2026-10-15 Vulkan probe; shader and embedded SPIR-V source included.'}
(assets/'probe-build.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
print(json.dumps({'binary':str(assets/'qglprobe-arm64-v8a'),'sha256':meta['files']['qglprobe-arm64-v8a']}))
