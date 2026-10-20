from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omniimmune.healer.git_branch import GitBranchEngine, GitBranchError

from test_git_branch import git, make_patch, make_repo, verified_verdict


def test_push_branch_requires_origin_remote(tmp_path: Path) -> None:
    repo, _, _ = make_repo(tmp_path)
    git(repo, "branch", "heal/sig-demo")

    with pytest.raises(GitBranchError, match="origin"):
        GitBranchEngine(repo).push_branch("heal/sig-demo")


def test_push_and_create_pr_returns_receipt_without_network_or_shell_interpolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, target, _ = make_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(remote))

    original = target.read_text(encoding="utf-8")
    patch = make_patch(target, original, "return 'contained'\n")
    engine = GitBranchEngine(repo)
    branch = engine.create_heal_branch("sig-demo", patch, verified_verdict())
    commit = git(repo, "rev-parse", branch)
    body = tmp_path / "pr body.md"
    body.write_text("verified repair\n", encoding="utf-8")
    calls: list[list[str]] = []
    original_run = engine._run

    def fake_run(args: list[str], **kwargs):
        if args[:3] == ["gh", "pr", "create"]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps({"url": "https://github.com/example/repo/pull/7"}), "")
        return original_run(args, **kwargs)

    monkeypatch.setattr(engine, "_run", fake_run)
    receipt = engine.push_and_create_pr(
        branch,
        base_branch="main",
        title="Auto-heal demo; keep quotes intact",
        body_file=body,
        expected_commit=commit,
    )

    assert receipt.branch == branch
    assert receipt.commit == commit
    assert receipt.url == "https://github.com/example/repo/pull/7"
    assert calls == [
        [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            "main",
            "--title",
            "Auto-heal demo; keep quotes intact",
            "--body-file",
            str(body),
            "--json",
            "url",
        ]
    ]
