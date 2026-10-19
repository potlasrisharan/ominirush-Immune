from __future__ import annotations

import sys
import time
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


class OmniHUD:
    """A small, dependency-light Rich dashboard for the closed-loop run.

    The dashboard intentionally keeps all state in Python values. That makes the
    same callbacks useful for an interactive terminal, Docker logs, and tests.
    """

    def __init__(self, console: Console | None = None, is_tty: bool | None = None) -> None:
        self.console = console or Console()
        self.is_tty = sys.stdout.isatty() if is_tty is None else is_tty
        self.started_at = time.perf_counter()
        self.phase = "Baseline"
        self.state: dict[str, Any] = {
            "attack": {},
            "telemetry": {},
            "triage": {},
            "patch": {},
            "verification": {},
            "timeline": [],
        }
        self._live: Live | None = None

    def _ensure_live(self) -> None:
        if self.is_tty and self._live is None:
            self._live = Live(self._render_layout(), console=self.console, refresh_per_second=8)
            self._live.start(refresh=True)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render_layout(), refresh=True)

    @staticmethod
    def _value(data: dict[str, Any], *names: str, default: Any = "—") -> Any:
        for name in names:
            if name in data and data[name] is not None:
                return data[name]
        return default

    def _panel_table(self, rows: list[tuple[str, Any]]) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bright_black")
        table.add_column(ratio=1)
        for label, value in rows:
            table.add_row(label, str(value))
        return table

    def _gauge(self, value: float, total: float = 100.0, width: int = 24) -> Group:
        percentage = max(0.0, min(1.0, value / total if total else 0.0))
        return Group(ProgressBar(total=1, completed=percentage, width=width), Text(f"{value:.0f}/{total:.0f}"))

    def _render_layout(self) -> Layout:
        layout = Layout(name="root")
        layout.split_column(Layout(name="body", ratio=1), Layout(name="footer", size=2))
        body = layout["body"]
        body.split_row(Layout(name="left"), Layout(name="right"))
        body["left"].split_column(Layout(name="attack"), Layout(name="triage"))
        body["right"].split_column(Layout(name="telemetry"), Layout(name="verify"))

        attack = self.state["attack"]
        telemetry = self.state["telemetry"]
        triage = self.state["triage"]
        patch = self.state["patch"]
        verification = self.state["verification"]
        body["attack"].update(
            Panel(
                self._panel_table(
                    [("Vector", self._value(attack, "vector", "attack_type")),
                     ("Rate", f"{self._value(attack, 'requests_per_sec', 'rate', default=0):.1f} req/s" if isinstance(self._value(attack, 'requests_per_sec', 'rate', default=0), (int, float)) else self._value(attack, 'requests_per_sec', 'rate')),
                     ("Payload", self._value(attack, "payload", "payload_preview")),
                     ("Status", self._value(attack, "status", default="ARMED"))]
                ), title="[red]RED SWARM · ATTACK RADAR[/red]", border_style="red"
            )
        )
        body["telemetry"].update(
            Panel(
                self._panel_table(
                    [("CPU", f"{self._value(telemetry, 'cpu_percent', default=0):.1f}%" if isinstance(self._value(telemetry, 'cpu_percent', default=0), (int, float)) else self._value(telemetry, 'cpu_percent')),
                     ("RAM", f"{self._value(telemetry, 'mem_mb', default=0):.1f} MB" if isinstance(self._value(telemetry, 'mem_mb', default=0), (int, float)) else self._value(telemetry, 'mem_mb')),
                     ("Connections", self._value(telemetry, "connections", "active_connections", default=0)),
                     ("Status", self._value(telemetry, "status", default="nominal"))]
                ), title="[cyan]TARGET CONTAINER · TELEMETRY[/cyan]", border_style="cyan"
            )
        )
        diff = str(self._value(patch, "diff", default="No patch applied yet."))
        diff_renderable = Syntax(diff[:5000], "diff", theme="ansi_dark", line_numbers=False)
        triage_group = Group(
            self._panel_table(
                [("Culprit", self._value(triage, "culprit", "culprit_file", default="—")),
                 ("Strategy", self._value(patch, "strategy", default="awaiting triage")),
                 ("Mode", self._value(patch, "status", default="analysis"))]
            ),
            diff_renderable,
        )
        body["triage"].update(Panel(triage_group, title="[blue]BLUE SWARM · TRIAGE & AST PATCH[/blue]", border_style="blue"))
        score = self._value(verification, "score", default=0)
        verify_group = Group(
            self._panel_table(
                [("Score", self._value(verification, "score", default=0)),
                 ("Vaccinated", self._value(verification, "vaccinated_count", default=0)),
                 ("Latency", self._value(verification, "latency", "before_after", default="—")),
                 ("Verdict", self._value(verification, "status", "verdict", default="pending"))]
            ),
            self._gauge(float(score) if isinstance(score, (int, float)) else 0),
        )
        body["verify"].update(Panel(verify_group, title="[green]VERIFICATION · IMMUNITY SCORE[/green]", border_style="green"))
        elapsed = time.perf_counter() - self.started_at
        timeline = "  ".join(f"{item}" for item in self.state["timeline"][-6:]) or "baseline"
        layout["footer"].update(Panel(Text(f"Phase: {self.phase}  ·  {timeline}  ·  runtime {elapsed:.2f}s"), box=box.MINIMAL, border_style="bright_black"))
        return layout

    def _update(self, key: str, data: dict[str, Any], phase: str) -> None:
        self.phase = phase
        self.state[key].update(data)
        self.state["timeline"].append(phase)
        if self.is_tty:
            self._ensure_live()
            self._refresh()
        else:
            title = {"attack": "RED SWARM", "telemetry": "TARGET TELEMETRY", "triage": "BLUE SWARM TRIAGE", "patch": "PATCH", "verification": "VERIFICATION"}.get(key, phase)
            rows = [(str(k).replace("_", " ").title(), v) for k, v in data.items() if v not in (None, "")]
            self.console.print(Panel(self._panel_table(rows or [("Status", phase)]), title=f"[bold]{title} · {phase}[/bold]", border_style="bright_blue"))

    def update_attack(self, **data: Any) -> None:
        self._update("attack", data, "Attack")

    def update_telemetry(self, **data: Any) -> None:
        self._update("telemetry", data, "Telemetry")

    def update_triage(self, **data: Any) -> None:
        self._update("triage", data, "Triage")

    def update_patch(self, **data: Any) -> None:
        self._update("patch", data, "Heal")

    def update_verification(self, **data: Any) -> None:
        self._update("verification", data, "Verify")

    def render_summary(self, summary: Any) -> None:
        payload = summary.model_dump() if hasattr(summary, "model_dump") else dict(summary)
        self.state["verification"].update(payload)
        self.phase = "Complete"
        if self._live is not None:
            self._refresh()
            self._live.stop()
            self._live = None
        else:
            self.console.print(Panel(self._panel_table([("Immunity score", payload.get("score", 0)), ("Incidents verified", f"{payload.get('incidents_verified', 0)}/{payload.get('incidents_total', 0)}"), ("Vaccinated", payload.get("vaccinated_count", 0))]), title="[bold green]OMNIRESILIENCE · RUN COMPLETE[/bold green]", border_style="green"))
