# Outage RCA Agent — Multi-Agent DAG Orchestration

A DAG-based agent that does **root-cause analysis for multi-service outages**. You hand it a batch of alerts — several services degraded at the same time, each with a different symptom — and it investigates every service **in parallel**, computes the intersection of candidate causes **in a real Python sandbox**, and tells you whether you are looking at *one incident or N separate ones*, plus the single remediation that brings everything back.

Built on the EAG Session 8 architecture: a NetworkX `DiGraph` **is** the agent loop. An LLM Planner writes the graph; an Executor walks it; independent nodes run concurrently; every node's state is persisted to disk so a killed run resumes where it left off.

```
ALERTS ──► planner ──► researcher(checkout) ──┐
                  ├──► researcher(search)   ──┼──► coder ──► formatter ──► ANSWER
                  └──► researcher(auth)     ──┘      └─────► sandbox_executor
                      (all three run in parallel)            (verifies the math)
```

---

## How the DAG works, step by step

**1. The query becomes a graph, not a loop.**
The run starts with a single `planner` node. The Planner is an LLM call whose *output is a program*: a JSON list of nodes (`skill`, `inputs`, `metadata`) that the Executor validates against a Pydantic `NodeSpec` and splices into the graph. For a 3-service outage it emits three `researcher` nodes (one per service, each scoped to *its* service via `metadata.question`), a `coder` that reads all three, and a terminal `formatter`.

**2. The Executor walks the graph; parallelism is free.**
The loop is four lines of logic: find every `pending` node whose predecessors are all `complete`/`skipped` → run them **concurrently** with `asyncio.gather` → splice in any successors their results emit → persist. Nobody schedules the three researchers "in parallel" explicitly — they simply have no dependency on each other, so they become ready in the same wave. The layer's wall-clock cost is the **slowest branch, not the sum** (measured below: 28.7s vs 64.3s).

**3. Skills are data, not code.**
Every node runs through one dispatcher. A skill is just a yaml entry (`agent_config.yaml`) + a prompt file + an allowed-tools list + a temperature. The Researcher may call `web_search`/`fetch_url` (multi-turn MCP tool loop), the Critic calls `validate_service_record`, the Formatter calls nothing. Adding a skill = one yaml block + one `.md` file — the orchestrator never changes.

**4. Investigation branches produce structured evidence.**
Each investigator takes one alert ("checkout-service: HTTP 503s; depends on PostgreSQL and Redis") and returns `candidate_causes` — snake_case labels grounded in what it found. Two interchangeable investigator skills exist, and the Planner picks per query: `researcher` (web research on the failure mode) and `telemetry_investigator`, which reads the platform's **own telemetry** — `sandbox/telemetry/<service>/{app.log, metrics.json, status.md}` — exactly as a production deployment would read real log/metric stores. Telemetry mode is the stronger demonstration: causes come from actual log lines with onset timestamps, and dependencies whose health probes are green get *excluded* even if that failure mode is common in general. Swapping evidence sources changed `tools_allowed` and a prompt — the graph and Executor stayed identical.

**5. The correlation is computed, not vibed.**
The `coder` node embeds the three cause-lists as literals in a small Python script: count how many services list each cause → top common cause → `confidence = appearances / services` → verdict (`single root cause` vs `multiple independent incidents`). The orchestrator auto-attaches a `sandbox_executor` (subprocess, scrubbed env, 30s timeout) that actually runs it. The formatter quotes the coder; the sandbox stdout is the persisted ground truth. In our demo run the two disagreed on a tie-break — and the sandbox was right. That's the point of the diamond.

**6. A Critic gates anything that must be schema-correct.**
When the user demands a *validated* incident record, the Planner inserts a `critic` between the distiller and the formatter. The Critic doesn't eyeball the schema — it calls a deterministic MCP tool, `validate_service_record(json_str)`, and converts the tool's verdict to pass/fail. On **fail**, the Executor marks the blocked child `skipped` and splices in **one recovery Planner** carrying the failure rationale (a per-target cap stops loops). The recovery plan produces a corrected answer.

**7. Everything is persisted; any run resumes.**
Each session writes `code/state/sessions/<sid>/`: `query.txt`, `graph.json` (the whole DiGraph via `nx.node_link_data`), and one `NodeState` JSON per node (status, inputs, result, the exact prompt sent). All writes are atomic (tmp file + `os.replace`). `python3 flow.py --resume <sid>` reloads the graph, resets `running → pending`, and continues — completed nodes are never re-run.

