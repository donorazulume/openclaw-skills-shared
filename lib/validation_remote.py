"""Authoritative Remote Truth Validation & Stale Clone Guard (SPEC-FIREFLY-VAL-001).

Validates git clone state, remote commit existence, and CI deployment status against GitHub API
to prevent false negatives caused by stale or drifted local clones.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


class StaleCloneError(RuntimeError):
    """Raised when validation preflight detects a stale or dirty local clone."""

    pass


def get_local_git_info() -> dict[str, Any]:
    """Return local HEAD commit SHA and working tree dirty status."""
    try:
        head_res = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        head_sha = head_res.stdout.strip()
    except Exception:
        head_sha = "unknown"

    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        dirty = bool(status_res.stdout.strip())
    except Exception:
        dirty = False

    return {"head_sha": head_sha, "dirty": dirty}


def get_remote_head_sha(repo: str = "donorazulume/openclaw-docker", branch: str = "main") -> str:
    """Fetch authoritative HEAD SHA from GitHub API."""
    try:
        cmd = ["gh", "api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        sha = res.stdout.strip()
        if sha:
            return sha
    except Exception:
        pass

    # Fallback to git ls-remote if gh CLI is unavailable or unauthenticated
    try:
        cmd = ["git", "ls-remote", f"https://github.com/{repo}.git", f"refs/heads/{branch}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        parts = res.stdout.strip().split()
        if parts:
            return parts[0]
    except Exception as exc:
        raise RuntimeError(f"Failed to query remote head SHA for {repo}:{branch}") from exc

    raise RuntimeError(f"Empty remote head SHA for {repo}:{branch}")


def check_ci_deploy_status(
    commit_sha: str, repo: str = "donorazulume/openclaw-docker"
) -> tuple[bool, str]:
    """Query GitHub Actions workflow runs to verify Build + Deploy jobs for commit_sha are green."""
    try:
        cmd = [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?head_sha={commit_sha}",
            "--jq",
            ".workflow_runs[] | {name: .name, status: .status, conclusion: .conclusion}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = res.stdout.strip()
        if not output:
            return False, f"No CI workflow runs found for commit {commit_sha[:8]}"

        runs = []
        for line in output.splitlines():
            line_str = line.strip()
            if line_str:
                try:
                    runs.append(json.loads(line_str))
                except json.JSONDecodeError:
                    pass

        if not runs:
            return False, f"No parseable CI workflow runs found for commit {commit_sha[:8]}"

        all_success = True
        details = []

        for r in runs:
            name = r.get("name", "workflow")
            st = r.get("status")
            conc = r.get("conclusion")
            if st != "completed" or conc != "success":
                all_success = False
            details.append(f"{name}:{st}/{conc}")

        msg = f"Commit {commit_sha[:8]} CI runs: " + ", ".join(details)
        return all_success, msg
    except Exception as exc:
        return False, f"Failed to query CI deploy status for commit {commit_sha[:8]}: {exc}"


def validate_remote_truth(
    repo: str = "donorazulume/openclaw-docker",
    branch: str = "main",
    require_clean_clone: bool = True,
) -> dict[str, Any]:
    """Preflight guard: fails fast with StaleCloneError (ERR_STALE_CLONE) if local clone lags origin/main or is dirty."""
    local_info = get_local_git_info()
    local_sha = local_info["head_sha"]
    dirty = local_info["dirty"]

    remote_sha = get_remote_head_sha(repo, branch)

    if dirty and require_clean_clone:
        raise StaleCloneError(
            f"ERR_STALE_CLONE: Validation preflight blocked due to uncommitted working tree drift. "
            f"HEAD is at {local_sha[:8]}, remote origin/{branch} is at {remote_sha[:8]}."
        )

    if local_sha != remote_sha:
        raise StaleCloneError(
            f"ERR_STALE_CLONE: Validation preflight blocked — local clone ({local_sha[:8]}) lags "
            f"authoritative remote origin/{branch} ({remote_sha[:8]}). "
            f"Run `git fetch origin && git reset --hard origin/{branch}` to synchronize."
        )

    ci_ok, ci_msg = check_ci_deploy_status(remote_sha, repo)

    return {
        "status": "PASS",
        "remote_head_sha": remote_sha,
        "local_head_sha": local_sha,
        "clean": not dirty,
        "ci_deploy_ok": ci_ok,
        "ci_deploy_message": ci_msg,
    }


if __name__ == "__main__":
    try:
        result = validate_remote_truth()
        print(json.dumps(result, indent=2))
        sys.exit(0)
    except StaleCloneError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
