"""Rate limiter tests against the queup-dev Redis (see P2P.md, Testing)."""
import os
import sys
import time
import uuid

import pytest

PRIVATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"
ROOT = PRIVATE.rstrip("/").replace("/private/", "/public_html/")  # the matching public tree
if not PRIVATE.rstrip("/").endswith("/private/queup-dev"):
    raise RuntimeError("refusing to run outside the queup-dev tree: " + PRIVATE)
sys.path.insert(0, ROOT)

import lib.ratelimiter as rl  # noqa: E402


@pytest.fixture
def user():
    name = "zr" + uuid.uuid4().hex[:6]
    yield name
    with rl.RateLimiter(PRIVATE + "ratelimit.db", env(name, "action=chk")) as r:
        r.conn.delete("rl:action:" + name, "rl:mesh:" + name)


def env(user, query):
    return {"REMOTE_USER": user, "QUERY_STRING": query}


def hit(user, query):
    with rl.RateLimiter(PRIVATE + "ratelimit.db", env(user, query)) as r:
        return r.bucket, r.should_limit(user)


def test_buckets():
    assert rl.bucket_for(env("x", "setup=true&action=offlinesync&room=AAAAA")) == "mesh"
    assert rl.bucket_for(env("x", "sseupdate=true&room=AAAAA")) == "mesh"
    assert rl.bucket_for(env("x", "action=add&room=AAAAA&queue=q")) == "action"
    assert rl.bucket_for(env("x", "setup=true&action=del&room=AAAAA")) == "action"
    # an sseupdate flag can't move a queue or room action into the larger budget
    assert rl.bucket_for(env("x", "action=add&room=AAAAA&queue=q&sseupdate=1")) == "action"
    assert rl.bucket_for(env("x", "setup=true&action=lock&room=AAAAA&sseupdate=1")) == "action"
    assert rl.bucket_for(env("x", "setup=true&action=peerjoin&room=AAAAA&queue=q")) == "action"


def test_fourth_action_in_a_second_is_limited(user):
    results = [hit(user, "action=add&room=AAAAA&queue=q")[1] for _ in range(4)]
    assert results == [False, False, False, True]


def test_rejected_retries_do_not_extend_the_lockout(user):
    for _ in range(4):
        hit(user, "action=add&room=AAAAA&queue=q")
    # keep retrying about every 0.3s, as a stuck client would
    end = time.time() + 1.2
    while time.time() < end:
        hit(user, "action=add&room=AAAAA&queue=q")
        time.sleep(0.3)
    # the window that opened at the first request has run out by now
    assert hit(user, "action=add&room=AAAAA&queue=q")[1] is False


def test_mesh_traffic_never_uses_the_action_budget(user):
    for _ in range(20):
        assert hit(user, "setup=true&action=offlinesync&room=AAAAA")[1] is False
    assert hit(user, "action=add&room=AAAAA&queue=q") == ("action", False)


def test_mesh_budget_is_still_enforced(user):
    limit = rl.LIMITS["mesh"][0]
    results = [hit(user, "setup=true&action=peerleave&room=AAAAA")[1] for _ in range(limit + 1)]
    assert results[:limit] == [False] * limit and results[limit] is True
