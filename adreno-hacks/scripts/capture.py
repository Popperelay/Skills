"""Portable Adreno driver log capture; Python standard library + adb."""
import argparse
import html
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from shader_stats import parse_logs
from device_runtime import recovery_script, worker_script, watchdog_script, validate_state
from evidence import prepare_probe, source_snapshot, pull_verified, parse_probe, verify_sources, driver_libraries

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def find_adb(override=None):
    candidates = [override, shutil.which("adb")]
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(key):
            candidates.append(str(Path(os.environ[key]) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    try:
        home = Path.home()
    except RuntimeError:
        home = Path.cwd()
    candidates += [str(home / "AppData/Local/Android/Sdk/platform-tools/adb.exe"),
                   str(home / "Android/Sdk/platform-tools/adb"),
                   str(home / "Library/Android/sdk/platform-tools/adb")]
    # Reuse the executable of an already running ADB server, regardless of SDK installation.
    if os.name == "nt":
        try:
            p = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_Process -Filter \"name='adb.exe'\").ExecutablePath"],
                               capture_output=True, timeout=15)
            candidates += p.stdout.decode("utf-8", "replace").splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
    for candidate in candidates:
        if candidate and Path(candidate.strip()).is_file():
            return str(Path(candidate.strip()).resolve())
    raise RuntimeError("ADB not found; put platform-tools on PATH or use --adb.")

