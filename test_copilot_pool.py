import datetime as dt
import json
import os
import urllib.request

import pytest

import copilot_pool as cp

NOW = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)
POOL = "team/pool"
PR = "https://github.com/octo/repo/pull/3"


class FakeGitHub:
    """Routes (method, path) to canned responses and records every call.

    Pool issues are served through the GraphQL route from `self.issues[label]`.
    """

    def __init__(self, routes=None, issues=None):
        self.routes = dict(routes or {})
        self.issues = issues or {}
        self.calls = []

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        if (method, path) == ("POST", "/graphql") and (method, path) not in self.routes:
            nodes = self.issues.get(body["variables"]["labels"][0], [])
            page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
            return {"data": {"repository": {"issues": page}}}
        resp = self.routes.get((method, path))
        if isinstance(resp, Exception):
            raise resp
        return resp(body) if callable(resp) else resp

    def called(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def node(number, author, body, editor=None, assignees=(), created_at=NOW):
    return {
        "number": number,
        "body": body,
        "createdAt": created_at.isoformat(),
        "author": {"login": author} if author else None,
        "editor": {"login": editor} if editor else None,
        "assignees": {"nodes": [{"login": a} for a in assignees]},
    }


def status_body(user, remaining, updated_at=NOW, quota=300, **extra):
    data = {
        "username": user,
        "remaining": remaining,
        "quota": quota,
        "used": quota - remaining if isinstance(remaining, int) else 0,
        "source": "billing-api",
        "updated_at": updated_at.isoformat() if isinstance(updated_at, dt.datetime) else updated_at,
        **extra,
    }
    return cp.json_block(data)


def status_node(user, remaining, number=1, author=None, editor=None, **kw):
    return node(number, author or user, status_body(user, remaining, **kw), editor=editor)


def request_node(pr=PR, requester="alice", helper="bob", number=7, created_at=NOW, editor=None):
    body = cp.json_block({"pr": pr, "requester": requester})
    return node(number, requester, body, editor=editor, assignees=[helper], created_at=created_at)


def as_issue(n):
    """Convert a GraphQL node to the dict shape Pool.pool_issues returns."""
    return {
        "number": n["number"],
        "body": n["body"],
        "created_at": n["createdAt"],
        "author": (n["author"] or {}).get("login"),
        "editor": (n["editor"] or {}).get("login"),
        "assignees": [a["login"] for a in n["assignees"]["nodes"]],
    }


def pool_routes(members=("alice", "bob"), owner_type="User", private=True):
    return {
        ("GET", f"/repos/{POOL}"): {"private": private, "owner": {"login": "team", "type": owner_type}},
        ("GET", f"/repos/{POOL}/collaborators"): [
            {"login": m, "permissions": {"push": True}} for m in members
        ],
    }


def make_pool(gh, tmp_path, me="alice", used=0, quota=300, billing=True, **cfg):
    config = cp.Config(username=me, pool_repo=POOL, monthly_quota=quota, token="t", **cfg)
    billing_gh = FakeGitHub(
        {
            ("GET", f"/users/{me}/settings/billing/premium_request/usage"): {
                "usageItems": [{"product": "Copilot", "grossQuantity": used}]
            }
        }
    )
    return cp.Pool(config, gh, billing_gh if billing else None, cp.State(), tmp_path / "state.json", clock=lambda: NOW)


def seed_state(pool, **fields):
    """Write state to disk the way another process would, then load it."""
    with pool.mutate_state() as state:
        for key, value in fields.items():
            setattr(state, key, value)
    pool.reload_state()


def disk_state(pool):
    return cp.State.load(pool.state_path)


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "https://github.com/octo/repo/pull/12",
        "https://github.com/octo/repo/pull/12/files",
        "github.com/octo/repo/pull/12",
        "octo/repo#12",
    ],
)
def test_parse_pr_ref(ref):
    assert cp.parse_pr_ref(ref) == cp.PullRef("octo", "repo", 12)


@pytest.mark.parametrize(
    "ref",
    [
        "https://github.com/octo/repo/issues/12",
        "https://github.com/../x/pull/1",
        "octo/..#1",
        "./repo#1",
        None,
        42,
    ],
)
def test_parse_pr_ref_rejects_invalid_and_traversal(ref):
    with pytest.raises(ValueError):
        cp.parse_pr_ref(ref)


# --- status validation and peer selection ----------------------------------


