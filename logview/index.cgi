#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""QueUp logview: /queup/logview/

  (no query)           overview of every room (aggregate stats, visible to any logged-in
                       user); only rooms you own are links / selectable for compare
  ?room=ROOM1          single-room analysis (room owners, section 0, and admins)
  ?api=rooms           JSON overview of every room, each flagged `owned` for this user
  ?api=room&room=X     JSON analysis of one room (owners/admins only)
  ?api=compare&rooms=A,B,C   JSON array of room analyses (owners/admins, rooms you can view)
  &cap=4               stale cap in hours (an open queue entry older than this
                       is treated as forgotten and excluded from statistics)

Admins (listed in <private>/logview_admins) can see every room.  Everything this needs lives in this folder (index.cgi, app.html, lvlib/).  It
reads the room logs that roomd.wsgi writes and, for owner checks, reuses
queup/lib (same as queup/admin/index.cgi).  Parsed results are cached in
<private>/queup/logview_cache/ (falls back to the system temp dir).
"""
import json
import os
import re
import sys
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.append(os.path.dirname(HERE))  # the QueUp tree this logview belongs to
from lvlib import analyze  # noqa: E402

ADMINS = set()  # filled from <private>/logview_admins once the private dir is known
ROOM_RGX = r'^[A-Z0-9]{5}$'
DEV = os.environ.get("QUEUP_LV_DEV") == "1"   # local testing only


def out(body, ctype="text/html", status="200 OK"):
    extra = ""
    if user in ADMINS and ctype == "application/json":  # cache diagnostics for admins
        cs = analyze.CACHE_STATS
        extra = "X-Logview-Cache: hit=%d fresh=%d miss=%d dir=%s\r\n" % (
            cs["hit"], cs["fresh"], cs["miss"], cs["dir"])
    sys.stdout.write("Status: %s\r\nContent-Type: %s; charset=utf-8\r\n%s"
                     "Cache-Control: no-store\r\n\r\n" % (status, ctype, extra))
    sys.stdout.flush()
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.exit(0)


def fail(status, msg):
    out("<h3>%s</h3>" % msg.replace("<", "&lt;"), status=status)


def json_fail(status, msg):
    out(json.dumps({"error": msg}), "application/json", status)


# --- paths ------------------------------------------------------------------------
def private_dir():
    if os.environ.get("QUEUP_LV_PRIVATE"):
        return os.environ["QUEUP_LV_PRIVATE"]
    # the private dir of the tree serving this request (queup, queup-dev, ...)
    from lib.privatepath import resolve_private_path
    return resolve_private_path(os.environ)


def load_admins(priv):
    """Usernames allowed to see every room, one per line in <private>/logview_admins."""
    try:
        with open(os.path.join(priv, "logview_admins")) as f:
            return {line.strip().lower() for line in f if line.strip() and not line.startswith("#")}
    except OSError:
        return set()


def log_dir(priv):
    return os.environ.get("QUEUP_LV_LOGDIR") or os.path.join(priv, "logs")


# --- authorization ------------------------------------------------------------------
def owns_room(user, room):
    if DEV:
        return room in os.environ.get("QUEUP_LV_OWNED", "").split(",")
    try:
        import lib.methods
        lib.methods.set_private_path(priv)  # this tree's Redis, not the import-time default
        if not lib.methods.rds.get("room" + room):
            return False
        return user in lib.methods.getowners(room)  # includes section "0" users and menon18
    except Exception:
        return False


def can_view(user, room, logdir):
    if not re.match(ROOM_RGX, room):
        return False
    if user in ADMINS:
        return os.path.exists(os.path.join(logdir, room + ".log"))
    return owns_room(user, room) and os.path.exists(os.path.join(logdir, room + ".log"))


# --- request ---------------------------------------------------------------------
user = os.environ.get("REMOTE_USER", "").lower()
if DEV and os.environ.get("QUEUP_LV_USER"):
    user = os.environ["QUEUP_LV_USER"]
if not user:
    fail("401 Unauthorized", "Not logged in.")
if "@" in user:
    fail("400 Bad Request", "Please log in with your username, not your email.  "
                            "You may need to clear site settings and try again.")

q = {k: v[0] for k, v in parse_qs(os.environ.get("QUERY_STRING", "")).items()}
room = q.get("room", "").upper()
try:
    cap = int(q.get("cap", analyze.DEFAULT_CAP_HOURS))
except ValueError:
    cap = analyze.DEFAULT_CAP_HOURS
cap = min(max(cap, 1), 24 * 30)

try:
    priv = private_dir()
except Exception:
    fail("500 Internal Server Error", "Could not locate the QueUp private directory.")
ADMINS.update(load_admins(priv))
admin = user in ADMINS
logdir = log_dir(priv)
api = q.get("api")

if api:
    try:
        if api == "rooms":
            rooms = analyze.rooms_overview(logdir, cap, priv)
            for rm in rooms:  # only rooms you own may be opened or compared
                rm["owned"] = admin or owns_room(user, rm["room"])
            out(json.dumps(rooms, separators=(",", ":")), "application/json")
        elif api == "room":
            if not can_view(user, room, logdir):
                json_fail("403 Forbidden", "You are not authorized to view this room.")
            out(analyze.room_json(logdir, room, cap, priv), "application/json")
        elif api == "compare":
            rooms = [r.upper() for r in q.get("rooms", "").split(",") if r][:8]
            if not rooms or not all(can_view(user, r, logdir) for r in rooms):
                json_fail("403 Forbidden", "Choose 1-8 rooms that you own.")
            out("[" + ",".join(analyze.room_json(logdir, r, cap, priv) for r in rooms) + "]",
                "application/json")
        else:
            json_fail("400 Bad Request", "Unknown api.")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("logview error: %r\n" % (e,))
        json_fail("500 Internal Server Error", "Analysis failed; see the server error log.")

# --- HTML page ------------------------------------------------------------------
if room and not can_view(user, room, logdir):
    fail("403 Forbidden", "You are not an owner of this room (or it has no log).")

with open(os.path.join(HERE, "app.html"), encoding="utf-8") as f:
    page = f.read()
config = json.dumps({"user": user, "admin": admin, "room": room, "cap": cap,
                     "defaultCap": analyze.DEFAULT_CAP_HOURS}).replace("</", "<\\/")
out(page.replace("/*LV_CONFIG*/", "window.LV = " + config + ";"))
