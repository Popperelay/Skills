"""Validate one translation chunk:  python dub/validate_chunk.py <session_id> <NN>
Prints missing/extra keys and lines whose estimated spoken length exceeds budget; last line PASS/FAIL."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import session_dir  # noqa: E402
from s5_translate import validate  # noqa: E402


def main() -> int:
    sid, nn = sys.argv[1], int(sys.argv[2])
    r = validate(session_dir(sid), nn)
    if r["status"] in ("missing", "invalid_json"):
        print(f"FAIL: {r['status']} {r.get('error', '')} ({r['out']})")
        return 1
    n = r["lines"]
    print(f"lines={n} missing={r['missing']} extra={r['extra']} empty={r['empty']}")
    print(f"over_budget={len(r['over_budget'])} ({len(r['over_budget'])/n:.0%}); "
          f"over_by_>15%={len(r['over_by_too_much'])}")
    for i, u, b in r["over_budget"]:
        print(f"  i={i} units={u} budget={b}")
    print("PASS" if r["pass"] else "FAIL")
    return 0 if r["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
