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

- **Auth** comes from the GitHub CLI: log in once with `gh auth login`. The script runs
  `gh auth token` whenever it starts, and never stores the token. The default `gh` login includes the
  `repo` scope it needs.
- **Credits** are read from Copilot's own account endpoint (`/copilot_internal/user`), the same one
  the Copilot clients use. It works for personal plans and for seats paid by an organization, which
  GitHub's billing API doesn't cover. It is undocumented and could change. If it fails, the script
  estimates usage as (reviews it has requested this month) × `credits_per_review`, against the
  monthly credits you enter. That estimate misses chat and agent usage, so it is only used
  for your own `request`: while it's in use you advertise 0 credits and decline others' requests.
  Spending beyond your allowance is never counted as available.
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
  - checks its own row in the pool repo's `README.md`, a table of every member's credits. It commits
    a new row only when its remaining, monthly, used or source value has changed. If another member
    committed in the meantime, it rereads the file and tries again;
  - edits its own `pool-heartbeat` issue every `heartbeat_interval` (5 min), which creates no commit.
    GitHub's record of when that issue was last edited is the member's "last seen" time;
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
  the member with the most credits, among those with at least `credits_per_review` left and a
  heartbeat within `stale_after` (15 min). If that member can't request reviewers on the repo, it invites them, with
  the `triage` role on org repos and as a regular collaborator on personal repos. Then it opens a
  request issue assigned to them.

## Trust and tampering

Anyone with write access to the pool repo can edit any issue or file in it, and GitHub can't restrict
that.

- **Heartbeat and request issues** count only if they were last edited by their author, and
  "last seen" uses GitHub's own edit time.
- **The README board** can't be attributed: our tests showed that API commits are unsigned and can
  carry any author. So rows are treated as hints for choosing a helper. Rows are validated: known
  fields only, numbers within range, nothing dated in the future. Each member's script rewrites its
  own row if it doesn't match. A tampered row can at worst send a request to the wrong member or keep
  someone from being picked. The helper's own script still checks its real credits, the rate limit
  and the requester's access before spending anything.

On first `run`, the script closes the member's old `pool-status` issue from earlier versions.

## Caveats

- Collaborator access is revoked by the requester's running script, so keep `run` running after `request`.
- Org-paid seats: check that your organization allows lending its Copilot credits to outsiders.
- Org repos may block outside collaborators. Copilot code review also has to be enabled for the helper.
- A helper without `credits_per_review` credits left declines the request. Run `request` again to pick the next member.

## Tests

```sh
python3 -m pytest
```
