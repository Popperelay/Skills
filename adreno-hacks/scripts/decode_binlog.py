"""Decode self-describing QGL binlogs; stop at unknown or incomplete records.

Derived from three files captured with one Adreno driver. Markers are per stream.
Does not interpret timestamp units, hardware counters, or signed integer semantics.
"""
import argparse
import collections
import hashlib
import html
import json
import mmap
from pathlib import Path
import sys
import time
from urllib.parse import quote


class DecodeError(ValueError):
    def __init__(self, code, offset, message):
        super().__init__(message)
        self.code, self.offset = code, offset


class Reader:
    def __init__(self, data, sample_limit=2):
        self.b = data
        self.pos = 0
        if not len(data):
            raise DecodeError('empty_input', 0, 'Empty binlog')
        self.definition = data[0]
        if not 1 <= self.definition <= 253:
            raise DecodeError('unsupported_or_corrupt', 0, 'Unsupported definition marker')
        self.enumeration = (self.definition + 1) & 255
        self.end = (self.definition + 2) & 255
        self.schemas = {}
        self.enums = {}
        self.counts = collections.Counter()
        self.top_counts = collections.Counter()
        self.samples = collections.defaultdict(list)
        self.sample_limit = sample_limit
        self.schema_bytes = 0
        self.numeric = {}
        self.roundtrip = hashlib.sha256()
        self.roundtrip_bytes = 0

    def take(self, n):
        start = self.pos
        if self.pos + n > len(self.b):
            raise DecodeError('truncated', start, f'truncated at {start:#x}, need {n}, remaining {len(self.b)-start}')
        self.pos += n
        return self.b[start:self.pos]

    def string(self):
        stop = self.b.find(b'\0', self.pos)
        if stop < 0:
            raise DecodeError('truncated', self.pos, f'unterminated string at {self.pos:#x}')
        raw = self.b[self.pos:stop]
        self.pos = stop + 1
        try:
            return raw.decode('utf-8')
        except UnicodeError as error:
            raise DecodeError('unsupported_or_corrupt', stop-len(raw), 'Invalid UTF-8 string') from error

    def definitions(self):
        while self.pos < len(self.b) and self.b[self.pos] in (self.definition, self.enumeration):
            start = self.pos
            marker, tag = self.take(2)
            values = []
            while True:
                value = self.string()
                if value == '':
                    break
                values.append(value)
            if not values:
                raise DecodeError('unsupported_or_corrupt', start, 'Empty definition')
            table = self.schemas if marker == self.definition else self.enums
            if tag == 0 or tag in (self.definition, self.enumeration, self.end) or tag in self.schemas or tag in self.enums:
                raise DecodeError('unsupported_or_corrupt', start, f'Duplicate or reserved tag {tag:#x}')
            if len(set(values[1:])) != len(values[1:]) and marker == self.definition:
                raise DecodeError('unsupported_or_corrupt', start, 'Duplicate object field names')
            table[tag] = {'name': values[0], 'fields' if marker == self.definition else 'values': values[1:], 'offset': start}
            # Re-encode the schema without copying original bytes.
            encoded = bytes((marker, tag)) + b''.join(x.encode('utf-8') + b'\0' for x in values) + b'\0'
            if encoded != self.b[start:self.pos]:
                raise DecodeError('roundtrip_mismatch', start, 'Schema re-encoding mismatch')
            self.schema_bytes += len(encoded)
            self.roundtrip.update(encoded)
            self.roundtrip_bytes += len(encoded)

    def object(self, depth=0):
        if depth > 32:
            raise DecodeError('unsupported_or_corrupt', self.pos, 'Maximum nesting depth')
        start = self.pos
        tag = self.take(1)[0]
        if tag not in self.schemas:
            raise DecodeError('unsupported_or_corrupt', start, f'Unknown object tag {tag:#x}')
        schema = self.schemas[tag]
        name = schema['name']
        result = {'type': name, 'offset': start, 'fields': {}}
        encoded = bytearray((tag,))
        for field in schema['fields']:
            prefix = field[0]
            if field.startswith('ao'):
                array = []
                # All observed object arrays are the final field and terminate
                # at their owning object's end marker, without another delimiter.
                if field != schema['fields'][-1]:
                    raise DecodeError('unsupported_or_corrupt', self.pos, f'Unsupported nonfinal object array {name}.{field}')
                while True:
                    if self.pos == len(self.b):
                        raise DecodeError('truncated', self.pos, f'Unterminated array {name}.{field}')
                    if self.b[self.pos] == self.end:
                        break
                    child, raw = self.object(depth + 1)
                    array.append(child)
                    encoded.extend(raw)
                value = array
            elif prefix == 'o':
                value, raw = self.object(depth + 1)
                encoded.extend(raw)
            elif prefix in ('i', 'e', 'p', 'l'):
                width = 8 if prefix in ('p', 'l') else 4
                raw = self.take(width)
                value = int.from_bytes(raw, 'little', signed=False)
                encoded.extend(value.to_bytes(width, 'little', signed=False))
                key = f'{name}.{field}'
                if key not in self.pending_numeric:
                    self.pending_numeric[key] = {'count': 1, 'min': value, 'max': value}
                else:
                    stat = self.pending_numeric[key]
                    stat['count'] += 1
                    stat['min'] = min(stat['min'], value)
                    stat['max'] = max(stat['max'], value)
            elif prefix == 's':
                value = self.string()
                encoded.extend(value.encode('utf-8') + b'\0')
            else:
                raise DecodeError('unsupported_or_corrupt', self.pos, f'Unsupported field {name}.{field}')
            result['fields'][field] = value
        if self.take(1)[0] != self.end:
            raise DecodeError('unsupported_or_corrupt', self.pos-1, f'Wrong end marker, object={name}, start={start:#x}')
        encoded.append(self.end)
        result['end_offset'] = self.pos
        if encoded != self.b[start:self.pos]:
            raise DecodeError('roundtrip_mismatch', start, f'Object re-encoding mismatch {name}')
        self.pending_counts[name] += 1
        if len(self.samples[name]) + len(self.pending_samples[name]) < self.sample_limit:
            self.pending_samples[name].append(result)
        return result, encoded

    def run(self):
        while self.pos < len(self.b):
            self.definitions()
            if self.pos == len(self.b):
                break
            self.pending_counts = collections.Counter()
            self.pending_numeric = {}
            self.pending_samples = collections.defaultdict(list)
            obj, raw = self.object()
            self.top_counts[obj['type']] += 1
            self.roundtrip.update(raw)
            self.roundtrip_bytes += len(raw)
            self.counts.update(self.pending_counts)
            for key, stat in self.pending_numeric.items():
                if key not in self.numeric:
                    self.numeric[key] = stat
                else:
                    target = self.numeric[key]
                    target['count'] += stat['count']
                    target['min'] = min(target['min'], stat['min'])
                    target['max'] = max(target['max'], stat['max'])
            for key, samples in self.pending_samples.items():
                self.samples[key].extend(samples)


