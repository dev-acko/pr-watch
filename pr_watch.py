#!/usr/bin/env python3
"""
Long-running GitHub PR watcher.

Polls a pull request and automatically:
  - waits for required approvals
  - updates the branch when behind base
  - merges when approvals + checks are green

Stops with a macOS alert when checks fail (e.g. Sonar) or conflicts appear.

Requires: gh CLI authenticated (`gh auth login`)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
LOG = logging.getLogger("pr-watch")

GH_BIN = os.environ.get("GH_BIN") or shutil.which("gh") or "/opt/homebrew/bin/gh"

GITHUB_PR_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)

FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
RUNNING_STATUSES = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"})


class ExitReason(str, Enum):
    MERGED = "merged"
    CHECK_FAILED = "check_failed"
    CONFLICTS = "conflicts"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass
class PrTarget:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def label(self) -> str:
        return f"{self.slug}#{self.number}"


@dataclass
class WatchConfig:
    target: PrTarget
    required_approvals: int = 2
    interval_seconds: int = 60
    merge_method: str = "merge"
    dry_run: bool = False
    required_check_patterns: list[str] = field(default_factory=list)
    stop_check_patterns: list[str] = field(default_factory=list)


@dataclass
class CliArgs:
    pr_urls: list[str]
    url: Optional[str]
    pr: Optional[int]
    repo: Optional[str]
    config: Optional[str]
    list_profiles: bool
    approvals: Optional[int]
    interval: Optional[int]
    merge_method: Optional[str]
    required_checks: Optional[str]
    stop_on_checks: Optional[str]
    chain_file: Optional[str]
    parallel: bool
    dry_run: bool
    verbose: bool


@dataclass
class PollSnapshot:
    state: str
    mergeable: Optional[str]
    mergeable_state: str
    review_decision: str
    approval_count: int
    head_sha: str
    checks_pending: list[str]
    checks_failed: list[str]
    checks_passed: list[str]
    all_checks: list[dict[str, Any]]


@dataclass
class RuntimeStatus:
    label: str
    phase: str = "queued"
    detail: str = "waiting to start"
    approvals: str = "-"
    mergeable_state: str = "-"
    pending_count: int = 0
    failed_count: int = 0
    done: bool = False
    exit_reason: str = "-"


class GhClient:
    def __init__(self, target: PrTarget) -> None:
        self.target = target

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_json: Optional[dict] = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [GH_BIN, *args, "--repo", self.target.slug]
        LOG.debug("running: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=json.dumps(input_json) if input_json else None,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"gh command failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def fetch_snapshot(self) -> PollSnapshot:
        fields = ",".join(
            [
                "state",
                "mergeable",
                "mergeStateStatus",
                "reviewDecision",
                "reviews",
                "headRefOid",
                "statusCheckRollup",
            ]
        )
        result = self._run(
            ["pr", "view", str(self.target.number), "--json", fields]
        )
        data = json.loads(result.stdout)

        approvals = count_approvals(data.get("reviews") or [])
        pending, failed, passed, all_checks = classify_checks(
            data.get("statusCheckRollup") or []
        )

        return PollSnapshot(
            state=data.get("state") or "UNKNOWN",
            mergeable=data.get("mergeable"),
            mergeable_state=(data.get("mergeStateStatus") or "UNKNOWN").upper(),
            review_decision=(data.get("reviewDecision") or "UNKNOWN").upper(),
            approval_count=approvals,
            head_sha=data.get("headRefOid") or "",
            checks_pending=pending,
            checks_failed=failed,
            checks_passed=passed,
            all_checks=all_checks,
        )

    def update_branch(self) -> bool:
        if WatchState.dry_run:
            LOG.info("[dry-run] would update branch for %s", self.target.label)
            return True

        endpoint = (
            f"repos/{self.target.owner}/{self.target.repo}/"
            f"pulls/{self.target.number}/update-branch"
        )
        result = self._run(["api", "-X", "PUT", endpoint], check=False)
        if result.returncode == 0:
            LOG.info("update-branch triggered for %s", self.target.label)
            return True

        message = (result.stderr or result.stdout).strip()
        if "already up to date" in message.lower():
            LOG.info("branch already up to date")
            return False
        raise RuntimeError(f"update-branch failed: {message}")

    def merge(self, method: str) -> bool:
        if WatchState.dry_run:
            LOG.info("[dry-run] would merge %s via %s", self.target.label, method)
            return True

        args = ["pr", "merge", str(self.target.number), f"--{method}"]
        result = self._run(args, check=False)
        if result.returncode == 0:
            LOG.info("merged %s via %s", self.target.label, method)
            return True

        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"merge failed: {message}")


class WatchState:
    dry_run: bool = False
    shutdown_requested: bool = False
    status_lock = threading.Lock()
    statuses: dict[str, RuntimeStatus] = {}


def set_runtime_status(
    label: str,
    *,
    phase: str,
    detail: str,
    approvals: str = "-",
    mergeable_state: str = "-",
    pending_count: int = 0,
    failed_count: int = 0,
    done: bool = False,
    exit_reason: str = "-",
) -> None:
    with WatchState.status_lock:
        current = WatchState.statuses.get(label) or RuntimeStatus(label=label)
        current.phase = phase
        current.detail = detail
        current.approvals = approvals
        current.mergeable_state = mergeable_state
        current.pending_count = pending_count
        current.failed_count = failed_count
        current.done = done
        current.exit_reason = exit_reason
        WatchState.statuses[label] = current


def snapshot_statuses() -> list[RuntimeStatus]:
    with WatchState.status_lock:
        return [RuntimeStatus(**vars(item)) for item in WatchState.statuses.values()]


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def render_parallel_dashboard() -> None:
    statuses = sorted(snapshot_statuses(), key=lambda item: item.label)
    if not statuses:
        return

    lines = [
        "",
        "PR Watch Dashboard",
        "STATUS       PR                                  APPROVALS  MERGE      PEND  FAIL  DETAIL",
    ]
    for item in statuses:
        lines.append(
            f"{item.phase[:11]:<11}  "
            f"{item.label[:34]:<34}  "
            f"{item.approvals[:9]:<9}  "
            f"{item.mergeable_state[:9]:<9}  "
            f"{item.pending_count:>4}  "
            f"{item.failed_count:>4}  "
            f"{truncate(item.detail, 48)}"
        )

    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def count_approvals(reviews: list[dict[str, Any]]) -> int:
    latest_by_author: dict[str, str] = {}
    for review in reviews:
        author = (review.get("author") or {}).get("login")
        state = review.get("state")
        if not author or not state:
            continue
        latest_by_author[author] = state
    return sum(1 for state in latest_by_author.values() if state == "APPROVED")


def classify_checks(
    rollup: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    pending: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    latest_by_name: dict[str, dict[str, Any]] = {}

    for check in rollup:
        name = check.get("name") or check.get("context") or "unknown-check"
        status = (check.get("status") or "").upper()
        conclusion = (check.get("conclusion") or "").upper()
        sort_key = (
            check.get("completedAt")
            or check.get("startedAt")
            or check.get("createdAt")
            or ""
        )
        candidate = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "sort_key": sort_key,
        }
        current = latest_by_name.get(name)
        if current is None or candidate["sort_key"] >= current["sort_key"]:
            latest_by_name[name] = candidate

    normalized = sorted(latest_by_name.values(), key=lambda item: item["name"].lower())

    for check in normalized:
        name = check["name"]
        status = check["status"]
        conclusion = check["conclusion"]

        if status in RUNNING_STATUSES or (status != "COMPLETED" and not conclusion):
            pending.append(name)
        elif conclusion in FAILED_CONCLUSIONS:
            failed.append(name)
        elif conclusion in ("SUCCESS", "SKIPPED", "NEUTRAL") or status == "COMPLETED":
            passed.append(name)
        else:
            pending.append(name)

    return pending, failed, passed, normalized


def matches_any(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    lowered = name.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def notify(title: str, message: str, *, alert: bool = False) -> None:
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    if alert:
        script = f'display alert "{safe_title}" message "{safe_message}"'
    else:
        script = (
            f'display notification "{safe_message}" '
            f'with title "{safe_title}" sound name "Glass"'
        )
    subprocess.run(["osascript", "-e", script], check=False)


def parse_target(
    *,
    pr_url: Optional[str],
    pr_number: Optional[int],
    repo: Optional[str],
) -> PrTarget:
    if pr_url:
        match = GITHUB_PR_RE.search(pr_url)
        if not match:
            raise ValueError(f"could not parse GitHub PR URL: {pr_url}")
        return PrTarget(
            owner=match.group("owner"),
            repo=match.group("repo"),
            number=int(match.group("number")),
        )

    if pr_number is None:
        raise ValueError("provide --pr or --url")

    if repo:
        if "/" not in repo:
            raise ValueError("--repo must be owner/repo")
        owner, repo_name = repo.split("/", 1)
        return PrTarget(owner=owner, repo=repo_name, number=pr_number)

    result = subprocess.run(
        [GH_BIN, "repo", "view", "--json", "nameWithOwner"],
        capture_output=True,
        text=True,
        check=True,
    )
    slug = json.loads(result.stdout)["nameWithOwner"]
    owner, repo_name = slug.split("/", 1)
    return PrTarget(owner=owner, repo=repo_name, number=pr_number)


def parse_targets(
    *,
    cli_urls: list[str],
    explicit_url: Optional[str],
    pr_number: Optional[int],
    repo: Optional[str],
    chain_file: Optional[str],
) -> list[PrTarget]:
    raw_urls: list[str] = []
    if explicit_url:
        raw_urls.append(explicit_url)
    raw_urls.extend(cli_urls)

    if chain_file:
        with Path(chain_file).expanduser().open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                raw_urls.append(stripped)

    targets: list[PrTarget] = []
    if pr_number is not None:
        targets.append(parse_target(pr_url=None, pr_number=pr_number, repo=repo))

    for raw_url in raw_urls:
        targets.append(parse_target(pr_url=raw_url, pr_number=None, repo=None))

    return targets


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATHS = (
    Path.home() / ".config" / "pr-watch" / "repos.json",
    SCRIPT_DIR / "repos.json",
)


def load_profiles_config(explicit_path: Optional[str] = None) -> dict[str, Any]:
    paths = [Path(explicit_path)] if explicit_path else list(DEFAULT_CONFIG_PATHS)
    for path in paths:
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            LOG.debug("loaded repo profiles from %s", path)
            return data
    return {"defaults": {}, "repos": {}}


def resolve_repo_profile(
    slug: str, config_data: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    defaults = config_data.get("defaults") or {}
    repo_cfg = (config_data.get("repos") or {}).get(slug)
    merged = {**defaults, **(repo_cfg or {})}
    return merged, repo_cfg is not None


def patterns_from_profile(profile: dict[str, Any], key: str) -> list[str]:
    value = profile.get(key)
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return split_patterns(str(value))


def pick_config_value(
    cli_value: Any,
    profile: dict[str, Any],
    profile_key: str,
    fallback: Any,
) -> Any:
    if cli_value is not None:
        return cli_value
    if profile_key in profile and profile[profile_key] is not None:
        return profile[profile_key]
    return fallback


def list_profiles(config_data: dict[str, Any]) -> None:
    defaults = config_data.get("defaults") or {}
    repos = config_data.get("repos") or {}
    print("Defaults:")
    for key, value in defaults.items():
        print(f"  {key}: {value}")
    print("\nConfigured repos:")
    if not repos:
        print("  (none)")
        return
    for slug, profile in repos.items():
        description = profile.get("description", "")
        required = ", ".join(patterns_from_profile(profile, "required_checks")) or "all"
        stop_on = ", ".join(patterns_from_profile(profile, "stop_on_checks")) or "all"
        print(f"  {slug}")
        if description:
            print(f"    {description}")
        print(f"    required_checks: {required}")
        print(f"    stop_on_checks: {stop_on}")


def verify_gh_auth() -> None:
    result = subprocess.run([GH_BIN, "auth", "status"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh is not authenticated or unavailable at {GH_BIN}. Run: gh auth login"
        )


def handle_signal(signum: int, _frame: Any) -> None:
    WatchState.shutdown_requested = True
    LOG.warning("shutdown requested (signal %s), finishing current poll...", signum)


def relevant_failures(snapshot: PollSnapshot, config: WatchConfig) -> list[str]:
    if config.stop_check_patterns:
        return [
            name
            for name in snapshot.checks_failed
            if matches_any(name, config.stop_check_patterns)
        ]
    if config.required_check_patterns:
        return [
            name
            for name in snapshot.checks_failed
            if matches_any(name, config.required_check_patterns)
        ]
    return list(snapshot.checks_failed)


CONFLICT_ERROR_MARKERS = (
    "merge conflict",
    "merge conflicts",
    "conflicting",
    "not mergeable",
    "can't be merged",
    "cannot be merged",
    "head ref is dirty",
)


def has_merge_conflicts(snapshot: PollSnapshot) -> bool:
    return (
        snapshot.mergeable_state == "DIRTY"
        or snapshot.mergeable == "CONFLICTING"
    )


def is_merge_state_unknown(snapshot: PollSnapshot) -> bool:
    return (
        snapshot.mergeable_state == "UNKNOWN"
        or snapshot.mergeable == "UNKNOWN"
    )


def is_conflict_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in CONFLICT_ERROR_MARKERS)


def stop_for_conflicts(target_label: str) -> ExitReason:
    msg = (
        f"{target_label} has merge conflicts. "
        "Resolve manually and restart the watcher."
    )
    notify("PR Watch — conflicts", msg, alert=True)
    LOG.error(msg)
    set_runtime_status(
        target_label,
        phase="conflicts",
        detail="merge conflicts detected",
        done=True,
        exit_reason=ExitReason.CONFLICTS.value,
    )
    return ExitReason.CONFLICTS


def relevant_pending(snapshot: PollSnapshot, config: WatchConfig) -> list[str]:
    if config.required_check_patterns:
        return [
            name
            for name in snapshot.checks_pending
            if matches_any(name, config.required_check_patterns)
        ]
    return list(snapshot.checks_pending)


def should_update_branch(snapshot: PollSnapshot, config: WatchConfig) -> bool:
    if has_merge_conflicts(snapshot) or is_merge_state_unknown(snapshot):
        return False
    if snapshot.mergeable_state == "BEHIND":
        return True

    # GitHub often reports BLOCKED when the branch is out of date even if checks passed.
    ready_except_merge_state = (
        snapshot.approval_count >= config.required_approvals
        and not relevant_failures(snapshot, config)
        and not relevant_pending(snapshot, config)
    )
    if snapshot.mergeable_state == "BLOCKED" and ready_except_merge_state:
        return True
    return False


def can_attempt_merge(snapshot: PollSnapshot, config: WatchConfig) -> bool:
    if snapshot.state != "OPEN":
        return False
    if snapshot.approval_count < config.required_approvals:
        return False
    if has_merge_conflicts(snapshot) or is_merge_state_unknown(snapshot):
        return False
    if snapshot.mergeable_state in {"BEHIND", "DIRTY", "UNKNOWN"}:
        return False
    if snapshot.mergeable not in {None, "MERGEABLE"}:
        return False
    if relevant_pending(snapshot, config):
        return False
    if relevant_failures(snapshot, config):
        return False
    if snapshot.mergeable_state in {"CLEAN", "UNSTABLE", "HAS_HOOKS", "BLOCKED"}:
        # BLOCKED can still mean only hooks; if checks+approvals are fine, try merge.
        return (
            snapshot.review_decision in {"APPROVED", ""}
            or snapshot.approval_count >= config.required_approvals
        )
    return snapshot.mergeable_state == "CLEAN"


def watch_loop(config: WatchConfig, *, dashboard_mode: bool = False) -> ExitReason:
    update_triggered_for_sha: Optional[str] = None
    client = GhClient(config.target)
    set_runtime_status(
        config.target.label,
        phase="starting",
        detail="initializing watcher",
    )
    LOG.info(
        "watching %s | approvals=%d | interval=%ds | merge=%s%s",
        config.target.label,
        config.required_approvals,
        config.interval_seconds,
        config.merge_method,
        " | DRY-RUN" if config.dry_run else "",
    )

    while not WatchState.shutdown_requested:
        try:
            snapshot = client.fetch_snapshot()
        except RuntimeError as exc:
            LOG.error("poll failed: %s", exc)
            set_runtime_status(
                config.target.label,
                phase="error",
                detail=truncate(f"poll failed: {exc}", 48),
                done=False,
                exit_reason=ExitReason.ERROR.value,
            )
            time.sleep(config.interval_seconds)
            continue

        if not dashboard_mode:
            LOG.info(
                "poll: state=%s mergeable=%s mergeable_state=%s approvals=%d/%d "
                "pending=%s failed=%s",
                snapshot.state,
                snapshot.mergeable,
                snapshot.mergeable_state,
                snapshot.approval_count,
                config.required_approvals,
                snapshot.checks_pending or "-",
                snapshot.checks_failed or "-",
            )

        if snapshot.state == "MERGED":
            msg = f"{config.target.label} is already merged."
            notify("PR Watch — merged", msg)
            LOG.info(msg)
            set_runtime_status(
                config.target.label,
                phase="merged",
                detail="already merged",
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state="MERGED",
                pending_count=0,
                failed_count=0,
                done=True,
                exit_reason=ExitReason.MERGED.value,
            )
            return ExitReason.MERGED

        if snapshot.state == "CLOSED":
            msg = f"{config.target.label} was closed without merging."
            notify("PR Watch — stopped", msg, alert=True)
            LOG.error(msg)
            set_runtime_status(
                config.target.label,
                phase="closed",
                detail="closed without merge",
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(relevant_pending(snapshot, config)),
                failed_count=len(relevant_failures(snapshot, config)),
                done=True,
                exit_reason=ExitReason.ERROR.value,
            )
            return ExitReason.ERROR

        if has_merge_conflicts(snapshot):
            return stop_for_conflicts(config.target.label)

        if is_merge_state_unknown(snapshot):
            set_runtime_status(
                config.target.label,
                phase="waiting",
                detail="waiting for GitHub mergeability",
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(relevant_pending(snapshot, config)),
                failed_count=len(relevant_failures(snapshot, config)),
            )
            if not dashboard_mode:
                LOG.info("waiting for GitHub to compute mergeability")
            time.sleep(config.interval_seconds)
            continue

        failures = relevant_failures(snapshot, config)
        pending = relevant_pending(snapshot, config)
        if failures and not relevant_pending(snapshot, config):
            msg = (
                f"{config.target.label} cannot merge. Failed checks: "
                f"{', '.join(failures)}. Fix issues and restart the watcher."
            )
            notify("PR Watch — checks failed", msg, alert=True)
            LOG.error(msg)
            set_runtime_status(
                config.target.label,
                phase="failed",
                detail=truncate(f"failed checks: {', '.join(failures)}", 48),
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(pending),
                failed_count=len(failures),
                done=True,
                exit_reason=ExitReason.CHECK_FAILED.value,
            )
            return ExitReason.CHECK_FAILED

        if snapshot.approval_count < config.required_approvals:
            set_runtime_status(
                config.target.label,
                phase="approvals",
                detail="waiting for approvals",
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(pending),
                failed_count=len(failures),
            )
            if not dashboard_mode:
                LOG.info(
                    "waiting for approvals (%d/%d)",
                    snapshot.approval_count,
                    config.required_approvals,
                )
        elif should_update_branch(snapshot, config):
            set_runtime_status(
                config.target.label,
                phase="updating",
                detail="updating branch against base",
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(pending),
                failed_count=len(failures),
            )
            if update_triggered_for_sha != snapshot.head_sha:
                try:
                    client.update_branch()
                    update_triggered_for_sha = snapshot.head_sha
                    notify(
                        "PR Watch — updating branch",
                        f"{config.target.label}: update branch triggered, waiting for CI/Sonar.",
                    )
                except RuntimeError as exc:
                    if is_conflict_error(str(exc)):
                        return stop_for_conflicts(config.target.label)
                    LOG.error("update-branch error: %s", exc)
            else:
                set_runtime_status(
                    config.target.label,
                    phase="checks",
                    detail="update sent; waiting for CI",
                    approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                    mergeable_state=snapshot.mergeable_state,
                    pending_count=len(pending),
                    failed_count=len(failures),
                )
                if not dashboard_mode:
                    LOG.info("update already triggered for current head; waiting for CI")
        elif pending:
            set_runtime_status(
                config.target.label,
                phase="checks",
                detail=truncate(f"waiting: {', '.join(pending)}", 48),
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(pending),
                failed_count=len(failures),
            )
            if not dashboard_mode:
                LOG.info(
                    "waiting for checks: %s",
                    ", ".join(pending),
                )
        elif can_attempt_merge(snapshot, config):
            set_runtime_status(
                config.target.label,
                phase="merging",
                detail=f"attempting {config.merge_method} merge",
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(pending),
                failed_count=len(failures),
            )
            try:
                client.merge(config.merge_method)
                msg = f"Successfully merged {config.target.label}."
                notify("PR Watch — merged", msg)
                LOG.info(msg)
                set_runtime_status(
                    config.target.label,
                    phase="merged",
                    detail=f"merged via {config.merge_method}",
                    approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                    mergeable_state="MERGED",
                    pending_count=0,
                    failed_count=0,
                    done=True,
                    exit_reason=ExitReason.MERGED.value,
                )
                return ExitReason.MERGED
            except RuntimeError as exc:
                if is_conflict_error(str(exc)):
                    return stop_for_conflicts(config.target.label)
                LOG.warning("merge attempt failed, will retry: %s", exc)
                set_runtime_status(
                    config.target.label,
                    phase="retrying",
                    detail=truncate(f"merge retry: {exc}", 48),
                    approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                    mergeable_state=snapshot.mergeable_state,
                    pending_count=len(pending),
                    failed_count=len(failures),
                )
        else:
            set_runtime_status(
                config.target.label,
                phase="waiting",
                detail=truncate(
                    f"review={snapshot.review_decision} merge={snapshot.mergeable_state}",
                    48,
                ),
                approvals=f"{snapshot.approval_count}/{config.required_approvals}",
                mergeable_state=snapshot.mergeable_state,
                pending_count=len(pending),
                failed_count=len(failures),
            )
            if not dashboard_mode:
                LOG.info(
                    "not ready yet (review_decision=%s, mergeable_state=%s)",
                    snapshot.review_decision,
                    snapshot.mergeable_state,
                )

        time.sleep(config.interval_seconds)

    notify("PR Watch — stopped", f"Stopped watching {config.target.label}.")
    set_runtime_status(
        config.target.label,
        phase="stopped",
        detail="stopped by signal",
        done=True,
        exit_reason=ExitReason.INTERRUPTED.value,
    )
    return ExitReason.INTERRUPTED


def build_watch_config(
    target: PrTarget,
    args: CliArgs,
    profiles_config: dict[str, Any],
) -> WatchConfig:
    profile, has_repo_profile = resolve_repo_profile(target.slug, profiles_config)
    if has_repo_profile:
        LOG.info(
            "using repo profile for %s%s",
            target.slug,
            f" — {profile['description']}" if profile.get("description") else "",
        )
    else:
        LOG.info("no repo profile for %s — using defaults", target.slug)

    required_checks = (
        split_patterns(args.required_checks)
        if args.required_checks is not None
        else patterns_from_profile(profile, "required_checks")
    )
    stop_on_checks = (
        split_patterns(args.stop_on_checks)
        if args.stop_on_checks is not None
        else patterns_from_profile(profile, "stop_on_checks")
    )

    config = WatchConfig(
        target=target,
        required_approvals=pick_config_value(args.approvals, profile, "approvals", 2),
        interval_seconds=pick_config_value(args.interval, profile, "interval", 60),
        merge_method=pick_config_value(args.merge_method, profile, "merge_method", "merge"),
        dry_run=args.dry_run,
        required_check_patterns=required_checks,
        stop_check_patterns=stop_on_checks,
    )

    if required_checks:
        LOG.info("[%s] required checks: %s", target.label, ", ".join(required_checks))
    else:
        LOG.info("[%s] required checks: all", target.label)

    if stop_on_checks:
        LOG.info("[%s] stop on checks: %s", target.label, ", ".join(stop_on_checks))
    else:
        LOG.info("[%s] stop on checks: all failures", target.label)

    return config


def watch_chain(configs: list[WatchConfig]) -> ExitReason:
    total = len(configs)
    for index, config in enumerate(configs, start=1):
        LOG.info("starting chain item %d/%d: %s", index, total, config.target.label)
        reason = watch_loop(config)
        if reason not in {ExitReason.MERGED, ExitReason.INTERRUPTED}:
            LOG.error(
                "chain stopped at item %d/%d: %s (%s)",
                index,
                total,
                config.target.label,
                reason.value,
            )
            return reason
        if reason == ExitReason.INTERRUPTED:
            LOG.warning(
                "chain interrupted at item %d/%d: %s",
                index,
                total,
                config.target.label,
            )
            return reason

    if total > 1:
        notify(
            "PR Watch — chain complete",
            f"Finished processing {total} PRs in order.",
        )
    return ExitReason.MERGED


def watch_parallel(configs: list[WatchConfig]) -> ExitReason:
    results: dict[str, ExitReason] = {}
    results_lock = threading.Lock()

    for config in configs:
        set_runtime_status(
            config.target.label,
            phase="queued",
            detail="waiting for worker slot",
        )

    def worker(config: WatchConfig) -> None:
        reason = watch_loop(config, dashboard_mode=True)
        with results_lock:
            results[config.target.label] = reason

    threads = [
        threading.Thread(target=worker, args=(config,), daemon=True)
        for config in configs
    ]
    for thread in threads:
        thread.start()

    try:
        while any(thread.is_alive() for thread in threads):
            render_parallel_dashboard()
            time.sleep(1)
    finally:
        for thread in threads:
            thread.join()
        render_parallel_dashboard()

    reasons = list(results.values())
    if reasons and all(reason in {ExitReason.MERGED, ExitReason.INTERRUPTED} for reason in reasons):
        notify(
            "PR Watch — parallel complete",
            f"Finished processing {len(configs)} PRs in parallel.",
        )
        return ExitReason.MERGED

    for config in configs:
        reason = results.get(config.target.label)
        if reason and reason not in {ExitReason.MERGED, ExitReason.INTERRUPTED}:
            return reason
    return ExitReason.ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Long-running GitHub PR watcher with auto update-branch and merge.",
        epilog=(
            "Shortcut: pr-watch https://github.com/owner/repo/pull/123\n"
            "Chain: pr-watch <pr1> <pr2> <pr3>\n"
            "Parallel: pr-watch --parallel <pr1> <pr2> <pr3>\n"
            "Repo-specific rules are loaded from ~/.config/pr-watch/repos.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "pr_urls",
        nargs="*",
        help="GitHub PR URL(s) as positional shortcuts; multiple values run as a chain in order",
    )
    parser.add_argument("--url", help="GitHub PR URL")
    parser.add_argument("--pr", type=int, help="PR number (uses current repo if --repo omitted)")

    parser.add_argument("--repo", help="owner/repo (optional with --pr in current git repo)")
    parser.add_argument(
        "--config",
        help="path to repos.json profile config (default: ~/.config/pr-watch/repos.json)",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="show configured repo profiles and exit",
    )
    parser.add_argument(
        "--approvals",
        type=int,
        default=None,
        help="required approval count (overrides repo profile)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="poll interval in seconds (overrides repo profile)",
    )
    parser.add_argument(
        "--merge-method",
        choices=["merge", "squash", "rebase"],
        default=None,
        help="merge strategy (overrides repo profile)",
    )
    parser.add_argument(
        "--required-checks",
        default=None,
        help="comma-separated substrings; only these checks gate merge (overrides repo profile)",
    )
    parser.add_argument(
        "--stop-on-checks",
        default=None,
        help="comma-separated substrings; only these failures stop the watcher (overrides repo profile)",
    )
    parser.add_argument(
        "--chain-file",
        default=None,
        help="path to a text file containing one PR URL per line; lines starting with # are ignored",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="watch multiple PRs in parallel with a live dashboard",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log actions without updating branch or merging",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="debug logging",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(argv)
    args = CliArgs(**vars(parsed))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    profiles_config = load_profiles_config(args.config)
    if args.list_profiles:
        list_profiles(profiles_config)
        return 0

    if not args.pr_urls and not args.url and args.pr is None and not args.chain_file:
        parser.error("provide a PR URL, multiple PR URLs, --chain-file, or --pr")

    try:
        verify_gh_auth()
        targets = parse_targets(
            cli_urls=args.pr_urls,
            explicit_url=args.url,
            pr_number=args.pr,
            repo=args.repo,
            chain_file=args.chain_file,
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        LOG.error("%s", exc)
        return 1

    if not targets:
        parser.error("no PR targets resolved from the provided input")

    configs = [build_watch_config(target, args, profiles_config) for target in targets]

    WatchState.dry_run = args.dry_run
    WatchState.shutdown_requested = False
    WatchState.statuses = {}
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    reason = watch_parallel(configs) if args.parallel and len(configs) > 1 else watch_chain(configs)
    return 0 if reason in {ExitReason.MERGED, ExitReason.INTERRUPTED} else 1


def split_patterns(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


if __name__ == "__main__":
    sys.exit(main())
