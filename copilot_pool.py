#!/usr/bin/env python3
"""Share GitHub Copilot code-review credits among people who trust each other.

Coordination happens through a shared private "pool" repository:
  * each member's daemon keeps a `pool-status` issue updated with its remaining credits
  * a review request is a `copilot-request` issue assigned to the chosen helper

Commands:
  init                 one-time configuration
  run                  long-running daemon (publish status, accept invites, serve requests)
  request <pr>         get Copilot to review a PR, borrowing a pool member's credits if needed
  status               show the pool
"""

import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

API = "https://api.github.com"
COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"
REQUEST_LABEL = "copilot-request"
STATUS_LABEL = "pool-status"
REVIEW_ROLES = {"triage", "write", "maintain", "admin"}
CLOCK_SKEW = dt.timedelta(minutes=5)
DAY = dt.timedelta(days=1)
# How long an invite may exist before its request issue is opened.
PENDING_INVITE_GRACE = dt.timedelta(minutes=5)
CONFIG_DIR = Path(os.environ.get("COPILOT_POOL_HOME", "~/.config/copilot-pool")).expanduser()

# Hides why a request can't be served so requesters can't probe the helper's private PRs.
CANNOT_SERVE = "Couldn't request a Copilot review for this PR. Check the PR is open and that you can request reviewers on it."

ISSUES_QUERY = """
query($owner: String!, $name: String!, $labels: [String!], $states: [IssueState!], $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(labels: $labels, states: $states, first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        body
        createdAt
        author { login }
        editor { login }
        assignees(first: 10) { nodes { login } }
      }
    }
  }
}
"""

log = logging.getLogger("copilot-pool")


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class StripAuthOnOriginChange(urllib.request.HTTPRedirectHandler):
    """urllib forwards every header on redirect; never send the token to a different origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old, target = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new is not None and (old.scheme, old.netloc) != (target.scheme, target.netloc):
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(StripAuthOnOriginChange)


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, body=None, params=None):
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "copilot-pool")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with OPENER.open(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw).get("message", "")
            except ValueError:
                message = raw.decode(errors="replace")
            raise GitHubError(e.code, message) from None
        return json.loads(raw) if raw else None


def graphql(gh, query, variables):
    resp = gh.request("POST", "/graphql", {"query": query, "variables": variables})
    if resp.get("errors"):
        raise GitHubError(200, "; ".join(e.get("message", "") for e in resp["errors"]))
    return resp["data"]


def paginate(gh, path, params=None):
    items, page = [], 1
    while True:
        batch = gh.request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


@dataclass
class Config:
    username: str
    pool_repo: str
    monthly_quota: int
    token: str = ""
    token_command: str = ""
    billing_token: str = ""
    poll_interval: int = 60
    heartbeat_interval: int = 300
    stale_after: int = 900
    # How long a helper keeps retrying a request while waiting for its collaborator access to land.
    access_timeout: int = 3600
    # Requests this member will serve per requester in any 24h window; 0 disables the limit.
    max_requests_per_requester_per_day: int = 10
    # How long after a request completes to wait for Copilot's review before revoking the helper's access.
    review_grace: int = 3600

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text()))

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # O_CREAT's mode is ignored when the file already exists.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(asdict(self), indent=2) + "\n")

    def resolve_token(self):
        if self.token:
            return self.token
        if self.token_command:
            return subprocess.run(
                self.token_command, shell=True, check=True, capture_output=True, text=True
            ).stdout.strip()
        raise SystemExit("No token configured; run `copilot_pool.py init`.")


@dataclass
class State:
    """Local bookkeeping; the fallback usage count when the billing API is unavailable."""

    month: str = ""
    requested_count: int = 0
    handled_issues: list = field(default_factory=list)
    # requester -> ISO timestamps of requests served for them, for the rate limit
    served: dict = field(default_factory=dict)
    # collaborator invites this member sent and still has to revoke
    invites: list = field(default_factory=list)

    @classmethod
    def load(cls, path):
        try:
            state = cls(**json.loads(Path(path).read_text()))
        except FileNotFoundError:
            return cls()
        for invite in state.invites:
            # Records from older versions lack an id. Derive one from the content so it stays the same
            # across reloads and the record can still be revoked and removed.
            if "id" not in invite:
                invite["id"] = hashlib.sha256(json.dumps(invite, sort_keys=True).encode()).hexdigest()[:32]
        return state

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.replace(tmp, path)

    @classmethod
    @contextlib.contextmanager
    def locked(cls, path):
        """Read-modify-write under an exclusive lock; `request` and `run` share this file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = cls.load(path)
            yield state
            state.save(path)

    def roll_month(self, now):
        month = now.strftime("%Y-%m")
        if self.month != month:
            self.month = month
            self.requested_count = 0


