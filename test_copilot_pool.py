import base64
import datetime as dt
import json
import os
import subprocess
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


def node(number, author, body, editor=None, assignees=(), created_at=NOW, edited_at=None, title=None, labels=()):
    return {
        "labels": {"nodes": [{"name": n} for n in labels]},
        "number": number,
        "title": title if title is not None else f"issue {number}",
        "body": body,
        "createdAt": created_at.isoformat(),
        "lastEditedAt": edited_at.isoformat() if edited_at else None,
        "author": {"login": author} if author else None,
        "editor": {"login": editor} if editor else None,
        "assignees": {"nodes": [{"login": a} for a in assignees]},
    }


def entry(user, remaining, quota=300, used=None, source="copilot", updated_at=NOW):
    return {
        "username": user,
        "remaining": remaining,
        "quota": quota,
        "used": quota - remaining if used is None and isinstance(remaining, int) else (used or 0),
        "source": source,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, dt.datetime) else updated_at,
    }


BOARD = ("GET", f"/repos/{POOL}/contents/README.md")
BOARD_PUT = ("PUT", f"/repos/{POOL}/contents/README.md")


def with_board(gh, *entries, online=None, text=None, sha="s1"):
    """Serve a README board with `entries` and heartbeats for `online` members (default: all of them)."""
    if text is None:
        text = cp.render_board({e["username"]: e for e in entries})
    gh.routes[BOARD] = {"content": base64.b64encode(text.encode()).decode(), "sha": sha}
    users = [e["username"] for e in entries] if online is None else online
    gh.issues[cp.HEARTBEAT_LABEL] = [
        heartbeat_node(100 + i, u, edited_at=NOW - dt.timedelta(minutes=1)) for i, u in enumerate(users)
    ]
    return gh


def heartbeat_node(number, user, body=None, **kw):
    kw.setdefault("labels", [cp.HEARTBEAT_LABEL])
    body = cp.heartbeat_body(user, NOW) if body is None else body
    return node(number, user, body, title=f"pool-heartbeat: {user}", **kw)


def put_board(gh, index=-1):
    """Decode the board written by the index-th README PUT."""
    body = gh.called(*BOARD_PUT)[index][2]
    return body, cp.extract_json_block(base64.b64decode(body["content"]).decode())


def request_node(pr=PR, requester="alice", helper="bob", number=7, created_at=NOW, editor=None):
    body = cp.json_block({"pr": pr, "requester": requester})
    return node(number, requester, body, editor=editor, assignees=[helper], created_at=created_at)


def as_issue(n):
    """Convert a GraphQL node to the dict shape Pool.pool_issues returns."""
    return {
        "number": n["number"],
        "title": n["title"],
        "body": n["body"],
        "created_at": n["createdAt"],
        "author": (n["author"] or {}).get("login"),
        "editor": (n["editor"] or {}).get("login"),
        "assignees": [a["login"] for a in n["assignees"]["nodes"]],
        "labels": [lbl["name"] for lbl in n["labels"]["nodes"]],
    }


def pool_routes(members=("alice", "bob"), owner_type="User", private=True):
    return {
        ("GET", f"/repos/{POOL}"): {"private": private, "owner": {"login": "team", "type": owner_type}},
        ("GET", f"/repos/{POOL}/collaborators"): [
            {"login": m, "permissions": {"push": True}} for m in members
        ],
    }


def quota_snapshot(used, entitlement, unlimited=False):
    return {
        "login": "x",
        "quota_snapshots": {
            "premium_interactions": {
                "entitlement": entitlement,
                "remaining": entitlement - used,
                "credits_used": used,
                "overage_permitted": False,
                "unlimited": unlimited,
            }
        },
    }


SETTINGS = ("GET", f"/repos/{POOL}/contents/pool.json")


def settings_resp(data):
    text = data if isinstance(data, str) else json.dumps(data)
    return {"content": base64.b64encode(text.encode()).decode(), "sha": "p1"}


