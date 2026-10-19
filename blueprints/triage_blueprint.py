"""
Triage Engine Blueprint for OmniImmune
Maps failure signals (stack traces, latency SLA breaches, timeouts)
to a deterministic IncidentSignature and culprit AST location.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

from omniimmune.models import BugClass, IncidentSignature, TelemetrySample


class TriageEngine:
    def __init__(self, target_file: Path | str = "target_app/app.py"):
        self.target_file = Path(target_file)

    def diagnose_from_exception(
        self,
        stderr_logs: str,
        endpoint: str,
        dna_id: str = "",
        telemetry: Optional[list[TelemetrySample]] = None,
    ) -> IncidentSignature:
        """
        Parses Python traceback from stderr logs to identify culprit file, line, and exception.
        """
        exception_type = "UnhandledException"
        culprit_file = str(self.target_file)
        culprit_line = None
        culprit_func = None

        # Extract exception type (e.g. ValueError: CRITICAL_ORDER_PANIC...)
        lines = stderr_logs.strip().splitlines()
        for line in reversed(lines):
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*Error|[A-Za-z_][A-Za-z0-9_]*Exception):\s*(.*)", line.strip())
            if match:
                exception_type = match.group(1)
                break

        # Extract last frame in target_app
        frame_regex = re.compile(r'File "([^"]+)", line (\d+), in ([a-zA-Z_][a-zA-Z0-9_]*)')
        for line in reversed(lines):
            fmatch = frame_regex.search(line)
            if fmatch:
                culprit_file = fmatch.group(1)
                culprit_line = int(fmatch.group(2))
                culprit_func = fmatch.group(3)
                break

        sig_id = IncidentSignature.make_sig_id(BugClass.PANIC, endpoint, exception_type)

        return IncidentSignature(
            sig_id=sig_id,
            bug_class=BugClass.PANIC,
            failure_kind="exception",
            endpoint=endpoint,
            exception_type=exception_type,
            culprit_file=culprit_file,
            culprit_function=culprit_func,
            culprit_line=culprit_line,
            stack_trace=stderr_logs,
            telemetry=telemetry or [],
            dna_id=dna_id,
        )

    def diagnose_from_sla_breach(
        self,
        endpoint: str,
        p99_ms: float,
        dna_id: str = "",
        telemetry: Optional[list[TelemetrySample]] = None,
    ) -> IncidentSignature:
        """
        Diagnoses event-loop starvation by performing AST inspection on target_app.
        """
        source_code = self.target_file.read_text(encoding="utf-8")
        parsed_ast = ast.parse(source_code)

        culprit_func = None
        culprit_line = None

        # Look for blocking threading.Lock in async functions
        for node in ast.walk(parsed_ast):
            if isinstance(node, ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.With):
                        culprit_func = node.name
                        culprit_line = node.lineno
                        break

        sig_id = IncidentSignature.make_sig_id(BugClass.STARVATION, endpoint, None)

        return IncidentSignature(
            sig_id=sig_id,
            bug_class=BugClass.STARVATION,
            failure_kind="sla_breach",
            endpoint=endpoint,
            exception_type=None,
            culprit_file=str(self.target_file),
            culprit_function=culprit_func,
            culprit_line=culprit_line,
            stack_trace=f"SLA breach: p99 latency was {p99_ms:.1f}ms",
            telemetry=telemetry or [],
            dna_id=dna_id,
        )

    def diagnose_from_timeout(
        self,
        endpoint: str,
        dna_id: str = "",
        telemetry: Optional[list[TelemetrySample]] = None,
    ) -> IncidentSignature:
        """
        Diagnoses uncapped upstream call by finding httpx calls with timeout=None.
        """
        source_code = self.target_file.read_text(encoding="utf-8")
        parsed_ast = ast.parse(source_code)

        culprit_func = None
        culprit_line = None

        for node in ast.walk(parsed_ast):
            if isinstance(node, ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        # Detect httpx.AsyncClient(timeout=None)
                        for keyword in child.keywords:
                            if keyword.arg == "timeout" and isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                                culprit_func = node.name
                                culprit_line = node.lineno
                                break

        sig_id = IncidentSignature.make_sig_id(BugClass.UPSTREAM_TIMEOUT, endpoint, None)

        return IncidentSignature(
            sig_id=sig_id,
            bug_class=BugClass.UPSTREAM_TIMEOUT,
            failure_kind="hang",
            endpoint=endpoint,
            exception_type="TimeoutException",
            culprit_file=str(self.target_file),
            culprit_function=culprit_func,
            culprit_line=culprit_line,
            stack_trace="Uncapped upstream timeout detected: httpx client initialized without timeout bounds",
            telemetry=telemetry or [],
            dna_id=dna_id,
        )