@dataclass(frozen=True)
class PullRef:
    owner: str
    repo: str
    number: int

    @property
    def url(self):
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"

    @property
    def api(self):
        return f"/repos/{self.owner}/{self.repo}"

    @property
    def full_name(self):
        return f"{self.owner}/{self.repo}"


def parse_pr_ref(ref):
    if not isinstance(ref, str):
        raise ValueError(f"Not a pull request reference: {ref!r}")
    m = re.fullmatch(
        r"(?:(?:https?://)?(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+)(?:/pull/|#)(\d+)(?:[/?#].*)?", ref.strip()
    )
    # "." and ".." would turn into path traversal in API URLs.
    if not m or {m.group(1), m.group(2)} & {".", ".."}:
        raise ValueError(f"Not a pull request reference: {ref!r} (use a PR URL or owner/repo#N)")
    return PullRef(m.group(1), m.group(2), int(m.group(3)))


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def extract_json_block(text):
    m = re.search(r"```json\s*\n(.*?)\n```", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def json_block(data):
    return "```json\n" + json.dumps(data, indent=2, sort_keys=True) + "\n```"


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def valid_status(data, author, now):
    """Return the status if it is well-formed and plausible, else None."""
    if not isinstance(data, dict) or data.get("username") != author:
        return None
    remaining, quota = data.get("remaining"), data.get("quota")
    if not (is_int(remaining) and is_int(quota) and 0 <= remaining <= quota):
        return None
    try:
        updated = parse_time(data["updated_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    # A timestamp in the future would keep a status "fresh" forever.
    if updated.tzinfo is None or updated > now + CLOCK_SKEW:
        return None
    return data


def request_pr(issue):
    """Return the PullRef a request issue asks for, or None if the body is malformed."""
    payload = extract_json_block(issue["body"])
    if not isinstance(payload, dict):
        return None
    try:
        return parse_pr_ref(payload.get("pr"))
    except ValueError:
        return None


def edited_by_author(issue):
    # Anyone with write access can edit any issue, so content is only attributable to its author
    # if nobody else was the last to touch it.
    return issue["editor"] in (None, issue["author"])


def pick_peer(statuses, me, now, stale_after):
    """Pick the fresh, non-self member with the most remaining credits."""
    candidates = [
        s
        for s in statuses
        if s["username"] != me
        and s["remaining"] > 0
        and (now - parse_time(s["updated_at"])).total_seconds() <= stale_after
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (s["remaining"], s["username"].lower()))["username"]


def request_copilot(gh, pr):
    resp = gh.request(
        "POST", f"{pr.api}/pulls/{pr.number}/requested_reviewers", {"reviewers": [COPILOT_REVIEWER]}
    )
    reviewers = [r.get("login", "").lower() for r in (resp or {}).get("requested_reviewers", [])]
    return any("copilot" in login for login in reviewers)


def has_review_access(gh, pr, user):
    try:
        perm = gh.request("GET", f"{pr.api}/collaborators/{user}/permission")
    except GitHubError as e:
        if e.status == 404:
            return False
        raise
    return perm.get("role_name") in REVIEW_ROLES or perm.get("permission") in ("write", "admin")


def invite_collaborator(gh, pr, user):
    repo = gh.request("GET", pr.api)
    # The permission parameter only applies to org-owned repos; personal-repo collaborators always get write.
    body = {"permission": "triage"} if repo["owner"]["type"] == "Organization" else {}
    gh.request("PUT", f"{pr.api}/collaborators/{user}", body)


class Pool:
    def __init__(self, config, gh, billing_gh, state, state_path, clock=utcnow):
        self.cfg = config
        self.gh = gh
        self.billing_gh = billing_gh
        self.state = state
        self.state_path = state_path
        self.clock = clock
        self._last_published = None
        self._pool_repo_info = None

    @property
    def me(self):
        return self.cfg.username

    @contextlib.contextmanager
    def mutate_state(self):
        # Reload under the lock so a concurrent `request` or `run` never loses another's write.
        with State.locked(self.state_path) as state:
            yield state
        self.state = state

    def reload_state(self):
        self.state = State.load(self.state_path)

    def pool_repo_info(self):
        if self._pool_repo_info is None:
            self._pool_repo_info = self.gh.request("GET", f"/repos/{self.cfg.pool_repo}")
        return self._pool_repo_info

    def check_pool_repo(self):
        if not self.pool_repo_info().get("private"):
            raise SystemExit(f"Pool repo {self.cfg.pool_repo} must be private; it exposes members' usage and PRs.")

    def members(self):
        """Direct collaborators with write access (plus the owner of a personal pool repo)."""
        # Without affiliation=direct, org repos also list every org member with inherited access.
        collaborators = paginate(
            self.gh, f"/repos/{self.cfg.pool_repo}/collaborators", {"affiliation": "direct"}
        )
        members = {c["login"] for c in collaborators if c.get("permissions", {}).get("push")}
        owner = self.pool_repo_info()["owner"]
        if owner["type"] == "User":
            members.add(owner["login"])
        return members

    def pool_issues(self, label):
        """Open issues with `label`, including who last edited each one."""
        owner, name = self.cfg.pool_repo.split("/")
        issues, cursor = [], None
        while True:
            data = graphql(
                self.gh,
                ISSUES_QUERY,
                {"owner": owner, "name": name, "labels": [label], "states": ["OPEN"], "cursor": cursor},
            )
            conn = data["repository"]["issues"]
            for node in conn["nodes"]:
                issues.append(
                    {
                        "number": node["number"],
                        "body": node["body"],
                        "created_at": node["createdAt"],
                        "author": (node.get("author") or {}).get("login"),
                        "editor": (node.get("editor") or {}).get("login"),
                        "assignees": [a["login"] for a in node["assignees"]["nodes"]],
                    }
                )
            if not conn["pageInfo"]["hasNextPage"]:
                return issues
            cursor = conn["pageInfo"]["endCursor"]

    def usage(self):
        """Return (premium requests used this month, source)."""
        now = self.clock()
        if self.billing_gh is not None:
            try:
                data = self.billing_gh.request(
                    "GET",
                    f"/users/{self.me}/settings/billing/premium_request/usage",
                    params={"year": now.year, "month": now.month},
                )
                used = sum(
                    item.get("grossQuantity", 0)
                    for item in data.get("usageItems", [])
                    if item.get("product", "copilot").lower() == "copilot"
                )
                return used, "billing-api"
            except GitHubError as e:
                log.warning("Billing usage API unavailable (%s); using local count", e)
                self.billing_gh = None
        same_month = self.state.month == now.strftime("%Y-%m")
        return (self.state.requested_count if same_month else 0), "local-count"

    def remaining(self):
        used, _ = self.usage()
        return int(max(0, self.cfg.monthly_quota - used))

    def record_usage(self):
        with self.mutate_state() as state:
            state.roll_month(self.clock())
            state.requested_count += 1

    def read_statuses(self, members):
        now = self.clock()
        statuses = []
        for issue in self.pool_issues(STATUS_LABEL):
            if issue["author"] not in members or not edited_by_author(issue):
                continue
            status = valid_status(extract_json_block(issue["body"]), issue["author"], now)
            if status:
                statuses.append({**status, "issue_number": issue["number"]})
        return statuses

    def publish_status(self, members):
        used, source = self.usage()
        remaining = int(max(0, self.cfg.monthly_quota - used))
        now = self.clock()
        if (
            self._last_published
            and self._last_published[0] == remaining
            and (now - self._last_published[1]).total_seconds() < self.cfg.heartbeat_interval
        ):
            return
        status = {
            "username": self.me,
            "remaining": remaining,
            "quota": self.cfg.monthly_quota,
            "used": used,
            "source": source,
            "updated_at": now.isoformat(timespec="seconds"),
        }
        body = f"Copilot credit status for @{self.me}, maintained by copilot_pool.py.\n\n{json_block(status)}"
        # Match by author alone so a status someone else tampered with gets overwritten, not duplicated.
        mine = [i for i in self.pool_issues(STATUS_LABEL) if i["author"] == self.me]
        if mine:
            self.gh.request("PATCH", f"/repos/{self.cfg.pool_repo}/issues/{mine[0]['number']}", {"body": body})
        else:
            self.gh.request(
                "POST",
                f"/repos/{self.cfg.pool_repo}/issues",
                {"title": f"pool-status: {self.me}", "body": body, "labels": [STATUS_LABEL]},
            )
        self._last_published = (remaining, now)
        log.debug("Published status: %s", status)

    def accept_invitations(self, members, open_requests):
        """Accept an invite only if its inviter has an open request for a PR in that repo."""
        wanted = set()
        for issue in open_requests:
            pr = request_pr(issue)
            if pr and issue["author"] in members and edited_by_author(issue):
                wanted.add((issue["author"], pr.full_name.lower()))
        for inv in paginate(self.gh, "/user/repository_invitations"):
            inviter = inv["inviter"]["login"]
            repo = inv["repository"]["full_name"]
            if (inviter, repo.lower()) in wanted:
                self.gh.request("PATCH", f"/user/repository_invitations/{inv['id']}")
                log.info("Accepted invitation to %s from %s", repo, inviter)

    def _close(self, number, comment, reason="completed"):
        repo = self.cfg.pool_repo
        self.gh.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": comment})
        self.gh.request(
            "PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed", "state_reason": reason}
        )

    def _served_recently(self, requester):
        cutoff = self.clock() - DAY
        return [t for t in self.state.served.get(requester, []) if parse_time(t) > cutoff]

    def serve_requests(self, members, open_requests):
        for issue in open_requests:
            if self.me not in issue["assignees"]:
                continue
            try:
                self.handle_request(issue, members)
            except Exception:
                # One bad request must not block the others.
                log.exception("Request #%s failed", issue["number"])

    def handle_request(self, issue, members):
        number = issue["number"]
        requester = issue["author"]
        if requester not in members:
            log.warning("Ignoring request #%s from non-member %s", number, requester)
            return
        if not edited_by_author(issue):
            self._close(number, f"@{requester} this request was edited by @{issue['editor']}; not serving it.", "not_planned")
            return
        pr = request_pr(issue)
        if pr is None:
            self._close(number, f"@{requester} this request has no valid `pr` field; not serving it.", "not_planned")
            return
        if number in self.state.handled_issues:
            self._close(number, "Already handled.")
            return

        limit = self.cfg.max_requests_per_requester_per_day
        if limit and len(self._served_recently(requester)) >= limit:
            self._close(
                number,
                f"@{requester} @{self.me} serves at most {limit} requests per person per day. Try again later.",
                "not_planned",
            )
            return
        if self.remaining() <= 0:
            self._close(
                number,
                f"@{requester} sorry, @{self.me} is out of Copilot credits. Run `request` again to pick someone else.",
                "not_planned",
            )
            return

        age = (self.clock() - parse_time(issue["created_at"])).total_seconds()
        try:
            pull = self.gh.request("GET", f"{pr.api}/pulls/{pr.number}")
            # The helper's token may reach repos the requester can't; don't act on those.
            authorized = pull["user"]["login"] == requester or has_review_access(self.gh, pr, requester)
            listed = request_copilot(self.gh, pr) if pull["state"] == "open" and authorized else None
        except GitHubError as e:
            log.info("Request #%s for %s not servable yet: %s", number, pr.url, e)
            listed = None
        if listed is None:
            # Every failure looks the same and waits the same time so the outcome reveals nothing about the PR.
            if age >= self.cfg.access_timeout:
                self._close(number, f"@{requester} {CANNOT_SERVE}", "not_planned")
            return

        now = self.clock()
        with self.mutate_state() as state:
            state.handled_issues = (state.handled_issues + [number])[-500:]
            cutoff = now - DAY
            recent = [t for t in state.served.get(requester, []) if parse_time(t) > cutoff]
            state.served[requester] = recent + [now.isoformat(timespec="seconds")]
            state.roll_month(now)
            state.requested_count += 1
        note = (
            ""
            if listed
            else "\n\nNote: GitHub accepted the request but doesn't list Copilot as a reviewer; "
            "check that @" + self.me + " has Copilot code review enabled."
        )
        self._close(number, f"@{requester} requested a Copilot review on {pr.url} using @{self.me}'s credits.{note}")
        log.info("Requested Copilot review on %s for %s", pr.url, requester)

    def _invite_still_needed(self, inv, open_requests):
        try:
            age = self.clock() - parse_time(inv["created_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return False  # fail closed: an invite we can't date gets revoked
        # Hard cap so no request state, open issue or tampering can keep access alive indefinitely.
        if age >= dt.timedelta(seconds=self.cfg.access_timeout + self.cfg.review_grace):
            return False
        for issue in open_requests:
            pr = request_pr(issue)
            if (
                issue["author"] == self.me
                and edited_by_author(issue)
                and inv["helper"] in issue["assignees"]
                and pr
                and pr.full_name.lower() == inv["repo"].lower()
            ):
                return True
        if inv.get("issue") is None:
            # `request` is between sending the invite and opening the issue, or crashed in between.
            return age < PENDING_INVITE_GRACE
        try:
            issue = self.gh.request("GET", f"/repos/{self.cfg.pool_repo}/issues/{inv['issue']}")
        except GitHubError as e:
            if e.status in (404, 410):
                return False
            raise
        if issue["state"] != "closed":
            return True
        if issue.get("state_reason") != "completed":
            return False
        if (self.clock() - parse_time(issue["closed_at"])).total_seconds() >= self.cfg.review_grace:
            return False
        # Give Copilot time to post its review before pulling the access that requested it.
        reviews = paginate(self.gh, f"/repos/{inv['repo']}/pulls/{inv['pr']}/reviews")
        return not any("copilot" in ((r.get("user") or {}).get("login") or "").lower() for r in reviews)

    def revoke_invite(self, inv):
        api = f"/repos/{inv['repo']}"
        for pending in paginate(self.gh, f"{api}/invitations"):
            if (pending.get("invitee") or {}).get("login") == inv["helper"]:
                self.gh.request("DELETE", f"{api}/invitations/{pending['id']}")
        try:
            self.gh.request("DELETE", f"{api}/collaborators/{inv['helper']}")
        except GitHubError as e:
            if e.status != 404:
                raise
        log.info("Revoked @%s's access to %s", inv["helper"], inv["repo"])

    def forget_invite(self, invite_id):
        with self.mutate_state() as state:
            state.invites = [i for i in state.invites if i.get("id") != invite_id]

    def cleanup_invites(self, open_requests):
        for inv in list(self.state.invites):
            try:
                if self._invite_still_needed(inv, open_requests):
                    continue
                self.revoke_invite(inv)
                self.forget_invite(inv["id"])
            except GitHubError as e:
                log.error("Couldn't revoke @%s's access to %s: %s", inv.get("helper"), inv.get("repo"), e)
            except KeyError as e:
                # Without a repo or helper there is nothing to revoke; keeping it would log forever.
                log.error("Dropping unrevocable invite record %s (missing %s)", inv, e)
                self.forget_invite(inv["id"])

    def request_review(self, pr, via_pool=False):
        """Requester side. Returns a human-readable outcome."""
        if not via_pool and self.remaining() > 0:
            listed = request_copilot(self.gh, pr)
            self.record_usage()
            suffix = "" if listed else " (GitHub didn't list Copilot as a reviewer; check your Copilot plan)"
            return f"Requested Copilot review on {pr.url} with your own credits{suffix}"

        members = self.members()
        peer = pick_peer(self.read_statuses(members), self.me, self.clock(), self.cfg.stale_after)
        if peer is None:
            raise SystemExit("No online pool member has credits left.")

        invite = None
        if not has_review_access(self.gh, pr, peer):
            invite = {
                "id": uuid.uuid4().hex,
                "issue": None,
                "repo": pr.full_name,
                "pr": pr.number,
                "helper": peer,
                "created_at": self.clock().isoformat(timespec="seconds"),
            }
            # Record before granting, so a crash or failure below still leaves something to revoke.
            with self.mutate_state() as state:
                state.invites.append(invite)
        invited = invite is not None

        payload = {"pr": pr.url, "requester": self.me}
        body = (
            f"@{peer}, please request a Copilot review on {pr.url}.\n\n"
            f"{'I invited you as a collaborator; your daemon will accept it.' if invited else ''}\n\n"
            f"{json_block(payload)}"
        )
        try:
            if invite:
                invite_collaborator(self.gh, pr, peer)
            issue = self.gh.request(
                "POST",
                f"/repos/{self.cfg.pool_repo}/issues",
                {
                    "title": f"Copilot review: {pr.owner}/{pr.repo}#{pr.number}",
                    "body": body,
                    "labels": [REQUEST_LABEL],
                    "assignees": [peer],
                },
            )
        except Exception:
            if invite:
                try:
                    self.revoke_invite(invite)
                    self.forget_invite(invite["id"])
                except GitHubError as e:
                    log.error("Couldn't roll back invite for @%s; `run` will retry: %s", peer, e)
            raise
        if invite:
            with self.mutate_state() as state:
                for record in state.invites:
                    if record["id"] == invite["id"]:
                        record["issue"] = issue["number"]
        return f"Asked @{peer} to request Copilot on {pr.url}{' (invited as collaborator)' if invited else ''}: {issue['html_url']}"

    def run_once(self):
        self.reload_state()
        members = self.members()
        open_requests = self.pool_issues(REQUEST_LABEL)
        steps = (
            (self.publish_status, (members,)),
            (self.accept_invitations, (members, open_requests)),
            (self.serve_requests, (members, open_requests)),
            (self.cleanup_invites, (open_requests,)),
        )
        for step, args in steps:
            try:
                step(*args)
            except Exception:
                # Steps are independent; a failure in one must not starve the rest.
                log.exception("%s failed", step.__name__)


def ensure_labels(gh, repo):
    for name, color in ((REQUEST_LABEL, "8250df"), (STATUS_LABEL, "0e8a16")):
        try:
            gh.request("POST", f"/repos/{repo}/labels", {"name": name, "color": color})
        except GitHubError as e:
            if e.status != 422:  # 422 means the label already exists
                raise


def cmd_init(args):
    config_path = CONFIG_DIR / "config.json"
    print("Repo token: a classic PAT with `repo` scope. Leave blank to use `gh auth token`.")
    token = getpass.getpass("Token: ").strip()
    token_command = "" if token else "gh auth token"
    cfg = Config(username="", pool_repo="", monthly_quota=0, token=token, token_command=token_command)
    gh = GitHub(cfg.resolve_token())
    login = gh.request("GET", "/user")["login"]
    print(f"Authenticated as {login}")

    print("Optional billing token: fine-grained PAT with 'Plan: read' for accurate usage. Blank to skip.")
    billing_token = getpass.getpass("Billing token: ").strip()
    pool_repo = input("Pool repo (owner/name): ").strip()
    quota = input("Monthly premium request quota [300]: ").strip() or "300"
    limit = input("Max requests you'll serve per member per day, 0 = unlimited [10]: ").strip() or "10"

    if not gh.request("GET", f"/repos/{pool_repo}").get("private"):
        raise SystemExit(f"Pool repo {pool_repo} must be private.")
    ensure_labels(gh, pool_repo)
    cfg = Config(
        username=login,
        pool_repo=pool_repo,
        monthly_quota=int(quota),
        token=token,
        token_command=token_command,
        billing_token=billing_token,
        max_requests_per_requester_per_day=int(limit),
    )
    cfg.save(config_path)
    print(f"Saved {config_path}")


def build_pool():
    config_path = CONFIG_DIR / "config.json"
    if not config_path.exists():
        raise SystemExit("Not configured; run `copilot_pool.py init` first.")
    cfg = Config.load(config_path)
    gh = GitHub(cfg.resolve_token())
    billing_gh = GitHub(cfg.billing_token) if cfg.billing_token else gh
    state_path = CONFIG_DIR / "state.json"
    pool = Pool(cfg, gh, billing_gh, State.load(state_path), state_path)
    pool.check_pool_repo()
    return pool


def cmd_run(args):
    pool = build_pool()
    log.info("Serving pool %s as %s every %ss", pool.cfg.pool_repo, pool.me, pool.cfg.poll_interval)
    while True:
        try:
            pool.run_once()
        except Exception:
            # A long-running daemon must survive transient failures (network, token refresh).
            log.exception("Cycle failed")
        time.sleep(pool.cfg.poll_interval)


def cmd_request(args):
    print(build_pool().request_review(parse_pr_ref(args.pr), via_pool=args.via_pool))


def cmd_status(args):
    pool = build_pool()
    now = pool.clock()
    statuses = sorted(pool.read_statuses(pool.members()), key=lambda s: -s["remaining"])
    print(f"{'member':<24}{'remaining':>10}{'quota':>8}  {'source':<12}last seen")
    for s in statuses:
        age = int((now - parse_time(s["updated_at"])).total_seconds())
        stale = " (stale)" if age > pool.cfg.stale_after else ""
        print(f"{s['username']:<24}{s['remaining']:>10}{s['quota']:>8}  {s.get('source', '?'):<12}{age}s ago{stale}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("status").set_defaults(func=cmd_status)
    req = sub.add_parser("request")
    req.add_argument("pr", help="PR URL or owner/repo#number")
    req.add_argument("--via-pool", action="store_true", help="use a pool member even if you have credits")
    req.set_defaults(func=cmd_request)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except GitHubError as e:
        sys.exit(f"GitHub API error: {e}")


if __name__ == "__main__":
    main()
