from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omniimmune.models import ImmunitySummary


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
