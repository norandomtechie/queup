"""Offline actions: per-peer signing keys, signed envelopes, and batch sync (see RELAY.md)."""
import base64
import hashlib
import hmac
import json
import re
import secrets
from time import time

import lib.methods as m

RETENTION_SECONDS = 3 * 3600    # offline actions older than this are rejected
PEERKEY_TTL = 86400             # peerkeys<room> members unseen this long are pruned
APPLIED_TTL = 86400             # how long processed envelope ids are remembered
CLOCK_TOLERANCE = 5             # seconds of residual clock skew tolerated
MAX_BATCH = 200
SELF_ACTIONS = ("uadd", "udel")
OWNER_ACTIONS = ("usdel", "qmrk", "qclr")   # staff remove, set mark, clear queue
OFFLINE_ACTIONS = SELF_ACTIONS + OWNER_ACTIONS


def issue_peer_key(room, peerid, user, version=0):
    """Return this peer's signing key, creating it on first join; None if the peerid belongs to another user."""
    rds = m.rds
    key = "peerkeys" + room
    now = time()
    raw = rds.hget(key, peerid)
    if raw:
        rec = json.loads(raw)
        if rec.get("user") != user:
            return None
        rec["seen"] = now
        rec["v"] = version
    else:
        rec = {"key": base64.b64encode(secrets.token_bytes(32)).decode(),
               "user": user, "issued": now, "seen": now, "v": version}
    rds.hset(key, peerid, json.dumps(rec))
    rds.expire(key, PEERKEY_TTL)
    # whole-key TTL can't expire individual members, so prune them here
    stale = []
    for field, val in rds.hgetall(key).items():
        try:
            if json.loads(val).get("seen", 0) < now - PEERKEY_TTL:
                stale.append(field)
        except ValueError:
            stale.append(field)
    if stale:
        rds.hdel(key, *stale)
    return rec["key"]


def get_peer_records(room, peerids):
    """peerid -> {key, user, issued, seen} for the peerids that have joined."""
    if not peerids:
        return {}
    raws = m.rds.hmget("peerkeys" + room, peerids)
    return {pid: json.loads(raw) for pid, raw in zip(peerids, raws) if raw}


PRESENCE_TTL = 120
MAX_HEARTBEAT_BATCH = 300


def mark_present(room, records, owners):
    """Refresh presence for {peerid: record}; True if any peer (re)appeared."""
    if not records:
        return False
    now = time()
    pipe = m.rds.pipeline()
    pipe.zadd("peers" + room, {pid: now for pid in records})
    for pid, rec in records.items():
        pipe.hsetnx("peermeta" + room, pid, json.dumps({
            "user": rec["user"], "owner": rec["user"] in owners, "joined": rec["issued"],
            "v": rec.get("v", 0)}))
    pipe.expire("peers" + room, PRESENCE_TTL)
    pipe.expire("peermeta" + room, PRESENCE_TTL)
    res = pipe.execute()
    return res[0] > 0 or any(res[1:1 + len(records)])


def canonical(e):
    # "|" appears in none of the fields' allowed character sets
    return "|".join([e["id"], e["peerid"], e["room"], e["user"], e["action"], e["queue"],
                     e["target"], e["waitdata"], e["value"], str(e["t"]), str(e["seq"])])


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _well_formed(e, room):
    try:
        act = e.get("action")
        if not (e.get("room") == room
                and isinstance(e.get("peerid"), str) and re.match(m.PEERID_RGX, e["peerid"])
                and isinstance(e.get("user"), str) and re.match(m.USER_RGX, e["user"])
                and act in OFFLINE_ACTIONS
                and isinstance(e.get("queue"), str) and re.match(m.QUEUE_RGX, e["queue"])
                and all(isinstance(e.get(f), str) for f in ("target", "waitdata", "value", "sig"))
                and _is_int(e.get("t")) and _is_int(e.get("seq")) and e["seq"] >= 0):
            return False
        # each field is either required or must be empty, per action
        if act in ("usdel", "qmrk"):
            if not re.match(m.USER_RGX, e["target"]):
                return False
        elif e["target"] != "":
            return False
        if act == "uadd":
            if e["waitdata"] != "" and not re.match(m.WAITDATA_RGX, e["waitdata"]):
                return False
        elif e["waitdata"] != "":
            return False
        if act == "qmrk":
            return e["value"] in ("0", "1")
        return e["value"] == ""
    except Exception:
        return False


