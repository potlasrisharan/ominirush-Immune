"""Standalone, dependency-free mission-control dashboard.

The terminal HUD is useful while an Arbiter is attached to a TTY.  This module
provides the same small surface for a browser without requiring a template
engine, a frontend build, or a second process.  A caller can either mount
``create_dashboard_router(store)`` on an existing FastAPI service or use
``create_dashboard_app()`` for a tiny dashboard service.

The update contract is intentionally plain JSON.  ``POST /dashboard/state``
merges a partial update into the current snapshot and ``PUT`` replaces it.
The page polls ``GET /dashboard/state`` so an Arbiter, CLI process, or test can
publish progress without knowing anything about the HTML implementation.
"""

from __future__ import annotations

import copy
import asyncio
import json
import time
from collections.abc import Mapping
from threading import RLock
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from omniimmune.events import Event, EventBus, EventType


DEFAULT_STATE: dict[str, Any] = {
    "resilience_score": 100,
    "resilience_total": 100,
    "phase": "mission control",
    "status": "STANDING BY",
    "updated_at": "Awaiting telemetry",
    "latency": {
        "before_ms": 0,
        "after_ms": 0,
        "before_label": "before heal",
        "after_label": "after heal",
    },
    "diff": (
        "# LibCST patch stream\n"
        "# No repair has been applied yet.\n"
        "# Publish a state update to inspect the live patch."
    ),
    "branches": [
        {"name": "heal/sig-order-panic-01", "status": "CREATED", "detail": "verified"},
        {"name": "heal/sig-starvation-02", "status": "CREATED", "detail": "verified"},
        {"name": "heal/sig-upstream-timeout-03", "status": "CREATED", "detail": "verified"},
    ],
    "topology": {
        "red": {"label": "Red Swarm", "status": "armed"},
        "target": {"label": "Target App", "status": "nominal"},
        "blue": {"label": "Blue Swarm", "status": "healing"},
        "vault": {"label": "Cryptographic Vault", "status": "sealed"},
    },
    "timeline": [
        {"label": "dashboard connected", "status": "ready"},
        {"label": "waiting for mission telemetry", "status": "pending"},
    ],
}


def _clone(value: Any) -> Any:
    """Copy state values while keeping the public store free of shared lists."""

    return copy.deepcopy(value)


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), Mapping):
            nested = dict(target[key])
            _merge(nested, value)
            target[key] = nested
        else:
            target[key] = _clone(value)


def _normalise_state(payload: Mapping[str, Any], *, base: Mapping[str, Any]) -> dict[str, Any]:
    """Accept both dashboard-shaped data and an ``ImmunitySummary`` dump.

    Summary fields are kept intact so consumers can add their own metadata,
    while the aliases below make the existing Arbiter result immediately
    visible in the dashboard.
    """

    state = _clone(dict(base))
    _merge(state, payload)

    if "score" in payload and "resilience_score" not in payload:
        state["resilience_score"] = payload["score"]
    if "total" in payload and "resilience_total" not in payload:
        state["resilience_total"] = payload["total"]
    elif "incidents_total" in payload and "resilience_total" not in payload:
        state["resilience_total"] = 100
    if "patch_diffs" in payload and "diff" not in payload and payload["patch_diffs"]:
        state["diff"] = "\n\n".join(str(item) for item in payload["patch_diffs"])
    if "timeline" in payload and isinstance(payload["timeline"], list):
        state["timeline"] = payload["timeline"]
    if "branch" in payload and "branches" not in payload and payload["branch"]:
        state["branches"] = [{"name": str(payload["branch"]), "status": "CREATED"}]
    if "incidents" in payload and isinstance(payload["incidents"], list):
        branches: list[dict[str, Any]] = []
        latest_latency: dict[str, Any] | None = None
        for incident in payload["incidents"]:
            if not isinstance(incident, Mapping):
                continue
            branch = incident.get("branch")
            if branch:
                branches.append(
                    {
                        "name": str(branch),
                        "status": str(incident.get("branch_status") or "CREATED"),
                        "detail": str(incident.get("commit") or "verified"),
                    }
                )
            before = incident.get("before")
            after = incident.get("after")
            if isinstance(before, Mapping) or isinstance(after, Mapping):
                latest_latency = {
                    "before_ms": (before or {}).get("p99_ms", 0) if isinstance(before, Mapping) else 0,
                    "after_ms": (after or {}).get("p99_ms", 0) if isinstance(after, Mapping) else 0,
                }
        state["branches"] = branches
        if latest_latency is not None:
            state["latency"] = {**state.get("latency", {}), **latest_latency}
    if "before_after" in payload and "latency" not in payload:
        state["latency"] = payload["before_after"]
    if isinstance(payload.get("before"), Mapping) or isinstance(payload.get("after"), Mapping):
        state["latency"] = {
            **state.get("latency", {}),
            "before_ms": (payload.get("before") or {}).get("p99_ms", 0),
            "after_ms": (payload.get("after") or {}).get("p99_ms", 0),
        }
    return state


