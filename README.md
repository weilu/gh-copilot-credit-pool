# copilot-pool

Share GitHub Copilot code-review credits within a group of people who trust each other.
When you run out of Copilot credits, the script asks the pool member with the most credits left
to request a Copilot review on your PR. It invites them as a collaborator first if needed. Their
script accepts the invite and makes the request, so the review is billed to them.

Requirements: Python 3.9+ (standard library only), the [GitHub CLI](https://cli.github.com/), and
macOS or Linux (the script uses `fcntl` file locks).

## Setup (once per pool)

1. Create a **private** repo to act as the pool, e.g. `yourteam/copilot-pool`. The script refuses
   to run if the pool repo is public.
2. Add every member as a direct collaborator with write access. Those collaborators *are* the
   pool, plus the owner if it's a personal repo. People who only have access through org or team
   membership are not members.

The first member's `init` creates the labels and a `pool.json` holding the [pool settings](#pool-settings).

## Setup (once per member)

```sh
gh auth login            # if you haven't already
./copilot_pool.py init
```

`init` asks for:

- **Pool repo** (`owner/name`).
- **Rate limit**: the most requests you'll serve per member in any 24 hours (default 10, 0 = unlimited).
  Your own script enforces it, so each member sets their own.
- **Monthly Copilot credits**, only if Copilot's endpoint can't be read (see [Credits](#credits)).

Auth comes from the GitHub CLI. The script runs `gh auth token` whenever it starts and never stores
the token. The default `gh` login includes the `repo` scope it needs.

Your files live in `~/.config/copilot-pool/`. Set `COPILOT_POOL_HOME` to use a different directory.
- `config.json` (mode 600) holds your settings. You can also edit `poll_interval` (60s),
  `heartbeat_interval` (5 min), `stale_after` (15 min), `access_timeout` (1h) and `review_grace` (1h)
  there, along with `min_credits_to_lend` (0), your own floor described under
  [Pool settings](#pool-settings).
- `state.json` is the script's own bookkeeping: invites it still has to revoke, requests it has
  served, and its heartbeat issue number.

## Use

```sh
./copilot_pool.py run                         # keep this running (tmux, launchd, systemd…)
./copilot_pool.py request https://github.com/o/r/pull/12
./copilot_pool.py request o/r#12 --via-pool   # borrow even if you still have credits
./copilot_pool.py status                      # credits, last change and last seen for each member
```

## Pool settings

`pool.json` in the pool repo is shared by all members, and any member can edit it. Scripts reread
it every cycle.

```json
{
  "credits_per_review": 100
}
```

- `credits_per_review` is the least a member must have left to take a review, or to use their own
  credits. It is also the cost per review in the local estimate (see [Credits](#credits)). Lower it
  once you know what a review actually costs. Anything other than a whole number from 1 to 1,000,000,
  or a missing or unreadable file, means 100. Your script logs a warning when the value changes.

Each member can also set `min_credits_to_lend` in their own `config.json`. You then lend only while
you have at least the larger of that and `credits_per_review` left. Below your floor you advertise 0
credits, so nobody picks you. The shared value can make you stricter but can never make you lend
below your own floor. A `credits_per_review` left in an older `config.json` is kept as
`min_credits_to_lend`.

## Credits

Credits are read from Copilot's own account endpoint (`/copilot_internal/user`), the same one the
Copilot clients use. It works for personal plans and for seats paid by an organization, which
GitHub's billing API doesn't cover. It is undocumented and could change.

If it can't be read, the script estimates usage as (reviews it requested this month) ×
`credits_per_review`, against the monthly credits entered at `init`. That estimate misses chat and
agent usage, so it is only used for your own `request`. While the script is relying on it, you
advertise 0 credits and decline others' requests. Spending beyond your allowance is never counted
as available.

## How it works

- Every `poll_interval` the daemon:
  - checks its own row in the pool repo's `README.md`, a table of every member's credits. It commits
    a new row only when its remaining, monthly, used or source value has changed. If another member
    committed in the meantime, it rereads the file and tries again;
  - every `heartbeat_interval`, edits its own `pool-heartbeat: <login>` issue, which creates no
    commit. GitHub's record of when that issue was last edited is the member's "last seen" time. The
    issue number is kept in `state.json`, and each edit also resets the title, the label and the
    open state, so a tampered heartbeat gets repaired;
  - accepts a repo invitation only when the inviter is a pool member with an open request for a PR
    in that repo;
  - handles open `copilot-request` issues assigned to it. It requests
    `copilot-pull-request-reviewer[bot]` on the PR, comments, and closes the issue. It acts only when
    the requester wrote the PR or can request reviewers on it, because the helper's token may reach
    repos the requester can't. Any failure is retried until `access_timeout`, then the issue is
    closed with the same generic message whatever the cause. That way a request can't be used to
    find out about PRs the helper can see and the requester can't;
  - removes collaborator access it granted for its own requests once they're done. A completed
    request first waits for Copilot's review, or `review_grace`, whichever comes first. Access is
    always removed within `access_timeout + review_grace` (2h) of the invite, whatever state the
    request is in. The invite is recorded before it's sent, and withdrawn if opening the request
    issue fails.
- `request` uses your own credits if you have at least `credits_per_review` left. Otherwise it picks
  the member with the most credits, among those with at least `credits_per_review` left and a
  heartbeat within `stale_after`. If that member can't request reviewers on the repo, it invites
  them, with the `triage` role on org repos and as a regular collaborator on personal repos. Then it
  opens a request issue assigned to them.

## Trust and tampering

Anyone with write access to the pool repo can edit any issue or file in it, and GitHub can't restrict
that.

- **Request issues** count only if they were last edited by their author.
- **Heartbeat issues** count only if they were last edited by their author and still have the title,
  body format and label the script writes. "Last seen" uses GitHub's own edit time.
- **The README board** can't be attributed: API commits are unsigned and can carry any author. So
  rows are treated as hints for choosing a helper. Rows are validated: known fields only, numbers
  within range, nothing dated in the future. Each member's script rewrites its own row if it doesn't
  match.
- **`pool.json`** is shared on purpose. Values are validated, but any member can change
  `credits_per_review` for everyone. Raising it only makes helpers more selective. Lowering it lets
  helpers with very few credits left accept reviews. If a review then costs more than they have
  left, it fails, or, on a seat that allows spending beyond the allowance, it is billed as overage
  to that member or their organization. If you're on such a seat, set `min_credits_to_lend` in your
  own config: the shared value can't go below it.

Apart from `pool.json`, tampering can at worst send a request to the wrong member or keep someone
from being picked. The helper's own script still checks its real credits, the rate limit and the
requester's access before spending anything.

On first `run`, the script closes the member's old `pool-status` issue from earlier versions.

## Caveats

- Collaborator access is revoked by the requester's running script, so keep `run` running after `request`.
- Org-paid seats: check that your organization allows lending its Copilot credits to outsiders.
- Org repos may block outside collaborators. Copilot code review also has to be enabled for the helper.
- A helper below its lending floor (`credits_per_review`, or its own higher `min_credits_to_lend`) declines the request. Run `request` again to pick the next member.

## Tests

```sh
python3 -m pytest
```
