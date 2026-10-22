"""Backend tests for offline sync and queue atomicity, against the queup-dev Redis (see P2P.md, Testing)."""
import base64
import hashlib
import hmac
import math
import importlib.util
import os
import random
import string
import sys
import threading
import time
import uuid

import pytest

PRIVATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"
ROOT = PRIVATE.rstrip("/").replace("/private/", "/public_html/")  # the matching public tree
if not PRIVATE.rstrip("/").endswith("/private/queup-dev"):
    raise RuntimeError("refusing to run outside the queup-dev tree: " + PRIVATE)
sys.path.insert(0, ROOT)

import lib.methods as m  # noqa: E402
import lib.lock  # noqa: E402
import lib.offlinesync as osync  # noqa: E402

m.set_private_path(PRIVATE)
lib.lock.set_private_path(PRIVATE)

_spec = importlib.util.spec_from_file_location("admin_methods", os.path.join(ROOT, "admin/alib/methods.py"))
admin_methods = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_methods)
admin_methods.set_private_path(PRIVATE)

Q = "default_queue"


@pytest.fixture
def room():
    for _ in range(50):
        code = "Z" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if not m.rds.exists("room" + code):
            break
    m.createroom(code, "tstaff1")
    yield code
    if m.get1q(code):
        m.set1q(False, code)
    if m.rds.exists("room" + code):
        m.deleteroom(code)
    m.rds.delete("peerkeys" + code, "offlineapplied" + code, "offlinestatus" + code, "qwatermark" + code, "lastadd" + code)
    log = PRIVATE + "logs/" + code + ".log"
    if os.path.exists(log):
        os.remove(log)


def join(room, user):
    pid = str(uuid.uuid4())
    key = osync.issue_peer_key(room, pid, user)
    assert key
    return {"peerid": pid, "user": user, "key": key, "seq": 0}


def env(peer, room, action, t=None, queue=Q, waitdata=None, target="", value=""):
    peer["seq"] += 1
    if waitdata is None:
        waitdata = "station 4" if action == "uadd" else ""
    e = {"id": str(uuid.uuid4()), "peerid": peer["peerid"], "room": room, "user": peer["user"],
         "action": action, "queue": queue, "target": target, "waitdata": waitdata, "value": value,
         "t": math.ceil((time.time() if t is None else t) * 1000), "seq": peer["seq"]}
    e["sig"] = sign(e, peer["key"])
    return e


def sign(e, key):
    return base64.b64encode(hmac.new(base64.b64decode(key), osync.canonical(e).encode(),
                                     hashlib.sha256).digest()).decode()


def users(room, queue=Q):
    return [x[0] for x in m.getusers(queue, room)]


def sync(room, envelopes, submitter="tstud2"):
    return osync.apply_offline_batch(room, envelopes, submitter)


# --- signing / authentication ---------------------------------------------

