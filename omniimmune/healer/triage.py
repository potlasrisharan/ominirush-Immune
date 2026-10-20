from __future__ import annotations

import ast
import re
from pathlib import Path

from omniimmune.models import BugClass, IncidentSignature, TelemetrySample


_TRACEBACK_FRAME = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in '
    r'(?P<function>[A-Za-z_][A-Za-z0-9_]*|<[^>\r\n]+>)'
)
_EXCEPTION = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?::\s*(?P<message>.*))?$"
)


class TriageEngine:
    def __init__(self, target_file: Path | str = "target_app/app.py") -> None:
        self.target_file = Path(target_file)

    def _relative_path(self, value: Path | str) -> str:
        """Return a repository-relative path without leaking host paths."""
        root = Path(__file__).resolve().parents[2]
        path = Path(value)
        relative_target = self.target_file
        if relative_target.is_absolute():
            try:
                relative_target = relative_target.resolve().relative_to(root)
            except ValueError:
                relative_target = Path(relative_target.name)

        if path.is_absolute():
            try:
                return path.resolve().relative_to(root).as_posix()
            except ValueError:
                # Tracebacks from a target process can use a different checkout
                # prefix. Preserve the repository suffix when it is identifiable.
                target_parts = relative_target.parts
                if len(path.parts) >= len(target_parts) and path.parts[-len(target_parts):] == target_parts:
                    return Path(*target_parts).as_posix()
                # Do not expose the host checkout prefix for an unrelated
                # traceback; the basename is enough for a human-readable
                # incident without leaking local filesystem layout.
                return path.name
        return path.as_posix()

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

        culprit_file = self._relative_path(self.target_file)
        culprit_line: int | None = None
        culprit_function: str | None = None
        # The last traceback frame is the point where the exception escaped.
        frames = list(_TRACEBACK_FRAME.finditer(stderr_logs))
        if frames:
            frame = frames[-1]
            culprit_file = self._relative_path(frame.group("file"))
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
            culprit_file=self._relative_path(self.target_file),
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
            culprit_file=self._relative_path(self.target_file),
            culprit_function=culprit_function,
            culprit_line=culprit_line,
            stack_trace="Uncapped upstream timeout detected: httpx client initialized without timeout bounds",
            telemetry=self._telemetry(telemetry),
            dna_id=dna_id,
        )

    def diagnose_from_resource_exhaustion(
        self,
        endpoint: str,
        peak_mem_mb: float = 0.0,
        dna_id: str = "",
        telemetry: list[TelemetrySample] | None = None,
    ) -> IncidentSignature:
        """Identify an endpoint retaining request data in an unbounded cache."""
        source = self.target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.target_file))
        culprit_function: str | None = None
        culprit_line: int | None = None
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            has_cache_write = any(
                (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and "cache" in target.value.id.lower()
                        for target in node.targets
                    )
                )
                or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"append", "update", "setdefault"}
                    and isinstance(node.func.value, ast.Name)
                    and "cache" in node.func.value.id.lower()
                )
                for node in ast.walk(function)
            )
            if has_cache_write:
                culprit_function = function.name
                culprit_line = function.lineno
                break

        return IncidentSignature(
            sig_id=IncidentSignature.make_sig_id(BugClass.RESOURCE_EXHAUSTION, endpoint, None),
            bug_class=BugClass.RESOURCE_EXHAUSTION,
            failure_kind="resource_exhaustion",
            endpoint=endpoint,
            exception_type="MemoryError",
            message_token="cache_growth",
            culprit_file=self._relative_path(self.target_file),
            culprit_function=culprit_function,
            culprit_line=culprit_line,
            stack_trace=f"Unbounded cache growth detected: peak retained memory {peak_mem_mb:.3f}MB",
            telemetry=self._telemetry(telemetry),
            dna_id=dna_id,
        )

    # Additive aliases make the diagnosis convenient for callers that name the
    # observed condition rather than the bug class.
    diagnose_from_memory_exhaustion = diagnose_from_resource_exhaustion
    diagnose_from_leak = diagnose_from_resource_exhaustion
