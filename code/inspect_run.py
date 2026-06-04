"""Read-only trace inspector for a persisted Session 8 run.

Usage:  python3 inspect_run.py <session_id>

Prints: the DAG (nodes + status + labels), the per-node latency table
(start/elapsed/finish, parallel-layer max-vs-sum), every critic verdict,
the coder's code + sandbox stdout when present, and the final answer.
Touches nothing; reads state/sessions/<sid>/ only.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent / "state" / "sessions"


def main() -> None:
    if len(sys.argv) < 2:
        sids = sorted(p.name for p in ROOT.iterdir() if p.is_dir())
        print("usage: python3 inspect_run.py <session_id>\navailable:", *sids, sep="\n  ")
        return
    sid = sys.argv[1]
    d = ROOT / sid
    g = json.loads((d / "graph.json").read_text())
    print(f"query: {(d / 'query.txt').read_text().strip()}\n")

    print("DAG:")
    for n in g["nodes"]:
        md = n.get("metadata") or {}
        tags = {k: v for k, v in md.items() if k in ("label", "recovers", "recovery_reason")}
        print(f"  {n['id']:6s} {n.get('skill', '?'):20s} {n.get('status', '?'):9s} {tags or ''}")
    print("edges:", ", ".join(f"{e['source']}→{e['target']}" for e in g["edges"]))

    states = []
    for p in sorted(glob.glob(str(d / "nodes" / "n_*.json"))):
        s = json.loads(Path(p).read_text())
        if s.get("started_at") and s.get("completed_at"):
            states.append(s)
    if states:
        t0 = min(s["started_at"] for s in states)
        print(f"\n{'node':6s} {'skill':20s} {'start(rel)':>10s} {'elapsed':>9s} {'finish(rel)':>11s}")
        for s in sorted(states, key=lambda x: x["started_at"]):
            st, ct = s["started_at"], s["completed_at"]
            print(f"{s['node_id']:6s} {s['skill']:20s} {st-t0:9.2f}s {ct-st:8.2f}s {ct-t0:10.2f}s")
        par = [s for s in states if s["skill"] == "researcher"]
        if len(par) > 1:
            els = [s["completed_at"] - s["started_at"] for s in par]
            wall = max(s["completed_at"] for s in par) - min(s["started_at"] for s in par)
            print(f"\nparallel layer: sum-of-elapsed={sum(els):.2f}s  "
                  f"max-branch={max(els):.2f}s  layer-wall-clock={wall:.2f}s  ← max, not sum")

    for s in states:
        out = (s.get("result") or {}).get("output") or {}
        if s["skill"] == "critic":
            print(f"\n[{s['node_id']}] CRITIC verdict: {json.dumps(out)}")
        if s["skill"] == "coder" and out.get("code"):
            print(f"\n[{s['node_id']}] CODER code:\n{out['code']}")
        if s["skill"] == "sandbox_executor":
            print(f"\n[{s['node_id']}] SANDBOX exit={out.get('exit_code')} stdout:\n{out.get('stdout', '').strip()}")

    for s in reversed(states):
        out = (s.get("result") or {}).get("output") or {}
        if s["skill"] == "formatter" and out.get("final_answer"):
            print(f"\nFINAL ({s['node_id']}): {out['final_answer']}")
            break


if __name__ == "__main__":
    main()
