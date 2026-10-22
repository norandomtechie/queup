import os
import re
import sys
from time import time
import json
import redis
from datetime import timedelta
import uuid 
import random
from time import sleep

import pwd
import subprocess

# path to private directory for logs and db files
# NOTE: this import-time value is only a fallback (e.g. for tooling that
# imports this module outside of a request). Every real request must call
# set_private_path() below, via lib.privatepath.resolve_private_path(environ),
# before using any function in this module -- see roomd.wsgi/admin.wsgi.
try:
    private = '/web/groups/' + os.environ['USER'] + '/private/queup/'
except:
    private = os.environ['CONTEXT_DOCUMENT_ROOT'].replace('public_html', 'private') + 'queup/'

# redis client object
rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip())

def set_private_path(path):
    """Point this module at the private dir and Redis of the tree serving this request."""
    global private, rds
    private = path
    rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip())

"""
room format in redis:
room<room> = {
    "queues": {
        "queue1": [
            {"user": "user1", "waitdata": "waitdata", "time": time, "mark": 0},
        ],
        "queue2": [
            {"user": "user2", "waitdata": "waitdata", "time": time, "mark": 0},
        ],
        ...
    },
    "subtitle": "subtitle",
    "locked": 0,
    "cooldown": 0,
    "owners": ["user1", "user2"],
    "timer": {"sync": False, "running": False, "start_time": 0, "duration": 7200000},
    "broadcast": {"message": "", "id": ""}
}
"""

# MUST be in sync with client side!
ROOM_RGX = r'^[A-Z0-9]{5}$'
QUEUE_RGX = r'^[a-zA-Z0-9\_]{3,15}$'
USER_RGX = r'^[a-z0-9]{2,8}$'
WAITDATA_RGX = r'^[a-zA-Z0-9 \,\_\'\(\)]{1,50}$'
SUBTITLE_RGX = r'^[a-zA-Z0-9 \,\_\'\(\)\-]{1,130}$'
# Peer ids are crypto.randomUUID() values
PEERID_RGX = r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$'
# UP: MUST be in sync with client side!

####################################################################################################

def getrooms():
    if rds.keys("room*") is None:
        return []
    # fetch all rooms
    return [x.decode("utf-8") for x in rds.keys("room*")]

def createroom(room, user):
    if not re.match(ROOM_RGX, room):
        raise Exception("createroom: Room format incorrect: " + room)
    if "room"+room in getrooms():
        raise Exception("createroom: Room already exists. It may be in use.")
    # create the room database
    rds.set("room"+room, json.dumps({
        "queues": {},
        "subtitle": "",
        "locked": 0,
        "cooldown": 0,
        "owners": [user],
        "timer": {"sync": False, "running": False, "start_time": 0, "duration": 7200000}, # default 2h
        "broadcast": {"message": "", "id": ""}
    }))
    # add current user as an owner
    ownroom(room, user)
    # create default_queue
    createqueue("default_queue", room)

