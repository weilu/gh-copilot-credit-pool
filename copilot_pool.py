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
import datetime as dt
import getpass
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

API = "https://api.github.com"
COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"
REQUEST_LABEL = "copilot-request"
STATUS_LABEL = "pool-status"
REVIEW_ROLES = {"triage", "write", "maintain", "admin"}
CONFIG_DIR = Path(os.environ.get("COPILOT_POOL_HOME", "~/.config/copilot-pool")).expanduser()

log = logging.getLogger("copilot-pool")


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


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
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw).get("message", "")
            except ValueError:
                message = raw.decode(errors="replace")
            raise GitHubError(e.code, message) from None
        return json.loads(raw) if raw else None


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

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text()))

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)

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

    @classmethod
    def load(cls, path):
        try:
            return cls(**json.loads(Path(path).read_text()))
        except FileNotFoundError:
            return cls()

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")

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


def parse_pr_ref(ref):
    m = re.fullmatch(
        r"(?:(?:https?://)?(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+)(?:/pull/|#)(\d+)(?:[/?#].*)?", ref.strip()
    )
    if not m:
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


def pick_peer(statuses, me, now, stale_after):
    """Pick the fresh, non-self member with the most remaining credits."""
    candidates = [
        s
        for s in statuses
        if s["username"] != me
        and s.get("remaining", 0) > 0
        and (now - parse_time(s["updated_at"])).total_seconds() <= stale_after
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (s["remaining"], s["username"].lower()))["username"]


