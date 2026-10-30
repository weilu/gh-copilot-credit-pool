import datetime as dt

import pytest

import copilot_pool as cp

NOW = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)
POOL = "team/pool"


class FakeGitHub:
    """Routes (method, path) to canned responses and records every call."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        resp = self.routes.get((method, path))
        if isinstance(resp, Exception):
            raise resp
        return resp() if callable(resp) else resp

    def called(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def status_issue(user, remaining, updated_at=NOW, number=1, author=None):
    data = {
        "username": user,
        "remaining": remaining,
        "quota": 300,
        "used": 300 - remaining,
        "source": "billing-api",
        "updated_at": updated_at.isoformat(),
    }
    return {"number": number, "user": {"login": author or user}, "body": cp.json_block(data)}


def request_issue(pr_url, requester="alice", number=7, created_at=NOW):
    return {
        "number": number,
        "user": {"login": requester},
        "created_at": created_at.isoformat(),
        "body": cp.json_block({"pr": pr_url, "requester": requester}),
    }


def make_pool(gh, me="alice", used=0, quota=300, tmp_path=None, billing=True):
    cfg = cp.Config(username=me, pool_repo=POOL, monthly_quota=quota, token="t")
    billing_gh = FakeGitHub(
        {
            ("GET", f"/users/{me}/settings/billing/premium_request/usage"): {
                "usageItems": [{"product": "Copilot", "grossQuantity": used}]
            }
        }
    )
    state_path = tmp_path / "state.json"
    return cp.Pool(cfg, gh, billing_gh if billing else None, cp.State(), state_path, clock=lambda: NOW)


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


def test_parse_pr_ref_rejects_garbage():
    with pytest.raises(ValueError):
        cp.parse_pr_ref("https://github.com/octo/repo/issues/12")


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


def test_remaining_from_billing_api(tmp_path):
    pool = make_pool(FakeGitHub(), used=280, tmp_path=tmp_path)
    assert pool.remaining() == 20


def test_remaining_falls_back_to_local_count_when_billing_fails(tmp_path):
    pool = make_pool(FakeGitHub(), tmp_path=tmp_path)
    pool.billing_gh = FakeGitHub(
        {("GET", "/users/alice/settings/billing/premium_request/usage"): cp.GitHubError(403, "no")}
    )
    pool.state.month = "2026-10"
    pool.state.requested_count = 299
    assert pool.remaining() == 1
    assert pool.billing_gh is None


def test_local_count_resets_each_month(tmp_path):
    pool = make_pool(FakeGitHub(), tmp_path=tmp_path, billing=False)
    pool.state.month = "2026-09"
    pool.state.requested_count = 300
    assert pool.remaining() == 300


def test_read_statuses_ignores_non_members_and_impersonation(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", f"/repos/{POOL}/issues"): [
                status_issue("bob", 10, number=1),
                status_issue("mallory", 999, number=2),
                status_issue("carol", 999, number=3, author="bob"),
            ]
        }
    )
    pool = make_pool(gh, tmp_path=tmp_path)
    statuses = pool.read_statuses({"alice", "bob", "carol"})
    assert [s["username"] for s in statuses] == ["bob"]


def test_publish_status_creates_then_updates(tmp_path):
    gh = FakeGitHub({("GET", f"/repos/{POOL}/issues"): []})
    pool = make_pool(gh, used=100, tmp_path=tmp_path)
    pool.publish_status({"alice"})
    (_, _, body, _), = gh.called("POST", f"/repos/{POOL}/issues")
    assert cp.extract_json_block(body["body"])["remaining"] == 200
    assert body["labels"] == [cp.STATUS_LABEL]

    gh.routes[("GET", f"/repos/{POOL}/issues")] = [status_issue("alice", 200, number=5)]
    pool.publish_status({"alice"}, force=True)
    assert gh.called("PATCH", f"/repos/{POOL}/issues/5")


def test_publish_status_skips_when_unchanged_within_heartbeat(tmp_path):
    gh = FakeGitHub({("GET", f"/repos/{POOL}/issues"): []})
    pool = make_pool(gh, tmp_path=tmp_path)
    pool.publish_status({"alice"})
    pool.publish_status({"alice"})
    assert len(gh.called("POST", f"/repos/{POOL}/issues")) == 1


def test_accept_invitations_only_from_members(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", "/user/repository_invitations"): [
                {"id": 1, "inviter": {"login": "bob"}, "repository": {"full_name": "bob/x"}},
                {"id": 2, "inviter": {"login": "stranger"}, "repository": {"full_name": "s/y"}},
            ]
        }
    )
    pool = make_pool(gh, tmp_path=tmp_path)
    pool.accept_invitations({"alice", "bob"})
    assert gh.called("PATCH", "/user/repository_invitations/1")
    assert not gh.called("PATCH", "/user/repository_invitations/2")


def test_request_review_uses_own_credits_when_available(tmp_path):
    gh = FakeGitHub(
        {
            ("POST", "/repos/octo/repo/pulls/3/requested_reviewers"): {
                "requested_reviewers": [{"login": "Copilot"}]
            }
        }
    )
    pool = make_pool(gh, used=10, tmp_path=tmp_path)
    msg = pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert "your own credits" in msg
    assert pool.state.requested_count == 1


def test_request_review_borrows_from_richest_peer_and_invites(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", f"/repos/{POOL}/collaborators"): [{"login": u} for u in ("alice", "bob", "carol")],
            ("GET", f"/repos/{POOL}/issues"): [status_issue("bob", 10, number=1), status_issue("carol", 90, number=2)],
            ("GET", "/repos/octo/repo/collaborators/carol/permission"): {"permission": "read", "role_name": "read"},
            ("GET", "/repos/octo/repo"): {"owner": {"type": "Organization"}},
            ("POST", f"/repos/{POOL}/issues"): {"html_url": "https://github.com/team/pool/issues/9"},
        }
    )
    pool = make_pool(gh, used=300, tmp_path=tmp_path)
    msg = pool.request_review(cp.parse_pr_ref("octo/repo#3"))

    assert "@carol" in msg and "invited" in msg
    (_, _, invite, _), = gh.called("PUT", "/repos/octo/repo/collaborators/carol")
    assert invite == {"permission": "triage"}
    (_, _, issue, _), = gh.called("POST", f"/repos/{POOL}/issues")
    assert issue["assignees"] == ["carol"]
    assert issue["labels"] == [cp.REQUEST_LABEL]
    assert cp.extract_json_block(issue["body"])["pr"] == "https://github.com/octo/repo/pull/3"


def test_request_review_skips_invite_when_peer_has_access(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", f"/repos/{POOL}/collaborators"): [{"login": "alice"}, {"login": "bob"}],
            ("GET", f"/repos/{POOL}/issues"): [status_issue("bob", 10)],
            ("GET", "/repos/octo/repo/collaborators/bob/permission"): {"permission": "write", "role_name": "write"},
            ("POST", f"/repos/{POOL}/issues"): {"html_url": "u"},
        }
    )
    pool = make_pool(gh, used=300, tmp_path=tmp_path)
    pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert not gh.called("PUT", "/repos/octo/repo/collaborators/bob")


def test_invite_omits_permission_for_personal_repo():
    gh = FakeGitHub({("GET", "/repos/octo/repo"): {"owner": {"type": "User"}}})
    cp.invite_collaborator(gh, cp.parse_pr_ref("octo/repo#3"), "bob")
    (_, _, body, _), = gh.called("PUT", "/repos/octo/repo/collaborators/bob")
    assert body == {}


def test_request_review_fails_when_nobody_has_credits(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", f"/repos/{POOL}/collaborators"): [{"login": "alice"}, {"login": "bob"}],
            ("GET", f"/repos/{POOL}/issues"): [status_issue("bob", 0)],
        }
    )
    pool = make_pool(gh, used=300, tmp_path=tmp_path)
    with pytest.raises(SystemExit):
        pool.request_review(cp.parse_pr_ref("octo/repo#3"))


def helper_gh(overrides=None):
    routes = {
        ("GET", "/repos/octo/repo/pulls/3"): {"state": "open"},
        ("POST", "/repos/octo/repo/pulls/3/requested_reviewers"): {"requested_reviewers": [{"login": "Copilot"}]},
    }
    routes.update(overrides or {})
    return FakeGitHub(routes)


def test_handle_request_requests_copilot_and_closes(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, me="bob", used=0, tmp_path=tmp_path)
    pool.handle_request(request_issue("https://github.com/octo/repo/pull/3"), {"alice", "bob"})

    (_, _, reviewers, _), = gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert reviewers == {"reviewers": [cp.COPILOT_REVIEWER]}
    (_, _, close, _), = gh.called("PATCH", f"/repos/{POOL}/issues/7")
    assert close == {"state": "closed", "state_reason": "completed"}
    assert pool.state.handled_issues == [7]
    assert pool.state.requested_count == 1


def test_handle_request_waits_for_access_then_gives_up(tmp_path):
    gh = helper_gh({("GET", "/repos/octo/repo/pulls/3"): cp.GitHubError(404, "Not Found")})
    pool = make_pool(gh, me="bob", tmp_path=tmp_path)
    pool.handle_request(request_issue("octo/repo#3"), {"alice", "bob"})
    assert not gh.called("PATCH", f"/repos/{POOL}/issues/7")

    old = request_issue("octo/repo#3", created_at=NOW - dt.timedelta(hours=2))
    pool.handle_request(old, {"alice", "bob"})
    (_, _, close, _), = gh.called("PATCH", f"/repos/{POOL}/issues/7")
    assert close["state_reason"] == "not_planned"


def test_handle_request_declines_when_out_of_credits(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, me="bob", used=300, tmp_path=tmp_path)
    pool.handle_request(request_issue("octo/repo#3"), {"alice", "bob"})
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    (_, _, close, _), = gh.called("PATCH", f"/repos/{POOL}/issues/7")
    assert close["state_reason"] == "not_planned"


def test_handle_request_ignores_non_members(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, me="bob", tmp_path=tmp_path)
    pool.handle_request(request_issue("octo/repo#3", requester="mallory"), {"alice", "bob"})
    assert gh.calls == []


def test_handle_request_does_not_double_spend(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, me="bob", tmp_path=tmp_path)
    pool.state.handled_issues = [7]
    pool.handle_request(request_issue("octo/repo#3"), {"alice", "bob"})
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert gh.called("PATCH", f"/repos/{POOL}/issues/7")


def test_handle_request_closes_when_pr_not_open(tmp_path):
    gh = helper_gh({("GET", "/repos/octo/repo/pulls/3"): {"state": "closed"}})
    pool = make_pool(gh, me="bob", tmp_path=tmp_path)
    pool.handle_request(request_issue("octo/repo#3"), {"alice", "bob"})
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")


def test_run_once_continues_after_step_failure(tmp_path):
    gh = FakeGitHub(
        {
            ("GET", f"/repos/{POOL}/collaborators"): [{"login": "alice"}],
            ("GET", f"/repos/{POOL}/issues"): cp.GitHubError(500, "boom"),
            ("GET", "/user/repository_invitations"): [],
        }
    )
    pool = make_pool(gh, tmp_path=tmp_path)
    pool.run_once()
    assert gh.called("GET", "/user/repository_invitations")


def test_config_roundtrip_is_private(tmp_path):
    path = tmp_path / "cfg" / "config.json"
    cfg = cp.Config(username="a", pool_repo=POOL, monthly_quota=300, token="secret")
    cfg.save(path)
    assert cp.Config.load(path) == cfg
    assert path.stat().st_mode & 0o777 == 0o600
