# copilot-pool

Share GitHub Copilot code-review credits within a group of people who trust each other.
When you're out of premium requests, the script asks the pool member with the most credits
left to request Copilot on your PR (inviting them as a collaborator if needed). Their daemon
accepts the invite and makes the request, so the review is billed to them.

Python 3.9+, standard library only.

## Setup (once per pool)

1. Create a **private** repo to act as the pool, e.g. `yourteam/copilot-pool`.
2. Add every member as a collaborator on it. Collaborators of the pool repo *are* the pool:
   only their invites are auto-accepted and only their requests are served.

## Setup (once per member)

```sh
./copilot_pool.py init
```

- **Repo token**: a classic PAT with `repo` scope, or leave blank to use `gh auth token`.
  (Fine-grained PATs can't act on repos owned by other users, which helpers need.)
- **Billing token** (optional): a fine-grained PAT with the *Plan: read* user permission, used to
  read your real premium-request usage. Without it, the script counts the reviews it has requested
  itself this month.
- **Pool repo** and **monthly quota** (e.g. 300 for Copilot Pro, 1500 for Pro+).

Config lives in `~/.config/copilot-pool/config.json` (mode 600). Override the directory with `COPILOT_POOL_HOME`.

## Use

```sh
./copilot_pool.py run                         # keep this running (tmux, launchd, systemd…)
./copilot_pool.py request https://github.com/o/r/pull/12
./copilot_pool.py request o/r#12 --via-pool   # borrow even if you still have credits
./copilot_pool.py status                      # who has how many credits
```

## How it works

- Every `poll_interval` (60s) the daemon:
  - updates its `pool-status` issue in the pool repo with its remaining credits. It rewrites the
    issue when the number changes, and at least every `heartbeat_interval`;
  - accepts repo invitations whose inviter is a pool member;
  - handles open `copilot-request` issues assigned to it. It requests
    `copilot-pull-request-reviewer[bot]` on the PR, comments, and closes the issue. If its access
    hasn't arrived yet, it retries for up to `access_timeout` (1h).
- `request` picks the member with the most remaining credits whose status is newer than
  `stale_after` (15 min). If that member can't request reviewers on the repo, it invites them, with
  the `triage` role on org repos and as a regular collaborator on personal repos. Then it opens a
  request issue assigned to them.

## Caveats

- Invited helpers stay collaborators on your repo until you remove them.
- Org repos may block outside collaborators. Copilot code review also has to be enabled for the helper.
- A helper who runs out of credits declines the request. Run `request` again to pick the next member.

## Tests

```sh
python3 -m pytest
```