def list_issues(gh, repo, **params):
    issues = gh.request(
        "GET", f"/repos/{repo}/issues", params={"state": "open", "per_page": 100, **params}
    )
    return [i for i in issues if "pull_request" not in i]


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

    @property
    def me(self):
        return self.cfg.username

    def save_state(self):
        self.state.save(self.state_path)

    def members(self):
        collaborators = self.gh.request(
            "GET", f"/repos/{self.cfg.pool_repo}/collaborators", params={"per_page": 100}
        )
        return {c["login"] for c in collaborators}

    def usage(self):
        """Return (premium requests used this month, source)."""
        now = self.clock()
        self.state.roll_month(now)
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
        return self.state.requested_count, "local-count"

    def remaining(self):
        used, _ = self.usage()
        return max(0, self.cfg.monthly_quota - used)

    def record_usage(self):
        self.state.roll_month(self.clock())
        self.state.requested_count += 1
        self.save_state()

    def read_statuses(self, members):
        statuses = []
        for issue in list_issues(self.gh, self.cfg.pool_repo, labels=STATUS_LABEL):
            author = issue["user"]["login"]
            data = extract_json_block(issue["body"])
            # Only trust a status published by the member it describes.
            if author not in members or not data or data.get("username") != author:
                continue
            statuses.append({**data, "issue_number": issue["number"]})
        return statuses

    def publish_status(self, members, force=False):
        used, source = self.usage()
        remaining = max(0, self.cfg.monthly_quota - used)
        now = self.clock()
        if (
            not force
            and self._last_published
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
        mine = [s for s in self.read_statuses(members) if s["username"] == self.me]
        if mine:
            self.gh.request(
                "PATCH", f"/repos/{self.cfg.pool_repo}/issues/{mine[0]['issue_number']}", {"body": body}
            )
        else:
            self.gh.request(
                "POST",
                f"/repos/{self.cfg.pool_repo}/issues",
                {"title": f"pool-status: {self.me}", "body": body, "labels": [STATUS_LABEL]},
            )
        self._last_published = (remaining, now)
        log.debug("Published status: %s", status)

    def accept_invitations(self, members):
        for inv in self.gh.request("GET", "/user/repository_invitations", params={"per_page": 100}):
            inviter = inv["inviter"]["login"]
            repo = inv["repository"]["full_name"]
            if inviter in members and inviter != self.me:
                self.gh.request("PATCH", f"/user/repository_invitations/{inv['id']}")
                log.info("Accepted invitation to %s from %s", repo, inviter)

    def _close(self, issue, comment, reason="completed"):
        repo = self.cfg.pool_repo
        self.gh.request("POST", f"/repos/{repo}/issues/{issue['number']}/comments", {"body": comment})
        self.gh.request(
            "PATCH", f"/repos/{repo}/issues/{issue['number']}", {"state": "closed", "state_reason": reason}
        )

    def serve_requests(self, members):
        issues = list_issues(self.gh, self.cfg.pool_repo, labels=REQUEST_LABEL, assignee=self.me)
        for issue in issues:
            try:
                self.handle_request(issue, members)
            except GitHubError as e:
                log.error("Request #%s failed: %s", issue["number"], e)

    def handle_request(self, issue, members):
        number = issue["number"]
        requester = issue["user"]["login"]
        payload = extract_json_block(issue["body"])
        if requester not in members or not payload or "pr" not in payload:
            log.warning("Ignoring request #%s from %s: not a valid pool request", number, requester)
            return
        if number in self.state.handled_issues:
            self._close(issue, "Already handled.")
            return
        pr = parse_pr_ref(payload["pr"])
        age = (self.clock() - parse_time(issue["created_at"])).total_seconds()

        if self.remaining() <= 0:
            self._close(
                issue,
                f"@{requester} sorry, @{self.me} is out of Copilot credits. Run `request` again to pick someone else.",
                "not_planned",
            )
            return

        try:
            pull = self.gh.request("GET", f"{pr.api}/pulls/{pr.number}")
        except GitHubError as e:
            # 404/403 usually means the collaborator invite hasn't been accepted yet.
            if e.status in (403, 404) and age < self.cfg.access_timeout:
                log.info("No access to %s yet; will retry", pr.url)
                return
            self._close(issue, f"@{requester} couldn't access {pr.url}: {e}", "not_planned")
            return
        if pull["state"] != "open":
            self._close(issue, f"{pr.url} is no longer open; nothing to do.", "not_planned")
            return

        try:
            listed = request_copilot(self.gh, pr)
        except GitHubError as e:
            if e.status in (403, 404, 422) and age < self.cfg.access_timeout:
                log.info("Can't request reviewers on %s yet (%s); will retry", pr.url, e)
                return
            self._close(issue, f"@{requester} failed to request Copilot on {pr.url}: {e}", "not_planned")
            return

        self.state.handled_issues = (self.state.handled_issues + [number])[-500:]
        self.record_usage()
        note = (
            ""
            if listed
            else "\n\nNote: GitHub accepted the request but doesn't list Copilot as a reviewer; "
            "check that @" + self.me + " has Copilot code review enabled."
        )
        self._close(issue, f"@{requester} requested a Copilot review on {pr.url} using @{self.me}'s credits.{note}")
        log.info("Requested Copilot review on %s for %s", pr.url, requester)

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

        invited = False
        if not has_review_access(self.gh, pr, peer):
            invite_collaborator(self.gh, pr, peer)
            invited = True

        payload = {"pr": pr.url, "requester": self.me}
        body = (
            f"@{peer}, please request a Copilot review on {pr.url}.\n\n"
            f"{'I invited you as a collaborator; your daemon will accept it.' if invited else ''}\n\n"
            f"{json_block(payload)}"
        )
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
        return f"Asked @{peer} to request Copilot on {pr.url}{' (invited as collaborator)' if invited else ''}: {issue['html_url']}"

    def run_once(self):
        members = self.members()
        for step in (self.publish_status, self.accept_invitations, self.serve_requests):
            try:
                step(members)
            except GitHubError as e:
                log.error("%s failed: %s", step.__name__, e)


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

    gh.request("GET", f"/repos/{pool_repo}")
    ensure_labels(gh, pool_repo)
    cfg = Config(
        username=login,
        pool_repo=pool_repo,
        monthly_quota=int(quota),
        token=token,
        token_command=token_command,
        billing_token=billing_token,
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
    return Pool(cfg, gh, billing_gh, State.load(state_path), state_path)


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
    statuses = sorted(pool.read_statuses(pool.members()), key=lambda s: -s.get("remaining", 0))
    print(f"{'member':<24}{'remaining':>10}{'quota':>8}  {'source':<12}last seen")
    for s in statuses:
        age = int((now - parse_time(s["updated_at"])).total_seconds())
        stale = " (stale)" if age > pool.cfg.stale_after else ""
        print(f"{s['username']:<24}{s['remaining']:>10}{s['quota']:>8}  {s['source']:<12}{age}s ago{stale}")


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