def _signature_ok(e, rec):
    try:
        secret = base64.b64decode(rec["key"])
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(secret, canonical(e).encode(), hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, e["sig"])


def apply_offline_batch(room, envelopes, submitter, now=None):
    """Verify and apply offline envelopes; returns ({id: status}, log rows). Safe to resubmit."""
    rds = m.rds
    now = time() if now is None else now
    results = {}
    peer_recs = {}
    candidates = []

    for e in envelopes[:MAX_BATCH]:
        if not isinstance(e, dict):
            continue
        eid = e.get("id")
        if not isinstance(eid, str) or not re.match(m.PEERID_RGX, eid) or eid in results:
            continue
        if not _well_formed(e, room):
            results[eid] = "invalid"
            continue
        pid = e["peerid"]
        if pid not in peer_recs:
            raw = rds.hget("peerkeys" + room, pid)
            peer_recs[pid] = json.loads(raw) if raw else None
        rec = peer_recs[pid]
        if rec is None or rec.get("user") != e["user"] or not _signature_ok(e, rec):
            results[eid] = "bad-signature"
            continue
        t_sec = e["t"] / 1000.0
        bad_time = (t_sec < rec["issued"] - CLOCK_TOLERANCE or t_sec > now + CLOCK_TOLERANCE
                    or t_sec < now - RETENTION_SECONDS)
        candidates.append((t_sec, pid, e["seq"], e, bad_time))

    # within one peer seq is exact causal order; across peers, corrected time
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    owners = m.getowners(room)
    singlequeue = m.get1q(room)
    applied_key = "offlineapplied" + room
    status_key = "offlinestatus" + room
    wm_key = "qwatermark" + room
    lastadd_key = "lastadd" + room
    state = {}

    def mutate(rds_room, pipe):
        # may run more than once (WATCH retry) -- rebuild all outputs each time
        outcome, logs, wm_updates, processed, lastadd_updates, statuses = {}, [], {}, [], {}, {}
        # ids are recorded per signing user, matching roomd's "user:opid" for online requests
        ids = [c[3]["user"] + ":" + c[3]["id"] for c in candidates]
        flags = pipe.smismember(applied_key, ids) if ids else []
        already = {i for i, f in zip(ids, flags) if f}
        previous = dict(zip(ids, pipe.hmget(status_key, ids))) if ids else {}
        wm = {k.decode(): float(v) for k, v in pipe.hgetall(wm_key).items()}
        stored_last_add = {k.decode(): float(v) for k, v in pipe.hgetall(lastadd_key).items()}
        queues = rds_room["queues"]
        locked = rds_room.get("locked") == 1
        cooldown = int(rds_room.get("cooldown") or 0)
        batch_last_add = {}
        # Last add per user at batch start (entries removed later in the batch still count)
        initial_last_add = dict(stored_last_add)
        for qq in queues:
            for x in queues[qq]:
                initial_last_add[x["user"]] = max(initial_last_add.get(x["user"], 0), x["time"])
        changed = False

        for t_sec, pid, seq, e, bad_time in candidates:
            eid, user, q, act = e["id"], e["user"], e["queue"], e["action"]
            nid = user + ":" + eid
            if nid in already:
                # repeat a rejection so the sender learns why; anything else is just a duplicate
                prev = (previous.get(nid) or b"").decode()
                outcome[eid] = prev if prev.startswith("rejected:") or prev == "bad-time" else "duplicate"
                continue
            processed.append(nid)
            status = None
            fence = None          # membership watermark key to advance
            subject = e["target"] if act in ("usdel", "qmrk") else user
            member_key = subject + "|" + q
            queue_mark = wm.get("*|" + q, 0)
            if bad_time:
                status = "bad-time"
            elif q not in queues:
                status = "invalid"
            elif act in OWNER_ACTIONS and user not in owners:
                # ownership as of now, not as of when the key was issued
                status = "rejected:not-owner"
            elif act == "qclr":
                if t_sec < queue_mark:
                    status = "superseded"
                else:
                    # A clear removes only entries that existed when it happened
                    queues[q] = [x for x in queues[q] if x["time"] > t_sec]
                    wm["*|" + q] = t_sec
                    wm_updates["*|" + q] = t_sec
                    status = "applied"
                    logs.append([str(t_sec), user, "oqclr", room, q, submitter])
            elif act == "qmrk":
                entry = next((x for x in queues[q] if x["user"] == subject), None)
                if t_sec < max(wm.get(member_key, 0), queue_mark, wm.get("m:" + member_key, 0)):
                    # a later mark, or the user was removed/re-added since
                    status = "superseded"
                elif entry is None:
                    status = "rejected:not-in-queue"     # marks don't express membership: no fence
                elif entry["time"] > t_sec:
                    status = "superseded"                # this entry is newer than the mark
                else:
                    entry["mark"] = int(e["value"])
                    wm["m:" + member_key] = t_sec
                    wm_updates["m:" + member_key] = t_sec
                    status = "applied"
                    logs.append([str(t_sec), user, "oqmrk", room, q, submitter, subject, e["value"]])
            elif t_sec < max(wm.get(member_key, 0), queue_mark):
                # A newer change for this user/queue already won
                status = "superseded"
            else:
                entries = queues[q]
                in_queue = any(x["user"] == subject for x in entries)
                if act == "uadd":
                    last_add = max(initial_last_add.get(user, 0), batch_last_add.get(user, 0))
                    # Room rules before membership: a rejected add must not fence anything
                    if locked and user not in owners:
                        status = "rejected:locked"
                    elif cooldown > 0 and (t_sec - last_add) < cooldown * 60:
                        status = "rejected:cooldown"
                    elif singlequeue and any(x["user"] == user for qq in queues if qq != q for x in queues[qq]):
                        status = "rejected:singlequeue"
                    elif in_queue:
                        status = "rejected:already-in-queue"
                    else:
                        idx = len(entries)
                        while idx > 0 and entries[idx - 1]["time"] > t_sec:
                            idx -= 1
                        entries.insert(idx, {"user": user, "waitdata": e["waitdata"], "time": t_sec,
                                             "mark": 0, "offline": True})
                        batch_last_add[user] = t_sec
                        if t_sec > stored_last_add.get(user, 0):
                            lastadd_updates[user] = max(t_sec, lastadd_updates.get(user, 0))
                        status = "applied"
                        logs.append([str(t_sec), user, "ouadd", room, q, submitter, e["waitdata"]])
                else:   # udel (self) or usdel (staff removing subject)
                    if not in_queue:
                        status = "rejected:not-in-queue"
                    else:
                        queues[q] = [x for x in entries if x["user"] != subject]
                        status = "applied"
                        if act == "udel":
                            logs.append([str(t_sec), user, "oudel", room, q, submitter])
                        else:
                            logs.append([str(t_sec), user, "ousdel", room, q, submitter, subject])
                # These outcomes express the user's intended membership, so they fence older actions
                if status in ("applied", "rejected:not-in-queue", "rejected:already-in-queue"):
                    fence = member_key
            if status == "applied":
                changed = True
            if fence:
                wm[fence] = t_sec
                wm_updates[fence] = t_sec
            outcome[eid] = status
            statuses[nid] = status
            if status != "applied":
                what = act + (":" + e["target"] if e["target"] else "")
                logs.append([str(t_sec), user, "orej", room, q, submitter, what, status])

        def writes(p):
            if processed:
                p.sadd(applied_key, *processed)
                p.expire(applied_key, APPLIED_TTL)
                p.hset(status_key, mapping=statuses)
                p.expire(status_key, APPLIED_TTL)
            if wm_updates:
                p.hset(wm_key, mapping=wm_updates)
            if lastadd_updates:
                p.hset(lastadd_key, mapping=lastadd_updates)
        state["outcome"], state["logs"] = outcome, logs
        return changed, writes

    if candidates:
        m.update_room_atomically(room, mutate, extra_watch=(applied_key, status_key, wm_key, lastadd_key))
        results.update(state.get("outcome", {}))
        log_lines = state.get("logs", [])
    else:
        log_lines = []
    log_lines.sort(key=lambda row: float(row[0]))
    return results, log_lines