@pytest.mark.parametrize(
    "data",
    [
        [1],
        "text",
        {"username": "bob", "remaining": "lots", "quota": 300, "updated_at": NOW.isoformat()},
        {"username": "bob", "remaining": True, "quota": 300, "updated_at": NOW.isoformat()},
        {"username": "bob", "remaining": 301, "quota": 300, "updated_at": NOW.isoformat()},
        {"username": "bob", "remaining": -1, "quota": 300, "updated_at": NOW.isoformat()},
        {"username": "bob", "remaining": 10, "quota": 300, "updated_at": "yesterday"},
        {"username": "bob", "remaining": 10, "quota": 300, "updated_at": "2026-10-30T12:00:00"},
        {"username": "bob", "remaining": 10, "quota": 300, "updated_at": (NOW + dt.timedelta(days=1)).isoformat()},
        {"username": "bob", "remaining": 10, "quota": 300},
        {"username": "carol", "remaining": 10, "quota": 300, "updated_at": NOW.isoformat()},
    ],
)
def test_valid_status_rejects_malformed(data):
    assert cp.valid_status(data, "bob", NOW) is None


def test_valid_status_accepts_small_clock_skew():
    data = {"username": "bob", "remaining": 10, "quota": 300, "updated_at": (NOW + dt.timedelta(minutes=1)).isoformat()}
    assert cp.valid_status(data, "bob", NOW) == data


def test_pick_peer_prefers_most_remaining_and_skips_self_stale_and_empty():
    stale = NOW - dt.timedelta(hours=1)
    statuses = [
        {"username": "alice", "remaining": 999, "updated_at": NOW.isoformat()},
        {"username": "bob", "remaining": 50, "updated_at": NOW.isoformat()},
        {"username": "carol", "remaining": 200, "updated_at": stale.isoformat()},
        {"username": "dave", "remaining": 120, "updated_at": NOW.isoformat()},
        {"username": "erin", "remaining": 0, "updated_at": NOW.isoformat()},
    ]
    assert cp.pick_peer(statuses, "alice", NOW, stale_after=900) == "dave"


def test_pick_peer_none_available():
    assert cp.pick_peer([], "alice", NOW, 900) is None


# --- membership --------------------------------------------------------------


def test_members_are_direct_pushers_plus_personal_owner(tmp_path):
    routes = pool_routes()
    routes[("GET", f"/repos/{POOL}/collaborators")] = [
        {"login": "bob", "permissions": {"push": True}},
        {"login": "reader", "permissions": {"pull": True, "push": False}},
    ]
    gh = FakeGitHub(routes)
    pool = make_pool(gh, tmp_path)
    assert pool.members() == {"bob", "team"}
    (_, _, _, params), = gh.called("GET", f"/repos/{POOL}/collaborators")
    assert params["affiliation"] == "direct"


def test_members_exclude_org_owner(tmp_path):
    gh = FakeGitHub(pool_routes(owner_type="Organization"))
    assert make_pool(gh, tmp_path).members() == {"alice", "bob"}


def test_members_paginates(tmp_path):
    pages = {
        1: [{"login": f"u{i}", "permissions": {"push": True}} for i in range(100)],
        2: [{"login": "last", "permissions": {"push": True}}],
    }

    class Paged(FakeGitHub):
        def request(self, method, path, body=None, params=None):
            if path == f"/repos/{POOL}/collaborators":
                return pages[params["page"]]
            return super().request(method, path, body, params)

    pool = make_pool(Paged(pool_routes(owner_type="Organization")), tmp_path)
    assert len(pool.members()) == 101


def test_check_pool_repo_refuses_public(tmp_path):
    pool = make_pool(FakeGitHub(pool_routes(private=False)), tmp_path)
    with pytest.raises(SystemExit):
        pool.check_pool_repo()


# --- credits -------------------------------------------------------------------


def test_remaining_from_billing_api(tmp_path):
    assert make_pool(FakeGitHub(), tmp_path, used=280.5).remaining() == 19


def test_remaining_falls_back_to_local_count_when_billing_fails(tmp_path):
    pool = make_pool(FakeGitHub(), tmp_path)
    pool.billing_gh = FakeGitHub(
        {("GET", "/users/alice/settings/billing/premium_request/usage"): cp.GitHubError(403, "no")}
    )
    pool.state.month = "2026-10"
    pool.state.requested_count = 299
    assert pool.remaining() == 1
    assert pool.billing_gh is None


