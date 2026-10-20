from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from omniimmune.healer.git_branch import GitBranchEngine, GitBranchError
from omniimmune.models import LatencyStats, PatchResult, VerificationVerdict


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "OmniImmune Test")
    git(repo, "config", "user.email", "omniimmune-test@example.invalid")
    target = repo / "target_app.py"
    target.write_text("return 'healthy'\n", encoding="utf-8")
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "target_app.py", "README.txt")
    git(repo, "commit", "-m", "base")
    return repo, target, git(repo, "rev-parse", "HEAD")


def verified_verdict(*, dna_sha256: str = "a" * 64, passed: bool = True) -> VerificationVerdict:
    return VerificationVerdict(
        sig_id="sig-order-panic-01",
        dna_id="dna-trigger-01",
        dna_sha256=dna_sha256,
        passed=passed,
        before=None,
        after=LatencyStats(
            p50_ms=10.0,
            p95_ms=20.0,
            p99_ms=30.25,
            error_rate=0.0,
            survival_rate=1.0,
            peak_mem_mb=12.0,
        ),
        regression_passed=True,
        escalation_passed=True,
    )


def make_patch(target: Path, original: str, modified: str) -> PatchResult:
    target.write_text(modified, encoding="utf-8")
    return PatchResult(
        sig_id="sig-order-panic-01",
        strategy="input_guard",
        file_path=str(target),
        diff="-return 'healthy'\n+return 'contained'\n",
        applied=True,
        base_file_sha256=hashlib.sha256(original.encode()).hexdigest(),
    )


def test_verified_patch_creates_isolated_branch_and_restores_original_worktree(tmp_path: Path) -> None:
    repo, target, base = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    patch = make_patch(target, original, "return 'contained'\n")
    (repo / "README.txt").write_text("user unstaged\n", encoding="utf-8")
    (repo / "staged.txt").write_text("user staged\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")

    engine = GitBranchEngine(repo)
    branch = engine.create_heal_branch("sig-order-panic-01", patch, verified_verdict())

    assert branch == "heal/sig-order-panic-01"
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base
    assert target.read_text(encoding="utf-8") == original
    assert git(repo, "diff", "--", "README.txt")
    assert git(repo, "diff", "--cached", "--", "staged.txt")
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"

    repair_commit = git(repo, "rev-parse", branch)
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", repair_commit) == "target_app.py"
    assert git(repo, "show", f"{branch}:target_app.py") == "return 'contained'"
    message = git(repo, "log", "-1", "--format=%B", branch)
    assert "Signature ID: sig-order-panic-01" in message
    assert "Triggering DNA: dna-trigger-01" in message
    assert f"Triggering DNA SHA-256: {'a' * 64}" in message
    assert "Pre-heal Error Rate: 100.0%" in message
    assert "Post-heal Survival Rate: 100.0%" in message
    assert "Latency p99: 30.2ms" in message
    assert "Baseline Verification: PASSED" in message
    assert "Escalation Verification: PASSED" in message
    assert "Verified by: OmniImmune Engine" in message


def test_branch_commit_supplies_identity_without_git_config(tmp_path: Path) -> None:
    repo, target, _ = make_repo(tmp_path)
    git(repo, "config", "--unset", "user.name", check=False)
    git(repo, "config", "--unset", "user.email", check=False)
    original = target.read_text(encoding="utf-8")
    patch = make_patch(target, original, "return 'contained'\n")

    branch = GitBranchEngine(repo).create_heal_branch(
        "sig-order-panic-identity", patch, verified_verdict()
    )

    assert git(repo, "show", "-s", "--format=%an <%ae>", branch) == (
        "OmniImmune Engine <omniimmune-engine@localhost>"
    )


