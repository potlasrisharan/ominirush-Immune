# OmniResilience: Autonomous Chaos-Driven Self-Healing Engine
> *"Most software fails in production because developers only test what they expect. OmniResilience tests the unexpected, breaks the system, heals the code, and proves the fix—autonomously."*

---

## 📌 Executive Summary
**OmniResilience** is an autonomous multi-agent resilience platform that closes the loop between **Chaos Engineering (Red Team)** and **Automated Code Repair (Blue Team)** in an isolated Docker sandbox.

Traditional chaos engineering (Gremlin, Chaos Mesh) requires engineers to manually write fault scenarios, watch the system collapse, and manually write patches. OmniResilience replaces this brittle manual cycle with a closed-loop agent swarm:
1. **Autonomous Red Swarm (Chaos Injection):** Parallel OmniRush sub-agents launch coordinated attack vectors (payload fuzzing, concurrency deadlocks, latency spikes, and memory pressure) against a running containerized service until it fails or degrades.
2. **Autonomous Blue Swarm (Triage & Self-Healing):** Upon detecting a crash or SLA violation, the healing agent analyzes stack traces, captures container telemetry, locates root-cause AST in the source repository, and synthesizes a minimal hardening patch.
3. **Verification Sandbox:** The engine hot-reloads the patched container and replays the exact failure-inducing chaos sequence. If the application survives, it commits the fix and generates a verified pull request.

---

## 🎯 Track Selection & Strategic Positioning
- **Primary Track:** **Systems, Infrastructure & Cybersecurity**
- **Secondary Track / Tag:** **Developer Tools & Open Source**
- **Target Category:** 1st Place + Best Use of OmniRush Sub-Agents

### Why This Track Wins:
90% of hackathon entries build generic markdown PR reviewers or basic chatbots. Systems, Infrastructure & Cybersecurity is uncrowded, highly technical, and directly rewarded by the judging rubric:
- **True Parallel Execution:** Requires simultaneous, multi-vector stress testing across network, memory, and application logic.
- **Single-Command Reproducibility:** Entire stack runs via `docker compose up --build`. Zero local configuration needed.
- **Deterministic Validation:** The win condition isn't subjective prose—the patched container either survives the re-injected chaos attack or it doesn't.

---

## ⚙️ First-Principles Architecture

```
                                  ┌───────────────────────────────┐
                                  │      OmniResilience Core      │
                                  │   (Orchestrator & Arbiter)    │
                                  └───────────────┬───────────────┘
                                                  │
                 ┌────────────────────────────────┴────────────────────────────────┐
                 ▼                                                                 ▼
   [ RED TEAM: CHAOS SWARM ]                                     [ BLUE TEAM: HEALING ENGINE ]
   (Parallel OmniRush Sub-Agents)                                 (OmniRush Code Intelligence)
   ┌─────────────────────────────────────────┐                   ┌─────────────────────────────┐
   │ 1. Payload & Boundary Fuzzer            │                   │ 1. Telemetry & Log Triage   │
   │    • Extreme payloads, nulls, SQLi/XSS  │                   │    • Stack trace parser     │
   │ 2. Concurrency & Race Condition Trigger │                   │ 2. AST Code Repair Agent    │
   │    • Bursts, thread starvation          │                   │    • Adds validation/guards │
   │ 3. Latency & Network Disruption Agent   │                   │    • Implements timeouts    │
   │    • Throttling, socket drop            │                   │    • Adds circuit breakers  │
   └────────────────────┬────────────────────┘                   └──────────────┬──────────────┘
                        │ Coordinated Attacks                                   │ Verified Patch
                        ▼                                                       ▼
       ┌─────────────────────────────────────────────────────────────────────────────┐
       │                       SANDBOXED DOCKER RUNTIME                              │
       │                                                                             │
       │   ┌────────────────────────┐             ┌──────────────────────────────┐   │
       │   │ Target Microservice    │             │ Real-time Telemetry Observer │   │
       │   │ (HTTP API / Database)  │ ◄────────── │ (Health, Latency, CPU, RAM)  │   │
       │   └────────────────────────┘             └──────────────┬───────────────┘   │
       └───────────────────────┬─────────────────────────────────┘                   │
                               │                                                     │
                               └──────────────► CRASH DETECTED! ─────────────────────┘
```

---

## 🤖 OmniRush Sub-Agent Roster