def decode_file(path, sample_limit=2):
    started = time.monotonic()
    with path.open('rb') as f:
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if path.stat().st_size else b''
        reader, failure = None, None
        try:
            reader = Reader(data, sample_limit)
            reader.run()
            if not reader.top_counts:
                raise DecodeError('unsupported_or_corrupt', reader.pos, 'No complete event records')
        except DecodeError as exc:
            failure = {'code': exc.code, 'offset': exc.offset, 'offset_hex': hex(exc.offset), 'message': str(exc)}
        digest = hashlib.sha256(data).hexdigest()
        validated = reader.roundtrip_bytes if reader else 0
        roundtrip = reader.roundtrip.hexdigest() if reader else hashlib.sha256(b'').hexdigest()
        status = 'complete' if failure is None else 'partial' if reader and reader.top_counts else 'failed'
        next_step = None
        if failure:
            next_step = ('Read references/binlog.md and use REA with the original matching driver to investigate; '
                         'write a validated temporary decoder under report/ if the format differs. '
                         'For truncation, preserve the incomplete tail; do not invent missing bytes or relax checks.')
        result = {
            'schema_version': 1, 'input': str(path.resolve()), 'size': len(data), 'sha256': digest,
            'status': status, 'error': failure, 'rea_required': failure is not None, 'next_step': next_step,
            'consumed_bytes': reader.pos if reader else 0, 'validated_reencoded_bytes': validated,
            'unvalidated_tail_bytes': len(data) - validated, 'roundtrip_sha256': roundtrip,
            'validated_prefix_roundtrip_matches': roundtrip == hashlib.sha256(memoryview(data)[:validated]).hexdigest(),
            'full_roundtrip_matches': failure is None and roundtrip == digest,
            'definition_marker': reader.definition if reader else None,
            'enum_marker': reader.enumeration if reader else None, 'end_marker': reader.end if reader else None,
            'schema_bytes': reader.schema_bytes if reader else 0,
            'object_schemas': reader.schemas if reader else {}, 'enum_schemas': reader.enums if reader else {},
            'object_counts_including_nested': reader.counts if reader else {},
            'top_level_event_counts': reader.top_counts if reader else {},
            'numeric_fields_unsigned_raw': reader.numeric if reader else {}, 'samples': reader.samples if reader else {},
            'elapsed_seconds': round(time.monotonic()-started, 3),
            'statistics_scope': 'complete validated top-level records and their nested objects only',
            'validation_profile': {'driver_build': 'f6b5df5188', 'compiler': 'E031.45.02.26',
                                   'vulkan_adreno_sha256': '979ac22c0d46c26804d559a93ab6c93c66272f224d3a5d0908f94a51247154b0'},
            'limitations': ['Only one driver build independently validated; other versions require verification.',
                            'Integer and enum values preserved as unsigned bit patterns; signed semantics may differ.',
                            'Timestamp units and counter interpretations are unknown.',
                            f'At most {sample_limit} examples per object type exported; every complete event parsed and re-encoded.'],
        }
        if isinstance(data, mmap.mmap):
            data.close()
    return result


