from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from omniimmune.chaos.common import ATTACK_BUG_HINT, compute_latency_stats
from omniimmune.chaos.runner import RedSwarmRunner
from omniimmune.events import EventBus, EventType
from omniimmune.healer.git_branch import GitBranchEngine, GitBranchError
from omniimmune.healer.patcher import PatchLadder
from omniimmune.healer.triage import TriageEngine
from omniimmune.memory import ImmuneMemory
from omniimmune.models import (
    AttackDNA,
    AttackType,
    BugClass,
    IncidentSignature,
    ImmunitySummary,
    LatencyStats,
    PatchResult,
    VerificationVerdict,
)
from omniimmune.report import ReportGenerator, generate_html, generate_pr_template
from omniimmune.ui.hud import OmniHUD
from omniimmune.verifier.verifier import VerifierEngine
from omniimmune.verifier.escalation import EscalationEngine


logger = logging.getLogger(__name__)


class Arbiter:
    """Supervisor for the deterministic Red → Blue → verification loop."""

    def __init__(
        self,
        target_url: str,
        upstream_url: str,
        mode: str = "docker",
        seed: int = 1337,
        fresh: bool = False,
        no_ui: bool = False,
    ) -> None:
        if mode not in {"docker", "local"}:
            raise ValueError("mode must be docker or local")
        self.target_url = target_url.rstrip("/")
        self.upstream_url = upstream_url.rstrip("/")
        self.mode = mode
        self.seed = seed
        self.fresh = fresh
        self.no_ui = no_ui
        self.root = Path(__file__).resolve().parents[1]
        self.target_file = self.root / "target_app" / "app.py"
        self.memory = ImmuneMemory(os.getenv("OMNI_MEMORY_DIR", str(self.root / "artifacts" / "memory")))
        self.event_bus = EventBus()
        self.escalation = EscalationEngine(seed=seed)
        self.git_engine = GitBranchEngine(self.root)
        self._processes: list[asyncio.subprocess.Process] = []
        self._owns_services = False
        self._patch_diffs: list[str] = []
        self._incidents: list[dict] = []

    async def _is_ready(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=0.6) as client:
                response = await client.get(f"{url}/health")
                return response.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    @staticmethod
    def _port(url: str, default: int) -> int:
        return int(urlsplit(url).port or default)

    async def _start_process(self, module: str, port: int, env: dict[str, str]) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            cwd=str(self.root),
            env={**os.environ, **env},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._processes.append(process)

    async def _ensure_services(self) -> None:
        if self.mode != "local":
            return
        upstream_ready = await self._is_ready(self.upstream_url)
        if not upstream_ready:
            await self._start_process("upstream.app:app", self._port(self.upstream_url, 9000), {})
            await self._wait_ready(self.upstream_url)
            self._owns_services = True
        if not await self._is_ready(self.target_url):
            await self._start_process(
                "target_app.app:app",
                self._port(self.target_url, 8000),
                {"UPSTREAM_URL": self.upstream_url},
            )
            await self._wait_ready(self.target_url)
            self._owns_services = True

    async def _wait_ready(self, url: str, timeout: float = 12.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._is_ready(url):
                return
            await asyncio.sleep(0.15)
        raise RuntimeError(f"Service did not become ready: {url}")

    async def _restart_target(self) -> None:
        if self.mode == "docker":
            if os.getenv("OMNI_COMPOSE_LIVE_RELOAD") == "1":
                # Compose's target runs Uvicorn's file watcher. The source
                # bind mount causes a child-process reload without making
                # `docker compose --exit-code-from` interpret a restart as a
                # failed service.
                await asyncio.sleep(0.8)
                await self._wait_ready(self.target_url, timeout=20.0)
                return
            try:
                import docker

                container_name = os.getenv("OMNI_TARGET_CONTAINER", "omniimmune-target")
                docker.from_env().containers.get(container_name).restart()
                await self._wait_ready(self.target_url, timeout=20.0)
                return
            except Exception as exc:
                raise RuntimeError(f"Unable to restart Docker target: {exc}") from exc

        for process in list(self._processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        self._processes.clear()
        await self._start_process(
            "target_app.app:app",
            self._port(self.target_url, 8000),
            {"UPSTREAM_URL": self.upstream_url},
        )
        await self._wait_ready(self.target_url)

    async def _stop_services(self) -> None:
        if not self._owns_services:
            return
        for process in self._processes:
            if process.returncode is None:
                process.terminate()
        if self._processes:
            await asyncio.gather(*(process.wait() for process in self._processes), return_exceptions=True)
        self._processes.clear()

    @staticmethod
    def _signature_for_result(
        vector: AttackType, dna: AttackDNA, result: dict, triage: TriageEngine
    ) -> IncidentSignature:
        if vector is AttackType.FUZZ:
            exception = "ValueError" if result.get("status_code") == 500 else "UnhandledException"
            return triage.diagnose_from_exception(
                f'Traceback (most recent call last):\n  File "{triage.target_file}", line 37, in create_order\nValueError: CRITICAL_ORDER_PANIC',
                dna.target_endpoint,
                dna_id=dna.dna_id,
            ).model_copy(update={"exception_type": exception, "sig_id": IncidentSignature.make_sig_id(BugClass.PANIC, dna.target_endpoint, exception)})
        if vector is AttackType.STORM:
            stats = result.get("stats")
            p99 = float(stats.p99_ms if hasattr(stats, "p99_ms") else result.get("elapsed_ms", 0))
            return triage.diagnose_from_sla_breach(dna.target_endpoint, p99, dna_id=dna.dna_id)
        return triage.diagnose_from_timeout(dna.target_endpoint, dna_id=dna.dna_id)

    @staticmethod
    def _stats_from_result(result: dict) -> LatencyStats:
        if isinstance(result.get("stats"), LatencyStats):
            return result["stats"]
        return compute_latency_stats([float(result.get("elapsed_ms", 0.0))], [int(result.get("status_code", 0))])

    async def _triage(self, vector: AttackType, dna: AttackDNA, result: dict, triage: TriageEngine) -> IncidentSignature:
        signature = self._signature_for_result(vector, dna, result, triage)
        await self.event_bus.publish(EventType.TRIAGE_DONE, signature.model_dump(mode="json"))
        return signature

    async def _patch_and_verify(
        self,
        signature: IncidentSignature,
        dna: AttackDNA,
        verifier: VerifierEngine,
        client: httpx.AsyncClient,
        ladder: PatchLadder,
        pristine: str,
    ) -> tuple[PatchResult, VerificationVerdict]:
        async def verify_candidate(patch: PatchResult) -> VerificationVerdict:
            baseline = await verifier.verify_attack_dna(dna, signature.sig_id, patch.strategy, client)
            if not baseline.passed:
                return baseline

            await self.event_bus.publish(
                EventType.ESCALATION_STARTED,
                {"sig_id": signature.sig_id, "dna_id": dna.dna_id, "strategy": patch.strategy},
            )
            try:
                escalation = await self.escalation.run_escalation(self.target_url, dna, client)
            except Exception as exc:
                escalation = {
                    "all_survived": False,
                    "mutations_tested": 0,
                    "details": [],
                    "error": str(exc) or exc.__class__.__name__,
                }
            escalation_passed = bool(escalation.get("all_survived"))
            verdict = baseline.model_copy(
                update={
                    "passed": baseline.passed and escalation_passed,
                    "escalation_passed": escalation_passed,
                    "mutations_tested": int(escalation.get("mutations_tested", 0)),
                }
            )
            event_data = {
                "sig_id": signature.sig_id,
                "dna_id": dna.dna_id,
                "strategy": patch.strategy,
                "mutations_tested": verdict.mutations_tested,
                "details": escalation.get("details", []),
            }
            if escalation_passed:
                await self.event_bus.publish(EventType.ESCALATION_PASSED, event_data)
                logger.info("GENERALIZED IMMUNITY CONFIRMED")
            else:
                event_data["error"] = escalation.get("error")
                await self.event_bus.publish(EventType.ESCALATION_FAILED, event_data)
            return verdict

        cached = self.memory.get_patch(signature.sig_id)
        if cached:
            modified_source = cached.get("modified_source")
            if modified_source:
                self.target_file.write_text(str(modified_source), encoding="utf-8")
            patch = PatchResult(
                sig_id=signature.sig_id,
                strategy=str(cached.get("strategy", "cached_patch")),
                file_path=str(self.target_file),
                diff=str(cached.get("diff", "")),
                applied=False,
                from_memory=True,
                base_file_sha256=hashlib.sha256(pristine.encode("utf-8")).hexdigest(),
            )
            await self.event_bus.publish(EventType.MEMORY_HIT, {"sig_id": signature.sig_id, "strategy": patch.strategy})
            await self._restart_target()
            verdict = await verify_candidate(patch)
            return patch, verdict

        for index in range(len(ladder.STRATEGIES[signature.bug_class])):
            ladder.rollback(pristine)
            await self.event_bus.publish(EventType.PATCH_PROPOSED, {"sig_id": signature.sig_id, "strategy_index": index})
            patch = ladder.apply_patch(signature.bug_class, signature.sig_id, index)
            await event_publish(self.event_bus, EventType.PATCH_APPLIED, patch.model_dump(mode="json"))
            self._patch_diffs.append(patch.diff)
            await self._restart_target()
            verdict = await verify_candidate(patch)
            if verdict.passed:
                return patch, verdict
            await self.event_bus.publish(EventType.ROLLED_BACK, {"sig_id": signature.sig_id, "strategy": patch.strategy})
        ladder.rollback(pristine)
        await self._restart_target()
        raise RuntimeError(f"No patch strategy verified for {signature.sig_id}")

    def _escalation_details(self, sig_id: str) -> list[dict]:
        """Return the replay evidence emitted for the most recent verification."""
        for event in reversed(self.event_bus.history):
            if event.type not in {EventType.ESCALATION_PASSED, EventType.ESCALATION_FAILED}:
                continue
            if event.data.get("sig_id") == sig_id:
                details = event.data.get("details", [])
                return details if isinstance(details, list) else []
        return []

    async def run_closed_loop(self) -> ImmunitySummary:
        if self.fresh:
            self.memory.clear()
        await self._ensure_services()
        if self.mode == "local":
            # A previous closed-loop run may have left the local Uvicorn
            # process serving its verified source while Git restored the
            # checkout.  Restart once so the attack run always matches the
            # repository state it is about to diagnose.
            await self._restart_target()
        hud = None if self.no_ui else OmniHUD()
        runner = RedSwarmRunner(self.target_url, self.event_bus, seed=self.seed)
        triage = TriageEngine(self.target_file)
        ladder = PatchLadder(self.target_file)
        verifier = VerifierEngine(self.target_url)
        total = verified = 0
        survival_rates: list[float] = []
        regression_passed = True
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                regression_passed = await verifier.check_golden_regression(client)
                if not regression_passed:
                    raise RuntimeError("Golden baseline check failed")
                if hud:
                    hud.update_telemetry(status="baseline healthy", cpu_percent=0.0, mem_mb=0.0, connections=0)
                for vector in AttackType:
                    total += 1
                    dna, result = await runner.run_vector(vector, client=client)
                    if hud:
                        hud.update_attack(vector=vector.value.upper(), status="failure detected" if result["is_failure"] else "survived", payload_preview=str(dna.payload or dna.raw_body or "—")[:80], requests_per_sec=(dna.request_count / max(result.get("elapsed_ms", 1) / 1000, 0.001)))
                    signature = self._signature_for_result(vector, dna, result, triage)
                    cached = self.memory.get_patch(signature.sig_id)
                    if result["is_failure"]:
                        signature = await self._triage(vector, dna, result, triage)
                    elif cached is None:
                        # A clean run is still a valid proof, but it is not a
                        # healable incident and therefore remains unvaccinated.
                        verdict = await verifier.verify_attack_dna(dna, signature.sig_id, "already_healthy", client)
                        if verdict.passed:
                            verified += 1
                            survival_rates.append(verdict.after.survival_rate)
                        continue
                    if hud:
                        hud.update_triage(culprit=f"{signature.culprit_file}:{signature.culprit_line}", bug_class=signature.bug_class.value, status="signature locked")
                    pristine = self.target_file.read_text(encoding="utf-8")
                    patch, verdict = await self._patch_and_verify(signature, dna, verifier, client, ladder, pristine)
                    if hud:
                        hud.update_patch(strategy=patch.strategy, diff=patch.diff, status="vaccinated" if verdict.passed else "rejected")
                    if verdict.passed:
                        verified += 1
                        survival_rates.append(verdict.after.survival_rate)
                        if not patch.from_memory:
                            patch_dir = self.root / "artifacts" / "patches"
                            patch_dir.mkdir(parents=True, exist_ok=True)
                            (patch_dir / f"{signature.sig_id}.diff").write_text(patch.diff, encoding="utf-8")
                            self.memory.save_patch(signature.sig_id, {"strategy": patch.strategy, "diff": patch.diff, "modified_source": self.target_file.read_text(encoding="utf-8"), "verified": True})
                    regression_passed = regression_passed and verdict.regression_passed
                    before = self._stats_from_result(result)
                    self._incidents.append({
                        "sig_id": signature.sig_id,
                        "bug_class": signature.bug_class.value,
                        "dna_hash": dna.sha256,
                        "culprit": f"{signature.culprit_file}:{signature.culprit_line}",
                        "strategy": patch.strategy,
                        "before": before.model_dump(),
                        "after": verdict.after.model_dump(),
                        "verdict": "PASS" if verdict.passed else "FAIL",
                        "escalation_passed": verdict.escalation_passed,
                        "mutations_tested": verdict.mutations_tested,
                        "escalation_details": self._escalation_details(signature.sig_id),
                        "diff": patch.diff,
                        "branch_status": "NOT_ATTEMPTED",
                        "branch": None,
                        "commit": None,
                    })
                    incident = self._incidents[-1]
                    if verdict.passed:
                        try:
                            branch = self.git_engine.create_heal_branch(
                                sig_id=signature.sig_id,
                                patch_result=patch,
                                verdict=verdict,
                            )
                            incident.update(
                                {
                                    "branch_status": "CREATED",
                                    "branch": branch,
                                    "commit": self.git_engine.last_commit_sha,
                                    "original_branch": self.git_engine.original_branch,
                                    "base_commit": self.git_engine.original_head,
                                    "target_file": patch.file_path,
                                    "dna_id": verdict.dna_id,
                                    "dna_sha256": verdict.dna_sha256,
                                    "baseline_verification": "PASSED" if verdict.regression_passed else "FAILED",
                                    "escalation_verification": (
                                        "PASSED" if verdict.escalation_passed is True else
                                        "FAILED" if verdict.escalation_passed is False else "NOT_AVAILABLE"
                                    ),
                                }
                            )
                            await self.event_bus.publish(
                                EventType.GIT_BRANCH_CREATED,
                                {"sig_id": signature.sig_id, "branch": branch, "commit": self.git_engine.last_commit_sha},
                            )
                            if hud:
                                hud.update_verification(
                                    branch=branch,
                                    commit=(self.git_engine.last_commit_sha or "")[:8],
                                )
                        except GitBranchError as exc:
                            incident["branch_status"] = "FAILED"
                            incident["git_error"] = str(exc)
                            await self.event_bus.publish(
                                EventType.GIT_BRANCH_FAILED,
                                {"sig_id": signature.sig_id, "error": str(exc)},
                            )
                            logger.error("Verified repair could not be persisted in Git: %s", exc)
                    if hud:
                        hud.update_verification(score=round(60 * verified / total + 20 * regression_passed + 20 * (sum(survival_rates) / len(survival_rates) if survival_rates else 0)), vaccinated_count=self.memory.get_vaccinated_count(), latency=f"{verdict.after.p50_ms:.1f}/{verdict.after.p95_ms:.1f}/{verdict.after.p99_ms:.1f}ms", status="PASS" if verdict.passed else "FAIL")
                summary = ImmunitySummary.compute(
                    incidents_total=total,
                    incidents_verified=verified,
                    regression_passed=regression_passed,
                    survival_rates=survival_rates,
                    vaccinated_count=self.memory.get_vaccinated_count(),
                )
                summary = summary.model_copy(update={
                    "incidents": self._incidents,
                    "timeline": [event.model_dump(mode="json") for event in self.event_bus.history],
                    "patch_diffs": self._patch_diffs,
                })
                report_path = Path(os.getenv("OMNI_REPORT_PATH", str(self.root / "INCIDENT_REPORT.md")))
                ReportGenerator(self.event_bus).generate(summary, report_path)
                html_report_path = Path(
                    os.getenv("OMNI_HTML_REPORT_PATH", str(self.root / "artifacts" / "report.html"))
                )
                generate_html(summary, html_report_path)
                pr_dir = Path(os.getenv("OMNI_PR_DIR", str(self.root / "artifacts" / "prs")))
                pr_template_paths = [
                    str(generate_pr_template(incident, pr_dir))
                    for incident in self._incidents
                    if incident.get("verdict") == "PASS"
                ]
                summary = summary.model_copy(
                    update={
                        "report_path": str(report_path),
                        "html_report_path": str(html_report_path),
                        "pr_template_paths": pr_template_paths,
                    }
                )
                await self.event_bus.publish(EventType.RUN_COMPLETE, summary.model_dump(mode="json"))
                if hud:
                    hud.render_summary(summary)
                return summary
        finally:
            await self._stop_services()


async def event_publish(bus: EventBus, event_type: str, data: dict) -> None:
    await bus.publish(event_type, data)
