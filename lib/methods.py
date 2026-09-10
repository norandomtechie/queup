import os
import re
import sys
from time import time
import json
import redis
from datetime import timedelta
import uuid 

import pwd
import subprocess

# path to private directory for logs and db files
try:
    private = '/web/groups/' + os.environ['USER'] + '/private/queup/'
except:
    private = os.environ['CONTEXT_DOCUMENT_ROOT'].replace('public_html', 'private') + 'queup/'

# redis client object
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
        os.path.delete(private + "sections/" + room + ".json")

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

def addquser(user, waitdata, queue, room):
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
    # insert the user into the queue array
    rds_room = json.loads(rds.get("room"+room))
    if queue not in rds_room["queues"]:
        raise Exception("addquser: Queue {0} does not exist.".format(queue))
    # check if user is already in queue
    if user in rds_room["queues"][queue]:
        raise Exception("addquser: User {0} is already in queue {1} in room {2}".format(user, queue, room))
    # add user to queue
    rds_room["queues"][queue].append({"user": user, "waitdata": waitdata, "time": time(), "mark": 0})
    rds.set("room"+room, json.dumps(rds_room))

def delquser(user, queue, room):
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
    # remove the user from the queue array
    rds_room = json.loads(rds.get("room"+room))
    if queue not in rds_room["queues"]:
        raise Exception("delquser: Queue {0} does not exist.".format(queue))
    # remove user from queue by checking against user (fails silently if user not in queue)
    rds_room["queues"][queue] = [x for x in rds_room["queues"][queue] if x["user"] != user]
    rds.set("room"+room, json.dumps(rds_room))

def getqueues(room):
    if not rds.exists("room"+room):
        raise Exception("getqueues: " + room + " does not exist.")
    rds_room = json.loads(rds.get("room"+room))
    return list(rds_room["queues"].keys())

def getlastadd(room, username):
    if not rds.exists(private + "room.log"):
        return 0    # no one could have added themselves to this queue at UNIX epoch!
    last_add_time = 0
    rds_room = json.loads(rds.get("room"+room))
    for queue in rds_room["queues"]:
        for user in rds_room["queues"][queue]:
            if user["user"] == username and user["time"] > last_add_time:
                last_add_time = user["time"]
    return last_add_time

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

def togglemark(user, queue, room):
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
    rds_room = json.loads(rds.get("room"+room))
    if queue not in rds_room["queues"]:
        raise Exception("togglemark: Queue {0} does not exist in room {1}".format(queue, room))
    marked = [x["mark"] for x in rds_room["queues"][queue] if x["user"] == user]
    if len(marked) == 0:
        raise Exception("togglemark: User {0} not in queue {1} in room {2}".format(user, queue, room))
    marked = marked[0]
    element = [x for x in rds_room["queues"][queue] if x["user"] == user][0]
    idx = rds_room["queues"][queue].index(element)
    rds_room["queues"][queue][idx]["mark"] = 1 - marked
    rds.set("room"+room, json.dumps(rds_room))
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
