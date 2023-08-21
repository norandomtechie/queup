import os
import json
import re
import sys
import sqlite3
import shutil
from time import time, sleep
try:
    from mod_python import apache, util
    import pyinotify
except:
    pass

ROOM_RGX = r'^[A-Z0-9]{5}$'
QUEUE_RGX = r'^[a-zA-Z0-9\_]{3,15}$'
USER_RGX = r'^[a-z0-9]{2,8}$'
WAITDATA_RGX = r'^[a-zA-Z0-9 \_]{1,50}$'
SUBTITLE_RGX = r'^[a-zA-Z0-9 \_\-]{1,130}$'

class DBConnection:
    def __init__(self, room_db):
        self.conn = sqlite3.connect(room_db)
        self.cur = self.conn.cursor()
    def __enter__(self):
        return [self.conn, self.cur]
    def __exit__(self, type, value, traceback):
        self.conn.commit()
        self.conn.close()

def createroom(room, user):
    if not re.match(ROOM_RGX, room):
        raise Exception("createroom: Room format incorrect: " + room)
    if "room"+room in getrooms():
        raise Exception("createroom: Room already exists. It may be in use.")
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("CREATE TABLE room{0} (owners TEXT, subtitle TEXT, locked INTEGER)".format(room))
    # add current user as an owner
    ownroom(room, user)
    # create default_queue
    createqueue("default_queue", room)
    # lock room by default
    # lockroom(room)