def make_pool(gh, tmp_path, me="alice", used=0, quota=300, copilot=True, credits_per_review=1, **cfg):
    """`used`/`quota` are what the Copilot endpoint reports; pool.json sets the review cost (1 by default)."""
    config = cp.Config(username=me, pool_repo=POOL, monthly_credits=quota, token="t", **cfg)
    gh.routes.setdefault(SETTINGS, settings_resp({"credits_per_review": credits_per_review}))
    gh.routes.setdefault(
        ("GET", cp.COPILOT_USER_ENDPOINT),
        quota_snapshot(used, quota) if copilot else cp.GitHubError(404, "Not Found"),
    )
    return cp.Pool(config, gh, cp.State(), tmp_path / "state.json", clock=lambda: NOW)


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
    data = entry("bob", 10, updated_at=NOW + dt.timedelta(minutes=1))
    assert cp.valid_status(data, "bob", NOW) == data


@pytest.mark.parametrize(
    "bad",
    [{"used": -1}, {"used": "x"}, {"source": "made-up"}, {"source": None}],
)
def test_valid_status_rejects_bad_used_or_source(bad):
    assert cp.valid_status({**entry("bob", 10), **bad}, "bob", NOW) is None


def test_valid_status_strips_unknown_fields():
    data = {**entry("bob", 10), "note": "[click](https://evil.example)"}
    assert "note" not in cp.valid_status(data, "bob", NOW)


