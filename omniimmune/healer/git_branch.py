from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from omniimmune.models import PatchResult, VerificationVerdict


class GitBranchError(RuntimeError):
    """Raised when a verified repair cannot be persisted safely in Git."""


class GitBranchEngine:
    """Persist one verified repair in an isolated ``heal/`` branch.

    The engine deliberately never checks out a branch and never uses the
    repository's real index.  A temporary index is populated from the captured
    ``HEAD`` tree, the patched file is added to that index, and Git plumbing is
    used to create the audit commit and branch reference.
    """

    _UNSUPPORTED_STATE_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")

    def __init__(self, repo_path: Path | str = ".") -> None:
        self.repo_path = Path(repo_path).expanduser()
        self.last_branch_name: str | None = None
        self.last_commit_sha: str | None = None
        self.last_cleanup_completed: bool | None = None
        self.original_branch: str | None = None
        self.original_head: str | None = None

    @staticmethod
    def sanitize_sig_id(sig_id: str) -> str:
        if not isinstance(sig_id, str):
            raise GitBranchError("sig_id must be a string")
        normalized = unicodedata.normalize("NFKD", sig_id).encode("ascii", "ignore").decode()
        normalized = re.sub(r"[\s/\\]+", "-", normalized.strip())
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized)
        normalized = re.sub(r"\.{2,}", "-", normalized)
        normalized = re.sub(r"-{2,}", "-", normalized)
        normalized = normalized.strip(".-")
        if not normalized:
            raise GitBranchError("sig_id does not contain a usable branch identifier")
        if normalized.endswith(".lock"):
            normalized = normalized[: -len(".lock")].rstrip(".-")
        if not normalized or normalized in {".", ".."}:
            raise GitBranchError("sig_id produces an invalid branch identifier")
        return normalized[:120].rstrip(".-")

    @classmethod
    def branch_name_for(cls, sig_id: str) -> str:
        return f"heal/{cls.sanitize_sig_id(sig_id)}"

    def _run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                args,
                cwd=self._root,
                env=env,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitBranchError(f"Git command failed to start or timed out: {' '.join(args)}: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitBranchError(f"Git command failed ({result.returncode}): {' '.join(args)}: {detail}")
        return result

    def _resolve_repository(self) -> tuple[str, str, str]:
        candidate = self.repo_path.resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise GitBranchError(f"Repository path is not a directory: {candidate}")
        try:
            root_text = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitBranchError(f"Git is unavailable: {exc}") from exc
        if root_text.returncode != 0:
            detail = (root_text.stderr or root_text.stdout).strip()
            raise GitBranchError(f"Not a Git repository: {candidate}: {detail}")
        self._root = Path(root_text.stdout.strip()).resolve()
        branch_result = self._run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            raise GitBranchError("Repository is in detached-HEAD state; refusing automated branching")
        head = self._run(["git", "rev-parse", "HEAD"]).stdout.strip()
        for marker in self._UNSUPPORTED_STATE_MARKERS:
            marker_result = self._run(["git", "rev-parse", "--verify", "-q", marker], check=False)
            if marker_result.returncode == 0:
                raise GitBranchError(f"Repository has unsupported in-progress Git state: {marker}")
        conflicts = self._run(["git", "diff", "--name-only", "--diff-filter=U"], check=False)
        if conflicts.returncode == 0 and conflicts.stdout.strip():
            raise GitBranchError("Repository has unresolved merge conflicts")
        return branch_result.stdout.strip(), head, self._root.as_posix()

    def _target_path(self, file_path: str) -> tuple[Path, str]:
        if not isinstance(file_path, str) or not file_path.strip():
            raise GitBranchError("patch_result.file_path must be a non-empty path")
        supplied = Path(file_path).expanduser()
        target = supplied.resolve() if supplied.is_absolute() else (self._root / supplied).resolve()
        try:
            relative = target.relative_to(self._root)
        except ValueError as exc:
            raise GitBranchError("Patch target must reside inside the Git repository") from exc
        if not target.is_file() or target.is_symlink():
            raise GitBranchError(f"Patch target is not a regular file: {file_path}")
        relative_name = relative.as_posix()
        tracked = self._run(["git", "ls-files", "--error-unmatch", "--", relative_name], check=False)
        if tracked.returncode != 0 or tracked.stdout.strip() != relative_name:
            raise GitBranchError(f"Patch target is not a tracked file: {relative_name}")
        staged = self._run(["git", "diff", "--cached", "--quiet", "--", relative_name], check=False)
        if staged.returncode != 0:
            raise GitBranchError(
                f"Target file has pre-existing staged changes; refusing to overwrite user work: {relative_name}"
            )
        return target, relative_name

    def _commit_message(
        self,
        sig_id: str,
        patch_result: PatchResult,
        verdict: VerificationVerdict,
        original_branch: str,
        original_head: str,
        relative_path: str,
    ) -> str:
        if not verdict.passed:
            raise GitBranchError("Cannot create a repair branch from an unsuccessful verification verdict")
        if not verdict.regression_passed:
            raise GitBranchError("Cannot create a repair branch before golden regression passes")
        dna_sha256 = str(getattr(verdict, "dna_sha256", "") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", dna_sha256):
            raise GitBranchError("Verification verdict does not contain the triggering DNA SHA-256")
        after = verdict.after
        if after is None:
            raise GitBranchError("Verification verdict is missing post-heal metrics")
        for name in ("survival_rate", "p99_ms"):
            value = getattr(after, name, None)
            if value is None or not math.isfinite(float(value)):
                raise GitBranchError(f"Verification verdict is missing required post-heal metric: {name}")
        before_error_rate = float(verdict.before.error_rate) if verdict.before is not None else 1.0
        if not math.isfinite(before_error_rate):
            raise GitBranchError("Verification verdict contains an invalid pre-heal error rate")
        escalation = getattr(verdict, "escalation_passed", None)
        escalation_status = "PASSED" if escalation is True else "FAILED" if escalation is False else "NOT_AVAILABLE"
        baseline_status = "PASSED" if verdict.regression_passed else "FAILED"
        return "\n".join(
            [
                f"fix(resilience): auto-heal {sig_id} using {patch_result.strategy}",
                "",
                f"- Signature ID: {sig_id}",
                f"- Triggering DNA: {verdict.dna_id}",
                f"- Triggering DNA SHA-256: {dna_sha256}",
                f"- Original Branch: {original_branch}",
                f"- Base Commit: {original_head}",
                f"- Target File: {relative_path}",
                f"- Patch Strategy: {patch_result.strategy}",
                f"- Pre-heal Error Rate: {before_error_rate:.1%}",
                f"- Post-heal Survival Rate: {float(after.survival_rate):.1%}",
                f"- Latency p99: {float(after.p99_ms):.1f}ms",
                f"- Baseline Verification: {baseline_status}",
                f"- Escalation Verification: {escalation_status}",
                f"- Golden Regression: {baseline_status}",
                "- Verified by: OmniImmune Engine",
                "",
            ]
        )

    def _restore_target(self, relative_path: str, target: Path, original_head: str) -> None:
        content = self._run(["git", "show", f"{original_head}:{relative_path}"]).stdout
        target.write_text(content, encoding="utf-8")

    def _checked_out_branches(self) -> set[str]:
        """Return local branch names checked out in any worktree."""
        worktrees = self._run(["git", "worktree", "list", "--porcelain"]).stdout
        checked_out: set[str] = set()
        for line in worktrees.splitlines():
            if line.startswith("branch refs/heads/"):
                checked_out.add(line.removeprefix("branch refs/heads/"))
        return checked_out

    def _delete_branch(self, branch_name: str, checked_out: set[str] | None = None) -> bool:
        """Delete a local branch unless it is checked out in a worktree."""
        checked_out = self._checked_out_branches() if checked_out is None else checked_out
        if branch_name in checked_out:
            return False
        result = self._run(["git", "branch", "-D", "--", branch_name], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitBranchError(f"Unable to delete repair branch {branch_name}: {detail}")
        return True

    def cleanup_heal_branches(self, prefix: str = "heal/sig-") -> list[str]:
        """Delete matching local repair branches that are not checked out.

        Branches checked out in the current worktree or any linked worktree are
        left untouched.  An empty match set is a successful no-op.
        """
        self._resolve_repository()
        branches = self._run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
        ).stdout.splitlines()
        matching = [branch for branch in branches if branch.startswith(prefix)]
        if not matching:
            return []

        checked_out = self._checked_out_branches()
        deleted: list[str] = []
        for branch_name in matching:
            if self._delete_branch(branch_name, checked_out):
                deleted.append(branch_name)
        return deleted

    def create_heal_branch(
        self,
        sig_id: str,
        patch_result: PatchResult,
        verdict: VerificationVerdict,
        *,
        overwrite: bool = False,
    ) -> str:
        self.last_branch_name = None
        self.last_commit_sha = None
        self.last_cleanup_completed = None
        original_branch, original_head, _ = self._resolve_repository()
        self.original_branch = original_branch
        self.original_head = original_head
        branch_name = self.branch_name_for(sig_id)
        ref_name = f"refs/heads/{branch_name}"
        ref_check = self._run(["git", "show-ref", "--verify", "--quiet", ref_name], check=False)
        branch_exists = ref_check.returncode == 0
        if branch_exists and not overwrite:
            raise GitBranchError(f"Repair branch already exists; refusing to overwrite: {branch_name}")
        check_format = self._run(["git", "check-ref-format", "--branch", branch_name], check=False)
        if check_format.returncode != 0:
            detail = (check_format.stderr or check_format.stdout).strip()
            raise GitBranchError(f"Invalid generated Git branch name {branch_name!r}: {detail}")
        target, relative_path = self._target_path(patch_result.file_path)
        current_content = target.read_bytes()
        head_content = self._run(["git", "show", f"{original_head}:{relative_path}"]).stdout.encode()
        if current_content == head_content:
            raise GitBranchError("Patch target has no working-tree change to persist")
        if patch_result.base_file_sha256:
            expected_base = hashlib.sha256(head_content).hexdigest()
            if patch_result.base_file_sha256 != expected_base:
                raise GitBranchError(
                    "Patch was generated from a different base file; refusing to mix it with user changes"
                )
        message = self._commit_message(
            sig_id,
            patch_result,
            verdict,
            original_branch,
            original_head,
            relative_path,
        )

        temporary_index: str | None = None
        branch_created = False
        try:
            with tempfile.NamedTemporaryFile(prefix="omniimmune-index-", delete=False) as index_file:
                temporary_index = index_file.name
            os.unlink(temporary_index)
            isolated_env = {**os.environ, "GIT_INDEX_FILE": temporary_index}
            self._run(["git", "read-tree", original_head], env=isolated_env)
            self._run(["git", "add", "--", relative_path], env=isolated_env)
            tree = self._run(["git", "write-tree"], env=isolated_env).stdout.strip()
            commit = self._run(
                ["git", "commit-tree", tree, "-p", original_head],
                env=isolated_env,
                input_text=message,
            ).stdout.strip()
            if branch_exists:
                if not self._delete_branch(branch_name):
                    raise GitBranchError(
                        f"Repair branch is checked out; refusing to overwrite: {branch_name}"
                    )
            update = self._run(
                ["git", "update-ref", ref_name, commit, "0" * 40],
                check=False,
            )
            if update.returncode != 0:
                existing = self._run(["git", "show-ref", "--verify", "--quiet", ref_name], check=False)
                if existing.returncode == 0:
                    raise GitBranchError(
                        f"Repair branch appeared during creation; refusing to overwrite: {branch_name}"
                    )
                detail = (update.stderr or update.stdout).strip()
                raise GitBranchError(
                    f"Unable to create repair branch {branch_name}: {detail or 'reference update failed'}"
                )
            branch_created = True
            self.last_branch_name = branch_name
            self.last_commit_sha = commit
            self._restore_target(relative_path, target, original_head)
            self.last_cleanup_completed = True
            return branch_name
        except GitBranchError:
            if branch_created:
                self.last_cleanup_completed = False
            raise
        except Exception as exc:
            if branch_created:
                self.last_cleanup_completed = False
                raise GitBranchError(
                    f"Repair branch {branch_name} was created ({self.last_commit_sha}) but cleanup failed: {exc}"
                ) from exc
            raise GitBranchError(f"Unable to create repair branch {branch_name}: {exc}") from exc
        finally:
            if temporary_index is not None:
                try:
                    os.unlink(temporary_index)
                except FileNotFoundError:
                    pass
