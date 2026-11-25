"""Lossless metric extraction alongside the original driver text."""
import hashlib
import json
from pathlib import Path
import re


def statistics(text):
    metrics, headings, unparsed = [], [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        while headings and headings[-1][0] >= indent:
            headings.pop()
        match = re.match(r"\s*-?\s*(.+?)\s*:\s*(-?\d+(?:\.\d+)?)(?:,\s*(-?\d+)\s*\(rpt\)(?:,\s*ratio\s+([\d.]+))?)?\s*$", line)
        if match:
            label, value, expanded, ratio = match.groups()
            metrics.append({"path": [v for _, v in headings] + [label.strip()],
                            "name": label.strip(), "value": float(value) if '.' in value else int(value),
                            "rpt_expanded": int(expanded) if expanded is not None else None,
                            "ratio": float(ratio) if ratio is not None else None,
                            "raw": line})
        elif stripped.startswith('-') and ':' not in stripped:
            headings.append((indent, stripped.lstrip('- ').strip()))
        else:
            unparsed.append(line)
    return {"metrics": metrics, "unparsed_lines": unparsed}


def parse_logs(source, out):
    source, out = Path(source), Path(out)
    paths = [source] if source.is_file() else sorted(source.rglob('*.log'))
    rows, audit = [], []
    (out / 'shaders').mkdir(exist_ok=True)
    for path in paths:
        text = path.read_text(encoding='utf-8', errors='replace')
        headers = list(re.finditer(r'^=+\s*CRC (.+?)\s*=+\s*$', text, re.M))
        entry = {"source_file": str(path.resolve()), "crc_sections": len(headers), "recognized": 0, "skipped": []}
        for pos, match in enumerate(headers):
            chunk = text[match.end():headers[pos + 1].start() if pos + 1 < len(headers) else len(text)]
            crc = re.search(r'HW\s+Code\s+CRC:\s*(0x[0-9a-fA-F]+)', chunk)
            source_crc = re.search(r'Shader Source CRC:\s*(0x[0-9a-fA-F]+)', chunk)
            hw = re.search(r'^=+\s*HW .+?=+\s*$', chunk, re.M)
            instructions = re.findall(r'^\s*\d+\[[0-9a-fA-F_]+\].*$', chunk[hw.end():], re.M) if hw else []
            if not crc or not hw or not instructions:
                entry['skipped'].append({"stage": match[1], "line": text.count('\n', 0, match.start()) + 1,
                                         "reason": 'missing hardware CRC, HW section or recognized instructions'})
                continue
            tail = chunk[hw.end():]
            start = tail.find('- Instruction stats')
            stats = re.split(r'(?m)^shaderAllocations:', tail[start:] if start >= 0 else '')[0].strip()
            stage = re.sub(r'[^a-zA-Z0-9_-]', '-', match[1].strip())
            name = f'{len(rows):03d}-{stage}-{crc[1]}'
            isa_file, stats_file = f'shaders/{name}.isa.txt', f'shaders/{name}.stats.txt'
            isa = '\n'.join(instructions) + '\n'
            (out / isa_file).write_text(isa, encoding='utf-8')
            (out / stats_file).write_text(stats, encoding='utf-8')
            metric_text = re.split(r"(?m)^(?:glslSymbols|linkResult|metadataAllocation|shaderAllocations):", stats)[0]
            parsed = statistics(metric_text)
            def number(label):
                m = re.search(label + r'\s*:\s*(-?\d+)', stats)
                return int(m[1]) if m else None
            rows.append(dict(stage=match[1], hw_crc=crc[1], source_crc=source_crc[1] if source_crc else None,
                             source_file=str(path.resolve()), origin='probe' if 'probe' in path.parts else 'application',
                             isa_file=isa_file, stats_file=stats_file,
                             full_registers=number('Full Registers'), half_registers=number('Half Registers'),
                             scratch_bytes=number(r'(?:Scratch(?: space| Memory)?(?: Size)?|Private Memory Size)'),
                             max_waves=number('Max num of waves'), wave_size=number('Wave sizes'),
                             unified_registers=number('Unified Regs'), ugpr_registers=number('uGPR Registers'),
                             register_footprint=number('Total footprint'), fiber_count=number('Fiber Count'),
                             instruction_groups=[{"encoded": int(a), "rpt_expanded": int(b)} for a, b in re.findall(r'All Instructions:\s*(\d+),\s*(\d+) \(rpt\)', stats)],
                             statistics=parsed, encoded_instruction_lines=len(instructions),
                             machine_code=[re.search(r'\[([0-9a-fA-F_]+)\]', line)[1] for line in instructions],
                             sha256=hashlib.sha256(isa.encode()).hexdigest(), pass_mapping=None))
            entry['recognized'] += 1
        audit.append(entry)
    (out / 'parse-audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    return rows