def test_local_count_resets_each_month(tmp_path):
    pool = make_pool(FakeGitHub(), tmp_path, billing=False)
    pool.state.month = "2026-09"
    pool.state.requested_count = 300
    assert pool.remaining() == 300


# --- statuses ------------------------------------------------------------------


def test_read_statuses_drops_untrusted_and_malformed(tmp_path):
    gh = FakeGitHub(
        pool_routes(members=("alice", "bob", "carol", "dave", "erin")),
        issues={
            cp.STATUS_LABEL: [
                status_node("bob", 10, number=1),
                status_node("mallory", 999, number=2),
                status_node("carol", 999, number=3, author="bob"),
                status_node("dave", 50, number=4, editor="bob"),
                node(5, "erin", "```json\n[1]\n```"),
                status_node("alice", 5, number=6, editor="alice"),
            ]
        },
    )
    pool = make_pool(gh, tmp_path)
    statuses = pool.read_statuses({"alice", "bob", "carol", "dave", "erin"})
    assert sorted(s["username"] for s in statuses) == ["alice", "bob"]


def test_publish_status_creates_then_updates(tmp_path):
    gh = FakeGitHub(pool_routes())
    pool = make_pool(gh, tmp_path, used=100)
    pool.publish_status({"alice"})
    (_, _, body, _), = gh.called("POST", f"/repos/{POOL}/issues")
    status = cp.extract_json_block(body["body"])
    assert status["remaining"] == 200
    assert cp.valid_status(status, "alice", NOW)
    assert body["labels"] == [cp.STATUS_LABEL]

    gh.issues[cp.STATUS_LABEL] = [status_node("alice", 200, number=5)]
    pool._last_published = None
    pool.publish_status({"alice"})
    assert gh.called("PATCH", f"/repos/{POOL}/issues/5")


def test_publish_status_overwrites_own_tampered_issue(tmp_path):
    gh = FakeGitHub(pool_routes(), issues={cp.STATUS_LABEL: [status_node("alice", 999, number=5, editor="bob")]})
    make_pool(gh, tmp_path).publish_status({"alice", "bob"})
    assert gh.called("PATCH", f"/repos/{POOL}/issues/5")
    assert not gh.called("POST", f"/repos/{POOL}/issues")


def test_publish_status_skips_when_unchanged_within_heartbeat(tmp_path):
    gh = FakeGitHub(pool_routes())
    pool = make_pool(gh, tmp_path)
    pool.publish_status({"alice"})
    pool.publish_status({"alice"})
    assert len(gh.called("POST", f"/repos/{POOL}/issues")) == 1


# --- invitations ---------------------------------------------------------------


def invitation(inv_id, inviter, repo):
    return {"id": inv_id, "inviter": {"login": inviter}, "repository": {"full_name": repo}}


def test_accept_invitations_only_for_open_requests_from_inviter(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", "/user/repository_invitations"): [
                invitation(1, "alice", "Octo/Repo"),
                invitation(2, "alice", "alice/unrelated"),
                invitation(3, "stranger", "octo/repo"),
                invitation(4, "carol", "octo/repo"),
            ]
        }
    )
    pool = make_pool(gh, tmp_path, me="bob")
    # Assigned to someone else: still accepted, since the requester may re-pick a helper.
    requests = [as_issue(request_node(requester="alice", helper="dave"))]
    pool.accept_invitations({"alice", "bob", "carol"}, requests)
    accepted = [c[1] for c in gh.calls if c[0] == "PATCH"]
    assert accepted == ["/user/repository_invitations/1"]


def test_accept_invitations_ignores_tampered_request(tmp_path):
    gh = FakeGitHub({("GET", "/user/repository_invitations"): [invitation(1, "alice", "octo/repo")]})
    pool = make_pool(gh, tmp_path, me="bob")
    pool.accept_invitations({"alice", "bob", "carol"}, [as_issue(request_node(editor="carol"))])
    assert not gh.called("PATCH", "/user/repository_invitations/1")


# --- requester side ------------------------------------------------------------


def test_request_review_uses_own_credits_when_available(tmp_path):
    gh = FakeGitHub(
        {("POST", "/repos/octo/repo/pulls/3/requested_reviewers"): {"requested_reviewers": [{"login": "Copilot"}]}}
    )
    pool = make_pool(gh, tmp_path, used=10)
    assert "your own credits" in pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert pool.state.requested_count == 1