def test_applies_signed_add_and_logs_it(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd", waitdata="Alice, station 4")
    results, logs = sync(room, [e])
    assert results[e["id"]] == "applied"
    assert users(room) == ["tstud1"]
    assert logs == [[str(e["t"] / 1000.0), "tstud1", "ouadd", room, Q, "tstud2", "Alice, station 4"]]


def test_resubmission_is_duplicate_and_changes_nothing(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    assert sync(room, [e])[0][e["id"]] == "applied"
    results, logs = sync(room, [e], submitter="tstud1")
    assert results[e["id"]] == "duplicate"
    assert logs == []
    assert users(room) == ["tstud1"]


def test_forged_and_tampered_envelopes_rejected(room):
    p = join(room, "tstud1")
    tampered = env(p, room, "uadd")
    tampered["waitdata"] = "somewhere else"
    impersonated = env(p, room, "uadd")
    impersonated["user"] = "tstud2"          # signed as tstud1, claims tstud2
    unknown = env({"peerid": str(uuid.uuid4()), "user": "tstud1",
                   "key": base64.b64encode(b"x" * 32).decode(), "seq": 0}, room, "uadd")
    results, _ = sync(room, [tampered, impersonated, unknown])
    assert set(results.values()) == {"bad-signature"}
    assert users(room) == []


def test_tampered_copy_cannot_poison_genuine_envelope(room):
    p = join(room, "tstud1")
    genuine = env(p, room, "uadd")
    forged = dict(genuine, waitdata="forged")
    assert sync(room, [forged], submitter="tstud2")[0][genuine["id"]] == "bad-signature"
    assert sync(room, [genuine], submitter="tstud3")[0][genuine["id"]] == "applied"


def test_envelope_for_another_room_is_invalid(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    e["room"] = "ZZZZZ" if room != "ZZZZZ" else "ZZZZY"
    e["sig"] = sign(e, p["key"])
    assert sync(room, [e])[0][e["id"]] == "invalid"


def test_peerid_cannot_be_claimed_by_another_user(room):
    p = join(room, "tstud1")
    assert osync.issue_peer_key(room, p["peerid"], "tstud2") is None
    assert osync.issue_peer_key(room, p["peerid"], "tstud1") == p["key"]   # rejoin reuses key


# --- time bounds ------------------------------------------------------------

def test_time_bounds(room):
    p = join(room, "tstud1")
    future = env(p, room, "uadd", t=time.time() + 600)
    before_key = env(p, room, "uadd", t=time.time() - 600)
    results, logs = sync(room, [future, before_key])
    assert results[future["id"]] == "bad-time"
    assert results[before_key["id"]] == "bad-time"
    assert {row[7] for row in logs} == {"bad-time"}


def test_retention_window(room):
    p = join(room, "tstud1")
    # backdate the key so only the retention bound can reject this
    rec = m.json.loads(m.rds.hget("peerkeys" + room, p["peerid"]))
    rec["issued"] = time.time() - 5 * 3600
    m.rds.hset("peerkeys" + room, p["peerid"], m.json.dumps(rec))
    old = env(p, room, "uadd", t=time.time() - osync.RETENTION_SECONDS - 60)
    assert sync(room, [old])[0][old["id"]] == "bad-time"


# --- conflict resolution ----------------------------------------------------

def test_lone_del_rejected(room):
    p = join(room, "tstud1")
    e = env(p, room, "udel")
    assert sync(room, [e])[0][e["id"]] == "rejected:not-in-queue"


def test_add_then_del_in_one_batch_nets_out(room):
    p = join(room, "tstud1")
    now = time.time()
    add, dele = env(p, room, "uadd", t=now), env(p, room, "udel", t=now + 1)
    results, _ = sync(room, [dele, add])     # submitted out of order
    assert results[add["id"]] == results[dele["id"]] == "applied"
    assert users(room) == []


def test_out_of_order_across_batches(room):
    p = join(room, "tstud1")
    now = time.time()
    add, dele = env(p, room, "uadd", t=now), env(p, room, "udel", t=now + 1)
    assert sync(room, [dele])[0][dele["id"]] == "rejected:not-in-queue"
    assert sync(room, [add])[0][add["id"]] == "superseded"
    assert users(room) == []


def test_online_action_after_recovery_supersedes_stale_offline(room):
    p = join(room, "tstud1")
    stale_add = env(p, room, "uadd", t=time.time())
    time.sleep(0.05)
    m.addquser("tstud1", "online", Q, room)
    m.delquser("tstud1", Q, room)
    assert sync(room, [stale_add])[0][stale_add["id"]] == "superseded"
    assert users(room) == []


def test_queue_clear_supersedes_older_offline_add(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd", t=time.time())
    time.sleep(0.05)
    m.setqueuewatermark(Q, room)
    assert sync(room, [e])[0][e["id"]] == "superseded"


def test_policy_rejection_does_not_fence_older_actions(room):
    # in queue since just now, 10-minute cooldown; offline: leave, then re-add
    m.setcooldown(10, room)
    m.addquser("tstud1", "online", Q, room)
    p = join(room, "tstud1")
    now = time.time()
    dele, readd = env(p, room, "udel", t=now + 1), env(p, room, "uadd", t=now + 2)
    # a partial batch with only the re-add arrives first: cooldown rejects it
    assert sync(room, [readd])[0][readd["id"]] == "rejected:cooldown"
    # the earlier del must still apply -- the rejected add expressed no state
    assert sync(room, [dele])[0][dele["id"]] == "applied"
    assert users(room) == []


def test_readd_already_satisfied_fences_older_del(room):
    # in queue; offline leave then re-add: if the re-add syncs first, the later-arriving leave is stale
    m.addquser("tstud1", "online", Q, room)
    p = join(room, "tstud1")
    now = time.time()
    dele, readd = env(p, room, "udel", t=now + 1), env(p, room, "uadd", t=now + 2)
    assert sync(room, [readd])[0][readd["id"]] == "rejected:already-in-queue"
    assert sync(room, [dele])[0][dele["id"]] == "superseded"
    assert users(room) == ["tstud1"]


def test_cooldown_survives_removal_earlier_in_batch(room):
    m.setcooldown(10, room)
    m.addquser("tstud1", "online", Q, room)
    p = join(room, "tstud1")
    now = time.time()
    dele, readd = env(p, room, "udel", t=now + 1), env(p, room, "uadd", t=now + 2)
    results, _ = sync(room, [dele, readd])
    assert results[dele["id"]] == "applied"
    assert results[readd["id"]] == "rejected:cooldown"
    assert users(room) == []


def test_cooldown_counts_earlier_adds_in_same_batch(room):
    m.setcooldown(10, room)
    p = join(room, "tstud1")
    now = time.time()
    a1, d1, a2 = (env(p, room, "uadd", t=now), env(p, room, "udel", t=now + 1),
                  env(p, room, "uadd", t=now + 2))
    results, _ = sync(room, [a1, d1, a2])
    assert (results[a1["id"]], results[d1["id"]], results[a2["id"]]) == \
        ("applied", "applied", "rejected:cooldown")


def test_online_cooldown_measured_from_last_add_even_after_leaving(room):
    assert m.getlastadd(room, "tstud1") == 0
    m.addquser("tstud1", "x", Q, room)
    m.delquser("tstud1", Q, room)       # helped and removed
    assert abs(m.getlastadd(room, "tstud1") - time.time()) < 5


def test_offline_cooldown_after_being_helped_online(room):
    m.setcooldown(10, room)
    m.addquser("tstud1", "x", Q, room)
    m.delquser("tstud1", Q, room)
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    assert sync(room, [e])[0][e["id"]] == "rejected:cooldown"


def test_offline_add_updates_lastadd_for_online_cooldown(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    sync(room, [e])
    assert m.getlastadd(room, "tstud1") == e["t"] / 1000.0


def test_locked_room_rejects_non_owner_add(room):
    m.lockroom(room)
    stud, staff = join(room, "tstud1"), join(room, "tstaff1")
    s, o = env(stud, room, "uadd"), env(staff, room, "uadd")
    results, _ = sync(room, [s, o])
    assert results[s["id"]] == "rejected:locked"
    assert results[o["id"]] == "applied"


def test_singlequeue(room):
    m.createqueue("second_q", room)
    m.set1q(True, room)
    p = join(room, "tstud1")
    now = time.time()
    a, b = env(p, room, "uadd", t=now), env(p, room, "uadd", t=now + 1, queue="second_q")
    results, _ = sync(room, [a, b])
    assert results[a["id"]] == "applied"
    assert results[b["id"]] == "rejected:singlequeue"


def test_deleted_queue_is_invalid(room):
    p = join(room, "tstud1")
    m.createqueue("gone_q", room)
    e = env(p, room, "uadd", queue="gone_q")
    m.deletequeue("gone_q", room)
    assert sync(room, [e])[0][e["id"]] == "invalid"


# --- staff actions ---------------------------------------------------------

def marks(room, queue=Q):
    return {x[0]: x[3] for x in m.getusers(queue, room)}


def test_staff_remove_applies_and_logs(room):
    m.addquser("tstud1", "x", Q, room)
    staff = join(room, "tstaff1")
    e = env(staff, room, "usdel", target="tstud1")
    results, logs = sync(room, [e], submitter="tstud1")
    assert results[e["id"]] == "applied"
    assert users(room) == []
    assert logs == [[str(e["t"] / 1000.0), "tstaff1", "ousdel", room, Q, "tstud1", "tstud1"]]


def test_student_cannot_use_staff_actions(room):
    m.addquser("tstud2", "x", Q, room)
    stud = join(room, "tstud1")
    rm, mk, cl = (env(stud, room, "usdel", target="tstud2"), env(stud, room, "qmrk", target="tstud2", value="1"),
                  env(stud, room, "qclr"))
    results, logs = sync(room, [rm, mk, cl])
    assert {results[x["id"]] for x in (rm, mk, cl)} == {"rejected:not-owner"}
    assert users(room) == ["tstud2"] and marks(room) == {"tstud2": 0}
    assert {row[6] for row in logs} == {"usdel:tstud2", "qmrk:tstud2", "qclr"}


def test_owner_status_checked_at_sync_time(room):
    m.ownroom(room, "tstaff2")
    m.addquser("tstud1", "x", Q, room)
    staff2 = join(room, "tstaff2")
    e = env(staff2, room, "usdel", target="tstud1")
    m.delownroom(room, "tstaff2")          # no longer an owner by the time it syncs
    assert sync(room, [e])[0][e["id"]] == "rejected:not-owner"
    assert users(room) == ["tstud1"]


def test_staff_remove_fences_older_student_add(room):
    stud, staff = join(room, "tstud1"), join(room, "tstaff1")
    now = time.time()
    add, rm = env(stud, room, "uadd", t=now), env(staff, room, "usdel", t=now + 1, target="tstud1")
    assert sync(room, [rm])[0][rm["id"]] == "rejected:not-in-queue"
    assert sync(room, [add])[0][add["id"]] == "superseded"
    assert users(room) == []


def test_mark_is_absolute_and_idempotent(room):
    m.addquser("tstud1", "x", Q, room)
    staff = join(room, "tstaff1")
    now = time.time()
    on, on_again = (env(staff, room, "qmrk", t=now, target="tstud1", value="1"),
                    env(staff, room, "qmrk", t=now + 1, target="tstud1", value="1"))
    results, _ = sync(room, [on, on_again])
    assert results[on["id"]] == results[on_again["id"]] == "applied"
    assert marks(room) == {"tstud1": 1}              # a toggle would have flipped back to 0
    assert sync(room, [on])[0][on["id"]] == "duplicate"


def test_older_offline_mark_loses_to_newer_online_toggle(room):
    m.addquser("tstud1", "x", Q, room)
    staff = join(room, "tstaff1")
    stale = env(staff, room, "qmrk", t=time.time(), target="tstud1", value="1")
    time.sleep(0.05)
    m.togglemark("tstud1", Q, room)     # online: 0 -> 1
    m.togglemark("tstud1", Q, room)     # online: 1 -> 0, the latest intent
    assert sync(room, [stale])[0][stale["id"]] == "superseded"
    assert marks(room) == {"tstud1": 0}


def test_mark_does_not_carry_over_to_a_newer_entry(room):
    staff, stud = join(room, "tstaff1"), join(room, "tstud1")
    now = time.time()
    add1, rm, add2 = (env(stud, room, "uadd", t=now), env(stud, room, "udel", t=now + 1),
                      env(stud, room, "uadd", t=now + 3))
    mark_old = env(staff, room, "qmrk", t=now + 0.5, target="tstud1", value="1")
    results, _ = sync(room, [add1, mark_old, rm, add2])
    assert results[mark_old["id"]] == "applied"
    assert marks(room) == {"tstud1": 0}              # the re-added entry starts unmarked


def test_mark_on_absent_user_rejected_without_fencing(room):
    staff, stud = join(room, "tstaff1"), join(room, "tstud1")
    now = time.time()
    mk, add = env(staff, room, "qmrk", t=now + 1, target="tstud1", value="1"), env(stud, room, "uadd", t=now)
    assert sync(room, [mk])[0][mk["id"]] == "rejected:not-in-queue"
    assert sync(room, [add])[0][add["id"]] == "applied"


def test_clear_removes_only_entries_that_existed_then(room):
    m.addquser("tstud1", "x", Q, room)       # before the outage
    staff, stud2, stud3 = join(room, "tstaff1"), join(room, "tstud2"), join(room, "tstud3")
    now = time.time()
    before = env(stud2, room, "uadd", t=now)
    clear = env(staff, room, "qclr", t=now + 1)
    after = env(stud3, room, "uadd", t=now + 2)
    results, logs = sync(room, [after, clear, before])
    assert {results[x["id"]] for x in (before, clear, after)} == {"applied"}
    assert users(room) == ["tstud3"]
    assert [r[2] for r in logs] == ["ouadd", "oqclr", "ouadd"]


def test_clear_supersedes_late_arriving_older_add(room):
    staff, stud = join(room, "tstaff1"), join(room, "tstud1")
    now = time.time()
    add, clear = env(stud, room, "uadd", t=now), env(staff, room, "qclr", t=now + 1)
    assert sync(room, [clear])[0][clear["id"]] == "applied"
    assert sync(room, [add])[0][add["id"]] == "superseded"
    assert users(room) == []


def test_field_rules_per_action(room):
    staff = join(room, "tstaff1")
    bad = [
        env(staff, room, "usdel"),                                   # target required
        env(staff, room, "qmrk", target="tstud1", value="2"),         # value must be 0/1
        env(staff, room, "qclr", target="tstud1"),                   # no target for clear
        env(staff, room, "udel", value="1"),                         # no value for self del
        env(staff, room, "usdel", target="tstud1", waitdata="hi"),    # waitdata only for uadd
    ]
    assert set(sync(room, bad)[0].values()) == {"invalid"}


# --- online requests carry click time + operation id (slow-server safety) ---

def test_late_online_request_cannot_undo_newer_offline_actions(room):
    # a join and leave sat on a hung server while the user rejoined over the mesh; the server ran them first
    p = join(room, "tstud1")
    t0 = time.time()
    add_id, del_id = str(uuid.uuid4()), str(uuid.uuid4())
    rejoin = env(p, room, "uadd", t=t0 + 3)
    m.addquser("tstud1", "x", Q, room, at=t0, opid=add_id)      # processed late
    m.delquser("tstud1", Q, room, at=t0 + 1, opid=del_id)       # processed late
    assert sync(room, [rejoin])[0][rejoin["id"]] == "applied"
    assert users(room) == ["tstud1"]


def test_late_online_request_after_sync_is_stale(room):
    p = join(room, "tstud1")
    t0 = time.time()
    leave_online = str(uuid.uuid4())
    add, rejoin = env(p, room, "uadd", t=t0), env(p, room, "uadd", t=t0 + 2)
    dele = env(p, room, "udel", t=t0 + 1)
    sync(room, [add, dele, rejoin])
    assert users(room) == ["tstud1"]
    # a leave clicked at t0+1.5 reaches the server only now
    with pytest.raises(m.StaleAction):
        m.delquser("tstud1", Q, room, at=t0 + 1.5, opid=leave_online)
    assert users(room) == ["tstud1"]


def test_online_request_and_its_mesh_copy_apply_once(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    # mesh copy first, then the same operation arrives over HTTP
    assert sync(room, [e])[0][e["id"]] == "applied"
    with pytest.raises(m.DuplicateAction):
        m.addquser("tstud1", "x", Q, room, at=e["t"] / 1000.0, opid="tstud1:" + e["id"])
    # and the other way round
    m.delquser("tstud1", Q, room)
    time.sleep(0.01)                    # envelope times are whole milliseconds
    e2 = env(p, room, "uadd")
    m.addquser("tstud1", "x", Q, room, at=e2["t"] / 1000.0, opid="tstud1:" + e2["id"])
    assert sync(room, [e2])[0][e2["id"]] == "duplicate"
    assert users(room) == ["tstud1"]


def test_redundant_online_join_fences_older_pending_leave(room):
    # online join, then a pending offline leave, then a later redundant online join: the last join wins
    p = join(room, "tstud1")
    t0 = time.time()
    m.addquser("tstud1", "a", Q, room, at=t0, opid=str(uuid.uuid4()))
    leave = env(p, room, "udel", t=t0 + 1)
    m.fence(room, "tstud1|" + Q, t0 + 2)          # what roomd does for join B
    assert sync(room, [leave])[0][leave["id"]] == "superseded"
    assert users(room) == ["tstud1"]


def test_fence_only_raises(room):
    m.fence(room, "tstud1|" + Q, 100.0)
    m.fence(room, "tstud1|" + Q, 50.0)
    assert float(m.rds.hget("qwatermark" + room, "tstud1|" + Q)) == 100.0


def test_recorded_rejection_makes_mesh_copy_a_duplicate(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    m.recordop(room, "tstud1:" + e["id"])   # e.g. online add answered with [cooldown]
    assert sync(room, [e])[0][e["id"]] == "duplicate"
    assert users(room) == []


def test_other_users_cannot_suppress_an_action_by_recording_its_id(room):
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    m.recordop(room, "tstud2:" + e["id"])   # tstud2 saw the id over the mesh and "decided" it
    assert sync(room, [e])[0][e["id"]] == "applied"
    assert users(room) == ["tstud1"]


def test_resubmitted_rejection_repeats_the_reason(room):
    m.lockroom(room)
    p = join(room, "tstud1")
    e = env(p, room, "uadd")
    assert sync(room, [e])[0][e["id"]] == "rejected:locked"
    assert sync(room, [e])[0][e["id"]] == "rejected:locked"   # the originator still learns why


def test_online_clear_keeps_people_who_joined_after_the_click(room):
    m.addquser("tstud1", "x", Q, room)
    t_click = time.time()
    time.sleep(0.05)
    m.addquser("tstud2", "x", Q, room)                  # joined while the clear was in flight
    m.clearqueue(Q, room, at=t_click, opid=str(uuid.uuid4()))
    assert users(room) == ["tstud2"]


def test_online_mark_for_an_older_entry_is_stale(room):
    t_click = time.time()
    time.sleep(0.05)
    m.addquser("tstud1", "x", Q, room)                  # re-added after staff clicked Mark
    with pytest.raises(m.StaleAction):
        m.togglemark("tstud1", Q, room, at=t_click, opid=str(uuid.uuid4()))
    assert marks(room) == {"tstud1": 0}


# --- presence ---------------------------------------------------------------

def test_mark_present_recreates_expired_meta_with_original_join_time(room):
    p = join(room, "tstud1")
    rec = osync.get_peer_records(room, [p["peerid"]])
    issued = rec[p["peerid"]]["issued"]
    assert osync.mark_present(room, rec, m.getowners(room)) is True       # first appearance
    assert osync.mark_present(room, rec, m.getowners(room)) is False      # plain refresh: silent
    m.rds.delete("peermeta" + room, "peers" + room)                       # expired during an outage
    assert osync.mark_present(room, rec, m.getowners(room)) is True
    meta = m.json.loads(m.rds.hget("peermeta" + room, p["peerid"]))
    assert meta == {"user": "tstud1", "owner": False, "joined": issued, "v": 0}
    assert m.rds.ttl("peermeta" + room) > 0 and m.rds.ttl("peers" + room) > 0


def test_get_peer_records_skips_unknown_peers(room):
    p = join(room, "tstaff1")
    recs = osync.get_peer_records(room, [p["peerid"], str(uuid.uuid4())])
    assert list(recs) == [p["peerid"]] and recs[p["peerid"]]["user"] == "tstaff1"


# --- ordering ---------------------------------------------------------------

def test_offline_adds_interleave_by_time(room):
    p1, p2, p3 = join(room, "tstud1"), join(room, "tstud2"), join(room, "tstud3")
    t0 = time.time()
    early = env(p1, room, "uadd", t=t0)
    time.sleep(0.3)
    m.addquser("tstaff1", "online after recovery", Q, room)   # time ~ t0+0.3
    late = env(p2, room, "uadd", t=time.time() + 0.2)
    mid = env(p3, room, "uadd", t=t0 + 0.1)
    sync(room, [late, early, mid])
    assert users(room) == ["tstud1", "tstud3", "tstaff1", "tstud2"]


# --- atomicity --------------------------------------------------------------

def test_concurrent_sync_and_online_adds_both_survive(room):
    online_users = ["on%d" % i for i in range(8)]
    offline_peers = [join(room, "of%d" % i) for i in range(8)]
    envelopes = [env(p, room, "uadd") for p in offline_peers]
    errors = []

    def online(u):
        try:
            m.addquser(u, "x", Q, room)
        except Exception as ex:  # pragma: no cover - surfaced below
            errors.append(ex)

    def offline(e):
        try:
            sync(room, [e])
        except Exception as ex:  # pragma: no cover
            errors.append(ex)

    threads = [threading.Thread(target=online, args=(u,)) for u in online_users] + \
              [threading.Thread(target=offline, args=(e,)) for e in envelopes]
    random.shuffle(threads)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(users(room)) == sorted(online_users + [p["user"] for p in offline_peers])


def test_addquser_duplicate_check_fixed(room):
    m.addquser("tstud1", "x", Q, room)
    with pytest.raises(Exception, match="already in queue"):
        m.addquser("tstud1", "x", Q, room)


# --- cleanup / admin log ----------------------------------------------------

def test_deleteroom_removes_phase2_keys(room):
    p = join(room, "tstud1")
    sync(room, [env(p, room, "uadd")])
    keys = ("peerkeys" + room, "offlineapplied" + room, "offlinestatus" + room, "qwatermark" + room, "lastadd" + room)
    assert m.rds.exists(*keys) == 5
    m.deleteroom(room)
    assert m.rds.exists(*keys) == 0


def test_admin_log_rejoins_commas_and_sorts(room):
    lines = [
        "200.0,tstud1,uadd,%s,%s,Alice, station 4" % (room, Q),
        "100.0,tstud2,ouadd,%s,%s,tstud1,Bob, lab (2)" % (room, Q),
        "150.0,tstaff1,rown,%s,aa,bb,cc" % room,
        "300.0,tstud1,udel,%s,%s" % (room, Q),
    ]
    os.makedirs(PRIVATE + "logs", exist_ok=True)
    with open(PRIVATE + "logs/" + room + ".log", "w") as f:
        f.write("\n".join(lines) + "\n")
    rows = admin_methods.getdblog(room)
    # getdblog splices the section in at index 2
    assert [r[0] for r in rows] == ["100.0", "150.0", "200.0", "300.0"]
    assert rows[0][-1] == "Bob, lab (2)" and rows[0][-2] == "tstud1"
    assert rows[1][-1] == "aa,bb,cc"
    assert rows[2][-1] == "Alice, station 4"
    assert len(rows[3]) == 6
