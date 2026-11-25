"""Supplementary Vulkan probe, source snapshot and raw-file integrity."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import tarfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pull_verified(d, remote, local):
    local.parent.mkdir(parents=True, exist_ok=True)
    remote_hash = d.shell('sha256sum ' + shlex.quote(remote), root=True).split()[0]
    d.call('pull', remote, str(local))
    item = {'file': str(local), 'device_sha256': remote_hash, 'sha256': digest(local), 'bytes': local.stat().st_size}
    item['verified'] = item['sha256'] == remote_hash
    if not item['verified']:
        raise RuntimeError('File integrity mismatch: ' + remote)
    return item


def parse_probe(text):
    result = {'origin': 'independent_vulkan_probe', 'properties': {}, 'features': {}, 'extensions': [],
              'executables': [], 'statistics': [], 'internal_representations': [], 'mapped_libraries': []}
    for line in text.splitlines():
        if line.startswith('PROPERTY '):
            key, value = line[9:].split(' ', 1)
            try:
                value = float(value) if '.' in value or 'e' in value.lower() else int(value)
            except ValueError:
                pass
            result['features' if 'Features.' in key else 'properties'][key] = value
        elif line.startswith('EXT '):
            _, name, version = line.split()
            result['extensions'].append({'name': name, 'spec_version': int(version)})
        elif line.startswith('EXEC '):
            m = re.match(r'EXEC (\d+) (.*?) \| (.*?) \| subgroup=(\d+)', line)
            if m: result['executables'].append(dict(index=int(m[1]),name=m[2],description=m[3],subgroup_size=int(m[4])))
        elif line.startswith('EXECUTABLE '):
            result['features']['pipeline_executable_properties'] = {k:bool(int(v)) for k,v in re.findall(r'(\w+)=(\d+)',line)}
        elif line.startswith('STAT '):
            m = re.match(r'STAT (\d+) (.*?) \| (.*?) \| (?:format=(\d+) )?(.+)', line)
            if m:
                value = m[5]
                try: value = float(value) if m[4] == '3' else int(value)
                except ValueError: pass
                result['statistics'].append(dict(executable=int(m[1]),name=m[2],description=m[3],format=int(m[4]) if m[4] else None,value=value))
        elif line.startswith('IR_COUNT '):
            _, index, count = line.split()
            result['internal_representations'].append({'executable': int(index), 'count': int(count)})
        elif line.startswith('VERIFY '):
            result['verification'] = {k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',line)}
        elif line.startswith('GPU_TIMESTAMP '):
            result['single_dispatch_ns'] = float(re.search(r'region_ns=([\d.]+)',line)[1])
        elif line.startswith('PIPELINE '):
            result['cpu_create_ms'] = float(re.search(r'cpu_create_ms=([\d.]+)',line)[1])
        elif line.startswith('MAP '): result['mapped_libraries'].append(line[4:])
    # Driver strings span multiple lines; retain the exact preamble separately.
    result['driver_description'] = text[text.find('GPU '):text.find('EXT ')]
    result['statistic_count'] = len(result['statistics'])
    return result


def prepare_probe(d, remote):
    assets = Path(__file__).resolve().parents[1] / 'assets'
    abi = d.shell('getprop ro.product.cpu.abi')
    if abi != 'arm64-v8a':
        return {'status':'unsupported', 'reason':'Bundled Vulkan probe supports arm64-v8a only', 'abi':abi}
    meta = json.loads((assets/'probe-build.json').read_text(encoding='utf-8'))
    binary = assets/'qglprobe-arm64-v8a'
    if digest(binary) != meta['files'][binary.name]:
        raise RuntimeError('Bundled Vulkan probe hash mismatch')
    d.call('push', str(binary), remote+'/qglprobe')
    d.shell('chmod 755 '+shlex.quote(remote+'/qglprobe'), root=True)
    return {'status':'ready','abi':abi,'binary_sha256':digest(binary),'build':meta}


def source_snapshot(d, info, remote, out, roots):
    q = shlex.quote
    package = info['package']
    dump = d.shell('dumpsys package '+q(package))
    match = re.search(r'\bdataDir=(\S+)', dump)
    if not match:
        return {'status':'unavailable','reason':'Package dataDir was not reported'}
    base = match[1]+'/files'
    discovered = False
    if not roots:
        candidates = d.shell('find '+q(base)+' -type d -name '+q(Path.cwd().name.lower()),root=True,check=False).splitlines()
        roots = [p for p in candidates if p.startswith(base+'/')]
        discovered = bool(roots)
    extensions = {'.slang','.slangh','.hlsl','.glsl','.vert','.frag','.comp','.spv','.pyscene'}
    all_files = d.shell('find '+q(base)+' -type f',root=True).splitlines()
    selected = {p for p in all_files if p.startswith(base+'/') and Path(p).suffix.lower() in extensions}
    for root in roots:
        if not root.startswith(base+'/') or '..' in Path(root).parts:
            raise RuntimeError('--source-root must be inside the app files directory')
        selected.update(p for p in all_files if p.startswith(root.rstrip('/')+'/') and Path(p).suffix.lower() in extensions|{'.py','.json'})
    if not selected:
        return {'status':'unavailable','reason':'No supported shader/source files found','app_files':base}
    relative = sorted(p[len(base)+1:] for p in selected)
    local_list = out/'source-files.txt'
    local_list.write_text('\n'.join(relative)+'\n',encoding='utf-8',newline='\n')
    d.call('push',str(local_list),remote+'/source-files.txt')
    d.shell('cd '+q(base)+' && tar -cf '+q(remote+'/source-snapshot.tar')+' -T '+q(remote+'/source-files.txt'),root=True,timeout=60)
    archive = out/'source-snapshot.tar'
    integrity = pull_verified(d,remote+'/source-snapshot.tar',archive)
    manifest=[]
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isfile():
                with tar.extractfile(member) as stream:
                    manifest.append({'path':base+'/'+member.name,'archive_path':member.name,
                                     'bytes':member.size,'sha256':hashlib.file_digest(stream,'sha256').hexdigest()})
    return {'status':'captured','app_files':base,'source_roots':roots,'roots_discovered':discovered,
            'scope':'Shader files throughout app files; Python/JSON additionally within selected project source roots',
            'archive':'source-snapshot.tar','integrity':integrity,'files':manifest,
            'exclusions':'App user databases and non-source assets; use --source-root for another source subtree'}


def verify_sources(d, sources, remote):
    if sources.get('status') != 'captured': return
    text = d.shell('cd '+shlex.quote(sources['app_files'])+' && while IFS= read -r f; do sha256sum "$f"; done < '+shlex.quote(remote+'/source-files.txt'),root=True,timeout=60)
    actual = {line.split(None,1)[1]:line.split()[0] for line in text.splitlines() if line.strip()}
    sources['changed_after_capture'] = [f['archive_path'] for f in sources['files'] if actual.get(f['archive_path']) != f['sha256']]
    sources['verified_after_capture'] = not sources['changed_after_capture']


def driver_libraries(d, probe_runs, out):
    paths = {line.split()[-1] for run in probe_runs for line in run.get('mapped_libraries',[]) if line.split() and line.split()[-1].startswith(('/vendor/','/system_ext/'))}
    # The compiler backend may have unloaded by the time /proc/self/maps is read.
    for path in ('/vendor/lib64/libllvm-qcom.so','/vendor/lib64/libllvm-qgl.so','/vendor/lib64/hw/vulkan.adreno.so'):
        if d.shell('test -f '+shlex.quote(path)+' && echo YES',root=True,check=False): paths.add(path)
    items=[]
    for path in sorted(paths):
        item=pull_verified(d,path,out/'driver-libraries'/path.lstrip('/'))
        item['device_path']=path
        items.append(item)
    return {'status':'captured' if items else 'unavailable','files':items}