| Agent Name | Role | Responsibilities |
|---|---|---|
| **Supervisor / Arbiter** | Controller | Coordinates red/blue lifecycle, manages Docker sandbox, gates pull request output. |
| **Chaos Sub-Agent 1 (FuzzNet)** | Red Team | Generates malicious, corrupted, and malformed inputs targeting exposed endpoints. |
| **Chaos Sub-Agent 2 (StormLoad)** | Red Team | Generates aggressive asynchronous request spikes to trigger race conditions and unhandled deadlocks. |
| **Chaos Sub-Agent 3 (LatencyDrift)** | Red Team | Uses traffic control (`tc`/proxy) to inject packet drops and 10x latency to break synchronous timeouts. |
| **Diagnostic Sub-Agent (TraceHunter)**| Blue Team | Ingests stderr, container exit codes, and Prometheus/log streams to pinpoint failure line numbers. |
| **Patching Sub-Agent (AutoFixer)** | Blue Team | Modifies source code to insert input guards, concurrency locks, circuit breakers, or backoff logic. |

---

## 🔄 The Closed-Loop Execution Flow

1. **Phase 1: Baselines & Target Ingestion**
   - User targets any repository with a `Dockerfile` or `docker-compose.yml`.
   - OmniResilience boots the service and measures clean baseline latency and throughput.
2. **Phase 2: Parallel Chaos Waves**
   - The 3 Red Team sub-agents attack in coordinated waves.
   - Example failure trigger: `FuzzNet` passes an unbounded payload causing an uncaught exception (`ZeroDivisionError` or unhandled JSON panic) resulting in container crash (Exit 1).
3. **Phase 3: Root-Cause Synthesis & Patch Generation**
   - TraceHunter extracts the stack trace and matches it to `src/api/routes.py#L42`.
   - AutoFixer applies an AST-level patch: wraps the route with schema validation and fallback error boundaries.
4. **Phase 4: Verification Trial (Prove It)**
   - Sandbox hot-reloads the patched container.
   - The Arbiter re-executes the exact failing attack wave.
   - System confirms `HTTP 200/400 Handled` instead of `HTTP 500 / Process Termination`.
5. **Phase 5: Git Artifact Generation**
   - Emits a clean `git diff`, a markdown Chaos Audit Report with graphs, and a verified pull request branch.

---

## 🛠️ Tech Stack & Reproducibility
- **Agent Orchestrator:** Python 3.12 (asyncio, OmniRush SDK / GPT-6 Astra)
- **Container Sandbox:** Docker Engine & Docker SDK for Python
- **Chaos Primitives:** HTTPX / Locust async load generation + synthetic network delay
- **Interface:**
  - Rich CLI (terminal dashboards, live status bars, colored diffs)
  - Single-Command Runner: `docker compose up --build`
- **Bundled Target App:** Includes a vulnerable microservice (Python FastAPI / Node.js) exhibiting common real-world bugs (unbounded memory buffer, missing timeout, uncaught null reference) for instant judge testing.

---

## 🏆 Submission Form Fast-Copy (Unstop & OmniRush Dashboard)

### Project Title
`OmniResilience: Autonomous Chaos Engineering & Self-Healing Agent Swarm`

### Short Description (max 120 chars)
`Autonomous multi-agent system that injects parallel chaos into Docker services, breaks them, and synthesizes verified patches.`

### Detailed Description (for Unstop / Dashboard)
```markdown
OmniResilience redefines system reliability by bridging the gap between Chaos Engineering and Automated Program Repair.

Instead of passively reviewing code or waiting for production outages, OmniResilience spins up target microservices inside an isolated Docker environment and deploys a coordinated swarm of parallel OmniRush sub-agents to actively stress-test it:
1. Red Team Sub-Agents execute simultaneous attack vectors: boundary payload fuzzing, concurrency storms, and synthetic latency injection.
2. When the service crashes or breaches SLA thresholds, the system halts chaos injection and passes container telemetry and stack traces to Blue Team Sub-Agents.
3. The healing agent pinpoints the faulty code in the repository, constructs an AST-level remediation patch (adding input validation, circuit breakers, and defensive guards), and applies it.
4. The engine automatically rebuilds the sandbox and re-executes the exact chaos vector to scientifically verify that the failure mode is resolved.
5. Once proven, it outputs a clean Git diff and a comprehensive Chaos Resilience Audit Report.

Everything is containerized, pinned, and executable via a single command: `docker compose up --build`.
```
