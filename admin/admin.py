#! /usr/bin/env python3
import os, sys, re
from mod_python import apache, util
from json import dumps
import pyinotify
import sqlite3

ROOM_RGX = r'^[A-Z0-9]{5}$'

class DBConnection:
    def __init__(self, room_db):
        self.conn = sqlite3.connect(room_db)
        self.cur = self.conn.cursor()
    def __enter__(self):
        return [self.conn, self.cur]
    def __exit__(self, type, value, traceback):
        self.conn.commit()
        self.conn.close()

def getowners(room_db, room):
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

try:
    private = os.environ['DOCUMENT_ROOT'] + os.environ['CONTEXT_PREFIX'] + '/private/queup/'
except KeyError:
    # os.environ['DOCUMENT_ROOT'] is not set by CGI resulting in a exception
    # so we are being invoked by mod_python, so we need different env vars
    private = os.environ['HOME'] + '/private/queup/'

def getdblog(room):
    with open(private + "room.log") as f:
        data = [x.split(",") for x in f.read().split("\n") if room in x]
    return data

def handler(req):
    # initialize some variables
    user = req.user
    query = util.FieldStorage(req)
    ip = req.useragent_ip

    class EventHandler(pyinotify.ProcessEvent):
        def process_IN_MODIFY(self, event):
            try:
                data = json.dumps(getdblog(room))
                sys.stderr.write("writes out - " + data + "\r\n")
                sys.stderr.flush()
                req.write("data: %s\n\r" % data)
            except:
                pass

    # now check our variables
    accepted_keys = ['sseupdate', 'log', 'fulllog']
    querychecked = any([x in query for x in accepted_keys]) and 'room' in query
    room = query.get("room", "")
    
    # check if room exists
    if not os.path.exists(private + "rooms/" + room + ".db"):
        return apache.HTTP_NOT_FOUND
    room_db = private + "rooms/" + room + ".db"
    
    # if user is not owner of room, return 403
    if user not in getowners(room_db, room):
        return apache.HTTP_FORBIDDEN
    
    # this section handles enabling any student to join a room
    if querychecked and 'log' in query:
        room = query.get("room", "")
        req.content_type = "application/json"
        req.send_http_header()
        req.write(dumps(getdblog(room)[-50:]))
        return apache.OK
    elif querychecked and 'fulllog' in query:
        room = query.get("room", "")
        req.content_type = "application/json"
        req.send_http_header()
        req.write(dumps(getdblog(room)))
        return apache.OK
    elif querychecked and 'sseupdate' in query:
        room = query.get("room", "")
        req.headers_out['Cache-Control'] = 'no-cache;public'
        req.content_type = "text/event-stream;charset=UTF-8"
        req.send_http_header()
        req.write("\n\r")
        global wm
        wm = pyinotify.WatchManager()
        notifier = pyinotify.Notifier(wm, EventHandler(), timeout=30*1000)
        wdd = wm.add_watch(private + 'rooms/' + room + '.db', pyinotify.IN_MODIFY, rec=True)
        # start pyinotify
        while True:
            notifier.process_events()
            while notifier.check_events():
                # above line returns after 30 seconds or if queue is updated
                notifier.read_events()
                notifier.process_events()
            try:
                req.write("data: %s\n\r" % dumps(getdblog(room)[-50:]))
            except:
                wm.close()
                try:
                    sys.exit(0)
                except:
                    os._exit(0)
    # invalid request
    else:
        return apache.HTTP_BAD_REQUEST