def test_request_review_borrows_from_richest_peer_and_records_invite(tmp_path):
    routes = pool_routes(members=("alice", "bob", "carol"))
    routes.update(
        {
            ("GET", "/repos/octo/repo/collaborators/carol/permission"): {"permission": "read", "role_name": "read"},
            ("GET", "/repos/octo/repo"): {"owner": {"type": "Organization"}},
            ("POST", f"/repos/{POOL}/issues"): {"number": 9, "html_url": "https://github.com/team/pool/issues/9"},
        }
    )
    gh = FakeGitHub(routes, issues={cp.STATUS_LABEL: [status_node("bob", 10, number=1), status_node("carol", 90, number=2)]})
    pool = make_pool(gh, tmp_path, used=300)
    msg = pool.request_review(cp.parse_pr_ref("octo/repo#3"))

    assert "@carol" in msg and "invited" in msg
    (_, _, invite, _), = gh.called("PUT", "/repos/octo/repo/collaborators/carol")
    assert invite == {"permission": "triage"}
    (_, _, issue, _), = gh.called("POST", f"/repos/{POOL}/issues")
    assert issue["assignees"] == ["carol"]
    assert cp.extract_json_block(issue["body"])["pr"] == PR
    (record,) = disk_state(pool).invites
    assert {k: record[k] for k in ("issue", "repo", "pr", "helper", "created_at")} == {
        "issue": 9, "repo": "octo/repo", "pr": 3, "helper": "carol", "created_at": NOW.isoformat(timespec="seconds")
    }


def test_request_review_skips_invite_when_peer_has_access(tmp_path):
    routes = pool_routes()
    routes.update(
        {
            ("GET", "/repos/octo/repo/collaborators/bob/permission"): {"permission": "write", "role_name": "write"},
            ("POST", f"/repos/{POOL}/issues"): {"number": 9, "html_url": "u"},
        }
    )
    gh = FakeGitHub(routes, issues={cp.STATUS_LABEL: [status_node("bob", 10)]})
    pool = make_pool(gh, tmp_path, used=300)
    pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert not gh.called("PUT", "/repos/octo/repo/collaborators/bob")
    assert pool.state.invites == []


def test_invite_omits_permission_for_personal_repo():
    gh = FakeGitHub({("GET", "/repos/octo/repo"): {"owner": {"type": "User"}}})
    cp.invite_collaborator(gh, cp.parse_pr_ref("octo/repo#3"), "bob")
    (_, _, body, _), = gh.called("PUT", "/repos/octo/repo/collaborators/bob")
    assert body == {}


def test_request_review_fails_when_nobody_has_credits(tmp_path):
    gh = FakeGitHub(pool_routes(), issues={cp.STATUS_LABEL: [status_node("bob", 0)]})
    with pytest.raises(SystemExit):
        make_pool(gh, tmp_path, used=300).request_review(cp.parse_pr_ref("octo/repo#3"))


# --- helper side ---------------------------------------------------------------


def helper_gh(overrides=None):
    routes = {
        ("GET", "/repos/octo/repo/pulls/3"): {"state": "open", "user": {"login": "alice"}},
        ("POST", "/repos/octo/repo/pulls/3/requested_reviewers"): {"requested_reviewers": [{"login": "Copilot"}]},
    }
    routes.update(overrides or {})
    return FakeGitHub(routes)


MEMBERS = {"alice", "bob", "carol"}


def closes(gh, number=7):
    return [c[2] for c in gh.called("PATCH", f"/repos/{POOL}/issues/{number}")]


