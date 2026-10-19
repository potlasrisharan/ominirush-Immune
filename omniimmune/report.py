from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re
from typing import Any

from omniimmune.models import ImmunitySummary


def _value(value: Any, key: str, default: Any = "") -> Any:
    """Read a field from either the report dictionaries or a model object."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _metric(metrics: Any, name: str, default: Any = "—") -> Any:
    if not isinstance(metrics, dict):
        return default
    return metrics.get(name, default)


def _metric_text(metrics: Any, name: str, *, rate: bool = False) -> str:
    value = _metric(metrics, name)
    if value in (None, ""):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.1%}" if rate else f"{number:.1f} ms"


def _safe_filename(value: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "incident")).strip(".-")
    return normalized or "incident"


def _incident_rows(summary: Any) -> list[dict[str, Any]]:
    incidents = _value(summary, "incidents", []) or []
    return [item if isinstance(item, dict) else item.model_dump(mode="json") for item in incidents]


def generate_html(summary: ImmunitySummary | dict[str, Any], output_path: Path | str = "artifacts/report.html") -> str:
    """Write and return a self-contained dark-mode postmortem dashboard.

    The dashboard intentionally has no external assets or JavaScript dependencies so it
    remains useful when copied out of the repository or viewed from an incident ticket.
    """
    score = _value(summary, "score", 0)
    total = _value(summary, "incidents_total", 0)
    verified = _value(summary, "incidents_verified", 0)
    regression = bool(_value(summary, "regression_passed", False))
    survival = float(_value(summary, "mean_survival_rate", 0.0) or 0.0)
    incidents = _incident_rows(summary)

    cards: list[str] = []
    for incident in incidents:
        sig_id = str(incident.get("sig_id", "incident"))
        before = incident.get("before", {}) or {}
        after = incident.get("after", {}) or {}
        culprit = incident.get("culprit") or ":".join(
            str(part) for part in (incident.get("culprit_file"), incident.get("culprit_line")) if part
        ) or "Unknown AST location"
        branch = incident.get("branch") or f"heal/{sig_id}"
        commit = str(incident.get("commit") or "")[:8] or "pending"
        pr_file = f"artifacts/prs/{_safe_filename(sig_id)}.md"
        command = f'gh pr create --head {branch} --title "Auto-heal {sig_id}" --body-file {pr_file}'
        diff = str(incident.get("diff") or "No LibCST diff recorded.")
        escalation = incident.get("escalation_details") or []
        escalation_text = json.dumps(escalation, indent=2, sort_keys=True, default=str) if escalation else (
            f"mutations tested: {incident.get('mutations_tested', 0)}\n"
            f"all mutations survived: {incident.get('escalation_passed', 'NOT_AVAILABLE')}"
        )
        cards.append(
            f'''<article class="incident-card">
  <div class="incident-heading"><div><span class="eyebrow">INCIDENT</span><h2>{escape(sig_id)}</h2></div>
    <span class="status {'pass' if incident.get('verdict') == 'PASS' else 'fail'}">{escape(str(incident.get('verdict', 'UNKNOWN')))}</span>
  </div>
  <div class="tags"><span>{escape(str(incident.get('bug_class', 'unknown')))}</span><span>DNA {escape(str(incident.get('dna_hash', '—'))[:12])}</span></div>
  <dl class="facts"><div><dt>AST culprit</dt><dd><code>{escape(str(culprit))}</code></dd></div>
    <div><dt>p99 latency</dt><dd><strong>{escape(_metric_text(before, 'p99_ms'))}</strong> → <strong>{escape(_metric_text(after, 'p99_ms'))}</strong></dd></div>
    <div><dt>Error rate</dt><dd><strong>{escape(_metric_text(before, 'error_rate', rate=True))}</strong> → <strong>{escape(_metric_text(after, 'error_rate', rate=True))}</strong></dd></div>
  </dl>
  <details><summary>LibCST diff and adversarial proof</summary>
    <h3>Patch diff</h3><pre class="diff"><code>{escape(diff)}</code></pre>
    <h3>Replay / escalation evidence</h3><pre class="proof"><code>{escape(escalation_text)}</code></pre>
  </details>
  <div class="branch-card"><div><span class="eyebrow">AUTO-BRANCH</span><code>{escape(str(branch))}</code><small>commit {escape(commit)}</small></div>
    <label>Copyable PR command<input readonly value="{escape(command, quote=True)}" aria-label="Copyable gh pr create command"></label>
  </div>
</article>'''
        )

    html = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OmniImmune Postmortem Dashboard</title>
<style>
:root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; background: #0d1117; color: #e6edf3; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; background: #0d1117; }} main {{ max-width: 1180px; margin: 0 auto; padding: 48px 24px 72px; }}
.eyebrow {{ color: #8b949e; font-size: 11px; letter-spacing: .14em; font-weight: 700; }} h1, h2, h3, p {{ margin-top: 0; }} h1 {{ font-size: clamp(28px, 5vw, 48px); letter-spacing: -.04em; margin-bottom: 8px; }} h2 {{ margin: 4px 0 0; font-size: 22px; }} h3 {{ font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: .08em; margin: 22px 0 8px; }}
.muted, small {{ color: #8b949e; }} .hero {{ display: flex; justify-content: space-between; gap: 24px; align-items: end; margin-bottom: 34px; }}
.score {{ min-width: 180px; border: 1px solid #30363d; border-radius: 14px; padding: 18px 20px; background: #161b22; }} .score strong {{ display: block; color: #58a6ff; font-size: 38px; line-height: 1; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 34px; }} .metric, .incident-card {{ border: 1px solid #30363d; background: #161b22; border-radius: 12px; }} .metric {{ padding: 16px; }} .metric strong {{ display: block; font-size: 21px; margin-top: 7px; }} .badge {{ color: #3fb950; }} .badge.fail {{ color: #f85149; }}
.incident-card {{ padding: 22px; margin: 16px 0; }} .incident-heading, .branch-card, .tags, .facts {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }} .status, .tags span {{ border-radius: 999px; padding: 5px 10px; font-size: 12px; font-weight: 700; }} .status.pass {{ color: #3fb950; background: #12261a; }} .status.fail {{ color: #f85149; background: #32151a; }} .tags {{ justify-content: flex-start; margin: 16px 0; }} .tags span {{ background: #21262d; color: #8b949e; }}
.facts {{ align-items: stretch; margin: 0 0 20px; }} .facts div {{ flex: 1; background: #0d1117; border-radius: 8px; padding: 13px; }} dt {{ color: #8b949e; font-size: 12px; margin-bottom: 6px; }} dd {{ margin: 0; }} code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }} code {{ color: #79c0ff; }} details {{ border-top: 1px solid #30363d; padding-top: 14px; }} summary {{ cursor: pointer; color: #58a6ff; font-weight: 600; }} pre {{ overflow: auto; white-space: pre-wrap; word-break: break-word; border-radius: 8px; padding: 14px; font-size: 12px; line-height: 1.55; }} .diff {{ background: #0b1e12; color: #aff5b4; }} .proof {{ background: #0d1117; color: #c9d1d9; border: 1px solid #21262d; }}
.branch-card {{ align-items: stretch; margin-top: 20px; padding: 14px; border: 1px solid #1f6feb; border-radius: 9px; background: #0d1b2e; }} .branch-card small {{ display: block; margin-top: 5px; }} label {{ color: #8b949e; font-size: 11px; display: flex; flex: 1; flex-direction: column; gap: 5px; }} input {{ width: 100%; color: #c9d1d9; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 8px; font: 12px ui-monospace, monospace; }}
@media (max-width: 760px) {{ .hero, .facts, .branch-card {{ display: block; }} .score {{ margin-top: 20px; }} .metrics {{ grid-template-columns: repeat(2, 1fr); }} .facts div {{ margin-bottom: 8px; }} label {{ margin-top: 12px; }} }}
</style></head><body><main>
<header class="hero"><div><span class="eyebrow">OMNIIMMUNE / CLOSED-LOOP POSTMORTEM</span><h1>Resilience report</h1><p class="muted">Break it → Heal it → Prove it</p></div><div class="score"><span class="eyebrow">IMMUNITY SCORE</span><strong>{escape(str(score))}/100</strong></div></header>
<section class="metrics" aria-label="Run summary"><div class="metric"><span class="eyebrow">VERIFIED</span><strong>{escape(str(verified))} / {escape(str(total))}</strong></div><div class="metric"><span class="eyebrow">REGRESSION</span><strong class="badge {'fail' if not regression else ''}">{'PASS' if regression else 'FAIL'}</strong></div><div class="metric"><span class="eyebrow">MEAN SURVIVAL</span><strong class="badge">{survival:.1%}</strong></div><div class="metric"><span class="eyebrow">INCIDENTS</span><strong>{len(incidents)}</strong></div></section>
<section><span class="eyebrow">POSTMORTEMS</span><h2>Incident dashboard</h2>{''.join(cards) if cards else '<p class="muted">No incidents recorded.</p>'}</section>
</main></body></html>'''
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return html


def generate_pr_template(incident: dict[str, Any], output_dir: Path | str = "artifacts/prs") -> Path:
    """Write a reviewer-ready Markdown PR body for one verified incident."""
    incident = incident if isinstance(incident, dict) else incident.model_dump(mode="json")
    sig_id = str(incident.get("sig_id", "incident"))
    before = incident.get("before", {}) or {}
    after = incident.get("after", {}) or {}
    culprit = incident.get("culprit") or incident.get("culprit_file") or "Unknown AST location"
    diff = str(incident.get("diff") or "No LibCST diff recorded.")
    details = incident.get("escalation_details") or []
    proof = json.dumps(details, indent=2, sort_keys=True, default=str) if details else (
        f"Mutations tested: {incident.get('mutations_tested', 0)}\n"
        f"All mutations survived: {incident.get('escalation_passed', 'NOT_AVAILABLE')}"
    )
    strategy = incident.get("strategy") or "recorded LibCST strategy"
    content = f'''# Auto-heal `{sig_id}`

## Incident and AST culprit

- **Signature:** `{sig_id}`
- **Bug class:** `{incident.get("bug_class", "unknown")}`
- **DNA hash:** `{incident.get("dna_hash", incident.get("dna_sha256", "—"))}`
- **AST culprit:** `{culprit}`
- **Patch strategy:** `{strategy}`

## Before / after latency and error rates

| Metric | Before | After |
|---|---:|---:|
| p50 latency | {_metric_text(before, "p50_ms")} | {_metric_text(after, "p50_ms")} |
| p95 latency | {_metric_text(before, "p95_ms")} | {_metric_text(after, "p95_ms")} |
| p99 latency | {_metric_text(before, "p99_ms")} | {_metric_text(after, "p99_ms")} |
| Error rate | {_metric_text(before, "error_rate", rate=True)} | {_metric_text(after, "error_rate", rate=True)} |
| Survival rate | {_metric_text(before, "survival_rate", rate=True)} | {_metric_text(after, "survival_rate", rate=True)} |

## LibCST patch and explanation

The repair was generated by the deterministic LibCST patch ladder using `{strategy}`. The transformer changes the AST while preserving formatting and then compiles the candidate before replay verification.

```diff
{diff.rstrip()}
```

## Replay and adversarial escalation proof

- Baseline replay: **{incident.get("verdict", "UNKNOWN")}**
- Golden-path regression: **{incident.get("baseline_verification", "NOT_AVAILABLE")}**
- Adversarial escalation: **{"PASSED" if incident.get("escalation_passed") is True else "FAILED" if incident.get("escalation_passed") is False else "NOT_AVAILABLE"}**
- Mutations tested: **{incident.get("mutations_tested", 0)}**

```json
{proof}
```

## Approval checklist

- [ ] Confirm the AST culprit and bug class match the incident signature.
- [ ] Review the LibCST diff and patch strategy.
- [ ] Confirm before/after p99 and error-rate improvements.
- [ ] Confirm golden-path regression is green.
- [ ] Confirm replay and all adversarial mutations survived.
- [ ] Approve and merge the isolated `heal/{sig_id}` branch.
'''
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{_safe_filename(sig_id)}.md"
    path.write_text(content, encoding="utf-8")
    return path


class ReportGenerator:
    """Render the event history and healing artifacts as an operator report."""

    def __init__(self, event_bus: Any | None = None) -> None:
        self.event_bus = event_bus

    @staticmethod
    def _ms(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _metric(metrics: dict[str, Any], name: str, default: str = "—") -> str:
        value = metrics.get(name, default)
        return f"{value:.1f}" if isinstance(value, float) else str(value)

    @staticmethod
    def generate_html(
        summary: ImmunitySummary | dict[str, Any], output_path: Path | str = "artifacts/report.html"
    ) -> str:
        return generate_html(summary, output_path)

    @staticmethod
    def generate_pr_template(incident: dict[str, Any], output_dir: Path | str = "artifacts/prs") -> Path:
        return generate_pr_template(incident, output_dir)

    def generate(self, summary: ImmunitySummary, output_path: Path | str = "INCIDENT_REPORT.md") -> str:
        events = summary.timeline or [event.model_dump() for event in getattr(self.event_bus, "history", [])]
        incidents = summary.incidents
        lines: list[str] = [
            "# OmniResilience Incident Report",
            "",
            "> Automated closed-loop post-mortem: **Break it → Heal it → Prove it**.",
            "",
            "## Executive Summary",
            "",
            f"- **Immunity score:** **{summary.score}/100**",
            f"- **Incidents verified:** {summary.incidents_verified}/{summary.incidents_total}",
            f"- **Golden-path regression:** {'PASS' if summary.regression_passed else 'FAIL'}",
            f"- **Mean survival rate:** {summary.mean_survival_rate:.1%}",
            f"- **Vaccinated signatures:** {summary.vaccinated_count}",
            "",
            "The score is calculated as `60 × verified/total + 20 × regression + 20 × survival`.",
            "",
            "## Event Timeline",
            "",
            "| Timestamp (UTC) | Event | Details |",
            "|---|---|---|",
        ]
        for event in events:
            if isinstance(event, dict):
                ts = event.get("ts", 0)
                event_type = event.get("type", "event")
                data = event.get("data", {})
            else:
                ts, event_type, data = event.ts, event.type, event.data
            details = ", ".join(f"{key}={value}" for key, value in data.items())
            lines.append(f"| {self._ms(float(ts))} | `{event_type}` | {details.replace('|', '\\|')} |")

        lines.extend([
            "",
            "## Incident Table",
            "",
            "| Incident ID | Bug Class | Triggering DNA Hash | Culprit | Strategy | Before (p50/p95/p99, errors, survival) | After (p50/p95/p99, errors, survival) | Verdict |",
            "|---|---|---|---|---|---|---|---|",
        ])
        for incident in incidents:
            before = incident.get("before", {}) or {}
            after = incident.get("after", {}) or {}
            before_text = "/".join(self._metric(before, key) for key in ("p50_ms", "p95_ms", "p99_ms", "error_rate", "survival_rate"))
            after_text = "/".join(self._metric(after, key) for key in ("p50_ms", "p95_ms", "p99_ms", "error_rate", "survival_rate"))
            lines.append(
                f"| `{incident.get('sig_id', '—')}` | {incident.get('bug_class', '—')} | `{incident.get('dna_hash', '—')}` | `{incident.get('culprit', '—')}` | `{incident.get('strategy', '—')}` | {before_text} | {after_text} | **{incident.get('verdict', 'UNKNOWN')}** |"
            )

        lines.extend(["", "## Automated Repair Branch", ""])
        for incident in incidents:
            status = incident.get("branch_status", "NOT_ATTEMPTED")
            lines.extend(
                [
                    f"### `{incident.get('sig_id', '—')}`",
                    "",
                    f"- Status: {status}",
                    f"- Branch: `{incident.get('branch') or '—'}`",
                    f"- Commit: `{incident.get('commit') or '—'}`",
                    f"- Original Branch: `{incident.get('original_branch') or '—'}`",
                    f"- Base Commit: `{incident.get('base_commit') or '—'}`",
                    f"- Target File: `{incident.get('target_file') or '—'}`",
                    f"- Patch Strategy: `{incident.get('strategy') or '—'}`",
                    f"- Triggering DNA ID: `{incident.get('dna_id') or '—'}`",
                    f"- Triggering DNA SHA-256: `{incident.get('dna_sha256') or incident.get('dna_hash') or '—'}`",
                    f"- Baseline Verification: {incident.get('baseline_verification', 'NOT_AVAILABLE')}",
                    f"- Escalation Verification: {incident.get('escalation_verification', 'NOT_AVAILABLE')}",
                ]
            )
            if incident.get("git_error"):
                lines.append(f"- Git Error: {incident['git_error']}")
            lines.append("")

        lines.extend(["", "## Applied Patches", ""])
        patches = summary.patch_diffs or [incident.get("diff", "") for incident in incidents]
        for index, diff in enumerate(patches, 1):
            lines.extend([f"### Patch {index}", "", "```diff", diff.rstrip(), "```", ""])

        lines.extend([
            "## Closed-Loop Sequence",
            "",
            "```mermaid",
            "sequenceDiagram",
            "    participant R as Red Swarm",
            "    participant T as Target",
            "    participant A as Arbiter",
            "    participant B as Blue Swarm CST Patch",
            "    participant V as Replay Verifier",
            "    R->>T: AttackDNA wave",
            "    T-->>A: Crash / SLA breach",
            "    A->>B: Triage culprit and signature",
            "    B->>T: Apply patch and restart",
            "    A->>V: Replay exact DNA",
            "    V-->>A: 2xx / handled 4xx/504 + golden paths",
            "    A-->>R: Immune signature committed",
            "```",
            "",
        ])
        content = "\n".join(lines)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return content