def test_pick_peer_prefers_most_remaining_and_skips_self_stale_and_empty():
    stale = NOW - dt.timedelta(hours=1)
    statuses = [
        {"username": "alice", "remaining": 999, "last_seen": NOW.isoformat()},
        {"username": "bob", "remaining": 50, "last_seen": NOW.isoformat()},
        {"username": "carol", "remaining": 200, "last_seen": stale.isoformat()},
        {"username": "dave", "remaining": 120, "last_seen": NOW.isoformat()},
        {"username": "erin", "remaining": 0, "last_seen": NOW.isoformat()},
        {"username": "frank", "remaining": 500, "last_seen": None},
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


def test_remaining_from_copilot_endpoint(tmp_path):
    pool = make_pool(FakeGitHub(), tmp_path, used=11000, quota=11900)
    assert pool.usage() == (11000, 11900, "copilot")
    assert pool.remaining() == 900


def test_remaining_ignores_overage_and_bad_values(tmp_path):
    gh = FakeGitHub({("GET", cp.COPILOT_USER_ENDPOINT): quota_snapshot(-500, 11900)})
    assert make_pool(gh, tmp_path).remaining() == 11900
    gh = FakeGitHub({("GET", cp.COPILOT_USER_ENDPOINT): quota_snapshot(12000, 11900)})
    assert make_pool(gh, tmp_path).remaining() == 0


def test_remaining_unlimited(tmp_path):
    gh = FakeGitHub({("GET", cp.COPILOT_USER_ENDPOINT): quota_snapshot(0, 0, unlimited=True)})
    assert make_pool(gh, tmp_path).remaining() == cp.UNLIMITED_CREDITS


@pytest.mark.parametrize(
    "response",
    [cp.GitHubError(404, "Not Found"), {"login": "x"}, {"quota_snapshots": {"premium_interactions": {"entitlement": "?"}}}],
)
def test_remaining_falls_back_to_local_count_in_credits(tmp_path, response):
    gh = FakeGitHub({("GET", cp.COPILOT_USER_ENDPOINT): response})
    pool = make_pool(gh, tmp_path, quota=11900, credits_per_review=90)
    seed_state(pool, month="2026-10", requested_count=100)
    assert pool.usage() == (9000, 11900, "local-count")
    assert pool.remaining() == 2900


def test_local_estimate_is_never_lent(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob", copilot=False, quota=11900, credits_per_review=100)
    assert pool.remaining() == 11900
    assert pool.lendable() == 0
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert closes(gh)[0]["state_reason"] == "not_planned"


def test_local_estimate_publishes_zero_lendable_credits(tmp_path):
    gh = with_board(FakeGitHub(pool_routes()))
    make_pool(gh, tmp_path, copilot=False, quota=11900).publish_status({"alice"})
    _, board = put_board(gh)
    assert (board["alice"]["remaining"], board["alice"]["source"]) == (0, "local-count")


def test_own_request_may_use_local_estimate(tmp_path):
    gh = FakeGitHub({("POST", "/repos/octo/repo/pulls/3/requested_reviewers"): {"requested_reviewers": []}})
    pool = make_pool(gh, tmp_path, copilot=False, quota=11900)
    assert "your own credits" in pool.request_review(cp.parse_pr_ref("octo/repo#3"))


def test_local_count_resets_each_month(tmp_path):
    pool = make_pool(FakeGitHub(), tmp_path, copilot=False)
    seed_state(pool, month="2026-09", requested_count=300)
    assert pool.remaining() == 300


def test_pick_peer_requires_enough_credits_for_a_review():
    statuses = [
        {"username": "bob", "remaining": 99, "last_seen": NOW.isoformat()},
        {"username": "carol", "remaining": 100, "last_seen": NOW.isoformat()},
    ]
    assert cp.pick_peer(statuses, "alice", NOW, 900, min_credits=100) == "carol"
    assert cp.pick_peer(statuses[:1], "alice", NOW, 900, min_credits=100) is None


def test_request_review_borrows_when_own_credits_below_one_review(tmp_path):
    routes = pool_routes(members=("alice", "carol"))
    routes.update(
        {
            ("GET", "/repos/octo/repo/collaborators/carol/permission"): {"role_name": "write"},
            ("POST", f"/repos/{POOL}/issues"): {"number": 9, "html_url": "u"},
        }
    )
    gh = with_board(FakeGitHub(routes), entry("carol", 500, quota=11900))
    pool = make_pool(gh, tmp_path, used=11850, quota=11900, credits_per_review=100)
    assert "@carol" in pool.request_review(cp.parse_pr_ref("octo/repo#3"))
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")


def test_helper_declines_when_below_one_review(tmp_path):
    gh = helper_gh()
    pool = make_pool(gh, tmp_path, me="bob", used=11850, quota=11900, credits_per_review=100)
    pool.handle_request(as_issue(request_node()), MEMBERS)
    assert not gh.called("POST", "/repos/octo/repo/pulls/3/requested_reviewers")
    assert closes(gh)[0]["state_reason"] == "not_planned"


def test_credits_per_review_comes_from_pool_settings(tmp_path):
    gh = FakeGitHub({SETTINGS: settings_resp({"credits_per_review": 37})})
    assert make_pool(gh, tmp_path).credits_per_review == 37


@pytest.mark.parametrize(
    "resp",
    [
        cp.GitHubError(404, "Not Found"),
        cp.GitHubError(500, "boom"),
        settings_resp("not json"),
        settings_resp([1, 2]),
        settings_resp({}),
        settings_resp({"credits_per_review": 0}),
        settings_resp({"credits_per_review": -5}),
        settings_resp({"credits_per_review": "50"}),
        settings_resp({"credits_per_review": True}),
        settings_resp({"credits_per_review": 10**7}),
        {"sha": "x"},
    ],
)
def test_credits_per_review_falls_back_to_default(tmp_path, resp):
    gh = FakeGitHub({SETTINGS: resp})
    assert make_pool(gh, tmp_path).credits_per_review == cp.DEFAULT_CREDITS_PER_REVIEW


def test_credits_per_review_is_cached_within_a_cycle_and_refreshed_each_cycle(tmp_path):
    gh = FakeGitHub({**pool_routes(), SETTINGS: settings_resp({"credits_per_review": 40}), ("GET", "/user/repository_invitations"): []})
    pool = make_pool(gh, tmp_path)
    assert pool.credits_per_review == 40
    gh.routes[SETTINGS] = settings_resp({"credits_per_review": 60})
    assert pool.credits_per_review == 40
    assert len(gh.called(*SETTINGS)) == 1
    pool.run_once()
    assert pool.credits_per_review == 60


def test_ensure_pool_settings_creates_default_only_when_missing():
    gh = FakeGitHub({SETTINGS: cp.GitHubError(404, "Not Found")})
    assert cp.ensure_pool_settings(gh, POOL) is True
    (_, _, body, _), = gh.called("PUT", f"/repos/{POOL}/contents/pool.json")
    assert json.loads(base64.b64decode(body["content"])) == {"credits_per_review": cp.DEFAULT_CREDITS_PER_REVIEW}

    gh = FakeGitHub({SETTINGS: settings_resp({"credits_per_review": 5})})
    assert cp.ensure_pool_settings(gh, POOL) is False
    assert not gh.called("PUT", f"/repos/{POOL}/contents/pool.json")


def test_ensure_pool_settings_tolerates_concurrent_create():
    gh = FakeGitHub(
        {SETTINGS: cp.GitHubError(404, "Not Found"), ("PUT", f"/repos/{POOL}/contents/pool.json"): cp.GitHubError(422, "sha")}
    )
    assert cp.ensure_pool_settings(gh, POOL) is False


def test_config_migrates_monthly_quota_and_drops_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"username": "a", "pool_repo": POOL, "monthly_quota": 300, "billing_token": "x", "credits_per_review": 5})
    )
    cfg = cp.Config.load(path)
    assert cfg.monthly_credits == 300
    assert not hasattr(cfg, "billing_token")
    assert not hasattr(cfg, "credits_per_review")