def getroomsubtitle(room):
    if not os.path.exists(room_db):
        raise Exception("getroomsubtitle: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("getroomsubtitle: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        # get the subtitle
        subtitle = list(cur.execute("SELECT subtitle FROM room{0}".format(room)))
        if len(subtitle) == 0:
            return ""
        subtitle = subtitle[0]
        return subtitle[0]

def setroomsubtitle(room, subtitle):
    if not os.path.exists(room_db):
        raise Exception("setroomsubtitle: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("setroomsubtitle: Room format incorrect: " + room)
    if subtitle != '' and not re.match(SUBTITLE_RGX, subtitle):
        raise Exception("setroomsubtitle: Bad subtitle: " + subtitle)
    with DBConnection(room_db) as [conn, cur]:
        # set the subtitle
        cur.execute("UPDATE room{0} SET subtitle = (?)".format(room), (subtitle,))

def lockroom(room):
    if not os.path.exists(room_db):
        raise Exception("lockroom: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("lockroom: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        # set the locked value to 1
        cur.execute("UPDATE room{0} SET locked = 1".format(room))

def unlockroom(room):
    if not os.path.exists(room_db):
        raise Exception("unlockroom: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("unlockroom: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        # set the locked value to 0
        cur.execute("UPDATE room{0} SET locked = 0".format(room))

def isroomlocked(room):
    if not os.path.exists(room_db):
        raise Exception("isroomlocked: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("isroomlocked: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        # get the locked value
        locked = list(cur.execute("SELECT locked FROM room{0}".format(room)))
        if len(locked) == 0:
            return False
        locked = locked[0]
        return locked[0] == 1

def ownroom(room, newusers):
    if not os.path.exists(room_db):
        raise Exception("ownroom: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("ownroom: Room format incorrect: " + room)
    # nothing to do if no new users
    if newusers == "":
        return
    if not all([re.match(USER_RGX, x) for x in newusers.split(",")]):
        raise Exception("ownroom: Bad usernames: " + newusers)
    with DBConnection(room_db) as [conn, cur]:
        # get old users first
        oldusers = getowners(room)
        # remove any repeated users
        allusers = list(set(oldusers + newusers.split(",")))
        if len(oldusers) == 0:
            cur.execute("INSERT INTO room{0} (owners) VALUES (?)".format(room), (",".join(allusers),))
        else:
            # set owners value to str of allusers
            cur.execute("UPDATE room{0} SET owners = (?)".format(room), (",".join(allusers),))
            
def delownroom(room, delusers):
    if not os.path.exists(room_db):
        raise Exception("delownroom: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("delownroom: Room format incorrect: " + room)
    # nothing to do if no new users
    if delusers == "":
        return
    if not all([re.match(USER_RGX, x) for x in delusers.split(",")]):
        raise Exception("delownroom: Bad usernames: " + delusers)
    with DBConnection(room_db) as [conn, cur]:
        # get old users first
        oldusers = getowners(room)
        # remove users that are in delusers
        allusers = [x for x in oldusers if str(x) not in delusers.split(",")]
        if len(allusers) == 0:
            # cur.execute("INSERT INTO room{0} (owners) VALUES (?)".format(room), (",".join(allusers),))
            raise Exception("The room cannot have no owners!")
        else:
            # set owners value to str of allusers
            cur.execute("UPDATE room{0} SET owners = (?)".format(room), (",".join(allusers),))

def getowners(room):
    if not os.path.exists(room_db):
        return []
    if not re.match(ROOM_RGX, room):
        raise Exception("getowners: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        allusers = list(cur.execute("SELECT owners FROM room{0}".format(room)))
        if len(allusers) == 0:
            return []
        allusers = allusers[0]
        allusers = allusers[0].split(",")
        return allusers

def deleteroom(room):
    if not os.path.exists(room_db):
        raise Exception("deleteroom: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("deleteroom: Room format incorrect: " + room)
    # find queues associated with room
    queues = getqueues(room)
    # delete all queues associated with room
    for queue in queues:
        deletequeue(queue, room)
    # delete the room from the db (IMPORTANT as it triggers sseupdate to close client-side)
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("DROP TABLE room{0}".format(room))
    # finally, delete the room database file
    os.remove(room_db)

def createqueue(queue, room):
    if not os.path.exists(room_db):
        raise Exception("createqueue: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    with DBConnection(room_db) as [conn, cur]:
        if "room" + room not in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            raise Exception("The room {0} did not exist.".format(room))
        if not re.match(QUEUE_RGX, queue):
            raise Exception("createqueue: Bad queue name: " + queue)
        # ensure queue table does not exist already
        # makes sure that anyone on there is no longer there
        if "room" + room + "_queue" + queue in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            deletequeue(queue, room)
        # create the queue table
        cur.execute("CREATE TABLE room{0}_queue{1} (username TEXT KEY, time REAL, data TEXT, marked INTEGER)".format(room, queue))
        return True

def renamequeue(oldqueue, newqueue, room):
    if not os.path.exists(room_db):
        raise Exception("renamequeue: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    with DBConnection(room_db) as [conn, cur]:
        # ensure queue table exists
        if "room" + room + "_queue" + oldqueue not in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            raise Exception("renamequeue: Queue {0} did not exist.".format(oldqueue))
        if not re.match(ROOM_RGX, room):
            raise Exception("renamequeue: Room format incorrect: " + room)
        if not re.match(QUEUE_RGX, newqueue):
            raise Exception("renamequeue: Bad queue name: " + newqueue)
        # create the queue table
        cur.execute("ALTER TABLE room{0}_queue{1} RENAME TO room{0}_queue{2}".format(room, oldqueue, newqueue))
        return True

def deletequeue(queue, room):
    if not os.path.exists(room_db):
        raise Exception("deletequeue: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    if not re.match(ROOM_RGX, room):
        raise Exception("deletequeue: Room format incorrect: " + room)
    elif not re.match(QUEUE_RGX, queue):
        raise Exception("deletequeue: Bad queue name: " + queue)
    with DBConnection(room_db) as [conn, cur]:
        # delete the queue table if it exists
        if "room" + room + "_queue" + queue in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            cur.execute("DROP TABLE room{0}_queue{1}".format(room, queue))

def addquser(user, waitdata, queue, room):
    if not os.path.exists(room_db):
        raise Exception("addquser: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
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
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("INSERT INTO room{0}_queue{1} (username, time, data, marked) VALUES (?, ?, ?, ?)".format(room, queue), (user, time(), waitdata, '0'))

def delquser(user, queue, room):
    if not os.path.exists(room_db):
        raise Exception("delquser: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
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
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("DELETE FROM room{0}_queue{1} WHERE username == ?".format(room, queue), (user,))

def getrooms():
    if not os.path.exists(room_db):
        return []
    with DBConnection(room_db) as [conn, cur]:
        # get all rooms, then get all room data
        with conn:
            roomnames = [str(row[0]) for row in cur.execute('SELECT name FROM sqlite_master') if row[0].startswith("room") and "queue" not in row[0]]
        return roomnames

def getqueues(room):
    if not os.path.exists(room_db):
        raise Exception("getqueues: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    with DBConnection(room_db) as [conn, cur]:
        if room == "":
            return [row[0] for row in cur.execute("SELECT name FROM sqlite_master") if re.match(r'room[A-Z0-9]{5}_queue.*', row[0])]
        else:
            # get all queues in room
            return [str(row[0].replace("room{0}_queue".format(room), "")) for row in cur.execute("SELECT name FROM sqlite_master") if row[0].startswith("room{0}_queue".format(room))]

def getusers(queue, room):
    if not os.path.exists(room_db):
        raise Exception("getusers: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
    with DBConnection(room_db) as [conn, cur]:
        # we don't need to check if room == "" because getqueues does that for us
        if queue == "":
            queues = sorted(getqueues(room))
            all_users = {}
            for _q in queues:
                if room == "":
                    r = _q.split("_")[0].replace("room", "")
                else:
                    r = room
                q = _q
                if r not in all_users:
                    all_users[r] = {}
                all_users[r][q] = sorted([row for row in cur.execute("SELECT username, time, data, marked FROM room{0}_queue{1}".format(r, q))])
            return all_users
        else:
            # get all users in queue
            return sorted([row for row in cur.execute("SELECT username, time, data, marked FROM room{0}_queue{1}".format(room, queue))])

def togglemark(user, queue, room):
    if not os.path.exists(room_db):
        raise Exception("togglemark: " + room_db.split("/")[-1].replace(".db", "") + " does not exist.")
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
    with DBConnection(room_db) as [conn, cur]:
        # get the marked value
        marked = list(cur.execute("SELECT marked FROM room{0}_queue{1} WHERE username == ?".format(room, queue), (user,)))
        # if marked is empty, then user is not in queue
        if len(marked) == 0:
            raise Exception("togglemark: User {0} not in queue {1} in room {2}".format(user, queue, room))
        marked = marked[0]
        # toggle the marked value
        cur.execute("UPDATE room{0}_queue{1} SET marked = ? WHERE username == ?".format(room, queue), (1 - int(marked[0]), user))
        return True

