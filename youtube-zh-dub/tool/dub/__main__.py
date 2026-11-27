"""python -m dub <command>

  doctor                                   environment preflight
  run [SOURCE] [--id ID] [--cookies F] [--formats hard,soft] [--from STAGE] [--set k.sub=v ...]
                                           run / resume until done or a gate (exit 0/10/20/30/1)
  status --id ID                           stage states, gate, QA summary, outputs (JSON)
  validate --id ID --chunk N               validate one translation chunk
  preview --id ID [--minutes 3]            short preview cut of the first minutes
  clean --id ID [--yes]                    list intermediates; --yes moves them to trash/ (no deletion)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config  # noqa: E402
import pipeline  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dub", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("doctor")
    r = sp.add_parser("run")
    r.add_argument("source", nargs="?", default=None, help="YouTube URL or local video file")
    r.add_argument("--id", default=None)
    r.add_argument("--cookies", default=None, help="Netscape cookies.txt for YouTube (not stored)")
    r.add_argument("--formats", default=None, help="comma list: hard,soft (default from dub.toml)")
    r.add_argument("--from", dest="from_stage", choices=pipeline.STAGES, default=None)
    r.add_argument("--asr-prompt", default=None)
    r.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    st = sp.add_parser("status")
    st.add_argument("--id", required=True)
    v = sp.add_parser("validate")
    v.add_argument("--id", required=True)
    v.add_argument("--chunk", type=int, required=True)
    pv = sp.add_parser("preview")
    pv.add_argument("--id", required=True)
    pv.add_argument("--minutes", type=float, default=3.0)
    cl = sp.add_parser("clean")
    cl.add_argument("--id", required=True)
    cl.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "doctor":
        import doctor
        return doctor.run_doctor()
    if a.cmd == "run":
        formats = [x.strip() for x in a.formats.split(",") if x.strip()] if a.formats else None
        return pipeline.run(a.source, a.id, a.cookies, formats, a.from_stage,
                            config.parse_overrides(a.overrides), a.asr_prompt)
    if a.cmd == "status":
        print(json.dumps(pipeline.status(a.id), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "validate":
        import s5_translate
        from common import session_dir
        res = s5_translate.validate(session_dir(a.id), a.chunk)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["pass"] else 1
    if a.cmd == "preview":
        return pipeline.preview(a.id, a.minutes)
    if a.cmd == "clean":
        return pipeline.clean(a.id, a.yes)
    return 1


if __name__ == "__main__":
    sys.exit(main())