# --- statuses ------------------------------------------------------------------


def test_read_statuses_keeps_valid_member_rows_with_liveness(tmp_path):
    gh = with_board(
        FakeGitHub(),
        entry("bob", 10),
        entry("mallory", 99),
        entry("carol", 999, quota=10),
        entry("dave", 50),
        entry("erin", 70),
        online=["bob", "dave", "mallory"],
    )
    gh.issues[cp.HEARTBEAT_LABEL].append(heartbeat_node(200, "erin", editor="bob", edited_at=NOW))
    pool = make_pool(gh, tmp_path)
    statuses = {s["username"]: s for s in pool.read_statuses({"alice", "bob", "carol", "dave", "erin"})}
    assert sorted(statuses) == ["bob", "dave", "erin"]
    assert statuses["bob"]["last_seen"] == (NOW - dt.timedelta(minutes=1)).isoformat()
    # A heartbeat last edited by someone else doesn't count.
    assert statuses["erin"]["last_seen"] is None


def test_read_statuses_heartbeat_falls_back_to_creation_time(tmp_path):
    gh = with_board(FakeGitHub(), entry("bob", 10), online=[])
    gh.issues[cp.HEARTBEAT_LABEL] = [heartbeat_node(1, "bob", created_at=NOW - dt.timedelta(minutes=2))]
    (status,) = make_pool(gh, tmp_path).read_statuses({"bob"})
    assert status["last_seen"] == (NOW - dt.timedelta(minutes=2)).isoformat()


@pytest.mark.parametrize("text", ["", "no json", "```json\n[1]\n```", "```json\n{bad\n```"])
def test_read_statuses_tolerates_broken_board(tmp_path, text):
    gh = with_board(FakeGitHub(), text=text)
    assert make_pool(gh, tmp_path).read_statuses({"alice", "bob"}) == []


def test_read_statuses_missing_board(tmp_path):
    gh = FakeGitHub({BOARD: cp.GitHubError(404, "Not Found")})
    assert make_pool(gh, tmp_path).read_statuses({"alice"}) == []


def test_publish_status_creates_board_when_missing(tmp_path):
    gh = FakeGitHub({**pool_routes(), BOARD: cp.GitHubError(404, "Not Found")})
    make_pool(gh, tmp_path, used=100).publish_status({"alice"})
    body, board = put_board(gh)
    assert "sha" not in body
    assert board == {"alice": entry("alice", 200, updated_at=NOW.isoformat(timespec="seconds"))}
    text = base64.b64decode(body["content"]).decode()
    assert "| @alice | 200 | 300 | 100 | copilot |" in text