**8. Failures are classified before anyone reacts.**
`recovery.classify_failure` buckets an error as `transient` (gateway 5xx — already retried, skip), `validation_error` (malformed plan — a prompt bug, skip), or `upstream_failure` (queue one recovery Planner). 22 unit tests pin this behaviour. We watched it work live: a machine with a missing dependency produced a storm of upstream failures that the `MAX_NODES=60` cap cut off, while genuine 503s inside the same storm were correctly skipped as transient.

---

## Skill catalogue

| skill | tools | temp | job |
|---|---|---|---|
| planner | — | 0.4 | writes/extends the graph as JSON |
| researcher | web_search, fetch_url | 0.7 | investigates one service's failure mode (web) |
| **telemetry_investigator** | list_dir, read_file, web_search | 0.3 | **new skill** — investigates one service from the platform's own logs/metrics/status probes |
| retriever | search_knowledge | 0.2 | searches the indexed knowledge base |
| distiller | — | 0.1 | extracts the canonical incident record |
| critic | **validate_service_record** | 0.0 | tool-grounded pass/fail gate |
| coder | — | 0.2 | emits the correlation script `{code, summary}` |
| sandbox_executor | (subprocess) | — | runs the code; stdout = ground truth |
| **remediation_advisor** | — | 0.3 | **new skill** — turns the root cause into an ordered fix |
| summariser / formatter | — | 0.3 | condense / render the final answer |

The canonical incident record every branch converges on:

```json
{"service": "checkout-service", "symptom": "HTTP 503",
 "dependencies": ["PostgreSQL", "Redis"],
 "candidate_causes": ["postgres_unavailable", "redis_unavailable", "connection_pool_exhausted"]}
```

---

## Usage

### Setup (once)

```bash
cd code
pip install -r requirements.txt          # mcp, ddgs, crawl4ai, streamlit, …
python3 -m playwright install chromium   # browser for fetch_url
cp ../.env.example ../.env               # then fill in GEMINI_API_KEY (minimum)
```

The V8 gateway (port 8108) auto-starts on first run; or start it yourself: `cd gateway && python3 main.py`.

### Run from the UI (Streamlit)

```bash
cd code
streamlit run streamlit_app.py
```

Paste your alerts (one per line, `service: symptom; depends on A and B`), pick the evidence source — **platform telemetry** (reads the synthetic incident under `sandbox/telemetry/`) or **web research** — hit **Run RCA**, and watch the nodes complete live. The app then renders the DAG, the per-node latency table with the max-vs-sum proof, critic verdicts, the coder's script + sandbox stdout, and the final answer. It can also re-open any past session.

### Run from the CLI

Telemetry-grounded RCA (reads the synthetic incident under `sandbox/telemetry/`):

```bash
python3 flow.py 'Three services in our platform are degraded right now. Investigate each using our local platform telemetry under telemetry/<service-name>/ (app.log, metrics.json, status.md) and tell me whether they share a single root cause or are separate incidents:
 - checkout-service: returning HTTP 503s; depends on PostgreSQL and Redis
 - search-service:   requests timing out; depends on Elasticsearch and PostgreSQL
 - auth-service:     intermittent HTTP 500s; depends on PostgreSQL and an LDAP server
Use web search only to interpret unfamiliar errors.'
```

Web-research RCA (same graph shape; the Planner picks `researcher` branches instead):

```bash
python3 flow.py 'Three services in our platform are degraded right now. Investigate each and tell me whether they share a single root cause or are separate incidents:
 - checkout-service: returning HTTP 503s; depends on PostgreSQL and Redis
 - search-service:   requests timing out; depends on Elasticsearch and PostgreSQL
 - auth-service:     intermittent HTTP 500s; depends on PostgreSQL and an LDAP server'
```

```bash
python3 flow.py --resume <sid>        # continue a killed run
python3 inspect_run.py <sid>          # DAG + latency table + verdicts + sandbox stdout
python3 replay.py <sid>               # step through a trace node by node
python3 -m pytest tests/test_recovery.py   # 22 tests pin the recovery policy
curl "localhost:8108/v1/cost/by_agent?session=<sid>"   # per-skill token spend
```