def test_branch_name_sanitization_is_deterministic_and_validated() -> None:
    assert GitBranchEngine.branch_name_for("sig-order-panic-01") == "heal/sig-order-panic-01"
    assert GitBranchEngine.branch_name_for("sig/order panic #01") == "heal/sig-order-panic-01"
    assert GitBranchEngine.branch_name_for("sig..attack") == "heal/sig-attack"
    assert GitBranchEngine.branch_name_for("../main") == "heal/main"
    with pytest.raises(GitBranchError):
        GitBranchEngine.branch_name_for("")


def test_existing_branch_and_failed_verification_are_safe(tmp_path: Path) -> None:
    repo, target, _ = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    patch = make_patch(target, original, "return 'contained'\n")
    engine = GitBranchEngine(repo)
    engine.create_heal_branch("sig-order-panic-01", patch, verified_verdict())
    existing = git(repo, "rev-parse", "heal/sig-order-panic-01")
    target.write_text("different user work\n", encoding="utf-8")
    with pytest.raises(GitBranchError, match="already exists"):
        engine.create_heal_branch("sig-order-panic-01", patch, verified_verdict())
    assert git(repo, "rev-parse", "heal/sig-order-panic-01") == existing
    assert target.read_text(encoding="utf-8") == "different user work\n"

    with pytest.raises(GitBranchError, match="unsuccessful"):
        engine.create_heal_branch("sig-failed", patch, verified_verdict(passed=False))
    assert git(repo, "show-ref", "--verify", "--quiet", "refs/heads/heal/sig-failed", check=False) == ""


def test_cleanup_removes_matching_heal_branches_without_touching_main(tmp_path: Path) -> None:
    repo, _, base = make_repo(tmp_path)
    git(repo, "branch", "heal/sig-test")
    git(repo, "branch", "heal/sig-test-two")
    git(repo, "branch", "keep/me")

    deleted = GitBranchEngine(repo).cleanup_heal_branches()

    assert deleted == ["heal/sig-test", "heal/sig-test-two"]
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "show-ref", "--verify", "--quiet", "refs/heads/heal/sig-test", check=False) == ""
    assert git(repo, "show-ref", "--verify", "--quiet", "refs/heads/heal/sig-test-two", check=False) == ""
    assert git(repo, "rev-parse", "keep/me") == base
    assert GitBranchEngine(repo).cleanup_heal_branches() == []


def test_cleanup_skips_checked_out_heal_branch(tmp_path: Path) -> None:
    repo, _, _ = make_repo(tmp_path)
    git(repo, "branch", "heal/sig-test")
    git(repo, "switch", "heal/sig-test")

    assert GitBranchEngine(repo).cleanup_heal_branches() == []
    assert git(repo, "branch", "--show-current") == "heal/sig-test"


def test_overwrite_replaces_existing_heal_branch(tmp_path: Path) -> None:
    repo, target, _ = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    engine = GitBranchEngine(repo)

    first_patch = make_patch(target, original, "return 'first'\n")
    engine.create_heal_branch("sig-overwrite", first_patch, verified_verdict())
    first_commit = git(repo, "rev-parse", "heal/sig-overwrite")

    second_patch = make_patch(target, original, "return 'second'\n")
    branch = engine.create_heal_branch("sig-overwrite", second_patch, verified_verdict(), overwrite=True)

    assert branch == "heal/sig-overwrite"
    assert git(repo, "rev-parse", branch) != first_commit
    assert git(repo, "show", f"{branch}:target_app.py") == "return 'second'"
    assert git(repo, "branch", "--show-current") == "main"


def test_invalid_target_and_missing_post_metrics_are_rejected(tmp_path: Path) -> None:
    repo, target, _ = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    target.write_text("return 'contained'\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    patch = PatchResult(
        sig_id="sig-invalid",
        strategy="input_guard",
        file_path=str(outside),
        diff="",
        applied=True,
        base_file_sha256=hashlib.sha256(original.encode()).hexdigest(),
    )
    with pytest.raises(GitBranchError, match="inside the Git repository"):
        GitBranchEngine(repo).create_heal_branch("sig-invalid", patch, verified_verdict())

    incomplete = verified_verdict()
    incomplete.after = None  # type: ignore[assignment]
    valid_patch = make_patch(target, original, "return 'contained-again'\n")
    with pytest.raises(GitBranchError, match="post-heal metrics"):
        GitBranchEngine(repo).create_heal_branch("sig-incomplete", valid_patch, incomplete)