def test_publish_status_commits_only_when_values_change(tmp_path):
    old = NOW - dt.timedelta(days=3)
    gh = with_board(FakeGitHub(pool_routes()), entry("alice", 200, used=100, updated_at=old))
    pool = make_pool(gh, tmp_path, used=100)
    pool.publish_status({"alice"})
    assert gh.called(*BOARD_PUT) == []

    gh.routes[("GET", cp.COPILOT_USER_ENDPOINT)] = quota_snapshot(150, 300)
    pool.publish_status({"alice"})
    body, board = put_board(gh)
    assert body["sha"] == "s1"
    assert (board["alice"]["remaining"], board["alice"]["used"]) == (150, 150)
    assert board["alice"]["updated_at"] == NOW.isoformat(timespec="seconds")


def test_publish_status_commits_when_quota_or_source_changes(tmp_path):
    gh = with_board(FakeGitHub(pool_routes()), entry("alice", 200, quota=300, used=100))
    make_pool(gh, tmp_path, used=100, quota=400).publish_status({"alice"})
    assert len(gh.called(*BOARD_PUT)) == 1


def test_publish_status_keeps_valid_peers_and_drops_bad_rows(tmp_path):
    gh = with_board(
        FakeGitHub(pool_routes()),
        entry("bob", 10),
        entry("gone", 10),
        {**entry("carol", 10), "remaining": "lots"},
    )
    make_pool(gh, tmp_path).publish_status({"alice", "bob", "carol"})
    _, board = put_board(gh)
    assert sorted(board) == ["alice", "bob"]
    assert board["bob"] == entry("bob", 10)


def test_publish_status_rewrites_own_tampered_row(tmp_path):
    gh = with_board(FakeGitHub(pool_routes()), entry("alice", 999, used=0, quota=999))
    make_pool(gh, tmp_path, used=100).publish_status({"alice"})
    _, board = put_board(gh)
    assert board["alice"]["remaining"] == 200


def test_publish_status_retries_on_conflict(tmp_path):
    gh = with_board(FakeGitHub(pool_routes()))
    attempts = []

    def put(body):
        attempts.append(body)
        if len(attempts) == 1:
            raise cp.GitHubError(409, "sha mismatch")
        return {"commit": {"sha": "c"}}

    gh.routes[BOARD_PUT] = put
    make_pool(gh, tmp_path).publish_status({"alice"})
    assert len(attempts) == 2


def test_publish_status_gives_up_after_repeated_conflicts(tmp_path):
    gh = with_board(FakeGitHub(pool_routes()))
    gh.routes[BOARD_PUT] = cp.GitHubError(409, "sha mismatch")
    with pytest.raises(cp.GitHubError):
        make_pool(gh, tmp_path).publish_status({"alice"})
    assert len(gh.called(*BOARD_PUT)) == 3


def heartbeat_gh():
    gh = FakeGitHub({**pool_routes(), ("POST", f"/repos/{POOL}/issues"): {"number": 42}})
    for n in (2, 5, 7, 42):
        gh.routes.setdefault(("PATCH", f"/repos/{POOL}/issues/{n}"), {"number": n, "user": {"login": "alice"}})
    return gh


def test_heartbeat_creates_records_and_edits_only_that_issue(tmp_path):
    gh = heartbeat_gh()
    pool = make_pool(gh, tmp_path)
    pool.heartbeat()
    (_, _, created, _), = gh.called("POST", f"/repos/{POOL}/issues")
    assert created["labels"] == [cp.HEARTBEAT_LABEL]
    assert created["title"] == "pool-heartbeat: alice"
    assert disk_state(pool).heartbeat_issue == 42

    # A look-alike issue appearing later is never touched; only the recorded number is.
    gh.issues[cp.HEARTBEAT_LABEL] = [heartbeat_node(5, "alice")]
    pool._last_heartbeat = NOW - dt.timedelta(minutes=10)
    pool.heartbeat()
    assert gh.called("PATCH", f"/repos/{POOL}/issues/42")
    assert not gh.called("PATCH", f"/repos/{POOL}/issues/5")
    assert len(gh.called("POST", f"/repos/{POOL}/issues")) == 1