Other things it handles (assignment base queries): `Say hello.` (2-node minimum DAG) · the Shannon Wikipedia extraction · the London/Paris/Berlin populations comparison · a graceful failure on a nonexistent file · kill-and-resume mid-run.

---

## Measured results (from the traces in `code/state/sessions/`)

**Telemetry-grounded RCA (the flagship run)** — `s8-71958b0a`, three `telemetry_investigator` branches reading the synthetic incident under `sandbox/telemetry/`:

```
checkout  onset 09:14:02Z  causes [postgresql_unavailable, connection_pool_exhausted]
search    onset 09:14:11Z  causes [postgresql_unavailable]          (ES probes green → excluded)
auth      onset 09:14:15Z  causes [postgresql_unavailable]          (LDAP bind 11ms → excluded)
sandbox:  {"top_common_cause": "postgresql_unavailable", "confidence": 1.0,
           "verdict": "single root cause"}
parallel layer: sum 60.3s → max 24.6s · whole run 36.4s end-to-end
```

The onset timestamps line up within 13 seconds — the cascade is visible in the evidence itself, and every cause traces to a specific log line persisted in the investigator's NodeState.

**Parallel fan-out = max, not sum (web-research mode)** — outage run `s8-103329bb`, planner + all three researchers in one uninterrupted window:

```
n:1  planner                start  0.00s   elapsed  6.18s
n:3  researcher (search)    start  6.38s   elapsed 28.69s ┐
n:2  researcher (checkout)  start 13.53s   elapsed 21.54s ├ all finish 35.08s
n:4  researcher (auth)      start 21.01s   elapsed 14.07s ┘ (gather barrier)
sum-of-branches 64.31s  →  layer wall-clock 28.69s (the slowest branch)
```

**Sandbox-grounded verdict** — same run, `sandbox_executor` stdout (exit 0):

```json
{"top_common_cause": "postgresql_unavailable", "confidence": 1.0,
 "total_services": 3, "verdict": "single root cause"}
```

**Critic both ways** — `s8-42c2d3c8`: well-specified checkout record → tool says valid → `pass`. `s8-9a674f74`: under-specified payments record → `fail` → formatter `skipped`, recovery planner spliced (`recovers: n:2, recovery_reason: critic_fail`) → corrected final answer. Both critics show 2 gateway calls each (the tool round-trip) in the cost ledger.

**New skill, zero orchestrator changes** — `s8-78ef88d2`: `planner → remediation_advisor → formatter`, a 5-step PostgreSQL failover plan covering all three services.

**Token telemetry** — outage run: 10 calls, 13.8k in / 2.0k out, **router_calls = 0** (every skill pinned via `agent_routing.yaml`).

Full logs for every run: `code/logs/*.log`.

---

## What was changed vs. the shared starting code

```
prompts/coder.md                rewritten (was the stub)
prompts/critic.md               rewritten — verdicts grounded in the tool
prompts/remediation_advisor.md  NEW skill prompt
prompts/telemetry_investigator.md  NEW skill prompt (+ skills.py entries for
                                the existing list_dir/read_file MCP tools;
                                synthetic incident data in sandbox/telemetry/)
prompts/planner.md              skill-list line + incident decomposition guidance
prompts/researcher.md           candidate_causes guidance for failure investigations
prompts/distiller.md            canonical incident-record schema
agent_config.yaml               critic tools_allowed + remediation_advisor entry
mcp_server.py                   ONE appended tool: validate_service_record
skills.py                       its _TOOL_CATALOG entry
gateway/agent_routing.yaml      remediation_advisor → gemini pin
streamlit_app.py / inspect_run.py   NEW, read-only tooling (UI + trace viewer)
```

`flow.py` (Executor), `recovery.py`, `persistence.py`, `schemas.py`, `sandbox.py` and all Session-7 modules are untouched. Recovery tests: **22/22 green**.

## Design notes & limitations

The sandbox is a usability boundary, not a security one (env allowlist, temp cwd, 30s timeout — no network/filesystem isolation). Resume is at node granularity: a researcher killed mid-tool-loop re-runs from its top. The Critic is only as good as what it can verify — that is exactly why it was given a deterministic validation tool instead of being asked to eyeball schemas. Researchers run at temperature 0.7 against the live web, so cause lists vary run-to-run; the correlation stays stable because it is computed in the sandbox from whatever the branches actually returned.