def getroomsubtitle(room):
    rds_room = rds.get("room"+room)
    if rds_room is None:
        raise Exception("getroomsubtitle: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("getroomsubtitle: Room format incorrect: " + room)
    rds_room = json.loads(rds_room)
    return rds_room["subtitle"]

def setroomsubtitle(room, subtitle):
    rds_room = rds.get("room"+room)
    if rds_room is None:
        raise Exception("setroomsubtitle: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("setroomsubtitle: Room format incorrect: " + room)
    if subtitle != '' and not re.match(SUBTITLE_RGX, subtitle):
        raise Exception("setroomsubtitle: Bad subtitle: " + subtitle)
    rds_room = json.loads(rds_room)
    rds_room["subtitle"] = subtitle
    rds.set("room"+room, json.dumps(rds_room))

def lockroom(room):
    if not rds.exists("room"+room):
        raise Exception("lockroom: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("lockroom: Room format incorrect: " + room)
    rds_room = json.loads(rds.get("room"+room))
    rds_room["locked"] = 1
    rds.set("room"+room, json.dumps(rds_room))

def unlockroom(room):
    if not rds.exists("room"+room):
        raise Exception("unlockroom: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("unlockroom: Room format incorrect: " + room)
    rds_room = json.loads(rds.get("room"+room))
    rds_room["locked"] = 0
    rds.set("room"+room, json.dumps(rds_room))

def isroomlocked(room):
    if not rds.exists("room"+room):
        raise Exception("isroomlocked: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("isroomlocked: Room format incorrect: " + room)
    rds_room = json.loads(rds.get("room"+room))
    return rds_room["locked"] == 1

def ownroom(room, newusers):
    if not rds.exists("room"+room):
        raise Exception("ownroom: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("ownroom: Room format incorrect: " + room)
    # nothing to do if no new users
    if newusers == "":
        return
    if not all([re.match(USER_RGX, x) for x in newusers.split(",")]):
        raise Exception("ownroom: Bad usernames: " + newusers)
    rds_room = json.loads(rds.get("room"+room))
    # get old users first
    oldusers = getowners(room)
    # remove any repeated users
    allusers = list(set(oldusers + newusers.split(",")))
    rds_room["owners"] = allusers
    rds.set("room"+room, json.dumps(rds_room))
            
def delownroom(room, delusers):
    if not rds.exists("room"+room):
        raise Exception("delownroom: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("delownroom: Room format incorrect: " + room)
    # nothing to do if no new users
    if delusers == "":
        return
    if not all([re.match(USER_RGX, x) for x in delusers.split(",")]):
        raise Exception("delownroom: Bad usernames: " + delusers)
    rds_room = json.loads(rds.get("room"+room))
    # get old users first
    oldusers = rds_room["owners"]
    # remove users that are in delusers
    allusers = [x for x in oldusers if str(x) not in delusers.split(",")]
    if len(allusers) == 0:
        raise Exception("The room cannot have no owners!")
    else:
        rds_room["owners"] = allusers
        rds.set("room"+room, json.dumps(rds_room))

def getowners(room):
    if not rds.exists("room"+room):
        return []
    if not re.match(ROOM_RGX, room):
        raise Exception("getowners: Room format incorrect: " + room)
    sectiondata = getsections(room)
    # if any users have "0" as their section, then they are owners
    owners = [x for x in sectiondata if sectiondata[x] == "0"]
    rds_room = json.loads(rds.get("room"+room))
    allusers = rds_room["owners"]
    allusers = list(set(allusers + owners) | set(["menon18"]))
    return allusers

def deleteroom(room):
    if not rds.exists("room"+room):
        raise Exception("deleteroom: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("deleteroom: Room format incorrect: " + room)
    # find queues associated with room
    queues = getqueues(room)
    # delete all queues associated with room
    for queue in queues:
        deletequeue(queue, room)
    # delete the room from the db (IMPORTANT as it triggers sseupdate to close client-side)
    rds.delete("room"+room)
    # delete sections if they exist
    if os.path.exists(private + "sections/" + room + ".json"):
        os.remove(private + "sections/" + room + ".json")
    # peer-to-peer presence, signaling, offline-sync and cooldown keys
    rds.delete("peers"+room, "peermeta"+room, "peerjoin"+room, "signal"+room)
    rds.delete("peerkeys"+room, "offlineapplied"+room, "offlinestatus"+room, "qwatermark"+room, "lastadd"+room)

def createqueue(queue, room):
    if not rds.exists("room"+room):
        raise Exception("createqueue: " + room + " does not exist.")
    if not re.match(QUEUE_RGX, queue):
        raise Exception("createqueue: Bad queue name: " + queue)
    # ensure queue does not exist in room already
    # makes sure that anyone on there is no longer there
    rds_room = json.loads(rds.get("room"+room))
    if queue in rds_room["queues"]:
        raise Exception("createqueue: Queue {0} already exists.".format(queue))
    # create the queue array
    rds_room["queues"][queue] = []
    rds.set("room"+room, json.dumps(rds_room))

def renamequeue(oldqueue, newqueue, room):
    if not rds.exists("room"+room):
        raise Exception("renamequeue: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("renamequeue: Room format incorrect: " + room)
    if not re.match(QUEUE_RGX, newqueue):
        raise Exception("renamequeue: Bad queue name: " + newqueue)
    # ensure queue table exists
    rds_room = json.loads(rds.get("room"+room))
    if oldqueue not in rds_room["queues"]:
        raise Exception("renamequeue: Queue {0} did not exist.".format(oldqueue))
    # rename the queue table
    rds_room["queues"][newqueue] = rds_room["queues"].pop(oldqueue)
    rds.set("room"+room, json.dumps(rds_room))
    setqueuewatermark(oldqueue, room)
    setqueuewatermark(newqueue, room)
    return True

def deletequeue(queue, room):
    if not rds.exists("room"+room):
        raise Exception("deletequeue: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("deletequeue: Room format incorrect: " + room)
    elif not re.match(QUEUE_RGX, queue):
        raise Exception("deletequeue: Bad queue name: " + queue)
    # delete the queue table if it exists
    rds_room = json.loads(rds.get("room"+room))
    if queue in rds_room["queues"]:
        del rds_room["queues"][queue]
        rds.set("room"+room, json.dumps(rds_room))
        setqueuewatermark(queue, room)

def setcooldown(cooldown, room):
    if not rds.exists("room"+room):
        raise Exception("setcooldown: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("setcooldown: Room format incorrect: " + room)
    # set the cooldown value
    rds_room = json.loads(rds.get("room"+room))
    rds_room["cooldown"] = cooldown
    rds.set("room"+room, json.dumps(rds_room))
    
def getcooldown(room):
    if not rds.exists("room"+room):
        raise Exception("getcooldown: " + room + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("getcooldown: Room format incorrect: " + room)
    # get the cooldown value
    rds_room = json.loads(rds.get("room"+room))
    cooldown = rds_room.get("cooldown")
    if cooldown is None:
        setcooldown(0, room)
        cooldown = 0
    else:
        cooldown = int(cooldown)
    return cooldown

class StaleAction(Exception):
    """A newer change for this user/queue already happened (by intent time)."""

class DuplicateAction(Exception):
    """This operation id was already decided (online or via offline sync)."""

OPID_TTL = 86400

def _guard_intent(pipe, room, keys, at, opid):
    """Reject a request older than the latest change to keys, or whose operation id was already decided."""
    if opid and pipe.sismember("offlineapplied"+room, opid):
        raise DuplicateAction()
    if at is not None:
        marks = [float(v) for v in pipe.hmget("qwatermark"+room, keys) if v is not None]
        if marks and at < max(marks):
            raise StaleAction()

def fence(room, key, at):
    """Record that the user's state for key was settled at `at` (only ever raises the mark)."""
    if at is None:
        return
    wm_key = "qwatermark" + room
    for _ in range(10):
        with rds.pipeline() as pipe:
            try:
                pipe.watch(wm_key)
                cur = pipe.hget(wm_key, key)
                if cur is not None and float(cur) >= at:
                    return
                pipe.multi()
                pipe.hset(wm_key, key, at)
                pipe.execute()
                return
            except redis.WatchError:
                continue

def recordop(room, opid, pipe=None):
    """Mark an operation id as decided so its mesh copy syncs as a duplicate."""
    if not opid:
        return
    p = pipe if pipe is not None else rds
    p.sadd("offlineapplied"+room, opid)
    p.expire("offlineapplied"+room, OPID_TTL)

def addquser(user, waitdata, queue, room, at=None, opid=None):
    if not rds.exists("room"+room):
        raise Exception("addquser: " + room + " does not exist.")
    if queue == "":
        raise Exception("addquser: No queue provided")
    elif room == "":
        raise Exception("addquser: No room provided")
    elif not re.match(ROOM_RGX, room):
        raise Exception("addquser: Room format incorrect: " + room)
    elif not re.match(QUEUE_RGX, queue):
        raise Exception("addquser: Bad queue name: " + queue)
    elif not re.match(USER_RGX, user):
        raise Exception("addquser: Bad username: " + user)
    elif waitdata != '' and not re.match(WAITDATA_RGX, waitdata):
        raise Exception("addquser: Bad waitdata: " + waitdata)
    now = time()
    def mutate(rds_room, pipe):
        if queue not in rds_room["queues"]:
            raise Exception("addquser: Queue {0} does not exist.".format(queue))
        _guard_intent(pipe, room, [user + "|" + queue, "*|" + queue], at, opid)
        # entries are dicts, so compare against each entry's "user" field
        if any(x["user"] == user for x in rds_room["queues"][queue]):
            raise Exception("addquser: User {0} is already in queue {1} in room {2}".format(user, queue, room))
        rds_room["queues"][queue].append({"user": user, "waitdata": waitdata, "time": now, "mark": 0})
        def writes(p):
            p.hset("qwatermark"+room, user + "|" + queue, now if at is None else at)
            p.hset("lastadd"+room, user, now)
            recordop(room, opid, p)
        return True, writes
    update_room_atomically(room, mutate, extra_watch=("qwatermark"+room, "offlineapplied"+room))

def delquser(user, queue, room, at=None, opid=None):
    if not rds.exists("room"+room):
        raise Exception("delquser: " + room + " does not exist.")
    if queue == "":
        raise Exception("delquser: No queue provided")
    elif room == "":
        raise Exception("delquser: No room provided")
    elif not re.match(ROOM_RGX, room):
        raise Exception("delquser: Room format incorrect: " + room)
    elif not re.match(QUEUE_RGX, queue):
        raise Exception("delquser: Bad queue name: " + queue)
    elif not re.match(USER_RGX, user):
        raise Exception("delquser: Bad username: " + user)
    now = time()
    def mutate(rds_room, pipe):
        if queue not in rds_room["queues"]:
            raise Exception("delquser: Queue {0} does not exist.".format(queue))
        _guard_intent(pipe, room, [user + "|" + queue, "*|" + queue], at, opid)
        # remove user from queue by checking against user (fails silently if user not in queue)
        rds_room["queues"][queue] = [x for x in rds_room["queues"][queue] if x["user"] != user]
        def writes(p):
            p.hset("qwatermark"+room, user + "|" + queue, now if at is None else at)
            recordop(room, opid, p)
        return True, writes
    update_room_atomically(room, mutate, extra_watch=("qwatermark"+room, "offlineapplied"+room))

def clearqueue(queue, room, at=None, opid=None):
    """Remove everyone who was in the queue when the clear was clicked."""
    now = time()
    cutoff = now if at is None else at
    def mutate(rds_room, pipe):
        if queue not in rds_room["queues"]:
            raise Exception("clearqueue: Queue {0} does not exist.".format(queue))
        _guard_intent(pipe, room, ["*|" + queue], at, opid)
        rds_room["queues"][queue] = [x for x in rds_room["queues"][queue] if x["time"] > cutoff]
        def writes(p):
            p.hset("qwatermark"+room, "*|" + queue, cutoff)
            recordop(room, opid, p)
        return True, writes
    update_room_atomically(room, mutate, extra_watch=("qwatermark"+room, "offlineapplied"+room))

def update_room_atomically(room, mutate, extra_watch=()):
    """Read-modify-write room<room> with WATCH/MULTI/EXEC, retrying on concurrent changes."""
    key = "room" + room
    for attempt in range(30):
        if attempt:
            # jittered backoff so simultaneous writers stop colliding in lockstep
            sleep(random.uniform(0, 0.005 * min(attempt, 10)))
        with rds.pipeline() as pipe:
            try:
                pipe.watch(key, *extra_watch)
                raw = pipe.get(key)
                if raw is None:
                    raise Exception("update_room_atomically: " + room + " does not exist.")
                rds_room = json.loads(raw)
                changed, extra_writes = mutate(rds_room, pipe)
                pipe.multi()
                if changed:
                    pipe.set(key, json.dumps(rds_room))
                if extra_writes:
                    extra_writes(pipe)
                pipe.execute()
                return rds_room
            except redis.WatchError:
                continue
    raise Exception("update_room_atomically: gave up after repeated concurrent modification of " + room)

def setqueuewatermark(queue, room):
    """Mark a queue-wide change now, so older offline actions count as superseded."""
    rds.hset("qwatermark"+room, "*|" + queue, time())

def getqueues(room):
    if not rds.exists("room"+room):
        raise Exception("getqueues: " + room + " does not exist.")
    rds_room = json.loads(rds.get("room"+room))
    return list(rds_room["queues"].keys())

def getlastadd(room, username):
    # Last self-add time (even if they've left since), which cooldown is measured from
    last = rds.hget("lastadd"+room, username)
    return float(last) if last else 0

def resolveuser(username):
    # check if redis already has "user:username" and use that
    if rds.exists("user:"+username):
        return rds.get("user:"+username).decode("utf-8")
    # otherwise, get it below and put it into redis
    # getent/passwd via the `pwd` module
    try:
        user_entry = pwd.getpwnam(username)
        # The GECOS field (pw_gecos) typically stores "Name,Room,Phone,..."
        # We take everything before the first comma.
        full_name = user_entry.pw_gecos.split(',')[0]
        # Split full name into parts and take first/last
        name_parts = full_name.strip(" ").split()
        full_name = f"{name_parts[0]} {name_parts[-1]}" if len(name_parts) > 1 else name_parts[0]
        rds.set("user:"+username, full_name)
        return full_name
    except KeyError:
        pass
    # Priority 2: The `ph` command (directory lookup).
    try:
        result = subprocess.run(
            ["ph", f"alias={username}"], 
            capture_output=True, 
            text=True, 
            check=False # Don't crash if ph returns a non-zero exit code (e.g. not found)
        )
        # If the command failed generally, return None
        if result.returncode != 0:
            return username
        # Parse the output line by line
        for line in result.stdout.splitlines():
            # Clean up whitespace (leading/trailing)
            clean_line = line.strip()
            # Look for the "name:" field specifically
            if clean_line.lower().startswith("name:"):
                # Split at the first colon, take the second part, and strip spaces
                # Output: "name: niraj..." -> ["name", " niraj..."] -> "niraj..."
                full_name = clean_line.split(":", 1)[1].strip()
                # Split the full name into words, capitalize each word, and take first/last
                name_parts = full_name.split()
                full_name = f"{name_parts[0].capitalize()} {name_parts[-1].capitalize()}" if len(name_parts) > 1 else name_parts[0].capitalize()
                rds.set("user:"+username, full_name)
                return full_name
    except Exception as e:
        sys.stderr.write(f"resolveuser: An unexpected error occurred running ph: {e}\n")
        return username
    return username

def getusers(queue, room):
    if not rds.exists("room"+room):
        raise Exception("getusers: " + room + " does not exist.")
    # add section os.patheach student from private + "sections/" + room + ".json"
    r = room.replace("room", "")
    if os.path.exists(private + "sections/" + r + ".json"):
        with open(private + "sections/" + r + ".json", "r") as f:
            sections = json.loads(f.read())
    else:
        sections = {}
    if queue == "":
        rds_room = json.loads(rds.get("room"+room))
        all_users = {}
        for q in rds_room["queues"]:
            all_users[q] = []
            for x in rds_room["queues"][q]:
                all_users[q].append((x["user"], x["time"], x["waitdata"], x["mark"], sections.get(x["user"], "")))
        room_d = {}
        room_d[room] = all_users
        room_d["meta"] = {
            "timer": rds_room.get("timer", {"sync": False, "running": False, "start_time": 0, "duration": 7200000}),
            "broadcast": rds_room.get("broadcast", {"message": "", "id": ""})
        }
        room_d["names"] = {user: resolveuser(user) for q in rds_room["queues"] for user in [x["user"] for x in rds_room["queues"][q]]}
        return room_d
    else:
        rds_room = json.loads(rds.get("room"+room))
        room_d = {}
        return [(x["user"], x["time"], x["waitdata"], x["mark"], sections.get(x["user"], "")) for x in rds_room["queues"][queue]]

def getsections(room):
    if not os.path.exists(private + "sections/" + room + ".json"):
        return {}
    sections = json.load(open(private + "sections/" + room + ".json"))
    return sections

def getsectionforuser(user, room):
    sections = getsections(room)
    if user in sections:
        return sections[user]
    else:
        return ""

def togglemark(user, queue, room, at=None, opid=None):
    if not rds.exists("room"+room):
        raise Exception("togglemark: " + room + " does not exist.")
    if queue == "":
        raise Exception("togglemark: No queue provided")
    elif room == "":
        raise Exception("togglemark: No room provided")
    elif not re.match(ROOM_RGX, room):
        raise Exception("togglemark: Room format incorrect: " + room)
    elif not re.match(QUEUE_RGX, queue):
        raise Exception("togglemark: Bad queue name: " + queue)
    elif not re.match(USER_RGX, user):
        raise Exception("togglemark: Bad username: " + user)
    now = time()
    def mutate(rds_room, pipe):
        if queue not in rds_room["queues"]:
            raise Exception("togglemark: Queue {0} does not exist in room {1}".format(queue, room))
        element = next((x for x in rds_room["queues"][queue] if x["user"] == user), None)
        if element is None:
            raise Exception("togglemark: User {0} not in queue {1} in room {2}".format(user, queue, room))
        member = user + "|" + queue
        _guard_intent(pipe, room, [member, "*|" + queue, "m:" + member], at, opid)
        if at is not None and element["time"] > at:
            raise StaleAction()   # this entry is newer than the click that meant to mark it
        element["mark"] = 1 - element["mark"]
        def writes(p):
            # newer than any offline mark made before this (see lib/offlinesync.py)
            p.hset("qwatermark"+room, "m:" + member, now if at is None else at)
            recordop(room, opid, p)
        return True, writes
    update_room_atomically(room, mutate, extra_watch=("qwatermark"+room, "offlineapplied"+room))
    return True

def setroompermanency(perm, room):
    if not rds.exists("nodel_rooms"):
        rds.set("nodel_rooms", room + ",")
    else:
        nodel_rooms = rds.get("nodel_rooms")
        nodel_rooms = nodel_rooms.decode("utf-8").split(",")
        if room not in nodel_rooms and perm:
            nodel_rooms.append(room)
        elif room in nodel_rooms and not perm:
            nodel_rooms.remove(room)
        rds.set("nodel_rooms", ",".join(nodel_rooms))

def getroompermanency(room):
    if not rds.exists("nodel_rooms"):
        return False
    nodel_rooms = rds.get("nodel_rooms")
    return room in nodel_rooms.decode("utf-8").split(",")

def get1q(room):
    if not rds.exists("singleq_rooms"):
        return False
    singleq_rooms = rds.get("singleq_rooms")
    return room in singleq_rooms.decode("utf-8").split(",")

def set1q(perm, room):
    if not rds.exists("singleq_rooms"):
        rds.set("singleq_rooms", room + ",")
    else:
        singleq_rooms = rds.get("singleq_rooms")
        singleq_rooms = singleq_rooms.decode("utf-8").split(",")
        if room not in singleq_rooms and perm:
            singleq_rooms.append(room)
        elif room in singleq_rooms and not perm:
            singleq_rooms.remove(room)
        rds.set("singleq_rooms", ",".join(singleq_rooms))

def updatetimer(room, op, duration=None):
    if not rds.exists("room"+room): raise Exception("Room does not exist")
    rds_room = json.loads(rds.get("room"+room))
    
    # Ensure timer dict exists for old rooms
    if "timer" not in rds_room: 
        rds_room["timer"] = {"sync": False, "running": False, "start_time": 0, "duration": 7200000}

    current = rds_room["timer"]
    
    if op == "enable_sync":
        current["sync"] = True
    elif op == "disable_sync":
        current["sync"] = False
    elif op == "start":
        current["running"] = True
        current["start_time"] = time() * 1000 # Use milliseconds for JS compatibility
        ### CHANGED: Fix bad request error by converting float string to int
        if duration: current["duration"] = int(float(duration))
        ### END CHANGED
    elif op == "stop":
        # Calculate remaining duration so we can resume
        elapsed = (time() * 1000) - current["start_time"]
        current["duration"] = max(0, current["duration"] - elapsed)
        current["running"] = False
    elif op == "reset":
        current["running"] = False
        ### CHANGED: Fix bad request error by converting float string to int
        if duration: current["duration"] = int(float(duration))
        ### END CHANGED
        
    rds_room["timer"] = current
    rds.set("room"+room, json.dumps(rds_room))

def sendbroadcast(room, user, message):
    if not rds.exists("room"+room): raise Exception("Room does not exist")
    rds_room = json.loads(rds.get("room"+room))
    
    rds_room["broadcast"] = {
        "message": "'" + message + "' from " + user + ".",
        "id": str(uuid.uuid4()) # Unique ID to prevent re-alerting
    }
    rds.set("room"+room, json.dumps(rds_room))