def test_heartbeat_restores_title_labels_and_open_state(tmp_path):
    gh = heartbeat_gh()
    pool = make_pool(gh, tmp_path)
    seed_state(pool, heartbeat_issue=2)
    pool.heartbeat()
    (_, _, patch, _), = gh.called("PATCH", f"/repos/{POOL}/issues/2")
    assert patch["state"] == "open"
    assert patch["title"] == "pool-heartbeat: alice"
    assert patch["labels"] == [cp.HEARTBEAT_LABEL]
    assert cp.is_own_heartbeat({"author": "alice", "title": patch["title"], "labels": patch["labels"], "body": patch["body"]})


def test_heartbeat_replaces_recorded_issue_that_isnt_ours(tmp_path):
    gh = heartbeat_gh()
    gh.routes[("PATCH", f"/repos/{POOL}/issues/7")] = {"number": 7, "user": {"login": "bob"}}
    pool = make_pool(gh, tmp_path)
    seed_state(pool, heartbeat_issue=7)
    pool.heartbeat()
    assert gh.called("POST", f"/repos/{POOL}/issues")
    assert disk_state(pool).heartbeat_issue == 42


def test_heartbeat_is_throttled(tmp_path):
    gh = heartbeat_gh()
    pool = make_pool(gh, tmp_path)
    pool.heartbeat()
    pool.heartbeat()
    assert len(gh.called("POST", f"/repos/{POOL}/issues")) == 1
    assert gh.called("PATCH", f"/repos/{POOL}/issues/42") == []


def test_heartbeat_recreates_when_recorded_issue_is_gone(tmp_path):
    gh = heartbeat_gh()
    gh.routes[("PATCH", f"/repos/{POOL}/issues/7")] = cp.GitHubError(404, "Not Found")
    pool = make_pool(gh, tmp_path)
    seed_state(pool, heartbeat_issue=7)
    pool.heartbeat()
    assert gh.called("POST", f"/repos/{POOL}/issues")
    assert disk_state(pool).heartbeat_issue == 42


def test_heartbeat_adopts_existing_issue_once(tmp_path):
    gh = heartbeat_gh()
    gh.issues[cp.HEARTBEAT_LABEL] = [heartbeat_node(5, "alice", editor="bob")]
    pool = make_pool(gh, tmp_path)
    pool.heartbeat()
    assert gh.called("PATCH", f"/repos/{POOL}/issues/5")
    assert not gh.called("POST", f"/repos/{POOL}/issues")
    assert disk_state(pool).heartbeat_issue == 5


@pytest.mark.parametrize(
    "lookalike",
    [
        node(9, "alice", "a request", title="pool-heartbeat: alice", labels=[cp.HEARTBEAT_LABEL]),
        heartbeat_node(9, "alice", labels=[cp.HEARTBEAT_LABEL, cp.REQUEST_LABEL]),
        node(9, "alice", cp.heartbeat_body("alice", NOW), title="Copilot review: octo/repo#3", labels=[cp.HEARTBEAT_LABEL]),
    ],
    ids=["request-body", "request-label", "other-title"],
)
def test_heartbeat_never_adopts_or_counts_lookalikes(tmp_path, lookalike):
    gh = heartbeat_gh()
    gh.issues[cp.HEARTBEAT_LABEL] = [lookalike]
    pool = make_pool(gh, tmp_path)
    assert pool.last_seen({"alice"}) == {}
    pool.heartbeat()
    assert not gh.called("PATCH", f"/repos/{POOL}/issues/9")
    assert gh.called("POST", f"/repos/{POOL}/issues")


def test_board_failure_does_not_suppress_heartbeat(tmp_path):
    gh = FakeGitHub(
        {
            **pool_routes(),
            BOARD: cp.GitHubError(500, "boom"),
            ("GET", "/user/repository_invitations"): [],
            ("POST", f"/repos/{POOL}/issues"): {"number": 42},
        }
    )
    make_pool(gh, tmp_path).run_once()
    (_, _, created, _), = gh.called("POST", f"/repos/{POOL}/issues")
    assert created["labels"] == [cp.HEARTBEAT_LABEL]