class DashboardStore:
    """Small thread-safe state store suitable for callbacks and test clients."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._lock = RLock()
        self._state = _normalise_state(initial or {}, base=DEFAULT_STATE)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return _clone(self._state)

    def update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._state = _normalise_state(payload, base=self._state)
            return _clone(self._state)

    def replace(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._state = _normalise_state(payload, base=DEFAULT_STATE)
            return _clone(self._state)


class DashboardBroadcaster:
    """Fan out EventBus events to connected dashboard SSE clients.

    A broadcaster owns only subscriptions and client queues; the dashboard
    store remains the source of truth for the existing polling API.  EventBus
    callbacks are deliberately synchronous so publishing retains its existing
    ordering semantics, while queue writes are marshalled onto each client's
    event loop when an arbiter publishes from another thread.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        store: DashboardStore | None = None,
        *,
        keepalive_seconds: float = 15.0,
    ) -> None:
        self.event_bus = event_bus or EventBus()
        self.store = store or DashboardStore()
        self.keepalive_seconds = max(0.1, float(keepalive_seconds))
        self._clients: dict[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop] = {}
        self._lock = RLock()
        self.event_bus.subscribe_all(self._on_event)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register the current async task and return its event queue."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            self._clients[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Disconnect a client queue (safe to call more than once)."""

        with self._lock:
            self._clients.pop(queue, None)

    def close(self) -> None:
        """Detach from the EventBus when an embedding app is torn down."""

        self.event_bus.unsubscribe_all(self._on_event)
        with self._lock:
            self._clients.clear()

    def _dashboard_patch(self, event: Event) -> dict[str, Any]:
        """Translate lifecycle events into a compatible dashboard snapshot."""

        status_by_type = {
            EventType.ATTACK_STARTED: "ATTACK IN PROGRESS",
            EventType.FAILURE_DETECTED: "FAILURE DETECTED",
            EventType.TRIAGE_DONE: "TRIAGE COMPLETE",
            EventType.PATCH_PROPOSED: "PATCH PROPOSED",
            EventType.PATCH_APPLIED: "PATCH APPLIED",
            EventType.VERIFY_STARTED: "VERIFYING REPAIR",
            EventType.VERIFY_PASSED: "VERIFICATION PASSED",
            EventType.VERIFY_FAILED: "VERIFICATION FAILED",
            EventType.ROLLED_BACK: "PATCH ROLLED BACK",
            EventType.GIT_BRANCH_CREATED: "HEAL BRANCH CREATED",
            EventType.GIT_BRANCH_FAILED: "HEAL BRANCH FAILED",
            getattr(EventType, "GIT_BRANCH_PUSHED", "git_branch_pushed"): "HEAL BRANCH PUSHED",
            getattr(EventType, "GIT_BRANCH_PUSH_FAILED", "git_branch_push_failed"): "HEAL BRANCH PUSH FAILED",
            getattr(EventType, "PR_CREATED", "pr_created"): "PULL REQUEST CREATED",
            getattr(EventType, "PR_FAILED", "pr_failed"): "PULL REQUEST FAILED",
            EventType.RUN_COMPLETE: "MISSION COMPLETE",
        }
        if event.type == EventType.RUN_COMPLETE:
            # A completed summary already has the aliases understood by the
            # normaliser, and preserving it here keeps all summary fields.
            summary = dict(event.data)
            summary.setdefault("status", "MISSION COMPLETE")
            summary.setdefault("updated_at", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(event.ts)))
            return summary

        patch: dict[str, Any] = {
            "status": status_by_type.get(event.type, event.type.replace("_", " ").upper()),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(event.ts)),
        }
        timeline = self.store.snapshot().get("timeline", [])
        patch["timeline"] = [*timeline, {"label": event.type.replace("_", " "), "status": patch["status"], **event.data}]
        if event.type == EventType.PATCH_APPLIED and event.data.get("diff"):
            patch["diff"] = event.data["diff"]
        if event.type == EventType.GIT_BRANCH_CREATED and event.data.get("branch"):
            patch["branches"] = [
                *self.store.snapshot().get("branches", []),
                {"name": event.data["branch"], "status": "CREATED", "detail": event.data.get("commit", "verified")},
            ]
        return patch

    def _on_event(self, event: Event) -> None:
        state = self.store.update(self._dashboard_patch(event))
        payload = {
            "type": event.type,
            "data": _clone(event.data),
            "ts": event.ts,
            "state": state,
        }
        with self._lock:
            client_items = tuple(self._clients.items())
        for queue, loop in client_items:
            try:
                if loop.is_closed():
                    self.unsubscribe(queue)
                elif loop is asyncio.get_running_loop():
                    queue.put_nowait(payload)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, payload)
            except RuntimeError:
                # There is no running loop when a synchronous publisher emits
                # outside an async request; connected loops are handled above.
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, payload)
                except RuntimeError:
                    self.unsubscribe(queue)

    async def stream(self, request: Request):
        queue = self.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), self.keepalive_seconds)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # Named events make browser handlers expressive; the generic
                # message handler below remains useful to non-browser clients.
                event_type = str(payload.get("type", "message"))
                body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
                yield f"event: {event_type}\ndata: {body}\n\n"
        finally:
            self.unsubscribe(queue)

    async def response(self, request: Request) -> StreamingResponse:
        return StreamingResponse(
            self.stream(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


def _json_for_script(value: Mapping[str, Any]) -> str:
    """Serialize initial state safely inside a script tag."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


_DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07101d">
<title>OmniImmune · Mission Control</title>
<style>
:root{color-scheme:dark;--bg:#07101d;--panel:#0b1828;--panel2:#0d2033;--line:#1b3449;--text:#e8f2f5;--muted:#7d98a6;--cyan:#63e7e1;--green:#76e3a4;--amber:#f3bd69;--red:#ff6f80;--blue:#91a9ff;--shadow:0 20px 60px #0006}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -20%,#173a53 0,#07101d 48%);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.17;background-image:linear-gradient(#7be7e111 1px,transparent 1px),linear-gradient(90deg,#7be7e111 1px,transparent 1px);background-size:44px 44px;mask-image:linear-gradient(to bottom,#000,transparent 80%)}
.shell{width:min(1500px,calc(100% - 40px));margin:auto;padding:30px 0 45px}.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:25px}.kicker{color:var(--cyan);font-size:11px;letter-spacing:.18em;font-weight:700}.title{font-size:clamp(28px,4vw,48px);letter-spacing:-.05em;margin:4px 0 5px}.subtitle{color:var(--muted);margin:0}.status{align-items:center;border:1px solid #2b5268;background:#0b2030;border-radius:999px;color:var(--cyan);display:inline-flex;gap:9px;padding:9px 13px;font-size:11px;font-weight:700;letter-spacing:.08em;white-space:nowrap}.dot{background:currentColor;border-radius:50%;box-shadow:0 0 14px currentColor;height:7px;width:7px}.grid{display:grid;grid-template-columns:1.05fr 1.55fr 1fr;gap:15px}.card{background:linear-gradient(145deg,#0d2032e8,#091523e8);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}.card-head{align-items:center;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;padding:14px 16px}.label{font-size:11px;letter-spacing:.13em;color:var(--muted);font-weight:700}.card-body{padding:16px}.score-card{grid-row:span 2;min-height:410px}.score-wrap{display:grid;place-items:center;padding:24px 0 20px;position:relative}.meter{--score:100;width:190px;height:190px;display:grid;place-items:center;border-radius:50%;background:conic-gradient(var(--cyan) calc(var(--score)*1%),#183246 0);box-shadow:0 0 50px #28d8d933}.meter:after{background:var(--bg);border-radius:50%;content:"";position:absolute;width:166px;height:166px}.score{position:relative;text-align:center;z-index:1}.score strong{display:block;font-size:42px;letter-spacing:-.07em}.score span{color:var(--muted);font-size:11px;letter-spacing:.14em}.metrics{display:grid;grid-template-columns:repeat(2,1fr);border-top:1px solid var(--line);margin:0 16px}.metric{padding:14px 5px;border-right:1px solid var(--line)}.metric:last-child{border:0}.metric b{display:block;font-size:20px}.metric small{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}.topology-card{grid-column:span 2}.topology{height:245px;display:block;width:100%}.topology text{fill:var(--text);font-family:inherit;font-size:12px;font-weight:700}.topology .sub{fill:var(--muted);font-size:10px;font-weight:500}.wire{stroke:#2c596a;stroke-dasharray:5 6;stroke-width:1.5}.node{fill:#0d2535;stroke:#3c6b7d;stroke-width:1.5}.node.red{fill:#2c1929;stroke:var(--red)}.node.blue{fill:#18234b;stroke:var(--blue)}.node.vault{fill:#1d2b2b;stroke:var(--green)}.pulse{fill:var(--cyan);filter:drop-shadow(0 0 6px var(--cyan))}.latency-card{grid-column:span 2}.bar-row{display:grid;grid-template-columns:100px 1fr 60px;align-items:center;gap:12px;margin:17px 0}.bar-row label{font-size:12px;color:var(--muted)}.track{background:#183043;border-radius:99px;height:11px;overflow:hidden}.bar{background:linear-gradient(90deg,var(--red),#f69c6b);border-radius:inherit;height:100%;min-width:2px;transition:width .4s ease}.bar.after{background:linear-gradient(90deg,var(--cyan),var(--green))}.bar-row output{font:700 13px ui-monospace,monospace;text-align:right}.diff-card{grid-column:span 2}.diff{background:#07111c;border:1px solid #1d3848;border-radius:9px;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;max-height:295px;overflow:auto;padding:13px;white-space:pre-wrap}.diff-line{display:block;min-height:1.6em}.diff-line.add{background:#0a342d;color:#9bf0c6}.diff-line.del{background:#3a1c2a;color:#ff9dad}.diff-line.hunk{color:#9daeff}.diff-line.comment{color:#718b98}.branches{display:flex;flex-direction:column;gap:9px}.branch{align-items:center;background:#0b1927;border:1px solid #1a3444;border-radius:9px;display:flex;gap:10px;justify-content:space-between;padding:10px 11px}.branch-name{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.pill{border-radius:99px;font-size:9px;font-weight:800;letter-spacing:.09em;padding:3px 7px}.pill.ready,.pill.verified,.pill.passed{background:#153e35;color:var(--green)}.pill.pending,.pill.healing{background:#3c321a;color:var(--amber)}.pill.failed,.pill.error{background:#481e2c;color:var(--red)}.timeline{list-style:none;margin:0;padding:0}.timeline li{border-left:1px solid #2d5663;margin-left:5px;padding:0 0 14px 16px;position:relative;color:var(--muted)}.timeline li:before{background:var(--cyan);border:3px solid var(--panel);border-radius:50%;content:"";height:7px;left:-5px;position:absolute;top:3px;width:7px}.timeline li:last-child{padding-bottom:0}.timeline strong{color:var(--text);display:block;font-size:12px}.tiny{color:var(--muted);font-size:11px}.refresh{background:transparent;border:1px solid #2d5261;border-radius:7px;color:var(--cyan);cursor:pointer;font:inherit;font-size:11px;padding:6px 9px}.refresh:hover,.refresh:focus-visible{background:#123145;outline:none}.footer{color:#557381;font-size:11px;margin-top:18px;text-align:right}@media(max-width:1050px){.grid{grid-template-columns:1fr 1fr}.score-card{grid-row:auto}.topology-card,.latency-card,.diff-card{grid-column:span 2}}@media(max-width:650px){.shell{width:min(100% - 24px,1500px);padding-top:20px}.topbar{display:block}.status{margin-top:15px}.grid{display:block}.card{margin-bottom:14px}.topology{height:220px}.bar-row{grid-template-columns:78px 1fr 53px;gap:8px}}
</style>
</head>
<body>
<main class="shell">
  <header class="topbar"><div><div class="kicker">OMNIIMMUNE / LIVE MISSION CONTROL</div><h1 class="title">Resilience command deck</h1><p class="subtitle">Break it · Heal it · Prove it — closed-loop service immunity in real time.</p></div><div class="status"><i class="dot"></i><span id="status">STANDING BY</span></div></header>
  <section class="grid" aria-label="Mission control dashboard">
    <article class="card score-card"><div class="card-head"><span class="label">RESILIENCE SCORE</span><span class="pill ready" id="score-state">LIVE</span></div><div class="score-wrap"><div class="meter" id="meter"><div class="score"><strong id="score">100/100</strong><span>IMMUNITY INDEX</span></div></div></div><div class="metrics"><div class="metric"><b id="verified">—</b><small>verified</small></div><div class="metric"><b id="vaccinated">—</b><small>vaccinated</small></div></div><div class="card-body"><div class="tiny">Current phase</div><strong id="phase">mission control</strong><div class="tiny" style="margin-top:13px">Last telemetry</div><strong id="updated">Awaiting telemetry</strong></div></article>
    <article class="card topology-card"><div class="card-head"><span class="label">LIVE TOPOLOGY</span><span class="tiny">encrypted control plane</span></div><svg class="topology" viewBox="0 0 760 245" role="img" aria-label="Topology connecting Red Swarm, Target App, Blue Swarm, and Cryptographic Vault"><path class="wire" d="M145 122H275M485 122H615M380 72V43M380 174V202"/><circle class="pulse" cx="210" cy="122" r="3"/><circle class="pulse" cx="550" cy="122" r="3"/><g transform="translate(65 82)"><rect class="node red" width="160" height="80" rx="12"/><text x="80" y="32" text-anchor="middle">Red Swarm</text><text class="sub" id="top-red" x="80" y="52" text-anchor="middle">armed</text></g><g transform="translate(275 82)"><rect class="node" width="210" height="80" rx="12"/><text x="105" y="32" text-anchor="middle">Target App</text><text class="sub" id="top-target" x="105" y="52" text-anchor="middle">nominal</text></g><g transform="translate(535 82)"><rect class="node blue" width="160" height="80" rx="12"/><text x="80" y="32" text-anchor="middle">Blue Swarm</text><text class="sub" id="top-blue" x="80" y="52" text-anchor="middle">healing</text></g><g transform="translate(300 8)"><rect class="node vault" width="160" height="54" rx="10"/><text x="80" y="23" text-anchor="middle">Cryptographic Vault</text><text class="sub" id="top-vault" x="80" y="41" text-anchor="middle">sealed</text></g></svg></article>
    <article class="card latency-card"><div class="card-head"><span class="label">LATENCY CONTAINMENT</span><span class="tiny">before / after heal</span></div><div class="card-body"><div class="bar-row"><label id="before-label">before heal</label><div class="track"><div class="bar" id="before-bar" style="width:2%"></div></div><output id="before">0 ms</output></div><div class="bar-row"><label id="after-label">after heal</label><div class="track"><div class="bar after" id="after-bar" style="width:2%"></div></div><output id="after">0 ms</output></div></div></article>
    <article class="card diff-card"><div class="card-head"><span class="label">BLUE SWARM / LIBCST SYNTAX DIFF</span><span class="pill healing" id="diff-status">AWAITING PATCH</span></div><div class="card-body"><pre class="diff" id="diff" aria-label="LibCST syntax diff"># LibCST patch stream
# No repair has been applied yet.</pre></div></article>
    <article class="card"><div class="card-head"><span class="label">HEAL BRANCHES</span><span class="tiny" id="branch-count">3 branches</span></div><div class="card-body"><div class="branches" id="branches"><div class="branch"><span class="branch-name">heal/sig-order-panic-01</span><span class="pill created">CREATED</span></div></div></div></article>
    <article class="card"><div class="card-head"><span class="label">EVENT STREAM</span><button class="refresh" id="refresh" type="button">refresh</button></div><div class="card-body"><ol class="timeline" id="timeline"><li><strong>dashboard connected</strong><span>ready</span></li></ol></div></article>
  </section>
  <div class="footer">polling <code>/dashboard/state</code> · <span id="connection">connected</span></div>
</main>
<script>
const INITIAL_STATE=__INITIAL_STATE__;
const byId=(id)=>document.getElementById(id);
const text=(value,fallback='—')=>value===null||value===undefined||value===''?fallback:String(value);
const escapeHtml=(value)=>text(value,'').replace(/[&<>"']/g,(char)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const numeric=(value,fallback=0)=>{const result=Number(value);return Number.isFinite(result)?result:fallback};
function renderDiff(value){const source=text(value,'# No patch has been applied yet.');return source.split(/\r?\n/).map((line)=>{let kind='';if(line.startsWith('+')&&!line.startsWith('+++'))kind='add';else if(line.startsWith('-')&&!line.startsWith('---'))kind='del';else if(line.startsWith('@@'))kind='hunk';else if(line.trim().startsWith('#'))kind='comment';return `<span class="diff-line ${kind}">${escapeHtml(line)||' '}</span>`}).join('')}
function render(state){
 const score=Math.max(0,Math.min(100,numeric(state.resilience_score??state.score,100)));const total=Math.max(1,numeric(state.resilience_total,100));
 byId('score').textContent=`${Math.round(score)}/${Math.round(total)}`;byId('meter').style.setProperty('--score',`${score}`);byId('status').textContent=text(state.status,'STANDING BY').toUpperCase();byId('phase').textContent=text(state.phase,'mission control');byId('updated').textContent=text(state.updated_at,'Awaiting telemetry');
 byId('verified').textContent=text(state.incidents_verified,'—');byId('vaccinated').textContent=text(state.vaccinated_count,'—');byId('score-state').textContent=score>=100?'LIVE':score>=60?'DEGRADED':'ALERT';
 const topology=state.topology||{};['red','target','blue','vault'].forEach((key)=>{const node=topology[key]||{};const element=byId(`top-${key}`);if(element)element.textContent=text(node.status,'unknown')});
 const latency=state.latency||{};const before=Math.max(0,numeric(latency.before_ms??latency.before??state.before_latency_ms));const after=Math.max(0,numeric(latency.after_ms??latency.after??state.after_latency_ms));const max=Math.max(before,after,1);byId('before').textContent=`${before} ms`;byId('after').textContent=`${after} ms`;byId('before-bar').style.width=`${Math.max(before?2:0,before/max*100)}%`;byId('after-bar').style.width=`${Math.max(after?2:0,after/max*100)}%`;byId('before-label').textContent=text(latency.before_label,'before heal');byId('after-label').textContent=text(latency.after_label,'after heal');
 byId('diff').innerHTML=renderDiff(state.diff);byId('diff-status').textContent=state.diff&&String(state.diff).includes('No repair')?'AWAITING PATCH':'PATCH READY';
 const branches=Array.isArray(state.branches)?state.branches:[];byId('branch-count').textContent=`${branches.length} branch${branches.length===1?'':'es'}`;byId('branches').innerHTML=branches.length?branches.map((branch)=>{const item=typeof branch==='string'?{name:branch,status:'READY'}:branch||{};const status=text(item.status,'READY');const tone=status.toLowerCase().replace(/[^a-z]/g,'');return `<div class="branch"><span class="branch-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name||'heal/sig-pending')}</span><span class="pill ${tone}">${escapeHtml(status)}</span></div>`}).join(''):'<div class="tiny">No heal branches recorded.</div>';
 const events=Array.isArray(state.timeline)?state.timeline:[];byId('timeline').innerHTML=events.length?events.slice(-8).reverse().map((event)=>{const item=typeof event==='string'?{label:event,status:'event'}:event||{};return `<li><strong>${escapeHtml(item.label||item.type||'event')}</strong><span>${escapeHtml(item.status||item.detail||'recorded')}</span></li>`}).join(''):'<li><strong>dashboard connected</strong><span>waiting for telemetry</span></li>';
}
 const stateUrl=window.location.pathname.replace(/\/?(?:\?.*)?$/,'/state');
 let dashboardState=INITIAL_STATE;
 async function refresh(){try{const response=await fetch(stateUrl,{headers:{Accept:'application/json'},cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);dashboardState=await response.json();render(dashboardState);byId('connection').textContent='connected'}catch(error){byId('connection').textContent='retrying'}}
 function handleEvent(message){
  const payload=message&&message.data?message.data:message;const state=payload&&payload.state;
  if(state){dashboardState=state;render(state)}
  byId('connection').textContent='live';
 }
 const stream=new EventSource('/api/v1/events/stream');
 stream.onmessage=(message)=>{try{handleEvent({data:JSON.parse(message.data)})}catch(error){byId('connection').textContent='retrying'}};
 ['attack_started','attack_finished','failure_detected','triage_done','patch_proposed','patch_applied','verify_started','verify_passed','verify_failed','rolled_back','memory_hit','escalation_started','escalation_passed','escalation_failed','git_branch_created','git_branch_failed','git_branch_pushed','git_branch_push_failed','pr_created','pr_failed','run_complete'].forEach((eventType)=>stream.addEventListener(eventType,(message)=>{try{handleEvent({data:JSON.parse(message.data)})}catch(error){byId('connection').textContent='retrying'}}));
 stream.onerror=()=>{byId('connection').textContent='reconnecting'};
 render(dashboardState);byId('refresh').addEventListener('click',refresh);
 </script>
</body>
</html>'''


def dashboard_html(state: Mapping[str, Any] | None = None) -> str:
    """Return the complete standalone dashboard document for ``state``."""

    payload = _normalise_state(state or {}, base=DEFAULT_STATE)
    return _DASHBOARD_HTML.replace("__INITIAL_STATE__", _json_for_script(payload))


def create_dashboard_router(
    store: DashboardStore | None = None,
    *,
    prefix: str = "/dashboard",
    event_bus: EventBus | None = None,
    broadcaster: DashboardBroadcaster | None = None,
) -> APIRouter:
    """Create a router exposing the page, state API, and event stream."""

    dashboard_store = store or DashboardStore()
    dashboard_broadcaster = broadcaster or DashboardBroadcaster(event_bus, dashboard_store)
    router = APIRouter(prefix=prefix, tags=["mission-control"])

    @router.get("", response_class=HTMLResponse, name="dashboard")
    async def get_dashboard() -> HTMLResponse:
        return HTMLResponse(dashboard_html(dashboard_store.snapshot()))

    @router.get("/state", name="dashboard_state")
    async def get_dashboard_state() -> dict[str, Any]:
        return dashboard_store.snapshot()

    @router.post("/state", name="update_dashboard_state")
    async def update_dashboard_state(payload: dict[str, Any]) -> dict[str, Any]:
        return dashboard_store.update(payload)

    @router.put("/state", name="replace_dashboard_state")
    async def replace_dashboard_state(payload: dict[str, Any]) -> dict[str, Any]:
        return dashboard_store.replace(payload)

    @router.get("/api/v1/events/stream", name="dashboard_events_stream")
    async def dashboard_events_stream(request: Request) -> StreamingResponse:
        return await dashboard_broadcaster.response(request)

    return router


def create_dashboard_app(
    store: DashboardStore | None = None,
    *,
    title: str = "OmniImmune Mission Control",
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Build a standalone FastAPI application serving the live dashboard."""

    application = FastAPI(title=title)
    dashboard_store = store or DashboardStore()
    dashboard_broadcaster = DashboardBroadcaster(event_bus, dashboard_store)
    application.include_router(create_dashboard_router(dashboard_store, broadcaster=dashboard_broadcaster))

    # Keep the stream at the documented API path as well as under the router
    # prefix.  The latter is useful to callers mounting the router unchanged;
    # the former is what the inline dashboard client connects to.
    @application.get("/api/v1/events/stream", name="api_dashboard_events_stream")
    async def api_dashboard_events_stream(request: Request) -> StreamingResponse:
        return await dashboard_broadcaster.response(request)

    return application


# Convenient module-level objects for ``uvicorn omniimmune.ui.web:app`` and
# ``main_app.include_router(omniimmune.ui.web.router)``.
dashboard_store = DashboardStore()
dashboard_event_bus = EventBus()
dashboard_broadcaster = DashboardBroadcaster(dashboard_event_bus, dashboard_store)
router = create_dashboard_router(dashboard_store, broadcaster=dashboard_broadcaster)
app = FastAPI(title="OmniImmune Mission Control")
app.include_router(router)
@app.get("/api/v1/events/stream", name="api_dashboard_events_stream")
async def api_dashboard_events_stream(request: Request) -> StreamingResponse:
    return await dashboard_broadcaster.response(request)
dashboard_router = router
dashboard_app = app

# Friendly aliases for callers that prefer the conventional FastAPI names.
create_app = create_dashboard_app
get_dashboard_router = create_dashboard_router

__all__ = [
    "DEFAULT_STATE",
    "DashboardBroadcaster",
    "DashboardStore",
    "app",
    "create_app",
    "create_dashboard_app",
    "create_dashboard_router",
    "dashboard_app",
    "dashboard_broadcaster",
    "dashboard_event_bus",
    "dashboard_html",
    "dashboard_router",
    "dashboard_store",
    "get_dashboard_router",
    "router",
]