def test_symlink_target_is_rejected(tmp_path: Path) -> None:
    repo, target, _ = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    link = repo / "target-link.py"
    link.symlink_to(target.name)
    patch = PatchResult(
        sig_id="sig-link",
        strategy="input_guard",
        file_path="target-link.py",
        diff="",
        applied=True,
        base_file_sha256=hashlib.sha256(original.encode()).hexdigest(),
    )
    with pytest.raises(GitBranchError, match="symbolic link"):
        GitBranchEngine(repo).create_heal_branch("sig-link", patch, verified_verdict())


def test_failed_branch_persistence_restores_patched_target(tmp_path: Path, monkeypatch) -> None:
    repo, target, _ = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    patch = make_patch(target, original, "return 'contained'\n")
    engine = GitBranchEngine(repo)
    original_run = engine._run

    def fail_update_ref(args, **kwargs):
        if args[:2] == ["git", "update-ref"]:
            raise GitBranchError("simulated ref failure")
        return original_run(args, **kwargs)

    monkeypatch.setattr(engine, "_run", fail_update_ref)
    with pytest.raises(GitBranchError, match="simulated ref failure"):
        engine.create_heal_branch("sig-ref-failure", patch, verified_verdict())
    assert target.read_text(encoding="utf-8") == original


def test_unrelated_staged_changes_do_not_leak_into_commit(tmp_path: Path) -> None:
    repo, target, base = make_repo(tmp_path)
    original = target.read_text(encoding="utf-8")
    (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    git(repo, "add", "unrelated.txt")
    patch = make_patch(target, original, "return 'contained'\n")
    engine = GitBranchEngine(repo)
    branch = engine.create_heal_branch("sig-isolated", patch, verified_verdict())
    commit = git(repo, "rev-parse", branch)
    assert git(repo, "rev-parse", f"{commit}^1") == base
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit) == "target_app.py"
    assert git(repo, "diff", "--cached", "--", "unrelated.txt")


def test_push_and_create_pr_uses_non_force_push_and_gh_json_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = make_repo(tmp_path)
    body = repo / "pr-body.md"
    body.write_text("verified repair\n", encoding="utf-8")
    engine = GitBranchEngine(repo)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:example/repo.git\n", "")
        if args[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"url": "https://github.com/example/repo/pull/1"}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engine, "_resolve_repository", lambda: ("main", "b" * 40, repo.as_posix()))
    monkeypatch.setattr(engine, "_run", fake_run)
    receipt = engine.push_and_create_pr(
        "heal/sig-safe",
        base_branch="main",
        title="Auto-heal sig-safe",
        body_file=body,
        expected_commit="a" * 40,
    )

    assert receipt.branch == "heal/sig-safe"
    assert receipt.commit == "a" * 40
    assert receipt.url == "https://github.com/example/repo/pull/1"
    assert ["git", "push", "--set-upstream", "origin", "heal/sig-safe:refs/heads/heal/sig-safe"] in calls
    gh_call = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    assert "--body-file" in gh_call
    assert "--head" in gh_call and "heal/sig-safe" in gh_call
    assert "--json" not in gh_call


def test_push_branch_requires_origin_without_touching_git_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _, base = make_repo(tmp_path)
    engine = GitBranchEngine(repo)

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 2, "", "No such remote")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engine, "_resolve_repository", lambda: ("main", base, repo.as_posix()))
    monkeypatch.setattr(engine, "_run", fake_run)
    with pytest.raises(GitBranchError, match="No Git remote"):
        engine.push_branch("heal/sig-safe")
