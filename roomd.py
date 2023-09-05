#! /usr/bin/env python3
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

# predefined here so handler will set their values
room_db = ""
private = ""

def doexec(path):
    exec(compile(open(path).read(), path, "exec"), globals())

class DBConnection:
    def __init__(self, room_db):
        self.conn = sqlite3.connect(room_db)
        self.cur = self.conn.cursor()
    def __enter__(self):
        return [self.conn, self.cur]
    def __exit__(self, type, value, traceback):
        self.conn.commit()
        self.conn.close()

# MUST be in sync with client side!
ROOM_RGX = r'^[A-Z0-9]{5}$'
QUEUE_RGX = r'^[a-zA-Z0-9\_]{3,15}$'
USER_RGX = r'^[a-z0-9]{2,8}$'
WAITDATA_RGX = r'^[a-zA-Z0-9 \,\_\'\(\)]{1,50}$'
SUBTITLE_RGX = r'^[a-zA-Z0-9 \,\_\'\(\)\-]{1,130}$'

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

def getroompermanency(room):
    if not os.path.exists(private + "nodel_rooms"):
        return False
    with open(private + "nodel_rooms", "r") as f:
        return room in f.read().split("\n")

def acquireLock(path):
    t = 0
    while t < 5 and os.path.exists(path + ".lck"):
        sleep(1)
        t += 1
    if t == 5:
        raise Exception("lockdir " + path + ".lck" + " not removed.")
    try:
        os.mkdir(path + ".lck")
    except:
        raise Exception("Unable to create lockdir")
    return path + ".lck"

def releaseLock(lock):
    if os.path.exists(lock):
        try:
            shutil.rmtree(lock)
        except:
            raise Exception("Unable to remove lockdir")

def lockAndWriteLog(data):
    global private
    lock = acquireLock(private + "room.log")
    with open(private + "room.log", "a+") as f:
        f.write(data + "\n")
    releaseLock(lock)

class Lock:
    def __init__(self, lockdir):
        self.lockdir = lockdir
        self.lock = None
    def __enter__(self):
        self.lock = acquireLock(self.lockdir)
        return self.lock
    def __exit__(self, type, value, traceback):
        releaseLock(self.lock)

# implement a rate limiter class using the token bucket algo 
# that takes the username, checks if they have an entry in 
# ratelimit.db, and if so, checks if they have made more than 
# 3 requests in the last second. if so, return false. otherwise, 
# return true and update the database.

class RateLimiter:
    def __init__(self, dbpath):
        self.dbpath = dbpath
    def __enter__(self):
        create = not os.path.exists(self.dbpath)
        self.conn = sqlite3.connect(self.dbpath)
        self.cur = self.conn.cursor()
        if create:
            with self.conn:
                self.cur.execute("CREATE TABLE ratelimit (username TEXT KEY, time REAL)")
        return [self, self.conn, self.cur]
    def __exit__(self, type, value, traceback):
        self.conn.commit()
        self.conn.close()
    def should_limit(self, username):
        # get the last 5 requests
        last5 = [x[0] for x in self.cur.execute("SELECT time FROM ratelimit WHERE username == (?) ORDER BY time DESC LIMIT 5", (username,))]
        # if there are less than 5 requests, then we're good
        if len(last5) < 5:
            self.cur.execute("INSERT INTO ratelimit (username, time) VALUES (?, ?)", (username, time()))
            return False
        # if the last request was more than a second ago, we're good
        if time() - last5[-1] > 1:
            self.cur.execute("INSERT INTO ratelimit (username, time) VALUES (?, ?)", (username, time()))
            return False
        # otherwise, we're not good
        return True