class Device:
    def __init__(self, adb, serial=None):
        self.adb, self.serial = adb, serial
        self.commands = []
        if not serial:
            lines = self.call("devices").splitlines()
            devices = [l.split()[0] for l in lines if len(l.split()) == 2 and l.split()[1] == "device"]
            if len(devices) != 1:
                raise RuntimeError("Expected one authorized phone; use --serial. " + repr(lines))
            self.serial = devices[0]

    def call(self, *args, timeout=30, check=True):
        command = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        p = subprocess.run(command, capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        self.commands.append(dict(args=list(args), code=p.returncode, stdout=out, stderr=err))
        if check and p.returncode:
            raise RuntimeError(f"ADB failed ({p.returncode}): {err or out}")
        return out.strip()

    def shell(self, script, root=False, **kwargs):
        return self.call("shell", ("su -c " + shlex.quote(script)) if root else script, **kwargs)

def probe(d, package=None, config=None):
    info = {"serial": d.serial, "adb": d.adb}
    for key in ("ro.product.model", "ro.soc.model", "ro.build.fingerprint"):
        info[key] = d.shell("getprop " + key)
    info["gpu"] = d.shell("cat /sys/class/kgsl/kgsl-3d0/gpu_model", check=False)
    info["root"] = d.shell("id", root=True, check=False)
    info["selinux"] = d.shell("getenforce")
    activity = d.shell("dumpsys activity activities")
    matches = re.findall(r"(?:mResumedActivity|topResumedActivity).*? u\d+ ([\w.]+)/(?:[\w.$]+)", activity)
    if not matches:
        window = d.shell("dumpsys window windows")
        matches = re.findall(r"mCurrentFocus=.*? u\d+ ([\w.]+)/[\w.$]+", window)
    info["package"] = package or (matches[-1] if matches else None)
    info["config"] = config
    for directory in ("/data/vendor/gpu", "/data/misc/gpu"):
        if not info["config"] and d.shell("test -d " + directory + " && echo yes", root=True, check=False) == "yes":
            info["config"] = directory + "/qgl_config.txt"
    info["driver"] = d.shell("getprop ro.gfx.driver.0; getprop ro.hardware.vulkan")
    return info

def report(out, info, rows, error=None):
    collection = json.loads((out / "collection.json").read_text(encoding="utf-8")) if (out / "collection.json").exists() else {}
    write_json(out / "summary.json", {"schema_version": 2, "device": info, "shaders": rows, "collection": collection, "shader_counts": {"application": sum(r.get("origin") == "application" for r in rows), "probe": sum(r.get("origin") == "probe" for r in rows)}, "error": error})
    esc = html.escape
    links = ['<a href="summary.json">JSON</a>']
    for name in ("runner.json", "restoration.json", "recovery.json", "collection.json", "parse-audit.json", "source-snapshot.tar"):
        if (out / name).exists():
            links.append(f'<a href="{name}">{name}</a>')
    body = "<h1>Adreno shader 采集</h1><nav>" + " · ".join(links) + "</nav>"
    body += "<pre>" + esc(json.dumps(info, ensure_ascii=False, indent=2)) + "</pre>"
    if error:
        body += "<p>Capture error: " + esc(error) + "</p>"
    if not rows:
        body += "<p>未识别到 ISA。请检查原始日志、应用启动以及 pipeline 编译或缓存行为。</p>"
    body += "<p>应用与独立 Vulkan 探针的结果分开记录。编译统计为静态数据；采集开销会影响性能计时。binlog 保留原始二进制。</p>"
    body += "<table><tr><th>来源 / Stage / CRC</th><th>Full / Half</th><th>Scratch</th><th>Max waves</th><th>Evidence</th></tr>"
    for r in rows:
        body += f'<tr><td>{esc(r["origin"])} / {esc(r["stage"])} / {esc(r["hw_crc"])}</td><td>{r["full_registers"]} / {r["half_registers"]}</td><td>{r["scratch_bytes"]}</td><td>{r["max_waves"]}</td><td><a href="{r["isa_file"]}">ISA</a> · <a href="{r["stats_file"]}">Stats</a></td></tr>'
    body += "</table><h2>补充采集</h2><pre>" + esc(json.dumps({"probe_status": collection.get("probe", {}).get("status"), "source_status": collection.get("sources", {}).get("status"), "logcat": collection.get("logcat")}, ensure_ascii=False, indent=2)) + "</pre><h2>原始证据</h2><ul>"
    for run in collection.get('probe', {}).get('runs', []):
        if run.get('pipeline_cache'):
            body += f'<li><a href="{esc(run["pipeline_cache"], quote=True)}">独立探针 pipeline cache</a>；API 统计 {run["statistic_count"]} 项（详见 JSON）</li>'
    for item in collection.get('driver_libraries', {}).get('files', []):
        path = Path(item['file']).relative_to(out).as_posix()
        body += f'<li><a href="{esc(path, quote=True)}">{esc(item["device_path"])}</a></li>'
    for p in sorted((out / "raw").rglob("*")):
        if p.is_file():
            body += f'<li><a href="{esc(p.relative_to(out).as_posix(), quote=True)}">{esc(p.relative_to(out).as_posix())}</a></li>'
    body += "</ul><p>缺失字段表示不可用或未识别，不能按零解释。source_crc 是驱动 CRC，不是源文件 SHA-256；未确认的 Pass 对应关系保持未知。</p>"
    (out / "index.html").write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Adreno capture</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{padding:10px;border:1px solid #ccd;text-align:left}pre{white-space:pre-wrap;background:#eef2f8;padding:16px}a{color:#065db7}</style>' + body + "</html>", encoding="utf-8")

def restore(d, state, out, stop=False):
    validate_state(state)
    if not (out / "recovery.json").exists(): write_json(out / "recovery.json", state)
    result = {"restored": False, "config": state["config"], "recovery": str(out / "recovery.json")}
    try:
        output = d.shell("sh " + shlex.quote(state["remote"] + "/restore.sh") + (" --stop" if stop else ""), root=True)
        check = d.shell("test ! -e " + shlex.quote(state["remote"] + "/armed") + " && test ! -d /data/local/tmp/adreno-capture.lock && echo VERIFIED", root=True)
        result.update(restored="RESTORED" in output and check == "VERIFIED", output=output)
    except Exception as error:
        result["error"] = str(error)
    write_json(out / "restoration.json", result)
    return result


def capture(d, info, args, out):
    package, config = info["package"], info["config"]
    if not package or not re.fullmatch(r"[A-Za-z0-9_.]+", package) or package == "com.android.systemui" or "launcher" in package.lower():
        raise RuntimeError("Select the intended foreground app or use --package.")
    if "uid=0" not in info["root"] or info["selinux"] != "Permissive":
        raise RuntimeError("Existing root and Permissive SELinux are required; no settings changed.")
    if config not in ("/data/vendor/gpu/qgl_config.txt", "/data/misc/gpu/qgl_config.txt"):
        raise RuntimeError("No supported configuration path.")
    if d.shell("test -d /data/local/tmp/adreno-capture.lock && echo LOCKED",root=True,check=False):
        raise RuntimeError("Another capture owns the device lock; recover its recovery.json first.")
    component = d.shell("cmd package resolve-activity --brief " + shlex.quote(package)).splitlines()[-1]
    if not re.fullmatch(re.escape(package) + r"/[\w.$]+", component):
        raise RuntimeError("Cannot resolve launch activity: " + component)
    extras = json.loads(args.launch_args)
    if not isinstance(extras,list) or not all(isinstance(v,str) for v in extras):
        raise RuntimeError("--launch-args must be a JSON string array.")
    uid = re.search(r'uid:(\d+)', d.shell('cmd package list packages -U '+shlex.quote(package)))
    if not uid: raise RuntimeError('Cannot resolve app UID for streaming logcat')
    remote = "/data/local/tmp/adreno-capture-" + uuid.uuid4().hex
    state = dict(schema_version=2,remote=remote,config=config,package=package,adb=d.adb,serial=d.serial)
    write_json(out / "recovery.json", state)
    q = shlex.quote
    directory = config.rsplit('/',1)[0]
    def listing():
        text=d.shell("find "+q(directory)+" -maxdepth 1 -type f -exec stat -c '%n|%s|%Y' {} \\;",root=True)
        return {s.split('|')[0]:s for s in text.splitlines() if '|' in s}
    before=listing()
    d.shell('mkdir -p '+q(remote))
    evidence={"seconds":args.seconds,"binlog_requested":args.binlog,"integrity":[],"probe":{},"sources":{}}
    write_json(out/'collection.json',evidence)
    probe_info=prepare_probe(d,remote)
    evidence['probe']=probe_info
    evidence['sources']=source_snapshot(d,info,remote,out,args.source_root)
    write_json(out/'collection.json',evidence)
    scripts={'restore.sh':recovery_script(state),'capture.sh':worker_script(state,component,extras,args.seconds,args.binlog,int(uid[1]),probe_info['status']=='ready'),
             'watchdog.sh':watchdog_script(state,args.seconds)}
    for name,text in scripts.items():
        (out/name).write_text(text,encoding='utf-8',newline='\n')
        d.call('push',str(out/name),remote+'/'+name)
    failure=None
    try:
        d.shell('nohup sh '+q(remote+'/watchdog.sh')+' > '+q(remote+'/watchdog.log')+' 2>&1 < /dev/null & nohup sh '+q(remote+'/capture.sh')+' > '+q(remote+'/worker.log')+' 2>&1 < /dev/null &',root=True)
        deadline=time.monotonic()+args.seconds+75
        while time.monotonic()<deadline:
            status=d.shell('cat '+q(remote+'/exitcode')+' 2>/dev/null || true',root=True)
            if status:
                if status!='0': failure='Device worker exited '+status
                break
            if d.shell('test -f '+q(remote+'/done')+' && echo DONE',root=True,check=False):
                time.sleep(.2)
                status=d.shell('cat '+q(remote+'/exitcode')+' 2>/dev/null || true',root=True)
                if status!='0': failure='Device worker interrupted; watchdog/recovery completed'
                break
            time.sleep(.5)
        else: failure='Device capture deadline exceeded'
    except (Exception,KeyboardInterrupt) as error:
        failure=str(error) or type(error).__name__
    finally:
        restored=restore(d,state,out,stop=True)
        if not restored['restored']:
            raise RuntimeError('Restoration incomplete; reconnect and use --recover. '+str(restored))
    # Configuration is verified before any secondary parsing or file transfer.
    time.sleep(1)
    evidence['restoration']=restored
    evidence['worker_identity']=d.shell('cat '+q(remote+'/worker'),root=True,check=False)
    pids=set(d.shell('cat '+q(remote+'/pids')+' 2>/dev/null || true',root=True).split())
    info['captured_pids']=sorted(pids)
    verify_sources(d,evidence['sources'],remote)
    probe_pids=set()
    if probe_info['status']=='ready':
        runs=[]
        for run in (0,1):
            dest=out/'probe'/str(run)
            dest.mkdir(parents=True,exist_ok=True)
            files=d.shell('find '+q(remote+'/probe/'+str(run))+' -maxdepth 1 -type f',root=True,check=False).splitlines()
            for path in files:
                if not path.startswith(remote+'/probe/'): continue
                evidence['integrity'].append(pull_verified(d,path,dest/Path(path).name))
            if (dest/'stdout.txt').exists():
                parsed=parse_probe((dest/'stdout.txt').read_text(encoding='utf-8'))
                parsed['exit_code']=int((dest/'exitcode').read_text()) if (dest/'exitcode').exists() else None
                parsed['status']='captured' if parsed['exit_code']==0 else 'failed'
                parsed['pipeline_cache']='probe/'+str(run)+'/pipeline_cache.bin' if (dest/'pipeline_cache.bin').exists() else None
                runs.append(parsed)
            if (dest/'pid').exists():probe_pids.update((dest/'pid').read_text().split())
        evidence['probe']['runs']=runs
        evidence['probe']['status']='captured' if len(runs)==2 and all(r['status']=='captured' for r in runs) else 'incomplete'
    evidence['driver_libraries']=driver_libraries(d,evidence['probe'].get('runs',[]),out)
    raw=out/'raw';raw.mkdir(exist_ok=True)
    after=listing()
    for path,record in after.items():
        name=path.rsplit('/',1)[-1]
        if before.get(path)==record or not re.match(r'(?:glsl_shader_|qgl_bin_log_)',name):continue
        origin='application' if any(re.search(r'(?:^|_)'+re.escape(pid)+r'(?:_|[.])',name) for pid in pids) else 'probe' if any(re.search(r'(?:^|_)'+re.escape(pid)+r'(?:_|[.])',name) for pid in probe_pids) else None
        if not origin:continue
        staged=remote+'/'+name
        d.shell('cp '+q(path)+' '+q(staged)+' && chmod 644 '+q(staged),root=True)
        evidence['integrity'].append(pull_verified(d,staged,raw/origin/name))
        d.shell('rm -f '+q(staged),root=True)
    for name in ('logcat-window.txt','logcat-error.txt','worker.log','watchdog.log','config.signature','config.after'):
        if d.shell('test -f '+q(remote+'/'+name)+' && echo YES',root=True,check=False):
            evidence['integrity'].append(pull_verified(d,remote+'/'+name,raw/name))
    logcat=(raw/'logcat-window.txt').read_text(encoding='utf-8') if (raw/'logcat-window.txt').exists() else ''
    evidence['logcat']={'mode':'continuous_device_stream_by_app_uid','uid':int(uid[1]),'file':'raw/logcat-window.txt','bytes':len(logcat.encode()),'first_frame_marker_present':'RAYLOOM_ANDROID_FRAME_OK' in logcat,
                        'scope':'App UID main/system/crash buffers from before launch until shutdown; kernel buffer drops remain possible'}
    evidence['pid_coverage']={'application':sorted(pids),'probe':sorted(probe_pids),'method':'ps package and colon subprocess names sampled each second; very short subprocesses can be missed'}
    write_json(out/'collection.json',evidence)
    # Retain small recovery metadata on device; remove the duplicated source archive.
    d.shell('rm -f '+q(remote+'/source-snapshot.tar'),root=True,check=False)
    if failure:raise RuntimeError(failure)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("adb", "serial", "package", "config", "out", "parse", "recover"):
        p.add_argument("--" + key)
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--binlog", action="store_true")
    p.add_argument("--source-root", action="append", default=[], help="Device source directory inside app files; repeatable")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--launch-args", default="[]")
    args = p.parse_args()
    if not 1 <= args.seconds <= 300:
        p.error("--seconds must be 1..300")
    base = (Path.cwd() / "report").resolve()
    out = Path(args.out).resolve() if args.out else base / ("adreno-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    if not out.is_relative_to(base):
        p.error("--out must be inside the current project's report/")
    if not (args.probe or args.parse or args.recover) and (out / "recovery.json").exists():
        p.error("--out already contains a capture; choose a new output directory")
    out.mkdir(parents=True, exist_ok=True)
    info, rows, error, d = {}, [], None, None
    try:
        if args.parse:
            info = {"offline_source": str(Path(args.parse).resolve())}
            if not Path(args.parse).exists():
                raise RuntimeError("Parse source does not exist.")
            rows = parse_logs(Path(args.parse), out)
        elif args.recover:
            state = json.loads(Path(args.recover).read_text(encoding="utf-8"))
            d = Device(args.adb or state["adb"], args.serial or state["serial"])
            result = restore(d, state, out, stop=True)
            info = {"recovery": state, "result": result}
            if not result['restored']: raise RuntimeError('Recovery verification failed')
        else:
            d = Device(find_adb(args.adb), args.serial)
            info = probe(d, args.package, args.config)
            if not args.probe:
                capture(d, info, args, out)
                rows = parse_logs(out / "raw", out)
    except (Exception, KeyboardInterrupt) as e:
        error = str(e) or type(e).__name__
    finally:
        if (out / "raw").exists() and not rows:
            rows = parse_logs(out / "raw", out)
        if d:
            write_json(out / "runner.json", d.commands)
        report(out, info, rows, error)
    print(json.dumps({"report": str(out / "index.html"), "shader_count": len(rows), "error": error,
                      "device": info}, ensure_ascii=False))
    return 1 if error else 0

if __name__ == "__main__":
    sys.exit(main())
