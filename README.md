# pr-watch

A local, long-running GitHub pull request watcher that polls your PR and automatically:

- waits for the required number of approvals
- triggers **Update branch** when the PR falls behind its base branch
- waits for CI / quality checks to finish
- **merges** the PR as soon as it is fully mergeable

When a check fails (for example SonarQube) or merge conflicts appear, the script shows a macOS notification/alert and stops.

No cloud service, no AI, no tokens in the repo — authentication uses your local [GitHub CLI](https://cli.github.com/) session.

---

## Why use this?

If you have ever had a PR that was:

- approved and green, but blocked by **Update branch**
- waiting on CI to rerun after an update
- beaten to the merge by someone else while you were waiting

…this script watches the PR for you and merges the moment GitHub allows it.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| **Python 3.9+** | stdlib only — no `pip install` needed |
| **GitHub CLI (`gh`)** | [Install gh](https://cli.github.com/) |
| **Authenticated `gh` session** | `gh auth login` |
| **Merge permission** | your GitHub user must be allowed to merge the target PR |
| **macOS** (optional) | desktop notifications use `osascript`; the script still works on Linux without popups |

### GitHub token scopes

Your `gh` token needs at least the `repo` scope:

```bash
gh auth login -h github.com
gh auth refresh -s repo
```

---

## Installation

```bash
git clone https://github.com/dev-acko/pr-watch.git
cd pr-watch
./install.sh
```

Add to `~/.zshrc` if prompted:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

This installs a global `pr-watch` command you can run from **any folder**.

Or run without installing:

```bash
python3 pr_watch.py --url "https://github.com/owner/repo/pull/123"
```

---

## Quick start (global command)

Just pass a PR URL — repo rules are picked up automatically:

```bash
pr-watch "https://github.com/ackotech/AckoFlutter/pull/123"
pr-watch "https://github.com/ackotech/auto-bff/pull/2524"
```

The script reads `~/.config/pr-watch/repos.json` and applies the matching profile.

### Watch by PR URL

```bash
pr-watch "https://github.com/owner/repo/pull/123"
# or
python3 pr_watch.py --url "https://github.com/owner/repo/pull/123"
```

### Watch by PR number + repository

```bash
python3 pr_watch.py --pr 123 --repo owner/repo
```

### Watch by PR number from inside a git clone

If you are already inside the repository directory:

```bash
cd /path/to/repo
python3 /path/to/pr_watch.py --pr 123
```

The script runs until it merges, hits a failure, or you stop it with **Ctrl+C**.

---

## How it works

Every poll cycle (default: 60 seconds), the script fetches PR state via `gh` and decides what to do next.

```text
┌─────────────────────────────────────────┐
│           Poll PR via gh CLI            │
└─────────────────┬───────────────────────┘
                  │
     ┌────────────┼────────────┐
     ▼            ▼            ▼
 < N approvals  Behind base   Checks running
     │            │            │
     │            ▼            │
     │     Trigger update      │
     │       branch            │
     │            │            │
     └────────────┴────────────┘
                  │
                  ▼
         Checks failed? ──yes──► Alert + stop
                  │
                 no
                  ▼
         Conflicts? ──yes──► Alert + stop
                  │
                 no
                  ▼
    Approvals + checks green? ──yes──► Merge + stop
                  │
                 no
                  ▼
            Sleep and poll again
```

### Approval counting

- Counts the **latest review per reviewer**
- Only `APPROVED` reviews count
- Dismissed or superseded reviews are ignored automatically

### Update branch

Triggered when:

- GitHub reports the PR as `BEHIND`, or
- the PR is `BLOCKED` but approvals and checks are otherwise satisfied (common when only a base-branch update is missing)

The script triggers update **once per commit SHA** to avoid spamming the API.

### Merge

When all of the following are true:

- PR is `OPEN`
- required approvals are present
- no relevant checks are pending
- no relevant checks have failed
- no merge conflicts
- GitHub merge preconditions are satisfied

…the script calls `gh pr merge`.

If a merge attempt fails (for example the base branch moved again), it logs a warning and retries on the next poll.

### Merge method

Default is a **regular merge commit** (`--merge-method merge`), not squash.

Use `--merge-method squash` or `--merge-method rebase` if your repo prefers those.

### Merge conflicts

If GitHub reports conflicts, the script **never merges** and **stops immediately** with an alert:

- `mergeStateStatus` is `DIRTY`
- `mergeable` is `CONFLICTING`
- `update branch` or `merge` API calls return a conflict error

Resolve conflicts locally, push fixes, then restart the watcher. It will not retry merge while conflicts exist.

### Stop conditions

| Event | Behavior |
|-------|----------|
| PR merged (by you or someone else) | success notification, exit `0` |
| Required check failed | alert popup, exit `1` |
| Merge conflicts | alert popup, exit `1` — no merge attempted |
| PR closed without merge | alert popup, exit `1` |
| Ctrl+C / SIGTERM | notification, exit `0` |

---

## Repo profiles (preconfigured rules)

Edit `~/.config/pr-watch/repos.json` to define per-repo settings. On install, this file is created from the bundled template.

```json
{
  "defaults": {
    "approvals": 2,
    "interval": 60,
    "merge_method": "merge"
  },
  "repos": {
    "ackotech/AckoFlutter": {
      "description": "Flutter app — Sonar quality gates",
      "required_checks": ["Sonar"],
      "stop_on_checks": ["Sonar"]
    },
    "ackotech/auto-bff": {
      "description": "Auto BFF — Sonar + test coverage shards",
      "required_checks": ["Sonar", "Coverage"],
      "stop_on_checks": ["Sonar", "Coverage"]
    }
  }
}
```

| Profile field | Purpose |
|---------------|---------|
| `required_checks` | Only wait for checks whose names contain these substrings |
| `stop_on_checks` | Only stop the watcher when these checks fail |
| `approvals` | Override default approval count for this repo |
| `interval` | Override poll interval for this repo |
| `merge_method` | Override merge strategy for this repo |

List configured profiles:

```bash
pr-watch --list-profiles
```

CLI flags always override profile values:

```bash
pr-watch "https://github.com/ackotech/auto-bff/pull/123" --interval 30
```

If a repo is not in the config, the script falls back to built-in defaults (2 approvals, all checks, regular merge).

---

## CLI reference

```text
usage: pr_watch.py [-h] (--url URL | --pr PR) [--repo REPO]
                   [--approvals APPROVALS] [--interval INTERVAL]
                   [--merge-method {merge,squash,rebase}]
                   [--required-checks REQUIRED_CHECKS]
                   [--stop-on-checks STOP_ON_CHECKS] [--dry-run] [-v]
```

### Target (required — pick one)

| Flag | Description |
|------|-------------|
| `--url URL` | Full GitHub PR URL |
| `--pr N` | PR number; use with `--repo` or from current git directory |

### Optional flags

| Flag | Default | Description |
|------|---------|-------------|
| `--repo owner/repo` | current repo via `gh repo view` | Repository slug |
| `--approvals N` | `2` | Minimum number of approvals required before merge actions |
| `--interval SECONDS` | `60` | Time between polls |
| `--merge-method` | `merge` | `merge` (regular merge commit), `squash`, or `rebase` |
| `--required-checks` | all checks | Comma-separated substrings; only matching checks must pass before merge |
| `--stop-on-checks` | all failures | Comma-separated substrings; only matching check failures stop the watcher |
| `--dry-run` | off | Log intended actions without updating branch or merging |
| `-v`, `--verbose` | off | Debug logging |

---

## Examples

### Basic usage

```bash
python3 pr_watch.py --url "https://github.com/my-org/my-service/pull/456"
```

### Faster polling (every 30 seconds)

```bash
python3 pr_watch.py --pr 456 --repo my-org/my-service --interval 30
```

### Squash merge instead of regular merge

```bash
python3 pr_watch.py --pr 456 --repo my-org/my-service --merge-method squash
```

### Only gate on specific CI checks

Useful when a repo has many checks but only a few should block merge:

```bash
python3 pr_watch.py \
  --pr 456 \
  --repo my-org/my-service \
  --required-checks "build,unit-tests,sonar"
```

Only checks whose names contain `build`, `unit-tests`, or `sonar` (case-insensitive substring match) are waited on.

### Only stop on specific check failures

Useful when optional checks fail but should not kill the watcher:

```bash
python3 pr_watch.py \
  --pr 456 \
  --repo my-org/my-service \
  --stop-on-checks "sonar"
```

### Dry run (safe testing)

See what the script would do without changing anything:

```bash
python3 pr_watch.py --pr 456 --repo my-org/my-service --dry-run -v
```

### Single approval repo

```bash
python3 pr_watch.py --pr 789 --repo my-org/my-lib --approvals 1
```

---

## Running in the background

### Foreground (simplest)

Leave the terminal open:

```bash
python3 pr_watch.py --url "https://github.com/owner/repo/pull/123"
```

### `nohup` (close terminal safely)

```bash
nohup python3 pr_watch.py \
  --url "https://github.com/owner/repo/pull/123" \
  >> ~/pr-watch.log 2>&1 &
```

Follow logs:

```bash
tail -f ~/pr-watch.log
```

Stop background process:

```bash
pgrep -fl pr_watch.py
kill <pid>
```

### `tmux` (if installed)

```bash
tmux new -s pr-watch
python3 pr_watch.py --url "https://github.com/owner/repo/pull/123"
# detach: Ctrl+B, then D
# reattach: tmux attach -t pr-watch
```

---

## Inspecting checks for your repo

To find exact check names for `--required-checks` or `--stop-on-checks`:

```bash
gh pr checks 123 --repo owner/repo
```

Example output:

```text
SonarQube analysis    pass    2m
CI / build            pass    4m
lint                  skip    0m
```

Then use substrings like `--stop-on-checks "Sonar"` or `--required-checks "build,lint"`.

---

## Notifications

On macOS the script uses `osascript`:

- **Notifications** (non-blocking): merge success, update-branch triggered, manual stop
- **Alerts** (blocking dialog): check failures, conflicts, PR closed

On Linux and other platforms the script still runs; notifications are best-effort and may be no-ops depending on the environment.

---

## Security

- **No secrets in this repository**
- Authentication is handled entirely by your local `gh` credential store
- Anyone cloning this repo must authenticate with their own GitHub account
- The script only acts on the PR you pass on the command line

Do not commit:

- log files from background runs
- personal access tokens
- `.env` files

---

## Troubleshooting

### `gh is not authenticated`

```bash
gh auth login -h github.com
```

### `gh command failed` / permission denied

Ensure your account can merge the PR and that the token has `repo` scope:

```bash
gh auth refresh -s repo
```

### Merge keeps retrying but never succeeds

Common causes:

- branch protection rules not satisfied (required reviewers, signed commits, etc.)
- base branch moved again immediately before merge
- checks still pending under a different name than you expect

Run with `-v` and inspect:

```bash
gh pr view 123 --repo owner/repo --json state,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup
```

### Script does not stop on a failed optional check

Use `--stop-on-checks` to narrow which failures matter, or omit it to stop on any failed check.

### Update branch not triggering

The script waits until approvals are satisfied before updating. If the PR has conflicts, it stops with an alert — resolve them manually, push, then restart.

### Script squash-merged but I wanted a regular merge

The default is now `merge` (regular merge commit). Older runs used `squash` by default. Pass `--merge-method merge` explicitly if needed.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | merged successfully, or stopped via Ctrl+C |
| `1` | check failure, conflicts, PR closed, or startup error |

---

## Project layout

```text
pr-watch/
├── README.md
├── .gitignore
└── pr_watch.py
```

---

## License

Use freely. No warranty — merge automation can race with other contributors and branch protection rules. Test with `--dry-run` first on important PRs.