######################
# Main application.
######################
def handler(req):
    global ip, room_db, private
    class EventHandler(pyinotify.ProcessEvent):
        def __init__(self, roomname):
            self.roomname = roomname
        def process_IN_MODIFY(self, event):
            try:
                req.write("data: %s\n\r" % json.dumps(getusers("", self.roomname)))
            except:
                pass
    
    # grab config based on IP and init all variables
    if 'HOME' not in os.environ:
        os.environ['HOME'] = '/var/www/html'
    try:
        private = os.environ['DOCUMENT_ROOT'].replace("~", "") + os.environ['CONTEXT_PREFIX'].replace("~", "") + '/private/queup/'
    except KeyError:
        # os.environ['DOCUMENT_ROOT'] is not set by CGI resulting in a exception
        # so we are being invoked by mod_python, so we need different env vars
        private = os.environ['HOME'] + '/private/queup/'
    
    # initialize some variables
    user = req.user
    if user == "":
        return apache.HTTP_UNAUTHORIZED
    
    # check rate limit for this user
    with RateLimiter(private + "ratelimit.db") as [rl, conn, cur]:
        if rl.should_limit(user):
            return apache.HTTP_PRECONDITION_FAILED   # why I can't do IM_A_TEAPOT or TOO_MANY_REQUESTS is beyond science
    
    query = util.FieldStorage(req)
    ip = req.useragent_ip
    
    room = query.get('room', '').decode('utf-8')
    if len(room) != 5 or not re.search(r"[A-Z0-9]{5}", room):
        req.log_error("Invalid room name: " + room + "\n")
        return apache.HTTP_BAD_REQUEST 
    action = query.get('action', '').decode('utf-8')
    actions = ['add', 'del', 'chk', 'ren', 'own', 'delown', 'setsub', 'lock', 'unlock', 'clear', 'mark']
    if 'sseupdate' not in query and not (action in actions):
        req.log_error("Invalid action: " + action + "\n")
        return apache.HTTP_BAD_REQUEST
    setup = query.get('setup', '').decode('utf-8')
    roomsetup = (setup != '') and (action != '') and (room != '') and 'queue' not in query
    queuesetup = (setup != '') and (action != '') and (room != '') and 'queue' in query
    querychecked = setup == '' and (action != '') and (room != '') and 'queue' in query

    # do we have a room to access? then we must be either its owner or we must 
    # be chk, sseupdate, or adding/deleting ourselves from a queue (no setup)
    room_db = private + "rooms/" + room + ".db"
    
    # is user owner? set to true if room doesn't exist, must be owner to create room
    if os.path.exists(room_db):
        try:
            is_owner = user in getowners(room)
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                # file exists but table does not, seems like a mistake.
                # remove it and recreate.
                os.remove(room_db)
        conditions_for_access_nodb = [
            action in ['chk'],
            'sseupdate' in query,
            (action in ['add', 'del'] and querychecked)
        ]
        # if db exists, but we're not add/del to a queue, and we're not owner,
        # then bad request.
        if not any(conditions_for_access_nodb) and not is_owner:
            req.log_error("FailedReqCheck: user not in getowners(room) and roomsetup. getowners: " + str(getowners(room)))
            return apache.HTTP_BAD_REQUEST
    elif roomsetup and action == 'add':
        # db does not exist and roomsetup+add means we are creating a room
        is_owner = True
    else:   # so no db exists, and we're not creating one. what's the point?
        req.log_error("Malformed request while setting is_owner. Query was %s\r\n" % query)
        return apache.HTTP_BAD_REQUEST
    
    newusers = query.get('newusers', '').decode('utf-8').strip()
    subtitle = query.get('subtitle', '').decode('utf-8').strip()
    
    # now check our variables
    rooms = getrooms()
    
    # this section handles adding and removing in a room
    if roomsetup:
        # check actions based on whether adding/deleting
        will_add     = action == 'add'  # room should not be in the database already
        will_del     = action == 'del' and "room"+room in rooms # room was in the database
        will_chk     = action == 'chk' and "room"+room in rooms # room was in the database
        will_own     = action == 'own' and "room"+room in rooms # room was in the database
        will_own     = will_own and (newusers == "" or all([re.match(USER_RGX, x) for x in newusers.split(",")]))
        will_delown  = action == 'delown' and "room"+room in rooms # room was in the database
        will_delown  = will_delown and (newusers == "" or all([re.match(USER_RGX, x) for x in newusers.split(",")]))
        will_setsub  = action == 'setsub' and "room"+room in rooms # room was in the database
        will_setsub  = will_setsub and (subtitle == "" or re.match(SUBTITLE_RGX, subtitle))
        will_tgllock = action in ['lock', 'unlock'] and "room"+room in rooms # room was in the database
        # perform the action
        if will_add:
            try:
                createroom(room, user)
            except Exception as e:
                req.log_error("Error creating room %s by owner %s from %s\r\n" % (room, user, ip))
                req.write(str(e))
                return apache.OK
            lockAndWriteLog(",".join([str(time()), user, room, "create"]))
            rooms = getrooms()
            userdata = getusers("", room)
            userdata["is-owner"] = is_owner
            userdata["subtitle"] = ""
            userdata["is-locked"] = False
            # it is possible to define a room first before creating it, so check permanency anyway
            userdata["is-permanent"] = getroompermanency(room)
            req.write(json.dumps(userdata))
            return apache.OK
        elif will_chk:
            if "room"+room not in rooms:
                req.log_error("Room %s not found in database. May be misconfigured." % room)
                return apache.HTTP_BAD_REQUEST
            userdata = getusers("", room)
            userdata["is-owner"] = is_owner
            userdata["subtitle"] = getroomsubtitle(room)
            userdata["is-locked"] = isroomlocked(room)
            userdata["is-permanent"] = getroompermanency(room)
            req.write(json.dumps(userdata))
            return apache.OK
        else:
            if not is_owner:
                req.log_error("roomsetup: User %s is not an owner of room %s. Query was %s\r\n" % (user, room, query))
                return apache.HTTP_UNAUTHORIZED
            try:
                if will_del:
                    if room in open(private + "nodel_rooms").read():
                        return apache.HTTP_BAD_REQUEST
                    deleteroom(room)
                    lockAndWriteLog(",".join([str(time()), user, room, "remover"]))
                    req.write(json.dumps({"status": "success"}))
                    return apache.OK
                elif will_own:
                    ownroom(room, newusers)
                    lockAndWriteLog(",".join([str(time()), user, room, "own", newusers]))
                    req.write(json.dumps(getowners(room)))
                    return apache.OK
                elif will_delown:
                    delownroom(room, newusers)
                    lockAndWriteLog(",".join([str(time()), user, room, "delown", newusers]))
                    req.write(json.dumps(getowners(room)))
                    return apache.OK
                elif will_setsub:
                    setroomsubtitle(room, subtitle)
                    lockAndWriteLog(",".join([str(time()), user, room, "setsub", subtitle]))
                    req.write(json.dumps({"status": "success"}))
                    return apache.OK
                elif will_tgllock:
                    if action == 'unlock':
                        unlockroom(room)
                        lockAndWriteLog(",".join([str(time()), user, room, "unlock"]))
                    else:
                        lockroom(room)
                        lockAndWriteLog(",".join([str(time()), user, room, "lock"]))
                    req.write(json.dumps({"status": "success"}))
                    return apache.OK
                else:
                    req.log_error("No valid query " + str(query) + "\n")
                    return apache.HTTP_BAD_REQUEST
            except sqlite3.IntegrityError:  # our fault
                req.log_error("IntegrityError running action %s on room %s by owner %s from %s. Query was %s\r\n" % (action, room, user, ip, query))
                return apache.HTTP_INTERNAL_SERVER_ERROR
            except: # their fault
                req.log_error("Unrecognized error running action %s on room %s by user %s from %s. Query was %s\r\n" % (action, room, user, ip, query))
                return apache.HTTP_BAD_REQUEST
    elif queuesetup:
        if not is_owner:
            req.log_error("queuesetup: User %s is not an owner of room %s. Query was %s\r\n" % (user, room, query))
            return apache.HTTP_UNAUTHORIZED
        room = query.get('room', '').encode('ascii').strip()
        queue = query.get('queue', '').encode('ascii').strip()
        if len(room) != 5 or not re.search(ROOM_RGX, room):
            req.log_error("InvalidRoomError: running action %s on room %s by owner %s from %s. Query was %s\r\n" % (action, room, user, ip, query))
            return apache.HTTP_BAD_REQUEST
        if not re.search(QUEUE_RGX, queue):
            req.log_error("InvalidQueueError: running action %s on room %s by owner %s from %s. Query was %s\r\n" % (action, room, user, ip, query))
            return apache.HTTP_BAD_REQUEST
        queue = query.get('queue', '').encode('ascii').strip()
        newqueue = query.get('newqueue', '').decode('utf-8').strip()
        username = query.get('username', '').decode('utf-8').strip()
        # check actions based on whether adding/deleting/checking/renaming
        will_add = query.get('action', None).decode('utf-8').strip() == 'add'
        will_del = query.get('action', None).decode('utf-8').strip() == 'del' and "room"+room in rooms # room was in the database
        will_del = will_del and queue in getqueues(room) # queue was in the database
        will_chk = query.get('action', None).decode('utf-8').strip() == 'chk' and "room"+room in rooms # room was in the database
        will_chk = will_chk and queue in getqueues(room) # queue was in the database
        will_ren = query.get('action', None).decode('utf-8').strip() == 'ren' and "room"+room in rooms # room was in the database
        will_ren = will_ren and queue in getqueues(room) # queue was in the database
        will_ren = will_ren and re.match(QUEUE_RGX, newqueue) and newqueue not in getqueues(room) # new queue must not already exist and newqueue != ''
        will_clear = query.get('action', None).decode('utf-8').strip() == 'clear' and "room"+room in rooms # room was in the database
        will_clear = will_clear and queue in getqueues(room) # queue was in the database
        will_mark = query.get('action', None).decode('utf-8').strip() == 'mark' and "room"+room in rooms # room was in the database
        will_mark = will_mark and queue in getqueues(room) and any([username == x[0] for x in getusers(queue, room)])   # queue was in the database and user is in the queue
        # perform the action
        if will_add or will_del or will_ren or will_clear or will_mark:
            try:
                if will_add:
                    try:
                        createqueue(queue, room)
                    except sqlite3.OperationalError as e:
                        if "already exists" in str(e):
                            pass
                    lockAndWriteLog(",".join([str(time()), user, room, "createq"]))
                    req.write(json.dumps(getusers("", room)))
                elif will_del:
                    deletequeue(queue, room)
                    lockAndWriteLog(",".join([str(time()), user, room, "removeq"]))
                elif will_ren:
                    renamequeue(queue, newqueue, room)
                    lockAndWriteLog(",".join([str(time()), user, room, "removeq"]))
                    req.write(json.dumps(getusers("", room)))
                elif will_clear:
                    # get all users and remove them from the queue
                    for u in getusers(queue, room):
                        delquser(u[0], queue, room)
                    req.write(json.dumps(getusers("", room)))
                elif will_mark:
                    # toggle mark on user
                    togglemark(username, queue, room)
                    req.write(json.dumps(getusers("", room)))
                else:
                    req.log_error("No valid under queuesetup query " + str(query) + "\n")
                    return apache.HTTP_BAD_REQUEST
                return apache.OK
            except sqlite3.IntegrityError: 
                req.log_error("IntegrityError: Error adding/removing room %s by owner %s from %s\r\n" % (room, user, ip))
                return apache.HTTP_INTERNAL_SERVER_ERROR
            except Exception as e:
                req.log_error("Error adding/removing/renaming queue %s in room %s by owner %s from %s\r\n" % (queue, room, user, ip))
                req.log_error(str(e))
                return apache.OK
        elif will_chk:
            req.write(json.dumps(getusers("", room)))
            return apache.OK
        else:
            sys.stderr.write("Invalid action: " + str(query.get('action', None).decode('utf-8').strip()) + "\n")
            sys.stderr.write("query string: " + str(query) + "\n")
            sys.stderr.flush()
            return apache.HTTP_BAD_REQUEST
    elif querychecked:
        room = query.get('room', '').encode('ascii').strip()
        queue = query.get('queue', '').encode('ascii').strip()
        username = query.get('username', '').encode('ascii').strip()
        if room == '' or "room"+room not in rooms or queue == '' or queue not in getqueues(room):
            sys.stderr.write("Invalid room/queue name: " + str(room) + "/" + str(queue) + "," + str(rooms) + "," + str(getqueues(room)) + "\n")
            sys.stderr.flush()
            return apache.HTTP_BAD_REQUEST
        # room name should already be in database
        db_queue = getusers(queue, room)
        # check if room is locked before adding anyone unless we are owner
        # perform actions based on whether adding/deleting (will_del does not include owner deleting users)
        will_add = action == 'add' and not any([user == x[0] for x in db_queue]) # username should not be in the room already
        will_del = action == 'del' and any([user == x[0] for x in db_queue]) and (username == '') # username was in the room and is not someone else or empty
        staff_del = is_owner and action == 'del' and username is not None and any([username == x[0] for x in db_queue]) # username was in the room
        if isroomlocked(room) and not is_owner and will_add:
            req.log_error("Room %s is locked. Query was %s\r\n" % (room, query))
            return apache.HTTP_LOCKED
        # only make changes to database if changes are to be made
        try:
            if will_add:
                waitdata = query.get('waitdata', '').encode('ascii').strip()
                if waitdata != '' and not re.match(WAITDATA_RGX, waitdata):
                    req.log_error("Invalid waitdata '" + waitdata + "' when adding " + user + " to queue. \n")
                    return apache.HTTP_BAD_REQUEST
                addquser(user, waitdata, queue, room)
                lockAndWriteLog(",".join([str(time()), user, waitdata, room, "add"]))
            elif will_del:
                delquser(user, queue, room)
                lockAndWriteLog(",".join([str(time()), user, room, "del"]))
                req.write("success\n")
            elif staff_del:
                # owner is removing someone from the queue
                delquser(username, queue, room)
                lockAndWriteLog(",".join([str(time()), user, username, room, "staffdel"]))
                req.write("success\n")
            else:
                req.log_error("Invalid action in querychecked: " + query.get('action', None) + "\n")
                return apache.HTTP_BAD_REQUEST
        except sqlite3.IntegrityError: 
            req.log_error("IntegrityError: Error adding/removing station %d for user %s in room %s from %s\r\n" % (user, room, ip))
            return apache.HTTP_INTERNAL_SERVER_ERROR
        except Exception as e:
            req.log_error("Error in querychecked: action %s, station %d for user %s in room %s from %s\r\n" % (action, user, room, ip))
            req.log_error(str(e))
            return apache.OK
        return apache.OK
    #
    # Otherwise it is waiting for updates
    #
    elif 'sseupdate' in query:
        room = query.get('room', '').encode('ascii').strip()
        if room == '' or "room"+room not in rooms:
            req.log_error("Room %s not found in database" % room)
            return apache.HTTP_BAD_REQUEST
        req.headers_out['Cache-Control'] = 'no-cache;public'
        req.content_type = "text/event-stream;charset=UTF-8"
        req.send_http_header()
        req.write("data: %s\n\r" % json.dumps(getusers("", room)))
        global wm
        wm = pyinotify.WatchManager()
        notifier = pyinotify.Notifier(wm, EventHandler(room), timeout=30*1000)
        wdd = wm.add_watch(room_db, pyinotify.IN_MODIFY, rec=True)
        # start pyinotify
        while True:
            notifier.process_events()
            while notifier.check_events():
                # above line returns after 30 seconds or if room is updated
                notifier.read_events()
                notifier.process_events()
            try:
                req.write("data: %s\n\r" % json.dumps(getusers("", room)))
            except:
                wm.close()
                try:
                    sys.exit(0)
                except:
                    os._exit(0)
    else:
        req.content_type = "text/plain"
        req.send_http_header()
        req.write("Invalid request.")
        return apache.OK
