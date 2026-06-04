"""Streamlit wrapper for the Session 8 DAG agent (outage RCA).

Paste a batch of service alerts, run the agent, watch nodes complete
live, then inspect the DAG, the parallel-layer latency proof, critic
verdicts, the coder's script + sandbox stdout, and the final answer.

Read-only with respect to the architecture: this file shells out to
`flow.py` exactly like the CLI does and then reads the persisted
session directory. The orchestrator is untouched.

Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import glob
import json
import re
import subprocess
import sys
from pathlib import Path

import streamlit as st

CODE_DIR = Path(__file__).parent
SESSIONS = CODE_DIR / "state" / "sessions"

DEFAULT_ALERTS = (
    "checkout-service: returning HTTP 503s; depends on PostgreSQL and Redis\n"
    "search-service: requests timing out; depends on Elasticsearch and PostgreSQL\n"
    "auth-service: intermittent HTTP 500s; depends on PostgreSQL and an LDAP server"
)

st.set_page_config(page_title="Outage RCA Agent", page_icon="🛠", layout="wide")


# ── session-trace helpers (mirror inspect_run.py) ───────────────────────────

def load_session(sid: str) -> dict:
    d = SESSIONS / sid
    out: dict = {"sid": sid, "query": "", "graph": None, "nodes": []}
    qp = d / "query.txt"
    gp = d / "graph.json"
    if qp.exists():
        out["query"] = qp.read_text().strip()
    if gp.exists():
        out["graph"] = json.loads(gp.read_text())
    for p in sorted(glob.glob(str(d / "nodes" / "n_*.json"))):
        try:
            out["nodes"].append(json.loads(Path(p).read_text()))
        except (OSError, ValueError):
            continue
    return out


def render_session(sess: dict) -> None:
    nodes = [n for n in sess["nodes"] if n.get("started_at") and n.get("completed_at")]

    # Final answer first — that's what an on-call engineer wants.
    for n in reversed(nodes):
        o = (n.get("result") or {}).get("output") or {}
        if n["skill"] == "formatter" and o.get("final_answer"):
            st.success(o["final_answer"])
            break

    if sess["graph"]:
        st.subheader("DAG")
        rows = []
        for n in sess["graph"]["nodes"]:
            md = n.get("metadata") or {}
            rows.append({
                "node": n["id"], "skill": n.get("skill", ""),
                "status": n.get("status", ""), "label": md.get("label", ""),
                "recovers": md.get("recovers", ""),
                "question": (md.get("question") or "")[:80],
            })
        st.dataframe(rows, use_container_width=True)
        edges = ", ".join(f"{e['source']}→{e['target']}" for e in sess["graph"]["edges"])
        st.caption(f"edges: {edges}")

    if nodes:
        st.subheader("Per-node latency")
        t0 = min(n["started_at"] for n in nodes)
        rows = [{
            "node": n["node_id"], "skill": n["skill"],
            "start (rel s)": round(n["started_at"] - t0, 2),
            "elapsed (s)": round(n["completed_at"] - n["started_at"], 2),
            "finish (rel s)": round(n["completed_at"] - t0, 2),
        } for n in sorted(nodes, key=lambda x: x["started_at"])]
        st.dataframe(rows, use_container_width=True)

        par = [n for n in nodes if n["skill"] == "researcher"]
        if len(par) > 1:
            els = [n["completed_at"] - n["started_at"] for n in par]
            wall = max(n["completed_at"] for n in par) - min(n["started_at"] for n in par)
            c1, c2, c3 = st.columns(3)
            c1.metric("sum of branches", f"{sum(els):.1f}s")
            c2.metric("slowest branch", f"{max(els):.1f}s")
            c3.metric("parallel layer wall-clock", f"{wall:.1f}s",
                      help="≈ slowest branch, not the sum — the asyncio.gather barrier")

    for n in nodes:
        o = (n.get("result") or {}).get("output") or {}
        if n["skill"] == "critic":
            v = o.get("verdict", "?")
            (st.error if v == "fail" else st.info)(
                f"Critic {n['node_id']}: **{v}** — {o.get('rationale', '')}")
        if n["skill"] == "coder" and o.get("code"):
            with st.expander(f"Coder {n['node_id']} — emitted Python"):
                st.code(o["code"], language="python")
        if n["skill"] == "sandbox_executor":
            with st.expander(f"Sandbox {n['node_id']} — ground truth "
                             f"(exit {o.get('exit_code')})", expanded=True):
                st.code((o.get("stdout") or "").strip() or "(no stdout)")
        if n["skill"] == "remediation_advisor" and o.get("remediation_steps"):
            with st.expander(f"Remediation plan {n['node_id']}", expanded=True):
                st.json(o)


# ── live run ─────────────────────────────────────────────────────────────────

def run_flow(query: str) -> str | None:
    """Shell out to flow.py, stream its stdout into the page, return sid."""
    box = st.empty()
    lines: list[str] = []
    sid: str | None = None
    proc = subprocess.Popen(
        [sys.executable, "-u", "flow.py", query],
        cwd=str(CODE_DIR), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip())
        if sid is None:
            m = re.search(r"session (s8-[0-9a-f]+)", line)
            if m:
                sid = m.group(1)
        box.code("\n".join(lines[-25:]) or "starting …")
    proc.wait()
    box.code("\n".join(lines[-25:]))
    return sid


# ── UI ───────────────────────────────────────────────────────────────────────

st.title("🛠 Outage RCA Agent")
st.caption("Multi-agent DAG orchestration — parallel per-service investigation, "
           "sandbox-verified correlation, single root cause or separate incidents.")

mode = st.sidebar.radio("Mode", ["Batch of alerts (RCA)", "Free-form query", "Inspect a past run"])
st.sidebar.caption("Gateway V8 auto-starts on the first run (a one-time ~15s wait). "
                   "Needs `.env` with GEMINI_API_KEY.")

if mode == "Batch of alerts (RCA)":
    st.markdown("One alert per line — `service: symptom; depends on A and B`")
    alerts = st.text_area("Degraded services", DEFAULT_ALERTS, height=120)
    ask_remedy = st.checkbox("Also ask for a remediation recommendation", value=False)
    if st.button("Run RCA", type="primary"):
        lines = [a.strip() for a in alerts.splitlines() if a.strip()]
        if len(lines) < 2:
            st.warning("Give at least two alerts — correlation needs siblings.")
        else:
            n = len(lines)
            query = (
                f"{n} services in our platform are degraded right now. Investigate each "
                f"and tell me whether they share a single root cause or are separate incidents:\n"
                + "\n".join(f" - {a}" for a in lines)
            )
            if ask_remedy:
                query += ("\nIf there is a single shared root cause, also recommend the "
                          "remediation that resolves all affected services.")
            with st.spinner("Planner is writing the graph …"):
                sid = run_flow(query)
            if sid:
                st.session_state["last_sid"] = sid
                render_session(load_session(sid))

elif mode == "Free-form query":
    q = st.text_area("Query", "Find the populations of London, Paris, Berlin "
                              "and tell me which two are closest in size.", height=90)
    if st.button("Run", type="primary") and q.strip():
        with st.spinner("Running the DAG …"):
            sid = run_flow(q.strip())
        if sid:
            st.session_state["last_sid"] = sid
            render_session(load_session(sid))

else:
    sids = sorted((p.name for p in SESSIONS.iterdir() if p.is_dir()), reverse=True) \
        if SESSIONS.exists() else []
    if not sids:
        st.info("No saved sessions yet — run something first.")
    else:
        default = st.session_state.get("last_sid")
        idx = sids.index(default) if default in sids else 0
        sid = st.selectbox("Session", sids, index=idx)
        sess = load_session(sid)
        st.markdown(f"**Query:** {sess['query']}")
        render_session(sess)