def test_handle_request_requests_copilot_and_closes(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob")
    pool.handle_request(as_issue(request_node()), MEMBERS)

    (_, _, reviewers, _), = gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert reviewers == {"reviewers": [cp.COPILOT_REVIEWER]}
    assert closes(gh) == [{"state": "closed", "state_reason": "completed"}]
    assert pool.state.handled_issues == [7]
    assert pool.state.requested_count == 1
    assert pool.state.served == {"alice": [NOW.isoformat(timespec="seconds")]}


def test_handle_request_allows_non_author_with_review_access(tmp_path):
    gh = helper_gh(
        {
            ("GET", "/repos/octo/repo/pulls/3"): {"state": "open", "user": {"login": "someone"}},
            ("GET", "/repos/octo/repo/collaborators/alice/permission"): {"role_name": "write"},
        }
    )
    make_pool(gh, tmp_path, me="bob").handle_request(as_issue(request_node()), MEMBERS)
    assert gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")


@pytest.mark.parametrize(
    "overrides",
    [
        {("GET", "/repos/octo/repo/pulls/3"): cp.GitHubError(404, "Not Found")},
        {("GET", "/repos/octo/repo/pulls/3"): {"state": "closed", "user": {"login": "alice"}}},
        {
            ("GET", "/repos/octo/repo/pulls/3"): {"state": "open", "user": {"login": "someone"}},
            ("GET", "/repos/octo/repo/collaborators/alice/permission"): cp.GitHubError(404, "Not Found"),
        },
        {
            ("GET", "/repos/octo/repo/pulls/3"): {"state": "open", "user": {"login": "someone"}},
            ("GET", "/repos/octo/repo/collaborators/alice/permission"): {"role_name": "read"},
        },
        {("POST", "/repos/octo/repo/pulls/3/requested_reviewers"): cp.GitHubError(422, "nope")},
    ],
    ids=["no-access", "pr-closed", "requester-not-collaborator", "requester-read-only", "request-fails"],
)
def test_handle_request_failures_wait_then_close_identically(tmp_path, overrides):
    gh = helper_gh(overrides)
    pool = make_pool(gh, tmp_path, me="bob")
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert closes(gh) == []

    old = as_issue(request_node(created_at=NOW - dt.timedelta(hours=2)))
    pool.handle_request(old, MEMBERS)
    (_, _, comment, _), = gh.called("POST", f"/repos/{POOL}/issues/7/comments")
    assert comment == {"body": f"@alice {cp.CANNOT_SERVE}"}
    assert closes(gh) == [{"state": "closed", "state_reason": "not_planned"}]
    assert pool.state.requested_count == 0


def test_handle_request_never_requests_for_unauthorized_requester(tmp_path):
    gh = helper_gh(
        {
            ("GET", "/repos/octo/repo/pulls/3"): {"state": "open", "user": {"login": "someone"}},
            ("GET", "/repos/octo/repo/collaborators/alice/permission"): {"role_name": "read"},
        }
    )
    make_pool(gh, tmp_path, me="bob").handle_request(as_issue(request_node()), MEMBERS)
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")


def test_handle_request_declines_when_out_of_credits(tmp_path):
    gh = helper_gh()
    make_pool(gh, tmp_path, me="bob", used=300).handle_request(as_issue(request_node()), MEMBERS)
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert closes(gh)[0]["state_reason"] == "not_planned"


def test_handle_request_enforces_per_requester_daily_limit(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob", max_requests_per_requester_per_day=2)
    seed_state(pool, served={
        "alice": [(NOW - dt.timedelta(hours=1)).isoformat(), (NOW - dt.timedelta(hours=2)).isoformat()],
        "carol": [],
    })
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    (_, _, comment, _), = gh.called("POST", f"/repos/{POOL}/issues/7/comments")
    assert "at most 2" in comment["body"]


def test_handle_request_daily_limit_window_slides(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob", max_requests_per_requester_per_day=1)
    seed_state(pool, served={"alice": [(NOW - dt.timedelta(hours=25)).isoformat()]})
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert len(pool.state.served["alice"]) == 1


def test_handle_request_zero_limit_means_unlimited(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob", max_requests_per_requester_per_day=0)
    seed_state(pool, served={"alice": [NOW.isoformat()] * 50})
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")


def test_handle_request_ignores_non_members(tmp_path):
    gh = helper_gh()
    make_pool(gh, tmp_path, me="bob").handle_request(as_issue(request_node(requester="mallory")), MEMBERS)
    assert gh.calls == []


def test_handle_request_refuses_request_edited_by_someone_else(tmp_path):
    gh = helper_gh()
    make_pool(gh, tmp_path, me="bob").handle_request(as_issue(request_node(editor="carol")), MEMBERS)
    assert not gh.called("GET", "/repos/octo/repo/pulls/3")
    assert closes(gh)[0]["state_reason"] == "not_planned"


@pytest.mark.parametrize("body", ["no json here", "```json\n[1]\n```", "```json\n{\"pr\": 5}\n```", "```json\n{\"pr\": \"../x#1\"}\n```"])
def test_handle_request_closes_malformed(tmp_path, body):
    gh = helper_gh()
    issue = as_issue(node(7, "alice", body, assignees=["bob"]))
    make_pool(gh, tmp_path, me="bob").handle_request(issue, MEMBERS)
    assert closes(gh)[0]["state_reason"] == "not_planned"
    assert not gh.called("GET", "/repos/octo/repo/pulls/3")


def test_handle_request_does_not_double_spend(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob")
    seed_state(pool, handled_issues=[7])
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert closes(gh)


def test_serve_requests_continues_past_a_failing_request(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob")
    broken = as_issue(request_node(number=6))
    broken["created_at"] = "not a date"
    pool.serve_requests(MEMBERS, [broken, as_issue(request_node(number=7)), as_issue(request_node(number=8, helper="carol"))])
    assert closes(gh, 7) == [{"state": "closed", "state_reason": "completed"}]
    assert closes(gh, 8) == []


# --- collaborator cleanup ------------------------------------------------------

INVITE = {"id": "inv1", "issue": 9, "repo": "octo/repo", "pr": 3, "helper": "carol", "created_at": NOW.isoformat()}


def cleanup_gh(issue, reviews=(), pending=()):
    return FakeGitHub(
        {
            ("GET", f"/repos/{POOL}/issues/9"): issue,
            ("GET", "/repos/octo/repo/pulls/3/reviews"): list(reviews),
            ("GET", "/repos/octo/repo/invitations"): list(pending),
            ("DELETE", "/repos/octo/repo/collaborators/carol"): None,
            ("DELETE", "/repos/octo/repo/invitations/55"): None,
        }
    )


def closed_issue(reason="completed", closed_at=NOW):
    return {"state": "closed", "state_reason": reason, "closed_at": closed_at.isoformat()}


def run_cleanup(gh, tmp_path, open_requests=(), invite=INVITE):
    pool = make_pool(gh, tmp_path)
    seed_state(pool, invites=[dict(invite)])
    pool.cleanup_invites(list(open_requests))
    return disk_state(pool)


def test_cleanup_waits_for_copilot_review(tmp_path):
    gh = cleanup_gh(closed_issue())
    state = run_cleanup(gh, tmp_path)
    assert not gh.called("DELETE", "/repos/octo/repo/collaborators/carol")
    assert state.invites == [INVITE]


def test_cleanup_revokes_after_copilot_review(tmp_path):
    gh = cleanup_gh(closed_issue(), reviews=[{"user": {"login": "copilot-pull-request-reviewer[bot]"}}])
    state = run_cleanup(gh, tmp_path)
    assert gh.called("DELETE", "/repos/octo/repo/collaborators/carol")
    assert state.invites == []


def test_cleanup_revokes_after_grace_period(tmp_path):
    gh = cleanup_gh(closed_issue(closed_at=NOW - dt.timedelta(hours=2)))
    run_cleanup(gh, tmp_path)
    assert gh.called("DELETE", "/repos/octo/repo/collaborators/carol")


def test_cleanup_revokes_declined_request_and_pending_invitation(tmp_path):
    pending = [{"id": 55, "invitee": {"login": "carol"}}, {"id": 56, "invitee": {"login": "dave"}}]
    gh = cleanup_gh(closed_issue(reason="not_planned"), pending=pending)
    gh.routes[("DELETE", "/repos/octo/repo/collaborators/carol")] = cp.GitHubError(404, "Not Found")
    state = run_cleanup(gh, tmp_path)
    assert gh.called("DELETE", "/repos/octo/repo/invitations/55")
    assert not gh.called("DELETE", "/repos/octo/repo/invitations/56")
    assert state.invites == []


def test_cleanup_keeps_access_while_issue_open(tmp_path):
    gh = cleanup_gh({"state": "open"})
    run_cleanup(gh, tmp_path)
    assert not gh.called("DELETE", "/repos/octo/repo/collaborators/carol")


def test_cleanup_keeps_access_while_another_request_needs_it(tmp_path):
    gh = cleanup_gh(closed_issue(reason="not_planned"))
    other = as_issue(request_node(pr="octo/repo#4", requester="alice", helper="carol", number=10))
    run_cleanup(gh, tmp_path, open_requests=[other])
    assert not gh.called("DELETE", "/repos/octo/repo/collaborators/carol")


def test_cleanup_keeps_invite_when_revoke_fails(tmp_path):
    gh = cleanup_gh(closed_issue(reason="not_planned"))
    gh.routes[("DELETE", "/repos/octo/repo/collaborators/carol")] = cp.GitHubError(500, "boom")
    state = run_cleanup(gh, tmp_path)
    assert state.invites == [INVITE]


def test_cleanup_revokes_after_hard_deadline_even_if_request_still_open(tmp_path):
    gh = cleanup_gh({"state": "open"})
    old = {**INVITE, "created_at": (NOW - dt.timedelta(hours=3)).isoformat()}
    still_open = as_issue(request_node(pr="octo/repo#3", helper="carol", number=9))
    state = run_cleanup(gh, tmp_path, open_requests=[still_open], invite=old)
    assert gh.called("DELETE", "/repos/octo/repo/collaborators/carol")
    assert state.invites == []


def test_cleanup_ignores_tampered_open_request(tmp_path):
    gh = cleanup_gh(closed_issue(reason="not_planned"))
    tampered = as_issue(request_node(pr="octo/repo#4", helper="carol", number=10, editor="carol"))
    run_cleanup(gh, tmp_path, open_requests=[tampered])
    assert gh.called("DELETE", "/repos/octo/repo/collaborators/carol")


def test_cleanup_keeps_pending_invite_briefly_then_revokes(tmp_path):
    pending = {**INVITE, "issue": None}
    gh = cleanup_gh(None)
    assert run_cleanup(gh, tmp_path, invite=pending).invites == [pending]
    assert not gh.called("DELETE", "/repos/octo/repo/collaborators/carol")

    stale = {**pending, "created_at": (NOW - dt.timedelta(minutes=10)).isoformat()}
    gh = cleanup_gh(None)
    assert run_cleanup(gh, tmp_path / "2", invite=stale).invites == []
    assert gh.called("DELETE", "/repos/octo/repo/collaborators/carol")


def borrow_routes(issue_resp):
    routes = pool_routes(members=("alice", "carol"))
    routes.update(
        {
            ("GET", "/repos/octo/repo/collaborators/carol/permission"): {"role_name": "read"},
            ("GET", "/repos/octo/repo"): {"owner": {"type": "User"}},
            ("POST", f"/repos/{POOL}/issues"): issue_resp,
            ("GET", "/repos/octo/repo/invitations"): [{"id": 55, "invitee": {"login": "carol"}}],
            ("DELETE", "/repos/octo/repo/invitations/55"): None,
            ("DELETE", "/repos/octo/repo/collaborators/carol"): cp.GitHubError(404, "Not Found"),
        }
    )
    return FakeGitHub(routes, issues={cp.STATUS_LABEL: [status_node("carol", 90)]})


def test_request_review_records_invite_before_granting(tmp_path):
    gh = borrow_routes({"number": 9, "html_url": "u"})
    pool = make_pool(gh, tmp_path, used=300)
    recorded_before_put = []
    real = gh.request

    def spy(method, path, body=None, params=None):
        if method == "PUT":
            recorded_before_put.append(len(disk_state(pool).invites))
        return real(method, path, body, params)

    gh.request = spy
    pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert recorded_before_put == [1]


def test_request_review_rolls_back_invite_when_issue_creation_fails(tmp_path):
    gh = borrow_routes(cp.GitHubError(500, "boom"))
    pool = make_pool(gh, tmp_path, used=300)
    with pytest.raises(cp.GitHubError):
        pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert gh.called("PUT", "/repos/octo/repo/collaborators/carol")
    assert gh.called("DELETE", "/repos/octo/repo/invitations/55")
    assert disk_state(pool).invites == []


def test_request_review_keeps_record_when_rollback_fails(tmp_path):
    gh = borrow_routes(cp.GitHubError(500, "boom"))
    gh.routes[("GET", "/repos/octo/repo/invitations")] = cp.GitHubError(502, "bad gateway")
    pool = make_pool(gh, tmp_path, used=300)
    with pytest.raises(cp.GitHubError):
        pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    (record,) = disk_state(pool).invites
    assert record["issue"] is None and record["helper"] == "carol"


def test_cleanup_revokes_old_format_invite_records(tmp_path):
    pool = make_pool(cleanup_gh(closed_issue()), tmp_path)
    pool.state_path.write_text(json.dumps({"invites": [{"issue": 9, "repo": "octo/repo", "pr": 3, "helper": "carol"}]}))
    pool.reload_state()
    pool.cleanup_invites([])
    assert pool.gh.called("DELETE", "/repos/octo/repo/collaborators/carol")
    assert disk_state(pool).invites == []


@pytest.mark.parametrize("created_at", [None, "garbage", 5])
def test_cleanup_revokes_undatable_invites(tmp_path, created_at):
    gh = cleanup_gh({"state": "open"})
    state = run_cleanup(gh, tmp_path, invite={**INVITE, "created_at": created_at})
    assert gh.called("DELETE", "/repos/octo/repo/collaborators/carol")
    assert state.invites == []


def test_cleanup_drops_unrevocable_invite_records(tmp_path):
    state = run_cleanup(cleanup_gh(None), tmp_path, invite={"id": "x", "created_at": "garbage"})
    assert state.invites == []


# --- concurrency between `request` and `run` -----------------------------------


def test_daemon_write_does_not_lose_invite_recorded_by_request(tmp_path):
    daemon = make_pool(FakeGitHub(), tmp_path, me="alice", billing=False)
    daemon.reload_state()
    gh = borrow_routes({"number": 9, "html_url": "u"})
    requester = make_pool(gh, tmp_path, me="alice", used=300)
    requester.request_review(cp.parse_pr_ref("octo/repo#3"))

    # The daemon's in-memory state predates the invite; its next write must not drop it.
    daemon.record_usage()
    state = disk_state(daemon)
    assert len(state.invites) == 1
    assert state.requested_count == 1


def test_run_once_reloads_state_written_by_other_process(tmp_path):
    gh = FakeGitHub({**pool_routes(), ("GET", "/user/repository_invitations"): []})
    pool = make_pool(gh, tmp_path)
    other = make_pool(FakeGitHub(), tmp_path)
    seed_state(other, handled_issues=[42])
    pool.run_once()
    assert pool.state.handled_issues == [42]


# --- daemon loop ---------------------------------------------------------------


def test_run_once_survives_malformed_status_from_a_member(tmp_path):
    gh = FakeGitHub(
        {**pool_routes(), ("GET", "/user/repository_invitations"): []},
        issues={cp.STATUS_LABEL: [node(1, "bob", "```json\n[1]\n```")]},
    )
    make_pool(gh, tmp_path).run_once()
    assert gh.called("POST", f"/repos/{POOL}/issues")
    assert gh.called("GET", "/user/repository_invitations")


def test_run_once_continues_after_step_failure(tmp_path):
    gh = FakeGitHub({**pool_routes(), ("GET", "/user/repository_invitations"): []})
    pool = make_pool(gh, tmp_path)
    pool.publish_status = lambda members: (_ for _ in ()).throw(cp.GitHubError(500, "boom"))
    pool.run_once()
    assert gh.called("GET", "/user/repository_invitations")


# --- redirects -----------------------------------------------------------------


@pytest.mark.parametrize(
    "target, keeps_auth",
    [
        ("https://api.github.com/repositories/1/pulls/3", True),
        ("https://evil.example.com/x", False),
        ("http://api.github.com/x", False),
        ("https://api.github.com:8443/x", False),
    ],
)
def test_redirect_strips_token_when_origin_changes(target, keeps_auth):
    req = urllib.request.Request("https://api.github.com/repos/octo/repo/pulls/3")
    req.add_header("Authorization", "Bearer secret")
    new = cp.StripAuthOnOriginChange().redirect_request(req, None, 301, "Moved", {}, target)
    assert new.has_header("Authorization") is keeps_auth


# --- config --------------------------------------------------------------------


def test_config_roundtrip_is_private(tmp_path):
    path = tmp_path / "cfg" / "config.json"
    cfg = cp.Config(username="a", pool_repo=POOL, monthly_quota=300, token="secret")
    cfg.save(path)
    assert cp.Config.load(path) == cfg
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_config_save_locks_down_existing_directory(tmp_path):
    folder = tmp_path / "cfg"
    folder.mkdir(mode=0o755)
    cp.Config(username="a", pool_repo=POOL, monthly_quota=300, token="secret").save(folder / "config.json")
    assert folder.stat().st_mode & 0o777 == 0o700


def test_config_save_never_exposes_token_even_under_permissive_umask(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o644)
    modes = []
    real_fdopen = os.fdopen

    def spy(fd, *args, **kwargs):
        modes.append(os.fstat(fd).st_mode & 0o777)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", spy)
    old = os.umask(0)
    try:
        cp.Config(username="a", pool_repo=POOL, monthly_quota=300, token="secret").save(path)
    finally:
        os.umask(old)
    assert modes == [0o600]
    assert path.stat().st_mode & 0o777 == 0o600
