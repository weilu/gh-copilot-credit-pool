# copilot-pool

Share GitHub Copilot code-review credits within a group of people who trust each other.
When you're out of premium requests, the script asks the pool member with the most credits
left to request Copilot on your PR (inviting them as a collaborator if needed). Their daemon
accepts the invite and makes the request, so the review is billed to them.

Python 3.9+, standard library only. macOS/Linux (it uses `fcntl` file locks).

## Setup (once per pool)

1. Create a **private** repo to act as the pool, e.g. `yourteam/copilot-pool`.
2. Add every member as a direct collaborator with write access. Those collaborators *are* the
   pool, plus the owner if it's a personal repo. People who only have access through org or team
   membership are not members. The script refuses to run if the pool repo is public.

## Setup (once per member)

```sh
./copilot_pool.py init
```

- **Token**: a classic PAT with `repo` scope, or leave blank to use `gh auth token`.
  (Fine-grained PATs can't act on repos owned by other users, which helpers need.)
- **Credits** are read from Copilot's own account endpoint (`/copilot_internal/user`), the same one
  the Copilot clients use. It works for personal plans and for seats paid by an organization, which
  GitHub's billing API doesn't cover. It is undocumented and could change. If it fails, the script
  estimates usage as (reviews it has requested this month) × `credits_per_review`, against the
  monthly credits you enter. Spending beyond your allowance is never counted as available.
- **Credits per review**: the least a member must have left to take a review, and the cost used for
  that estimate (default 100). Lower it once you know what a review actually costs.
- **Pool repo**.
- **Rate limit**: the most requests you'll serve per member in any 24 hours (default 10, 0 = unlimited).
  It is enforced by your own script, so each member sets their own.

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
  - updates its `pool-status` issue in the pool repo with its remaining Copilot credits. It rewrites the
    issue when the number changes, and at least every `heartbeat_interval`;
  - accepts a repo invitation only when the inviter is a pool member with an open request for a PR
    in that repo;
  - handles open `copilot-request` issues assigned to it. It requests
    `copilot-pull-request-reviewer[bot]` on the PR, comments, and closes the issue. It acts only when
    the requester wrote the PR or can request reviewers on it, because the helper's token may reach
    repos the requester can't. Any failure is retried until `access_timeout` (1h), then the issue is
    closed with the same generic message whatever the cause. That way a request can't be used to
    find out about PRs the helper can see and the requester can't;
  - removes collaborator access it granted for its own requests once they're done. A completed
    request first waits for Copilot's review, or `review_grace` (1h), whichever comes first.
    Access is always removed within `access_timeout + review_grace` (2h) of the invite, whatever
    state the request is in. The invite is recorded before it's sent, and withdrawn if opening the
    request issue fails.
- `request` uses your own credits if you have at least `credits_per_review` left. Otherwise it picks
  the member with the most credits, among those with at least `credits_per_review` left and a status
  newer than `stale_after` (15 min). If that member can't request reviewers on the repo, it invites them, with
  the `triage` role on org repos and as a regular collaborator on personal repos. Then it opens a
  request issue assigned to them.

## Trust and tampering

Anyone with write access to the pool repo can edit any issue in it, and GitHub can't restrict that.
So the script ignores status and request issues that were last edited by someone other than their
author. Your own script overwrites a tampered status on its next update. A status must also be
well-formed, have `remaining` between 0 and `quota`, and not be dated in the future.

## Caveats

- Collaborator access is revoked by the requester's running script, so keep `run` running after `request`.
- Org-paid seats: check that your organization allows lending its Copilot credits to outsiders.
- Org repos may block outside collaborators. Copilot code review also has to be enabled for the helper.
- A helper without `credits_per_review` credits left declines the request. Run `request` again to pick the next member.

## Tests

```sh
python3 -m pytest
```
