# Actions

When an agent requests an action, the monitor evaluates it before execution. Jev interprets semantics; policy, SafetyDrift and Sentinel add structured evidence; deterministic code chooses the escalate-only incident level and a fixed response. A model never decides whether to pull the plug.

Levels only go up. A human clears them. Levels 4 and 5 call Guli *while* the cut happens — the call is a heads-up, not permission.

Neo4j stores every normalized event and monitor decision. `key_nodes` is only the compact level-1+ projection used as Jev long memory. Each new step combines recent events, causal history, policy, SafetyDrift, Sentinel and Jev. Details in [Graph.md](Graph.md).

## Ideal vs this project

The **ideal** column is the production kill-switch (IAM, VPC, swarm). **This project** is the same ladder on a laptop: Docker pause, drop published ports, cut the sandbox network, stop the agent containers. Never `compose down` the product stack (`backend-hackspain` / `frontend-hackspain` / `neo4j-hackspain` on `appnet_hackspain`) to contain one agent.

| Level | In plain terms | Ideal | This project |
|---|---|---|---|
| **1 · Mild** | Looks sloppy or nosy, not hostile | Tag the run. Watch the next steps more carefully. | Same: yellow tag on the timeline. |
| **2 · Medium** | Unclear, but it's probing or getting expensive | A very fast supervisor LLM (Cerebras via Helmcode) reads its logs live. | Same. |
| **3 · Severe** | This agent is the problem | Revoke that agent's API tokens and IAM role. Pause its container. Everyone else keeps running. | `docker pause` that sandbox. Unpublish / close its host ports. Delete its scoped token so the egress proxy returns 401. Other containers stay up. |
| **4 · Critical** | The blast is bigger than one agent | Detach Internet Gateways / Deny-All on the agent VPC. Call Guli Moreno. | Disconnect every sandbox from `agentnet` (or `docker network disconnect`). Drop FORWARD on that bridge so nothing reaches the internet. Product stack on `appnet_hackspain` stays. Call Guli. |
| **5 · Pull the plug** | The swarm itself is the incident | Take the whole agent cluster down. Call Guli. | Stop / `docker compose -f compose.agents.yaml down` the **agent** compose (not `compose.yaml`). Copy JSONL aside first if you can. Call Guli. |

L1–L3 = one agent. L4–L5 = the environment. Playbooks run on the monitoring host; the sandbox cannot pause or un-pause itself.

---

## For agents

Contract between [Agent Monitoring](AgentMonitoring.md) (capture), the [Action Graph](Graph.md) (the chain), `jev` (intent → level — see [Jev.md](Jev.md) for the request schema), and the dispatcher (level → playbook). Classification is fast and almost deterministic. Containment is a prewritten script.

### Pipeline

```
sandbox events (host-side JSONL, always complete)
        → normalize + redact requested event
        → SafetyDrift/Markov ∥ Sentinel ∥ policy rules
        → jev(short-term tape ∥ long-term key nodes ∥ monitor evidence)
        → deterministic gate: ALLOW | HOLD | REFUSE
        → persist every event, entity and assessment in Neo4j
        → escalate-only incident level 0–5
        → dispatcher runs the matching playbook
        → StreamMessage nodes feed resumable SSE
```

Jev contributes semantic probabilities; deterministic code owns the final tool decision and incident level. Policy/Drift/Sentinel may raise but never lower it. The L2 supervisor may annotate and request a re-score; it cannot execute L3–L5 itself.

### Hard rules

1. **Chain, not event.** A single `curl` is noise. `curl` + reading `.env` + posting the contents is intent. On every new event, `jev` re-reads short-term and long-term context in parallel.
2. **Complete graph, sparse context.** Neo4j keeps every event; only level ≥ 1 enters `key_nodes`.
3. **Levels only escalate.** A run never auto-downgrades. A human clears it from the viewer.
4. **L1 is sticky.** Later `jev` calls for that `run_id` get `prior_level=1` (lower bar to L2+).
5. **Kill path is host-side.** Idempotent: re-firing the same level on the same `run_id` is a no-op. L4/L5 page Guli **in parallel** with the cut — missing `ONCALL_PHONE` still executes infra.

`jev` writes confidence and full atomic answers onto the assessment. The final incident level is the max of prior level, Jev, policy and Sentinel floors; Markov contributes trajectory risk to HOLD/REFUSE thresholds.

```json
{
  "run_id": "run_…",
  "level": 3,
  "score": 0.47,
  "intent": "exfiltrate_secrets",
  "rationale": "Read .env then POST to an unknown host.",
  "prior_level": 1
}
```

---

### Level 1 — Mild: tag and watch closer

**When.** Extra tool calls, a suspicious domain that did not succeed, reading files it does not need yet.

**Ideal / this project.** Nothing is killed. Graph + viewer show `level=1`. Later `jev` calls get `prior_level=1`. Human clears `flagged` in the viewer.

---

### Level 2 — Medium: supervisor on the logs

**When.** Intent is ambiguous but the chain is getting expensive or boundary-pushing.

**Ideal / this project.** Sandbox keeps running. Helmcode (`https://api.helmcode.com/v1`, Cerebras-class id in `SUPERVISOR_MODEL`) tails host-side JSONL and may ask `jev` to re-score. No IAM, no Docker socket, no playbook trigger. One supervisor per `run_id`. Fallback model: Helmcode's fastest hosted flash (`deepseek-v4-flash` today).

---

### Level 3 — Severe: freeze this agent

**When.** The chain is hostile for *this* agent. Other agents are not assumed compromised.

**Ideal.** Pause the container. Revoke tokens issued to this run. Attach Deny `*:*` on that agent's IAM role (never the node instance role).