def capture_context(source, summary=None):
    start = source if source.is_dir() else source.parent
    if summary is None:
        summary = next((p/'summary.json' for p in (start, *start.parents) if (p/'summary.json').is_file()), None)
    if summary is None:
        return {'summary': None, 'device': {}, 'driver_descriptions': [], 'driver_libraries': [], 'binlog_hashes': []}
    data = json.loads(Path(summary).read_text(encoding='utf-8-sig'))
    collection = data.get('collection', {})
    return {'summary': str(Path(summary).resolve()), 'device': data.get('device', {}),
            'driver_descriptions': list(dict.fromkeys(r['driver_description'] for r in collection.get('probe', {}).get('runs', []) if r.get('driver_description'))),
            'driver_libraries': [{k: r.get(k) for k in ('file', 'device_path', 'sha256', 'verified')}
                                 for r in collection.get('driver_libraries', {}).get('files', [])],
            'binlog_hashes': [{'name': str(r.get('file', '')).replace('\\', '/').rsplit('/', 1)[-1], 'sha256': r.get('sha256')}
                              for r in collection.get('integrity', []) if 'qgl_bin_log_' in str(r.get('file', ''))]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input', type=Path, help='QGL binlog file or directory containing qgl_bin_log_*.bin')
    ap.add_argument('--out', required=True, type=Path, help='New or empty directory under the current project report/')
    ap.add_argument('--summary', type=Path, help='Original capture summary.json; otherwise discover in ancestors')
    ap.add_argument('--sample-limit', type=int, default=2)
    args = ap.parse_args()
    source, out = args.input.resolve(), args.out.resolve()
    if not source.exists():
        ap.error('Input does not exist')
    if not out.is_relative_to((Path.cwd()/'report').resolve()):
        ap.error('--out must be inside the current project report/')
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        ap.error('--out must be a new or empty directory')
    if args.sample_limit < 0:
        ap.error('--sample-limit must be nonnegative')
    paths = [source] if source.is_file() else sorted(p for p in source.rglob('qgl_bin_log_*.bin') if p.is_file())
    if not paths:
        ap.error('No qgl_bin_log_*.bin files found')
    context = capture_context(source, args.summary)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, path in enumerate(paths):
        result = decode_file(path, args.sample_limit)
        expected = [r['sha256'] for r in context['binlog_hashes'] if r['name'] == path.name and r['sha256']]
        result['capture_hash_match'] = result['sha256'] in expected if expected else None
        filename = f'{i:03d}-{path.stem}.json'
        (out/filename).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        rows.append({k: result[k] for k in ('input', 'status', 'error', 'rea_required', 'sha256', 'capture_hash_match',
                                          'size', 'validated_reencoded_bytes', 'unvalidated_tail_bytes', 'full_roundtrip_matches')})
        rows[-1].update(json=filename, top_level_events=sum(result['top_level_event_counts'].values()))
    summary = {'schema_version': 1, 'capture_context': context, 'files': rows,
               'rea_required': any(r['rea_required'] for r in rows),
               'next_step': 'For every incomplete or failed file, follow references/binlog.md; use the original driver with REA and keep temporary adapters under report/.'}
    (out/'binlog-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    esc = html.escape
    body = '<h1>Adreno binlog 解码</h1><p><a href="binlog-summary.json">汇总 JSON</a></p><p>只统计已验证完整事件；JSON 默认仅导出每类两个样本。原始整数、时间戳与驱动累计值不直接解释为 GPU 耗时或硬件 counter。</p>'
    body += '<pre>'+esc(json.dumps(context, ensure_ascii=False, indent=2))+'</pre>'
    body += '<table><tr><th>输入</th><th>状态</th><th>完整顶层事件</th><th>验证前缀 / 总字节</th><th>采集哈希关联</th><th>诊断</th></tr>'
    for row in rows:
        body += f'<tr><td><a href="{quote(row["json"])}">{esc(row["input"])}</a></td><td>{row["status"]}</td><td>{row["top_level_events"]}</td><td>{row["validated_reencoded_bytes"]} / {row["size"]}</td><td>{json.dumps(row["capture_hash_match"])}</td><td>{esc(json.dumps(row["error"], ensure_ascii=False))}</td></tr>'
    body += '</table><p>采集哈希关联 false 时不要使用这份元数据判断手机或驱动；null 表示关联尚未验证。</p>'
    if summary['rea_required']:
        body += '<p><strong>需要 REA 调查：</strong>保留失败偏移和尾部，按 skill 的 references/binlog.md 用原采集驱动定位写入代码；格式改变时在 report/ 编写临时解码器。截断文件不能靠放宽校验补全。</p>'
    (out/'index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Adreno binlog</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:32px auto;padding:0 20px}pre{white-space:pre-wrap}td,pre{overflow-wrap:anywhere}table{border-collapse:collapse}td,th{border:1px solid #ccd;padding:9px;text-align:left}</style>'+body+'</html>', encoding='utf-8')
    code = 1 if any(r['status'] == 'failed' or (r['error'] and r['error']['code'] != 'truncated') for r in rows) else 2 if summary['rea_required'] else 0
    print(json.dumps({'report': str(out/'index.html'), 'files': len(rows), 'rea_required': summary['rea_required'], 'exit_code': code}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