@pytest.mark.parametrize(
    "value",
    [None, [], {}, ["copilot"], {"a": 1}, 1.5, True, "x", -1, 10**30, float("nan")],
)
@pytest.mark.parametrize("key", ["username", "remaining", "quota", "used", "source", "updated_at"])
def test_valid_status_never_raises_on_odd_values(key, value):
    result = cp.valid_status({**entry("bob", 10), key: value}, "bob", NOW)
    assert result is None or set(result) == {"username", *cp.STATUS_FIELDS, "updated_at"}


def test_unhashable_source_in_peer_row_does_not_break_pool(tmp_path):
    gh = with_board(FakeGitHub(pool_routes()), entry("bob", 10), {**entry("carol", 20), "source": ["copilot"]})
    pool = make_pool(gh, tmp_path)
    assert [s["username"] for s in pool.read_statuses({"alice", "bob", "carol"})] == ["bob"]
    pool.publish_status({"alice", "bob", "carol"})
    _, board = put_board(gh)
    assert sorted(board) == ["alice", "bob"]


def test_publish_status_repairs_own_row_with_bad_timestamp(tmp_path):
    future = NOW + dt.timedelta(days=30)
    gh = with_board(FakeGitHub(pool_routes()), entry("alice", 200, used=100, updated_at=future))
    make_pool(gh, tmp_path, used=100).publish_status({"alice"})
    _, board = put_board(gh)
    assert board["alice"]["updated_at"] == NOW.isoformat(timespec="seconds")


def test_retire_legacy_status_issues_closes_only_own(tmp_path):
    gh = FakeGitHub(issues={cp.LEGACY_STATUS_LABEL: [node(1, "alice", "old"), node(2, "bob", "old")]})
    make_pool(gh, tmp_path).retire_legacy_status_issues()
    assert [c[1] for c in gh.calls if c[0] == "PATCH"] == [f"/repos/{POOL}/issues/1"]


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
    gh = with_board(FakeGitHub(routes), entry("bob", 10), entry("carol", 90))
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
    gh = with_board(FakeGitHub(routes), entry("bob", 10))
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
    gh = with_board(FakeGitHub(pool_routes()), entry("bob", 0))
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
    return with_board(FakeGitHub(routes), entry("carol", 90))


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
    daemon = make_pool(FakeGitHub(), tmp_path, me="alice", copilot=False)
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
    )
    with_board(gh, text="```json\n[1]\n```")
    make_pool(gh, tmp_path).run_once()
    assert gh.called(*BOARD_PUT)
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


def test_token_defaults_to_gh_cli(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="gho_abc\n", stderr="")

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    assert cp.Config(username="a", pool_repo=POOL, monthly_credits=1).resolve_token() == "gho_abc"
    assert calls == ["gh auth token"]


def test_explicit_token_still_honoured(monkeypatch):
    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: pytest.fail("should not shell out"))
    assert cp.Config(username="a", pool_repo=POOL, monthly_credits=1, token="t").resolve_token() == "t"


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CalledProcessError(1, "gh auth token", stderr="no oauth token"),
        subprocess.CompletedProcess("gh auth token", 0, stdout="\n", stderr=""),
    ],
)
def test_gh_token_failure_says_to_log_in(monkeypatch, result):
    def fake_run(cmd, **kw):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="gh auth login"):
        cp.Config(username="a", pool_repo=POOL, monthly_credits=1).resolve_token()


def test_config_roundtrip_is_private(tmp_path):
    path = tmp_path / "cfg" / "config.json"
    cfg = cp.Config(username="a", pool_repo=POOL, monthly_credits=300, token="secret")
    cfg.save(path)
    assert cp.Config.load(path) == cfg
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_config_save_locks_down_existing_directory(tmp_path):
    folder = tmp_path / "cfg"
    folder.mkdir(mode=0o755)
    cp.Config(username="a", pool_repo=POOL, monthly_credits=300, token="secret").save(folder / "config.json")
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
        cp.Config(username="a", pool_repo=POOL, monthly_credits=300, token="secret").save(path)
    finally:
        os.umask(old)
    assert modes == [0o600]
    assert path.stat().st_mode & 0o777 == 0o600