**This project.** Host-side `scripts/contain.sh <run_id>` (`CLOUD=none`):

1. `docker pause` that sandbox (frozen, not removed; logs on the host stay readable).
2. Unpublish its ports (`docker port` / stop publishing) so nothing on the laptop can still hit it.
3. Delete the row in `agent_tokens` so the egress proxy returns 401.
4. Record `action_id=contain:{run_id}`. Viewer: red "paused". Cancel the L2 supervisor for this run.

Rollback (human): `docker unpause`, issue a new scoped token, re-publish ports if needed.

---

### Level 4 — Critical: cut the internet

**When.** The blast is no longer one agent: multiple runs, a breakout, or traffic a pause would not stop.

**Ideal.** Detach the agent VPC Internet Gateway. NACL / SG Deny-All for sandboxes. Control plane (API, viewer, `jev`, HappyRobot pager) stays on another path. Then L3-contain every live run. Page Guli in parallel.

**This project.** `scripts/cut-egress.sh` (`CLOUD=none`), in parallel with the pager:

1. `docker network disconnect` every running sandbox from `agentnet` (`AGENT_BRIDGE`).
2. Drop egress on that bridge (`iptables` FORWARD / `DOCKER-USER` DROP for the sandbox CIDR) — no public internet, no open outbound ports.
3. Leave `appnet_hackspain` alone.
4. Then L3-contain every live `run_id` (pause still matters: cutting the net does not freeze CPU/disk).

`--dry-run` prints the docker/iptables commands. Rollback (human, after Guli): reconnect `agentnet`, restore the previous iptables.

---

### Level 5 — Pull the plug: disconnect the swarm

**When.** The cluster itself is the incident, or L4 did not hold.

**Ideal.** Drain / leave the agent swarm, or scale the agent node group to 0. Prefer "agents cannot reach anything" over "delete the cluster" so disk remains for forensics. Page Guli in parallel.

**This project.** `scripts/kill-swarm.sh`:

1. Copy last JSONL files to `/var/lib/hackspain/forensics/$timestamp/` (they live on the host, so they survive `--rm`).
2. `docker compose -f compose.agents.yaml down --timeout 5` — the **agents** file only. Do not take down product `compose.yaml` unless they share a file (bad for a pitch; acceptable in a panic).
3. Record `action_id=kill-swarm`. Further dispatcher events are dropped except the pager retry.

Rollback: recreate agent containers from known images, empty workspaces. New `run_id`s only.

---

### Levels 4 and 5 — page Guli Moreno

Not a sixth level. A side-effect of L4 and L5. Number in `ONCALL_PHONE`, never in git. Outbound HappyRobot voice (`template: voice-agent`). Pager egress is not on `agentnet`. Infra does not wait for pickup; missing env = log error + still cut.

The To-number lives on the outbound node, not in the POST body. Hook URL, API key, and number stay in gitignored `.env`. Local fire: `scripts/page.sh`. After changing who gets the call, confirm the run's `to` before anyone picks up.

```
POST $HAPPYROBOT_HOOK_URL
Authorization: Bearer $HAPPYROBOT_API_KEY
{
  "tipo_emergencia": "{intent} (level {level}, run {run_id})",
  "pautas": "{action_taken}. Abra {viewer_url}.",
  "nivel_gravedad": "crítico",
  "nombre_contacto": "$ONCALL_NAME"
}
```

Voice: identify as the pager, say `{action_taken}`, ask him to open `{viewer_url}`. Retry once on no pickup. Do not roll infra back.

---

### Dispatcher

The monitor calls the idempotent dispatcher after every fused gate decision. It records stable action IDs and executes armed demo-world counters in reverse order at L3+.

```
on_gate(run, assessment):
    level = max(run.level, assessment.incident_level)
    persist event + assessment + StreamMessages in Neo4j
    match level:
        1: tag_run(run)
        2: tag_run(run); start_supervisor(run)
        3: contain.sh run.id                         # pause first, then ports + token
        4: contain all live runs; cut-egress.sh & page_guli(4)
        5: kill-swarm.sh & page_guli(5)
    if level >= 3: execute_armed_counters_reverse_order(run)
```

Jumping 1 → 4 still contains live runs, then cuts egress. Jumping to 5 still copies logs, then kills the agent compose.

| Failure | What we do |
|---|---|
| `jev` timeout / crash | Keep the last level. Do not ignore. Do not jump to L5. Alert the viewer. |
| L3 fails after `docker pause` | Leave it paused. Retry token delete. Do not unpause. |
| L4 docker/iptables error | Still disconnect what you can. Page Guli with `action_taken=partial`. |
| HappyRobot 5xx | Retry once. Infra already ran. |

### Build order

1. Split networks: `agentnet` vs `appnet_hackspain`. Per-run token at container start.
2. `jev` on the chain → L1 tag in the viewer.
3. Dispatcher stub; wire L1.
4. `contain.sh` (pause + close ports + drop token) → L3.
5. Helmcode supervisor → L2.
6. `cut-egress.sh --dry-run` then real disconnect → L4.
7. HappyRobot pager (`scripts/page.sh`) on L4/L5 without awaiting.
8. `kill-swarm.sh` + forensics copy; separate `compose.agents.yaml` → L5.

Env keys: see `.env_template` (`HELMCODE_*`, `HAPPYROBOT_*`, `ONCALL_*`, `CLOUD=none`, `AGENT_BRIDGE=agentnet`). AWS ids (`AGENT_VPC_ID`, `IGW_ID`, `AGENT_NACL_ID`) are only for the ideal path.
