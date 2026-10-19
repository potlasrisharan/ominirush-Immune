from __future__ import annotations

import ast
import re
from pathlib import Path

from omniimmune.models import BugClass, IncidentSignature, TelemetrySample


_TRACEBACK_FRAME = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<function>[A-Za-z_][A-Za-z0-9_]*)'
)
_EXCEPTION = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))(?::\s*(?P<message>.*))?$"
)


class TriageEngine:
    def __init__(self, target_file: Path | str = "target_app/app.py") -> None:
        self.target_file = Path(target_file)

    @staticmethod
    def _telemetry(telemetry: list[TelemetrySample] | None) -> list[TelemetrySample]:
        return list(telemetry or [])

    def diagnose_from_exception(
        self,
        stderr_logs: str,
        endpoint: str,
        dna_id: str = "",
        telemetry: list[TelemetrySample] | None = None,
    ) -> IncidentSignature:
        lines = stderr_logs.splitlines()
        exception_type = "UnhandledException"
        message_token: str | None = None
        for line in reversed(lines):
            match = _EXCEPTION.match(line.strip())
            if match:
                exception_type = match.group("type")
                message = (match.group("message") or "").strip()
                message_token = message.split()[0] if message else None
                break

        culprit_file = str(self.target_file)
        culprit_line: int | None = None
        culprit_function: str | None = None
        # The last traceback frame is the point where the exception escaped.
        frames = list(_TRACEBACK_FRAME.finditer(stderr_logs))
        if frames:
            frame = frames[-1]
            culprit_file = frame.group("file")
            culprit_line = int(frame.group("line"))
            culprit_function = frame.group("function")

        return IncidentSignature(
            sig_id=IncidentSignature.make_sig_id(BugClass.PANIC, endpoint, exception_type),
            bug_class=BugClass.PANIC,
            failure_kind="exception",
            endpoint=endpoint,
            exception_type=exception_type,
            message_token=message_token,
            culprit_file=culprit_file,
            culprit_function=culprit_function,
            culprit_line=culprit_line,
            stack_trace=stderr_logs,
            telemetry=self._telemetry(telemetry),
            dna_id=dna_id,
        )

    def diagnose_from_sla_breach(
        self,
        endpoint: str,
        p99_ms: float,
        dna_id: str = "",
        telemetry: list[TelemetrySample] | None = None,
    ) -> IncidentSignature:
        source = self.target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.target_file))
        culprit_function: str | None = None
        culprit_line: int | None = None
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            has_blocking_lock = any(
                isinstance(node, ast.With)
                and any(
                    (
                        isinstance(item.context_expr, ast.Name)
                        and item.context_expr.id == "_inventory_lock"
                    )
                    or (
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Attribute)
                        and isinstance(item.context_expr.func.value, ast.Name)
                        and item.context_expr.func.value.value == "threading"
                        and item.context_expr.func.attr.value == "Lock"
                    )
                    for item in node.items
                )
                for node in ast.walk(function)
            )
            if has_blocking_lock:
                culprit_function = function.name
                culprit_line = function.lineno
                break

        return IncidentSignature(
            sig_id=IncidentSignature.make_sig_id(BugClass.STARVATION, endpoint, None),
            bug_class=BugClass.STARVATION,
            failure_kind="sla_breach",
            endpoint=endpoint,
            culprit_file=str(self.target_file),
            culprit_function=culprit_function,
            culprit_line=culprit_line,
            stack_trace=f"SLA breach: p99 latency was {p99_ms:.1f}ms",
            telemetry=self._telemetry(telemetry),
            dna_id=dna_id,
        )

    def diagnose_from_timeout(
        self,
        endpoint: str,
        dna_id: str = "",
        telemetry: list[TelemetrySample] | None = None,
    ) -> IncidentSignature:
        source = self.target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.target_file))
        culprit_function: str | None = None
        culprit_line: int | None = None
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            has_uncapped_client = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx"
                and node.func.attr == "AsyncClient"
                and any(
                    keyword.arg == "timeout"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is None
                    for keyword in node.keywords
                )
                for node in ast.walk(function)
            )
            if has_uncapped_client:
                culprit_function = function.name
                culprit_line = function.lineno
                break

        return IncidentSignature(
            sig_id=IncidentSignature.make_sig_id(BugClass.UPSTREAM_TIMEOUT, endpoint, None),
            bug_class=BugClass.UPSTREAM_TIMEOUT,
            failure_kind="hang",
            endpoint=endpoint,
            exception_type="TimeoutException",
            culprit_file=str(self.target_file),
            culprit_function=culprit_function,
            culprit_line=culprit_line,
            stack_trace="Uncapped upstream timeout detected: httpx client initialized without timeout bounds",
            telemetry=self._telemetry(telemetry),
            dna_id=dna_id,
        )
