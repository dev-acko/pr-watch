# pr-watch

A local GitHub pull request watcher that polls your PR and automatically:

- waits for the required number of approvals
- triggers **Update branch** when the PR falls behind base
- waits for CI / quality checks to finish
- merges the PR the moment GitHub allows it

It also supports watching multiple PRs:

- sequentially in a chain
- in parallel with a live terminal dashboard

No cloud service, no repo-stored token, no AI dependency. Authentication uses your local [GitHub CLI](https://cli.github.com/) session.

---

## Why use this?

This is useful when a PR is:

- approved, but still waiting for branch update
- green, but still waiting for a merge window
- stuck behind CI reruns
- one of several PRs you want to keep an eye on together

Instead of manually refreshing GitHub, `pr-watch` keeps polling and merges as soon as the PR becomes ready.

---

## Features

- single PR watch by URL or PR number
- repo-aware config profiles from `~/.config/pr-watch/repos.json`
- default merge strategy is a regular merge commit
- sequential chain mode for ordered PR processing
- parallel mode for watching many PRs at once
- live terminal dashboard in parallel mode
- macOS notifications and alerts via `osascript`
- no third-party Python dependencies

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| **Python 3.9+** | stdlib only |
| **GitHub CLI (`gh`)** | [Install gh](https://cli.github.com/) |
| **Authenticated `gh` session** | `gh auth login` |
| **Merge permission** | your account must be allowed to merge the target PR |
| **macOS** (optional) | desktop notifications use `osascript`; the script still works without popups on other platforms |

### GitHub token scopes

Your `gh` token needs at least the `repo` scope:

```bash
gh auth login -h github.com
gh auth refresh -s repo
```

---

## Installation

### Standard install

```bash
git clone https://github.com/dev-acko/pr-watch.git
cd pr-watch
./install.sh
```

This installs two terminal commands:

- `pr-watch`
- `prwatch`

Both work from any folder.

If `~/.local/bin` is not already on your `PATH`, add this to your shell config:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

For `zsh`, add it to `~/.zshrc`, then reload:

```bash
source ~/.zshrc
```

### Manual setup

If you do not want to run `install.sh`, you can symlink it yourself:

```bash
mkdir -p ~/.local/bin
ln -sf /absolute/path/to/pr-watch/pr-watch ~/.local/bin/pr-watch
ln -sf /absolute/path/to/pr-watch/pr-watch ~/.local/bin/prwatch
```

### Run without installing

```bash
python3 pr_watch.py --url "https://github.com/owner/repo/pull/123"
```

---

## Quick start

### Single PR

```bash
pr-watch "https://github.com/ackotech/auto-bff/pull/2524"
```

### Sequential chain

PRs are processed one by one, in the order you pass them:

```bash
pr-watch \
  "https://github.com/owner/repo/pull/101" \
  "https://github.com/owner/repo/pull/102" \
  "https://github.com/owner/repo/pull/103"
```

### Parallel watch

All PRs are watched together, and the terminal shows a live dashboard:

```bash
pr-watch --parallel \
  "https://github.com/owner/repo/pull/101" \
  "https://github.com/owner/repo/pull/102" \
  "https://github.com/owner/repo/pull/103"
```

### PR list from a file

```bash
pr-watch --chain-file prs.txt
```

Example `prs.txt`:

```text
# one PR per line
https://github.com/owner/repo/pull/101
https://github.com/owner/repo/pull/102
```

---

## How terminal setup works

After installation, you can use `pr-watch` exactly like any other terminal command:

```bash
pr-watch "https://github.com/owner/repo/pull/123"
prwatch "https://github.com/owner/repo/pull/123"
```

This works because the installer places a launcher in `~/.local/bin`, which your shell reads from `PATH`.

If you prefer aliases instead of a symlink-based install, you can add one to `~/.zshrc`:

```bash
alias pr-watch="$HOME/path/to/pr-watch/pr-watch"
alias prwatch="$HOME/path/to/pr-watch/pr-watch"
```

The symlink approach is recommended because it works from any terminal session without managing aliases manually.

---

## Default behavior

- default polling interval: `60` seconds
- default required approvals: `2`
- default merge method: `merge`
- if multiple PR URLs are passed without `--parallel`, they run sequentially

That means the default merge behavior is a regular merge commit, not squash.

---

## How it works

Every poll cycle, the script fetches PR state via `gh` and decides what to do next.

```text
poll PR
  -> waiting for approvals
  -> waiting for checks
  -> update branch if behind
  -> stop if checks fail
  -> stop if conflicts exist
  -> merge when fully ready
```

### Approval counting

- only the latest review per reviewer counts
- only `APPROVED` reviews count

### Update branch

Triggered when:

- GitHub reports `BEHIND`, or
- GitHub reports `BLOCKED` but approvals and required checks are otherwise complete

The script only triggers update once per head SHA to avoid repeating the same update call.

### Merge readiness

The script merges only when all of these are true:

- PR is `OPEN`
- required approvals are present
- required checks are not pending
- required checks are not failing
- there are no merge conflicts
- GitHub reports the PR as mergeable

### Duplicate check handling

GitHub sometimes returns older and newer runs for the same check in the same rollup. `pr-watch` keeps only the latest run for each check name so stale `IN_PROGRESS` runs do not block a merge incorrectly.

---

## Sequential chain vs parallel mode

### Sequential chain

Default when you pass multiple PR URLs:

```bash
pr-watch pr1 pr2 pr3
```

Behavior:

- watches `pr1`
- merges `pr1` when ready
- then starts `pr2`
- then starts `pr3`

Use this when order matters.

### Parallel mode

```bash
pr-watch --parallel pr1 pr2 pr3
```

Behavior:

- starts a watcher for every PR immediately
- polls all of them together
- merges each one independently as soon as it becomes ready

Use this when order does not matter.

### Parallel dashboard

In parallel mode, the terminal shows one row per PR with:

- current phase
- PR label
- approvals
- merge state
- pending check count
- failed check count
- short detail text

Example:

```text
PR Watch Dashboard
STATUS       PR                                  APPROVALS  MERGE      PEND  FAIL  DETAIL
checks       ackotech/auto-bff#2722              4/2        CLEAN         2     0  waiting: Test Coverage shard 3/4...
merging      ackotech/auto-bff#2723              4/2        CLEAN         0     0  attempting merge merge
merged       ackotech/auto-bff#2724              4/2        MERGED        0     0  merged via merge
```

---

## Repo profiles

Repo-specific rules live in:

```text
~/.config/pr-watch/repos.json
```

The installer creates this file from the bundled template if it does not already exist.

Example:

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
| `required_checks` | only checks containing these substrings gate merge |
| `stop_on_checks` | only checks containing these substrings stop the watcher on failure |
| `approvals` | required approval count for this repo |
| `interval` | poll interval for this repo |
| `merge_method` | merge strategy override for this repo |

Show active profiles:

```bash
pr-watch --list-profiles
```

CLI flags override profile values.

---

## CLI reference

```text
usage: pr_watch.py [-h] [--url URL] [--pr PR] [--repo REPO] [--config CONFIG]
                   [--list-profiles] [--approvals APPROVALS]
                   [--interval INTERVAL]
                   [--merge-method {merge,squash,rebase}]
                   [--required-checks REQUIRED_CHECKS]
                   [--stop-on-checks STOP_ON_CHECKS] [--chain-file CHAIN_FILE]
                   [--parallel] [--dry-run] [-v]
                   [pr_urls ...]
```

### Target input

| Input | Description |
|-------|-------------|
| positional `pr_urls` | one or more GitHub PR URLs |
| `--url URL` | single GitHub PR URL |
| `--pr N` | PR number |
| `--repo owner/repo` | repo slug when using `--pr` outside the repo directory |
| `--chain-file FILE` | one PR URL per line |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--approvals N` | `2` | required approvals |
| `--interval SECONDS` | `60` | poll interval |
| `--merge-method` | `merge` | `merge`, `squash`, or `rebase` |
| `--required-checks` | all checks | comma-separated substrings of checks to wait for |
| `--stop-on-checks` | all failures | comma-separated substrings of failures that should stop the watcher |
| `--parallel` | off | watch multiple PRs in parallel with a live dashboard |
| `--dry-run` | off | show intended actions without updating or merging |
| `-v`, `--verbose` | off | debug logging |

---

## Examples

### Watch one PR by URL

```bash
pr-watch "https://github.com/my-org/my-service/pull/456"
```

### Watch by PR number + repo

```bash
pr-watch --pr 456 --repo my-org/my-service
```

### Faster polling

```bash
pr-watch --pr 456 --repo my-org/my-service --interval 30
```

### Force squash merge

```bash
pr-watch --pr 456 --repo my-org/my-service --merge-method squash
```

### Wait only on selected checks

```bash
pr-watch \
  --pr 456 \
  --repo my-org/my-service \
  --required-checks "build,unit-tests,sonar"
```

### Stop only on selected failures

```bash
pr-watch \
  --pr 456 \
  --repo my-org/my-service \
  --stop-on-checks "sonar"
```

### Safe dry run

```bash
pr-watch --pr 456 --repo my-org/my-service --dry-run -v
```

### Sequential multi-PR run

```bash
pr-watch pr1 pr2 pr3
```

### Parallel multi-PR run

```bash
pr-watch --parallel pr1 pr2 pr3
```

---

## Running in the background

### Foreground

Best for single PRs or for `--parallel` mode where you want the dashboard:

```bash
pr-watch "https://github.com/owner/repo/pull/123"
```

### `nohup`

```bash
nohup pr-watch \
  "https://github.com/owner/repo/pull/123" \
  >> ~/pr-watch.log 2>&1 &
```

Follow logs:

```bash
tail -f ~/pr-watch.log
```

### `tmux`

```bash
tmux new -s pr-watch
pr-watch --parallel pr1 pr2 pr3
```

Detach with `Ctrl+B`, then `D`.

Reattach:

```bash
tmux attach -t pr-watch
```

---

## Inspecting check names

To find the exact checks to use with `--required-checks` or `--stop-on-checks`:

```bash
gh pr checks 123 --repo owner/repo
```

Then use partial names such as:

```bash
pr-watch --pr 123 --repo owner/repo --required-checks "Sonar,Coverage"
```

---

## Notifications

On macOS the script uses `osascript`:

- notifications for merge success, update-branch, and manual stop
- alert dialogs for failed checks, conflicts, and PR closed

On Linux or other platforms, the watcher still runs even if desktop notifications are unavailable.

---

## Troubleshooting

### `gh is not authenticated`

```bash
gh auth login -h github.com
gh auth refresh -s repo
```

### `gh` is installed but not found

The script tries:

- `GH_BIN` environment variable
- your current `PATH`
- `/opt/homebrew/bin/gh`

You can force a custom path like this:

```bash
GH_BIN=/custom/path/to/gh pr-watch "https://github.com/owner/repo/pull/123"
```

### Merge keeps retrying

Common reasons:

- branch protection rules are not fully satisfied
- base branch moved again
- another required check name is not included in your profile

Inspect the raw PR state:

```bash
gh pr view 123 --repo owner/repo --json state,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup
```

### Update branch is not triggered

The watcher only attempts update when approvals are already satisfied and the PR is behind without conflicts.

### I wanted a merge commit, not squash

The default is already:

```text
--merge-method merge
```

You only need to pass a different value if your repo prefers `squash` or `rebase`.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | merged successfully, already merged, or stopped via Ctrl+C |
| `1` | check failure, merge conflicts, closed PR, or startup error |

---

## Project layout

```text
pr-watch/
├── README.md
├── install.sh
├── pr-watch
├── pr_watch.py
└── repos.json
```

---

## License

Use freely. No warranty. Always test with `--dry-run` first if you are using it on important PRs or new repos.
